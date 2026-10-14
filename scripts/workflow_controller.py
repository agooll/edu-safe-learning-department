#!/usr/bin/env python3
"""Enforce the L1 education workflow with persistent, chained receipts."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
MAX_REVISIONS = 2
WORKFLOW_ID_PATTERN = re.compile(r"wf-[0-9a-f]{24}")
TERMINAL_STATUSES = {"blocked", "escalated", "clarification-required", "released"}
ENFORCEMENT = {
    "enforcement_level": "L1",
    "platform_required_tool_verified": False,
    "direct_response_blocked": False,
    "known_limitation": "控制器可强校验已进入的流程，但AstronClaw平台尚未证明会阻止模型绕过控制器直接回答。",
}


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str, *, next_action: str = "停止发布并检查工作流状态") -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_module("edu_handoff_validator", SKILL_ROOT / "scripts" / "validate_handoff.py")
GATE = load_module(
    "edu_safety_gate",
    SKILL_ROOT / "subskills" / "edu-output-guard" / "scripts" / "safety_gate.py",
)
ROUTER = load_module(
    "edu_request_router",
    SKILL_ROOT / "subskills" / "edu-request-router" / "scripts" / "route_request.py",
)
LEARNING_STATE = load_module(
    "edu_learning_state_controller",
    SKILL_ROOT / "subskills" / "edu-learning-stage-monitor" / "scripts" / "learning_state.py",
)


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise WorkflowError("INVALID_JSON", "输入必须是JSON对象")
    return value


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def resolved_session_dir(path: str) -> Path:
    return Path(path).expanduser().resolve()


def controller_dir(session_dir: Path) -> Path:
    return session_dir / ".workflow-controller"


def workflow_path(session_dir: Path, workflow_id: str) -> Path:
    if not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
        raise WorkflowError("WORKFLOW_ID_INVALID", "workflow_id格式无效")
    return controller_dir(session_dir) / "workflows" / f"{workflow_id}.json"


def events_path(session_dir: Path) -> Path:
    return controller_dir(session_dir) / "workflow-events.jsonl"


def clean_for_persistence(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    clean, redactions = GATE.redact_value(value)
    if not isinstance(clean, dict):
        raise WorkflowError("PERSISTENCE_SANITIZATION_FAILED", "无法把工作流数据安全转换为对象")
    return clean, list(dict.fromkeys(redactions))


def validate_envelope(kind: str, payload: dict[str, Any]) -> None:
    errors = VALIDATOR.validate(kind, payload)
    if errors:
        raise WorkflowError(
            f"{kind.upper()}_INVALID",
            "; ".join(errors),
            next_action=f"修复{kind}结构后重新提交，不得跳过控制器",
        )


def new_state(request_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    return {
        "schema_version": "1.0",
        "workflow_id": f"wf-{uuid.uuid4().hex[:24]}",
        "request_id": request_id,
        "status": "initializing",
        "revision_count": 0,
        "expected_action": "start",
        "next_skill": "edu-output-guard",
        "next_action": "执行生成前安全门禁",
        "artifacts": [],
        "receipts": [],
        "current_draft_artifact_id": None,
        "current_quality_artifact_id": None,
        "final_artifact_id": None,
        "release_artifact_id": None,
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
    }


def issue_receipt(state: dict[str, Any], kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    clean_payload, redactions = clean_for_persistence(payload)
    sequence = len(state["receipts"]) + 1
    artifact_id = f"artifact-{sequence:03d}-{kind}"
    artifact = {
        "artifact_id": artifact_id,
        "kind": kind,
        "revision_count": state["revision_count"],
        "payload": clean_payload,
        "redactions": redactions,
    }
    previous = state["receipts"][-1]["receipt_sha256"] if state["receipts"] else None
    receipt = {
        "schema_version": "1.0",
        "workflow_id": state["workflow_id"],
        "request_id": state["request_id"],
        "sequence": sequence,
        "step": kind,
        "artifact_id": artifact_id,
        "revision_count": state["revision_count"],
        "issued_at_utc": utc_now(),
        "previous_receipt_sha256": previous,
        "payload_sha256": sha256_json(clean_payload),
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    state["artifacts"].append(artifact)
    state["receipts"].append(receipt)
    state["updated_at_utc"] = receipt["issued_at_utc"]
    return receipt


def artifact_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, list):
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "artifacts不是列表")
    result: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("artifact_id"), str):
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "artifact结构损坏")
        if artifact["artifact_id"] in result:
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "artifact_id重复")
        result[artifact["artifact_id"]] = artifact
    return result


def validate_receipt_chain(state: dict[str, Any]) -> None:
    receipts = state.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "工作流没有任何真实回执")
    artifacts = artifact_map(state)
    previous: str | None = None
    for index, receipt in enumerate(receipts, start=1):
        if not isinstance(receipt, dict):
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "receipt结构损坏")
        stored_hash = receipt.get("receipt_sha256")
        body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        if stored_hash != sha256_json(body):
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "receipt哈希不匹配")
        if receipt.get("sequence") != index or receipt.get("previous_receipt_sha256") != previous:
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "receipt顺序或链路不匹配")
        if receipt.get("workflow_id") != state.get("workflow_id") or receipt.get("request_id") != state.get("request_id"):
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "receipt归属不匹配")
        artifact = artifacts.get(str(receipt.get("artifact_id")))
        if artifact is None or artifact.get("kind") != receipt.get("step"):
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "receipt引用的artifact不存在")
        if receipt.get("payload_sha256") != sha256_json(artifact.get("payload")):
            raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "artifact内容已被篡改")
        previous = str(stored_hash)

    kinds = [str(receipt.get("step")) for receipt in receipts]
    status = state.get("status")
    if kinds[0] != "preflight":
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "首个回执必须是preflight")
    if status in {"awaiting-draft", "awaiting-quality", "ready-for-release", "released"} and "route" not in kinds:
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "缺少route回执")
    if status in {"awaiting-quality", "ready-for-release", "released"} and not any(kind == "draft" for kind in kinds):
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "缺少draft回执")
    if status in {"ready-for-release", "released"} and not any(kind == "quality" for kind in kinds):
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "缺少quality回执")
    if status in {"ready-for-release", "released"} and "final-gate" not in kinds:
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "缺少final-gate回执")
    if status == "released" and kinds[-1] != "release-authorization":
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "缺少最终发布授权回执")


def persist(session_dir: Path, state: dict[str, Any], *receipts: dict[str, Any]) -> None:
    path = workflow_path(session_dir, state["workflow_id"])
    atomic_write(path, state)
    for receipt in receipts:
        append_jsonl(
            events_path(session_dir),
            {
                "timestamp_utc": receipt["issued_at_utc"],
                "workflow_id": state["workflow_id"],
                "request_id": state["request_id"],
                "sequence": receipt["sequence"],
                "step": receipt["step"],
                "receipt_sha256": receipt["receipt_sha256"],
                "status": state["status"],
                "revision_count": state["revision_count"],
            },
        )


def load_state(session_dir: Path, workflow_id: str) -> dict[str, Any]:
    path = workflow_path(session_dir, workflow_id)
    if not path.is_file():
        raise WorkflowError("WORKFLOW_NOT_FOUND", "找不到指定工作流")
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", str(exc)) from exc
    if not isinstance(state, dict):
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "工作流状态必须是JSON对象")
    if state.get("workflow_id") != workflow_id:
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "workflow_id与文件名不一致")
    validate_receipt_chain(state)
    return state


def get_artifact_payload(state: dict[str, Any], artifact_id: str | None) -> dict[str, Any]:
    if not artifact_id:
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "工作流缺少当前artifact引用")
    artifact = artifact_map(state).get(artifact_id)
    if artifact is None or not isinstance(artifact.get("payload"), dict):
        raise WorkflowError("WORKFLOW_INTEGRITY_ERROR", "当前artifact不存在或损坏")
    return artifact["payload"]


def ensure_learning_context(session_dir: Path, request: dict[str, Any], route: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if route.get("workflow_mode") != "continuous-learning":
        return route, {}
    try:
        state, checkpoint = LEARNING_STATE.validate_state(session_dir)
    except LEARNING_STATE.StateError as exc:
        if exc.code != "STATE_MISSING":
            raise WorkflowError("LEARNING_STATE_INVALID", str(exc), next_action="修复学习状态后重新开始工作流") from exc
        goal = str(request.get("goal") or request.get("content") or "").strip()
        topic = str(request.get("subject") or request.get("topic") or goal).strip()
        note_policy = str(request.get("note_policy") or "ask")
        reminder_policy = str(request.get("reminder_policy") or "ask")
        try:
            LEARNING_STATE.init_session(session_dir, goal, topic, None, note_policy, reminder_policy)
            state, checkpoint = LEARNING_STATE.validate_state(session_dir)
        except LEARNING_STATE.StateError as init_exc:
            raise WorkflowError("LEARNING_STATE_INIT_FAILED", str(init_exc), next_action="补充安全学习目标并重试") from init_exc

    if route.get("task_type") != "retention-review":
        route = dict(route)
        route["primary_skill"] = state["next_skill"]
        route["next_action"] = state["next_action"]
        skill_to_task = {
            "edu-personalized-planner": "personalization",
            "edu-learning-diagnostician": "diagnosis",
            "edu-teaching-designer": "teaching",
            "edu-assessment-reviewer": "assessment",
            "edu-spaced-review-scheduler": "retention-review",
            "edu-learning-note-capture": "personalization",
        }
        route["task_type"] = skill_to_task.get(state["next_skill"], route.get("task_type"))

    session_context = {
        "mode": "continuous-learning",
        "message_class": str(request.get("message_class") or "main-line"),
        "active_thread": checkpoint["active_thread"],
        "state": {
            "stage": state["stage"],
            "mastery_status": state["mastery_status"],
            "next_skill": state["next_skill"],
            "next_action": state["next_action"],
        },
    }
    return route, {"learning_state": state, "session_context": session_context}


def summary(state: dict[str, Any]) -> dict[str, Any]:
    latest = state["receipts"][-1]
    result = {
        "schema_version": "1.0",
        "ok": True,
        "workflow_id": state["workflow_id"],
        "request_id": state["request_id"],
        "status": state["status"],
        "revision_count": state["revision_count"],
        "expected_action": state["expected_action"],
        "next_skill": state.get("next_skill"),
        "next_action": state["next_action"],
        "receipt_count": len(state["receipts"]),
        "latest_receipt_sha256": latest["receipt_sha256"],
        "release_allowed": state["status"] in {"ready-for-release", "released"},
        "enforcement": ENFORCEMENT,
    }
    terminal = state.get("terminal_decision")
    if terminal:
        result["terminal_decision"] = terminal
    return result


def start_workflow(session_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    validate_envelope("request", request)
    request_id = str(request["request_id"])
    state = new_state(request_id)

    preflight = GATE.preflight(request)
    validate_envelope("gate", preflight)
    preflight_receipt = issue_receipt(state, "preflight", preflight)
    decision = preflight["decision"]
    if decision != "allow":
        state["status"] = {
            "clarify": "clarification-required",
            "block": "blocked",
            "escalate": "escalated",
        }.get(decision, "blocked")
        state["terminal_decision"] = decision
        state["expected_action"] = "none"
        state["next_skill"] = None
        state["next_action"] = preflight["next_action"]
        persist(session_dir, state, preflight_receipt)
        return summary(state)

    persist(session_dir, state, preflight_receipt)
    sanitized_request = preflight["sanitized_request"]
    route = ROUTER.route(sanitized_request)
    route, learning = ensure_learning_context(session_dir, sanitized_request, route)
    if learning:
        learning_receipt = issue_receipt(state, "learning-state", learning)
        state["session_context"] = learning["session_context"]
        persist(session_dir, state, learning_receipt)
    validate_envelope("route", route)
    route_receipt = issue_receipt(state, "route", route)
    state["sanitized_request"] = sanitized_request
    state["route_artifact_id"] = route_receipt["artifact_id"]

    if route["status"] != "routed":
        state["status"] = "clarification-required" if route["status"] == "clarify" else "blocked"
        state["terminal_decision"] = route["status"]
        state["expected_action"] = "none"
        state["next_skill"] = None
    else:
        state["status"] = "awaiting-draft"
        state["expected_action"] = "submit-draft"
        state["next_skill"] = route["primary_skill"]
    state["next_action"] = route["next_action"]
    persist(session_dir, state, route_receipt)
    return summary(state)


def submit_draft(session_dir: Path, workflow_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    state = load_state(session_dir, workflow_id)
    if state["status"] != "awaiting-draft" or state["expected_action"] != "submit-draft":
        raise WorkflowError("STEP_ORDER_VIOLATION", "当前工作流不接受draft", next_action=state["next_action"])
    clean_draft, _ = clean_for_persistence(draft)
    validate_envelope("draft", clean_draft)
    if clean_draft["request_id"] != state["request_id"]:
        raise WorkflowError("REQUEST_ID_MISMATCH", "draft的request_id不匹配")
    route = get_artifact_payload(state, state.get("route_artifact_id"))
    if clean_draft["producer_skill"] != route.get("primary_skill"):
        raise WorkflowError("PRODUCER_MISMATCH", "draft必须由当前唯一主责Skill产生")

    receipt = issue_receipt(state, "draft", clean_draft)
    state["current_draft_artifact_id"] = receipt["artifact_id"]
    if clean_draft["status"] == "needs_clarification":
        state["status"] = "clarification-required"
        state["terminal_decision"] = "clarify"
        state["expected_action"] = "none"
        state["next_skill"] = None
    elif clean_draft["status"] == "blocked":
        state["status"] = "blocked"
        state["terminal_decision"] = "block"
        state["expected_action"] = "none"
        state["next_skill"] = None
    else:
        state["status"] = "awaiting-quality"
        state["expected_action"] = "submit-quality"
        state["next_skill"] = "edu-quality-auditor"
    state["next_action"] = clean_draft["next_action"]
    persist(session_dir, state, receipt)
    return summary(state)


def controller_escalation(state: dict[str, Any], code: str, next_action: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": state["request_id"],
        "stage": "final",
        "decision": "escalate",
        "reason_codes": [code],
        "risk_level": "high",
        "human_review_required": True,
        "redactions": [],
        "safe_output": "",
        "next_action": next_action,
    }


def submit_quality(session_dir: Path, workflow_id: str, quality: dict[str, Any]) -> dict[str, Any]:
    state = load_state(session_dir, workflow_id)
    if state["status"] != "awaiting-quality" or state["expected_action"] != "submit-quality":
        raise WorkflowError("STEP_ORDER_VIOLATION", "当前工作流不接受quality", next_action=state["next_action"])
    clean_quality, _ = clean_for_persistence(quality)
    validate_envelope("quality", clean_quality)
    if clean_quality["request_id"] != state["request_id"]:
        raise WorkflowError("REQUEST_ID_MISMATCH", "quality的request_id不匹配")
    if clean_quality["decision"] == "pass":
        required_checks = {"complete", "traceable", "pedagogically_appropriate", "actionable", "uncertainty_disclosed"}
        checks = clean_quality.get("checks", {})
        if not required_checks.issubset(checks) or not all(checks.get(name) is True for name in required_checks):
            raise WorkflowError("QUALITY_PASS_INVALID", "quality为pass时所有强制检查必须明确为true")

    quality_receipt = issue_receipt(state, "quality", clean_quality)
    state["current_quality_artifact_id"] = quality_receipt["artifact_id"]
    decision = clean_quality["decision"]
    if decision == "revise":
        if state["revision_count"] >= MAX_REVISIONS:
            final = controller_escalation(state, "REVISION_LIMIT", "两次返修后仍未通过，转人工复核")
            final_receipt = issue_receipt(state, "final-gate", final)
            state["final_artifact_id"] = final_receipt["artifact_id"]
            state["status"] = "escalated"
            state["terminal_decision"] = "escalate"
            state["expected_action"] = "none"
            state["next_skill"] = "human-review"
            state["next_action"] = final["next_action"]
            persist(session_dir, state, quality_receipt, final_receipt)
            return summary(state)
        state["revision_count"] += 1
        state["status"] = "awaiting-draft"
        state["expected_action"] = "submit-draft"
        route = get_artifact_payload(state, state.get("route_artifact_id"))
        state["next_skill"] = route["primary_skill"]
        state["next_action"] = clean_quality["next_action"]
        persist(session_dir, state, quality_receipt)
        return summary(state)

    if decision == "escalate":
        final = controller_escalation(state, "QUALITY_ESCALATED", clean_quality["next_action"])
    else:
        draft = get_artifact_payload(state, state.get("current_draft_artifact_id"))
        route = get_artifact_payload(state, state.get("route_artifact_id"))
        final_payload: dict[str, Any] = {
            "request_id": state["request_id"],
            "request": state["sanitized_request"],
            "draft": draft,
            "quality": clean_quality,
            "human_review_required": bool(route.get("human_review_required")),
            "revision_count": state["revision_count"],
        }
        if isinstance(state.get("session_context"), dict):
            final_payload["session"] = state["session_context"]
        final = GATE.final_gate(final_payload)
        if final.get("decision") == "revise" and state["revision_count"] >= MAX_REVISIONS:
            final = controller_escalation(state, "REVISION_LIMIT", "两次返修后最终门禁仍未通过，转人工复核")

    validate_envelope("gate", final)
    final_receipt = issue_receipt(state, "final-gate", final)
    state["final_artifact_id"] = final_receipt["artifact_id"]
    final_decision = final["decision"]
    if final_decision == "release":
        state["status"] = "ready-for-release"
        state["expected_action"] = "release"
        state["next_skill"] = "edu-safe-learning-department"
        state["next_action"] = "运行release并原样返回控制器授权的safe_output"
    elif final_decision == "revise":
        state["revision_count"] += 1
        state["status"] = "awaiting-draft"
        state["expected_action"] = "submit-draft"
        route = get_artifact_payload(state, state.get("route_artifact_id"))
        state["next_skill"] = route["primary_skill"]
        state["next_action"] = final["next_action"]
    elif final_decision == "block":
        state["status"] = "blocked"
        state["terminal_decision"] = "block"
        state["expected_action"] = "none"
        state["next_skill"] = None
        state["next_action"] = final["next_action"]
    else:
        state["status"] = "escalated"
        state["terminal_decision"] = "escalate"
        state["expected_action"] = "none"
        state["next_skill"] = "human-review"
        state["next_action"] = final["next_action"]
    persist(session_dir, state, quality_receipt, final_receipt)
    return summary(state)


def release_output(session_dir: Path, workflow_id: str) -> dict[str, Any]:
    state = load_state(session_dir, workflow_id)
    if state["status"] == "released":
        final = get_artifact_payload(state, state.get("final_artifact_id"))
        release_artifact = get_artifact_payload(state, state.get("release_artifact_id"))
        return {
            "schema_version": "1.0",
            "ok": True,
            "workflow_id": workflow_id,
            "status": "released",
            "safe_output": final["safe_output"],
            "release_receipt": release_artifact,
            "enforcement": ENFORCEMENT,
        }
    if state["status"] != "ready-for-release" or state["expected_action"] != "release":
        raise WorkflowError("RELEASE_NOT_AUTHORIZED", "工作流尚未取得最终发布授权", next_action=state["next_action"])
    final = get_artifact_payload(state, state.get("final_artifact_id"))
    if final.get("decision") != "release" or not isinstance(final.get("safe_output"), str) or not final["safe_output"].strip():
        raise WorkflowError("RELEASE_NOT_AUTHORIZED", "最终门禁回执不是有效release")
    final_receipt = state["receipts"][-1]
    authorization = {
        "workflow_id": workflow_id,
        "request_id": state["request_id"],
        "authorized_at_utc": utc_now(),
        "final_receipt_sha256": final_receipt["receipt_sha256"],
        "safe_output_sha256": hashlib.sha256(final["safe_output"].encode("utf-8")).hexdigest(),
        **ENFORCEMENT,
    }
    receipt = issue_receipt(state, "release-authorization", authorization)
    state["release_artifact_id"] = receipt["artifact_id"]
    state["status"] = "released"
    state["terminal_decision"] = "release"
    state["expected_action"] = "none"
    state["next_skill"] = None
    state["next_action"] = "仅原样返回safe_output，不添加未经门禁审查的内容"
    persist(session_dir, state, receipt)
    validate_receipt_chain(state)
    return {
        "schema_version": "1.0",
        "ok": True,
        "workflow_id": workflow_id,
        "status": "released",
        "safe_output": final["safe_output"],
        "release_receipt": authorization,
        "enforcement": ENFORCEMENT,
    }


def inspect_workflow(session_dir: Path, workflow_id: str, verify_only: bool = False) -> dict[str, Any]:
    state = load_state(session_dir, workflow_id)
    result = summary(state)
    result["integrity_verified"] = True
    if verify_only:
        result["verified_steps"] = [receipt["step"] for receipt in state["receipts"]]
    return result


def write_result(result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", required=True, help="Persistent, user-writable session directory")
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--input", required=True, help="request envelope JSON file or -")
    for name in ("submit-draft", "submit-quality"):
        command = commands.add_parser(name)
        command.add_argument("--workflow-id", required=True)
        command.add_argument("--input", required=True, help="envelope JSON file or -")
    for name in ("release", "status", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--workflow-id", required=True)
    args = parser.parse_args()
    session_dir = resolved_session_dir(args.session_dir)
    try:
        if args.command == "start":
            result = start_workflow(session_dir, load_json(args.input))
        elif args.command == "submit-draft":
            result = submit_draft(session_dir, args.workflow_id, load_json(args.input))
        elif args.command == "submit-quality":
            result = submit_quality(session_dir, args.workflow_id, load_json(args.input))
        elif args.command == "release":
            result = release_output(session_dir, args.workflow_id)
        else:
            result = inspect_workflow(session_dir, args.workflow_id, args.command == "verify")
        write_result(result)
        return 0
    except (WorkflowError, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, WorkflowError):
            code = exc.code
            next_action = exc.next_action
        else:
            code = "CONTROLLER_FAILURE"
            next_action = "修复输入或运行环境后从控制器恢复，不得直接发布模型答案"
        write_result({
            "schema_version": "1.0",
            "ok": False,
            "status": "blocked",
            "decision": "block",
            "error_code": code,
            "message": str(exc),
            "safe_output": "",
            "next_action": next_action,
            "enforcement": ENFORCEMENT,
        })
        return 2


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
