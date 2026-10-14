# 学习笔记摘要协议

```json
{
  "schema_version": "1.0",
  "completed_steps": ["完成基线诊断", "完成引导练习"],
  "confirmed_understanding": ["能解释斜率的单位意义"],
  "misconceptions_corrected": ["区分斜率与截距"],
  "side_question_summaries": ["插问解释了横截距，不改变主线阶段"],
  "transfer_summary": "能在不同计费情境中识别单位变化率",
  "review_summary": "延迟复习通过",
  "next_recommendation": "用图像和表格分别表示同一函数"
}
```

所有字段都是去标识化摘要。列表元素必须为字符串；迁移、复习和下一建议必须非空。摘要不能包含原始聊天、联系电话、身份证号、邮箱、正式成绩或健康诊断。

落盘位置：

```text
<SESSION_DIR>/
└── notes/
    ├── index.json
    ├── review-schedule.json
    ├── <goal_id>.reminders.json
    ├── <goal_id>.receipt.json
    └── <goal_id>.md
```

`index.json` 保存 `goal_id`、相对路径、主题、UTC 创建时间、凭据路径、凭据摘要、提醒清单和首次复习时间。相同 `goal_id` 的结课笔记只创建一次。

时间戳凭据包含 `recorded_at_utc`、`clock_source: system-utc`、`verification_level: local-integrity`、笔记 SHA-256 和凭据 SHA-256。验证时同时检查凭据摘要、笔记摘要、笔记内嵌时间和运行审计事件。该设计可检测本地文件不一致；外部权威时间证明需另接 RFC 3161 或平台签名服务。

笔记成功落盘后，以已验证 UTC 时间为唯一锚点，初始化 20 分钟、1 小时、9 小时、1 天、2 天、6 天、31 天七个绝对检查点，并生成同时间锚定的提醒清单。提醒清单只是待注册任务描述；只有取得 AstronClaw 成功任务 ID 并绑定后才算主动提醒已启用。后续只允许 `edu-spaced-review-scheduler` 记录用户选择、提醒绑定和复习结果；不得把检查点改成按上次复习时间累加的滚动间隔。
