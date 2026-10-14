#!/usr/bin/env python3
"""Classify, push, and restore nested side-question branches for learning."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_DEPTH = 8
MAX_SNAPSHOT_BYTES = 64 * 1024
REQUIRED_SNAPSHOT_FIELDS = {
    "schema_version",
    "goal",
    "stage",
    "current_topic",
    "completed_steps",
    "confirmed_understanding",
    "evidence_ids",
    "last_exchange",
    "next_skill",
    "next_action",
}
PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)
RESUME_PATTERNS = (
    re.compile(r"(回到|返回|恢复).{0,6}(主线|原问题|原来的学习)"),
    re.compile(r"(继续刚才|插问结束|继续原来的|回归主线)"),
    re.compile(r"(?i)(resume|back to the main|continue where we left off)"),
)
SIDE_PATTERNS = (
    re.compile(r"^(顺便问|插问|题外话|另外问|先问一下|对了|临时问)"),
    re.compile(r"(?i)^(side question|by the way|quick question)"),
)
GOAL_REPLACEMENT_PATTERNS = (
    re.compile(r"(不学这个了|放弃原目标|换个学习目标|改学|把.{1,30}作为主线)"),
    re.compile(r"(?i)(replace the goal|new main goal|stop learning this)"),
)


class ContextError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def attach_session_dir(result: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    result.setdefault("session_dir", str(session_dir))
    return result


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stack_path(session_dir: Path) -> Path:
    return session_dir / "checkpoint-stack.json"


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def initial_stack() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "active_thread": "main",
        "thread_stack": [],
        "next_branch_number": 1,
        "updated_at": now(),
    }


def load_stack(session_dir: Path) -> dict[str, Any]:
    path = stack_path(session_dir)
    if not path.exists():
        stack = initial_stack()
        atomic_write(path, stack)
        return stack
    try:
        stack = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextError("CHECKPOINT_INVALID", str(exc)) from exc
    if not isinstance(stack, dict) or not isinstance(stack.get("thread_stack"), list):
        raise ContextError("CHECKPOINT_INVALID", "checkpoint stack must be an object with thread_stack")
    if not isinstance(stack.get("next_branch_number"), int) or stack["next_branch_number"] < 1:
        raise ContextError("CHECKPOINT_INVALID", "next_branch_number must be a positive integer")
    expected_active = stack["thread_stack"][-1].get("thread_id") if stack["thread_stack"] else "main"
    if stack.get("active_thread") != expected_active:
        raise ContextError("CHECKPOINT_INVALID", "active_thread does not match the top branch")
    return stack


def read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextError("INPUT_INVALID", str(exc)) from exc
    if not isinstance(data, dict):
        raise ContextError("INPUT_INVALID", f"{path.name} must contain a JSON object")
    return data


def flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(flatten(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(flatten(item) for item in value.values())
    return ""


def ensure_sanitized(value: Any, field: str) -> None:
    text = flatten(value)
    if any(pattern.search(text) for pattern in PII_PATTERNS):
        raise ContextError("SENSITIVE_DATA", f"{field} contains personal information; run the output guard first")


def classify_message(message: str) -> str:
    text = message.strip()
    if any(pattern.search(text) for pattern in GOAL_REPLACEMENT_PATTERNS):
        return "goal-replacement"
    if any(pattern.search(text) for pattern in RESUME_PATTERNS):
        return "resume"
    if any(pattern.search(text) for pattern in SIDE_PATTERNS):
        return "side-question"
    return "main-line"


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_SNAPSHOT_FIELDS - set(snapshot))
    if missing:
        raise ContextError("SNAPSHOT_INVALID", "snapshot is missing required fields", missing_fields=missing)
    for field in ("schema_version", "goal", "stage", "current_topic", "next_skill", "next_action"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field].strip():
            raise ContextError("SNAPSHOT_INVALID", f"{field} must be a non-empty string")
    for field in ("completed_steps", "confirmed_understanding", "evidence_ids"):
        if not isinstance(snapshot.get(field), list) or not all(isinstance(item, str) for item in snapshot[field]):
            raise ContextError("SNAPSHOT_INVALID", f"{field} must be a string list")
    if not isinstance(snapshot.get("last_exchange"), dict):
        raise ContextError("SNAPSHOT_INVALID", "last_exchange must be an object")
    encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ContextError("SNAPSHOT_TOO_LARGE", "snapshot exceeds 64 KiB; store concise summaries only")
    ensure_sanitized(snapshot, "snapshot")


def push_branch(session_dir: Path, question: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    stack = load_stack(session_dir)
    question = question.strip()
    if not question:
        raise ContextError("QUESTION_REQUIRED", "side question must be non-empty")
    if len(question) > 1000:
        raise ContextError("QUESTION_TOO_LONG", "side question must be summarized to 1000 characters or fewer")
    ensure_sanitized(question, "question")
    validate_snapshot(snapshot)
    if len(stack["thread_stack"]) >= MAX_DEPTH:
        raise ContextError("BRANCH_LIMIT", f"nested side questions cannot exceed depth {MAX_DEPTH}")

    number = stack["next_branch_number"]
    thread_id = f"branch-{number:03d}"
    parent = stack["active_thread"]
    branch = {
        "thread_id": thread_id,
        "parent_thread": parent,
        "question": question,
        "parent_snapshot": snapshot,
        "created_at": now(),
    }
    stack["thread_stack"].append(branch)
    stack["active_thread"] = thread_id
    stack["next_branch_number"] = number + 1
    stack["updated_at"] = now()
    atomic_write(stack_path(session_dir), stack)
    return {
        "ok": True,
        "status": "branch-pushed",
        "thread_id": thread_id,
        "parent_thread": parent,
        "active_thread": thread_id,
        "branch_depth": len(stack["thread_stack"]),
        "next_action": "通过完整教育安全工作流处理当前插问",
    }


def validate_summary(summary: dict[str, Any]) -> None:
    required = ("answer_summary", "impact_on_main", "unresolved_points", "safe_to_resume")
    missing = [field for field in required if field not in summary]
    if missing:
        raise ContextError("SUMMARY_INVALID", "branch summary is missing required fields", missing_fields=missing)
    if not isinstance(summary["answer_summary"], str) or not summary["answer_summary"].strip():
        raise ContextError("SUMMARY_INVALID", "answer_summary must be a non-empty string")
    if not isinstance(summary["impact_on_main"], str) or not summary["impact_on_main"].strip():
        raise ContextError("SUMMARY_INVALID", "impact_on_main must be a non-empty string")
    if not isinstance(summary["unresolved_points"], list) or not all(isinstance(item, str) for item in summary["unresolved_points"]):
        raise ContextError("SUMMARY_INVALID", "unresolved_points must be a string list")
    if summary["safe_to_resume"] is not True:
        raise ContextError("RESUME_BLOCKED", "branch must be safely closed or escalated before resuming")
    ensure_sanitized(summary, "branch summary")


def pop_branch(session_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    stack = load_stack(session_dir)
    if not stack["thread_stack"]:
        raise ContextError("NO_ACTIVE_BRANCH", "there is no side-question branch to resume from")
    validate_summary(summary)
    branch = stack["thread_stack"].pop()
    restored_thread = branch["parent_thread"]
    stack["active_thread"] = restored_thread
    stack["updated_at"] = now()
    atomic_write(stack_path(session_dir), stack)
    resume_context = dict(branch["parent_snapshot"])
    resume_context.update({
        "side_question": branch["question"],
        "branch_summary": summary,
        "restored_thread": restored_thread,
        "closed_thread": branch["thread_id"],
        "remaining_depth": len(stack["thread_stack"]),
    })
    return {
        "ok": True,
        "status": "branch-popped",
        "active_thread": restored_thread,
        "resume_context": resume_context,
        "next_skill": resume_context["next_skill"],
        "next_action": resume_context["next_action"],
    }


def result_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ContextError):
        return {"ok": False, "error_code": exc.code, "message": str(exc), **exc.details}
    if isinstance(exc, OSError):
        return {"ok": False, "error_code": "CONTEXT_STORAGE_ERROR", "message": str(exc)}
    return {"ok": False, "error_code": "UNEXPECTED_ERROR", "message": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=Path(".edu-learning-session"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser("classify")
    classify_parser.add_argument("--message", required=True)

    push_parser = subparsers.add_parser("push")
    push_parser.add_argument("--question", required=True)
    push_parser.add_argument("--snapshot", required=True, type=Path)

    pop_parser = subparsers.add_parser("pop")
    pop_parser.add_argument("--summary", required=True, type=Path)

    subparsers.add_parser("status")

    args = parser.parse_args()
    session_dir = args.session_dir.expanduser().resolve()
    try:
        if args.command == "classify":
            classification = classify_message(args.message)
            result = {"ok": True, "classification": classification, "requires_confirmation": classification == "goal-replacement"}
        elif args.command == "push":
            result = push_branch(session_dir, args.question, read_object(args.snapshot))
        elif args.command == "pop":
            result = pop_branch(session_dir, read_object(args.summary))
        else:
            stack = load_stack(session_dir)
            result = {
                "ok": True,
                "status": "valid",
                "active_thread": stack["active_thread"],
                "branch_depth": len(stack["thread_stack"]),
                "next_branch_number": stack["next_branch_number"],
            }
        sys.stdout.write(json.dumps(attach_session_dir(result, session_dir), ensure_ascii=False, indent=2) + "\n")
        return 0
    except (OSError, json.JSONDecodeError, ContextError, ValueError, TypeError) as exc:
        sys.stdout.write(json.dumps(attach_session_dir(result_error(exc), session_dir), ensure_ascii=False, indent=2) + "\n")
        return 1


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
