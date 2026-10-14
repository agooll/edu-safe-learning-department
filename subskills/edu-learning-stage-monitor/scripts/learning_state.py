#!/usr/bin/env python3
"""Manage evidence-gated learner stages without allowing silent promotion."""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = (
    "goal-setup",
    "baseline-diagnosis",
    "explanation",
    "guided-practice",
    "feedback",
    "retry",
    "transfer-check",
    "spaced-review",
    "completed",
)

TRANSITIONS = {
    "goal-setup": {"baseline-diagnosis": {"goal-confirmed"}},
    "baseline-diagnosis": {
        "explanation": {"diagnostic-response"},
        "guided-practice": {"diagnostic-response", "baseline-passed"},
    },
    "explanation": {"guided-practice": {"concept-check"}},
    "guided-practice": {"feedback": {"practice-attempt"}},
    "feedback": {
        "retry": {"feedback-recorded"},
        "transfer-check": {"practice-passed"},
    },
    "retry": {"guided-practice": {"remediation-completed"}},
    "transfer-check": {
        "spaced-review": {"transfer-passed"},
        "explanation": {"transfer-gap"},
    },
    "spaced-review": {
        "completed": {"review-passed"},
        "retry": {"review-gap"},
    },
    "completed": {},
}

STAGE_ROUTE = {
    "goal-setup": ("edu-personalized-planner", "确认学习目标、时间约束和通过标准"),
    "baseline-diagnosis": ("edu-learning-diagnostician", "完成一项基线诊断并记录可验证证据"),
    "explanation": ("edu-teaching-designer", "讲解当前最小知识缺口并完成概念检查"),
    "guided-practice": ("edu-teaching-designer", "完成一道与当前目标对齐的引导练习"),
    "feedback": ("edu-assessment-reviewer", "依据明确标准反馈本次练习并决定重试或迁移"),
    "retry": ("edu-teaching-designer", "针对已确认错因完成一次最小补救练习"),
    "transfer-check": ("edu-assessment-reviewer", "完成不照搬原题的迁移任务并验证理解"),
    "spaced-review": ("edu-teaching-designer", "安排并完成一次延迟检索复习"),
    "completed": ("edu-learning-note-capture", "生成经安全审校的模块学习笔记并按同意策略落盘"),
}

EVIDENCE_TYPES = set().union(*(required for targets in TRANSITIONS.values() for required in targets.values()))
PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)
REMINDER_POLICIES = {"scheduled", "ask", "interaction-only", "disabled"}


class StateError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def resolved_session_dir(path: Path) -> Path:
    return path.expanduser().resolve()


def attach_session_dir(result: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    result.setdefault("session_dir", str(session_dir))
    return result


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise StateError("INVALID_JSON", f"{path.name} must contain a JSON object")
    return data


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def state_path(session_dir: Path) -> Path:
    return session_dir / "state.json"


def checkpoint_path(session_dir: Path) -> Path:
    return session_dir / "checkpoint-stack.json"


def evidence_path(session_dir: Path) -> Path:
    return session_dir / "evidence-log.jsonl"


def ensure_sanitized(value: str, field: str) -> None:
    if any(pattern.search(value) for pattern in PII_PATTERNS):
        raise StateError("SENSITIVE_DATA", f"{field} contains personal information; sanitize it before persistence")


def validate_state(session_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = state_path(session_dir)
    if not path.exists():
        raise StateError("STATE_MISSING", "learning state is missing; run init first")
    try:
        state = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("STATE_INVALID", str(exc)) from exc

    required_strings = ("schema_version", "session_id", "goal_id", "goal", "stage", "stage_status", "mastery_status", "note_policy", "next_skill", "next_action")
    missing = [field for field in required_strings if not isinstance(state.get(field), str) or not state[field].strip()]
    if missing:
        raise StateError("STATE_INVALID", "state has missing or empty fields", missing_fields=missing)
    if state["stage"] not in STAGES:
        raise StateError("STATE_INVALID", "state has an unsupported stage", stage=state["stage"])
    if state["stage_status"] not in {"active", "completed"}:
        raise StateError("STATE_INVALID", "state has an unsupported stage_status")
    if state["mastery_status"] not in {"not-demonstrated", "demonstrated"}:
        raise StateError("STATE_INVALID", "state has an unsupported mastery_status")
    if state["note_policy"] not in {"on-completion", "ask", "disabled"}:
        raise StateError("STATE_INVALID", "state has an unsupported note_policy")
    reminder_policy = state.get("reminder_policy", "ask")
    if reminder_policy not in REMINDER_POLICIES:
        raise StateError("STATE_INVALID", "state has an unsupported reminder_policy")
    state["reminder_policy"] = reminder_policy
    if state["stage"] == "completed" and state["mastery_status"] != "demonstrated":
        raise StateError("STATE_INVALID", "completed stage requires demonstrated mastery")
    if state["stage"] != "completed" and state["mastery_status"] == "demonstrated":
        raise StateError("STATE_INVALID", "mastery cannot be demonstrated before completed")

    checkpoint = {"schema_version": "1.0", "active_thread": "main", "thread_stack": [], "next_branch_number": 1}
    cpath = checkpoint_path(session_dir)
    if cpath.exists():
        try:
            checkpoint = read_json(cpath)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError("CHECKPOINT_INVALID", str(exc)) from exc
        if not isinstance(checkpoint.get("thread_stack"), list) or not isinstance(checkpoint.get("active_thread"), str):
            raise StateError("CHECKPOINT_INVALID", "checkpoint stack shape is invalid")
        expected_active = checkpoint["thread_stack"][-1].get("thread_id") if checkpoint["thread_stack"] else "main"
        if checkpoint.get("active_thread") != expected_active:
            raise StateError("CHECKPOINT_INVALID", "active_thread does not match the top branch")
    return state, checkpoint


def init_session(
    session_dir: Path,
    goal: str,
    topic: str,
    session_id: str | None,
    note_policy: str = "ask",
    reminder_policy: str = "ask",
) -> dict[str, Any]:
    goal = goal.strip()
    topic = topic.strip()
    if not goal:
        raise StateError("GOAL_REQUIRED", "goal must be non-empty")
    ensure_sanitized(goal, "goal")
    ensure_sanitized(topic, "topic")
    if note_policy not in {"on-completion", "ask", "disabled"}:
        raise StateError("NOTE_POLICY_INVALID", "note_policy must be on-completion, ask, or disabled")
    if reminder_policy not in REMINDER_POLICIES:
        raise StateError("REMINDER_POLICY_INVALID", "reminder_policy must be scheduled, ask, interaction-only, or disabled")
    path = state_path(session_dir)
    if path.exists():
        state, checkpoint = validate_state(session_dir)
        return {"ok": True, "status": "existing", "state": state, "active_thread": checkpoint["active_thread"]}

    timestamp = now()
    state = {
        "schema_version": "1.0",
        "session_id": session_id or f"session-{uuid.uuid4().hex[:12]}",
        "goal_id": f"goal-{uuid.uuid4().hex[:12]}",
        "goal": goal,
        "topic": topic,
        "stage": "goal-setup",
        "stage_status": "active",
        "mastery_status": "not-demonstrated",
        "note_policy": note_policy,
        "reminder_policy": reminder_policy,
        "next_skill": STAGE_ROUTE["goal-setup"][0],
        "next_action": STAGE_ROUTE["goal-setup"][1],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    checkpoint = {
        "schema_version": "1.0",
        "active_thread": "main",
        "thread_stack": [],
        "next_branch_number": 1,
        "updated_at": timestamp,
    }
    atomic_write(path, state)
    atomic_write(checkpoint_path(session_dir), checkpoint)
    return {"ok": True, "status": "initialized", "state": state, "active_thread": "main"}


def replace_goal(session_dir: Path, goal: str, topic: str) -> dict[str, Any]:
    state, checkpoint = validate_state(session_dir)
    if checkpoint["active_thread"] != "main":
        raise StateError("BRANCH_ACTIVE", "close all side-question branches before replacing the main goal", active_thread=checkpoint["active_thread"])
    goal = goal.strip()
    topic = topic.strip()
    if not goal:
        raise StateError("GOAL_REQUIRED", "goal must be non-empty")
    ensure_sanitized(goal, "goal")
    ensure_sanitized(topic, "topic")
    previous = {
        "goal_id": state["goal_id"],
        "stage": state["stage"],
        "mastery_status": state["mastery_status"],
    }
    state.update({
        "goal_id": f"goal-{uuid.uuid4().hex[:12]}",
        "goal": goal,
        "topic": topic,
        "stage": "goal-setup",
        "stage_status": "active",
        "mastery_status": "not-demonstrated",
        "next_skill": STAGE_ROUTE["goal-setup"][0],
        "next_action": STAGE_ROUTE["goal-setup"][1],
        "updated_at": now(),
    })
    atomic_write(state_path(session_dir), state)
    event = {
        "event": "goal_replaced",
        "session_id": state["session_id"],
        "previous_goal_id": previous["goal_id"],
        "previous_stage": previous["stage"],
        "previous_mastery_status": previous["mastery_status"],
        "new_goal_id": state["goal_id"],
        "created_at": state["updated_at"],
    }
    append_jsonl(session_dir / "runtime-events.jsonl", event)
    return {"ok": True, "status": "goal-replaced", "previous": previous, "state": state}


def set_reminder_policy(session_dir: Path, reminder_policy: str) -> dict[str, Any]:
    state, checkpoint = validate_state(session_dir)
    if checkpoint["active_thread"] != "main":
        raise StateError("BRANCH_ACTIVE", "close all side-question branches before changing reminder consent")
    if reminder_policy not in REMINDER_POLICIES:
        raise StateError("REMINDER_POLICY_INVALID", "reminder_policy must be scheduled, ask, interaction-only, or disabled")
    previous = state.get("reminder_policy", "ask")
    state["reminder_policy"] = reminder_policy
    state["updated_at"] = now()
    atomic_write(state_path(session_dir), state)
    event = {
        "event": "reminder_policy_changed",
        "session_id": state["session_id"],
        "goal_id": state["goal_id"],
        "previous_policy": previous,
        "reminder_policy": reminder_policy,
        "created_at": state["updated_at"],
    }
    append_jsonl(session_dir / "runtime-events.jsonl", event)
    return {"ok": True, "status": "reminder-policy-updated", "reminder_policy": reminder_policy, "state": state}


def load_evidence(session_dir: Path) -> list[dict[str, Any]]:
    path = evidence_path(session_dir)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StateError("EVIDENCE_INVALID", f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise StateError("EVIDENCE_INVALID", f"evidence line {line_number} is not an object")
        records.append(record)
    return records


def record_evidence(session_dir: Path, evidence_type: str, summary: str, source: str, thread_id: str | None) -> dict[str, Any]:
    state, checkpoint = validate_state(session_dir)
    if evidence_type not in EVIDENCE_TYPES:
        raise StateError("EVIDENCE_TYPE_INVALID", "unsupported evidence type", allowed=sorted(EVIDENCE_TYPES))
    summary = summary.strip()
    source = source.strip()
    if not summary or not source:
        raise StateError("EVIDENCE_INVALID", "summary and source must be non-empty")
    if len(summary) > 500 or len(source) > 300:
        raise StateError("EVIDENCE_INVALID", "summary or source is too long")
    ensure_sanitized(summary, "summary")
    ensure_sanitized(source, "source")
    active_thread = checkpoint["active_thread"]
    selected_thread = thread_id or active_thread
    if selected_thread != active_thread:
        raise StateError("THREAD_MISMATCH", "evidence thread must match active_thread", active_thread=active_thread)
    record = {
        "evidence_id": f"evidence-{uuid.uuid4().hex[:12]}",
        "session_id": state["session_id"],
        "goal_id": state["goal_id"],
        "stage": state["stage"],
        "thread_id": selected_thread,
        "type": evidence_type,
        "summary": summary,
        "source": source,
        "created_at": now(),
    }
    append_jsonl(evidence_path(session_dir), record)
    return {"ok": True, "status": "recorded", "evidence": record, "main_line_eligible": selected_thread == "main"}


def advance(session_dir: Path, target: str) -> dict[str, Any]:
    state, checkpoint = validate_state(session_dir)
    current = state["stage"]
    if checkpoint["active_thread"] != "main":
        raise StateError("BRANCH_ACTIVE", "main learning stage is frozen while a side-question branch is active", active_thread=checkpoint["active_thread"])
    allowed = TRANSITIONS[current]
    if target not in allowed:
        raise StateError("TRANSITION_INVALID", "stage transition is not allowed", current_stage=current, allowed_targets=sorted(allowed))
    required = allowed[target]
    records = load_evidence(session_dir)
    available = {
        record.get("type")
        for record in records
        if record.get("goal_id") == state["goal_id"] and record.get("stage") == current and record.get("thread_id") == "main"
    }
    missing = sorted(required - available)
    if missing:
        raise StateError("EVIDENCE_MISSING", "required evidence is missing", current_stage=current, target_stage=target, missing_evidence=missing)

    state["stage"] = target
    state["stage_status"] = "completed" if target == "completed" else "active"
    state["mastery_status"] = "demonstrated" if target == "completed" else "not-demonstrated"
    state["next_skill"], state["next_action"] = STAGE_ROUTE[target]
    state["updated_at"] = now()
    atomic_write(state_path(session_dir), state)
    event = {
        "event": "stage_advanced",
        "session_id": state["session_id"],
        "goal_id": state["goal_id"],
        "from_stage": current,
        "to_stage": target,
        "created_at": state["updated_at"],
    }
    append_jsonl(session_dir / "runtime-events.jsonl", event)
    return {"ok": True, "status": "advanced", "transition": event, "state": state}


def result_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, StateError):
        return {"ok": False, "error_code": exc.code, "message": str(exc), **exc.details}
    if isinstance(exc, OSError):
        return {"ok": False, "error_code": "STATE_STORAGE_ERROR", "message": str(exc)}
    return {"ok": False, "error_code": "UNEXPECTED_ERROR", "message": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=Path(".edu-learning-session"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--goal", required=True)
    init_parser.add_argument("--topic", default="")
    init_parser.add_argument("--session-id")
    init_parser.add_argument("--note-policy", choices=("on-completion", "ask", "disabled"), default="ask")
    init_parser.add_argument("--reminder-policy", choices=tuple(sorted(REMINDER_POLICIES)), default="ask")

    replace_parser = subparsers.add_parser("replace-goal")
    replace_parser.add_argument("--goal", required=True)
    replace_parser.add_argument("--topic", default="")

    reminder_parser = subparsers.add_parser("set-reminder-policy")
    reminder_parser.add_argument("--policy", required=True, choices=tuple(sorted(REMINDER_POLICIES)))

    subparsers.add_parser("validate")
    subparsers.add_parser("show")

    evidence_parser = subparsers.add_parser("evidence")
    evidence_parser.add_argument("--type", required=True, dest="evidence_type")
    evidence_parser.add_argument("--summary", required=True)
    evidence_parser.add_argument("--source", required=True)
    evidence_parser.add_argument("--thread-id")

    advance_parser = subparsers.add_parser("advance")
    advance_parser.add_argument("target", choices=STAGES)

    args = parser.parse_args()
    session_dir = resolved_session_dir(args.session_dir)
    try:
        if args.command == "init":
            result = init_session(session_dir, args.goal, args.topic, args.session_id, args.note_policy, args.reminder_policy)
        elif args.command == "replace-goal":
            result = replace_goal(session_dir, args.goal, args.topic)
        elif args.command == "set-reminder-policy":
            result = set_reminder_policy(session_dir, args.policy)
        elif args.command in {"validate", "show"}:
            state, checkpoint = validate_state(session_dir)
            result = {"ok": True, "status": "valid", "state": state, "active_thread": checkpoint["active_thread"], "branch_depth": len(checkpoint["thread_stack"])}
        elif args.command == "evidence":
            result = record_evidence(session_dir, args.evidence_type, args.summary, args.source, args.thread_id)
        else:
            result = advance(session_dir, args.target)
        sys.stdout.write(json.dumps(attach_session_dir(result, session_dir), ensure_ascii=False, indent=2) + "\n")
        return 0
    except (OSError, json.JSONDecodeError, StateError, ValueError, TypeError) as exc:
        sys.stdout.write(json.dumps(attach_session_dir(result_error(exc), session_dir), ensure_ascii=False, indent=2) + "\n")
        return 1


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
