#!/usr/bin/env python3
"""Persist a verified, privacy-safe end-of-module learning note."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SUMMARY_LIST_FIELDS = (
    "completed_steps",
    "confirmed_understanding",
    "misconceptions_corrected",
    "side_question_summaries",
)
SUMMARY_TEXT_FIELDS = ("transfer_summary", "review_summary", "next_recommendation")
EBBINGHAUS_CHECKPOINTS = (
    {"checkpoint_id": "r01", "label": "20分钟", "offset_minutes": 20, "review_mode": "immediate-retrieval"},
    {"checkpoint_id": "r02", "label": "1小时", "offset_minutes": 60, "review_mode": "quick-recall"},
    {"checkpoint_id": "r03", "label": "9小时", "offset_minutes": 540, "review_mode": "quick-recall"},
    {"checkpoint_id": "r04", "label": "1天", "offset_minutes": 1440, "review_mode": "focused-review"},
    {"checkpoint_id": "r05", "label": "2天", "offset_minutes": 2880, "review_mode": "focused-review"},
    {"checkpoint_id": "r06", "label": "6天", "offset_minutes": 8640, "review_mode": "transfer-review"},
    {"checkpoint_id": "r07", "label": "31天", "offset_minutes": 44640, "review_mode": "monthly-retention-check"},
)
PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(姓名|学生姓名)\s*[:：=]\s*[\u4e00-\u9fff]{2,4}"),
)
REMINDER_POLICIES = {"scheduled", "ask", "interaction-only", "disabled"}


class NoteError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def attach_session_dir(result: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    result.setdefault("session_dir", str(session_dir))
    return result


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise NoteError("TIMESTAMP_INVALID", "timestamp must be an RFC 3339 UTC value ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise NoteError("TIMESTAMP_INVALID", "timestamp is not valid RFC 3339 UTC") from exc
    return parsed


def add_minutes(timestamp: str, minutes: int) -> str:
    return (parse_utc(timestamp) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise NoteError("NOTE_STORAGE_ERROR", str(exc)) from exc


def read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoteError("INPUT_INVALID", str(exc)) from exc
    if not isinstance(data, dict):
        raise NoteError("INPUT_INVALID", f"{path.name} must contain a JSON object")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def snapshot_files(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    """Capture pre-commit bytes so a multi-file note write can be rolled back."""
    snapshots: dict[Path, bytes | None] = {}
    for path in paths:
        snapshots[path] = path.read_bytes() if path.exists() else None
    return snapshots


def restore_files(snapshots: dict[Path, bytes | None]) -> None:
    """Restore every touched file after an incomplete note commit."""
    for path, content in snapshots.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".rollback.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)


def flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(flatten(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(flatten(item) for item in value.values())
    return ""


def ensure_sanitized(value: Any) -> None:
    text = flatten(value)
    if any(pattern.search(text) for pattern in PII_PATTERNS):
        raise NoteError("SENSITIVE_DATA", "note summary contains personal information; sanitize it before persistence")


def validate_goal_id(goal_id: str) -> None:
    if not re.fullmatch(r"goal-[a-z0-9]{6,32}", goal_id):
        raise NoteError("GOAL_ID_INVALID", "goal_id cannot be used as a safe note filename")


def validate_summary(summary: dict[str, Any]) -> None:
    if summary.get("schema_version") != "1.0":
        raise NoteError("SUMMARY_INVALID", "schema_version must be 1.0")
    for field in SUMMARY_LIST_FIELDS:
        value = summary.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise NoteError("SUMMARY_INVALID", f"{field} must be a list of non-empty strings")
    for field in SUMMARY_TEXT_FIELDS:
        if not isinstance(summary.get(field), str) or not summary[field].strip():
            raise NoteError("SUMMARY_INVALID", f"{field} must be a non-empty string")
    if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
        raise NoteError("SUMMARY_TOO_LARGE", "note summary exceeds 64 KiB")
    ensure_sanitized(summary)


def load_evidence(session_dir: Path, goal_id: str) -> list[dict[str, Any]]:
    path = session_dir / "evidence-log.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NoteError("EVIDENCE_INVALID", f"invalid evidence JSONL at line {line_number}") from exc
        if isinstance(record, dict) and record.get("goal_id") == goal_id and record.get("thread_id") == "main":
            records.append(record)
    return records


def render_note(state: dict[str, Any], summary: dict[str, Any], evidence: list[dict[str, Any]], created_at: str) -> str:
    topic = str(state.get("topic") or state["goal"])
    goal_id = str(state["goal_id"])
    lines = [
        f"# 学习模块总结：{topic}",
        "",
        f"- 学习目标：{state['goal']}",
        f"- 完成状态：{state['stage']} / {state['mastery_status']}",
        f"- 目标编号：{goal_id}",
        f"- 可验证时间戳（UTC）：{created_at}",
        f"- 时间戳凭据：`{goal_id}.receipt.json`",
        "",
        "## 已完成步骤",
        "",
    ]
    lines.extend(f"- {item}" for item in summary["completed_steps"])
    lines.extend(["", "## 已确认理解", ""])
    lines.extend(f"- {item}" for item in summary["confirmed_understanding"])
    lines.extend(["", "## 主要纠错", ""])
    lines.extend(f"- {item}" for item in summary["misconceptions_corrected"])
    lines.extend(["", "## 主线学习证据", ""])
    if evidence:
        lines.extend(f"- `{item.get('evidence_id', 'unknown')}` · {item.get('type', 'unknown')} · {item.get('summary', '')}" for item in evidence)
    else:
        lines.append("- 未找到可列出的主线证据摘要")
    lines.extend(["", "## 插问与主线影响", ""])
    if summary["side_question_summaries"]:
        lines.extend(f"- {item}" for item in summary["side_question_summaries"])
    else:
        lines.append("- 本模块无已归档插问")
    lines.extend([
        "",
        "## 迁移与复习",
        "",
        f"- 迁移验证：{summary['transfer_summary']}",
        f"- 延迟复习：{summary['review_summary']}",
        f"- 首次遗忘曲线检查点：{add_minutes(created_at, EBBINGHAUS_CHECKPOINTS[0]['offset_minutes'])}（20分钟）",
        "",
        "## 下一建议",
        "",
        summary["next_recommendation"],
        "",
        "> 时间戳凭据用于校验本地笔记完整性；没有第三方时间戳服务时，不把它表述为外部权威签时。",
        "> 本笔记仅包含去标识化学习摘要，不包含完整聊天记录或正式成绩档案。",
        "",
    ])
    return "\n".join(lines)


def build_receipt(goal_id: str, note_name: str, recorded_at: str, note_sha256: str) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": "1.0",
        "goal_id": goal_id,
        "note_path": note_name,
        "recorded_at_utc": recorded_at,
        "clock_source": "system-utc",
        "verification_level": "local-integrity",
        "note_sha256": note_sha256,
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical_json(receipt))
    return receipt


def initial_review_item(state: dict[str, Any], recorded_at: str) -> dict[str, Any]:
    return {
        "goal_id": str(state["goal_id"]),
        "topic": str(state.get("topic") or state["goal"]),
        "anchor_timestamp_utc": recorded_at,
        "checkpoint_index": 0,
        "next_checkpoint_id": EBBINGHAUS_CHECKPOINTS[0]["checkpoint_id"],
        "next_review_at_utc": add_minutes(recorded_at, EBBINGHAUS_CHECKPOINTS[0]["offset_minutes"]),
        "review_count": 0,
        "skipped_count": 0,
        "status": "scheduled",
        "pending_decision": None,
        "last_outcome": None,
        "last_evidence_id": None,
    }


def reminder_registration_status(policy: str) -> str:
    return {
        "scheduled": "pending-registration",
        "ask": "awaiting-consent",
        "interaction-only": "interaction-only",
        "disabled": "disabled",
    }[policy]


def build_reminder_manifest(
    notes_dir: Path,
    state: dict[str, Any],
    recorded_at: str,
    receipt_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    goal_id = str(state["goal_id"])
    policy = str(state.get("reminder_policy", "ask"))
    if policy not in REMINDER_POLICIES:
        raise NoteError("REMINDER_POLICY_INVALID", "reminder_policy must be scheduled, ask, interaction-only, or disabled")
    status = reminder_registration_status(policy)
    tasks = []
    for checkpoint in EBBINGHAUS_CHECKPOINTS:
        checkpoint_id = checkpoint["checkpoint_id"]
        tasks.append({
            "reminder_id": f"{goal_id}-{checkpoint_id}",
            "checkpoint_id": checkpoint_id,
            "checkpoint_label": checkpoint["label"],
            "run_at_utc": add_minutes(recorded_at, checkpoint["offset_minutes"]),
            "skill_name": "edu-safe-learning-department",
            "prompt": (
                f"使用 edu-safe-learning-department 检查目标 {goal_id} 的复习状态。"
                f"本次来源检查点为 {checkpoint_id}；先严格校验时间戳并运行 due，"
                "只处理最早未完成检查点，到期时仅提供立即复习或跳过本次两个选择。"
            ),
            "registration_status": status,
            "external_task_id": None,
        })
    immutable_schedule = {
        "goal_id": goal_id,
        "anchor_timestamp_utc": recorded_at,
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
    manifest = {
        "schema_version": "1.0",
        "goal_id": goal_id,
        "anchor_timestamp_utc": recorded_at,
        "receipt_sha256": receipt_sha256,
        "reminder_policy": policy,
        "scheduler_registration_required": policy == "scheduled",
        "schedule_sha256": sha256_bytes(canonical_json(immutable_schedule)),
        "tasks": tasks,
    }
    path = notes_dir / f"{goal_id}.reminders.json"
    atomic_write_json(path, manifest)
    return path, manifest


def reminder_next_action(policy: str) -> str:
    if policy == "scheduled":
        return "按提醒清单在 AstronClaw 注册七个一次性任务，并逐项回写平台任务 ID"
    if policy == "ask":
        return "展示七个提醒时间并询问是否启用 AstronClaw 定时提醒"
    if policy == "interaction-only":
        return "不创建外部提醒；后续每次交互先检查最早到期项目"
    return "不主动提醒；学习者仍可随时主动查询复习状态"


def upsert_review_schedule(notes_dir: Path, state: dict[str, Any], recorded_at: str) -> dict[str, Any]:
    path = notes_dir / "review-schedule.json"
    schedule = read_object(path) if path.exists() else {
        "schema_version": "2.0",
        "algorithm": "ebbinghaus-observation-checkpoints-v1",
        "scientific_scope": "engineering schedule mapped to Ebbinghaus relearning observation intervals; not a universal personalized optimum",
        "checkpoints": [dict(item) for item in EBBINGHAUS_CHECKPOINTS],
        "items": [],
    }
    if schedule.get("algorithm") == "expanding-retrieval-v1":
        raise NoteError("REVIEW_SCHEDULE_MIGRATION_REQUIRED", "legacy review schedule must be migrated before adding a new note")
    if schedule.get("schema_version") != "2.0" or schedule.get("algorithm") != "ebbinghaus-observation-checkpoints-v1" or schedule.get("checkpoints") != [dict(item) for item in EBBINGHAUS_CHECKPOINTS]:
        raise NoteError("REVIEW_SCHEDULE_INVALID", "Ebbinghaus checkpoint schedule is invalid")
    items = schedule.get("items")
    if not isinstance(items, list):
        raise NoteError("REVIEW_SCHEDULE_INVALID", "review schedule items must be a list")
    goal_id = str(state["goal_id"])
    existing = next((item for item in items if isinstance(item, dict) and item.get("goal_id") == goal_id), None)
    if existing is None:
        existing = initial_review_item(state, recorded_at)
        items.append(existing)
        atomic_write_json(path, schedule)
    return existing


def runtime_event_has_receipt(session_dir: Path, goal_id: str, receipt_sha256: str, recorded_at: str, note_path: str) -> bool:
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
            and event.get("receipt_sha256") == receipt_sha256
            and event.get("recorded_at_utc") == recorded_at
            and event.get("note_path") == note_path
        ):
            return True
    return False


def verify_timestamp(session_dir: Path, goal_id: str) -> dict[str, Any]:
    validate_goal_id(goal_id)
    notes_dir = session_dir / "notes"
    note_path = notes_dir / f"{goal_id}.md"
    receipt_path = notes_dir / f"{goal_id}.receipt.json"
    if not note_path.exists() or not receipt_path.exists():
        raise NoteError("TIMESTAMP_RECEIPT_MISSING", "note or timestamp receipt is missing")
    receipt = read_object(receipt_path)
    required = ("schema_version", "goal_id", "note_path", "recorded_at_utc", "clock_source", "verification_level", "note_sha256", "receipt_sha256")
    if any(not isinstance(receipt.get(field), str) or not receipt[field] for field in required):
        raise NoteError("TIMESTAMP_RECEIPT_INVALID", "timestamp receipt has missing fields")
    if (
        receipt["schema_version"] != "1.0"
        or receipt["goal_id"] != goal_id
        or receipt["note_path"] != note_path.name
        or receipt["clock_source"] != "system-utc"
        or receipt["verification_level"] != "local-integrity"
    ):
        raise NoteError("TIMESTAMP_RECEIPT_INVALID", "timestamp receipt identity does not match the note")
    parse_utc(receipt["recorded_at_utc"])
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    expected_receipt_hash = sha256_bytes(canonical_json(unsigned))
    checks = {
        "receipt_hash_matches": expected_receipt_hash == receipt["receipt_sha256"],
        "note_hash_matches": sha256_file(note_path) == receipt["note_sha256"],
        "timestamp_embedded_in_note": f"- 可验证时间戳（UTC）：{receipt['recorded_at_utc']}" in note_path.read_text(encoding="utf-8-sig"),
        "audit_event_matches": runtime_event_has_receipt(
            session_dir,
            goal_id,
            receipt["receipt_sha256"],
            receipt["recorded_at_utc"],
            f"notes/{note_path.name}",
        ),
    }
    valid = all(checks.values())
    return {
        "ok": valid,
        "status": "verified" if valid else "invalid",
        "goal_id": goal_id,
        "recorded_at_utc": receipt["recorded_at_utc"],
        "verification_level": receipt["verification_level"],
        "receipt_sha256": receipt["receipt_sha256"],
        "checks": checks,
    }


def capture(session_dir: Path, summary: dict[str, Any], confirmed: bool) -> dict[str, Any]:
    state_path = session_dir / "state.json"
    if not state_path.exists():
        raise NoteError("STATE_MISSING", "learning state is missing")
    state = read_object(state_path)
    if state.get("stage") != "completed" or state.get("mastery_status") != "demonstrated":
        raise NoteError("MODULE_NOT_COMPLETED", "end-of-module notes require completed stage and demonstrated mastery")
    policy = state.get("note_policy")
    if policy not in {"on-completion", "ask", "disabled"}:
        raise NoteError("NOTE_POLICY_INVALID", "learning state has an invalid note_policy")
    reminder_policy = str(state.get("reminder_policy", "ask"))
    if reminder_policy not in REMINDER_POLICIES:
        raise NoteError("REMINDER_POLICY_INVALID", "learning state has an invalid reminder_policy")

    checkpoint_path = session_dir / "checkpoint-stack.json"
    checkpoint = read_object(checkpoint_path) if checkpoint_path.exists() else {"active_thread": "main", "thread_stack": []}
    if checkpoint.get("active_thread") != "main" or checkpoint.get("thread_stack"):
        raise NoteError("BRANCH_ACTIVE", "close all side-question branches before capturing a final note")

    validate_summary(summary)
    goal_id = str(state.get("goal_id"))
    validate_goal_id(goal_id)
    evidence = load_evidence(session_dir, goal_id)
    created_at = now()
    note_text = render_note(state, summary, evidence, created_at)
    ensure_sanitized(note_text)

    if policy == "disabled":
        return {"ok": True, "status": "skipped", "reason": "note_policy_disabled", "note_path": None}
    if policy == "ask" and not confirmed:
        return {"ok": True, "status": "pending-confirmation", "note_preview": note_text, "note_path": None}

    notes_dir = session_dir / "notes"
    note_path = notes_dir / f"{goal_id}.md"
    receipt_path = notes_dir / f"{goal_id}.receipt.json"
    if note_path.exists():
        verification = verify_timestamp(session_dir, goal_id)
        if not verification["ok"]:
            raise NoteError("EXISTING_NOTE_INVALID", "existing note failed timestamp verification; do not overwrite it")
        reminder_path = notes_dir / f"{goal_id}.reminders.json"
        rebuild_reminders = not reminder_path.exists()
        if reminder_path.exists():
            existing_manifest = read_object(reminder_path)
            if existing_manifest.get("reminder_policy") != reminder_policy:
                tasks = existing_manifest.get("tasks")
                if isinstance(tasks, list) and any(
                    isinstance(task, dict) and task.get("registration_status") == "registered"
                    for task in tasks
                ):
                    raise NoteError("REMINDER_POLICY_LOCKED", "cannot replace reminder policy after platform tasks were registered")
                rebuild_reminders = True
        if rebuild_reminders:
            receipt = read_object(receipt_path)
            reminder_path, _ = build_reminder_manifest(
                notes_dir,
                state,
                verification["recorded_at_utc"],
                receipt["receipt_sha256"],
            )
        return {
            "ok": True,
            "status": "existing",
            "goal_id": goal_id,
            "note_path": str(note_path),
            "timestamp_receipt_path": str(receipt_path),
            "reminder_manifest_path": str(reminder_path),
            "reminder_policy": reminder_policy,
            "timestamp_verification": verification,
            "next_action": reminder_next_action(reminder_policy),
        }

    index_path = notes_dir / "index.json"
    schedule_path = notes_dir / "review-schedule.json"
    reminder_path = notes_dir / f"{goal_id}.reminders.json"
    runtime_events_path = session_dir / "runtime-events.jsonl"
    touched_paths = (note_path, receipt_path, schedule_path, reminder_path, index_path, runtime_events_path)
    snapshots = snapshot_files(touched_paths)
    try:
        atomic_write_text(note_path, note_text)
        receipt = build_receipt(goal_id, note_path.name, created_at, sha256_file(note_path))
        atomic_write_json(receipt_path, receipt)
        review_item = upsert_review_schedule(notes_dir, state, created_at)
        reminder_path, reminder_manifest = build_reminder_manifest(
            notes_dir,
            state,
            created_at,
            receipt["receipt_sha256"],
        )

        index = read_object(index_path) if index_path.exists() else {"schema_version": "1.0", "notes": []}
        if index.get("schema_version") != "1.0" or not isinstance(index.get("notes"), list):
            raise NoteError("INDEX_INVALID", "notes/index.json has an invalid shape")
        if any(isinstance(item, dict) and item.get("goal_id") == goal_id for item in index["notes"]):
            raise NoteError("INDEX_INVALID", "notes/index.json already contains the current goal without a valid note")
        index["notes"].append({
            "goal_id": goal_id,
            "path": note_path.name,
            "topic": str(state.get("topic") or state["goal"]),
            "created_at": created_at,
            "timestamp_receipt": receipt_path.name,
            "receipt_sha256": receipt["receipt_sha256"],
            "review_schedule": "review-schedule.json",
            "reminder_manifest": reminder_path.name,
            "next_review_at_utc": review_item["next_review_at_utc"],
        })
        atomic_write_json(index_path, index)
        append_jsonl(runtime_events_path, {
            "event": "learning_note_captured",
            "session_id": state.get("session_id"),
            "goal_id": goal_id,
            "note_path": f"notes/{note_path.name}",
            "recorded_at_utc": created_at,
            "receipt_sha256": receipt["receipt_sha256"],
        })
        verification = verify_timestamp(session_dir, goal_id)
        if not verification["ok"]:
            raise NoteError("TIMESTAMP_VERIFICATION_FAILED", "new note failed timestamp verification")
    except Exception as exc:
        try:
            restore_files(snapshots)
        except OSError as rollback_exc:
            raise NoteError(
                "CAPTURE_ROLLBACK_FAILED",
                f"note capture failed and rollback could not restore the prior state: {rollback_exc}",
            ) from exc
        raise
    return {
        "ok": True,
        "status": "captured",
        "goal_id": goal_id,
        "note_path": str(note_path),
        "recorded_at_utc": created_at,
        "timestamp_receipt_path": str(receipt_path),
        "reminder_manifest_path": str(reminder_path),
        "reminder_policy": reminder_policy,
        "scheduler_registration_required": reminder_manifest["scheduler_registration_required"],
        "receipt_sha256": receipt["receipt_sha256"],
        "next_review_at_utc": review_item["next_review_at_utc"],
        "next_action": reminder_next_action(reminder_policy),
    }


def result_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, NoteError):
        return {"ok": False, "error_code": exc.code, "message": str(exc), **exc.details}
    if isinstance(exc, OSError):
        return {"ok": False, "error_code": "NOTE_STORAGE_ERROR", "message": str(exc)}
    return {"ok": False, "error_code": "UNEXPECTED_ERROR", "message": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=Path(".edu-learning-session"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--summary", required=True, type=Path)
    capture_parser.add_argument("--confirm", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--goal-id", required=True)
    args = parser.parse_args()
    session_dir = args.session_dir.expanduser().resolve()
    try:
        if args.command == "capture":
            result = capture(session_dir, read_object(args.summary), args.confirm)
        else:
            result = verify_timestamp(session_dir, args.goal_id)
        sys.stdout.write(json.dumps(attach_session_dir(result, session_dir), ensure_ascii=False, indent=2) + "\n")
        return 0 if result.get("ok") else 1
    except (OSError, json.JSONDecodeError, NoteError, ValueError, TypeError) as exc:
        sys.stdout.write(json.dumps(attach_session_dir(result_error(exc), session_dir), ensure_ascii=False, indent=2) + "\n")
        return 1


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
