"""relay 返修回归：旧库启动、吊销不复活、轮换补偿与并发领取。"""
import contextlib
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import app_server
import relay
from tests.fake_new_api import FakeNewApi


@contextlib.contextmanager
def newapi_env(fake, timeout=2):
    values = {
        "NEW_API_ENABLED": "1",
        "NEW_API_BASE_URL": fake.base_url,
        "NEW_API_TOKEN": fake.token,
        "NEW_API_USER_ID": "1",
        "NEW_API_TIMEOUT": str(timeout),
    }
    with mock.patch.dict(os.environ, values, clear=False):
        yield


LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  status TEXT NOT NULL,
  issued INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS relay_keys (
  id TEXT PRIMARY KEY,
  owner_ref TEXT NOT NULL,
  user_id TEXT,
  key_hash TEXT NOT NULL UNIQUE,
  key_hint TEXT NOT NULL,
  channel_id TEXT NOT NULL REFERENCES channels(id),
  status TEXT NOT NULL,
  issue_request_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  revoked_at REAL,
  UNIQUE (owner_ref, issue_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_key_per_owner
  ON relay_keys (owner_ref) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS relay_accounts (
  owner_ref TEXT PRIMARY KEY,
  user_id TEXT,
  org_id TEXT,
  mode TEXT NOT NULL,
  identity TEXT NOT NULL,
  name TEXT,
  updated_at REAL NOT NULL
);
"""


def build_legacy_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO channels (id, label, status) VALUES ('ch-a', '旧通道', 'active')")
        now = time.time()
        conn.execute(
            "INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, "
            "issue_request_id, created_at) VALUES ('rk_legacy_active', 'legacy-active', ?, 'old-hint', "
            "'ch-a', 'active', 'r1', ?)", (relay.digest("old-active"), now))
        conn.execute(
            "INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, "
            "issue_request_id, created_at, revoked_at) VALUES ('rk_legacy_revoked', 'legacy-revoked', ?, "
            "'old-hint', 'ch-a', 'revoked', 'r1', ?, ?)", (relay.digest("old-revoked"), now - 10, now - 5))
        conn.commit()
    finally:
        conn.close()


def relay_rows(path, owner_ref=None):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if owner_ref:
            return conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? ORDER BY created_at",
                                (owner_ref,)).fetchall()
        return conn.execute("SELECT * FROM relay_keys ORDER BY created_at").fetchall()
    finally:
        conn.close()


def insert_existing_failed(path, owner_ref, request_id, name, provider_token_id=None, quota=1000):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, state, "
            "issue_request_id, provider_token_id, token_name, quota, attempts, next_attempt_at, created_at) "
            "VALUES (?, ?, ?, '', 'ch-newapi', 'failed', 'failed', ?, ?, ?, ?, 5, NULL, ?)",
            ("rk_failed_" + owner_ref, owner_ref, relay.digest("pending:rk_failed_" + owner_ref),
             request_id, provider_token_id, name, quota, time.time() - 60))
        conn.commit()
    finally:
        conn.close()


def insert_pending(path, owner_ref, request_id, name, quota=0):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, state, "
            "issue_request_id, token_name, quota, attempts, next_attempt_at, created_at) "
            "VALUES (?, ?, ?, '', 'ch-newapi', 'pending', 'pending', ?, ?, ?, 0, 0, ?)",
            ("rk_pending_" + owner_ref, owner_ref, relay.digest("pending:rk_pending_" + owner_ref),
             request_id, name, quota, time.time() - 60))
        conn.commit()
    finally:
        conn.close()


class LegacyStartupTests(unittest.TestCase):
    def test_old_database_startup_keeps_revoked_rows_revoked(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            relay_path = os.path.join(tmp, "relay.sqlite")
            main_path = os.path.join(tmp, "main.sqlite")
            build_legacy_db(relay_path)

            relay.init(relay_path)

            active = relay_rows(relay_path, "legacy-active")[0]
            revoked = relay_rows(relay_path, "legacy-revoked")[0]
            self.assertEqual(active["state"], "active")
            self.assertEqual(revoked["state"], "revoked")
            self.assertEqual(relay.key_view(relay_path, "legacy-revoked")["state"], "none")

            replay = relay.issue_key(relay_path, "legacy-revoked", "r1", quota=1000)
            self.assertEqual(replay["status"], "revoked")
            self.assertIsNone(replay["plaintext"])
            self.assertEqual(fake.create_count, 0)
            self.assertIsNone(fake.token_by_name("campus:legacy-revoked:r1"))

            with mock.patch.object(app_server, "DB_PATH", main_path), \
                    mock.patch.object(app_server, "RELAY_DB", relay_path), \
                    mock.patch.object(app_server, "DATA_DIR", tmp):
                app_server.setup()

            self.assertEqual(relay_rows(relay_path, "legacy-revoked")[0]["state"], "revoked")
            # 旧 active 行没有上游 token id，启动回填后应降级为 pending 等待补发。
            self.assertEqual(relay_rows(relay_path, "legacy-active")[0]["state"], "pending")

    def test_fresh_database_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            relay.init(path)
            conn = sqlite3.connect(path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(relay_keys)").fetchall()}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(relay_keys)").fetchall()}
            finally:
                conn.close()
            self.assertTrue({"state", "claim", "delivery_claim", "delivery_claim_at"} <= columns)
            self.assertIn("idx_relay_keys_due", indexes)


class RotateFailedCompensationTests(unittest.TestCase):
    def test_rotate_failed_row_deletes_upstream_token_by_id(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            old_name = "campus:rotate-by-id:r-old"
            old = fake.add_token(old_name, 1000, unlimited=True)
            insert_existing_failed(path, "rotate-by-id", "r-old", old_name, provider_token_id=old["id"])

            result = relay.rotate_key(path, "rotate-by-id", "r-new", quota=1000)

            self.assertEqual(result["status"], "active")
            self.assertTrue(result["plaintext"])
            self.assertIsNone(fake.token_by_name(old_name))
            states = [row["state"] for row in relay_rows(path, "rotate-by-id")]
            self.assertEqual(states, ["revoked", "active"])

    def test_rotate_failed_row_resolves_orphan_by_token_name(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            old_name = "campus:rotate-by-name:r-old"
            fake.add_token(old_name, 1000, unlimited=True)
            insert_existing_failed(path, "rotate-by-name", "r-old", old_name, provider_token_id=None)

            result = relay.rotate_key(path, "rotate-by-name", "r-new", quota=1000)

            self.assertEqual(result["status"], "active")
            self.assertIsNone(fake.token_by_name(old_name))
            self.assertEqual([row["state"] for row in relay_rows(path, "rotate-by-name")],
                             ["revoked", "active"])


class ConcurrentClaimTests(unittest.TestCase):
    def test_concurrent_retry_claims_once_and_keeps_active_token(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            insert_pending(path, "retry-race", "r1", "campus:retry-race:r1")

            barrier = threading.Barrier(2)
            entered = threading.Event()
            release = threading.Event()
            other_done = threading.Event()
            finish_calls = []
            finish_lock = threading.Lock()
            original_lookup = relay._quota_lookup_for
            original_finish = relay.finish_issue
            results = {}

            def gated_lookup(db_path, quota_lookup, row):
                barrier.wait(timeout=10)
                return original_lookup(db_path, quota_lookup, row)

            def gated_finish(job, deliver_plaintext=True):
                with finish_lock:
                    finish_calls.append(job)
                    first = len(finish_calls) == 1
                if first:
                    entered.set()
                    self.assertTrue(release.wait(10), "测试未释放网络窗口")
                return original_finish(job, deliver_plaintext)

            def run(name):
                try:
                    results[name] = relay.retry_due(path, owner_ref="retry-race",
                                                    owner_refs=["retry-race"], limit_pending=1)
                finally:
                    other_done.set()

            threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
            with mock.patch.object(relay, "_quota_lookup_for", new=gated_lookup), \
                    mock.patch.object(relay, "finish_issue", new=gated_finish):
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(5), "只有一个补发者进入网络阶段")
                self.assertTrue(other_done.wait(5), "另一个补发者未退出")
                release.set()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(finish_calls), 1)
            self.assertEqual(fake.create_count, 1)
            self.assertEqual(fake.delete_count, 0)
            token = fake.token_by_name("campus:retry-race:r1")
            self.assertIsNotNone(token)
            row = relay_rows(path, "retry-race")[0]
            self.assertEqual(row["state"], "active")
            self.assertEqual(row["provider_token_id"], token["id"])

    def test_concurrent_delivery_returns_plaintext_once(self):
        with FakeNewApi() as fake, tempfile.TemporaryDirectory() as tmp, newapi_env(fake):
            path = os.path.join(tmp, "relay.sqlite")
            relay.init(path)
            issued = relay.issue_key(path, "deliver-race", "r1", quota=1000)
            self.assertTrue(issued["plaintext"])
            conn = sqlite3.connect(path)
            try:
                conn.execute("UPDATE relay_keys SET delivered_at = NULL WHERE owner_ref = 'deliver-race'")
                conn.commit()
            finally:
                conn.close()

            entered = threading.Event()
            release = threading.Event()
            lock = threading.Lock()
            calls = {"n": 0}
            real_client = relay.NewApiClient(relay.NewApiConfig.from_env())

            class GatedClient:
                def get_key(self, provider_id):
                    with lock:
                        calls["n"] += 1
                        first = calls["n"] == 1
                    if first:
                        entered.set()
                        assert release.wait(10), "测试未释放取明文窗口"
                    return real_client.get_key(provider_id)

            gated = GatedClient()
            results = {}

            def run(name):
                results[name] = relay._deliver_active_key(path, issued["key_id"], True)

            first = threading.Thread(target=run, args=("first",))
            second = threading.Thread(target=run, args=("second",))
            with mock.patch.object(relay, "_client", new=lambda: gated):
                first.start()
                self.assertTrue(entered.wait(5), "第一个请求未进入取明文阶段")
                second.start()
                second.join(timeout=10)
                self.assertFalse(second.is_alive())
                release.set()
                first.join(timeout=10)

            self.assertFalse(first.is_alive())
            self.assertEqual(calls["n"], 1)
            plaintexts = [results["first"]["plaintext"], results["second"]["plaintext"]]
            self.assertEqual(sum(value is not None for value in plaintexts), 1)
            row = relay_rows(path, "deliver-race")[0]
            self.assertIsNotNone(row["delivered_at"])
            self.assertIsNone(row["delivery_claim"])


if __name__ == "__main__":
    unittest.main()
