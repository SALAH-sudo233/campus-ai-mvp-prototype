import concurrent.futures
import hashlib
import json
import secrets
import sqlite3
import time
import unittest

import authz
import config
import migrations
import pat_service
from tests.support import DemoServer, login, register

PILOT = config.PILOT_ORG["id"]


class PatHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = DemoServer().__enter__()
        code, self.member = register(self.server, "2024001091")
        self.assertEqual(code, 201)
        self.session = self.member["token"]
        with sqlite3.connect(self.server.db_path) as conn:
            self.user_id = conn.execute("SELECT user_id FROM students WHERE identity = '2024001091'").fetchone()[0]

    def tearDown(self):
        self.server.__exit__(None, None, None)

    def mint(self, request_id=None, token=None, **fields):
        data = {"requestId": request_id or secrets.token_hex(8), **fields}
        return self.server.post("/api/me/tokens", data, token or self.session)

    def update_membership(self, **fields):
        with sqlite3.connect(self.server.db_path) as conn:
            for field, value in fields.items():
                self.assertIn(field, ("role", "admission_status", "org_id"))
                conn.execute("UPDATE org_memberships SET " + field + " = ? WHERE user_id = ?",
                             (value, self.user_id))

    def nonpilot_session(self, role=authz.ORG_MEMBER):
        admin = self.server.admin_token()
        code, org = self.server.post("/api/orgs", {"name": "第二学院", "slug": "second-pat"}, admin)
        self.assertEqual(code, 201)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, "
                         "external_ref, name, joined_at) VALUES (?, ?, ?, 'active', ?, ?, ?)",
                         (org["id"], self.user_id, role, "2024001091", "多组织成员", time.time()))
        code, session = login(self.server, "2024001091", org_id=org["id"])
        self.assertEqual(code, 200)
        return org["id"], session["token"]

    def test_nonpilot_pat_reads_account_profile(self):
        _, session = self.nonpilot_session()
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        code, profile = self.server.get("/api/me", created["token"])
        self.assertEqual(code, 200)
        self.assertEqual(profile["identity"], "2024001091")
        self.assertEqual(profile["balance"], self.member["balance"])

    def test_nonpilot_pat_consumes_account_balance(self):
        _, session = self.nonpilot_session()
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        code, profile = self.server.post("/api/consume", {"amount": 17, "requestId": "nonpilot-use"},
                                         created["token"])
        self.assertEqual(code, 200)
        self.assertEqual(profile["balance"], self.member["balance"] - 17)
        self.assertEqual(profile["spent"], self.member["spent"] + 17)
        with sqlite3.connect(self.server.db_path) as conn:
            balance, spent = conn.execute("SELECT balance, spent FROM students WHERE user_id = ?",
                                           (self.user_id,)).fetchone()
        self.assertEqual((balance, spent), (profile["balance"], profile["spent"]))

    def test_nonpilot_pat_rotates_account_key(self):
        _, session = self.nonpilot_session()
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        code, key = self.server.post("/api/key/rotate", {"requestId": "nonpilot-rotate"}, created["token"])
        self.assertEqual(code, 200)
        self.assertEqual(key["status"], "active")
        self.assertTrue(key["plaintext"])
        with sqlite3.connect(self.server.relay_path) as conn:
            active_keys = conn.execute("SELECT COUNT(*) FROM relay_keys WHERE user_id = ? AND revoked_at IS NULL",
                                       (self.user_id,)).fetchone()[0]
        self.assertEqual(active_keys, 1)

    def test_platform_pat_without_membership_reads_account_profile(self):
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("INSERT INTO platform_admins (user_id, created_at) VALUES (?, ?)",
                         (self.user_id, time.time()))
            conn.execute("DELETE FROM org_memberships WHERE user_id = ?", (self.user_id,))
        code, created = self.mint()
        self.assertEqual(code, 201)
        self.assertIsNone(created["item"]["orgSnapshot"])
        self.assertEqual(created["item"]["roleSnapshot"], authz.PLATFORM_ADMIN)
        code, profile = self.server.get("/api/me", created["token"])
        self.assertEqual(code, 200)
        self.assertEqual(profile["identity"], "2024001091")
        self.assertEqual(profile["role"], authz.PLATFORM_ADMIN)
        self.assertIsNone(profile["org"])

    def test_pat_without_account_identity_returns_401(self):
        code, created = self.mint()
        self.assertEqual(code, 201)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE students SET user_id = NULL WHERE user_id = ?", (self.user_id,))
        for method, path, data in (("GET", "/api/me", None), ("GET", "/api/me/tokens", None),
                                   ("POST", "/api/consume", {"amount": 17, "requestId": "missing-use"}),
                                   ("POST", "/api/key/rotate", {"requestId": "missing-rotate"})):
            with self.subTest(path=path):
                code, body = self.server.request(method, path, data, created["token"])
                self.assertEqual(code, 401)
                self.assertIn("重新签发", body["error"])
        with sqlite3.connect(self.server.db_path) as conn:
            last_used = conn.execute("SELECT last_used_at FROM pat_tokens WHERE id = ?",
                                     (created["item"]["id"],)).fetchone()[0]
        self.assertIsNone(last_used)

    def test_platform_pat_without_account_identity_returns_401(self):
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("INSERT INTO platform_admins (user_id, created_at) VALUES (?, ?)",
                         (self.user_id, time.time()))
            conn.execute("DELETE FROM org_memberships WHERE user_id = ?", (self.user_id,))
            conn.execute("UPDATE students SET user_id = NULL WHERE user_id = ?", (self.user_id,))
        code, created = self.mint()
        self.assertEqual(code, 201)
        for path in ("/api/me", "/api/me/tokens"):
            with self.subTest(path=path):
                code, body = self.server.get(path, created["token"])
                self.assertEqual(code, 401)
                self.assertIn("重新签发", body["error"])

    def test_nonpilot_pat_preserves_issuing_org_for_work_and_audit(self):
        org_id, session = self.nonpilot_session()
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        self.assertEqual(created["item"]["orgSnapshot"], org_id)
        with sqlite3.connect(self.server.db_path) as conn:
            stored = conn.execute("SELECT org_snapshot, role_snapshot FROM pat_tokens WHERE id = ?",
                                  (created["item"]["id"],)).fetchone()
        self.assertEqual(stored, (org_id, authz.ORG_MEMBER))
        code, work = self.server.post("/api/works", {"title": "第二学院作品", "summary": "多组织 PAT 回归",
                                                   "category": "学习"}, created["token"])
        self.assertEqual(code, 201)
        with sqlite3.connect(self.server.db_path) as conn:
            stored_org = conn.execute("SELECT org_id FROM works WHERE work_id = ?",
                                      (work["workId"],)).fetchone()[0]
            audit_org = conn.execute("SELECT org_id FROM audit_events WHERE action = 'work.submitted' "
                                     "AND resource_id = ?", (work["workId"],)).fetchone()[0]
        self.assertEqual(stored_org, org_id)
        self.assertEqual(audit_org, org_id)

    def test_nonpilot_admin_pat_uses_bound_org_live_role(self):
        org_id, session = self.nonpilot_session(authz.ORG_ADMIN)
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        pat = created["token"]
        self.assertEqual(self.server.get("/api/orgs/%s/dashboard" % org_id, pat)[0], 200)
        self.assertEqual(self.server.get("/api/orgs/%s/dashboard" % PILOT, pat)[0], 403)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE org_memberships SET role = ? WHERE user_id = ? AND org_id = ?",
                         (authz.ORG_ADMIN, self.user_id, PILOT))
            conn.execute("UPDATE org_memberships SET role = ? WHERE user_id = ? AND org_id = ?",
                         (authz.ORG_MEMBER, self.user_id, org_id))
        for target in (org_id, PILOT):
            code, body = self.server.get("/api/orgs/%s/dashboard" % target, pat)
            self.assertEqual(code, 401)
            self.assertIn("重新签发", body["error"])

    def test_nonpilot_member_pat_does_not_inherit_pilot_role_or_admission(self):
        self.update_membership(role=authz.ORG_ADMIN)
        org_id, session = self.nonpilot_session()
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        pat = created["token"]
        for target in (org_id, PILOT):
            self.assertEqual(self.server.get("/api/orgs/%s/dashboard" % target, pat)[0], 403)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE org_memberships SET admission_status = 'disabled' "
                         "WHERE user_id = ? AND org_id = ?", (self.user_id, PILOT))
        self.assertEqual(self.server.post("/api/works", {"title": "独立准入", "summary": "只读本组织准入",
                                                       "category": "学习"}, pat)[0], 201)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE org_memberships SET admission_status = 'disabled' "
                         "WHERE user_id = ? AND org_id = ?", (self.user_id, org_id))
        self.assertEqual(self.server.get("/api/works", pat)[0], 401)

    def test_removed_bound_membership_does_not_fall_back_to_pilot(self):
        org_id, session = self.nonpilot_session()
        code, created = self.mint(token=session)
        self.assertEqual(code, 201)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("DELETE FROM org_memberships WHERE user_id = ? AND org_id = ?",
                         (self.user_id, org_id))
        code, body = self.server.get("/api/works", created["token"])
        self.assertEqual(code, 401)
        self.assertIn("重新签发", body["error"])
        self.assertEqual(self.server.get("/api/works", self.session)[0], 200)

    def test_mint_list_revoke_and_one_time_plaintext(self):
        before = time.time()
        code, created = self.mint("once")
        self.assertEqual(code, 201)
        plaintext = created["token"]
        self.assertTrue(plaintext.startswith("cpat_"))
        self.assertGreaterEqual(created["item"]["expiresAt"], before + 30 * 86400)
        self.assertEqual(self.server.get("/api/me", plaintext)[0], 200)
        code, listing = self.server.get("/api/me/tokens", self.session)
        self.assertEqual(code, 200)
        self.assertEqual(len(listing["items"]), 1)
        self.assertTrue(plaintext not in json.dumps(listing), "listing exposed plaintext")
        self.assertNotIn("token_hash", listing["items"][0])
        self.assertIsNotNone(listing["items"][0]["lastUsedAt"])
        with sqlite3.connect(self.server.db_path) as conn:
            stored_hash = conn.execute("SELECT token_hash FROM pat_tokens").fetchone()[0]
            self.assertTrue(stored_hash == hashlib.sha256(plaintext.encode()).hexdigest())
            self.assertTrue(plaintext not in "\n".join(conn.iterdump()), "database exposed plaintext")
        path = "/api/me/tokens/" + created["item"]["id"]
        code, revoked = self.server.request("DELETE", path, token=self.session)
        self.assertEqual(code, 200)
        self.assertEqual(revoked["item"]["status"], "revoked")
        self.assertEqual(self.server.get("/api/me", plaintext)[0], 401)
        self.assertEqual(self.server.request("DELETE", path, token=self.session)[0], 200)
        self.assertEqual(self.server.get("/api/me", self.session)[0], 200)

    def test_replay_never_returns_plaintext_even_after_revocation(self):
        code, first = self.mint("stable")
        self.assertEqual(code, 201)
        code, replay = self.mint("stable")
        self.assertEqual(code, 200)
        self.assertTrue(replay["replayed"])
        self.assertNotIn("token", replay)
        self.assertEqual(first["item"]["id"], replay["item"]["id"])
        self.server.request("DELETE", "/api/me/tokens/" + first["item"]["id"], token=self.session)
        self.assertNotIn("token", self.mint("stable")[1])

    def test_concurrent_replay_creates_one_token_and_reveals_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.mint("concurrent"), range(4)))
        self.assertEqual(sorted(code for code, _ in responses), [200, 200, 200, 201])
        self.assertEqual(sum("token" in result for _, result in responses), 1)
        self.assertEqual(len({result["item"]["id"] for _, result in responses}), 1)

    def test_limit_is_atomic_and_replay_is_available_at_limit(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda number: self.mint("limit-%d" % number), range(12)))
        self.assertEqual(sum(code == 201 for code, _ in results), 10)
        self.assertEqual(sum(code == 409 for code, _ in results), 2)
        successful = next(index for index, (code, _) in enumerate(results) if code == 201)
        self.assertEqual(self.mint("limit-%d" % successful)[0], 200)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE pat_tokens SET expires_at = 1 WHERE request_id = ?", ("limit-%d" % successful,))
        self.assertEqual(self.mint()[0], 201)

    def test_expired_unknown_and_unauthenticated_tokens(self):
        _, created = self.mint()
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE pat_tokens SET expires_at = ?", (time.time() - 1,))
        for path in ("/api/me", "/api/me/tokens", "/api/orgs/%s/dashboard" % PILOT):
            self.assertEqual(self.server.get(path, created["token"])[0], 401)
        code, body = self.server.get("/api/me", "cpat_" + secrets.token_urlsafe(32))
        self.assertEqual(code, 401)
        self.assertIn("重新签发", body["error"])
        self.assertEqual(self.server.get("/api/me/tokens")[0], 401)
        self.assertEqual(self.server.post("/api/me/tokens", {"requestId": "none"})[0], 401)
        listing = self.server.get("/api/me/tokens", self.session)[1]
        self.assertEqual(listing["items"][0]["status"], "expired")

    def test_invalid_expiration_label_and_idempotency_keys(self):
        for days in (0, 91, True, 1.5, "30", None, [], {}):
            self.assertEqual(self.mint(expiresInDays=days)[0], 400)
        for label in ("", " ", 1, None, "a" * 81):
            self.assertEqual(self.mint(label=label)[0], 400)
        for key in (None, True, [], "", " " * 2, "x" * 129):
            self.assertEqual(self.server.post("/api/me/tokens", {"requestId": key}, self.session)[0], 400)
        self.assertEqual(self.mint(expiresInDays=1)[0], 201)
        self.assertEqual(self.mint(expiresInDays=90)[0], 201)

    def test_token_ownership_and_missing_revoke(self):
        _, created = self.mint("same-key")
        _, other = register(self.server, "2024001092")
        code, other_token = self.mint("same-key", token=other["token"])
        self.assertEqual(code, 201)
        self.assertNotEqual(created["item"]["id"], other_token["item"]["id"])
        self.assertEqual(self.server.request("DELETE", "/api/me/tokens/" + created["item"]["id"],
                                             token=other["token"])[0], 404)
        self.assertEqual(self.server.request("DELETE", "/api/me/tokens/missing", token=self.session)[0], 404)

    def test_live_org_admin_permissions_and_downgrade(self):
        self.update_membership(role=authz.ORG_ADMIN)
        _, created = self.mint()
        pat = created["token"]
        self.assertEqual(self.server.get("/api/orgs/%s/dashboard" % PILOT, pat)[0], 200)
        self.assertEqual(self.server.get("/api/admin/accounts", pat)[0], 200)
        self.assertEqual(self.server.post("/api/admin/revoke", {"owner_ref": "stu_2024001091"}, pat)[0], 403)
        self.assertEqual(self.server.post("/api/orgs/%s/imports/preview" % PILOT, {}, pat)[0], 400)
        self.update_membership(role=authz.ORG_MEMBER)
        for path in ("/api/me", "/api/orgs/%s/dashboard" % PILOT):
            code, body = self.server.get(path, pat)
            self.assertEqual(code, 401)
            self.assertIn("重新签发", body["error"])
        self.assertEqual(self.server.get("/api/me", self.session)[0], 200)

    def test_org_move_invalidates_pat_bound_to_previous_membership(self):
        self.update_membership(role=authz.ORG_ADMIN)
        _, created = self.mint()
        admin = self.server.admin_token()
        code, new_org = self.server.post("/api/orgs", {"name": "第二学院", "slug": "second-pat"}, admin)
        self.assertEqual(code, 201)
        self.update_membership(org_id=new_org["id"])
        for org_id in (PILOT, new_org["id"]):
            code, body = self.server.get("/api/orgs/%s/dashboard" % org_id, created["token"])
            self.assertEqual(code, 401)
            self.assertIn("重新签发", body["error"])

    def test_pending_review_preserves_existing_allowed_range(self):
        _, created = self.mint()
        pat = created["token"]
        self.update_membership(admission_status="pending_review")
        self.assertEqual(self.server.get("/api/me", pat)[0], 200)
        self.assertEqual(self.server.get("/api/onboarding", pat)[0], 200)
        self.assertEqual(self.server.post("/api/onboarding", {"action": "start", "stepId": "team-basics"}, pat)[0], 200)
        self.assertEqual(self.server.get("/api/works", pat)[0], 403)
        self.assertEqual(self.server.post("/api/consume", {"amount": 1}, pat)[0], 403)
        self.assertEqual(self.server.post("/api/key/rotate", {}, pat)[0], 403)
        self.assertEqual(self.server.post("/api/logout", {}, pat)[0], 200)

    def test_disabled_membership_or_user_invalidates_immediately(self):
        _, created = self.mint()
        self.update_membership(admission_status="disabled")
        self.assertEqual(self.server.get("/api/me", created["token"])[0], 401)
        self.update_membership(admission_status="active")
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE users SET disabled_at = ? WHERE id = ?", (time.time(), self.user_id))
        self.assertEqual(self.server.get("/api/me", created["token"])[0], 401)

    def test_session_logout_pat_logout_and_self_revocation_are_independent(self):
        _, first = self.mint()
        _, second = self.mint()
        self.assertEqual(self.server.post("/api/logout", {}, first["token"])[0], 200)
        self.assertEqual(self.server.get("/api/me", first["token"])[0], 200)
        self.assertEqual(self.server.get("/api/me", self.session)[0], 200)
        self.assertEqual(self.server.post("/api/logout", {}, self.session)[0], 200)
        self.assertEqual(self.server.get("/api/me", self.session)[0], 401)
        self.assertEqual(self.server.get("/api/me", first["token"])[0], 200)
        _, relogin = login(self.server, "2024001091")
        self.assertEqual(self.server.request("DELETE", "/api/me/tokens/" + first["item"]["id"],
                                             token=first["token"])[0], 200)
        self.assertEqual(self.server.get("/api/me", first["token"])[0], 401)
        self.assertEqual(self.server.get("/api/me", second["token"])[0], 200)
        self.assertEqual(self.server.get("/api/me", relogin["token"])[0], 200)

    def test_real_work_flow_and_audit_identity(self):
        _, created = self.mint()
        pat = created["token"]
        code, work = self.server.post("/api/works", {"title": "PAT 作品", "summary": "用 agent 提交", "category": "学习"}, pat)
        self.assertEqual(code, 201)
        self.update_membership(role=authz.ORG_ADMIN)
        _, teacher_pat = self.mint()
        self.assertEqual(work["status"], "published")
        self.assertEqual(self.server.post("/api/orgs/%s/works/%s/review" % (PILOT, work["workId"]),
                                          {"decision": "approve"}, teacher_pat["token"])[0], 409)
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE works SET status = 'pending_review' WHERE work_id = ?", (work["workId"],))
        code, reviewed = self.server.post("/api/orgs/%s/works/%s/review" % (PILOT, work["workId"]),
                                          {"decision": "approve"}, teacher_pat["token"])
        self.assertEqual(code, 200)
        self.assertEqual(reviewed["status"], "published")
        self.assertEqual(self.server.post("/api/orgs/%s/works/%s/feature" % (PILOT, work["workId"]),
                                          {"featured": True}, teacher_pat["token"])[0], 200)
        self.assertEqual(self.server.post("/api/orgs/%s/works/%s/unpublish" % (PILOT, work["workId"]),
                                          {}, teacher_pat["token"])[0], 200)
        events = self.server.get("/api/orgs/%s/audit" % PILOT, teacher_pat["token"])[1]["events"]
        work_event = next(item for item in events if item["action"] == "work.submitted")
        self.assertEqual(work_event["metadata"]["authMethod"], "pat")
        self.assertEqual(work_event["metadata"]["patId"], created["item"]["id"])
        with sqlite3.connect(self.server.db_path) as conn:
            audits = [json.loads(row[0]) for row in conn.execute(
                "SELECT metadata_json FROM audit_events WHERE action = 'http.post'")]
        self.assertTrue(any(item.get("authMethod") == "session" for item in audits))
        self.assertTrue(any(item.get("authMethod") == "pat" and item.get("result") == "success" for item in audits))
        self.assertTrue(any(item.get("authMethod") == "pat" and item.get("status") == 409 for item in audits))
        self.assertTrue(pat not in json.dumps(events), "audit exposed plaintext")

    def test_static_toolkit_assets_are_downloadable_but_private_files_are_not(self):
        for path in ("/cli/campus-cli", "/skills/campus-platform/SKILL.md", "/agent-tools.js"):
            self.assertEqual(self.server.get(path)[0], 200)
        self.assertEqual(self.server.get("/pat_service.py")[0], 404)


class PermissionSnapshotTests(unittest.TestCase):
    def test_scope_or_capability_loss_is_invalid(self):
        self.assertTrue(pat_service.permissions_shrank(authz.ORG_ADMIN, authz.ORG_MEMBER))
        self.assertTrue(pat_service.permissions_shrank(authz.PLATFORM_ADMIN, authz.ORG_ADMIN))
        self.assertFalse(pat_service.permissions_shrank(authz.ORG_ADMIN, authz.PLATFORM_ADMIN))
        self.assertTrue(pat_service.permissions_shrank("unknown", authz.ORG_MEMBER))
        self.assertFalse(pat_service.permissions_shrank(authz.ORG_MEMBER, authz.ORG_MEMBER))

    def test_migration_version_is_reserved_for_b_line(self):
        self.assertEqual(next(item[:2] for item in migrations.MIGRATIONS if item[0] == 10),
                         (10, "personal-access-tokens"))
