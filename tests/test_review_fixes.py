"""复审问题的真实 HTTP 回归：停用失效、导入清理、权限矩阵、不存在组织。"""
import sqlite3
import time
import unittest

import authz
import config
from tests.support import TEST_AUTH_VALUE, DemoServer, register

HEADER = "external_ref,student_id,email,name,role,status\n"
PILOT = config.PILOT_ORG["id"]


def member_for(server, admin, student_id):
    code, data = server.get("/api/orgs/%s/members" % PILOT, admin)
    assert code == 200, data
    return next(item for item in data["members"] if item["studentId"] == student_id)


class DisabledMemberTests(unittest.TestCase):
    def test_disable_blocks_old_session_quota_and_key(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001021")
            self.assertEqual(code, 201, student)
            token = student["token"]
            user = member_for(server, admin, "2024001021")
            self.assertEqual(server.get("/api/me", token)[0], 200)
            self.assertEqual(server.get("/api/me", token)[1]["relayKey"]["status"], "active")

            code, updated = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                         {"admissionStatus": "disabled"}, admin)
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "disabled")

            self.assertEqual(server.get("/api/me", token)[0], 401)
            self.assertEqual(server.post("/api/consume", {"amount": 10, "requestId": "after-disable"}, token)[0], 401)
            self.assertEqual(server.post("/api/key/rotate", {"requestId": "rotate-after-disable"}, token)[0], 401)
            self.assertEqual(server.get("/api/works", token)[0], 401)

            member = member_for(server, admin, "2024001021")
            self.assertEqual(member["balance"], 100000)
            accounts = server.get("/api/admin/accounts?q=2024001021", admin)[1]["accounts"]
            self.assertEqual(next(item for item in accounts if item["identity"] == "2024001021")["key_status"], "none")
            code, denied = server.post("/api/login", {"mode": "student", "identity": "2024001021",
                                                      "password": TEST_AUTH_VALUE,
                                                      "requestId": "login-disabled"})
            self.assertEqual(code, 403, denied)

            code, _ = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                   {"admissionStatus": "active"}, admin)
            self.assertEqual(code, 200)
            code, again = server.post("/api/login", {"mode": "student", "identity": "2024001021",
                                                     "password": TEST_AUTH_VALUE,
                                                     "requestId": "login-after-enable"})
            self.assertEqual(code, 200, again)
            self.assertEqual(server.post("/api/consume", {"amount": 10, "requestId": "after-enable"},
                                         again["token"])[0], 200)

    def test_cannot_disable_self(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, teacher = register(server, "2024001023")
            self.assertEqual(code, 201, teacher)
            user = member_for(server, admin, "2024001023")
            code, _ = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                   {"role": "org_admin"}, admin)
            self.assertEqual(code, 200)

            code, body = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                      {"admissionStatus": "disabled"}, teacher["token"])
            self.assertEqual(code, 400, body)
            self.assertIn("不能停用自己", body["error"])
            self.assertEqual(server.get("/api/me", teacher["token"])[0], 200)
            self.assertEqual(member_for(server, admin, "2024001023")["admissionStatus"], "active")

    def test_pending_review_can_login_and_read_self_only(self):
        """用户已拍板：待复核只保留登录、个人信息（含余额流水）、引导和登出，其余成员动作 403。"""
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001022")
            self.assertEqual(code, 201, student)
            code, work = server.post("/api/works", {"title": "待复核前作品", "summary": "复核前提交。",
                                                     "category": "学习"}, student["token"])
            self.assertEqual(code, 201, work)
            user = member_for(server, admin, "2024001022")
            code, updated = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                         {"admissionStatus": "pending_review"}, admin)
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")

            code, relogin = server.post("/api/login", {"mode": "student", "identity": "2024001022",
                                                       "password": TEST_AUTH_VALUE,
                                                       "requestId": "login-pending"})
            self.assertEqual(code, 200, relogin)
            self.assertEqual(relogin["admissionStatus"], "pending_review")
            pending = relogin["token"]
            code, me = server.get("/api/me", pending)
            self.assertEqual(code, 200, me)
            self.assertEqual(me["admissionStatus"], "pending_review")
            self.assertEqual(me["balance"], 100000)
            self.assertTrue(me["ledger"], me)
            self.assertEqual(server.get("/api/onboarding", pending)[0], 200)
            code, progress = server.post("/api/onboarding", {"action": "start", "stepId": "team-basics"},
                                         pending)
            self.assertEqual(code, 200, progress)

            denied = [
                ("/api/consume", {"amount": 10, "requestId": "pending-consume"}),
                ("/api/key/rotate", {"requestId": "pending-rotate"}),
                ("/api/works", {"title": "待复核作品", "summary": "不应写入。", "category": "学习"}),
                ("/api/works/%s/withdraw" % work["workId"], {}),
            ]
            for path, body in denied:
                code, data = server.post(path, body, pending)
                self.assertEqual(code, 403, (path, data))
                self.assertEqual(data.get("error"), authz.PENDING_REVIEW_MESSAGE, (path, data))
            for path in ("/api/works", "/api/works/mine", "/api/works/%s" % work["workId"]):
                self.assertEqual(server.get(path, pending)[0], 403, path)
            self.assertEqual(server.post("/api/logout", {}, pending)[0], 200)

            code, _ = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                   {"admissionStatus": "active"}, admin)
            self.assertEqual(code, 200)
            code, active = server.post("/api/login", {"mode": "student", "identity": "2024001022",
                                                       "password": TEST_AUTH_VALUE,
                                                       "requestId": "login-pending-active"})
            self.assertEqual(code, 200, active)
            token = active["token"]
            self.assertEqual(server.post("/api/consume", {"amount": 10, "requestId": "active-consume"}, token)[0], 200)
            self.assertEqual(server.post("/api/key/rotate", {"requestId": "active-rotate"}, token)[0], 200)
            self.assertEqual(server.get("/api/works", token)[0], 200)
            code, again = server.post("/api/works", {"title": "复核后作品", "summary": "复核通过后提交。",
                                                     "category": "学习"}, token)
            self.assertEqual(code, 201, again)
            self.assertEqual(server.post("/api/works/%s/withdraw" % work["workId"], {}, token)[0], 200)


class ImportCleanupTests(unittest.TestCase):
    def _insert_expired_batches(self, server, count):
        now = time.time()
        conn = sqlite3.connect(server.db_path, timeout=15)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for index in range(count):
                batch_id = "imp_old_%03d" % index
                conn.execute(
                    "INSERT INTO import_batches (id, org_id, actor_user_id, file_sha256, schema_version, status, "
                    "preview_digest, expires_at, created_at, total_rows) "
                    "VALUES (?, ?, NULL, ?, 1, 'previewed', ?, ?, ?, 1)",
                    (batch_id, PILOT, "0" * 64, batch_id, now - 3 * 86400, now - 4 * 86400))
                conn.execute("INSERT INTO import_rows (batch_id, row_no, row_key, fields_json, result) "
                             "VALUES (?, 1, 'r', '{}', 'rejected')", (batch_id,))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _old_batch_count(self, server):
        conn = sqlite3.connect(server.db_path, timeout=15)
        try:
            return conn.execute("SELECT COUNT(*) FROM import_batches WHERE id GLOB 'imp_old_*'").fetchone()[0]
        finally:
            conn.close()

    def test_preview_survives_more_than_200_expired_batches(self):
        with DemoServer() as server:
            admin = server.admin_token()
            self._insert_expired_batches(server, 201)
            self.assertEqual(self._old_batch_count(server), 201)
            body = (HEADER + "fix-1,2024999201,2024999201@example.edu.cn,清理测试,org_member,active\n").encode("utf-8")
            code, preview = server.request("POST", "/api/orgs/%s/imports/preview" % PILOT, raw=body, token=admin)
            self.assertEqual(code, 200, preview)
            self.assertEqual(preview["counts"]["accepted"], 1)
            self.assertEqual(self._old_batch_count(server), 0)

    def test_preview_survives_cleanup_failure(self):
        with DemoServer() as server:
            admin = server.admin_token()
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                conn.execute("CREATE TRIGGER refuse_cleanup BEFORE DELETE ON import_batches "
                             "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
                conn.commit()
            finally:
                conn.close()
            body = (HEADER + "fix-2,2024999202,2024999202@example.edu.cn,清理失败,org_member,active\n").encode("utf-8")
            code, preview = server.request("POST", "/api/orgs/%s/imports/preview" % PILOT, raw=body, token=admin)
            self.assertEqual(code, 200, preview)
            self.assertEqual(preview["counts"]["accepted"], 1)


class AuthzMatrixCoverageTests(unittest.TestCase):
    def test_self_service_routes_are_registered_in_matrix(self):
        self_service = [("profile", "read_self"), ("onboarding", "read"), ("keys", "rotate_self"),
                        ("usage", "consume_self"), ("sessions", "revoke_self")]
        for role in authz.ROLES:
            for resource, action in self_service:
                self.assertIsNotNone(authz.scope_for(role, resource, action), (role, resource, action))
        self.assertEqual(authz.scope_for(authz.PLATFORM_ADMIN, "accounts", "list"), "all")
        self.assertEqual(authz.scope_for(authz.ORG_ADMIN, "accounts", "revoke_key"), "org")
        self.assertIsNone(authz.scope_for(authz.ORG_MEMBER, "accounts", "list"))
        self.assertIsNone(authz.scope_for(authz.ORG_MEMBER, "accounts", "revoke_key"))
        self.assertIsNone(authz.scope_for(authz.ORG_ADMIN, "works", "read_any"))
        self.assertEqual(authz.scope_for(authz.PLATFORM_ADMIN, "works", "read_any"), "all")
        self.assertTrue(authz.work_readable({"role": authz.PLATFORM_ADMIN, "user_id": None}, "org-x", None, False))

    def test_admin_and_self_routes_keep_existing_behavior(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, teacher = register(server, "2024001024")
            self.assertEqual(code, 201, teacher)
            user = member_for(server, admin, "2024001024")
            code, _ = server.patch("/api/orgs/%s/members/%s" % (PILOT, user["userId"]),
                                   {"role": "org_admin"}, admin)
            self.assertEqual(code, 200)
            token = teacher["token"]
            self.assertEqual(server.get("/api/onboarding", token)[0], 200)
            self.assertEqual(server.post("/api/consume", {"amount": 10, "requestId": "admin-consume"}, token)[0], 200)
            self.assertEqual(server.post("/api/key/rotate", {"requestId": "admin-rotate"}, token)[0], 200)
            code, accounts = server.get("/api/admin/accounts", token)
            self.assertEqual(code, 200, accounts)
            self.assertTrue(all(item["org_id"] == PILOT for item in accounts["accounts"]))
            self.assertEqual(server.post("/api/logout", {}, token)[0], 200)
            self.assertEqual(server.get("/api/me", token)[0], 401)

            code, member = register(server, "2024001025")
            self.assertEqual(code, 201, member)
            self.assertEqual(server.get("/api/admin/accounts", member["token"])[0], 403)

    def test_work_detail_permissions_unchanged(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, author = register(server, "2024001026")
            self.assertEqual(code, 201, author)
            code, other = register(server, "2024001027")
            self.assertEqual(code, 201, other)
            code, work = server.post("/api/works", {"title": "权限回归作品", "summary": "详情读取口径。",
                                                    "category": "学习"}, author["token"])
            self.assertEqual(code, 201, work)
            path = "/api/works/%s" % work["workId"]
            self.assertEqual(server.get(path, author["token"])[0], 200)
            self.assertEqual(server.get(path, other["token"])[0], 403)
            # 后台管理员会话不是学生会话，保持原有 401；平台管理员走 /api/orgs/<id>/works。
            self.assertEqual(server.get(path, admin)[0], 401)
            self.assertEqual(server.get("/api/works/work_missing", author["token"])[0], 404)
            code, approved = server.post("/api/orgs/%s/works/%s/review" % (PILOT, work["workId"]),
                                         {"decision": "approve"}, admin)
            self.assertEqual(code, 200, approved)
            self.assertEqual(server.get(path, other["token"])[0], 200)


class MissingOrgTests(unittest.TestCase):
    def test_missing_org_returns_404_instead_of_500_or_fake_success(self):
        with DemoServer() as server:
            admin = server.admin_token()
            missing = "org_does_not_exist"
            for name in ("tree", "summary", "trends", "dashboard", "members", "works", "audit",
                         "imports/template"):
                code, data = server.get("/api/orgs/%s/%s" % (missing, name), admin)
                self.assertEqual(code, 404, (name, data))

            body = (HEADER + "missing-1,2024999301,2024999301@example.edu.cn,不存在组织,org_member,active\n"
                    ).encode("utf-8")
            code, data = server.request("POST", "/api/orgs/%s/imports/preview" % missing, raw=body, token=admin)
            self.assertEqual(code, 404, data)
            code, data = server.post("/api/orgs/%s/imports/imp_missing/commit" % missing,
                                     {"requestId": "missing-commit"}, admin)
            self.assertEqual(code, 404, data)
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                leftover = conn.execute("SELECT COUNT(*) FROM import_batches WHERE org_id = ?",
                                        (missing,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(leftover, 0)


if __name__ == "__main__":
    unittest.main()
