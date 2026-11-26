"""SQLite 版本化迁移。每个版本一个事务，重复执行只补未完成的版本。"""
import hashlib
import os
import secrets
import sqlite3
import time

import authz
import config
import roster
from identity import normalize_identity, owner_ref_of


class MigrationConflict(RuntimeError):
    """迁移遇到无法自动合并的数据冲突；保留原数据，交人工处理。"""


LEGACY_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS students (
      owner_ref TEXT PRIMARY KEY, mode TEXT NOT NULL, identity TEXT NOT NULL,
      pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL, balance INTEGER NOT NULL,
      spent INTEGER NOT NULL, created_at REAL NOT NULL, UNIQUE (mode, identity))""",
    """CREATE TABLE IF NOT EXISTS ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT, owner_ref TEXT NOT NULL, title TEXT NOT NULL,
      amount INTEGER NOT NULL, kind TEXT NOT NULL, request_id TEXT NOT NULL,
      created_at REAL NOT NULL, UNIQUE (owner_ref, request_id))""",
    """CREATE TABLE IF NOT EXISTS sessions (
      token_hash TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, expires_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS login_failures (
      owner_ref TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL,
      updated_at REAL NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS admin_sessions (
      token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)""",
]

CORE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS organizations (
      id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active', admin_label TEXT NOT NULL DEFAULT '教师',
      member_label TEXT NOT NULL DEFAULT '学生', quota_limit INTEGER, created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY, display_name TEXT, created_at REAL NOT NULL, disabled_at REAL)""",
    """CREATE TABLE IF NOT EXISTS user_identities (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id),
      kind TEXT NOT NULL, value_norm TEXT NOT NULL, value_display TEXT NOT NULL,
      source TEXT NOT NULL, verified_at REAL, UNIQUE (kind, value_norm))""",
    """CREATE TABLE IF NOT EXISTS org_memberships (
      org_id TEXT NOT NULL REFERENCES organizations(id), user_id TEXT NOT NULL REFERENCES users(id),
      role TEXT NOT NULL, admission_status TEXT NOT NULL, external_ref TEXT NOT NULL,
      student_id TEXT, email TEXT, name TEXT, joined_at REAL, left_at REAL,
      PRIMARY KEY (org_id, user_id), UNIQUE (org_id, external_ref))""",
    """CREATE TABLE IF NOT EXISTS platform_admins (
      user_id TEXT PRIMARY KEY REFERENCES users(id), created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS org_policies (
      org_id TEXT PRIMARY KEY REFERENCES organizations(id), trend_days INTEGER NOT NULL DEFAULT 90,
      updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS import_batches (
      id TEXT PRIMARY KEY, org_id TEXT NOT NULL, actor_user_id TEXT, file_sha256 TEXT NOT NULL,
      schema_version INTEGER NOT NULL, status TEXT NOT NULL, preview_digest TEXT NOT NULL,
      expires_at REAL NOT NULL, created_at REAL NOT NULL, committed_at REAL,
      total_rows INTEGER NOT NULL DEFAULT 0, accepted_count INTEGER NOT NULL DEFAULT 0,
      rejected_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0,
      conflict_count INTEGER NOT NULL DEFAULT 0, result_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS import_rows (
      id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES import_batches(id),
      row_no INTEGER NOT NULL, row_key TEXT, fields_json TEXT NOT NULL, result TEXT NOT NULL,
      reason_code TEXT, message TEXT, UNIQUE (batch_id, row_no))""",
    """CREATE TABLE IF NOT EXISTS audit_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id TEXT, actor_role TEXT, org_id TEXT,
      action TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT,
      request_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS onboarding_progress (
      user_id TEXT NOT NULL, flow_id TEXT NOT NULL, flow_version INTEGER NOT NULL,
      current_step_id TEXT, step_states_json TEXT NOT NULL DEFAULT '{}', draft_json TEXT NOT NULL DEFAULT '{}',
      auto_open_pending INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
      started_at REAL, finished_at REAL, exit_count INTEGER NOT NULL DEFAULT 0,
      last_exit_at REAL, updated_at REAL NOT NULL, PRIMARY KEY (user_id, flow_id))""",
    """CREATE TABLE IF NOT EXISTS works (
      work_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_user_id TEXT, title TEXT NOT NULL,
      summary TEXT NOT NULL, author_display_name TEXT NOT NULL, category TEXT NOT NULL,
      cover_path TEXT, source_kind TEXT NOT NULL, status TEXT NOT NULL, visibility TEXT NOT NULL,
      artifact_ref TEXT, open_target_json TEXT NOT NULL DEFAULT '{}', published_version TEXT,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, published_at REAL, withdrawn_at REAL,
      removed_by TEXT)""",
    """CREATE TABLE IF NOT EXISTS featured_works (
      org_id TEXT NOT NULL, work_id TEXT NOT NULL REFERENCES works(work_id),
      featured_by TEXT, created_at REAL NOT NULL, PRIMARY KEY (org_id, work_id))""",
]

LEGACY_COLUMNS = {
    "students": [("user_id", "TEXT"), ("org_id", "TEXT"), ("primary_identity_id", "INTEGER")],
    "ledger": [("user_id", "TEXT"), ("org_id", "TEXT")],
    "sessions": [("user_id", "TEXT"), ("org_id", "TEXT")],
    "admin_sessions": [("user_id", "TEXT"), ("role", "TEXT")],
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_identities_user ON user_identities(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_memberships_user ON org_memberships(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_memberships_org_status ON org_memberships(org_id, admission_status)",
    "CREATE INDEX IF NOT EXISTS idx_students_user ON students(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_students_org ON students(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_org_time ON ledger(org_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_user_time ON ledger(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_imports_org ON import_batches(org_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_org_time ON audit_events(org_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_works_org_status ON works(org_id, status, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_works_owner ON works(owner_user_id)",
]


def connect(path):
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _execute_all(conn, statements):
    for sql in statements:
        conn.execute(sql)


def _add_columns(conn):
    for table, columns in LEGACY_COLUMNS.items():
        existing = _columns(conn, table)
        for name, kind in columns:
            if name not in existing:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))


def _new_user(conn, display_name=None, prefix="u"):
    user_id = prefix + "_" + secrets.token_hex(12)
    conn.execute("INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
                 (user_id, display_name, time.time()))
    return user_id


def _identity_user(conn, kind, value):
    if not value:
        return None
    row = conn.execute("SELECT user_id FROM user_identities WHERE kind = ? AND value_norm = ?",
                       (kind, value)).fetchone()
    return row["user_id"] if row else None


def _add_identity(conn, user_id, kind, value, source="migration"):
    if not value:
        return
    existing = _identity_user(conn, kind, value)
    if existing:
        return existing
    conn.execute("INSERT INTO user_identities (user_id, kind, value_norm, value_display, source, verified_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (user_id, kind, value, value, source, time.time()))
    return user_id


def _ensure_org(conn):
    org = conn.execute("SELECT id FROM organizations WHERE slug = ?", (config.PILOT_ORG["slug"],)).fetchone()
    if org:
        return org["id"]
    now = time.time()
    conn.execute("INSERT INTO organizations (id, slug, name, status, admin_label, member_label, created_at) "
                 "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                 (config.PILOT_ORG["id"], config.PILOT_ORG["slug"], config.PILOT_ORG["name"],
                  config.PILOT_ORG["admin_label"], config.PILOT_ORG["member_label"], now))
    conn.execute("INSERT OR IGNORE INTO org_policies (org_id, trend_days, updated_at) VALUES (?, 90, ?)",
                 (config.PILOT_ORG["id"], now))
    return config.PILOT_ORG["id"]


def _seed_roster(conn, org_id):
    now = time.time()
    for student_id, name in sorted(roster.ROSTER.items()):
        user_id = _identity_user(conn, "student", student_id)
        if not user_id:
            user_id = _new_user(conn, name, prefix="u_roster")
            _add_identity(conn, user_id, "student", student_id, source="roster")
        conn.execute(
            "INSERT OR IGNORE INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
            "student_id, email, name, joined_at) VALUES (?, ?, 'org_member', 'active', ?, ?, NULL, ?, ?)",
            (org_id, user_id, "roster:" + student_id, student_id, name, now))


def _seed_works(conn, org_id, work_id_prefix=""):
    now = time.time()
    for item in config.SEED_WORKS:
        conn.execute(
            "INSERT OR IGNORE INTO works (work_id, org_id, owner_user_id, title, summary, author_display_name, "
            "category, cover_path, source_kind, status, visibility, artifact_ref, open_target_json, "
            "published_version, created_at, updated_at, published_at) "
            "VALUES (?, ?, NULL, ?, ?, '平台演示', ?, NULL, 'seed_demo', 'published', 'organization', NULL, ?, "
            "'demo-v1', ?, ?, ?)",
            (work_id_prefix + item["work_id"], org_id, item["title"], item["summary"], item["category"],
             '{"kind":"local_preview","preview_id":"%s"}' % item["preview_id"], now, now, now))


def _backfill_legacy(conn, org_id):
    now = time.time()
    for student in conn.execute("SELECT owner_ref, mode, identity, created_at FROM students").fetchall():
        mode = student["mode"]
        identity = student["identity"]
        sid = roster.student_id_of(mode, identity)
        roster_user = _identity_user(conn, "student", sid) if sid else None
        identity_user = _identity_user(conn, mode, identity)
        user_id = roster_user or identity_user
        if not user_id:
            user_id = _new_user(conn, roster.name_for(mode, identity), prefix="u_legacy")
        _add_identity(conn, user_id, mode, identity, source="legacy")
        if sid and sid in roster.ROSTER:
            _add_identity(conn, user_id, "student", sid, source="roster")
            conn.execute(
                "INSERT OR IGNORE INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
                "student_id, name, joined_at) VALUES (?, ?, 'org_member', 'active', ?, ?, ?, ?)",
                (org_id, user_id, "roster:" + sid, sid, roster.ROSTER.get(sid), now))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
                "student_id, email, name, joined_at) VALUES (?, ?, 'org_member', 'pending_review', ?, ?, ?, ?, ?)",
                (org_id, user_id, student["owner_ref"], sid, identity if mode == "email" else None,
                 roster.name_for(mode, identity), student["created_at"] or now))
        identity_row = conn.execute("SELECT id FROM user_identities WHERE kind = ? AND value_norm = ?",
                                    (mode, identity)).fetchone()
        conn.execute("UPDATE students SET user_id = ?, org_id = ?, primary_identity_id = ? WHERE owner_ref = ?",
                     (user_id, org_id, identity_row["id"] if identity_row else None, student["owner_ref"]))
    conn.execute("UPDATE ledger SET user_id = (SELECT user_id FROM students s WHERE s.owner_ref = ledger.owner_ref), "
                 "org_id = (SELECT org_id FROM students s WHERE s.owner_ref = ledger.owner_ref) "
                 "WHERE user_id IS NULL")
    conn.execute("UPDATE sessions SET user_id = (SELECT user_id FROM students s WHERE s.owner_ref = sessions.owner_ref), "
                 "org_id = (SELECT org_id FROM students s WHERE s.owner_ref = sessions.owner_ref) "
                 "WHERE user_id IS NULL")


def _migration_1(conn):
    _execute_all(conn, LEGACY_SCHEMA)


def _migration_2(conn):
    _execute_all(conn, CORE_SCHEMA)


def _migration_3(conn):
    _add_columns(conn)


def _migration_4(conn):
    org_id = _ensure_org(conn)
    _seed_roster(conn, org_id)
    _seed_works(conn, org_id)


def _migration_5(conn):
    org_id = _ensure_org(conn)
    _backfill_legacy(conn, org_id)


def _migration_6(conn):
    _execute_all(conn, INDEXES)


def _case_fold_collisions(conn):
    """统计邮箱归一后会撞车的组数；只返回计数，错误信息里不带原始身份值。"""
    identity_dups = 0
    seen_identities = set()
    for row in conn.execute("SELECT value_norm FROM user_identities WHERE kind = 'email'").fetchall():
        key = normalize_identity("email", row["value_norm"])
        if key in seen_identities:
            identity_dups += 1
        else:
            seen_identities.add(key)
    account_dups = 0
    seen_refs = set()
    for row in conn.execute("SELECT identity FROM students WHERE mode = 'email'").fetchall():
        ref = owner_ref_of("email", row["identity"])
        if ref in seen_refs:
            account_dups += 1
        else:
            seen_refs.add(ref)
    return identity_dups, account_dups


def _fold_email_case(conn):
    """把历史邮箱身份归一为小写，并同步重算 students.owner_ref 及其引用表。

    先整体检查撞车再写，任何一组归一后重复都中止迁移并抛 MigrationConflict，
    由 migrate() 回滚整个版本，不静默丢行。

    login_failures 是易失的风控计数：升级前可能已经有归一后 owner_ref 的行（有人用小写登录
    失败过），搬移会撞主键卡死升级，所以直接丢弃旧 owner_ref 的行，不做 rename。
    """
    identity_dups, account_dups = _case_fold_collisions(conn)
    if identity_dups or account_dups:
        raise MigrationConflict(
            "email-case-fold 迁移中止：邮箱身份归一后出现重复（身份记录 %d 条、账户 %d 条），"
            "请先人工合并大小写不同的重复账号后再重试。" % (identity_dups, account_dups))
    moves = []
    stage = "students"
    try:
        # 只有邮箱身份的归一结果会变，学号账户的 owner_ref 保持原样。
        for row in conn.execute("SELECT owner_ref, identity FROM students WHERE mode = 'email'").fetchall():
            new_ref = owner_ref_of("email", row["identity"])
            new_identity = normalize_identity("email", row["identity"])
            if new_ref != row["owner_ref"] or new_identity != row["identity"]:
                moves.append((row["owner_ref"], new_ref, new_identity))
        for old_ref, new_ref, new_identity in moves:
            stage = "students"
            conn.execute("UPDATE students SET owner_ref = ?, identity = ? WHERE owner_ref = ?",
                         (new_ref, new_identity, old_ref))
            for table in ("ledger", "sessions"):
                stage = table
                conn.execute("UPDATE %s SET owner_ref = ? WHERE owner_ref = ?" % table, (new_ref, old_ref))
            stage = "login_failures"
            conn.execute("DELETE FROM login_failures WHERE owner_ref = ?", (old_ref,))
        stage = "user_identities"
        for row in conn.execute("SELECT id, value_norm, value_display FROM user_identities "
                                "WHERE kind = 'email'").fetchall():
            value_norm = normalize_identity("email", row["value_norm"])
            value_display = normalize_identity("email", row["value_display"])
            if value_norm != row["value_norm"] or value_display != row["value_display"]:
                conn.execute("UPDATE user_identities SET value_norm = ?, value_display = ? WHERE id = ?",
                             (value_norm, value_display, row["id"]))
        stage = "org_memberships"
        for row in conn.execute("SELECT org_id, user_id, email FROM org_memberships "
                                "WHERE email IS NOT NULL").fetchall():
            email = normalize_identity("email", row["email"])
            if email != row["email"]:
                conn.execute("UPDATE org_memberships SET email = ? WHERE org_id = ? AND user_id = ?",
                             (email, row["org_id"], row["user_id"]))
    except sqlite3.IntegrityError as exc:
        raise MigrationConflict(
            "email-case-fold 迁移中止：表 %s 在邮箱身份归一后出现唯一键冲突（需搬移的账户 %d 条），"
            "请先人工合并重复账号后再重试。" % (stage, len(moves))) from exc


def _migration_7(conn):
    _fold_email_case(conn)


def _migration_9(conn):
    """演示会话标记与试点组织总额度；额度字段复用 organizations.quota_limit。"""
    for table in ("sessions", "admin_sessions"):
        if "demo" not in _columns(conn, table):
            conn.execute("ALTER TABLE %s ADD COLUMN demo INTEGER NOT NULL DEFAULT 0" % table)
    conn.execute("UPDATE organizations SET quota_limit = ? WHERE id = ? AND quota_limit IS NULL",
                 (config.PILOT_TOTAL_QUOTA, config.PILOT_ORG["id"]))


def _migration_10(conn):
    conn.execute("""CREATE TABLE pat_tokens (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE, token_hint TEXT NOT NULL, label TEXT NOT NULL,
        role_snapshot TEXT NOT NULL, org_snapshot TEXT, created_at REAL NOT NULL,
        created_by TEXT NOT NULL, expires_at REAL NOT NULL, last_used_at REAL,
        revoked_at REAL, revoked_by TEXT, request_id TEXT NOT NULL,
        UNIQUE(user_id, request_id))""")
    conn.execute("CREATE INDEX idx_pat_user ON pat_tokens(user_id, created_at)")


def _migration_11(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS reward_settlements (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        period_label TEXT NOT NULL,
        base_quota INTEGER NOT NULL,
        tiers_json TEXT NOT NULL,
        total_awarded INTEGER NOT NULL,
        winner_count INTEGER NOT NULL,
        settled_by TEXT,
        request_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(org_id, request_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reward_settlements_org "
                 "ON reward_settlements(org_id, created_at DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS work_rewards (
        id TEXT PRIMARY KEY,
        settlement_id TEXT NOT NULL REFERENCES reward_settlements(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        work_id TEXT NOT NULL UNIQUE REFERENCES works(work_id),
        owner_user_id TEXT NOT NULL REFERENCES users(id),
        owner_ref TEXT,
        rank INTEGER NOT NULL,
        tier_code TEXT NOT NULL,
        tier_label TEXT NOT NULL,
        multiplier INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        valid_upvotes INTEGER NOT NULL,
        created_at REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_rewards_org "
                 "ON work_rewards(org_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_rewards_owner "
                 "ON work_rewards(owner_user_id, created_at DESC)")


def _migration_12(conn):
    org_id = ensure_org(conn, "four-end-demo", "四端演示学院", "校内运营", "学生",
                        quota_limit=config.DEFAULT_MEMBER_QUOTA * 3)
    _seed_works(conn, org_id, work_id_prefix=org_id + ":")
    now = time.time()
    names = {authz.ORG_MEMBER: "演示学生", authz.ORG_OPERATOR: "演示校内运营",
             authz.COLLEGE_VIEWER: "演示学院观察员", authz.PLATFORM_ADMIN: "演示平台管理员"}
    for role, name in names.items():
        user_id = _new_user(conn, name, prefix="u_four_end_demo")
        _add_identity(conn, user_id, "demo", role, source="four-end-demo")
        if role == authz.PLATFORM_ADMIN:
            conn.execute("INSERT INTO platform_admins (user_id, created_at) VALUES (?, ?)", (user_id, now))
        else:
            conn.execute("INSERT INTO org_memberships (org_id, user_id, role, admission_status, external_ref, "
                         "name, joined_at) VALUES (?, ?, ?, 'active', ?, ?, ?)",
                         (org_id, user_id, role, "four-end-demo:" + role, name, now))
        conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, "
                     "created_at, user_id, org_id) VALUES (?, 'demo', ?, ?, ?, 0, 0, ?, ?, ?)",
                     (owner_ref_of("demo", role), role, secrets.token_hex(16), secrets.token_hex(32),
                      now, user_id, org_id if role != authz.PLATFORM_ADMIN else None))


MIGRATIONS = [
    (1, "legacy-schema", _migration_1),
    (2, "org-schema", _migration_2),
    (3, "legacy-columns", _migration_3),
    (4, "pilot-org-and-seeds", _migration_4),
    (5, "legacy-backfill", _migration_5),
    (6, "indexes", _migration_6),
    (7, "email-case-fold", _migration_7),
    (8, "org-quota-limit", lambda conn: (
        None if "quota_limit" in _columns(conn, "organizations")
        else conn.execute("ALTER TABLE organizations ADD COLUMN quota_limit INTEGER")
    )),
    (9, "demo-login-and-pilot-quota", _migration_9),
    (10, "personal-access-tokens", _migration_10),
    (11, "work-rewards", _migration_11),
    (12, "four-end-roles-and-identities", _migration_12),
]


def migrate(db_path):
    conn = connect(db_path)
    applied = []
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                     "name TEXT NOT NULL, applied_at REAL NOT NULL)")
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, name, fn in MIGRATIONS:
            if version in done:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 另一个进程可能已经在我们等待写锁时应用了同一版本。
                if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
                    conn.execute("COMMIT")
                    continue
                fn(conn)
                conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                             (version, name, time.time()))
                conn.execute("COMMIT")
                applied.append(version)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return applied
    finally:
        conn.close()


def ensure_org(conn, slug, name, admin_label="教师", member_label="学生", quota_limit=None):
    row = conn.execute("SELECT id FROM organizations WHERE slug = ?", (slug,)).fetchone()
    if row:
        return row["id"]
    org_id = "org_" + hashlib.sha256((slug + str(time.time())).encode("utf-8")).hexdigest()[:16]
    now = time.time()
    conn.execute("INSERT INTO organizations (id, slug, name, status, admin_label, member_label, quota_limit, created_at) "
                 "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
                 (org_id, slug, name, admin_label, member_label, quota_limit, now))
    conn.execute("INSERT OR IGNORE INTO org_policies (org_id, trend_days, updated_at) VALUES (?, 90, ?)",
                 (org_id, now))
    _seed_works(conn, org_id)
    return org_id


def backfill_relay(relay_db, main_db):
    """把旧中转站台账按 owner_ref 关联到新用户与组织，可重复执行。"""
    if not os.path.exists(relay_db):
        return
    main = connect(main_db)
    relay_conn = connect(relay_db)
    try:
        mappings = {r["owner_ref"]: (r["user_id"], r["org_id"])
                    for r in main.execute("SELECT owner_ref, user_id, org_id FROM students").fetchall()}
        for owner_ref, (user_id, org_id) in mappings.items():
            if not user_id:
                continue
            relay_conn.execute("UPDATE relay_accounts SET user_id = ?, org_id = ? WHERE owner_ref = ?",
                               (user_id, org_id, owner_ref))
            relay_conn.execute("UPDATE relay_keys SET user_id = ? WHERE owner_ref = ?", (user_id, owner_ref))
    finally:
        relay_conn.close()
        main.close()


def fold_relay_email_case(relay_db):
    """中转站台账独立在另一个库，按同一身份口径归一邮箱并同步 owner_ref。

    只依赖台账自身存的 mode/identity 重算，可重复执行；由 app_server.setup 在 relay.init
    之后调用。主库迁移已把 students.owner_ref 改成同一口径，这里不跟上的话旧账户的密钥
    会挂在不存在的 owner_ref 上，登录后看起来像没有密钥。
    """
    if not os.path.exists(relay_db):
        return 0
    conn = connect(relay_db)
    moved = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        accounts = conn.execute("SELECT owner_ref, mode, identity FROM relay_accounts").fetchall()
        targets = {}
        for row in accounts:
            targets.setdefault(owner_ref_of(row["mode"], row["identity"]), []).append(row["owner_ref"])
        conflicts = [ref for ref, refs in targets.items() if len(refs) > 1]
        if conflicts:
            raise MigrationConflict(
                "email-case-fold 中转站台账归一后出现重复（%d 组），请先人工合并大小写不同的重复账号后再重试。"
                % len(conflicts))
        try:
            for row in accounts:
                new_ref = owner_ref_of(row["mode"], row["identity"])
                new_identity = normalize_identity(row["mode"], row["identity"])
                if new_ref == row["owner_ref"] and new_identity == row["identity"]:
                    continue
                conn.execute("UPDATE relay_accounts SET owner_ref = ?, identity = ? WHERE owner_ref = ?",
                             (new_ref, new_identity, row["owner_ref"]))
                conn.execute("UPDATE relay_keys SET owner_ref = ? WHERE owner_ref = ?",
                             (new_ref, row["owner_ref"]))
                moved += 1
        except sqlite3.IntegrityError as exc:
            raise MigrationConflict(
                "email-case-fold 中转站台账归一后出现唯一键冲突（表 relay_accounts / relay_keys），"
                "请先人工合并重复账号后再重试。") from exc
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return moved


def status(db_path):
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()
