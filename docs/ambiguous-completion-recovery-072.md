# 072 · 回答结束与内容歧义冲突的恢复

## 现场证据

候选页显示“已连接 / 回答处理需要重试”，当前会话仍为 `in_progress`，第一题没有形成 `CandidateAnswer`。本题已有持久录音分片，字幕属于当前采集中的转写；这不等于正式答案已经提交。

本地 API 日志与调用记录的 UTC 时间线：

| 时间 | 事实 | 含义 |
| --- | --- | --- |
| 10:39:19 | 理解结果触发 `completion_has_unresolved_ambiguity` | 同时声明普通回答完成和未解歧义，领域校验拒绝 |
| 10:40:19–10:40:28 | 针对术语的澄清被选中并实际播完 | 确有送达记录，不能假定候选人从未听过澄清 |
| 10:40:54 | `unresolved_clarification_required` | 该次准备缺少合法的内容澄清上下文 |
| 10:40:58 | `unresolved_answer_contract_invalid` | 后续结果仍违反待核实收尾合同；未保存原始模型响应，不能进一步断言具体哪个字段错误 |
| 10:42:13 | `understanding_unavailable`，自动恢复受限 | 最终进入需要手动重试的状态 |

本场相关 TTS 均成功。网关调用 `success` 只证明供应商调用和 JSON 层通过，不证明领域语义通过。`understanding_retry_budget.exhausted=true` 也可能表示不可自动重试的合同错误，不能单凭它推断时间预算泄漏。外层取消的模型纠正调用可能没有独立调用记录，不能由“只有一条成功记录”推断没有发起纠正。

## 问题与处理

| 问题 | 处理及边界 |
| --- | --- |
| `answer + finish_answer + ambiguities` 在完整校验时被拒绝，后置歧义策略不可达 | 统一语义 seam 只收敛这一已知冲突：原完成证据、当前话语和目标原文通过检查后，撤销提交权限，保留可靠主张并进入具体澄清 |
| 缺少两个错误码的针对性纠正提示 | 为 `completion_has_unresolved_ambiguity`、`unresolved_answer_contract_invalid` 增加固定指令，区分知识缺口、可确定的音近词与真实歧义；不让删除疑点或伪造送达记录成为过关方式 |
| 内容澄清交流中继续发起不带送达记录的推测理解，正式路径又重新请求 | 在针对性澄清选择开始时取消旧推测；当前 capture 的澄清阶段跳过无上下文预览，正式路径继续使用真实 final、送达边界和新回复。此缺陷可由代码与合成测试复现，但无法将两条现场拒绝日志逐条归属到预览或正式调用 |

### 安全收敛顺序

1. 原始 wire、引用与 canonical schema 先通过。
2. 仅对 `answer/clarification_request + finish_answer`，存在真实歧义、target、basis、足够语义置信度且未提供内容澄清上下文的结果处理；原语义校验必须只报 `completion_has_unresolved_ambiguity`。
3. 清除 basis 前，检查每条完成引用确属原文，并对原对象执行当前发言校验。引用中的结束语、旧完成语、当前改口继续/退出、已经解释的旧问题仍被拒绝。
4. 只投影为 `clarification_request + clarify`、`confidence≤0.64`、`completion_basis=null`、`followup_allowed=false`。保留候选结束本题的语义、claims、证据、能力点分区、歧义与目标。
5. 投影后仍完整执行 schema、领域证据及当前发言校验。非法 claims、未知能力点和伪造 target 不能因此通过；没有正式提交权限。
6. 已有内容澄清上下文时不走首次澄清投影。真实送达后的新回复明确坚持结束，才可能沿既有 `answer_unresolved` 合同收尾；继续思考/纠正按新回复处理。整场退出依然使用独立确认。

## 版本与运行边界

- 当前理解 Prompt 为 `interview_turn_understanding.v86–97`，组合决策为 `interview_turn_decision.v117–128`，派生控制说明为 `semantic_controls.v3`。领域、网关与 Mock 同步支持新版本并保留旧版本读取。
- wire 字段、REST/WebSocket 协议、评分规则、供应商和路由配置未变；无需数据库迁移。
- 本轮只使用本地合成测试和只读现场证据。真人会话没有被自动重试、恢复、提交或重放；外部模型真实效果不由本地测试保证。
- 仓库验证与实际服务加载分别记录在操作日志。活动面试期间保持 API 运行，未提交字幕仍需由当前采集继续处理，不能以重启作为恢复手段。
- 最终25文件整合回归 **704 passed**，包括63项新增；日志 `/private/tmp/interviewer-072-integrated-verified.log`。072结束时因活动面试尚未加载；随后已随073在空闲窗口加载API及worker，原业务摘要保持。完整命令、早期失败、文件TTS测试夹具修正和恢复说明见操作日志072/073。
