#!/usr/bin/env python3
"""Create a deterministic single-owner route for an education request."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


TASK_SKILLS = {
    "teaching": "edu-teaching-designer",
    "assessment": "edu-assessment-reviewer",
    "diagnosis": "edu-learning-diagnostician",
    "personalization": "edu-personalized-planner",
    "retention-review": "edu-spaced-review-scheduler",
}

ROLE_ALIASES = {
    "student": "student",
    "学生": "student",
    "learner": "student",
    "teacher": "teacher",
    "教师": "teacher",
    "老师": "teacher",
    "guardian": "guardian",
    "parent": "guardian",
    "家长": "guardian",
    "监护人": "guardian",
    "administrator": "administrator",
    "admin": "administrator",
    "教育管理者": "administrator",
    "管理员": "administrator",
    "教务": "administrator",
}

ROLE_WORKFLOWS = {
    "student": "continuous-learning",
    "teacher": "teacher-workbench",
    "guardian": "guardian-support",
    "administrator": "governance-review",
}

ALIASES = {
    "教学": "teaching",
    "备课": "teaching",
    "题目生成": "teaching",
    "批改": "assessment",
    "评阅": "assessment",
    "评分": "assessment",
    "诊断": "diagnosis",
    "学情": "diagnosis",
    "错因": "diagnosis",
    "个性化": "personalization",
    "规划": "personalization",
    "推荐": "personalization",
    "复习": "retention-review",
    "保持性复习": "retention-review",
}

KEYWORDS = {
    "teaching": ("教案", "课件", "备课", "教学设计", "知识点", "出题", "试题", "例题", "lesson", "quiz"),
    "assessment": ("批改", "评阅", "评分", "判分", "作业", "作文", "答卷", "grade", "mark"),
    "diagnosis": ("学情", "错因", "薄弱", "错误类型", "知识漏洞", "诊断", "进度", "diagnos"),
    "personalization": ("个性化", "学习计划", "学习路径", "习题推荐", "自适应", "规划", "陪练", "recommend"),
    "retention-review": ("到期复习", "保持性复习", "该复习", "复习提醒", "跳过复习", "已经会了", "review due", "skip review"),
}

NEXT_ACTIONS = {
    "teaching": "生成与学段、目标和时长对齐的教学草稿",
    "assessment": "依据评分标准逐项绑定作答证据并形成候选反馈",
    "diagnosis": "从作答证据提出可验证的错因与知识点假设",
    "personalization": "生成含里程碑、通过标准和复盘点的学习路径",
    "retention-review": "校验笔记时间戳并判断复习是否到期",
}


def read_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    return data


def write_json(data: dict[str, Any], output: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def text_of(request: dict[str, Any]) -> str:
    values = [request.get("content"), request.get("goal"), request.get("student_response")]
    return "\n".join(str(value) for value in values if value).lower()


def normalize_explicit_task(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in TASK_SKILLS:
        return lowered
    return ALIASES.get(lowered)


def invalid_explicit_task(value: Any) -> bool:
    if value is None or value == "":
        return False
    if not isinstance(value, str):
        return True
    return value.strip().lower() != "auto" and normalize_explicit_task(value) is None


def normalize_role(request: dict[str, Any]) -> str | None:
    actor = request.get("actor")
    if not isinstance(actor, dict) or not isinstance(actor.get("role"), str):
        return None
    return ROLE_ALIASES.get(actor["role"].strip().lower())


def infer_task(request: dict[str, Any]) -> tuple[str | None, list[str]]:
    explicit = normalize_explicit_task(request.get("task_type"))
    if explicit:
        return explicit, []

    text = text_of(request)
    scores = {task: sum(text.count(word) for word in words) for task, words in KEYWORDS.items()}
    positive = [task for task, score in scores.items() if score > 0]
    if not positive:
        return None, []

    # Assessment creates evidence used by diagnosis and planning, so it owns mixed grading requests.
    precedence = ("retention-review", "assessment", "diagnosis", "personalization", "teaching")
    best = max(scores.values())
    tied = [task for task in precedence if scores[task] == best]
    primary = tied[0]
    supporting = [task for task in precedence if task != primary and scores[task] > 0][:2]
    return primary, supporting


def missing_fields(task: str, request: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    actor = request.get("actor")
    if not isinstance(actor, dict) or not actor.get("role"):
        missing.append("actor.role")

    if task == "teaching":
        if not request.get("subject"):
            missing.append("subject")
        if not request.get("grade_level"):
            missing.append("grade_level")
        if not request.get("goal") and not request.get("content"):
            missing.append("goal")
    elif task == "assessment":
        if not request.get("student_response"):
            missing.append("student_response")
        if not request.get("rubric") and not request.get("reference_answer"):
            missing.append("rubric_or_reference_answer")
    elif task == "diagnosis":
        if not request.get("student_response") and not request.get("evidence"):
            missing.append("student_response_or_evidence")
    elif task == "personalization":
        if not request.get("goal"):
            missing.append("goal")
        constraints = request.get("constraints")
        constraints = constraints if isinstance(constraints, dict) else {}
        if not any(constraints.get(key) for key in ("duration_minutes", "weekly_minutes", "deadline", "available_time")):
            missing.append("available_time_or_deadline")
    return missing


def is_high_impact(request: dict[str, Any]) -> bool:
    constraints = request.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    if any(constraints.get(key) is True for key in ("formal_grade", "admission", "placement", "discipline")):
        return True
    return any(word in text_of(request) for word in ("正式成绩", "录取", "分班", "处分", "记入档案"))


def individual_records_requested(request: dict[str, Any]) -> bool:
    constraints = request.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    scope = request.get("data_scope", constraints.get("data_scope"))
    if isinstance(scope, str):
        normalized = scope.strip().lower()
        if normalized == "aggregate":
            return False
        if normalized == "individual":
            return True
    return bool(request.get("student_response") or request.get("evidence"))


def role_missing_fields(role: str, request: dict[str, Any]) -> list[str]:
    consent = request.get("consent")
    consent = consent if isinstance(consent, dict) else {}
    contains_individual_records = individual_records_requested(request)
    missing: list[str] = []
    if role == "guardian" and contains_individual_records and consent.get("guardian_authorized") is not True:
        missing.append("consent.guardian_authorized")
    if role == "administrator" and contains_individual_records and consent.get("admin_authorized") is not True:
        missing.append("consent.admin_authorized")
    return missing


def route(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "req-unspecified")
    role = normalize_role(request)
    if role is None:
        return {
            "schema_version": "1.0",
            "request_id": request_id,
            "status": "clarify",
            "actor_role": "unknown",
            "workflow_mode": None,
            "task_type": "unknown",
            "primary_skill": None,
            "supporting_skills": [],
            "missing_fields": ["actor.role"],
            "supported_roles": sorted(ROLE_WORKFLOWS),
            "risk_level": "medium",
            "human_review_required": False,
            "next_action": "确认当前身份是学生、教师、监护人还是教育管理者",
        }
    if invalid_explicit_task(request.get("task_type")):
        return {
            "schema_version": "1.0",
            "request_id": request_id,
            "status": "clarify",
            "actor_role": role,
            "workflow_mode": ROLE_WORKFLOWS[role],
            "task_type": "unknown",
            "primary_skill": None,
            "supporting_skills": [],
            "missing_fields": ["task_type"],
            "risk_level": "medium",
            "human_review_required": False,
            "next_action": "把 task_type 改为 teaching、assessment、diagnosis、personalization、retention-review 或 auto",
        }
    task, supporting_tasks = infer_task(request)
    if task is None and role == "student" and (request.get("content") or request.get("goal")):
        task = "teaching"
    if task is None and role == "guardian" and (request.get("content") or request.get("goal")):
        task = "personalization"
    if task is None:
        return {
            "schema_version": "1.0",
            "request_id": request_id,
            "status": "clarify",
            "actor_role": role,
            "workflow_mode": ROLE_WORKFLOWS[role],
            "task_type": "unknown",
            "primary_skill": None,
            "supporting_skills": [],
            "missing_fields": ["task_type"],
            "risk_level": "medium",
            "human_review_required": False,
            "next_action": "确认需要教学设计、作业评阅、学情诊断还是学习规划",
        }

    missing = missing_fields(task, request) + role_missing_fields(role, request)
    high_impact = is_high_impact(request)
    risk_flags = request.get("risk_flags") if isinstance(request.get("risk_flags"), list) else []
    risk_level = "high" if high_impact else ("medium" if risk_flags else "low")
    status = "clarify" if missing else "routed"
    individual_records = individual_records_requested(request)
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "status": status,
        "actor_role": role,
        "workflow_mode": ROLE_WORKFLOWS[role],
        "task_type": task,
        "primary_skill": TASK_SKILLS[task] if status == "routed" else None,
        "supporting_skills": [TASK_SKILLS[item] for item in supporting_tasks],
        "missing_fields": missing,
        "risk_level": risk_level,
        "human_review_required": high_impact
        or (role == "administrator" and individual_records)
        or bool(request.get("human_review_required")),
        "next_action": NEXT_ACTIONS[task] if status == "routed" else f"补充必要信息：{', '.join(missing)}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="UTF-8 JSON file or - for stdin")
    parser.add_argument("--output", help="Optional UTF-8 JSON output path")
    args = parser.parse_args()
    try:
        result = route(read_json(args.input))
        write_json(result, args.output)
        return 0
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        write_json({
            "schema_version": "1.0",
            "status": "error",
            "error_code": "INVALID_INPUT",
            "message": str(exc),
            "next_action": "提供有效的 UTF-8 JSON 请求对象",
        }, args.output)
        return 2


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
