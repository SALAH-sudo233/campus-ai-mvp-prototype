import unittest

from tests.support import DemoServer, register

HEADER = "external_ref,student_id,email,name,role,status\n"


class EndToEndTests(unittest.TestCase):
    def test_platform_build_import_register_consume_teacher_trend(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, org = server.post("/api/orgs", {"name": "端到端学院", "slug": "e2e-college"}, admin)
            self.assertEqual(code, 201, org)
            org_id = org["id"]
            body = (HEADER +
                    "e2e-teacher,2024001198,2024001198@mail.hust.edu.cn,示例教师,教师,active\n"
                    "e2e-student,2024001199,2024001199@mail.hust.edu.cn,示例学生,学生,active\n").encode("utf-8")
            code, preview = server.request("POST", "/api/orgs/%s/imports/preview" % org_id, raw=body, token=admin)
            self.assertEqual(code, 200, preview)
            code, committed = server.post("/api/orgs/%s/imports/%s/commit" % (org_id, preview["batchId"]),
                                          {"requestId": "e2e-import"}, admin)
            self.assertEqual(code, 200, committed)
            self.assertEqual(committed["counts"]["accepted"], 2)

            code, student = register(server, "2024001199", org_id=org_id)
            self.assertEqual(code, 201, student)
            code, consumed = server.post("/api/consume", {"amount": 5000, "requestId": "e2e-consume"}, student["token"])
            self.assertEqual(code, 200, consumed)
            code, teacher = register(server, "2024001198", org_id=org_id)
            self.assertEqual(code, 201, teacher)
            self.assertEqual(teacher["role"], "org_admin")

            code, dashboard = server.get("/api/orgs/%s/dashboard?days=120" % org_id, teacher["token"])
            self.assertEqual(code, 200, dashboard)
            self.assertGreaterEqual(dashboard["summary"]["memberCount"], 2)
            self.assertGreaterEqual(dashboard["summary"]["totalConsumed"], 5000)
            self.assertEqual(len(dashboard["trend"]["series"]), 90)
            self.assertTrue(dashboard["trend"]["truncated"])
            self.assertEqual(len([cell for cell in dashboard["heatmap"]["cells"]]), 90)

            code, work = server.post("/api/works", {"title": "端到端作品", "summary": "真实入口走完的作品。", "category": "学习"}, student["token"])
            self.assertEqual(code, 201, work)
            code, pending = server.get("/api/orgs/%s/works?status=pending_review" % org_id, teacher["token"])
            self.assertEqual(code, 200, pending)
            self.assertEqual(len(pending["items"]), 1)
            code, approved = server.post("/api/orgs/%s/works/%s/review" % (org_id, work["workId"]),
                                         {"decision": "approve"}, teacher["token"])
            self.assertEqual(code, 200, approved)
            code, gallery = server.get("/api/works", student["token"])
            self.assertIn(work["workId"], [item["workId"] for item in gallery["items"]])


if __name__ == "__main__":
    unittest.main()
