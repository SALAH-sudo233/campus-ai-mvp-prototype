"""组织用量汇总。额度按 organizations.quota_limit 分摊，students/ledger 是平台账面。"""
import datetime
import time

import config

MAX_TREND_DAYS = 90


def _day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _quota_meta(conn, org_id):
    total = None
    try:
        row = conn.execute("SELECT quota_limit FROM organizations WHERE id = ?", (org_id,)).fetchone()
        total = int(row["quota_limit"]) if row and row["quota_limit"] is not None else None
    except (IndexError, KeyError, TypeError, ValueError):
        total = None
    roster = conn.execute("SELECT COUNT(*) AS n FROM org_memberships WHERE org_id = ?", (org_id,)).fetchone()["n"]
    roster = max(int(roster or 0), 1)
    member_quota = total // roster if total else config.DEFAULT_MEMBER_QUOTA
    return total, member_quota


def summary(conn, org_id, now=None, provider_quota=None):
    now = now or time.time()
    total_quota, member_quota = _quota_meta(conn, org_id)
    active = conn.execute("SELECT COUNT(*) AS n FROM org_memberships WHERE org_id = ? AND admission_status = 'active'",
                          (org_id,)).fetchone()["n"]
    balances = conn.execute(
        "SELECT COALESCE(SUM(x.balance), 0) AS balance, COALESCE(SUM(x.spent), 0) AS spent FROM ("
        "SELECT s.user_id, SUM(s.balance) AS balance, SUM(s.spent) AS spent FROM students s "
        "JOIN org_memberships m ON m.user_id = s.user_id AND m.org_id = ? AND m.admission_status = 'active' "
        "AND s.org_id = m.org_id "
        "GROUP BY s.user_id) x", (org_id,)).fetchone()
    active_members = conn.execute(
        "SELECT COUNT(DISTINCT l.user_id) AS n FROM ledger l "
        "JOIN org_memberships m ON m.user_id = l.user_id AND m.org_id = ? AND m.admission_status = 'active' "
        "WHERE l.org_id = m.org_id AND l.kind = 'use' AND l.created_at >= ?",
        (org_id, now - 30 * 86400)).fetchone()["n"]
    # New-API token 不设硬限额；额度分摊与消耗口径统一取平台账面。
    remaining = int(balances["balance"])
    consumed = int(balances["spent"])
    reward_agg = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt FROM work_rewards WHERE org_id = ?",
        (org_id,),
    ).fetchone()
    return {
        "memberCount": active,
        "activeMemberCount": active_members,
        "totalQuota": total_quota,
        "memberQuota": member_quota,
        "totalConsumed": consumed,
        "remainingQuota": remaining,
        "rewardTotal": int(reward_agg["total"] or 0),
        "rewardedWorkCount": int(reward_agg["cnt"] or 0),
        "quotaSource": "platform_ledger",
        "activeTokenCount": int((provider_quota or {}).get("activeTokens") or 0),
        "range": {"from": _day(now - 30 * 86400), "to": _day(now), "days": 30},
        "available": True,
    }


def trend(conn, org_id, days=90, now=None):
    now = now or time.time()
    requested = int(days or 90)
    limit = max(1, min(requested, MAX_TREND_DAYS))
    truncated = requested > MAX_TREND_DAYS
    start = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(days=limit - 1)
    rows = conn.execute(
        "SELECT date(created_at, 'unixepoch') AS day, SUM(-amount) AS value FROM ledger "
        "WHERE org_id = ? AND kind = 'use' AND created_at >= ? GROUP BY day",
        (org_id, start.timestamp())).fetchall()
    values = {row["day"]: int(row["value"] or 0) for row in rows}
    series = []
    for index in range(limit):
        day = (start + datetime.timedelta(days=index)).strftime("%Y-%m-%d")
        series.append({"date": day, "value": values.get(day, 0)})
    return {
        "series": series,
        "unit": "Token",
        "range": {"from": series[0]["date"], "to": series[-1]["date"], "days": limit},
        "truncated": truncated,
        "coverage": "observed" if rows else "empty",
        "available": True,
    }


def ranking(conn, org_id, limit=10, now=None):
    now = now or time.time()
    rows = conn.execute(
        "SELECT COALESCE(m.name, u.display_name, '未命名成员') AS name, m.student_id, "
        "COALESCE(SUM(-l.amount), 0) AS consumed FROM ledger l "
        "JOIN org_memberships m ON m.user_id = l.user_id AND m.org_id = ? "
        "LEFT JOIN users u ON u.id = l.user_id "
        "WHERE l.org_id = m.org_id AND l.kind = 'use' AND l.created_at >= ? GROUP BY l.user_id "
        "ORDER BY consumed DESC, name LIMIT ?", (org_id, now - 90 * 86400, limit)).fetchall()
    return [{"name": row["name"], "studentId": row["student_id"] or "", "consumed": int(row["consumed"])}
            for row in rows]


def activity(conn, org_id, now=None):
    now = now or time.time()
    rows = conn.execute(
        "SELECT m.user_id, COUNT(DISTINCT date(l.created_at, 'unixepoch')) AS active_days FROM org_memberships m "
        "LEFT JOIN ledger l ON l.user_id = m.user_id AND l.org_id = m.org_id "
        "AND l.kind = 'use' AND l.created_at >= ? "
        "WHERE m.org_id = ? AND m.admission_status = 'active' GROUP BY m.user_id",
        (now - 30 * 86400, org_id)).fetchall()
    buckets = [{"label": "30 天未使用", "count": 0, "tone": "quiet"},
               {"label": "1–2 天", "count": 0, "tone": "low"},
               {"label": "3–7 天", "count": 0, "tone": "mid"},
               {"label": "8 天以上", "count": 0, "tone": "high"}]
    for row in rows:
        days = int(row["active_days"] or 0)
        index = 0 if days == 0 else 1 if days <= 2 else 2 if days <= 7 else 3
        buckets[index]["count"] += 1
    return {"buckets": buckets, "range": {"days": 30}}


def heatmap(conn, org_id, now=None):
    data = trend(conn, org_id, MAX_TREND_DAYS, now)
    return {"cells": [{"date": item["date"], "value": item["value"]} for item in data["series"]],
            "range": data["range"], "unit": "Token"}


def dashboard(conn, org_id, days=90, now=None, provider_quota=None):
    now = now or time.time()
    return {
        "summary": summary(conn, org_id, now, provider_quota),
        "trend": trend(conn, org_id, days, now),
        "heatmap": heatmap(conn, org_id, now),
        "activity": activity(conn, org_id, now),
        "ranking": ranking(conn, org_id, 10, now),
    }
