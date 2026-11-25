import os
import tempfile
import unittest

import config
import migrations
import rewards_service
import works_service
from tests.support import DemoServer, register


class WorkRewardsTests(unittest.TestCase):
    def test_settlement_flow_self_vote_no_repeat_and_idempotency(self):
        with DemoServer() as server:
            admin = server.admin_token()
            org_id = config.PILOT_ORG["id"]
            # 注册 3 名作者和 3 名投票同学
            _, a1 = register(server, "2024001021")
            _, a2 = register(server, "2024001022")
            _, a3 = register(server, "2024001023")
            _, v1 = register(server, "2024001024")
            _, v2 = register(server, "2024001025")
            _, v3 = register(server, "2024001026")

            # a1 同时再用邮箱注册第二身份，验证只发一份奖励不双倍入账
            c_email, _ = server.post("/api/register", {
                "mode": "email", "identity": "2024001021@mail.hust.edu.cn", "password": "password123",
            })
            self.assertEqual(c_email, 201)

            _, w1 = server.post("/api/works", {"title": "选课雷达", "summary": "智能选课"}, a1["token"])
            _, w2 = server.post("/api/works", {"title": "算力预约", "summary": "实验台预约"}, a2["token"])
            _, w3 = server.post("/api/works", {"title": "食堂指数", "summary": "排队预测"}, a3["token"])

            # w1: 3 有效赞 + 1 自赞（自赞必须不计入有效票）
            for tok in (v1["token"], v2["token"], v3["token"], a1["token"]):
                self.assertEqual(server.post("/api/works/%s/upvote" % w1["workId"], {}, tok)[0], 200)
            # w2: 2 有效赞
            for tok in (v1["token"], v2["token"]):
                self.assertEqual(server.post("/api/works/%s/upvote" % w2["workId"], {}, tok)[0], 200)
            # w3: 仅作者自赞（0 有效赞，不应入围）
            self.assertEqual(server.post("/api/works/%s/upvote" % w3["workId"], {}, a3["token"])[0], 200)

            code, preview = server.get("/api/orgs/%s/rewards/preview" % org_id, admin)
            self.assertEqual(code, 200, preview)
            self.assertEqual([t["slots"] for t in preview["tiers"]], [1, 5, 10])
            self.assertEqual([t["multiplier"] for t in preview["tiers"]], [10, 5, 1])
            self.assertEqual([c["workId"] for c in preview["candidates"]], [w1["workId"], w2["workId"]])
            self.assertEqual(preview["candidates"][0]["validUpvotes"], 3)
            self.assertEqual(preview["candidates"][0]["totalUpvotes"], 4)
            self.assertEqual(preview["candidates"][0]["amount"], preview["baseQuota"] * 10)
            self.assertEqual(preview["candidates"][1]["amount"], preview["baseQuota"] * 5)

            # 教师与学生均无权结算或预览管理端结算接口
            self.assertEqual(server.get("/api/orgs/%s/rewards/preview" % org_id, a1["token"])[0], 403)
            self.assertEqual(server.post("/api/orgs/%s/rewards/settle" % org_id, {
                "periodLabel": "非法尝试", "requestId": "bad-1",
            }, a1["token"])[0], 403)

            # 平台管理员确认统一结算（幂等验证）
            payload = {"periodLabel": "2026 第 1 期", "requestId": "req-settle-1"}
            code, settled = server.post("/api/orgs/%s/rewards/settle" % org_id, payload, admin)
            self.assertEqual(code, 201, settled)
            self.assertFalse(settled["duplicate"])
            self.assertEqual(settled["winnerCount"], 2)
            self.assertEqual(settled["totalAwarded"], preview["baseQuota"] * 15)

            code_dup, settled_dup = server.post("/api/orgs/%s/rewards/settle" % org_id, payload, admin)
            self.assertEqual(code_dup, 200, settled_dup)
            self.assertTrue(settled_dup["duplicate"])
            self.assertEqual(settled_dup["settlementId"], settled["settlementId"])

            # 核对作者账本（单人只领 1 份，且 ledger 保持旧 4 键结构）
            _, me1 = server.get("/api/me", a1["token"])
            self.assertEqual(me1["balance"], preview["baseQuota"] + preview["baseQuota"] * 10)
            self.assertEqual(me1["quota"]["rewardQuota"], preview["baseQuota"] * 10)
            self.assertEqual(me1["quota"]["rewardCount"], 1)
            reward_ledgers = [row for row in me1["ledger"] if row["type"] == "reward"]
            self.assertEqual(len(reward_ledgers), 1)
            self.assertEqual(set(reward_ledgers[0]), {"title", "amount", "type", "time"})

            # 广场作品卡带获奖徽章信息，学生可读奖励汇总
            _, gallery = server.get("/api/works", a1["token"])
            g_w1 = next(item for item in gallery["items"] if item["workId"] == w1["workId"])
            self.assertIsNotNone(g_w1["reward"])
            self.assertEqual(g_w1["reward"]["multiplier"], 10)
            code_sum, summary = server.get("/api/rewards/summary", a1["token"])
            self.assertEqual(code_sum, 200, summary)
            self.assertEqual(summary["rewardedWorkCount"], 2)

            # 同一作品终身不重复获奖：下期预览不再包含 w1、w2
            _, next_preview = server.get("/api/orgs/%s/rewards/preview" % org_id, admin)
            self.assertEqual(next_preview["candidates"], [])

    def test_demo_candidates_seed_covers_three_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "demo.sqlite")
            migrations.migrate(db_path)
            conn = migrations.connect(db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                works_service.ensure_social_tables(conn)
                inserted = rewards_service.ensure_demo_candidates(conn, config.PILOT_ORG["id"])
                inserted_again = rewards_service.ensure_demo_candidates(conn, config.PILOT_ORG["id"])
                conn.execute("COMMIT")
                self.assertEqual(inserted, 7)
                self.assertEqual(inserted_again, 0)
                preview = rewards_service.preview_settlement(conn, config.PILOT_ORG["id"], config.DEFAULT_MEMBER_QUOTA)
                self.assertEqual(preview["winnerCount"], 7)
                self.assertEqual([c["multiplier"] for c in preview["candidates"]], [10, 5, 5, 5, 5, 5, 1])
                self.assertEqual(preview["candidates"][0]["studentId"], "2024001100")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
