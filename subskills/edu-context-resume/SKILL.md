---
name: edu-context-resume
description: 保存和恢复持续学习过程中的插问分支、嵌套插问与主线位置。学习者说“顺便问一下”“插问”“题外话”，或说“回到主线”“继续刚才”“恢复学习”时使用；回答插问前保存去标识化主线快照，分支结束后逐层恢复目标、阶段、进度、证据和唯一下一动作，且不推进主线阶段。
---

# 学习插问恢复员

把插问视为线程栈，不把话题切换误认为学习目标已经改变。只修改 `.edu-learning-session/checkpoint-stack.json`。

## 消息分类

先运行：

```text
python subskills/edu-context-resume/scripts/context_stack.py --session-dir "<SESSION_DIR>" classify --message "..."
```

分类闭集为：

- `main-line`：主线回答或正常学习请求；
- `side-question`：明确插问、题外问题或临时概念澄清；
- `resume`：回到主线、继续刚才或插问结束；
- `goal-replacement`：明确放弃原目标并选择新主线。

模糊话题变化不要直接替换目标；先确认是插问还是换主线。

## 插问开始

1. 先通过 `edu-output-guard` 处理并去标识化用户消息。
2. 按 `references/context-contract.md` 生成主线快照。
3. 执行：

```text
python subskills/edu-context-resume/scripts/context_stack.py --session-dir "<SESSION_DIR>" push --question "..." --snapshot main-context.json
```

4. 使用现有路由、业务 Skill、质量审校和最终门禁回答插问。
5. 插问分支期间不得让 `edu-learning-stage-monitor` 推进主线阶段。

允许嵌套插问；每次 `push` 保存当前父分支，最大深度为 8。

## 返回主线

插问完成后生成不含原始敏感文本的 `branch-summary.json`，再执行：

```text
python subskills/edu-context-resume/scripts/context_stack.py --session-dir "<SESSION_DIR>" pop --summary branch-summary.json
```

逐层弹出分支，不跨级返回。使用返回的 `resume_context` 展示完整恢复卡片：

1. 原始学习目标与主题；
2. 插问前学习阶段和准确位置；
3. 已完成进度；
4. 已确认理解和证据；
5. 最近一次主线问答摘要；
6. 本次插问、结论及其对主线的影响；
7. 恢复后的唯一下一 Skill 和下一动作。

不要只回答“已回到主线”，也不要重新开始整个课程。

## 目标替换

只有学习者明确表示放弃原目标并建立新目标时，才返回 `goal-replacement`。先关闭全部插问、生成旧主线总结，再要求 `edu-learning-stage-monitor` 执行 `replace-goal`；不得静默把插问提升为主线。

所有快照只保存去标识化摘要，不保存完整聊天记录、正式成绩或不必要的学生个人信息。
