"""New-API 协议、影子状态机、失败矩阵与演示入口门控。"""
import contextlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import config
import migrate
import relay
from identity import owner_ref_of
from tests.fake_new_api import FakeNewApi
from tests.support import DemoServer, login, register

PILOT = config.PILOT_ORG["id"]


@contextlib.contextmanager
def newapi_env(fake, timeout=2):
    values = {
        "NEW_API_ENABLED": "1",
        "NEW_API_BASE_URL": fake.base_url,
        "NEW_API_TOKEN": fake.token,
        "NEW_API_USER_ID": "1",
        "NEW_API_TIMEOUT": str(timeout),
    }
    with mock.patch.dict(os.environ, values, clear=False):
        yield


def client_for(fake, timeout=2):
    return relay.NewApiClient(relay.NewApiConfig(True, fake.base_url, fake.token, "1", timeout))


def relay_rows(path, owner_ref=None):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if owner_ref:
            return conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? ORDER BY created_at",
                                (owner_ref,)).fetchall()
        return conn.execute("SELECT * FROM relay_keys ORDER BY created_at").fetchall()
    finally:
        conn.close()


class ProtocolTests(unittest.TestCase):
    def test_create_list_get_key_and_delete(self):
        with FakeNewApi() as fake:
            client = client_for(fake)
            created = client.create_and_get("campus:u1:r1", 100000)
            self.assertGreater(created["id"], 0)
            self.assertTrue(created["key"].startswith("sk-test-"))
            listed = client.list_tokens()
            self.assertEqual([item["name"] for item in listed], ["campus:u1:r1"])
            self.assertNotIn(created["key"], json.dumps(listed))
            self.assertEqual(client.get_key(created["id"]), created["key"])
            self.assertTrue(client.delete_token(created["id"]))
            self.assertEqual(client.list_tokens(), [])

    def test_duplicate_and_lost_response_are_claimed_by_name(self):
        with FakeNewApi() as fake:
            client = client_for(fake)
            fake.fail_mode = "create_conflict_once"
            first = client.create_and_get("campus:u2:r1", 50000)
            self.assertEqual(fake.create_count, 1)
            self.assertEqual(fake.token_by_name("campus:u2:r1")["id"], first["id"])

            fake.fail_mode = "drop_create_response_once"
            with self.assertRaises(relay.NewApiError) as raised:
                client.create_and_get("campus:u2:r2", 50000)
            self.assertEqual(raised.exception.category, "upstream_5xx")
            self.assertIsNotNone(fake.token_by_name("campus:u2:r2"))
            claimed = client.create_and_get("campus:u2:r2", 50000)
            self.assertEqual(fake.create_count, 2)
            self.assertEqual(fake.token_by_name("campus:u2:r2")["id"], claimed["id"])

    def test_failure_matrix_categories_never_leak_body(self):
        with FakeNewApi() as fake:
            client = client_for(fake, timeout=0.2)
            fake.timeout_sleep = 0.5
            cases = (("auth_failed", "auth_failed"), ("rate_limited", "rate_limited"),
                     ("http_500", "upstream_5xx"), ("timeout", "timeout"), ("quota", "quota_exhausted"),
                     ("token_limit", "quota_exhausted"))
            for mode, category in cases:
                fake.fail_mode = mode
                with self.assertRaises(relay.NewApiError) as raised:
                    client.create_token("campus:fail:%s" % mode, 1)
                self.assertEqual(raised.exception.category, category)
                self.assertNotIn(fake.token, str(raised.exception))
            fake.fail_mode = "phantom_success"
            with self.assertRaises(relay.NewApiError) as raised:
                client.create_and_get("campus:phantom:r1", 1)
            self.assertEqual(raised.exception.category, "upstream_5xx")

    def test_list_masks_keys_and_paginates(self):
        with FakeNewApi() as fake:
            for index in range(105):
                fake.add_token("campus:page:%03d" % index, 10)
            tokens = client_for(fake).list_tokens()
            self.assertEqual(len(tokens), 105)
            self.assertTrue(all(item["name"].startswith("campus:") for item in tokens))


class ShadowStateTests(unittest.TestCase):
    def test_token_name_is_ascii_and_bounded(self):
        name = relay.token_name("学生-测试", "请求-一" * 20)
        self.assertTrue(name.startswith("campus:"))
        self.assertTrue(name.isascii())
        self.assertLessEqual(len(name), relay.TOKEN_NAME_LIMIT)

    def test_issue_replay_rotate_and_revoke(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            first = relay.issue_key(path, "u1", "r1", quota=100000)
            self.assertEqual(first["status"], "active")
            self.assertTrue(first["plaintext"])
            self.assertTrue(first["hint"])
            again = relay.issue_key(path, "u1", "r1", quota=100000)
            self.assertEqual(again["status"], "active")
            self.assertIsNone(again["plaintext"])
            token = fake.token_by_name("campus:u1:r1")
            self.assertEqual(token["remain_quota"], 100000)

            rotated = relay.rotate_key(path, "u1", "r2", quota=100000)
            self.assertEqual(rotated["status"], "active")
            self.assertTrue(rotated["plaintext"])
            self.assertIsNone(fake.token_by_name("campus:u1:r1"))
            self.assertIsNotNone(fake.token_by_name("campus:u1:r2"))
            self.assertEqual([row["state"] for row in relay_rows(path, "u1")], ["revoked", "active"])

            self.assertEqual(relay.revoke_by_owner(path, "u1"), "none")
            self.assertIsNone(fake.token_by_name("campus:u1:r2"))
            self.assertFalse(any(row["state"] == "active" for row in relay_rows(path, "u1")))

    def test_transient_failure_backoff_then_failed(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            fake.fail_mode = "http_500"
            first = relay.issue_key(path, "u2", "r1", quota=1000)
            self.assertEqual(first["status"], "pending")
            row = relay_rows(path, "u2")[0]
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(row["last_error"], "upstream_5xx")
            self.assertAlmostEqual(row["next_attempt_at"] - time.time(), 30, delta=3)

            for expected in (120, 600, 3600, 21600):
                conn = sqlite3.connect(path)
                conn.execute("UPDATE relay_keys SET next_attempt_at = 0 WHERE owner_ref = 'u2'")
                conn.commit()
                conn.close()
                relay.retry_due(path, owner_ref="u2", owner_refs=["u2"], limit_pending=1)
                row = relay_rows(path, "u2")[0]
                self.assertEqual(row["state"], "pending")
                self.assertAlmostEqual(row["next_attempt_at"] - time.time(), expected, delta=3)

            conn = sqlite3.connect(path)
            conn.execute("UPDATE relay_keys SET next_attempt_at = 0 WHERE owner_ref = 'u2'")
            conn.commit()
            conn.close()
            relay.retry_due(path, owner_ref="u2", owner_refs=["u2"], limit_pending=1)
            row = relay_rows(path, "u2")[0]
            self.assertEqual(row["state"], "failed")
            self.assertEqual(row["attempts"], relay.MAX_ATTEMPTS)
            self.assertIsNone(row["next_attempt_at"])

    def test_failure_matrix_statuses_and_quota_failed(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake, timeout=0.3):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            fake.timeout_sleep = 0.5
            for mode, expected in (("auth_failed", "pending"), ("rate_limited", "pending"),
                                   ("http_500", "pending"), ("timeout", "pending"),
                                   ("quota", "failed"), ("token_limit", "failed")):
                fake.fail_mode = mode
                result = relay.issue_key(path, "u-" + mode, "r1", quota=1000)
                self.assertEqual(result["status"], expected, mode)
                self.assertIsNone(result["plaintext"])
                row = relay_rows(path, "u-" + mode)[0]
                self.assertEqual(row["last_error"], "quota_exhausted" if expected == "failed" else
                                 ("timeout" if mode == "timeout" else
                                  "rate_limited" if mode == "rate_limited" else
                                  "auth_failed" if mode == "auth_failed" else "upstream_5xx"))

    def test_pending_lazy_retry_and_manual_reissue(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            fake.fail_mode = "auth_failed"
            relay.issue_key(path, "u3", "r1", quota=1000)
            fake.fail_mode = None
            relay.retry_due(path, owner_ref="u3", owner_refs=[], limit_pending=1)
            self.assertEqual(relay_rows(path, "u3")[0]["state"], "pending")

            conn = sqlite3.connect(path)
            conn.execute("UPDATE relay_keys SET next_attempt_at = 0 WHERE owner_ref = 'u3'")
            conn.commit()
            conn.close()
            result = relay.retry_due(path, owner_ref="u3", owner_refs=["u3"], limit_pending=1,
                                     allow_delivery_for="u3")
            self.assertEqual(result["plaintext"], fake.token_by_name("campus:u3:r1")["key"])
            self.assertEqual(relay_rows(path, "u3")[0]["state"], "active")
            count = fake.create_count
            self.assertEqual(relay.manual_reissue(path, "u3"), "active")
            self.assertEqual(fake.create_count, count)

    def test_stale_creating_claims_existing_token_without_duplicate(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            existing = fake.add_token("campus:u4:r1", 1000)
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, state, "
                         "issue_request_id, token_name, quota, attempts, next_attempt_at, created_at) "
                         "VALUES ('rk_stale', 'u4', ?, '', 'ch-newapi', 'creating', 'creating', 'r1', "
                         "'campus:u4:r1', 1000, 0, 0, ?)", (relay.digest("pending:rk_stale"), time.time() - 120))
            conn.commit()
            conn.close()
            result = relay.issue_key(path, "u4", "r1", quota=1000)
            self.assertEqual(result["status"], "active")
            self.assertEqual(result["plaintext"], existing["key"])
            self.assertEqual(fake.create_count, 0)
            self.assertEqual(relay_rows(path, "u4")[0]["provider_token_id"], existing["id"])

    def test_missing_provider_token_is_recreated(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            first = relay.issue_key(path, "u5", "r1", quota=1000)
            token = fake.token_by_name("campus:u5:r1")
            with fake.lock:
                fake.tokens.pop(token["id"])
            conn = sqlite3.connect(path)
            conn.execute("UPDATE relay_keys SET delivered_at = NULL WHERE owner_ref = 'u5'")
            conn.commit()
            conn.close()
            relay.retry_due(path, owner_ref="u5", allow_delivery_for="u5")
            self.assertEqual(relay_rows(path, "u5")[0]["state"], "pending")
            conn = sqlite3.connect(path)
            conn.execute("UPDATE relay_keys SET next_attempt_at = 0 WHERE owner_ref = 'u5'")
            conn.commit()
            conn.close()
            second = relay.retry_due(path, owner_ref="u5", owner_refs=["u5"], limit_pending=1,
                                     allow_delivery_for="u5")
            self.assertTrue(second["plaintext"])
            self.assertNotEqual(second["plaintext"], first["plaintext"])
            self.assertEqual(relay_rows(path, "u5")[0]["state"], "active")

    def test_legacy_active_without_provider_id_backfills_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, state, "
                         "issue_request_id, token_name, quota, attempts, created_at) "
                         "VALUES ('rk_legacy', 'u6', ?, 'old', 'ch-a', 'active', 'active', 'r1', "
                         "'campus:u6:r1', 1000, 0, ?)", (relay.digest("old"), time.time()))
            conn.commit()
            conn.close()
            self.assertEqual(relay.backfill_legacy(path), 1)
            row = relay_rows(path, "u6")[0]
            self.assertEqual(row["state"], "pending")
            self.assertIsNone(row["last_error"])

    def test_cutover_dry_run_then_delete_only_campus_prefix(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp:
            client = client_for(fake)
            fake.add_token("campus:u7:r1", 1000)
            fake.add_token("campus:u8:r1", 1000)
            fake.add_token("shared:keep-me", 9999)
            db_path = os.path.join(tmp, "relay.sqlite")
            relay.init(db_path)

            preview = relay.relay_cutover(client, relay_db=db_path, apply=False)
            self.assertEqual(preview["matched"], 2)
            self.assertEqual(preview["deleted"], 0)
            self.assertIsNotNone(fake.token_by_name("campus:u7:r1"))

            with self.assertRaises(ValueError):
                relay.relay_cutover(client, relay_db=db_path, apply=True, backup_dir=os.path.join(tmp, "b"),
                                    confirm="no")
            result = relay.relay_cutover(client, relay_db=db_path, apply=True,
                                         backup_dir=os.path.join(tmp, "b"), confirm="DELETE")
            self.assertEqual(result["deleted"], 2)
            self.assertEqual(result["failed"], [])
            self.assertTrue(os.path.exists(result["backup"]))
            self.assertTrue(os.path.exists(result["manifest"]))
            self.assertEqual(os.stat(result["manifest"]).st_mode & 0o777, 0o600)
            self.assertIsNone(fake.token_by_name("campus:u7:r1"))
            self.assertIsNotNone(fake.token_by_name("shared:keep-me"))
            with open(result["manifest"], encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertTrue(all(set(item) == {"id", "name"} for item in manifest))

    def test_migrate_cli_dry_run_does_not_delete(self):
        import io
        from contextlib import redirect_stdout

        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            db_path = os.path.join(tmp, "relay.sqlite")
            relay.init(db_path)
            fake.add_token("campus:u9:r1", 1000)
            output = io.StringIO()
            with mock.patch.object(migrate.app_server, "RELAY_DB", db_path), redirect_stdout(output):
                code = migrate.main(["--relay-cutover"])
            self.assertEqual(code, 0)
            self.assertIn("dry-run", output.getvalue())
            self.assertIn("campus:u9:r1", output.getvalue())
            self.assertIsNotNone(fake.token_by_name("campus:u9:r1"))


class EndToEndNewApiTests(unittest.TestCase):
    def test_quota_failure_keeps_registration_201_but_marks_key_failed(self):
        with DemoServer() as server:
            server.new_api.fail_mode = "quota"
            code, registered = register(server, "2024001106")
            self.assertEqual(code, 201, registered)
            self.assertEqual(registered["relayKey"]["status"], "failed")
            self.assertEqual(registered["relayKey"]["lastError"], "quota_exhausted")
            self.assertIsNone(registered["relayKey"]["plaintext"])

    def test_register_login_rotate_disable(self):
        with DemoServer() as server:
            code, registered = register(server, "2024001101")
            self.assertEqual(code, 201, registered)
            self.assertEqual(registered["relayKey"]["status"], "active")
            self.assertTrue(registered["relayKey"]["plaintext"])
            owner_ref = owner_ref_of("student", "2024001101")
            issue_name = relay.token_name(owner_ref, "issue:reg-2024001101")
            self.assertIsNotNone(server.new_api.token_by_name(issue_name))

            code, logged = login(server, "2024001101")
            self.assertEqual(code, 200, logged)
            self.assertIsNone(logged["relayKey"]["plaintext"])
            self.assertEqual(logged["relayKey"]["status"], "active")

            code, rotated = server.post("/api/key/rotate", {"requestId": "e2e-rotate"}, logged["token"])
            self.assertEqual(code, 200, rotated)
            self.assertTrue(rotated["plaintext"])
            self.assertIsNone(server.new_api.token_by_name(issue_name))
            rotate_name = relay.token_name(owner_ref, "rotate:e2e-rotate")
            self.assertIsNotNone(server.new_api.token_by_name(rotate_name))

            admin = server.admin_token()
            member = next(item for item in server.get("/api/orgs/%s/members" % PILOT, admin)[1]["members"]
                          if item["studentId"] == "2024001101")
            code, updated = server.patch("/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                                         {"admissionStatus": "disabled"}, admin)
            self.assertEqual(code, 200, updated)
            self.assertIsNone(server.new_api.token_by_name(rotate_name))
            self.assertFalse(any(row["state"] == "active" for row in relay_rows(server.relay_path, owner_ref)))

    def test_quota_share_and_all_readers_use_newapi(self):
        with DemoServer() as server:
            code, registered = register(server, "2024001102")
            self.assertEqual(code, 201, registered)
            owner_ref = owner_ref_of("student", "2024001102")
            name = relay.token_name(owner_ref, "issue:reg-2024001102")
            token = server.new_api.token_by_name(name)
            conn = sqlite3.connect(server.db_path)
            try:
                roster = conn.execute("SELECT COUNT(*) FROM org_memberships WHERE org_id = ?", (PILOT,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(roster, 200)
            self.assertEqual(token["remain_quota"], config.PILOT_TOTAL_QUOTA // roster)
            self.assertEqual(token["remain_quota"], config.DEFAULT_MEMBER_QUOTA)

            server.new_api.set_quota(name, remaining=70000, used=30000)
            code, me = server.get("/api/me", registered["token"])
            self.assertEqual(code, 200, me)
            self.assertEqual(me["quota"]["totalQuota"], config.PILOT_TOTAL_QUOTA)
            self.assertEqual(me["quota"]["memberQuota"], config.DEFAULT_MEMBER_QUOTA)
            self.assertEqual(me["quota"]["remainingQuota"], 70000)
            self.assertEqual(me["quota"]["usedQuota"], 30000)
            self.assertEqual(me["quota"]["source"], "newapi")

            admin = server.admin_token()
            code, dash = server.get("/api/orgs/%s/dashboard" % PILOT, admin)
            self.assertEqual(code, 200, dash)
            self.assertEqual(dash["summary"]["quotaSource"], "newapi")
            self.assertEqual(dash["summary"]["totalQuota"], config.PILOT_TOTAL_QUOTA)
            self.assertEqual(dash["summary"]["memberQuota"], config.DEFAULT_MEMBER_QUOTA)
            self.assertEqual(dash["summary"]["remainingQuota"], 70000)
            self.assertEqual(dash["summary"]["totalConsumed"], 30000)

            accounts = server.get("/api/admin/accounts?q=2024001102", admin)[1]["accounts"]
            account = next(item for item in accounts if item["identity"] == "2024001102")
            self.assertEqual(account["quota"]["remainingQuota"], 70000)
            self.assertEqual(account["quota"]["usedQuota"], 30000)
            members = server.get("/api/orgs/%s/members" % PILOT, admin)[1]["members"]
            member = next(item for item in members if item["studentId"] == "2024001102")
            self.assertEqual(member["quota"]["remainingQuota"], 70000)
            self.assertEqual(member["remainingQuota"], 70000)

    def test_pending_recovery_delivers_once(self):
        with DemoServer() as server:
            server.new_api.fail_mode = "auth_failed"
            code, registered = register(server, "2024001103")
            self.assertEqual(code, 201, registered)
            self.assertEqual(registered["relayKey"]["status"], "pending")
            self.assertIsNone(registered["relayKey"]["plaintext"])
            server.new_api.fail_mode = None
            conn = sqlite3.connect(server.relay_path)
            conn.execute("UPDATE relay_keys SET next_attempt_at = 0 WHERE owner_ref = ?",
                         (owner_ref_of("student", "2024001103"),))
            conn.commit()
            conn.close()

            code, first = server.get("/api/me", registered["token"])
            self.assertEqual(code, 200, first)
            self.assertTrue(first["relayKey"]["plaintext"])
            self.assertEqual(first["relayKey"]["status"], "active")
            code, second = server.get("/api/me", registered["token"])
            self.assertEqual(code, 200, second)
            self.assertIsNone(second["relayKey"]["plaintext"])
            self.assertEqual(server.new_api.create_count, 1)

    def test_pending_not_retried_for_inactive_member(self):
        with DemoServer() as server:
            code, registered = register(server, "2024001104")
            self.assertEqual(code, 201, registered)
            owner_ref = owner_ref_of("student", "2024001104")
            conn = sqlite3.connect(server.relay_path)
            conn.execute("INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, state, "
                         "issue_request_id, token_name, quota, attempts, next_attempt_at, created_at) "
                         "VALUES ('rk_extra', ?, ?, '', 'ch-newapi', 'pending', 'pending', 'extra', "
                         "'campus:extra:r1', 1000, 1, 0, ?)",
                         (owner_ref, relay.digest("pending:rk_extra"), time.time()))
            conn.commit()
            conn.close()
            conn = sqlite3.connect(server.db_path)
            conn.execute("UPDATE org_memberships SET admission_status = 'pending_review' "
                         "WHERE org_id = ? AND user_id = (SELECT user_id FROM students WHERE owner_ref = ?)",
                         (PILOT, owner_ref))
            conn.commit()
            conn.close()
            count = server.new_api.create_count
            code, me = server.get("/api/me", registered["token"])
            self.assertEqual(code, 200, me)
            self.assertIsNone(me["relayKey"]["plaintext"])
            row = next(row for row in relay_rows(server.relay_path, owner_ref) if row["id"] == "rk_extra")
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(server.new_api.create_count, count)

    def test_manual_reissue_route_requires_active_and_succeeds_for_pending(self):
        with DemoServer() as server:
            server.new_api.fail_mode = "auth_failed"
            code, registered = register(server, "2024001105")
            self.assertEqual(code, 201, registered)
            self.assertEqual(registered["relayKey"]["status"], "pending")
            admin = server.admin_token()
            member = next(item for item in server.get("/api/orgs/%s/members" % PILOT, admin)[1]["members"]
                          if item["studentId"] == "2024001105")
            server.new_api.fail_mode = None
            code, result = server.post("/api/orgs/%s/members/%s/reissue-key" % (PILOT, member["userId"]),
                                       {}, admin)
            self.assertEqual(code, 200, result)
            self.assertEqual(result["statuses"], ["active"])
            code, me = server.get("/api/me", registered["token"])
            self.assertTrue(me["relayKey"]["plaintext"])

            code, updated = server.patch("/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                                         {"admissionStatus": "pending_review"}, admin)
            self.assertEqual(code, 200, updated)
            code, denied = server.post("/api/orgs/%s/members/%s/reissue-key" % (PILOT, member["userId"]),
                                       {}, admin)
            self.assertEqual(code, 409, denied)

    def test_demo_login_gate_and_demo_session_has_no_real_token(self):
        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "0"}) as server:
            self.assertFalse(server.get("/api/config")[1]["demoLoginEnabled"])
            self.assertEqual(server.post("/api/demo/login", {"role": "student"})[0], 404)

        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "1"}) as server:
            self.assertTrue(server.get("/api/config")[1]["demoLoginEnabled"])
            code, real = register(server, "2024001100")
            self.assertEqual(code, 201, real)
            before = len(server.new_api.tokens)
            code, demo = server.post("/api/demo/login", {"role": "student"})
            self.assertEqual(code, 200, demo)
            self.assertTrue(demo["demo"])
            self.assertEqual(demo["relayKey"]["status"], "demo")
            self.assertIsNone(demo["relayKey"]["plaintext"])
            self.assertEqual(len(server.new_api.tokens), before)
            self.assertEqual(server.post("/api/key/rotate", {"requestId": "demo-rotate"}, demo["token"])[0], 403)

            code, demo_admin = server.post("/api/demo/login", {"role": "admin"})
            self.assertEqual(code, 200, demo_admin)
            self.assertTrue(demo_admin["demo"])
            self.assertEqual(server.post("/api/admin/revoke", {"owner_ref": "stu_x"}, demo_admin["token"])[0], 403)
            self.assertEqual(server.post("/api/orgs/%s/members/u_x/revoke-key" % PILOT, {},
                                         demo_admin["token"])[0], 403)
            self.assertEqual(server.post("/api/orgs/%s/members/u_x/reissue-key" % PILOT, {},
                                         demo_admin["token"])[0], 403)


if __name__ == "__main__":
    unittest.main()
