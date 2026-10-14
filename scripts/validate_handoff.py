#!/usr/bin/env python3
"""Validate machine-readable envelopes used by the education workflow."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


ENUMS = {
    "request.task_type": {"teaching", "assessment", "diagnosis", "personalization", "retention-review", "auto"},
    "route.status": {"routed", "clarify", "blocked", "error"},
    "draft.status": {"draft", "needs_clarification", "blocked"},
    "quality.decision": {"pass", "revise", "escalate"},
    "gate.stage": {"preflight", "final"},
    "gate.decision": {"allow", "clarify", "release", "revise", "block", "escalate"},
    "state.stage": {"goal-setup", "baseline-diagnosis", "explanation", "guided-practice", "feedback", "retry", "transfer-check", "spaced-review", "completed"},
    "route.actor_role": {"student", "teacher", "guardian", "administrator", "unknown"},
    "route.workflow_mode": {"continuous-learning", "teacher-workbench", "guardian-support", "governance-review"},
}


def load(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("envelope must be a JSON object")
    return data


def nonempty_string(data: dict[str, Any], field: str, errors: list[str]) -> None:
    if not isinstance(data.get(field), str) or not str(data[field]).strip():
        errors.append(f"{field} must be a non-empty string")


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate(kind: str, data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if kind != "branch-summary":
        nonempty_string(data, "schema_version", errors)
    if kind in {"request", "route", "draft", "quality", "gate"}:
        nonempty_string(data, "request_id", errors)

    if kind == "request":
        actor = data.get("actor")
        if not isinstance(actor, dict) or not isinstance(actor.get("role"), str) or not actor.get("role", "").strip():
            errors.append("actor.role must be a non-empty string")
        if not data.get("content") and not data.get("goal"):
            errors.append("content or goal is required")
        task_type = data.get("task_type", "auto")
        if task_type not in ENUMS["request.task_type"]:
            errors.append("task_type has an unsupported value")

    elif kind == "route":
        status = data.get("status")
        if status not in ENUMS["route.status"]:
            errors.append("status has an unsupported value")
        if not is_string_list(data.get("supporting_skills")):
            errors.append("supporting_skills must be a string list")
        if not is_string_list(data.get("missing_fields")):
            errors.append("missing_fields must be a string list")
        nonempty_string(data, "next_action", errors)
        if data.get("actor_role") not in ENUMS["route.actor_role"]:
            errors.append("actor_role has an unsupported value")
        if status == "routed" and data.get("workflow_mode") not in ENUMS["route.workflow_mode"]:
            errors.append("workflow_mode has an unsupported value")
        if status == "routed":
            nonempty_string(data, "primary_skill", errors)
        elif data.get("primary_skill") not in (None, ""):
            errors.append("primary_skill must be empty unless status is routed")

    elif kind == "draft":
        status = data.get("status")
        if status not in ENUMS["draft.status"]:
            errors.append("status has an unsupported value")
        nonempty_string(data, "producer_skill", errors)
        nonempty_string(data, "next_action", errors)
        if status == "draft":
            nonempty_string(data, "content", errors)
        for field in ("evidence_refs", "assumptions", "limitations"):
            if not is_string_list(data.get(field)):
                errors.append(f"{field} must be a string list")

    elif kind == "quality":
        decision = data.get("decision")
        if decision not in ENUMS["quality.decision"]:
            errors.append("decision has an unsupported value")
        if not isinstance(data.get("checks"), dict):
            errors.append("checks must be an object")
        if not isinstance(data.get("issues"), list):
            errors.append("issues must be a list")
        nonempty_string(data, "next_action", errors)
        if decision == "pass" and data.get("issues"):
            blocking = [issue for issue in data["issues"] if isinstance(issue, dict) and issue.get("severity") in {"high", "critical"}]
            if blocking:
                errors.append("pass cannot contain high or critical issues")

    elif kind == "gate":
        stage = data.get("stage")
        decision = data.get("decision")
        if stage not in ENUMS["gate.stage"]:
            errors.append("stage has an unsupported value")
        if decision not in ENUMS["gate.decision"]:
            errors.append("decision has an unsupported value")
        if not is_string_list(data.get("reason_codes")):
            errors.append("reason_codes must be a string list")
        if not is_string_list(data.get("redactions")):
            errors.append("redactions must be a string list")
        nonempty_string(data, "next_action", errors)
        if stage == "final" and decision == "release":
            nonempty_string(data, "safe_output", errors)

    elif kind == "state":
        for field in ("session_id", "goal_id", "goal", "stage", "stage_status", "mastery_status", "note_policy", "reminder_policy", "next_skill", "next_action"):
            nonempty_string(data, field, errors)
        if data.get("stage") not in ENUMS["state.stage"]:
            errors.append("stage has an unsupported value")
        if data.get("stage_status") not in {"active", "completed"}:
            errors.append("stage_status has an unsupported value")
        if data.get("mastery_status") not in {"not-demonstrated", "demonstrated"}:
            errors.append("mastery_status has an unsupported value")
        if data.get("note_policy") not in {"on-completion", "ask", "disabled"}:
            errors.append("note_policy has an unsupported value")
        if data.get("reminder_policy") not in {"scheduled", "ask", "interaction-only", "disabled"}:
            errors.append("reminder_policy has an unsupported value")
        if data.get("stage") == "completed" and data.get("mastery_status") != "demonstrated":
            errors.append("completed stage requires demonstrated mastery")

    elif kind == "snapshot":
        for field in ("goal", "stage", "current_topic", "next_skill", "next_action"):
            nonempty_string(data, field, errors)
        for field in ("completed_steps", "confirmed_understanding", "evidence_ids"):
            if not is_string_list(data.get(field)):
                errors.append(f"{field} must be a string list")
        if not isinstance(data.get("last_exchange"), dict):
            errors.append("last_exchange must be an object")

    elif kind == "branch-summary":
        for field in ("answer_summary", "impact_on_main"):
            nonempty_string(data, field, errors)
        if not is_string_list(data.get("unresolved_points")):
            errors.append("unresolved_points must be a string list")
        if data.get("safe_to_resume") is not True:
            errors.append("safe_to_resume must be true")

    elif kind == "note-summary":
        for field in ("completed_steps", "confirmed_understanding", "misconceptions_corrected", "side_question_summaries"):
            if not is_string_list(data.get(field)):
                errors.append(f"{field} must be a string list")
        for field in ("transfer_summary", "review_summary", "next_recommendation"):
            nonempty_string(data, field, errors)

    elif kind == "timestamp-receipt":
        for field in ("goal_id", "note_path", "recorded_at_utc", "clock_source", "verification_level", "note_sha256", "receipt_sha256"):
            nonempty_string(data, field, errors)
        if data.get("clock_source") != "system-utc":
            errors.append("clock_source must be system-utc")
        if data.get("verification_level") != "local-integrity":
            errors.append("verification_level must be local-integrity")
        if not isinstance(data.get("recorded_at_utc"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["recorded_at_utc"]):
            errors.append("recorded_at_utc must be second-precision RFC 3339 UTC")
        for field in ("note_sha256", "receipt_sha256"):
            if not isinstance(data.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", data[field]):
                errors.append(f"{field} must be a lowercase SHA-256 hex digest")

    elif kind == "review-state":
        for field in ("goal_id", "topic", "anchor_timestamp_utc", "status"):
            nonempty_string(data, field, errors)
        if data.get("schema_version") != "2.0":
            errors.append("review-state schema_version must be 2.0")
        if not isinstance(data.get("checkpoint_index"), int) or not 0 <= data["checkpoint_index"] <= 7:
            errors.append("checkpoint_index must be an integer from 0 to 7")
        if not isinstance(data.get("review_count"), int) or data["review_count"] < 0:
            errors.append("review_count must be a non-negative integer")
        if not isinstance(data.get("skipped_count"), int) or data["skipped_count"] < 0:
            errors.append("skipped_count must be a non-negative integer")
        if isinstance(data.get("checkpoint_index"), int) and data["checkpoint_index"] < 7:
            nonempty_string(data, "next_checkpoint_id", errors)
            nonempty_string(data, "next_review_at_utc", errors)
        elif data.get("checkpoint_index") == 7 and (data.get("next_checkpoint_id") is not None or data.get("next_review_at_utc") is not None):
            errors.append("completed review-state must clear next checkpoint and time")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("request", "route", "draft", "quality", "gate", "state", "snapshot", "branch-summary", "note-summary", "timestamp-receipt", "review-state"))
    parser.add_argument("path", help="UTF-8 JSON file or - for stdin")
    args = parser.parse_args()
    try:
        errors = validate(args.kind, load(args.path))
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        errors = [str(exc)]
    result = {"ok": not errors, "kind": args.kind, "errors": errors}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if not errors else 1


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
