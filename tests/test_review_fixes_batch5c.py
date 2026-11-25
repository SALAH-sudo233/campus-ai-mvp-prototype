"""复审第五批 C：登录与吊销串行化，以及启动时清扫存量密钥。"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app_server
import config
from identity import owner_ref_of
from tests.support import TEST_AUTH_VALUE, login, register
from tests.fake_new_api import FakeNewApi

PILOT = config.PILOT_ORG["id"]


class InProcessServer:
    """在本进程启动真实 HTTP 服务，便于精确卡住密钥发放窗口。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="campus-test-")
        self.saved = {}
        self.saved_env = {}
        self.new_api = FakeNewApi()
        self.server = None
        self.thread = None

    def __enter__(self):
        self.new_api.__enter__()
        for name, value in (("NEW_API_ENABLED", "1"), ("NEW_API_BASE_URL", self.new_api.base_url),
                            ("NEW_API_TOKEN", self.new_api.token), ("NEW_API_USER_ID", "1"),
                            ("NEW_API_TIMEOUT", "2"), ("DEMO_ALLOW_LOGIN", "0")):
            self.saved_env[name] = os.environ.get(name)
            os.environ[name] = value
        for name in ("DB_PATH", "RELAY_DB", "ADMIN_USERNAME", "ADMIN_PASSWORD"):
            self.saved[name] = getattr(app_server, name)
        app_server.DB_PATH = os.path.join(self.dir, "backend.sqlite")
        app_server.RELAY_DB = os.path.join(self.dir, "relay.sqlite")
        app_server.ADMIN_USERNAME = "test-admin"
        app_server.ADMIN_PASSWORD = "test-" + TEST_AUTH_VALUE

        class QuietHandler(app_server.Handler):
            def log_message(self, format, *args):
                pass

        app_server.setup()
        self.server = app_server.Server(("localhost", 0), QuietHandler)
        self.base = "http://localhost:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def db_path(self):
        return os.path.join(self.dir, "backend.sqlite")

    @property
    def relay_path(self):
        return os.path.join(self.dir, "relay.sqlite")

    def __exit__(self, exc_type, exc, tb):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
        for name, value in self.saved.items():
            setattr(app_server, name, value)
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.new_api.__exit__(exc_type, exc, tb)
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path, token=None):
        return self.request("GET", path, token=token)

    def post(self, path, body=None, token=None):
        return self.request("POST", path, body=body if body is not None else {}, token=token)

    def patch(self, path, body, token=None):
        return self.request("PATCH", path, body=body, token=token)

    def admin_token(self):
        code, data = self.post("/api/admin/login", {"username": "test-admin",
                                                    "password": "test-" + TEST_AUTH_VALUE})
        assert code == 200, data
        return data["token"]


def member_for(server, admin, student_id):
    code, data = server.get("/api/orgs/%s/members" % PILOT, admin)
    assert code == 200, data
    return next(item for item in data["members"] if item["studentId"] == student_id)


def relay_key_states(server, student_id):
    conn = sqlite3.connect(server.relay_path, timeout=15)
    try:
        return conn.execute("SELECT status, revoked_at FROM relay_keys WHERE owner_ref = ? ORDER BY created_at",
                            (owner_ref_of("student", student_id),)).fetchall()
    finally:
        conn.close()


class LoginRevocationRaceTests(unittest.TestCase):
    def test_login_network_phase_does_not_hold_lock_and_revokes_after_status_change(self):
        with InProcessServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001161")
            self.assertEqual(code, 201, student)
            member = member_for(server, admin, "2024001161")
            code, revoked = server.post("/api/orgs/%s/members/%s/revoke-key" % (PILOT, member["userId"]),
                                        {}, admin)
            self.assertEqual(code, 200, revoked)

            entered = threading.Event()
            release = threading.Event()
            patch_done = threading.Event()
            responses = {}
            original_finish = app_server._finish_key_issue

            def gated_finish(job, user_id=None):
                entered.set()
                self.assertTrue(release.wait(5), "测试未释放网络窗口")
                return original_finish(job, user_id)

            def login_request():
                responses["login"] = login(server, "2024001161")

            def pending_review_request():
                responses["patch"] = server.patch(
                    "/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                    {"admissionStatus": "pending_review"}, admin)
                patch_done.set()

            app_server._finish_key_issue = gated_finish
            login_thread = threading.Thread(target=login_request)
            patch_thread = threading.Thread(target=pending_review_request)
            try:
                login_thread.start()
                self.assertTrue(entered.wait(5), "登录未进入密钥发放窗口")
                patch_thread.start()
                self.assertTrue(patch_done.wait(2), "网络阶段不应持有全局 LOCK")
            finally:
                release.set()
                login_thread.join(timeout=10)
                patch_thread.join(timeout=10)
                app_server._finish_key_issue = original_finish

            self.assertFalse(login_thread.is_alive())
            self.assertFalse(patch_thread.is_alive())
            code, logged_in = responses["login"]
            self.assertEqual(code, 200, logged_in)
            self.assertIsNone(logged_in["relayKey"]["plaintext"])
            code, updated = responses["patch"]
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")
            states = relay_key_states(server, "2024001161")
            self.assertTrue(states)
            self.assertFalse(any(status == "active" for status, _ in states), states)
            self.assertTrue(any(status == "revoked" for status, _ in states), states)


class SetupRevokesLegacyKeysTests(unittest.TestCase):
    def test_setup_revokes_active_key_for_existing_pending_review_member(self):
        with InProcessServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001162")
            self.assertEqual(code, 201, student)
            self.assertTrue(student["relayKey"]["plaintext"])
            member = member_for(server, admin, "2024001162")

            # 绕过状态变更接口，模拟迁移后遗留的 pending_review 成员仍持有 active 密钥。
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                conn.execute("UPDATE org_memberships SET admission_status = 'pending_review' "
                             "WHERE org_id = ? AND user_id = ?", (PILOT, member["userId"]))
                conn.commit()
            finally:
                conn.close()
            self.assertTrue(any(status == "active" for status, _ in relay_key_states(server, "2024001162")))

            app_server.setup()
            states = relay_key_states(server, "2024001162")
            self.assertFalse(any(status == "active" for status, _ in states), states)
            revoked_at = [stamp for status, stamp in states if status == "revoked"]
            self.assertTrue(revoked_at)

            # 重复启动只做幂等清扫，不再次改写已吊销密钥。
            app_server.setup()
            again = relay_key_states(server, "2024001162")
            self.assertEqual(again, states)


if __name__ == "__main__":
    unittest.main()
