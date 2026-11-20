"""原型后端：模拟学生注册、密码登录、Token 发放和中转站密钥发放。只用标准库，python3 server.py 即可运行。

流程：
1. 注册：学生用校园邮箱或学号加自设密码注册。服务端存加盐哈希，不存明文。
2. 注册成功后，服务端幂等发放 100,000 Token，并向中转站（relay.py）申请一枚密钥。
3. 登录：校验密码后发会话令牌。之后的接口只认令牌，不认前端传来的学生编号。
4. 中转站按设计维护实名台账（学生编号、登录身份、姓名），供管理后台查看与吊销；密钥明文只在发放那一次返回。

生产版把账号密码换成 HUST SSO 或 CAS，把账本和中转站换成真实服务。这里只有演示数据。
"""
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import threading
import time
import urllib.parse

import relay
import roster

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DEMO_DB", os.path.join(ROOT, "demo-backend.sqlite"))
RELAY_DB = os.environ.get("DEMO_RELAY_DB", os.path.join(ROOT, "demo-relay.sqlite"))
INITIAL_GRANT = 100000
MIN_PASSWORD = 8
MAX_FAILURES = 5
FAILURE_TTL = 900
FAILURE_CAP = 10000
LOCK_SECONDS = 60
SESSION_SECONDS = 8 * 3600
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_SESSION_SECONDS = 2 * 3600
STATIC_FILES = {"/", "/index.html", "/admin", "/admin.html", "/app.js", "/admin.js", "/styles.css"}
LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
  owner_ref TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  identity TEXT NOT NULL,
  pw_salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  balance INTEGER NOT NULL,
  spent INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (mode, identity)
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  amount INTEGER NOT NULL,
  kind TEXT NOT NULL,
  request_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (owner_ref, request_id)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  owner_ref TEXT NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (
  owner_ref TEXT PRIMARY KEY,
  failures INTEGER NOT NULL,
  locked_until REAL NOT NULL,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY,
  expires_at REAL NOT NULL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def setup():
    conn = db()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    relay.init(RELAY_DB)


def owner_ref_of(mode, identity):
    return "stu_" + hashlib.sha256(("%s:%s" % (mode, identity)).encode("utf-8")).hexdigest()[:16]


def hash_password(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT).hex()


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_identity(mode, identity):
    if mode not in ("email", "student"):
        return "身份类型只能是邮箱或学号"
    if not identity or len(identity) > 80:
        return "请输入校园邮箱或学号"
    if mode == "email" and ("@" not in identity or identity.startswith("@") or identity.endswith("@")):
        return "邮箱格式不正确"
    if mode == "student" and not identity.isdigit():
        return "学号只能是数字"
    return None


def check_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return "密码至少 %d 位" % MIN_PASSWORD
    if len(password) > 128:
        return "密码不能超过 128 位"
    return None


def ledger_rows(conn, owner_ref):
    rows = conn.execute("SELECT title, amount, kind, created_at FROM ledger WHERE owner_ref = ? ORDER BY id DESC",
                        (owner_ref,)).fetchall()
    return [{"title": r["title"], "amount": r["amount"], "type": r["kind"],
             "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))} for r in rows]


def new_session(conn, owner_ref):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions (token_hash, owner_ref, expires_at) VALUES (?, ?, ?)",
                 (token_digest(token), owner_ref, now + SESSION_SECONDS))
    return token


def owner_from_token(token):
    if not token:
        return None
    conn = db()
    try:
        row = conn.execute("SELECT owner_ref, expires_at FROM sessions WHERE token_hash = ?",
                           (token_digest(token),)).fetchone()
    finally:
        conn.close()
    if row is None or row["expires_at"] < time.time():
        return None
    return row["owner_ref"]


def key_block(result):
    return {"status": result["status"], "plaintext": result["plaintext"], "hint": result["hint"]}


def ensure_key(owner_ref, request_id, mode=None, identity=None):
    """给学生申请中转站密钥。已有有效密钥时不重复发放，也不再返回明文。

    先把实名写进中转站台账（姓名按名册带出，查不到就是空）。台账是中转站的管理依据，
    写入失败按中转站不可用处理，不影响学生登录。
    """
    try:
        if mode and identity:
            relay.upsert_account(RELAY_DB, owner_ref, mode, identity, roster.name_for(mode, identity))
        return key_block(relay.issue_key(RELAY_DB, owner_ref, "issue:" + request_id))
    except relay.AlreadyIssued:
        return {"status": "active", "plaintext": None, "hint": relay.active_hint(RELAY_DB, owner_ref)}
    except relay.RelayUnavailable as exc:
        return {"status": "unavailable", "plaintext": None, "hint": None, "note": str(exc)}


def profile(conn, owner_ref):
    row = conn.execute("SELECT mode, identity, balance, spent FROM students WHERE owner_ref = ?",
                       (owner_ref,)).fetchone()
    return {"mode": row["mode"], "identity": row["identity"], "balance": row["balance"], "spent": row["spent"],
            "ledger": ledger_rows(conn, owner_ref)}


def do_register(mode, identity, password, request_id):
    problem = check_identity(mode, identity) or check_password(password)
    if problem:
        return 400, {"error": problem}
    if not roster.is_enrolled(mode, identity):
        return 403, {"error": "该身份不在学生名册内，无法注册"}
    owner_ref = owner_ref_of(mode, identity)
    salt = secrets.token_bytes(16)
    pw_hash = hash_password(password, salt)
    with LOCK:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone():
                conn.execute("ROLLBACK")
                return 409, {"error": "该账号已注册，请直接登录"}
            now = time.time()
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                         (owner_ref, mode, identity, salt.hex(), pw_hash, INITIAL_GRANT, now))
            conn.execute("INSERT INTO ledger (owner_ref, title, amount, kind, request_id, created_at) "
                         "VALUES (?, '新用户注册赠送', ?, 'grant', ?, ?)",
                         (owner_ref, INITIAL_GRANT, "grant:" + owner_ref, now))
            token = new_session(conn, owner_ref)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    keys = ensure_key(owner_ref, request_id, mode, identity)
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    view.update({"token": token, "firstLogin": True, "relayKey": keys})
    return 201, view


def do_login(mode, identity, password, request_id):
    problem = check_identity(mode, identity)
    if problem or not isinstance(password, str) or not password:
        return 401, {"error": "账号或密码错误"}
    owner_ref = owner_ref_of(mode, identity)
    with LOCK:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            fail = conn.execute("SELECT failures, locked_until FROM login_failures WHERE owner_ref = ?",
                                (owner_ref,)).fetchone()
            if fail and fail["locked_until"] > now:
                conn.execute("ROLLBACK")
                wait = int(fail["locked_until"] - now) + 1
                return 429, {"error": "尝试次数过多，请 %d 秒后再试" % wait}
            row = conn.execute("SELECT pw_salt, pw_hash FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()
            salt = bytes.fromhex(row["pw_salt"]) if row else b"\x00" * 16
            candidate = hash_password(password, salt)
            ok = row is not None and hmac.compare_digest(candidate, row["pw_hash"])
            if not ok:
                failures = (fail["failures"] if fail else 0) + 1
                locked_until = now + LOCK_SECONDS if failures >= MAX_FAILURES else 0
                stored = 0 if locked_until else failures
                conn.execute("DELETE FROM login_failures WHERE locked_until < ? AND updated_at < ?",
                             (now, now - FAILURE_TTL))
                conn.execute("DELETE FROM login_failures WHERE owner_ref IN (SELECT owner_ref FROM login_failures "
                             "ORDER BY updated_at LIMIT MAX(0, (SELECT COUNT(*) FROM login_failures) - ?))",
                             (FAILURE_CAP,))
                conn.execute("INSERT INTO login_failures (owner_ref, failures, locked_until, updated_at) "
                             "VALUES (?, ?, ?, ?) ON CONFLICT(owner_ref) DO UPDATE SET failures = ?, "
                             "locked_until = ?, updated_at = ?",
                             (owner_ref, stored, locked_until, now, stored, locked_until, now))
                conn.execute("COMMIT")
                return 401, {"error": "账号或密码错误"}
            conn.execute("DELETE FROM login_failures WHERE owner_ref = ?", (owner_ref,))
            token = new_session(conn, owner_ref)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    keys = ensure_key(owner_ref, request_id, mode, identity)
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    view.update({"token": token, "firstLogin": False, "relayKey": keys})
    return 200, view


def do_me(owner_ref):
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    view["relayKey"] = {"status": "active" if relay.active_hint(RELAY_DB, owner_ref) else "none",
                        "plaintext": None, "hint": relay.active_hint(RELAY_DB, owner_ref)}
    return 200, view


def do_rotate(owner_ref, request_id):
    try:
        return 200, key_block(relay.rotate_key(RELAY_DB, owner_ref, "rotate:" + request_id))
    except relay.RelayUnavailable as exc:
        return 503, {"error": str(exc)}


def do_consume(owner_ref, amount, request_id):
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount > 10 ** 7:
        return 400, {"error": "消耗数量必须是正整数"}
    with LOCK:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT balance FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()
            dup = conn.execute("SELECT 1 FROM ledger WHERE owner_ref = ? AND request_id = ?",
                               (owner_ref, request_id)).fetchone()
            if dup is None:
                if row["balance"] < amount:
                    conn.execute("ROLLBACK")
                    return 409, {"error": "Token 余额不足"}
                conn.execute("UPDATE students SET balance = balance - ?, spent = spent + ? WHERE owner_ref = ?",
                             (amount, amount, owner_ref))
                conn.execute("INSERT INTO ledger (owner_ref, title, amount, kind, request_id, created_at) "
                             "VALUES (?, 'AI 创作体验', ?, 'use', ?, ?)",
                             (owner_ref, -amount, request_id, time.time()))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    conn = db()
    try:
        return 200, profile(conn, owner_ref)
    finally:
        conn.close()


def new_admin_session(conn):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO admin_sessions (token_hash, expires_at) VALUES (?, ?)",
                 (token_digest(token), now + ADMIN_SESSION_SECONDS))
    return token


def is_admin_token(token):
    if not token:
        return False
    conn = db()
    try:
        row = conn.execute("SELECT expires_at FROM admin_sessions WHERE token_hash = ?",
                           (token_digest(token),)).fetchone()
    finally:
        conn.close()
    return row is not None and row["expires_at"] >= time.time()


def do_admin_login(username, password):
    if not ADMIN_PASSWORD or not ADMIN_USERNAME:
        return 404, {"error": "管理功能未启用"}
    name_ok = isinstance(username, str) and hmac.compare_digest(username, ADMIN_USERNAME)
    pass_ok = isinstance(password, str) and hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (name_ok and pass_ok):
        return 401, {"error": "管理员账号或密码错误"}
    conn = db()
    try:
        token = new_admin_session(conn)
    finally:
        conn.close()
    return 200, {"token": token}


def do_admin_accounts(query):
    return 200, {"accounts": relay.list_accounts(RELAY_DB, query)}


def do_admin_revoke(owner_ref):
    if not owner_ref:
        return 400, {"error": "缺少 owner_ref"}
    relay.revoke_by_owner(RELAY_DB, owner_ref)
    return 200, {"ok": True}


def do_reset():
    with LOCK:
        conn = db()
        try:
            for table in ("ledger", "sessions", "login_failures", "admin_sessions", "students"):
                conn.execute("DELETE FROM " + table)
        finally:
            conn.close()
        relay.reset(RELAY_DB)
    return 200, {"ok": True}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length > 65536:
            return {}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _owner(self):
        header = self.headers.get("Authorization", "")
        return owner_from_token(header[7:]) if header.startswith("Bearer ") else None

    def _admin(self):
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and is_admin_token(header[7:])

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/api/me":
            owner = self._owner()
            return self._send(*do_me(owner)) if owner else self._send(401, {"error": "请先登录"})
        if path == "/api/admin/accounts":
            if not self._admin():
                return self._send(401, {"error": "请先登录管理后台"})
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [None])[0]
            return self._send(*do_admin_accounts(query))
        if path.startswith("/api/"):
            return self._send(404, {"error": "未知接口"})
        if path not in STATIC_FILES:
            return self._send(404, {"error": "未找到"})
        if path == "/admin":
            self.path = "/admin.html"
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        data = self._body()
        rid = str(data.get("requestId") or secrets.token_hex(8))[:64]
        mode, identity, password = data.get("mode", "email"), str(data.get("identity") or "").strip(), data.get("password")
        if path == "/api/register":
            return self._send(*do_register(mode, identity, password, rid))
        if path == "/api/login":
            return self._send(*do_login(mode, identity, password, rid))
        if path in ("/api/key/rotate", "/api/consume", "/api/logout"):
            header = self.headers.get("Authorization", "")
            owner = self._owner()
            if not owner:
                return self._send(401, {"error": "请先登录"})
            if path == "/api/key/rotate":
                return self._send(*do_rotate(owner, rid))
            if path == "/api/consume":
                return self._send(*do_consume(owner, data.get("amount"), rid))
            conn = db()
            try:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(header[7:]),))
            finally:
                conn.close()
            return self._send(200, {"ok": True})
        if path == "/api/admin/login":
            return self._send(*do_admin_login(data.get("username"), data.get("password")))
        if path == "/api/admin/revoke":
            if not self._admin():
                return self._send(401, {"error": "请先登录管理后台"})
            return self._send(*do_admin_revoke(str(data.get("owner_ref") or "").strip()))
        if path == "/api/reset" and os.environ.get("DEMO_ALLOW_RESET") == "1":
            return self._send(*do_reset())
        return self._send(404, {"error": "未知接口"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    setup()
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "127.0.0.1")
    srv = Server((host, port), Handler)
    print("prototype backend on http://%s:%d" % (host, port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
