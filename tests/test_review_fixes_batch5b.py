"""复审第五批 B 的真实 HTTP 回归：成员变为待复核时吊销其已有密钥。"""
import sqlite3
import time
import unittest

import config
from tests.support import DemoServer, login, register

PILOT = config.PILOT_ORG["id"]


def members(server, admin, org_id):
    code, data = server.get("/api/orgs/%s/members" % org_id, admin)
    assert code == 200, data
    return data["members"]


def member_for(server, admin, org_id, student_id):
    return next(item for item in members(server, admin, org_id) if item["studentId"] == student_id)


def set_status(server, admin, org_id, user_id, status):
    return server.patch("/api/orgs/%s/members/%s" % (org_id, user_id), {"admissionStatus": status}, admin)


def account_for(server, admin, identity):
    code, data = server.get("/api/admin/accounts?q=%s" % identity, admin)
    assert code == 200, data
    return next(item for item in data["accounts"] if item["identity"] == identity)


def revoked_audits(server, admin, org_id):
    code, data = server.get("/api/orgs/%s/audit" % org_id, admin)
    assert code == 200, data
    return [event for event in data["events"] if event["action"] == "member.key_revoked"]


def relay_key_statuses(server, identity):
    """读中转站台账：该身份历次密钥的状态，用于区分「吊销」与「从未发放」。"""
    conn = sqlite3.connect(server.relay_path, timeout=15)
    try:
        rows = conn.execute("SELECT k.status FROM relay_keys k JOIN relay_accounts a ON a.owner_ref = k.owner_ref "
                            "WHERE a.identity = ? ORDER BY k.created_at", (identity,)).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def create_org(server, admin, slug, name):
    code, data = server.post("/api/orgs", {"name": name, "slug": slug}, admin)
    assert code == 201, data
    return data["id"]


def add_legacy_membership(server, org_id, user_id, student_id):
    """直接写库，模拟该用户在另一组织的既有归属（导入已拒绝跨组织并入）。"""
    conn = sqlite3.connect(server.db_path, timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
                     "student_id, name, joined_at) VALUES (?, ?, 'org_member', 'active', ?, ?, ?, ?)",
                     (org_id, user_id, "legacy-b5b", student_id, "历史成员", time.time()))
        conn.execute("COMMIT")
    finally:
        conn.close()


class PendingReviewRevokesKeyTests(unittest.TestCase):
    def test_pending_review_revokes_existing_key_and_blocks_rotate(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001151")
            self.assertEqual(code, 201, student)
            self.assertTrue(student["relayKey"]["plaintext"])
            self.assertEqual(account_for(server, admin, "2024001151")["key_status"], "active")

            user = member_for(server, admin, PILOT, "2024001151")
            code, updated = set_status(server, admin, PILOT, user["userId"], "pending_review")
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")
            # 台账里旧密钥是被吊销掉的，不是从未发放。
            self.assertIn("revoked", relay_key_statuses(server, "2024001151"))
            self.assertEqual(account_for(server, admin, "2024001151")["key_status"], "none")

            code, data = login(server, "2024001151")
            self.assertEqual(code, 200, data)
            self.assertEqual(data["admissionStatus"], "pending_review")
            self.assertIsNone(data["relayKey"]["plaintext"])
            self.assertEqual(data["relayKey"]["status"], "none")
            code, denied = server.post("/api/key/rotate", {"requestId": "rotate-pending"}, data["token"])
            self.assertEqual(code, 403, denied)
            # 改状态前建立的会话同样拿不到轮换能力。
            code, denied = server.post("/api/key/rotate", {"requestId": "rotate-pending-old"}, student["token"])
            self.assertEqual(code, 403, denied)

    def test_repeat_pending_review_does_not_repeat_revocation(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001152")
            self.assertEqual(code, 201, student)
            user = member_for(server, admin, PILOT, "2024001152")
            self.assertEqual(set_status(server, admin, PILOT, user["userId"], "pending_review")[0], 200)
            self.assertEqual(len(revoked_audits(server, admin, PILOT)), 1)

            code, again = set_status(server, admin, PILOT, user["userId"], "pending_review")
            self.assertEqual(code, 200, again)
            self.assertEqual(again["admissionStatus"], "pending_review")
            self.assertEqual(len(revoked_audits(server, admin, PILOT)), 1)

    def test_back_to_active_reissues_key_only_on_next_login(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001153")
            self.assertEqual(code, 201, student)
            user = member_for(server, admin, PILOT, "2024001153")
            self.assertEqual(set_status(server, admin, PILOT, user["userId"], "pending_review")[0], 200)
            self.assertEqual(account_for(server, admin, "2024001153")["key_status"], "none")

            code, restored = set_status(server, admin, PILOT, user["userId"], "active")
            self.assertEqual(code, 200, restored)
            self.assertEqual(restored["admissionStatus"], "active")
            # 改回 active 只解除限制，不自动补发；密钥由下一次登录补发。
            self.assertEqual(account_for(server, admin, "2024001153")["key_status"], "none")
            self.assertEqual(len(revoked_audits(server, admin, PILOT)), 1)

            code, data = login(server, "2024001153")
            self.assertEqual(code, 200, data)
            self.assertTrue(data["relayKey"]["plaintext"])
            self.assertEqual(data["relayKey"]["status"], "active")
            self.assertEqual(account_for(server, admin, "2024001153")["key_status"], "active")
            code, rotated = server.post("/api/key/rotate", {"requestId": "rotate-restored"}, data["token"])
            self.assertEqual(code, 200, rotated)

    def test_other_org_membership_change_keeps_key(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, student = register(server, "2024001154")
            self.assertEqual(code, 201, student)
            self.assertTrue(student["relayKey"]["plaintext"])
            user = member_for(server, admin, PILOT, "2024001154")
            org_b = create_org(server, admin, "batch5b-org-b", "第五批乙院")
            add_legacy_membership(server, org_b, user["userId"], "2024001154")
            other = member_for(server, admin, org_b, "2024001154")

            code, updated = set_status(server, admin, org_b, other["userId"], "pending_review")
            self.assertEqual(code, 200, updated)
            self.assertEqual(updated["admissionStatus"], "pending_review")
            # 只吊销本组织账户密钥：另一组织的归属状态与账户密钥都不受影响。
            self.assertEqual(member_for(server, admin, PILOT, "2024001154")["admissionStatus"], "active")
            self.assertEqual(account_for(server, admin, "2024001154")["key_status"], "active")
            self.assertNotIn("revoked", relay_key_statuses(server, "2024001154"))
            code, rotated = server.post("/api/key/rotate", {"requestId": "rotate-other-org"}, student["token"])
            self.assertEqual(code, 200, rotated)


if __name__ == "__main__":
    unittest.main()
