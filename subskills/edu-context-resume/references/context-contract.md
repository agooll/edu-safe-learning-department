# 插问上下文协议

## context_snapshot

```json
{
  "schema_version": "1.0",
  "goal": "理解一次函数斜率",
  "stage": "guided-practice",
  "current_topic": "解释单位变化率",
  "completed_steps": ["完成基线诊断", "理解斜率定义"],
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

字段必须是经门禁处理的摘要。禁止存储电话号码、身份证号、邮箱、完整成绩档案或原始聊天记录。

## branch_summary

```json
{
  "answer_summary": "插问解释了横截距的现实意义",
  "impact_on_main": "有助于区分斜率与截距，不改变当前阶段",
  "unresolved_points": [],
  "safe_to_resume": true
}
```

## resume_context

`pop` 返回父级 `context_snapshot`，并增加：

```text
side_question
branch_summary
restored_thread
closed_thread
remaining_depth
```

恢复后必须采用快照中的 `next_skill` 和 `next_action`。插问结论可以作为解释背景，但不能自动成为主线阶段证据。

