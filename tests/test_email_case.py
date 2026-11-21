"""邮箱身份大小写归一：旧库迁移、真实 HTTP 登录/注册、跨组织导入冲突。"""
import hashlib
import os
import sqlite3
import tempfile
import unittest

import app_server
import config
import identity
import migrations
import relay
from tests.support import TEST_AUTH_VALUE, DemoServer, login, register

HEADER = "external_ref,student_id,email,name,role,status\n"
PILOT = config.PILOT_ORG["id"]


def legacy_ref(mode, identity_value):
    """改动前 owner_ref_of 的算法：按原样身份哈希，用来构造旧库。"""
    return "stu_" + hashlib.sha256(("%s:%s" % (mode, identity_value)).encode("utf-8")).hexdigest()[:16]


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


def create_org(server, admin, slug, name):
    code, data = server.post("/api/orgs", {"name": name, "slug": slug}, admin)
    assert code == 201, data
    return data["id"]


def seed_legacy_email_db(db_path, relay_path, email_value, password):
    """构造一个改动前的库：邮箱身份带大小写、各引用表都挂着旧 owner_ref。"""
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    pw_hash = app_server.hash_password(password, salt)
    old_ref = legacy_ref("email", email_value)
    conn = sqlite3.connect(db_path)
    for statement in migrations.LEGACY_SCHEMA:
        conn.execute(statement)
    conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at) "
                 "VALUES (?, 'email', ?, ?, ?, 5000, 200, 1700000000)",
                 (old_ref, email_value, salt.hex(), pw_hash))
    conn.execute("INSERT INTO ledger (owner_ref, title, amount, kind, request_id, created_at) "
                 "VALUES (?, '旧注册赠送', 5000, 'grant', ?, 1700000001)", (old_ref, "grant:" + old_ref))
    conn.execute("INSERT INTO sessions (token_hash, owner_ref, expires_at) VALUES (?, ?, ?)",
                 (app_server.token_digest("legacy-session-token"), old_ref, 9999999999))
    conn.execute("INSERT INTO login_failures (owner_ref, failures, locked_until, updated_at) "
                 "VALUES (?, 0, 0, 1700000002)", (old_ref,))
    conn.commit()
    conn.close()
    relay.init(relay_path)
    relay_conn = sqlite3.connect(relay_path)
    relay_conn.execute("INSERT INTO relay_accounts (owner_ref, mode, identity, name, updated_at) "
                       "VALUES (?, 'email', ?, '旧张三', 1700000003)", (old_ref, email_value))
    relay_conn.execute("INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, "
                       "issue_request_id, created_at) VALUES ('rk_legacy', ?, ?, 'legacy-hint', 'ch-a', "
                       "'active', 'issue:legacy', 1700000004)",
                       (old_ref, relay.digest("legacy-plaintext")))
    relay_conn.commit()
    relay_conn.close()
    return old_ref


class EmailCaseMigrationTests(unittest.TestCase):
    def test_mixed_case_legacy_account_stays_loggable_after_migration(self):
        email_value = "Alice@Example.edu"
        server = DemoServer()
        old_ref = seed_legacy_email_db(server.db_path, server.relay_path, email_value, TEST_AUTH_VALUE)
        new_ref = identity.owner_ref_of("email", "alice@example.edu")
        self.assertNotEqual(old_ref, new_ref)
        with server:
            conn = sqlite3.connect(server.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                student = conn.execute("SELECT * FROM students WHERE owner_ref = ?", (new_ref,)).fetchone()
                self.assertIsNotNone(student, "students.owner_ref 应重算到归一后的值")
                self.assertEqual(student["identity"], "alice@example.edu")
                identity_row = conn.execute(
                    "SELECT * FROM user_identities WHERE kind = 'email' AND value_norm = ?",
                    ("alice@example.edu",)).fetchone()
                self.assertIsNotNone(identity_row)
                self.assertEqual(identity_row["value_display"], "alice@example.edu")
                membership = conn.execute("SELECT * FROM org_memberships WHERE user_id = ?",
                                          (student["user_id"],)).fetchone()
                self.assertEqual(membership["email"], "alice@example.edu")
                for table in ("ledger", "sessions", "login_failures"):
                    row = conn.execute("SELECT owner_ref FROM %s WHERE owner_ref = ?" % table,
                                       (new_ref,)).fetchone()
                    self.assertIsNotNone(row, "%s.owner_ref 应同步到新值" % table)
                    stale = conn.execute("SELECT 1 FROM %s WHERE owner_ref = ?" % table,
                                         (old_ref,)).fetchone()
                    self.assertIsNone(stale, "%s.owner_ref 不应残留旧值" % table)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM students").fetchone()[0], 1)
            finally:
                conn.close()

            relay_conn = sqlite3.connect(server.relay_path, timeout=15)
            relay_conn.row_factory = sqlite3.Row
            try:
                account = relay_conn.execute("SELECT * FROM relay_accounts").fetchone()
                self.assertEqual(account["owner_ref"], new_ref)
                self.assertEqual(account["identity"], "alice@example.edu")
                key = relay_conn.execute("SELECT * FROM relay_keys WHERE id = 'rk_legacy'").fetchone()
                self.assertEqual(key["owner_ref"], new_ref)
                self.assertEqual(key["status"], "revoked")
            finally:
                relay_conn.close()

            # 旧会话和台账仍可用；该账户准入为 pending_review，存量密钥按准入规则吊销。
            code, me = server.get("/api/me", "legacy-session-token")
            self.assertEqual(code, 200, me)
            self.assertEqual(me["identity"], "alice@example.edu")
            self.assertEqual(me["balance"], 5000)
            self.assertEqual(me["relayKey"]["status"], "none")
            self.assertIsNone(me["relayKey"]["plaintext"])
            self.assertIsNone(me["relayKey"]["hint"])
            self.assertEqual([item["amount"] for item in me["ledger"]], [5000])
            code, lower = login(server, "alice@example.edu", mode="email")
            self.assertEqual(code, 200, lower)
            self.assertEqual(lower["identity"], "alice@example.edu")
            code, upper = login(server, "ALICE@EXAMPLE.EDU", mode="email")
            self.assertEqual(code, 200, upper)
            self.assertEqual(upper["identity"], "alice@example.edu")
            self.assertEqual([item["amount"] for item in upper["ledger"]], [5000])

            # 重复执行不产生新的迁移版本，数据保持归一后的形态。
            self.assertEqual(migrations.migrate(server.db_path), [])
            self.assertEqual(migrations.fold_relay_email_case(server.relay_path), 0)

    def test_case_merge_collision_aborts_migration_with_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "collision.sqlite")
            salt = bytes.fromhex("00112233445566778899aabbccddeeff")
            pw_hash = app_server.hash_password(TEST_AUTH_VALUE, salt)
            conn = sqlite3.connect(db_path)
            for statement in migrations.LEGACY_SCHEMA:
                conn.execute(statement)
            for email_value in ("dup@example.edu", "DUP@Example.edu"):
                conn.execute(
                    "INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at) "
                    "VALUES (?, 'email', ?, ?, ?, 10, 0, 1)",
                    (legacy_ref("email", email_value), email_value, salt.hex(), pw_hash))
            conn.commit()
            conn.close()

            with self.assertRaises(migrations.MigrationConflict) as caught:
                migrations.migrate(db_path)
            message = str(caught.exception)
            self.assertIn("邮箱身份归一后出现重复", message)
            self.assertNotIn("dup@example.edu", message.lower())
            self.assertNotIn(TEST_AUTH_VALUE, message)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                versions = [row[0] for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2, 3, 4, 5, 6])
                rows = conn.execute("SELECT identity FROM students ORDER BY identity").fetchall()
                self.assertEqual([row[0] for row in rows], ["DUP@Example.edu", "dup@example.edu"])
                emails = conn.execute("SELECT value_norm FROM user_identities WHERE kind = 'email'").fetchall()
                self.assertEqual(len(emails), 2)
            finally:
                conn.close()
            with self.assertRaises(migrations.MigrationConflict):
                migrations.migrate(db_path)


class EmailCaseEndToEndTests(unittest.TestCase):
    def test_register_mixed_case_then_login_any_case_and_reject_duplicate(self):
        with DemoServer() as server:
            code, account = register(server, "2024001191@Mail.Example.edu", mode="email")
            self.assertEqual(code, 201, account)
            self.assertEqual(account["identity"], "2024001191@mail.example.edu")
            code, mixed = login(server, "2024001191@MAIL.EXAMPLE.EDU", mode="email")
            self.assertEqual(code, 200, mixed)
            self.assertEqual(mixed["identity"], "2024001191@mail.example.edu")
            code, again = register(server, "2024001191@mail.EXAMPLE.edu", mode="email")
            self.assertEqual(code, 409, again)
            conn = sqlite3.connect(server.db_path, timeout=15)
            try:
                count = conn.execute("SELECT COUNT(*) FROM students WHERE identity = ?",
                                     ("2024001191@mail.example.edu",)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)

    def test_cross_org_import_rejects_same_email_in_other_case(self):
        with DemoServer() as server:
            admin = server.admin_token()
            org_a = create_org(server, admin, "case-org-a", "大小写组织甲")
            org_b = create_org(server, admin, "case-org-b", "大小写组织乙")
            code, batch = preview(server, admin, org_a, ["case-a-1,alice,,甲组织学生,org_member,active"])
            self.assertEqual(code, 200, batch)
            code, committed = commit(server, admin, org_a, batch["batchId"])
            self.assertEqual(code, 200, committed)
            self.assertEqual(committed["counts"]["accepted"], 1, committed)

            code, account = register(server, "Alice@Example.edu", mode="email", org_id=org_a)
            self.assertEqual(code, 201, account)
            self.assertEqual(account["identity"], "alice@example.edu")
            self.assertEqual(account["org"]["id"], org_a)

            code, batch_b = preview(server, admin, org_b,
                                    ["case-b-1,,ALICE@Example.EDU,乙组织学生,org_member,active"])
            self.assertEqual(code, 200, batch_b)
            self.assertEqual(batch_b["counts"]["accepted"], 0, batch_b)
            self.assertEqual(batch_b["counts"]["conflicts"], 1, batch_b)
            self.assertEqual(batch_b["rows"][0]["reasonCode"], "identity_conflict", batch_b)
            self.assertEqual(batch_b["rows"][0]["message"], "该身份已属于其他组织", batch_b)
            code, result = commit(server, admin, org_b, batch_b["batchId"])
            self.assertEqual(code, 200, result)
            self.assertEqual(result["counts"]["accepted"], 0, result)
            self.assertEqual(result["counts"]["conflicts"], 1, result)
            self.assertEqual(members(server, admin, org_b), [])


if __name__ == "__main__":
    unittest.main()
