import unittest

import config
from tests.support import DemoServer, login, register

OLD_ACCOUNT_FIELDS = {"mode", "identity", "balance", "spent", "ledger"}


class LegacyCompatibilityTests(unittest.TestCase):
    def test_old_student_and_admin_contracts(self):
        with DemoServer() as server:
            code, registered = register(server, "2024001013")
            self.assertEqual(code, 201, registered)
            self.assertTrue(OLD_ACCOUNT_FIELDS.issubset(registered))
            self.assertIn("token", registered)
            self.assertTrue(registered["firstLogin"])
            self.assertIsNotNone(registered["relayKey"]["plaintext"])
            token = registered["token"]

            code, logged = login(server, "2024001013")
            self.assertEqual(code, 200, logged)
            self.assertTrue(OLD_ACCOUNT_FIELDS.issubset(logged))
            self.assertFalse(logged["firstLogin"])
            self.assertIsNone(logged["relayKey"]["plaintext"])

            token = logged["token"]
            code, me = server.get("/api/me", token)
            self.assertEqual(code, 200)
            self.assertTrue(OLD_ACCOUNT_FIELDS.issubset(me))
            self.assertEqual(set(me["ledger"][0]), {"title", "amount", "type", "time"})

            before = me["balance"]
            code, consumed = server.post("/api/consume", {"amount": 1200, "requestId": "compat-consume-1"}, token)
            self.assertEqual(code, 200, consumed)
            self.assertEqual(consumed["balance"], before - 1200)
            code, again = server.post("/api/consume", {"amount": 1200, "requestId": "compat-consume-1"}, token)
            self.assertEqual(code, 200)
            self.assertEqual(again["balance"], consumed["balance"])

            code, rotated = server.post("/api/key/rotate", {"requestId": "compat-rotate-1"}, token)
            self.assertEqual(code, 200, rotated)
            self.assertTrue({"status", "plaintext", "hint"}.issubset(rotated))
            self.assertIsNotNone(rotated["plaintext"])

            admin = server.admin_token()
            code, accounts = server.get("/api/admin/accounts?q=2024001013", admin)
            self.assertEqual(code, 200)
            row = next(item for item in accounts["accounts"] if item["identity"] == "2024001013")
            self.assertEqual(row["org_id"], config.PILOT_ORG["id"])
            code, revoked = server.post("/api/admin/revoke", {"owner_ref": row["owner_ref"]}, admin)
            self.assertEqual(code, 200)

            code, _ = server.post("/api/logout", {}, token)
            self.assertEqual(code, 200)
            code, _ = server.get("/api/me", token)
            self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
