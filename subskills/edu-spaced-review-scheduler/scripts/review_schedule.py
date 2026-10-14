#!/usr/bin/env python3
"""Run strict timestamp-gated Ebbinghaus observation checkpoints."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALGORITHM = "ebbinghaus-observation-checkpoints-v1"
CHECKPOINTS = (
    {"checkpoint_id": "r01", "label": "20分钟", "offset_minutes": 20, "review_mode": "immediate-retrieval"},
    {"checkpoint_id": "r02", "label": "1小时", "offset_minutes": 60, "review_mode": "quick-recall"},
    {"checkpoint_id": "r03", "label": "9小时", "offset_minutes": 540, "review_mode": "quick-recall"},
    {"checkpoint_id": "r04", "label": "1天", "offset_minutes": 1440, "review_mode": "focused-review"},
    {"checkpoint_id": "r05", "label": "2天", "offset_minutes": 2880, "review_mode": "focused-review"},
    {"checkpoint_id": "r06", "label": "6天", "offset_minutes": 8640, "review_mode": "transfer-review"},
    {"checkpoint_id": "r07", "label": "31天", "offset_minutes": 44640, "review_mode": "monthly-retention-check"},
)
MODE_ACTIONS = {
    "immediate-retrieval": "闭卷回忆一个核心概念，并用一句话说明",
    "quick-recall": "闭卷回忆核心概念，再完成一道无答案线索的快速检索题",
    "focused-review": "完成两至三个关键点检索，只复习暴露的缺口",
    "transfer-review": "完成一道不照搬原题的迁移任务并核对错因",
    "monthly-retention-check": "完成一次综合保持性检查，验证解释、迁移和纠错能力",
}
PASS_EVIDENCE = {"review-passed", "retrieval-check-passed", "transfer-check-passed"}
REVIEW_EVIDENCE_TYPES = PASS_EVIDENCE | {"review-gap"}
EVIDENCE_SOURCES = {"assessment-reviewer", "diagnostic-review", "teacher-confirmed"}
PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)
REMINDER_POLICIES = {"scheduled", "ask", "interaction-only", "disabled"}
REMINDER_REGISTRATION_STATES = {
    "pending-registration",
    "registered",
    "registration-failed",
    "awaiting-consent",
    "interaction-only",
    "disabled",
}


class ReviewError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def attach_session_dir(result: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    result.setdefault("session_dir", str(session_dir))
    return result


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ReviewError("TIMESTAMP_INVALID", "timestamp must include a timezone")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReviewError("TIMESTAMP_INVALID", "timestamp must be RFC 3339 UTC ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReviewError("TIMESTAMP_INVALID", "timestamp is not valid RFC 3339 UTC") from exc


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_object(path: Path, code: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(code, str(exc)) from exc
    if not isinstance(data, dict):
        raise ReviewError(code, f"{path.name} must contain a JSON object")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def validate_goal_id(goal_id: str) -> None:
    if not re.fullmatch(r"goal-[a-z0-9]{6,32}", goal_id):
        raise ReviewError("GOAL_ID_INVALID", "goal_id has an invalid format")


def ensure_sanitized(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ReviewError("EVIDENCE_INVALID", "evidence summary must be non-empty")
    if len(text.encode("utf-8")) > 2048:
        raise ReviewError("EVIDENCE_INVALID", "evidence summary exceeds 2 KiB")
    if any(pattern.search(text) for pattern in PII_PATTERNS):
        raise ReviewError("SENSITIVE_DATA", "evidence summary contains personal information")


def runtime_event_has_receipt(session_dir: Path, goal_id: str, receipt_hash: str, recorded_at: str, note_path: str) -> bool:
    path = session_dir / "runtime-events.jsonl"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        if (
            event.get("event") == "learning_note_captured"
            and event.get("goal_id") == goal_id
            and event.get("receipt_sha256") == receipt_hash
            and event.get("recorded_at_utc") == recorded_at
            and event.get("note_path") == note_path
        ):
            return True
    return False


def verify_note_timestamp(session_dir: Path, goal_id: str) -> dict[str, Any]:
    validate_goal_id(goal_id)
    notes_dir = session_dir / "notes"
    note_path = notes_dir / f"{goal_id}.md"
    receipt_path = notes_dir / f"{goal_id}.receipt.json"
    if not note_path.exists() or not receipt_path.exists():
        raise ReviewError("TIMESTAMP_RECEIPT_MISSING", "note or timestamp receipt is missing")
    receipt = read_object(receipt_path, "TIMESTAMP_RECEIPT_INVALID")
    required = ("schema_version", "goal_id", "note_path", "recorded_at_utc", "clock_source", "note_sha256", "receipt_sha256", "verification_level")
    if any(not isinstance(receipt.get(field), str) or not receipt[field] for field in required):
        raise ReviewError("TIMESTAMP_RECEIPT_INVALID", "timestamp receipt has missing fields")
    if receipt["goal_id"] != goal_id or receipt["note_path"] != note_path.name:
        raise ReviewError("TIMESTAMP_RECEIPT_INVALID", "timestamp receipt identity mismatch")
    if receipt["schema_version"] != "1.0" or receipt["clock_source"] != "system-utc" or receipt["verification_level"] != "local-integrity":
        raise ReviewError("TIMESTAMP_RECEIPT_INVALID", "timestamp receipt schema, clock source, or verification level is invalid")
    parse_utc(receipt["recorded_at_utc"])
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    checks = {
        "receipt_hash_matches": sha256_bytes(canonical_json(unsigned)) == receipt["receipt_sha256"],
        "note_hash_matches": sha256_bytes(note_path.read_bytes()) == receipt["note_sha256"],
        "timestamp_embedded_in_note": f"- 可验证时间戳（UTC）：{receipt['recorded_at_utc']}" in note_path.read_text(encoding="utf-8-sig"),
        "audit_event_matches": runtime_event_has_receipt(
            session_dir,
            goal_id,
            receipt["receipt_sha256"],
            receipt["recorded_at_utc"],
            f"notes/{note_path.name}",
        ),
    }
    if not all(checks.values()):
        raise ReviewError("TIMESTAMP_VERIFICATION_FAILED", "note timestamp verification failed", checks=checks)
    return {
        "recorded_at_utc": receipt["recorded_at_utc"],
        "verification_level": receipt["verification_level"],
        "receipt_sha256": receipt["receipt_sha256"],
        "checks": checks,
    }


def scheduled_at(anchor: str, checkpoint_index: int) -> str:
    return format_utc(parse_utc(anchor) + timedelta(minutes=CHECKPOINTS[checkpoint_index]["offset_minutes"]))


def reminder_prompt(goal_id: str, checkpoint_id: str) -> str:
    return (
        f"使用 edu-safe-learning-department 检查目标 {goal_id} 的复习状态。"
        f"本次来源检查点为 {checkpoint_id}；先严格校验时间戳并运行 due，"
        "只处理最早未完成检查点，到期时仅提供立即复习或跳过本次两个选择。"
    )


def reminder_schedule_payload(goal_id: str, anchor: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "goal_id": goal_id,
        "anchor_timestamp_utc": anchor,
        "tasks": [
            {
                "reminder_id": task["reminder_id"],
                "checkpoint_id": task["checkpoint_id"],
                "run_at_utc": task["run_at_utc"],
                "skill_name": task["skill_name"],
                "prompt": task["prompt"],
            }
            for task in tasks
        ],
    }


def load_reminder_manifest(session_dir: Path, goal_id: str) -> tuple[Path, dict[str, Any]]:
    verification = verify_note_timestamp(session_dir, goal_id)
    path = session_dir / "notes" / f"{goal_id}.reminders.json"
    if not path.exists():
        raise ReviewError("REMINDER_MANIFEST_MISSING", "reminder manifest is missing")
    manifest = read_object(path, "REMINDER_MANIFEST_INVALID")
    policy = manifest.get("reminder_policy")
    tasks = manifest.get("tasks")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("goal_id") != goal_id
        or manifest.get("anchor_timestamp_utc") != verification["recorded_at_utc"]
        or manifest.get("receipt_sha256") != verification["receipt_sha256"]
        or policy not in REMINDER_POLICIES
        or manifest.get("scheduler_registration_required") != (policy == "scheduled")
        or not isinstance(tasks, list)
        or len(tasks) != len(CHECKPOINTS)
    ):
        raise ReviewError("REMINDER_MANIFEST_INVALID", "reminder manifest identity or policy is invalid")
    for index, (task, checkpoint) in enumerate(zip(tasks, CHECKPOINTS)):
        if not isinstance(task, dict):
            raise ReviewError("REMINDER_MANIFEST_INVALID", "reminder task is not an object")
        checkpoint_id = checkpoint["checkpoint_id"]
        if (
            task.get("reminder_id") != f"{goal_id}-{checkpoint_id}"
            or task.get("checkpoint_id") != checkpoint_id
            or task.get("checkpoint_label") != checkpoint["label"]
            or task.get("run_at_utc") != scheduled_at(manifest["anchor_timestamp_utc"], index)
            or task.get("skill_name") != "edu-safe-learning-department"
            or task.get("prompt") != reminder_prompt(goal_id, checkpoint_id)
            or task.get("registration_status") not in REMINDER_REGISTRATION_STATES
        ):
            raise ReviewError("REMINDER_MANIFEST_INVALID", "reminder task does not match the timestamp-anchored schedule", checkpoint_id=checkpoint_id)
        external_id = task.get("external_task_id")
        if task["registration_status"] == "registered":
            if not isinstance(external_id, str) or not re.fullmatch(r"[A-Za-z0-9._:@/-]{1,256}", external_id):
                raise ReviewError("REMINDER_MANIFEST_INVALID", "registered reminder has an invalid external task id", checkpoint_id=checkpoint_id)
        elif external_id is not None:
            raise ReviewError("REMINDER_MANIFEST_INVALID", "unregistered reminder cannot contain an external task id", checkpoint_id=checkpoint_id)
    expected_hash = sha256_bytes(canonical_json(reminder_schedule_payload(goal_id, manifest["anchor_timestamp_utc"], tasks)))
    if manifest.get("schedule_sha256") != expected_hash:
        raise ReviewError("REMINDER_MANIFEST_INVALID", "reminder schedule hash does not match")
    return path, manifest


def reminder_status(session_dir: Path, goal_id: str) -> dict[str, Any]:
    _, manifest = load_reminder_manifest(session_dir, goal_id)
    registered = sum(task["registration_status"] == "registered" for task in manifest["tasks"])
    policy = manifest["reminder_policy"]
    if registered == len(CHECKPOINTS):
        next_action = "七个定时提醒均已取得平台任务 ID"
    elif policy == "scheduled":
        next_action = "仅在平台成功创建任务后逐项绑定 external_task_id"
    elif policy == "ask":
        next_action = "先取得学习者明确同意，再把提醒策略改为 scheduled"
    elif policy == "interaction-only":
        next_action = "不创建平台任务；每次进入学生线路运行 due"
    else:
        next_action = "不主动提醒；保留用户主动查询入口"
    if registered == len(CHECKPOINTS):
        status = "registered"
    elif policy == "scheduled":
        status = "registration-pending"
    else:
        status = policy
    return {
        "ok": True,
        "status": status,
        "goal_id": goal_id,
        "reminder_policy": policy,
        "registered_count": registered,
        "pending_count": len(CHECKPOINTS) - registered,
        "tasks": manifest["tasks"],
        "next_action": next_action,
    }


def bind_reminder(
    session_dir: Path,
    goal_id: str,
    checkpoint_id: str,
    external_task_id: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._:@/-]{1,256}", external_task_id):
        raise ReviewError("EXTERNAL_TASK_ID_INVALID", "external task id has an invalid format")
    path, manifest = load_reminder_manifest(session_dir, goal_id)
    if manifest["reminder_policy"] != "scheduled":
        raise ReviewError("REMINDER_CONSENT_REQUIRED", "scheduled reminder policy is required before binding platform tasks")
    duplicate = next(
        (
            task
            for task in manifest["tasks"]
            if task.get("checkpoint_id") != checkpoint_id
            and task.get("registration_status") == "registered"
            and task.get("external_task_id") == external_task_id
        ),
        None,
    )
    if duplicate is not None:
        raise ReviewError(
            "REMINDER_TASK_ID_REUSED",
            "one platform task id cannot be bound to multiple checkpoints",
            existing_checkpoint_id=duplicate.get("checkpoint_id"),
        )
    matches = [task for task in manifest["tasks"] if task["checkpoint_id"] == checkpoint_id]
    if len(matches) != 1:
        raise ReviewError("CHECKPOINT_INVALID", "checkpoint id is not present in the reminder manifest")
    task = matches[0]
    if task["registration_status"] == "registered" and task["external_task_id"] != external_task_id:
        raise ReviewError("REMINDER_BINDING_CONFLICT", "checkpoint is already bound to another platform task")
    if task["registration_status"] == "registered":
        registered = sum(item["registration_status"] == "registered" for item in manifest["tasks"])
        return {
            "ok": True,
            "status": "existing",
            "goal_id": goal_id,
            "checkpoint_id": checkpoint_id,
            "external_task_id": external_task_id,
            "registered_count": registered,
            "pending_count": len(CHECKPOINTS) - registered,
            "next_action": "继续绑定下一项平台任务" if registered < len(CHECKPOINTS) else "等待首个绝对时间检查点",
        }
    current = (at or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    task["registration_status"] = "registered"
    task["external_task_id"] = external_task_id
    task["registered_at_utc"] = format_utc(current)
    atomic_write_json(path, manifest)
    append_jsonl(session_dir / "review-events.jsonl", {
        "event": "review_reminder_bound",
        "goal_id": goal_id,
        "checkpoint_id": checkpoint_id,
        "external_task_id": external_task_id,
        "registered_at_utc": format_utc(current),
    })
    registered = sum(item["registration_status"] == "registered" for item in manifest["tasks"])
    return {
        "ok": True,
        "status": "registered",
        "goal_id": goal_id,
        "checkpoint_id": checkpoint_id,
        "external_task_id": external_task_id,
        "registered_count": registered,
        "pending_count": len(CHECKPOINTS) - registered,
        "next_action": "继续绑定下一项平台任务" if registered < len(CHECKPOINTS) else "等待首个绝对时间检查点",
    }


def new_review_item(goal_id: str, topic: str, anchor: str) -> dict[str, Any]:
    return {
        "goal_id": goal_id,
        "topic": topic,
        "anchor_timestamp_utc": anchor,
        "checkpoint_index": 0,
        "next_checkpoint_id": CHECKPOINTS[0]["checkpoint_id"],
        "next_review_at_utc": scheduled_at(anchor, 0),
        "review_count": 0,
        "skipped_count": 0,
        "status": "scheduled",
        "pending_decision": None,
        "last_outcome": None,
        "last_evidence_id": None,
    }


def validate_schedule(schedule: dict[str, Any]) -> None:
    if schedule.get("algorithm") == "expanding-retrieval-v1":
        raise ReviewError("REVIEW_SCHEDULE_MIGRATION_REQUIRED", "legacy review schedule must be migrated")
    if schedule.get("schema_version") != "2.0" or schedule.get("algorithm") != ALGORITHM:
        raise ReviewError("REVIEW_SCHEDULE_INVALID", "review schedule schema or algorithm is invalid")
    if schedule.get("checkpoints") != [dict(item) for item in CHECKPOINTS] or not isinstance(schedule.get("items"), list):
        raise ReviewError("REVIEW_SCHEDULE_INVALID", "Ebbinghaus checkpoints or items are invalid")


def validate_item(item: dict[str, Any]) -> None:
    required = ("goal_id", "topic", "anchor_timestamp_utc", "checkpoint_index", "review_count", "skipped_count", "status")
    if any(field not in item for field in required):
        raise ReviewError("REVIEW_ITEM_INVALID", "review item has missing fields")
    index = item["checkpoint_index"]
    if not isinstance(index, int) or not 0 <= index <= len(CHECKPOINTS):
        raise ReviewError("REVIEW_ITEM_INVALID", "checkpoint_index is invalid")
    if not isinstance(item["review_count"], int) or item["review_count"] < 0 or not isinstance(item["skipped_count"], int) or item["skipped_count"] < 0:
        raise ReviewError("REVIEW_ITEM_INVALID", "review or skipped count is invalid")
    parse_utc(item["anchor_timestamp_utc"])
    if index == len(CHECKPOINTS):
        if item.get("next_checkpoint_id") is not None or item.get("next_review_at_utc") is not None or item.get("status") != "completed":
            raise ReviewError("REVIEW_ITEM_INVALID", "completed review item has an invalid terminal state")
    else:
        checkpoint = CHECKPOINTS[index]
        if item.get("next_checkpoint_id") != checkpoint["checkpoint_id"] or item.get("next_review_at_utc") != scheduled_at(item["anchor_timestamp_utc"], index):
            raise ReviewError("REVIEW_TIME_MISMATCH", "next review time is not the exact timestamp-anchored checkpoint")


def load_schedule(session_dir: Path, goal_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    path = session_dir / "notes" / "review-schedule.json"
    if not path.exists():
        raise ReviewError("REVIEW_SCHEDULE_MISSING", "review schedule is missing")
    schedule = read_object(path, "REVIEW_SCHEDULE_INVALID")
    validate_schedule(schedule)
    matches = [item for item in schedule["items"] if isinstance(item, dict) and item.get("goal_id") == goal_id]
    if len(matches) != 1:
        raise ReviewError("REVIEW_ITEM_INVALID", "review item is missing or duplicated")
    validate_item(matches[0])
    return path, schedule, matches[0]


def migrate_schedule(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "notes" / "review-schedule.json"
    if not path.exists():
        raise ReviewError("REVIEW_SCHEDULE_MISSING", "review schedule is missing")
    legacy = read_object(path, "REVIEW_SCHEDULE_INVALID")
    if legacy.get("algorithm") == ALGORITHM:
        validate_schedule(legacy)
        return {"ok": True, "status": "already-current", "migrated_items": 0}
    if legacy.get("algorithm") != "expanding-retrieval-v1" or not isinstance(legacy.get("items"), list):
        raise ReviewError("REVIEW_SCHEDULE_INVALID", "unsupported schedule cannot be migrated")
    migrated: list[dict[str, Any]] = []
    for old in legacy["items"]:
        if not isinstance(old, dict) or not isinstance(old.get("goal_id"), str):
            raise ReviewError("REVIEW_ITEM_INVALID", "legacy schedule contains an invalid item")
        verification = verify_note_timestamp(session_dir, old["goal_id"])
        anchor = verification["recorded_at_utc"]
        if old.get("anchor_timestamp_utc") not in (None, anchor):
            raise ReviewError("REVIEW_ANCHOR_MISMATCH", "legacy anchor does not match the verified note timestamp", goal_id=old["goal_id"])
        migrated.append(new_review_item(old["goal_id"], str(old.get("topic") or "已归档学习目标"), anchor))
    backup = path.with_name("review-schedule.expanding-v1.backup.json")
    if not backup.exists():
        atomic_write_json(backup, legacy)
    current = {
        "schema_version": "2.0",
        "algorithm": ALGORITHM,
        "scientific_scope": "engineering schedule mapped to Ebbinghaus relearning observation intervals; not a universal personalized optimum",
        "checkpoints": [dict(item) for item in CHECKPOINTS],
        "items": migrated,
    }
    atomic_write_json(path, current)
    append_jsonl(session_dir / "runtime-events.jsonl", {
        "event": "review_schedule_migrated",
        "from_algorithm": "expanding-retrieval-v1",
        "to_algorithm": ALGORITHM,
        "migrated_items": len(migrated),
        "recorded_at_utc": format_utc(utc_now()),
    })
    return {"ok": True, "status": "migrated", "migrated_items": len(migrated), "backup_path": str(backup)}


def review_status(session_dir: Path, goal_id: str, at: datetime | None = None) -> dict[str, Any]:
    verification = verify_note_timestamp(session_dir, goal_id)
    _, _, item = load_schedule(session_dir, goal_id)
    current = (at or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    anchor = parse_utc(item["anchor_timestamp_utc"])
    if verification["recorded_at_utc"] != item["anchor_timestamp_utc"]:
        raise ReviewError("REVIEW_ANCHOR_MISMATCH", "review anchor does not match the verified note timestamp")
    if current < anchor:
        raise ReviewError("CLOCK_BEFORE_ANCHOR", "current time is earlier than the verified note timestamp")
    for field in ("last_choice_at_utc", "last_review_at_utc"):
        if item.get(field) and current < parse_utc(item[field]):
            raise ReviewError("CLOCK_ROLLBACK", "current time is earlier than a recorded review action", field=field)
    if item["checkpoint_index"] == len(CHECKPOINTS):
        return {
            "ok": True,
            "goal_id": goal_id,
            "checked_at_utc": format_utc(current),
            "status": "completed",
            "due": False,
            "available_actions": [],
            "next_skill": None,
            "next_action": "艾宾浩斯检查点已全部处理，可按学习目标另行建立个性化保持计划",
            "timestamp_verification": verification,
        }
    checkpoint = CHECKPOINTS[item["checkpoint_index"]]
    due_at = parse_utc(item["next_review_at_utc"])
    due = current >= due_at
    overdue_minutes = max(0.0, (current - due_at).total_seconds() / 60)
    if not due:
        state = "not-due"
        actions: list[str] = []
        next_skill = None
        next_action = f"等待检查点 {checkpoint['label']}：{format_utc(due_at)}"
    elif item.get("status") == "review-in-progress":
        state = "review-in-progress"
        actions = ["skip"]
        next_skill = "edu-assessment-reviewer"
        next_action = MODE_ACTIONS[checkpoint["review_mode"]]
    else:
        state = "awaiting-choice"
        actions = ["review", "skip"]
        next_skill = None
        next_action = f"检查点 {checkpoint['label']} 已到期，请选择立即复习或跳过本次"
    return {
        "ok": True,
        "goal_id": goal_id,
        "checked_at_utc": format_utc(current),
        "anchor_timestamp_utc": item["anchor_timestamp_utc"],
        "checkpoint_index": item["checkpoint_index"],
        "checkpoint_id": checkpoint["checkpoint_id"],
        "checkpoint_label": checkpoint["label"],
        "scheduled_at_utc": item["next_review_at_utc"],
        "elapsed_minutes_since_note": round((current - anchor).total_seconds() / 60, 3),
        "overdue_minutes": round(overdue_minutes, 3),
        "due": due,
        "status": state,
        "review_mode": checkpoint["review_mode"],
        "available_actions": actions,
        "timestamp_verification": verification,
        "next_skill": next_skill,
        "next_action": next_action,
    }


def due_reviews(session_dir: Path, at: datetime | None = None) -> dict[str, Any]:
    path = session_dir / "notes" / "review-schedule.json"
    if not path.exists():
        return {"ok": True, "status": "empty", "due_count": 0, "selected": None, "invalid_items": [], "next_action": "继续当前学习主线"}
    schedule = read_object(path, "REVIEW_SCHEDULE_INVALID")
    validate_schedule(schedule)
    statuses: list[dict[str, Any]] = []
    invalid_items: list[dict[str, str]] = []
    for item in schedule["items"]:
        goal_id = item.get("goal_id") if isinstance(item, dict) else None
        if not isinstance(goal_id, str):
            invalid_items.append({"goal_id": "unknown", "error_code": "REVIEW_ITEM_INVALID"})
            continue
        try:
            statuses.append(review_status(session_dir, goal_id, at))
        except ReviewError as exc:
            invalid_items.append({"goal_id": goal_id, "error_code": exc.code})
    due = [status for status in statuses if status.get("due")]
    due.sort(key=lambda status: parse_utc(status["scheduled_at_utc"]))
    selected = due[0] if due else None
    if invalid_items:
        return {
            "ok": False,
            "status": "invalid-items",
            "due_count": len(due),
            "selected": None,
            "invalid_items": invalid_items,
            "next_action": "停止自动调度并人工检查时间戳或复习计划完整性",
        }
    return {
        "ok": True,
        "status": "due" if selected else "none-due",
        "due_count": len(due),
        "selected": selected,
        "invalid_items": invalid_items,
        "next_action": selected["next_action"] if selected else "继续当前学习主线",
    }


def advance_checkpoint(item: dict[str, Any], outcome: str) -> None:
    item["checkpoint_index"] += 1
    item["pending_decision"] = None
    item["last_outcome"] = outcome
    if item["checkpoint_index"] == len(CHECKPOINTS):
        item["next_checkpoint_id"] = None
        item["next_review_at_utc"] = None
        item["status"] = "completed"
    else:
        item["next_checkpoint_id"] = CHECKPOINTS[item["checkpoint_index"]]["checkpoint_id"]
        item["next_review_at_utc"] = scheduled_at(item["anchor_timestamp_utc"], item["checkpoint_index"])
        item["status"] = "scheduled"


def choose_review(session_dir: Path, goal_id: str, decision: str, at: datetime | None = None) -> dict[str, Any]:
    if decision not in {"review", "skip"}:
        raise ReviewError("REVIEW_DECISION_INVALID", "decision must be review or skip")
    status = review_status(session_dir, goal_id, at)
    if not status["due"]:
        raise ReviewError("REVIEW_NOT_DUE", "the checkpoint is not due yet")
    if decision == "review" and status["status"] == "review-in-progress":
        return {
            "ok": True,
            "status": "review-already-started",
            "goal_id": goal_id,
            "checkpoint_id": status["checkpoint_id"],
            "decision": decision,
            "mastery_claimed": False,
            "next_review_at_utc": status["scheduled_at_utc"],
            "next_skill": status["next_skill"],
            "next_action": status["next_action"],
        }
    path, schedule, item = load_schedule(session_dir, goal_id)
    current = (at or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    checkpoint = CHECKPOINTS[item["checkpoint_index"]]
    event = {
        "event": "retention_review_choice",
        "goal_id": goal_id,
        "checkpoint_id": checkpoint["checkpoint_id"],
        "scheduled_at_utc": item["next_review_at_utc"],
        "chosen_at_utc": format_utc(current),
        "decision": decision,
    }
    if decision == "review":
        item["status"] = "review-in-progress"
        item["pending_decision"] = "review"
        item["last_choice_at_utc"] = format_utc(current)
        next_skill = "edu-assessment-reviewer"
        next_action = MODE_ACTIONS[checkpoint["review_mode"]]
        result_status = "review-started"
    else:
        item["skipped_count"] += 1
        item["last_choice_at_utc"] = format_utc(current)
        advance_checkpoint(item, "skipped-by-user")
        next_skill = None
        next_action = "本次检查点已按用户选择跳过；不据此声称掌握"
        result_status = "skipped-by-user"
    atomic_write_json(path, schedule)
    append_jsonl(session_dir / "review-events.jsonl", event)
    return {
        "ok": True,
        "status": result_status,
        "goal_id": goal_id,
        "checkpoint_id": checkpoint["checkpoint_id"],
        "decision": decision,
        "mastery_claimed": False,
        "next_review_at_utc": item.get("next_review_at_utc"),
        "next_skill": next_skill,
        "next_action": next_action,
    }


def record_review_evidence(
    session_dir: Path,
    goal_id: str,
    evidence_type: str,
    summary: str,
    source: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    verify_note_timestamp(session_dir, goal_id)
    _, _, item = load_schedule(session_dir, goal_id)
    if item.get("status") != "review-in-progress" or item.get("pending_decision") != "review":
        raise ReviewError("REVIEW_NOT_STARTED", "choose review before recording review evidence")
    if evidence_type not in REVIEW_EVIDENCE_TYPES:
        raise ReviewError("EVIDENCE_INVALID", "unsupported retention evidence type")
    if source not in EVIDENCE_SOURCES:
        raise ReviewError("EVIDENCE_SOURCE_INVALID", "retention evidence requires an approved reviewer source")
    ensure_sanitized(summary)
    checkpoint_path = session_dir / "checkpoint-stack.json"
    if checkpoint_path.exists():
        checkpoint = read_object(checkpoint_path, "CHECKPOINT_INVALID")
        if checkpoint.get("active_thread") != "main" or checkpoint.get("thread_stack"):
            raise ReviewError("BRANCH_ACTIVE", "retention evidence must be recorded on the main thread")
    current = (at or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    if current < parse_utc(item["anchor_timestamp_utc"]):
        raise ReviewError("CLOCK_BEFORE_ANCHOR", "evidence time cannot precede the note timestamp")
    evidence = {
        "evidence_id": f"evidence-review-{uuid.uuid4().hex[:12]}",
        "goal_id": goal_id,
        "checkpoint_id": item["next_checkpoint_id"],
        "thread_id": "main",
        "type": evidence_type,
        "summary": summary.strip(),
        "source": source,
        "created_at_utc": format_utc(current),
    }
    append_jsonl(session_dir / "review-evidence.jsonl", evidence)
    return {"ok": True, "status": "recorded", "evidence": evidence}


def require_review_evidence(session_dir: Path, item: dict[str, Any], evidence_id: str, evidence_type: str, current: datetime) -> dict[str, Any]:
    path = session_dir / "review-evidence.jsonl"
    if not path.exists():
        raise ReviewError("EVIDENCE_MISSING", "retention review evidence log is missing")
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            evidence = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewError("EVIDENCE_INVALID", f"invalid review evidence JSONL at line {line_number}") from exc
        if isinstance(evidence, dict) and evidence.get("evidence_id") == evidence_id:
            matches.append(evidence)
    if len(matches) != 1:
        raise ReviewError("EVIDENCE_MISSING", "exactly one matching retention evidence record is required")
    evidence = matches[0]
    if evidence.get("goal_id") != item["goal_id"] or evidence.get("checkpoint_id") != item["next_checkpoint_id"] or evidence.get("thread_id") != "main" or evidence.get("type") != evidence_type:
        raise ReviewError("EVIDENCE_MISMATCH", "retention evidence does not match the goal, checkpoint, thread, or type")
    if evidence.get("source") not in EVIDENCE_SOURCES:
        raise ReviewError("EVIDENCE_SOURCE_INVALID", "retention evidence has an unapproved source")
    ensure_sanitized(str(evidence.get("summary") or ""))
    evidence_time = parse_utc(evidence.get("created_at_utc"))
    if evidence_time > current:
        raise ReviewError("EVIDENCE_FROM_FUTURE", "retention evidence cannot be newer than the review record")
    if item.get("last_choice_at_utc") and evidence_time < parse_utc(item["last_choice_at_utc"]):
        raise ReviewError("EVIDENCE_BEFORE_CHOICE", "retention evidence cannot predate the review choice")
    return evidence


def record_review(
    session_dir: Path,
    goal_id: str,
    outcome: str,
    evidence_id: str,
    evidence_type: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    verify_note_timestamp(session_dir, goal_id)
    path, schedule, item = load_schedule(session_dir, goal_id)
    if item.get("status") != "review-in-progress" or item.get("pending_decision") != "review":
        raise ReviewError("REVIEW_NOT_STARTED", "choose review before recording its result")
    if outcome not in {"passed", "gap"}:
        raise ReviewError("REVIEW_OUTCOME_INVALID", "outcome must be passed or gap")
    if not re.fullmatch(r"evidence-[A-Za-z0-9-]{4,64}", evidence_id):
        raise ReviewError("EVIDENCE_INVALID", "evidence_id has an invalid format")
    if outcome == "gap" and evidence_type != "review-gap":
        raise ReviewError("EVIDENCE_INVALID", "a gap outcome requires review-gap evidence")
    if outcome == "passed" and evidence_type not in PASS_EVIDENCE:
        raise ReviewError("EVIDENCE_INVALID", "evidence type cannot support a passed outcome")
    current = (at or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    require_review_evidence(session_dir, item, evidence_id, evidence_type, current)
    checkpoint_id = item["next_checkpoint_id"]
    item["review_count"] += 1
    item["last_review_at_utc"] = format_utc(current)
    item["last_evidence_id"] = evidence_id
    if outcome == "passed":
        advance_checkpoint(item, "passed")
        next_skill = None
        next_action = "当前检查点复习通过，等待下一个严格时间检查点"
        result_status = "passed"
    else:
        item["pending_decision"] = None
        item["status"] = "gap-detected"
        item["last_outcome"] = "gap"
        next_skill = "edu-learning-diagnostician"
        next_action = "诊断遗忘点并完成最小补救；当前检查点保持未完成"
        result_status = "gap-detected"
    atomic_write_json(path, schedule)
    append_jsonl(session_dir / "review-events.jsonl", {
        "event": "retention_review_recorded",
        "goal_id": goal_id,
        "checkpoint_id": checkpoint_id,
        "recorded_at_utc": format_utc(current),
        "outcome": outcome,
        "evidence_id": evidence_id,
        "evidence_type": evidence_type,
        "schedule_status": item["status"],
        "next_review_at_utc": item.get("next_review_at_utc"),
    })
    return {
        "ok": True,
        "status": result_status,
        "goal_id": goal_id,
        "checkpoint_id": checkpoint_id,
        "recorded_at_utc": format_utc(current),
        "next_review_at_utc": item.get("next_review_at_utc"),
        "review_count": item["review_count"],
        "next_skill": next_skill,
        "next_action": next_action,
    }


def result_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ReviewError):
        return {"ok": False, "error_code": exc.code, "message": str(exc), **exc.details}
    if isinstance(exc, OSError):
        return {"ok": False, "error_code": "REVIEW_STORAGE_ERROR", "message": str(exc)}
    return {"ok": False, "error_code": "UNEXPECTED_ERROR", "message": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=Path(".edu-learning-session"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("due")
    subparsers.add_parser("migrate")
    reminder_parser = subparsers.add_parser("reminders")
    reminder_parser.add_argument("--goal-id", required=True)
    bind_parser = subparsers.add_parser("bind-reminder")
    bind_parser.add_argument("--goal-id", required=True)
    bind_parser.add_argument("--checkpoint-id", required=True, choices=tuple(item["checkpoint_id"] for item in CHECKPOINTS))
    bind_parser.add_argument("--external-task-id", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--goal-id", required=True)
    choice_parser = subparsers.add_parser("choose")
    choice_parser.add_argument("--goal-id", required=True)
    choice_parser.add_argument("--decision", required=True, choices=("review", "skip"))
    evidence_parser = subparsers.add_parser("evidence")
    evidence_parser.add_argument("--goal-id", required=True)
    evidence_parser.add_argument("--evidence-type", required=True, choices=tuple(sorted(REVIEW_EVIDENCE_TYPES)))
    evidence_parser.add_argument("--summary", required=True)
    evidence_parser.add_argument("--source", required=True, choices=tuple(sorted(EVIDENCE_SOURCES)))
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--goal-id", required=True)
    record_parser.add_argument("--outcome", required=True, choices=("passed", "gap"))
    record_parser.add_argument("--evidence-id", required=True)
    record_parser.add_argument("--evidence-type", required=True)
    args = parser.parse_args()
    session_dir = args.session_dir.expanduser().resolve()
    try:
        if args.command == "due":
            result = due_reviews(session_dir)
        elif args.command == "migrate":
            result = migrate_schedule(session_dir)
        elif args.command == "reminders":
            result = reminder_status(session_dir, args.goal_id)
        elif args.command == "bind-reminder":
            result = bind_reminder(session_dir, args.goal_id, args.checkpoint_id, args.external_task_id)
        elif args.command == "status":
            result = review_status(session_dir, args.goal_id)
        elif args.command == "choose":
            result = choose_review(session_dir, args.goal_id, args.decision)
        elif args.command == "evidence":
            result = record_review_evidence(session_dir, args.goal_id, args.evidence_type, args.summary, args.source)
        else:
            result = record_review(session_dir, args.goal_id, args.outcome, args.evidence_id, args.evidence_type)
        sys.stdout.write(json.dumps(attach_session_dir(result, session_dir), ensure_ascii=False, indent=2) + "\n")
        return 0 if result.get("ok") else 1
    except (OSError, json.JSONDecodeError, ReviewError, ValueError, TypeError) as exc:
        sys.stdout.write(json.dumps(attach_session_dir(result_error(exc), session_dir), ensure_ascii=False, indent=2) + "\n")
        return 1


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
