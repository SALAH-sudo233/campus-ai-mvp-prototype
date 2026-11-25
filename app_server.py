"""校园 AI 创作平台原型后端。标准库 + SQLite，旧接口保持兼容。"""
import hashlib
import hmac
import http.server
import mimetypes
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
import pat_service
import relay
import rewards_service
import roster
import works_service
from identity import normalize_identity, owner_ref_of

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DEMO_DB", os.path.join(ROOT, "demo-backend.sqlite"))
DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
RELAY_DB = os.environ.get("DEMO_RELAY_DB", os.path.join(ROOT, "demo-relay.sqlite"))
INITIAL_GRANT = config.DEFAULT_MEMBER_QUOTA
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
                "/import", "/import.html", "/gallery", "/gallery.html", "/app.js", "/admin.js",
                "/teacher.js", "/import.js", "/gallery.js", "/components.js", "/styles.css",
                "/agent-tools.js", "/cli/campus-cli", "/skills/campus-platform/SKILL.md"}
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
    migrations.fold_relay_email_case(RELAY_DB)
    migrations.backfill_relay(RELAY_DB, DB_PATH)
    relay.backfill_legacy(RELAY_DB, quota_lookup=_quota_for_owner)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        works_service.ensure_social_tables(conn, DATA_DIR)
        if _demo_login_enabled():
            rewards_service.ensure_demo_candidates(conn, config.PILOT_ORG["id"])
        conn.execute("COMMIT")
    finally:
        conn.close()
    revoke_inactive_member_keys()


def revoke_inactive_member_keys():
    """启动时清扫存量非准入成员的密钥；重复执行不会产生新的吊销。"""
    conn = db()
    try:
        refs = [row["owner_ref"] for row in conn.execute(
            "SELECT DISTINCT s.owner_ref FROM students s "
            "JOIN org_memberships m ON m.user_id = s.user_id AND m.org_id = s.org_id "
            "WHERE m.admission_status IN ('pending_review', 'disabled')").fetchall()]
    finally:
        conn.close()
    for ref in refs:
        relay.revoke_by_owner(RELAY_DB, ref)
    return len(refs)


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


def new_session(conn, owner_ref, user_id=None, org_id=None, demo=False):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions (token_hash, owner_ref, user_id, org_id, expires_at, demo) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (token_digest(token), owner_ref, user_id, org_id, now + SESSION_SECONDS, 1 if demo else 0))
    return token


def new_admin_session(conn, demo=False):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO admin_sessions (token_hash, user_id, role, expires_at, demo) VALUES (?, NULL, ?, ?, ?)",
                 (token_digest(token), authz.PLATFORM_ADMIN, now + ADMIN_SESSION_SECONDS, 1 if demo else 0))
    return token


def admin_session_info(token):
    if not token:
        return None
    conn = db()
    try:
        row = conn.execute("SELECT expires_at, demo FROM admin_sessions WHERE token_hash = ?",
                           (token_digest(token),)).fetchone()
    finally:
        conn.close()
    if row is None or row["expires_at"] < time.time():
        return None
    return {"demo": bool(row["demo"])}


def is_admin_token(token):
    return admin_session_info(token) is not None


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


def _principal_from_pat(token, conn):
    row = conn.execute("SELECT * FROM pat_tokens WHERE token_hash = ?", (token_digest(token),)).fetchone()
    context = org_service.AUTH_CONTEXT.get()
    if context is not None:
        context.update({"authMethod": "pat", "patId": row["id"] if row else None})
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= time.time():
        raise authz.AuthzError(401, pat_service.REISSUE_MESSAGE)
    principal = _principal_for(conn, row["user_id"], row["org_snapshot"])
    if not principal or pat_service.permissions_shrank(row["role_snapshot"], principal["role"]):
        raise authz.AuthzError(401, pat_service.REISSUE_MESSAGE)
    student = conn.execute("SELECT owner_ref FROM students WHERE user_id = ? "
                           "ORDER BY created_at LIMIT 1", (row["user_id"],)).fetchone()
    if student is None:
        raise authz.AuthzError(401, pat_service.REISSUE_MESSAGE)
    principal.update({"owner_ref": student["owner_ref"],
                      "auth_method": "pat", "pat_id": row["id"]})
    conn.execute("UPDATE pat_tokens SET last_used_at = ? WHERE id = ?", (time.time(), row["id"]))
    return principal


def _org_quota(conn, org_id):
    if not org_id:
        return None, INITIAL_GRANT
    row = conn.execute("SELECT quota_limit FROM organizations WHERE id = ?", (org_id,)).fetchone()
    total = int(row["quota_limit"]) if row and row["quota_limit"] is not None else None
    roster = conn.execute("SELECT COUNT(*) AS n FROM org_memberships WHERE org_id = ?", (org_id,)).fetchone()["n"]
    roster = max(int(roster or 0), 1)
    return total, (total // roster if total else INITIAL_GRANT)


def _active_owner_refs(conn, org_id=None, owner_ref=None):
    sql = ("SELECT s.owner_ref FROM students s JOIN org_memberships m "
           "ON m.user_id = s.user_id AND m.org_id = s.org_id "
           "WHERE m.admission_status = 'active' ")
    params = []
    if org_id:
        sql += "AND m.org_id = ? "
        params.append(org_id)
    if owner_ref:
        sql += "AND s.owner_ref = ? "
        params.append(owner_ref)
    return [row["owner_ref"] for row in conn.execute(sql, tuple(params)).fetchall()]


def _quota_for_owner(owner_ref):
    conn = db()
    try:
        row = conn.execute("SELECT org_id FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()
        return _org_quota(conn, row["org_id"] if row else None)[1]
    finally:
        conn.close()


def principal_from_token(token, conn=None):
    if not token:
        return None
    own = conn is None
    conn = conn or db()
    try:
        if token.startswith("cpat_"):
            return _principal_from_pat(token, conn)
        row = conn.execute("SELECT owner_ref, user_id, org_id, expires_at, demo FROM sessions WHERE token_hash = ?",
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
            principal["auth_method"] = "session"
            principal["demo"] = bool(row["demo"])
        return principal
    finally:
        if own:
            conn.close()


def _bearer(headers):
    header = headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else None


def key_block(result):
    return {"status": result["status"], "plaintext": result.get("plaintext"), "hint": result.get("hint"),
            "nextRetryAt": result.get("next_retry_at"), "lastError": result.get("last_error")}


def _prepare_key_issue(owner_ref, request_id, mode=None, identity=None, user_id=None, org_id=None,
                       rotate=False):
    if mode and identity:
        relay.upsert_account(RELAY_DB, owner_ref, mode, identity, roster.name_for(mode, identity),
                             user_id=user_id, org_id=org_id)
    conn = db()
    try:
        _, quota = _org_quota(conn, org_id)
    finally:
        conn.close()
    issue_request_id = ("rotate:" if rotate else "issue:") + request_id
    job = relay.prepare_issue(RELAY_DB, owner_ref, issue_request_id, quota=quota, rotate=rotate)
    job["_path"] = RELAY_DB
    return job


def _finish_key_issue(job, user_id=None):
    try:
        result = relay.finish_issue(job, deliver_plaintext=True)
        if user_id:
            conn = relay.connect(RELAY_DB)
            try:
                conn.execute("UPDATE relay_keys SET user_id = ? WHERE id = ?", (user_id, result.get("key_id")))
            finally:
                conn.close()
        return key_block(result)
    except relay.AlreadyIssued:
        return {"status": "active", "plaintext": None, "hint": None}


def ensure_key(owner_ref, request_id, mode=None, identity=None, user_id=None, org_id=None):
    try:
        job = _prepare_key_issue(owner_ref, request_id, mode, identity, user_id, org_id)
    except relay.AlreadyIssued:
        return {"status": "active", "plaintext": None, "hint": relay.active_hint(RELAY_DB, owner_ref)}
    return _finish_key_issue(job, user_id)


def inactive_key_block(owner_ref):
    """非准入成员的发钥回执：不给明文，只回状态与掩码，口径与 /api/me 一致。"""
    view = relay.key_view(RELAY_DB, owner_ref)
    return {"status": view["state"], "plaintext": None, "hint": view["hint"],
            "nextRetryAt": view["nextRetryAt"], "lastError": view["lastError"]}


def issue_key_for_member(owner_ref, request_id, mode=None, identity=None, user_id=None, org_id=None,
                         rotate=False):
    """发钥的唯一入口：持 LOCK 重读准入，只有 active 成员才发/轮换明文密钥。

    登录、注册、主动轮换都走这里。锁内只做准入重读与影子预占，网络调用在锁外；
    网络返回后再读一次准入，期间被改成 pending_review/disabled 就丢弃明文并吊销残留。
    非 active 只回状态与掩码，与 /api/me 同口径。

    返回 (密钥块, 是否已准入)。调用方不得在持有 LOCK 时调用本函数。
    """
    with LOCK:
        conn = db()
        try:
            row = _student_row(conn, owner_ref)
            member_id = user_id or (row["user_id"] if row else None)
            member_org = org_id or (row["org_id"] if row else None)
            membership = _membership(conn, member_id, member_org)
            active = membership is not None and membership["admission_status"] == "active"
            admitted_org = membership["org_id"] if membership else member_org
            _, quota = _org_quota(conn, admitted_org)
        finally:
            conn.close()
        if not active:
            key_ids = []
            if rotate:
                # 非 active 时不仅不发，还要顺手吊销残留的 active 密钥；没有有效密钥时也是空操作。
                key_ids = relay.prepare_revoke(RELAY_DB, owner_ref)
        else:
            try:
                job = _prepare_key_issue(owner_ref, request_id, mode, identity, member_id, admitted_org, rotate=rotate)
            except relay.AlreadyIssued:
                job = None
    if not active:
        if key_ids:
            relay.finish_revocations(key_ids, RELAY_DB)
        return inactive_key_block(owner_ref), False
    if job is None:
        return {"status": "active", "plaintext": None, "hint": relay.active_hint(RELAY_DB, owner_ref)}, True
    result = _finish_key_issue(job, member_id)

    # 网络阶段不持全局 LOCK；回来后再读一次准入。成员若已被停用/待复核，
    # 必须丢弃刚取到的明文并吊销残留，保证最终不留 active 密钥。
    revoke_ids = []
    with LOCK:
        conn = db()
        try:
            still_active = owner_ref in set(_active_owner_refs(conn, org_id=admitted_org, owner_ref=owner_ref))
        finally:
            conn.close()
        if not still_active:
            revoke_ids = relay.prepare_revoke(RELAY_DB, owner_ref)
    if revoke_ids:
        relay.finish_revocations(revoke_ids, RELAY_DB)
    if not still_active:
        return inactive_key_block(owner_ref), False
    return result, True


def _student_row(conn, owner_ref):
    return conn.execute("SELECT * FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()


def profile(conn, owner_ref):
    row = _student_row(conn, owner_ref)
    principal = _principal_for(conn, row["user_id"] if row and row["user_id"] else None,
                               row["org_id"] if row else None)
    view = {"mode": row["mode"], "identity": row["identity"], "balance": row["balance"],
            "spent": row["spent"], "ledger": ledger_rows(conn, owner_ref)}
    total_quota, member_quota = _org_quota(conn, row["org_id"] if row else None)
    key_state = relay.key_view(RELAY_DB, owner_ref)
    reward_summary = rewards_service.user_reward_summary(
        conn, row["user_id"] if row else None, row["org_id"] if row else None
    )
    view["quota"] = {
        "totalQuota": total_quota,
        "memberQuota": member_quota,
        "remainingQuota": int(row["balance"] or 0),
        "usedQuota": int(row["spent"] or 0),
        "rewardQuota": reward_summary["totalAmount"],
        "rewardCount": reward_summary["workCount"],
        "state": key_state["state"],
        "source": "platform_ledger",
        "displayBalance": row["balance"],
    }
    if principal:
        view.update({"org": principal.get("org"), "role": principal.get("role"),
                     "admissionStatus": principal.get("admission_status"),
                     "labels": principal["org"] and {"admin": principal["org"]["adminLabel"],
                                                      "member": principal["org"]["memberLabel"]},
                     "onboarding": onboarding.summary(conn, principal["user_id"])})
    return view


def _user_for_registration(conn, mode, identity, sid):
    identity = normalize_identity(mode, identity)
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
    value = normalize_identity(kind, value)
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
    identity = normalize_identity(mode, identity)
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
            _, grant = _org_quota(conn, membership["org_id"])
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at, "
                         "user_id, org_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                         (owner_ref, mode, identity, salt.hex(), pw_hash, grant, now, user_id,
                          membership["org_id"]))
            conn.execute("INSERT INTO ledger (owner_ref, user_id, org_id, title, amount, kind, request_id, created_at) "
                         "VALUES (?, ?, ?, '新用户注册赠送', ?, 'grant', ?, ?)",
                         (owner_ref, user_id, membership["org_id"], grant, "grant:" + owner_ref, now))
            onboarding.ensure(conn, user_id, auto_pending=1)
            token = new_session(conn, owner_ref, user_id, membership["org_id"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    # 注册与准入变更串行：锁内重读准入，非 active 时只回状态与掩码，不补发明文密钥。
    keys, _ = issue_key_for_member(owner_ref, request_id, mode, identity, user_id, membership["org_id"])
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    view.update({"token": token, "firstLogin": True, "relayKey": keys})
    return 201, view


def do_login(mode, identity, password, request_id, org_id=None):
    identity = normalize_identity(mode, identity)
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
    # 登录事务提交后再发钥：helper 在锁内重读准入，与成员状态变更串行，避免吊销后又补发。
    keys, _ = issue_key_for_member(owner_ref, request_id, mode, identity, row["user_id"],
                                   membership["org_id"] if membership else row["org_id"])
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    view.update({"token": token, "firstLogin": False, "relayKey": keys})
    return 200, view


def do_me(owner_ref, demo=False):
    delivered = {"plaintext": None}
    if not demo:
        conn = db()
        try:
            active = bool(_active_owner_refs(conn, owner_ref=owner_ref))
        finally:
            conn.close()
        delivered = relay.retry_due(
            RELAY_DB, owner_ref=owner_ref, limit_pending=1 if active else 0,
            quota_lookup=_quota_for_owner, allow_delivery_for=owner_ref if active else None,
            owner_refs=[owner_ref] if active else None)
        relay.sync_provider_quotas(RELAY_DB)
    conn = db()
    try:
        view = profile(conn, owner_ref)
    finally:
        conn.close()
    if demo:
        view["relayKey"] = {"status": "demo", "plaintext": None, "hint": None,
                            "note": "演示账号只用于界面演示，不发放真实成员 token"}
        view["demo"] = True
        return 200, view
    key_state = relay.key_view(RELAY_DB, owner_ref)
    view["relayKey"] = {"status": key_state["state"], "plaintext": delivered.get("plaintext"),
                        "hint": key_state["hint"], "nextRetryAt": key_state["nextRetryAt"],
                        "lastError": key_state["lastError"]}
    return 200, view


def do_rotate(owner_ref, request_id):
    block, admitted = issue_key_for_member(owner_ref, request_id, rotate=True)
    if not admitted:
        # 与 authz 的待复核口径一致：轮换被拒绝，已吊销的旧密钥不会因此复活。
        return 403, {"error": authz.PENDING_REVIEW_MESSAGE}
    return 200, block


def do_consume(owner_ref, amount, request_id):
    """仅演示：只改平台分摊账面；New-API token 不设硬限额，不做同步扣减。"""
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
        view = profile(conn, owner_ref)
        view["demoOnly"] = True
        view["notice"] = "平台消费仅用于演示账面，不代表 New-API 实际扣减"
        return 200, view
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


def do_demo_login(role):
    """演示快捷登录使用服务端配置的管理员凭据映射到目标端身份。"""
    if not _demo_login_enabled():
        return 404, {"error": "演示登录未启用"}
    if role == "admin":
        if not ADMIN_USERNAME or not ADMIN_PASSWORD:
            return 404, {"error": "管理功能未启用"}
        conn = db()
        try:
            token = new_admin_session(conn, demo=True)
        finally:
            conn.close()
        return 200, {"token": token, "role": authz.PLATFORM_ADMIN, "demo": True}
    if role not in ("student", "teacher"):
        return 400, {"error": "不支持的演示身份"}
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return 404, {"error": "管理功能未启用"}
    target_role = authz.ORG_ADMIN if role == "teacher" else authz.ORG_MEMBER
    preferred_identity = "2024001040" if role == "teacher" else "2024001100"
    conn = db()
    try:
        row = conn.execute(
            "SELECT s.owner_ref, s.user_id, s.org_id FROM students s "
            "JOIN org_memberships m ON m.user_id = s.user_id AND m.org_id = s.org_id "
            "WHERE m.role = ? AND m.admission_status = 'active' "
            "ORDER BY CASE WHEN s.identity = ? THEN 0 ELSE 1 END, s.created_at LIMIT 1",
            (target_role, preferred_identity),
        ).fetchone()
        if not row:
            return 503, {"error": "暂无可用的演示身份"}
        token = new_session(conn, row["owner_ref"], row["user_id"], row["org_id"], demo=True)
        view = profile(conn, row["owner_ref"])
    finally:
        conn.close()
    view["relayKey"] = {"status": "demo", "plaintext": None, "hint": None,
                        "note": "演示账号只用于界面演示，不发放真实成员 token"}
    view.update({"token": token, "firstLogin": False, "demo": True})
    return 200, view


def _demo_login_enabled():
    return os.environ.get("DEMO_ALLOW_LOGIN") == "1"


def _admin_principal(token):
    if not token:
        return None
    info = admin_session_info(token)
    if info is not None:
        return {"kind": "platform", "user_id": None, "role": authz.PLATFORM_ADMIN, "org_id": None,
                "org": None, "admission_status": None, "demo": info["demo"]}
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
        if relay.sync_provider_quotas(RELAY_DB):
            provider_quota = relay.quota_overview(RELAY_DB, org_id)
        else:
            provider_quota = None
        data = analytics.dashboard(conn, org_id, days, provider_quota=provider_quota)
        data["organization"] = org_service.org(conn, org_id)
        data["featuredWorks"] = works_service.featured(conn, org_id)
        return 200, data
    finally:
        conn.close()


def _merge_key_hints(conn, rows, org_id=None):
    for item in rows:
        views = [relay.key_view(RELAY_DB, ref) for ref in item.get("ownerRefs", [])]
        active = next((view for view in views if view["state"] == "active"), None)
        current = active or next((view for view in views if view["state"] != "none"), None)
        item["keyHint"] = active["hint"] if active else None
        item["keyStatus"] = current["state"] if current else "none"
        item["attempts"] = 0
        item["lastError"] = current["lastError"] if current else None
        item["nextRetryAt"] = current["nextRetryAt"] if current else None
        resolved_org = org_id or (conn.execute("SELECT org_id FROM students WHERE owner_ref = ?",
                                               (item["ownerRefs"][0],)).fetchone()["org_id"]
                                  if item.get("ownerRefs") else None)
        total_quota, member_quota = _org_quota(conn, resolved_org)
        remaining = int(item.get("balance") or 0)
        used = int(item.get("spent") or 0)
        item["quota"] = {"totalQuota": total_quota, "memberQuota": member_quota,
                         "remainingQuota": remaining, "usedQuota": used,
                         "source": "platform_ledger"}
        item["remainingQuota"] = remaining
        item["usedQuota"] = used
    return rows


def _enrich_account_quotas(conn, accounts):
    refs = [item.get("owner_ref") for item in accounts if item.get("owner_ref")]
    balances = {}
    if refs:
        placeholders = ",".join("?" for _ in refs)
        rows = conn.execute("SELECT owner_ref, COALESCE(balance, 0) AS balance, COALESCE(spent, 0) AS spent "
                            "FROM students WHERE owner_ref IN (%s)" % placeholders, tuple(refs)).fetchall()
        balances = {row["owner_ref"]: (int(row["balance"]), int(row["spent"])) for row in rows}
    shares = {}
    for item in accounts:
        org_id = item.get("org_id")
        if org_id not in shares:
            shares[org_id] = _org_quota(conn, org_id)
        total_quota, member_quota = shares[org_id]
        remaining, used = balances.get(item.get("owner_ref"), (None, None))
        item["quota"] = {"totalQuota": total_quota, "memberQuota": member_quota,
                         "remainingQuota": remaining, "usedQuota": used,
                         "source": "platform_ledger"}
        item["remainingQuota"] = remaining
        item["usedQuota"] = used
    return accounts


def _prepare_user_revocations(conn, user_id, org_id):
    """锁内只预占吊销状态；网络删除必须在释放全局 LOCK 后执行。"""
    refs = [row["owner_ref"] for row in conn.execute(
        "SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ?", (user_id, org_id)).fetchall()]
    key_ids = []
    for ref in refs:
        key_ids.extend(relay.prepare_revoke(RELAY_DB, ref))
    return refs, key_ids


def _revoke_user_keys(conn, user_id, org_id):
    """只吊销该用户在本组织成员关系下的账户（owner_ref）密钥，不碰其他组织的账户。"""
    refs, key_ids = _prepare_user_revocations(conn, user_id, org_id)
    relay.finish_revocations(key_ids, RELAY_DB)
    return refs


def org_members(org_id, query=None):
    conn = db()
    try:
        active_refs = _active_owner_refs(conn, org_id=org_id)
        relay.retry_due(RELAY_DB, org_id=org_id, limit_pending=1, quota_lookup=_quota_for_owner,
                        allow_delivery_for=None, owner_refs=active_refs)
        relay.sync_provider_quotas(RELAY_DB)
        members = org_service.members(conn, org_id, query)
        return 200, {"members": _merge_key_hints(conn, members, org_id)}
    finally:
        conn.close()


def _import_identity_keys(fields):
    """一行名册的全部身份键：行内学号、行内邮箱，以及名册邮箱里解析出的学号。

    邮箱本地部分就是名册学号时，它和行内学号是同一个人；预览与确认共用这一口径。
    """
    keys = []
    for kind, value in (("student", fields.get("student_id")), ("email", fields.get("email"))):
        if value:
            keys.append((kind, normalize_identity(kind, value)))
    email = normalize_identity("email", fields.get("email"))
    if email and roster.is_enrolled("email", email):
        key = ("student", roster.student_id_of("email", email))
        if key not in keys:
            keys.append(key)
    return keys


def _identity_owners(conn, keys):
    """把身份键解析成用户主体，同一用户只记一次。"""
    owners = []
    for kind, value in keys:
        value = normalize_identity(kind, value)
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
                    normalize_identity("email", existing["email"] or "") ==
                    normalize_identity("email", fields["email"] or "") and
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
         fields.get("student_id") or None, normalize_identity("email", fields.get("email")) or None,
         fields["name"], now))
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
    def handle_one_request(self):
        context_token = org_service.AUTH_CONTEXT.set({"authMethod": "session", "patId": None})
        self._audit_principal = None
        self._audit_status = None
        try:
            return super().handle_one_request()
        finally:
            try:
                if (self._audit_status is not None and getattr(self, "command", "GET") in
                        ("POST", "PATCH", "PUT", "DELETE") and _bearer(self.headers)):
                    principal = self._audit_principal or {}
                    conn = db()
                    try:
                        org_service.audit(conn, principal.get("user_id"), principal.get("role"),
                                          principal.get("org_id"), "http." + self.command.lower(),
                                          "endpoint", urllib.parse.urlparse(self.path).path,
                                          metadata={"result": "success" if self._audit_status < 400 else "error",
                                                    "status": self._audit_status})
                    finally:
                        conn.close()
            finally:
                org_service.AUTH_CONTEXT.reset(context_token)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        self._audit_status = code
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
        if isinstance(exc, rewards_service.RewardError):
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
            mb = max(1, limit // (1024 * 1024))
            raise importer.ImportError_("file_too_large", f"文件超过 {mb} MB 上限", 413)
        return self.rfile.read(length) if length else b""

    def _principal(self):
        principal = principal_from_token(_bearer(self.headers))
        self._audit_principal = principal
        return principal

    def _admin(self):
        return _admin_principal(_bearer(self.headers))

    def _require_user(self):
        principal = self._principal()
        if not principal:
            raise authz.AuthzError(401, "请先登录")
        return principal

    def _require_admin(self):
        token = _bearer(self.headers)
        info = admin_session_info(token)
        if info is not None:
            principal = {"kind": "platform", "user_id": None, "role": authz.PLATFORM_ADMIN, "org_id": None,
                         "org": None, "admission_status": None, "demo": info["demo"]}
            self._audit_principal = principal
            return principal
        principal = self._principal()
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
                                        "demoLoginEnabled": _demo_login_enabled(),
                                        "onboarding": onboarding.public_config()})
            if path == "/api/me":
                principal = self._require_user()
                authz.require(principal, "profile", "read_self", owner_user_id=principal["user_id"])
                return self._send(*do_me(principal["owner_ref"], principal.get("demo", False)))
            if path == "/api/me/tokens":
                principal = self._require_user()
                authz.require(principal, "profile", "read_self", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    return self._send(200, pat_service.list_tokens(conn, principal))
                finally:
                    conn.close()
            if path == "/api/onboarding":
                principal = self._require_user()
                authz.require(principal, "onboarding", "read", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    return self._send(200, {"config": onboarding.public_config(),
                                            "progress": onboarding.load(conn, principal["user_id"])})
                finally:
                    conn.close()
            if path == "/api/rewards/summary":
                principal = self._require_user()
                org_id = _scope_org(principal, query.get("orgId", [None])[0])
                authz.require(principal, "rewards", "read", org_id=org_id)
                conn = db()
                try:
                    _, member_quota = _org_quota(conn, org_id)
                    return self._send(200, rewards_service.org_summary(conn, org_id, member_quota))
                finally:
                    conn.close()
            if path == "/api/works":
                principal = self._require_user()
                org_id = _scope_org(principal, query.get("orgId", [None])[0])
                authz.require(principal, "works", "read_gallery", org_id=org_id)
                conn = db()
                try:
                    return self._send(200, {"items": works_service.gallery(conn, org_id, viewer_user_id=principal.get("user_id")), "nextCursor": None})
                finally:
                    conn.close()
            match = re.match(r"^/api/works/([^/]+)/download$", path)
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
                    art = works_service.get_artifact(conn, row["artifact_ref"])
                    if not art or not art["storage_path"] or not os.path.isfile(art["storage_path"]):
                        return self._send(404, {"error": "该作品暂无可下载安装包"})
                    real_pkg = os.path.realpath(art["storage_path"])
                    pkg_root = os.path.realpath(os.path.join(DATA_DIR, "packages"))
                    if not real_pkg.startswith(pkg_root + os.sep):
                        return self._send(403, {"error": "无权访问该资源"})
                    with open(real_pkg, "rb") as fh:
                        payload = fh.read()
                    fname = art["file_name"] or os.path.basename(real_pkg)
                    ascii_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", fname) or "package.zip"
                    quoted = urllib.parse.quote(fname)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; filename*=UTF-8''{quoted}')
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                finally:
                    conn.close()
            match = re.match(r"^/api/works/([^/]+)/comments$", path)
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
                    return self._send(200, {"items": works_service.list_comments(conn, row["work_id"])})
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
                    card = works_service._card(row)
                    works_service.attach_rewards(conn, [card])
                    return self._send(200, card)
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
            match = re.match(r"^/api/orgs/([^/]+)/rewards/preview$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "rewards", "settle", org_id=org_id)
                custom_slots = {}
                for tier in config.REWARD_TIERS:
                    raw_slot = query.get(tier["code"], [None])[0]
                    if raw_slot is not None and str(raw_slot).strip() != "":
                        custom_slots[tier["code"]] = raw_slot
                excluded = [x for x in query.get("exclude", []) if str(x).strip()]
                conn = db()
                try:
                    _, member_quota = _org_quota(conn, org_id)
                    return self._send(200, rewards_service.preview_settlement(
                        conn, org_id, member_quota, custom_slots=custom_slots or None,
                        excluded_work_ids=excluded or None))
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/rewards$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "rewards", "read", org_id=org_id)
                conn = db()
                try:
                    _, member_quota = _org_quota(conn, org_id)
                    return self._send(200, rewards_service.org_summary(conn, org_id, member_quota))
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
                    return self._send(200, {"items": works_service.attach_rewards(conn, [works_service._card(row) for row in rows])})
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
                org_filter = query.get("orgId", [None])[0] if principal["role"] == authz.PLATFORM_ADMIN else principal.get("org_id")
                conn = db()
                try:
                    active_refs = _active_owner_refs(conn, org_id=org_filter)
                finally:
                    conn.close()
                relay.retry_due(RELAY_DB, org_id=org_filter, limit_pending=1, quota_lookup=_quota_for_owner,
                                allow_delivery_for=None, owner_refs=active_refs)
                relay.sync_provider_quotas(RELAY_DB)
                accounts = relay.list_accounts(RELAY_DB, q, org_filter)
                conn = db()
                try:
                    accounts = _enrich_account_quotas(conn, accounts)
                finally:
                    conn.close()
                return self._send(200, {"accounts": accounts})
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
                        active_refs = _active_owner_refs(conn, org_id=org_id)
                        relay.retry_due(RELAY_DB, org_id=org_id, limit_pending=1, quota_lookup=_quota_for_owner,
                                        allow_delivery_for=None, owner_refs=active_refs)
                        relay.sync_provider_quotas(RELAY_DB)
                        tree = org_service.tree(conn, org_id)
                        items = {item["userId"]: item for item in _merge_key_hints(
                            conn, org_service.members(conn, org_id), org_id)}
                        for child in tree.get("children", []):
                            rich = items.get(child["id"])
                            if rich:
                                child["keyHint"] = rich.get("keyHint")
                                child["keyStatus"] = rich.get("keyStatus")
                                child["remainingQuota"] = rich.get("remainingQuota")
                                child["quota"] = rich.get("quota")
                        return self._send(200, tree)
                    finally:
                        conn.close()
                if action in ("summary", "trends", "dashboard"):
                    authz.require(principal, "usage", "read_org", org_id=org_id)
                    days = int(query.get("days", [90])[0])
                    if action == "summary":
                        conn = db()
                        try:
                            provider_quota = (relay.quota_overview(RELAY_DB, org_id)
                                              if relay.sync_provider_quotas(RELAY_DB) else None)
                            return self._send(200, analytics.summary(conn, org_id, provider_quota=provider_quota))
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
                    active_refs = _active_owner_refs(conn, org_id=org_id)
                    relay.retry_due(RELAY_DB, org_id=org_id, limit_pending=1, quota_lookup=_quota_for_owner,
                                    allow_delivery_for=None, owner_refs=active_refs)
                    relay.sync_provider_quotas(RELAY_DB)
                    item = org_service.member(conn, org_id, match.group(2))
                    if not item:
                        return self._send(403, {"error": "无权访问该资源"})
                    _merge_key_hints(conn, [item], org_id)
                    return self._send(200, item)
                finally:
                    conn.close()
            if path.startswith("/media/"):
                fname = path[len("/media/"):]
                if not re.match(r"^(img_[a-f0-9]{20}|demo_[a-z0-9_]{3,40})\.(png|jpg|gif|webp)$", fname):
                    return self._send(404, {"error": "未找到图片"})
                media_root = os.path.realpath(os.path.join(DATA_DIR, "media"))
                target_file = os.path.realpath(os.path.join(media_root, fname))
                if not (target_file.startswith(media_root + os.sep) and os.path.isfile(target_file)):
                    return self._send(404, {"error": "未找到图片"})
                ctype, _ = mimetypes.guess_type(target_file)
                with open(target_file, "rb") as fh:
                    raw_bytes = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(raw_bytes)))
                self.end_headers()
                self.wfile.write(raw_bytes)
                return
            if path.startswith("/hosted/"):
                parts = path[len("/hosted/"):].split("/", 1)
                art_id = parts[0]
                rel = parts[1] if len(parts) > 1 and parts[1] else "index.html"
                if not re.match(r"^[a-zA-Z0-9_-]{4,64}$", art_id):
                    return self._send(404, {"error": "未找到托管站点"})
                site_root = os.path.realpath(os.path.join(DATA_DIR, "hosted", art_id))
                target_file = os.path.realpath(os.path.join(site_root, urllib.parse.unquote(rel)))
                if os.path.isdir(target_file):
                    target_file = os.path.join(target_file, "index.html")
                if not (target_file.startswith(site_root + os.sep) and os.path.isfile(target_file)):
                    return self._send(404, {"error": "未找到托管页面"})
                ctype, _ = mimetypes.guess_type(target_file)
                ctype = ctype or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                    ctype = ctype + "; charset=utf-8"
                with open(target_file, "rb") as fh:
                    raw_bytes = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups allow-modals")
                self.send_header("Content-Length", str(len(raw_bytes)))
                self.end_headers()
                self.wfile.write(raw_bytes)
                return
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
            if path == "/gallery":
                self.path = "/gallery.html"
            return super().do_GET()
        except (authz.AuthzError, importer.ImportError_, onboarding.OnboardingError, works_service.WorkError,
                rewards_service.RewardError, ValueError) as exc:
            return self._error(exc)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/me/tokens/([^/]+)", path)
            if not match:
                return self._send(404, {"error": "未知接口"})
            principal = self._require_user()
            authz.require(principal, "profile", "read_self", owner_user_id=principal["user_id"])
            conn = db()
            try:
                return self._send(200, pat_service.revoke(conn, principal, match.group(1)))
            finally:
                conn.close()
        except (authz.AuthzError, ValueError) as exc:
            return self._error(exc)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        data = self._body()
        try:
            work_match = re.match(r"^/api/works/([^/]+)$", parsed.path)
            if work_match:
                principal = self._require_user()
                authz.require(principal, "works", "submit", owner_user_id=principal["user_id"])
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = works_service.update(conn, work_match.group(1), principal["user_id"], data)
                    org_service.audit(conn, principal["user_id"], principal["role"], row["org_id"],
                                      "work.updated", "work", row["work_id"])
                    conn.execute("COMMIT")
                    return self._send(200, works_service._card(row))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/members/([^/]+)$", parsed.path)
            if not match:
                return self._send(404, {"error": "未知接口"})
            principal = self._require_admin()
            org_id = self._org_allowed(principal, match.group(1))
            self._require_org(org_id)
            authz.require(principal, "members", "update_status", org_id=org_id)
            if data.get("role") is not None:
                authz.require(principal, "members", "update_role", org_id=org_id)
            revoke_key_ids = []
            with LOCK:
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    item, revoke_keys = org_service.update_member(conn, org_id, match.group(2), data,
                                                                  principal.get("user_id"), principal["role"])
                    if not item:
                        conn.execute("ROLLBACK")
                        return self._send(403, {"error": "无权访问该资源"})
                    if revoke_keys:
                        refs, revoke_key_ids = _prepare_user_revocations(conn, match.group(2), org_id)
                        if data.get("admissionStatus") == "pending_review":
                            reason = "admission_pending_review"
                        else:
                            reason = "admission_disabled"
                        org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                          "member.key_revoked", "member", match.group(2),
                                          metadata={"reason": reason, "keys": len(refs)})
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            if revoke_key_ids:
                relay.finish_revocations(revoke_key_ids, RELAY_DB)
            return self._send(200, item)
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
            if path == "/api/me/tokens":
                principal = self._require_user()
                authz.require(principal, "profile", "read_self", owner_user_id=principal["user_id"])
                data = self._body()
                conn = db()
                try:
                    return self._send(*pat_service.create(conn, principal, data))
                finally:
                    conn.close()
            if path == "/api/orgs":
                principal = self._require_admin()
                authz.require(principal, "organizations", "create")
                data = self._body()
                name = str(data.get("name") or "").strip()
                slug = str(data.get("slug") or "").strip().lower()
                if not name or len(name) > 80 or not re.match(r"^[a-z0-9-]{2,60}$", slug):
                    return self._send(400, {"error": "组织名称必填，slug 只能用小写字母、数字和连字符"})
                quota_raw = data.get("quotaLimit")
                quota_limit = None
                if quota_raw is not None and str(quota_raw).strip() != "":
                    if isinstance(quota_raw, bool):
                        return self._send(400, {"error": "总额度上限必须是正整数"})
                    try:
                        quota_limit = int(quota_raw)
                    except (TypeError, ValueError):
                        return self._send(400, {"error": "总额度上限必须是正整数"})
                    if quota_limit <= 0 or quota_limit > 10 ** 12:
                        return self._send(400, {"error": "总额度上限必须在 1 到 1,000,000,000,000 之间"})
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT 1 FROM organizations WHERE slug = ?", (slug,)).fetchone():
                        conn.execute("ROLLBACK")
                        return self._send(409, {"error": "该组织标识（slug）已存在"})
                    org_id = migrations.ensure_org(conn, slug, name, data.get("adminLabel") or "教师",
                                                   data.get("memberLabel") or "学生", quota_limit=quota_limit)
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "organization.created", "organization", org_id,
                                      metadata={"name": name, "quotaLimit": quota_limit})
                    conn.execute("COMMIT")
                    return self._send(201, org_service.org(conn, org_id))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/rewards/settle$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "rewards", "settle", org_id=org_id)
                data = self._body()
                period_label = data.get("periodLabel")
                custom_slots = data.get("slots") if isinstance(data.get("slots"), dict) else None
                excluded = data.get("excludedWorkIds") if isinstance(data.get("excludedWorkIds"), list) else None
                rid = str(data.get("requestId") or ("settle-" + secrets.token_hex(8)))[:80]
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    _, member_quota = _org_quota(conn, org_id)
                    result = rewards_service.settle(
                        conn, org_id, member_quota, period_label,
                        principal.get("user_id"), principal["role"], rid,
                        custom_slots=custom_slots, excluded_work_ids=excluded,
                    )
                    conn.execute("COMMIT")
                    return self._send(200 if result.get("duplicate") else 201, result)
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/orgs/([^/]+)/members$", path)
            if match:
                principal = self._require_admin()
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "import", org_id=org_id)
                data = self._body()
                name = str(data.get("name") or "").strip()
                role_raw = str(data.get("role") or "org_member").strip()
                role = {"学生": authz.ORG_MEMBER, "教师": authz.ORG_ADMIN, "老师": authz.ORG_ADMIN}.get(role_raw, role_raw)
                status = str(data.get("admissionStatus") or "active").strip()
                student_id = str(data.get("studentId") or "").strip()
                email = normalize_identity("email", str(data.get("email") or "").strip())
                if not name or len(name) > 80:
                    return self._send(400, {"error": "姓名必填且不超过 80 字符"})
                if role not in (authz.ORG_MEMBER, authz.ORG_ADMIN):
                    return self._send(400, {"error": "角色只能是学生或教师"})
                if status not in ("active", "disabled", "pending_review"):
                    return self._send(400, {"error": "准入状态无效"})
                if not student_id and not email:
                    return self._send(400, {"error": "学号/工号与校园邮箱至少填写一项"})
                if student_id and (not student_id.isdigit() or len(student_id) > 40):
                    return self._send(400, {"error": "学号/工号只能是数字"})
                if email and ("@" not in email or email.startswith("@") or email.endswith("@") or len(email) > 80):
                    return self._send(400, {"error": "校园邮箱格式不正确"})
                external_ref = str(data.get("externalRef") or "").strip() or ("manual:" + (student_id or email))
                fields = {"external_ref": external_ref, "student_id": student_id, "email": email,
                          "name": name, "role": role, "status": status}
                now = time.time()
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    outcome, reason = _write_import_row(conn, org_id, fields, now)
                    if outcome != "accepted":
                        conn.execute("ROLLBACK")
                        msg = "该成员编号已存在" if reason == "already_present" else "该学号或邮箱已被占用"
                        return self._send(409, {"error": msg})
                    created_row = conn.execute("SELECT user_id FROM org_memberships WHERE org_id = ? AND external_ref = ?",
                                               (org_id, external_ref)).fetchone()
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "member.created", "membership", created_row["user_id"],
                                      metadata={"role": role, "status": status})
                    conn.execute("COMMIT")
                    item = org_service.member(conn, org_id, created_row["user_id"])
                    _merge_key_hints(conn, [item])
                    return self._send(201, item)
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
                revoke_key_ids = []
                with LOCK:
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
                            if principal.get("demo"):
                                conn.execute("ROLLBACK")
                                return self._send(403, {"error": "演示账号不能吊销真实密钥"})
                            results = []
                            for user_id in user_ids:
                                refs, key_ids = _prepare_user_revocations(conn, user_id, org_id)
                                revoke_key_ids.extend(key_ids)
                                results.append({"userId": user_id, "status": "revoked", "keys": len(refs)})
                            org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                              "member.bulk_revoke", "organization", org_id, rid,
                                              {"userCount": len(user_ids)})
                        else:
                            conn.execute("ROLLBACK")
                            return self._send(400, {"error": "动作只能是 grant 或 revoke"})
                        conn.execute("COMMIT")
                    except BaseException:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
                    finally:
                        conn.close()
                if revoke_key_ids:
                    relay.finish_revocations(revoke_key_ids, RELAY_DB)
                return self._send(200, {"results": results})
            match = re.match(r"^/api/orgs/([^/]+)/members/([^/]+)/revoke-key$", path)
            if match:
                principal = self._require_admin()
                if principal.get("demo"):
                    return self._send(403, {"error": "演示账号不能吊销真实密钥"})
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "members", "revoke_key", org_id=org_id)
                key_ids = []
                with LOCK:
                    conn = db()
                    try:
                        if org_service.users_outside_org(conn, org_id, [match.group(2)]):
                            return self._send(403, {"error": "无权访问该资源"})
                        refs, key_ids = _prepare_user_revocations(conn, match.group(2), org_id)
                        if not refs:
                            return self._send(403, {"error": "无权访问该资源"})
                        conn.execute("BEGIN IMMEDIATE")
                        org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                          "member.key_revoked", "member", match.group(2))
                        conn.execute("COMMIT")
                    except BaseException:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
                    finally:
                        conn.close()
                relay.finish_revocations(key_ids, RELAY_DB)
                return self._send(200, {"ok": True})
            match = re.match(r"^/api/orgs/([^/]+)/members/([^/]+)/reissue-key$", path)
            if match:
                principal = self._require_admin()
                if principal.get("demo"):
                    return self._send(403, {"error": "演示账号不能操作真实密钥"})
                org_id = self._org_allowed(principal, match.group(1))
                self._require_org(org_id)
                authz.require(principal, "accounts", "reissue_key", org_id=org_id)
                conn = db()
                try:
                    if org_service.users_outside_org(conn, org_id, [match.group(2)]):
                        return self._send(403, {"error": "无权访问该资源"})
                    admitted = conn.execute(
                        "SELECT 1 FROM org_memberships WHERE org_id = ? AND user_id = ? AND admission_status = 'active'",
                        (org_id, match.group(2))).fetchone()
                    if not admitted:
                        return self._send(409, {"error": "成员未处于 active 状态，不能重发密钥"})
                    refs = [row["owner_ref"] for row in conn.execute(
                        "SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ?",
                        (match.group(2), org_id)).fetchall()]
                    if not refs:
                        return self._send(403, {"error": "无权访问该资源"})
                    statuses = [relay.manual_reissue(RELAY_DB, ref, quota_lookup=_quota_for_owner) for ref in refs]
                    conn.execute("BEGIN IMMEDIATE")
                    org_service.audit(conn, principal.get("user_id"), principal["role"], org_id,
                                      "member.key_reissued", "member", match.group(2),
                                      metadata={"statuses": statuses})
                    conn.execute("COMMIT")
                    return self._send(200, {"statuses": statuses})
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
            if path == "/api/works/preview-image":
                principal = self._require_user()
                authz.require(principal, "works", "submit", owner_user_id=principal["user_id"])
                payload = self._raw(works_service.MAX_PREVIEW_IMAGE_BYTES)
                result = works_service.save_preview_image(DATA_DIR, payload)
                return self._send(201, result)
            if path == "/api/works/upload":
                principal = self._require_user()
                authz.require(principal, "works", "submit", owner_user_id=principal["user_id"])
                query = urllib.parse.parse_qs(parsed.query)
                sw = query.get("softwareType", ["web"])[0]
                filename = query.get("filename", ["upload.bin"])[0]
                payload = self._raw(works_service.MAX_ARTIFACT_BYTES)
                conn = db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = works_service.save_artifact(conn, DATA_DIR, principal["user_id"], principal["org_id"], sw, filename, payload)
                    conn.execute("COMMIT")
                    return self._send(201, result)
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
            match = re.match(r"^/api/works/([^/]+)/upvote$", path)
            if match:
                principal = self._require_user()
                conn = db()
                try:
                    row = works_service.get(conn, match.group(1))
                    if not row:
                        return self._send(404, {"error": "未找到作品"})
                    authz.require(principal, "works", "read_gallery", org_id=row["org_id"])
                    conn.execute("BEGIN IMMEDIATE")
                    result = works_service.toggle_upvote(conn, row["work_id"], row["org_id"], principal["user_id"])
                    conn.execute("COMMIT")
                    return self._send(200, result)
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            match = re.match(r"^/api/works/([^/]+)/comments$", path)
            if match:
                principal = self._require_user()
                data = self._body()
                conn = db()
                try:
                    row = works_service.get(conn, match.group(1))
                    if not row:
                        return self._send(404, {"error": "未找到作品"})
                    authz.require(principal, "works", "read_gallery", org_id=row["org_id"])
                    author_name = (principal.get("membership") or {}).get("name") if isinstance(principal.get("membership"), dict) else (principal["membership"]["name"] if principal.get("membership") else None)
                    author_name = author_name or principal.get("display_name") or "成员"
                    conn.execute("BEGIN IMMEDIATE")
                    result = works_service.add_comment(conn, row["work_id"], row["org_id"], principal["user_id"],
                                                       author_name, principal["role"], data.get("body"))
                    conn.execute("COMMIT")
                    return self._send(201, result)
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
            identity = normalize_identity(mode, data.get("identity"))
            password = data.get("password")
            target_org = data.get("orgId")
            if path == "/api/demo/login":
                return self._send(*do_demo_login(data.get("role")))
            if path == "/api/register":
                return self._send(*do_register(mode, identity, password, rid, target_org))
            if path == "/api/login":
                return self._send(*do_login(mode, identity, password, rid, target_org))
            if path in ("/api/key/rotate", "/api/consume", "/api/logout"):
                principal = self._require_user()
                if path == "/api/key/rotate":
                    if principal.get("demo"):
                        return self._send(403, {"error": "演示账号不发放真实成员 token"})
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
                if principal.get("demo"):
                    return self._send(403, {"error": "演示账号不能修改真实密钥"})
                owner_ref = str(data.get("owner_ref") or "").strip()
                if not owner_ref:
                    return self._send(400, {"error": "缺少 owner_ref"})
                # 与发钥入口共用 LOCK：管理端吊销不会和在途发钥互相穿插，吊销结果不会被补发覆盖。
                with LOCK:
                    conn = db()
                    try:
                        row = conn.execute("SELECT org_id FROM students WHERE owner_ref = ?",
                                           (owner_ref,)).fetchone()
                        if not row:
                            return self._send(403, {"error": "无权访问该资源"})
                        authz.require(principal, "accounts", "revoke_key", org_id=row["org_id"])
                        key_ids = relay.prepare_revoke(RELAY_DB, owner_ref)
                        conn.execute("BEGIN IMMEDIATE")
                        org_service.audit(conn, principal.get("user_id"), principal["role"], row["org_id"],
                                          "admin.key_revoked", "account", owner_ref)
                        conn.execute("COMMIT")
                    finally:
                        conn.close()
                relay.finish_revocations(key_ids, RELAY_DB)
                return self._send(200, {"ok": True})
            if path == "/api/reset" and os.environ.get("DEMO_ALLOW_RESET") == "1":
                return self._send(*do_reset())
            return self._send(404, {"error": "未知接口"})
        except (authz.AuthzError, importer.ImportError_, onboarding.OnboardingError, works_service.WorkError,
                rewards_service.RewardError, ValueError) as exc:
            return self._error(exc)


def do_reset():
    with LOCK:
        conn = db()
        try:
            for table in ("work_rewards", "reward_settlements", "onboarding_progress", "ledger", "sessions",
                          "login_failures", "admin_sessions", "students"):
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
