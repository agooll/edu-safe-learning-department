---
name: edu-spaced-review-scheduler
description: 严格校验结课笔记的 UTC 时间戳、笔记摘要、凭据摘要和审计事件，并按艾宾浩斯原始遗忘实验的七个观察时距工程化调度复习和绑定已授权的 AstronClaw 定时任务。学生再次进入系统、询问到期复习、注册或收到复习提醒，或需要在“立即复习”和“跳过本次”之间选择时使用；所有检查点必须锚定原笔记时间，外部任务必须有真实回执，跳过必须留痕且不能被表述为掌握。
---

# 艾宾浩斯复习调度员

只管理结课后的保持性检查。先读取 `references/scientific-basis.md` 和 `references/review-contract.md`，不要把观察时距误称为对所有学习者都最优的医学或心理学定律。

## 严格入口

每次进入学生线路时运行：

```text
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" due
```

对每个项目依次完成四项时间校验：凭据 SHA-256、笔记 SHA-256、笔记内嵌 UTC 时间和审计事件。任一失败就停止该项目，不计算或修改复习时间。

七个检查点全部从笔记 `recorded_at_utc` 绝对计算：20 分钟、1 小时、9 小时、1 天、2 天、6 天、31 天。不得按上次复习时间重新漂移，不得四舍五入到本地日期，也不得静默越过较早未处理检查点。

## 到期选择

检查点到期时固定提供两个明确动作：

```text
1. 立即复习
2. 跳过本次
```

只处理最早的未完成检查点，每轮保持一个下一动作。

### 立即复习

```text
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" choose --goal-id <goal_id> --decision review
```

按检查点返回的 `review_mode` 生成主动检索、重点回顾、迁移任务或月度保持检查。先选择复习，再由批准来源写入证据，最后记录 `passed` 或 `gap`。通过后进入下一个绝对时间点；存在缺口时保持当前检查点未完成并回流诊断。

### 跳过本次

```text
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" choose --goal-id <goal_id> --decision skip
```

立即记录 `skipped-by-user` 并进入下一个检查点。跳过不需要证明，但必须写入选择时间、检查点和审计日志，同时返回 `mastery_claimed: false`。不得把跳过转换成掌握、通过或正式学习证据。

## 复习证据

复习开始后才允许写证据：

```text
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" evidence --goal-id <goal_id> --evidence-type retrieval-check-passed --summary <去标识化摘要> --source assessment-reviewer
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" record --goal-id <goal_id> --outcome passed --evidence-id <evidence_id> --evidence-type retrieval-check-passed
```

证据必须匹配目标、检查点和主线线程，且不能早于“立即复习”选择或晚于结果记录。插问、空证据、其他目标证据和未经批准来源全部失败关闭。

## 旧计划迁移

检测到 `expanding-retrieval-v1` 时不静默解释，先运行 `migrate`。迁移前逐项校验笔记时间，保留 `review-schedule.expanding-v1.backup.json`，然后从原始笔记锚点重建七个严格检查点。

所有状态只写入本地复习计划和审计日志，不写正式成绩或学生档案。

## AstronClaw 定时任务

笔记落盘后先验证提醒清单：

```text
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" reminders --goal-id <goal_id>
```

仅当 `reminder_policy=scheduled` 且学习者已经授权时，调用 AstronClaw 定时任务能力创建七个一次性任务。每个任务只要求再次调用总 Skill、校验该目标的到期状态并展示用户选择。平台成功返回任务 ID 后逐项绑定：

```text
python subskills/edu-spaced-review-scheduler/scripts/review_schedule.py --session-dir "<SESSION_DIR>" bind-reminder --goal-id <goal_id> --checkpoint-id r01 --external-task-id <task_id>
```

注册失败或平台没有定时任务能力时，不伪造任务 ID；向用户说明已降级为交互时检查。
