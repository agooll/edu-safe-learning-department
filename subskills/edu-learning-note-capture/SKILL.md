---
name: edu-learning-note-capture
description: 将已完成学习模块的目标、掌握证据、纠错、插问结论、迁移结果和复习结果整理为去标识化 Markdown 笔记并安全落盘，同时生成 UTC 时间戳、笔记摘要凭据、艾宾浩斯七点复习计划和定时提醒清单。学生持续学习阶段进入 completed、要求保存结课笔记、生成复习提醒或校验笔记时间时使用；写入前必须通过质量与安全门禁，并遵循笔记与提醒同意策略。
---

# 学习笔记归档员

只归档已经验证的学习结果，不负责教学、评分、推进阶段或更新后续复习结果。写入总控选定并持续复用的 `<SESSION_DIR>/notes/`；不得按当前工作目录另建状态。

## 强制前置条件

同时满足以下条件才可生成结课笔记：

- `state.stage: completed`；
- `state.mastery_status: demonstrated`；
- 当前活动线程为 `main`，没有未关闭插问；
- 笔记摘要已经通过 `edu-quality-auditor` 和 `edu-output-guard`；
- 不包含原始聊天记录或个人敏感信息。

不满足时失败关闭，不伪造结课笔记。

## 同意策略

- `on-completion`：学习者在会话开始时已明确同意，完成后自动写入。
- `ask`：先返回预览，学习者确认后使用 `--confirm` 写入。
- `disabled`：不写文件，只报告已跳过。

不要替学习者选择同意策略。

## 操作步骤

1. 根据 `references/note-contract.md` 生成去标识化 `note-summary.json`。
2. 将摘要交给质量审校和最终输出门禁。
3. 运行：

```text
python subskills/edu-learning-note-capture/scripts/capture_note.py --session-dir "<SESSION_DIR>" capture --summary note-summary.json
```

4. `note_policy: ask` 返回预览后，取得明确确认再运行：

```text
python subskills/edu-learning-note-capture/scripts/capture_note.py --session-dir "<SESSION_DIR>" capture --summary note-summary.json --confirm
```

5. 写入 `<goal_id>.receipt.json`，用笔记 SHA-256、UTC 时间和凭据 SHA-256建立本地完整性校验；从该 UTC 时间绝对计算 20 分钟、1 小时、9 小时、1 天、2 天、6 天、31 天检查点并初始化 `review-schedule.json`。
6. 生成 `<goal_id>.reminders.json`，包含七个一次性提醒的绝对 UTC 时间、最小化调用提示和注册状态。只生成清单不等于外部任务已经注册。
7. 运行 `verify --goal-id <goal_id>`，四项校验全部通过后才报告写入成功。
8. 返回笔记路径、提醒清单、`goal_id`、时间戳、20 分钟首次检查时间和一个下一动作。相同 `goal_id` 重复执行时保持幂等，不覆盖已完成笔记。

## 笔记内容

只记录：学习目标与主题、完成阶段、已确认理解、主要纠错、主线证据 ID、插问结论及影响、迁移验证、延迟复习、UTC 时间戳、首次保持性复习时间和下一建议。不要保存完整作答、正式成绩、健康信息或其他学生数据。

本地凭据只能证明文件与记录当前保持一致，验证级别必须写作 `local-integrity`。未接入第三方签时服务时，不宣称该时间戳具有外部权威证明力。
