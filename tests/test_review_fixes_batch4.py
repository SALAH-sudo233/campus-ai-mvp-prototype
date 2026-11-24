"""复审第四批的真实 HTTP 回归：组织看板按组织收敛，待复核登录不补发明文密钥。"""
import sqlite3
import time
import unittest

import config
from tests.support import TEST_AUTH_VALUE, DemoServer, login, register

HEADER = "external_ref,student_id,email,name,role,status\n"
PILOT = config.PILOT_ORG["id"]


def create_org(server, admin, slug, name):
    code, data = server.post("/api/orgs", {"name": name, "slug": slug}, admin)
    assert code == 201, data
    return data["id"]


def preview(server, admin, org_id, rows):
    body = (HEADER + "".join(row + "\n" for row in rows)).encode("utf-8")
    return server.request("POST", "/api/orgs/%s/imports/preview" % org_id, raw=body, token=admin)


def commit(server, admin, org_id, batch_id):
    return server.post("/api/orgs/%s/imports/%s/commit" % (org_id, batch_id),
                       {"requestId": "commit-" + batch_id}, admin)


def members(server, admin, org_id):
    code, data = server.get("/api/orgs/%s/members" % org_id, admin)
    assert code == 200, data
    return data["members"]


def member_for(server, admin, org_id, student_id):
    return next(item for item in members(server, admin, org_id) if item["studentId"] == student_id)


def org_admin(server, admin, org_id, student_id):
    code, batch = preview(server, admin, org_id, ["teacher-b4,%s,,组织教师,org_admin,active" % student_id])
    assert code == 200, batch
    code, committed = commit(server, admin, org_id, batch["batchId"])
    assert code == 200, committed
    code, account = register(server, student_id, password=TEST_AUTH_VALUE, org_id=org_id)
    assert code == 201, account
    return account


def add_legacy_membership(server, org_id, user_id, student_id):
    """直接写库，模拟历史上已存在的多组织归属（导入已拒绝跨组织并入）。"""
    conn = sqlite3.connect(server.db_path, timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
                     "student_id, name, joined_at) VALUES (?, ?, 'org_member', 'active', ?, ?, ?, ?)",
                     (org_id, user_id, "legacy-b4", student_id, "历史成员", time.time()))
        conn.execute("COMMIT")
    finally:
        conn.close()


def setup_cross_org(server):
    admin = server.admin_token()
    code, student = register(server, "2024001091")
    assert code == 201, student
    code, consumed = server.post("/api/consume", {"amount": 10, "requestId": "b4-consume"}, student["token"])
    assert code == 200, consumed
    org_b = create_org(server, admin, "org-batch4-b", "第四批乙院")
    teacher = org_admin(server, admin, org_b, "2024999701")
    victim = member_for(server, admin, PILOT, "2024001091")
    add_legacy_membership(server, org_b, victim["userId"], "2024001091")
    return admin, org_b, teacher, victim


class OrgDashboardScopeTests(unittest.TestCase):
    def test_other_org_dashboard_ignores_legacy_member_spend(self):
        with DemoServer() as server:
            admin, org_b, teacher, _ = setup_cross_org(server)
            code, dash = server.get("/api/orgs/%s/dashboard" % org_b, teacher["token"])
            self.assertEqual(code, 200, dash)
            summary = dash["summary"]
            self.assertEqual(summary["totalConsumed"], 0, dash)
            self.assertEqual(summary["remainingQuota"], 100000, dash)
            self.assertEqual(summary["quotaSource"], "newapi", dash)
            self.assertEqual(summary["activeTokenCount"], 1, dash)
            self.assertEqual(summary["activeMemberCount"], 0, dash)
            self.assertEqual(dash["ranking"], [])
            buckets = {item["label"]: item["count"] for item in dash["activity"]["buckets"]}
            self.assertEqual(buckets["1–2 天"], 0, dash["activity"])
            self.assertEqual(sum(item["value"] for item in dash["trend"]["series"]), 0)
            self.assertEqual(member_for(server, admin, org_b, "2024001091")["spent"], 0)

    def test_pilot_dashboard_keeps_its_own_spend(self):
        with DemoServer() as server:
            admin, _, _, victim = setup_cross_org(server)
            code, dash = server.get("/api/orgs/%s/dashboard" % PILOT, admin)
            self.assertEqual(code, 200, dash)
            summary = dash["summary"]
            self.assertEqual(summary["totalConsumed"], 0, dash)
            self.assertEqual(summary["remainingQuota"], 100000, dash)
            self.assertEqual(summary["totalQuota"], config.PILOT_TOTAL_QUOTA, dash)
            self.assertEqual(summary["quotaSource"], "newapi", dash)
            self.assertEqual(summary["activeMemberCount"], 1, dash)
            ranked = {item["studentId"]: item["consumed"] for item in dash["ranking"]}
            self.assertEqual(ranked.get("2024001091"), 10, dash["ranking"])
            buckets = {item["label"]: item["count"] for item in dash["activity"]["buckets"]}
            self.assertEqual(buckets["1–2 天"], 1, dash["activity"])
            self.assertEqual(sum(item["value"] for item in dash["trend"]["series"]), 10)


class PendingLoginKeyTests(unittest.TestCase):
    def test_pending_review_login_returns_no_plaintext_key(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001092")
            self.assertEqual(code, 201, student)
            self.assertTrue(student["relayKey"]["plaintext"])
            member = member_for(server, admin, PILOT, "2024001092")
            code, updated = server.patch("/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                                         {"admissionStatus": "pending_review"}, admin)
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")
            code, revoked = server.post("/api/orgs/%s/members/%s/revoke-key" % (PILOT, member["userId"]),
                                        {}, admin)
            self.assertEqual(code, 200, revoked)

            code, data = login(server, "2024001092")
            self.assertEqual(code, 200, data)
            self.assertEqual(data["identity"], "2024001092")
            self.assertEqual(data["balance"], 100000)
            self.assertEqual(data["admissionStatus"], "pending_review")
            self.assertIsNone(data["relayKey"]["plaintext"])
            self.assertEqual(data["relayKey"]["status"], "none")
            code, me = server.get("/api/me", data["token"])
            self.assertEqual(code, 200, me)
            self.assertIsNone(me["relayKey"]["plaintext"])
            accounts = server.get("/api/admin/accounts?q=2024001092", admin)[1]["accounts"]
            self.assertEqual(next(item for item in accounts if item["identity"] == "2024001092")["key_status"],
                             "none")

    def test_active_login_still_issues_plaintext_key(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001093")
            self.assertEqual(code, 201, student)
            member = member_for(server, admin, PILOT, "2024001093")
            self.assertEqual(member["admissionStatus"], "active")
            code, revoked = server.post("/api/orgs/%s/members/%s/revoke-key" % (PILOT, member["userId"]),
                                        {}, admin)
            self.assertEqual(code, 200, revoked)
            code, data = login(server, "2024001093")
            self.assertEqual(code, 200, data)
            self.assertTrue(data["relayKey"]["plaintext"])
            self.assertEqual(data["relayKey"]["status"], "active")
            code, again = login(server, "2024001093")
            self.assertEqual(code, 200, again)
            self.assertIsNone(again["relayKey"]["plaintext"])
            self.assertEqual(again["relayKey"]["status"], "active")
            self.assertTrue(again["relayKey"]["hint"])


if __name__ == "__main__":
    unittest.main()
