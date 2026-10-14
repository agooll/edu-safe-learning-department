#!/usr/bin/env python3
"""Run a no-network demonstration of stage gating and side-question recovery."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


SKILL_ROOT = Path(__file__).resolve().parents[1]
STATE_SCRIPT = SKILL_ROOT / "subskills" / "edu-learning-stage-monitor" / "scripts" / "learning_state.py"
CONTEXT_SCRIPT = SKILL_ROOT / "subskills" / "edu-context-resume" / "scripts" / "context_stack.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


def main() -> int:
    learning_state = load_module("demo_learning_state", STATE_SCRIPT)
    context_stack = load_module("demo_context_stack", CONTEXT_SCRIPT)
    trace = []
    with tempfile.TemporaryDirectory(prefix="edu-learning-demo-") as folder:
        session_dir = Path(folder) / ".edu-learning-session"
        initialized = learning_state.init_session(session_dir, "理解一次函数斜率", "一次函数", "session-demo")
        trace.append({"event": "session_initialized", "stage": initialized["state"]["stage"]})

        learning_state.record_evidence(session_dir, "goal-confirmed", "已确认目标和通过标准", "learner-confirmation", None)
        baseline = learning_state.advance(session_dir, "baseline-diagnosis")
        trace.append({"event": "main_advanced", "stage": baseline["state"]["stage"]})

        state = baseline["state"]
        snapshot = {
            "schema_version": "1.0",
            "goal": state["goal"],
            "stage": state["stage"],
            "current_topic": "完成斜率基线诊断",
            "completed_steps": ["确认学习目标"],
            "confirmed_understanding": [],
            "evidence_ids": [],
            "last_exchange": {
                "question_summary": "确认学习目标",
                "learner_response_summary": "希望理解斜率并解决情境题"
            },
            "next_skill": state["next_skill"],
            "next_action": state["next_action"],
        }
        branch = context_stack.push_branch(session_dir, "顺便问一下，横截距是什么？", snapshot)
        trace.append({"event": "side_question_pushed", "active_thread": branch["active_thread"]})

        try:
            learning_state.advance(session_dir, "explanation")
        except learning_state.StateError as exc:
            trace.append({"event": "main_advance_blocked", "error_code": exc.code})

        summary = {
            "answer_summary": "解释了横截距是图像与横轴交点的横坐标",
            "impact_on_main": "帮助区分斜率与截距，不改变主线阶段",
            "unresolved_points": [],
            "safe_to_resume": True,
        }
        restored = context_stack.pop_branch(session_dir, summary)
        trace.append({
            "event": "main_restored",
            "active_thread": restored["active_thread"],
            "next_action": restored["next_action"],
        })

        learning_state.record_evidence(session_dir, "diagnostic-response", "完成基线题并暴露斜率与截距混淆", "learner-response", None)
        explanation = learning_state.advance(session_dir, "explanation")
        trace.append({"event": "main_advanced", "stage": explanation["state"]["stage"]})

        result = {
            "ok": True,
            "trace": trace,
            "final_stage": explanation["state"]["stage"],
            "active_thread": restored["active_thread"],
            "next_skill": explanation["state"]["next_skill"],
            "next_action": explanation["state"]["next_action"],
        }
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
