# 中央工作流控制器契约

## 目的与边界

`scripts/workflow_controller.py` 是本包唯一的 L1 执行入口。它把输入预检、角色/任务路由、学生学习状态、业务草稿、独立质量审计、最终门禁和发布授权串成一个持久化事务。

L1 能保证：只要调用控制器，步骤就不能乱序，缺失或被篡改的回执不能用于放行，只有最终 `release` 命令返回用户可见内容。L1 不能阻止模型完全不调用控制器而自行回复；该平台边界见 `platform-enforcement.md`。

## 固定命令序列

所有路径都从 Skill 根目录解析。每个任务复用同一个绝对 `SESSION_DIR`，并保存 `start` 返回的 `workflow_id`。

```powershell
python scripts/workflow_controller.py --session-dir "<SESSION_DIR>" start --input examples/teaching-request.json
python scripts/workflow_controller.py --session-dir "<SESSION_DIR>" submit-draft --workflow-id <WORKFLOW_ID> --input examples/controller-draft.json
python scripts/workflow_controller.py --session-dir "<SESSION_DIR>" submit-quality --workflow-id <WORKFLOW_ID> --input examples/controller-quality.json
python scripts/workflow_controller.py --session-dir "<SESSION_DIR>" release --workflow-id <WORKFLOW_ID>
python scripts/workflow_controller.py --session-dir "<SESSION_DIR>" verify --workflow-id <WORKFLOW_ID>
```

调用方每一步都必须先检查 `ok`、`status` 和 `expected_action`。不得因为“预计下一步会通过”而提前执行后续命令。

## 状态机

| 当前状态 | 允许动作 | 成功后的典型状态 | 禁止事项 |
|---|---|---|---|
| 新请求 | `start` | `awaiting-draft` | 直接生成并发布答案 |
| `awaiting-draft` | `submit-draft` | `awaiting-quality` | 跳过独立审计 |
| `awaiting-quality` | `submit-quality` | `ready-for-release` 或 `awaiting-draft` | 自行宣布质量通过 |
| `ready-for-release` | `release` | `released` | 在放行前显示草稿 |
| `released` | `release`、`status`、`verify` | `released` | 修改已授权输出 |
| `clarification-required`、`blocked`、`escalated` | `status`、`verify` | 原状态 | 继续业务生成或发布 |

质量审计或最终门禁要求返修时，`revision_count` 由控制器递增。允许的返修计数是 `0..2`；两次返修后仍未通过会生成 `REVISION_LIMIT` 升级回执并停止自动发布。

## 回执链与持久化

状态文件位于：

```text
<SESSION_DIR>/.workflow-controller/workflows/<workflow_id>.json
```

去标识化事件摘要位于：

```text
<SESSION_DIR>/.workflow-controller/workflow-events.jsonl
```

正常一次性任务的强制步骤顺序是：

```text
preflight → route → draft → quality → final-gate → release-authorization
```

学生持续学习任务可在 `preflight` 与 `route` 之间增加 `learning-state`。每个回执至少绑定：

- `workflow_id`、`request_id`、序号和步骤名；
- 当前 `revision_count`；
- 对应 artifact 的 SHA-256；
- 上一回执 SHA-256；
- 本回执 SHA-256 和 UTC 签发时间。

`verify` 会重新计算回执与 artifact 哈希并检查顺序、归属和必需步骤。任何字段、草稿、审计结果或回执被修改后都返回 `WORKFLOW_INTEGRITY_ERROR`。本地时间与哈希只证明本地完整性，不等同于第三方可信时间戳或不可抵赖签名。

事件日志只保存步骤、状态、哈希、计数和时间，不保存原始作答、完整草稿或个人信息。

## 各命令的输出约束

- `start`：执行 request 契约校验、生成前门禁、确定性路由；学生线路同时校验或初始化学习状态。
- `submit-draft`：校验请求 ID、唯一生产者 Skill 和 draft 契约；不返回草稿正文。
- `submit-quality`：校验独立质量 envelope；`pass` 时五个强制检查必须全部为布尔值 `true`，然后由控制器调用最终门禁。
- `release`：仅在最终门禁已经产生非空 `safe_output` 且决策为 `release` 时签发 `release-authorization`，返回 `safe_output`。
- `status`：返回当前状态和下一动作，不替代完整性验证。
- `verify`：验证已有链并列出 `verified_steps`，不改变状态。

除 `release` 外的所有成功输出都只含流程摘要。失败输出必须包含 `ok: false`、稳定错误代码、空 `safe_output` 和一个安全下一动作，且不得出现 Python traceback。

## 学生主线与插问

学生连续学习请求在 `start` 中使用阶段监控器的当前 `next_skill` 和 `next_action` 覆盖普通一次性路由，防止模型自行选择学习阶段。插问仍需先由插问栈保存主线快照，再用新的 `request_id` 启动独立控制器事务；插问完成后逐层恢复快照。插问的放行回执不能作为主线阶段通过证据。

笔记候选、到期复习内容和提醒文案也是用户可见输出，因此必须作为新事务经过同一控制器。实际笔记写入、复习状态更新和外部提醒创建仍分别由其专属状态脚本执行，并保存真实回执。

## 失败关闭清单

以下情况均不得返回业务答案：

- 未取得 `workflow_id`；
- `expected_action` 与准备执行的命令不一致；
- request ID 或唯一生产者不匹配；
- 质量通过但五项强制检查不全；
- 回执、artifact 或状态文件完整性失败；
- 控制器脚本、Python 解释器或持久化目录不可用；
- 最终状态不是 `ready-for-release`；
- `release` 未返回非空 `safe_output`；
- 平台把模型直接回复误当成控制器放行。

