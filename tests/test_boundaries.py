from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    path = SKILL / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


validator = load_module("boundary_validator", "scripts/validate_handoff.py")
learning_state = load_module(
    "boundary_learning_state",
    "subskills/edu-learning-stage-monitor/scripts/learning_state.py",
)
context_stack = load_module(
    "boundary_context_stack",
    "subskills/edu-context-resume/scripts/context_stack.py",
)
note_capture = load_module(
    "boundary_note_capture",
    "subskills/edu-learning-note-capture/scripts/capture_note.py",
)
review_schedule = load_module(
    "boundary_review_schedule",
    "subskills/edu-spaced-review-scheduler/scripts/review_schedule.py",
)


def completed_session(session_dir: Path, reminder_policy: str = "scheduled") -> dict:
    state = {
        "schema_version": "1.0",
        "session_id": "session-boundary",
        "goal_id": "goal-abcdef123456",
        "goal": "理解一次函数斜率",
        "topic": "一次函数",
        "stage": "completed",
        "stage_status": "completed",
        "mastery_status": "demonstrated",
        "note_policy": "on-completion",
        "reminder_policy": reminder_policy,
        "next_skill": "edu-learning-note-capture",
        "next_action": "生成学习笔记",
    }
    note_capture.atomic_write_json(session_dir / "state.json", state)
    note_capture.atomic_write_json(
        session_dir / "checkpoint-stack.json",
        {
            "schema_version": "1.0",
            "active_thread": "main",
            "thread_stack": [],
            "next_branch_number": 1,
        },
    )
    return state


def note_summary() -> dict:
    return json.loads((SKILL / "examples" / "note-summary.json").read_text(encoding="utf-8"))


class ContractBoundaryTests(unittest.TestCase):
    def test_state_contract_rejects_invalid_reminder_policy(self):
        state = completed_session_data = {
            "schema_version": "1.0",
            "session_id": "session-contract",
            "goal_id": "goal-abcdef123456",
            "goal": "理解斜率",
            "stage": "goal-setup",
            "stage_status": "active",
            "mastery_status": "not-demonstrated",
            "note_policy": "ask",
            "reminder_policy": "silently-enable",
            "next_skill": "edu-personalized-planner",
            "next_action": "确认学习目标",
        }
        errors = validator.validate("state", state)
        self.assertIn("reminder_policy has an unsupported value", errors)
        self.assertEqual(completed_session_data["reminder_policy"], "silently-enable")


class RouterRoleBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = load_module(
            "boundary_router",
            "subskills/edu-request-router/scripts/route_request.py",
        )

    def test_explicit_invalid_task_type_does_not_fall_back_to_keywords(self):
        result = self.router.route(
            {
                "request_id": "req-invalid-task",
                "actor": {"role": "teacher"},
                "task_type": "delete-records",
                "content": "生成一份数学教案",
                "subject": "数学",
                "grade_level": "八年级",
            }
        )
        self.assertEqual(result["status"], "clarify")
        self.assertIsNone(result["primary_skill"])
        self.assertEqual(result["missing_fields"], ["task_type"])

    def test_admin_aggregate_evidence_does_not_require_individual_authorization(self):
        result = self.router.route(
            {
                "request_id": "req-admin-aggregate",
                "actor": {"role": "administrator"},
                "task_type": "diagnosis",
                "content": "分析去标识化班级统计摘要",
                "data_scope": "aggregate",
                "evidence": ["全班 42 人的知识点正确率统计"],
            }
        )
        self.assertEqual(result["status"], "routed")
        self.assertNotIn("consent.admin_authorized", result["missing_fields"])
        self.assertFalse(result["human_review_required"])

    def test_admin_individual_evidence_requires_authorization_and_review(self):
        request = {
            "request_id": "req-admin-individual",
            "actor": {"role": "administrator"},
            "task_type": "diagnosis",
            "content": "分析一名学生的学习记录",
            "data_scope": "individual",
            "evidence": ["去标识化个体作答摘要"],
        }
        blocked = self.router.route(request)
        self.assertEqual(blocked["status"], "clarify")
        self.assertIn("consent.admin_authorized", blocked["missing_fields"])
        request["consent"] = {"admin_authorized": True}
        allowed = self.router.route(request)
        self.assertEqual(allowed["status"], "routed")
        self.assertTrue(allowed["human_review_required"])


class ContextBoundaryTests(unittest.TestCase):
    @staticmethod
    def snapshot() -> dict:
        return {
            "schema_version": "1.0",
            "goal": "理解一次函数",
            "stage": "guided-practice",
            "current_topic": "斜率",
            "completed_steps": ["完成基线诊断"],
            "confirmed_understanding": ["能识别变量"],
            "evidence_ids": ["evidence-001"],
            "last_exchange": {"question_summary": "斜率是什么", "learner_response_summary": "变化率"},
            "next_skill": "edu-teaching-designer",
            "next_action": "完成一道引导练习",
        }

    def test_side_question_accepts_1000_characters_and_rejects_1001(self):
        with tempfile.TemporaryDirectory() as folder:
            accepted = context_stack.push_branch(Path(folder) / "accepted", "问" * 1000, self.snapshot())
            self.assertEqual(accepted["branch_depth"], 1)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(context_stack.ContextError) as rejected:
                context_stack.push_branch(Path(folder) / "rejected", "问" * 1001, self.snapshot())
            self.assertEqual(rejected.exception.code, "QUESTION_TOO_LONG")

    def test_eight_nested_questions_are_allowed_and_ninth_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            session_dir = Path(folder)
            for index in range(8):
                result = context_stack.push_branch(session_dir, f"插问 {index + 1}", self.snapshot())
                self.assertEqual(result["branch_depth"], index + 1)
            with self.assertRaises(context_stack.ContextError) as rejected:
                context_stack.push_branch(session_dir, "第九层插问", self.snapshot())
            self.assertEqual(rejected.exception.code, "BRANCH_LIMIT")


class EvidenceBoundaryTests(unittest.TestCase):
    def test_learning_evidence_accepts_500_characters_and_rejects_501(self):
        with tempfile.TemporaryDirectory() as folder:
            session_dir = Path(folder)
            learning_state.init_session(session_dir, "理解斜率", "一次函数", "session-boundary")
            accepted = learning_state.record_evidence(
                session_dir, "goal-confirmed", "证" * 500, "learner-response", "main"
            )
            self.assertTrue(accepted["ok"])
            with self.assertRaises(learning_state.StateError) as rejected:
                learning_state.record_evidence(
                    session_dir, "goal-confirmed", "证" * 501, "learner-response", "main"
                )
            self.assertEqual(rejected.exception.code, "EVIDENCE_INVALID")


class NoteTransactionTests(unittest.TestCase):
    def test_invalid_index_rolls_back_every_new_note_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            session_dir = Path(folder)
            state = completed_session(session_dir)
            notes_dir = session_dir / "notes"
            note_capture.atomic_write_json(
                notes_dir / "index.json", {"schema_version": "1.0", "notes": "corrupt"}
            )
            with self.assertRaises(note_capture.NoteError) as rejected:
                note_capture.capture(session_dir, note_summary(), False)
            self.assertEqual(rejected.exception.code, "INDEX_INVALID")
            goal_id = state["goal_id"]
            self.assertFalse((notes_dir / f"{goal_id}.md").exists())
            self.assertFalse((notes_dir / f"{goal_id}.receipt.json").exists())
            self.assertFalse((notes_dir / f"{goal_id}.reminders.json").exists())
            self.assertFalse((notes_dir / "review-schedule.json").exists())
            self.assertFalse((session_dir / "runtime-events.jsonl").exists())


class ReminderBoundaryTests(unittest.TestCase):
    def test_one_platform_task_id_cannot_bind_multiple_checkpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            session_dir = Path(folder)
            completed_session(session_dir)
            captured = note_capture.capture(session_dir, note_summary(), False)
            goal_id = captured["goal_id"]
            first = review_schedule.bind_reminder(session_dir, goal_id, "r01", "platform-task-001")
            self.assertEqual(first["status"], "registered")
            with self.assertRaises(review_schedule.ReviewError) as rejected:
                review_schedule.bind_reminder(session_dir, goal_id, "r02", "platform-task-001")
            self.assertEqual(rejected.exception.code, "REMINDER_TASK_ID_REUSED")

    def test_duplicate_review_choice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            session_dir = Path(folder)
            completed_session(session_dir)
            captured = note_capture.capture(session_dir, note_summary(), False)
            goal_id = captured["goal_id"]
            due_at = review_schedule.parse_utc(captured["recorded_at_utc"]) + timedelta(minutes=20)
            first = review_schedule.choose_review(session_dir, goal_id, "review", due_at)
            repeated = review_schedule.choose_review(
                session_dir, goal_id, "review", due_at + timedelta(seconds=1)
            )
            self.assertEqual(first["status"], "review-started")
            self.assertEqual(repeated["status"], "review-already-started")
            events = [
                json.loads(line)
                for line in (session_dir / "review-events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            choices = [event for event in events if event.get("event") == "retention_review_choice"]
            self.assertEqual(len(choices), 1)


if __name__ == "__main__":
    unittest.main()
