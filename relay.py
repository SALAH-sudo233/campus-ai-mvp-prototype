"""New-API 中转站影子存储。

平台不再生成本地假钥。成员 token 的创建、取明文、轮换和吊销都映射到
New-API，本模块只保存可恢复的影子状态、SHA-256 摘要与掩码。额度分摊
与账面在平台侧，New-API token 一律 unlimited_quota，不存上游额度缓存。
明文只在发放的那一次返回，绝不落库。
"""
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from identity import normalize_identity


KEY_PREFIX = "campus:"
NEWAPI_CHANNEL = "ch-newapi"
BACKOFF_SECONDS = (30, 120, 600, 3600, 21600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
CREATING_LEASE_SECONDS = 60
DELIVERY_LEASE_SECONDS = 60
DEFAULT_TIMEOUT = 5.0
TOKEN_NAME_LIMIT = 50
ERROR_CATEGORIES = frozenset({
    "auth_failed", "rate_limited", "quota_exhausted", "bad_request", "timeout", "network", "upstream_5xx",
})

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
  user_id TEXT,
  key_hash TEXT NOT NULL UNIQUE,
  key_hint TEXT NOT NULL,
  channel_id TEXT NOT NULL REFERENCES channels(id),
  status TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  claim TEXT,
  provider_token_id INTEGER,
  token_name TEXT,
  quota INTEGER NOT NULL DEFAULT 0,
  remaining_quota INTEGER,
  used_quota INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL,
  last_error TEXT,
  delivered_at REAL,
  delivery_claim TEXT,
  delivery_claim_at REAL,
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

CHANNELS = [
    (NEWAPI_CHANNEL, "New-API", "active"),
    ("ch-a", "模拟上游通道 A", "active"),
    ("ch-b", "模拟上游通道 B", "active"),
    ("ch-c", "模拟上游通道 C", "active"),
]

KEY_COLUMNS = {
    "user_id": "TEXT",
    "state": "TEXT DEFAULT 'pending'",
    "claim": "TEXT",
    "provider_token_id": "INTEGER",
    "token_name": "TEXT",
    "quota": "INTEGER NOT NULL DEFAULT 0",
    "remaining_quota": "INTEGER",
    "used_quota": "INTEGER",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at": "REAL",
    "last_error": "TEXT",
    "delivered_at": "REAL",
    "delivery_claim": "TEXT",
    "delivery_claim_at": "REAL",
}


class RelayUnavailable(Exception):
    pass


class AlreadyIssued(Exception):
    pass


class NewApiError(Exception):
    def __init__(self, category, detail="", status=None, duplicate=False, provider_token_id=None):
        category = category if category in ERROR_CATEGORIES else "upstream_5xx"
        super().__init__(category)
        self.category = category
        self.detail = detail
        self.status = status
        self.duplicate = duplicate
        self.provider_token_id = provider_token_id


class NewApiConfig:
    def __init__(self, enabled, base_url="", token="", user_id="", timeout=DEFAULT_TIMEOUT):
        self.enabled = bool(enabled and base_url and token)
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.user_id = user_id or ""
        try:
            timeout_value = float(timeout or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout_value = DEFAULT_TIMEOUT
        self.timeout = min(max(timeout_value, 0.1), DEFAULT_TIMEOUT)

    @classmethod
    def from_env(cls):
        enabled = _truthy(os.environ.get("NEW_API_ENABLED"))
        return cls(enabled, os.environ.get("NEW_API_BASE_URL", ""), os.environ.get("NEW_API_TOKEN", ""),
                   os.environ.get("NEW_API_USER_ID", ""), os.environ.get("NEW_API_TIMEOUT", DEFAULT_TIMEOUT))

    @property
    def reason(self):
        if not _truthy(os.environ.get("NEW_API_ENABLED")):
            return "disabled"
        if not os.environ.get("NEW_API_BASE_URL") or not os.environ.get("NEW_API_TOKEN"):
            return "missing_config"
        return None


def connect(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _log_init(config):
    reason = config.reason
    if reason:
        print("relay: New-API unavailable (%s); key issuance will stay pending" % reason, flush=True)
    else:
        print("relay: New-API client configured; issuance is enabled", flush=True)


def init(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        key_columns = {row[1] for row in conn.execute("PRAGMA table_info(relay_keys)").fetchall()}
        for name, kind in KEY_COLUMNS.items():
            if name not in key_columns:
                conn.execute("ALTER TABLE relay_keys ADD COLUMN %s %s" % (name, kind))
        account_columns = {row[1] for row in conn.execute("PRAGMA table_info(relay_accounts)").fetchall()}
        for name in ("user_id", "org_id"):
            if name not in account_columns:
                conn.execute("ALTER TABLE relay_accounts ADD COLUMN %s TEXT" % name)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relay_keys_user ON relay_keys(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relay_keys_due ON relay_keys(state, next_attempt_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relay_keys_token_name ON relay_keys(token_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relay_accounts_org ON relay_accounts(org_id)")
        for channel_id, label, status in CHANNELS:
            conn.execute("INSERT OR IGNORE INTO channels (id, label, status) VALUES (?, ?, ?)",
                         (channel_id, label, status))
        # 老库新增 state 列时默认值会把所有旧行填成 pending，必须按 status 重新
        # 判定；否则已吊销的行会被当成待补发而复活并重新返回明文。
        conn.execute("UPDATE relay_keys SET state = status "
                     "WHERE state IS NULL OR state = '' "
                     "OR (state = 'pending' AND status IN ('active', 'creating', 'revoking', 'revoked', 'failed'))")
        for row in conn.execute("SELECT id, owner_ref, issue_request_id, token_name FROM relay_keys").fetchall():
            if not row["token_name"]:
                conn.execute("UPDATE relay_keys SET token_name = ? WHERE id = ?",
                             (token_name(row["owner_ref"], row["issue_request_id"]), row["id"]))
    finally:
        conn.close()
    _log_init(NewApiConfig.from_env())


def backfill_legacy(path, quota_lookup=None):
    """把没有 provider_token_id 的旧 active 行降级为 pending，等待补发。"""
    changed = 0
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, owner_ref, issue_request_id, quota FROM relay_keys "
                            "WHERE state = 'active' AND provider_token_id IS NULL").fetchall()
        now = time.time()
        for row in rows:
            quota = int(row["quota"] or 0)
            if quota <= 0 and quota_lookup:
                quota = int(quota_lookup(row["owner_ref"]) or 0)
            conn.execute("UPDATE relay_keys SET state = 'pending', status = 'pending', quota = ?, "
                         "attempts = 0, next_attempt_at = ?, last_error = NULL, "
                         "token_name = COALESCE(token_name, ?) WHERE id = ?",
                         (quota, now, token_name(row["owner_ref"], row["issue_request_id"]), row["id"]))
            changed += 1
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return changed


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
    if not plain:
        return ""
    if len(plain) <= 8:
        return plain[:2] + "…" + plain[-2:]
    return plain[:4] + "…" + plain[-4:]


def token_name(owner_ref, issue_request_id):
    """确定性 token 名；尽量保留可读格式，超过 New-API 50 字符上限时哈希压缩。"""
    raw = "%s%s:%s" % (KEY_PREFIX, owner_ref, issue_request_id)
    if len(raw) <= TOKEN_NAME_LIMIT and raw.isascii():
        return raw
    owner_hash = hashlib.sha256(str(owner_ref).encode("utf-8")).hexdigest()[:14]
    request_hash = hashlib.sha256(str(issue_request_id).encode("utf-8")).hexdigest()[:16]
    return "%s%s:%s" % (KEY_PREFIX, owner_hash, request_hash)


class NewApiClient:
    def __init__(self, config):
        self.config = config

    def _request(self, method, path, payload=None):
        if not self.config.enabled:
            raise NewApiError("network", "New-API is not configured")
        data = None
        headers = {"Accept": "application/json", "User-Agent": "campus-prototype-relay/1"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + self.config.token
        if self.config.user_id:
            headers["New-Api-User"] = str(self.config.user_id)
        request = urllib.request.Request(self.config.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read(1024 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read(65536)
            raise self._http_error(exc.code, body) from None
        except (socket.timeout, TimeoutError):
            raise NewApiError("timeout", "request timed out") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise NewApiError("timeout", "request timed out") from None
            raise NewApiError("network", "network error") from None
        except OSError:
            raise NewApiError("network", "network error") from None
        if status >= 500:
            raise NewApiError("upstream_5xx", "upstream error", status=status)
        if status in (401, 403):
            raise NewApiError("auth_failed", "authentication failed", status=status)
        if status == 429:
            raise NewApiError("rate_limited", "rate limited", status=status)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise NewApiError("upstream_5xx", "invalid response", status=status) from None
        if isinstance(body, dict) and body.get("success") is False:
            raise self._body_error(body, status)
        return body if isinstance(body, dict) else {"data": body}

    def _http_error(self, status, body):
        message = _message_from_body(body).lower()
        category = "bad_request"
        if status in (401, 403):
            category = "auth_failed"
        elif status == 429:
            category = "rate_limited"
        elif status >= 500:
            category = "upstream_5xx"
        elif _looks_quota(message):
            category = "quota_exhausted"
        duplicate = status in (400, 409) or "duplicate" in message or "exists" in message
        return NewApiError(category, "upstream rejected request", status=status, duplicate=duplicate)

    def _body_error(self, body, status):
        message = str(body.get("message") or "").lower()
        if _looks_quota(message):
            category = "quota_exhausted"
        elif "auth" in message or "unauthorized" in message or "登录" in message:
            category = "auth_failed"
        else:
            category = "bad_request"
        duplicate = "duplicate" in message or "exists" in message or "已存在" in message
        return NewApiError(category, "upstream rejected request", status=status, duplicate=duplicate)

    def list_tokens(self):
        items = []
        page = 0
        while page <= 100:
            query = urllib.parse.urlencode({"p": page, "size": 100})
            body = self._request("GET", "/api/token/?" + query)
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, dict):
                batch = data.get("items") or []
                total = int(data.get("total") or 0)
            elif isinstance(data, list):
                batch = data
                total = len(batch)
            else:
                batch = body.get("items") if isinstance(body, dict) else []
                total = len(batch) if isinstance(batch, list) else 0
            if not isinstance(batch, list):
                raise NewApiError("upstream_5xx", "invalid token list")
            items.extend(batch)
            if not batch or len(batch) < 100 or (total and len(items) >= total):
                break
            page += 1
        return [_public_token(item) for item in items if isinstance(item, dict)]

    def find_token(self, name):
        for token in self.list_tokens():
            if token.get("name") == name:
                return token
        return None

    def create_token(self, name, quota):
        payload = {
            "name": name,
            # 平台额度只在本地记账；New-API token 不设硬限额，避免依赖超管充值。
            "remain_quota": 0,
            "unlimited_quota": True,
            "expired_time": -1,
            "model_limits_enabled": False,
            "model_limits": "",
            "allow_ips": "",
            "group": "",
        }
        try:
            return self._request("POST", "/api/token", payload)
        except NewApiError as exc:
            if exc.duplicate:
                existing = self.find_token(name)
                if existing:
                    return {"success": True, "data": existing, "claimed": True}
            raise

    def get_key(self, token_id):
        path = "/api/token/%s/key" % urllib.parse.quote(str(token_id))
        try:
            body = self._request("GET", path)
        except NewApiError as exc:
            # 实例实测以 GET 取明文；旧版 rc.51 源码为 POST，保留回退兼容。
            if exc.status not in (400, 404, 405):
                raise
            body = self._request("POST", path)
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            key = data.get("key") or data.get("token") or data.get("value")
        else:
            key = data if isinstance(data, str) else None
        if not key:
            raise NewApiError("upstream_5xx", "token key missing", provider_token_id=token_id)
        return str(key)

    def delete_token(self, token_id):
        try:
            self._request("DELETE", "/api/token/%s" % urllib.parse.quote(str(token_id)))
            return True
        except NewApiError as exc:
            if exc.status == 404:
                return True
            raise

    def create_and_get(self, name, quota):
        existing = self.find_token(name)
        provider_id = existing.get("id") if existing else None
        if not existing:
            created = self.create_token(name, quota)
            data = created.get("data") if isinstance(created, dict) else None
            if isinstance(data, dict) and data.get("id"):
                provider_id = data.get("id")
            if not provider_id:
                for _ in range(3):
                    existing = self.find_token(name)
                    if existing:
                        provider_id = existing.get("id")
                        break
                    time.sleep(0.05)
        if not provider_id:
            raise NewApiError("upstream_5xx", "created token not visible")
        if not existing:
            existing = self.find_token(name) or {}
        try:
            key = self.get_key(provider_id)
        except NewApiError as exc:
            exc.provider_token_id = provider_id
            raise
        return {"id": int(provider_id), "key": key, "name": name,
                "remain_quota": _int_or_none(existing.get("remain_quota")),
                "used_quota": _int_or_none(existing.get("used_quota"))}


def _message_from_body(body):
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return ""
    return str(parsed.get("message") or "") if isinstance(parsed, dict) else ""


def _looks_quota(message):
    if any(word in message for word in ("quota", "额度", "insufficient", "无可用", "超额", "最多", "数量上限")):
        return True
    return (("token" in message or "tokens" in message) and "limit" in message and "rate" not in message) \
        or "maximum number of tokens" in message


def _public_token(item):
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "status": item.get("status"),
        "remain_quota": item.get("remain_quota"),
        "used_quota": item.get("used_quota"),
        "unlimited_quota": item.get("unlimited_quota"),
        "expired_time": item.get("expired_time"),
        "created_time": item.get("created_time"),
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def client_from_env():
    return NewApiClient(NewApiConfig.from_env())


def _client():
    return client_from_env()


def _row(conn, key_id):
    return conn.execute("SELECT * FROM relay_keys WHERE id = ?", (key_id,)).fetchone()


def _active_rows(conn, owner_ref):
    return conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? AND state = 'active'", (owner_ref,)).fetchall()


def _replay_row(conn, owner_ref, issue_request_id):
    return conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? AND issue_request_id = ?",
                        (owner_ref, issue_request_id)).fetchone()


def _mark(conn, key_id, state, **fields):
    fields["state"] = state
    fields["status"] = state
    columns = ["state = ?", "status = ?"]
    params = [state, state]
    for name, value in fields.items():
        if name in ("state", "status"):
            continue
        columns.append("%s = ?" % name)
        params.append(value)
    params.append(key_id)
    conn.execute("UPDATE relay_keys SET %s WHERE id = ?" % ", ".join(columns), tuple(params))


def _insert_key(conn, owner_ref, issue_request_id, quota, claim):
    key_id = "rk_" + secrets.token_hex(8)
    name = token_name(owner_ref, issue_request_id)
    conn.execute(
        "INSERT INTO relay_keys (id, owner_ref, key_hash, key_hint, channel_id, status, state, claim, "
        "issue_request_id, token_name, quota, attempts, next_attempt_at, created_at) "
        "VALUES (?, ?, ?, '', ?, 'creating', 'creating', ?, ?, ?, ?, 0, ?, ?)",
        (key_id, owner_ref, digest("pending:" + key_id), NEWAPI_CHANNEL, claim, issue_request_id, name,
         int(quota or 0), time.time() + CREATING_LEASE_SECONDS, time.time()))
    return _row(conn, key_id)


def _result(row, plaintext=None, replayed=False):
    state = row["state"] if row is not None else "none"
    if state == "creating":
        state = "pending"
    hint = row["key_hint"] if row is not None and state == "active" else None
    return {
        "key_id": row["id"] if row is not None else None,
        "hint": hint,
        "plaintext": plaintext,
        "replayed": bool(replayed),
        "status": state,
        "next_retry_at": row["next_attempt_at"] if row is not None else None,
        "last_error": row["last_error"] if row is not None else None,
    }


def _latest_row(conn, owner_ref):
    return conn.execute(
        "SELECT * FROM relay_keys WHERE owner_ref = ? AND state != 'revoked' "
        "ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'revoking' THEN 1 WHEN 'creating' THEN 2 "
        "WHEN 'pending' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END, created_at DESC LIMIT 1",
        (owner_ref,)).fetchone()


def _existing_unfinished(conn, owner_ref):
    return conn.execute(
        "SELECT * FROM relay_keys WHERE owner_ref = ? AND state IN ('creating', 'pending', 'failed') "
        "ORDER BY created_at DESC LIMIT 1", (owner_ref,)).fetchone()


def prepare_issue(path, owner_ref, issue_request_id, quota=0, rotate=False):
    """锁内预占影子状态；不在调用方全局 LOCK 中做任何网络请求。"""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        lease_until = now + CREATING_LEASE_SECONDS
        claim = secrets.token_hex(8)

        def stale_creating(row):
            return row["next_attempt_at"] is None or row["next_attempt_at"] <= now

        def resume(row):
            if quota and not row["quota"]:
                conn.execute("UPDATE relay_keys SET quota = ? WHERE id = ?", (int(quota), row["id"]))
            _mark(conn, row["id"], "creating", claim=claim, next_attempt_at=lease_until, last_error=None)
            conn.execute("COMMIT")
            return {"action": "network", "key_id": row["id"],
                    "provider_token_id": row["provider_token_id"], "deliver_plaintext": True,
                    "claim": claim}

        replay = _replay_row(conn, owner_ref, issue_request_id)
        if replay is not None:
            state = replay["state"]
            if state == "active":
                if replay["provider_token_id"] and replay["delivered_at"] is None:
                    conn.execute("COMMIT")
                    return {"action": "deliver", "key_id": replay["id"],
                            "provider_token_id": int(replay["provider_token_id"]), "deliver_plaintext": True}
                conn.execute("COMMIT")
                return {"action": "final", "key_id": replay["id"], "deliver_plaintext": False, "replayed": True}
            if state == "creating":
                if stale_creating(replay):
                    return resume(replay)
                conn.execute("COMMIT")
                return {"action": "final", "key_id": replay["id"], "deliver_plaintext": False, "replayed": True}
            if state in ("pending", "failed"):
                due = state == "pending" and (replay["next_attempt_at"] is None or replay["next_attempt_at"] <= now)
                if due or (rotate and state == "failed"):
                    return resume(replay)
                conn.execute("COMMIT")
                return {"action": "final", "key_id": replay["id"], "deliver_plaintext": False, "replayed": True}
            conn.execute("COMMIT")
            return {"action": "final", "key_id": replay["id"], "deliver_plaintext": False, "replayed": True}

        active = _active_rows(conn, owner_ref)
        if active and not rotate:
            conn.execute("ROLLBACK")
            raise AlreadyIssued("该成员已有有效密钥，请使用重新生成")

        revoke_ids = []
        existing = _existing_unfinished(conn, owner_ref)
        if existing is not None:
            if existing["state"] == "failed" and rotate:
                # 失败行可能已在上游建出 token（响应丢失或取明文失败）。轮换时不能
                # 只在本地标 revoked，必须进 revoking 队列由 finish_revocations 同步撤销。
                _mark(conn, existing["id"], "revoking", claim=None, revoked_at=now,
                      next_attempt_at=now, attempts=0, last_error=None)
                revoke_ids.append(existing["id"])
                existing = None
            elif existing["state"] == "creating":
                if stale_creating(existing):
                    return resume(existing)
                conn.execute("COMMIT")
                return {"action": "final", "key_id": existing["id"], "deliver_plaintext": False}
            elif existing["state"] == "pending" and (existing["next_attempt_at"] is None or
                                                     existing["next_attempt_at"] <= now or rotate):
                if rotate and existing["issue_request_id"] != issue_request_id:
                    # 沿用原 token 名：它可能已在响应丢失时创建，改名会留下孤儿 token。
                    conn.execute("UPDATE relay_keys SET issue_request_id = ? WHERE id = ?",
                                 (issue_request_id, existing["id"]))
                return resume(existing)
            else:
                conn.execute("COMMIT")
                return {"action": "final", "key_id": existing["id"], "deliver_plaintext": False}

        row = _insert_key(conn, owner_ref, issue_request_id, quota, claim)
        conn.execute("COMMIT")
        return {"action": "network", "key_id": row["id"], "provider_token_id": None, "deliver_plaintext": True,
                "claim": claim, "revoke_ids": revoke_ids}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _deliver_active_key(path, key_id, deliver_plaintext):
    conn = connect(path)
    try:
        row = _row(conn, key_id)
    finally:
        conn.close()
    if row is None or row["state"] != "active" or not row["provider_token_id"]:
        return _result(row)
    if not deliver_plaintext:
        return _result(row, replayed=True)

    # 先原子领取交付权，再调用上游取明文。并发的第二个请求看到交付租约后
    # 直接返回不含明文的回执，保证同一份明文最多返回一次。
    claim = secrets.token_hex(8)
    claimed_at = time.time()
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE relay_keys SET delivery_claim = ?, delivery_claim_at = ? "
            "WHERE id = ? AND state = 'active' AND provider_token_id IS NOT NULL AND delivered_at IS NULL "
            "AND (delivery_claim IS NULL OR delivery_claim_at IS NULL OR delivery_claim_at <= ?)",
            (claim, claimed_at, key_id, claimed_at - DELIVERY_LEASE_SECONDS))
        claimed = cursor.rowcount == 1
        row = _row(conn, key_id)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if not claimed or row is None:
        return _result(row)

    provider_id = int(row["provider_token_id"])
    try:
        plaintext = _client().get_key(provider_id)
    except NewApiError as exc:
        conn = connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _row(conn, key_id)
            if current is not None and current["delivery_claim"] == claim:
                if exc.status == 404 and current["state"] == "active":
                    # 上游 token 已不存在：清掉失效 id，按确定性名称重新认领或创建。
                    conn.execute("UPDATE relay_keys SET state = 'pending', status = 'pending', "
                                 "provider_token_id = NULL, attempts = 0, next_attempt_at = ?, last_error = ?, "
                                 "delivery_claim = NULL, delivery_claim_at = NULL WHERE id = ?",
                                 (time.time(), exc.category, key_id))
                elif current["state"] == "active":
                    conn.execute("UPDATE relay_keys SET last_error = ?, delivery_claim = NULL, "
                                 "delivery_claim_at = NULL WHERE id = ?",
                                 (exc.category, key_id))
                else:
                    conn.execute("UPDATE relay_keys SET delivery_claim = NULL, delivery_claim_at = NULL "
                                 "WHERE id = ?", (key_id,))
            row = _row(conn, key_id)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return _result(row)

    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _row(conn, key_id)
        if (current is None or current["state"] != "active" or
                current["provider_token_id"] != provider_id or current["delivery_claim"] != claim):
            if current is not None and current["delivery_claim"] == claim:
                conn.execute("UPDATE relay_keys SET delivery_claim = NULL, delivery_claim_at = NULL "
                             "WHERE id = ?", (key_id,))
            row = _row(conn, key_id)
            conn.execute("COMMIT")
            return _result(row)
        cursor = conn.execute(
            "UPDATE relay_keys SET delivered_at = ?, last_error = NULL, delivery_claim = NULL, "
            "delivery_claim_at = NULL WHERE id = ? AND state = 'active' AND delivery_claim = ?",
            (time.time(), key_id, claim))
        row = _row(conn, key_id)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if cursor.rowcount != 1:
        return _result(row)
    return _result(row, plaintext, replayed=True)


def _result_for_path(path, key_id):
    conn = connect(path)
    try:
        return _result(_row(conn, key_id))
    finally:
        conn.close()


def finish_issue(job, deliver_plaintext=True):
    if not job:
        return _result(None)
    if job.get("action") == "final":
        path = job.get("_path")
        return _result_for_path(path, job["key_id"]) if path else {"key_id": job.get("key_id"), "hint": None,
                                                                   "plaintext": None, "replayed": True,
                                                                   "status": "pending", "next_retry_at": None,
                                                                   "last_error": None}
    if job.get("action") == "deliver":
        return _deliver_active_key(job["_path"], job["key_id"],
                                   deliver_plaintext and job.get("deliver_plaintext", True))

    path = job.get("_path")
    if path is None:
        raise RelayUnavailable("内部调用缺少 relay 路径")
    conn = connect(path)
    try:
        row = _row(conn, job["key_id"])
        if row is None:
            return _result(None)
        name = row["token_name"] or token_name(row["owner_ref"], row["issue_request_id"])
        quota = int(row["quota"] or 0)
    finally:
        conn.close()
    provider_id = job.get("provider_token_id")
    try:
        if provider_id:
            plaintext = _client().get_key(provider_id)
            token = {"id": int(provider_id), "key": plaintext, "name": name,
                     "remain_quota": None, "used_quota": None}
        else:
            token = _client().create_and_get(name, quota)
        provider_id = int(token["id"])
        plaintext = token["key"]
    except NewApiError as exc:
        provider_id = exc.provider_token_id or provider_id
        claim = job.get("claim")
        conn = connect(path)
        result_key_id = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _row(conn, job["key_id"])
            if current is not None:
                result_key_id = current["id"]
                owned = current["state"] == "creating" and (claim is None or current["claim"] == claim)
                if owned:
                    attempts = int(current["attempts"] or 0) + 1
                    failed = exc.category == "quota_exhausted" or attempts >= MAX_ATTEMPTS
                    state = "failed" if failed else "pending"
                    next_attempt = None if failed else time.time() + BACKOFF_SECONDS[
                        min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
                    if exc.status == 404:
                        # 旧 provider id 已失效，下一次按 token 名重新认领。
                        provider_id = None
                        conn.execute("UPDATE relay_keys SET provider_token_id = NULL WHERE id = ?", (current["id"],))
                    conn.execute("UPDATE relay_keys SET state = ?, status = ?, provider_token_id = COALESCE(?, provider_token_id), "
                                 "attempts = ?, next_attempt_at = ?, last_error = ?, claim = NULL WHERE id = ?",
                                 (state, state, provider_id, attempts, next_attempt, exc.category, current["id"]))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        if job.get("revoke_ids"):
            finish_revocations(job["revoke_ids"], path)
        return _result_for_path(path, result_key_id) if result_key_id else _result(None)

    old_ids = []
    cancelled = False
    adopted = False
    claim = job.get("claim")
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _row(conn, job["key_id"])
        if current is None:
            conn.execute("COMMIT")
            return _result(None)
        owned = current["state"] == "creating" and (claim is None or current["claim"] == claim)
        if not owned:
            if current["state"] == "active" and current["provider_token_id"] == provider_id:
                # 另一个领取者已经把同一个上游 token 置为 active，不能删除。
                adopted = True
            elif current["state"] == "creating":
                # 新的领取者会按确定性名称认领同一个上游 token，同样不能删除。
                adopted = True
            else:
                cancelled = True
        else:
            for old in _active_rows(conn, current["owner_ref"]):
                if old["id"] == current["id"]:
                    continue
                _mark(conn, old["id"], "revoking", claim=None, revoked_at=time.time(),
                      next_attempt_at=time.time(), attempts=0)
                old_ids.append(old["id"])
            delivered_at = time.time() if deliver_plaintext else None
            # 上游 token 不设硬限额，额度只看平台台账；这里不再缓存 remain/used。
            conn.execute(
                "UPDATE relay_keys SET state = 'active', status = 'active', provider_token_id = ?, key_hash = ?, "
                "key_hint = ?, remaining_quota = NULL, used_quota = NULL, claim = NULL, "
                "attempts = 0, next_attempt_at = NULL, last_error = NULL, delivered_at = ?, revoked_at = NULL "
                "WHERE id = ?",
                (provider_id, digest(plaintext), hint_of(plaintext), delivered_at, current["id"]))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    if job.get("revoke_ids"):
        finish_revocations(job["revoke_ids"], path)
    if adopted:
        return _result_for_path(path, job["key_id"])

    if cancelled:
        try:
            _client().delete_token(provider_id)
        except NewApiError as exc:
            conn = connect(path)
            try:
                current = _row(conn, job["key_id"])
                if current is not None and not (current["state"] == "active" and
                                                current["provider_token_id"] != provider_id):
                    conn.execute("UPDATE relay_keys SET state = 'revoking', status = 'revoking', provider_token_id = ?, "
                                 "next_attempt_at = ?, attempts = attempts + 1, last_error = ?, claim = NULL "
                                 "WHERE id = ?", (provider_id, time.time() + BACKOFF_SECONDS[0], exc.category,
                                                  job["key_id"]))
            finally:
                conn.close()
        else:
            conn = connect(path)
            try:
                current = _row(conn, job["key_id"])
            finally:
                conn.close()
            if current is None or not (current["state"] == "active" and
                                       current["provider_token_id"] != provider_id):
                _set_revoked(path, job["key_id"])
        return _result_for_path(path, job["key_id"])

    if old_ids:
        finish_revocations(old_ids, path)
    result = _result_for_path(path, job["key_id"])
    if deliver_plaintext:
        result["plaintext"] = plaintext
    return result


def _set_revoked(path, key_id):
    conn = connect(path)
    try:
        conn.execute("UPDATE relay_keys SET state = 'revoked', status = 'revoked', revoked_at = ?, "
                     "next_attempt_at = NULL, last_error = NULL, claim = NULL, delivery_claim = NULL, "
                     "delivery_claim_at = NULL WHERE id = ?", (time.time(), key_id))
    finally:
        conn.close()


def issue_key(path, owner_ref, issue_request_id, quota=0):
    job = prepare_issue(path, owner_ref, issue_request_id, quota=quota, rotate=False)
    job["_path"] = path
    return finish_issue(job, deliver_plaintext=True)


def rotate_key(path, owner_ref, issue_request_id, quota=0):
    job = prepare_issue(path, owner_ref, issue_request_id, quota=quota, rotate=True)
    job["_path"] = path
    return finish_issue(job, deliver_plaintext=True)


def prepare_revoke(path, owner_ref):
    conn = connect(path)
    jobs = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? AND state IN "
                            "('active', 'creating', 'pending', 'failed', 'revoking')", (owner_ref,)).fetchall()
        now = time.time()
        for row in rows:
            if row["provider_token_id"]:
                _mark(conn, row["id"], "revoking", claim=None, revoked_at=now,
                      next_attempt_at=now, attempts=0)
                jobs.append(row["id"])
            else:
                _mark(conn, row["id"], "revoked", claim=None, revoked_at=now,
                      next_attempt_at=None, last_error=None)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return jobs


def finish_revocations(key_ids, path):
    for key_id in key_ids:
        conn = connect(path)
        try:
            row = _row(conn, key_id)
        finally:
            conn.close()
        if row is None or row["state"] != "revoking":
            continue
        try:
            provider_id = row["provider_token_id"]
            if not provider_id:
                # 失败/响应丢失的行可能没记下上游 id，但上游已按确定性名称建出
                # token。删除前先按名称认领，避免只标本地 revoked 留下孤儿。
                name = row["token_name"] or token_name(row["owner_ref"], row["issue_request_id"])
                existing = _client().find_token(name)
                if existing is None:
                    _set_revoked(path, key_id)
                    continue
                provider_id = existing.get("id")
                if not provider_id:
                    raise NewApiError("upstream_5xx", "token id missing")
                conn = connect(path)
                try:
                    conn.execute("UPDATE relay_keys SET provider_token_id = ? WHERE id = ? AND state = 'revoking'",
                                 (int(provider_id), key_id))
                finally:
                    conn.close()
            _client().delete_token(int(provider_id))
        except NewApiError as exc:
            attempts = int(row["attempts"] or 0) + 1
            delay = BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)]
            conn = connect(path)
            try:
                conn.execute("UPDATE relay_keys SET attempts = ?, next_attempt_at = ?, last_error = ? "
                             "WHERE id = ? AND state = 'revoking'",
                             (attempts, time.time() + delay, exc.category, key_id))
            finally:
                conn.close()
            continue
        _set_revoked(path, key_id)


def revoke_by_owner(path, owner_ref):
    key_ids = prepare_revoke(path, owner_ref)
    finish_revocations(key_ids, path)
    return status_for_owner(path, owner_ref)


def _quota_lookup_for(path, quota_lookup, row):
    quota = int(row["quota"] or 0)
    if quota <= 0 and quota_lookup:
        quota = int(quota_lookup(row["owner_ref"]) or 0)
        if quota:
            conn = connect(path)
            try:
                conn.execute("UPDATE relay_keys SET quota = ? WHERE id = ?", (quota, row["id"]))
            finally:
                conn.close()
    return quota


def retry_due(path, owner_ref=None, org_id=None, now=None, limit_pending=1, limit_revoking=1,
              quota_lookup=None, allow_delivery_for=None, owner_refs=None):
    """读路径惰性补发：每次最多一条到期 pending、一条 revoking，不做后台轮询。

    owner_refs 限定可补发的成员集合；调用方用它把 pending 限制在 active 成员内。
    """
    now = now or time.time()
    refs = list(owner_refs) if owner_refs is not None else None
    plaintext = None
    pending = []
    if refs is None or refs:
        sql = ("SELECT k.* FROM relay_keys k LEFT JOIN relay_accounts a ON a.owner_ref = k.owner_ref "
               "WHERE ((k.state = 'pending' AND (k.next_attempt_at IS NULL OR k.next_attempt_at <= ?)) "
               "OR (k.state = 'creating' AND (k.next_attempt_at IS NULL OR k.next_attempt_at <= ?))) ")
        params = [now, now]
        if owner_ref:
            sql += "AND k.owner_ref = ? "
            params.append(owner_ref)
        if org_id:
            sql += "AND a.org_id = ? "
            params.append(org_id)
        if refs is not None:
            sql += "AND k.owner_ref IN (%s) " % ",".join("?" for _ in refs)
            params.extend(refs)
        sql += "ORDER BY k.next_attempt_at, k.created_at LIMIT ?"
        params.append(max(0, int(limit_pending)))
        conn = connect(path)
        try:
            pending = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    for row in pending:
        _quota_lookup_for(path, quota_lookup, row)
        claim = secrets.token_hex(8)
        claim_now = time.time()
        lease_until = claim_now + CREATING_LEASE_SECONDS
        conn = connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _row(conn, row["id"])
            if current is None or current["state"] not in ("pending", "creating"):
                conn.execute("COMMIT")
                continue
            if current["state"] == "pending":
                cursor = conn.execute(
                    "UPDATE relay_keys SET state = 'creating', status = 'creating', claim = ?, "
                    "next_attempt_at = ?, last_error = NULL WHERE id = ? AND state = 'pending' "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                    (claim, lease_until, row["id"], claim_now))
            else:
                if current["next_attempt_at"] is not None and current["next_attempt_at"] > claim_now:
                    # 另一个补发者仍持有未过期的领取租约，不能重复发钥。
                    conn.execute("COMMIT")
                    continue
                cursor = conn.execute(
                    "UPDATE relay_keys SET claim = ?, next_attempt_at = ?, last_error = NULL "
                    "WHERE id = ? AND state = 'creating' "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                    (claim, lease_until, row["id"], claim_now))
            if cursor.rowcount != 1:
                conn.execute("COMMIT")
                continue
            current = _row(conn, row["id"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        deliver = bool(allow_delivery_for and row["owner_ref"] == allow_delivery_for)
        job = {"action": "network", "key_id": row["id"], "provider_token_id": current["provider_token_id"],
               "deliver_plaintext": deliver, "_path": path, "claim": claim}
        result = finish_issue(job, deliver_plaintext=deliver)
        if deliver and result.get("plaintext"):
            plaintext = result["plaintext"]

    revoked_sql = ("SELECT k.id FROM relay_keys k LEFT JOIN relay_accounts a ON a.owner_ref = k.owner_ref "
                   "WHERE k.state = 'revoking' AND (k.next_attempt_at IS NULL OR k.next_attempt_at <= ?) ")
    revoked_params = [now]
    if owner_ref:
        revoked_sql += "AND k.owner_ref = ? "
        revoked_params.append(owner_ref)
    if org_id:
        revoked_sql += "AND a.org_id = ? "
        revoked_params.append(org_id)
    revoked_sql += "ORDER BY k.next_attempt_at, k.created_at LIMIT ?"
    revoked_params.append(max(1, int(limit_revoking)))
    conn = connect(path)
    try:
        revoke_ids = [row["id"] for row in conn.execute(revoked_sql, tuple(revoked_params)).fetchall()]
    finally:
        conn.close()
    if revoke_ids:
        finish_revocations(revoke_ids, path)

    if allow_delivery_for and not plaintext:
        conn = connect(path)
        try:
            active = conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? AND state = 'active' "
                                  "AND provider_token_id IS NOT NULL AND delivered_at IS NULL LIMIT 1",
                                  (allow_delivery_for,)).fetchone()
        finally:
            conn.close()
        if active:
            plaintext = _deliver_active_key(path, active["id"], True).get("plaintext")
    return {"plaintext": plaintext, "pending_attempted": len(pending), "revoking_attempted": len(revoke_ids)}


def manual_reissue(path, owner_ref, quota_lookup=None):
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT 1 FROM relay_keys WHERE owner_ref = ? AND state = 'active' LIMIT 1",
                              (owner_ref,)).fetchone()
        if active is not None:
            conn.execute("COMMIT")
            return "active"
        rows = conn.execute("SELECT * FROM relay_keys WHERE owner_ref = ? AND state IN ('pending', 'failed')",
                            (owner_ref,)).fetchall()
        for row in rows:
            quota = int(row["quota"] or 0)
            if quota <= 0 and quota_lookup:
                quota = int(quota_lookup(owner_ref) or 0)
            conn.execute("UPDATE relay_keys SET state = 'pending', status = 'pending', quota = ?, attempts = 0, "
                         "next_attempt_at = ?, last_error = NULL WHERE id = ?", (quota, time.time(), row["id"]))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    retry_due(path, owner_ref=owner_ref, limit_pending=1, quota_lookup=quota_lookup,
              allow_delivery_for=None)
    return status_for_owner(path, owner_ref)


def sync_provider_quotas(path):
    """只回填 provider_token_id；额度不在上游缓存。"""
    client = _client()
    if not client.config.enabled:
        return False
    try:
        tokens = client.list_tokens()
    except NewApiError:
        return False
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for token in tokens:
            name = token.get("name")
            if not name or not str(name).startswith(KEY_PREFIX):
                continue
            conn.execute("UPDATE relay_keys SET provider_token_id = COALESCE(provider_token_id, ?) "
                         "WHERE token_name = ?", (token.get("id"), name))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return True


def quota_overview(path, org_id):
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM relay_keys k "
            "JOIN relay_accounts a ON a.owner_ref = k.owner_ref "
            "WHERE k.state = 'active' AND a.org_id = ?", (org_id,)).fetchone()
    finally:
        conn.close()
    return {"available": True, "activeTokens": int(row["n"] or 0)}


def key_view(path, owner_ref):
    conn = connect(path)
    try:
        row = _latest_row(conn, owner_ref)
    finally:
        conn.close()
    if row is None:
        return {"state": "none", "hint": None, "quota": 0, "remainingQuota": None, "usedQuota": None,
                "lastError": None, "nextRetryAt": None}
    return {"state": row["state"], "hint": row["key_hint"] if row["state"] == "active" else None,
            "quota": int(row["quota"] or 0), "remainingQuota": None,
            "usedQuota": None, "lastError": row["last_error"],
            "nextRetryAt": row["next_attempt_at"]}


def active_hint(path, owner_ref):
    conn = connect(path)
    try:
        row = conn.execute("SELECT id, key_hint, provider_token_id FROM relay_keys "
                           "WHERE owner_ref = ? AND state = 'active'", (owner_ref,)).fetchone()
        if row is not None and not row["provider_token_id"]:
            conn.execute("UPDATE relay_keys SET state = 'pending', status = 'pending', attempts = 0, "
                         "next_attempt_at = ?, last_error = NULL WHERE id = ?",
                         (time.time(), row["id"]))
            row = None
    finally:
        conn.close()
    return row["key_hint"] if row else None


def status_for_owner(path, owner_ref):
    return key_view(path, owner_ref)["state"]


def key_info(path, plain):
    conn = connect(path)
    try:
        row = conn.execute("SELECT id, state FROM relay_keys WHERE key_hash = ?", (digest(plain),)).fetchone()
    finally:
        conn.close()
    if row is None or row["state"] != "active":
        return None
    return {"key_id": row["id"], "channel": "New-API"}


def list_accounts(path, query=None, org_id=None):
    """管理页用：列出台账及当前或待补发的 token 状态。"""
    conn = connect(path)
    try:
        sql = ("SELECT a.owner_ref, a.user_id, a.org_id, a.mode, a.identity, a.name, a.updated_at, "
               "k.id AS key_id, k.key_hint, k.state AS key_state, k.quota, k.remaining_quota, k.used_quota, "
               "k.attempts, k.last_error, k.next_attempt_at, k.created_at AS key_created_at "
               "FROM relay_accounts a LEFT JOIN relay_keys k ON k.id = ("
               "SELECT k2.id FROM relay_keys k2 WHERE k2.owner_ref = a.owner_ref AND k2.state != 'revoked' "
               "ORDER BY CASE k2.state WHEN 'active' THEN 0 WHEN 'revoking' THEN 1 WHEN 'creating' THEN 2 "
               "WHEN 'pending' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END, k2.created_at DESC LIMIT 1) ")
        params = []
        where = []
        if query:
            like = "%" + query + "%"
            where.append("(a.identity LIKE ? OR a.name LIKE ?)")
            params.extend([like, like])
        if org_id:
            where.append("a.org_id = ?")
            params.append(org_id)
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY a.updated_at DESC"
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return [{"owner_ref": r["owner_ref"], "user_id": r["user_id"], "org_id": r["org_id"],
             "mode": r["mode"], "identity": r["identity"], "name": r["name"],
             "key_id": r["key_id"], "key_hint": r["key_hint"], "key_status": r["key_state"] or "none",
             "quota": int(r["quota"] or 0), "remainingQuota": r["remaining_quota"],
             "usedQuota": r["used_quota"], "attempts": int(r["attempts"] or 0), "lastError": r["last_error"],
             "nextRetryAt": r["next_attempt_at"]} for r in rows]


def upsert_account(path, owner_ref, mode, identity, name, user_id=None, org_id=None):
    identity = normalize_identity(mode, identity)
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO relay_accounts (owner_ref, user_id, org_id, mode, identity, name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_ref) DO UPDATE SET user_id = COALESCE(excluded.user_id, relay_accounts.user_id), "
            "org_id = COALESCE(excluded.org_id, relay_accounts.org_id), mode = excluded.mode, "
            "identity = excluded.identity, name = excluded.name, updated_at = excluded.updated_at",
            (owner_ref, user_id, org_id, mode, identity, name, time.time()))
    finally:
        conn.close()


def relay_cutover(client, relay_db=None, apply=False, backup_dir=None, confirm=None):
    """只清理 New-API 中 campus: 前缀 token。默认 dry-run；apply 需显式确认并先备份。"""
    tokens = client.list_tokens()
    campus = [{"id": token.get("id"), "name": token.get("name")} for token in tokens
              if str(token.get("name") or "").startswith(KEY_PREFIX)]
    result = {"apply": bool(apply), "matched": len(campus), "tokens": campus,
              "deleted": 0, "failed": [], "backup": None, "manifest": None}
    if not apply:
        return result
    if confirm != "DELETE":
        raise ValueError("relay cutover 需要人工确认")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = backup_dir or os.path.join(os.path.dirname(os.path.abspath(relay_db or "relay.sqlite")),
                                            "relay-cutover-backup-" + stamp)
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    if relay_db and os.path.exists(relay_db):
        backup_path = os.path.join(backup_dir, "relay-%s.sqlite" % stamp)
        source = sqlite3.connect(relay_db)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        try:
            os.chmod(backup_path, 0o600)
        except OSError:
            pass
        result["backup"] = backup_path
    manifest_path = os.path.join(backup_dir, "campus-tokens-%s.json" % stamp)
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(campus, handle, ensure_ascii=False, indent=2)
    result["manifest"] = manifest_path

    for token in campus:
        try:
            client.delete_token(token["id"])
            result["deleted"] += 1
        except NewApiError as exc:
            result["failed"].append({"id": token["id"], "error": exc.category})

    remaining = {token.get("id") for token in client.list_tokens()
                 if str(token.get("name") or "").startswith(KEY_PREFIX)}
    for token in campus:
        if token["id"] in remaining and not any(item["id"] == token["id"] for item in result["failed"]):
            result["failed"].append({"id": token["id"], "error": "upstream_5xx"})
    return result
