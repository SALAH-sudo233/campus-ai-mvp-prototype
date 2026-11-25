"""限时个人访问令牌；明文仅在创建成功时返回。"""
import hashlib
import secrets
import time

import authz

MAX_ACTIVE_TOKENS = 10
REISSUE_MESSAGE = "个人访问令牌已失效，请重新签发"


def permissions_shrank(previous_role, current_role):
    previous = [row for row in authz.matrix_rows() if row["role"] == previous_role]
    if not previous:
        return True
    scopes = {"self": 0, "org": 1, "all": 2}
    return any(scopes.get(authz.scope_for(current_role, row["resource"], row["action"]), -1)
               < scopes[row["scope"]] for row in previous)


def metadata(row, now=None):
    now = time.time() if now is None else now
    status = "revoked" if row["revoked_at"] is not None else (
        "expired" if row["expires_at"] <= now else "active")
    return {"id": row["id"], "hint": row["token_hint"], "label": row["label"],
            "roleSnapshot": row["role_snapshot"], "orgSnapshot": row["org_snapshot"],
            "createdAt": row["created_at"], "expiresAt": row["expires_at"],
            "lastUsedAt": row["last_used_at"], "revokedAt": row["revoked_at"], "status": status}


def create(conn, principal, data):
    request_id = data.get("requestId")
    days = data.get("expiresInDays", 30)
    label = data.get("label", "我的 agent")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("requestId 必须是 1 到 128 字符的幂等键")
    if type(days) is not int or not 1 <= days <= 90:
        raise ValueError("有效期必须是 1 到 90 天的整数")
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        raise ValueError("名称必须是 1 到 80 字符")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT * FROM pat_tokens WHERE user_id = ? AND request_id = ?",
                                (principal["user_id"], request_id)).fetchone()
        if existing:
            conn.execute("COMMIT")
            return 200, {"item": metadata(existing), "replayed": True}
        now = time.time()
        count = conn.execute("SELECT COUNT(*) FROM pat_tokens WHERE user_id = ? "
                             "AND revoked_at IS NULL AND expires_at > ?",
                             (principal["user_id"], now)).fetchone()[0]
        if count >= MAX_ACTIVE_TOKENS:
            raise authz.AuthzError(409, "最多保留 10 个有效个人访问令牌，请先撤销旧令牌")
        plaintext = "cpat_" + secrets.token_urlsafe(32)
        token_id = "pat_" + secrets.token_hex(16)
        conn.execute("INSERT INTO pat_tokens (id, user_id, token_hash, token_hint, label, role_snapshot, "
                     "org_snapshot, created_at, created_by, expires_at, request_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (token_id, principal["user_id"], hashlib.sha256(plaintext.encode()).hexdigest(),
                      "cpat_…" + plaintext[-4:], label.strip(), principal["role"], principal.get("org_id"),
                      now, principal["user_id"], now + days * 86400, request_id))
        row = conn.execute("SELECT * FROM pat_tokens WHERE id = ?", (token_id,)).fetchone()
        conn.execute("COMMIT")
        return 201, {"token": plaintext, "item": metadata(row), "replayed": False}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def list_tokens(conn, principal):
    rows = conn.execute("SELECT * FROM pat_tokens WHERE user_id = ? ORDER BY created_at DESC",
                        (principal["user_id"],)).fetchall()
    return {"items": [metadata(row) for row in rows]}


def revoke(conn, principal, token_id):
    conn.execute("UPDATE pat_tokens SET revoked_at = COALESCE(revoked_at, ?), "
                 "revoked_by = COALESCE(revoked_by, ?) WHERE id = ? AND user_id = ?",
                 (time.time(), principal["user_id"], token_id, principal["user_id"]))
    row = conn.execute("SELECT * FROM pat_tokens WHERE id = ? AND user_id = ?",
                       (token_id, principal["user_id"])).fetchone()
    if not row:
        raise authz.AuthzError(404, "未找到个人访问令牌")
    return {"item": metadata(row)}
