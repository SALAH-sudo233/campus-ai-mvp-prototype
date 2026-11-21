"""复审第二批的真实 HTTP 回归：跨组织导入冲突，以及密钥、余额的组织范围口径。"""
import json
import sqlite3
import time
import unittest

import config
from tests.support import TEST_AUTH_VALUE, DemoServer, register

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
    """先导入一名组织管理员，再用该身份注册出可登录的教师账号。"""
    code, batch = preview(server, admin, org_id, ["teacher-1,%s,,组织教师,org_admin,active" % student_id])
    assert code == 200, batch
    code, committed = commit(server, admin, org_id, batch["batchId"])
    assert code == 200, committed
    code, account = register(server, student_id, password=TEST_AUTH_VALUE, org_id=org_id)
    assert code == 201, account
    return account


class CrossOrgImportTests(unittest.TestCase):
    def test_same_student_id_and_email_are_identity_conflicts(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, by_sid = register(server, "2024001071")
            self.assertEqual(code, 201, by_sid)
            code, by_email = register(server, "2024001072@mail.hust.edu.cn", mode="email")
            self.assertEqual(code, 201, by_email)
            org_name = "复审其他学院"
            org_b = create_org(server, admin, "org-review-b", org_name)
            hints = {item["identity"]: server.get("/api/me", item["token"])[1]["relayKey"]["hint"]
                     for item in (by_sid, by_email)}
            cases = [
                ("2024001071", "x-1,2024001071,,跨组织学生,org_member,active", by_sid),
                ("2024001072", "x-2,,2024001072@mail.hust.edu.cn,跨组织学生,org_member,active", by_email),
            ]
            for sid, row, account in cases:
                code, batch = preview(server, admin, org_b, [row])
                self.assertEqual(code, 200, batch)
                self.assertEqual(batch["counts"]["accepted"], 0, batch)
                self.assertEqual(batch["counts"]["conflicts"], 1, batch)
                self.assertEqual(batch["rows"][0]["reasonCode"], "identity_conflict", batch)
                self.assertEqual(batch["rows"][0]["message"], "该身份已属于其他组织", batch)
                self.assertNotIn(org_name, json.dumps(batch, ensure_ascii=False))
                code, csv_bytes = server.get("/api/orgs/%s/imports/%s/errors.csv" % (org_b, batch["batchId"]),
                                             admin)
                self.assertEqual(code, 200)
                csv_text = csv_bytes.decode("utf-8")
                self.assertIn("identity_conflict", csv_text)
                self.assertIn("该身份已属于其他组织", csv_text)
                self.assertNotIn(org_name, csv_text)
                self.assertNotIn(sid, csv_text)
                self.assertNotIn("2024001072@mail.hust.edu.cn", csv_text)
                code, result = commit(server, admin, org_b, batch["batchId"])
                self.assertEqual(code, 200, result)
                self.assertEqual(result["counts"]["accepted"], 0, result)
                self.assertEqual(result["counts"]["conflicts"], 1, result)
            self.assertEqual(members(server, admin, org_b), [])
            for account in (by_sid, by_email):
                code, me = server.get("/api/me", account["token"])
                self.assertEqual(code, 200, me)
                self.assertEqual(me["balance"], 100000)
                self.assertEqual(me["relayKey"]["hint"], hints[account["identity"]])
                self.assertEqual(server.post("/api/consume", {"amount": 10,
                                                               "requestId": "still-" + account["identity"]},
                                             account["token"])[0], 200)
                self.assertEqual(member_for(server, admin, PILOT, account["identity"].split("@")[0]
                                            )["admissionStatus"], "active")

    def test_identity_claimed_between_preview_and_commit_counts_as_conflict(self):
        with DemoServer() as server:
            admin = server.admin_token()
            org_a = create_org(server, admin, "org-review-a", "复审组织甲")
            org_b = create_org(server, admin, "org-review-race", "复审组织乙")
            row = "race-1,2024999411,,竞态学生,org_member,active"
            code, batch_b = preview(server, admin, org_b, [row])
            self.assertEqual(code, 200, batch_b)
            self.assertEqual(batch_b["counts"]["accepted"], 1, batch_b)
            code, batch_a = preview(server, admin, org_a, [row])
            self.assertEqual(code, 200, batch_a)
            self.assertEqual(batch_a["counts"]["accepted"], 1, batch_a)
            code, first = commit(server, admin, org_a, batch_a["batchId"])
            self.assertEqual(code, 200, first)
            self.assertEqual(first["counts"]["accepted"], 1, first)
            code, raced = commit(server, admin, org_b, batch_b["batchId"])
            self.assertEqual(code, 200, raced)
            self.assertEqual(raced["counts"]["accepted"], 0, raced)
            self.assertEqual(raced["counts"]["conflicts"], 1, raced)
            self.assertEqual(raced["partialFailures"],
                             [{"rowNo": 2, "reasonCode": "identity_conflict"}], raced)
            self.assertEqual(members(server, admin, org_b), [])
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                ownership = [tuple(row) for row in conn.execute(
                    "SELECT org_id, admission_status FROM org_memberships WHERE student_id = ?",
                    ("2024999411",)).fetchall()]
            finally:
                conn.close()
            self.assertEqual(ownership, [(org_a, "active")])

    def test_other_org_admin_cannot_revoke_key(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001073")
            self.assertEqual(code, 201, student)
            token = student["token"]
            victim = member_for(server, admin, PILOT, "2024001073")
            org_b = create_org(server, admin, "org-review-teach", "复审他组织")
            teacher = org_admin(server, admin, org_b, "2024999501")
            code, data = server.post("/api/orgs/%s/members/%s/revoke-key" % (org_b, victim["userId"]),
                                     {}, teacher["token"])
            self.assertEqual(code, 403, data)
            code, data = server.post("/api/orgs/%s/members/bulk" % org_b,
                                     {"action": "revoke", "userIds": [victim["userId"]],
                                      "requestId": "bulk-cross-org"}, admin)
            self.assertEqual(code, 403, data)
            code, me = server.get("/api/me", token)
            self.assertEqual(code, 200, me)
            self.assertEqual(me["relayKey"]["status"], "active")
            self.assertEqual(server.post("/api/consume", {"amount": 10, "requestId": "victim-still"},
                                         token)[0], 200)
            accounts = server.get("/api/admin/accounts?q=2024001073", admin)[1]["accounts"]
            self.assertEqual(next(item for item in accounts if item["identity"] == "2024001073")["key_status"],
                             "active")

    def test_org_scoped_revoke_and_disable_leave_other_org_account_alone(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001075")
            self.assertEqual(code, 201, student)
            token = student["token"]
            victim = member_for(server, admin, PILOT, "2024001075")
            hint = server.get("/api/me", token)[1]["relayKey"]["hint"]
            org_b = create_org(server, admin, "org-review-scope", "复审范围组织")
            teacher = org_admin(server, admin, org_b, "2024999502")
            # 导入已拒绝跨组织并入；这里直接写库，模拟历史上已存在的多组织归属。
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
                             "student_id, name, joined_at) VALUES (?, ?, 'org_member', 'active', ?, ?, ?, ?)",
                             (org_b, victim["userId"], "legacy-1", "2024001075", "历史成员", time.time()))
                conn.execute("COMMIT")
            finally:
                conn.close()

            code, data = server.post("/api/orgs/%s/members/%s/revoke-key" % (org_b, victim["userId"]),
                                     {}, teacher["token"])
            self.assertEqual(code, 403, data)
            code, updated = server.patch("/api/orgs/%s/members/%s" % (org_b, victim["userId"]),
                                         {"admissionStatus": "disabled"}, teacher["token"])
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "disabled")

            code, me = server.get("/api/me", token)
            self.assertEqual(code, 200, me)
            self.assertEqual(me["relayKey"]["hint"], hint)
            self.assertEqual(server.post("/api/consume", {"amount": 10, "requestId": "scope-still"},
                                         token)[0], 200)
            self.assertEqual(member_for(server, admin, PILOT, "2024001075")["admissionStatus"], "active")
            scoped = member_for(server, admin, org_b, "2024001075")
            self.assertEqual(scoped["admissionStatus"], "disabled")
            self.assertEqual(scoped["balance"], 0)
            self.assertEqual(scoped["spent"], 0)
            self.assertEqual(scoped["ownerRefs"], [])
            self.assertIsNone(scoped["keyHint"])
            pilot = member_for(server, admin, PILOT, "2024001075")
            self.assertEqual(pilot["balance"], 99990)
            self.assertEqual(len(pilot["ownerRefs"]), 1)


if __name__ == "__main__":
    unittest.main()
