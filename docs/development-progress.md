# 开发进度

## 2026-08-31：流式模型健康探针与 Qwen Realtime 协议校正

- 模型配置和路由测试现已覆盖 `stt.streaming` 与 `speech.dialogue_realtime`：两者复用统一 stream handshake probe，在真实 Provider 确认端点、凭据、模型访问和 session 后置为 ready，不再要求静音样本产生 final transcript。
- DashScope Qwen 3.5 Omni Realtime 已同步当前 session 结构、`qwen3-asr-flash-realtime` 输入转写模型和 `Tina` 默认音色；历史配置在 adapter seam 内兼容归一化，管理员不需要先迁移数据库记录。
- 健康探针只证明连接与 session 初始化；真实 WER、final 延迟、首音、打断、音质和费用仍属于部署环境的脱敏样本验收，不因本项自动标记完成。

## 2026-08-30：受控实时语音追问与异步评分

- 实际面试已形成两条可选表达链路：`cascade` 保留服务端 STT → 受控追问 → TTS/本地或云数字人的兼容路径；`s2s` 使用统一 `speech.dialogue_realtime` stream，把已经由服务端规则批准的追问通过 OpenAI Realtime 或阿里云百炼 Qwen Realtime 以 PCM delta 尽快下发。两条路径复用同一 Interview Lifecycle、追问决策、题目快照、事件和前端播放状态机。
- S2S 不取代证据链：同一份候选人 PCM 仍进入权威 streaming/batch STT，唯一 final 形成 CandidateAnswer；完整 LLM 评分由 DurableWorkItem/Outbox 异步执行，先返回 `answer.accepted + evaluation.queued`，评分完成后再广播 `evaluation.completed` 并刷新报告。实时语音输出、浏览器 partial 和 Provider 自由生成文本都不能直接入库为答案或评分。
- 追问是零权重子轮次，带 `parent/root/depth`，只允许深度 1、每道原题 1 次、会话默认最多 2 次，并受回答时间/长度门槛约束。候选人投影只显示“请补充说明”，不暴露目标关键点、标准答案、内部原因或评分。
- 候选人运行时同时修复了自动播题、16 kHz PCM 与私有媒体格式、完整本地录音备份、断线等待后 batch repair、心跳、企业实时事件、`record_video=false` 麦克风降级、异步评分状态和重复播报/提交。
- 新增 OpenAI 官方 provider（Chat/Embedding/TTS/batch STT/Realtime）和 DashScope Qwen Realtime 模型目录/adapter。火山引擎豆包实时语音仅记录为后续扩展：仓库没有把未完成的二进制会话协议伪装成可用 route。

## 2026-08-29：API response/marshal seam 与 transport 拆分

- 新增 FastAPI 适配的声明式 `fields + marshal` 白名单投影，公开候选人详情和答题响应已迁移，内部评分、题目快照、联系方式等未声明字段不会进入响应。
- 集合响应统一为 `items + next_cursor`，显式 202 响应统一走 response factory；业务、Provider、Persistence、认证、限流和 FastAPI 请求校验错误统一为 `error.code/message/details`，请求校验不会回显原输入。
- `app/api/routes.py` 已缩为 router 总装入口；全部路径按 `system/admin/catalog/talent/plans/interviews/realtime` 物理拆入 `app/api/routers/`。Service Locator、HTTP response/fields 和实时连接/跨实例广播全部位于 `app/transport/`，`app/api` 不再混放 transport implementation。

## 2026-08-29 完成快照

仓库内可独立完成的里程碑 0-13 能力已经实现并通过自动化验证：岗位题库构建、PDF/URL 简历安全摄取、岗位初筛/人工复核/7 天差异化留存、候选人 CRUD、AI/人工候选人个人题库、私有文件、候选人/计划/预约、明确同意、可审计随机抽题、服务端 streaming/batch STT、受控实时语音追问、异步评分、报告导出、企业复核、RBAC/审计、Outbox 加固、PostgreSQL/RLS adapter、Redis 事件 adapter、心跳监控和抽题公平性评估均已有代码与测试。

这里的“完成”只表示仓库实现与本地/离线验收完成，不等于外部生产环境已经通过。DashScope 已有 Qwen-Audio 3.0 实时 ASR/Qwen3-ASR batch 和 Qwen Realtime adapter，OpenAI 已有官方 Realtime adapter，腾讯云智能数智人已有 WebRTC 云渲染会话 adapter 与候选人 TCPlayerLite 播放；本机 PostgreSQL 16、Redis 7 和官方 ClamAV daemon 已完成隔离集成验收，但目标 PostgreSQL/Redis、阿里云 OSS、生产扫描签名更新、OpenAI/阿里/腾讯账号、模型/形象授权、实时首音/打断/并发指标和真实脱敏金标仍需要部署环境输入；生产 readiness 在依赖缺失或健康检查过期时失败关闭。

预约现已显式选择“自研数字人 / 云数字人”。新预约默认低成本自研模式：复用计划冻结的题目 TTS 私有资产，服务端签发短期地址，React 内置形象展示统一说话状态，不创建云会话；云模式完整保留腾讯 create/stat/start/WSS drive/WebRTC/SFU/close 链路，并在不可用时通过同一个 LocalAvatarDelivery 降级。历史无 `avatar_mode` 数据继续按云模式解释，既有链路未删除；后端和浏览器分别只有一个 delivery/playback interface，避免两套业务控制代码。

当前统一验证基线：

- `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
- `cd app/web && npm test -- --run && npm run build`：React 生产构建通过，Vitest `32 passed`；此前依赖审计为 0 个 high 漏洞。
- `.venv/bin/python -m pytest -q`：`220 passed, 5 skipped`；5 个环境测试仅在提供真实 PostgreSQL/Redis/clamd 地址时启用。
- `git diff --check`：通过。

### React Web 工作台

- `app/web` 已建立 React 19 + Vite 工程，FastAPI 从 `/web/bundles/*` 同源托管生产 bundle，并保留 `/web/assets/*`、`/web/vendor/*` 和既有业务 API 路径。
- 总览、题库、招聘流程、面试计划、面试会话、模型服务、公开邀请和候选人面试入口已无损接入 React shell；框架切换不改变 REST/WebSocket 路径或请求体。
- 面试计划的“生成并启用”在同一 Plan Assembly 事务内完成就绪校验和批准，工作台不再要求创建人点击“审批计划”；API 仍保留默认草稿模式供需要编辑/分权审批的客户端使用。一次性邀请弹窗提供“复制链接”和即时反馈。
- 创建预约表单增加数字人方案选择，默认“自研数字人（推荐，低成本）”，也可选择“云数字人（实时视频，需配置服务）”；候选人房间展示实际执行模式和云失败降级原因，两种模式共用播放、停止与云 session 回收 runtime。
- 候选人邀请页已把“身份核验并确认预约”与“到点检查设备并进入面试”拆开；确认事务创建提前 30 分钟的持久邮件提醒，队列不携带邮箱明文，SMTP 授权码通过空置的环境变量占位等待部署配置。
- 招聘流程已支持岗位增查改删；删除前展示候选人/岗位要求/计划/预约影响并要求输入完整岗位名称，确认后清除该岗位候选人敏感数据、归档下游流程，同时保留共享题库与不可识别历史。
- 新建岗位弹窗同时采集首版岗位要求、必备/加分技能、目标级别和面试时长；后端原子创建 JobPosition 与 RoleRequirement，创建后可直接用于简历初筛。既有岗位卡片同时提供“添加岗位要求/新增要求版本”，无需删除重建。
- 岗位题库入口始终可用：未关联时显示“关联题库”，已关联时显示“管理题库”。管理弹窗列出当前关系与剩余组织题库；没有新增项时明确说明现有题库已全部关联，并提供题库管理入口，不再用 disabled“暂无可选题库”伪装成故障。
- 候选人和简历版本均支持增查改删：PDF/URL 上传创建不可变内容版本，受控查看，展示名可乐观并发修改；删除会取消未运行工作、清理隔离/私有对象，并保护已进入计划或面试历史的版本。上传接口立即返回，摄取成功后原子排队后台初筛；不同简历版本的审阅工作按 `resume_document_id + input_hash` 隔离，同一 queued 审阅缺少工作时自动补建。短简历单次审阅，长简历按页和 Token 预算执行证据 Map/分层压缩/最终 Reduce；输入与结构化输出预算独立配置，长模型调用使用覆盖 Worker hard limit 的任务租约。失败初筛可在候选人详情通过版本化领域命令重新排队，重置 work attempt 并保留 replay 审计。列表展示处理进度、中文失败原因、符合性、来源页、命中依据和缺口并支持人工复核；服务端统一按 0–59 不符合、60–74 待人工复核、75–100 符合归一化模型建议。仅最终不符合者设置 7 天期限。
- 候选人列表外层现已提供独立“简历问答”弹窗：只有生效初筛结论符合才开放；AI 不符合/待复核不生成，人工改判符合时排入独立工作。AI/人工题都必须绑定审阅中的证据快照且题干点名证据标签；编辑/删除收在三点菜单，批准/拒绝保留为高频操作。无证据旧题、不符合审阅问题和已归档题不会进入读取或新计划。
- Resume Review 最终 Prompt/Schema 已升级为 `resume_review.v6` / `resume_review_reduce.v4`，只负责摘要、证据和岗位要求；预算内单次审阅如果仍以 `provider_output_truncated` 结束，会自动转为完整 Map/Reduce 并记录降级事实，不保存半截 JSON。问题改由 `resume_experience_question_generation.v1` 在符合资格后独立生成。
- 统一 HTTP、路由级 workspace query、schema form 和候选人录音恢复继续作为框架无关 deep modules；题库层级一次聚合加载，总览只取统计和最近 5 条，不再产生岗位/题库 N+1 或首屏完整题库下载。
- `WorkbenchProvider` 已接管认证、hash 路由、角色重定向、加载、刷新、弹窗和 toast；业务组件及命令 hooks 按 `questions/workflow/plans/interviews/models/candidate` 六个 feature 分区，React feature registry 统一导航与角色声明。
- 空工作区可从题库页直接建立岗位与题库，再无缝进入首道题目表单；招聘流程的岗位卡片从组织题库列表建立显式关联，直接复用题目、语音模型和音色，不再重复创建或手填声音。模型连接/配置/路由复用统一 ResourceCard interface，招聘流程复用 panel/table/list interface，保留期清理占位以中文只读状态呈现。搜索工具栏按实际五个控件建立响应式网格，不再换行错位。
- 模型服务已按题库式父子层级组织：首页用卡片目录展示已接入厂商、模型数量、就绪数和模型类型，点击厂商进入独立子页面，在锁定厂商上下文中完成模型增删改查与测试；业务用途和插件清单保留为首页折叠管理区。用途继续使用受控枚举并自动推导能力，只允许选择 enabled + ready 且能力兼容的模型。
- 旧 `app/web/app.js`、HTML 字符串视图和 schema-form DOM renderer 已物理删除；生产入口只加载 Vite bundle。
- 行为测试覆盖生产 Bearer/RBAC/WebSocket ticket、reviewer React 挂载且不访问 `/admin/*`、空工作区首题入口、外部取消/超时、断线录音完整重放，以及预约成功但邀请失败时保留原预约供重试。

## 里程碑对账

| 里程碑 | 仓库状态 | 已完成证据 | 外部/兼容边界 |
| --- | --- | --- | --- |
| 0 项目骨架 | ✅ verified | FastAPI、统一错误、健康检查、启动/worker 命令、自动化测试 | 生产观测平台由部署环境选择 |
| 1 题库管理 | ✅ verified | CRUD/归档、JSON 批量 import、rebuild/build job、语音重建 | 批量 UI 仍以 API 为主 |
| 2 模型网关 | ✅ closed（配置 v2 + Prompt 治理） | `chat_json/chat_text/embedding/STT/TTS/avatar/realtime_speech` schema、invoke/open_stream/open_speech_dialogue、重试/fallback/超时/共享断路器、加密凭证；已实现 OpenAI Realtime、DashScope Qwen Realtime/ASR 与腾讯云数智人 WebRTC adapter | 真实凭据、区域、模型/形象授权、并发和健康测试待联调；豆包二进制实时会话 adapter 为 TODO |
| 3 结构化题库查询 | ✅ closed | Question Catalog、Memory/SQLite/PostgreSQL 下推实现、跨岗位拒绝；旧 QuestionService/向量 repository 已删除 | 真实 PostgreSQL 查询计划待环境验收 |
| 4 岗位要求与计划 | ✅ closed | execution v2 canonical slots、显式一次性迁移、候选池冻结、覆盖/难度/去重、权重/时长守恒、审批不可变 | 部署旧数据时先运行迁移命令 |
| 5 会话与实时事件 | ✅ verified | 生命周期、持久事件、WebSocket、Redis 跨实例 adapter、心跳超时恢复、浏览器 16k PCM 实时 STT、统一 Avatar Delivery Runtime、腾讯 WebRTC/SFU 播放与会话回收 | 目标网络/浏览器与腾讯并发仍需外部验收 |
| 6 数字人与语音 | ✅ verified（仓库） | 预约级 local/cloud、cascade/s2s 选择，冻结 TTS 签名播放、浏览器本地形象、云失败复用 local 降级、OpenAI/DashScope realtime speech、DashScope streaming/batch STT、真实 TTS、腾讯云数智人 WebRTC | OpenAI/阿里/腾讯真实凭据、音质/WER/首音/打断/费用和生产 route 未验收；自研形象后续可扩展口型/3D |
| 7 评分与报告 | ✅ verified | 可解释评分、append-only revision、current-only 汇总、JSON/CSV 导出、脱敏人工金标一致性/公平性校准 API | 真实脱敏金标尚未提供，不能形成业务校准结论 |
| 8 岗位题库构建 | ✅ verified | import/rebuild/build、结构校验、Outbox、语音版本/readiness | 真实 TTS 音质与区域策略待联调 |
| 9 企业简历库 | ✅ verified | multipart/URL、SSRF、隔离/扫描、保留页边界解析、原件/解析文本私有 FileObject、候选人及简历版本 CRUD、异步单次/Map-Reduce 可解释初筛、AI/人工个人题库 CRUD/审核/归档、版本化 0–59/60–74/75–100 分数带、人工复核/7 天自动留存、本地/OSS contract、加密联系人 | 真实 OSS/扫描器、目标模型上下文预算与真实简历初筛校准待环境验收 |
| 10 预约与填报 | ✅ verified | PATCH、哈希 token、邀请 UI、服务端告知/明确同意、强匹配、预约确认、30 分钟 SMTP 邮件提醒、时间/设备/model gate、原子幂等 start、候选人安全投影 | SMTP 凭据与目标邮件服务实发待配置；短信未选择通道 |
| 11 可审计语音闭环 | ✅ verified | HMAC 选题、唯一选择事实、streaming final、batch 修复、两阶段评分 | 真实 STT WER/延迟待录音集 |
| 12 企业复核 | ✅ verified | reviewer 权限、签名音频、授权与实际下载审计、转写/评分/报告 revision、导出 | ATS 人工决定集成不属于 AI 报告 |
| 12.5 核心一致性 | ✅ closed | `PLAN/CONSENT/APPOINTMENT/REPORT/SEARCH/CANDIDATE-ACCESS` 回归矩阵；旧 runtime interface 已删除 | 外部环境验收独立列示 |
| 13 生产化/公平性 | ✅ verified（仓库） | PostgreSQL/RLS migration、RBAC、审计、加密、Outbox dead-letter、共享断路器、Redis bus、心跳、公平性 API | 外部服务均标 `environment_pending` |
| 15 题库级 TTS 配置与 Celery 调度 | ✅ verified（仓库与本机） | KnowledgeBaseSpeechProfile、组织默认 route 初始化、voice catalog、整库 SpeechBuild、可理解的试听可用性、revision 防旧写、模型引用保护与 Celery+DurableWorkItem 已实现 | 真实外部 TTS 凭据和目标生产 Redis/PostgreSQL 仍属环境验收 |
| 17 智能生题任务工作台 | ✅ verified（仓库与本机） | 独立 React 路由、批次历史/Worker 投影、持久停止、revision 防迟到、失败/分片重试、输出截断自适应单槽位恢复和审核导入已实现 | 供应商已接受的在途请求不能保证撤销；真实截断恢复、生产并发/成本仍需目标账户验收 |

### 已完成：题库级 TTS 与层级页面

- `#questions` 只展示当前组织的题库卡片：岗位、题目数、当前 TTS 模型/声音、语音 ready/failed 计数和总体状态。
- `#questions/{knowledge_base_id}` 才加载该题库题目、完整增删改查/试听入口、KnowledgeBaseSpeechProfile 和 SpeechBuild 历史，删除题库首页的全题目平铺；删除采用保留历史快照/资产的归档语义。
- 题库配置显式选择一个已 ready 的 TTS ModelConfiguration 和该模型声音；已添加但未测试/失败/停用的 TTS 也会显示原因，并可在配置弹窗直接测试。切换模型或声音创建新 revision，并让全部活动题进入异步重建。
- 创建题目、单题语音重建和题库配置切换均返回 `202 + job_id`，不在 HTTP 请求内执行 TTS；所有 Celery task/执行入口位于 `app/workers/`。
- DurableWorkItem/Outbox 继续作为数据库真相，Celery broker 只调度；`KB-SPEECH-001` 已有代码、Celery eager、Provider fake、React 行为和全量回归证据。
- 题库配置下拉框可选择并查看尚未就绪的 TTS 及声音，但保存按钮继续以真实探针 ready 为门槛；模型探针对 429/超时等 retryable 错误执行有界退避并显示可操作中文错误。2026-08-27 对当前智谱连接的真实厂商校验和 GLM-TTS 探针均返回 HTTP 429，三次退避后仍失败，因此外部账户目前不可用，未伪造 profile/语音资产。
- 新题库优先继承组织已启用且 ready 的 `question_speech_generation` route，冻结实际模型与默认音色；没有真实默认值时才保持待配置（本地离线 mock 明确显示“不可试听”）。题目接口以 `speech_preview` 区分可试听、未配置、生成中、生成失败、开发 mock 和私有文件缺失，页面禁用无效试听并直接告诉用户下一步操作。
- 智能生题已实现 `QuestionGenerationBatch -> QuestionBlueprint -> 1–2 题 Celery 子任务 -> 去重/补槽 merge -> GeneratedQuestionDraft -> 人工确认 -> Question` 闭环。10 道题默认拆为 5 个独立生成工作，避免单次大响应超时；规划互斥键与合并层完全匹配/高阈值相似检查共同抑制重复，缺题最多补生成两轮。独立工作台展示规划、分片、合并和补生成进度；候选行可打开完整题目详情，支持逐题编辑、删除、单题异步导入或批量导入剩余题目。AI 草稿不会进入计划或面试；正式题目保留生成批次/草稿来源。Mock/Memory/SQLite 合同和 React 行为均不依赖外网。2026-08-27 的 DeepSeek 单题真实闭环仍有效；新的多任务结构仍需在目标模型账户做真实 10 题并发、费用与限流验收。

### 已完成：智能生题任务工作台

- `#questions/{knowledge_base_id}/generation[/{batch_id}]` 是独立路由级页面；题库详情只保留入口，不再加载或内联展示整批生成状态。
- 工作台按题库列出历史批次；运行态只展示紧凑进度和可用命令，审核态把候选题直接提升到批次标题下方。正常 planning/generate chunk/merge/import 内部明细不占据页面，只有失败项才展示错误与对应恢复动作；候选题审核与导入仍复用原草稿合同。
- `stop/resume/retry-failed/retry-chunk` 由 QuestionGenerationService 深模块拥有，前端不调用通用管理员 Outbox replay。停止递增 execution revision、取消未领取工作；在途 Worker 在 Provider 调用前和结果提交前做 guard，停止后的迟到结果只能 supersede。
- 停止后继续只重建未完成槽位，人工重试保留成功分片和旧失败事实。Outbox 记录协作式取消、开始/完成时间、错误码和 retryability；Celery process/result 不成为业务真相。
- `finish_reason=length` 现在映射为 `provider_output_truncated`，失败调用仍记录脱敏 token/推理用量；生题 route 每个 DurableWorkItem attempt 只调用一次 Provider。双槽位截断自动 supersede 原 chunk 并原子创建两个单槽位替代工作，活动进度不会重复计数；单槽位仍截断则首轮终止并保留人工重试入口。生成 Prompt/Schema 已升级为 v2，并以 4000/8000 token 预算及长度/数量边界约束输出。
- 自动化覆盖排队停止/恢复、过期在途结果丢弃、单失败分片重试不重做成功项，以及 React 停止/分片重试不访问 `/admin/work-items/*`。
- 候选题列表的非操作区域支持鼠标和键盘打开完整题干、标准答案、技能与评分关键点；单题导入是独立 DurableWorkItem，成功后仅冻结该草稿，其他草稿继续审核，批量导入只处理剩余项。
- 单题导入使用草稿级 version 条件而不是要求页面批次 version 必须完全最新；连续点击不同候选题可以立即排队，第一题提交/完成推进批次 version 不再让第二题误报冲突，同一道草稿被编辑后的过期确认仍会失败关闭。
- “新建生题任务”是固定页面操作，点击后用 Modal 承载模型、数量、标签、定位和可选要求；关闭弹窗不改变页面主体，任务配置不再以内联展开/收起卡片挤占历史和审核区域。

## 本轮实现清单

### PDF 与私有文件

- `app/file_storage/` 提供 `PrivateFileStorage`、本地私有 adapter、阿里云 OSS adapter 和最长 15 分钟签名访问。
- `ResumeIngestionService` 统一 `local_upload/url_import -> quarantine -> validate -> scan -> private store -> parse -> ready`。
- URL 下载在初始地址和每个重定向逐跳做 scheme、凭据、DNS 和非全局 IP 校验，禁用环境代理，并限制重定向、连接/读取时间与字节数。
- 非 PDF、超限、加密 PDF、空文本、EICAR、环回/私网 URL 均失败关闭；幂等重试复用同一简历/工作项。
- PDF 原件和解析文本分别写入私有 FileObject；公开 API 不返回正文、对象键或本地路径；旧 JSON `resume_text` 上传已删除。
- Web 上传弹窗提供本地 PDF/公开 URL 双入口、任务状态轮询和 ready 后审阅。

### API、异步任务与运维

- 补齐 CandidateProfile/Appointment/KnowledgeBase/Question PATCH、题目归档、题库 import/rebuild/build、题目/经历题语音重建、简历列表/详情和报告导出。
- Outbox 支持最大尝试、指数退避、dead-letter、状态指标、租约恢复和带原因/主体/审计的人工重放。
- 管理员可查询工作项、审计事件、公平性分布并触发心跳超时扫描。
- 留存任务默认 dry-run；管理员显式执行后删除到期候选人的私有简历/录音并清空联系方式、审阅、转写、评分/报告敏感内容，操作全程审计。
- 初筛自动留存由 Celery Beat 周期任务执行：只扫描 `screening_unqualified` 且超过 7 天期限的候选人，复用同一可审计清理 seam；符合、待复核或处理中的候选人不设置该期限。

### 模型、语音与 readiness

- 题库目录卡片不再暴露内部 `model_configuration_id`；语音信息以独立“读题语音”区展示声音标识和“已配置/待配置”状态，模型配置细节继续留在题库详情与模型服务中。
- `llm.chat_text` 已加入统一 schema；OpenAI-compatible 支持 Chat/Embedding/Speech TTS，DeepSeek/智谱 Chat 复用共享 runtime 并适配 JSON Object，智谱另实现官方 GLM-TTS，DashScope 支持 Qwen Chat/Embedding 以及 Qwen3-TTS/CosyVoice，均有离线 HTTP 合同测试。
- 模型管理已拆分为 `ProviderConnection → ModelConfiguration → ModelRoute`：Provider manifest 声明连接/凭证及 `llm/embedding/tts/stt/avatar` 模型表单，管理 UI 通用渲染；模型测试更新健康事实，route target 只引用 ready 的模型配置。未知字段返回 422、同组织重复 capability/purpose 返回冲突、predefined 目录外模型返回冲突；生产缺少精确 route 时返回 `provider_route_missing`。
- OpenAI-compatible 共享 transport 默认不继承环境代理，只有显式 `use_environment_proxy=true` 才读取代理变量；缺少 SOCKS transport 等初始化错误映射为 `provider_transport_unavailable`，不再泄漏原始 500。测试 helper 强制新建内存 store；当前全量 `220` 项通过测试不会触碰开发 SQLite。
- `ModelGateway.open_stream()` 只在音频接受前允许 fallback；`ValidatedSTTStream` 校验 chunk/总量、事件序号和唯一 authoritative final。`open_speech_dialogue()` 使用同一 route/断路/调用日志策略，但只承载受控追问表达。
- `stt-stream` WebSocket 保存音频，stream final 立即形成 CandidateAnswer 并排队完整评分；final 缺失/断流时使用 `stt.batch` 修复。S2S 输出 delta 不形成答案，也不绕过评分 Prompt/Schema。
- 非 mock TTS 结果必须从 data URI/受控 HTTP(S) 复制到 PrivateFileStorage 并形成 FileObject，才可标 `production_ready`。
- 生产邀请/start 要求 streaming STT、batch STT、评分和题目语音 route 为非 mock、已实现且健康事实未过期。

### 数据、安全与多实例

- 联系方式采用 Fernet 密文和租户 HMAC 精确查找，API 只返回掩码；Provider credentials 在 repository seam 密封。
- 生产 Bearer token 映射为 `Principal`，按 admin/interviewer/reviewer 做 RBAC；候选人 token 由生产密钥签名且不明文持久化/返回后台详情，public 窄接口的安全投影不包含标准答案、rubric、候选池或未来题干。
- 所有 `/api/v1` HTTP 结果写元数据审计，未认证失败也记录；URL bearer token 在持久化前统一替换为 `{token}`。
- invitation、候选人会话与签名文件端点按客户端/操作组限流；生产强制 Redis，缺失或故障时通用失败关闭，429/503 同样审计。
- 回答录音和简历使用短期签名访问；音频授权与实际下载分别审计，不再挂载公开 `/media`。
- PostgreSQL migration 提供 JSONB documents、乐观并发、Outbox/凭证/调用日志、关键唯一约束和强制租户 RLS。
- Redis event bus 支持跨实例广播并忽略本实例回环；无 Redis 时本地 bus 保持相同 interface。
- 模型断路器使用 Persistence 中的租户级 `ModelCircuitState`，应用实例共享失败/恢复状态。

### 公平性与人工决策

- 公平性服务按岗位比较会话题量、平均难度和技能覆盖，超过阈值产生人工复核告警并记录审计。
- 脱敏金标校准只接受 current evaluation ID、人工分和不透明 cohort，输出 MAE/RMSE/偏差、分层差异与不自动生效的线性拟合；少于 30 条/每组少于 10 条会告警。
- 报告始终 `human_decision_required=true`，不写录用/淘汰；当前没有用户提供的真实金标，STT WER、评分一致性和漂移仍未完成实际校准。

## 外部环境待验收

| 项目 | 状态 | 为什么不能在仓库内宣称完成 | 已提供的验收入口 |
| --- | --- | --- | --- |
| PostgreSQL 集群/RLS/查询计划 | `local_integration_verified / target_pending` | 本机 PostgreSQL 16 已通过；目标集群 DSN、角色和负载不同 | 显式 owner migration、最小权限 runtime、真实事务/CAS/约束/RLS/索引集成测试 |
| Redis 多实例广播 | `local_integration_verified / target_pending` | 本机 Redis 7 已通过；目标集群拓扑/故障策略不同 | 跨实例 Pub/Sub、正常关闭和生产限流真实测试 |
| 阿里云 OSS | `environment_pending` | 官方 `oss2` SDK 与 adapter 已可加载，但没有 bucket、RAM 凭据和区域 | SDK 依赖、OSS adapter、SSE/签名 fake-bucket contract、`/readyz` 只读 bucket 鉴权探针 |
| 恶意文件扫描器 | `local_integration_verified / target_pending` | 官方 ClamAV Debian arm64 daemon 已用隔离 EICAR 签名库通过 PING、干净样本与 FOUND；目标环境完整病毒库、freshclam 更新和告警仍未提供 | command/clamd 双 adapter、生产连接 readiness、env-gated 真实 EICAR 测试 |
| OpenAI/OpenAI-compatible/DeepSeek/智谱/DashScope 模型服务 | `partial_real_verified / target_pending` | 当前 DeepSeek 凭据已完成一次真实智能生题；OpenAI Realtime 与 DashScope Qwen Realtime 只有离线协议合同，智谱 TTS 仍被账户 429 拒绝，其他目标模型的区域、授权、费用/延迟和长期稳定性数据仍不完整 | 离线 HTTP/WebSocket 合同、声明式模型目录、动态 route UI、真实 DeepSeek 候选题审核批次、私有 TTS copy/hash 与 readiness |
| OpenAI / DashScope 实时语音追问 | `repository_verified / environment_pending` | 统一 `speech.dialogue_realtime`、受控逐字追问、PCM delta、打断/降级和 S2S 早于评分 ack 的端到端合同已实现；没有 API Key/Workspace、模型权限和真实网络指标 | 配置 `candidate_followup_dialogue` route，实测首音、抖动、barge-in、音质、成本和长期连接；S2S 失败必须继续 cascade |
| DashScope 真实 STT | `repository_verified / environment_pending` | Qwen-Audio 3.0 duplex streaming、Qwen3-ASR batch、16k PCM 浏览器链路与离线合同已实现；没有 Workspace/API Key/录音金标 | 配置连接与两条 purpose route，真实测 WER、partial/final 延迟、断流修复、费用和健康 TTL |
| 火山引擎豆包实时语音 | `not_implemented / optional` | 已确认官方产品能力，但尚未实现其二进制 StartConnection/StartSession 协议，也未证明能严格表达服务端批准的追问 | 保持 manifest 不可路由；后续按官方完整协议新增 adapter、合同测试和真实网络验收 |
| 腾讯云 WebRTC 数智人 | `repository_verified / environment_pending` | HTTPS create/stat/start/close、签名 WSS SEND_TEXT、TCPlayerLite 拉流和会话回收已实现；没有 AppKey/AccessToken/形象资产/并发 | 配置连接、形象与 avatar route，在目标浏览器验证建流/口型/延迟/离场回收和并发计费 |
| 评分/公平性金标 | `repository_verified / data_pending` | 脱敏 current-evaluation 校准 API、分层指标、样本量告警与审计已实现 | 企业提供经授权的真实脱敏 evaluation ID + 人工分 + opaque cohort，至少 30 条且每 cohort 至少 10 条 |
| 邮件提醒/短信邀请 | `environment_pending` / `external_choice_required` | SMTP adapter 与 30 分钟提醒已实现，但授权码、发件域名和目标服务未配置；短信通道未选择 | 一次性邀请 token/API 与手工安全分发保持可用；配置 `INTERVIEWER_SMTP_*` 后由 Celery 投递提醒 |
| 其它 WebRTC/SFU 或数字人厂商 | `optional` | 当前选定腾讯云托管 SFU；自建 LiveKit/Janus/WHEP 尚未选择 | 只需新增 provider/player adapter，不改变面试编排 |

## 已关闭的兼容边界

- 管理员直接创建/启动会话、客户端 REST/WebSocket 文本答案、计划运行时 `items`、旧全局题目创建/列表和 `QuestionService`/向量 repository/worker 分支均已物理删除，不再用 production 条件分支隐藏。
- 旧计划只能在应用升级前通过 `python -m app.migrations.plan_execution_v2 --dry-run` 检查，再执行无 `--dry-run` 的显式一次性迁移；运行时不会懒迁移。
- 旧模型配置只能在升级前通过 `python -m app.migrations.model_configuration_v2 data/interviewer.sqlite3 --dry-run` 检查，再执行同命令去掉 `--dry-run`；运行时不读取旧 collection 或 route target。
- 当前仓库 SQLite 数据检查为 0 份 InterviewPlan，未产生数据改写。接口删除和迁移行为由 `tests/test_production_compatibility.py`、`tests/test_plan_assembly.py` 覆盖，`COMPAT-001` 已标 `closed`。
