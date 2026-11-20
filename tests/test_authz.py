import unittest

import authz
import config
from tests.support import DemoServer, register

HEADER = "external_ref,student_id,email,name,role,status\n"


class AuthzMatrixTests(unittest.TestCase):
    def test_three_roles_matrix(self):
        platform = {"role": authz.PLATFORM_ADMIN, "user_id": None, "org_id": None}
        admin = {"role": authz.ORG_ADMIN, "user_id": "u-admin", "org_id": "org-a"}
        member = {"role": authz.ORG_MEMBER, "user_id": "u-member", "org_id": "org-a"}
        self.assertTrue(authz.allowed(platform, "organizations", "create"))
        self.assertFalse(authz.allowed(admin, "organizations", "create"))
        self.assertTrue(authz.allowed(admin, "members", "import", org_id="org-a"))
        self.assertFalse(authz.allowed(admin, "members", "import", org_id="org-b"))
        self.assertFalse(authz.allowed(member, "members", "list", org_id="org-a"))
        self.assertTrue(authz.allowed(member, "works", "submit", owner_user_id="u-member"))
        self.assertFalse(authz.allowed(member, "works", "submit", owner_user_id="u-other"))
        self.assertFalse(authz.allowed(member, "audit", "read", org_id="org-a"))
        self.assertTrue(authz.allowed(admin, "audit", "read", org_id="org-a"))
        self.assertTrue(authz.allowed(platform, "works", "unpublish", org_id="org-b"))


class AuthzHttpTests(unittest.TestCase):
    def test_cross_org_is_403_without_existence_leak(self):
        with DemoServer() as server:
            admin_token = server.admin_token()
            code, created = server.post("/api/orgs", {"name": "第二组织", "slug": "second-org"}, admin_token)
            self.assertEqual(code, 201, created)
            other_id = created["id"]
            code, data = register(server, "2024001003")
            self.assertEqual(code, 201, data)
            member_token = data["token"]
            code, members = server.get("/api/orgs/%s/members" % config.PILOT_ORG["id"], admin_token)
            self.assertEqual(code, 200, members)
            user = next(item for item in members["members"] if item["studentId"] == "2024001003")
            code, _ = server.patch("/api/orgs/%s/members/%s" % (config.PILOT_ORG["id"], user["userId"]),
                                   {"role": "org_admin"}, admin_token)
            self.assertEqual(code, 200)
            code, body = server.get("/api/orgs/%s/summary" % other_id, member_token)
            self.assertEqual(code, 403, body)
            code2, body2 = server.get("/api/orgs/org_does_not_exist/summary", member_token)
            self.assertEqual(code2, 403, body2)
            self.assertEqual(body, body2)
            code, own = server.get("/api/orgs/%s/summary" % config.PILOT_ORG["id"], member_token)
            self.assertEqual(code, 200, own)
            code, platform_view = server.get("/api/orgs/%s/summary" % other_id, admin_token)
            self.assertEqual(code, 200, platform_view)

    def test_org_member_cannot_read_org_usage(self):
        with DemoServer() as server:
            code, data = register(server, "2024001004")
            self.assertEqual(code, 201, data)
            code, _ = server.get("/api/orgs/%s/summary" % config.PILOT_ORG["id"], data["token"])
            self.assertEqual(code, 403)

    def test_cross_org_member_key_actions_are_rejected(self):
        with DemoServer() as server:
            admin = server.admin_token()
            pilot = config.PILOT_ORG["id"]
            code, org_b = server.post("/api/orgs", {"name": "第二学院", "slug": "second-college"}, admin)
            self.assertEqual(code, 201, org_b)
            body = (HEADER + "b-1,2024999018,2024999018@example.edu.cn,外院学生,学生,active\n").encode("utf-8")
            code, preview = server.request("POST", "/api/orgs/%s/imports/preview" % org_b["id"], raw=body, token=admin)
            self.assertEqual(code, 200, preview)
            code, _ = server.post("/api/orgs/%s/imports/%s/commit" % (org_b["id"], preview["batchId"]),
                                  {"requestId": "authz-import"}, admin)
            self.assertEqual(code, 200)
            code, member_b = register(server, "2024999018", org_id=org_b["id"])
            self.assertEqual(code, 201, member_b)
            b_token = member_b["token"]
            b_user = server.get("/api/orgs/%s/members" % org_b["id"], admin)[1]["members"][0]["userId"]
            self.assertEqual(server.get("/api/me", b_token)[1]["relayKey"]["status"], "active")

            code, teacher = register(server, "2024001017")
            teacher_token = teacher["token"]
            pilot_members = server.get("/api/orgs/%s/members" % pilot, admin)[1]["members"]
            teacher_user = next(item for item in pilot_members if item["studentId"] == "2024001017")["userId"]
            server.patch("/api/orgs/%s/members/%s" % (pilot, teacher_user), {"role": "org_admin"}, admin)

            code, _ = server.post("/api/orgs/%s/members/%s/revoke-key" % (pilot, b_user), {}, teacher_token)
            self.assertEqual(code, 403)
            accounts = server.get("/api/admin/accounts?q=2024999018", admin)[1]["accounts"]
            self.assertEqual(next(item for item in accounts if item["identity"] == "2024999018")["key_status"], "active")

            code, _ = server.post("/api/orgs/%s/members/bulk" % pilot,
                                  {"action": "revoke", "userIds": [b_user], "requestId": "cross-revoke"}, admin)
            self.assertEqual(code, 403)
            accounts = server.get("/api/admin/accounts?q=2024999018", admin)[1]["accounts"]
            self.assertEqual(next(item for item in accounts if item["identity"] == "2024999018")["key_status"], "active")

            before_b = server.get("/api/me", b_token)[1]["balance"]
            code, _ = server.post("/api/orgs/%s/members/bulk" % pilot,
                                  {"action": "grant", "userIds": [b_user], "amount": 5000, "requestId": "cross-grant"},
                                  admin)
            self.assertEqual(code, 403)
            self.assertEqual(server.get("/api/me", b_token)[1]["balance"], before_b)

            before_teacher = server.get("/api/me", teacher_token)[1]["balance"]
            code, _ = server.post("/api/orgs/%s/members/bulk" % pilot,
                                  {"action": "grant", "userIds": [teacher_user, b_user], "amount": 5000,
                                   "requestId": "mixed-grant"}, admin)
            self.assertEqual(code, 403)
            self.assertEqual(server.get("/api/me", teacher_token)[1]["balance"], before_teacher)

            code, granted = server.post("/api/orgs/%s/members/bulk" % pilot,
                                        {"action": "grant", "userIds": [teacher_user], "amount": 5000,
                                         "requestId": "valid-grant"}, admin)
            self.assertEqual(code, 200, granted)
            after = server.get("/api/me", teacher_token)[1]["balance"]
            self.assertEqual(after, before_teacher + 5000)
            code, again = server.post("/api/orgs/%s/members/bulk" % pilot,
                                      {"action": "grant", "userIds": [teacher_user], "amount": 5000,
                                       "requestId": "valid-grant"}, admin)
            self.assertEqual(code, 200, again)
            self.assertEqual(server.get("/api/me", teacher_token)[1]["balance"], after)

            code, tree = server.get("/api/orgs/%s/tree" % pilot, admin)
            self.assertEqual(code, 200)
            node = next(item for item in tree["children"] if item["studentId"] == "2024001017")
            self.assertEqual(node["role"], "org_admin")
            self.assertTrue(node["keyHint"])

            code, events = server.get("/api/audit-events", teacher_token)
            self.assertEqual(code, 200)
            self.assertTrue(all(event["orgId"] == pilot for event in events["events"]))
            code, _ = server.get("/api/audit-events?orgId=%s" % org_b["id"], teacher_token)
            self.assertEqual(code, 403)

    def test_consume_request_id_cannot_collide_with_initial_grant(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, data = register(server, "2024001019")
            self.assertEqual(code, 201, data)
            token = data["token"]
            account = server.get("/api/admin/accounts?q=2024001019", admin)[1]["accounts"][0]
            before = server.get("/api/me", token)[1]["balance"]
            request_id = "grant:" + account["owner_ref"]
            code, charged = server.post("/api/consume", {"amount": 100, "requestId": request_id}, token)
            self.assertEqual(code, 200, charged)
            self.assertEqual(charged["balance"], before - 100)
            code, duplicate = server.post("/api/consume", {"amount": 100, "requestId": request_id}, token)
            self.assertEqual(code, 200)
            self.assertEqual(duplicate["balance"], before - 100)


if __name__ == "__main__":
    unittest.main()
