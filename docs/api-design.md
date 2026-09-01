# 接口设计

本文定义目标业务流程的第一版 API 语义。实际实现可生成 OpenAPI，但不能改变这里的资源边界、候选人隐私和事件语义，除非同步更新本文档。

## 通用约定

- Base path：`/api/v1`
- 后台 API 使用 Bearer token；公开邀请端先使用一次性 `invitation_token`，登记成功后换取短期 `candidate_session_token`。
- 所有后台资源隐含 `organization_id`，服务端从登录主体获取，不能信任客户端传入值。
- 时间使用 ISO 8601 UTC；预约额外保存展示时区。
- ID 使用不可猜测的 UUID/ULID。
- 导入、异步审阅、邀请、登记、候选人开始、答案提交和重试支持 `Idempotency-Key`。
- 所有修改聚合的请求携带 `expected_version`；陈旧写入返回 `409 PERSISTENCE_CONFLICT`。
- 交互式客户端对可能被后台模型任务推进 version 的资源使用统一 latest-version command：提交前 GET 最新资源，仅当配置 revision 或命令相关业务字段未改变时采用最新 version；读写间再次冲突最多重读一次。真实语义变化返回本地 `RESOURCE_SEMANTIC_CONFLICT` 并要求重新确认，不能按供应商或模型类型写特例。
- 明确建模为异步工作的接口（题库 import/rebuild/build、PDF 摄取等）返回 `202 Accepted` 和 `job_id`，通过工作项接口查询状态。
- 创建题目、题目语音重建和题库语音配置切换不得在 HTTP 请求内调用 TTS；它们只提交 DurableWorkItem 并由 `app/workers/` 中的 Celery task 执行。
- `GET /healthz` 只表示进程存活；`GET /readyz` 对数据库/Redis 和生产密钥、OSS bucket 鉴权、扫描器执行只读探针，未就绪返回 `503` 与不含密钥值的逐项结果。业务模型 route readiness 仍由预约准入按组织、purpose 和健康 TTL 判断。
- 成功资源响应保持资源本身为顶层对象，避免为已有客户端引入破坏性的二次 `data` 包络；集合响应统一为 `{"items": [...], "next_cursor": null|string}`，即使当前实现尚未分页也保留 cursor 槽位；异步命令统一返回 `202`。二进制文件、音频和 CSV 导出不套 JSON 格式。
- JSON 输出统一经过 `app/transport/http/responses.py`；需要防止内部字段泄露的投影在 `app/transport/http/fields/` 声明 allow-list 并由 `marshal` 执行。字段声明只负责 transport 投影，不承载领域计算；服务层不能依赖 transport fields。`app/api/routes.py` 只总装 `app/api/routers/` 下按业务域拆分的 router，不放 response、module 构造或实时连接 implementation。

通用错误：

```json
{
  "error": {
    "code": "APPOINTMENT_NOT_READY",
    "message": "The appointment is not ready.",
    "details": {"failed_checks": ["stt_route"]}
  }
}
```

公开邀请、填报和匹配接口对“token 不存在、已过期、候选人不匹配”使用相同 HTTP 状态和通用消息，防止枚举候选人或预约。

请求体、路径和查询参数校验失败同样使用上述错误包络，固定为 `422 REQUEST_VALIDATION_FAILED`；`details.fields` 只返回 `location/message/type`，不回显请求原值。Provider、Persistence、认证与限流错误也跨同一 response seam，客户端不再兼容 FastAPI 默认 `detail` 形状。

## Web 工作台与候选人页面

- `GET /`：内置企业工作台。
- `GET /web/*`：静态资源。
- `GET /api/v1/auth/session`：使用现有后台 Bearer token 返回当前主体、组织和角色；不创建新的登录凭据。
- `POST /api/v1/auth/websocket-ticket`：使用现有后台 Bearer token 为指定面试签发最长 60 秒、一次用途的浏览器 WebSocket ticket。原有 WebSocket `Authorization` header 认证继续兼容，浏览器工作台使用 ticket query，避免把长期 Bearer token 放入 URL。
- `/#positions`、`/#knowledge-bases`、`/#candidates`、`/#plans`、`/#appointments`、`/#interviews`：后台工作区。
- `/#invite/{invitation_token}`：公开邀请、姓名/邮箱/手机号填报、授权和设备检查。
- start 响应首次导航可在 URL fragment 中携带 `candidate_session_token`；前端立即转存到 `sessionStorage` 并用 `history.replaceState` 清除 fragment 中的 token，随后进入 `/#candidate/{interview_id}`。
- 候选人 HTTP 请求通过 `X-Candidate-Session-Token` header 调用 public 窄接口；WebSocket 握手使用同一短期 token。后台 Bearer API 不接受候选人 token。
- 后台工作台只按当前角色和当前路由加载允许访问的资源；`interviewer/reviewer` 不得因为无权读取 `/admin/*` 而导致整个工作台加载失败。

当前代码已实现本文主流程中的岗位/题库批量 import/rebuild/build 查询、题目更新/归档/语音重建、候选人 PATCH、PDF multipart 与 HTTPS URL 摄取、简历版本/短期访问、经历题审核与语音重建、候选人专属计划、预约 PATCH/邀请、公开填报、readiness、self-start、服务端 streaming/batch STT、企业复核、签名音频、重评和 JSON/CSV 报告导出接口。旧 JSON `resume_text` 上传已删除，非 multipart 请求返回 `415 RESUME_MULTIPART_REQUIRED`。持久 Outbox 提供状态/指标、指数退避、dead-letter 与人工重放；题库导入、重建和 PDF 摄取默认返回 `202`。

后台 HTTP 在生产使用 Bearer RBAC，`admin/interviewer/reviewer` 权限按路由分离；成功、失败和未认证请求均写元数据审计，URL 中 invitation/file/media token 会先替换为 `{token}`。签名文件和媒体只有分钟级有效期，实际媒体下载另记审计。外部环境验收边界见 [开发进度](development-progress.md)：真实云服务未提供时不能把对应 adapter 标为生产健康。

公开 invitation/intake/readiness/start、候选人会话和签名文件访问按客户端与操作组限流；开发使用进程内时间窗，生产必须配置 `INTERVIEWER_REDIS_URL`，Redis 不可用时返回通用 `503 RATE_LIMITER_UNAVAILABLE`，超过阈值返回 `429 RATE_LIMIT_EXCEEDED`。两者都审计脱敏 route，不暴露 token 是否存在。

深度审查登记的六个接口/领域语义差距已在仓库范围关闭：公开题库搜索统一为有 scope 的 Question Catalog；计划运行时只接受 execution v2 canonical slots；Candidate Intake 验证明示同意和服务端告知版本；公开 start 校验时间窗及持久设备 readiness；报告所有字段只读取当前评分 revision；候选人只读取 token 保护的 allow-list 投影。迁移和验证证据统一记录在 [已知问题与修复设计](known-issues-and-remediation.md)。

### 已知问题修复合同（已验证）

| ID | 接口保持/调整 | 完成条件 |
| --- | --- | --- |
| `PLAN-001` | `PATCH /interview-plans/{plan_id}` 只编辑 canonical 槽位、候选池、经历问题和策略；schema 拒绝 `items` | 预约 start 执行批准的 execution v2 revision，审批重新计算完整 readiness |
| `CONSENT-001` | intake 使用 `consent.accepted/version/recording_accepted`；服务端校验允许版本并记录告知 hash 和服务端时间 | 缺失隐私同意返回 `CONSENT_REQUIRED`；要求录音但未同意返回 `RECORDING_CONSENT_REQUIRED`，且无持久副作用 |
| `APPOINTMENT-001` | readiness 形成带有效期的设备事实；start 通过统一 admission command 校验 token、登记、同意、时间窗和全部 readiness | 窗口前返回 `APPOINTMENT_TOO_EARLY`，窗口后返回 `APPOINTMENT_WINDOW_CLOSED`；并发/重复 start 只返回一个会话 |
| `REPORT-001` | 报告响应结构不变，`evaluation_ids` 明确等于生成时各答案的当前评分指针集合 | 分数、证据、风险和 `manual_review` 只使用这些 current revisions |
| `SEARCH-001` | `/questions/search` 固定为有岗位/题库 scope 的关键词和结构化查询；相似题只能使用显式治理 purpose | 公开搜索与计划候选池跨同一个 Question Catalog seam，未配置 embedding 时仍可用 |
| `CANDIDATE-ACCESS-001` | `/public/interviews/{id}` 系列只接受候选人 token header 并返回 allow-list 投影 | 不返回标准答案、rubric、候选池、未来题干或 token；媒体必须属于当前会话/轮次 |

## 岗位与岗位题库 API

### 岗位

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions` | 创建岗位；工作台同时提交 `initial_requirement`，服务端在同一事务创建首版岗位要求 |
| `GET` | `/api/v1/job-positions` | 列出岗位 |
| `GET` | `/api/v1/job-positions/{position_id}` | 获取岗位及题库摘要 |
| `PATCH` | `/api/v1/job-positions/{position_id}` | 修改或归档岗位 |
| `GET` | `/api/v1/job-positions/{position_id}/deletion-impact` | 预览删除将影响的候选人、岗位要求、计划和预约数量 |
| `DELETE` | `/api/v1/job-positions/{position_id}` | 携带 `expected_version` 和与岗位名称完全一致的 `confirmation` 删除岗位 |

岗位删除必须先向操作者展示 deletion-impact 并进行名称精确确认。命令清除该岗位候选人的联系方式、简历、录音、转写、评分和报告敏感内容，归档相关岗位要求/计划、取消预约，并保留不可识别的历史面试和审计占位。组织共享的 KnowledgeBase、Question、语音配置和语音资产不随岗位删除。

新建岗位请求的 `initial_requirement` 包含 `title/description/must_have_skills/nice_to_have_skills/seniority/interview_duration_minutes`。工作台必须提交该对象；服务端先完成全部字段校验和技能规范化，再在一个租户事务内写入 JobPosition 与首版 RoleRequirement。成功响应保持岗位字段，并增加 `initial_role_requirement`；任一步失败都不得留下没有首版要求的岗位。低层 API 客户端仍可省略该字段以兼容已有集成，但这条兼容路径不用于新工作台。

创建示例：

```json
{
  "code": "backend-senior",
  "name": "资深后端工程师",
  "department": "研发中心",
  "description": "负责高并发服务和线上稳定性",
  "default_duration_minutes": 45
}
```

### 岗位题库

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions/{position_id}/knowledge-bases` | 为岗位创建题库 |
| `POST` | `/api/v1/job-positions/{position_id}/knowledge-base-assignments` | 把组织内已有题库关联到岗位；请求携带 `knowledge_base_id + expected_position_version`，不复制题目或语音配置 |
| `GET` | `/api/v1/job-positions/{position_id}/knowledge-bases` | 列出岗位题库 |
| `GET` | `/api/v1/knowledge-bases` | 列出当前组织全部题库摘要；支持岗位、状态过滤，返回题目/语音计数和当前 TTS/声音摘要 |
| `GET` | `/api/v1/workspace/question-catalog` | 后台一次读取岗位、题库与题目集合，供路由级工作台加载；不替代既有资源接口 |
| `GET` | `/api/v1/workspace/question-overview` | 总览页只读取题目计数与最近 5 项，避免首屏下载完整题库 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 查看题库、构建状态和计数 |
| `PATCH` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 修改名称、说明或归档；语音配置使用独立命令接口 |
| `PUT` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-profile` | 以 `expected_version + expected_speech_profile_revision` 设置 TTS 模型、声音和输出参数；变化时创建新 revision 并返回整库重建 job。profile revision 未变时，服务端可吸收后台进度造成的任意 KnowledgeBase version 推进；profile 真正被并发修改时仍返回 409。新 revision 与旧 revision 未完成工作的协作取消在同一事务提交 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-options` | `items` 返回可选择的已就绪 TTS 模型；`candidates` 同时返回已添加但未测试/失败/停用的 TTS 及不可选原因和归一化声音目录，不包含 Provider 凭据 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds` | 列出语音构建历史、当前进度和失败计数 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds/{job_id}` | 查询一次整库语音构建和题目级失败摘要 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds/{job_id}/retry-failed` | 以 `expected_version + Idempotency-Key` 只重试当前 profile revision 和该 build 冻结清单下当前仍为 `speech_status=failed` 的题目；按题目当前 version 创建新工作，不复用旧 dead-letter，也可恢复旧版 worker 误写为 completed/superseded 的失败子工作 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/imports` | 上传或结构化导入题目 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/rebuild` | 重建索引和缺失读题语音 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/builds/{job_id}` | 查询构建工作项 |

SpeechBuild 的 `failed` 计数和 `failed_items` 只包含已经进入 `dead_letter` 的题目子工作；仍可由 Worker 自动领取的 DurableWorkItem `status=failed` 对 API 投影为 `pending/running`。因此页面只对真正终态失败显示人工重试，自动重试期间保留已完成题目的试听能力。

导入使用 multipart 文件或 JSON items。响应立即返回：

```json
{
  "job_id": "job_kb_01J...",
  "knowledge_base_id": "kb_backend_cn",
  "status": "queued",
  "tasks": ["parse", "validate_candidate_pool", "question_speech"]
}
```

新建题库可以不指定语音配置。若组织存在 enabled 的 `tts.synthesize + question_speech_generation` route，且 primary ModelConfiguration/连接均 enabled、模型为 `ready`，服务端解析模型 `default_voice` 并保存 revision 1 的明确 speech profile（`source=model_route_default + model_route_id`）；新题随后直接按该冻结配置异步生成。没有有效默认路由时返回 `speech_build_status=configuration_required`，前端引导进入题库详情选择 TTS 模型和声音。route 后续变化不自动改写已创建题库，避免静默重建和费用。

招聘流程不在岗位卡片内创建或重新配置题库，而是调用 assignment 接口从题库列表选择。关联后的岗位直接复用题库现有 Question、KnowledgeBaseSpeechProfile、声音和不可变语音资产；重复关联为幂等成功，未关联题库不能进入该岗位的搜索或计划。

构建工作项依次校验题目、标准答案、关键点、rubric、技能、难度和题型，写入结构化候选池，并按题库的 KnowledgeBaseSpeechProfile 异步生成每道活动题的 `QuestionSpeechAsset`。部分失败时题库保持 `building` 或进入 `failed`，响应必须列出失败题目和重试入口；不能把缺少评分依据或匹配当前 profile revision 语音的题库标为 `ready`。MVP 不生成 embedding，也不依赖向量数据库。

设置语音配置请求：

```json
{
  "expected_version": 4,
  "model_configuration_id": "model_cfg_tts_01J...",
  "voice_profile_id": "tongtong",
  "language": "zh-CN",
  "audio_format": "audio/wav",
  "speaking_rate": 1.0
}
```

服务端只接受已启用、状态为 `ready` 且支持 `tts.synthesize` 的 ModelConfiguration，并校验声音属于该模型的 voice catalog。模型、声音、语言、格式或语速与当前 profile 不同则原子增加 `speech_profile.revision`、把题库语音 readiness 置为 rebuilding，并创建 `knowledge_base.speech.rebuild` DurableWorkItem；完全相同的配置和 `Idempotency-Key` 返回已有 job，不重复计费。响应为 `202`：

```json
{
  "knowledge_base_id": "kb_backend_cn",
  "speech_profile_revision": 3,
  "job_id": "work_kb_speech_01J...",
  "status": "pending",
  "question_count": 42
}
```

父工作项在 Celery worker 中冻结活动 Question ID/version 清单并分批创建题目级工作项，进度投影至少包含 `total/pending/running/ready/failed/superseded`。切换到 revision 4 后，revision 3 的迟到结果不得成为当前资产。已批准计划或历史会话引用的旧 QuestionSpeechAsset 不删除、不覆盖。

KnowledgeBase 的 version 也会因后台构建状态推进而变化。配置页面不能长期复用打开弹窗时的 version：提交前先读取最新 KnowledgeBase；若最新 speech profile 的 revision、模型配置 ID/version、声音、语言、格式和语速与打开时一致，可用最新 version 提交；若这些字段已经变化，必须刷新并要求用户重新确认。提交与读取之间的极窄竞态最多按同一规则重读并重试一次，不能无条件覆盖他人的语音配置。

### 题目

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/questions` | 创建题目并排队校验/语音；返回 `202` 和工作项，不等待 TTS |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/questions` | 列出题目 |
| `PATCH` | `/api/v1/questions/{question_id}` | 修改题目；内容变化产生新版本和新语音任务 |
| `DELETE` | `/api/v1/questions/{question_id}?expected_version={version}` | 从当前题库归档题目；保留历史面试快照和不可变语音资产，不再出现在活动题列表 |
| `POST` | `/api/v1/questions/{question_id}/speech/regenerate` | 以 `expected_version + Idempotency-Key` 按题库当前 speech profile 重建单题语音；重复命令返回同一工作，响应 `202`，不在请求内执行 TTS |
| `POST` | `/api/v1/question-speech-assets/{asset_id}/content-url` | 鉴权并审计后签发五分钟题目语音试听地址；浏览器不接触存储凭据 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-options` | 返回题库定位/标签默认值和可用于结构化生题的 ready LLM 模型，不包含凭据 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches` | 创建智能生题批次并返回 `202 + work_item_id`；请求包含模型、数量、定位、标签和可选要求，HTTP 不执行 LLM |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches` | 列出该题库最近生成批次和状态 |
| `GET` | `/api/v1/question-generation-batches/{batch_id}` | 查看批次上下文、候选草稿、失败或导入结果 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/stop` | 以 `expected_version + Idempotency-Key` 请求停止；取消未执行工作并让在途结果在提交前失效 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/resume` | 以 `expected_version + Idempotency-Key` 继续 stopped 批次的未完成槽位，保留已完成分片 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/retry-failed` | 以 `expected_version + Idempotency-Key` 重试规划、合并或全部失败分片，不重做成功分片 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/chunks/{chunk_id}/retry` | 以 `expected_version + Idempotency-Key` 只重试一个失败分片 |
| `PATCH` | `/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}` | 以批次 `expected_version` 修改一条候选草稿并重新校验评分依据 |
| `DELETE` | `/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}?expected_version={version}` | 从审核批次删除候选草稿，不影响正式题库 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}/import` | 以 `expected_draft_version? + Idempotency-Key` 异步导入单个候选题；兼容接收 `expected_version`，但其他草稿推进批次版本不阻塞本题；批次继续处于审核态 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/import` | 以 `expected_version + Idempotency-Key` 冻结剩余草稿并异步批量导入正式题库 |
| `POST` | `/api/v1/questions/search` | 后台按关键词和结构化字段查题，不用于实时抽题 |

题目读取投影增加 `speech_preview={available,reason,message}`。只有 ready QuestionSpeechAsset、
`production_ready=true` 且关联 ready FileObject 时 `available=true`；开发 mock 返回
`reason=development_mock_asset` 和“配置语音”操作提示，不再把 `mock-tts://` 展示为可试听音频。直接请求旧 mock 资产
返回 `409 QUESTION_SPEECH_PREVIEW_UNAVAILABLE`；真实 Provider 资产缺少私有文件时继续返回
`409 QUESTION_SPEECH_ASSET_NOT_PRIVATE`，但消息明确提示重新生成或检查私有存储。

智能生题创建体为 `model_configuration_id + target_count(1..30) + positioning + tags + requirements?`，
必须携带 `Idempotency-Key`。只有 `enabled + ready + llm.chat_json` 模型可选。PATCH 草稿允许修改题干、答案、
关键点、技能、难度和题型；服务端会重新校验非空答案、关键点和技能。导入接口只接受 `reviewing` 批次。
单题导入是草稿级条件命令：服务端校验目标草稿的 `expected_draft_version`（旧客户端可只传批次
`expected_version`），事务写入使用读取到的最新批次版本，因此另一个草稿刚提交/完成导入不会制造无关的
`PERSISTENCE_CONFLICT`。该草稿随后标记为 `importing -> imported/failed`，成功后不能再次编辑、删除或导入；其他草稿仍可审核，
批量导入只处理尚未导入的草稿，并拒绝与在途单题导入并发，
生成状态依次为 `queued -> generating -> reviewing`，其中 `phase` 进一步公开
`planning/generating/merging/refilling`；响应的 `generation_progress` 提供规划数、子任务完成数、已接受/过滤题数和
补生成轮次。任务控制增加 `generating/queued -> stopping -> stopped -> generating`；停止不能承诺撤销已经到达
供应商的 HTTP 请求，但会停止新调用并通过 `execution_revision` 丢弃停止前的迟到结果。批次详情返回 `tasks`、
`available_actions`、`control_history` 和结构化错误投影，客户端无需、也不能直接调度或 replay DurableWorkItem。
若双槽位生成以 `provider_output_truncated` 失败，服务端会把原 chunk 投影为 `superseded`，在其 `recovery` 中返回
`strategy/reason/replacement_chunk_ids` 与不含正文的 token/finish_reason 诊断，并创建两个单槽位替代任务；
`generation_progress.total_chunks` 只统计活动替代任务。该恢复不改变批次 ID、不重做成功分片，也不要求客户端发起
重试。单槽位仍截断时批次进入 `failed`，错误标记为不可自动重试，但既有显式分片/失败重试命令仍可由面试官执行。
导入状态为 `importing -> imported`；正式题目 ID 返回在
`imported_question_ids`。生成或导入失败时批次保留 `last_error` 和关联工作项，AI 输出永远不会因为读取接口或轮询
而自动入库。既有创建、查询、草稿审核与导入路径保持兼容。

创建题目示例：

```json
{
  "title": "Python GIL",
  "question_text": "请解释 GIL 对 CPU 密集型多线程程序的影响。",
  "standard_answer": "GIL 限制同一进程内线程同时执行 Python 字节码……",
  "key_points": [
    {"text": "解释字节码并行限制", "weight": 0.4},
    {"text": "区分 CPU 与 I/O 密集场景", "weight": 0.3}
  ],
  "skills": ["python", "concurrency"],
  "difficulty": "senior",
  "type": "open_ended",
  "rubric": {"semantic_weight": 0.5, "key_point_weight": 0.5}
}
```

后台搜索请求必须明确岗位和题库范围。`query` 使用关系库全文/关键词搜索；技能、难度和题型使用结构化过滤，不做向量召回：

```json
{
  "job_position_id": "pos_backend",
  "knowledge_base_ids": ["kb_backend_cn"],
  "query": "Python 并发与性能",
  "filters": {"difficulty": ["mid", "senior"]},
  "limit": 20,
  "include_answer": true
}
```

服务端验证所有题库属于当前组织并已显式关联到 `job_position_id`；查询底层在关联校验后强制过滤 `organization_id + knowledge_base_ids + active + valid + speech-ready` 及技能/难度/题型条件。实时 Question Selection 不调用该搜索接口，而是直接使用批准计划冻结的题目 ID/version 候选清单。

## 简历库与 AI 审阅 API

### 候选人记录和简历

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/candidate-profiles` | 企业创建候选人基本信息；新工作台同时提交 `job_position_id` 建立岗位候选关系 |
| `GET` | `/api/v1/candidate-profiles` | 搜索组织内简历库；返回最新岗位初筛投影、人工复核结果和清理期限 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}` | 查看候选人及最新岗位初筛投影 |
| `PATCH` | `/api/v1/candidate-profiles/{candidate_id}` | 更新基本信息或归档 |
| `DELETE` | `/api/v1/candidate-profiles/{candidate_id}?expected_version={version}` | 乐观并发地逻辑归档候选人并从活动列表隐藏，保留审计事实 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/experience-questions` | 读取候选人简历问答；只返回生效结论符合且证据绑定有效的非归档项 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/experience-questions` | 人工创建绑定候选人、符合的 ResumeReview 和 1–3 个简历证据标签的问题；初始为草稿 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 以 multipart 上传本地 PDF，创建新简历版本 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes/import-url` | 从公开 HTTPS URL 异步导入 PDF，创建新简历版本 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 列出简历版本和摄取状态 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}` | 查看解析状态和授权元数据 |
| `PATCH` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}` | 以 `expected_version + display_name` 修改展示文件名；不覆盖 PDF 内容 |
| `DELETE` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}?expected_version={version}` | 删除未被计划/面试历史引用的简历版本，同时取消待处理工作并清理私有文件 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}/content-url` | 鉴权并审计后获取短期下载入口 |
| `GET` | `/api/v1/file-ingestion-jobs/{job_id}` | 查看 PDF 摄取工作项、失败码和简历状态 |

创建示例：

```json
{
  "name": "张三",
  "email": "candidate@example.com",
  "phone": "+8613812345678",
  "external_ref": "ats-2048"
}
```

服务端规范化并加密邮箱和手机号；响应默认只返回脱敏值。简历只接受 PDF，限制类型和大小并做恶意文件扫描，原文件存 Private File Storage 而非数据库 BLOB。调用方不能提供可信 `storage_uri`；服务端只保存 `file_object_id` 和受控元数据。

简历的“改”只作用于展示文件名，且仍须以 `.pdf` 结尾；替换 PDF 必须再次调用创建接口形成递增的 `resume_version`，不能原地改写证据来源。删除使用乐观并发：待领取的 `resume.ingest/resume.review` 工作项转为 `cancelled`，隔离文件、PDF、解析文本和未进入历史的派生资产被清理，资源留下最小 `deleted` 审计占位并从列表隐藏。工作项已经运行时返回 `409 RESUME_DOCUMENT_PROCESSING`；已被 InterviewPlan 或 InterviewSession 快照引用时返回 `409 RESUME_DOCUMENT_IN_USE`，防止破坏历史证据。

候选人列表中的 `screening` 是最新 ResumeReview 的岗位维度投影：`ai_recommendation` 保留模型原建议，`effective_outcome` 优先采用人工复核结论，`matched_requirements/unmet_requirements` 用于解释入选或淘汰。该投影是辅助建议，不是自动录用决定。

本地文件上传使用 `multipart/form-data`：

```text
file=<candidate.pdf>
display_name=张三-后端工程师简历.pdf  # 可选
job_position_id=pos_backend           # 可选；与下一字段同时提交时，摄取成功后自动排队初筛
role_requirement_id=role_01J...       # 可选
```

服务端分块读取并在 10 MiB 默认硬上限内拒绝超限输入，随后计算 SHA-256；隔离文件写入 worker 管理的私有目录。响应 `202 Accepted`：

```json
{
  "resume_document_id": "resume_01J...",
  "ingestion_job_id": "job_resume_ingest_01J...",
  "source_type": "local_upload",
  "ingestion_status": "queued"
}
```

URL 导入请求：

```json
{
  "url": "https://files.example.com/resumes/candidate.pdf",
  "display_name": "候选人简历.pdf",
  "job_position_id": "pos_backend",
  "role_requirement_id": "role_01J..."
}
```

URL 导入也返回 `202`，`source_type=url_import`。下载由 worker 使用受控 HTTP 客户端执行：生产默认只允许 HTTPS，限制连接/总超时、重定向次数和最大字节数；初始 URL 及每次重定向都要重新解析 DNS，并拒绝环回、私网、链路本地、保留地址、云元数据地址、非 HTTP(S) scheme 和 URL 内嵌凭证。MVP 不接收需要 Cookie、Authorization 或企业内网访问的 URL。

两种入口随后执行相同流水线：`receive/download -> quarantine -> PDF signature/MIME validation -> malware scan -> private store -> page-preserving parse -> ready`。同时提交岗位和要求时，摄取完成的同一事务创建 `ResumeReview + resume.review` 工作项，浏览器不轮询等待 LLM。外部 URL 只作为摄取来源；相同上传 `Idempotency-Key` 的重试返回同一个 `ResumeDocument`。不同上传命令即使 PDF 内容相同也形成不同不可变简历版本，其审阅工作幂等键按 `resume_document_id + input_hash` 隔离，不能复用另一个版本已完成或已删除的工作项。

JSON `resume_text` 兼容请求已删除；开发和生产均以系统托管 PDF 为唯一简历文件真相。

### 异步简历审阅与经历问题

`POST /api/v1/candidate-profiles/{candidate_id}/resume-reviews`

```json
{
  "resume_document_id": "resume_01J...",
  "job_position_id": "pos_backend",
  "role_requirement_id": "role_01J...",
  "experience_question_count": 3
}
```

响应 `202`：

```json
{
  "review": {"id": "rr_01J...", "status": "queued"},
  "job": {"id": "job_resume_01J...", "status": "pending", "kind": "resume.review"}
}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/resume-reviews/{review_id}` | 返回岗位初筛、项目/技能证据、告警和问题状态 |
| `POST` | `/api/v1/resume-reviews/{review_id}/retry` | 以 `expected_version + reason + Idempotency-Key` 将 failed/dead-letter 初筛原子恢复为 queued/pending，并记录操作者审计；相同命令在 version 校验前返回原 replay 结果 |
| `PATCH` | `/api/v1/resume-reviews/{review_id}/screening-review` | 人工复核初筛；提交 `expected_version`、`decision=qualified|unqualified` 和可选说明，保留 AI 原建议并审计 |
| `GET` | `/api/v1/resume-reviews/{review_id}/experience-questions` | 仅在生效结论符合时列出证据绑定有效的问题；否则返回空集合 |
| `PATCH` | `/api/v1/experience-questions/{question_id}` | 人工编辑、批准或拒绝 |
| `DELETE` | `/api/v1/experience-questions/{question_id}?expected_version={version}` | 从候选人个人题库归档；保留已冻结计划和历史面试快照 |
| `POST` | `/api/v1/experience-questions/{question_id}/speech/regenerate` | 重试引用该题的预约级 failed/dead-letter 语音工作；未确认预约时不提前生成 |

创建与重试接口只验证并排队，绝不在 HTTP 请求内等待 LLM。同一 ready 简历的重复创建命令返回原 ResumeReview；若该审阅仍为 queued 但工作项缺失，命令会在同一事务补建指向当前审阅的 DurableWorkItem，并校验返回工作的 aggregate ID，避免界面永久显示排队。失败重试仅接受 `ResumeReview.status=failed` 且关联 DurableWorkItem 为 `failed/dead_letter` 的组合；源 ResumeDocument 必须仍为 `ready`，岗位和要求必须存在。命令使用审阅 version 防双击/并发覆盖，清空当前错误与分块进度、把工作 attempt 归零并增加 `replay_count`，但不创建第二份审阅。`GET` 在处理中返回 `status/processing_stage/processing_strategy/processing_progress`；完成后通过 `screening.recommendation/score/summary/matched_requirements/unmet_requirements` 给出可解释建议，证据包含 `source_pages`。服务端按 `candidate_screening_score.v1` 强制把 0–59 映射为 `unqualified`、60–74 映射为 `manual_review`、75–100 映射为 `qualified`，并返回 `screening_policy_version`；候选人列表的嵌套 screening 投影还返回 `question_generation_status/error/count`。模型建议与分数冲突时以分数带为准，人工 `screening-review` 决定仍可覆盖生效结论。只有生效结论为 `qualified` 时才排入独立的 `resume.experience_questions.generate` 工作；AI 不符合/待复核不生成，人工改判符合时才临时排队。AI 问题默认为 `draft`，批准后只变为可入计划的 `deferred`，不触发 TTS。不得把简历中的受保护属性或无关个人信息发送给模型。

候选人个人题库不新建第二套题目实体，而是按 `candidate_profile_id` 汇总 ExperienceQuestion。AI 生成项记录
`source_type=ai_generated` 和来源 ResumeReview；人工创建项记录 `source_type=manual`，并必须选择属于同一候选人且已完成的
ResumeReview，使岗位、简历版本和证据上下文可追溯。人工创建请求至少包含非空 `question_text`、`standard_answer`、
`key_points` 和 1–3 个 `evidence_refs` 标签；标签必须来自该审阅的项目/技能证据，且题干必须明确包含至少一个所选标签。服务端把标签解析为含证据文本和来源页的不可变快照。初始状态固定为 `draft`。编辑仍使用 `expected_version`，状态只允许
`draft/approved/rejected`；批准后写入 `speech_status=deferred` 并可进入新计划，但不创建 TTS 工作。DELETE 使用归档语义，已归档项不再出现在个人题库、
审阅问题列表或新计划中，但已批准计划和历史 InterviewQuestionSnapshot 保持不变。生效结论改为不符合后，读取、创建、编辑、语音生成和新计划组卷全部失败关闭；历史上没有有效证据快照或题干未点名证据的题也从活动读取与新计划中过滤。

同一候选人按岗位只取最新审阅决定留存：只要存在 `qualified` 或 `manual_review`/处理中结论，就不设置初筛清理期限；所有最新岗位结论均为 `unqualified` 时设置 `retention_reason=screening_unqualified` 和服务端时间加 7 天。Celery Beat 周期任务先按当前分数带校正存量候选人的期限，首次命中从该次运行起重新给足 7 天，再由 RetentionService 清除到期私有简历和敏感投影并写审计；列表读取或页面点击不产生隐式写入或物理删除。

## 岗位要求与面试计划 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions/{position_id}/role-requirements` | 创建岗位要求版本 |
| `GET` | `/api/v1/job-positions/{position_id}/role-requirements` | 列出岗位要求 |
| `POST` | `/api/v1/interview-plans/generate` | 生成候选人专属计划；默认草稿，`approve=true` 时原子校验并批准 |
| `GET` | `/api/v1/interview-plans` | 列出计划 |

React 岗位卡片根据该岗位是否已有要求，显示“添加岗位要求”或“新增要求版本”，因此升级前已有但尚无要求的岗位无需删除重建。新建岗位路径仍优先使用原子 `initial_requirement` 合同。
| `GET` | `/api/v1/interview-plans/{plan_id}` | 查看槽位、经历问题和就绪状态 |
| `PATCH` | `/api/v1/interview-plans/{plan_id}` | 编辑草稿或批准/归档 |

生成计划：

```json
{
  "job_position_id": "pos_backend",
  "knowledge_base_ids": ["kb_backend_cn"],
  "role_requirement_id": "role_01J...",
  "candidate_profile_id": "cand_01J...",
  "resume_review_id": "rr_01J...",
  "approve": true,
  "position_question_count": 6,
  "experience_question_ids": ["eq_01J...", "eq_01K..."],
  "strategy": {
    "coverage": ["python", "database", "system_design"],
    "difficulty_curve": true,
    "max_same_skill_questions": 2,
    "deduplication_threshold": 0.72,
    "randomization": "seeded_per_session"
  }
}
```

`approve` 默认为 `false`，保留 API 客户端“生成草稿—编辑—批准”的完整流程。React 工作台在面试官点击“生成并启用计划”时显式提交 `approve=true`；Plan Assembly 必须在同一事务里完成装配、readiness 校验和批准，成功响应直接为 `approved`。校验失败时不得留下需要创建人再处理的半成品草稿。

响应中的 `bank_slots` 只定义维度、难度、题型、权重和时长；计划装配通过关系库字段形成每个槽位的 `QuestionCandidatePool`，批准时冻结筛选条件、题目 ID/version 清单及哈希，岗位题由面试中的 Question Selection 在清单内随机选择。`experience_questions` 是经人工批准的固定问题，并在所有 `position_bank` 槽位之后执行。两者连同权重、阶段顺序和 `selection_policy` 构成唯一 execution v2 计划；请求、响应和持久运行时均不再包含 `items`。升级旧数据前使用 `python -m app.migrations.plan_execution_v2 --dry-run` 检查，再执行正式迁移。此过程不要求 embedding 或向量数据库。

批准计划前服务端必须验证：

- 岗位、题库、岗位要求、候选人和审阅同组织且关系一致。
- 题库为 `ready`，候选池足够并已冻结题目 ID/version 与集合哈希。
- 所选题库的 speech profile 输出参数完全相同；计划冻结唯一 `speech_profile_snapshot`，包含 TTS ModelConfiguration ID/version、音色、语言、格式、语速和指纹。题库后续切换模型/声音不改写已批准计划。
- Resume Review 为 `ready`，经历问题为 `approved` 且证据有效；其计划快照不绑定全局语音资产，状态为 `deferred`。
- 权重和为 1，时长守恒；放宽去重或覆盖约束必须写入 `assembly_summary.warnings`。

已批准计划不可编辑，只能归档或复制为新草稿。

## 预约、邀请与候选人填报 API

### 企业预约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/interview-appointments` | 创建预约草稿 |
| `GET` | `/api/v1/interview-appointments` | 列出预约 |
| `GET` | `/api/v1/interview-appointments/{appointment_id}` | 查看预约和 readiness |
| `PATCH` | `/api/v1/interview-appointments/{appointment_id}` | 修改尚未邀请的预约 |
| `POST` | `/api/v1/interview-appointments/{appointment_id}/invite` | 通过 readiness gate 并签发邀请 |
| `POST` | `/api/v1/interview-appointments/{appointment_id}/cancel` | 撤销邀请并取消预约 |

创建示例：

```json
{
  "plan_id": "plan_01J...",
  "candidate_profile_id": "cand_01J...",
  "job_position_id": "pos_backend",
  "scheduled_start_at": "2026-09-01T02:00:00Z",
  "scheduled_end_at": "2026-09-01T03:00:00Z",
  "settings": {
    "record_audio": true,
    "record_video": false,
    "avatar_mode": "local",
    "speech_dialogue_mode": "cascade",
    "avatar_id": "avatar_default_cn",
    "voice_profile_id": "voice_cn_01",
    "language": "zh-CN"
  },
  "admission_policy": {
    "early_start_grace_seconds": 0,
    "late_start_grace_seconds": 0,
    "device_readiness_ttl_seconds": 300,
    "consent_version": "v1"
  }
}
```

`settings.avatar_mode` 只接受 `local | cloud`。新预约省略时默认 `local`；`local` 复用计划冻结的 `QuestionSpeechAsset` 并由候选人浏览器渲染形象，`cloud` 调用已配置的 `avatar.speak/interview_question_delivery` route。已持久化但没有该字段的历史预约/会话按 `cloud` 解释，避免升级后改变旧场次。`PATCH` 只合并显式提供的 settings 字段。

`settings.speech_dialogue_mode` 只接受 `cascade | s2s`，默认 `cascade`。`cascade` 保留 `STT -> 受控追问策略 -> Avatar/TTS`；`s2s` 让同一 PCM 并行进入 `speech.dialogue_realtime/candidate_followup_dialogue`，但仍以服务端 STT final 和策略批准文本为真相。S2S route 不可用或输出不符合批准文本时自动回到 cascade，不能影响 CandidateAnswer 或评分。

预约的 `settings.voice_profile_id/language` 由计划冻结的 `speech_profile_snapshot` 派生。创建或修改时显式提交另一音色返回 `409 APPOINTMENT_SPEECH_PROFILE_MISMATCH`，不能让简历题和岗位题出现两套声音。

邀请响应只在签发时返回一次明文 URL：

```json
{
  "appointment": {"id": "appointment_01J...", "status": "invited"},
  "invitation_token": "opaque_token_returned_once",
  "join_url": "/#invite/opaque_token_returned_once"
}
```

React 工作台必须在这个一次性响应弹窗中提供“复制链接”操作和成功/失败反馈，不要求用户手工选中 URL。

数据库只保存 token 哈希。邀请阶段的 `can_invite` 检查计划批准、题库/候选池、岗位题语音和可执行的冻结 speech profile，不等待尚未触发的简历题 TTS；候选人 start 的 `can_start` 还要求本预约全部简历题语音资产已 ready、来源版本与 profile 精确匹配，并继续检查服务端 STT、时间窗和录音留存策略。

### 公开邀请与填报

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/public/interview-invitations/{token}` | 返回岗位名、时间、告知版本和所需字段的安全摘要 |
| `POST` | `/api/v1/public/interview-invitations/{token}/intake` | 提交姓名、邮箱、手机号与授权，匹配成功后确认预约并安排邮件提醒 |
| `POST` | `/api/v1/public/interview-invitations/{token}/readiness` | 上报浏览器、麦克风和音频格式检查 |
| `POST` | `/api/v1/public/interview-invitations/{token}/start` | 候选人在时间窗内幂等创建/启动会话 |

填报请求：

```json
{
  "name": "张三",
  "email": "candidate@example.com",
  "phone": "+8613812345678",
  "consent": {
    "accepted": true,
    "version": "v1",
    "recording_accepted": true
  }
}
```

姓名、邮箱和手机号三项均为必填。GET 邀请响应返回服务端冻结的实际隐私/录音告知正文、允许版本、`recording_required` 和内容 hash；客户端只能回传该版本，不能自定义告知。服务端只与预约绑定的 `CandidateProfile` 比较；至少邮箱或手机号之一精确匹配，姓名联合校验。成功后预约进入 `registered`，表示身份核验和预约确认已经完成，而不是面试已经开始；同一事务创建 `appointment.reminder.email` 和按经历题版本、预约 ID、profile 指纹幂等的 `question.speech.generate` DurableWorkItem。已有完全匹配资产可以复用。响应返回预约时间、安全的提醒状态和聚合 `speech_preparation` 进度，不返回简历内容、内部工作项 ID、题目或联系方式。失败响应不能说明哪个字段不匹配。

登记成功响应示例：

```json
{
  "appointment_id": "appointment_01J...",
  "status": "registered",
  "matched": true,
  "scheduled_start_at": "2026-09-01T02:00:00Z",
  "scheduled_end_at": "2026-09-01T03:00:00Z",
  "email_reminder": {
    "status": "scheduled",
    "scheduled_for": "2026-09-01T01:30:00Z"
  },
  "speech_preparation": {
    "status": "queued",
    "total": 3,
    "ready": 0,
    "failed": 0
  }
}
```

邀请页必须把登记与 start 拆成两次明确操作：“核验身份并确认预约”不能申请麦克风或调用 start；确认成功后显示预约时间和邮件提醒说明，到允许开始时间后才提供“检查设备并进入面试”。SMTP 使用 `INTERVIEWER_SMTP_*` 环境变量，授权码对应 `INTERVIEWER_SMTP_PASSWORD`，仓库样例保持为空。未配置或发送失败只能形成可重试/可观察状态，不能返回或记录虚假的 `sent`。

候选人 start 必须再次检查 token、登记、明确同意、时间窗、未过期设备 readiness 和服务端 STT readiness。默认开始窗口为闭区间 `[scheduled_start_at, scheduled_end_at]`；提前/延后宽限只能来自创建预约时冻结的显式策略。成功时在同一事务原子把预约标为 `consumed`，创建唯一 `InterviewSession` 并冻结候选人、计划、岗位和简历版本；重复调用返回同一会话。

### 候选人会话窄接口

以下接口公开在认证中间件层，但都必须携带 `X-Candidate-Session-Token`。响应只包含当前候选人完成面试所需的 allow-list 字段。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/public/interviews/{interview_id}` | 返回候选人姓名、会话状态、`avatar_mode` 和安全轮次投影；未来题干和所有答案/rubric/候选池均隐藏 |
| `POST` | `/api/v1/public/interviews/{interview_id}/audio-answers` | 提交当前轮次录音；媒体 URI 必须属于该会话和轮次，服务端从受控本地媒体或 `private-file://` FileObject 读取音频并经 STT 后评分；接口不向 Provider 暴露对象存储凭据 |
| `POST` | `/api/v1/public/interviews/{interview_id}/avatar/speak` | 读取当前安全题干并通过 Avatar Delivery seam 朗读；响应兼容 `browser_speech/audio/video/webrtc`，并返回实际 `avatar_mode`；WebRTC 响应带不透明 `session_id/player_kind` |
| `POST` | `/api/v1/public/interviews/{interview_id}/avatar/session/close` | 关闭当前候选人已取得的数智人会话，释放云渲染/SFU 并发；要求候选人 token |

候选人 token 由至少 32 字符的 `INTERVIEWER_CANDIDATE_TOKEN_SECRET` 对会话 ID 与创建时间做 HMAC-SHA256 派生，不明文持久化或出现在后台详情；验证使用常量时间比较，错误 token 返回 403。候选人 WebSocket 继续使用 `/interviews/{id}/live?role=candidate&token=...`，连接后只能发送设备就绪、媒体开始/分片/停止、未受信 partial 和心跳事件。

Avatar Delivery 响应额外包含 `avatar_mode=local|cloud` 和可空的 `fallback_reason=cloud_unavailable`。自研模式的 `audio_uri` 必须是当前轮次冻结语音的五分钟签名地址，不能返回 `private-file://`、对象键或供应商临时 URL。云模式缺少真实 route 或调用失败时不再次复制播放分支，而是调用同一个 local adapter；此时 `avatar_mode=local` 且设置 fallback reason。只有返回 `session_id` 的云媒体需要调用 close。

真实流式转写使用 `/api/v1/interviews/{interview_id}/stt-stream?token=...`：客户端先发送 `stream.open`（正式 Web 端为 `audio/pcm + 16000 Hz + mono`），随后发送二进制 PCM chunk，最后发送 `stream.finish`。服务端返回 `stream.ready/transcript.partial/transcript.final/stream.closed`，且只有唯一 `transcript.final` 可创建 CandidateAnswer；随后立即返回 `answer.accepted + evaluation.queued`，不等待评分。S2S 预约还会返回 `dialogue.ready`、安全的 `followup.selected`、`output.transcript.*`、流式 `output.audio.delta/done` 或 `dialogue.error`。连接或 Provider 失败后使用已保存录音走 `stt.batch`，不信任浏览器 SpeechRecognition。

## 面试会话、逐题评分与企业复核 API

不存在直接创建或直接 START 会话的后台 API。会话只能由公开 invitation start 经 Appointment Admission 创建并首次 START；`PATCH /interview-plans/{id}` 只接受 canonical `bank_slots`，额外字段包括 `items` 会在 schema 层拒绝。

音频回答通过候选人 public 接口或受 RBAC 保护的后台修复接口进入同一个服务：服务端先把轮次迁移到 `transcribing`，以 `stt.batch/candidate_answer_repair` 取得权威 final，再创建 CandidateAnswer 和评分工作项。请求中的 `development_transcript` 只供 localhost mock STT；生产环境显式拒绝。不存在客户端 `POST /answers` 文本入口。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/interviews` | 企业列出面试和复核状态 |
| `GET` | `/api/v1/interviews/{interview_id}` | 获取计划快照、当前阶段和轮次摘要 |
| `POST` | `/api/v1/interviews/{interview_id}/pause` | 暂停并保留当前抽题和转写状态 |
| `POST` | `/api/v1/interviews/{interview_id}/resume` | 继续当前轮次 |
| `POST` | `/api/v1/interviews/{interview_id}/recover` | 修复断线、STT 或 worker 中断 |
| `POST` | `/api/v1/interviews/{interview_id}/skip` | 按策略跳题并进入下一槽位 |
| `POST` | `/api/v1/interviews/{interview_id}/complete` | 人工结束；有转写/评分进行中时返回 409 |
| `POST` | `/api/v1/interviews/{interview_id}/cancel` | 取消且不生成正常报告 |
| `GET` | `/api/v1/interviews/{interview_id}/events` | 按 sequence 返回持久生命周期事实 |

所有命令进入同一个 `InterviewSessionLifecycle` seam。岗位题槽位的选择产生持久 `QuestionSelection` 和 `InterviewQuestionSnapshot`；断线重试不能重新抽题。题库阶段完成后自动切换到 `resume_experience` 阶段。

### 企业复核

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/interviews/{interview_id}/review` | 返回报告、每题题干、转写、评分证据和复核标记 |
| `POST` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/audio-url` | 生成短期、审计的音频签名 URL |
| `PATCH` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/transcript` | 人工修正最终转写并产生新 revision |
| `POST` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/regrade` | 使用冻结题目和指定转写 revision 重评 |
| `GET` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/evaluations` | 列出单题评分 revision |
| `GET` | `/api/v1/interviews/{interview_id}/reports` | 列出报告 revision |
| `GET` | `/api/v1/interviews/{interview_id}/report/export?format=csv|json` | 生成带权限与审计的报告导出 |
| `POST` | `/api/v1/interviews/{interview_id}/review-complete` | 记录企业复核完成，不代表录用决定 |

管理员可调用 `POST /api/v1/admin/evaluations/score-calibration` 提交 `dataset_version` 与 2–5000 条 `{evaluation_id, human_score, fairness_cohort?}`。Schema 拒绝额外字段，接口只解析本组织当前 evaluation revision，不接收候选人姓名、联系方式、转写或简历。响应给出 MAE/RMSE/偏差、±5/±10 一致率、题型/语言/STT 质量/不透明 cohort 分层和仅供人工评审的线性校准候选；样本少于 30 或 cohort 少于 10 时明确告警，校准不会自动写回评分或作录用决定。

复核响应示例：

```json
{
  "interview_id": "iv_01J...",
  "overall_score": 82,
  "job_fit_level": "match",
  "job_fit_evidence": {
    "supports": ["Python 并发与数据库维度达到岗位阈值"],
    "gaps": ["系统设计容量估算证据不足"]
  },
  "turns": [
    {
      "phase": "position_bank",
      "question_text": "请解释 GIL……",
      "final_transcript": "……",
      "score": 86,
      "evidence": [{"text": "……", "start_ms": 4200, "end_ms": 7800}],
      "audio_available": true,
      "review_flags": []
    }
  ],
  "human_decision": null
}
```

API 不根据 `job_fit_level` 自动写入录用/淘汰结果；若未来接 ATS，人员决定必须作为独立、显式、可审计的企业动作。

每次生成报告时，`evaluation_ids` 必须与生成瞬间每个答案的 `current_evaluation_id` 集合完全一致；`overall_score`、岗位证据、风险和 `manual_review` 都只从该集合推导。历史评分仅由引用它的历史报告展示，不能污染当前 revision。

## 实时 WebSocket API

状态通道：

`GET /api/v1/interviews/{interview_id}/live?token={candidate_session_token}`

服务端流式 STT 通道：`GET /api/v1/interviews/{interview_id}/stt-stream?token={candidate_session_token}`。客户端先发送 `stream.open`，再发送二进制音频帧，最后发送 `stream.finish`；服务端返回 `stream.ready`、`stt.transcript.partial`、唯一 `stt.transcript.final` 或 `stream.error`。final 缺失/断流时保存音频并触发 `stt.batch` 修复，不把不同 provider 的 partial 拼成 final。

事件包络：

```json
{
  "type": "stt.transcript.final",
  "request_id": "req_01J...",
  "interview_id": "iv_01J...",
  "turn_id": "turn_01J...",
  "ts": "2026-09-01T02:10:00Z",
  "payload": {}
}
```

### 客户端到服务端

| 事件 | 发送方 | 说明 |
| --- | --- | --- |
| `session.ready` | 候选人/面试官 | 页面和设备就绪；不等于 STT readiness |
| `candidate.media.start` | 候选人 | 声明当前轮次、MIME、采样率和语言 |
| binary frame | 候选人 | Opus/WebM、Ogg 或约定 WebRTC 音频 |
| `candidate.media.stop` | 候选人 | 结束录音并返回受当前会话/轮次约束的媒体 URI；随后调用 public audio-answers 或 streaming STT |
| `candidate.transcript.partial` | 候选人 | 可选的未受信浏览器辅助字幕，只用于界面广播 |
| `interviewer.control.pause/resume/recover/skip/complete/cancel` | 面试官 | 与 REST 共用生命周期命令 |
| `ping` | 任意 | 心跳 |

客户端不能发送 `candidate.transcript.final` 或 `candidate.answer.text`；两者均返回 `REALTIME_EVENT_UNSUPPORTED`。浏览器 SpeechRecognition 可以在本地界面显示未受信 partial，但不能创建答案或置信度。

### 服务端到客户端

| 事件 | 说明 |
| --- | --- |
| `session.state.changed` | 会话状态变化 |
| `question.selection.started` | 开始为岗位题槽位抽题 |
| `question.selected` | 返回冻结后的安全题干和阶段，不返回答案 |
| `avatar.speech.started/completed` | 预生成语音或数字人读题状态 |
| `media.recording.started/stopped` | 服务端录音状态 |
| `stt.transcript.partial` | 服务端 STT 临时转写，只用于展示 |
| `stt.transcript.final` | 服务端权威 final、置信度和片段时间戳 |
| `stt.repair.pending/completed` | 流式识别失败后的批量修复 |
| `answer.accepted` / `evaluation.queued` | 权威答案已落库，评分工作已持久化；响应不包含分数 |
| `followup.selected` | 安全追问投影，只含父子轮次与题干，不含内部缺失关键点 |
| `dialogue.ready/error/closed` | S2S 表达轨状态；失败不改变证据轨 |
| `output.transcript.delta/final` | S2S 实际播报文本，用于可观察性与批准文本校验 |
| `output.audio.delta/done` | Base64 PCM 语音分片；候选人端按采样率排队播放 |
| `evaluation.started/completed/failed` | 当前题评分状态和安全摘要 |
| `interview.phase.changed` | `position_bank` 切换到 `resume_experience` |
| `interview.completed` | 问答完成，报告可能仍在生成 |
| `report.ready` | 新报告 revision 可查看 |
| `error` | 统一可恢复/不可恢复错误 |

服务端音频路径：`binary/WebRTC -> stt.streaming -> partial/final -> CandidateAnswer -> evaluation`。如果 stream 失败，先持久化音频并发出 `stt.repair.pending`，通过 `stt.batch` 得到 final 后再评分；不同 provider 的 partial 不得拼接成一个 final。

## 模型网关和后台配置 API

请求/响应式业务只依赖 `invoke`；实时 STT 通过同一 Model Invocation deep module 的 `open_stream`，不能让实时网关直接 import Provider：

```python
class ModelGateway:
    async def invoke(
        self,
        capability: str,
        request: "InvocationRequest",
        *,
        route: "ModelRoute | None" = None,
    ) -> "InvocationResponse": ...

    async def open_stream(
        self,
        request: "StreamingSTTRequest",
        *,
        route: "ModelRoute | None" = None,
    ) -> "ValidatedSTTStream": ...

    async def open_speech_dialogue(
        self,
        request: "RealtimeSpeechDialogueRequest",
        *,
        route: "ModelRoute | None" = None,
    ) -> "ValidatedSpeechDialogueStream": ...
```

业务调用按 `organization_id + capability + purpose` 解析 route。正式面试目标流程要求统一 schema 覆盖 `llm.chat_json`、`tts.synthesize`、`stt.streaming`、`stt.batch` 和需要的 `avatar.speak`；没有 schema、可执行 adapter 和通过健康测试的能力不能进入 active route。`embedding.text` 是可选的未来题库治理能力，不是题库 ready、计划批准、预约邀请、随机抽题或答案评分的前置条件；仓库不再持久化旧向量题库 projection。

低延迟追问可额外配置 `speech.dialogue_realtime/candidate_followup_dialogue`。对应 ModelConfiguration 使用 `realtime_speech` 类型；没有 schema、可执行 adapter 和通过健康测试的模型不能进入 active route，缺 route 时面试继续使用 cascade。

模型管理分为三层：`ProviderConnection` 只保存组织级厂商连接、API Key 引用和区域等连接参数；`ModelConfiguration` 选择 `llm/embedding/tts/stt/avatar/realtime_speech` 类型及厂商模型，并保存该模型的专属设置和统一默认参数；`ModelRoute` 只引用模型配置。插件 manifest 返回 `connection_form`、`credential_form` 与各模型类型的 `configuration_form`，前端使用通用控件渲染器，不内置任何厂商字段。

ProviderConnection 与 ModelConfiguration 响应增加单调递增的 `configuration_revision`。创建为 1；连接参数/凭据或模型 settings/default parameters/启用状态/显示名修改时增加；凭据校验和任意能力健康探针只推进通用 `version` 与健康事实，不改变 `configuration_revision`。因此 LLM、Embedding、STT、TTS、实时语音和数字人的编辑/删除页面都可以安全吸收探针造成的新 version，同时拒绝覆盖真正的并发配置修改。

`POST /admin/model-routes` 使用强类型请求：`primary`/`fallbacks` 仅接受 `model_configuration_id`、`timeout_s`、`pricing`；路由创建时校验模型配置已启用、状态为 `ready` 且支持目标 capability。`policy` 仅接受既有重试、熔断、成本和 readiness 字段。生产环境找不到精确 route 时返回 `provider_route_missing`，只有 development/test 允许离线 mock fallback。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/admin/model-providers/catalog` | 已安装插件、能力和实现状态 |
| `POST/GET/PATCH/DELETE` | `/api/v1/admin/model-provider-connections[/{id}]` | 创建、列表/单项查看、修改和删除厂商连接；凭证只写入、读取时仅返回状态 |
| `POST` | `/api/v1/admin/model-provider-connections/{id}/validate` | 通过 Provider adapter 执行真实 API Key 鉴权探针，保存 `valid` / `invalid` / `model_required` 状态 |
| `GET` | `/api/v1/admin/model-provider-connections/{id}/model-catalog` | 返回该连接可配置的模型类型、模型目录和动态表单 schema |
| `POST/GET/PATCH/DELETE` | `/api/v1/admin/model-configurations[/{id}]` | 创建、列表/单项查看、修改和删除具体模型及其参数与启用状态 |
| `POST` | `/api/v1/admin/model-configurations/{id}/test` | 以模型配置的统一能力探针进行真实调用并更新健康状态；流式 STT/实时语音对话验证真实 session 握手，不用静音伪造识别质量样本 |
| `GET` | `/api/v1/admin/model-configurations/{id}/voices` | 返回 TTS 模型可选声音目录；静态 manifest、管理员映射或 Provider 只读目录由后端统一归一化 |
| `POST/GET` | `/api/v1/admin/model-routes` | 管理能力/purpose 路由 |
| `POST` | `/api/v1/admin/model-routes/{id}/test` | 测试 schema、primary 和 fallback |
| `GET` | `/api/v1/admin/work-items` | 查看状态计数、dead-letter 指标和工作项摘要 |
| `POST` | `/api/v1/admin/work-items/{id}/replay` | 带原因和审计地人工重放失败/dead-letter 工作项 |
| `GET` | `/api/v1/admin/audit-events` | 管理员读取租户审计事件 |
| `GET` | `/api/v1/admin/evaluations/question-selection-fairness` | 比较同岗位抽题数量、难度和技能覆盖分布 |
| `POST` | `/api/v1/admin/session-monitor/run` | 扫描心跳超时并通过生命周期命令暂停会话 |
| `POST` | `/api/v1/admin/retention/run` | 默认 dry-run 预览到期候选人；显式 `dry_run=false` 才清除私有文件、联系方式和面试敏感内容并审计 |

两个 `DELETE` 都必须在查询参数携带 `expected_version`，陈旧版本返回 `409`。删除 ProviderConnection 会在同一租户事务中删除它的加密凭证、全部 ModelConfiguration，以及引用这些模型的 ModelRoute 和对应断路器状态；删除单个 ModelConfiguration 只删除该模型及引用它的路由/断路器状态，同连接下其他模型和厂商连接保持不变。历史 ModelInvocationLog 作为脱敏审计事实保留。

流式能力的管理员测试采用分层语义：`stt.streaming` 和 `speech.dialogue_realtime` 在收到 Provider 的 ready/session-created/session-updated 确认后返回 `probe_mode=handshake`，证明端点、凭据、模型访问和 session 参数可用，并立即主动关闭连接。探针不要求静音产生 final transcript，也不生成自由回答；WER、真实 final、首音、音质和打断必须继续用脱敏语音样本及候选人端到端链路验收。模型路由测试复用相同握手合同。

具体 Provider manifest、STT/TTS 请求响应和错误语义见 [模型供应商插件化设计](model-provider-plugins.md)。
