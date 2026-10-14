# 艾宾浩斯检查点协议

## 存储结构

```text
<SESSION_DIR>/
├── runtime-events.jsonl
├── review-events.jsonl
├── review-evidence.jsonl
└── notes/
    ├── <goal_id>.md
    ├── <goal_id>.receipt.json
    ├── <goal_id>.reminders.json
    ├── index.json
    └── review-schedule.json
```

## 固定检查点

所有时间使用 UTC，并从结课笔记的已验证 `recorded_at_utc` 绝对计算。

| ID | 绝对偏移 | 模式 | 建议动作 |
|---|---:|---|---|
| `r01` | 20 分钟 | `immediate-retrieval` | 闭卷回忆一个核心概念 |
| `r02` | 1 小时 | `quick-recall` | 快速检索题 |
| `r03` | 9 小时 | `quick-recall` | 核心概念与例子检索 |
| `r04` | 1 天 | `focused-review` | 关键点检索并只补缺口 |
| `r05` | 2 天 | `focused-review` | 重点复习与一次应用 |
| `r06` | 6 天 | `transfer-review` | 不照搬原题的迁移任务 |
| `r07` | 31 天 | `monthly-retention-check` | 综合保持性验证 |

检查点不是相邻间隔累加，而是分别等于 `anchor + offset`。修改 `next_review_at_utc`、`checkpoint_index` 或锚点造成不一致时返回 `REVIEW_TIME_MISMATCH` 或 `REVIEW_ANCHOR_MISMATCH`。

## 状态机

```text
scheduled
→ awaiting-choice
├─ choose(review) → review-in-progress → passed → 下一个检查点
│                                      └→ gap-detected → 诊断后重做当前检查点
└─ choose(skip)   → skipped-by-user → 下一个检查点
```

`skip` 是用户自主选择，不声称掌握；`review` 只有产生同目标、同检查点、主线线程的证据后才能通过。

## 提醒注册状态

提醒清单的不可变部分由 `schedule_sha256` 校验。七个任务都绑定到同一笔记时间戳，但每次触发仍运行 `due`，因此不会跨过更早的未处理检查点。

```text
awaiting-consent ──用户同意──> pending-registration
pending-registration ──平台返回任务 ID──> registered
pending-registration ──失败/无工具──> interaction-only（向用户披露）
```

- 只有 `reminder_policy: scheduled` 才允许写入 `external_task_id`。
- 一个检查点只能绑定一个平台任务 ID，同一个平台任务 ID 也不能跨检查点复用；冲突时失败关闭。
- 生成提醒清单不等于注册成功；必须取得平台成功回执并执行 `bind-reminder`。
- 提醒文本只包含 `goal_id`、检查点 ID 和安全调用指令，不包含原始作答、成绩或身份信息。

## 时间与证据不变量

- 每次 `due`、`status`、`choose`、`evidence` 和 `record` 都重新校验笔记时间凭据。
- 当前时间早于笔记锚点时失败关闭。
- 最早未处理检查点优先，不因长期逾期自动吞并检查点。
- 复习未开始前不能预写证据。
- 证据只接受 `assessment-reviewer`、`diagnostic-review` 或 `teacher-confirmed` 来源。
- 跳过次数、复习次数、用户选择和结果均只追加审计记录。
