# 数据库与向量存储设计

本文记录当前本地存储实现，以及岗位题库、简历库、预约、题目语音、服务端 STT 和企业复核迁移到 PostgreSQL 的目标边界。向量存储是可选优化，不是正式面试主链路依赖。

## 当前决策

- 本地测试数据库：SQLite。
- 当前代码已有试验性向量存储：SQLite 保存向量 JSON，由 Python 计算余弦相似度。
- 最终长期存储：PostgreSQL。
- MVP/试点不建设必需的向量数据库，也不要求安装 pgvector。
- 简历、题目语音和候选人回答音频：私有对象存储；数据库只保存 URI、哈希和元数据。

题目已由企业归入明确岗位题库，并具备技能、难度、题型、状态、标准答案和 rubric。正式抽题只需 PostgreSQL 结构化过滤、冻结候选清单和稳定随机抽样；答案评分直接使用冻结题目与最终转写调用结构化 LLM，因此二者都不需要向量查询。

如果未来题量巨大、后台需要自然语言查题或自动发现近似重复题，可以选择启用 pgvector 或独立向量 adapter。向量数据必须保持可删除、可重建，不能成为 Question、InterviewPlan 或历史评分的真相来源。

## 环境变量

```bash
INTERVIEWER_DB_BACKEND=sqlite
INTERVIEWER_SQLITE_PATH=data/interviewer.sqlite3
INTERVIEWER_MEDIA_PATH=data/media
```

`INTERVIEWER_DB_BACKEND=memory` 只用于单元测试和临时演示。生产还需要对象存储、密钥管理、数据库 URL 和媒体签名 URL 配置，不能复用本地文件公开路径。

## 当前代码边界

现有本地 repository：

```text
app/repositories/provider.py
app/repositories/sqlite.py
app/repositories/memory.py
```

事务型 Persistence seam：

```text
app/persistence/interface.py
app/persistence/provider.py
app/persistence/memory.py
app/persistence/sqlite.py
```

当前事务工作区已有 `QuestionRepository`、`RoleRequirementRepository`、`InterviewPlanRepository`、`InterviewSessionRepository`、试验性 `VectorDocumentRepository`、模型配置/路由/调用、凭证和 Outbox repository。`VectorDocumentRepository` 不再是下一版业务依赖，可以保留作实验或在迁移中移除。岗位题库归属、候选人简历库、Resume Review、ExperienceQuestion、QuestionSpeechAsset、InterviewAppointment 和 CandidateIntake repository 尚未实现，新增时必须通过同一 Persistence interface，业务 module 不得回到直接读写 Store collection。

SQLite 当前用通用 JSON documents 表保存业务对象。`InterviewSession` 以单一聚合文档保存候选人、计划/题目快照、轮次、回答、评分/报告 revision、中断上下文和生命周期事件。生命周期命令产生的聚合、领域事件和 Outbox 工作项原子提交；这属于带事件日志的状态持久化，不是完整 event sourcing。

本地候选人录音由 `app/adapters/local_media.py` 写入：

```text
data/media/{interview_id}/{turn_id}/{recording_id}.{extension}
```

当前路径只适合开发。简历库和题目语音不能直接塞进现有公开 media 目录；目标实现需要私有媒体 adapter 和资源级授权。

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
resume_documents(..., candidate_profile_id text not null references candidate_profiles(id), file_hash text not null)
resume_reviews(..., resume_document_id text not null references resume_documents(id), job_position_id text not null references job_positions(id), version integer not null)
experience_questions(..., resume_review_id text not null references resume_reviews(id), version integer not null)
role_requirements(..., job_position_id text not null references job_positions(id), version integer not null)

interview_plans(..., job_position_id text not null, candidate_profile_id text not null, resume_review_id text not null, version integer not null)
interview_plan_knowledge_bases(..., plan_id text not null, knowledge_base_id text not null, knowledge_base_version integer not null)
interview_plan_candidate_questions(..., plan_id text not null, slot_id text not null, question_id text not null, question_version integer not null)

interview_appointments(..., candidate_profile_id text not null, plan_id text not null, invitation_token_hash text, version integer not null)
candidate_intakes(..., appointment_id text not null unique, matched_candidate_profile_id text not null)

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

model_provider_configs(...)
model_routes(...)
model_invocation_logs(...)
outbox_work_items(...)
audit_events(...)
```

所有包含 `organization_id` 的外键操作还要验证同组织。PostgreSQL 可使用复合外键或 repository 内的同事务检查，并以租户级 Row Level Security 作为第二道防线。

## 联系方式、邀请与匹配存储

- `CandidateProfile.normalized_email` 和 `normalized_phone` 使用应用层规范化后加密存储；另外保存带租户盐的查找哈希以支持精确匹配，不能用明文索引。
- 姓名用于显示和联合校验，不建立跨候选人的模糊匹配索引。
- `invitation_token` 只在签发响应中出现一次；数据库保存 `HMAC(server_secret, token)`、过期时间、撤销时间和消费时间。
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
