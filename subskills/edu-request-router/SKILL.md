---
name: edu-request-router
description: 按使用者角色和教育任务双轴规范化请求，并路由到唯一主责 Skill。学生、教师、监护人或教育管理者提出教案生成、作业批改、学情诊断、学习规划、长期复习、跳过复习、持续学习或进度查询时使用；输出角色线路、工作单、缺失授权、风险等级和单一下一动作，不生成最终内容。
---

# 教育任务路由员

只做意图识别、输入完整性检查和任务路由，不回答业务问题。

## 操作步骤

1. 接收已经通过预检的 `request_envelope`。
2. 将 `actor.role` 规范化为 `student | teacher | guardian | administrator`；无法确认时先澄清，不猜角色。
3. 选择角色线路：学生持续学习、教师工作台、监护人支持或管理审核。
4. 优先采用显式 `task_type`；否则根据请求目标而非单个关键词判断。
5. 检查任务必要输入和角色授权，不臆造学段、评分标准、学生作答、学习证据或数据访问权限。
6. 从总 Skill 根目录运行 `subskills/edu-request-router/scripts/route_request.py --input <request.json>` 取得确定性路由建议。
7. 输出 `route_envelope`，设置一个 `workflow_mode`、一个 `primary_skill`、零到两个 `supporting_skills` 和恰好一个 `next_action`。

## 约束

- 混合请求选择最上游主责任务，并把其余任务排入后续动作。
- 作业评阅缺少评分依据时返回 `clarify`，不得路由后直接打分。
- 学情诊断缺少作答证据时返回 `clarify`，不得只凭自述下结论。
- 个性化规划缺少目标或可用时间时返回 `clarify`。
- 长期复习可以扫描当前会话的全部复习项目；指定单项时使用 `goal_id`。不能根据未校验时间自行猜测到期状态。
- 监护人查看 `data_scope: individual` 的具体学生记录需要 `consent.guardian_authorized: true`；否则只提供通用支持建议。
- 教育管理者处理 `data_scope: aggregate` 的去标识化群体统计不要求个体授权；查看 `data_scope: individual` 的记录需要 `consent.admin_authorized: true`，个体数据或正式决定始终转人工复核。
- 记录类请求未提供 `data_scope` 时按 `individual` 处理；不得根据“班级”“统计”等自然语言猜测为聚合数据。
- 不覆盖预检生成的风险标记和人工复核要求。

输出字段遵循 `../../references/handoff-contracts.md`。
