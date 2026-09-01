# 已知问题与修复设计

本文记录 2026-08-25 深度审查确认的实现问题、目标设计、迁移步骤和验收测试。它是修复工作的执行清单；目标领域不变量仍以 `docs/domain-model.md` 为准，接口语义以 `docs/api-design.md` 为准。

状态约定：`open` 表示已确认、尚未修复；`in_progress` 表示实现或验收仍在进行；`verified` 表示仓库代码、迁移/配置保护和可执行自动化测试全部通过；`closed` 表示相关兼容路径也已删除。外部服务另用 `environment_pending`，不能因为 adapter 的离线测试通过就宣称真实云环境健康。

## 修复顺序

| 顺序 | ID | 优先级 | 问题 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `PLAN-001` | P1 | InterviewPlan 双执行表示导致人工编辑被预约路径忽略 | closed |
| 2 | `CONSENT-001` | P1 | Candidate Intake 未验证明确的隐私与录音同意 | closed |
| 3 | `APPOINTMENT-001` | P1 | 候选人 start 未校验预约时间窗和设备准入事实 | closed（仓库） |
| 4 | `REPORT-001` | P2 | 当前报告的 `manual_review` 被历史评分 revision 污染 | closed |
| 5 | `SEARCH-001` | P2 | 公开题库搜索仍使用旧向量兼容路径 | closed |
| 6 | `CANDIDATE-ACCESS-001` | P1 | 候选人页面生产鉴权不可用且管理员投影泄漏标准答案 | closed |

五项已在进入 PDF/对象存储等里程碑 13A 前按同一修复批次完成本地验证，避免继续在错误 interface 上扩展生产能力。

## PLAN-001：InterviewPlan 使用了两套执行真相

### 修复前现状与影响

- `app/services/plan_assembly.py` 同时持久化固定 `items`、随机抽题 `bank_slots` 和经历题快照。
- `app/services/plans.py` 的人工编辑只更新 `items`。
- 旧管理员直建路径读取 `items`，预约 start 则读取 `bank_slots` 和经历题快照。
- 临时回归探针已经复现：人工编辑题目后，预约实际执行的题目不是编辑后的题目。

因此，当前 InterviewPlan interface 泄漏了内部 representation，调用方必须知道应该读取哪一份数据；面试官修改并批准的计划可能被正式预约静默忽略。

### 目标设计

把 Interview Plan Assembly 加深为唯一拥有计划编辑、校验、批准和执行物化规则的 deep module。canonical execution plan 只包含：

- `bank_slots`；
- 每个槽位的 `QuestionCandidatePool`；
- 已批准的 `ExperienceQuestion` 快照；
- 权重、时长、阶段顺序和 `selection_policy`；
- `assembly_summary` 和 readiness 结果。

旧 `items` 不再是可写领域真相。迁移期间可作为只读 compatibility projection；任何旧固定题目都转换成“候选池仅包含一个题目”的槽位，从而保持原执行语义。直接创建会话和预约创建会话必须跨同一个 Plan Assembly seam 读取同一份 canonical execution plan。

### 修改步骤

1. 在 Plan Assembly module 内增加 draft revision/approval 命令，吸收人工编辑后的候选池重建、权重归一、时长守恒和 readiness 校验。
2. 修改 `PATCH /api/v1/interview-plans/{plan_id}`：输入 `bank_slots`、`experience_question_ids` 或 selection policy；旧 `items` 输入在兼容期内立即转换为单候选槽位，不直接持久化为第二份执行表示。
3. 修改 `InterviewService.create_interview` 和 `AppointmentService.start`，让两条路径都从 canonical execution plan 形成 `InterviewPlanSnapshot` 和 `QuestionSelection`。
4. 为已有计划编写一次性迁移：有 `items` 但没有有效槽位时，为每个 item 生成单候选槽位；已有 `bank_slots` 时以槽位为准，并记录迁移告警。
5. 迁移稳定后删除旧 direct-create 的 `items` 分支和相关浅层测试，用 Plan Assembly interface 的行为测试替代。

### 实现与验证（2026-08-25）

- `InterviewPlanAssembly.patch_plan()` 独占草稿编辑、经历题快照、权重/时长再平衡和审批校验；已批准计划拒绝原地编辑。
- `materialize_execution()` 只读取 `execution_schema_version=2` 的冻结槽位和经历题快照；运行时遇到旧 `items` 会返回 `INTERVIEW_PLAN_MIGRATION_REQUIRED`，不再懒迁移或返回 projection。
- 一次性命令 `python -m app.migrations.plan_execution_v2 [--dry-run]` 在部署前把旧固定题目转换为单候选槽位并删除 `items`；当前 SQLite 数据检查为 0 份计划，无数据需要改写。
- 管理员 `POST /api/v1/interviews`、`POST /interviews/{id}/start` 和计划 `items` schema 均已物理删除；正式会话只由 Appointment Admission 创建并 START。
- `tests/test_plan_assembly.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_production_compatibility.py` 验证 v2 物化、迁移语义、批准不可变和旧 interface 不在 OpenAPI 中。状态改为 `closed`。

### 验收测试

- 人工替换、删除、重排题目后，预约 start 执行批准的同一计划 revision。
- 人工编辑跨岗位题目、空候选池、权重不为 1 或时长不守恒时审批失败。
- 已批准计划不可原地修改；复制为新草稿后产生新的 candidate pool hash。
- 断线恢复复用原 `QuestionSelection`，不会因兼容 projection 再次抽题。
- 旧计划迁移前后的题目、顺序、权重和阶段语义一致。

## CONSENT-001：Candidate Intake 没有明确同意证据

### 修复前现状与影响

当前请求和持久化只包含 `consent_version`。客户端即使没有明确接受隐私告知或录音，也能登记并开始本地面试；系统无法证明候选人同意了什么内容。

### 目标设计

Candidate Matching module 只在以下条件全部满足时形成 CandidateIntake：

- `privacy_accepted=true`；
- 预约要求录音时 `recording_accepted=true`；
- `consent_version` 非空且属于服务端当前允许版本；
- 服务端保存 `notice_hash`、`consented_at` 和匹配方法，不信任客户端时间。

规范化、查找哈希、明确同意验证和最小化候选人快照收进同一个 module implementation，调用方只消费“已匹配且已授权”的结果，提升 locality。

### 修改步骤

1. 把 Candidate Intake 请求改成 `consent.accepted/version/recording_accepted` 的结构化对象。
2. 为 `CandidateIntake` 增加 `privacy_accepted`、`recording_accepted`、`notice_hash` 和服务端 `consented_at`。
3. 根据 `InterviewAppointment.settings.record_audio` 决定是否必须接受录音；缺失隐私同意返回 `CONSENT_REQUIRED`，需要录音但未同意返回 `RECORDING_CONSENT_REQUIRED`，两者都不创建 intake、不推进预约状态。
4. 旧 intake 标记为 `legacy_unverified`；生产 start 要求重新同意，开发数据可通过显式迁移开关保留。
5. 日志只记录预约 ID、告知版本/hash 和结果，不保存告知正文或联系方式。

### 实现与验证（2026-08-25）

- 预约只接受服务端 `CONSENT_NOTICE_CATALOG` 中允许的版本，冻结实际隐私/录音告知正文并对规范化内容生成 SHA-256；公开邀请返回候选人实际需要确认的正文、版本和 hash。
- intake 使用 `consent.accepted/version/recording_accepted`，只有姓名与至少一个规范化联系方式匹配且授权满足预约设置时，才保存 `match_method`、授权布尔值、notice hash 和服务端时间并推进状态。
- 会话创建时把已验证授权证据冻结到 `InterviewCandidate`；重复 intake 不会弱化已验证授权。
- `tests/test_position_resume_appointment_flow.py` 验证拒绝隐私同意、错误版本、拒绝必需录音均失败且预约保持 `invited`，同时验证不录音预约允许 `recording_accepted=false`。
- 管理员直建会话入口已删除，所有新会话都要求已验证 Candidate Intake；状态改为 `closed`。

### 验收测试

- 隐私同意缺失或为 false 时登记失败且无持久副作用。
- 预约要求录音而录音同意为 false 时登记失败。
- 不录音预约允许 `recording_accepted=false`，但仍要求隐私同意。
- 服务端保存的时间、版本和 notice hash 可在 InterviewCandidate 快照中追溯。
- 重复 intake 幂等返回同一同意事实，不能覆盖为更弱授权。

## APPOINTMENT-001：start 没有预约时间窗准入

### 修复前现状与影响

公开 start 只校验邀请 token 状态和 `invitation_expires_at`，没有校验 `scheduled_start_at/scheduled_end_at`，也没有消费已持久化的设备 readiness。现有端到端测试甚至使用未来预约并立即开始，因此没有覆盖真实准入不变量。

### 目标设计

Appointment module 提供一个小而稳定的 admission interface，在同一事务内吸收：token、登记状态、明确同意、预约时间窗、设备 readiness、模型 readiness、一次性消费和唯一 InterviewSession 创建。时间由注入的 Clock dependency 提供，测试使用固定 clock；不让 HTTP 或 WebSocket 调用方自行判断时间。

默认 start 窗口为 `[scheduled_start_at, scheduled_end_at]`。若组织需要提前/延后宽限，使用显式 `early_start_grace_seconds/late_start_grace_seconds` 策略，并冻结到预约；邀请过期时间仍是独立条件，不能替代预约窗口。

### 修改步骤

1. 为公开 readiness 保存浏览器、麦克风、音频格式、检查时间和有效期；start 只接受未过期的 readiness fact。
2. 在 Appointment module 注入 Clock，并实现预约窗口和宽限策略判断。
3. 把准入判断、预约 `registered -> consumed`、唯一 session 创建和首次生命周期 START 放进同一事务/命令；重复 start 返回同一会话。
4. 时间窗外分别返回 `APPOINTMENT_TOO_EARLY` 或 `APPOINTMENT_WINDOW_CLOSED`；公开响应不返回候选人或计划敏感数据。
5. 更新现有测试，使用固定时钟而不是远期硬编码日期。

### 实现与验证（2026-08-25）

- 新增 `AppointmentAdmission`，统一校验已验证授权、默认或带宽限的预约窗口、带 TTL 的设备事实和即时模型 readiness；`AppointmentService` 与最终 `InterviewService` 事务共用该规则和可注入 Clock。
- readiness 上报会持久化浏览器支持、麦克风授权、音频 MIME、服务端检查时间和失效时间。
- 最终事务再次校验全部准入条件，并原子创建/首次 START 会话与 `registered -> consumed`；相同预约的重复或并发 start 返回同一会话。
- `tests/test_known_issue_remediations.py` 用固定时钟覆盖窗口前/后、设备过期、模型 readiness 过期和授权缺失；端到端测试以两个并发 start 验证只生成一个 interview ID。
- 仓库准入缺口及旧直接 start 路由均已删除，状态改为 `closed（仓库）`；真实 Provider 与 PostgreSQL 仍分别按 `environment_pending` 验收，不能借此宣称生产环境通过。

### 验收测试

- 窗口前、窗口内、窗口后和宽限边缘均有固定时钟测试。
- readiness 缺失或过期时不能 start。
- 两个并发 start 只创建一个 session，并只消费预约一次。
- 创建 session 后进程中断，重试仍返回相同 session 和 `QuestionSelection`。
- Memory、SQLite 和未来 PostgreSQL adapter 通过同一 admission contract tests。

## REPORT-001：历史评分 revision 污染当前报告

### 修复前现状与影响

报告分数从每个答案的 `current_evaluation_id` 读取，但 `manual_review` 判定扫描全部 `evaluation_revisions`。一个已经被新 revision 替代的低置信度评分仍会让当前报告保持 `manual_review`，违反“报告只引用当前 revision”的领域不变量。

### 目标设计

Report module 在生成开始时先物化唯一的 `current_evaluations` 集合，分数、维度、证据、风险、`manual_review` 和 `evaluation_ids` 全部只从该集合计算。旧 revision 只属于历史报告，不参与新报告。

### 修改步骤

1. 通过每个 CandidateAnswer 的 `current_evaluation_id` 构建 current evaluation map，缺失指针时形成显式报告告警。
2. 用同一集合计算总分、维度、复核标记、证据和 `evaluation_ids`，禁止再次遍历全部历史 revision。
3. 重评完成后原子更新当前指针并请求新报告；旧 InterviewReport 保留旧 `evaluation_ids`。
4. 在报告生成时断言 `report.evaluation_ids` 与生成时的当前指针集合一致。

### 实现与验证（2026-08-25）

- `ReportService.build_report()` 开始时按每个答案的 `current_evaluation_id` 物化唯一 current evaluation 集合；总分、维度、证据、复核标记和 `evaluation_ids` 全部从该集合产生。
- `tests/test_known_issue_remediations.py` 验证被替代的低置信度旧 revision 不再污染当前报告，同时当前 revision 低置信度时仍返回 `manual_review`。
- 历史评分/报告 revision 是预期审计事实而非兼容执行路径；当前报告只读 current pointers，状态改为 `closed`。

### 验收测试

- 被高置信度新 revision 替代的低置信度旧评分不再触发当前 `manual_review`。
- 当前 revision 低置信度或带 review flag 时必须触发 `manual_review`。
- 每个报告只引用生成时的 current evaluation IDs；历史报告仍可重放。
- 并发重评与报告生成通过 version 冲突重试，不产生混合 revision 报告。

## SEARCH-001：公开题库搜索与主链路语义不一致

### 修复前现状与影响

计划装配调用 Catalog module 的无向量结构化搜索，公开 `POST /api/v1/questions/search` 却调用旧 Question module 的 embedding/余弦搜索。两个 module 读取相同 Question 数据但暴露不同 interface；公开路径还把部分过滤留在 Python 全量列表之后。

### 目标设计

把 Question Catalog 加深为唯一的查询 module。它的 interface 接收租户派生的 scope、岗位、题库集合、结构化条件和查询 purpose；实现隐藏关系查询、关键词搜索、readiness 过滤和可选相似度 projection。计划装配与公开后台搜索跨同一个 seam，调用方不感知 embedding adapter。

`POST /api/v1/questions/search` 固定为无向量的关键词/结构化查询。若保留相似题实验，使用显式的后台治理入口或 `purpose=similarity_governance`，其不可用不得影响候选池、计划或评分。

### 修改步骤

1. 定义 `QuestionSearchScope`：服务端组织、必填岗位和非空题库 IDs；验证所有题库属于岗位和组织。
2. 把 `status=active`、`validation_status=valid`、题库/岗位/技能/难度/题型过滤下推到 Persistence module 的查询 interface，不先读取全量 Question。
3. 让公开 route 和 Plan Assembly 都调用 Question Catalog interface；删除旧 route 到 QuestionService 的向量分支。
4. 若保留向量能力，将其放在可删除、可重建的内部 projection/adapter 后，并使用不同的显式治理语义。
5. 兼容期内对旧请求返回弃用提示，随后删除旧搜索实现和只验证向量路径的测试。

### 实现与验证（2026-08-25）

- 公开 `/questions/search` 与 Plan Assembly 现在都调用 `CatalogService.search_questions()`；公开 schema 强制岗位和非空题库 scope，不再触发 embedding。
- `QuestionRepository.search_catalog()` 把租户、岗位、题库、active、valid、speech-ready、技能、难度和题型条件交给 Persistence backend；SQLite 用 `json_extract/json_each`，PostgreSQL 用 JSONB 条件，Memory adapter 遵守同一 contract。
- Web 工作台搜索增加岗位题库范围选择，避免前端继续发送旧无 scope 请求。
- 端到端测试验证无 embedding 的结构化搜索、跨岗位拒绝和计划/预约闭环；`tests/test_persistence_contract.py` 对 Memory/SQLite 验证未就绪、跨租户/岗位/题库和结构化条件均不会泄漏。
- `app/services/questions.py`、`VectorDocumentRepository`、Memory/SQLite 向量集合和 `question.index` worker 分支均已删除；全局题目创建/列表路由也已删除。状态改为 `closed`；真实 PostgreSQL `EXPLAIN` 仍属于 `DATA-001` 环境验收。

### 验收测试

- 未提供岗位或题库 scope 时请求失败。
- 跨岗位、跨组织、inactive、invalid 或不满足候选池 readiness 的题目不会泄漏。
- 未配置 embedding route 时结构化搜索、计划装配和预约仍全部通过。
- 公开搜索和计划候选池对相同 scope/filters 返回一致题目集合。
- PostgreSQL 查询计划证明过滤在数据库执行；Memory/SQLite adapter 通过同一查询 contract。

## CANDIDATE-ACCESS-001：候选人生产访问与安全投影不成立

### 修复前现状与影响

预约 start 返回 `/#candidate/{interview_id}?token=...`，但候选人页面随后调用需要后台 Bearer/RBAC 的 `/api/v1/interviews/{id}`、`audio-answers` 和 `avatar/speak`。开发模式因默认管理员身份掩盖了问题，生产环境会返回 401。更严重的是后台会话详情包含所有轮次的 `question_snapshot`，其中有标准答案、rubric 和后续题目，不能作为候选人投影。

### 修改方法

1. 新建 `/api/v1/public/interviews/{id}` 候选人窄接口，所有操作必须同时验证 `candidate_session_token`。
2. 返回 allow-list 投影：候选人姓名、会话状态、轮次 ID/顺序/状态；只向当前或已完成轮次返回题干，不返回计划、候选池、标准答案、rubric、报告 revision 或 token。
3. 将候选人录音提交和读题移动到同一 public namespace；录音 URI 必须属于当前 `interview_id/current_turn_id`，阻止跨会话引用。
4. 将公开候选人路由纳入 Redis fail-closed 限流；管理员详情、复核、报告等端点继续使用 Bearer/RBAC。
5. 前端邀请页执行姓名/联系方式匹配、明确同意、麦克风 readiness 和公开 start，随后只调用候选人窄接口。

### 实现与验证（2026-08-25）

- `candidate_session_token` 由生产密钥对会话 ID/创建时间做 HMAC-SHA256 派生，数据库和后台详情不持久化/返回明文；`InterviewService.get_candidate_interview()` 使用 allow-list 构造安全投影并以常量时间比较验证 token。
- `submit_candidate_audio_answer()` 限制录音路径到当前会话/轮次；候选人 REST 文本答案和 WebSocket `candidate.answer.text/candidate.transcript.final` 均已删除。
- React `candidate` feature 已实现 `/#invite/{token}` 登记页和候选人 public API 调用，生产不再依赖后台 Bearer 身份。
- `tests/test_realtime_media.py` 验证错误 token、跨会话媒体和标准答案投影均被拒绝，并完成 public 音频转写评分闭环。状态为 `closed`。

## 生产化审查问题（DOC-GAPS-001）

| ID | 优先级 | 状态 | 问题 | 修复与验证 | 剩余边界 |
| --- | --- | --- | --- | --- | --- |
| `FILE-001` | P1 | `verified` | 简历仍依赖 `resume_text`/调用方 URI，没有 PDF、扫描和私有文件真相 | 新增 PrivateFileStorage、FileObject、multipart/URL、SSRF/隔离/扫描/PDF 解析；原件和解析文本分别私有化；旧 JSON 入口删除；官方 OSS SDK 已纳入依赖，command/clamd INSTREAM 扫描合同、本地/OSS contract 与恶意/环回/幂等测试通过；官方 ClamAV arm64 daemon 的 PING/干净样本/EICAR 集成测试已通过 | 真实 OSS bucket/RAM、目标 clamd 完整签名库更新与告警 `environment_pending` |
| `ASYNC-001` | P1 | `verified` | 题库构建/PDF/worker 缺少退避、dead-letter、监控和重放 | import/rebuild/build 与 PDF 均返回 job；Outbox 增加最大尝试、指数退避、dead-letter、指标和审计重放；故障矩阵通过 | 外部告警平台由部署环境接入 |
| `DURABLE-MODEL-RETRY-001` | P1 | `verified（仓库）` | 模型调用首次可重试失败时，部分服务提前写领域 failed；TTS 因此推进 Question version，下一次成功响应被自身的 source-version guard 判成 superseded，出现 6/10 假终态 | 统一 `Outbox failed=重试等待 / dead_letter=领域终态`；SpeechBuild 把可领取失败计入 pending，TTS 保持 Question version，题库导入、Resume Review/经历题、评分和报告同步采用终态门禁；后端故障注入与 React 部分完成试听回归通过 | 真实供应商限流/超时率、目标 Celery/Redis/PostgreSQL 的长时间故障恢复仍为部署环境验收 |
| `STREAM-001` | P1 | `verified（仓库）` | 没有 `stt.streaming/open_stream`、唯一 final 和断流修复 | 新增 streaming schema、网关开流、序号/大小/唯一 final 校验、WebSocket、私有录音和 batch repair；DashScope 提供低延迟 partial/final，`media_http` 保留通用 batch-final stream；端到端测试通过 | 阿里云真实凭据、目标网络、WER/延迟/费用 `environment_pending` |
| `MODEL-STREAM-PROBE-001` | P1 | `verified（仓库与本机）` | 流式 STT 模型测试用静音 WAV 强求 final，真实握手成功仍误报 `provider_final_transcript_missing`；实时语音对话没有统一测试 schema | 模型配置与 route 复用 stream handshake probe；STT/实时语音收到 ready 后主动关闭并返回 `probe_mode=handshake`；Qwen 3.5 session、输入转写模型、音色和受控指令更新顺序按当前协议校正；合同测试与本机百炼真实握手通过 | WER、final/首音延迟、打断、音质和费用仍为独立环境验收 |
| `SECURITY-001` | P1 | `verified` | 联系人/凭证明文、无 RBAC/公开限流、敏感访问/失败请求无审计，URL token 可能进入日志 | 联系人 Fernet+租户 HMAC、ProviderSecretVault、生产 Bearer RBAC、Redis fail-closed 公开限流、未认证/成功请求审计、签名文件/媒体、媒体实际下载审计、URL token 脱敏；生产权限/限流测试通过 | 外部 IdP/企业 SSO 尚未选择 |
| `DATA-001` | P1 | `in_progress` | 只有 SQLite JSON，缺少生产租户 RLS 和数据库唯一约束 | 已提供显式 owner migration 与最小权限 runtime adapter；本机 PostgreSQL 16 真实验证事务/CAS、RLS、Outbox/预约约束、题库索引 EXPLAIN，离线 SQL 不变量继续通过 | 目标生产 PostgreSQL 的角色、并发负载、备份恢复与 EXPLAIN 仍为 `environment_pending`，完成前不改 verified |
| `MEDIA-001` | P1 | `verified（仓库）` | mock TTS URI/公开 media 不能作为生产资产 | 非 mock TTS 和生产候选人录音使用 PrivateFileStorage+FileObject；移除 `/media` 静态挂载；`media_http` 支持 HTTPS audio/video 数字人；腾讯云专属 adapter/TCPlayerLite 已实现 WebRTC 云渲染与会话回收；签名访问与下载审计通过 | 真实账号、形象授权、并发和媒体播放质量 `environment_pending` |
| `AVATAR-DELIVERY-001` | P1 | `verified（仓库）` | 数字人曾只能隐式走云 route，创建人无法按预约选择低成本自研方案；云降级与前端播放容易复制分支 | 预约 settings 增加强类型 `local/cloud`；新预约默认 local，历史缺字段按 cloud；AvatarDelivery 以 Local/Cloud 两个 adapter 复用冻结 TTS 签名访问、统一响应、React play/stop 和云 session 回收；腾讯链路完整保留并标注扩展 TODO；后端合同/安全音频和 Vitest 行为通过 | 自研高精度口型/Live2D/3D、自建 SFU 属体验扩展；腾讯真实媒体质量/费用仍为 `environment_pending` |
| `MODEL-PROVIDER-001` | P1 | `verified（仓库）` | 只有 OpenAI-compatible LLM/Embedding，没有真实 TTS adapter；DashScope manifest 声明能力但不可执行；路由 UI 漏掉 TTS 并把 `retry_count` 误写为 `max_retries` | OpenAI-compatible 增加 Speech TTS；DashScope 增加 Qwen Chat/Embedding、Qwen3-TTS/CosyVoice 与 streaming/batch ASR；腾讯云增加数智人 WebRTC adapter；路由请求改为强类型、生产缺 route 失败关闭；供应商合同、registry、路由与私有资产哈希测试通过 | 阿里/腾讯真实账号、区域、API Key、模型/形象授权、音质/WER/延迟/费用验收为 `environment_pending` |
| `MODEL-PROVIDER-002` | P1 | `verified（仓库）` | manifest 没有可执行模型目录/默认配置语义，路由只能手输模型；DeepSeek/智谱未成为独立插件，OpenAI-compatible 厂商容易复制 runtime | 增加 defaults、predefined/customizable 与模型目录校验；DeepSeek/智谱薄 adapter 复用共享 runtime；千问目录/UI 显式化；配置默认值、目录路由约束、厂商 HTTP 合同和全量回归通过 | 三家真实 API Key、区域/账号授权、模型可用性、延迟/费用与结构化输出稳定性为 `environment_pending` |
| `MODEL-PROVIDER-003` | P1 | `verified（仓库）` | 智谱只声明 LLM，单一 `test_model` 会把 `glm-tts` 当成 LLM；配置 UI 无编辑/按能力测试，HTTPX 初始化时缺 SOCKS 依赖会裸抛 500 | 增加 GLM-TTS adapter/模型目录、`capability + model` 测试协议、配置编辑 UI、显式环境代理开关和结构化 transport 错误；厂商 HTTP 合同、API、UI 与全量回归通过 | 真实智谱凭据、模型授权、音色、音质/延迟/费用为 `environment_pending` |
| `MODEL-CONFIG-V2-001` | P1 | `closed` | 厂商账号、API Key、具体模型和 route target 混在 `ModelProviderConfig`，前端硬编码厂商字段，多个模型会复制凭证和参数 | 拆分 ProviderConnection/ModelConfiguration/ModelRoute；v2 manifest 提供动态表单；route 只引用 ready model；旧 API、collection 和运行时 target 已删除；显式迁移与回归测试通过 | 真实厂商模型仍需逐个测试，健康状态为部署环境事实 |
| `QUESTION-GEN-TRUNCATION-001` | P1 | `verified（仓库）` | `finish_reason=length` 曾被归为通用 JSON 错误，网关与 Outbox 会用相同参数嵌套重试，双槽位长响应反复计费且不能保留批次进度 | 新增 `provider_output_truncated` 与失败 token 诊断；非重试工作首轮 dead-letter；生题 attempt 只调用一次 Provider；双槽位截断原子拆成两个单槽位工作；Prompt/Schema v2、Provider/网关/Memory/SQLite/工作流合同测试通过 | 未自动重放历史失败批次；目标 DeepSeek 账户的 10 题并发、费用与真实截断恢复仍为 `environment_pending` |
| `RESUME-REVIEW-TRUNCATION-001` | P1 | `verified（仓库）` | 真实 `deepseek-v4-pro` 单次简历审阅的 6000 输出 token 中有 4081 个 reasoning token，以 `finish_reason=length` 结束并留下未完整 JSON | 最终 Prompt/Schema 继续升级为 v6/v4：审阅只输出初筛与有限证据，不再耦合经历题；单次截断时丢弃半截输出、自动转为完整 Map/Reduce；问题在符合资格后由独立 v1 合同生成。Prompt 合同、截断降级和资格门禁测试通过 | 不自动消费真实模型重放历史失败审阅；目标账户重试质量/时延/成本仍为 `environment_pending` |
| `QUESTION-SPEECH-DEFAULT-001` | P1 | `verified（仓库）` | 非生产环境创建题库时无条件绑定开发 mock，即使组织已有 ready 的真实题目 TTS route；mock 资产被标为语音 ready，点击试听只得到难理解的“非私有生产资产”错误 | 新题库优先解析并冻结 enabled/ready `question_speech_generation` route primary 与默认音色；题目投影增加 `speech_preview`；mock 试听返回专门错误和配置建议；React 明示“开发模拟语音（不可试听）”并禁用无效按钮；后端/前端合同测试通过 | 不批量改写旧题库或删除历史 mock 资产；目标 TTS 的真实音质、费用和对象存储仍为环境验收 |
| `TEST-ISOLATION-001` | P0 | `verified` | `reset_store_for_tests()` 曾取得默认 SQLite 并执行 `reset()`，全量测试会删除本地开发数据 | helper 改为直接替换成全新 `InMemoryStore`；回归测试证明临时 SQLite 不被重置，99 项全量测试前后开发 DB SHA-256 不变 | 本轮误删前数据无法从 SQLite `.recover`/本地快照恢复；已恢复可确认的智谱非秘密配置，API Key 需管理员重填 |
| `REALTIME-001` | P2 | `verified（仓库）` | 多实例事件、断路器、心跳和厂商实时媒体曾缺失 | Redis bus/共享断路器/心跳已验证；新增浏览器 16k PCM → DashScope duplex ASR、腾讯数智人 create/stat/start/drive/close 与 TCPlayerLite WebRTC/SFU 播放 | 目标 Redis 集群、阿里/腾讯真实凭据、形象并发、WER/延迟/费用仍为 `environment_pending` |
| `REALTIME-DIALOGUE-001` | P1 | `verified（仓库）` | 追问曾依赖“完整 STT → 完整 LLM → 完整 TTS”的串行链路，评分同步阻塞下一轮；追问预算、父子轮次、PCM 录音和断线恢复也没有形成同一运行时合同 | 新增 `speech.dialogue_realtime` 深模块及 OpenAI Realtime、DashScope Qwen Realtime adapter；预约可选 `cascade/s2s`。S2S 音频按 delta 即时下发，但只逐字表达服务端已经批准的澄清题；权威转写仍由 STT 形成 CandidateAnswer，完整评分改为 Outbox 异步执行。追问固定为零权重子轮次，并限制深度、每题次数、总次数、回答时长和文本长度；浏览器 16 kHz PCM、完整本地备份、batch repair、心跳和角色安全事件均有合同/端到端测试 | OpenAI/百炼真实凭据、模型权限、区域、网络抖动、首音延迟/打断/音质/费用为 `environment_pending`；火山引擎豆包具有官方实时语音产品，但其二进制会话协议及“严格播报已批准追问”尚未实现，明确保留 `TODO`，不得配置为 ready route |
| `FAIRNESS-001` | P2 | `verified（仓库）` | 只有抽题分布，没有 AI/人工评分一致性与 cohort 差异校准 | 保留抽题公平性投影；新增只接受 current evaluation ID、人工分和 opaque cohort 的校准 API，输出 MAE/RMSE/偏差/分层/样本量告警且绝不自动改分 | 用户尚未提供真实脱敏金标；实际公平性结论为 `data_pending` |
| `RETENTION-001` | P1 | `verified` | 敏感数据只有设计中的到期字段，没有可执行删除边界 | 新增默认 dry-run 的管理员留存服务；显式执行删除私有文件/录音并清空联系人、审阅、经历题、转写、评分/报告敏感内容，审计测试通过 | 企业实际留存天数与 legal hold 策略由部署配置决定 |
| `COMPAT-001` | P2 | `closed` | 计划 `items`、管理员直建会话、文本答案和旧向量 QuestionService 曾同时存在 | 提供显式 v2 迁移命令；运行时旧表示、旧 schema、旧路由、旧 service/repository/worker 分支和前端 fallback 均已删除；OpenAPI/端到端测试通过 | 部署已有旧数据时必须先运行 v2 迁移；当前 SQLite 检查无计划数据 |
| `KB-SPEECH-001` | P1 | `verified（仓库与本机）` | 题库页曾平铺全部题目，题库只能保存声音且 HTTP 会直接等待 TTS；运行中切换模型还会与进度 version 写入竞争 | 已实现 KnowledgeBaseSpeechProfile、voice catalog、整库 SpeechBuild、revision 防旧写、分层 React UI/API、模型引用删除保护与 Celery+DurableWorkItem；切换改为 profile revision 语义 CAS 并原子取消旧任务，前端增加进度自动刷新、取代提示和失败重试 spinner；全量回归、Celery eager 和前端行为测试通过 | 真实外部 TTS、目标 Redis/PostgreSQL 属部署环境验收，不回退本项仓库状态 |

### KB-SPEECH-001 修改步骤与批量重建不变量

1. React `#questions` 改为只加载当前组织 KnowledgeBase 摘要；点击进入 `#questions/{knowledge_base_id}`，详情路由再加载题目、当前语音配置、TTS 模型/声音目录和构建进度。
2. KnowledgeBase 增加 `speech_profile` 与独立 revision，绑定已 ready 的 TTS ModelConfiguration、voice、language、format 和 speaking rate；旧 `language/voice_profile_id` 通过显式迁移转成 profile，无法解析模型时标记 `configuration_required`。
3. `PUT /knowledge-bases/{id}/speech-profile` 使用 CAS 和 Idempotency-Key；配置变化原子创建 `knowledge_base.speech.rebuild` 父工作项并立即返回 202，不在请求线程调用 Provider。
4. 所有 Celery task 和异步执行编排放在 `app/workers/`。父 task 冻结 Question ID/version manifest、分批 fan-out 子工作项；子 task 调用显式 TTS 模型并复制到 PrivateFileStorage。
5. 子 task 提交前验证 Question version 与 speech profile revision；旧 revision 只能成为历史资产或 superseded，不能覆盖当前指针。历史计划/会话资产保持可读。
6. Celery 只调度 `organization_id + work_item_id`；DurableWorkItem 继续提供租约、幂等、退避、dead-letter、进度和人工重放。Beat dispatcher 补发发布失败与过期租约。
7. 自动化验收至少覆盖：路由级加载无全量题目首屏；无效/非 TTS 模型与非法 voice 拒绝；切换模型或声音整库 fan-out；重复投递不重复资产；运行中再次切换时旧结果不覆盖；部分失败只重试失败项；worker crash 恢复；已批准计划和历史会话继续播放旧资产。

### FILE-001 修改步骤与恢复语义

1. API 在创建 `ResumeDocument(processing)`、`FileObject` 和 `resume.ingest` 工作项前校验上传 PDF magic/MIME/大小；URL import 强制 `Idempotency-Key`。
2. URL worker 禁用环境代理，在初始 URL 和每个重定向重新解析 DNS，拒绝所有非全局地址、嵌入凭据和生产 HTTP。
3. 文件只在隔离区读取；扫描失败、感染、加密 PDF、空文本或解析失败时原子标记 Resume/File/WorkItem 错误，不能触发 Resume Review。
4. clean PDF 和解析文本分别写 PrivateFileStorage，成功事务只保存受控对象键/哈希；API projection 移除正文与键。
5. worker 进程中断可从同一 work item 重试；相同幂等键返回同一简历版本，不重复创建对象。dead-letter 只能经管理员写明原因后重放。

### STREAM-001 修改步骤与最终转写不变量

1. `ModelGateway.open_stream(StreamingSTTRequest)` 根据组织/capability/purpose 解析 route，只允许在任何音频被接受前 fallback。
2. `ValidatedSTTStream` 限制单 chunk、总字节、单调 sequence，并拒绝多个 final、final 后继续发送或 provider 缺失 final。
3. `stt-stream` 保存完整录音；唯一服务端 final 通过 `submit_streaming_answer()` 进入生命周期与评分，不接受浏览器 final。
4. provider 中断或 finish 无 final 时，录音进入 `stt.batch/candidate_answer_repair`；修复成功前轮次不能进入 evaluating。
5. 生产 readiness 要求 streaming/batch route 为非 mock、implemented 且 `last_health` 未超过 TTL。

### MODEL-PROVIDER-001 修改步骤与外部边界

1. `openai_compatible` 在原 Chat/Embedding deep module 内增加 `/audio/speech` 二进制响应适配，统一产出 `TTSSynthesizeResponse`，不把音频协议泄漏到题库或面试编排。
2. `dashscope` 复用 OpenAI-compatible Qwen Chat/Embedding，在 TTS seam 内分别转换 Qwen3-TTS multimodal-generation 与 CosyVoice/Qwen-Audio `SpeechSynthesizer` 请求。
3. 非 mock 远程音频必须复制到 PrivateFileStorage，最终内容哈希以实际私有文件为准，不能信任临时 URL 或供应商元数据。
4. ModelRoute API 拒绝未知 policy/target 字段和同组织重复 capability/purpose；生产缺失精确 route 返回 `provider_route_missing`，development/test 才可使用 mock fallback。
5. CI 使用离线 HTTP fake 验证请求形状、鉴权、响应归一化、音频格式和哈希，不依赖真实 API Key。只有在目标区域用真实凭据完成 route test、私有音频播放、延迟/费用和 readiness TTL 验收后，外部状态才能从 `environment_pending` 改为健康。

### MODEL-PROVIDER-002 修改步骤与 Dify 参考边界

1. 参考 Dify 官方插件的声明式 Provider/Model schema 和 runtime adapter 分工，把 `defaults`、`model_selection`、模型 ID/能力/default 元数据收进现有 `provider.json`；不复制 Dify 面向多租户插件市场的庞大 ProviderManager。
2. Registry 拒绝空 predefined 目录、重复模型 ID、模型越权能力和同一能力多个默认模型；Provider 配置先合并非秘密 defaults 再按 schema 校验。
3. ModelRoute 对 predefined provider 强制校验 `model + capability`，customizable provider 保留自定义模型 ID；管理页面从 catalog 预填地址并按目标能力给出模型候选。
4. `DeepSeekProvider` 与 `ZhipuAIProvider` 继承 `OpenAICompatibleProvider`，只声明 provider ID 和 `json_object` 结构化输出策略；共享 runtime 统一完成 HTTP、Bearer、错误、usage 与响应归一化。
5. `tests/test_openai_compatible_vendor_providers.py` 用离线 HTTP fake 验证两家 URL、鉴权、JSON Schema prompt、`response_format=json_object` 和 Provider 元数据；registry/API 测试验证目录与默认配置，全量 `93 passed`。
6. 真实供应商仍需分别创建配置和 route 后执行连接测试；账号未授权、模型退市或厂商兼容差异属于外部健康事实，不能因离线合同通过而标记生产 healthy。

### MODEL-PROVIDER-003 修改步骤与能力模型边界

1. 智谱 manifest 同时声明 `llm.chat_json`、`llm.chat_text` 与 `tts.synthesize`；`glm-5.2` 只属于 LLM，`glm-tts` 只属于 TTS，每项能力各有默认模型。
2. `ZhipuAIProvider` 继续复用 OpenAI-compatible Chat/二进制语音 runtime，只在厂商 seam 校验官方 `/audio/speech` 的 `glm-tts`、WAV/PCM、1024 字符上限和默认 `tongtong` 音色。
3. Provider test body 可选 `capability + model`；解析顺序为请求、`test_models[capability]`、能力匹配的旧 `test_model`、manifest 默认模型。`predefined` 模型/能力不匹配时调用前返回 `MODEL_PROVIDER_MODEL_UNAVAILABLE`。
4. 管理页面新增编辑和按能力测试弹窗；编辑以 `expected_version` 提交，空 API Key 不发送 `credentials`，测试切换到 `tts.synthesize` 时模型自动切为 `glm-tts`。
5. 共享 HTTP transport 默认 `trust_env=false`，只有显式配置环境代理才继承代理变量；transport 初始化失败映射为 `provider_transport_unavailable`。离线合同、API/UI 与全量 99 项测试已通过，真实账号联调保持 `environment_pending`。

### MODEL-CONFIG-V2-001 修改步骤与模型配置边界

1. `ProviderPluginDefinition` 通过 v2 manifest 声明连接、凭证、模型类型和厂商模型参数表单；服务端严格校验，前端使用同一组通用控件渲染。
2. `ProviderConnection` 只保存 API Key 引用、Base URL、区域等连接级信息；`ModelConfiguration` 保存一个 `llm/embedding/tts/stt/avatar` 模型、专属 settings、统一 defaults、能力和健康事实。
3. 模型测试允许 `untested/failed` 配置执行隔离探针，成功后置 ready；普通调用和 route 创建仍拒绝未就绪模型。
4. ModelRoute target 只保存 `model_configuration_id/timeout_s/pricing`；网关在一次深模块调用内解析模型、连接、凭证和 adapter，并应用调用显式参数优先的模型默认值。
5. `app.migrations.model_configuration_v2` 支持 SQLite dry-run/一次性迁移；旧 API、`provider_configs` collection 和 `provider_config_id + model` 运行时表示已删除，动态表单、迁移、API、网关和 UI 回归测试覆盖该边界。

### TEST-ISOLATION-001 修复与数据恢复记录

1. 根因是 `reset_store_for_tests()` 调用 `get_store()`，默认后端为 SQLite，随后 `SQLiteStore.reset()` 执行四张表的 `DELETE`；这违反测试不得触碰开发数据的边界。
2. helper 现直接替换全局 store 为新 `InMemoryStore`，不再打开或 reset 默认数据库；新增测试先向临时 SQLite 写入 sentinel，再执行 helper 并重开数据库验证 sentinel 仍存在。
3. 修复后执行全量 99 项测试，`data/interviewer.sqlite3` 测试前后 SHA-256 均为 `816ab277130d9c53aa1d0032b8331d4d13e68bfd3ce75ab1f661cec6abac3498`。
4. 对受影响数据库先复制到 `/private/tmp/interviewer-reset-20260825.sqlite3`，再用 SQLite `.recover` 和本地 APFS snapshot 检查；未找回已删除凭证或业务记录。不能猜测密钥，已把可确认的 `mpc_11b4d940e3134074` 非秘密智谱配置按原 ID 恢复为 disabled，等待管理员在新编辑 UI 中补 API Key 后启用。

### SECURITY-001 修改步骤与审计边界

1. CandidateProfile 只保存邮箱/手机号密文、掩码和租户 HMAC lookup hash；匹配使用 hash，生产拒绝旧明文。
2. Provider credentials 在 repository seam 密封；生产缺少密钥或读取旧未密封值时失败关闭。
3. HTTP 中间件把 Bearer token 映射为组织 Principal，并按 admin/interviewer/reviewer 拦截；候选人 WebSocket 继续使用预约短期 token。
4. 所有 API 结果记录 route/method/status/actor，不记录 body；未认证请求也记审计，invitation/file/media URL token 先替换为 `{token}`。
5. 简历/音频 grant 只有分钟级，音频实际打开另记 `answer.audio.downloaded`；报告 CSV/JSON 导出记 `interview.report.exported`。
6. invitation 与签名文件路由按 IP/操作组限流；生产缺少 Redis 或 Redis 故障时失败关闭，429/503 也写脱敏审计。

### DATA-001 环境验收清单

`DATA-001` 只有完成以下真实数据库测试才可从 `in_progress` 改为 `verified`：

本机隔离 PostgreSQL 16 已完成 migration owner/runtime role 分离、RLS 跨租户拒绝、事务回滚、CAS、Outbox 幂等、预约唯一约束、结构化题库过滤和索引 `EXPLAIN (ANALYZE, BUFFERS)`；以下清单仍需在目标生产集群按实际角色、参数和负载复验。

- 在非表 owner 应用角色上运行 migration，并验证 `FORCE RLS` 对跨租户读写均拒绝。
- 运行 Memory/SQLite 同一套 Persistence contract，再对 PostgreSQL 跑事务回滚、CAS、Outbox 租约/幂等和结构化题库过滤。
- 并发执行两个 appointment start，证明数据库唯一约束只保留一个 InterviewSession。
- 对结构化题库查询执行 `EXPLAIN (ANALYZE, BUFFERS)`，确认租户/岗位/题库/readiness 条件在数据库执行。
- 验证 provider secrets 物理列只有密文，审计/调用日志不含候选人正文或 token。

## 完成与关闭规则

每个问题修复后必须同时完成：代码、数据迁移、自动化测试、对应接口/领域/进度文档更新。兼容代码仍存在时状态最多为 `verified`；只有旧 representation、旧路由或旧输入完全删除后才改为 `closed`。
