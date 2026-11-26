import hashlib
import os
import secrets
import sqlite3
import tempfile
import unittest

import config
import migrations
from identity import owner_ref_of
from tests.support import DemoServer, login, register


PILOT = config.PILOT_ORG["id"]


class PatIdentityHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = DemoServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.platform = self.server.admin_token()
        code, self.student = register(self.server, "2024001085")
        self.assertEqual(code, 201, self.student)
        with sqlite3.connect(self.server.db_path) as conn:
            self.user_id = conn.execute("SELECT user_id FROM students WHERE identity = '2024001085'").fetchone()[0]

    def mint(self, session, request_id="bound-identity"):
        code, result = self.server.post("/api/me/tokens", {"requestId": request_id}, session)
        self.assertEqual(code, 201, result)
        return result

    def other_org_session(self, role="org_operator"):
        code, org = self.server.post("/api/orgs", {"name": "绑定学院", "slug": "pat-bound-org"}, self.platform)
        self.assertEqual(code, 201, org)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, name, joined_at) "
                         "VALUES (?, ?, ?, 'active', 'bound-member', '绑定成员', 1)", (org["id"], self.user_id, role))
        code, logged_in = login(self.server, "2024001085", org_id=org["id"])
        self.assertEqual(code, 200, logged_in)
        return org["id"], logged_in["token"]

    def test_profile_uses_bound_org_and_role_not_registered_org(self):
        org_id, session = self.other_org_session()
        pat = self.mint(session)
        for token in (session, pat["token"]):
            code, profile = self.server.get("/api/me", token)
            self.assertEqual(code, 200, profile)
            self.assertEqual(profile["org"]["id"], org_id)
            self.assertEqual(profile["role"], "org_operator")
            self.assertEqual(profile["identity"], "2024001085")

    def test_rotation_uses_bound_admission_even_if_registration_org_disabled(self):
        org_id, session = self.other_org_session("org_member")
        pat = self.mint(session)
        code, updated = self.server.patch("/api/orgs/%s/members/%s" % (PILOT, self.user_id),
                                          {"admissionStatus": "disabled"}, self.platform)
        self.assertEqual(code, 200, updated)
        code, result = self.server.post("/api/key/rotate", {"requestId": "bound-rotation"}, pat["token"])
        self.assertEqual(code, 200, result)
        self.assertTrue(result.get("plaintext"))
        code, profile = self.server.get("/api/me", pat["token"])
        self.assertEqual(code, 200, profile)
        self.assertEqual(profile["org"]["id"], org_id)
        self.assertEqual(profile["admissionStatus"], "active")

    def test_bound_pending_org_cannot_rotate_using_active_registration_org(self):
        org_id, session = self.other_org_session("org_member")
        pat = self.mint(session)
        code, updated = self.server.patch("/api/orgs/%s/members/%s" % (org_id, self.user_id),
                                          {"admissionStatus": "pending_review"}, self.platform)
        self.assertEqual(code, 200, updated)
        self.assertEqual(self.server.post("/api/key/rotate", {"requestId": "pending-bound"}, pat["token"])[0], 403)
        code, profile = self.server.get("/api/me", pat["token"])
        self.assertEqual(code, 200, profile)
        self.assertEqual(profile["admissionStatus"], "pending_review")
        self.assertEqual(profile["org"]["id"], org_id)

    def test_platform_pat_retains_issuing_org_with_multiple_memberships(self):
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("INSERT INTO platform_admins (user_id, created_at) VALUES (?, 1)", (self.user_id,))
        org_id, session = self.other_org_session()
        pat = self.mint(session)
        code, profile = self.server.get("/api/me", pat["token"])
        self.assertEqual(code, 200, profile)
        self.assertEqual(profile["role"], "platform_admin")
        self.assertEqual(profile["org"]["id"], org_id)
        for target in (org_id, PILOT):
            self.assertEqual(self.server.get("/api/orgs/%s/dashboard" % target, pat["token"])[0], 200)

    def test_multi_identity_pat_persists_owner_and_never_uses_oldest_account(self):
        code, email = register(self.server, "2024001085@example.edu.cn", mode="email")
        self.assertEqual(code, 201, email)
        pat = self.mint(email["token"])
        email_ref = owner_ref_of("email", "2024001085@example.edu.cn")
        with sqlite3.connect(self.server.db_path) as conn:
            stored = conn.execute("SELECT owner_ref FROM pat_tokens WHERE id = ?", (pat["item"]["id"],)).fetchone()[0]
            self.assertEqual(stored, email_ref)
            conn.execute("UPDATE students SET created_at = 0 WHERE mode = 'student' AND user_id = ?", (self.user_id,))
        code, profile = self.server.get("/api/me", pat["token"])
        self.assertEqual(code, 200, profile)
        self.assertEqual(profile["identity"], "2024001085@example.edu.cn")
        code, consumed = self.server.post("/api/consume", {"amount": 19, "requestId": "email-only"}, pat["token"])
        self.assertEqual(code, 200, consumed)
        self.assertEqual(consumed["mode"], "email")
        self.assertEqual(consumed["balance"], email["balance"] - 19)
        self.assertEqual(self.server.get("/api/me", self.student["token"])[1]["balance"], self.student["balance"])
        code, rotated = self.server.post("/api/key/rotate", {"requestId": "email-key"}, pat["token"])
        self.assertEqual(code, 200, rotated)
        with sqlite3.connect(self.server.relay_path) as conn:
            owner = conn.execute("SELECT owner_ref FROM relay_keys WHERE key_hint = ? AND revoked_at IS NULL",
                                 (rotated["hint"],)).fetchone()[0]
            self.assertEqual(owner, email_ref)

    def test_missing_bound_identity_never_falls_back_to_another_identity(self):
        code, email = register(self.server, "2024001085@example.edu.cn", mode="email")
        self.assertEqual(code, 201, email)
        pat = self.mint(email["token"])
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE students SET user_id = NULL WHERE mode = 'email' AND user_id = ?", (self.user_id,))
        self.assertEqual(self.server.get("/api/me", pat["token"])[0], 401)
        self.assertEqual(self.server.get("/api/me", self.student["token"])[0], 200)


class PatIdentityMigrationTests(unittest.TestCase):
    def seed_v11_tokens(self, path):
        original = migrations.MIGRATIONS
        try:
            migrations.MIGRATIONS = original[:-1]
            migrations.migrate(path)
        finally:
            migrations.MIGRATIONS = original
        with sqlite3.connect(path) as conn:
            user_ids = [conn.execute("SELECT user_id FROM org_memberships WHERE student_id = ?",
                                     (student_id,)).fetchone()[0]
                        for student_id in ("2024001001", "2024001002", "2024001003", "2024001004")]
            other_org = migrations.ensure_org(conn, "legacy-pat-org", "历史令牌学院")
            conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, name, joined_at) "
                         "VALUES (?, ?, 'org_member', 'active', 'legacy-member', '历史成员', 1)",
                         (other_org, user_ids[0]))
            accounts = (("legacy-first", user_ids[0], PILOT, 1, 123),
                        ("legacy-second", user_ids[0], other_org, 2, 456),
                        ("ambiguous-first", user_ids[2], PILOT, 1, 789),
                        ("ambiguous-second", user_ids[2], PILOT, 2, 789),
                        ("legacy-null-org", user_ids[3], None, 1, 123))
            for owner_ref, user_id, org_id, created_at, balance in accounts:
                conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at, user_id, org_id) "
                             "VALUES (?, 'email', ?, '00', '00', ?, 0, ?, ?, ?)",
                             (owner_ref, owner_ref + "@example.edu.cn", balance, created_at, user_id, org_id))
            tokens = {}
            for token_id, user_id, org_id in (("legacy-valid", user_ids[0], other_org),
                                               ("legacy-first-org", user_ids[0], PILOT),
                                               ("legacy-orphan", user_ids[1], PILOT),
                                               ("legacy-ambiguous", user_ids[2], PILOT),
                                               ("legacy-unmatched", user_ids[3], PILOT),
                                               ("legacy-null", user_ids[3], None)):
                tokens[token_id] = "cpat_" + secrets.token_urlsafe(32)
                conn.execute("INSERT INTO pat_tokens (id, user_id, token_hash, token_hint, label, role_snapshot, org_snapshot, "
                             "created_at, created_by, expires_at, request_id) "
                             "VALUES (?, ?, ?, 'legacy', 'legacy', 'org_member', ?, 1, ?, 9999999999, ?)",
                             (token_id, user_id, hashlib.sha256(tokens[token_id].encode()).hexdigest(),
                              org_id, user_id, token_id))
        return tokens, other_org

    def test_v11_tokens_backfill_only_unique_accounts_in_snapshot_org_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "v11.sqlite")
            self.seed_v11_tokens(path)
            self.assertEqual(migrations.migrate(path), [12])
            self.assertEqual(migrations.migrate(path), [])
            with sqlite3.connect(path) as conn:
                rows = dict(conn.execute("SELECT id, owner_ref FROM pat_tokens"))
                self.assertEqual(rows, {"legacy-valid": "legacy-second", "legacy-first-org": "legacy-first",
                                        "legacy-orphan": None, "legacy-ambiguous": None,
                                        "legacy-unmatched": None, "legacy-null": None})
                self.assertEqual(dict(conn.execute("SELECT owner_ref, balance FROM students WHERE mode = 'email'")),
                                 {"legacy-first": 123, "legacy-second": 456, "ambiguous-first": 789,
                                  "ambiguous-second": 789, "legacy-null-org": 123})

    def test_migrated_pat_uses_snapshot_org_account_and_rejects_ambiguous_owners_over_http(self):
        server = DemoServer()
        tokens, org_id = self.seed_v11_tokens(server.db_path)
        with server:
            code, profile = server.get("/api/me", tokens["legacy-valid"])
            self.assertEqual(code, 200, profile)
            self.assertEqual(profile["org"]["id"], org_id)
            self.assertEqual(profile["identity"], "legacy-second@example.edu.cn")
            code, rotated = server.post("/api/key/rotate", {"requestId": "migrated-bound-key"}, tokens["legacy-valid"])
            self.assertEqual(code, 200, rotated)
            with sqlite3.connect(server.relay_path) as conn:
                owner = conn.execute("SELECT owner_ref FROM relay_keys WHERE key_hint = ? AND revoked_at IS NULL",
                                     (rotated["hint"],)).fetchone()[0]
                self.assertEqual(owner, "legacy-second")
            for token_id in ("legacy-ambiguous", "legacy-unmatched", "legacy-orphan", "legacy-null"):
                with self.subTest(token_id=token_id):
                    self.assertEqual(server.get("/api/me", tokens[token_id])[0], 401)
                    self.assertEqual(server.post("/api/key/rotate", {"requestId": token_id}, tokens[token_id])[0], 401)
            with sqlite3.connect(server.db_path) as conn:
                self.assertEqual(conn.execute("SELECT balance FROM students WHERE owner_ref = 'legacy-first'").fetchone()[0], 123)

    def test_platform_legacy_null_org_pat_requires_reissue_but_session_and_new_pat_work(self):
        server = DemoServer()
        self.seed_v11_tokens(server.db_path)
        legacy_pat = "cpat_" + secrets.token_urlsafe(32)
        session = secrets.token_urlsafe(32)
        with sqlite3.connect(server.db_path) as conn:
            user_id = migrations._new_user(conn, "历史平台管理员")
            conn.execute("INSERT INTO platform_admins (user_id, created_at) VALUES (?, 1)", (user_id,))
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at, user_id, org_id) "
                         "VALUES ('legacy-platform', 'email', 'legacy-platform@example.edu.cn', '00', '00', 123, 0, 1, ?, NULL)",
                         (user_id,))
            conn.execute("INSERT INTO sessions (token_hash, owner_ref, expires_at, user_id, org_id) "
                         "VALUES (?, 'legacy-platform', 9999999999, ?, NULL)",
                         (hashlib.sha256(session.encode()).hexdigest(), user_id))
            conn.execute("INSERT INTO pat_tokens (id, user_id, token_hash, token_hint, label, role_snapshot, org_snapshot, "
                         "created_at, created_by, expires_at, request_id) "
                         "VALUES ('legacy-platform-pat', ?, ?, 'legacy', 'legacy', 'platform_admin', NULL, 1, ?, "
                         "9999999999, 'legacy-platform-pat')",
                         (user_id, hashlib.sha256(legacy_pat.encode()).hexdigest(), user_id))
        with server:
            code, profile = server.get("/api/me", session)
            self.assertEqual(code, 200, profile)
            self.assertEqual(profile["role"], "platform_admin")
            self.assertIsNone(profile["org"])
            for path in ("/api/me", "/api/orgs"):
                with self.subTest(path=path):
                    code, denied = server.get(path, legacy_pat)
                    self.assertEqual(code, 401, denied)
                    self.assertEqual(denied["error"], "个人访问令牌已失效，请重新签发")
            self.assertEqual(server.post("/api/key/rotate", {"requestId": "legacy-platform-key"}, legacy_pat)[0], 401)
            with sqlite3.connect(server.db_path) as conn:
                row = conn.execute("SELECT owner_ref, last_used_at FROM pat_tokens WHERE id = 'legacy-platform-pat'").fetchone()
                self.assertEqual(row, (None, None))
                self.assertEqual(conn.execute("SELECT balance FROM students WHERE owner_ref = 'legacy-platform'").fetchone()[0], 123)
            code, issued = server.post("/api/me/tokens", {"requestId": "platform-reissued"}, session)
            self.assertEqual(code, 201, issued)
            self.assertIsNone(issued["item"]["orgSnapshot"])
            code, renewed = server.get("/api/me", issued["token"])
            self.assertEqual(code, 200, renewed)
            self.assertEqual(renewed["role"], "platform_admin")
            self.assertEqual(renewed["identity"], "legacy-platform@example.edu.cn")
            self.assertIsNone(renewed["org"])
            self.assertEqual(server.get("/api/orgs", issued["token"])[0], 200)


if __name__ == "__main__":
    unittest.main()
