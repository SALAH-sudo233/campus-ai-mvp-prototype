import os
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
    def test_v11_tokens_backfill_deterministically_without_losing_orphan_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "v11.sqlite")
            original = migrations.MIGRATIONS
            try:
                migrations.MIGRATIONS = original[:-1]
                migrations.migrate(path)
            finally:
                migrations.MIGRATIONS = original
            with sqlite3.connect(path) as conn:
                user_id = conn.execute("SELECT user_id FROM org_memberships WHERE student_id = '2024001001'").fetchone()[0]
                missing_user = conn.execute("SELECT user_id FROM org_memberships WHERE student_id = '2024001002'").fetchone()[0]
                for ref, created_at in (("legacy-first", 1), ("legacy-second", 2)):
                    conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at, user_id, org_id) "
                                 "VALUES (?, 'email', ?, '00', '00', 123, 0, ?, ?, ?)",
                                 (ref, ref + "@example.edu.cn", created_at, user_id, PILOT))
                for token_id, owner in (("legacy-valid", user_id), ("legacy-orphan", missing_user)):
                    conn.execute("INSERT INTO pat_tokens (id, user_id, token_hash, token_hint, label, role_snapshot, org_snapshot, "
                                 "created_at, created_by, expires_at, request_id) "
                                 "VALUES (?, ?, ?, 'legacy', 'legacy', 'org_member', ?, 1, ?, 9999999999, ?)",
                                 (token_id, owner, token_id, PILOT, owner, token_id))
            self.assertEqual(migrations.migrate(path), [12])
            self.assertEqual(migrations.migrate(path), [])
            with sqlite3.connect(path) as conn:
                rows = dict(conn.execute("SELECT id, owner_ref FROM pat_tokens"))
                self.assertEqual(rows, {"legacy-valid": "legacy-first", "legacy-orphan": None})
                self.assertEqual(conn.execute("SELECT balance FROM students WHERE owner_ref = 'legacy-second'").fetchone()[0], 123)


if __name__ == "__main__":
    unittest.main()
