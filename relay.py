"""模拟中转站：给学生发放中转站密钥，并维护一份实名台账。只依赖标准库，数据存在自己的 SQLite 文件里。

边界约定，和生产版的中转站保持一致：
- 中转站保存实名台账（owner_ref、登录身份、姓名），用于管理后台查看和吊销。姓名来自演示名册，名册外留空。
- 中转站只保存密钥的 SHA-256 摘要，明文只在发放那一次返回。
- 发放和重新生成都带请求编号，重复请求不会多发密钥。
- 上游真实密钥只在中转站内部，不会返回给平台和学生。这里的上游通道是假数据。
"""
import hashlib
import os
import secrets
import sqlite3
import time

KEY_PREFIX = "sk-mock-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  status TEXT NOT NULL,
  issued INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS relay_keys (
  id TEXT PRIMARY KEY,
  owner_ref TEXT NOT NULL,
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
  mode TEXT NOT NULL,
  identity TEXT NOT NULL,
  name TEXT,
  updated_at REAL NOT NULL
);
"""

MOCK_CHANNELS = [("ch-a", "模拟上游通道 A"), ("ch-b", "模拟上游通道 B"), ("ch-c", "模拟上游通道 C")]


class RelayUnavailable(Exception):
    pass


class AlreadyIssued(Exception):
    pass


def connect(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        for channel_id, label in MOCK_CHANNELS:
            conn.execute("INSERT OR IGNORE INTO channels (id, label, status) VALUES (?, ?, 'active')",
                         (channel_id, label))
    finally:
        conn.close()


def reset(path):
    conn = connect(path)
    try:
        conn.execute("DELETE FROM relay_keys")
        conn.execute("DELETE FROM relay_accounts")
        conn.execute("UPDATE channels SET issued = 0")
    finally:
        conn.close()


def digest(plain):
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def hint_of(plain):
    return plain[:len(KEY_PREFIX) + 4] + "…" + plain[-4:]


def _check_up():
    if os.environ.get("MOCK_RELAY_DOWN") == "1":
        raise RelayUnavailable("中转站暂时不可用")


def _replay(conn, owner_ref, request_id):
    row = conn.execute("SELECT id, key_hint, status FROM relay_keys WHERE owner_ref = ? AND issue_request_id = ?",
                       (owner_ref, request_id)).fetchone()
    if row is None:
        return None
    return {"key_id": row["id"], "hint": row["key_hint"], "plaintext": None, "replayed": True,
            "status": row["status"]}


def _insert_key(conn, owner_ref, request_id):
    channel = conn.execute(
        "SELECT id FROM channels WHERE status = 'active' ORDER BY issued, id LIMIT 1").fetchone()
    if channel is None:
        raise RelayUnavailable("没有可用的上游通道")
    plain = KEY_PREFIX + secrets.token_hex(16)
    key_id = "rk_" + secrets.token_hex(6)
    conn.execute(
        "INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, issue_request_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (key_id, owner_ref, digest(plain), hint_of(plain), channel["id"], request_id, time.time()))
    conn.execute("UPDATE channels SET issued = issued + 1 WHERE id = ?", (channel["id"],))
    return {"key_id": key_id, "hint": hint_of(plain), "plaintext": plain, "replayed": False, "status": "active"}


def upsert_account(path, owner_ref, mode, identity, name):
    """写入或更新中转站台账。按第 4 题决定，台账存实名，不做匿名化。"""
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO relay_accounts (owner_ref, mode, identity, name, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_ref) DO UPDATE SET mode = ?, identity = ?, name = ?, updated_at = ?",
            (owner_ref, mode, identity, name, time.time(), mode, identity, name, time.time()))
    finally:
        conn.close()


def issue_key(path, owner_ref, request_id):
    _check_up()
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = _replay(conn, owner_ref, request_id)
        if replay is not None:
            conn.execute("COMMIT")
            return replay
        active = conn.execute("SELECT id FROM relay_keys WHERE owner_ref = ? AND status = 'active'",
                              (owner_ref,)).fetchone()
        if active is not None:
            conn.execute("ROLLBACK")
            raise AlreadyIssued("该学生已有有效密钥，请使用重新生成")
        result = _insert_key(conn, owner_ref, request_id)
        conn.execute("COMMIT")
        return result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def rotate_key(path, owner_ref, request_id):
    _check_up()
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = _replay(conn, owner_ref, request_id)
        if replay is not None:
            conn.execute("COMMIT")
            return replay
        conn.execute("UPDATE relay_keys SET status = 'revoked', revoked_at = ? "
                     "WHERE owner_ref = ? AND status = 'active'", (time.time(), owner_ref))
        result = _insert_key(conn, owner_ref, request_id)
        conn.execute("COMMIT")
        return result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def key_info(path, plain):
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT k.id, k.status, c.label FROM relay_keys k JOIN channels c ON c.id = k.channel_id "
            "WHERE k.key_hash = ?", (digest(plain),)).fetchone()
    finally:
        conn.close()
    if row is None or row["status"] != "active":
        return None
    return {"key_id": row["id"], "channel": row["label"]}


def active_hint(path, owner_ref):
    conn = connect(path)
    try:
        row = conn.execute("SELECT key_hint FROM relay_keys WHERE owner_ref = ? AND status = 'active'",
                           (owner_ref,)).fetchone()
    finally:
        conn.close()
    return row["key_hint"] if row else None

def list_accounts(path, query=None):
    """管理页用：列出台账，可按学号/邮箱/姓名模糊搜索，带当前密钥状态。"""
    conn = connect(path)
    try:
        sql = ("SELECT a.owner_ref, a.mode, a.identity, a.name, a.updated_at, "
               "k.id AS key_id, k.key_hint, k.status AS key_status, k.created_at AS key_created_at "
               "FROM relay_accounts a LEFT JOIN relay_keys k "
               "ON k.owner_ref = a.owner_ref AND k.status = 'active' ")
        params = ()
        if query:
            sql += "WHERE a.identity LIKE ? OR a.name LIKE ? "
            like = "%" + query + "%"
            params = (like, like)
        sql += "ORDER BY a.updated_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{"owner_ref": r["owner_ref"], "mode": r["mode"], "identity": r["identity"], "name": r["name"],
             "key_id": r["key_id"], "key_hint": r["key_hint"], "key_status": r["key_status"] or "none"}
            for r in rows]


def revoke_by_owner(path, owner_ref):
    """管理页用：吊销某账户当前有效密钥。没有有效密钥时视为已吊销，不报错。"""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE relay_keys SET status = 'revoked', revoked_at = ? "
                     "WHERE owner_ref = ? AND status = 'active'", (time.time(), owner_ref))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
