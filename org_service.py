"""组织、成员、审计与批量操作。权限判断在 server 层按 authz 矩阵完成。"""
import json
import time


def audit(conn, actor_user_id, actor_role, org_id, action, resource_type, resource_id, request_id=None, metadata=None):
    conn.execute(
        "INSERT INTO audit_events (actor_user_id, actor_role, org_id, action, resource_type, resource_id, "
        "request_id, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (actor_user_id, actor_role, org_id, action, resource_type, resource_id, request_id,
         json.dumps(metadata or {}, ensure_ascii=False), time.time()))


def list_audit(conn, org_id=None, limit=100):
    if org_id:
        rows = conn.execute("SELECT * FROM audit_events WHERE org_id = ? ORDER BY id DESC LIMIT ?",
                            (org_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            metadata = {}
        result.append({"id": row["id"], "actorUserId": row["actor_user_id"], "actorRole": row["actor_role"],
                       "orgId": row["org_id"], "action": row["action"], "resourceType": row["resource_type"],
                       "resourceId": row["resource_id"], "requestId": row["request_id"],
                       "metadata": metadata, "createdAt": row["created_at"]})
    return result


def organizations(conn):
    rows = conn.execute(
        "SELECT o.*, (SELECT COUNT(*) FROM org_memberships m WHERE m.org_id = o.id AND m.admission_status = 'active') AS member_count "
        "FROM organizations o ORDER BY o.created_at, o.name").fetchall()
    return [{"id": row["id"], "slug": row["slug"], "name": row["name"], "status": row["status"],
             "adminLabel": row["admin_label"], "memberLabel": row["member_label"],
             "memberCount": row["member_count"]} for row in rows]


def org(conn, org_id):
    row = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "slug": row["slug"], "name": row["name"], "status": row["status"],
            "adminLabel": row["admin_label"], "memberLabel": row["member_label"]}


def members(conn, org_id, query=None):
    sql = (
        "SELECT m.user_id, m.role, m.admission_status, m.external_ref, m.student_id, m.email, m.name, "
        "COALESCE(m.name, u.display_name, '') AS display_name, u.disabled_at, "
        "COALESCE(SUM(s.balance), 0) AS balance, COALESCE(SUM(s.spent), 0) AS spent "
        "FROM org_memberships m JOIN users u ON u.id = m.user_id "
        "LEFT JOIN students s ON s.user_id = m.user_id AND s.org_id = m.org_id "
        "WHERE m.org_id = ? ")
    params = [org_id]
    if query:
        sql += "AND (m.name LIKE ? OR m.student_id LIKE ? OR m.email LIKE ?) "
        like = "%" + query + "%"
        params.extend([like, like, like])
    sql += "GROUP BY m.user_id ORDER BY m.role = 'org_admin' DESC, display_name, m.user_id"
    rows = conn.execute(sql, tuple(params)).fetchall()
    result = []
    for row in rows:
        owner_refs = [r["owner_ref"] for r in conn.execute(
            "SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ? ORDER BY created_at",
            (row["user_id"], org_id)).fetchall()]
        result.append({
            "userId": row["user_id"], "role": row["role"], "admissionStatus": row["admission_status"],
            "externalRef": row["external_ref"], "studentId": row["student_id"] or "",
            "email": row["email"] or "", "name": row["display_name"],
            "balance": int(row["balance"] or 0), "spent": int(row["spent"] or 0),
            "ownerRefs": owner_refs, "keyStatus": "none", "keyHint": None,
        })
    return result


def member(conn, org_id, user_id):
    for item in members(conn, org_id):
        if item["userId"] == user_id:
            return item
    return None


def users_outside_org(conn, org_id, user_ids):
    """返回不属于该组织的用户编号，用于批量操作前一次性拒绝跨组织请求。"""
    unique = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
    if not unique:
        return []
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute("SELECT user_id FROM org_memberships WHERE org_id = ? AND user_id IN (%s)" % placeholders,
                        tuple([org_id] + unique)).fetchall()
    found = {row["user_id"] for row in rows}
    return [user_id for user_id in unique if user_id not in found]


def tree(conn, org_id):
    current = org(conn, org_id)
    if not current:
        return None
    items = members(conn, org_id)
    return {
        "id": current["id"], "name": current["name"], "kind": "organization",
        "adminLabel": current["adminLabel"], "memberLabel": current["memberLabel"],
        "count": len(items),
        "children": [{"id": item["userId"], "name": item["name"], "kind": "member", "role": item["role"],
                      "studentId": item["studentId"], "balance": item["balance"], "spent": item["spent"],
                      "keyHint": item["keyHint"], "admissionStatus": item["admissionStatus"]} for item in items],
    }


def update_member(conn, org_id, user_id, changes, actor_id, actor_role):
    """更新准入状态或角色。返回 (成员视图, 是否需要吊销该用户密钥)。"""
    row = conn.execute("SELECT * FROM org_memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id)).fetchone()
    if not row:
        return None, False
    status = changes.get("admissionStatus")
    role = changes.get("role")
    if status is not None and status not in ("active", "disabled", "pending_review"):
        raise ValueError("准入状态只能是 active、disabled 或 pending_review")
    if role is not None and role not in ("org_admin", "org_member"):
        raise ValueError("组织内角色只能是 org_admin 或 org_member")
    if status == "disabled" and actor_id and actor_id == user_id:
        raise ValueError("不能停用自己的账号，请让其他管理员操作")
    revoke_keys = False
    if status is not None:
        conn.execute("UPDATE org_memberships SET admission_status = ? WHERE org_id = ? AND user_id = ?",
                     (status, org_id, user_id))
        audit(conn, actor_id, actor_role, org_id, "member.status_changed", "membership", user_id,
              metadata={"from": row["admission_status"], "to": status})
        if status == "disabled":
            # 停用即失效：只清该用户在本组织的会话，其他组织的归属不受影响。
            conn.execute("DELETE FROM sessions WHERE org_id = ? AND (user_id = ? OR owner_ref IN "
                         "(SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ?))",
                         (org_id, user_id, user_id, org_id))
            revoke_keys = True
    if role is not None:
        conn.execute("UPDATE org_memberships SET role = ? WHERE org_id = ? AND user_id = ?", (role, org_id, user_id))
        audit(conn, actor_id, actor_role, org_id, "member.role_changed", "membership", user_id,
              metadata={"from": row["role"], "to": role})
    return member(conn, org_id, user_id), revoke_keys


def bulk_grant(conn, org_id, user_ids, amount, request_id, actor_id, actor_role):
    results = []
    for user_id in user_ids:
        membership = conn.execute("SELECT 1 FROM org_memberships WHERE org_id = ? AND user_id = ?",
                                  (org_id, user_id)).fetchone()
        if not membership:
            results.append({"userId": user_id, "status": "forbidden"})
            continue
        accounts = conn.execute("SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ?",
                                (user_id, org_id)).fetchall()
        if not accounts:
            results.append({"userId": user_id, "status": "skipped", "reason": "no_account"})
            continue
        now = time.time()
        granted = 0
        for account in accounts:
            rid = "bulk-grant:%s:%s" % (request_id, account["owner_ref"])
            duplicate = conn.execute("SELECT 1 FROM ledger WHERE owner_ref = ? AND request_id = ?",
                                     (account["owner_ref"], rid)).fetchone()
            if duplicate:
                continue
            conn.execute("UPDATE students SET balance = balance + ? WHERE owner_ref = ?", (amount, account["owner_ref"]))
            conn.execute("INSERT INTO ledger (owner_ref, user_id, org_id, title, amount, kind, request_id, created_at) "
                         "VALUES (?, ?, ?, '平台批量赠送', ?, 'grant', ?, ?)",
                         (account["owner_ref"], user_id, org_id, amount, rid, now))
            granted += amount
        results.append({"userId": user_id, "status": "granted" if granted else "skipped", "amount": granted})
    audit(conn, actor_id, actor_role, org_id, "member.bulk_grant", "organization", org_id, request_id,
          {"userCount": len(user_ids), "amount": amount})
    return results
