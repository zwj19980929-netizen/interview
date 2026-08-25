# 实施路线图

本文用于指导后续 AI 协作者拆任务。优先做能跑通业务闭环的 MVP，再逐步增强实时体验和评分质量。

## 当前完成快照

里程碑 0-7 的旧版可运行 MVP 骨架已经落地。当前实现包括事务型 Persistence seam、SQLite/Memory contract、Interview Plan Assembly、已审批计划、InterviewSession 聚合快照、统一生命周期命令与事件、Outbox worker、浏览器语音兜底面试、append-only 评分/报告和 Model Invocation。里程碑 6 目前只完成 `avatar.speak` mock 与浏览器读题降级，尚未完成 STT/TTS 统一 schema 和真实语音 adapter。旧 MVP 验证了技术骨架，但没有覆盖新确认的岗位题库、简历审阅、预约匹配、随机抽题、真实服务端 STT 和企业音频复核流程。后续从里程碑 8 继续，并保持已有 deep module 边界。

## 里程碑 0：项目骨架

目标：把当前示例项目改造成可开发后端。

建议任务：

- 建立 `app/`、`tests/` 目录。
- 引入 FastAPI、Pydantic、配置管理、结构化日志。
- 定义统一错误响应和健康检查接口。
- 建立本地开发启动命令。
- 添加基础测试和格式化配置。

验收：

- `GET /healthz` 返回服务状态。
- 测试命令可在本地运行。

## 里程碑 1：题库管理

目标：支持题目、标准答案、关键点和标签的增删改查。

建议任务：

- 实现 `KnowledgeBase`、`Question` 数据模型。
- 实现创建、更新、列表、详情、归档题目 API。
- 实现 JSON 批量导入的校验和导入任务。
- 为题目状态和索引状态建立枚举。

验收：

- 面试官可以创建题目并保存标准答案。
- 题目缺少关键点时返回明确校验错误。

## 里程碑 2：模型网关和供应商配置

目标：让模型、语音和数字人能力可以像插件一样接入。

建议任务：

- 建立 Model Invocation deep module：单一 `invoke` interface、provider manifest entrypoint、能力枚举、mock adapter。
- 实现 `ModelProviderConfig`、`ModelRoute`、`ModelInvocationLog`。
- 实现 provider catalog、供应商配置、路由配置和测试调用 API。
- 实现 `mock` provider 和 `openai_compatible` provider 骨架。
- 定义 `llm.chat_json`、`llm.chat_text` 的统一请求/响应；`embedding.text` 只作为可选实验能力。

验收：

- 后台可以看到已安装 provider 插件。
- 管理员可以配置一个 provider，并为 `answer_evaluation` 配置路由。
- 业务代码只调用 `ModelGateway.invoke`，不直接 import provider；route 中的重试、fallback、超时、断路器和 schema 校验必须实际生效。

## 里程碑 3：题库查询和候选池

目标：根据岗位、题库和结构化条件形成可用候选题。

建议任务：

- 用关系库字段过滤组织、岗位、题库、技能、难度、题型和状态。
- 后台查题使用全文/关键词搜索，不要求向量召回。
- 实现题目校验任务和 `validation_status`。
- 实现 `POST /questions/search`。

验收：

- 输入岗位和结构化条件后能返回边界正确的候选题。
- 校验失败不会破坏题目草稿，也不会让无评分依据的题进入候选池。

## 里程碑 4：岗位要求和面试计划

目标：从岗位要求生成可编辑面试计划。

建议任务：

- 实现 `RoleRequirement` 创建和解析。
- 通过单一 `assemble(PlanAssemblyRequest)` interface 实现 `InterviewPlan` 草稿装配。
- 实现覆盖配额、题目/关键点去重、单技能上限、难度曲线、权重和时长分配。
- 返回每道题的选择原因以及覆盖、候选池不足和约束放宽摘要。
- 实现计划更新、批准，并把装配策略与摘要冻结到正式面试快照。

验收：

- 输入岗位要求后能生成一份包含题目、顺序、权重、预计时长的计划。
- 计划权重合计为 1、题目预计时长合计等于岗位面试时长，未覆盖维度不会静默丢失。
- 面试官可以删除或调整计划项。

## 里程碑 5：面试会话和实时事件

目标：完成一次文本或音频兜底面试。

建议任务：

- 实现 `InterviewSession`、`InterviewTurn`、`CandidateAnswer`。
- 通过统一生命周期命令实现创建、启动、暂停/超时恢复、跳题、结束和取消。
- 实现 WebSocket 事件包络、持久领域事件和会话状态同步，REST 与 WebSocket 共用迁移规则。
- MVP 先支持文本回答；随后支持音频分片和 STT mock。

验收：

- 候选人可以进入面试房间并完成多轮问答。
- 刷新页面后能恢复当前面试状态。

## 里程碑 6：数字人和语音能力

目标：把读题和语音识别从业务逻辑中解耦。

建议任务：

- 定义 `stt.streaming`、`stt.batch`、`tts.synthesize`、`avatar.speak` 统一请求/响应。
- 实现语音和数字人 provider 插件接口，通过模型网关调用。
- 实现 mock 数字人：先返回 TTS 音频或读题文本。
- 接入一个真实 STT 供应商前，保留 mock 和测试。
- 设计 WebRTC 信令或供应商会话映射。

验收：

- 数字人供应商不可用时，系统能降级为文字或普通 TTS。
- STT 输出 partial/final 事件，final 才进入评分。

## 里程碑 7：评分和报告

目标：产出可解释的单题评分和最终报告。

建议任务：

- 定义 `llm.chat_json` 统一请求/响应和评分 schema。
- 实现单题评分服务，支持 mock、规则和真实 LLM 三种模式。
- 实现重评接口。
- 实现报告汇总、推荐等级、导出字段。

验收：

- 每题评分包含覆盖关键点、缺失关键点、证据和建议。
- 报告能汇总总分、维度分、优势、风险和后续建议。

## 里程碑 8：岗位与岗位题库构建

目标：让题库真正以岗位为边界，并在上传后形成结构化候选池和异步读题语音，不依赖向量索引。

建议任务：

- 实现 `JobPosition`、岗位题库 API 和前端工作区；一个题库只属于一个岗位。
- 迁移现有 `knowledge_base_id` 数据，并在 repository 和搜索中强制 `organization + position + knowledge base` 过滤。
- 扩展 Outbox 为题库导入、结构化字段/评分依据校验和 `QuestionSpeechAsset` 构建流水线。
- 定义 `tts.synthesize` 统一 schema，接入一个真实 TTS adapter，把供应商结果复制到私有对象存储。
- 实现题库 `draft/building/ready/failed/archived` 状态、失败题目重试和 readiness 摘要。

验收：

- 一个岗位可以有多个题库，任何跨岗位创建、搜索或计划引用都被拒绝。
- 上传题库立即返回工作项；结构化字段/评分依据校验和题目语音全部完成后题库才进入 `ready`。
- 修改题干、语言或音色会生成新语音版本，历史资产不被覆盖。

## 里程碑 9：企业简历库与 AI 经历问题

目标：企业上传候选人信息和简历，AI 面向指定岗位异步生成可审核的项目经历问题。

建议任务：

- 实现 `CandidateProfile`、`ResumeDocument`、私有简历对象存储和文件扫描/解析。
- 加密联系方式并建立带租户盐的精确查找哈希。
- 实现 Resume Review deep module：脱敏、项目/职责/技能证据提取、证据位置和告警。
- 生成 `ExperienceQuestion` 草稿，支持人工编辑、批准/拒绝和批准后的语音生成。
- 为 `resume_review` 和 `resume_experience_question_generation` 配置独立 route、prompt/schema revision 和审计。

验收：

- 同一简历可针对不同岗位生成独立审阅，输入版本相同的重试幂等。
- 每个经历问题可追溯到项目证据和核验重点；未经人工批准不能进入计划。
- AI 输入不包含照片和与工作能力无关的受保护属性，不直接生成录用结论。

## 里程碑 10：候选人专属计划、预约与填报匹配

目标：面试官选择岗位题库和候选人生成计划，预约后由候选人安全登记并 self-start。

建议任务：

- 把 Interview Plan Assembly 调整为岗位题库 `bank_slots`、冻结候选池和已批准经历问题。
- 实现 `InterviewAppointment`、readiness gate、一次性 invitation token 哈希、过期、撤销和消费。
- 实现公开邀请页和 Candidate Intake；候选人填写姓名、邮箱、手机号与同意信息。
- 匹配仅针对预约绑定的 `CandidateProfile`，至少邮箱或手机号精确相同；错误响应防枚举。
- start 原子消费预约、创建唯一 InterviewSession，并冻结候选人、简历、岗位和计划快照。

验收：

- 题库、审阅、经历问题语音或生产 STT 未就绪时不能发邀请。
- 错误/过期/撤销 token 无法登记；重复登记/start 幂等，不能产生两个会话。
- 未匹配候选人不能查看简历、标准答案或面试房间。

## 里程碑 11：可审计随机抽题与服务端语音闭环

目标：数字人随机检索岗位题、读取预生成语音，服务端识别并逐题评分，再进入经历问题。

建议任务：

- 实现 Question Selection deep module：冻结候选池、会话随机种子、HMAC 槽位随机、覆盖/难度/去重和补位策略。
- 保存 `QuestionSelection` 和题目快照；以唯一约束保证断线或 worker 重试不重新抽题。
- 扩展生命周期：选择、读题、回答、`transcribing`、评分、下一槽位、阶段切换和报告。
- 定义 `stt.streaming`、`stt.batch` 统一 schema 与流 interface，接入真实服务端 STT provider。
- 客户端只传音频；partial 仅展示，final 创建回答。流式失败保存音频并用 batch 修复。
- 岗位题全部完成后执行固定的已批准经历问题；两类问题使用各自 rubric 逐题评分。

验收：

- 不同会话可随机抽到不同题，但每个选择都可解释、可重放且不超出批准题库。
- 浏览器提交的 final transcript 不能进入生产评分；服务端 final 才能推进到 evaluating。
- 每题完成“读题—录音—转写—评分”后才进入下一题，断线恢复不重复问题或评分。
- 岗位题阶段完成后才进入 `resume_experience` 阶段。

## 里程碑 12：客观报告与企业复核

目标：企业能查看总分、岗位匹配证据、每题答案和语音并完成独立复核。

建议任务：

- 报告改为 `strong_match/match/partial_match/insufficient_evidence/manual_review` 和逐岗位维度证据。
- 实现 review projection、回答音频短期签名 URL、下载/回听审计和 reviewer 权限。
- 实现转写 append-only revision、人工修正、重评和报告重新汇总。
- 在 UI 并列展示题干、阶段、音频、final transcript、STT 置信度、分数、证据、缺失点和复核标记。
- 人员录用决定作为未来 ATS 集成的独立显式动作，不能由 AI 报告自动写入。

验收：

- 任一报告分数能追溯到题目快照、转写 revision、评分 revision 和音频时间戳。
- 复核人修正转写后新增评分/报告 revision，历史版本保持可查。
- 无权限或过期签名 URL 不能播放音频，所有访问有审计事件。

## 里程碑 13：生产化与公平性加固

目标：把新闭环变成可试点系统。

建议任务：

- 迁移 PostgreSQL、私有对象存储、密钥管理、RBAC、租户 RLS、审计和数据到期删除；pgvector 不列为正式面试验收依赖。
- 加固 Outbox：指数退避、最大尝试、dead-letter、指标、告警和消息队列唤醒。
- 增加 Provider readiness、跨实例断路器、成本和数据区域策略。
- 建立题目难度、随机抽题分布、STT、简历证据和 AI/人工评分离线评估集。
- 增加 WebRTC、多实例实时事件分发、心跳超时监控；最后接真实视频数字人和口型同步。

验收：

- 外部供应商、worker 或连接中断后任务可恢复且不重复抽题/计分。
- 不同候选人的抽题难度与覆盖分布可比较，低置信度结果进入人工复核。
- 敏感数据访问、供应商发送、回听、导出和删除均可审计。

## 当前执行优先级

必须按依赖顺序推进：

1. 里程碑 8：岗位题库、结构化候选池和题目语音。
2. 里程碑 9：简历库、AI 审阅和经历问题。
3. 里程碑 10：候选人专属计划、预约、填报匹配。
4. 里程碑 11：随机抽题、服务端 STT、逐题生命周期。
5. 里程碑 12：报告与企业复核。
6. 里程碑 13：数据库、权限、WebRTC、数字人和生产加固。

真实服务端 STT 不再属于“第二阶段可选增强”，而是正式预约可开始的必要能力；WebRTC 和真实视频数字人仍可以在音频 WebSocket 闭环稳定后实现。向量数据库同样不属于当前必做项，只有题库治理出现可测量的语义搜索/近似去重需求后再单独立项。
