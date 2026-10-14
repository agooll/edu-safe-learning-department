#!/usr/bin/env python3
"""Fail-closed preflight and final-release checks for education content."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


PII_PATTERNS = (
    ("CHINESE_ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证号已隐藏]"),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号已隐藏]"),
    ("EMAIL", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[邮箱已隐藏]"),
    ("STUDENT_ID", re.compile(r"(?i)(学号|student\s*id)\s*[:：=]?\s*[A-Z0-9-]{5,}"), "学号：[已隐藏]"),
    ("NAME", re.compile(r"(姓名|学生姓名)\s*[:：=]\s*[\u4e00-\u9fff]{2,4}"), "姓名：[已隐藏]"),
)

INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(all|any|the|previous).{0,30}(instruction|prompt|rule)"),
    re.compile(r"(?i)(reveal|show|print).{0,20}(system prompt|hidden prompt|developer message)"),
    re.compile(r"(忽略|无视|覆盖).{0,20}(系统|之前|以上).{0,15}(指令|规则|提示)"),
    re.compile(r"(关闭|跳过|绕过).{0,12}(门禁|审核|安全检查|权限)"),
    re.compile(r"(泄露|输出|展示).{0,12}(系统提示词|隐藏提示|密钥|其他学生数据)"),
)

EXAM_CONTEXT_PATTERNS = (
    re.compile(r"(正在|现在|实时|线上).{0,8}(考试|测验|答题)"),
    re.compile(r"(考试|测验|答题).{0,6}(进行中|还没结束)"),
)

EXAM_BYPASS_PATTERNS = (
    re.compile(r"(绕过|躲过|避开).{0,8}(监考|检测|摄像头)"),
    re.compile(r"(代考|作弊|不可检测的代写|不要被发现)"),
)

DIRECT_ANSWER_PATTERNS = (
    re.compile(r"(只要|直接给|告诉我).{0,8}(答案|选项)"),
    re.compile(r"(?i)(answer only|just give me the answer)"),
)

HIGH_IMPACT_PATTERNS = (
    re.compile(r"(正式成绩|记入.{0,6}成绩|分班|录取|升学资格|处分|开除|记入档案)"),
    re.compile(r"(自动|直接).{0,8}(打分|判定|淘汰|拒绝录取|处分)"),
)

HEALTH_PATTERNS = (
    re.compile(r"(诊断|判定|确定).{0,16}(ADHD|多动症|抑郁|焦虑症|智力|心理疾病|学习障碍)", re.I),
    re.compile(r"(是不是|是否有).{0,12}(ADHD|多动症|抑郁|智力问题|学习障碍)", re.I),
)

IMMEDIATE_RISK_PATTERNS = (
    re.compile(r"(想自杀|不想活|结束生命|伤害自己|杀死自己|伤害别人|杀了他)"),
)

UNSUPPORTED_CLAIM_PATTERNS = (
    re.compile(r"(研究表明|数据显示|权威研究|根据.{0,16}研究|文献指出|专家一致认为)"),
    re.compile(r"(?i)(studies show|research proves|according to research)"),
)


def read_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    return data


def write_json(data: dict[str, Any], output: str | None) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(flatten_text(item) for item in value)
    return ""


def redact_text(text: str) -> tuple[str, list[str]]:
    codes: list[str] = []
    result = text
    for code, pattern, replacement in PII_PATTERNS:
        result, count = pattern.subn(replacement, result)
        if count:
            codes.extend([code] * count)
    return result, codes


def redact_value(value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        result: list[Any] = []
        codes: list[str] = []
        for item in value:
            clean, found = redact_value(item)
            result.append(clean)
            codes.extend(found)
        return result, codes
    if isinstance(value, dict):
        result_dict: dict[str, Any] = {}
        codes: list[str] = []
        for key, item in value.items():
            clean, found = redact_value(item)
            result_dict[key] = clean
            codes.extend(found)
        return result_dict, codes
    return value, []


def any_match(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def base_envelope(request_id: str, stage: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "stage": stage,
        "reason_codes": [],
        "risk_level": "low",
        "human_review_required": False,
        "redactions": [],
    }


def preflight(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "req-unspecified")
    result = base_envelope(request_id, "preflight")
    sanitized, redaction_codes = redact_value(request)
    result["sanitized_request"] = sanitized
    result["redactions"] = unique(redaction_codes)
    if redaction_codes:
        result["reason_codes"].append("PII_REDACTED")

    text = flatten_text(request)
    content = "\n".join(str(request.get(key) or "") for key in ("content", "goal", "student_response")).strip()
    if not content:
        result.update(decision="clarify", risk_level="medium", next_action="补充需要处理的教育任务或材料")
        result["reason_codes"].append("INPUT_EMPTY")
        return result

    if any_match(IMMEDIATE_RISK_PATTERNS, text):
        result.update(decision="escalate", risk_level="critical", human_review_required=True,
                      next_action="立即联系当地紧急服务、学校安全负责人或可信任成年人")
        result["reason_codes"].append("MINOR_IMMEDIATE_RISK")
        return result

    if any_match(INJECTION_PATTERNS, text):
        result.update(decision="block", risk_level="high", next_action="移除绕过规则或索取隐藏信息的指令后重试")
        result["reason_codes"].append("PROMPT_INJECTION")
        return result

    constraints = request.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    exam_context = constraints.get("active_exam") is True or any_match(EXAM_CONTEXT_PATTERNS, text)
    direct_answer = any_match(DIRECT_ANSWER_PATTERNS, text)
    exam_bypass = any_match(EXAM_BYPASS_PATTERNS, text)
    active_exam_assistance = exam_bypass or (exam_context and direct_answer)
    if active_exam_assistance:
        result.update(decision="block", risk_level="high",
                      next_action="改为请求概念提示、相似练习或考试结束后的讲解")
        result["reason_codes"].append("ACTIVE_EXAM_ASSISTANCE")
        return result

    if any_match(HEALTH_PATTERNS, text):
        result.update(decision="escalate", risk_level="high", human_review_required=True,
                      next_action="由具备资质的专业人员基于完整信息进行评估")
        result["reason_codes"].append("HEALTH_DIAGNOSIS")
        return result

    high_impact = any_match(HIGH_IMPACT_PATTERNS, text) or any(
        constraints.get(key) is True for key in ("formal_grade", "admission", "placement", "discipline")
    )
    if high_impact:
        result["reason_codes"].append("HIGH_IMPACT_DECISION")
        result["risk_level"] = "high"
        result["human_review_required"] = True
    elif redaction_codes:
        result["risk_level"] = "medium"

    result["decision"] = "allow"
    result["next_action"] = "将去标识化请求交给教育任务路由员"
    return result


def quality_decision(payload: dict[str, Any]) -> str | None:
    quality = payload.get("quality")
    if isinstance(quality, dict):
        value = quality.get("decision")
        return str(value).lower() if value else None
    value = payload.get("quality_decision")
    return str(value).lower() if value else None


def final_gate(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    request_id = str(payload.get("request_id") or request.get("request_id") or draft.get("request_id") or "req-unspecified")
    result = base_envelope(request_id, "final")
    revision_count = payload.get("revision_count", 0)
    revision_count = revision_count if isinstance(revision_count, int) and revision_count >= 0 else 0

    if quality_decision(payload) != "pass":
        exhausted = revision_count >= 2
        result.update(
            decision="escalate" if exhausted else "revise",
            risk_level="high" if exhausted else "medium",
            human_review_required=exhausted,
            safe_output="",
            next_action="转人工复核" if exhausted else "按质量问题清单修订草稿",
        )
        result["reason_codes"].append("REVISION_LIMIT" if exhausted else "QUALITY_NOT_PASSED")
        return result

    content = draft.get("content")
    if not isinstance(content, str) or not content.strip():
        exhausted = revision_count >= 2
        result.update(
            decision="escalate" if exhausted else "revise",
            risk_level="high" if exhausted else "medium",
            human_review_required=exhausted,
            safe_output="",
            next_action="转人工复核" if exhausted else "生成非空且完成任务的草稿",
        )
        result["reason_codes"].append("REVISION_LIMIT" if exhausted else "OUTPUT_EMPTY")
        return result

    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    if session.get("mode") == "continuous-learning":
        message_class = session.get("message_class")
        active_thread = session.get("active_thread")
        allowed_classes = {"main-line", "side-question", "resume", "goal-replacement"}
        mismatch = message_class not in allowed_classes or not isinstance(active_thread, str) or not active_thread
        if message_class == "side-question" and active_thread == "main":
            mismatch = True
        if message_class in {"main-line", "goal-replacement"} and active_thread != "main":
            mismatch = True
        if mismatch:
            result.update(decision="revise", risk_level="medium", safe_output="",
                          next_action="校正学习主线、插问分类和活动线程后重新执行门禁")
            result["reason_codes"].append("CONTEXT_STATE_MISMATCH")
            return result
        learner_state = session.get("state") if isinstance(session.get("state"), dict) else {}
        if draft.get("mastery_status") == "demonstrated" and learner_state.get("stage") != "completed":
            result.update(decision="revise", risk_level="medium", safe_output="",
                          next_action="移除无证据掌握结论并由学习阶段监控器验证")
            result["reason_codes"].append("UNSUPPORTED_MASTERY")
            return result

    clean_content, redaction_codes = redact_text(content)
    result["redactions"] = unique(redaction_codes)
    if redaction_codes:
        result["reason_codes"].append("PII_REDACTED")

    request_text = flatten_text(request)
    draft_text = flatten_text(draft)
    constraints = request.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}

    exam_context = constraints.get("active_exam") is True or any_match(EXAM_CONTEXT_PATTERNS, request_text)
    direct_answer = any_match(DIRECT_ANSWER_PATTERNS, request_text)
    exam_bypass = any_match(EXAM_BYPASS_PATTERNS, request_text)
    if exam_bypass or ((exam_context and direct_answer) and any(word in draft_text for word in ("答案", "选项", "answer"))):
        result.update(decision="block", risk_level="high", safe_output="",
                      next_action="提供概念提示、相似练习或考后讲解")
        result["reason_codes"].append("ACTIVE_EXAM_ASSISTANCE")
        return result

    if any_match(HEALTH_PATTERNS, draft_text):
        result.update(decision="escalate", risk_level="high", human_review_required=True,
                      safe_output="", next_action="由具备资质的专业人员复核")
        result["reason_codes"].append("HEALTH_DIAGNOSIS")
        return result

    evidence_refs = draft.get("evidence_refs")
    evidence_refs = evidence_refs if isinstance(evidence_refs, list) else []
    if any_match(UNSUPPORTED_CLAIM_PATTERNS, content) and not evidence_refs:
        result.update(decision="revise", risk_level="medium", safe_output="",
                      next_action="删除无法核验的研究断言或补充可追溯依据")
        result["reason_codes"].append("UNSUPPORTED_CLAIM")
        return result

    grading_language = any(word in content for word in ("得分", "分数", "评分", "满分"))
    has_rubric = bool(request.get("rubric") or request.get("reference_answer") or draft.get("rubric_used") is True)
    if grading_language and not has_rubric:
        result.update(decision="revise", risk_level="medium", safe_output="",
                      next_action="补充评分标准或移除无依据的精确评分")
        result["reason_codes"].append("RUBRIC_MISSING")
        return result

    high_impact = any_match(HIGH_IMPACT_PATTERNS, request_text + "\n" + draft_text) or any(
        constraints.get(key) is True for key in ("formal_grade", "admission", "placement", "discipline")
    )
    human_review = high_impact or bool(payload.get("human_review_required")) or bool(draft.get("human_review_required"))
    if human_review:
        result.update(decision="escalate", risk_level="high", human_review_required=True,
                      safe_output=clean_content, next_action="由授权教师或管理者复核后决定是否采用")
        result["reason_codes"].append("HIGH_IMPACT_DECISION")
        return result

    result.update(
        decision="release",
        risk_level="medium" if redaction_codes else "low",
        human_review_required=False,
        safe_output=clean_content,
        next_action="发布去标识化结果并保留一个可执行下一步",
    )
    return result


def append_audit(path: str | None, result: dict[str, Any]) -> None:
    if not path:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": result.get("request_id"),
        "stage": result.get("stage"),
        "decision": result.get("decision"),
        "reason_codes": result.get("reason_codes", []),
        "risk_level": result.get("risk_level"),
        "human_review_required": result.get("human_review_required", False),
    }
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def error_envelope(stage: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": "req-unspecified",
        "stage": stage,
        "decision": "escalate",
        "reason_codes": ["INVALID_INPUT"],
        "risk_level": "high",
        "human_review_required": True,
        "redactions": [],
        "safe_output": "",
        "message": message,
        "next_action": "提供有效的 UTF-8 JSON 并重新执行门禁",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("preflight", "final"))
    parser.add_argument("--input", required=True, help="UTF-8 JSON file or - for stdin")
    parser.add_argument("--output", help="Optional UTF-8 JSON output path")
    parser.add_argument("--audit-log", help="Optional append-only JSONL path; raw content is never logged")
    args = parser.parse_args()
    try:
        payload = read_json(args.input)
        result = preflight(payload) if args.stage == "preflight" else final_gate(payload)
        append_audit(args.audit_log, result)
        write_json(result, args.output)
        return 0
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        result = error_envelope(args.stage, str(exc))
        append_audit(args.audit_log, result)
        write_json(result, args.output)
        return 2


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
