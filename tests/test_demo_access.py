import json
import sqlite3
import unittest

import config
from tests.support import DemoServer, register


PILOT = config.PILOT_ORG["id"]


class DemoBoundaryHttpTests(unittest.TestCase):
    def test_invalid_demo_roles_return_400_without_crashing(self):
        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "1"}) as server:
            for role in (None, False, 1, [], {}, ["student"]):
                with self.subTest(role=role):
                    self.assertEqual(server.post("/api/demo/login", {"role": role})[0], 400)

    def test_demo_pat_preserves_key_delivery_restrictions(self):
        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "1"}) as server:
            code, demo = server.post("/api/demo/login", {"role": "student"})
            self.assertEqual(code, 200, demo)
            code, pat = server.post("/api/me/tokens", {"requestId": "demo-token"}, demo["token"])
            self.assertEqual(code, 201, pat)
            issued = len(server.new_api.tokens)
            code, profile = server.get("/api/me", pat["token"])
            self.assertEqual(code, 200, profile)
            self.assertTrue(profile.get("demo"))
            self.assertEqual(profile["relayKey"]["status"], "demo")
            self.assertIsNone(profile["relayKey"]["plaintext"])
            self.assertEqual(profile["org"]["id"], demo["org"]["id"])
            self.assertEqual(server.post("/api/key/rotate", {"requestId": "demo-pat-rotate"}, pat["token"])[0], 403)
            self.assertEqual(len(server.new_api.tokens), issued)
            with sqlite3.connect(server.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM org_memberships WHERE org_id = ?", (PILOT,)).fetchone()[0], 200)

    def test_demo_platform_cannot_reset_real_accounts(self):
        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "1"}) as server:
            code, real = register(server, "2024001086")
            self.assertEqual(code, 201, real)
            code, platform = server.post("/api/demo/login", {"role": "platform_admin"})
            self.assertEqual(code, 200, platform)
            code, result = server.post("/api/reset", {}, platform["token"])
            self.assertEqual(code, 403, result)
            self.assertIn("演示账号不能重置真实数据", result["error"])
            self.assertEqual(server.get("/api/me", real["token"])[0], 200)
            with sqlite3.connect(server.db_path) as conn:
                self.assertEqual(conn.execute("SELECT balance FROM students WHERE identity = '2024001086'").fetchone()[0], 100000)
                event = conn.execute("SELECT actor_role, resource_id, metadata_json FROM audit_events "
                                     "WHERE action = 'system.reset_denied'").fetchone()
                self.assertEqual(event[:2], ("platform_admin", "/api/reset"))
                self.assertEqual(json.loads(event[2])["status"], 403)
                self.assertEqual(json.loads(event[2])["reason"], "demo_account")

    def test_real_platform_admin_can_still_reset(self):
        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "1"}) as server:
            code, real = register(server, "2024001086")
            self.assertEqual(code, 201, real)
            code, result = server.post("/api/reset", {}, server.admin_token())
            self.assertEqual(code, 200, result)
            self.assertTrue(result["ok"])
            self.assertEqual(server.get("/api/me", real["token"])[0], 401)
            with sqlite3.connect(server.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM students").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
