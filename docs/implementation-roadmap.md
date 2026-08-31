# 实施路线图

本文用于指导后续 AI 协作者拆任务。优先做能跑通业务闭环的 MVP，再逐步增强实时体验和评分质量。

## 当前完成快照

里程碑 0-13 的仓库内实现已经落地并持续通过自动化测试。当前闭环包括 React Web 工作台、题库批量构建、PDF/URL 简历安全摄取与私有文件、脱敏 Resume Review、可解释岗位初筛/人工复核/7 天差异化留存、候选人 CRUD、候选人专属计划、预约邀请页/明确同意/准入、候选人 token 安全投影、HMAC 稳定随机抽题、服务端 streaming/batch STT、受控实时语音追问、异步逐题评分、预约级自研/云数字人选择与统一降级、报告导出、企业复核、RBAC/审计/公开限流、到期数据清理、PostgreSQL/RLS adapter、Outbox dead-letter、Redis 实时事件、心跳监控、抽题与评分公平性评估，以及 OpenAI、OpenAI-compatible、DeepSeek、智谱 Chat/GLM-TTS、DashScope/千问和腾讯云数智人 adapter。

状态必须分成“仓库 verified”“本机集成 verified”和“目标环境 pending”：实时媒体已实现 OpenAI Realtime、DashScope Qwen Realtime/ASR 与腾讯云智能数智人 WebRTC/SFU，但没有真实账号、Workspace、形象资产、并发、路由健康和测试样本时不能完成生产联调；火山引擎豆包虽有官方实时语音产品，本仓库仍缺其二进制会话 adapter，必须保持 `TODO/not implemented`。PostgreSQL 16、Redis 7 和官方 ClamAV daemon 已通过本机隔离集成测试，目标集群、阿里云 OSS、完整病毒库更新与告警仍保留外部边界。`INTERVIEWER_RUNTIME_ENV=production` 要求私有对象存储、显式非 mock 且近期健康的语音/评分 route、扫描器和生产密钥，否则邀请/start 失败关闭。

## 里程碑状态标记

| 里程碑 | 状态 | 已完成范围 | 未满足的主要验收 |
| --- | --- | --- | --- |
| 0 项目骨架 | ✅ verified | FastAPI、`healthz/readyz`、测试、启动和 worker 命令 | 观测平台由部署环境选择 |
| 1 题库管理 | ✅ verified | CRUD/归档、JSON import、rebuild/build job、语音重建 | 无仓库阻塞 |
| 2 模型网关和供应商配置 | ✅ verified | chat/embedding/STT/TTS/avatar/realtime-speech schema、invoke/open_stream/open_speech_dialogue、加密凭证、共享断路器、manifest 模型目录；OpenAI Realtime、DashScope Qwen Realtime/ASR 与腾讯云数智人 adapter | 真实凭据/区域/模型/形象授权和健康测试 `environment_pending`；豆包实时二进制 adapter 为显式 TODO |
| 3 题库查询和候选池 | ✅ closed | Question Catalog，Memory/SQLite/PostgreSQL 查询实现，本机 PostgreSQL 16 RLS/索引 `EXPLAIN`，旧向量题库 interface 已删除 | 目标生产集群需复验 |
| 4 岗位要求和面试计划 | ✅ closed | execution v2 canonical 槽位、显式迁移、冻结候选池、API 草稿编辑/审批、React 一次确认原子生成并启用、统一物化 | 部署旧数据须先运行迁移命令 |
| 5 面试会话和实时事件 | ✅ verified（仓库） | 生命周期、父子追问轮次/预算、持久事件、WebSocket、Redis bus、心跳超时、16k PCM 实时 ASR、完整录音修复、异步评分、S2S delta 与统一本地/云播放 runtime | 目标网络、首音/打断和厂商并发 `environment_pending` |
| 6 数字人和语音能力 | ✅ verified（仓库） | 预约级 local/cloud AvatarDelivery、自研形象复用冻结 TTS 与签名私有音频、云失败降级、OpenAI/DashScope realtime speech、DashScope streaming/batch STT、真实 TTS、腾讯云 WebRTC | OpenAI/阿里/腾讯真实账号与指标验收 `environment_pending`；更高精度本地口型/3D 为后续体验扩展 |
| 7 评分和报告 | ✅ verified（仓库） | 解释性评分、current-only revision、JSON/CSV 导出、脱敏人工金标校准 API | 真实金标 `data_pending` |
| 8 岗位与岗位题库构建 | ✅ verified | import/rebuild/build、Outbox、语音版本/readiness | 真实 TTS `environment_pending` |
| 9 企业简历库、岗位初筛与个人题库 | ✅ verified | PDF/URL、SSRF/扫描/解析、私有原件与解析文本、候选人及简历版本 CRUD、受控简历展示、可解释初筛/人工复核、AI/人工经历题 CRUD/审核/归档、7 天自动留存、加密联系人、本地/OSS contract | OSS/扫描器与真实简历校准 `environment_pending` |
| 10 候选人专属计划、预约与填报匹配 | ✅ verified | 哈希 token、邀请 UI、强匹配、确认预约、提前 30 分钟 SMTP 提醒、同意、时间/设备/model gate、原子 start、候选人窄接口与安全投影、RBAC/审计/PG 约束 | SMTP 凭据/发件域名 `environment_pending`；短信 `external_choice_required` |
| 11 可审计随机抽题与服务端语音闭环 | ✅ verified | HMAC 选择、唯一事实、stream final、batch 修复、两阶段评分 | 真实 STT 指标待外部录音集 |
| 12 客观报告与企业复核 | ✅ verified | 签名音频、实际下载审计、reviewer 权限、revision、导出 | ATS 决定不属于 AI 报告 |
| 12.5 核心一致性与隐私修复 | ✅ closed | 六项修复、显式迁移、旧 interface 删除与端到端回归 | 外部环境验收单独列示 |
| 13 生产化与公平性加固 | ✅ verified（仓库） | PostgreSQL/RLS、加密/RBAC/审计、Outbox、Redis、心跳、公平性 | 外部服务均按实际状态待验收 |
| 14 React 前端迁移 | ✅ verified | React 19/Vite、WorkbenchProvider、六个 feature components/hooks、统一 HTTP/聚合查询/录音状态机、角色导航与行为测试；旧 controller 已删除 | 真实浏览器媒体权限和外部 Provider 仍随部署环境验收 |
| 15 题库级 TTS 配置与 Celery 工作流 | ✅ verified（仓库与本机） | 领域、默认 route 初始化、API、worker、幂等、分层 UI、试听解释、引用保护和验收测试已落地 | 目标部署的真实 Redis/TTS/PostgreSQL 继续做环境验收 |
| 17 智能生题任务工作台 | ✅ verified（仓库与本机） | 独立路由、批次/Worker 投影、停止/继续、失败/分片重试、输出截断自适应恢复、revision 防迟到和审核导入 | 供应商在途请求撤销、真实截断恢复与目标账户成本/并发仍需环境验收 |

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
- 把评分从答题请求拆成 DurableWorkItem/Outbox，`answer.accepted` 不等待完整 LLM；追问作为零权重子轮次，受深度、次数、总量和回答门槛控制。

验收：

- 候选人可以进入面试房间并完成多轮问答。
- 刷新页面后能恢复当前面试状态。

## 里程碑 6：数字人和语音能力

目标：把读题和语音识别从业务逻辑中解耦。

建议任务：

- 定义 `stt.streaming`、`stt.batch`、`tts.synthesize`、`avatar.speak`、`speech.dialogue_realtime` 统一请求/响应。
- 实现语音和数字人 provider 插件接口，通过模型网关调用。
- 实现 mock 数字人：先返回 TTS 音频或读题文本。
- 实现 `AvatarDelivery` 深模块和预约级 `local/cloud` 选择：Local adapter 复用冻结 TTS 私有音频与浏览器渲染，Cloud adapter 复用统一模型路由；两者共享响应、播放与关闭合同。
- 在真实厂商端点/凭据验收前保留 mock；生产只启用测试通过的 DashScope streaming/batch route。
- `cascade/s2s` 共用追问决策和证据链；S2S 只流式表达已经批准的追问，失败时关闭表达轨并回退 cascade，不允许把自由生成文本写成题目或答案。
- 腾讯云数智人负责云渲染和 SFU；adapter 以 HTTPS 管理会话、以签名 WSS 长连接发送文本驱动，候选人端 TCPlayerLite 拉取 WebRTC 并在离场关闭。

验收：

- 数字人供应商不可用时，系统能降级为文字或普通 TTS。
- 新预约默认自研数字人且不创建云会话；显式云模式保留旧链路，失败时返回实际 local 模式和可观察原因。
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

- 实现 `JobPosition`、题库 API 和前端工作区；题库由组织统一维护，通过显式 assignment 关联到一个或多个岗位，关联后复用题目与题库语音配置。
- 岗位支持增查改删；新工作台在同一事务创建岗位与首版岗位要求，新候选人明确选择应聘岗位；危险删除先预览实际级联数量并要求岗位名确认，再通过留存清理 seam 清除岗位候选人敏感数据。共享题库和历史不可识别快照不随岗位删除。
- 迁移现有 `knowledge_base_id` 数据，并在 repository 和搜索中强制 `organization + position + knowledge base` 过滤。
- 扩展 Outbox 为题库导入、结构化字段/评分依据校验和 `QuestionSpeechAsset` 构建流水线。
- 定义 `tts.synthesize` 统一 schema，接入一个真实 TTS adapter，把供应商结果复制到私有对象存储。
- 实现题库 `draft/building/ready/failed/archived` 状态、失败题目重试和 readiness 摘要。

验收：

- 一个岗位可以有多个题库，任何跨岗位创建、搜索或计划引用都被拒绝。
- 上传题库立即返回工作项；结构化字段/评分依据校验和题目语音全部完成后题库才进入 `ready`。
- 修改题干、语言或音色会生成新语音版本，历史资产不被覆盖。

## 里程碑 9：企业简历库与 AI 经历问题

目标：企业维护候选人和简历，AI 面向指定岗位异步生成可复核的初筛建议；仅对生效结论符合的候选人生成与简历证据严格绑定的项目经历问题。

建议任务：

- 实现 `CandidateProfile` 和不可变 `ResumeDocument`；简历当前只接受 PDF。
- 定义 Private File Storage deep module，在相同 interface 下提供 `LocalPrivateFileAdapter` 和 `AliyunOssFileAdapter`；业务代码不直接依赖路径、bucket URL 或 `oss2`。
- 实现两种 PDF 摄取入口：浏览器 multipart 本地上传，以及公开 HTTPS URL 异步导入；两者共用隔离、哈希、类型校验、恶意文件扫描、私有存储和 PDF 解析流水线。
- URL 导入实现 SSRF 防护、逐跳重定向校验、DNS/IP 校验、大小/超时限制和来源 URL 脱敏；不支持认证 URL 或企业私网 URL。
- 加密联系方式并建立带租户盐的精确查找哈希。
- 实现 Resume Review deep module：调用方只排队/查询；内部负责脱敏、Token 预算、短简历单次审阅、长简历页感知 Map/分层压缩/最终 Reduce、证据来源页和结构化失败。
- Resume Review 同时输出 `qualified/unqualified/manual_review` 建议、分数、命中要求和缺口；领域策略强制将 0–59/60–74/75–100 分别归一化为不符合/待人工复核/符合，候选人列表展示生效结果，人工复核保留 AI 建议和审计。
- Resume Review 失败时由业务 retry 命令校验审阅 version、源简历和配置状态，把审阅与 dead-letter 工作原子恢复排队；React 直接展示中文失败原因和重试入口，不能要求招聘人员使用通用 Outbox 管理接口。
- Resume Review 工作幂等键绑定不可变简历版本；重复上传相同 PDF 不得复用旧版本工作，queued 审阅缺失工作项时排队命令必须自愈补建。
- Resume Review 的结构化输出预算独立于输入分块预算；任务租约必须长于 Worker hard time limit，避免合法长调用被重复领取。
- Resume Review 最终 Schema 必须对证据和要求设置数量/长度上界；预算内单次输出截断时自动转入页感知 Map/Reduce，不保存半截 JSON 也不同参数盲目重试。经历题使用符合资格后的独立 Prompt/Schema。
- 候选人支持增查改删；简历版本支持创建、列表/详情/短期受控查看、展示名修改和受引用保护的删除。PDF 内容更新只能重新上传为新版本；删除会取消未运行工作并清理隔离/私有对象。所有最新岗位结论均不符合时设置 7 天期限，周期留存任务自动清除，符合或待复核者取消期限。
- 只有生效结论符合时才生成 `ExperienceQuestion` 草稿；AI 不符合/待复核不生成，人工改判符合时排队。`CandidateQuestionBank` 作为按候选人聚合的简历问答投影，强制每题绑定并点名 ResumeReview 证据，支持 AI/人工来源、人工新建/编辑、批准/拒绝、归档和批准后的语音生成，不复制岗位 Question 实体。
- 为 `resume_review` 和 `resume_experience_question_generation` 配置独立 route、prompt/schema revision 和审计。

验收：

- 同一业务流程可以从本地 PDF 和公开 HTTPS PDF URL 创建 `ResumeDocument`；成功后均只读取系统托管文件，不依赖原 URL。
- 本地开发文件不暴露在静态目录；切换到阿里云 OSS 只改配置和 adapter，API、Resume Review 与历史 `ResumeDocument` 语义不变。
- 非 PDF、超限、恶意文件、私网/环回 URL、重定向绕过和下载超时均失败关闭，并保留可审计失败码；重试不重复创建版本或对象。
- 同一简历可针对不同岗位生成独立审阅，输入版本相同的重试幂等。
- 相同内容的两个 ResumeDocument 版本各自拥有指向正确审阅的工作项；任何 queued 审阅都不能缺少 DurableWorkItem。
- HTTP 提交不等待 LLM；上传携带岗位要求时，简历摄取成功和审阅排队在同一事务完成。长简历后页证据必须进入最终结论，任何分块失败不得生成部分淘汰结论。
- 每个经历问题都含不可变证据快照，题干明确写出所引项目/技能名称；无简历依据的通用题在生成、人工创建、读取与组卷边界被拒绝，未经人工批准不能进入计划。
- 面试官可从候选人列表外层“简历问答”弹窗看到符合候选人的有效问题，并基于该候选人的符合 Resume Review 新建、编辑、批准/拒绝和删除人工题；编辑/删除位于三点菜单，归档不破坏已冻结计划和历史面试。
- AI 输入不包含照片和与工作能力无关的受保护属性，不直接生成录用结论。
- 初筛入选/淘汰依据可查看并可人工覆盖；59/60/74/75 边界值与模型建议冲突均有自动化验收，不符合者到期自动清除，符合或待复核者不会被初筛留存任务选中。
- 模型超时、限流、断路等失败不会形成不符合结论；面试官可重试 failed/dead-letter 审阅，重复点击或状态已变化时返回 version/status 冲突，审计可追溯操作者和工作项。
- 面试官可在候选人详情完成简历创建、查看、改名和删除；过期 version 返回冲突，待处理工作与私有文件随删除清理，已进入计划/面试历史的版本拒绝删除。

## 里程碑 10：候选人专属计划、预约与填报匹配

目标：面试官选择岗位题库和候选人生成计划，预约后由候选人安全登记并 self-start。

建议任务：

- 把 Interview Plan Assembly 调整为岗位题库 `bank_slots`、冻结候选池和已批准经历问题。
- 实现 `InterviewAppointment`、readiness gate、一次性 invitation token 哈希、过期、撤销和消费。
- 实现公开邀请页和 Candidate Intake；候选人填写姓名、邮箱、手机号与同意信息。
- 将“核验身份并确认预约”与“检查设备并进入面试”拆开；确认事务创建开始前 30 分钟的邮件提醒 DurableWorkItem，SMTP 授权码只从部署环境读取。
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
- 13C：`stt.streaming` schema/open_stream/WebSocket、唯一 final、断流 batch 修复、OpenAI-compatible/智谱 GLM-TTS/DashScope TTS、`media_http` 真实 STT/HTTPS 数字人、候选人录音 FileObject、非 mock 私有复制和生产 readiness TTL 完成；外部凭据/区域/音质/延迟验收为 `environment_pending`。
- 13D：PostgreSQL/RLS migration、RBAC、HTTP/敏感访问审计、联系人/凭证加密、到期数据 dry-run/显式清理、Outbox 指数退避/dead-letter/指标/重放、共享断路器、Redis bus 和心跳监控完成。
- 13E：报告导出、复核签名媒体、实际下载审计、抽题公平性 API、脱敏评分校准 API、腾讯 WebRTC 数智人控制/播放链路完成；邮件/短信送达、真实数智人联调和真实金标结论需要外部通道、凭据、授权资产与业务样本。
- 13F：模型配置完成 ProviderConnection/ModelConfiguration/ModelRoute 分层，插件后端动态表单、模型健康探针、单向数据迁移和旧接口删除均完成；真实厂商联调保持 `environment_pending`。

仓库验收：

- 本地/URL PDF 的成功、幂等、无效签名、恶意文件、环回 URL、私有下载和本地/OSS contract 测试通过。
- streaming final 端到端、断流修复边界、worker dead-letter/replay、持久共享断路器、RBAC/未认证审计、敏感 URL token 脱敏、媒体下载审计均有自动化测试。
- PostgreSQL migration 的唯一约束/RLS 不变量已离线验证；真实集群 contract 和 `EXPLAIN` 必须在部署环境补验。
- 公平性服务可比较同岗位题量、难度与技能覆盖；报告继续强制人类最终决策。

## 里程碑 15：题库级 TTS 配置与 Celery 工作流

目标：让题库成为题目与读题语音的管理边界；管理员可以为每个题库选择具体 TTS 模型和声音，任何影响音频输出的切换都通过 Celery 安全地整库重建。

实施顺序：

1. **数据与迁移**：为 KnowledgeBase 增加 KnowledgeBaseSpeechProfile/revision/build status；QuestionSpeechAsset 增加 profile revision 和 ModelConfiguration ID/version。迁移从现有 `language + voice_profile_id` 和 `tts.synthesize/question_speech_generation` route primary 解析默认 profile；无法解析时标 `configuration_required`，不猜测模型。
2. **模型声音目录**：Model Administration 提供仅限 `ready + enabled + tts.synthesize` 的模型查询和 `/model-configurations/{id}/voices`；Provider manifest 静态 voices、管理员 voice_map 和可选 adapter 远程目录归一化为同一结构。
3. **Question Speech Build deep module**：配置命令以 CAS/Idempotency-Key 产生 profile revision 和父 DurableWorkItem；父工作冻结题目 manifest、按批创建子工作，子工作使用显式单模型 route 生成私有不可变资产，提交前执行 question/profile revision guard。
4. **Celery 基础设施**：新增 `app/workers/celery_app.py`、`dispatcher.py` 和 `knowledge_base_speech.py`；Web/service 不定义 Celery task、不直接执行外部 TTS。Redis broker 调度，Celery Beat 补发 due/expired work；数据库 DurableWorkItem 保持任务真相。
5. **API 改异步**：创建题目、单题语音重建、整库配置切换全部返回 `202 + job_id`；删除当前 `await process_speech_work()` 请求内执行路径。新增 SpeechBuild 列表/详情/只重试失败项。
6. **React 路由重构**：`#questions` 只列题库；`#questions/{kb_id}` 展示题目和语音设置。配置表单按选择模型加载 voice catalog，保存前展示影响题目数、预计会重建和可能产生费用；页面轮询/刷新构建进度并显示失败项。
7. **资源与依赖完整性**：仍被题库 profile 引用的 TTS ModelConfiguration 不允许删除；TTS settings 改动让引用题库显示 `rebuild_required` 并要求显式确认。旧资产由历史计划/会话继续引用，未引用资产再按留存策略回收。

验收：

- 题库首页请求不下载全组织 Question；进入一个题库只加载该题库题目。
- 只有健康 TTS 模型和其合法声音可以保存；相同配置幂等，不产生新 revision 或重复费用。
- 切换模型、声音、语言、格式或语速会为所有活动题创建新 revision 工作；新增/编辑单题只重建该题。
- 题库详情展示已添加但尚不可选的 TTS 及原因，可显式测试后刷新为可选；题目具备查看、新建、编辑、归档删除和当前语音资产受控试听，归档题的待执行语音任务不调用 Provider。
- 新题库从明确、enabled 且 ready 的 `question_speech_generation` route 初始化实际模型和默认音色；开发 mock 只验证流程，页面不得把 mock URI 标为可试听，并通过 `speech_preview` 给出普通用户可执行的配置/重试提示。
- 任务重复投递、乱序完成、worker crash、配置在构建中再次切换均不能让旧资产覆盖当前指针。
- 部分失败可只重试失败题；全部当前资产 ready 后题库才恢复 ready，预约 readiness 使用冻结 profile revision。
- Celery 不可用时已提交工作项仍保留，恢复后由 Beat dispatcher 补发；管理员状态和 replay 不依赖 Celery result backend。
- Memory/SQLite/PostgreSQL contract、Celery eager/真实 Redis broker 集成、Provider fake、React 行为和端到端计划/历史资产测试全部通过后，`KB-SPEECH-001` 才能标记 verified。

## 里程碑 16：AI 题目创作与人工审核

目标：面试官可基于题库定位、标签和可选要求批量生成候选题，先审核再一次性导入正式题库。

完成结果（2026-08-27）：

- KnowledgeBase 保存可复用的 `positioning/tags`；生题选项只暴露 ready `llm.chat_json` 模型。
- HTTP 以 202 创建 QuestionGenerationBatch 和 DurableWorkItem，不执行 LLM；Celery 通用 dispatcher/worker 执行 `plan -> generate_chunk -> merge` 持久工作流。
- AI 先规划与目标数量一致的互斥蓝图；每个子任务最多生成两题。严格 Schema、槽位完整性、评分依据、正式题库/批内去重通过后才形成 GeneratedQuestionDraft；缺失槽位最多补生成两轮。
- `question_blueprint_generation.v2` 对题目字段设置输出边界；Provider 明确区分 token 截断与普通 Schema 错误。每个生题工作 attempt 只进行一次外部调用，双槽位截断在同一批次/revision 内自动拆为两个单槽位工作，单槽位仍截断才终止并等待人工重试。
- 审核态支持 CAS 修改和删除；确认导入冻结剩余草稿，以独立幂等工作项创建正式 Question，并自动进入已有题目 TTS 链路。
- React 题库详情提供生成表单、规划/子任务/合并/补生成进度、自动刷新、候选题列表、编辑/删除和确认导入；模型不可用、任务失败和数量不足均给出明确状态。
- Memory/SQLite/PostgreSQL 共用 versioned document collection；正式 Question 记录批次/草稿来源，历史数据字段可空。

验收：生成前后正式题库隔离、生成/导入幂等、草稿 CRUD、错误模型拒绝、来源追踪、Celery 执行、React 行为与全量回归均有自动化证据。2026-08-27 当前 DeepSeek ready 配置通过真实 Celery 调用生成 1 道候选题并停留在审核态，正式题库仍为 1 道；真实供应商的持续费用、限流和 JSON Schema 稳定性仍在目标 Provider 环境验收，不影响仓库实现完成状态。

## 里程碑 17：智能生题任务工作台

目标：让面试官在独立页面管理一个题库的全部生题批次，并安全停止、恢复或重试失败子任务。

完成结果（2026-08-27）：

- 题库详情的“AI 智能生题”导航到 `#questions/{knowledge_base_id}/generation`；任务详情使用可分享的 batch 子路由，路由级只加载题库、生题选项、批次列表和所选批次。
- 批次投影把 DurableWorkItem 收敛成 `tasks/available_actions`；React 正常态只消费紧凑进度和命令，审核态优先展示候选题，只有失败项才展开错误码、retryability 与分片重试，不暴露成功 Worker 流程、Prompt、响应原文或凭据。
- QuestionGenerationService 统一提供 stop、resume、retry-failed、retry-chunk interface。停止是数据库事实，未领取工作取消，在途工作以 execution revision guard 丢弃迟到结果；不使用 Celery terminate 作为真相来源。
- 恢复和重试创建新的幂等工作，保留完成分片、旧失败和人工控制历史；草稿审核、删除和导入仍使用原 CAS/幂等合同。
- 候选题行可查看完整详情；单题导入与批量导入均经 Celery/Outbox，单题成功只冻结对应草稿，其他候选继续可审核，批量导入自动排除已导入项。
- 新建任务配置统一由“新建生题任务”按钮打开 Modal，不在工作台正文展开；任务历史和候选题始终是页面主体。
- Python 全量测试、Vitest、Vite build、compileall、diff check 与本地 API/Redis/Celery/浏览器投影验收通过。

外部边界：供应商已经接受的在途 HTTP 请求可能继续执行和计费，系统只能保证停止后不再发起新调用且迟到结果不入库；目标生产账户的并发、限流和成本仍需部署环境验收。

## 后续环境验收顺序

这些步骤不是仓库代码缺口，只有外部条件就绪后才能执行：

1. 在目标 PostgreSQL/Redis 上执行迁移、RLS 跨租户、并发/故障恢复和多实例广播测试。
2. 在私有阿里云 OSS bucket 与真实扫描器上执行上传、SSE、签名过期、感染文件和迁移演练。
3. 为 OpenAI、OpenAI-compatible、DashScope 或 `media_http` 提供真实凭据、区域、模型和端点，验证 LLM schema、Realtime 首音/打断、TTS/数字人音质、STT WER、final 延迟/费用、私有资产复制和 readiness 失效。若选择豆包实时语音，必须先在现有 `speech.dialogue_realtime` seam 内实现并验证其官方二进制会话协议，不能仅修改 manifest 为 ready。
4. 为已实现的 SMTP 提醒配置目标服务授权码、发件域名并完成退信/送达率/合规验收；自研数字人已可作为默认低成本路径，后续可在保留 AvatarDelivery interface 的前提下扩展更精确口型、Live2D/3D 或自建 WHEP。为可选腾讯云 WebRTC/SFU 配置 AppKey、AccessToken、形象资产与并发，在目标浏览器完成建流、口型、回收、费用和合规验收；短信通道仍待选择。普通 HTTPS 数字人视频继续由 `media_http` 支持。
5. 用企业人工金标建立题目难度、Resume Review 证据准确率及 AI/人工评分一致性基线；录用结果不能直接当作无偏标签。

正式服务端 STT 是生产预约的必要能力；S2S 是可选的低延迟表达轨，不取代 STT 和完整评分。默认自研数字人不依赖腾讯云，但仍要求真实 TTS 冻结音频和私有文件存储就绪。OpenAI/DashScope realtime speech、DashScope STT 与可选腾讯 WebRTC 数智人已有仓库实现，在真实凭据、授权资产、目标网络和指标验收前不能把对应外部 route 视为生产就绪。向量数据库不属于必做项，只有题库治理出现可测量需求后再单独立项。
