"""复审第五批 D：发钥入口统一走锁内重读准入，轮换与注册不再「吊销后又补发」。"""
import threading
import unittest

import app_server
import authz
import relay
import config
from identity import owner_ref_of
from tests.support import register
from tests.test_review_fixes_batch5c import InProcessServer, member_for, relay_key_states

PILOT = config.PILOT_ORG["id"]


class RotateIssuanceRaceTests(unittest.TestCase):
    def test_rotate_holds_lock_so_pending_review_revocation_wins(self):
        with InProcessServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001197")
            self.assertEqual(code, 201, student)
            self.assertIsNotNone(student["relayKey"]["plaintext"])
            member = member_for(server, admin, "2024001197")

            entered = threading.Event()
            release = threading.Event()
            patch_done = threading.Event()
            responses = {}
            original_rotate = relay.rotate_key

            def gated_rotate(path, owner_ref, request_id):
                entered.set()
                self.assertTrue(release.wait(5), "测试未释放轮换窗口")
                return original_rotate(path, owner_ref, request_id)

            def rotate_request():
                responses["rotate"] = server.post("/api/key/rotate", {"requestId": "race-rotate"},
                                                  student["token"])

            def pending_review_request():
                responses["patch"] = server.patch(
                    "/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                    {"admissionStatus": "pending_review"}, admin)
                patch_done.set()

            rotate_thread = threading.Thread(target=rotate_request)
            patch_thread = None
            relay.rotate_key = gated_rotate
            try:
                rotate_thread.start()
                self.assertTrue(entered.wait(5), "轮换未进入发钥窗口")
                patch_thread = threading.Thread(target=pending_review_request)
                patch_thread.start()
                self.assertFalse(patch_done.wait(0.5), "成员吊销未等待同一账户的在途轮换")
            finally:
                release.set()
                rotate_thread.join(timeout=10)
                if patch_thread is not None:
                    patch_thread.join(timeout=10)
                relay.rotate_key = original_rotate

            self.assertFalse(rotate_thread.is_alive())
            self.assertTrue(patch_thread is not None and not patch_thread.is_alive())
            code, rotated = responses["rotate"]
            self.assertEqual(code, 200, rotated)
            self.assertIsNotNone(rotated["plaintext"])
            code, updated = responses["patch"]
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")
            states = relay_key_states(server, "2024001197")
            self.assertTrue(states)
            self.assertFalse(any(status == "active" for status, _ in states), states)

    def test_rotate_rejects_and_revokes_when_admission_is_no_longer_active(self):
        with InProcessServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001198")
            self.assertEqual(code, 201, student)
            member = member_for(server, admin, "2024001198")
            code, updated = server.patch(
                "/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                {"admissionStatus": "pending_review"}, admin)
            self.assertEqual(code, 200, updated)

            # 绕过路由层权限矩阵，直接验证发钥入口的非准入分支：不发新密钥，并幂等吊销残留密钥。
            code, body = app_server.do_rotate(owner_ref_of("student", "2024001198"), "direct-rotate")
            self.assertEqual(code, 403, body)
            self.assertEqual(body["error"], authz.PENDING_REVIEW_MESSAGE)
            states = relay_key_states(server, "2024001198")
            self.assertFalse(any(status == "active" for status, _ in states), states)
            again = app_server.do_rotate(owner_ref_of("student", "2024001198"), "direct-rotate-2")
            self.assertEqual(again[0], 403, again[1])
            self.assertEqual(relay_key_states(server, "2024001198"), states)


class RegisterIssuanceRaceTests(unittest.TestCase):
    def test_register_cannot_issue_after_pending_review_revocation(self):
        with InProcessServer() as server:
            admin = server.admin_token()
            entered = threading.Event()
            release = threading.Event()
            patch_done = threading.Event()
            responses = {}
            original_ensure_key = app_server.ensure_key
            target = owner_ref_of("student", "2024001199")

            def gated_ensure_key(owner_ref, *args, **kwargs):
                if owner_ref == target:
                    entered.set()
                    self.assertTrue(release.wait(5), "测试未释放发钥窗口")
                return original_ensure_key(owner_ref, *args, **kwargs)

            def register_request():
                responses["register"] = register(server, "2024001199")

            def pending_review_request():
                member = member_for(server, admin, "2024001199")
                responses["patch"] = server.patch(
                    "/api/orgs/%s/members/%s" % (PILOT, member["userId"]),
                    {"admissionStatus": "pending_review"}, admin)
                patch_done.set()

            register_thread = threading.Thread(target=register_request)
            patch_thread = None
            app_server.ensure_key = gated_ensure_key
            try:
                register_thread.start()
                self.assertTrue(entered.wait(10), "注册未进入发钥窗口")
                patch_thread = threading.Thread(target=pending_review_request)
                patch_thread.start()
                self.assertFalse(patch_done.wait(0.5), "成员吊销未等待同一账户的在途注册")
            finally:
                release.set()
                register_thread.join(timeout=10)
                if patch_thread is not None:
                    patch_thread.join(timeout=10)
                app_server.ensure_key = original_ensure_key

            self.assertFalse(register_thread.is_alive())
            self.assertTrue(patch_thread is not None and not patch_thread.is_alive())
            code, registered = responses["register"]
            self.assertEqual(code, 201, registered)
            code, updated = responses["patch"]
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")
            states = relay_key_states(server, "2024001199")
            self.assertTrue(states)
            self.assertFalse(any(status == "active" for status, _ in states), states)


if __name__ == "__main__":
    unittest.main()
