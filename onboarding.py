"""服务端引导配置与进度状态机。步骤定义来自 config，渲染只消费数据。"""
import json
import time

import config

FLOW_ID = config.ONBOARDING_FLOW_ID
VERSION = config.ONBOARDING_FLOW_VERSION
STEPS = config.ONBOARDING_STEPS
STEP_IDS = [item["step_id"] for item in STEPS]
ALLOWED_DRAFT_KEYS = {"productChoice", "templateId", "title", "answers", "checks", "destination", "practiceEdits"}


class OnboardingError(Exception):
    def __init__(self, status, code, message, current=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.current = current


def public_config():
    return {
        "flowId": FLOW_ID,
        "flowVersion": VERSION,
        "audience": "student",
        "displayNameKey": "platform_name",
        "entryPolicy": "new-account-auto-existing-opt-in",
        "steps": STEPS,
        "videoUrl": config.ONBOARDING_VIDEO_URL,
    }


def _initial_states():
    return {step_id: {"status": "not_started", "updated_at": None} for step_id in STEP_IDS}


def ensure(conn, user_id, auto_pending=0):
    row = conn.execute("SELECT 1 FROM onboarding_progress WHERE user_id = ? AND flow_id = ?",
                       (user_id, FLOW_ID)).fetchone()
    if row:
        return
    now = time.time()
    conn.execute(
        "INSERT INTO onboarding_progress (user_id, flow_id, flow_version, current_step_id, step_states_json, "
        "draft_json, auto_open_pending, revision, updated_at) VALUES (?, ?, ?, NULL, ?, '{}', ?, 0, ?)",
        (user_id, FLOW_ID, VERSION, json.dumps(_initial_states(), ensure_ascii=False), auto_pending, now))


def _row(conn, user_id):
    return conn.execute("SELECT * FROM onboarding_progress WHERE user_id = ? AND flow_id = ?",
                        (user_id, FLOW_ID)).fetchone()


def _payload(row, user_id):
    states = json.loads(row["step_states_json"] or "{}")
    for step_id in STEP_IDS:
        states.setdefault(step_id, {"status": "not_started", "updated_at": None})
    completed = sum(1 for state in states.values() if state.get("status") == "completed")
    skipped = sum(1 for state in states.values() if state.get("status") == "skipped")
    if completed + skipped == len(STEP_IDS):
        status = "completed" if skipped == 0 else "finished_with_skips"
    elif any(state.get("status") != "not_started" for state in states.values()):
        status = "in_progress"
    else:
        status = "not_started"
    return {
        "userId": user_id,
        "flowId": row["flow_id"],
        "flowVersion": row["flow_version"],
        "status": status,
        "currentStepId": row["current_step_id"],
        "steps": states,
        "draft": json.loads(row["draft_json"] or "{}"),
        "completedCount": completed,
        "skippedCount": skipped,
        "autoOpenPending": bool(row["auto_open_pending"]),
        "revision": row["revision"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "exitCount": row["exit_count"],
        "updatedAt": row["updated_at"],
    }


def load(conn, user_id):
    row = _row(conn, user_id)
    if not row:
        return {
            "userId": user_id, "flowId": FLOW_ID, "flowVersion": VERSION, "status": "not_started",
            "currentStepId": None, "steps": _initial_states(), "draft": {}, "completedCount": 0,
            "skippedCount": 0, "autoOpenPending": False, "revision": 0, "startedAt": None,
            "finishedAt": None, "exitCount": 0, "updatedAt": None,
        }
    return _payload(row, user_id)


def summary(conn, user_id):
    data = load(conn, user_id)
    return {key: data[key] for key in ("flowId", "flowVersion", "status", "currentStepId", "completedCount",
                                       "skippedCount", "autoOpenPending", "revision")}


def _step_or_error(step_id):
    if step_id not in STEP_IDS:
        raise OnboardingError(400, "unknown_step", "未知引导步骤")


def _merge_draft(current, incoming):
    if incoming is None:
        return current
    if not isinstance(incoming, dict):
        raise OnboardingError(400, "invalid_draft", "草稿必须是对象")
    unknown = set(incoming) - ALLOWED_DRAFT_KEYS
    if unknown:
        raise OnboardingError(400, "invalid_draft_field", "草稿包含未允许的字段")
    merged = dict(current or {})
    merged.update(incoming)
    encoded = json.dumps(merged, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > config.ONBOARDING_MAX_DRAFT:
        raise OnboardingError(400, "draft_too_large", "草稿不能超过 16 KiB")
    return merged


def apply(conn, user_id, payload, auto_pending=0):
    """在一个写事务中更新。返回 (result, changed, current)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure(conn, user_id, auto_pending)
        row = _row(conn, user_id)
        current = _payload(row, user_id)
        expected = payload.get("expectedRevision")
        if expected is not None and int(expected) != int(current["revision"]):
            conn.execute("ROLLBACK")
            raise OnboardingError(409, "revision_conflict", "引导进度已在其他页面更新", current)
        action = str(payload.get("action") or "")
        step_id = payload.get("stepId")
        states = json.loads(row["step_states_json"] or "{}")
        for key in STEP_IDS:
            states.setdefault(key, {"status": "not_started", "updated_at": None})
        now = time.time()
        changed = False
        current_step = row["current_step_id"]
        started_at = row["started_at"]
        finished_at = row["finished_at"]
        exit_count = row["exit_count"]
        last_exit_at = row["last_exit_at"]
        auto_open = row["auto_open_pending"]
        draft = json.loads(row["draft_json"] or "{}")

        def set_state(target, status, **extra):
            nonlocal changed
            previous = states[target]
            if previous.get("status") == status and status in ("completed", "skipped"):
                return
            if previous.get("status") == status and all(previous.get(k) == v for k, v in extra.items()):
                return
            states[target] = dict(previous, status=status, updated_at=now, **extra)
            changed = True

        if action in ("start", "resume"):
            _step_or_error(step_id)
            set_state(step_id, "in_progress")
            current_step = step_id
            started_at = started_at or now
            auto_open = 0
        elif action == "complete":
            _step_or_error(step_id)
            kind = payload.get("completionKind") or "demo"
            if kind != "demo":
                raise OnboardingError(400, "completion_not_supported", "原型只接受演示完成记录")
            set_state(step_id, "completed", completion_kind=kind, completed_at=now)
            current_step = step_id
            started_at = started_at or now
            auto_open = 0
        elif action == "skip":
            _step_or_error(step_id)
            set_state(step_id, "skipped", skipped_at=now)
            current_step = step_id
            started_at = started_at or now
            auto_open = 0
        elif action == "skip_all":
            for target in STEP_IDS:
                if states[target].get("status") not in ("completed", "skipped"):
                    set_state(target, "skipped", skipped_at=now)
            current_step = None
            if finished_at is None:
                finished_at = now
                changed = True
            auto_open = 0
        elif action == "finish":
            destination = payload.get("destination") or "workspace"
            if destination not in ("gallery", "profile", "workspace"):
                raise OnboardingError(400, "invalid_destination", "未知去向")
            if finished_at is None:
                finished_at = now
                changed = True
            current_step = None
            auto_open = 0
        elif action == "exit":
            exit_count += 1
            last_exit_at = now
            auto_open = 0
            changed = True
        elif action == "save_draft":
            changed = True
        else:
            raise OnboardingError(400, "unknown_action", "未知引导操作")

        draft = _merge_draft(draft, payload.get("draft"))
        revision = int(row["revision"])
        if changed:
            revision += 1
            conn.execute(
                "UPDATE onboarding_progress SET current_step_id = ?, step_states_json = ?, draft_json = ?, "
                "auto_open_pending = ?, revision = ?, started_at = ?, finished_at = ?, exit_count = ?, "
                "last_exit_at = ?, updated_at = ? WHERE user_id = ? AND flow_id = ?",
                (current_step, json.dumps(states, ensure_ascii=False), json.dumps(draft, ensure_ascii=False),
                 auto_open, revision, started_at, finished_at, exit_count, last_exit_at, now, user_id, FLOW_ID))
        conn.execute("COMMIT")
        updated = _row(conn, user_id)
        return _payload(updated, user_id), changed, _payload(row, user_id)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
