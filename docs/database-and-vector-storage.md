# 数据库与向量存储设计

本文记录当前本地存储实现，以及岗位题库、简历库、预约、题目语音、服务端 STT 和企业复核迁移到 PostgreSQL 的目标边界。向量存储是可选优化，不是正式面试主链路依赖。

## 当前决策

- 本地测试数据库：SQLite。
- 当前代码已有试验性向量存储：SQLite 保存向量 JSON，由 Python 计算余弦相似度。
- 最终长期存储：PostgreSQL；当前已有可选择 adapter 与首版迁移，真实集群验收待部署环境。
- MVP/试点不建设必需的向量数据库，也不要求安装 pgvector。
- 简历、题目语音和候选人回答音频：私有文件存储；数据库只保存文件对象 ID、存储定位元数据、哈希和状态，不保存 BLOB。简历文件先实现本地私有文件 adapter，并预留阿里云 OSS adapter 通过配置切换。

题目已由企业归入明确岗位题库，并具备技能、难度、题型、状态、标准答案和 rubric。正式抽题只需 PostgreSQL 结构化过滤、冻结候选清单和稳定随机抽样；答案评分直接使用冻结题目与最终转写调用结构化 LLM，因此二者都不需要向量查询。

如果未来题量巨大、后台需要自然语言查题或自动发现近似重复题，可以选择启用 pgvector 或独立向量 adapter。向量数据必须保持可删除、可重建，不能成为 Question、InterviewPlan 或历史评分的真相来源。

## 环境变量

```bash
INTERVIEWER_DB_BACKEND=sqlite
INTERVIEWER_SQLITE_PATH=data/interviewer.sqlite3
INTERVIEWER_MEDIA_PATH=data/media
INTERVIEWER_FILE_STORAGE_BACKEND=local
INTERVIEWER_PRIVATE_FILE_ROOT=data/private-files
INTERVIEWER_FILE_QUARANTINE_ROOT=data/file-quarantine
INTERVIEWER_FILE_MAX_BYTES=10485760
INTERVIEWER_FILE_SIGNING_SECRET=replace-with-32-plus-random-characters
```

`INTERVIEWER_DB_BACKEND=memory` 只用于单元测试和临时演示。`INTERVIEWER_FILE_STORAGE_BACKEND=local` 只用于本地开发，目录不能挂载为公开静态资源。阿里云 OSS adapter 计划使用以下部署配置，密钥值必须来自密钥管理器或部署 Secret，不能进入普通配置文档或日志：

```bash
INTERVIEWER_FILE_STORAGE_BACKEND=aliyun_oss
INTERVIEWER_OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
INTERVIEWER_OSS_BUCKET=interviewer-private
INTERVIEWER_OSS_ACCESS_KEY_ID=from-deployment-secret
INTERVIEWER_OSS_ACCESS_KEY_SECRET=from-deployment-secret
INTERVIEWER_OSS_SSE=AES256
```

PostgreSQL 使用 `INTERVIEWER_DB_BACKEND=postgresql` 与 `INTERVIEWER_POSTGRES_DSN`；Redis 跨实例事件使用 `INTERVIEWER_REDIS_URL`。生产还必须提供联系人/Provider 凭证加密密钥、媒体签名密钥和 `INTERVIEWER_FILE_SCANNER_COMMAND`，不能复用开发默认值、公开本地目录或把 OSS bucket 设为公开读。

## 当前代码边界

现有本地 repository：

```text
app/repositories/provider.py
app/repositories/sqlite.py
app/repositories/memory.py
app/repositories/postgresql.py
```

事务型 Persistence seam：

```text
app/persistence/interface.py
app/persistence/provider.py
app/persistence/memory.py
app/persistence/sqlite.py
app/persistence/postgresql.py
migrations/001_postgresql_persistence.sql
```

当前事务工作区已有 `JobPositionRepository`、`KnowledgeBaseRepository`、`QuestionRepository`、`QuestionSpeechAssetRepository`、`CandidateProfileRepository`、`ResumeDocumentRepository`、`FileObjectRepository`、`AuditEventRepository`、`ResumeReviewRepository`、`ExperienceQuestionRepository`、`RoleRequirementRepository`、`InterviewPlanRepository`、`InterviewAppointmentRepository`、`CandidateIntakeRepository`、`InterviewSessionRepository`、`ModelCircuitStateRepository`、模型配置/路由/调用、加密凭证和 Outbox repository。它们通过同一个 versioned document interface 暴露，Memory、SQLite 与 PostgreSQL adapter 共用事务语义；Question Catalog 的租户、岗位、题库、状态/readiness、技能、难度和题型条件由 backend contract 执行。旧 `VectorDocumentRepository` 与 Memory/SQLite 向量集合已删除，关系型题库查询不持久化向量 projection。

联系人在写入前使用 Fernet 加密并以租户绑定 HMAC 查找，API 只返回掩码；Provider 凭证也在 repository seam 密封。`ResumeDocument` 只接受 PDF，引用原件与解析文本两个私有 FileObject；SQLite JSON 不再保存新简历正文。回答音频仍由本地媒体 adapter 保存，但不再静态挂载，只能用短期签名 token 回读并记录授权/下载审计。SQLite 仍只用于开发/测试；生产租户边界由 PostgreSQL `organization_id + FORCE RLS` 提供第二道防线。

PostgreSQL 首版迁移采用受约束 JSONB documents，以保持现有聚合事务语义，并增加 `(organization_id, idempotency_key)` Outbox 唯一约束、预约单会话唯一索引、选择槽位唯一检查、Provider 凭证复合主键和全部表的强制 RLS。它是可运行 adapter，不等同于下文完全规范化关系表的最终形态；真实数据库上的迁移、隔离、并发和 `EXPLAIN` 仍必须在部署环境执行，不能由离线 SQL 文本检查替代。

SQLite 当前用通用 JSON documents 表保存业务对象。`InterviewSession` 以单一聚合文档保存候选人、计划/题目快照、轮次、回答、评分/报告 revision、中断上下文和生命周期事件。生命周期命令产生的聚合、领域事件和 Outbox 工作项原子提交；这属于带事件日志的状态持久化，不是完整 event sourcing。

本地候选人录音由 `app/adapters/local_media.py` 写入：

```text
data/media/{interview_id}/{turn_id}/{recording_id}.{extension}
```

该路径只适合本地录音，但目录不再通过 StaticFiles 公开；回听必须经过 reviewer 权限、短期签名和下载审计。简历原件、解析文本和真实 TTS 资产已通过独立 Private File Storage seam 保存；非 mock TTS 只有复制并校验到私有存储后才可通过生产 readiness。

## Private File Storage seam

文件存储 module 在 `app/file_storage/` 提供小而稳定的 interface，隐藏路径清洗、流式写入、哈希校验、原子落盘/上传、加密选项、短期访问和删除细节：

```python
class PrivateFileStorage(Protocol):
    def store(self, *, organization_id, object_id, content, content_type, checksum) -> StoredFile: ...
    def open(self, object_key: str) -> bytes: ...
    def issue_read_access(self, object_key: str, *, expires_seconds: int = 300) -> str: ...
    def delete(self, object_key: str) -> None: ...
```

当前提供两个 adapter：

- `LocalPrivateFileAdapter`：本地开发使用，写入 `data/private-files/{organization_id}/...`；先写随机临时文件，校验哈希后原子移动到最终 key。文件不通过 `/web` 或普通静态目录暴露，回读必须经过鉴权接口。
- `AliyunOssFileAdapter`：使用阿里云 OSS 私有 bucket、固定 endpoint、服务端加密和受限凭证；下载使用最长 15 分钟签名 URL。离线 fake-bucket contract 已验证接口与 SSE/header 语义，真实 RAM/bucket/网络仍需环境验收；业务 module 不直接 import `oss2`，也不拼接 bucket URL。

`store` 返回 `file_object_id/object_key/content_hash/size_bytes/content_type/backend` 等受控元数据。`ResumeDocument` 只引用 `file_object_id`，因此从本地迁移到 OSS 时可以复制物理对象并更新文件对象定位，不改简历、审阅和面试历史的业务 ID。

## 目标 Repository 边界

在现有 interface 上增加：

```text
JobPositionRepository
KnowledgeBaseRepository
QuestionSpeechAssetRepository
CandidateProfileRepository
ResumeDocumentRepository
ResumeReviewRepository
ExperienceQuestionRepository
InterviewAppointmentRepository
CandidateIntakeRepository
```

`QuestionSelection`、`InterviewCandidate`、`InterviewPlanSnapshot`、`InterviewQuestionSnapshot`、轮次、答案、评分和报告仍由 `InterviewSessionRepository` 作为聚合拥有数据提交。对于企业复核查询，可以增加只读 projection repository，但不能成为第二个写入入口。

## PostgreSQL 目标关系

建议表及关键关系：

```sql
organizations(...)
users(...)

job_positions(
  id text primary key,
  organization_id text not null,
  code text not null,
  version integer not null,
  unique (organization_id, code)
)

knowledge_bases(
  id text primary key,
  organization_id text not null,
  job_position_id text not null references job_positions(id),
  status text not null,
  version integer not null
)

questions(
  id text primary key,
  organization_id text not null,
  knowledge_base_id text not null references knowledge_bases(id),
  version integer not null,
  validation_status text not null,
  speech_status text not null
)

question_speech_assets(
  id text primary key,
  organization_id text not null,
  owner_type text not null,
  owner_id text not null,
  source_version integer not null,
  language text not null,
  voice_profile_id text not null,
  content_hash text not null,
  audio_uri text not null,
  status text not null,
  unique (organization_id, owner_type, owner_id, source_version, language, voice_profile_id, content_hash)
)

candidate_profiles(..., organization_id text not null, version integer not null)
file_objects(..., organization_id text not null, category text not null, backend text not null, object_key text not null, content_hash text not null, size_bytes bigint not null, unique(organization_id, backend, object_key))
resume_documents(..., candidate_profile_id text not null references candidate_profiles(id), source_type text not null, file_object_id text references file_objects(id), file_hash text, ingestion_status text not null, scan_status text not null, parse_status text not null, version integer not null)
resume_reviews(..., resume_document_id text not null references resume_documents(id), job_position_id text not null references job_positions(id), version integer not null)
experience_questions(..., resume_review_id text not null references resume_reviews(id), version integer not null)
role_requirements(..., job_position_id text not null references job_positions(id), version integer not null)

interview_plans(..., job_position_id text not null, candidate_profile_id text not null, resume_review_id text not null, version integer not null)
interview_plan_knowledge_bases(..., plan_id text not null, knowledge_base_id text not null, knowledge_base_version integer not null)
interview_plan_candidate_questions(..., plan_id text not null, slot_id text not null, question_id text not null, question_version integer not null)

interview_appointments(..., candidate_profile_id text not null, plan_id text not null, invitation_token_hash text, version integer not null)
candidate_intakes(..., appointment_id text not null unique, matched_candidate_profile_id text not null, match_method text not null, consent_version text not null, privacy_accepted boolean not null, recording_accepted boolean not null, notice_hash text not null, consented_at timestamptz not null)

interviews(..., appointment_id text not null unique, version integer not null)
interview_candidates(..., interview_id text not null unique references interviews(id) on delete cascade)
interview_plan_snapshots(..., interview_id text not null unique references interviews(id) on delete cascade)
question_selections(..., interview_id text not null, slot_id text not null, unique(interview_id, slot_id))
interview_turns(...)
interview_question_snapshots(..., turn_id text not null unique references interview_turns(id) on delete cascade)
candidate_answers(..., turn_id text not null unique references interview_turns(id))
answer_transcript_revisions(..., answer_id text not null, revision integer not null, unique(answer_id, revision))
answer_evaluations(..., answer_id text not null, revision integer not null, unique(answer_id, revision))
interview_reports(..., interview_id text not null, revision integer not null, unique(interview_id, revision))
interview_lifecycle_events(..., interview_id text not null, sequence integer not null, unique(interview_id, sequence))

provider_connections(..., credential_ref text, version integer not null)
model_configurations(..., provider_connection_id text not null, model_type text not null, provider_model_id text not null, settings jsonb, default_parameters jsonb, version integer not null)
model_routes(...)
model_invocation_logs(...)
outbox_work_items(...)
audit_events(...)
```

所有包含 `organization_id` 的外键操作还要验证同组织。PostgreSQL 可使用复合外键或 repository 内的同事务检查，并以租户级 Row Level Security 作为第二道防线。

## 联系方式、邀请与匹配存储

- `CandidateProfile.normalized_email` 和 `normalized_phone` 使用应用层规范化后加密存储；另外保存带租户盐的查找哈希以支持精确匹配，不能用明文索引。
- 姓名用于显示和联合校验，不建立跨候选人的模糊匹配索引。
- 高熵 `invitation_token` 只在签发响应中出现一次；数据库保存 SHA-256 token hash、过期时间和消费时间，不保存明文。
- `candidate_session_token` 由 `INTERVIEWER_CANDIDATE_TOKEN_SECRET` 对 `interview_id + created_at` 做 HMAC-SHA256 派生，数据库和后台 API 均不保存/返回明文；生产缺少签名密钥时会话签发失败关闭。
- Candidate Intake 的匹配事务锁定预约记录，验证 token 和时间窗，比较预约绑定候选人的 email/phone 哈希，保存同意版本并把预约推进到 `registered`。
- start 事务通过 `UNIQUE(interviews.appointment_id)` 保证一个预约只创建一个会话，同时把预约推进到 `consumed`；幂等重试返回已有会话。
- 匹配失败日志只保存预约、原因枚举和请求哈希，不保存提交的明文联系方式。

## 简历与媒体对象存储

对象类型和推荐 key：

```text
resumes/{organization_id}/{candidate_profile_id}/{resume_document_id}/{file_hash}
question-speech/{organization_id}/{owner_type}/{owner_id}/{source_version}/{content_hash}.wav
interview-audio/{organization_id}/{interview_id}/{turn_id}/{recording_id}.webm
transcript-artifacts/{organization_id}/{interview_id}/{turn_id}/{revision}.json
```

- bucket 默认私有，静态 URI 不可由浏览器直接访问。
- 企业回听接口在鉴权和审计后签发分钟级 URL；候选人页面只能访问自己当前会话所需资源。
- 数据库保存 `content_hash`、字节数、MIME、加密 key/version、留存到期和删除状态。
- 简历、回答音频和转写按组织策略级联到期；题目语音可按题目版本保留，但被历史面试引用的资产在对应面试留存期内不能删除。
- Provider 返回的临时 TTS URL 必须复制到系统对象存储后才可标记资产 `ready`。
- `POST /api/v1/admin/retention/run` 默认 dry-run；显式执行时先删除私有对象/本地录音，再清空候选人密文与关联简历、审阅、经历题、转写、评分和报告敏感内容，最后写 `retention.purge.completed` 审计。失败不能伪标完成。

简历本地上传与 URL 导入共用同一持久工作流：

1. API 创建 `ResumeDocument(ingestion_status=queued)` 和 `resume.ingest` Outbox 工作项。
2. multipart 请求流或 URL 下载流进入隔离临时文件，边读边限制大小并计算 SHA-256；不得把整个 PDF 放进内存或数据库。
3. URL 下载只允许生产 HTTPS，初始地址和每次重定向都重新解析 DNS 并拒绝环回、RFC1918 私网、链路本地、保留网段、云元数据端点、非 HTTP(S) scheme 和 URL 用户信息；限制重定向、连接/总超时、响应大小和低速连接。
4. 校验响应 MIME、PDF magic bytes 和文件结构，执行恶意文件扫描；失败或感染文件留在隔离区并按策略删除，不能写入正式 key。
5. 通过当前 `PrivateFileStorage` adapter 写入正式 key，创建 `file_objects` 记录，再解析 PDF 并把脱敏文本写成独立私有对象。
6. 只有文件存储、扫描和解析全部成功才把 `ResumeDocument` 标为 `ready`；Resume Review 只能读取 `ready` 版本。

URL 原文可能包含候选人标识或临时签名参数，数据库默认只保存 `source_url_hash` 和审计所需的脱敏 host，不保存 query/fragment。系统不会把外部 URL 当作永久 `file_uri`，也不会让 OSS 直接回源任意 URL。

## 结构化候选池查询

计划装配通过关系表形成槽位候选清单。查询必须在 SQL 中强制租户、岗位和题库边界：

```sql
SELECT q.id, q.version, q.skills, q.difficulty, q.type
FROM questions q
JOIN knowledge_bases kb ON kb.id = q.knowledge_base_id
WHERE q.organization_id = $1
  AND kb.job_position_id = $2
  AND q.knowledge_base_id = ANY($3)
  AND q.status = 'active'
  AND q.validation_status = 'valid'
  AND q.speech_status = 'ready'
  AND q.difficulty = ANY($4)
  AND q.type = ANY($5)
  AND q.skills && $6;
```

计划批准后把结果中的题目 ID/version 清单、筛选条件和集合哈希保存为每个槽位的 `QuestionCandidatePool`。面试中的 Question Selection 只读取该清单，并用会话种子计算稳定随机值；它不再查询全题库，更不执行向量相似度。

可选向量表如果未来启用，应放在独立、可重建的 projection 中，例如 `optional_question_embeddings(question_id, question_version, model, embedding)`。删除该表或 Provider 不可用不能影响题库、计划、预约、面试和报告。Resume Review 默认不建向量；只有超长简历附件需要分段 RAG 时才使用候选人私有、短期 projection。

## 异步工作项与事务

外部 LLM、STT、TTS、文件解析和数字人调用不得占用数据库事务；可选 Embedding 任务遵守同一规则。流程拆成短事务并以 Outbox 记录事实：

1. 首个事务保存领域状态和工作项，例如题目 `validation_status=pending`、语音 `pending`、Resume Review `queued` 或轮次 `transcribing`。
2. worker 按租约领取工作项，在事务外调用 Provider 或处理文件。
3. 后续事务验证租约和目标 version，保存结果、领域事件和下一个工作项。
4. 重复投递通过 `(organization_id, idempotency_key)`、内容哈希和业务唯一约束返回同一结果。

工作项至少包含 type、aggregate ID/version、payload reference、idempotency key、attempt、next attempt、lease token/expiry 和最终错误。消息队列只能唤醒 worker，数据库仍是任务真相来源。

典型链路：

- 题库导入：`parse -> validate structured fields -> persist candidate pool -> speech generate -> build summary`。
- 简历审阅：`scan/parse -> redact -> review -> experience questions -> approved question speech`。
- 回答：`audio persist + turn.transcribing -> streaming final`；失败后 `batch repair -> answer final -> evaluate -> next question/report`。

STT 流本身是长连接，不持有数据库事务。开始时保存 stream attempt 元数据，final 或 error 到达后用短事务提交；所有音频 chunk 只流向媒体/STT adapter，不逐 chunk 写领域表。

## 乐观并发与不可变记录

所有可变聚合使用 compare-and-swap：

```sql
UPDATE interviews
SET data = $1, version = version + 1, updated_at = now()
WHERE organization_id = $2 AND id = $3 AND version = $4;
```

影响 0 行返回 `PERSISTENCE_CONFLICT`，不能无版本 upsert 或自动重放旧状态命令。

- `QuestionSelection` 的唯一约束避免断线恢复时重新随机。
- `InterviewPlan` 批准后保存题库版本、候选题 ID/version 和候选集合哈希；来源题库编辑不改变计划。
- `InterviewQuestionSnapshot` 保存实际题目、rubric 和 `speech_asset_id`；评分不回读源题。
- 转写、评分和报告为 append-only revision；旧 revision 禁止 UPDATE。
- 生命周期事件、会话状态和 `evaluation.requested` / `report.requested` 工作项原子提交。

Memory、SQLite 和 PostgreSQL adapter 必须通过同一套租户隔离、并发、幂等和 revision contract tests。

## 可选向量能力的启用门槛

以下情况至少出现一项，并有测量数据证明 PostgreSQL 全文搜索和结构化标签不足时，才评估 pgvector 或独立向量库：

- 单岗位题量大到后台人工查题明显变慢。
- 需要自然语言查找相似题、批量近似去重或跨语言题库治理。
- Resume Review 需要处理远超单次模型上下文的附件集合。

启用后也只通过可替换的 projection/adapter 服务后台治理或长文档分段检索。Question Selection 的 interface、批准候选清单和答案评分输入保持不变。

## 迁移与验收要求

切换 PostgreSQL/对象存储时必须保持：

- API 资源和状态语义不变，service 不直接依赖数据库或对象存储 SDK。
- 岗位—题库一对多和题库单岗位归属由约束验证。
- 简历、联系方式、token、音频和转写采用各自的加密、留存和审计策略。
- 计划候选池、随机选择事实、题目/候选人快照和 revision 在历史面试中稳定。
- 题库/简历异步流水线、STT 修复、评分和报告在崩溃与重复投递后可恢复。
- 模型供应商配置、每次 attempt、音频下载和人工转写修改可审计。
- 测试至少覆盖岗位题库结构化候选池、题目语音、简历审阅、候选人匹配、一次性预约、随机抽题幂等、服务端 STT final/batch 修复、逐题评分、报告和企业音频复核授权。可选向量 projection 不能成为这些主流程测试的必需 fixture。
