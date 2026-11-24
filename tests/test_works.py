import unittest

import config
from tests.support import DemoServer, register


class WorkLifecycleTests(unittest.TestCase):
    def test_submit_review_feature_withdraw_and_unpublish(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001009")
            self.assertEqual(code, 201)
            code, teacher = register(server, "2024001010")
            self.assertEqual(code, 201)
            members = server.get("/api/orgs/%s/members" % config.PILOT_ORG["id"], admin)[1]["members"]
            teacher_member = next(item for item in members if item["studentId"] == "2024001010")
            code, _ = server.patch("/api/orgs/%s/members/%s" % (config.PILOT_ORG["id"], teacher_member["userId"]),
                                   {"role": "org_admin"}, admin)
            self.assertEqual(code, 200)
            student_token = student["token"]
            teacher_token = teacher["token"]
            code, work = server.post("/api/works", {"title": "课程表小工具", "summary": "按周查看课程。", "category": "学习"}, student_token)
            self.assertEqual(code, 201, work)
            self.assertEqual(work["status"], "published")
            self.assertIn(work["workId"], [item["workId"] for item in server.get("/api/works", student_token)[1]["items"]])
            code, denied_review = server.post("/api/orgs/%s/works/%s/review" % (config.PILOT_ORG["id"], work["workId"]),
                                              {"decision": "approve"}, student_token)
            self.assertEqual(code, 403, denied_review)
            code, reviewed = server.post("/api/orgs/%s/works/%s/review" % (config.PILOT_ORG["id"], work["workId"]),
                                         {"decision": "approve"}, teacher_token)
            self.assertEqual(code, 409, reviewed)
            self.assertIn(work["workId"], [item["workId"] for item in server.get("/api/works", student_token)[1]["items"]])
            code, featured = server.post("/api/orgs/%s/works/%s/feature" % (config.PILOT_ORG["id"], work["workId"]),
                                         {"featured": True}, teacher_token)
            self.assertEqual(code, 200, featured)
            featured_list = server.get("/api/orgs/%s/featured" % config.PILOT_ORG["id"], teacher_token)[1]["items"]
            self.assertIn(work["workId"], [item["workId"] for item in featured_list])

            code, org2 = server.post("/api/orgs", {"name": "隔离学院", "slug": "isolation-college"}, admin)
            org2_featured = server.get("/api/orgs/%s/featured" % org2["id"], admin)[1]["items"]
            self.assertEqual(org2_featured, [])
            cross_code, cross = server.post("/api/orgs/%s/works/%s/review" % (org2["id"], work["workId"]),
                                            {"decision": "approve"}, teacher_token)
            self.assertEqual(cross_code, 403, cross)

            code, withdrawn = server.post("/api/works/%s/withdraw" % work["workId"], {}, student_token)
            self.assertEqual(code, 200, withdrawn)
            self.assertEqual(withdrawn["status"], "withdrawn")
            self.assertNotIn(work["workId"], [item["workId"] for item in server.get("/api/works", student_token)[1]["items"]])
            self.assertEqual(server.get("/api/orgs/%s/featured" % config.PILOT_ORG["id"], teacher_token)[1]["items"], [])

            code, work2 = server.post("/api/works", {"title": "活动筛选器", "summary": "筛选校园活动。", "category": "活动"}, student_token)
            server.post("/api/orgs/%s/works/%s/review" % (config.PILOT_ORG["id"], work2["workId"]),
                        {"decision": "approve"}, teacher_token)
            code, removed = server.post("/api/orgs/%s/works/%s/unpublish" % (config.PILOT_ORG["id"], work2["workId"]),
                                        {}, admin)
            self.assertEqual(code, 200, removed)
            self.assertEqual(removed["status"], "removed")
            self.assertNotIn(work2["workId"], [item["workId"] for item in server.get("/api/works", student_token)[1]["items"]])

    def test_author_can_withdraw_pending_and_other_user_cannot(self):
        with DemoServer() as server:
            code, one = register(server, "2024001011")
            code, two = register(server, "2024001012")
            code, work = server.post("/api/works", {"title": "待审作品", "summary": "简介", "category": "其他"}, one["token"])
            code, denied = server.post("/api/works/%s/withdraw" % work["workId"], {}, two["token"])
            self.assertEqual(code, 403)
            code, withdrawn = server.post("/api/works/%s/withdraw" % work["workId"], {}, one["token"])
            self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
