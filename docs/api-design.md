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
- 异步接口返回 `202 Accepted` 和 `job_id`，通过工作项接口查询状态，不在请求内等待模型或 TTS。

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
- 登记成功后前端只在内存或安全会话存储中持有 `candidate_session_token`，再进入 `/#candidate/{interview_id}`；URL 不长期暴露 session token。

当前 `/#candidate/{interview_id}?token=...` 只保留为本地 MVP 兼容路径，不是目标预约流程。

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

服务端验证所有题库属于 `job_position_id` 和当前组织；查询底层强制过滤 `organization_id + job_position_id + knowledge_base_ids + active`。实时 Question Selection 不调用该搜索接口，而是直接使用批准计划冻结的题目 ID/version 候选清单。

## 简历库与 AI 审阅 API

### 候选人记录和简历

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/candidate-profiles` | 企业创建候选人基本信息 |
| `GET` | `/api/v1/candidate-profiles` | 搜索组织内简历库 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}` | 查看候选人和简历版本 |
| `PATCH` | `/api/v1/candidate-profiles/{candidate_id}` | 更新基本信息或归档 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 上传新简历版本 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}` | 查看解析状态和授权元数据 |

创建示例：

```json
{
  "name": "张三",
  "email": "candidate@example.com",
  "phone": "+8613812345678",
  "external_ref": "ats-2048"
}
```

服务端规范化并加密邮箱和手机号；响应默认只返回脱敏值。简历上传限制类型、大小并做恶意文件扫描，原文件存对象存储而非数据库 BLOB。

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

响应中的 `bank_slots` 只定义维度、难度、题型、权重和时长；计划装配通过关系库字段形成每个槽位的 `QuestionCandidatePool`，批准时冻结筛选条件、题目 ID/version 清单及哈希，岗位题由面试中的 Question Selection 在清单内随机选择。`experience_questions` 是经人工批准的固定问题，并在所有 `position_bank` 槽位之后执行。此过程不要求 embedding 或向量数据库。

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
  "scheduled_start_at": "2026-09-01T02:00:00Z",
  "scheduled_end_at": "2026-09-01T03:00:00Z",
  "timezone": "Asia/Shanghai",
  "invitation_expires_at": "2026-09-01T03:00:00Z",
  "settings": {
    "record_audio": true,
    "allow_text_fallback": false,
    "avatar_id": "avatar_default_cn",
    "voice_profile_id": "voice_cn_01"
  }
}
```

邀请响应只在签发时返回一次明文 URL：

```json
{
  "id": "appt_01J...",
  "status": "invited",
  "invitation_url": "https://interview.example.com/#invite/inv_opaque_token",
  "expires_at": "2026-09-01T03:00:00Z"
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
    "version": "privacy-2026-09",
    "recording_accepted": true
  }
}
```

姓名、邮箱和手机号三项均为必填。服务端只与预约绑定的 `CandidateProfile` 比较；至少邮箱或手机号之一精确匹配，姓名联合校验。成功后预约进入 `registered`，返回短期 `candidate_session_token`，但不返回简历内容。失败响应不能说明哪个字段不匹配。

候选人 start 必须再次检查 token、登记、时间窗、设备和服务端 STT。成功时原子把预约标为 `consumed`，创建唯一 `InterviewSession` 并冻结候选人、计划、岗位和简历版本；重复调用返回同一会话。

## 面试会话、逐题评分与企业复核 API

当前直接 `POST /api/v1/interviews` 只作为本地 MVP 管理员兼容入口；目标生产流程只能由公开 start 从预约创建会话。

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

## 实时 WebSocket API

连接：

`GET /api/v1/interviews/{interview_id}/live?token={candidate_session_token}`

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
| `candidate.media.stop` | 候选人 | 结束录音；服务端等待 STT final |
| `candidate.answer.text` | 候选人 | 仅预约明确允许的文本兜底 |
| `interviewer.control.pause/resume/recover/skip/complete/cancel` | 面试官 | 与 REST 共用生命周期命令 |
| `ping` | 任意 | 心跳 |

生产客户端不能发送 `candidate.transcript.final`。浏览器 SpeechRecognition 可以在本地界面显示未受信 partial，但服务器忽略它，不能创建答案或置信度。

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
        capability: str,
        request: "StreamingInvocationRequest",
    ) -> "InvocationStream": ...
```

业务调用按 `organization_id + capability + purpose` 解析 route。正式面试目标流程要求统一 schema 覆盖 `llm.chat_json`、`tts.synthesize`、`stt.streaming`、`stt.batch` 和需要的 `avatar.speak`；没有 schema、可执行 adapter 和通过健康测试的能力不能进入 active route。`embedding.text` 继续作为兼容/未来题库治理能力存在，但不是题库 ready、计划批准、预约邀请、随机抽题或答案评分的前置条件。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/admin/model-providers/catalog` | 已安装插件、能力和实现状态 |
| `POST/GET/PATCH` | `/api/v1/admin/model-provider-configs` | 管理组织 provider 配置，凭证脱敏 |
| `POST` | `/api/v1/admin/model-provider-configs/{id}/test` | 测试连接和单项能力 |
| `POST/GET` | `/api/v1/admin/model-routes` | 管理能力/purpose 路由 |
| `POST` | `/api/v1/admin/model-routes/{id}/test` | 测试 schema、primary 和 fallback |
| `GET` | `/api/v1/admin/readiness/interview` | 检查计划、题目语音、STT、评分和存储 readiness |

具体 Provider manifest、STT/TTS 请求响应和错误语义见 [模型供应商插件化设计](model-provider-plugins.md)。
