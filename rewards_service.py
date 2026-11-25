"""作品激励（平台额外 Token 奖励）：纯按有效点赞排序、三档（10×/5×/1×，默认 1/5/10 人）、平台统一结算。"""
import datetime
import json
import secrets
import time

import config
import org_service
from identity import owner_ref_of

MAX_SLOTS_TOTAL = 200
MAX_PERIOD_LABEL = 60


class RewardError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def tiers_config(base_quota, custom_slots=None):
    """返回三档奖励配置（按组织成员初始额度的 10× / 5× / 1×，默认 1 / 5 / 10 个名额）。"""
    base = max(1, int(base_quota or config.DEFAULT_MEMBER_QUOTA))
    slot_overrides = {}
    if isinstance(custom_slots, dict):
        for key, raw in custom_slots.items():
            if raw is None or str(raw).strip() == "":
                continue
            if isinstance(raw, bool):
                raise RewardError(400, "invalid_slots", "奖励名额必须是非负整数")
            try:
                val = int(raw)
            except (TypeError, ValueError) as exc:
                raise RewardError(400, "invalid_slots", "奖励名额必须是非负整数") from exc
            if val < 0 or val > MAX_SLOTS_TOTAL:
                raise RewardError(400, "invalid_slots", "单档奖励名额必须在 0 到 %d 之间" % MAX_SLOTS_TOTAL)
            slot_overrides[str(key)] = val

    tiers = []
    total_slots = 0
    for item in config.REWARD_TIERS:
        code = item["code"]
        slots = slot_overrides.get(code, int(item["slots"]))
        total_slots += slots
        mult = int(item["multiplier"])
        tiers.append({
            "code": code,
            "label": item["label"],
            "multiplier": mult,
            "slots": slots,
            "amount": base * mult,
        })
    if total_slots <= 0 or total_slots > MAX_SLOTS_TOTAL:
        raise RewardError(400, "invalid_slots", "三档总获奖名额必须在 1 到 %d 之间" % MAX_SLOTS_TOTAL)
    return tiers


def _suggested_period_label(conn, org_id, now=None):
    now = now or time.time()
    year = datetime.datetime.fromtimestamp(now).year
    count = conn.execute("SELECT COUNT(*) AS n FROM reward_settlements WHERE org_id = ?", (org_id,)).fetchone()["n"]
    return "%d 第 %d 期" % (year, int(count or 0) + 1)


def work_reward_map(conn, work_ids):
    """批量查询作品获奖信息，返回 {work_id: reward_dict}。"""
    ids = [str(wid) for wid in (work_ids or []) if wid]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT r.work_id, r.settlement_id, r.rank, r.tier_code, r.tier_label, r.multiplier, "
        "r.amount, r.valid_upvotes, r.created_at, s.period_label "
        "FROM work_rewards r JOIN reward_settlements s ON s.id = r.settlement_id "
        "WHERE r.work_id IN (" + placeholders + ")",
        tuple(ids),
    ).fetchall()
    return {
        r["work_id"]: {
            "settlementId": r["settlement_id"],
            "periodLabel": r["period_label"],
            "rank": int(r["rank"]),
            "tierCode": r["tier_code"],
            "tierLabel": r["tier_label"],
            "multiplier": int(r["multiplier"]),
            "amount": int(r["amount"]),
            "validUpvotes": int(r["valid_upvotes"]),
            "createdAt": r["created_at"],
        }
        for r in rows
    }


def user_reward_summary(conn, user_id, org_id=None):
    """统计用户在平台累计获得的作品奖励 Token 总额与获奖件数。"""
    if not user_id:
        return {"totalAmount": 0, "workCount": 0}
    sql = "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt FROM work_rewards WHERE owner_user_id = ?"
    params = [user_id]
    if org_id:
        sql += " AND org_id = ?"
        params.append(org_id)
    row = conn.execute(sql, tuple(params)).fetchone()
    return {"totalAmount": int(row["total"] or 0), "workCount": int(row["cnt"] or 0)}


def _eligible_rows(conn, org_id):
    """查询本组织尚未获奖、已发布、非种子示例且有有效点赞的作品，按有效点赞降序排列。

    有效点赞口径：点赞人为本组织 active 成员，且不是作品作者本人（排除自赞）。
    """
    return conn.execute(
        "SELECT * FROM ("
        "SELECT w.work_id, w.title, w.summary, w.category, w.owner_user_id, w.author_display_name, "
        "w.open_target_json, w.published_at, w.created_at, "
        "COALESCE(m.name, u.display_name, w.author_display_name) AS author_name, "
        "COALESCE(m.student_id, '') AS student_id, "
        "COALESCE(m.admission_status, 'unknown') AS author_status, "
        "(SELECT COUNT(*) FROM work_upvotes v "
        " JOIN org_memberships vm ON vm.user_id = v.user_id AND vm.org_id = w.org_id AND vm.admission_status = 'active' "
        " WHERE v.work_id = w.work_id AND v.user_id != w.owner_user_id) AS valid_upvotes, "
        "(SELECT COUNT(*) FROM work_upvotes v WHERE v.work_id = w.work_id) AS total_upvotes, "
        "(SELECT COUNT(*) FROM work_comments c WHERE c.work_id = w.work_id) AS comment_count "
        "FROM works w "
        "LEFT JOIN org_memberships m ON m.user_id = w.owner_user_id AND m.org_id = w.org_id "
        "LEFT JOIN users u ON u.id = w.owner_user_id "
        "LEFT JOIN work_rewards wr ON wr.work_id = w.work_id "
        "WHERE w.org_id = ? AND w.status = 'published' AND w.source_kind != 'seed_demo' "
        "AND w.owner_user_id IS NOT NULL AND wr.work_id IS NULL "
        "AND COALESCE(m.admission_status, 'active') = 'active'"
        ") t WHERE t.valid_upvotes > 0 "
        "ORDER BY t.valid_upvotes DESC, COALESCE(t.published_at, t.created_at) ASC, t.work_id ASC",
        (org_id,),
    ).fetchall()


def _slot_sequence(tiers):
    seq = []
    for tier in tiers:
        for _ in range(tier["slots"]):
            seq.append(tier)
    return seq


def preview_settlement(conn, org_id, base_quota, custom_slots=None, excluded_work_ids=None):
    """预览本期拟获奖候选名单与发放总额（只读，不落库）。"""
    base = max(1, int(base_quota or config.DEFAULT_MEMBER_QUOTA))
    tiers = tiers_config(base, custom_slots)
    slot_seq = _slot_sequence(tiers)
    excluded = {str(x).strip() for x in (excluded_work_ids or []) if str(x).strip()}
    rows = _eligible_rows(conn, org_id)

    candidates = []
    active_rank = 0
    estimated_total = 0
    winner_count = 0
    for row in rows:
        wid = row["work_id"]
        is_excluded = wid in excluded
        assigned = None
        rank = None
        if not is_excluded and active_rank < len(slot_seq):
            assigned = slot_seq[active_rank]
            active_rank += 1
            rank = active_rank
            estimated_total += assigned["amount"]
            winner_count += 1
        try:
            target = json.loads(row["open_target_json"] or "{}")
        except (TypeError, ValueError):
            target = {}
        candidates.append({
            "workId": wid,
            "title": row["title"],
            "summary": row["summary"],
            "category": row["category"],
            "softwareType": target.get("softwareType") or "web",
            "ownerUserId": row["owner_user_id"],
            "authorName": row["author_name"] or row["author_display_name"] or "学生",
            "studentId": row["student_id"] or "",
            "validUpvotes": int(row["valid_upvotes"]),
            "totalUpvotes": int(row["total_upvotes"]),
            "commentCount": int(row["comment_count"]),
            "publishedAt": row["published_at"] or row["created_at"],
            "excluded": is_excluded,
            "rank": rank,
            "tierCode": assigned["code"] if assigned else None,
            "tierLabel": assigned["label"] if assigned else None,
            "multiplier": assigned["multiplier"] if assigned else None,
            "amount": assigned["amount"] if assigned else 0,
        })

    return {
        "orgId": org_id,
        "baseQuota": base,
        "budgetSource": "platform_extra",
        "budgetLabel": "平台额外激励（不扣减组织原定额度）",
        "suggestedPeriod": _suggested_period_label(conn, org_id),
        "tiers": tiers,
        "totalSlots": len(slot_seq),
        "winnerCount": winner_count,
        "estimatedTotal": estimated_total,
        "candidates": candidates,
    }


def _primary_owner_ref(conn, org_id, user_id, base_quota, now):
    """获取该成员在本组织的唯一主账户 owner_ref，确保同一成员只领一份奖励；若未开账本则自动补建。"""
    row = conn.execute(
        "SELECT owner_ref FROM students WHERE user_id = ? AND org_id = ? "
        "ORDER BY CASE WHEN mode = 'student' THEN 0 ELSE 1 END, created_at ASC LIMIT 1",
        (user_id, org_id),
    ).fetchone()
    if row:
        return row["owner_ref"]

    mem = conn.execute(
        "SELECT student_id, email FROM org_memberships WHERE org_id = ? AND user_id = ?",
        (org_id, user_id),
    ).fetchone()
    if not mem:
        raise RewardError(409, "missing_member", "获奖作者不在当前组织名册中")
    if mem["student_id"]:
        mode, identity = "student", str(mem["student_id"]).strip()
    elif mem["email"]:
        mode, identity = "email", str(mem["email"]).strip().lower()
    else:
        raise RewardError(409, "missing_identity", "获奖作者缺少可用学号或校园邮箱")

    owner_ref = owner_ref_of(mode, identity)
    existing = conn.execute("SELECT owner_ref FROM students WHERE owner_ref = ?", (owner_ref,)).fetchone()
    if existing:
        conn.execute("UPDATE students SET user_id = COALESCE(user_id, ?), org_id = COALESCE(org_id, ?) WHERE owner_ref = ?",
                     (user_id, org_id, owner_ref))
        return owner_ref

    id_row = conn.execute(
        "SELECT id FROM user_identities WHERE user_id = ? AND kind = ? AND value_norm = ?",
        (user_id, mode, identity),
    ).fetchone()
    conn.execute(
        "INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at, "
        "user_id, org_id, primary_identity_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (owner_ref, mode, identity, secrets.token_hex(8), "!reward_placeholder", int(base_quota), now,
         user_id, org_id, id_row["id"] if id_row else None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO ledger (owner_ref, user_id, org_id, title, amount, kind, request_id, created_at) "
        "VALUES (?, ?, ?, '初始额度', ?, 'grant', ?, ?)",
        (owner_ref, user_id, org_id, int(base_quota), "initial-grant:" + owner_ref, now),
    )
    return owner_ref


def _format_settlement(conn, srow):
    try:
        tiers = json.loads(srow["tiers_json"] or "[]")
    except (TypeError, ValueError):
        tiers = []
    wrows = conn.execute(
        "SELECT r.*, w.title, COALESCE(m.name, u.display_name, w.author_display_name) AS author_name, "
        "COALESCE(m.student_id, '') AS student_id "
        "FROM work_rewards r "
        "JOIN works w ON w.work_id = r.work_id "
        "LEFT JOIN org_memberships m ON m.user_id = r.owner_user_id AND m.org_id = r.org_id "
        "LEFT JOIN users u ON u.id = r.owner_user_id "
        "WHERE r.settlement_id = ? ORDER BY r.rank ASC",
        (srow["id"],),
    ).fetchall()
    return {
        "settlementId": srow["id"],
        "orgId": srow["org_id"],
        "periodLabel": srow["period_label"],
        "baseQuota": int(srow["base_quota"]),
        "tiers": tiers,
        "totalAwarded": int(srow["total_awarded"]),
        "winnerCount": int(srow["winner_count"]),
        "settledBy": srow["settled_by"],
        "requestId": srow["request_id"],
        "createdAt": srow["created_at"],
        "winners": [
            {
                "rewardId": r["id"],
                "workId": r["work_id"],
                "title": r["title"],
                "ownerUserId": r["owner_user_id"],
                "authorName": r["author_name"] or "学生",
                "studentId": r["student_id"] or "",
                "rank": int(r["rank"]),
                "tierCode": r["tier_code"],
                "tierLabel": r["tier_label"],
                "multiplier": int(r["multiplier"]),
                "amount": int(r["amount"]),
                "validUpvotes": int(r["valid_upvotes"]),
                "createdAt": r["created_at"],
            }
            for r in wrows
        ],
    }


def settle(conn, org_id, base_quota, period_label, actor_id, actor_role, request_id,
           custom_slots=None, excluded_work_ids=None):
    """在同一事务内幂等结算作品奖励并入账到作者主账本（平台额外激励）。"""
    label = str(period_label or "").strip()
    if not label or len(label) > MAX_PERIOD_LABEL:
        raise RewardError(400, "invalid_period", "结算期次名称必填且不超过 %d 字符" % MAX_PERIOD_LABEL)
    rid = str(request_id or "").strip()[:80]
    if not rid:
        raise RewardError(400, "invalid_request_id", "缺少幂等 requestId")

    existing = conn.execute(
        "SELECT * FROM reward_settlements WHERE org_id = ? AND request_id = ?",
        (org_id, rid),
    ).fetchone()
    if existing:
        data = _format_settlement(conn, existing)
        data["duplicate"] = True
        return data

    plan = preview_settlement(conn, org_id, base_quota, custom_slots=custom_slots, excluded_work_ids=excluded_work_ids)
    winners = [c for c in plan["candidates"] if c["tierCode"] is not None]
    if not winners:
        raise RewardError(409, "no_candidates", "当前暂无符合条件的入围作品（需为已发布学生作品、未曾获奖且至少有 1 个非自赞有效点赞）")

    now = time.time()
    settlement_id = "rset_" + secrets.token_hex(8)
    total_awarded = sum(int(w["amount"]) for w in winners)
    conn.execute(
        "INSERT INTO reward_settlements (id, org_id, period_label, base_quota, tiers_json, total_awarded, "
        "winner_count, settled_by, request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (settlement_id, org_id, label, int(plan["baseQuota"]), json.dumps(plan["tiers"], ensure_ascii=False),
         total_awarded, len(winners), actor_id or actor_role, rid, now),
    )

    for w in winners:
        owner_ref = _primary_owner_ref(conn, org_id, w["ownerUserId"], plan["baseQuota"], now)
        reward_id = "rw_" + secrets.token_hex(8)
        conn.execute(
            "INSERT INTO work_rewards (id, settlement_id, org_id, work_id, owner_user_id, owner_ref, rank, "
            "tier_code, tier_label, multiplier, amount, valid_upvotes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (reward_id, settlement_id, org_id, w["workId"], w["ownerUserId"], owner_ref, int(w["rank"]),
             w["tierCode"], w["tierLabel"], int(w["multiplier"]), int(w["amount"]), int(w["validUpvotes"]), now),
        )
        ledger_rid = "work-reward:%s:%s" % (settlement_id, w["workId"])
        ledger_title = "作品奖励 · 《%s》%s（%d×，%s）" % (w["title"], w["tierLabel"], int(w["multiplier"]), label)
        conn.execute("UPDATE students SET balance = balance + ? WHERE owner_ref = ?", (int(w["amount"]), owner_ref))
        conn.execute(
            "INSERT INTO ledger (owner_ref, user_id, org_id, title, amount, kind, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'reward', ?, ?)",
            (owner_ref, w["ownerUserId"], org_id, ledger_title, int(w["amount"]), ledger_rid, now),
        )

    org_service.audit(
        conn, actor_id, actor_role, org_id, "rewards.settled", "organization", org_id, rid,
        {"settlementId": settlement_id, "periodLabel": label, "winnerCount": len(winners),
         "totalAwarded": total_awarded},
    )
    srow = conn.execute("SELECT * FROM reward_settlements WHERE id = ?", (settlement_id,)).fetchone()
    data = _format_settlement(conn, srow)
    data["duplicate"] = False
    return data


def org_summary(conn, org_id, base_quota, limit=20):
    """组织作品激励规则与历史发奖汇总（供管理端、广场与教师端展示）。"""
    base = max(1, int(base_quota or config.DEFAULT_MEMBER_QUOTA))
    tiers = tiers_config(base)
    agg = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total_awarded, COUNT(*) AS rewarded_works FROM work_rewards WHERE org_id = ?",
        (org_id,),
    ).fetchone()
    settlement_rows = conn.execute(
        "SELECT * FROM reward_settlements WHERE org_id = ? ORDER BY created_at DESC LIMIT ?",
        (org_id, int(limit)),
    ).fetchall()
    settlements = [_format_settlement(conn, s) for s in settlement_rows]
    eligible_count = len(_eligible_rows(conn, org_id))
    return {
        "orgId": org_id,
        "baseQuota": base,
        "budgetSource": "platform_extra",
        "budgetLabel": "平台额外激励（不扣减组织原定额度）",
        "tiers": tiers,
        "totalAwarded": int(agg["total_awarded"] or 0),
        "rewardedWorkCount": int(agg["rewarded_works"] or 0),
        "settlementCount": len(settlements),
        "eligibleCandidateCount": eligible_count,
        "suggestedPeriod": _suggested_period_label(conn, org_id),
        "settlements": settlements,
    }


DEMO_CANDIDATE_WORKS = [
    {
        "work_id": "demo-award-course-copilot",
        "student_id": "2024001100",
        "title": "软院选课避坑雷达",
        "summary": "聚合历届选课口碑、作业量与给分分布，一键生成最优课表组合。",
        "software_type": "web",
        "category": "Web 应用",
        "platform": "浏览器",
        "version": "v1.2.0",
        "ai_tools": ["Codex", "Cursor"],
        "hosted_url": "hosted/demo-campus-events/index.html",
        "preview_images": ["media/demo_campus_events_1.jpg", "media/demo_campus_events_2.jpg"],
        "upvotes": 26,
    },
    {
        "work_id": "demo-award-lab-booking",
        "student_id": "2024001101",
        "title": "实验室工位与算力预约",
        "summary": "实时查看学院 GPU 节点空闲与实验台预约状态，微信/邮件到点提醒。",
        "software_type": "web",
        "category": "Web 应用",
        "platform": "浏览器",
        "version": "v1.1.0",
        "ai_tools": ["Codex", "Claude Code"],
        "hosted_url": "hosted/demo-focus-timer/index.html",
        "preview_images": ["media/demo_focus_timer_1.jpg", "media/demo_focus_timer_2.jpg"],
        "upvotes": 21,
    },
    {
        "work_id": "demo-award-canteen-radar",
        "student_id": "2024001102",
        "title": "韵苑食堂排队指数",
        "summary": "按课间时段预测各窗口排队时长，顺手推荐今天不踩雷的招牌菜。",
        "software_type": "app",
        "category": "App",
        "platform": "Android / iOS",
        "version": "v1.0.4",
        "ai_tools": ["Cursor"],
        "preview_images": ["media/demo_homework_app_1.jpg", "media/demo_homework_app_2.jpg"],
        "upvotes": 17,
    },
    {
        "work_id": "demo-award-paper-digest",
        "student_id": "2024001103",
        "title": "组会论文速读助手",
        "summary": "拖入 PDF 论文自动提炼研究问题、实验对照表与提问清单，适配组会汇报。",
        "software_type": "pc",
        "category": "PC 软件",
        "platform": "macOS / Windows",
        "version": "v0.9.8",
        "ai_tools": ["Claude Code", "欢喜 OS"],
        "preview_images": ["media/demo_club_poster_1.jpg", "media/demo_club_poster_2.jpg"],
        "upvotes": 14,
    },
    {
        "work_id": "demo-award-git-review",
        "student_id": "2024001104",
        "title": "课设代码自检官",
        "summary": "提交大作业前一键检查内存泄漏、硬编码密钥与实验报告排版规范。",
        "software_type": "pc",
        "category": "PC 软件",
        "platform": "macOS / Windows / Linux",
        "version": "v1.0.2",
        "ai_tools": ["Codex"],
        "preview_images": ["media/demo_focus_timer_2.jpg"],
        "upvotes": 11,
    },
    {
        "work_id": "demo-award-shuttle-live",
        "student_id": "2024001105",
        "title": "校车与教学楼导航卡",
        "summary": "课表自动联动东九/南一楼跨区步行与校车发车倒计时，赶早八不迷路。",
        "software_type": "app",
        "category": "App",
        "platform": "Android / 鸿蒙",
        "version": "v1.0.1",
        "ai_tools": ["Cursor", "欢喜 OS"],
        "preview_images": ["media/demo_homework_app_2.jpg"],
        "upvotes": 9,
    },
    {
        "work_id": "demo-award-poster-maker",
        "student_id": "2024001106",
        "title": "社团活动报名页生成器",
        "summary": "输入活动时间地点与议程，30 秒生成带二维码和报名统计的活动网页。",
        "software_type": "web",
        "category": "Web 应用",
        "platform": "浏览器",
        "version": "v1.0.0",
        "ai_tools": ["Codex", "欢喜 OS"],
        "hosted_url": "hosted/demo-campus-events/index.html",
        "preview_images": ["media/demo_club_poster_2.jpg"],
        "upvotes": 6,
    },
]


def ensure_demo_candidates(conn, org_id):
    """演示模式（DEMO_ALLOW_LOGIN=1）下幂等预置 7 件真实名册学生候选作品与有效点赞。

    第 1 名候选归属默认学生演示账号 2024001100，第 2–6 名覆盖二等奖区，第 7 名覆盖三等奖区，
    方便直接在演示版体验「管理端一键结算 → 广场亮 🏆 徽章 → 学生端余额 +100 万 Token 到账」。
    若已存在预置作品或已有结算记录，则不再重复插入。
    """
    if not org_id:
        return 0
    has_any = conn.execute(
        "SELECT 1 FROM works WHERE org_id = ? AND work_id LIKE 'demo-award-%' LIMIT 1",
        (org_id,),
    ).fetchone()
    has_settled = conn.execute(
        "SELECT 1 FROM reward_settlements WHERE org_id = ? LIMIT 1",
        (org_id,),
    ).fetchone()
    if has_any or has_settled:
        return 0

    voters = [
        r["user_id"] for r in conn.execute(
            "SELECT user_id FROM org_memberships WHERE org_id = ? AND admission_status = 'active' "
            "AND student_id NOT IN ('2024001100','2024001101','2024001102','2024001103','2024001104','2024001105','2024001106') "
            "ORDER BY student_id ASC LIMIT 40",
            (org_id,),
        ).fetchall()
    ]
    now = time.time()
    inserted = 0
    for idx, spec in enumerate(DEMO_CANDIDATE_WORKS):
        mem = conn.execute(
            "SELECT user_id, name FROM org_memberships WHERE org_id = ? AND student_id = ? AND admission_status = 'active'",
            (org_id, spec["student_id"]),
        ).fetchone()
        if not mem:
            continue
        target = {
            "softwareType": spec["software_type"],
            "version": spec["version"],
            "platform": spec["platform"],
            "aiTools": spec["ai_tools"],
            "previewImages": spec["preview_images"],
        }
        if spec.get("hosted_url"):
            target["hostedUrl"] = spec["hosted_url"]
        ts = now - (len(DEMO_CANDIDATE_WORKS) - idx) * 1800
        conn.execute(
            "INSERT OR IGNORE INTO works (work_id, org_id, owner_user_id, title, summary, author_display_name, "
            "category, cover_path, source_kind, status, visibility, artifact_ref, open_target_json, "
            "published_version, created_at, updated_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'student_demo', 'published', 'organization', NULL, ?, 1, ?, ?, ?)",
            (spec["work_id"], org_id, mem["user_id"], spec["title"], spec["summary"], mem["name"] or "学生",
             spec["category"], json.dumps(target, ensure_ascii=False), ts, ts, ts),
        )
        vote_count = min(int(spec["upvotes"]), len(voters))
        for v_idx in range(vote_count):
            conn.execute(
                "INSERT OR IGNORE INTO work_upvotes (work_id, user_id, created_at) VALUES (?, ?, ?)",
                (spec["work_id"], voters[v_idx], ts + (v_idx + 1) * 30),
            )
        inserted += 1
    return inserted
