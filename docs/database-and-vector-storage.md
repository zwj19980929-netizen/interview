# 数据库与向量存储设计

## 未作答响应的数据影响（023）

无DDL、表或旧数据迁移。answer_declined保存在现有TurnUnderstanding；非空权威原文、录音、utterance_id和understanding_id仍由CandidateAnswer引用。正常答案事务及Outbox产生评分revision，model_info记录declined_answer.v1及对应理解引用；混合证据保留全部审计ID而不把未作答发言当技术证据。历史会话和评分不回写、不补造提交。

## 本次识别修复的数据影响（019）

无 DDL、持久表、向量策略、候选答案或旧评分迁移。补送重放使用同一已录 PCM，保存顺序与字节数不变；新到音频仍只录一次。来源诊断写入服务日志且仅含文本散列、长度、时间和会话/流身份，不增加原始敏感转写日志。iv_01b96d6b40314e2f 的旧录音/事故状态保留，不用诊断或合成测试补写答案。

## 018：持有final时继续保留PCM

无DDL及历史数据修订。确认回复的server final、声学活动水位及待定起音仅属于当前owner进程；已确认识别主动暂停期间仍按序接收PCM，并沿用原私有分段持久化与唯一录音。准备窗口临时PCM最多60秒，异常恢复仍30秒；超限继续失败关闭而非丢帧。新有声后缀或缺失ASR final仍阻止封存complete；仅受确认规则判作非语音的后缀可随已有final封存，波形本身完整保留。

## 016：确认投影与请求诊断

沿用JSON文档，无新表/索引/DDL。agent_runtime.supplement_confirmation仅持久安全状态和turn/capture绑定，原文边界保留在当前owner进程；新采集清除旧确认，不迁移或自动恢复历史paused会话。补充询问不complete录音、不产生答案/评分；整个回答及口头控制交谈保存在同一私有录音/累计权威final。ModelInvocationLog增加可空http_status、固定rejection_category和白名单实时prompt_version；不保存HTTP错误正文。历史空字段保持兼容。

## 稳定预览不等于完整证据（015，仓库verified）

StableTranscriptPreview/PreparedTurnDecision仅进程内保存，预计算前沿用当前capture的 `seal(complete=false)` 封存录音前缀，不创建权威Utterance、CandidateAnswer或评分任务。PCM仍只记录一次，音频接受/本地发送水位不当作厂商处理ACK；最终final/输入fence/上下文及完整证据事务保持原约束。无DDL、数据迁移或历史证据修改。

## 采集恢复与同题重答（CONTINUOUS-CAPTURE-RECOVERY-014）

沿用InterviewSession.agent_runtime的JSON保存capture_recovery（status/turn_id/capture_id/attempt/max_attempts、内部安全cause_code/stage/capture_revision）；候选投影仅前五字段。识别恢复不推进媒体capture_revision，不重复写PCM；耗尽seal complete=false，保留现有私有片段，不创建答案/评分。

`prepare_candidate_retry` 在同一租户短事务内验证owner且ownership_id属于目标interview、in_progress/当前未回答题/无active接管，以及持久retry_required与capture_id/媒体revision。旧prefix元数据进入abandoned_captures，原segments仍保留；advance与session.retry_capture_revision原子写入。失败open再试复用已准备的新revision，不重复归档；新代已有完整证据/答案不得重置。同题重答不混合旧片段。Memory和SQLite包含并发/CAS回滚验收，无DDL，不删除或恢复历史会话。

## 路由探针所有权（ROUTE-READINESS-REFRESH-013）

复用 ModelRoute 的 versioned JSON 文档保存健康证据、配置指纹和短期探针租约，不新增表/DDL。领取与完成各使用短事务/CAS，外部模型调用期间不持有事务。不同实例只在租约可取得时发探针，晚回包必须验证租约标识和当前配置。租约到期允许安全接管，失败冷却限制请求风暴；不把进程内锁当作跨实例保证。

配置指纹不包含明文凭据；模型/连接配置 revision 与路由目标/策略用于绑定。健康探测不增加 configuration_revision。管理员列表只返回安全 readiness 投影，不返回租约秘密或原始 Provider 异常；历史候选人、预约状态和评分数据不由本刷新修改。

## INTERRUPTIBLE-AUTOMATIC-TURNS-012 持久化约束

流式表达复用私有 FileObject：只归档唯一有效 final 的完整 PCM/WAV，source_type=approved_streaming_tts，
不保存 PCM 到 AgentEvent/Redis、不保存 Provider URL。`agent_runtime.active_output_id/active_expression_act_event_id`
绑定当前输出和批准事实，清除 active 时同步清除；控制断线把批准事件引用置入 `expression_replay_act_event_id`，
新控制连接校验当前题/最后批准/status 后以新输出重播或丢弃标记。所有变更沿用短 JSON 聚合事务/CAS，无 DDL。
客户端只看到 opaque active_performance_id，不获得内部重播引用、owner、凭据或存储 key。

正式语音句段 final/理解准备只能 seal `complete=false` 的私有 Evidence 前缀，收音期间仍使用同一 stream/capture_revision；按字节水位拼接分段时间，轮换缓冲/缺 final 重放不能重复写录音。仅通过当前 final、决策上下文与输入 fence 后才 seal complete 并提交唯一答案。journal 增加内部 prepared_turn 的 proposal_id/capture_id 白名单，禁止持久化模型输出或音频；该进程提案丢失不能按引用猜造答案。无 DDL，沿用原 JSON 聚合、事务/CAS、owner/media fence 与留存规则。011 的强制 explicit 才 seal 语义由此替代。

## TURN-COMPLETION-UNDERSTANDING-011 持久化约束

正式静音不再 enqueue 自动 seal；候选人显式 finish 仍通过 EvidenceCommandJournal、drain、封存 checkpoint 与当前 fence。正在开放新采集时拒绝旧 capture 的显式完成；owner 丢失后无开放流的未知 receipt 仍允许按既有完整 checkpoint 恢复，不能因旧 process-local capture ID 不在内存而静默丢弃修复。处理阶段持久 Floor=none、reason=answer_processing，客户端不得据旧 candidate 快照提前重开。

TurnUnderstanding 新写入 prompt_version=v2，历史 v1 可读；内部 UnderstandingProblem 的 reason_code/attempts 随原 JSON 领域记录持久化，无新表/DDL。E/P 编号只属于单次冻结模型请求，还原的原文证据保持 canonical 存储，不保存非法模型输出。模型返回 JSON schema 成功并不证明领域内容已合格；内容失败另记录固定类别日志，不能包含转写/Prompt/凭据。原事故的录音、utterance、答案和 paused 状态不由本修复修改。

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
INTERVIEWER_MEDIA_RECORDING_BACKEND=local
INTERVIEWER_FILE_STORAGE_BACKEND=local
INTERVIEWER_PRIVATE_FILE_ROOT=data/private-files
INTERVIEWER_FILE_QUARANTINE_ROOT=data/file-quarantine
INTERVIEWER_FILE_MAX_BYTES=10485760
INTERVIEWER_FILE_SIGNING_SECRET=replace-with-32-plus-random-characters
INTERVIEWER_FILE_SCANNER_CLAMD_HOST=clamd.internal
INTERVIEWER_FILE_SCANNER_CLAMD_PORT=3310
INTERVIEWER_CELERY_BROKER_URL=redis://redis.internal:6379/1
INTERVIEWER_CELERY_QUEUE=interviewer
```

`INTERVIEWER_DB_BACKEND=memory` 只用于单元测试和临时演示。`INTERVIEWER_MEDIA_RECORDING_BACKEND=local` 与 `INTERVIEWER_FILE_STORAGE_BACKEND=local` 只用于本地开发，目录不能挂载为公开静态资源。生产录音必须设置 `INTERVIEWER_MEDIA_RECORDING_BACKEND=private`，并通过同一个 PrivateFileStorage seam 写入对象存储。阿里云 OSS adapter 使用以下部署配置，密钥值必须来自密钥管理器或部署 Secret，不能进入普通配置文档或日志：

```bash
INTERVIEWER_FILE_STORAGE_BACKEND=aliyun_oss
INTERVIEWER_MEDIA_RECORDING_BACKEND=private
INTERVIEWER_OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
INTERVIEWER_OSS_BUCKET=interviewer-private
INTERVIEWER_OSS_ACCESS_KEY_ID=from-deployment-secret
INTERVIEWER_OSS_ACCESS_KEY_SECRET=from-deployment-secret
INTERVIEWER_OSS_SSE=AES256
```

自托管 LiveKit 权威收音还需要以下独立部署配置。`API_SECRET` 与 OSS 密钥只能来自部署 Secret；正式模式只接受数据库租约/fence/journal/checkpoint 驱动的 `database_fenced`：

```bash
INTERVIEWER_LIVEKIT_URL=wss://livekit.internal
INTERVIEWER_LIVEKIT_API_KEY=from-deployment-secret
INTERVIEWER_LIVEKIT_API_SECRET=from-deployment-secret
INTERVIEWER_LIVEKIT_EGRESS_URL=https://livekit-egress.internal
INTERVIEWER_LIVEKIT_INGRESS_ENABLED=true
INTERVIEWER_LIVEKIT_INGRESS_MODE=database_fenced
INTERVIEWER_LIVEKIT_INGRESS_GRACE_SECONDS=30
INTERVIEWER_LIVEKIT_INGRESS_LEASE_SECONDS=15
INTERVIEWER_LIVEKIT_INGRESS_RENEW_SECONDS=5
INTERVIEWER_DEPLOYMENT_ID=prod-us-west-2
INTERVIEWER_RELEASE_REVISION=git-deadbeef
INTERVIEWER_REALTIME_AGENT_ENABLED_ORGANIZATIONS=org_a,org_b
INTERVIEWER_AGENT_ACCEPTANCE_REPORT=/run/secrets/realtime-agent-acceptance.json
INTERVIEWER_AGENT_ACCEPTANCE_REPORT_SECRET=from-deployment-secret
```

灰度名单必须逐个列出 organization ID，生产不接受 `*`。acceptance v2 报告由离线 runner 原子写入，必须精确绑定本次 deployment/revision；邀请、start、候选人票据签发与票据消费会重复验证，不能仅靠一次 `/readyz` 结果长期放行。

`EvidenceOwnershipRepository` 使用独立 `evidence_ownerships` document collection，不把高频续租写入 InterviewSession JSON，因此不与 Floor、AgentEvent、评分或报告的聚合 version 竞争。Memory/SQLite Adapter 保持同一事务合同；PostgreSQL 使用 `SELECT ... FOR UPDATE` 锁定所有权行、`clock_timestamp()` 作为唯一 lease 时钟，首次并发 INSERT 唯一冲突后重读胜者。候选人答案事务以固定锁顺序先读所有权行、再读 InterviewSession，并同时验证 owner instance、lease ID、epoch 和未到期。

`EvidenceCommandJournal` 使用独立 `evidence_commands` collection。命令 ID 是 organization/interview/idempotency key 的 SHA-256 派生值，原始 key 不落库；request fingerprint 防止同 key 更换命令。记录包含 control generation、target/claimed owner fence、deadline、available/claim expiry、attempt、状态和严格 allow-list outcome。claim 事务先锁当前 ownership，再锁选中的 command；claim 超时可 at-least-once 重领，complete/fail 必须重新验证当前 fence 与 claim ID。Memory 与 SQLite 已运行同一 journal round-trip 合同；PostgreSQL 继续使用通用 documents/RLS/`FOR UPDATE` 语义。

当前这些 collection 仍使用通用 PostgreSQL `documents` 物理表及其 tenant RLS，但是独立行/独立版本；目标压测若显示 lease renew 或 command scan 成为热点，再迁入专用物理表/索引而不改上层 Interface。Redis 仍不是 lease、命令或结果真相。

PostgreSQL schema 必须先由 `INTERVIEWER_POSTGRES_MIGRATION_DSN` 对应的 migration owner 执行 `python -m app.migrations.postgresql`；Web/worker 使用 `INTERVIEWER_DB_BACKEND=postgresql` 与最小权限 `INTERVIEWER_POSTGRES_DSN`，启动时只读校验 schema，不自动执行 DDL。Redis 跨实例事件使用 `INTERVIEWER_REDIS_URL`，Celery broker 使用独立的 `INTERVIEWER_CELERY_BROKER_URL`/逻辑 DB 或命名空间，不能和领域状态混为一体。生产还必须提供联系人/Provider 凭证加密密钥、媒体签名密钥，以及 `INTERVIEWER_FILE_SCANNER_COMMAND` 或内部 `INTERVIEWER_FILE_SCANNER_CLAMD_HOST/PORT`；clamd TCP 无认证/加密，只能部署在受信网络。不能复用开发默认值、公开本地目录或把 OSS bucket 设为公开读。

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

当前事务工作区已有 `JobPositionRepository`、`KnowledgeBaseRepository`、`QuestionRepository`、`QuestionSpeechAssetRepository`、`CandidateProfileRepository`、`ResumeDocumentRepository`、`FileObjectRepository`、`AuditEventRepository`、`ResumeReviewRepository`、`ExperienceQuestionRepository`、`RoleRequirementRepository`、`InterviewPlanRepository`、`InterviewAppointmentRepository`、`CandidateIntakeRepository`、`InterviewSessionRepository`、`EvidenceOwnershipRepository`、`EvidenceCommandRepository`、`ModelCircuitStateRepository`、模型配置/路由/调用、加密凭证和 Outbox repository。它们通过同一个 versioned document interface 暴露，Memory、SQLite 与 PostgreSQL adapter 共用事务语义；Question Catalog 的租户、岗位、题库、状态/readiness、技能、难度和题型条件由 backend contract 执行。旧 `VectorDocumentRepository` 与 Memory/SQLite 向量集合已删除，关系型题库查询不持久化向量 projection。

联系人在写入前使用 Fernet 加密并以租户绑定 HMAC 查找，API 只返回掩码；Provider 凭证也在 repository seam 密封。`ResumeDocument` 只接受 PDF，引用原件与解析文本两个私有 FileObject；SQLite JSON 不再保存新简历正文。开发模式可用受控本地媒体 adapter；生产回答音频写入 `candidate_answer_audio` FileObject，绑定组织、面试与轮次，并通过短期签名 token 回读和记录授权/下载审计。SQLite 仍只用于开发/测试；生产租户边界由 PostgreSQL `organization_id + FORCE RLS` 提供第二道防线。

PostgreSQL 首版迁移采用受约束 JSONB documents，以保持现有聚合事务语义，并增加 `(organization_id, idempotency_key)` Outbox 唯一约束、预约单会话唯一索引、选择槽位唯一检查、Provider 凭证复合主键和全部表的强制 RLS。显式迁移入口使用事务级 advisory lock；本机隔离 PostgreSQL 16 已验证非 owner runtime role 的跨租户读写、事务回滚、CAS、Outbox/预约约束和题库索引 `EXPLAIN`。它是可运行 adapter，不等同于下文完全规范化关系表的最终形态；目标生产集群仍必须重复迁移、并发和查询计划验收。

SQLite 当前用通用 JSON documents 表保存业务对象。`InterviewSession` 以单一聚合文档保存候选人、计划/题目快照、轮次、回答、评分/报告 revision、中断上下文和生命周期事件。生命周期命令产生的聚合、领域事件和 Outbox 工作项原子提交；这属于带事件日志的状态持久化，不是完整 event sourcing。

SQLite 事务中的 get/list/CAS 始终直接读取当前连接，数据库是提交与并发真相。提交成功后 adapter 只把本事务实际更新/删除的 document、Outbox、Provider secret 和 model invocation 深拷贝到同进程兼容缓存；回滚不应用任何 delta。启动或显式新建 `SQLiteStore` 才执行全库装载。这样一条 VAD/Evidence journal 命令不再反序列化无关的万级历史 audit/documents；Memory 与 PostgreSQL 的事务合同、数据库版本检查和 reopen 持久性不变。SQLite 仍不承担生产多实例一致性，生产使用 PostgreSQL/RLS。

候选人个人题库不新增 repository 或复制题目文档：`CandidateQuestionBank` 由 `ExperienceQuestionRepository` 按 `candidate_profile_id` 投影。人工题仍必须引用同一候选人的 `ResumeReview`，从该审阅继承岗位范围；`source_type` 区分 `ai_generated/manual`，逻辑删除写入 `status=archived` 和操作者/时间。新计划忽略 archived，历史计划与面试只读其已冻结快照，因此本次字段扩展不需要新增物理集合或破坏性迁移。

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
- `LocalPrivateFileAdapter`：只在 `development + INTERVIEWER_LOCAL_MEDIA=true` 时接受 LiveKit Egress 录像，写入 `data/private-files/interview-captures/` 并验证路径边界、对象存在性、hash 和字节数；其保护结论是 `local_private_development`，`encryption` 保持空值，不能被生产环境当作 OSS 加密证明。

`store` 返回 `file_object_id/object_key/content_hash/size_bytes/content_type/backend` 等受控元数据。`ResumeDocument` 只引用 `file_object_id`，因此从本地迁移到 OSS 时可以复制物理对象并更新文件对象定位，不改简历、审阅和面试历史的业务 ID。

题目 TTS 与动态 Agent 表达在调用 `store` 之前共用 `PrivateAssetImporter`。该 seam 对 WAV 做 RIFF chunk、fmt/data 唯一性、边界、padding 与 block alignment 校验；只处理已识别的 signed-limit RIFF/data 流式占位组合，以及 child chunk 完整到 EOF 后恰好漏计四字节 WAVE form type 的 outer size，并以规范化字节重新计算 checksum。其他长度偏差和畸形容器失败关闭；其他音频格式保持既有受控导入，声明为 WAV 却不具备 WAVE 魔数、或以其他 MIME 伪装 WAV 的响应拒绝写入。FileObject 一经被 QuestionSpeechAsset、AgentExpressionAudio 或历史会话引用仍不可原地修改，修复通过新对象和既有版本/预约冻结流程完成。

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
  speech_profile jsonb,
  speech_profile_revision integer not null default 0,
  speech_build_status text not null default 'configuration_required',
  version integer not null
)

job_position_knowledge_bases(
  organization_id text not null,
  job_position_id text not null references job_positions(id),
  knowledge_base_id text not null references knowledge_bases(id),
  created_at timestamptz not null,
  primary key (organization_id, job_position_id, knowledge_base_id)
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
  speech_profile_revision integer,
  speech_profile_fingerprint text,
  knowledge_base_speech_profile_revisions jsonb not null default '{}',
  model_configuration_id text,
  model_configuration_version integer,
  language text not null,
  voice_profile_id text not null,
  audio_format text not null,
  speaking_rate numeric not null,
  content_hash text not null,
  audio_uri text not null,
  status text not null,
  unique (organization_id, owner_type, owner_id, source_version, speech_profile_fingerprint, model_configuration_id, model_configuration_version, language, voice_profile_id, audio_format, speaking_rate, content_hash)
)

candidate_profiles(..., organization_id text not null, status text not null, retention_reason text, retention_expires_at timestamptz, version integer not null)
file_objects(..., organization_id text not null, category text not null, backend text not null, object_key text not null, content_hash text not null, size_bytes bigint not null, unique(organization_id, backend, object_key))
resume_documents(..., candidate_profile_id text not null references candidate_profiles(id), resume_version integer not null, file_name text not null, file_object_id text references file_objects(id), parsed_text_file_object_id text references file_objects(id), file_hash text, status text not null, deleted_at timestamptz, version integer not null)
resume_reviews(..., resume_document_id text not null references resume_documents(id), job_position_id text not null references job_positions(id), status text, processing_stage text, processing_strategy text, processing_progress jsonb, evidence_chunks jsonb, screening_recommendation text, screening_score integer, screening_policy_version text, matched_requirements jsonb, unmet_requirements jsonb, human_decision text, human_review_status text, human_review_note text, reviewed_by text, reviewed_at timestamptz, version integer not null)
experience_questions(..., resume_review_id text not null references resume_reviews(id), candidate_profile_id text not null references candidate_profiles(id), job_position_id text not null references job_positions(id), source_type text not null, status text not null, speech_status text not null, archived_at timestamptz, version integer not null)
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

`speech_dialogue_mode`、`followup_policy` 和追问 parent/root/depth/weight 进入现有预约/会话/轮次 document/JSONB 表达，不新增第二套“实时对话表”。追问仍是 `interview_turns`，其 CandidateAnswer 与 AnswerEvaluation 使用同一唯一约束。答案提交与 `answer.evaluate:{answer_id}:{revision}` Outbox 幂等键原子提交；请求线程不执行评分。S2S 原始音频 delta 只在逐字批准前进入有界内存缓冲；批准后输出与动态 TTS 一样复制为 `purpose=agent_expression_audio` 的私有 FileObject，会话只持久化不泄密的 `agent-expression://file_id`。它不构成评分证据；原始候选人录音、权威 STT final 和 Provider invocation 脱敏元数据继续按既有表保存。

当前 JSON document adapter 把同一关系存为 `JobPosition.knowledge_base_ids`，并把历史 `KnowledgeBase.job_position_id` 投影为初始关联。规范化 PostgreSQL 使用 `job_position_knowledge_bases`；两种存储都只引用题库，不复制 Question、KnowledgeBaseSpeechProfile 或 QuestionSpeechAsset。

PositionCandidateMembership 当前由 `CandidateProfile.job_position_id` 表达；旧 document 没有该字段时，可从同组织的 ResumeReview、InterviewPlan、InterviewAppointment 和 InterviewSession 岗位引用推导删除影响，但新工作台录入必须显式写入。岗位删除不对历史聚合做数据库物理级联：候选人复用 RetentionService 删除私有 FileObject/媒体并清空敏感投影，岗位、要求和计划归档，预约取消；共享题库引用只从已归档岗位清空，题库实体及语音资产保留。

兼容既有数据时不批量猜测或回填 TTS 模型：缺少 `speech_profile` 的旧题库在读取投影中视为
`configuration_required`，历史语音资产不再计入当前就绪数。管理员首次显式保存题库语音配置时，
系统在同一持久化事务中写入 profile revision 和整库构建清单，完成按需迁移，避免部署迁移阶段触发外部 TTS 成本。
新建题库则可从创建时已存在的精确 `question_speech_generation` route 解析并保存
`source=model_route_default/model_route_id`；这是新聚合初始化，不对存量题库做批量回填。Question 的
`speech_preview` 仅由 QuestionSpeechAsset/FileObject 读取时投影，不新增持久列；`production_ready=false` 或缺少 ready
FileObject 的资产永远不能签发试听地址。

所有包含 `organization_id` 的外键操作还要验证同组织。PostgreSQL 可使用复合外键或 repository 内的同事务检查，并以租户级 Row Level Security 作为第二道防线。

## 联系方式、邀请与匹配存储

- `CandidateProfile.normalized_email` 和 `normalized_phone` 使用应用层规范化后加密存储；另外保存带租户盐的查找哈希以支持精确匹配，不能用明文索引。
- 姓名用于显示和联合校验，不建立跨候选人的模糊匹配索引。
- 高熵 `invitation_token` 只在签发响应中出现一次；数据库保存 SHA-256 token hash、过期时间和消费时间，不保存明文。
- `candidate_session_token` 由 `INTERVIEWER_CANDIDATE_TOKEN_SECRET` 对 `interview_id + created_at` 做 HMAC-SHA256 派生，数据库和后台 API 均不保存/返回明文；生产缺少签名密钥时会话签发失败关闭。
- Candidate Intake 的匹配事务锁定预约记录，验证 token 和时间窗，比较预约绑定候选人的 email/phone 哈希，保存同意版本并把预约推进到 `registered`。同一事务按 `appointment_id + experience_question_id/version + speech_profile_fingerprint` 幂等创建简历题语音 DurableWorkItem，并把进度保存到 `InterviewAppointment.speech_preparation`；不更新 ExperienceQuestion 的全局资产指针。
- 同一登记事务以 `appointment.reminder.email:{appointment_id}` 幂等键创建 DurableWorkItem，`available_at` 为开始前 30 分钟；payload 只含 `appointment_id`。Worker 执行时才从 CandidateProfile 解密邮箱，SMTP 凭据不进入数据库、Outbox、审计或日志。未配置 SMTP 时工作保持 retryable，面试已开始、预约取消/消费或邮箱已清理时显式完成为 skipped。
- 预约级 `QuestionSpeechAsset` 继续进入同一 repository，以 `owner_type=experience_question + owner_id + source_version` 标识内容来源，并额外保存 ModelConfiguration ID/version、voice、language、audio format、speaking rate 和 profile fingerprint。只有这些字段全部匹配才可跨预约复用；取消预约只取消未完成工作，不删除可能被其他预约复用的不可变资产。
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
- 最新岗位初筛的生效结论全部为 `unqualified` 时，CandidateProfile 写入 `retention_reason=screening_unqualified` 和 7 天后的 `retention_expires_at`；人工复核或新审阅使任一岗位符合/待复核时在同一事务取消该期限。
- Provider 返回的临时 TTS URL 必须复制到系统对象存储后才可标记资产 `ready`。
- `POST /api/v1/admin/retention/run` 默认 dry-run；显式执行时先删除私有对象/本地录音（含 LiveKit Egress object、CandidateAnswer 音频和全部 Evidence capture revision），再清空候选人密文与关联简历、审阅、经历题、转写、对话理解/动作、评分和报告敏感内容，最后写批次审计与一次 `retention.candidate_evidence_purged` 候选人审计。失败不能伪标完成，对象键只以 SHA-256 进入审计。
- `app.workers.retention.run_screening_retention` 由 Celery Beat 周期唤醒，只处理已到期的 `screening_unqualified` 候选人并复用相同删除顺序；同一周期还运行 Evidence media GC，先删除 `capture_revision < current` 的私有对象，再硬删 segment、tombstone FileObject 并写 `retention.evidence_media_gc.completed`。重复运行是幂等的，符合或待复核候选人的当前 revision 不会进入 GC。
- 单份简历显式删除采用两阶段清理：同一事务做 version 校验、历史引用保护、取消尚未运行的摄取/审阅 Outbox 并写 `deleting`；事务外删除隔离文件和 Private File Storage 对象；随后把 FileObject/ResumeReview/派生经历题标为删除或归档、清空敏感证据并把 ResumeDocument 写为 `deleted`。已进入 InterviewPlan 或 InterviewSession 快照的版本拒绝删除，运行中工作不做强制终止。

简历本地上传与 URL 导入共用同一持久工作流：

1. API 创建 `ResumeDocument(ingestion_status=queued)` 和 `resume.ingest` Outbox 工作项。
2. multipart 请求流或 URL 下载流进入隔离临时文件，边读边限制大小并计算 SHA-256；不得把整个 PDF 放进内存或数据库。
3. URL 下载只允许生产 HTTPS，初始地址和每次重定向都重新解析 DNS 并拒绝环回、RFC1918 私网、链路本地、保留网段、云元数据端点、非 HTTP(S) scheme 和 URL 用户信息；限制重定向、连接/总超时、响应大小和低速连接。
4. 校验响应 MIME、PDF magic bytes 和文件结构，执行恶意文件扫描；失败或感染文件留在隔离区并按策略删除，不能写入正式 key。
5. 通过当前 `PrivateFileStorage` adapter 写入正式 key，创建 `file_objects` 记录，再逐页解析 PDF，以 form-feed 保存页边界并把文本写成独立私有对象。
6. 只有文件存储、扫描和解析全部成功才把 `ResumeDocument` 标为 `ready`；若上传命令携带岗位/要求，完成事务同时幂等创建 `ResumeReview + resume.review` 工作项，避免“简历 ready 但审阅未排队”的崩溃窗口。

URL 原文可能包含候选人标识或临时签名参数，数据库默认只保存 `source_url_hash` 和审计所需的脱敏 host，不保存 query/fragment。系统不会把外部 URL 当作永久 `file_uri`，也不会让 OSS 直接回源任意 URL。

## 结构化候选池查询

计划装配通过关系表形成槽位候选清单。查询必须在 SQL 中强制租户、岗位和题库边界：

```sql
SELECT q.id, q.version, q.skills, q.difficulty, q.type
FROM questions q
JOIN knowledge_bases kb ON kb.id = q.knowledge_base_id
JOIN job_position_knowledge_bases pkb
  ON pkb.organization_id = q.organization_id
 AND pkb.knowledge_base_id = kb.id
WHERE q.organization_id = $1
  AND pkb.job_position_id = $2
  AND q.knowledge_base_id = ANY($3)
  AND q.status = 'active'
  AND q.validation_status = 'valid'
  AND q.speech_status = 'ready'
  AND q.difficulty = ANY($4)
  AND q.type = ANY($5)
  AND q.skills && $6;
```

计划批准后把结果中的题目 ID/version 清单、筛选条件和集合哈希保存为每个槽位的 `QuestionCandidatePool`。面试中的 Question Selection 只读取该清单，并用会话种子计算稳定随机值；它不再查询全题库，更不执行向量相似度。

可选向量表如果未来启用，应放在独立、可重建的 projection 中，例如 `optional_question_embeddings(question_id, question_version, model, embedding)`。删除该表或 Provider 不可用不能影响题库、计划、预约、面试和报告。Resume Review 当前使用页感知 Map/Reduce，不为单份长简历建立向量 projection。

## 异步工作项与事务

外部 LLM、STT、TTS、文件解析和数字人调用不得占用数据库事务；可选 Embedding 任务遵守同一规则。流程拆成短事务并以 Outbox 记录事实，Celery 负责调度和唤醒：

- Resume Review 的业务重试在同一短事务内把 `resume_reviews.failed -> queued` 与对应 DurableWorkItem `failed/dead_letter -> pending` 一并提交，attempt 归零、`replay_count` 递增并写审计，避免界面显示处理中但 Worker 没有任务，或任务已重放但候选人仍显示失败；审阅同时保存最近 retry `Idempotency-Key/work_item_id`，重复传输在 version 校验前返回同一工作。
- Resume Review 工作的 Outbox 幂等键使用 `resume.review:{resume_document_id}:{input_hash}`：同一简历版本的重复排队仍复用一个工作项，不同简历版本即使内容和岗位配置完全相同也不会命中旧版本工作。排队入口发现 queued 审阅没有任何关联工作时必须补建，并拒绝接受 aggregate ID 指向其他审阅的幂等命中。
- Resume Review 领取工作时使用 330 秒租约，覆盖 Celery 默认 300 秒 hard time limit；不能沿用通用 60 秒租约，否则合法的长模型调用会被 Beat 当成 Worker 丢失并并发补发。任务硬超时后最多等待剩余租约窗口再恢复，避免双执行。
- 题库语音失败重试不 replay 原 `question.speech:{question}:{source_version}:{revision}` 工作。失败落库已推进 Question version，重试父工作必须冻结当前仍为 failed 的 Question ID/version，并以 `question.speech.retry:{question}:{current_version}:{revision}:{retry_parent_id}` 创建新子工作；同一人工命令仍由父工作的 `Idempotency-Key` 去重。

1. 首个事务保存领域状态和工作项，例如题目 `validation_status=pending`、语音 `pending`、Resume Review `queued` 或轮次 `transcribing`。
2. 事务提交后向 Celery 发布只含 `organization_id + work_item_id` 的小消息；发布失败不回滚已提交领域事实，由 Celery Beat dispatcher 扫描待执行/租约过期工作项补发。
3. `app/workers/` 中的 Celery task 按租约领取工作项，在事务外调用 Provider 或处理文件。
4. 后续事务验证租约和目标 version，保存结果、领域事件和下一个工作项。
5. 重复投递通过 `(organization_id, idempotency_key)`、内容哈希和业务唯一约束返回同一结果；幂等键必须包含正确的聚合身份，不能只用可跨聚合重复的内容哈希。

ProviderConnection/ModelConfiguration 文档的通用 `version` 覆盖配置和健康探针两类写入；`configuration_revision` 只覆盖管理员配置语义。旧文档缺少该字段时读取按 revision 1 兼容，首次配置 PATCH 写为 2。该字段让所有模型类型共用同一并发恢复策略，不需要按 Provider、能力或探针协议增加分支。

`DurableWorkItem.status=failed` 明确表示“本次 attempt 失败、仍等待自动重试”，不是领域终态；任何模型供应商和能力都不得据此把聚合写成 failed 或发出 `*.failed` 领域事件。只有 `error_retryable=false` 或耗尽 `max_attempts` 形成的 `dead_letter` 才能推进领域终态。该合同覆盖 LLM、Embedding、STT、TTS、实时语音和数字人；具体流程可以展示 retrying/building，但不能通过这类展示写入破坏 source-version guard 的聚合版本。

`DurableWorkItem.error_retryable=false` 是终止语义：即使 `attempt_count < max_attempts` 也立即进入 `dead_letter`，并从
自动 claimable 集合排除；人工 replay 仍可显式把它恢复为 pending。智能生题的多槽位输出截断由领域服务完成自适应
拆分，因此原工作以 `split_into_single_slot_chunks` 完成，原 chunk 进入 superseded，两个替代工作与批次更新在同一
事务提交；单槽位截断才使用上述非重试 dead-letter 语义。

工作项至少包含 type、aggregate ID/version、payload reference、idempotency key、attempt、next attempt、lease token/expiry、父/子关系、进度计数和最终错误。Celery broker/result backend 只能唤醒 worker，数据库仍是任务真相来源；管理员查询、重放和业务 readiness 不读取 Celery result。

Celery 部署合同：

- `app/workers/celery_app.py`：唯一 Celery app、序列化白名单、queue、`acks_late=true`、`task_reject_on_worker_lost=true`、soft/hard time limit。
- `app/workers/dispatcher.py`：将 DurableWorkItem 映射到 task，并由 Celery Beat 周期补发 due/expired lease；不包含业务处理。
- `app/workers/knowledge_base_speech.py`：整库 manifest、题目 fan-out、TTS 调用、私有资产落盘、revision 防旧写和进度聚合。
- 其它异步执行入口继续按领域放在 `app/workers/`；API/service 只验证命令、提交聚合与 DurableWorkItem，不直接执行长任务。
- Celery 自带 retry 只用于发布/进程级瞬时故障；业务退避、最大次数、dead-letter 和人工 replay 继续由 DurableWorkItem 控制，避免两套重试计数。

典型链路：

- 题库导入：`parse -> validate structured fields -> persist candidate pool -> speech generate -> build summary`。
- 题库语音切换：`speech profile CAS -> parent rebuild -> freeze question manifest -> child speech fan-out -> current revision guard -> progress aggregate`。
- 题库语音运行中切换使用 profile revision 语义 CAS；新 profile、旧 revision 未完成 Outbox 的 cancel/cancel_requested、全部 Question 新 source version 与新 parent build 在同一数据库事务提交。running 任务在 Provider 返回后和资产落库前都要重读 revision/cancel guard，不能以旧配置覆盖新资产。
- 简历审阅与问答：`scan/page-preserving parse -> atomic review enqueue -> redact/budget -> single-pass 或 evidence Map/compact/final Reduce -> effective qualified gate -> resume.experience_questions.generate -> interviewer approval -> plan freeze -> candidate intake -> appointment-scoped speech`；非符合结论在 gate 停止，问题持久化不可变证据快照，未确认预约不产生 TTS 成本。
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

`question_generation_batches` 是通用 versioned document collection；PostgreSQL 复用带 organization_id/RLS 的
`documents` 表，SQLite/Memory 使用同名 collection，因此无需新增专用表。批次保存上下文快照、草稿、状态、
生成/导入工作项 ID 和导入后的 Question ID；草稿更新、删除与确认导入使用批次 version CAS。正式 Question
保存 `generation_batch_id/generation_draft_id`，但历史 Question 不要求这两个可空字段。生成任务和导入任务分别
使用唯一 idempotency key，Celery/Redis 不持有草稿或导入结果。

任务工作台扩展字段仍保存在同一 versioned document：`execution_revision`、停止事实和受限 `control_history`。
规划、分片与合并 DurableWorkItem payload 冻结 execution revision；停止时 pending/failed/dead-letter 工作转为
cancelled，running 工作仅记录 cancel request 并保留 lease，待 Worker 通过 revision guard 以 superseded 收尾。
恢复或人工重试创建新的幂等工作项，不删除旧工作记录，也不依赖 Celery result backend。
双槽位截断恢复同样保留原工作与 chunk：原工作完成态记录拆分结果，原 chunk 保存 replacement IDs 和脱敏
finish/token 诊断；活动进度、后续 merge 和停止/恢复只处理替代 chunk，避免旧失败同时阻塞合并或重复计数。
单题导入继续复用 `knowledge_base.import` DurableWorkItem；payload 额外冻结 `generation_import_scope=single` 与
`generation_draft_id`。批次草稿保存独立 `version` 以及 `import_status/import_work_item_id/imported_question_id/import_error`，
正式 Question 仍以 `generation_batch_id + generation_draft_id` 去重。单题成功不会冻结整批，批量导入只携带
剩余未导入草稿。单题命令在事务内读取最新批次 version，仅对目标草稿 version 做条件校验；因此连续导入
不同草稿不会被无关聚合版本阻塞，而同一草稿被编辑后的过期确认仍会冲突。重试和单题/批量组合都不会创建重复 Question。

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

### 实时 Agent 新增持久事实

`agent_tickets` 保存一次性 ticket 的 SHA-256、organization/interview/connection/participant identity、角色、状态、60 秒到期时间和不含 bearer 的媒体投影；消费使用状态/version CAS，重复或过期票据不能重新打开通道。`interview_sessions.agent_runtime` 保存 floor、事件 sequence、`replay_history_floor`、有限 idempotency key、暖场删除边界、active performance、takeover lease，以及一次冻结的 `authoritative_media_binding(provider/room/candidate_identity/connection_id/bound_at/version)`。binding 不保存 LiveKit bearer，后续控制重连不得改变 room/identity。

`agent_events` 最多保留 1000 条 replayable 最小安全载荷：每种事件有显式字段 allow-list，未知字段不进入历史；`session.snapshot` 只保留空占位并在连接时从当前领域状态按角色重建。lease/actor、私有媒体 URI/object key、加密元数据、证据 binding、内部能力点和 Provider 原始输出不会进入共享 replay 或 Redis Pub/Sub。裁剪时推进 `replay_history_floor`，过旧 cursor 必须完整重同步，不能从残缺历史继续。

`InterviewTurn` 的 `utterances/current_understanding/conversation_acts` 是聚合内版本化 JSON 事实；服务端 final 和录音 URI 必须同时存在才能把 utterance 标为 authoritative。root evidence group 通过根/子 turn 与 answer 引用构造，不复制或覆盖原始音频；evaluation revision 保存使用的 answer/utterance revision 集合。

`interview_media_captures` 独立记录 requested/consented scopes、LiveKit room/participant/Egress、私有 object key/URI、SHA-256、字节数、`storage_protection`、可选 `encryption`、保留期、状态与失败原因，并以 `(organization_id, interview_id)` 唯一。开发环境可记录 `local_private_development` 且不声称静态加密；生产只能在对象级 AES256/KMS 元数据复核通过后完成。对象存储路径只含清洗后的 tenant/interview/capture ID；候选人和普通 AgentEvent 不得到 object key 或私有 URI。Egress webhook 在签名和 body hash 验证后通过 idempotent provider result 推进 capture，完整 hash 未读取成功时保持 `hash_pending`。

`evidence_media_streams` 以 interview/turn 的确定性 ID 保存当前 capture revision、连续 segment/frame checkpoint、完整标记与 repair 音频引用；`evidence_media_segments` 以 stream/revision/ordinal 唯一保存 frame 范围、checksum、byte count 与 `candidate_evidence_segment` FileObject 引用。reset 只单调推进 stream revision，旧 revision 由周期 GC 删除；候选人 purge 删除全部 revision，并把 stream/capture 清成最小 `retention_purged` tombstone，避免被放弃片段或直写 Egress object 脱离 FileObject 留存链路。

权威非答案提交与采集释放使用同一个 Persistence transaction：ConversationUtterance/current_understanding、`UTTERANCE_REJECTED` 事件与当前 stream revision CAS 任一失败均整体回滚。`abandoned_captures` 为未采纳采集增加 `rejected_utterance_id` 和原 repair URI，随后清空当前 repair 字段；不修改旧 segments/完整录音。writer 固定创建时 revision，repair 落盘前后复核 revision/complete/checkpoint，防止同 owner 下等长度新录音被旧异步结果覆盖。新增字段通过现有 JSON documents 存储，无 DDL 迁移；历史答案不强制补写 capture revision。

明确无 final 的采集使用现有 `transcription.started/failed` 生命周期事实与同事务 stream CAS；归档元数据增加 `reason=transcript_unavailable`、`untranscribed_audio_uri`，录音继续受既有留存/访问控制约束，不增加虚构 utterance。Evidence command payload 允许经长度/字符校验的 `capture_id` 和（仅自动 seal）`endpoint_id`，作为迟到计时命令的作用域证据；它们不能代替数据库 owner fence 或媒体 checkpoint。无需 DDL 迁移。

Memory、SQLite 与 PostgreSQL 通用 documents/RLS adapter 已实现上述 collection 合同。数据库时钟 ownership lease、单调 epoch、control generation、CandidateAnswer 事务 commit fence 与持久 command submit/claim/result journal 已由连接无关 owner executor 执行：数据库 polling 是正确性路径，Redis 只作 wake hint，remote controller 只等待 terminal receipt。LiveKit subscriber、StreamingSTTSession 与端点 timer 是当前 owner 的进程态，但新 owner 可从已 seal 的私有 segment/checkpoint 重建并执行 batch repair；未 seal 的内存后缀不冒充已持久化证据，必要时只接受服务端授权 sequence gap 内的浏览器私有 backfill。目标 PostgreSQL/RLS 并发、Redis/LiveKit 故障注入和真实 Egress 对象一致性仍必须由绑定当前 release 的外部验收报告证明，因此实时 Agent 状态继续为 `in_progress`。
