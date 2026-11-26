import sqlite3
import unittest

import authz
import config
import pat_service
from tests.support import DemoServer, login, register


PILOT = config.PILOT_ORG["id"]
HEADER = "external_ref,student_id,email,name,role,status\n"


class FourEndMatrixTests(unittest.TestCase):
    def test_five_roles_and_viewer_has_only_read_actions(self):
        self.assertEqual(set(authz.ROLES), {
            "platform_admin", "org_admin", "org_operator", "college_viewer", "org_member"})
        viewer = {"role": "college_viewer", "user_id": "viewer", "org_id": PILOT}
        for row in authz.matrix_rows():
            if row["role"] == "college_viewer":
                self.assertTrue(row["action"] == "read" or row["action"].startswith("read_"), row)
            if row["action"] not in ("read", "read_self", "read_org", "read_gallery", "read_own"):
                self.assertFalse(authz.allowed(viewer, row["resource"], row["action"],
                                               org_id=PILOT, owner_user_id="viewer"), row)
        for role in ("org_operator", "org_admin"):
            operator = {"role": role, "user_id": "operator", "org_id": PILOT}
            for resource, action in (("members", "import"), ("members", "update_status"),
                                     ("works", "review"), ("works", "feature")):
                self.assertTrue(authz.allowed(operator, resource, action, org_id=PILOT))
                self.assertFalse(authz.allowed(operator, resource, action, org_id="other"))
            for resource, action in (("members", "update_role"), ("members", "bulk"),
                                     ("rewards", "settle"), ("organizations", "create")):
                self.assertFalse(authz.allowed(operator, resource, action, org_id=PILOT))
            self.assertTrue(pat_service.permissions_shrank(role, "college_viewer"))


class FourEndHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = DemoServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.platform = self.server.admin_token()
        code, self.account = register(self.server, "2024001080")
        self.assertEqual(code, 201, self.account)
        self.token = self.account["token"]
        with sqlite3.connect(self.server.db_path) as conn:
            self.user_id = conn.execute("SELECT user_id FROM students WHERE identity = '2024001080'").fetchone()[0]

    def set_role(self, role):
        code, body = self.server.patch("/api/orgs/%s/members/%s" % (PILOT, self.user_id),
                                       {"role": role}, self.platform)
        self.assertEqual(code, 200, body)

    def test_viewer_dashboard_reads_and_all_business_writes_are_denied(self):
        code, minted = self.server.post("/api/me/tokens", {"requestId": "before-viewer"}, self.token)
        self.assertEqual(code, 201, minted)
        self.set_role("college_viewer")
        for path in ("/api/me", "/api/orgs/%s/dashboard" % PILOT,
                     "/api/orgs/%s/summary" % PILOT, "/api/orgs/%s/trends" % PILOT,
                     "/api/orgs/%s/featured" % PILOT, "/api/works"):
            with self.subTest(path=path):
                code, body = self.server.get(path, self.token)
                self.assertEqual(code, 200, body)
        work_id = config.SEED_WORKS[0]["work_id"]
        self.assertEqual(self.server.get("/api/me", self.token)[1]["relayKey"]["status"], "read_only")
        org_path = "/api/orgs/%s" % PILOT
        writes = [
            ("POST", "/api/me/tokens", {"requestId": "viewer-pat"}),
            ("DELETE", "/api/me/tokens/" + minted["item"]["id"], None),
            ("POST", "/api/works/%s/upvote" % work_id, {}),
            ("POST", "/api/works/%s/comments" % work_id, {"body": "学院不能评论"}),
            ("POST", "/api/works", {}),
            ("POST", "/api/works/preview-image", {}),
            ("POST", "/api/works/upload", {}),
            ("PATCH", "/api/works/" + work_id, {"title": "不能修改"}),
            ("POST", "/api/works/%s/withdraw" % work_id, {}),
            ("POST", "/api/onboarding", {"action": "start"}),
            ("POST", "/api/key/rotate", {"requestId": "viewer-key"}),
            ("POST", "/api/consume", {"amount": 1, "requestId": "viewer-use"}),
            ("POST", "/api/logout", {}),
            ("POST", "/api/orgs", {"name": "不能新增", "slug": "viewer-org"}),
            ("POST", org_path + "/members", {"name": "不能新增", "studentId": "2024999001"}),
            ("PATCH", org_path + "/members/" + self.user_id, {"role": "org_admin"}),
            ("POST", org_path + "/members/bulk", {"action": "grant", "amount": 1}),
            ("POST", org_path + "/members/%s/revoke-key" % self.user_id, {}),
            ("POST", org_path + "/members/%s/reissue-key" % self.user_id, {}),
            ("POST", org_path + "/imports/preview", {}),
            ("POST", org_path + "/imports/missing/commit", {}),
            ("POST", org_path + "/works/%s/review" % work_id, {"decision": "approve"}),
            ("POST", org_path + "/works/%s/feature" % work_id, {"featured": True}),
            ("POST", org_path + "/works/%s/unpublish" % work_id, {}),
            ("POST", org_path + "/rewards/settle", {}),
            ("POST", "/api/admin/revoke", {"owner_ref": "missing"}),
            ("POST", "/api/reset", {}),
        ]
        for method, path, body in writes:
            with self.subTest(method=method, path=path):
                code, result = self.server.request(method, path, body=body, token=self.token)
                self.assertEqual(code, 403, result)
        self.assertEqual(self.server.get("/api/me", minted["token"])[0], 401)
        issued = len(self.server.new_api.tokens)
        code, logged_in = login(self.server, "2024001080")
        self.assertEqual(code, 200, logged_in)
        self.assertEqual(logged_in["relayKey"]["status"], "read_only")
        self.assertEqual(len(self.server.new_api.tokens), issued)
        for suffix in ("members", "tree", "audit", "works", "imports/template"):
            self.assertEqual(self.server.get(org_path + "/" + suffix, self.token)[0], 403, suffix)
        with sqlite3.connect(self.server.db_path) as conn:
            self.assertIsNone(conn.execute("SELECT revoked_at FROM pat_tokens WHERE id = ?",
                                           (minted["item"]["id"],)).fetchone()[0])

    def test_operator_verifies_imports_and_reviews_without_platform_privileges(self):
        self.set_role("org_operator")
        code, student = register(self.server, "2024001081")
        self.assertEqual(code, 201, student)
        with sqlite3.connect(self.server.db_path) as conn:
            student_id = conn.execute("SELECT user_id FROM students WHERE identity = '2024001081'").fetchone()[0]
            conn.execute("UPDATE org_memberships SET admission_status = 'pending_review' WHERE user_id = ?",
                         (student_id,))
        path = "/api/orgs/%s/members/%s" % (PILOT, student_id)
        self.assertEqual(self.server.patch(path, {"admissionStatus": "active"}, self.token)[0], 200)
        self.assertEqual(self.server.patch(path, {"role": "org_admin"}, self.token)[0], 403)
        csv_data = (HEADER + "operator-import,2024999871,,新学生,org_member,active\n").encode()
        code, preview = self.server.request("POST", "/api/orgs/%s/imports/preview" % PILOT,
                                            raw=csv_data, token=self.token)
        self.assertEqual(code, 200, preview)
        code, committed = self.server.post("/api/orgs/%s/imports/%s/commit" % (PILOT, preview["batchId"]),
                                           {"requestId": "operator-import"}, self.token)
        self.assertEqual(code, 200, committed)
        self.assertEqual(committed["counts"]["accepted"], 1)
        work_id = config.SEED_WORKS[0]["work_id"]
        with sqlite3.connect(self.server.db_path) as conn:
            conn.execute("UPDATE works SET status = 'pending_review' WHERE work_id = ?", (work_id,))
        code, reviewed = self.server.post("/api/orgs/%s/works/%s/review" % (PILOT, work_id),
                                          {"decision": "approve"}, self.token)
        self.assertEqual(code, 200, reviewed)
        self.assertEqual(self.server.post("/api/orgs/%s/works/%s/feature" % (PILOT, work_id),
                                          {"featured": True}, self.token)[0], 200)
        for path, data in (("/api/orgs", {"name": "禁止", "slug": "forbidden"}),
                           ("/api/orgs/%s/rewards/settle" % PILOT, {}),
                           ("/api/orgs/%s/members/bulk" % PILOT, {"action": "grant"}),
                           ("/api/orgs/%s/members" % PILOT,
                            {"name": "提权", "studentId": "2024999872", "role": "org_admin"})):
            self.assertEqual(self.server.post(path, data, self.token)[0], 403, path)
        elevated = (HEADER + "elevate,2024999873,,提权,org_operator,active\n").encode()
        self.assertEqual(self.server.request("POST", "/api/orgs/%s/imports/preview" % PILOT,
                                             raw=elevated, token=self.token)[0], 403)
        code, elevated_batch = self.server.request("POST", "/api/orgs/%s/imports/preview" % PILOT,
                                                   raw=elevated, token=self.platform)
        self.assertEqual(code, 200, elevated_batch)
        self.assertEqual(self.server.post("/api/orgs/%s/imports/%s/commit" % (PILOT, elevated_batch["batchId"]),
                                          {"requestId": "operator-elevated"}, self.token)[0], 403)
        self.assertEqual(self.server.get("/api/orgs/missing/dashboard", self.token)[0], 403)

    def test_four_end_demo_sessions_use_dedicated_identities(self):
        with DemoServer(env_overrides={"DEMO_ALLOW_LOGIN": "1"}) as server:
            for role, expected in (("student", "org_member"), ("org_operator", "org_operator"),
                                   ("college_viewer", "college_viewer"), ("platform_admin", "platform_admin"),
                                   ("teacher", "org_operator"), ("admin", "platform_admin")):
                code, result = server.post("/api/demo/login", {"role": role})
                self.assertEqual(code, 200, result)
                self.assertEqual(result["role"], expected)
                if expected != "platform_admin":
                    self.assertNotEqual(result["org"]["id"], PILOT)
                    self.assertEqual(result["org"]["slug"], "four-end-demo")
                if role == "college_viewer":
                    self.assertEqual(server.get("/api/orgs/%s/dashboard" % result["org"]["id"], result["token"])[0], 200)
                    self.assertEqual(server.get("/api/orgs/%s/dashboard" % PILOT, result["token"])[0], 403)
                    self.assertEqual(server.post("/api/me/tokens", {}, result["token"])[0], 403)
            code, real = register(server, "2024001084")
            self.assertEqual(code, 201, real)
            self.assertEqual(real["balance"], 100000)
            with sqlite3.connect(server.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM org_memberships WHERE org_id = ?", (PILOT,)).fetchone()[0], 200)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM works WHERE org_id = ? AND work_id LIKE 'demo-award-%'", (PILOT,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
