"""作品状态机与组织内展示。精选关系按组织单独存储。"""
import json
import time


class WorkError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _card(row):
    try:
        target = json.loads(row["open_target_json"] or "{}")
    except (TypeError, ValueError):
        target = {}
    return {
        "workId": row["work_id"],
        "title": row["title"],
        "summary": row["summary"],
        "authorDisplayName": row["author_display_name"],
        "category": row["category"],
        "coverPath": row["cover_path"],
        "sourceKind": row["source_kind"],
        "visibilityLabel": "演示广场" if row["source_kind"] == "seed_demo" else "本组织",
        "publishedVersion": row["published_version"],
        "updatedAt": row["updated_at"],
        "openTarget": target,
        "featured": bool(row["featured"]) if "featured" in row.keys() else False,
        "status": row["status"],
    }


def gallery(conn, org_id, limit=24):
    rows = conn.execute(
        "SELECT w.*, CASE WHEN f.work_id IS NULL THEN 0 ELSE 1 END AS featured FROM works w "
        "LEFT JOIN featured_works f ON f.work_id = w.work_id AND f.org_id = w.org_id "
        "WHERE w.org_id = ? AND w.status = 'published' "
        "ORDER BY (w.source_kind = 'seed_demo') DESC, w.published_at DESC, w.work_id LIMIT ?",
        (org_id, limit)).fetchall()
    return [_card(row) for row in rows]


def featured(conn, org_id, limit=12):
    rows = conn.execute(
        "SELECT w.*, 1 AS featured FROM featured_works f JOIN works w ON w.work_id = f.work_id "
        "WHERE f.org_id = ? AND w.org_id = ? AND w.status = 'published' "
        "ORDER BY f.created_at DESC LIMIT ?", (org_id, org_id, limit)).fetchall()
    return [_card(row) for row in rows]


def mine(conn, user_id):
    rows = conn.execute("SELECT * FROM works WHERE owner_user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()
    return [_card(row) for row in rows]


def get(conn, work_id):
    return conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()


def submit(conn, org_id, user_id, author_name, data):
    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()
    category = str(data.get("category") or "其他").strip()
    if not title or len(title) > 60:
        raise WorkError(400, "invalid_title", "作品名称必填且不超过 60 字符")
    if not summary or len(summary) > 160:
        raise WorkError(400, "invalid_summary", "作品简介必填且不超过 160 字符")
    if len(category) > 30:
        raise WorkError(400, "invalid_category", "分类不能超过 30 字符")
    now = time.time()
    work_id = "work_" + __import__("secrets").token_hex(10)
    conn.execute(
        "INSERT INTO works (work_id, org_id, owner_user_id, title, summary, author_display_name, category, "
        "cover_path, source_kind, status, visibility, artifact_ref, open_target_json, published_version, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'student_demo', 'pending_review', "
        "'organization', NULL, '{}', NULL, ?, ?)",
        (work_id, org_id, user_id, title, summary, author_name or "学生", category, now, now))
    return get(conn, work_id)


def withdraw(conn, work_id, user_id):
    row = get(conn, work_id)
    if not row or row["owner_user_id"] != user_id:
        raise WorkError(403, "forbidden", "无权访问该作品")
    if row["status"] not in ("pending_review", "published", "rejected"):
        raise WorkError(409, "invalid_state", "当前状态不能撤回")
    now = time.time()
    conn.execute("UPDATE works SET status = 'withdrawn', withdrawn_at = ?, updated_at = ? WHERE work_id = ?",
                 (now, now, work_id))
    conn.execute("DELETE FROM featured_works WHERE work_id = ?", (work_id,))
    return get(conn, work_id)


def review(conn, work_id, org_id, decision, actor_id):
    row = get(conn, work_id)
    if not row or row["org_id"] != org_id:
        raise WorkError(403, "forbidden", "无权访问该作品")
    if row["status"] != "pending_review":
        raise WorkError(409, "invalid_state", "只有待审核作品可以审核")
    if decision not in ("approve", "reject"):
        raise WorkError(400, "invalid_decision", "审核结果只能是 approve 或 reject")
    now = time.time()
    status = "published" if decision == "approve" else "rejected"
    conn.execute("UPDATE works SET status = ?, updated_at = ?, published_at = ? WHERE work_id = ?",
                 (status, now, now if status == "published" else None, work_id))
    return get(conn, work_id)


def set_featured(conn, work_id, org_id, actor_id, value):
    row = get(conn, work_id)
    if not row or row["org_id"] != org_id:
        raise WorkError(403, "forbidden", "无权访问该作品")
    if value and row["status"] != "published":
        raise WorkError(409, "invalid_state", "只有已发布作品可以精选")
    if value:
        conn.execute("INSERT OR IGNORE INTO featured_works (org_id, work_id, featured_by, created_at) "
                     "VALUES (?, ?, ?, ?)", (org_id, work_id, actor_id, time.time()))
    else:
        conn.execute("DELETE FROM featured_works WHERE org_id = ? AND work_id = ?", (org_id, work_id))
    return get(conn, work_id)


def unpublish(conn, work_id, org_id, removed_by):
    row = get(conn, work_id)
    if not row or (org_id and row["org_id"] != org_id):
        raise WorkError(403, "forbidden", "无权访问该作品")
    if row["status"] not in ("published", "pending_review"):
        raise WorkError(409, "invalid_state", "当前状态不能下架")
    now = time.time()
    conn.execute("UPDATE works SET status = 'removed', removed_by = ?, updated_at = ?, withdrawn_at = ? "
                 "WHERE work_id = ?", (removed_by, now, now, work_id))
    conn.execute("DELETE FROM featured_works WHERE work_id = ?", (work_id,))
    return get(conn, work_id)
