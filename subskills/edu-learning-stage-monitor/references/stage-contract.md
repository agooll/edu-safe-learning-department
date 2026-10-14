# 学习阶段与证据门禁

| 当前阶段 | 可进入阶段 | 必需证据类型 |
|---|---|---|
| `goal-setup` | `baseline-diagnosis` | `goal-confirmed` |
| `baseline-diagnosis` | `explanation` | `diagnostic-response` |
| `baseline-diagnosis` | `guided-practice` | `diagnostic-response`、`baseline-passed` |
| `explanation` | `guided-practice` | `concept-check` |
| `guided-practice` | `feedback` | `practice-attempt` |
| `feedback` | `retry` | `feedback-recorded` |
| `feedback` | `transfer-check` | `practice-passed` |
| `retry` | `guided-practice` | `remediation-completed` |
| `transfer-check` | `spaced-review` | `transfer-passed` |
| `transfer-check` | `explanation` | `transfer-gap` |
| `spaced-review` | `completed` | `review-passed` |
| `spaced-review` | `retry` | `review-gap` |

证据记录必须包含 `evidence_id`、`goal_id`、`stage`、`thread_id`、`type`、`summary`、`source` 和时间。只有 `thread_id: main` 的证据能推进主线。

## 状态不变量

- `next_action` 始终为一个非空动作。
- 每次命令至多改变一个阶段。
- 插问分支活动时拒绝阶段推进。
- 已完成阶段不能被模型自行重新打开。
- 证据不足时返回缺失类型，不猜测学习者已经完成。
- 状态文件采用 UTF-8 JSON 原子写入；证据采用只追加 JSONL。
- 进入 `completed` 后必须路由 `edu-learning-note-capture`；实际写入遵循 `note_policy`，不以默认设置代替学习者同意。
- 外部定时提醒遵循 `reminder_policy`；只有 `scheduled` 表示已经授权创建平台任务，`ask`、`interaction-only` 和 `disabled` 都不能写入伪造的任务 ID。
- `spaced-review` 是结课前首次延迟检索；结课后的长期保持复习由 `edu-spaced-review-scheduler` 单独管理，不回写或伪造本阶段机的 `mastery_status`。
