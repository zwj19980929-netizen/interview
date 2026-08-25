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
- 明确建模为异步工作的接口（题库 import/rebuild/build、PDF 摄取等）返回 `202 Accepted` 和 `job_id`，通过工作项接口查询状态。

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

## Web 工作台与候选人页面

- `GET /`：内置企业工作台。
- `GET /web/*`：静态资源。
- `/#positions`、`/#knowledge-bases`、`/#candidates`、`/#plans`、`/#appointments`、`/#interviews`：后台工作区。
- `/#invite/{invitation_token}`：公开邀请、姓名/邮箱/手机号填报、授权和设备检查。
- start 响应首次导航可在 URL fragment 中携带 `candidate_session_token`；前端立即转存到 `sessionStorage` 并用 `history.replaceState` 清除 fragment 中的 token，随后进入 `/#candidate/{interview_id}`。
- 候选人 HTTP 请求通过 `X-Candidate-Session-Token` header 调用 public 窄接口；WebSocket 握手使用同一短期 token。后台 Bearer API 不接受候选人 token。

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
| `POST` | `/api/v1/job-positions` | 创建岗位 |
| `GET` | `/api/v1/job-positions` | 列出岗位 |
| `GET` | `/api/v1/job-positions/{position_id}` | 获取岗位及题库摘要 |
| `PATCH` | `/api/v1/job-positions/{position_id}` | 修改或归档岗位 |

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
| `GET` | `/api/v1/job-positions/{position_id}/knowledge-bases` | 列出岗位题库 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 查看题库、构建状态和计数 |
| `PATCH` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 修改题库配置或归档 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/imports` | 上传或结构化导入题目 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/rebuild` | 重建索引和缺失读题语音 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/builds/{job_id}` | 查询构建工作项 |

导入使用 multipart 文件或 JSON items。响应立即返回：

```json
{
  "job_id": "job_kb_01J...",
  "knowledge_base_id": "kb_backend_cn",
  "status": "queued",
  "tasks": ["parse", "validate_candidate_pool", "question_speech"]
}
```

构建工作项依次校验题目、标准答案、关键点、rubric、技能、难度和题型，写入结构化候选池，并按题库的 `language + voice_profile_id` 异步生成每道活动题的 `QuestionSpeechAsset`。部分失败时题库保持 `building` 或进入 `failed`，响应必须列出失败题目和重试入口；不能把缺少评分依据或语音的题库标为 `ready`。MVP 不生成 embedding，也不依赖向量数据库。

### 题目

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/questions` | 创建题目并排队校验/语音 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/questions` | 列出题目 |
| `PATCH` | `/api/v1/questions/{question_id}` | 修改题目；内容变化产生新版本和新语音任务 |
| `POST` | `/api/v1/questions/{question_id}/speech/regenerate` | 指定语言/音色重建读题语音 |
| `POST` | `/api/v1/questions/search` | 后台按关键词和结构化字段查题，不用于实时抽题 |

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

服务端验证所有题库属于 `job_position_id` 和当前组织；查询底层强制过滤 `organization_id + job_position_id + knowledge_base_ids + active + valid + speech-ready` 及技能/难度/题型条件。实时 Question Selection 不调用该搜索接口，而是直接使用批准计划冻结的题目 ID/version 候选清单。

## 简历库与 AI 审阅 API

### 候选人记录和简历

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/candidate-profiles` | 企业创建候选人基本信息 |
| `GET` | `/api/v1/candidate-profiles` | 搜索组织内简历库 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}` | 查看候选人和简历版本 |
| `PATCH` | `/api/v1/candidate-profiles/{candidate_id}` | 更新基本信息或归档 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 以 multipart 上传本地 PDF，创建新简历版本 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes/import-url` | 从公开 HTTPS URL 异步导入 PDF，创建新简历版本 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 列出简历版本和摄取状态 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}` | 查看解析状态和授权元数据 |
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

本地文件上传使用 `multipart/form-data`：

```text
file=<candidate.pdf>
display_name=张三-后端工程师简历.pdf  # 可选
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
  "display_name": "候选人简历.pdf"
}
```

URL 导入也返回 `202`，`source_type=url_import`。下载由 worker 使用受控 HTTP 客户端执行：生产默认只允许 HTTPS，限制连接/总超时、重定向次数和最大字节数；初始 URL 及每次重定向都要重新解析 DNS，并拒绝环回、私网、链路本地、保留地址、云元数据地址、非 HTTP(S) scheme 和 URL 内嵌凭证。MVP 不接收需要 Cookie、Authorization 或企业内网访问的 URL。

两种入口随后执行相同流水线：`receive/download -> quarantine -> PDF signature/MIME validation -> malware scan -> private store -> parse -> ready`。外部 URL 只作为摄取来源，成功后解析、审阅和下载都读取系统托管文件；不得长期依赖原 URL，也不得把它直接返回给浏览器。相同 `Idempotency-Key` 的重试返回同一个 `ResumeDocument`，底层可按文件哈希去重物理对象，但不能覆盖已有简历版本。

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
  "review_id": "rr_01J...",
  "job_id": "job_resume_01J...",
  "status": "queued"
}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/resume-reviews/{review_id}` | 返回项目/技能证据、告警和问题状态 |
| `GET` | `/api/v1/resume-reviews/{review_id}/experience-questions` | 列出 AI 生成问题 |
| `PATCH` | `/api/v1/experience-questions/{question_id}` | 人工编辑、批准或拒绝 |
| `POST` | `/api/v1/experience-questions/{question_id}/speech/regenerate` | 重建问题语音 |

AI 问题默认为 `draft`。接口返回每个问题的 `project_ref` 和 `evaluation_focus` 供面试官核对；批准后才排队生成正式语音并允许进入计划。不得把简历中的受保护属性或无关个人信息发送给评分模型。

## 岗位要求与面试计划 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions/{position_id}/role-requirements` | 创建岗位要求版本 |
| `GET` | `/api/v1/job-positions/{position_id}/role-requirements` | 列出岗位要求 |
| `POST` | `/api/v1/interview-plans/generate` | 生成候选人专属草稿计划 |
| `GET` | `/api/v1/interview-plans` | 列出计划 |
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

响应中的 `bank_slots` 只定义维度、难度、题型、权重和时长；计划装配通过关系库字段形成每个槽位的 `QuestionCandidatePool`，批准时冻结筛选条件、题目 ID/version 清单及哈希，岗位题由面试中的 Question Selection 在清单内随机选择。`experience_questions` 是经人工批准的固定问题，并在所有 `position_bank` 槽位之后执行。两者连同权重、阶段顺序和 `selection_policy` 构成唯一 execution v2 计划；请求、响应和持久运行时均不再包含 `items`。升级旧数据前使用 `python -m app.migrations.plan_execution_v2 --dry-run` 检查，再执行正式迁移。此过程不要求 embedding 或向量数据库。

批准计划前服务端必须验证：

- 岗位、题库、岗位要求、候选人和审阅同组织且关系一致。
- 题库为 `ready`，候选池足够并已冻结题目 ID/version 与集合哈希。
- Resume Review 为 `ready`，经历问题为 `approved` 且语音为 `ready`。
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
    "avatar_id": "avatar_default_cn",
    "voice_profile_id": "voice_cn_01"
  },
  "admission_policy": {
    "early_start_grace_seconds": 0,
    "late_start_grace_seconds": 0,
    "device_readiness_ttl_seconds": 300,
    "consent_version": "v1"
  }
}
```

邀请响应只在签发时返回一次明文 URL：

```json
{
  "appointment": {"id": "appointment_01J...", "status": "invited"},
  "invitation_token": "opaque_token_returned_once",
  "join_url": "/#invite/opaque_token_returned_once"
}
```

数据库只保存 token 哈希。readiness 至少检查计划批准、题库/候选池、经历问题语音、服务端 `stt.streaming` route 健康、时间窗和录音留存策略。

### 公开邀请与填报

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/public/interview-invitations/{token}` | 返回岗位名、时间、告知版本和所需字段的安全摘要 |
| `POST` | `/api/v1/public/interview-invitations/{token}/intake` | 提交姓名、邮箱、手机号与授权并匹配 |
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

姓名、邮箱和手机号三项均为必填。GET 邀请响应返回服务端冻结的实际隐私/录音告知正文、允许版本、`recording_required` 和内容 hash；客户端只能回传该版本，不能自定义告知。服务端只与预约绑定的 `CandidateProfile` 比较；至少邮箱或手机号之一精确匹配，姓名联合校验。成功后预约进入 `registered`，保存匹配方式、授权布尔值、告知 hash 与服务端时间，但不返回简历内容。失败响应不能说明哪个字段不匹配。

候选人 start 必须再次检查 token、登记、明确同意、时间窗、未过期设备 readiness 和服务端 STT readiness。默认开始窗口为闭区间 `[scheduled_start_at, scheduled_end_at]`；提前/延后宽限只能来自创建预约时冻结的显式策略。成功时在同一事务原子把预约标为 `consumed`，创建唯一 `InterviewSession` 并冻结候选人、计划、岗位和简历版本；重复调用返回同一会话。

### 候选人会话窄接口

以下接口公开在认证中间件层，但都必须携带 `X-Candidate-Session-Token`。响应只包含当前候选人完成面试所需的 allow-list 字段。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/public/interviews/{interview_id}` | 返回候选人姓名、会话状态和安全轮次投影；未来题干和所有答案/rubric/候选池均隐藏 |
| `POST` | `/api/v1/public/interviews/{interview_id}/audio-answers` | 提交当前轮次录音；媒体 URI 必须属于该会话和轮次，服务端 STT 后评分 |
| `POST` | `/api/v1/public/interviews/{interview_id}/avatar/speak` | 读取当前安全题干并通过 avatar/TTS seam 朗读 |

候选人 token 由至少 32 字符的 `INTERVIEWER_CANDIDATE_TOKEN_SECRET` 对会话 ID 与创建时间做 HMAC-SHA256 派生，不明文持久化或出现在后台详情；验证使用常量时间比较，错误 token 返回 403。候选人 WebSocket 继续使用 `/interviews/{id}/live?role=candidate&token=...`，连接后只能发送设备就绪、媒体开始/分片/停止、未受信 partial 和心跳事件。

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
```

业务调用按 `organization_id + capability + purpose` 解析 route。正式面试目标流程要求统一 schema 覆盖 `llm.chat_json`、`tts.synthesize`、`stt.streaming`、`stt.batch` 和需要的 `avatar.speak`；没有 schema、可执行 adapter 和通过健康测试的能力不能进入 active route。`embedding.text` 是可选的未来题库治理能力，不是题库 ready、计划批准、预约邀请、随机抽题或答案评分的前置条件；仓库不再持久化旧向量题库 projection。

模型管理分为三层：`ProviderConnection` 只保存组织级厂商连接、API Key 引用和区域等连接参数；`ModelConfiguration` 选择 `llm/embedding/tts/stt/avatar` 类型及厂商模型，并保存该模型的专属设置和统一默认参数；`ModelRoute` 只引用模型配置。插件 manifest 返回 `connection_form`、`credential_form` 与各模型类型的 `configuration_form`，前端使用通用控件渲染器，不内置任何厂商字段。

`POST /admin/model-routes` 使用强类型请求：`primary`/`fallbacks` 仅接受 `model_configuration_id`、`timeout_s`、`pricing`；路由创建时校验模型配置已启用、状态为 `ready` 且支持目标 capability。`policy` 仅接受既有重试、熔断、成本和 readiness 字段。生产环境找不到精确 route 时返回 `provider_route_missing`，只有 development/test 允许离线 mock fallback。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/admin/model-providers/catalog` | 已安装插件、能力和实现状态 |
| `POST/GET/PATCH` | `/api/v1/admin/model-provider-connections` | 管理厂商连接；凭证只写入、读取时仅返回状态 |
| `POST` | `/api/v1/admin/model-provider-connections/{id}/validate` | 校验连接字段与凭证是否具备创建模型的条件，不产生付费模型调用 |
| `GET` | `/api/v1/admin/model-provider-connections/{id}/model-catalog` | 返回该连接可配置的模型类型、模型目录和动态表单 schema |
| `POST/GET/PATCH` | `/api/v1/admin/model-configurations` | 管理具体模型及其厂商参数、统一默认参数和启用状态 |
| `POST` | `/api/v1/admin/model-configurations/{id}/test` | 以模型配置的统一能力探针进行真实调用并更新健康状态 |
| `POST/GET` | `/api/v1/admin/model-routes` | 管理能力/purpose 路由 |
| `POST` | `/api/v1/admin/model-routes/{id}/test` | 测试 schema、primary 和 fallback |
| `GET` | `/api/v1/admin/work-items` | 查看状态计数、dead-letter 指标和工作项摘要 |
| `POST` | `/api/v1/admin/work-items/{id}/replay` | 带原因和审计地人工重放失败/dead-letter 工作项 |
| `GET` | `/api/v1/admin/audit-events` | 管理员读取租户审计事件 |
| `GET` | `/api/v1/admin/evaluations/question-selection-fairness` | 比较同岗位抽题数量、难度和技能覆盖分布 |
| `POST` | `/api/v1/admin/session-monitor/run` | 扫描心跳超时并通过生命周期命令暂停会话 |
| `POST` | `/api/v1/admin/retention/run` | 默认 dry-run 预览到期候选人；显式 `dry_run=false` 才清除私有文件、联系方式和面试敏感内容并审计 |

具体 Provider manifest、STT/TTS 请求响应和错误语义见 [模型供应商插件化设计](model-provider-plugins.md)。
