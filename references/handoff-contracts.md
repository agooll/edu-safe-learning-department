# 子 Skill 交接协议

所有交接使用 UTF-8 JSON。未知字段可保留，但不得覆盖上游风险标记。时间使用 ISO 8601；用户内容仅放在 `payload`，不得写入日志。

## 目录

- `request_envelope`：教育任务请求
- `route_envelope`：唯一主责路由
- `draft_envelope`：业务候选草稿
- `quality_envelope`：独立质量审计
- `gate_envelope`：生成前和发布前门禁
- `session_context`：最终门禁使用的学习线程状态
- `learning_state`：持续学习阶段
- `context_snapshot`：插问前主线快照
- `resume_context`：插问结束后的恢复信息
- `note_summary`：完成模块后的安全笔记摘要
- `timestamp_receipt`：结课笔记的本地完整性时间凭据
- `retention_review_state`：结课后的长期复习调度状态

## request_envelope

```json
{
  "schema_version": "1.0",
  "request_id": "req-demo-001",
  "actor": {"role": "teacher"},
  "task_type": "teaching|assessment|diagnosis|personalization|retention-review|auto",
  "subject": "数学",
  "grade_level": "八年级",
  "goal": "理解一次函数斜率",
  "content": "用户原始任务或待处理材料",
  "student_response": "仅在评阅或诊断时提供",
  "reference_answer": "可选",
  "rubric": {"可选评分维度": "规则"},
  "evidence": ["已去标识化证据"],
  "data_scope": "aggregate|individual",
  "constraints": {"duration_minutes": 45, "active_exam": false},
  "consent": {"student_data_authorized": false}
}
```

必填字段为 `request_id`、`actor.role` 和能表达任务的 `content` 或 `goal`。业务 Skill 根据任务类型要求额外字段。

`data_scope` 在监护人或教育管理者提交记录、作答或证据时使用。`aggregate` 只允许去标识化群体统计；`individual` 表示个体记录并触发相应授权检查。字段缺失时只要请求携带 `student_response` 或 `evidence`，按更安全的 `individual` 处理，不根据“班级”“统计”等自然语言自行放宽权限。

## route_envelope

```json
{
  "schema_version": "1.0",
  "request_id": "req-demo-001",
  "status": "routed|clarify|blocked|error",
  "actor_role": "student|teacher|guardian|administrator",
  "workflow_mode": "continuous-learning|teacher-workbench|guardian-support|governance-review",
  "task_type": "teaching",
  "primary_skill": "edu-teaching-designer",
  "supporting_skills": [],
  "missing_fields": [],
  "risk_level": "low|medium|high",
  "human_review_required": false,
  "next_action": "生成与目标对齐的教学草稿"
}
```

`primary_skill` 和 `next_action` 在 `status: routed` 时必填。主责 Skill 只能有一个。

## draft_envelope

```json
{
  "schema_version": "1.0",
  "request_id": "req-demo-001",
  "producer_skill": "edu-teaching-designer",
  "status": "draft|needs_clarification|blocked",
  "content": "候选教育内容",
  "evidence_refs": ["material-1#paragraph-2"],
  "assumptions": [],
  "limitations": [],
  "confidence": "high|medium|low",
  "rubric_used": false,
  "human_review_required": false,
  "next_action": "提交独立质量审计"
}
```

不得把 `confidence` 当成事实正确率。评分任务必须明确 `rubric_used`。

## quality_envelope

```json
{
  "schema_version": "1.0",
  "request_id": "req-demo-001",
  "decision": "pass|revise|escalate",
  "checks": {
    "complete": true,
    "traceable": true,
    "pedagogically_appropriate": true,
    "actionable": true,
    "uncertainty_disclosed": true
  },
  "issues": [],
  "next_action": "提交最终安全门禁"
}
```

`issues` 中每项包含 `code`、`severity`、`evidence`、`repair_instruction`。

## gate_envelope

```json
{
  "schema_version": "1.0",
  "request_id": "req-demo-001",
  "stage": "preflight|final",
  "decision": "allow|clarify|release|revise|block|escalate",
  "reason_codes": [],
  "risk_level": "low|medium|high|critical",
  "human_review_required": false,
  "redactions": [],
  "sanitized_request": {},
  "safe_output": "",
  "next_action": ""
}
```

最终发布必须同时满足：质量审计为 `pass`、门禁为 `release`、`safe_output` 非空。高影响决定即使内容安全也保持 `human_review_required: true`，由总控转人工复核。

## session_context

持续学习草稿进入最终门禁时附带：

```json
{
  "mode": "continuous-learning",
  "message_class": "side-question",
  "active_thread": "branch-001",
  "state": {
    "stage": "guided-practice",
    "mastery_status": "not-demonstrated"
  }
}
```

`side-question` 必须对应非 `main` 线程；`main-line` 和 `goal-replacement` 必须对应 `main`。不一致时最终门禁返回 `CONTEXT_STATE_MISMATCH`。

## learning_state

```json
{
  "schema_version": "1.0",
  "session_id": "session-demo-001",
  "goal_id": "goal-demo-001",
  "goal": "理解一次函数斜率",
  "topic": "一次函数",
  "stage": "guided-practice",
  "stage_status": "active",
  "mastery_status": "not-demonstrated",
  "note_policy": "ask",
  "reminder_policy": "ask",
  "next_skill": "edu-teaching-designer",
  "next_action": "完成一道与当前目标对齐的引导练习"
}
```

只有 `edu-learning-stage-monitor` 可以修改阶段、掌握状态、提醒同意策略和学习下一动作。所有阶段转换需要 `evidence-log.jsonl` 中同一 `goal_id`、当前阶段、`thread_id: main` 的指定证据。

## context_snapshot

```json
{
  "schema_version": "1.0",
  "goal": "理解一次函数斜率",
  "stage": "guided-practice",
  "current_topic": "解释单位变化率",
  "completed_steps": ["完成基线诊断"],
  "confirmed_understanding": ["能区分变化量与总量"],
  "evidence_ids": ["evidence-001"],
  "last_exchange": {
    "question_summary": "斜率 2 表示什么",
    "learner_response_summary": "每增加 1 千米费用增加 2 元"
  },
  "next_skill": "edu-teaching-designer",
  "next_action": "完成一道单位变化率迁移题"
}
```

快照必须先经过隐私门禁，只保存摘要。不得保存完整聊天记录、正式成绩档案或不必要的个人信息。

## resume_context

恢复结果包含完整 `context_snapshot`，并增加：

```json
{
  "side_question": "为什么横截距不一定是零？",
  "branch_summary": {
    "answer_summary": "解释了横截距的现实意义",
    "impact_on_main": "帮助区分斜率与截距，不改变阶段",
    "unresolved_points": [],
    "safe_to_resume": true
  },
  "restored_thread": "main",
  "closed_thread": "branch-001",
  "remaining_depth": 0
}
```

恢复后严格采用快照中的 `next_skill` 和 `next_action`，不得根据插问自动推进主线。

## note_summary

```json
{
  "schema_version": "1.0",
  "completed_steps": ["完成基线诊断", "完成引导练习"],
  "confirmed_understanding": ["能解释斜率的单位意义"],
  "misconceptions_corrected": ["区分斜率和截距"],
  "side_question_summaries": ["插问解释了横截距，不改变主线阶段"],
  "transfer_summary": "能够在新情境中识别单位变化率",
  "review_summary": "延迟复习通过",
  "next_recommendation": "用图像和表格表达同一函数"
}
```

只有学习阶段为 `completed`、掌握状态为 `demonstrated` 且没有活动插问分支时，才能进入结课笔记写入。`note_policy: ask` 必须取得明确确认。

## timestamp_receipt

```json
{
  "schema_version": "1.0",
  "goal_id": "goal-demo-001",
  "note_path": "goal-demo-001.md",
  "recorded_at_utc": "2026-08-01T08:00:00Z",
  "clock_source": "system-utc",
  "verification_level": "local-integrity",
  "note_sha256": "<64位十六进制摘要>",
  "receipt_sha256": "<64位十六进制摘要>"
}
```

调度复习前必须校验凭据摘要、笔记摘要、笔记内嵌时间和 `runtime-events.jsonl` 中的对应审计事件。`local-integrity` 只表示本地记录一致；未接入第三方签时服务时不得宣称外部权威时间。

## review_reminder_manifest

```json
{
  "schema_version": "1.0",
  "goal_id": "goal-demo-001",
  "anchor_timestamp_utc": "2026-08-01T08:00:00Z",
  "receipt_sha256": "<64位十六进制摘要>",
  "reminder_policy": "scheduled",
  "scheduler_registration_required": true,
  "schedule_sha256": "<64位十六进制摘要>",
  "tasks": [
    {
      "reminder_id": "goal-demo-001-r01",
      "checkpoint_id": "r01",
      "checkpoint_label": "20分钟",
      "run_at_utc": "2026-08-01T08:20:00Z",
      "skill_name": "edu-safe-learning-department",
      "prompt": "<最小化安全调用提示>",
      "registration_status": "pending-registration",
      "external_task_id": null
    }
  ]
}
```

清单必须包含七项。`external_task_id` 只能来自平台创建任务的成功回执；`scheduled` 以外的策略禁止绑定，且生成清单本身不能作为已注册证据。

## retention_review_state

```json
{
  "schema_version": "2.0",
  "goal_id": "goal-demo-001",
  "anchor_timestamp_utc": "2026-08-01T08:00:00Z",
  "checkpoint_index": 0,
  "next_checkpoint_id": "r01",
  "next_review_at_utc": "2026-08-01T08:20:00Z",
  "review_count": 0,
  "skipped_count": 0,
  "status": "scheduled",
  "pending_decision": null,
  "last_outcome": null,
  "last_evidence_id": null
}
```

检查点从笔记时间绝对计算为 20 分钟、1 小时、9 小时、1 天、2 天、6 天、31 天。到期必须接受 `review` 或 `skip`：`review` 通过需要证据；`skip` 记录用户选择并推进检查点，但必须保持 `mastery_claimed: false`。`review-gap` 保留当前检查点并回流诊断或补救。
