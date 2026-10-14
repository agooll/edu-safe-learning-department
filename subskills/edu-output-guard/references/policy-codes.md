# 门禁规则代码

| 代码 | 含义 | 默认动作 |
|---|---|---|
| `INPUT_EMPTY` | 请求缺少可处理内容 | clarify |
| `PII_REDACTED` | 检测并遮盖个人信息 | allow/release |
| `PROMPT_INJECTION` | 尝试覆盖规则、泄露提示或关闭门禁 | block |
| `ACTIVE_EXAM_ASSISTANCE` | 实时考试、监考绕过或受控答案请求 | block |
| `HIGH_IMPACT_DECISION` | 正式评分、录取、分班、处分等 | escalate |
| `HEALTH_DIAGNOSIS` | 无资质医学、心理或障碍诊断 | escalate |
| `MINOR_IMMEDIATE_RISK` | 未成年人自伤、他伤或紧急危险 | escalate |
| `QUALITY_NOT_PASSED` | 独立质量审计未通过 | revise |
| `OUTPUT_EMPTY` | 草稿为空 | revise |
| `UNSUPPORTED_CLAIM` | 出现研究、数据或引用断言但无依据 | revise |
| `RUBRIC_MISSING` | 精确评分缺少评分标准 | revise |
| `REVISION_LIMIT` | 已达到两次返修上限 | escalate |
| `CONTEXT_STATE_MISMATCH` | 主线、插问分类与活动线程不一致 | revise |
| `UNSUPPORTED_MASTERY` | 未通过阶段门禁却声称已经掌握 | revise |

对外只展示必要的通俗解释，不展示检测表达式和绕过细节。
