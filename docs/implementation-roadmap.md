# 实施路线图

本文用于指导后续 AI 协作者拆任务。优先做能跑通业务闭环的 MVP，再逐步增强实时体验和评分质量。

## 当前完成快照

里程碑 0-13 的仓库内实现已经落地并通过 99 项自动化测试。当前闭环包括题库批量构建、PDF/URL 简历安全摄取与私有文件、脱敏 Resume Review、候选人专属计划、预约邀请页/明确同意/准入、候选人 token 安全投影、HMAC 稳定随机抽题、服务端 streaming/batch STT、逐题评分、报告导出、企业复核、RBAC/审计/公开限流、到期数据清理、PostgreSQL/RLS adapter、Outbox dead-letter、Redis 实时事件、心跳监控、抽题公平性评估，以及 OpenAI-compatible、DeepSeek、智谱 Chat/GLM-TTS 与 DashScope/千问模型 adapter。

状态必须分成“仓库 verified”和“外部 environment_pending”：OpenAI-compatible、DeepSeek、智谱与 DashScope/千问 adapter 已在仓库完成，但没有真实账号/凭据/区域时不能完成生产联调；真实 PostgreSQL/Redis、阿里云 OSS、恶意文件扫描器和 STT/数字人同样保留外部边界。`INTERVIEWER_RUNTIME_ENV=production` 要求显式、非 mock 且近期健康的语音/评分 route、扫描器和生产密钥，否则邀请/start 失败关闭。邮件/短信与 WebRTC/视频数字人还需要用户选择外部通道或供应商。

## 里程碑状态标记

| 里程碑 | 状态 | 已完成范围 | 未满足的主要验收 |
| --- | --- | --- | --- |
| 0 项目骨架 | ✅ verified | FastAPI、健康检查、测试、启动和 worker 命令 | 观测平台由部署环境选择 |
| 1 题库管理 | ✅ verified | CRUD/归档、JSON import、rebuild/build job、语音重建 | 无仓库阻塞 |
| 2 模型网关和供应商配置 | ✅ verified | chat/embedding/STT/TTS/avatar schema、invoke/open_stream、加密凭证、共享断路器、manifest 模型目录；OpenAI-compatible、DeepSeek、智谱与 DashScope/千问 adapter | 真实凭据/区域/模型健康测试 `environment_pending`；STT/数字人待选型 |
| 3 题库查询和候选池 | ✅ closed | Question Catalog，Memory/SQLite/PostgreSQL 查询实现，旧向量题库 interface 已删除 | 真实 PostgreSQL `EXPLAIN` 待环境验收 |
| 4 岗位要求和面试计划 | ✅ closed | execution v2 canonical 槽位、显式迁移、冻结候选池、人工编辑/审批、统一物化 | 部署旧数据须先运行迁移命令 |
| 5 面试会话和实时事件 | ✅ verified | 生命周期、持久事件、WebSocket、Redis bus、心跳超时 | WebRTC `external_choice_required` |
| 6 数字人和语音能力 | ✅ verified（TTS adapter/语音协议） | streaming/batch STT、OpenAI-compatible/智谱 GLM-TTS/DashScope TTS、私有复制、avatar seam、batch 修复 | TTS 外部联调 `environment_pending`；STT/视频 adapter 待选型 |
| 7 评分和报告 | ✅ verified | 解释性评分、current-only revision、JSON/CSV 导出 | 金标校准需要业务样本 |
| 8 岗位与岗位题库构建 | ✅ verified | import/rebuild/build、Outbox、语音版本/readiness | 真实 TTS `environment_pending` |
| 9 企业简历库与 AI 经历问题 | ✅ verified | PDF/URL、SSRF/扫描/解析、私有原件与解析文本、加密联系人、本地/OSS contract | OSS/扫描器 `environment_pending` |
| 10 候选人专属计划、预约与填报匹配 | ✅ verified | 哈希 token、邀请 UI、强匹配、同意、时间/设备/model gate、原子 start、候选人窄接口与安全投影、RBAC/审计/PG 约束 | 邮件/短信 `external_choice_required` |
| 11 可审计随机抽题与服务端语音闭环 | ✅ verified | HMAC 选择、唯一事实、stream final、batch 修复、两阶段评分 | 真实 STT 指标待外部录音集 |
| 12 客观报告与企业复核 | ✅ verified | 签名音频、实际下载审计、reviewer 权限、revision、导出 | ATS 决定不属于 AI 报告 |
| 12.5 核心一致性与隐私修复 | ✅ closed | 六项修复、显式迁移、旧 interface 删除与端到端回归 | 外部环境验收单独列示 |
| 13 生产化与公平性加固 | ✅ verified（仓库） | PostgreSQL/RLS、加密/RBAC/审计、Outbox、Redis、心跳、公平性 | 外部服务均按实际状态待验收 |

下面各里程碑保留“目标—任务—验收”作为已经执行的规格与回归基线，不是未领取的 TODO；当前状态以以上表格和文末外部环境清单为准。

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
- 实现 `ProviderConnection`、`ModelConfiguration`、`ModelRoute`、`ModelInvocationLog`。
- 实现 provider catalog、动态表单、厂商连接、分类模型配置、路由配置和模型测试 API。
- 实现可执行的 `mock`、`openai_compatible`、`deepseek`、`zhipuai` 和 `dashscope` provider adapter；共享 OpenAI-compatible runtime 吸收 HTTP/鉴权/错误/结构化输出，manifest 驱动默认配置和模型目录。
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
- 面试官可以删除或调整草稿槽位，批准后计划不可原地修改。

## 里程碑 5：面试会话和实时事件

目标：完成一次由预约准入、服务端语音转写驱动的面试。

建议任务：

- 实现 `InterviewSession`、`InterviewTurn`、`CandidateAnswer`。
- 通过统一生命周期命令实现创建、启动、暂停/超时恢复、跳题、结束和取消。
- 实现 WebSocket 事件包络、持久领域事件和会话状态同步，REST 与 WebSocket 共用迁移规则。
- 候选人只提交录音或服务端 streaming STT 音频；浏览器 partial 仅作显示，不能形成答案。

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

- 实现 `CandidateProfile` 和不可变 `ResumeDocument`；简历当前只接受 PDF。
- 定义 Private File Storage deep module，在相同 interface 下提供 `LocalPrivateFileAdapter` 和 `AliyunOssFileAdapter`；业务代码不直接依赖路径、bucket URL 或 `oss2`。
- 实现两种 PDF 摄取入口：浏览器 multipart 本地上传，以及公开 HTTPS URL 异步导入；两者共用隔离、哈希、类型校验、恶意文件扫描、私有存储和 PDF 解析流水线。
- URL 导入实现 SSRF 防护、逐跳重定向校验、DNS/IP 校验、大小/超时限制和来源 URL 脱敏；不支持认证 URL 或企业私网 URL。
- 加密联系方式并建立带租户盐的精确查找哈希。
- 实现 Resume Review deep module：脱敏、项目/职责/技能证据提取、证据位置和告警。
- 生成 `ExperienceQuestion` 草稿，支持人工编辑、批准/拒绝和批准后的语音生成。
- 为 `resume_review` 和 `resume_experience_question_generation` 配置独立 route、prompt/schema revision 和审计。

验收：

- 同一业务流程可以从本地 PDF 和公开 HTTPS PDF URL 创建 `ResumeDocument`；成功后均只读取系统托管文件，不依赖原 URL。
- 本地开发文件不暴露在静态目录；切换到阿里云 OSS 只改配置和 adapter，API、Resume Review 与历史 `ResumeDocument` 语义不变。
- 非 PDF、超限、恶意文件、私网/环回 URL、重定向绕过和下载超时均失败关闭，并保留可审计失败码；重试不重复创建版本或对象。
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

## 里程碑 12.5：核心一致性与隐私修复

目标：先消除深度审查发现的执行真相分裂、准入缺口和 revision 污染，再扩展生产能力。详细修改步骤与测试矩阵见 [已知问题与修复设计](known-issues-and-remediation.md)。

完成结果（2026-08-25）：

- `PLAN-001`：已由 Interview Plan Assembly 统一 execution v2 编辑、审批与执行物化；旧数据使用显式一次性迁移，旧 `items` runtime interface 已删除。
- `CONSENT-001`：已校验服务端允许的实际告知版本、明确隐私/录音同意，并冻结服务端时间和 notice hash。
- `APPOINTMENT-001`：已由 Appointment Admission 在最终事务内校验时间窗、同意、设备/模型 readiness，消费预约并唯一创建/START 会话。
- `REPORT-001`：已物化各答案的 current evaluation 集合，报告所有字段只从该集合推导。
- `SEARCH-001`：公开搜索和计划装配已共用 Question Catalog，结构化过滤已下推 Memory/SQLite Persistence backend。
- `CANDIDATE-ACCESS-001`：候选人页面已改用 token 保护的 public 窄接口和 allow-list 投影，不再依赖后台 Bearer API，也不暴露标准答案、rubric、候选池或未来题干。

验收：

- 人工编辑并批准计划后，预约执行同一 revision；旧计划显式迁移保持题目、权重和阶段语义。
- 拒绝/缺失同意、窗口外 start、过期 readiness 均失败且无部分写入；并发 start 只创建一个会话。
- 被替代的历史评分不影响当前报告；当前 `evaluation_ids` 与答案当前指针集合一致。
- 未配置 embedding 时，公开结构化搜索、候选池装配和预约闭环全部通过。
- 六项代码、显式迁移、旧 interface 删除、contract/回归测试和文档已同时完成，本里程碑按仓库范围标记 `closed`；外部服务状态不随之改变。

## 里程碑 13：生产化与公平性加固

目标：把新闭环变成可试点系统。

完成结果（仓库范围，2026-08-25）：

- 13A：`FileObject + PrivateFileStorage`、multipart/URL PDF、逐跳 SSRF、隔离/扫描/解析、原件/解析文本私有化、双入口任务状态 UI 和旧 `resume_text` 删除均完成。
- 13B：本地私有 adapter、阿里云 OSS adapter、SSE/header、短期签名和同 interface contract 完成；真实 bucket/RAM/区域为 `environment_pending`。
- 13C：`stt.streaming` schema/open_stream/WebSocket、唯一 final、断流 batch 修复、OpenAI-compatible/智谱 GLM-TTS/DashScope TTS、非 mock 私有复制和生产 readiness TTL 完成；外部凭据/区域/音质验收为 `environment_pending`，真实 STT/数字人仍需选型。
- 13D：PostgreSQL/RLS migration、RBAC、HTTP/敏感访问审计、联系人/凭证加密、到期数据 dry-run/显式清理、Outbox 指数退避/dead-letter/指标/重放、共享断路器、Redis bus 和心跳监控完成。
- 13E：报告导出、复核签名媒体、实际下载审计和抽题公平性 API 完成；邮件/短信、WebRTC、视频数字人和真实金标评估需要外部通道、厂商与业务样本。
- 13F：模型配置完成 ProviderConnection/ModelConfiguration/ModelRoute 分层，插件后端动态表单、模型健康探针、单向数据迁移和旧接口删除均完成；真实厂商联调保持 `environment_pending`。

仓库验收：

- 本地/URL PDF 的成功、幂等、无效签名、恶意文件、环回 URL、私有下载和本地/OSS contract 测试通过。
- streaming final 端到端、断流修复边界、worker dead-letter/replay、持久共享断路器、RBAC/未认证审计、敏感 URL token 脱敏、媒体下载审计均有自动化测试。
- PostgreSQL migration 的唯一约束/RLS 不变量已离线验证；真实集群 contract 和 `EXPLAIN` 必须在部署环境补验。
- 公平性服务可比较同岗位题量、难度与技能覆盖；报告继续强制人类最终决策。

## 后续环境验收顺序

这些步骤不是仓库代码缺口，只有外部条件就绪后才能执行：

1. 在目标 PostgreSQL/Redis 上执行迁移、RLS 跨租户、并发/故障恢复和多实例广播测试。
2. 在私有阿里云 OSS bucket 与真实扫描器上执行上传、SSE、签名过期、感染文件和迁移演练。
3. 为 OpenAI-compatible 或 DashScope 提供真实凭据、区域和模型，验证 LLM schema、TTS 音质/延迟/费用、私有资产复制与 readiness 失效；另行选择 STT 厂商，用真实录音测 WER、partial/final 延迟和断流 batch 修复。
4. 选择邮件/短信通道、WebRTC/SFU 和视频数字人供应商后，再实现其专属 adapter、模板、会话映射和合规验收。
5. 用企业人工金标建立题目难度、Resume Review 证据准确率及 AI/人工评分一致性基线；录用结果不能直接当作无偏标签。

正式服务端 STT 是生产预约的必要能力；WebRTC/视频数字人不阻塞已验证的 WebSocket 音频闭环。向量数据库也不属于必做项，只有题库治理出现可测量需求后再单独立项。
