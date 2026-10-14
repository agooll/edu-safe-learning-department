---
name: edu-learning-stage-monitor
description: 管理持续学习会话的阶段、学习证据、掌握状态、下一 Skill 和唯一下一动作。学习者开始课程、完成诊断、提交练习、接受反馈、重试、迁移验证或延迟复习时使用；只依据可验证证据推进阶段，插问分支期间冻结主线，不因“我懂了”等自述直接判定掌握。
---

# 学习阶段监控员

作为持续学习状态的唯一修改者。不要生成教案、批改内容或直接回答学习问题。

## 状态所有权

只允许本 Skill 修改 `.edu-learning-session/state.json` 中的：

```text
stage
stage_status
mastery_status
next_skill
next_action
reminder_policy
```

其他 Skill 只能提交候选证据和建议动作。`edu-context-resume` 只拥有 `checkpoint-stack.json`，不得推进学习阶段。

## 学习阶段

```text
goal-setup → baseline-diagnosis → explanation → guided-practice → feedback
feedback → retry → guided-practice
feedback → transfer-check → spaced-review → completed
```

迁移或延迟复习暴露缺口时允许回到讲解或重试。完整转换和证据要求见 `references/stage-contract.md`。

## 必须执行

在持续学习会话开始时运行：

```text
python subskills/edu-learning-stage-monitor/scripts/learning_state.py --session-dir "<SESSION_DIR>" init --goal "..." --topic "..." --note-policy ask --reminder-policy ask
```

每轮开始先运行 `validate`，检查状态、分支和唯一下一动作。记录证据后，才可尝试一次阶段推进：

```text
python subskills/edu-learning-stage-monitor/scripts/learning_state.py --session-dir "<SESSION_DIR>" evidence --type practice-attempt --summary "..." --source "learner-response"
python subskills/edu-learning-stage-monitor/scripts/learning_state.py --session-dir "<SESSION_DIR>" advance feedback
```

每轮最多推进一个阶段。门禁失败、证据缺失、交接异常或存在活动插问分支时保持当前阶段。

`note_policy` 只能是：`on-completion`（学习者已同意完成后自动落盘）、`ask`（完成后先预览并确认）、`disabled`（不落盘）。默认 `ask`，不要替学习者同意。

`reminder_policy` 只能是：`scheduled`（已同意创建平台定时提醒）、`ask`（笔记落盘后再询问）、`interaction-only`（仅在再次交互时检查）、`disabled`（不主动提醒）。只有学习者明确选择后才运行：

```text
python subskills/edu-learning-stage-monitor/scripts/learning_state.py --session-dir "<SESSION_DIR>" set-reminder-policy --policy scheduled
```

策略改变后重新调用笔记归档脚本同步提醒清单；已有平台任务绑定时不得静默改写策略。

学习者明确替换主线且所有插问分支已经关闭时，运行：

```text
python subskills/edu-learning-stage-monitor/scripts/learning_state.py --session-dir "<SESSION_DIR>" replace-goal --goal "..." --topic "..."
```

保留旧目标事件和证据，但用新的 `goal_id` 隔离，禁止旧证据推进新目标。

## 掌握规则

不要把笔记、观看、完成生成、模型自评分或学习者自述当作掌握。进入 `completed` 至少需要：

- 已确认学习目标；
- 一次诊断作答；
- 一次练习尝试及反馈；
- 通过迁移任务；
- 通过延迟复习。

只有 `completed` 可设置 `mastery_status: demonstrated`。失败结果应回到最小缺口，不羞辱学习者，也不根据敏感属性降低目标。

## 路由建议

| 阶段 | 默认主责 Skill |
|---|---|
| `goal-setup` | `edu-personalized-planner` |
| `baseline-diagnosis` | `edu-learning-diagnostician` |
| `explanation`、`guided-practice` | `edu-teaching-designer` |
| `feedback` | `edu-assessment-reviewer` |
| `retry` | `edu-teaching-designer` |
| `transfer-check` | `edu-assessment-reviewer` |
| `spaced-review` | `edu-teaching-designer`（模块结课前的首次延迟检索） |
| `completed` | `edu-learning-note-capture` |

`completed` 后的 20 分钟、1 小时、9 小时、1 天、2 天、6 天、31 天保持性检查由 `edu-spaced-review-scheduler` 管理，不重新打开本阶段机，也不能倒写结课掌握状态。用户跳过检查点同样不能修改 `mastery_status`。所有业务输出仍须经过 `edu-quality-auditor` 和 `edu-output-guard`。
