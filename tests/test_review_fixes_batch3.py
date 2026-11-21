"""复审第三批的真实 HTTP 回归：导入身份口径归一，名册邮箱与学号按同一人处理。"""
import json
import sqlite3
import unittest

import app_server
import config
from tests.support import DemoServer, register

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


class ImportIdentityKeysTests(unittest.TestCase):
    def test_roster_email_adds_student_key_and_non_roster_email_does_not(self):
        roster_email = "2024001081@mail.hust.edu.cn"
        self.assertEqual(app_server._import_identity_keys({"email": roster_email}),
                         [("email", roster_email), ("student", "2024001081")])
        self.assertEqual(app_server._import_identity_keys({"student_id": "2024001081"}),
                         [("student", "2024001081")])
        self.assertEqual(app_server._import_identity_keys({"student_id": "2024001081", "email": roster_email}),
                         [("student", "2024001081"), ("email", roster_email)])
        self.assertEqual(app_server._import_identity_keys({"email": "2024001999@mail.hust.edu.cn"}),
                         [("email", "2024001999@mail.hust.edu.cn")])
        self.assertEqual(app_server._import_identity_keys({"email": "zhang.san@example.com"}),
                         [("email", "zhang.san@example.com")])

    def test_roster_email_without_student_id_conflicts_then_registering_stays_in_pilot(self):
        with DemoServer() as server:
            admin = server.admin_token()
            org_name = "第三批乙院"
            org_b = create_org(server, admin, "org-batch3-b", org_name)
            email = "2024001082@mail.hust.edu.cn"
            code, batch = preview(server, admin, org_b, ["b3-1,,%s,乙院学生,org_member,active" % email])
            self.assertEqual(code, 200, batch)
            self.assertEqual(batch["counts"]["accepted"], 0, batch)
            self.assertEqual(batch["counts"]["conflicts"], 1, batch)
            self.assertEqual(batch["rows"][0]["reasonCode"], "identity_conflict", batch)
            self.assertEqual(batch["rows"][0]["message"], "该身份已属于其他组织", batch)
            self.assertNotIn(org_name, json.dumps(batch, ensure_ascii=False))
            code, result = commit(server, admin, org_b, batch["batchId"])
            self.assertEqual(code, 200, result)
            self.assertEqual(result["counts"]["accepted"], 0, result)
            self.assertEqual(result["counts"]["conflicts"], 1, result)
            self.assertEqual(members(server, admin, org_b), [])
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                self.assertIsNone(conn.execute(
                    "SELECT user_id FROM user_identities WHERE kind = 'email' AND value_norm = ?",
                    (email,)).fetchone())
            finally:
                conn.close()

            # 同一学生随后在试点组织用同一邮箱注册，账户必须落在试点组织。
            code, account = register(server, email, mode="email")
            self.assertEqual(code, 201, account)
            self.assertEqual(account["org"]["id"], PILOT, account)
            self.assertEqual(account["identity"], email)
            roster_member = next(item for item in members(server, admin, PILOT)
                                 if item["studentId"] == "2024001082")
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                student = conn.execute("SELECT user_id, org_id FROM students WHERE identity = ?",
                                       (email,)).fetchone()
                owner = conn.execute(
                    "SELECT user_id FROM user_identities WHERE kind = 'student' AND value_norm = ?",
                    ("2024001082",)).fetchone()
                ownership = [tuple(row) for row in conn.execute(
                    "SELECT org_id FROM org_memberships WHERE user_id = ?", (owner[0],)).fetchall()]
            finally:
                conn.close()
            self.assertEqual(student[1], PILOT)
            self.assertEqual(roster_member["userId"], student[0])
            self.assertEqual(ownership, [(PILOT,)])

    def test_student_id_only_row_conflicts_when_user_has_roster_email(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, account = register(server, "2024001083@mail.hust.edu.cn", mode="email")
            self.assertEqual(code, 201, account)
            org_b = create_org(server, admin, "org-batch3-sym", "第三批对称学院")
            code, batch = preview(server, admin, org_b, ["b3-2,2024001083,,同人学号,org_member,active"])
            self.assertEqual(code, 200, batch)
            self.assertEqual(batch["counts"]["accepted"], 0, batch)
            self.assertEqual(batch["counts"]["conflicts"], 1, batch)
            self.assertEqual(batch["rows"][0]["reasonCode"], "identity_conflict", batch)
            self.assertEqual(batch["rows"][0]["message"], "该身份已属于其他组织", batch)
            code, result = commit(server, admin, org_b, batch["batchId"])
            self.assertEqual(code, 200, result)
            self.assertEqual(result["counts"]["accepted"], 0, result)
            self.assertEqual(result["counts"]["conflicts"], 1, result)
            self.assertEqual(members(server, admin, org_b), [])

    def test_non_roster_email_and_clean_rows_still_import(self):
        with DemoServer() as server:
            admin = server.admin_token()
            org_b = create_org(server, admin, "org-batch3-clean", "第三批导入学院")
            rows = [
                "b3-1,,2024001999@mail.hust.edu.cn,校外邮箱,org_member,active",
                "b3-2,,zhang.san@example.com,个人邮箱,org_member,active",
                "b3-3,2024999301,,只有学号,org_member,active",
                "b3-4,2024999302,2024999302@example.com,学号邮箱齐全,org_admin,active",
            ]
            code, batch = preview(server, admin, org_b, rows)
            self.assertEqual(code, 200, batch)
            self.assertEqual(batch["counts"]["accepted"], 4, batch)
            self.assertEqual(batch["counts"]["conflicts"], 0, batch)
            code, result = commit(server, admin, org_b, batch["batchId"])
            self.assertEqual(code, 200, result)
            self.assertEqual(result["counts"]["accepted"], 4, result)
            self.assertEqual(sorted(item["externalRef"] for item in members(server, admin, org_b)),
                             ["b3-1", "b3-2", "b3-3", "b3-4"])

    def test_pilot_import_of_own_roster_student_keeps_same_org_conflict(self):
        with DemoServer() as server:
            admin = server.admin_token()
            before = len(members(server, admin, PILOT))
            code, batch = preview(server, admin, PILOT, ["b3-pilot,2024001085,,试点学生,org_member,active"])
            self.assertEqual(code, 200, batch)
            self.assertEqual(batch["counts"]["accepted"], 0, batch)
            self.assertEqual(batch["counts"]["conflicts"], 1, batch)
            self.assertEqual(batch["rows"][0]["reasonCode"], "identity_conflict", batch)
            self.assertEqual(batch["rows"][0]["message"], "该身份已在组织内，但外部编号不同", batch)
            code, result = commit(server, admin, PILOT, batch["batchId"])
            self.assertEqual(code, 200, result)
            self.assertEqual(result["counts"]["accepted"], 0, result)
            self.assertEqual(result["counts"]["conflicts"], 1, result)
            self.assertEqual(len(members(server, admin, PILOT)), before)
            member = next(item for item in members(server, admin, PILOT) if item["studentId"] == "2024001085")
            self.assertEqual(member["externalRef"], "roster:2024001085")


if __name__ == "__main__":
    unittest.main()
