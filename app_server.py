"""校园 AI 创作平台原型后端。标准库 + SQLite，旧接口保持兼容。"""
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import sqlite3
import threading
import time
import urllib.parse

import analytics
import authz
import config
import importer
import migrations
import onboarding
import org_service
import relay
import roster
import works_service

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
STATIC_FILES = {"/", "/index.html", "/admin", "/admin.html", "/teacher", "/teacher.html",
                "/import", "/import.html", "/app.js", "/admin.js", "/teacher.js", "/import.js",
                "/components.js", "/styles.css"}
MAX_JSON_BODY = 65536
MAX_IMPORT_BODY = importer.MAX_BYTES
LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def setup():
    migrations.migrate(DB_PATH)
    relay.init(RELAY_DB)
    migrations.backfill_relay(RELAY_DB, DB_PATH)


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


def new_session(conn, owner_ref, user_id=None, org_id=None):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions (token_hash, owner_ref, user_id, org_id, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (token_digest(token), owner_ref, user_id, org_id, now + SESSION_SECONDS))
    return token


def new_admin_session(conn):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO admin_sessions (token_hash, user_id, role, expires_at) VALUES (?, NULL, ?, ?)",
                 (token_digest(token), authz.PLATFORM_ADMIN, now + ADMIN_SESSION_SECONDS))
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


def _membership(conn, user_id, org_id=None):
    if not user_id:
        return None
    if org_id:
        return conn.execute("SELECT * FROM org_memberships WHERE user_id = ? AND org_id = ?",
                            (user_id, org_id)).fetchone()
    pilot = conn.execute("SELECT * FROM org_memberships WHERE user_id = ? AND org_id = ?",
                         (user_id, config.PILOT_ORG["id"])).fetchone()
    return pilot or conn.execute("SELECT * FROM org_memberships WHERE user_id = ? ORDER BY joined_at LIMIT 1",
                                 (user_id,)).fetchone()


def _principal_for(conn, user_id, org_id=None):
    if not user_id:
        return None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or user["disabled_at"]:
        return None
    membership = _membership(conn, user_id, org_id)
    # 停用成员的旧会话立即失效，视同未认证；pending_review 仍可登录，能力由 authz 矩阵限制。
    if membership and authz.admission_blocked(membership["admission_status"]):
        return None
    platform = conn.execute("SELECT 1 FROM platform_admins WHERE user_id = ?", (user_id,)).fetchone() is not None
    if platform:
        role = authz.PLATFORM_ADMIN
    elif membership:
        role = membership["role"]
    else:
        return None
    org = None
    if membership:
        org_row = conn.execute("SELECT * FROM organizations WHERE id = ?", (membership["org_id"],)).fetchone()
        if org_row:
            org = {"id": org_row["id"], "name": org_row["name"], "slug": org_row["slug"],
                   "adminLabel": org_row["admin_label"], "memberLabel": org_row["member_label"]}
    return {
        "kind": "platform" if platform else "user", "user_id": user_id, "role": role,
        "org_id": membership["org_id"] if membership else None,
        "admission_status": membership["admission_status"] if membership else None,
        "membership": membership, "org": org, "display_name": user["display_name"],
    }


def principal_from_token(token, conn=None):
    if not token:
        return None
    own = conn is None
    conn = conn or db()
    try:
        row = conn.execute("SELECT owner_ref, user_id, org_id, expires_at FROM sessions WHERE token_hash = ?",
                           (token_digest(token),)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        user_id = row["user_id"]
        if not user_id:
            stored = conn.execute("SELECT user_id FROM students WHERE owner_ref = ?", (row["owner_ref"],)).fetchone()
            user_id = stored["user_id"] if stored else None
        principal = _principal_for(conn, user_id, row["org_id"])
        if principal:
            principal["owner_ref"] = row["owner_ref"]
        return principal
    finally:
        if own:
            conn.close()


def _bearer(headers):
    header = headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else None


def key_block(result):
    return {"status": result["status"], "plaintext": result["plaintext"], "hint": result["hint"]}


def ensure_key(owner_ref, request_id, mode=None, identity=None, user_id=None, org_id=None):
    try:
        if mode and identity:
            relay.upsert_account(RELAY_DB, owner_ref, mode, identity, roster.name_for(mode, identity),
                                 user_id=user_id, org_id=org_id)
        result = relay.issue_key(RELAY_DB, owner_ref, "issue:" + request_id)
        if user_id:
            conn = relay.connect(RELAY_DB)
            try:
                conn.execute("UPDATE relay_keys SET user_id = ? WHERE owner_ref = ?", (user_id, owner_ref))
            finally:
                conn.close()
        return key_block(result)
    except relay.AlreadyIssued:
        return {"status": "active", "plaintext": None, "hint": relay.active_hint(RELAY_DB, owner_ref)}
    except relay.RelayUnavailable as exc:
        return {"status": "unavailable", "plaintext": None, "hint": None, "note": str(exc)}


def _student_row(conn, owner_ref):
    return conn.execute("SELECT * FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()


def profile(conn, owner_ref):
    row = _student_row(conn, owner_ref)
    principal = _principal_for(conn, row["user_id"] if row and row["user_id"] else None,
                               row["org_id"] if row else None)
    view = {"mode": row["mode"], "identity": row["identity"], "balance": row["balance"],
            "spent": row["spent"], "ledger": ledger_rows(conn, owner_ref)}
    if principal:
        view.update({"org": principal.get("org"), "role": principal.get("role"),
                     "admissionStatus": principal.get("admission_status"),
                     "labels": principal["org"] and {"admin": principal["org"]["adminLabel"],
                                                      "member": principal["org"]["memberLabel"]},
                     "onboarding": onboarding.summary(conn, principal["user_id"])})
    return view


def _user_for_registration(conn, mode, identity, sid):
    row = conn.execute("SELECT user_id FROM user_identities WHERE kind = ? AND value_norm = ?",
                       (mode, identity)).fetchone()
    if row:
        return row["user_id"]
    if sid:
        row = conn.execute("SELECT user_id FROM user_identities WHERE kind = 'student' AND value_norm = ?",
                           (sid,)).fetchone()
        if row:
            return row["user_id"]
    user_id = "u_" + secrets.token_hex(12)
    conn.execute("INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
                 (user_id, roster.name_for(mode, identity), time.time()))
    return user_id


def _ensure_identity(conn, user_id, kind, value, source):
    existing = conn.execute("SELECT user_id FROM user_identities WHERE kind = ? AND value_norm = ?",
                            (kind, value)).fetchone()
    if existing:
        return existing["user_id"]
    conn.execute("INSERT INTO user_identities (user_id, kind, value_norm, value_display, source, verified_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (user_id, kind, value, value, source, time.time()))
    return user_id


def _registration_membership(conn, user_id, mode, identity, org_id):
    sid = roster.student_id_of(mode, identity)
    membership = _membership(conn, user_id, org_id)
    if membership:
        return membership
    if org_id and org_id != config.PILOT_ORG["id"]:
        return None
    if not roster.is_enrolled(mode, identity):
        return None
    conn.execute(
        "INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, student_id, name, joined_at) "
        "VALUES (?, ?, 'org_member', 'active', ?, ?, ?, ?)",
        (config.PILOT_ORG["id"], user_id, "roster:" + sid, sid, roster.ROSTER.get(sid), time.time()))
    return _membership(conn, user_id, config.PILOT_ORG["id"])


def do_register(mode, identity, password, request_id, org_id=None):
    problem = check_identity(mode, identity) or check_password(password)
    if problem:
        return 400, {"error": problem}
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
            sid = roster.student_id_of(mode, identity)
            user_id = _user_for_registration(conn, mode, identity, sid)
            _ensure_identity(conn, user_id, mode, identity, "registration")
            if sid:
                _ensure_identity(conn, user_id, "student", sid, "roster")
            membership = _registration_membership(conn, user_id, mode, identity, org_id)
            if not membership or membership["admission_status"] != "active":
                conn.execute("ROLLBACK")
                return 403, {"error": "该身份尚未获得组织准入"}
            now = time.time()
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at, "
                         "user_id, org_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                         (owner_ref, mode, identity, salt.hex(), pw_hash, INITIAL_GRANT, now, user_id,
                          membership["org_id"]))
            conn.execute("INSERT INTO ledger (owner_ref, user_id, org_id, title, amount, kind, request_id, created_at) "
                         "VALUES (?, ?, ?, '新用户注册赠送', ?, 'grant', ?, ?)",
                         (owner_ref, user_id, membership["org_id"], INITIAL_GRANT, "grant:" + owner_ref, now))
            onboarding.ensure(conn, user_id, auto_pending=1)
            token = new_session(conn, owner_ref, user_id, membership["org_id"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    keys = ensure_key(owner_ref, request_id, mode, identity, user_id, membership["org_id"])
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    view.update({"token": token, "firstLogin": True, "relayKey": keys})
    return 201, view


def do_login(mode, identity, password, request_id, org_id=None):
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
                return 429, {"error": "尝试次数过多，请 %d 秒后再试" % (int(fail["locked_until"] - now) + 1)}
            row = conn.execute("SELECT * FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()
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
            membership = _membership(conn, row["user_id"], org_id or row["org_id"])
            if membership and membership["admission_status"] == "disabled":
                conn.execute("COMMIT")
                return 403, {"error": "该组织账户已停用"}
            conn.execute("DELETE FROM login_failures WHERE owner_ref = ?", (owner_ref,))
            token = new_session(conn, owner_ref, row["user_id"], membership["org_id"] if membership else row["org_id"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    if membership and membership["admission_status"] == "active":
        keys = ensure_key(owner_ref, request_id, mode, identity, row["user_id"], membership["org_id"])
    else:
        # 非 active（如 pending_review）登录只回状态与提示，不补发明文密钥；口径与 /api/me 一致。
        hint = relay.active_hint(RELAY_DB, owner_ref)
        keys = {"status": "active" if hint else "none", "plaintext": None, "hint": hint}
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
    hint = relay.active_hint(RELAY_DB, owner_ref)
    view["relayKey"] = {"status": "active" if hint else "none", "plaintext": None, "hint": hint}
    return 200, view


def do_rotate(owner_ref, request_id):
    conn = db()
    try:
        row = _student_row(conn, owner_ref)
    finally:
        conn.close()
    try:
        result = relay.rotate_key(RELAY_DB, owner_ref, "rotate:" + request_id)
        if row and row["user_id"]:
            relay_conn = relay.connect(RELAY_DB)
            try:
                relay_conn.execute("UPDATE relay_keys SET user_id = ? WHERE owner_ref = ?", (row["user_id"], owner_ref))
            finally:
                relay_conn.close()
        return 200, key_block(result)
    except relay.RelayUnavailable as exc:
        return 503, {"error": str(exc)}


def do_consume(owner_ref, amount, request_id):
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount > 10 ** 7:
        return 400, {"error": "消耗数量必须是正整数"}
    # 客户端请求编号只在本接口内解释，避免与赠送等其他流水的 request_id 撞号。
    request_id = "consume:" + str(request_id)[:64]
    with LOCK:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT balance, user_id, org_id FROM students WHERE owner_ref = ?",
                               (owner_ref,)).fetchone()
            dup = conn.execute("SELECT 1 FROM ledger WHERE owner_ref = ? AND request_id = ?",
                               (owner_ref, request_id)).fetchone()
            if dup is None:
                if row["balance"] < amount:
                    conn.execute("ROLLBACK")
                    return 409, {"error": "Token 余额不足"}
                conn.execute("UPDATE students SET balance = balance - ?, spent = spent + ? WHERE owner_ref = ?",
                             (amount, amount, owner_ref))
                conn.execute("INSERT INTO ledger (owner_ref, user_id, org_id, title, amount, kind, request_id, created_at) "
                             "VALUES (?, ?, ?, 'AI 创作体验', ?, 'use', ?, ?)",
                             (owner_ref, row["user_id"], row["org_id"], -amount, request_id, time.time()))
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
    return 200, {"token": token, "role": authz.PLATFORM_ADMIN}


def _admin_principal(token):
    if not token:
        return None
    if is_admin_token(token):
        return {"kind": "platform", "user_id": None, "role": authz.PLATFORM_ADMIN, "org_id": None,
                "org": None, "admission_status": None}
    principal = principal_from_token(token)
    if principal and principal["role"] in (authz.PLATFORM_ADMIN, authz.ORG_ADMIN):
        return principal
    return None


def _scope_org(principal, requested=None):
    if principal["role"] == authz.PLATFORM_ADMIN:
        return requested or config.PILOT_ORG["id"]
    return principal.get("org_id")


def _org_exists(conn, org_id):
    return conn.execute("SELECT 1 FROM organizations WHERE id = ?", (org_id,)).fetchone() is not None


def org_dashboard(org_id, days=90):
    conn = db()
    try:
        data = analytics.dashboard(conn, org_id, days)
        data["organization"] = org_service.org(conn, org_id)
        data["featuredWorks"] = works_service.featured(conn, org_id)
        return 200, data
    finally:
        conn.close()


def _merge_key_hints(conn, rows):
    for item in rows:
        hints = [relay.active_hint(RELAY_DB, ref) for ref in item.get("ownerRefs", [])]
        hints = [hint for hint in hints if hint]
        item["keyHint"] = hints[0] if hints else None
        item["keyStatus"] = "active" if hints else "none"
    return rows


def _revoke_user_keys(conn, user_id, org_id):
    """只吊销该用户在本组织成员关系下的账户（owner_ref）密钥，不碰其他组织的账户。"""
    refs = [row["owner_ref"] for row in conn.execute(
        "SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ?", (user_id, org_id)).fetchall()]
    for ref in refs:
        relay.revoke_by_owner(RELAY_DB, ref)
    return refs


def org_members(org_id, query=None):
    conn = db()
    try:
        return 200, {"members": _merge_key_hints(conn, org_service.members(conn, org_id, query))}
    finally:
        conn.close()


def _import_identity_keys(fields):
    """一行名册的全部身份键：行内学号、行内邮箱，以及名册邮箱里解析出的学号。

    邮箱本地部分就是名册学号时，它和行内学号是同一个人；预览与确认共用这一口径。
    """
    keys = []
    for kind, value in (("student", fields.get("student_id")), ("email", fields.get("email"))):
        if value:
            keys.append((kind, value))
    email = fields.get("email")
    if email and roster.is_enrolled("email", email):
        key = ("student", roster.student_id_of("email", email))
        if key not in keys:
            keys.append(key)
    return keys


def _identity_owners(conn, keys):
    """把身份键解析成用户主体，同一用户只记一次。"""
    owners = []
    for kind, value in keys:
        row = conn.execute("SELECT user_id FROM user_identities WHERE kind = ? AND value_norm = ?",
                           (kind, value)).fetchone()
        if row and row["user_id"] not in owners:
            owners.append(row["user_id"])
    return owners


def _preview_conflicts(conn, org_id, rows):
    for row in rows:
        if row["result"] != "accepted":
            continue
        fields = row["fields"]
        existing = conn.execute("SELECT * FROM org_memberships WHERE org_id = ? AND external_ref = ?",
                                (org_id, fields["external_ref"])).fetchone()
        if existing:
            same = (existing["student_id"] == (fields["student_id"] or None) and
                    (existing["email"] or "") == (fields["email"] or "") and
                    existing["name"] == fields["name"] and existing["role"] == fields["role"] and
                    existing["admission_status"] == fields["status"])
            row.update({"result": "skipped" if same else "conflict",
                        "reason_code": "already_present" if same else "member_conflict",
                        "message": "成员已存在，内容一致" if same else "外部编号已存在且字段不同，请人工处理"})
            continue
        for kind, value in _import_identity_keys(fields):
            identity = conn.execute("SELECT user_id FROM user_identities WHERE kind = ? AND value_norm = ?",
                                    (kind, value)).fetchone()
            if identity:
                other = conn.execute("SELECT external_ref FROM org_memberships WHERE org_id = ? AND user_id = ?",
                                     (org_id, identity["user_id"])).fetchone()
                if other and other["external_ref"] != fields["external_ref"]:
                    row.update({"result": "conflict", "reason_code": "identity_conflict",
                                "message": "该身份已在组织内，但外部编号不同"})
                    break
                if not other and conn.execute("SELECT 1 FROM org_memberships WHERE user_id = ?",
                                              (identity["user_id"],)).fetchone():
                    # 跨组织身份一律判冲突，不自动并入；原因不暴露对方组织名和对方账户信息。
                    row.update({"result": "conflict", "reason_code": "identity_conflict",
                                "message": "该身份已属于其他组织"})
                    break


def _batch_result(conn, batch):
    counts = {"accepted": batch["accepted_count"], "rejected": batch["rejected_count"],
              "skipped": batch["skipped_count"], "conflicts": batch["conflict_count"]}
    rows = conn.execute("SELECT row_no, fields_json, result, reason_code, message FROM import_rows "
                        "WHERE batch_id = ? ORDER BY row_no LIMIT 500", (batch["id"],)).fetchall()
    public_rows = []
    for row in rows:
        try:
            fields = json.loads(row["fields_json"] or "{}")
        except ValueError:
            fields = {}
        public_rows.append({"rowNo": row["row_no"], "result": row["result"], "reasonCode": row["reason_code"],
                            "message": row["message"], "fields": importer.masked_fields(fields)})
    return {"batchId": batch["id"], "status": batch["status"], "counts": counts,
            "rows": public_rows, "rowsTruncated": batch["total_rows"] > len(rows),
            "expiresAt": batch["expires_at"], "committedAt": batch["committed_at"]}


def _cleanup_expired_imports(conn, now):
    """一次删净过期批次及其明细；清理失败只回滚清理本身，不影响本次预览。"""
    try:
        conn.execute("SAVEPOINT import_cleanup")
        conn.execute("DELETE FROM import_rows WHERE batch_id IN ("
                     "SELECT id FROM import_batches WHERE expires_at < ? OR status = 'expired')",
                     (now - 86400,))
        conn.execute("DELETE FROM import_batches WHERE expires_at < ? OR status = 'expired'",
                     (now - 86400,))
        conn.execute("RELEASE import_cleanup")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK TO import_cleanup")
            conn.execute("RELEASE import_cleanup")
        except sqlite3.Error:
            pass


def import_preview(principal, org_id, data, schema_version=1):
    authz.require(principal, "members", "import", org_id=org_id)
    rows = importer.parse(data)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _preview_conflicts(conn, org_id, rows)
        batch_id = "imp_" + secrets.token_hex(10)
        now = time.time()
        digest = importer.digest(data)
        counts = {key: sum(1 for row in rows if row["result"] == key)
                  for key in ("accepted", "rejected", "skipped", "conflict")}
        conn.execute(
            "INSERT INTO import_batches (id, org_id, actor_user_id, file_sha256, schema_version, status, "
            "preview_digest, expires_at, created_at, total_rows, accepted_count, rejected_count, skipped_count, "
            "conflict_count) VALUES (?, ?, ?, ?, ?, 'previewed', ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, org_id, principal.get("user_id"), digest, schema_version,
             hashlib.sha256((org_id + digest + batch_id).encode()).hexdigest(), now + 1800, now, len(rows),
             counts["accepted"], counts["rejected"], counts["skipped"], counts["conflict"]))
        for row in rows:
            conn.execute("INSERT INTO import_rows (batch_id, row_no, row_key, fields_json, result, reason_code, message) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (batch_id, row["row_no"], row.get("row_key"),
                          json.dumps(row.get("fields") or {}, ensure_ascii=False), row["result"],
                          row.get("reason_code"), row.get("message")))
        _cleanup_expired_imports(conn, now)
        org_service.audit(conn, principal.get("user_id"), principal["role"], org_id, "import.preview",
                          "import_batch", batch_id, metadata={"rows": len(rows), **counts})
        conn.execute("COMMIT")
        batch = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        return 200, _batch_result(conn, batch)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _write_import_row(conn, org_id, fields, now):
    existing = conn.execute("SELECT 1 FROM org_memberships WHERE org_id = ? AND external_ref = ?",
                            (org_id, fields["external_ref"])).fetchone()
    if existing:
        return "skipped", "already_present"
    keys = _import_identity_keys(fields)
    owners = _identity_owners(conn, keys)
    if len(owners) > 1:
        # 同一行的多个身份键指向不同用户，不能当成一个人复用，判冲突。
        return "conflict", "identity_conflict"
    user_id = owners[0] if owners else None
    if user_id:
        if conn.execute("SELECT 1 FROM org_memberships WHERE org_id = ? AND user_id = ?",
                        (org_id, user_id)).fetchone():
            return "conflict", "identity_conflict"
        # 预览后确认前身份被其他组织占用的竞态同样判冲突：不写成员、不复用其他组织的账户。
        if conn.execute("SELECT 1 FROM org_memberships WHERE user_id = ?", (user_id,)).fetchone():
            return "conflict", "identity_conflict"
    if not user_id:
        user_id = "u_" + secrets.token_hex(12)
        conn.execute("INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
                     (user_id, fields["name"], now))
    for kind, value in keys:
        _ensure_identity(conn, user_id, kind, value, "import")
    conn.execute(
        "INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, student_id, email, name, "
        "joined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (org_id, user_id, fields["role"], fields["status"], fields["external_ref"],
         fields.get("student_id") or None, fields.get("email") or None, fields["name"], now))
    return "accepted", None


def import_commit(principal, org_id, batch_id, request_id):
    authz.require(principal, "members", "import", org_id=org_id)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute("SELECT * FROM import_batches WHERE id = ? AND org_id = ?", (batch_id, org_id)).fetchone()
        if not batch:
            conn.execute("ROLLBACK")
            return 403, {"error": "无权访问该资源"}
        if batch["status"] == "committed":
            conn.execute("ROLLBACK")
            return 200, json.loads(batch["result_json"] or "{}")
        if batch["expires_at"] < time.time():
            conn.execute("UPDATE import_batches SET status = 'expired' WHERE id = ?", (batch_id,))
            conn.execute("COMMIT")
            return 410, {"error": "预览已过期，请重新上传"}
        counts = {"accepted": 0, "rejected": 0, "skipped": 0, "conflicts": 0}
        failures = []
        now = time.time()
        rows = conn.execute("SELECT * FROM import_rows WHERE batch_id = ? ORDER BY row_no", (batch_id,)).fetchall()
        for row in rows:
            if row["result"] != "accepted":
                key = "conflicts" if row["result"] == "conflict" else row["result"]
                counts[key] = counts.get(key, 0) + 1
                continue
            fields = json.loads(row["fields_json"])
            result, reason = _write_import_row(conn, org_id, fields, now)
            if result == "accepted":
                counts["accepted"] += 1
            elif result == "skipped":
                counts["skipped"] += 1
            else:
                counts["conflicts"] += 1
                failures.append({"rowNo": row["row_no"], "reasonCode": reason})
        result = {"batchId": batch_id, "status": "committed", "counts": counts, "partialFailures": failures,
                  "committedAt": now}
        conn.execute("UPDATE import_batches SET status = 'committed', committed_at = ?, accepted_count = ?, "
                     "rejected_count = ?, skipped_count = ?, conflict_count = ?, result_json = ? WHERE id = ?",
                     (now, counts["accepted"], counts["rejected"], counts["skipped"], counts["conflicts"],
                      json.dumps(result, ensure_ascii=False), batch_id))
        org_service.audit(conn, principal.get("user_id"), principal["role"], org_id, "import.commit",
                          "import_batch", batch_id, request_id, {"counts": counts})
        conn.execute("COMMIT")
        return 200, result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


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

    def _send_csv(self, code, data, filename):
        self.send_response(code)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc):
        if isinstance(exc, authz.AuthzError):
            return self._send(exc.status, {"error": exc.message})
        if isinstance(exc, importer.ImportError_):
            return self._send(exc.status, {"error": exc.message, "reasonCode": exc.code})
        if isinstance(exc, onboarding.OnboardingError):
            payload = {"error": exc.message, "reasonCode": exc.code}
            if exc.current is not None:
                payload["current"] = exc.current
            return self._send(exc.status, payload)
        if isinstance(exc, works_service.WorkError):
            return self._send(exc.status, {"error": exc.message, "reasonCode": exc.code})
        if isinstance(exc, ValueError):
            return self._send(400, {"error": str(exc)})
        raise exc

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length > MAX_JSON_BODY:
            return {}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _raw(self, limit):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise importer.ImportError_("invalid_length", "上传长度不合法", 400)
        if length > limit:
            raise importer.ImportError_("file_too_large", "文件超过 10 MB 上限", 413)
        return self.rfile.read(length) if length else b""

    def _principal(self):
        return principal_from_token(_bearer(self.headers))

    def _admin(self):
        return _admin_principal(_bearer(self.headers))

    def _require_user(self):
        principal = self._principal()
        if not principal:
            raise authz.AuthzError(401, "请先登录")
        return principal

    def _require_admin(self):
        token = _bearer(self.headers)
        if is_admin_token(token):
            return {"kind": "platform", "user_id": None, "role": authz.PLATFORM_ADMIN, "org_id": None,
                    "org": None, "admission_status": None}
        principal = principal_from_token(token)
        if principal:
            if principal["role"] not in (authz.PLATFORM_ADMIN, authz.ORG_ADMIN):
                raise authz.AuthzError(403, "无权访问该资源")
            return principal
        raise authz.AuthzError(401, "请先登录管理后台")

    def _org_allowed(self, principal, org_id):
        if principal["role"] != authz.PLATFORM_ADMIN and principal.get("org_id") != org_id:
            raise authz.AuthzError(403, "无权访问该资源")
        return org_id

    def _require_org(self, org_id):
        """组织必须真实存在；不存在统一返回 404，而不是 500 或全 0 的假成功。"""
        conn = db()
        try:
            if not _org_exists(conn, org_id):
                raise authz.AuthzError(404, "未找到该组织")
        finally:
            conn.close()
        return org_id

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/health":
                return self._send(200, {"ok": True})
            if path == "/api/config":
                return self._send(200, {"productName": config.PRODUCT_NAME, "pilotOrgId": config.PILOT_ORG["id"],
                                        "onboarding": onboarding.public_config()})
            if path == "/api/me":
                principal = self._require_user()
                authz.require(principal, "profile", "read_self", owner_user_id=principal["user_id"])
                return self._send(*do_me(principal["owner_ref"]))
            if path == "/api/onboarding":
                principal = self._require_user()
                authz.require(principal, "onboarding", "read", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    return self._send(200, {"config": onboarding.public_config(),
                                            "progress": onboarding.load(conn, principal["user_id"])})
                finally:
                    conn.close()
            if path == "/api/works":
                principal = self._require_user()
                org_id = _scope_org(principal, query.get("orgId", [None])[0])
                authz.require(principal, "works", "read_gallery", org_id=org_id)
                conn = db()
                try:
                    return self._send(200, {"items": works_service.gallery(conn, org_id), "nextCursor": None})
                finally:
                    conn.close()
            if path == "/api/works/mine":
                principal = self._require_user()
                authz.require(principal, "works", "read_own", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    return self._send(200, {"items": works_service.mine(conn, principal["user_id"])})
                finally:
                    conn.close()
            match = re.match(r"^/api/works/([^/]+)$", path)
            if match:
                principal = self._require_user()
                conn = db()
                try:
                    row = works_service.get(conn, match.group(1))
                    if not row:
                        return self._send(404, {"error": "未找到作品"})
                    if not authz.work_readable(principal, row["org_id"], row["owner_user_id"],
                                               row["status"] == "published"):
                        return self._send(403, {"error": "无权访问该资源"})
                    return self._send(200, works_service._card(row))
                finally:
                    conn.close()
            if path == "/api/orgs":
                principal = self._require_admin()
                authz.require(principal, "organizations", "list")
                conn = db()
                try:
                    return self._send(200, {"organizations": org_service.organizations(conn)})
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/works$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "works", "review", org_id=org_id)
                status = query.get("status", [None])[0]
                conn = db()
                try:
                    sql = ("SELECT w.*, CASE WHEN f.work_id IS NULL THEN 0 ELSE 1 END AS featured FROM works w "
                           "LEFT JOIN featured_works f ON f.work_id = w.work_id AND f.org_id = w.org_id "
                           "WHERE w.org_id = ? ")
                    params = [org_id]
                    if status:
                        sql += "AND w.status = ? "
                        params.append(status)
                    sql += "ORDER BY w.updated_at DESC LIMIT 200"
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    return self._send(200, {"items": [works_service._card(row) for row in rows]})
                finally:
                    conn.close()
            if path == "/api/audit-events":
                principal = self._require_admin()
                org_id = query.get("orgId", [None])[0]
                if principal["role"] != authz.PLATFORM_ADMIN:
                    if org_id and org_id != principal.get("org_id"):
                        raise authz.AuthzError(403, "无权访问该资源")
                    org_id = principal.get("org_id")
                authz.require(principal, "audit", "read", org_id=org_id)
                conn = db()
                try:
                    return self._send(200, {"events": org_service.list_audit(conn, org_id)})
                finally:
                    conn.close()
            if path == "/api/admin/accounts":
                principal = self._require_admin()
                authz.require(principal, "accounts", "list", org_id=principal.get("org_id"))
                q = query.get("q", [None])[0]
                org_filter = None if principal["role"] == authz.PLATFORM_ADMIN else principal.get("org_id")
                return self._send(200, {"accounts": relay.list_accounts(RELAY_DB, q, org_filter)})
            match = re.match(r"^/api/orgs/([^/]+)/(tree|summary|trends|dashboard|members|featured|audit|imports/template)$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                action = match.group(2)
                if action == "tree":
                    authz.require(principal, "organizations", "read", org_id=org_id)
                    conn = db()
                    try:
                        tree = org_service.tree(conn, org_id)
                        items = {item["userId"]: item for item in _merge_key_hints(
                            conn, org_service.members(conn, org_id))}
                        for child in tree.get("children", []):
                            rich = items.get(child["id"])
                            if rich:
                                child["keyHint"] = rich.get("keyHint")
                                child["keyStatus"] = rich.get("keyStatus")
                        return self._send(200, tree)
                    finally:
                        conn.close()
                if action in ("summary", "trends", "dashboard"):
                    authz.require(principal, "usage", "read_org", org_id=org_id)
                    days = int(query.get("days", [90])[0])
                    if action == "summary":
                        conn = db()
                        try:
                            return self._send(200, analytics.summary(conn, org_id))
                        finally:
                            conn.close()
                    if action == "trends":
                        conn = db()
                        try:
                            return self._send(200, analytics.trend(conn, org_id, days))
                        finally:
                            conn.close()
                    return self._send(*org_dashboard(org_id, days))
                if action == "members":
                    authz.require(principal, "members", "list", org_id=org_id)
                    return self._send(*org_members(org_id, query.get("q", [None])[0]))
                if action == "featured":
                    authz.require(principal, "works", "read_gallery", org_id=org_id)
                    conn = db()
                    try:
                        return self._send(200, {"items": works_service.featured(conn, org_id)})
                    finally:
                        conn.close()
                if action == "audit":
                    authz.require(principal, "audit", "read", org_id=org_id)
                    conn = db()
                    try:
                        return self._send(200, {"events": org_service.list_audit(conn, org_id)})
                    finally:
                        conn.close()
                if action == "imports/template":
                    authz.require(principal, "members", "import", org_id=org_id)
                    return self._send_csv(200, importer.template_csv(), "campus-roster-template.csv")
            match = re.match(r"^/api/orgs/([^/]+)/imports/([^/]+)(/errors.csv)?$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "import", org_id=org_id)
                conn = db()
                try:
                    batch = conn.execute("SELECT * FROM import_batches WHERE id = ? AND org_id = ?",
                                         (match.group(2), org_id)).fetchone()
                    if not batch:
                        return self._send(403, {"error": "无权访问该资源"})
                    if match.group(3):
                        rows = [{"row_no": r["row_no"], "reason_code": r["reason_code"], "message": r["message"],
                                 "fields": json.loads(r["fields_json"] or "{}")}
                                for r in conn.execute("SELECT * FROM import_rows WHERE batch_id = ? AND result != 'accepted'",
                                                      (batch["id"],)).fetchall()]
                        return self._send_csv(200, importer.errors_csv(rows), "import-errors.csv")
                    return self._send(200, _batch_result(conn, batch))
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/members/([^/]+)$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "read", org_id=org_id)
                conn = db()
                try:
                    item = org_service.member(conn, org_id, match.group(2))
                    if not item:
                        return self._send(403, {"error": "无权访问该资源"})
                    _merge_key_hints(conn, [item])
                    return self._send(200, item)
                finally:
                    conn.close()
            if path.startswith("/api/"):
                return self._send(404, {"error": "未知接口"})
            if path not in STATIC_FILES:
                return self._send(404, {"error": "未找到"})
            if path == "/admin":
                self.path = "/admin.html"
            if path == "/teacher":
                self.path = "/teacher.html"
            if path == "/import":
                self.path = "/import.html"
            return super().do_GET()
        except (authz.AuthzError, importer.ImportError_, onboarding.OnboardingError, works_service.WorkError,
                ValueError) as exc:
            return self._error(exc)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        data = self._body()
        try:
            match = re.match(r"^/api/orgs/([^/]+)/members/([^/]+)$", parsed.path)
            if not match:
                return self._send(404, {"error": "未知接口"})
            principal = self._require_admin()
            org_id = self._org_allowed(principal, match.group(1))
            self._require_org(org_id)
            authz.require(principal, "members", "update_status", org_id=org_id)
            if data.get("role") is not None:
                authz.require(principal, "members", "update_role", org_id=org_id)
            conn = db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                item, revoke_keys = org_service.update_member(conn, org_id, match.group(2), data,
                                                              principal.get("user_id"), principal["role"])
                if not item:
                    conn.execute("ROLLBACK")
                    return self._send(403, {"error": "无权访问该资源"})
                if revoke_keys:
                    _revoke_user_keys(conn, match.group(2), org_id)
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "member.key_revoked", "member", match.group(2),
                                      metadata={"reason": "admission_disabled"})
                conn.execute("COMMIT")
                return self._send(200, item)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        except (authz.AuthzError, ValueError) as exc:
            return self._error(exc)
        except relay.RelayUnavailable as exc:
            return self._send(503, {"error": str(exc)})
        except sqlite3.Error:
            return self._send(503, {"error": "写入失败，请稍后重试"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/orgs":
                principal = self._require_admin()
                authz.require(principal, "organizations", "create")
                data = self._body()
                name = str(data.get("name") or "").strip()
                slug = str(data.get("slug") or "").strip().lower()
                if not name or len(name) > 80 or not re.match(r"^[a-z0-9-]{2,60}$", slug):
                    return self._send(400, {"error": "组织名称必填，slug 只能用小写字母、数字和连字符"})
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    org_id = migrations.ensure_org(conn, slug, name, data.get("adminLabel") or "教师",
                                                   data.get("memberLabel") or "学生")
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "organization.created", "organization", org_id, metadata={"name": name})
                    conn.execute("COMMIT")
                    return self._send(201, org_service.org(conn, org_id))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/imports/preview$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "import", org_id=org_id)
                raw = self._raw(MAX_IMPORT_BODY)
                return self._send(*import_preview(principal, org_id, raw))
            match = re.match(r"^/api/orgs/([^/]+)/imports/([^/]+)/commit$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                data = self._body()
                return self._send(*import_commit(principal, org_id, match.group(2),
                                                 str(data.get("requestId") or secrets.token_hex(8))[:64]))
            match = re.match(r"^/api/orgs/([^/]+)/members/bulk$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "bulk", org_id=org_id)
                data = self._body()
                user_ids = data.get("userIds") or []
                if (not isinstance(user_ids, list) or not user_ids or len(user_ids) > 200 or
                        any(not isinstance(user_id, str) for user_id in user_ids)):
                    return self._send(400, {"error": "请选择 1 到 200 名成员"})
                action = data.get("action")
                rid = str(data.get("requestId") or secrets.token_hex(8))[:64]
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if org_service.users_outside_org(conn, org_id, user_ids):
                        conn.execute("ROLLBACK")
                        return self._send(403, {"error": "无权访问该资源"})
                    if action == "grant":
                        amount = data.get("amount")
                        if not isinstance(amount, int) or amount <= 0 or amount > 10 ** 7:
                            conn.execute("ROLLBACK")
                            return self._send(400, {"error": "赠送额度必须是正整数"})
                        results = org_service.bulk_grant(conn, org_id, user_ids, amount, rid,
                                                         principal.get("user_id"), principal["role"])
                    elif action == "revoke":
                        results = []
                        for user_id in user_ids:
                            refs = _revoke_user_keys(conn, user_id, org_id)
                            results.append({"userId": user_id, "status": "revoked", "keys": len(refs)})
                        org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                          "member.bulk_revoke", "organization", org_id, rid,
                                          {"userCount": len(user_ids)})
                    else:
                        conn.execute("ROLLBACK")
                        return self._send(400, {"error": "动作只能是 grant 或 revoke"})
                    conn.execute("COMMIT")
                    return self._send(200, {"results": results})
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/members/([^/]+)/revoke-key$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "revoke_key", org_id=org_id)
                conn = db()
                try:
                    if org_service.users_outside_org(conn, org_id, [match.group(2)]):
                        return self._send(403, {"error": "无权访问该资源"})
                    refs = _revoke_user_keys(conn, match.group(2), org_id)
                    if not refs:
                        return self._send(403, {"error": "无权访问该资源"})
                    conn.execute("BEGIN IMMEDIATE")
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "member.key_revoked", "member", match.group(2))
                    conn.execute("COMMIT")
                    return self._send(200, {"ok": True})
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/works/([^/]+)/(review|feature|unpublish)$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                data = self._body()
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if match.group(3) == "review":
                        authz.require(principal, "works", "review", org_id=org_id)
                        row = works_service.review(conn, match.group(2), org_id, data.get("decision"),
                                                   principal.get("user_id"))
                    elif match.group(3) == "feature":
                        authz.require(principal, "works", "feature", org_id=org_id)
                        row = works_service.set_featured(conn, match.group(2), org_id, principal.get("user_id"),
                                                        bool(data.get("featured")))
                    else:
                        authz.require(principal, "works", "unpublish", org_id=org_id)
                        row = works_service.unpublish(conn, match.group(2), org_id,
                                                      principal.get("user_id") or principal["role"])
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "work." + match.group(3), "work", match.group(2))
                    conn.execute("COMMIT")
                    return self._send(200, works_service._card(row))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            if path == "/api/works":
                principal = self._require_user()
                authz.require(principal, "works", "submit", owner_user_id=principal["user_id"])
                data = self._body()
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = works_service.submit(conn, principal["org_id"], principal["user_id"],
                                               (principal.get("membership") or {})["name"] or principal.get("display_name"),
                                               data)
                    org_service.audit(conn, principal["user_id"], principal["role"], principal["org_id"],
                                      "work.submitted", "work", row["work_id"])
                    conn.execute("COMMIT")
                    return self._send(201, works_service._card(row))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/works/([^/]+)/withdraw$", path)
            if match:
                principal = self._require_user()
                authz.require(principal, "works", "withdraw", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = works_service.withdraw(conn, match.group(1), principal["user_id"])
                    org_service.audit(conn, principal["user_id"], principal["role"], row["org_id"],
                                      "work.withdrawn", "work", row["work_id"])
                    conn.execute("COMMIT")
                    return self._send(200, works_service._card(row))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            if path == "/api/onboarding":
                principal = self._require_user()
                authz.require(principal, "onboarding", "write", owner_user_id=principal["user_id"])
                data = self._body()
                conn = db()
                try:
                    state, changed, previous = onboarding.apply(conn, principal["user_id"], data)
                    return self._send(200, {"progress": state, "changed": changed})
                finally:
                    conn.close()
            data = self._body()
            rid = str(data.get("requestId") or secrets.token_hex(8))[:64]
            mode = data.get("mode", "email")
            identity = str(data.get("identity") or "").strip()
            password = data.get("password")
            target_org = data.get("orgId")
            if path == "/api/register":
                return self._send(*do_register(mode, identity, password, rid, target_org))
            if path == "/api/login":
                return self._send(*do_login(mode, identity, password, rid, target_org))
            if path in ("/api/key/rotate", "/api/consume", "/api/logout"):
                principal = self._require_user()
                if path == "/api/key/rotate":
                    authz.require(principal, "keys", "rotate_self", owner_user_id=principal["user_id"])
                    return self._send(*do_rotate(principal["owner_ref"], rid))
                if path == "/api/consume":
                    authz.require(principal, "usage", "consume_self", owner_user_id=principal["user_id"])
                    return self._send(*do_consume(principal["owner_ref"], data.get("amount"), rid))
                authz.require(principal, "sessions", "revoke_self", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                                 (token_digest(_bearer(self.headers)),))
                finally:
                    conn.close()
                return self._send(200, {"ok": True})
            if path == "/api/admin/login":
                return self._send(*do_admin_login(data.get("username"), data.get("password")))
            if path == "/api/admin/revoke":
                principal = self._require_admin()
                owner_ref = str(data.get("owner_ref") or "").strip()
                if not owner_ref:
                    return self._send(400, {"error": "缺少 owner_ref"})
                conn = db()
                try:
                    row = conn.execute("SELECT org_id FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()
                    if not row:
                        return self._send(403, {"error": "无权访问该资源"})
                    authz.require(principal, "accounts", "revoke_key", org_id=row["org_id"])
                    relay.revoke_by_owner(RELAY_DB, owner_ref)
                    conn.execute("BEGIN IMMEDIATE")
                    org_service.audit(conn, principal.get("user_id"), principal["role"], row["org_id"],
                                      "admin.key_revoked", "account", owner_ref)
                    conn.execute("COMMIT")
                    return self._send(200, {"ok": True})
                finally:
                    conn.close()
            if path == "/api/reset" and os.environ.get("DEMO_ALLOW_RESET") == "1":
                return self._send(*do_reset())
            return self._send(404, {"error": "未知接口"})
        except (authz.AuthzError, importer.ImportError_, onboarding.OnboardingError, works_service.WorkError,
                ValueError) as exc:
            return self._error(exc)


def do_reset():
    with LOCK:
        conn = db()
        try:
            for table in ("onboarding_progress", "ledger", "sessions", "login_failures", "admin_sessions", "students"):
                conn.execute("DELETE FROM " + table)
        finally:
            conn.close()
        relay.reset(RELAY_DB)
    return 200, {"ok": True}


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
