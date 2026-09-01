# 操作变更日志

本文件记录所有会修改仓库内容的工作项。只读检查不单独登记；同一目标下的代码、测试、迁移和文档修改合并为一个工作项。状态使用 `in_progress`、`verified`、`failed` 或 `cancelled`，历史条目只追加或补充结果，不删除。

## 记录格式

每个工作项必须包含：日期、ID、目标、关联问题、状态、实际修改文件、验证命令与结果、未完成事项或恢复说明。

## 2026-08-31 · DASHSCOPE-LLM-CATALOG-001

- 目标：核对阿里云百炼千问 Plus 的真实模型 ID，扩充 DashScope 头部与常用 LLM 目录，并让可自定义的模型类型在管理页可见、可选官方候选项。
- 关联问题：页面当前只预填 `qwen-plus`，容易让用户误以为模型名不存在；DashScope LLM 虽然允许手填任意 ID，但 manifest 中已声明的候选模型没有在表单中形成可发现建议。
- 状态：`verified（仓库）`。
- 计划修改：更新 DashScope provider manifest 及版本；为 customizable 模型输入增加目录建议但保留自由输入；增加后端 manifest/API 与 React 行为回归，同步 Provider 设计文档并重建生产 bundle。
- 实际修改文件：`app/providers/dashscope/provider.json`、`app/web/src/features/models/Page.jsx`、`app/web/src/App.test.jsx`、`tests/test_model_configuration_v2.py`、`docs/model-provider-plugins.md`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-{7m08dfhY.js,FhSZ_GOI.css}`、本日志。
- 实际实现：根据阿里云百炼官方模型资料确认 `qwen-plus` 是真实可用的官方模型 ID，保留为默认项并把展示名改为“千问 Plus（官方模型 ID）”。DashScope manifest 升级为 `0.5.0`，新增千问 3.8 Max/Flash、3.7 Plus/Flash、Flash、Turbo、Long 和 3 Coder Plus，以及百炼托管的 DeepSeek V4 Pro/Flash、GLM-5.2、Kimi K2.7 Code、MiniMax M3 和 MiMo V2.5 Pro。托管第三方模型默认使用集中 Prompt 约束 + 网关 Schema 校验，不假定它们原生支持 OpenAI JSON Schema。React 对 customizable 模型类型使用 datalist 展示 manifest 建议，选中已知模型时同步可读名称，同时继续允许已授权的快照或新模型 ID；新建配置仍必须独立测试成功才能用于正式路由。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_model_configuration_v2.py tests/test_provider_registry.py tests/test_dashscope_provider.py` 为 `30 passed`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `236 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `38 passed`；`npm run build` 成功生成 `index-7m08dfhY.js/index-FhSZ_GOI.css`；`git diff --check` 通过。
- 未完成事项或恢复说明：未使用真实百炼 API Key 发起付费调用；具体区域、业务空间授权和模型可用性仍由“测试模型”探针确认。首次 React 新回归直接设置受控 input 的 DOM value，未触发 React 状态更新而失败 1 条；改用原生 value setter 模拟真实输入后全量通过，该失败仅影响测试代码。一次从 `app/web` 目录读取根目录相对路径 `docs/change-log.md` 返回文件不存在，回到仓库根目录后读取成功。两次失败均无业务数据或外部调用副作用。

## 2026-08-31 · KB-SPEECH-SWITCH-CANCEL-UX-001

- 目标：语音整库构建期间切换模型/声音时，原子取代旧构建、协作取消旧子任务并启动新 revision，不再因后台进度高频推进 KnowledgeBase version 而暴露乐观锁冲突；为整库/单题失败重试提供可见的转圈与防重复提交状态。
- 关联问题：客户端最多两次“重读 version 再提交”仍可在多个 TTS 子工作密集完成时连续撞上 CAS；服务端缺少“期望 speech profile revision”的语义 CAS。旧 revision 虽有迟到结果 guard，但未主动取消 pending/failed 子任务。已有重试按钮没有本地 busy/spinner，快速重复点击时反馈不清晰。
- 状态：`verified（仓库与本机运行态）`。
- 计划修改：扩展 speech-profile 命令的语义 revision guard；在同一事务取消旧 revision 未完成工作并审计 supersede 关系；修正 SpeechBuild cancelled 投影；增加整库/单题重试 busy spinner；补并发、取消、迟到结果及 React 防重回归，同步 API/领域/存储/进度文档和生产 bundle。
- 实际修改文件：`app/schemas/api.py`、`app/api/routers/catalog.py`、`app/services/{knowledge_base_speech,catalog}.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-{Cvf-GtxJ.js,FhSZ_GOI.css}`、`tests/test_knowledge_base_speech.py`、`docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与诊断：API 日志确认同一页面先有一次 speech-profile `PUT 202`，随后密集出现四次 `409 PERSISTENCE_CONFLICT`，之后再次 `202`；根因是旧页面用会被语音子任务进度推进的 KnowledgeBase 通用 version 保护“切换语音配置”命令。现新增 `expected_speech_profile_revision` 语义 CAS：仅后台进度导致通用 version 变化时允许切换，真实的并发模型/声音配置变更仍返回冲突。切换在同一事务中创建新 revision、新整库构建并对旧 revision 的 pending/failed/running 父子工作分别执行取消或 `cancel_requested`；供应商调用迟到后在资产提交前再次校验取消标志、Question source version 和当前 profile revision，旧结果只记为 superseded，不覆盖新语音。页面配置弹窗明确提示会停止旧任务；整库和单题人工重试都增加 spinner、禁用与严格防双击；活动构建自动刷新进度。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `235 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `37 passed`；`npm run build` 成功生成 `index-Cvf-GtxJ.js/index-FhSZ_GOI.css`；`git diff --check` 通过。新增后端回归覆盖“旧通用 version + 当前 profile revision”切换成功、旧 running 工作收到取消、旧构建投影为 superseded、真实旧 profile revision 仍 409；前端回归覆盖整库/单题重试期间 spinner、disabled 和双击仅发一个请求。本机 API 已重启为 PID `10336`，`GET /healthz` 返回 ok，首页引用新 bundle；Celery 已重启并连接 Redis DB 2，dispatcher 返回 `dispatched: 0`。目标题库 `kb_a9f8e46e4bdf4d20` 当前为 speech profile revision 7、Qwen/Cherry，投影 `ready 10/10`、失败数 0。
- 未完成事项或恢复说明：无代码未完成事项。首次 React 回归因编辑时多出一个 `};` 发生语法失败，删除后全量通过；一次新增后端断言误选了最后一条 HTTP 审计而不是目标领域审计，改为按 action 查找后通过；两次失败均仅发生在测试环境，无持久数据副作用。浏览器若仍缓存旧 bundle，需要强制刷新后再验证新交互。

## 2026-08-31 · DURABLE-MODEL-RETRY-PROJECTION-001

- 目标：修复模型调用发生可重试异常时，持久任务尚未耗尽却提前把领域对象和批量构建投影标记为终态失败的问题；确保修复依赖统一 Outbox 终态语义，而非绑定 Qwen、GLM 或某一模型类型。
- 关联问题：题库 TTS 子任务首次失败后会被 Outbox 自动重试，但 Question 同时写入 `speech_status=failed` 并推进 version；下一次供应商调用即使成功，也会因 source version 过期被判为 `superseded`，形成 6/10 生成且无法自然恢复。构建投影还把仍可 claim 的 `failed` 工作误计为终态失败。
- 状态：`verified（仓库与本机运行投影）`。
- 计划修改：以 Outbox 返回的 `dead_letter` 作为领域失败唯一判据；可重试 `failed` 保持领域对象生成态，并在构建投影中归入 pending/retrying；修正题目行级展示与试听能力；审计其他模型驱动任务是否存在相同的提前终态写入；补后端/前端回归、生产 bundle 和相关设计文档。
- 实际修改文件：`app/services/{catalog,interviews,knowledge_base_speech,talent}.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`tests/test_{knowledge_base_speech,interview_session_aggregate}.py`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-kBIiQffi.js`、`docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与诊断：持久 SQLite 显示 revision 5 的 10 个 Qwen TTS 子工作中，4 个首轮错误为 `Provider rate limited the request.`，Outbox 正确启动第 2 次 attempt；旧代码却先把这 4 道 Question 写成 failed 并把 version 9 推进到 10，导致成功重试被 source-version guard 判为 superseded，最终稳定为 6 ready / 4 failed。现在仅 `dead_letter` 可推进领域失败；可重试 `failed` 在 SpeechBuild 计为 pending，TTS 不修改 source Question，题库导入、Resume Review/经历题、评分和报告同步使用该终态门禁。整库人工重试以当前 build manifest 与 Question failed 真相交集选题，因此能恢复旧 worker 已误标 completed/superseded 的 4 条历史数据。React 以 KnowledgeBase 终态显示整库重试入口，构建期间不再禁用已 ready 题目的试听。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `234 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `37 passed`；`npm run build` 成功生成 `index-kBIiQffi.js/index-cXhBMl4B.css`；`git diff --check` 通过。新增故障注入回归验证任意 Provider 的 retryable TTS 首次失败后 Question version/source_version 保持一致，第 2 次 attempt 产生 ready 资产且不是 superseded；另有回归验证历史 self-superseded 子工作可新建重试批次。本机重启 API 为 PID `5799`，`GET /healthz` 返回 ok，首页已引用 `index-kBIiQffi.js`，当前 revision 5 投影为 `6 ready / 4 superseded`而不再假装运行中，KnowledgeBase 仍如实保留 `4 failed` 供页面显示恢复按钮。旧 Celery 主进程 PID `2781` 已优雅停止，新 worker/beat 主进程 PID `6585` 以 prefork concurrency 10 连接 Redis DB 2，连续 dispatcher 均返回 `dispatched: 0`，确认未隐式触发任何外部模型任务。
- 未完成事项或恢复说明：未自动提交用户现有 4 条失败语音的真实 Qwen 重生成，避免未经用户点击就消耗外部模型额度；页面硬刷新后可点击“重试失败语音”只排队这 4 道。首次前端测试在仓库根目录执行因无 `package.json` 返回 ENOENT，一次定向 pytest 引用不存在的 `test_mvp_closed_loop.py` 而未运行；两者均无文件/数据副作用，在正确目录/测试集重跑后全部通过。

## 2026-08-31 · MODEL-AGNOSTIC-CONCURRENCY-001

- 目标：把模型任务的版本冲突防护从题库 TTS 个案提升为供应商无关、模型类型无关的统一并发合同，覆盖 LLM、Embedding、STT、TTS、实时语音和数字人配置，以及当前有人工命令的模型驱动异步工作流。
- 关联问题：模型/连接探针和后台模型工作会合法推进 ModelConfiguration、ProviderConnection、QuestionGenerationBatch、ResumeReview、Question 等聚合 version；页面弹窗或确认框若长期持有旧 version，会产生与具体供应商无关的 `PERSISTENCE_CONFLICT`。部分命令已有幂等回放优先检查，但前端获取最新 version 和语义变更 guard 仍散落在业务页面。
- 状态：`verified（仓库）`。
- 计划修改：增加通用 latest-version command helper；模型连接/所有模型类型配置的编辑删除、智能生题控制/审核/导入、简历初筛重试和题库语音命令统一采用最新资源 version，并在语义身份变化时失败关闭；审计服务端人工重试是否在 version 校验前识别同一幂等命令；补跨模型类型回归、文档和生产 bundle。
- 实际修改文件：`app/web/core/{concurrency.js,concurrency.test.js}`、`app/services/{model_admin,talent,catalog,knowledge_base_speech}.py`、`app/api/routers/talent.py`、`app/web/src/features/{models,questions,workflow}/Page.jsx`、`app/web/src/App.test.jsx`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-z_CNemdA.js`、`tests/test_{model_configuration_v2,candidate_screening,documented_gap_apis,knowledge_base_speech}.py`、`docs/{architecture,api-design,domain-model,model-provider-plugins,database-and-vector-storage,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与审计：新增供应商/能力无关的 `requestWithLatestVersion`，统一执行“重读最新资源 → 比较命令相关语义身份 → 用最新 version 提交 → 极窄 CAS 冲突再重读一次”。ProviderConnection/ModelConfiguration 增加 `configuration_revision`，配置修改递增，凭据/模型健康探针只推进通用 version；管理页的连接/模型编辑和删除因此覆盖全部当前可执行 `llm/embedding/stt/tts/realtime_speech/avatar` 类型，而非 Qwen/GLM 特例。智能生题 stop/resume/retry/chunk、草稿编辑/删除/单题及整批导入按 execution/draft 身份吸收后台 LLM 进度 version；题目、候选人、简历、初筛复核/重试按各自内容身份处理后台 TTS/摄取/LLM 写入。服务端人工恢复审计确认 QuestionGenerationBatch 已在 version 前检查控制幂等；补齐 KnowledgeBaseSpeechBuild、Question speech regenerate 和 ResumeReview retry，相同命令返回原工作，不同旧命令继续失败关闭。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `232 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `36 passed`；`npm run build` 通过并生成 `index-z_CNemdA.js/index-cXhBMl4B.css`；`git diff --check` 通过。参数化后端回归逐一执行 LLM、Embedding、batch STT、TTS、数字人和 realtime speech 探针，均验证通用 version 增加但 `configuration_revision` 保持 1，配置 PATCH 后才变为 2；前端单元回归覆盖运行态 version 吸收、读写间单次重试和配置语义变化失败关闭，React 集成回归验证连接探针从 version 3 推进到 5 后删除提交 version 5。
- 未完成事项或恢复说明：仓库内无未完成项；未对真实外部供应商发起调用，额度、网络、音质、WER 和数字人会话仍属部署环境验收。首次在仓库根目录串接 npm 命令因无 `package.json` 返回 ENOENT，无文件副作用；切换 `app/web` 后全量通过。本合同覆盖当前 API/React 中存在人工 CAS 命令的模型驱动流程；未来新增聚合命令必须复用该 helper 与“幂等回放先于 version 校验”合同。

## 2026-08-31 · KB-SPEECH-CONFLICT-RETRY-001

- 目标：修复题库语音配置弹窗因后台语音构建推进 KnowledgeBase version 而提交陈旧 `expected_version` 的冲突，并让整库/单题语音生成失败后可从题库详情真正重新排队生成。
- 关联问题：用户选择 Qwen TTS 时请求携带 version 54，而持久题库已由后台进度更新到 56；已有失败重试会复用 `question.speech:{question}:{version}:{profile_revision}` 幂等键，可能再次得到原 failed/dead-letter 工作项而没有新的可执行任务，React 题库页也没有暴露现有整库或单题重试 API。
- 状态：`verified（仓库）`。
- 计划修改：配置提交前重新读取 KnowledgeBase，仅在 speech profile 未发生并发语义变更时采用最新 version；极窄竞态最多重新校验并重试一次。整库失败重试创建新的工作身份，单题失败暴露既有重新生成命令，补充失败项投影与 React 重试入口；更新 API/领域/存储/进度文档、自动化测试和生产 bundle。
- 实际修改文件：`app/services/knowledge_base_speech.py`、`app/web/src/features/questions/Page.jsx`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-B7aTUrcS.js`、`tests/test_knowledge_base_speech.py`、`app/web/src/App.test.jsx`、`docs/{api-design,domain-model,database-and-vector-storage,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与诊断：持久 SQLite 事实显示 Qwen profile revision 3 已保存，10 个题目语音子工作最终全部 completed；报错请求仍携带页面旧 version 54，而后台构建已把 KnowledgeBase 推进到 56，因此该次失败是 CAS 防覆盖生效，不是 Qwen/Cherry 被 Provider 拒绝。React 保存前重读题库，只在 profile 语义身份未变时吸收新 version，极窄竞态按相同 guard 重试一次；真正并发修改 profile 时刷新并要求确认。SpeechBuild 投影返回 `failed_items`，题库/题目分别提供失败重试按钮；整库重试使用当前 failed Question version，并以 retry parent 区分新子工作，避免旧 source version 被 guard 跳过或复用 dead-letter 幂等项。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `225 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `34 passed`；`npm run build` 通过并生成 `index-B7aTUrcS.js/index-cXhBMl4B.css`；`git diff --check` 通过。新增后端回归验证失败落库推进 Question version 后，重试 manifest 使用当前 version、新 child 使用 `question.speech.retry:*` 且最终 ready；React 回归验证页面 version 54、后台 version 56 时提交 56，以及整库/单题失败重试请求。首次在仓库根目录执行 npm 测试因无 `package.json` 返回 ENOENT，无文件副作用；切换 `app/web` 后通过。
- 未完成事项或恢复说明：仓库内无未完成项；本次未调用真实 Qwen/GLM TTS，也未启动本地 8000 服务。目标部署仍需用真实凭据验证音频质量、限流和网络稳定性；这些外部验收不改变本次 CAS/重试根因与仓库修复。

## 2026-08-31 · APPOINTMENT-DEFERRED-RESUME-SPEECH-001

- 目标：统一岗位题与简历经历题的读题语音特征；简历题批准时不再调用 TTS，改为计划冻结所选题库的唯一语音特征，候选人完成身份核验和明确同意、预约进入 `registered` 后才异步生成本场简历题语音。
- 关联问题：ExperienceQuestion 当前使用 `voice_default_cn` 和组织默认 TTS 路由，不能保证与题库 KnowledgeBaseSpeechProfile 一致；批准即生成会为拒绝邀请或不参加面试的候选人产生无效成本。现有邀请与开始共用语音 readiness，若只移动调用时机会造成邀请前等待尚未触发的语音任务。
- 状态：`verified（仓库）`。
- 计划修改：为 InterviewPlan 冻结单一语音特征并拒绝多题库 profile 冲突；ExperienceQuestion 批准后进入延迟生成状态；CandidateIntake 成功时原子创建预约范围语音工作，复用匹配资产；拆分 `can_invite/can_start` 门禁并在取消预约时协作取消未完成工作；会话只消费预约准备完成的冻结资产。同步后端/前端测试及架构、接口、领域、检索、Provider、存储、进度、路线图和统一语言文档。
- 实际修改文件：`app/domain/{speech_profile,appointment_speech,appointment_admission}.py`、`app/services/{plan_assembly,talent,appointments,catalog,interviews}.py`、`app/web/src/core/ui.jsx`、`app/web/src/features/{workflow,candidate}/Page.jsx`、重建后的 `app/web/dist/index.html` 与 bundle、`tests/test_{plan_assembly,appointment_reminders,candidate_screening,position_resume_appointment_flow}.py`、`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现：ExperienceQuestion 新建为 `not_requested`、批准为 `deferred`，不再保存默认音色或在批准请求中调用 TTS。InterviewPlan 冻结包含题库 revision 映射的唯一 speech profile 指纹并拒绝多题库冲突；Candidate Intake 成功事务创建预约级幂等语音工作，worker 只更新 `InterviewAppointment.speech_preparation`，按完整 profile 复用资产且不覆盖 ExperienceQuestion。邀请/start readiness 分离，取消预约协作取消工作；会话创建只接受预约 ready 资产并冻结 profile/preparation 证据。React 同步展示“预约后生成”和确认后的语音准备提示。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `224 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `33 passed`；`npm run build` 通过并生成 `index-VnSd0Y0L.js/index-cXhBMl4B.css`；`git diff --check` 通过。端到端测试验证确认前无简历题 TTS、确认后 queued、完成前禁止 start、完成后资产的模型/version/音色/语言/格式/语速/题库 revision 指纹与计划一致，且会话快照引用预约资产。
- 未完成事项或恢复说明：无仓库内未完成项；本次未执行数据迁移或真实外部 TTS 调用。真实供应商费用、延迟、失败率和目标部署 worker 调度仍按既有生产环境验收边界执行。

## 2026-08-31 · MODEL-STREAM-PROBE-001

- 目标：修复 DashScope `stt.streaming` 模型配置测试把静音样本误判为模型故障，以及 `speech.dialogue_realtime` 缺少统一测试协议的问题；同步校正 Qwen 3.5 Omni Realtime 当前官方 session 事件结构。
- 关联问题：`qwen-audio-3.0-asr-flash-streaming` 已完成鉴权和 `task-started` 仍因静音没有 final transcript 而失败；`qwen3.5-omni-flash-realtime` 在发起厂商调用前直接返回 `MODEL_CAPABILITY_NOT_IMPLEMENTED`。旧 Qwen session payload 还使用兼容字段、旧输入转写模型名和不适用于 Qwen 3.5 的默认音色。
- 状态：`verified（仓库与本机）`。
- 设计边界：模型配置/路由测试验证真实端点、凭据、模型授权及 session 握手，返回 `probe_mode=handshake` 后主动关闭，不把静音伪装成识别质量样本；WER、final、首音、音质和打断仍由真实录音/面试端到端验收。Realtime Prompt 继续由 `app/core/prompt/` 提供，Provider 只转换厂商协议。
- 实际修改文件：`app/services/model_admin.py`、`app/providers/realtime_speech.py`、`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、`tests/test_model_configuration_v2.py`、`tests/test_dashscope_provider.py`、`docs/api-design.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、本日志。
- 实际实现：模型配置测试和 route 测试共享 `_probe_stream_handshake`，`stt.streaming` 与 `speech.dialogue_realtime` 只消费统一 ready 事件并安全 abort；补齐实时语音对话探针请求。DashScope Qwen 3.5 session 改为嵌套 PCM format，输入转写默认 `qwen3-asr-flash-realtime`、音色默认 `Tina`，历史 `qwen3-asr-flash`/`Cherry` 在 Provider seam 兼容；每个受控追问先更新 session instruction，再提交音频并创建 response。
- 验证命令与结果：定向 Realtime/DashScope/模型配置回归 `23 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `221 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `33 passed`；`npm run build` 通过并生成 `index-DPD89s-E.js/index-cXhBMl4B.css`；`git diff --check` 通过。重启 API 后，对 `model_cfg_47b17fd770184f74` 的真实 DashScope 测试返回 `probe_mode=handshake + stream.ready`，对 `model_cfg_4b6f183ae80c44e3` 返回 `probe_mode=handshake + dialogue.ready`；两项 ModelConfiguration 均已保存为 `ready`。
- 未完成事项或恢复说明：本次真实探针只确认百炼端点、当前 API Key、模型授权和 session 参数可用，不声称已完成 WER、真实 final/首音延迟、打断、音质、长连接稳定性或费用验收；这些仍需脱敏语音样本和候选人端到端压测。既有 API Key 未输出、未改写。

## 2026-08-31 · MODEL-FORM-HELP-UX-001

- 目标：在模型服务动态表单的字段标签旁增加圆形问号帮助入口；为阿里云百炼实时语音对话 WebSocket 等容易误填的模型配置字段提供鼠标悬停/键盘聚焦说明，降低管理员配置成本。
- 关联问题：用户在添加 DashScope `realtime_speech` 模型时不知道 `Realtime WebSocket` 字段用途和填写规则。
- 状态：`verified`。
- 实际修改文件：`app/web/src/core/ui.jsx`、`app/web/styles.css`、`app/web/src/features/models/Page.jsx`、`app/providers/dashscope/provider.json`、`app/web/src/App.test.jsx`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-DPD89s-E.js/index-cXhBMl4B.css`、本日志。
- 实际实现：`Field` 的 `hint` 统一渲染为标签旁可聚焦圆形 `?`，hover/focus 时显示 tooltip，并保留 `title/aria-describedby/role=tooltip`。DashScope `workspace_id` 与 `realtime_speech` 模型配置字段补充面向管理员的填写说明，明确 `Realtime WebSocket` 是 Qwen Realtime 服务端地址、何时可留空、北京/新加坡地址格式以及系统会自动追加模型参数。模型服务页同步把 `speech.dialogue_realtime` 和 `realtime_speech` 显示为中文能力/类型名称。
- 验证命令与结果：`cd app/web && npm test -- --run`：`33 passed`；`cd app/web && npm run build`：通过，生成 `index-DPD89s-E.js/index-cXhBMl4B.css`；浏览器实测 `http://127.0.0.1:5173/web/#models/provider_conn_42860516bcdd45a0` 添加 realtime 模型时，`Realtime WebSocket` 标签旁出现 `?`，tooltip 从隐藏变为可见并展示新说明；`curl /api/v1/admin/model-provider-connections/provider_conn_42860516bcdd45a0/model-catalog` 已返回 `help` 文案；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_model_form_help_pycache .venv/bin/python -m compileall -q app` 通过；`git diff --check` 通过。
- 未完成事项或恢复说明：无。此变更只影响动态表单展示文案和生产 bundle，不新增 API、数据结构、模型路由或供应商调用逻辑，因此无需更新接口、领域模型或路线图状态。

## 2026-08-30 · REALTIME-SPEECH-DIALOGUE-001

- 目标：在保留现有固定题、流式/批量 STT、TTS、自研/云数字人和完整评分链路的前提下，新增供应商无关的实时语音对话（S2S/STS）能力；接入 OpenAI Realtime 和阿里云百炼千问 Realtime，评估火山引擎豆包实时语音并在无法完整实现其官方二进制协议时保留明确 TODO；实现低延迟、受预算约束、可审计的动态澄清追问，并修复候选人实际面试中的 PCM 录音、断线修复、自动播题、状态同步、心跳、媒体权限和重复提交问题。
- 关联问题：动态追问元数据未进入 execution、评分建议没有运行时消费者；评分/报告同步阻塞下一题；浏览器 `audio/pcm` 与录音存储 MIME 合同冲突；STT 断线未按文档执行 batch repair；企业 `/live` 收不到正常转写/评分广播；数字人不自动主持；候选人忽略会话/题目事件且没有心跳；`record_video=false` 仍强制摄像头；读题与录音可并发、实时与降级提交语义不一致。
- 状态：`verified（仓库）`；真实厂商联调为 `environment_pending`，豆包 adapter 为显式 `TODO/not implemented`。
- 设计边界：实时语音对话只负责回合检测、低延迟澄清追问和音频输出；权威转写、题目快照、CandidateAnswer、异步完整评分与报告继续作为证据链真相。追问是原题的子轮次，必须有父轮次、原因、目标关键点、深度/总量/时间预算，不能独立增加计划权重或根据受保护属性改变难度。Provider 凭据只通过现有 ProviderConnection/ModelConfiguration/ModelRoute 注入，仓库默认留空。
- 计划修改：先新增统一实时语音对话 schema、stream interface、路由能力和 Provider manifest/adapter；随后扩展 Plan Assembly、Interview Lifecycle、Interview Service、WebSocket 与候选人/企业 React runtime；补齐 Prompt 合同、离线 Provider 合同、生命周期/端到端/前端测试，并同步架构、API、领域、检索评分、Provider、存储、问题、进度、路线图、统一语言及 ADR。
- 实际修改文件：
  - 统一能力、Schema 与 Prompt：`app/model_gateway/{capabilities,schemas,gateway,registry,dialogue}.py`、`app/core/prompt/realtime_dialogue.py`。
  - Provider：新增 `app/providers/openai/` 与共享 `app/providers/realtime_speech.py`；扩展 `app/providers/{dashscope,mock}/provider.py` 和 manifest。OpenAI 实现官方 HTTP Chat/Embedding/TTS/batch STT 与 Realtime WebSocket；DashScope 实现 Qwen Realtime 路由与 PCM 事件归一化；火山引擎未修改为 implemented。
  - 领域与编排：`app/domain/interview_lifecycle.py`、`app/services/{evaluation,interviews,streaming_stt,plan_assembly,appointments}.py`、`app/workers/outbox.py`、`app/schemas/api.py`。新增父子追问轮次/预算、确定性批准、异步评分、状态/事件和 `cascade/s2s` 预约设置。
  - 媒体与 Web：`app/adapters/{local_media,private_media}.py`、`app/transport/realtime.py`、`app/api/routers/realtime.py`、`app/web/candidate/{pcm-stream,runtime}.js`、`app/web/src/features/candidate/Page.jsx`、预约表单、行为测试、样式和重建后的 `app/web/dist/`。
  - 测试：新增 `tests/test_{realtime_speech_dialogue,openai_realtime_provider}.py`；扩展 DashScope、实时媒体、私有候选人媒体、生命周期、预约长流程和 MVP 闭环测试。
  - 文档：`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 实际实现：同一候选人 PCM 可并行进入权威 STT 与可选 S2S 表达轨；S2S 只流式逐字播报服务端已批准的追问，不自行决定问题，也不成为评分证据。权威 STT final 原子保存 CandidateAnswer 与 `answer.evaluate` 工作项后立即返回；worker 完整评分并追加 evaluation/report revision。追问固定零权重、深度 1、每根题最多 1 次、全场默认最多 2 次，并检查文本长度与剩余时间。浏览器实现 PCM delta 播放、完整本地备份、断线 batch repair、自动播题、心跳、媒体权限降级和防重复；角色投影不泄漏追问目标、标准答案或评分。Realtime transport 还把 commit 超时/断连统一映射为可观察 ProviderError；任何已输出音频必须同时提供可与批准题干比对的 final transcript，否则立即关闭 S2S 并走 cascade。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_realtime_dialogue_pycache .venv/bin/python -m compileall -q app tests` 通过；Realtime/OpenAI/媒体定向回归为 `12 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 最终为 `220 passed, 5 skipped in 10.02s`；`cd app/web && npm test` 为 `32 passed`；`npm run build` 成功，生成 `index--8cvjCid.js/index-Kmpcg04A.css`；`git diff --check` 通过。
- 失败与恢复留痕：首次前端测试误加 Vitest 不支持的 `--runInBand` 参数，去掉后通过；首次后端全量回归有 1 个旧 MVP 断言仍同步读取 score，按新合同改为验证 `pending/work_item_id`、执行 worker 后读取 append-only evaluation，复跑全量通过。一次只读 `rg` 命令中的 Markdown 反引号被 shell 当成命令替换并报告 `command not found: 106`，未写入仓库或业务数据，随后使用安全查询完成检查。首次暂存审计发现 4 个拆分后的 router 文件末尾多一个空白行，`git diff --cached --check` 失败；删除多余空白、重新暂存后通过，不影响业务数据。
- 未完成事项或恢复说明：OpenAI、阿里云和火山引擎真实凭据、区域/模型权限、实际首音、打断、网络抖动、费用与音质尚未提供；OpenAI/DashScope route 在管理员完成连接、探针和目标环境验收前不得视为生产 ready。火山引擎豆包虽有官方端到端实时语音能力，但其二进制 StartConnection/StartSession adapter 和“严格保持批准追问文本”尚未完成，本轮明确保留 TODO，没有伪造兼容实现或开放活动 route。

## 2026-08-29 · QUALIFIED-RESUME-QUESTIONS-UX-001

- 目标：只为生效初筛结论为“符合”的候选人生成简历问答；AI 不符合/待复核时不生成，人工复核改为符合后再排队生成。每道 AI/人工题必须绑定来自该份 ResumeReview 的具体简历证据快照。将个人题库从初筛报告长弹窗移出，在候选人表格外层提供“简历问答”弹窗入口，编辑/删除收入题目三点菜单。
- 关联问题：现有 Resume Review 无论符合与否都在同一模型响应中生成并持久化 ExperienceQuestion，且 `evidence_refs` 可为空；React 把 CandidateQuestionBank 放在初筛/简历详情弹窗中部，查找成本高，题目操作按钮又全部平铺。
- 状态：`verified`（仓库与本地运行时）。
- 设计决策：将经历题生成从 Resume Review 最终响应拆为独立的 `resume.experience_questions.generate` 持久工作，复用已配置的 `resume_experience_question_generation` purpose；候选人生效结论不是 `qualified` 时不排队，转为符合时原子排队。生成 module 只接受审阅中的项目/技能证据，Schema 强制每题至少一个精确证据标签，持久前再解析为不可变证据快照。
- 实际修改文件：`app/core/prompt/contracts.py`、`app/providers/mock/provider.py`、`app/services/{resume_review,talent,plan_assembly}.py`、`app/workers/outbox.py`、`app/schemas/api.py`；`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css` 及重建后的 `app/web/dist/`；`tests/test_{candidate_screening,prompt_governance,resume_review_pipeline,position_resume_appointment_flow}.py`；同步 `CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 实际实现：Resume Review 升级为 `resume_review.v6` / `resume_review_reduce.v4`，只返回证据与初筛，彻底移除同响应经历题；只有生效结论 `qualified` 才原子排入独立 `resume_experience_question_generation.v1` 工作，AI 不符合/待复核停在资格门禁，人工改判符合或重试失败/存量未生成状态才排队。AI Schema 强制 1–3 题和精确 evidence label，Talent module 继续校验引用归属、题干点名并冻结证据文本/来源页；人工新建/编辑同样必须提交证据标签并在题干写出标签。读取、语音和计划装配过滤不符合审阅、无证据快照或题干未点名的存量题；冻结计划同时保留 `evidence_refs`。React 从初筛详情移除题库，在候选人表格增加独立“简历问答”弹窗入口和生成状态；新建表单必须选择简历依据，卡片展示依据，题目及候选人的编辑/删除收入三点菜单。
- 验证命令与结果：定向资格/Prompt/长流程回归最终 `24 passed in 2.25s`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `212 passed, 5 skipped in 9.26s`；前端 `npm test -- --run` 为 `32 passed`；`npm run build` 成功；`compileall` 与 `git diff --check` 通过。停止旧服务 PID `23678`，最终以最新代码启动 PID `28105`；`GET /healthz` 为 200，用户指定候选人的个人题库 API 为 200 空集合，符合其当前生效结论 `unqualified` 和 `question_generation_status=not_eligible`；首页实际加载新 bundle `index-C3iURwrH.js/index-Kmpcg04A.css`。
- 失败与恢复留痕：首次定向长流程测试仍假定审阅完成后无条件已有题，改为显式人工复核符合并运行独立工作；首次 React 断言把折叠菜单中仍存在但不可见的 DOM 按钮误判为平铺按钮，改为验证 details 默认关闭、点击三点后展开。两次均只影响测试断言，没有业务数据副作用。文档批量补丁有两次因上下文不匹配未应用，随后拆成小补丁完成，没有产生部分写入。
- 未完成事项或恢复说明：没有自动调用付费真实模型为历史合格审阅补生成问题；再次“复核为符合”会为旧的未生成状态排队。真实 DeepSeek 的新问题质量、时延和费用仍为 `environment_pending`。上传失败日志已确认是旧调用 `deepseek-v4-pro` 在 6000 输出 token 中消耗 4081 reasoning token 后 `finish_reason=length` 导致 JSON 截断；该历史审阅后来已恢复为 `ready_for_review`，本轮没有重放付费审阅。

## 2026-08-29 · RESUME-REVIEW-TRUNCATION-001

- 目标：在保留最新 API router/transport 拆分的前提下，修复真实简历审阅因模型输出用尽限额、JSON 未完整而失败的问题；重启本地 API 使新增候选人个人题库路由实际生效。
- 关联问题：`candidate_400e16c5c49f470a` 的个人题库源码路由已存在但 8000 端口运行旧进程，OpenAPI 未加载该路由；`resume_review_0324bc54908047cb` 的真实调用在 `deepseek-v4-pro` 上以 `finish_reason=length` 结束，6000 输出 token 中有 4081 reasoning token，只留下 4905 字符的未完整 JSON。
- 状态：`verified`（仓库与本地运行时）。
- 计划修改：为 Resume Review 最终结构化响应增加数量/长度上界和精简指令、提升 Prompt 版本并补充合同测试；同步检索/Provider 文档；完成后重启 8000 服务并用实际 HTTP 验证健康检查、OpenAPI 与个人题库路由。
- 实际修改文件：`app/core/prompt/contracts.py`、`app/services/resume_review.py`、新增 `tests/test_resume_review_pipeline.py`、修改 `tests/test_prompt_governance.py`；同步 `docs/architecture.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md` 和本日志。用户已有的 `app/api/routers/`、`app/transport/` 及统一响应改造全部保留。
- 实际实现：最终审阅 Prompt 升级为 `resume_review.v5` / `resume_review_reduce.v3`，对摘要、项目/技能证据、命中/缺失要求、告警和 1–3 个经历题同时增加 `maxItems/maxLength`。预算内单次阶段若返回 `provider_output_truncated`，流水线丢弃半截 JSON，自动复用全文的页感知 evidence Map/final Reduce，并记录 `map_reduce_after_output_truncation + fallback_reason`；其他 Provider 错误和 Map/Reduce 内部截断仍保持结构化失败，不做无界成本重试。
- 验证命令与结果：定向 Prompt/降级/初筛测试 `20 passed in 1.57s`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_resume_review_pycache .venv/bin/python -m compileall -q app tests` 通过；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `211 passed, 5 skipped in 9.22s`；`git diff --check` 通过。停止 2026-08-28 22:57 启动的旧 PID `10252`，以 `.venv/bin/python main.py` 启动最新代码 PID `23678`；`GET /healthz` 为 200，OpenAPI 已注册候选人个人题库 GET/POST，用户给出的 `GET /api/v1/candidate-profiles/candidate_400e16c5c49f470a/experience-questions` 实测为 200 并返回 4 道经历题。
- 未完成事项或恢复说明：未自动调用付费真实模型重放已失败的 `resume_review_0324bc54908047cb`，避免在没有用户明确确认时产生额外调用与费用；候选人页保留“重新初筛”入口，下次重试会使用本轮修复。目标 DeepSeek 账户的实际恢复质量、时延和费用仍为 `environment_pending`。

## 2026-08-29 · API-ROUTER-MODULE-SPLIT-001

- 目标：修正 `app/api/routes.py` 仍集中全部路由、且 transport 支撑实现混入 `app/api` 的结构问题；让 `app/api` 只承担按业务域组织的路由声明和总装。
- 关联问题：上一工作项虽然抽离了 response、module 定位和 realtime implementation，但仍把这些文件放在 `app/api`，并保留约 1340 行单体路由注册表，没有达到用户期望的维护性和目录清晰度。
- 状态：`verified`（仓库）。
- 计划修改：把 fields/responses、module locator、实时连接管理移动到 `app/transport/`；把路由按 system、admin、catalog、talent、plans、interviews、realtime 拆入 `app/api/routers/`；`app/api/routes.py` 只保留 router 总装；保持所有路径、状态码、字段和错误合同不变。
- 实际修改文件：新增 `app/api/routers/{__init__,system,admin,catalog,talent,plans,interviews,realtime}.py`；把 `app/api/dependencies.py` 移为 `app/transport/service_locator.py`、`app/api/realtime.py` 移为 `app/transport/realtime.py`、`app/api/responses.py` 与 `app/api/fields/` 移为 `app/transport/http/responses.py` 与 `app/transport/http/fields/`，并新增 transport package 初始化文件；把 `app/api/routes.py` 重写为 13 行总装入口；同步修改 `app/main.py`、`app/core/{auth,errors,rate_limit}.py`、`tests/test_api_responses.py`、`docs/{architecture,api-design,development-progress,change-log}.md`。
- 实际实现：141 个 HTTP/WebSocket route 声明按 system、admin、catalog、talent、plans、interviews、realtime 七个业务 router 物理拆分；总装入口只按原顺序 include 子 router。`app/api` 现在只含 route package 和总装，不再含 fields、response factory、module locator 或实时连接 implementation。新增结构合同测试约束 router 清单、总装文件规模和 transport 文件不得回流 `app/api`；所有 URL、状态码、公开字段白名单和错误格式保持不变。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_router_split_pycache .venv/bin/python -m compileall -q app tests` 通过；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `210 passed, 5 skipped`；OpenAPI 成功生成 `107` 个 HTTP path，应用共注册 `150` 个含静态资源/WebSocket 的 route；`npm test -- --run` 为 `32 passed`；`npm run build` 成功；`git diff --check` 通过。
- 失败与恢复留痕：首次结构测试发现移动源码后遗留了空目录 `app/api/fields/`，按目标结构用 `rmdir` 删除空目录后全量测试通过；首次 OpenAPI 单行检查因 shell 中 f-string 转义产生 SyntaxError，随后改用 `%` 格式化通过；一次在 `app/web` 工作目录误用根目录相对 `.venv/bin/python` 返回“文件不存在”，随后回到仓库根目录执行。三次失败均未修改业务数据或外部状态。
- 未完成事项或恢复说明：无仓库内遗留。现有未提交的 `CANDIDATE-QUESTION-BANK-001` 与 `API-RESPONSE-MARSHAL-001` 修改全部保留，未执行 reset、checkout 或清理。

## 2026-08-29 · API-RESPONSE-MARSHAL-001

- 目标：审查并改善 `app/api` 的维护性与可读性；参考 `zfsoft-agent-platform/api/fields` 的声明式白名单投影，为 FastAPI 建立统一 response/marshal seam，在不破坏现有成功响应兼容形状的前提下统一集合、异步和错误响应，并让公开候选人投影不再由路由手工拼字段。
- 关联问题：`app/api/routes.py` 同时承担 REST 路由、WebSocket 连接管理、module 定位和响应序列化；JSON 返回普遍使用 `Dict[str, Any]`，缺少可复用字段合同；业务错误使用 `error` 包络，而请求校验错误仍使用 FastAPI 默认 `detail`；同类列表和 202 响应存在重复拼装。
- 状态：`verified`（仓库）。
- 计划修改：新增 `app/api/fields/` 声明式字段与 marshal 实现、统一 `app/api/responses.py` 响应工厂和错误处理；迁移重复列表/异步响应及安全敏感公开投影；增加合同测试并更新接口设计与架构说明。
- 实际修改文件：新增 `app/api/dependencies.py`、`app/api/realtime.py`、`app/api/responses.py`、`app/api/fields/__init__.py`、`app/api/fields/core.py`、`app/api/fields/common.py`、`app/api/fields/public_interview.py`、`tests/test_api_responses.py`；修改 `app/api/routes.py`、`app/main.py`、`app/core/errors.py`、`app/core/auth.py`、`app/core/rate_limit.py`、`docs/api-design.md`、`docs/architecture.md`、`docs/development-progress.md` 和本日志。
- 实际实现：以 `ApiJSONResponse` 作为 API router 默认 JSON transport；`api_response/accepted_response/collection_response/error_response` 统一资源编码、202、`items + next_cursor` 和 `error.code/message/details`。声明式 Field/Nested/ListOf 支持 key/attribute、嵌套 source、默认值、类型转换和显式 allow-list；公开候选人详情与答题响应已用 fields 投影，路由不再手写评分裁剪。FastAPI RequestValidationError 固定映射为 `422 REQUEST_VALIDATION_FAILED`，仅返回字段位置、消息和类型，不回显原输入；Provider、Persistence、认证和限流复用同一错误工厂。ServiceLocator 与实时连接/Redis fan-out/候选人事件裁剪分别移出 routes，并在 routes 中增加领域分区标识。
- 兼容决策：没有给成功资源强加 `data` 二次包络，既有前端仍直接读取资源字段；列表只统一补齐 `next_cursor`，二进制/音频/CSV 不进入 JSON marshal。这样获得统一 seam 与字段白名单，同时避免对现有 `/api/v1` 客户端做破坏性版本迁移。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_api_response_pycache .venv/bin/python -m compileall -q app tests` 通过；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `209 passed, 5 skipped`；定向 API/主流程/实时/限流/鉴权/候选人回归为 `31 passed`；`npm test -- --run` 为 `32 passed`；`npm run build` 成功；`git diff --check` 通过。
- 未完成事项或恢复说明：仓库开始前已有 `CANDIDATE-QUESTION-BANK-001` 的代码、文档和前端 bundle 修改，本工作项全部保留，未执行 reset、checkout 或清理。`routes.py` 仍作为全部路径的注册表，后续可按 admin/catalog/talent/interview 物理拆成子 router；本轮已先抽离 response、依赖构造和 realtime 三个高变化 implementation，合同与运行行为无仓库内遗留。

## 2026-08-29 · CANDIDATE-QUESTION-BANK-001

- 目标：把现有 ResumeReview 自动生成的 ExperienceQuestion 提升为候选人可见、可维护的个人题库；保留 AI 基于简历项目/技术描述自动生成追问题的能力，补齐人工新建、候选人范围查询、编辑、批准/拒绝和归档删除，并让正式面试继续复用既有 `resume_experience` 计划、语音和评分链路。
- 关联问题：现有 ExperienceQuestion 已绑定 CandidateProfile 并能由 AI 生成、编辑和进入计划，但只能按 ResumeReview 查询，缺少候选人个人题库入口、人工创建和删除合同，React 候选人详情也未展示这些题目。
- 状态：`verified`（仓库）。
- 计划修改：先更新 API、领域模型和统一语言；随后扩展 ExperienceQuestion schema/TalentService/API，增加 CandidateQuestionBank React 管理区和后端/前端测试，重建生产 bundle；最后运行定向与全量验证并补充实际文件、结果和未完成事项。
- 不变量：CandidateQuestionBank 是 ExperienceQuestion 的候选人范围投影，不复制岗位 KnowledgeBase/Question；人工题必须绑定候选人及其 ResumeReview，评分依据完整后才可批准；归档删除不破坏已批准计划和历史 InterviewQuestionSnapshot；AI 草稿仍需人工批准后才能进入计划。
- 实际修改文件：
  - 合同与领域：`app/schemas/api.py`、`app/domain/enums.py`、`app/api/routes.py`、`app/services/talent.py`、`app/services/catalog.py`。
  - React 与生产 bundle：`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/index.html`、`app/web/dist/bundles/index-B_0U1sv3.js`、`app/web/dist/bundles/index-CWyy4anR.css`；构建替换旧 hash bundle `index-jppRnCoU.js` 和 `index-Nk8y8ETG.css`。
  - 测试：`tests/test_candidate_screening.py`。
  - 同步文档：`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、本日志。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_candidate_bank_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`205 passed, 5 skipped in 9.46s`。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_candidate_screening.py -x`：最终定向复验 `9 passed in 1.23s`；覆盖 AI/人工来源、候选人范围查询、新建、编辑、批准与语音 ready、拒绝、归档、审阅范围隐藏和审计。
  - `cd app/web && npm test`：`32 passed`；新增行为测试覆盖 AI 题展示、人工新建、批准和删除。
  - `cd app/web && npm run build`：通过，生产 bundle 已更新。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：个人题库复用现有 Resume Review Prompt，本轮未新增或修改业务 Prompt。真实简历追问题质量、真实 TTS 音质和目标模型/对象存储仍沿用既有 `environment_pending` 边界；仓库内功能无未完成项。首次直接调用 `python` 因当前环境仅提供 `.venv/bin/python` 而失败；首次从 `app/web` 使用根目录相对测试过滤 `app/web/src/App.test.jsx` 未匹配文件，改为 `src/App.test.jsx` 后通过。两次失败都未修改业务数据或产生额外仓库副作用。

## 2026-08-28 · DUAL-AVATAR-DELIVERY-001

- 目标：保留现有腾讯云数字人 WebRTC/SFU 链路并标注后续扩展 TODO；新增预约级“自研数字人 / 云数字人”选择。自研模式复用计划冻结的题目 TTS 私有音频，在浏览器本地完成形象渲染和口型状态，不创建云数字人会话；两种模式共用 Avatar Delivery interface、候选人播放 runtime、失败降级与会话清理。
- 关联问题：云数字人按会话/并发计费过高；现有预约只能隐式走单一数字人 route，无法按场次选择低成本本地渲染；冻结题目音频的内部 URI 不能直接作为候选人浏览器播放地址。
- 状态：`verified`（仓库）；高精度本地口型/3D 与腾讯真实环境均按下述边界继续扩展。
- 计划修改：预约 settings 合同与会话安全投影、Avatar Delivery 深模块、候选人本地数字人播放/动画、预约选择 UI、后端与 React 测试、生产 bundle，以及架构/API/领域/Provider/进度/路线图文档。
- 兼容与安全边界：不删除或改写腾讯云 Provider、WSS 命令通道、TCPlayerLite 页面和关闭接口；历史预约没有 `avatar_mode` 时继续按云模式解释。新预约默认自研模式但可显式选择云模式。自研模式只播放当前轮次冻结且已进入 PrivateFileStorage 的题目语音，签发短期受控地址；不暴露对象键、供应商凭据、标准答案或未来题目。
- 实际修改文件：
  - 合同与服务：`app/schemas/api.py`、`app/model_gateway/schemas.py`、`app/services/appointments.py`、`app/services/avatar.py`、`app/services/interviews.py`、`app/api/routes.py`。
  - React 与生产 bundle：`app/web/candidate/avatar-runtime.js`、`app/web/src/features/candidate/Page.jsx`、`app/web/src/features/interviews/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/candidate/avatar-runtime.test.js`、`app/web/dist/index.html`、`app/web/dist/bundles/index-jppRnCoU.js`；构建删除旧 hash bundle `index-BAySj2hk.js`。
  - 验收：`tests/test_realtime_media.py`、`tests/test_production_compatibility.py`。
  - 文档：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`CONTEXT.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、本日志。
- 验证命令与结果：定向 Python 合同/实时媒体 `8 passed in 1.64s`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/python -m pytest -q` 为 `204 passed, 5 skipped in 9.42s`；`cd app/web && npm test -- --run` 为 `31 passed`；`npm run build` 成功并生成生产 bundle；`git diff --check` 通过。测试覆盖预约默认/显式/非法模式、本地浏览器语音、本地冻结音频五分钟签名访问与审计、云不可用明确降级、统一前端音频/浏览器语音/WebRTC lifecycle、云 session close 和预约表单 payload。
- 未完成事项或恢复说明：自研模式当前是低成本 2D 浏览器形象、冻结 TTS 音频、呼吸/说话状态和音量条，不声称已有音素级嘴型或 3D 实时驱动；后续在保留 AvatarDelivery interface 下扩展 Live2D/3D、viseme 或自建 WHEP/SFU。腾讯真实账号、授权形象、并发、媒体质量和费用仍为 `environment_pending`，本轮未写入凭据或发起真实供应商调用。预约 settings 已是 JSON 文档，不需要数据库迁移。首次从仓库根目录执行 `npm test` 因该目录没有 `package.json` 返回 ENOENT，随后在 `app/web` 重跑通过；失败命令没有仓库或业务数据副作用。

## 2026-08-28 · CANDIDATE-BOOKING-REMINDER-001

- 目标：把候选人邀请页从“核验后立即开始面试”拆成“核验身份并确认预约”与“到预约时间后检查设备并进入面试”两步；预约确认后创建面试前 30 分钟的持久邮件提醒任务。
- 关联问题：候选人提前打开邀请链接时会被立即要求设备检查并尝试开始；预约缺少候选人确认态和邮件提醒。
- 状态：`verified`（仓库）；SMTP 真实投递为 `environment_pending`。
- 安全与运行边界：提醒工作项只保存预约 ID，不保存邮箱明文；SMTP 授权码只从环境变量读取且仓库默认留空；未配置时保留可重试事实，不记录或伪造发送成功。
- 实际修改文件：
  - 预约与提醒：`app/services/appointments.py`、`app/services/appointment_reminders.py`、`app/services/interviews.py`、`app/adapters/email.py`、`app/workers/outbox.py`。
  - 候选人页面与构建：`app/web/src/features/candidate/Page.jsx`、`app/web/styles.css`、`app/web/dist/index.html`、`app/web/dist/bundles/index-Cw9rJDVe.js`、`app/web/dist/bundles/index-By4Tl22c.css`。
  - 配置与说明：`.env.example`、`README.md`。
  - 测试：`tests/test_appointment_reminders.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_interview_session_aggregate.py`、`app/web/src/App.test.jsx`。
  - 同步文档：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_appointment_reminders.py tests/test_position_resume_appointment_flow.py -q`：`5 passed in 1.35s`。
  - `npm test -- --run`：首次新增邀请页测试的 GET mock 未匹配 HTTP client 显式的 `method=GET`，导致表单未渲染；修正 mock 后 `29 passed in 0.85s`，无业务数据副作用。
  - `npm run build`：通过，生产 bundle 已更新。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - 首次全量 pytest：`195 passed, 5 skipped, 1 failed`；旧断言要求所有 Outbox 均完成，未包含新增的预约提醒。补充预约消费时协作取消未发送提醒并收紧断言后重跑。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`196 passed, 5 skipped in 8.77s`。
  - 本地浏览器重载 `/#invite/{invalid-token}`：生产 bundle 正常挂载，公开邀请不可用投影正确，控制台无 error/warn；身份确认与不触发媒体/start 的状态迁移由 Vitest mock API 行为测试覆盖，未用真实候选人资料或改写现有预约。
  - 本地运行实例已加载最终代码：旧 8000 监听进程在检查期间已退出，使用 `.venv/bin/python main.py` 启动新 API（PID `87974`），`GET /healthz` 返回 `{"status":"ok"}`。
  - 启动 Celery Worker/Beat 前只读检查 claimable DurableWorkItem 为 `[]`。首次沙箱内启动因不允许连接本机 Redis 而失败并 warm shutdown，无任务副作用；获批后重新启动成功，Worker ready，连续 dispatcher 均为 `dispatched: 0`。Beat 按既有计划运行一次筛选留存扫描，结果 `candidate_count=0`、未删除候选人，但新增一条批次审计 `audit_b5ed4233b5334619`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：SMTP 主机、账号、发件地址和 `INTERVIEWER_SMTP_PASSWORD` 授权码按用户要求保持空白，未发送真实邮件。部署时填写 `.env.example` 对应变量并保持 Celery worker/Beat 运行，即可对随后到期的提醒执行真实投递；还需用目标邮件服务完成发件域名、退信、送达率和合规验收。

## 2026-08-28 · PLAN-INVITATION-UX-001

- 目标：去掉面试计划“创建后再由同一人审批”的重复操作，让 React 工作台的“生成计划”作为一次明确确认并原子产生已批准计划；在候选人邀请弹窗增加一键复制和成功/失败反馈。
- 关联问题：面试计划自审批交互冗余；邀请链接缺少复制操作。
- 状态：`verified`。
- 实际修改文件：
  - 计划生成合同与事务：`app/schemas/api.py`、`app/services/plan_assembly.py`、`app/api/routes.py`。
  - React 交互与样式：`app/web/src/features/plans/Page.jsx`、`app/web/src/features/interviews/Page.jsx`、`app/web/styles.css`。
  - 测试与生产 bundle：`tests/test_plan_assembly.py`、`app/web/src/App.test.jsx`、`app/web/dist/index.html`、`app/web/dist/bundles/index-B94I_kXD.js`、`app/web/dist/bundles/index-CW8Qvsa4.css`。
  - 同步文档：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - 首次从 `app/web` 目录组合运行后端与前端定向测试时，`.venv/bin/python` 因相对路径错误未找到；未执行测试、无仓库副作用，改回仓库根目录后重跑。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_plan_assembly.py -q`：`4 passed in 0.64s`。
  - `npm test -- --run`（`app/web`）：`28 passed in 1.00s`，覆盖生成即启用、无自审批按钮和一键复制邀请链接。
  - `npm run build`（`app/web`）：通过，生产 bundle 已更新。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`194 passed, 5 skipped in 8.53s`。
  - 本地浏览器验证 `http://127.0.0.1:8000/#plans`：页面无“审批计划”按钮；生成弹窗显示一次确认说明和“生成并启用计划”；控制台无 error/warn。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：无。API 默认仍生成 `draft`，供确实需要编辑或职责分离的客户端使用；React 工作台显式传入 `approve=true`，在同一事务完成就绪校验与启用。

## 2026-08-25 · CORE-FIX-001

- 目标：把问题与操作日志加入 AI 强制索引，修复并验证 `PLAN-001`、`CONSENT-001`、`APPOINTMENT-001`、`REPORT-001`、`SEARCH-001`。
- 关联问题：`PLAN-001`、`CONSENT-001`、`APPOINTMENT-001`、`REPORT-001`、`SEARCH-001`。
- 状态：`verified`。
- 实际修改文件：
  - 协作与记录：`AGENTS.md`、`docs/change-log.md`、`docs/known-issues-and-remediation.md`。
  - 计划与执行：`app/services/plan_assembly.py`、`app/services/plans.py`、`app/services/interviews.py`、`app/domain/question_selection.py`、`app/schemas/api.py`、`app/api/routes.py`。
  - 同意与预约准入：`app/services/appointments.py`、`app/domain/appointment_admission.py`。
  - 报告与搜索：`app/services/reports.py`、`app/services/catalog.py`、`app/persistence/interface.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/web/app.js`。
  - 同步文档：`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`。
  - 验收测试：`tests/test_known_issue_remediations.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_mvp_flow.py`、`tests/test_persistence_contract.py`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m pytest -q`：`64 passed in 2.41s`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：五个问题均达到本地 `verified`，但未标记 `closed`。旧 `items` 请求/存量迁移、旧 QuestionService 向量实验和管理员直建会话仍是兼容路径；真实 streaming STT/TTS、PostgreSQL 唯一约束与查询计划、RBAC、审计、签名媒体和 PDF 私有文件链路仍属于后续生产化里程碑。

## 2026-08-25 · DOC-GAPS-001

- 目标：实现强制索引文档中所有尚未完成的能力，补齐代码、API、适配器、迁移/兼容处理、测试和 UI，并按实际验收证据更新完成标记。
- 关联问题：里程碑 0-13 的全部未满足验收项；重点覆盖 Private File Storage、PDF 本地/URL 摄取、题库批量构建、缺失 CRUD、流式 STT/TTS、生产 readiness、RBAC/审计、签名访问、Outbox 加固、PostgreSQL 约束、导出、公平性评估和兼容路径清理。
- 状态：`verified`（仓库范围）。
- 实际修改文件：
  - 协作、领域语言与入口：`AGENTS.md`、`CONTEXT.md`、`README.md`。
  - 设计与进度：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/known-issues-and-remediation.md`、`docs/change-log.md`。
  - API、领域与 schemas：`app/api/routes.py`、`app/schemas/api.py`、`app/domain/enums.py`、`app/domain/interview_lifecycle.py`、`app/domain/appointment_admission.py`、`app/domain/question_selection.py`。
  - 安全与文件：`app/core/auth.py`、`app/core/rate_limit.py`、`app/core/sensitive_data.py`、`app/file_storage/`、`app/services/private_assets.py`、`app/services/resume_ingestion.py`、`app/services/retention.py`。
  - 业务服务：`app/services/appointments.py`、`app/services/catalog.py`、`app/services/fairness.py`、`app/services/operations.py`、`app/services/review.py`、`app/services/session_monitor.py`、`app/services/streaming_stt.py`、`app/services/talent.py`、`app/services/avatar.py`、`app/services/evaluation.py`、`app/services/interviews.py`、`app/services/model_admin.py`、`app/services/plan_assembly.py`、`app/services/plans.py`、`app/services/realtime.py`、`app/services/reports.py`、`app/services/roles.py`。
  - 模型、实时与 worker：`app/model_gateway/gateway.py`、`app/model_gateway/schemas.py`、`app/model_gateway/streaming.py`、`app/providers/mock/provider.json`、`app/providers/mock/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/openai_compatible/provider.py`、`app/realtime_bus.py`、`app/workers/outbox.py`、`app/main.py`。
  - 数据层与迁移：`app/persistence/interface.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/persistence/postgresql.py`、`app/persistence/provider.py`、`app/repositories/memory.py`、`app/repositories/sqlite.py`、`app/repositories/postgresql.py`、`app/repositories/provider.py`、`migrations/001_postgresql_persistence.sql`。
  - Web 与依赖：`app/web/app.js`、`app/web/index.html`、`pyproject.toml`。
  - 测试：`tests/test_auth_audit.py`、`tests/test_documented_gap_apis.py`、`tests/test_fairness_evaluation.py`、`tests/test_known_issue_remediations.py`、`tests/test_model_invocation.py`、`tests/test_mvp_flow.py`、`tests/test_openai_compatible_provider.py`、`tests/test_persistence_contract.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_postgresql_adapter.py`、`tests/test_production_compatibility.py`、`tests/test_rate_limit.py`、`tests/test_realtime_media.py`、`tests/test_resume_ingestion.py`、`tests/test_retention.py`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `.venv/bin/python -m pytest -q`：`88 passed in 2.64s`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：仓库内可实现项已完成。真实 PostgreSQL/Redis 集群、阿里云 OSS、恶意文件扫描器、真实 STT/TTS/数字人、企业邮件短信通道、WebRTC/SFU 和企业金标数据仍需相应账号、部署环境、供应商选择或业务样本，统一保持 `environment_pending`/`external_choice_required`，不得解释为生产已验收。`DATA-001` 需真实数据库完成 RLS、并发与 `EXPLAIN` 后才能改为 `verified`；`COMPAT-001` 需先迁移旧 SQLite 数据和客户端再删除兼容代码，当前不能标 `closed`。

## 2026-08-25 · COMPAT-CLOSE-001

- 目标：继续完成尚未关闭的仓库工作，迁移并删除旧计划 `items`、管理员直建会话、客户端文本答案和旧向量题库 interface；在本机能力允许时补做 PostgreSQL 真实 adapter 验证。
- 关联问题：`PLAN-001`、`CONSENT-001`、`APPOINTMENT-001`、`SEARCH-001`、`COMPAT-001`、`CANDIDATE-ACCESS-001`、`DATA-001`。
- 状态：`verified`（仓库范围）；`COMPAT-001` 与六项核心问题已 `closed`，`DATA-001` 保持 `in_progress/environment_pending`。
- 实际修改文件：
  - 协作与领域文档：`AGENTS.md`、`CONTEXT.md`、`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/known-issues-and-remediation.md`、`docs/change-log.md`。
  - 计划 execution v2 与迁移：`app/schemas/api.py`、`app/services/plan_assembly.py`、`app/services/plans.py`、`app/migrations/__init__.py`、`app/migrations/plan_execution_v2.py`、`app/services/interviews.py`、`app/services/reports.py`。
  - 旧题库 interface 删除：删除 `app/services/questions.py`；修改 `app/api/routes.py`、`app/workers/outbox.py`、`app/persistence/interface.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/repositories/memory.py`、`app/repositories/sqlite.py`，移除全局题目创建/列表、VectorDocument repository/collection 和索引 worker 分支。
  - 预约、候选人与实时边界：`app/services/appointments.py`、`app/services/realtime.py`、`app/core/rate_limit.py`、`app/web/app.js`。新增 `/#invite/{token}` 登记/同意/设备检查页、候选人 public 窄接口、安全投影、当前轮次媒体范围校验和 HMAC 会话 token；删除直接创建/直接 START、REST/WebSocket 文本答案及前端 fallback。
  - 测试：`tests/test_plan_assembly.py`、`tests/test_interview_session_aggregate.py`、`tests/test_realtime_media.py`、`tests/test_mvp_flow.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_known_issue_remediations.py`、`tests/test_persistence_contract.py`、`tests/test_sqlite_store.py`、`tests/test_production_compatibility.py`、`tests/test_rate_limit.py`。
- 验证命令与结果：
  - `docker info --format '{{.ServerVersion}}'`：失败，Docker daemon socket 不存在；无仓库副作用，不能执行真实 PostgreSQL 容器验收。
  - SQLite 持久数据只读检查：0 份 InterviewPlan；未改写用户数据。
  - `.venv/bin/python -m app.migrations.plan_execution_v2 --dry-run`：通过，`migrated_plan_ids=[]`、`migrated_session_ids=[]`。
  - 首次 `.venv/bin/python -m compileall -q app tests`：因系统 Python pycache 指向沙箱外用户缓存目录而失败；未修改仓库。改用 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pyc` 后通过。
  - `node --check app/web/app.js`：通过。
  - `.venv/bin/pytest -q`：`84 passed in 2.93s`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：仓库中不存在待完成的兼容代码或未标记的本地功能。已有旧部署必须先停写，运行 v2 `--dry-run`，备份后再执行正式迁移；当前工作区因没有旧计划/会话而无需执行写迁移。真实 PostgreSQL/RLS/并发/`EXPLAIN`、Redis 双实例、OSS、扫描器、真实 STT/TTS/数字人、邮件短信与 WebRTC 仍需要外部服务、凭据或供应商选择，继续保持文档中的 `environment_pending/external_choice_required`，不标记为生产已验收。

## 2026-08-25 · RUNTIME-RESTART-001

- 目标：按用户要求重启本地前后端服务，并验证后端健康检查、前端工作台与静态资源可访问。
- 关联问题：无；本工作项属于本地运行环境操作，不改变产品功能状态。
- 状态：`verified`。
- 实际修改文件：`docs/change-log.md`。
- 验证命令与结果：
  - `lsof -nP -iTCP -sTCP:LISTEN` 与本地健康检查：确认 5001/8000 均无旧实例；8001 属于 `/Users/zhangwenjun/zwj_project/mbti_generate`，未停止或修改该进程。
  - `.venv/bin/python main.py`：启动成功，Uvicorn 进程 PID `88732` 监听 `127.0.0.1:8000`；前端由同一 FastAPI 进程通过 `/` 与 `/web` 同源提供。
  - `GET /healthz`：`HTTP 200 application/json`，响应 `{"status":"ok"}`。
  - `GET /`：`HTTP 200 text/html`，页面包含 `Interviewer 面试工作台` 与 `/web/app.js`。
  - `GET /web/app.js`：`HTTP 200 text/javascript`。
  - Codex 内置浏览器：成功打开 `http://127.0.0.1:8000/`，标题、主导航、总览与工作区指标完成渲染，浏览器控制台错误数为 0。
- 未完成事项或恢复说明：无。当前服务保持运行，浏览器标签页已保留供用户查看；停止服务时可向启动会话发送 `Ctrl-C`。

## 2026-08-25 · MODEL-PROVIDERS-001

- 目标：实现首批同时覆盖 LLM 与 TTS 的真实 Provider adapter；扩展 OpenAI-compatible 语音合成，实现 DashScope 的 Qwen LLM/Embedding 与 Qwen/CosyVoice TTS，并修复模型路由配置界面，使基础语音链路可配置、可测试、可审计；视频数字人留在后续阶段。
- 关联问题：真实 LLM/TTS `environment_pending`；模型路由前端 `max_retries` 与后端 `retry_count` 字段不一致、路由能力列表未覆盖 TTS。
- 状态：`verified（仓库范围）`；`MODEL-PROVIDER-001` 已标记 `verified（仓库）`。
- 实际修改文件：
  - Provider 与网关：`app/providers/openai_compatible/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/dashscope/__init__.py`、`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、`app/model_gateway/gateway.py`。
  - 路由、资产与 UI：`app/schemas/api.py`、`app/services/model_admin.py`、`app/services/catalog.py`、`app/web/app.js`。
  - 自动化测试：`tests/test_openai_compatible_provider.py`、`tests/test_dashscope_provider.py`、`tests/test_provider_registry.py`、`tests/test_model_invocation.py`、`tests/test_mvp_flow.py`、`tests/test_documented_gap_apis.py`。
  - 同步文档：`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m pytest -q`：`89 passed in 2.74s`。
  - `git diff --check`：通过。
  - 重启 `.venv/bin/python main.py`：成功，Uvicorn PID `92458` 监听 `127.0.0.1:8000`；`GET /healthz` 返回 `{"status":"ok"}`。
  - `GET /api/v1/admin/model-providers/catalog`：`openai_compatible@0.2.0` 与 `dashscope@0.2.0` 均为 `implemented=true`，并声明 `llm.chat_json`、`llm.chat_text`、`embedding.text`、`tts.synthesize`。
- 未完成事项或恢复说明：当前没有真实 OpenAI-compatible/DashScope API Key、百炼业务空间、区域和外部联调样本，因此只完成官方 HTTP 合同和仓库验收，不宣称生产 route 健康。启用生产前仍需配置凭据、模型与 capability/purpose route，并验证 schema、TTS 音质/延迟/费用、私有资产复制和 readiness TTL。STT 与视频数字人仍按用户优先级留待后续供应商选型；现有 `avatar.speak -> TTS/文字` 降级链路保持可用。

## 2026-08-25 · MODEL-PROVIDERS-002

- 目标：参考 Dify 的声明式 Provider/模型 schema 与共享 runtime 分层，加深现有 Model Invocation seam；接入智谱 GLM 与 DeepSeek，并把已接入的阿里云百炼千问模型显式呈现在 catalog/UI，减少 OpenAI-compatible 厂商 adapter 的重复实现。
- 关联问题：`MODEL-PROVIDER-002`；当前 Provider manifest 缺少可执行模型 schema 约束，路由 UI 只能手输模型名；智谱与 DeepSeek 尚未安装为独立 Provider。
- 状态：`verified（仓库范围）`；`MODEL-PROVIDER-002` 已标记 `verified（仓库）`。
- 实际修改文件：
  - Manifest 与共享 runtime：`app/model_gateway/registry.py`、`app/providers/openai_compatible/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/dashscope/provider.json`。
  - 新 Provider：`app/providers/deepseek/__init__.py`、`app/providers/deepseek/provider.py`、`app/providers/deepseek/provider.json`、`app/providers/zhipuai/__init__.py`、`app/providers/zhipuai/provider.py`、`app/providers/zhipuai/provider.json`。
  - 配置与界面：`app/services/model_admin.py`、`app/web/app.js`。
  - 测试：`tests/test_provider_registry.py`、`tests/test_mvp_flow.py`、`tests/test_openai_compatible_vendor_providers.py`。
  - 同步文档：`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider-pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - 定向 Provider/API 测试：`20 passed in 1.10s`。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider-pycache .venv/bin/python -m pytest -q`：`93 passed in 3.11s`。
  - `git diff --check`：通过。
  - 服务重启成功：Uvicorn PID `95153` 监听 `127.0.0.1:8000`；`GET /healthz` 返回 `{"status":"ok"}`；catalog 返回 `deepseek@1.0.0`、`zhipuai@1.0.0`、`dashscope@0.2.0` 且均 `implemented=true`。
  - 本地浏览器验收：模型服务页显示 `11 个 Provider · 5 个可调用`；DeepSeek/智谱/千问卡片和模型数正确；配置弹窗分别预填 DeepSeek `https://api.deepseek.com + deepseek-chat`、智谱 `https://open.bigmodel.cn/api/paas/v4 + glm-5.2`、千问 `https://dashscope.aliyuncs.com/compatible-mode/v1 + qwen-plus`；控制台无 warning/error，未提交假配置。
- 失败与恢复记录：首次直接执行 `compileall` 时，macOS Python 尝试把 pyc 写到工作区外的用户缓存并被 sandbox 拒绝；仓库没有副作用。随后显式设置任务专用 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider-pycache` 重跑并通过。旧 exec session 已不存在且 8000 端口无监听，因此直接启动新服务，无进程需要强制终止。文档收口后的独立即时健康探针曾两次短暂返回 connection refused，但同一时刻 PID `95153` 仍在监听且 Uvicorn 无异常；随后 health/catalog 请求和三次连续 health 探针均返回 200，无数据副作用且无需再次重启。
- 未完成事项或恢复说明：当前没有 DeepSeek、智谱或阿里云百炼真实 API Key、账号/区域和模型授权，因此只完成官方协议适配、声明式目录和仓库验收，不宣称生产 route 健康。启用生产前仍需逐家创建配置/route，执行真实连接测试并验收结构化输出稳定性、延迟、费用、限流和模型生命周期；STT 与视频数字人仍保持后续外部选型边界。

## 2026-08-25 · MODEL-PROVIDERS-003

- 目标：为智谱 Provider 增加官方 GLM-TTS 能力，拆分 LLM/TTS 的按能力测试模型；补齐 Provider 配置编辑与测试模型选择界面，并把 HTTP 客户端初始化/代理依赖异常映射为结构化 Provider 错误，避免测试接口裸返回 500。
- 关联问题：`MODEL-PROVIDER-003`；智谱 manifest 只声明 LLM、单一 `test_model` 会把 `glm-tts` 误用于 LLM、管理 UI 无法编辑配置、环境 SOCKS 依赖缺失会泄漏为 Internal Server Error。
- 状态：`verified（仓库范围）`；`MODEL-PROVIDER-003` 已标记 `verified（仓库）`。
- 实际修改文件：
  - Provider/runtime：`app/providers/zhipuai/provider.py`、`app/providers/zhipuai/provider.json`、`app/providers/openai_compatible/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、`app/providers/deepseek/provider.json`。
  - API、服务与界面：`app/schemas/api.py`、`app/api/routes.py`、`app/services/model_admin.py`、`app/web/app.js`。
  - 自动化测试：`tests/test_openai_compatible_vendor_providers.py`、`tests/test_provider_registry.py`、`tests/test_mvp_flow.py`。
  - 同步文档：`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - 智谱官方文档核对：确认 `POST https://open.bigmodel.cn/api/paas/v4/audio/speech`、`model=glm-tts`、WAV/PCM、系统/复刻音色、`speed 0.5-2.0` 和输入最长 1024 字符。
  - Provider/API 定向测试：`16 passed in 0.69s`；新增 GLM-TTS HTTP 合同、模型/能力错配、输入上限、transport 初始化错误和空 body 兼容测试。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider3-pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider3-pycache .venv/bin/python -m pytest -q`：`99 passed in 3.01s`。
  - `git diff --check`：通过。
  - 服务重启成功：Uvicorn PID `98856` 监听 `127.0.0.1:8000`；`GET /healthz` 返回 `{"status":"ok"}`。
  - 旧空 body curl 对已恢复但停用的配置返回结构化 `502 provider_config_disabled`，不再出现原始 SOCKS traceback/500；实际外部调用需补 API Key 后验证。
  - 内置浏览器验收：智谱 catalog 显示 `tts.synthesize` 与 6 个模型；测试弹窗在 LLM 默认 `glm-5.2`、切换 TTS 后自动选择 `glm-tts`；编辑弹窗包含状态、Base URL、默认音色、显式环境代理和“留空保留密钥”，控制台无 warning/error。
- 未完成事项或恢复说明：真实智谱账号 API Key 在 `TEST-ISOLATION-001` 数据副作用中无法恢复，已恢复该配置的非秘密字段并停用；管理员补密钥并启用后，仍需完成真实 LLM/TTS、音色、音质、延迟、费用、限流与私有资产复制验收，状态保持 `environment_pending`。视频数字人不在本工作项范围。

## 2026-08-25 · TEST-ISOLATION-001

- 目标：修复测试辅助函数误用默认开发 SQLite 的隔离缺陷，保证自动化测试只重置新建的内存 store，不再删除 `data/interviewer.sqlite3` 中的本地开发数据。
- 关联问题：`TEST-ISOLATION-001`；执行 Provider 全量回归时发现 `reset_store_for_tests()` 调用默认 `SQLiteStore.reset()`，导致本地数据库内容被测试清空。
- 状态：`verified`（隔离修复）；本轮数据删除副作用不可逆，恢复边界如下保留。
- 实际修改文件：`app/repositories/provider.py`、`tests/test_sqlite_store.py`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/change-log.md`；本地 `data/interviewer.sqlite3` 因误 reset 及后续恢复非秘密智谱配置而发生数据变化。
- 验证命令与结果：
  - 根因验证：`SQLiteStore.reset()` 会 `DELETE` documents、model_invocations、provider_secrets、outbox；旧 `reset_store_for_tests()` 确实通过默认 backend 取得该 SQLiteStore。
  - 修复：`reset_store_for_tests()` 现在直接替换为全新 `InMemoryStore`；`test_reset_store_for_tests_never_resets_development_sqlite` 用临时 SQLite sentinel 验证不再触碰持久库。
  - 修复后的首次全量：`99 passed in 2.97s`；测试前后 `data/interviewer.sqlite3` SHA-256 同为 `816ab277130d9c53aa1d0032b8331d4d13e68bfd3ce75ab1f661cec6abac3498`。
  - 最终全量：`99 passed in 3.01s`；Python 编译、前端语法和 `git diff --check` 均通过。
  - 恢复检查：先复制为 `/private/tmp/interviewer-reset-20260825.sqlite3`，SQLite `.recover` 仍只得到 reset 后数据；`tmutil listlocalsnapshots /` 没有可用用户数据快照。
- 未完成事项或恢复说明：误删前的 Provider secret、其它 Provider 配置/路由和可能存在的开发业务记录无法从 SQLite 或本地快照恢复，不能伪造。已按可确认信息将 `mpc_11b4d940e3134074` 恢复为 disabled 智谱配置，Base URL、LLM/TTS 按能力模型和 `tongtong` 音色已补回；管理员需在“模型服务 → 编辑”重新输入 API Key、启用并重建需要的 route。两个 `/private/tmp/interviewer-*-20260825.sqlite3` 恢复副本保留，未删除。

## 2026-08-25 · MODEL-CONFIG-V2-001

- 目标：把模型服务配置重构为“厂商连接 → 模型配置 → 能力路由”，由 Provider 插件声明厂商与模型类型特有的后端表单 schema，并由前端通用渲染；路由只引用已验证的模型配置，删除 `provider_config_id + model` 双字段执行表示。
- 关联问题：`MODEL-CONFIG-V2-001`；当前 `ModelProviderConfig` 混合账号凭据、连接参数和模型参数，具体模型不是独立资源，前端硬编码厂商字段。
- 状态：`closed`（仓库实现与旧运行时边界删除完成；真实厂商健康仍为 `environment_pending`）。
- 实际修改文件：`app/model_gateway/{capabilities,forms,gateway,registry,schemas}.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/domain/appointment_admission.py`、Persistence/repository 的 Memory/SQLite/PostgreSQL 实现、五个已实现 Provider manifest、`app/web/app.js`、`app/migrations/model_configuration_v2.py`、`migrations/001_postgresql_persistence.sql`、`migrations/002_model_configuration_v2.sql`、相关既有测试与 `tests/test_model_configuration_v2.py`；同步 `CONTEXT.md`、ADR 及架构/API/领域/Provider/数据库/问题/进度/路线图文档。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pyc .venv/bin/python -m compileall -q app`：通过。
  - `node --check app/web/app.js`：通过；所有 Provider manifest 通过 JSON 解析和 registry 加载，模型类型覆盖符合能力声明。
  - `.venv/bin/pytest -q`：`106 passed in 3.63s`。
  - `git diff --check`：通过。
  - `.venv/bin/python -m app.migrations.model_configuration_v2 data/interviewer.sqlite3 --dry-run`：输出 `2` 个 ProviderConnection、`2` 个 ModelConfiguration、`0` 个 ModelRoute；dry-run 未修改开发数据库。
- 未完成事项或恢复说明：未执行开发 SQLite 的实际单向迁移，部署升级前需先复核 dry-run，再去掉 `--dry-run` 执行；真实供应商 API Key、模型授权、费用/延迟及音质仍需逐个 ModelConfiguration 联调，不能由离线测试标记生产健康。

## 2026-08-25 · MODEL-CONFIG-V2-LOCAL-MIGRATION

- 目标：修复模型配置 v2 发布后本地工作区所有业务 API 因旧 SQLite `provider_secrets` 字段而返回 500 的启动故障，备份并执行已验证的一次性数据迁移。
- 关联问题：`MODEL-CONFIG-V2-001`；服务端日志确认 `sqlite3.OperationalError: no such column: provider_connection_id`。
- 状态：`complete`。
- 实际操作：停止 PID `13643`；将 `data/interviewer.sqlite3` 原样备份到 `/private/tmp/interviewer-pre-model-v2-20260825.sqlite3`；执行模型配置 v2 迁移；因工具 PTY 会暂停进程，最终通过 macOS Terminal 持续启动为 PID `14840`。
- 验证命令与结果：
  - 迁移前数据库与备份 SHA-256 均为 `56cbde2321bbf707d6696bd5590d33217011e6955d34fe10deeb42d48af26bb2`。
  - 迁移输出：`2` 个 ProviderConnection、`2` 个 ModelConfiguration、`0` 个 ModelRoute。
  - SQLite `PRAGMA integrity_check` 返回 `ok`；旧 `provider_configs` collection 已删除，`provider_secrets.provider_connection_id` 已存在。
  - `/healthz`、工作区业务集合、Provider catalog、ProviderConnection、ModelConfiguration 和 ModelRoute API 均返回 HTTP 200；工作区首屏所需接口不再出现 500。
- 未完成事项或恢复说明：迁移前备份保留在 `/private/tmp/interviewer-pre-model-v2-20260825.sqlite3`，可在服务停止后恢复。迁移得到的智谱 LLM/TTS 模型状态为 `untested`，需补有效 API Key 并分别测试后才能创建生产路由。

## 2026-08-26 · DEEPSEEK-JSON-PROBE-001

- 目标：修复 DeepSeek V4 Pro 模型配置测试将普通文本响应当作 JSON 解析，从而误报 `provider_schema_invalid` 的问题。
- 关联问题：模型配置的 `llm.chat_json` 连通性探针未提供 JSON Schema，导致 OpenAI-compatible/DeepSeek adapter 不会启用结构化输出。
- 状态：`verified`。
- 实际修改文件：`app/services/model_admin.py`、`app/providers/openai_compatible/provider.py`、`app/providers/mock/provider.py`、`tests/test_model_configuration_v2.py`、`tests/test_openai_compatible_vendor_providers.py`、`docs/model-provider-plugins.md`、`docs/change-log.md`。
- 实现结果：`llm.chat_json` 配置/路由探针现在携带要求 `message` 字段的最小 JSON Schema；OpenAI-compatible 的 `json_object`/`prompt` 路径会向厂商同时传入 schema 和由 schema 生成的合法 JSON 示例；空内容与非 JSON 内容保留结构化错误并附带非敏感 `finish_reason`。Mock provider 也返回符合同一探针 schema 的 `pong`。
- 验证命令与结果：
  - Provider/配置定向回归：`23 passed in 0.91s`。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-deepseek-pyc .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-deepseek-pyc .venv/bin/python -m pytest -q`：`106 passed in 3.35s`。
  - `git diff --check`：通过。
  - 停止旧 PID `14840` 并通过 macOS Terminal 启动修复版 PID `16697`；`GET /healthz` 返回 `{"status":"ok"}`。
  - 真实调用 `POST /api/v1/admin/model-configurations/model_cfg_ef64e33ec0b641a8/test`：HTTP 请求成功，DeepSeek `deepseek-v4-pro` 返回 `{"message":"pong"}`，用量 `173 + 38 = 211 tokens`，模型配置标记为 `ready`。
- 未完成事项或恢复说明：无；此次真实探针产生一次 DeepSeek 调用计费/配额用量，调用日志按现有 Model Invocation 规则保留。

## 2026-08-26 · PROVIDER-CREDENTIAL-PREFLIGHT-001

- 目标：在管理员添加模型厂商连接时立即执行真实 API Key 鉴权探针，而不是只校验字段格式或等到具体模型测试。
- 关联问题：ProviderConnection 的 `/validate` 当前只返回 `model_required`，不会访问厂商；前端添加连接后也不会自动校验密钥。
- 状态：`verified`。
- 实际修改文件：`app/providers/openai_compatible/provider.py`、`app/providers/zhipuai/provider.py`、`app/providers/mock/provider.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/web/app.js`、`tests/test_model_configuration_v2.py`、`tests/test_openai_compatible_vendor_providers.py`、`docs/api-design.md`、`docs/model-provider-plugins.md`、`docs/change-log.md`。
- 实现结果：Provider adapter seam 新增 `validate_credentials`。OpenAI-compatible、DeepSeek 和 DashScope 通过 Bearer 认证的 `/models` 执行无文本生成费用探针；智谱插件使用 `glm-4.7-flash` 的单 token 最小探针；Mock 使用本地结果。管理服务将远程结果持久化为 `valid` / `invalid` / `model_required`，前端创建连接后会立即执行该校验；失败连接保留供管理员编辑修复。连接层校验不替代每个 ModelConfiguration 的模型权限与输出契约测试。
- 验证命令与结果：
  - Provider/配置定向测试：`20 passed in 0.93s`。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-credential-pyc .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-credential-pyc .venv/bin/python -m pytest -q`：`109 passed in 3.63s`。
  - `node --check app/web/app.js` 与 `git diff --check`：通过。
  - 停止旧 PID `16697` 并启动更新版 PID `18926`，监听 `127.0.0.1:8000`。
  - 真实调用 `POST /api/v1/admin/model-provider-connections/provider_conn_f4b6040cac554a6e/validate`：DeepSeek API Key 鉴权成功，`credential_status=valid`，厂商返回 `3` 个当前可访问模型。
- 未完成事项或恢复说明：添加厂商连接的请求会先保存加密凭证，再执行远程校验；远程失败不删除连接，以便修正 Base URL 或 API Key。智谱凭证探针会产生极小的模型调用配额/计费；其余已实现的 OpenAI-compatible 列表探针不生成 token。

## 2026-08-25 · WEB-ARCH-001

- 目标：按生产优先级把内置 Web 前端重构为 React 工程，在保持既有 REST 路径、请求体和响应语义兼容的前提下，完整迁移现有后台与候选人功能，并补齐后台认证/RBAC、浏览器 WebSocket 鉴权、候选人录音断线恢复、统一请求与路由级加载。
- 关联问题：前端生产认证不可用、非管理员整页加载失败、录音断线丢失/卡死、预约创建后的邀请失败不可恢复、异步状态文案漂移、候选人评分泄漏、首屏 N+1 与前端行为测试缺失。
- 状态：`verified`（2026-08-26）。
- 计划修改：`docs/api-design.md`、`docs/architecture.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`、`app/core/auth.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/main.py`、`app/web/`（React + Vite）、相关前端/认证/实时测试。
- 实际修改：后端认证/实时/聚合查询边界 `app/core/auth.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/services/catalog.py`；React/Vite 工程与托管 `app/main.py`、`app/web/{package.json,package-lock.json,vite.config.js,index.html,src/,dist/}`；框架无关 `app/web/{core/,candidate/runtime.js,interviews/appointment.js}`；前端样式、API/架构/进度/路线图/README 和认证、媒体、React 行为测试。模型配置保留 manifest 驱动的预定义/自定义模型 ID，候选人房间保留音视频设备切换；`/web/` 启用静态 HTML 入口。旧 `app/web/app.js`、六个 HTML 字符串 view module、DOM schema renderer 与未使用的 presentation helper 已删除。
- 最终验证：`cd app/web && npm run build && npm test`（生产构建成功、Vitest `3 passed`）；`npm audit --audit-level=high`（0 漏洞）；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-react-complete-pyc .venv/bin/python -m compileall -q app tests`（通过）；`.venv/bin/python -m pytest -q`（`114 passed in 3.83s`）；`git diff --check`（通过）。应用内浏览器逐页验证六个后台 feature、React 表单弹窗、模型动态 schema 表单、公开邀请及候选人路由；最终生产 bundle 通过 `http://127.0.0.1:8766/web/#models` 加载，模型页与 React shell 可见、控制台 0 error、DOM 无旧 `app.js`，服务日志确认总览/题库聚合请求边界。
- 未完成事项或恢复说明：仓库内 React 迁移无未完成项。真实摄像头/麦克风权限、外部模型凭据、STT/TTS/数字人和生产 WebSocket 网络仍属于既有部署环境验收，不改变本工作项 `verified` 结论。

## 2026-08-26 · BACKEND-MEDIA-001

- 目标：补齐 React 工作台所依赖的生产后端媒体能力，区分已存在但待部署验收的 PostgreSQL/Redis/OSS/WebSocket adapter 与尚缺实现的真实 STT、数字人 Provider；在保持现有 REST/WebSocket interface 兼容的前提下实现可配置的非 mock 媒体 Provider、生产 readiness、私有媒体复制和契约测试。
- 关联问题：真实 STT 与视频数字人仅有 mock/清单占位，前端摄像头与录音链路缺少可部署的供应商 adapter；生产环境验收项描述未明确“已有实现”和“尚缺实现”。
- 状态：`verified`。
- 计划修改：`app/model_gateway/`、`app/providers/`、`app/services/`、必要的 schema/API、Provider 与实时媒体测试，以及 `docs/architecture.md`、`docs/api-design.md`、`docs/model-provider-plugins.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/known-issues-and-remediation.md`、`docs/change-log.md`。
- 实际修改：新增 `app/providers/media_http/`，以 HTTPS/Bearer 合同实现真实 multipart batch STT、安全缓存后统一 final 的 streaming adapter，以及 `audio/video` 数字人响应；新增 `app/adapters/private_media.py`，生产候选人录音通过 PrivateFileStorage 保存为绑定组织/面试/轮次的 `candidate_answer_audio` FileObject。`BatchSTTRequest.audio_bytes` 只在服务端内存传递并从序列化、调用哈希和日志排除；Interview、Realtime、Streaming STT、Enterprise Review、Retention 与 Appointment Admission 已统一支持私有录音、签名回听、删除和生产失败关闭。React 候选人页面新增真实数字人音频/视频播放，REST/WebSocket 路径和请求/响应兼容保持不变。同步更新 README、架构、API、领域模型、Provider、数据库、问题状态、进度和路线图。
- 最终验证：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests`（通过）；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q`（`124 passed in 3.96s`）；`cd app/web && npm run build && npm test -- --run`（Vite 生产构建成功、Vitest `3 passed`）；`git diff --check`（通过）。新增离线合同覆盖 multipart 音频、不安全/含凭据媒体 URL、唯一 final、健康探针、私有录音读回/签名复核、生产本地存储拒绝和 OSS readiness。
- 未完成事项或恢复说明：仓库内通用 STT/HTTPS 数字人 adapter 与生产私有录音已完成。仍需在部署环境提供 PostgreSQL、Redis、阿里云 OSS、扫描器、`media_http` 目标端点/API Key/模型和真实录音视频，验证 RLS、多实例网络、WER、延迟、费用、浏览器摄像头/麦克风权限及生产 WebSocket；这些是已有 adapter 的环境验收。若目标供应商要求低延迟 partial STT、WebRTC 信令或实时口型同步，需要在现有 provider seam 内增加其专属协议实现。

## 2026-08-26 · DEPLOY-VALIDATION-001

- 目标：开始执行生产环境验收，使用隔离的本机 PostgreSQL 16 与 Redis 7 实例验证迁移、RLS、持久化契约、跨实例事件和生产失败关闭；盘点可复用的真实模型连接，并明确 OSS、扫描器、STT/数字人和浏览器媒体仍缺少的外部条件。
- 状态：`verified`（本机隔离环境；目标云环境继续 pending）。
- 已执行环境动作：启动 `interviewer-validation-postgres`（仅本机 `127.0.0.1:55432`）和 `interviewer-validation-redis`（仅本机 `127.0.0.1:56379`）隔离容器；未改写现有 SQLite 业务数据，未启动临时 Web 服务。
- 计划修改：部署验收脚本/测试（如发现仓库缺少可重复入口）、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际修改：`PostgreSQLPersistence` 移除运行时自动 DDL，改为 schema 只读校验；新增 `python -m app.migrations.postgresql`，由 migration owner 在 advisory lock 内显式迁移。新增 `/readyz` Deployment Readiness module，区分进程存活与数据库、Redis、生产密钥、私有 OSS bucket 只读鉴权、扫描器就绪，结果不包含密钥值且不写数据/调用付费模型。修复 Redis subscriber 正常关闭时冒泡连接异常和 deprecated close。新增 clamd TCP `PING/INSTREAM` scanner adapter，保留 command scanner；官方 `oss2 2.19.1` 加入项目依赖。新增 PostgreSQL、Redis、clamd 与 readiness 测试，并同步 README、架构、API、数据库、问题、进度和路线图。
- 本机环境验收：PostgreSQL 16 migration 成功；非 owner `interviewer_app` 角色跨租户读取为 0、跨租户写被 RLS 拒绝；事务回滚、CAS、Outbox 幂等、预约唯一约束和结构化题库过滤通过；`EXPLAIN (ANALYZE, BUFFERS)` 使用 `idx_question_catalog_scope`，样本执行约 `0.038 ms`。Redis 7 鉴权 PING、两个独立 bus 实例 Pub/Sub、正常关闭和生产公开限流通过。生产 `/readyz` 对隔离 PostgreSQL/Redis 返回 ready，并准确把未配置的安全密钥、OSS 和扫描器列为 not ready。现有 SQLite 仅做非敏感状态盘点，未改写；DeepSeek 连接仍为 valid/模型 ready，智谱配置仍 untested。
- 最终验证：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests`（通过）；常规 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q`（`130 passed, 5 skipped in 4.06s`）；设置隔离 DSN 后 `tests/test_postgresql_integration.py tests/test_redis_integration.py`（`4 passed in 0.25s`）；`git diff --check`（通过）。5 个默认 skip 是仅在提供 PostgreSQL/Redis/clamd 真实地址时启用的环境测试。
- 未完成事项与恢复说明：目标阿里云 OSS endpoint/bucket/RAM、生产安全密钥、`media_http` STT/数字人 endpoint/API Key/模型、真实浏览器/生产 WebSocket、目标 PostgreSQL/Redis 集群仍未提供，不能宣称目标生产环境 ready。官方 ClamAV stable 预装库镜像没有 arm64 manifest；改用 amd64 仿真后下载长时间未完成，已中止前台 pull，保留 Docker 缓存层，真实 daemon/EICAR 测试待目标 clamd 或镜像就绪后运行。`interviewer-validation-postgres` 与 `interviewer-validation-redis` 容器继续运行以便后续验收，分别仅监听 `127.0.0.1:55432/56379`；如需恢复空间可停止并删除这两个具名容器。

## 2026-08-26 · DEPLOY-VALIDATION-002

- 目标：继续部署前验收，建立不泄露密钥、可重复执行的生产配置生成与静态检查入口，并完成本机 clamd/EICAR 真实协议测试及组合 readiness 探针。
- 关联问题：生产安全配置尚依赖人工拼装；ClamAV daemon/EICAR 与接近生产的组合 `/readyz` 尚无真实证据。
- 状态：`verified`（本机部署前配置与 clamd 协议；目标 OSS/云环境继续 pending）。
- 计划修改：`.gitignore`、生产部署配置 module/命令与测试、`README.md`、相关部署文档和 `docs/change-log.md`。
- 实际修改：`.gitignore` 忽略所有本地 `.env.*` 并保留 `.env.example` 例外；新增 `app.operations.production_config`，以 `generate_production_config`/`inspect_production_config` 两个主要 interface 统一生成和静态检查。生成操作原子创建、拒绝覆盖、固定 `0600`，生成三种角色独立 Bearer token、两把 Fernet key 与独立候选人/WebSocket/文件/媒体签名密钥；检查只返回已配置变量名、缺项和无密钥值的无效分组，不导出环境或连接外部服务。`/readyz` 复用同一安全配置校验，删除重复实现。新增 `tests/test_production_config.py`，并同步 README、架构、问题、进度和路线图。
- 本机环境动作与证据：生成 `.env.production.local`（Git 已忽略、权限 `-rw-------`，没有输出密钥），对隔离 PostgreSQL/Redis/clamd 配置静态检查得到 `18` 个已配置变量、仅缺 4 个 OSS 参数、无格式错误。官方 `clamav/clamav-debian:stable_base` 原生 arm64 镜像成功下载；`interviewer-validation-clamav` 仅绑定 `127.0.0.1:53310`，挂载 `/private/tmp/interviewer-clamav-db/local.ndb` 的 EICAR-only 验收签名库。完整预装库镜像下载曾长时间无进度并安全中止；首次未提权 pytest 因沙箱禁止本机 socket 而 PING false，允许本机连接后相同测试通过，这两次失败均未改业务数据。
- 验证命令与结果：配置/readiness 定向测试 `7 passed`；常规全量 `134 passed, 5 skipped in 3.99s`；真实 clamd `tests/test_clamd_integration.py` 为 `1 passed in 0.19s`；隔离 PostgreSQL、Redis、clamd 组合测试为 `5 passed in 0.36s`；组合生产 readiness 为 `database=true`、`database_backend=true`、`redis=true`、`security_secrets=true`、`malware_scanner=true`、`object_storage=false`，因此整体准确保持 `not_ready`；Python compileall 与 `git diff --check` 通过。
- 未完成事项或恢复说明：当前唯一的基础 `/readyz` 缺项是目标阿里云 OSS endpoint/bucket/RAM 凭据；目标 PostgreSQL/Redis、带完整且持续更新签名库的生产 clamd、`media_http` STT/数字人、生产域名/WebSocket 和真实浏览器权限仍必须在目标环境复验。`.env.production.local` 含本机验收密钥并保留供下一步使用，不纳入 Git；三个验收容器继续运行且只绑定本机。EICAR-only 库只证明真实 daemon 协议与 adapter 错误映射，不能作为生产恶意样本覆盖率证据。

## 2026-08-26 · WEB-UI-REMEDIATION-001

- 目标：修复 React 工作台无法从空环境建立首道题目，以及模型服务、招聘流程、搜索工具栏和通用列表/表格因样式契约缺失而出现的错位、挤压和原始 HTML 退化。
- 关联问题：题目页在无岗位题库时禁用唯一创建入口；`list-card`/`panel`/岗位卡片缺少样式实现；候选人表格漏用统一表格 class；搜索网格列数与实际控件不一致；技术状态直接暴露英文枚举。
- 状态：`verified`。
- 计划修改：React questions/models/workflow 与通用 UI module、全局样式、浏览器行为测试和开发文档；保持现有后端接口、请求体和响应语义不变。
- 实际修改：`app/web/src/features/questions/Page.jsx` 保持“新建题目”始终可用；无岗位题库时以两阶段引导先创建或选择岗位并建立题库，再自动打开首题表单，继续调用既有岗位、题库和题目接口。`app/web/src/core/ui.jsx` 新增中文状态映射与 ResourceCard interface；模型页的厂商连接、模型配置和能力路由统一使用该 interface。招聘流程补齐 panel 标题层级、统一 `data-table`，把 `[retention_purged]` 安全占位显示为不可操作的“已清除候选人”。`app/web/styles.css` 新增 panel、岗位卡片、资源列表、表单引导和响应式五控件搜索网格；生产 bundle 已更新。`app/web/src/App.test.jsx` 新增空工作区首题入口行为测试；同步开发进度与本日志。
- 验证命令与结果：`npm run build && npm test` 通过，Vite 生成生产 bundle，Vitest `4 passed`；Python 全量 `134 passed, 5 skipped in 4.09s`；compileall 与 `git diff --check` 通过。真实浏览器在 1440×900 桌面视口验证题库、招聘流程和模型服务：首题按钮 enabled、前置引导 dialog 可见、搜索工具栏高度 42px 单行、模型 6 条实际资源进入统一卡片、已清除候选人不可上传简历，三个页面控制台均无 warning/error；移动默认视口同时验证引导表单和插件卡片可读。
- 未完成事项或恢复说明：未替用户创建测试岗位、题库或题目，避免污染现有业务数据；现有 REST 请求体与响应语义未改变。后续若继续视觉精修，可基于真实业务数据补充长标题、更多路由和表格横向溢出场景，但本次报告的不可创建与明显错位已修复。

## 2026-08-26 · MODEL-ADMIN-CRUD-001

- 目标：补齐模型厂商连接和具体模型配置的查看、创建、修改、删除闭环；删除厂商时事务性删除其全部模型，删除单个模型时保留同厂商其他模型，并清理引用已删除模型的能力路由。
- 关联问题：当前管理 API 和 React 模型服务页只有创建、列表、修改与测试/校验，缺少单项读取和删除能力，无法完成模型资源生命周期管理。
- 状态：`verified`。
- 实际修改文件：`app/persistence/{interface,memory,sqlite,postgresql}.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/web/src/{core/ui.jsx,features/models/Page.jsx,App.test.jsx}`、`app/web/styles.css`、`app/web/dist/`、`tests/{test_model_configuration_v2,test_persistence_contract}.py`、`docs/{api-design,domain-model,model-provider-plugins,change-log}.md`。
- 实现结果：ProviderConnection 和 ModelConfiguration 均具备创建、列表/单项查看、修改和删除接口。两个 DELETE 使用 `expected_version`；厂商删除在同一事务清理加密凭证、全部子模型、引用路由和对应模型能力断路器状态，单模型删除保留厂商和兄弟模型，仅清理引用该模型的路由/断路器状态；历史脱敏调用日志保留。React 模型服务页新增查看、删除与明确影响范围的二次确认，沿用原有 provider manifest 动态表单及所有现有模型对接协议。
- 验证命令与结果：`tests/test_model_configuration_v2.py` 为 `10 passed`；Memory/SQLite 持久化合同 `tests/test_persistence_contract.py` 为 `16 passed`；Python compileall 与全量 pytest 为 `138 passed, 5 skipped`；`npm run build && npm test -- --run` 生产构建成功、Vitest `5 passed`；`git diff --check` 通过。测试未删除或修改开发 SQLite 中现有 DeepSeek/智谱/Mock 配置。已在 Terminal 重启为 PID `52786`，监听 `127.0.0.1:8000`；`GET /healthz` 返回 `ok`，首页返回 HTTP 200，OpenAPI 确认两个单项资源均加载 `GET/PATCH/DELETE`。
- 失败与恢复说明：首次从 `app/web` 工作目录运行根目录 `.venv/bin/python` 因相对路径错误立即失败，没有副作用；首次级联测试发现 circuit hash 编码括号优先级错误，修正后再次运行；测试对系统自动保留的开发 Mock 连接假设为空，调整为验证目标厂商 ID 和密钥确实消失。工具隔离网络首次无法访问 Terminal 中的本机服务，改用获准的本机连接执行相同只读探针后通过。以上失败均未触碰开发业务数据。
- 未完成事项：无仓库内遗留。

## 2026-08-27 · KNOWLEDGE-BASE-TTS-DESIGN-001

- 目标：拆解“题库列表 → 题库详情/题目 → 题库级 TTS 模型与声音配置 → 配置变化后整库异步重建语音”的产品与技术方案，并把 Celery 调度、worker 执行、幂等和历史资产保护写入正式文档。
- 关联问题：当前题库页面平铺全部题目；KnowledgeBase 只有 `language + voice_profile_id`，没有显式绑定 TTS ModelConfiguration；题库语音重建已有持久工作项语义，但尚未定义 Celery 只负责调度、数据库工作项仍是真相来源的部署合同。
- 状态：`complete（设计）`；运行时实现仍由 `KB-SPEECH-001` 追踪为 `open`，没有代码、迁移和验收证据前不得标记 `verified`。
- 实际修改文件：`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 设计结果：题库一级页面只加载题库摘要，详情页按单题库加载题目、TTS 配置和构建历史；KnowledgeBaseSpeechProfile 显式冻结 TTS ModelConfiguration 版本、声音、语言、格式和语速，任何影响音频输出的变化都会递增 revision 并创建整库 KnowledgeBaseSpeechBuild。父构建冻结活动题 manifest，题目级工作项支持部分失败重试，旧 revision 的完成结果不能覆盖当前资产，历史计划/会话继续引用不可变旧资产。HTTP 只创建持久工作项并返回 202；所有 Celery task 和执行入口位于 `app/workers/`，Redis/Celery 只负责调度，DurableWorkItem/Outbox 继续作为真相来源，Celery Beat 负责补发 due/expired 工作。被题库当前配置引用的 TTS 模型禁止直接删除，必须先切换题库配置。
- 验证命令与结果：`rg` 交叉核对领域术语、接口路径、Celery 文件边界和问题编号；`git diff --check` 通过。文档明确区分“设计完成”和“运行时未实现”。
- 失败与恢复说明：一次只读 `rg` 校验命令把 Markdown 反引号直接放入 shell 双引号，zsh 将其中两个状态文本误作命令并报告 `command not found`；没有文件或运行时副作用，随后改用不含命令替换语义的查询完成核对。
- 未完成事项：尚未实现数据迁移、TTS voice catalog、题库语音 deep module、Celery 基础设施、异步 API、React 分层页面和自动化验收；按实施路线图里程碑 15 顺序继续。

## 2026-08-27 · KNOWLEDGE-BASE-TTS-IMPLEMENTATION-001

- 目标：实现题库级 TTS 模型/声音配置、配置 revision、整库异步语音重建、Celery worker 调度和题库列表/详情分层 React 页面，并保持现有业务接口兼容。
- 关联问题：`KB-SPEECH-001`。
- 状态：`verified`（仓库与本地 Redis/Celery/API/UI）。
- 计划修改：领域 schema、Memory/SQLite/PostgreSQL 持久化与迁移、模型声音目录、Catalog/Question Speech Build module、REST 接口、`app/workers/` Celery 调度、React questions feature、依赖/部署配置、自动化测试及对应正式文档。
- 安全与兼容约束：HTTP 不直接调用外部 TTS；Celery 消息只携带组织和持久工作项 ID；旧题库字段和现有调用方保留兼容读取；旧 profile revision 的迟到结果不得覆盖当前语音；历史资产不可变；现有用户业务数据不清空。
- 实际修改：新增 `app/services/knowledge_base_speech.py`、`app/workers/celery_app.py`、`app/workers/dispatcher.py`、`app/workers/knowledge_base_speech.py` 和 `tests/test_knowledge_base_speech.py`；修改 `app/services/catalog.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/persistence/interface.py`、`app/workers/outbox.py`、Mock provider manifest、`pyproject.toml`；重构 `app/web/src/features/questions/Page.jsx`、路由/数据加载/UI 状态/CSS/前端测试并重建 `app/web/dist/`；同步更新 README、接口、领域、数据库、供应商、架构、已知问题、进度和路线图文档。
- 实现结果：题库首页只加载并展示题库摘要；详情页按单一题库加载题目、TTS 模型/声音选项和构建进度。题库 profile 采用 revision + optimistic version；切换模型、声音或输出参数冻结全题清单并创建父构建，worker 再扇出可重试的题目子工作项。单题变化只重建该题；旧 revision 的迟到结果标记 `superseded` 且不能覆盖当前资产。删除被题库引用的 TTS 模型/厂商返回 `409 MODEL_CONFIGURATION_IN_USE`。旧题库不猜测模型，按读取投影显示 `configuration_required`，首次显式配置时完成非破坏性按需迁移。
- 异步边界：HTTP 题目创建/语音重建返回 `202` 和持久工作项；Celery Beat 只扫描 durable work，消息只包含 `organization_id` 与 `work_item_id`；worker 重新认领并执行，Redis 不作为业务事实源。Beat schedule 文件落到 `/tmp`，不污染仓库。
- 验证：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `141 passed, 5 skipped`；`npm run build` 通过；`npm test -- --run` 为 `6 passed`；`git diff --check` 通过。真实本地 Redis `PING` 返回 `PONG`，Celery 5.6.3 worker/Beat 成功连接 `redis://127.0.0.1:6379/2` 并周期派发 durable scanner；重启 API 后 `/healthz` 为 `ok`。浏览器复验题库列表、详情、禁用无可用 TTS 的配置按钮及旧题库/题目“待配置语音”投影通过。
- 失败与恢复：首次 compileall 因 macOS 默认 pycache 目录无权限失败，未修改仓库，改用 `/tmp` pycache 后通过；首次全量 pytest 因尚未安装新增 Celery 依赖而收集失败，安装已声明依赖后通过；过渡测试仍断言旧同步语音契约，更新为 `202 + worker` 契约后通过；一次页面整文件 patch、一次 CSS patch 因上下文不匹配未生效，均以更小补丁重试；浏览器 `networkidle` 等待能力不可用，改用 DOM ready 与显式短等待；sandbox 内 `redis-cli` 连接受限，经批准在本机只读验证成功。上述失败均无业务数据损坏。
- 未完成事项：尚未用真实供应商凭据执行会产生外部成本的 TTS 合成，也未在目标生产 PostgreSQL/Redis 网络和多实例部署中验收；仓库内实现与本地基础设施闭环已完成，不将其宣称为生产环境验收。

## 2026-08-27 · QUESTION-CRUD-TTS-ELIGIBILITY-001

- 目标：修复用户已添加 TTS 模型但题库详情仍不允许配置语音的问题，并补齐题目查看、新建、编辑、删除和语音试听的完整管理闭环；题目编辑继续只重建单题语音，题目删除不触发整库外部调用。
- 关联问题：题库详情把无“ready”模型直接表现为不可操作；题目列表仅有详情入口，缺少编辑与删除。
- 状态：`verified`（仓库与本地 API/React 浏览器）。
- 计划修改：模型 TTS 可选性诊断/反馈、Question 删除领域接口及 HTTP 合同、受控语音资产试听、React 题目操作与确认交互、行为/后端测试、接口/领域/进度文档；保持 Question Catalog 和 Knowledge Base Speech Build 两个既有深模块 seam，不在页面复制生成规则。
- 安全与兼容约束：不删除用户已有业务数据；删除题目必须带 expected version、组织隔离并保留历史面试快照/不可变语音资产；模型未校验或声音目录不完整时给出可操作原因，不把失败配置伪装为 ready。
- 根因与修复：用户新增的 `GlM-TTS/glm-tts` 能力正确，但健康状态为 `untested`；旧 `speech-options` 只返回 ready 模型，页面又直接禁用配置按钮，导致资源看似“消失”。接口现在保留兼容 `items`（仅可保存）并新增 `candidates`（所有 TTS、状态、声音和不可选原因）；配置入口始终可用，弹窗展示该模型并提供“测试并启用”，测试成功后原位刷新模型/声音选择。未替用户自动执行真实智谱 TTS 探针，避免未经确认产生外部调用或费用。
- 实际修改：`app/services/knowledge_base_speech.py` 增加 TTS eligibility projection；`app/services/catalog.py` 增加带版本归档删除、活动列表过滤、归档任务 supersede guard，并避免未变化题干重复生成语音；`app/api/routes.py` 新增 Question DELETE。React 题库详情增加未测试 TTS 诊断/测试、题目查看/编辑/删除和受控试听，通用 ModalForm 支持不可提交态并补充样式；更新后端/前端测试、生产 bundle 与接口/领域/Provider/进度/路线图文档。
- 删除与试听语义：删除只把 Question 归档并从活动列表移除，历史面试快照和不可变语音资产保留；并发必须通过 `expected_version`。归档前已经排队的语音工作由 worker 标记 `superseded`，不调用外部 TTS。试听只在题目有当前 ready `speech_asset_id` 时启用，通过 `/question-speech-assets/{id}/content-url` 获取五分钟受控地址。
- 验证：Python compileall 通过；题库语音/文档缺口定向测试 `7 passed`；全量 pytest `142 passed, 5 skipped`；Vite 生产构建通过；Vitest `8 passed`；`git diff --check` 通过。重启 API（PID `74263`）和 Celery worker/Beat 后，只读接口确认现有 `GlM-TTS` 作为 `untested` candidate 返回、音色 `tongtong` 可见。浏览器在现有题库验证配置按钮可点击、测试入口/不可提交保护、题目试听/查看/编辑/删除按钮、预填编辑表单和归档确认文案；未修改现有题目或触发真实 TTS 测试。
- 未完成事项：现有智谱模型仍需用户在配置弹窗点击“测试并启用”完成一次真实 Provider 探针；探针成功后才能保存题库 profile 并启动整库 Celery 语音重建。目标生产供应商、对象存储与网络验收仍按既有环境清单执行。

## 2026-08-27 · REAL-TTS-CHAIN-VALIDATION-001

- 目标：按真实可用标准验收题库 TTS 全链路，修复未就绪模型无法在下拉框选中、测试失败反馈不足和 Provider 可重试错误只调用一次的问题；以真实智谱请求、题库配置、Celery 资产生成和试听接口作为验收链。
- 状态：`verified`（仓库、本地 API/UI 与真实错误链）；真实智谱生成 `environment_blocked`。
- 已执行真实验证：`POST /api/v1/admin/model-configurations/model_cfg_45037aa67bd448da/test` 已实际到达智谱，返回结构化 `provider_rate_limited`，模型未被错误标记为 ready，当前未创建题库 profile 或语音任务。
- 计划修改：配置表单允许选择所有已添加 TTS 并预选声音，但只允许 ready 模型保存；模型测试增加有界退避重试和中文可操作错误；补齐自动化合同、文档、重启与真实链路复验。
- 安全约束：不绕过 ready gate，不把 429/额度不足伪装成成功；外部重试有严格次数上限；不输出 Provider 密钥或请求正文；未通过测试不得触发整库付费生成。
- 实际修改：Model Administration 探针改为 `retry_count=2 + retry_backoff_ms=250`，统一 Provider 错误处理把 `provider_rate_limited` 映射为 HTTP 429；题库语音表单改为所有已添加 TTS 均可在下拉框选中并联动声音，选项明确展示待测试/测试失败/已就绪，只有 ready 选项允许提交。测试失败后原位刷新状态并把 429 解释为检查智谱账户余额/并发限制，未吞掉结构化错误。同步更新 React 初始状态、后端/前端测试、生产 bundle 和 Provider/开发进度文档。
- 自动化验证：Model Administration + Knowledge Base Speech 定向测试 `14 passed`，其中探针前两次返回 retryable 429、第三次成功并验证三次尝试；全量 pytest `142 passed, 5 skipped`；Vitest `8 passed`；Python compileall、Vite production build、`git diff --check` 均通过。
- 真实验证证据：首次真实 GLM-TTS 探针返回 `provider_rate_limited`；启用有界重试并重启后再次调用，响应 HTTP `429`、`attempts=3`、invocation `model_invocation_7c6ab23146224165`。同一智谱 ProviderConnection 的真实凭据校验同样返回 HTTP 429，说明请求已越过本地路由/凭据读取/adapter/网络边界并被厂商账户侧拒绝。`speech-options` 实际返回模型、状态和 `tongtong` 声音；浏览器验证模型/声音下拉均可选，保存按钮因模型失败保持禁用。
- 当前边界与恢复：仓库接口不是占位实现；模型列表、声音目录、测试、profile gate、Celery 工作项、资产访问和题目 CRUD/试听均有执行实现与合同测试。但当前智谱账户在厂商校验和 TTS 两条请求上都返回 429，无法诚实完成“真实音频生成/试听”验收。用户处理智谱余额、套餐、并发或风控限制后，点击“测试并启用”；成功会把模型置为 ready，随后保存题库配置即可触发整库生成，无需再改代码。

## 2026-08-27 · AI-QUESTION-GENERATION-001

- 目标：实现基于题库定位、标签和可选要求的智能批量生题，提供生成批次、候选草稿逐题修改/删除、人工确认后批量导入正式题库的完整闭环。
- 状态：`verified`。
- 计划修改：统一领域语言、QuestionGenerationBatch 持久资源、LLM 选择与结构化生成、Celery worker、草稿审核/导入接口、React 审核工作台、题库来源追踪、自动化测试和架构/API/领域/Provider/数据库/进度文档。
- 不变量：AI 结果永远先进入草稿批次，不自动成为正式 Question；HTTP 不等待或执行 LLM；只有 ready 且支持 `llm.chat_json` 的具体模型可生成；草稿编辑/删除和确认导入使用 optimistic version；确认导入后批次冻结且幂等；正式 Question 仍必须通过完整评分依据校验，语音继续由现有 Celery 工作项生成。
- 安全与成本：生成数量限制在 1–30；可选要求和题库上下文长度受限；Celery 消息只携带组织和持久工作项 ID；不记录 Provider 凭据或完整 prompt；失败不产生部分正式题目。
- 实际修改：新增 `QuestionGenerationService` 深模块、批次/草稿持久模型与 Memory/SQLite/PostgreSQL 文档集合、生成选项/批次/草稿 CAS/确认导入 API、`question_generation.generate` worker dispatch、Mock 结构化生题和正式题目来源追踪；KnowledgeBase 增加定位/标签。React 题库详情新增模型/数量/定位/标签/可选要求表单、自动刷新、候选题编辑/删除和明确确认导入；状态统一中文展示。OpenAI-compatible adapter 增加受限的推理前缀/Markdown 包裹 JSON 兼容解析，生成 work lease 调整为 180 秒并启用至少 5 秒持久退避，防止慢响应重复领取。
- 修改文件：`CONTEXT.md`、`app/schemas/api.py`、`app/services/question_generation.py`、`app/services/catalog.py`、`app/api/routes.py`、`app/workers/outbox.py`、`app/providers/mock/provider.py`、`app/providers/openai_compatible/provider.py`、Memory/SQLite/PostgreSQL persistence/repository、`app/web/core/workspace.js`、`app/web/src/core/{WorkbenchProvider,ui}.jsx`、`app/web/src/features/questions/Page.jsx`、`app/web/styles.css`、前后端测试与架构/API/领域/Provider/数据库/检索/进度/路线图文档。
- 自动化验证：全量 pytest `148 passed, 5 skipped`；Vitest `9 passed`；Vite production build 通过；Python compileall、持久化合同、worker/service/API/React 行为和 `git diff --check` 通过。环境未安装 `ruff`，因此该命令不可用，未以此替代现有测试证据。
- 真实验收：API、Redis、Celery worker/Beat 使用最终代码运行；当前 ready DeepSeek V4 Pro 经 Celery 实际生成批次 `question_gen_2abbe405a2d44939`，一次调用完成，耗时 33.493 秒，token 用量 438/2626，状态为 `reviewing`。浏览器确认页面显示“待审核 · 1 道”、候选题编辑/删除、刷新和“确认导入题库”；编辑表单完整回填题干、答案、关键点、技能、难度和题型。未点击确认导入，正式题库保持 1 道，证明草稿隔离成立。
- 失败与恢复留痕：首次真实调用暴露推理模型返回说明/围栏 JSON、输出预算不足，以及旧 60 秒 lease 与约 60 秒两次 Provider 调用重叠造成重复领取/熔断；曾产生两个失败验证批次，但没有产生正式 Question。通过受限 JSON parser、提高结构化输出预算、180 秒 lease、持久退避和 `generating` crash recovery 修复，并增加回归测试；最终真实批次一次完成。历史失败批次按审计语义保留，不删除用户数据。曾在错误目录执行 npm、从 `app/web` 调用不存在的 `.venv/bin/python`、沙箱禁止 `ps`/端口/Redis，均改在正确目录或批准的本机运行方式重试；compileall 首次因 macOS 默认 pycache 目录不在可写边界而失败，改用 `/tmp/interviewer-python-cache` 后通过，均无仓库副作用。
- 未完成事项：本工作项的仓库实现、自动化与当前 DeepSeek 单次真实闭环已完成。生产环境仍需对目标模型账户执行并发、费用、长时间限流和多种题目数量的稳定性验收；候选题是否导入由用户在审核后决定，不属于自动验收动作。

## 2026-08-27 · PROMPT-GOVERNANCE-001

- 目标：建立 `app/core/prompt/` 统一 Prompt 维护 seam，迁移仓库内全部 LLM Prompt；在模型网关统一校验所有声明 JSON Schema 的 AI 响应，避免格式或内容不合规进入业务模块；把两项规则写入根 `AGENTS.md`。
- 状态：`verified`。
- 计划修改：盘点并迁移智能生题、简历审阅、答案评分和模型探针 Prompt；新增集中模板接口和结构化响应校验器；让所有 `ChatJSONRequest` 在网关返回前执行 schema/业务基础规则校验；补齐单元与回归测试，并同步架构、Provider、开发进度和操作留痕。
- 不变量：Provider 仍负责协议与 JSON 文本解析，业务 Prompt 不进入 Provider；格式校验失败必须返回结构化、可观察且不会泄露完整响应的错误；业务模块仍可在统一格式校验之后执行更严格的领域规则。
- 实际修改：新增 `app/core/prompt/contracts.py` 与 `validation.py`。`prompt_contract(name, context)` 集中提供智能生题、答案评分、简历审阅、JSON/text 模型探针和智谱 credential probe 的版本化消息及响应 Schema；OpenAI-compatible JSON Object 强化指令也移入该目录。QuestionGeneration/Evaluation/Talent/ModelAdmin/Zhipu Provider 删除内嵌 Prompt，成功结果或请求 metadata 记录 prompt version。Model Gateway 删除内嵌校验实现，统一调用 `validate_structured_response`，覆盖类型、required、enum、min/max items、min/max length、数值边界、unique items、additionalProperties 和仅空白字符串，并将失败映射为 `provider_schema_invalid` 后再执行 route 重试/fallback。
- 项目规则：根 `AGENTS.md` 新增“Prompt 与 AI 响应治理”，明确所有 Prompt/响应 Schema 必须位于 `app/core/prompt/`，指定格式 AI 响应校验通过前不得写领域对象或触发后续任务；推荐目录树同步增加该 package。AST 治理测试会拒绝在该目录外新增 `ChatMessage(...)` Prompt 构造。
- 修改文件：`AGENTS.md`、`app/core/prompt/{__init__,contracts,validation}.py`、`app/model_gateway/gateway.py`、`app/providers/{openai_compatible,zhipuai}/provider.py`、`app/services/{question_generation,evaluation,talent,model_admin}.py`、`tests/test_prompt_governance.py`、`tests/test_model_configuration_v2.py`，以及架构、领域、Provider、开发进度和本变更日志。
- 验证：`PYTHONPYCACHEPREFIX=/tmp/interviewer-python-cache .venv/bin/python -m compileall -q app tests` 通过；Prompt/网关/生题/模型配置/简历闭环定向测试通过；全量 pytest `155 passed, 5 skipped`；`git diff --check` 通过。代码扫描确认业务代码中的 `ChatMessage` 构造只存在于 `app/core/prompt/contracts.py`（类型声明除外）。
- 运行验收：最终代码已重启 API（PID `89420`）和 Celery Worker/Beat；`GET /healthz` 返回 `ok`，worker 连接本机 Redis 并进入 ready，durable dispatcher 正常且无待派发任务。
- 失败与恢复：一次组合 `rg` 因 shell 引号不匹配只读失败；一次大 patch 因 OpenAI-compatible helper 实际上下文不同未应用，拆成精确补丁后成功；首次定向测试仍期待宽松探针 schema，更新为严格 `message == pong` 合同后通过。为载入最后的 prompt version 修改做 warm shutdown 时 Celery 退出码为 1，并把一个无业务 payload 的周期 dispatcher 唤醒消息重新入队；新 worker 启动后消费两个 dispatcher 消息，均 `dispatched: 0`，没有业务数据或外部模型副作用。
- 未完成事项：当前仓库内已有 LLM Prompt 均已迁移并受静态治理测试保护；未来新增 Prompt 和指定格式响应必须按 `AGENTS.md` 规则扩展同一合同 seam。目标生产模型仍需持续验收不同厂商对严格 Schema 的遵循稳定性。

## 2026-08-27 · QUESTION-GENERATION-FANOUT-001

- 目标：把智能生题从单次大响应改造成“AI 蓝图规划 → 小批量 Celery 子任务 → 父批次统一校验/去重/补槽 → 人工审核”，解决一次生成 10 道题的 Provider 超时，并降低并行生成的相似/重复题。
- 状态：`verified`。
- 计划修改：在 `app/core/prompt/` 新增蓝图规划与按槽位生成合同；扩展 QuestionGenerationBatch 和 DurableWorkItem 父子状态；worker 支持规划、分片、合并与定向补生成；建立蓝图互斥键、规范化哈希、词元相似度和单写者合并规则；保持现有创建/查询/审核/导入接口兼容，补齐失败恢复、幂等和测试，并同步架构/API/领域/Provider/进度文档。
- 不变量：HTTP 仍只创建父批次和 durable work；所有外部模型调用仍仅由 `app/workers/` 执行；子任务不得直接写 GeneratedQuestionDraft；Prompt/Schema 只位于 `app/core/prompt/`；任何候选结果必须先通过统一结构化校验和父批次去重；已导入正式题目和历史失败批次不改写。
- 实际修改：新增 `question_blueprint_planning.v1` 与 `question_blueprint_generation.v1` 两个集中 Prompt/Schema 合同；父批次改为 `question_generation.plan -> question_generation.generate_chunk -> question_generation.merge` 持久工作流。规划结果冻结与目标数量一致的 QuestionBlueprint，生成工作每项最多两个槽位并独立使用 90 秒 route，merge 是唯一 GeneratedQuestionDraft 写入者；它按槽位顺序合并、与活动正式题/已接受题做规范化完全匹配和高阈值文本相似检查，并对缺失槽位最多定向补生成两轮。旧 `question_generation.generate` work kind 可由新规划处理器读取，历史批次与接口路径不迁移、不改写。
- 前端与可观察性：批次投影新增 `generation_progress`，公开 phase、规划数、子任务完成数、接受/过滤数和补生成轮次；React 审核卡显示规划、并行生成、合并、补生成四阶段以及真实子任务进度条，保留现有自动轮询、草稿编辑/删除和确认导入行为。生产 bundle 已重建。
- 修改文件：`app/core/prompt/contracts.py`、`app/services/question_generation.py`、`app/providers/mock/provider.py`、`app/workers/outbox.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`tests/test_question_generation.py`、`tests/test_prompt_governance.py`、`CONTEXT.md` 及架构/API/领域/检索/Provider/进度/路线图文档。
- 自动化验证：Mock 10 题链路证明先产生 10 个蓝图，再形成 5 个不超过 2 题的子工作，第三轮 dispatcher 完成 merge 并得到 10 道唯一候选题；定向 Python `12 passed`，全量 Python `157 passed, 5 skipped`，Vitest `10 passed`，Vite production build、Python compileall 和 `git diff --check` 均通过。编译检查首次未指定 pycache 时被 macOS 用户缓存目录权限拒绝，随后以 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-python-cache` 通过，无仓库副作用。
- 真实运行与历史失败：改造前真实批次 `question_gen_55b25541a5674c0d` 的 10 题大请求连续命中 60 秒 Provider timeout，最终 dead-letter，未生成草稿或正式 Question；该历史失败按审计语义保留且未自动重放，避免额外模型费用。最终代码已重启本地开发模式 API（PID `94092`）与 Celery Worker/Beat，`GET /` 和 `/healthz` 返回 200，Redis dispatcher 正常。首次重启误沿用生产 runtime，首页按预期返回 401；随即 warm shutdown 并显式覆盖为 development，同时保留现有 SQLite、Provider 凭据和数据。
- 未完成事项：仓库内规划、扇出、合并、补生成、前端进度和自动化证据已完成。新的真实 DeepSeek 10 题调用会产生外部费用，未擅自提交；需要由用户从页面创建新批次后验收目标账户的并发限流、耗时与成本。生产 PostgreSQL/Redis 多实例并发仍属于部署环境验收。

## 2026-08-27 · QUESTION-GENERATION-WORKBENCH-001

- 目标：把智能生题从题库详情内联卡片改造成独立任务工作台，提供批次历史、规划/分片/合并进度、单个或全部失败任务重试、持久化停止、停止后继续未完成项，以及候选题审核与导入。
- 状态：`verified`（仓库与本机 API/Redis/Celery/React 浏览器）。
- 计划修改：扩展 QuestionGenerationBatch 生命周期、执行 revision、控制事实和子任务投影；新增停止、继续、重试失败项、单分片重试接口；所有 Worker 在外部模型调用前和结果提交前执行停止/revision guard；增加 `#questions/{knowledge_base_id}/generation[/{batch_id}]` React 路由级页面；补齐后端/前端行为测试与架构、接口、领域、数据库、进度和路线图文档。
- 不变量：数据库批次与 DurableWorkItem 仍是任务真相来源，不能用 Celery result 或 `terminate=True` 作为停止事实；停止后不再发起新模型调用，已经在途且不可撤销的 Provider 请求允许返回但其旧 revision 结果必须丢弃；重试只补失败/未完成槽位并保留成功结果；前端不直接调用通用管理员 Outbox replay；草稿在人工确认前仍不能成为正式 Question。
- 安全与成本：停止、继续和重试都要求 optimistic version 与幂等保护并记录操作者/原因；页面只展示结构化错误、attempt、时间和受限 Provider 摘要，不返回完整 Prompt、AI 原始响应或凭据。浏览器验收没有创建、停止、恢复或重试任何批次。
- 实际修改：`app/persistence/interface.py` 增加协作式 Outbox cancel、开始/结束时间和结构化错误元数据；`app/services/question_generation.py` 增加 execution revision、stop/resume/retry-failed/retry-chunk、Worker 双重 guard、控制历史和 `tasks/available_actions` 投影；`app/schemas/api.py` 与 `app/api/routes.py` 增加四个控制合同。React router/workspace query 增加独立生成子路由，`QuestionsPage` 增加批次历史、任务详情、分片表、错误与人工操作记录，并把题库详情入口改为导航；同步更新样式、生产 bundle、前后端测试、`CONTEXT.md` 及架构/API/领域/数据库/进度/路线图文档。
- 自动化验证：`git diff --check`、Python compileall 通过；全量 pytest `160 passed, 5 skipped`；Vitest `12 passed`；Vite production build 通过。新增测试覆盖排队停止/恢复、在途旧 revision supersede、单失败分片重试不重做成功项，以及 React 停止/分片重试只调用领域接口而不访问通用 `/admin/work-items/*`。
- 运行与浏览器验收：development API 运行于 `127.0.0.1:8000`，Celery Worker/Beat 连接本机 Redis DB 2。真实 React 页面列出 5 个历史批次；历史 timeout 批次展示 dead-letter、`5/5` 和“重试失败项”；运行批次展示 10 个规划方向、5 个 `slot_01..slot_10` 分片与各自 attempt。该既有批次最终完成 5/5 分片与 merge，进入 `reviewing`，生成 10 个待审核候选且开放“导入”操作。浏览器仅做读取和路由切换，未提交控制命令，并把工作台页面保留给用户。
- 失败与恢复：首次在 sandbox 内启动 API/Worker 分别因本机端口绑定和 Redis 连接权限被拒绝；停止该 Worker 后使用已批准的本机开发命令成功启动。首次新增前端路由后两个旧行为测试仍假设内联弹窗，更新为子页面合同后通过；补充控制行为测试后最终 Vitest 全绿。启动 Celery 时数据库原本已有一条 claimable 的 10 题批次，Beat 按既有持久事实自动派发，DeepSeek 规划请求返回 200 并继续扇出 5 个分片；这不是本轮新建/重试任务，但确实可能产生该既有任务的供应商费用，未删除或伪造其审计事实。
- 未完成事项：仓库功能已完成。Provider 已接受的在途请求无法由通用停止命令保证立即撤销，系统只保证停止后不发起新调用且旧 revision 结果不入库；目标生产 PostgreSQL/Redis 多实例、Provider 并发/限流/费用仍按环境清单验收。

## 2026-08-27 · QUESTION-DRAFT-DETAIL-SINGLE-IMPORT-001

- 目标：让智能生题候选列表的非操作区域可点击查看完整题目详情，并支持把单个候选题独立导入正式题库，同时保留编辑、删除与批量导入。
- 状态：`verified`（仓库与本机 API/Redis/Celery/React 浏览器）。
- 计划修改：先补充单候选导入 API/领域合同和幂等、版本语义，再实现候选行点击详情、操作按钮事件隔离与单题导入交互，最后补齐前后端测试、生产 bundle、接口/领域/进度文档和本地运行验收。
- 不变量：单题导入仍必须通过正式 Question 评分依据校验；已导入候选不能重复导入；其他草稿继续保留可审核，批次不因一次单题导入而冻结；编辑、删除、导入按钮不得冒泡触发行详情；历史批次和正式题目不被改写。
- 实际修改：新增 `POST /api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}/import`；QuestionGenerationService 为单题导入冻结草稿、写入幂等 `knowledge_base.import` 工作并投影单题任务，Catalog worker 成功后只把对应草稿置为 imported，失败按 DurableWorkItem 是否还能重试投影 importing/failed。批量导入排除已导入题并拒绝与在途单题导入并发，正式 Question 继续以 generation batch/draft 来源去重。React 候选行改为鼠标/键盘可打开详情，展示题干、标准答案、技能、难度、题型和评分关键点；操作区隔离冒泡并新增“单独导入”，导入中/成功后冻结编辑与删除。
- 修改文件：`app/services/question_generation.py`、`app/services/catalog.py`、`app/api/routes.py`、`app/web/src/features/questions/Page.jsx`、`app/web/styles.css`、`app/web/dist/`、`tests/test_question_generation.py`、`app/web/src/App.test.jsx`，以及接口、领域、数据库、开发进度、路线图和本日志。
- 验证：定向 QuestionGeneration 测试 `8 passed`；全量 Python `161 passed, 5 skipped`；Vitest `12 passed`；Vite production build、Python compileall 和 `git diff --check` 通过。新增后端合同覆盖单题导入提交/幂等、批次保持 reviewing、成功草稿冻结、剩余两题批量导入且最终无重复；React 行为覆盖行点击详情、编辑按钮不触发详情和单题导入领域接口。
- 运行验收：API 已以 PID `8890` 重启，`/healthz` 返回 ok，OpenAPI 包含单题导入路径；Celery Worker/Beat 主进程 PID `8934` 连接 Redis DB 2 且 dispatcher 无待处理工作。真实页面显示 10 个可点击候选行、每行“单独导入/编辑/删除”和详情提示；只打开并关闭详情、编辑和单题导入确认弹窗，没有提交导入、编辑或删除，浏览器 console 0 warning/error。
- 未完成事项：仓库闭环已完成；目标生产 PostgreSQL/Redis 多实例、真实对象存储和 TTS 仍按既有环境验收清单执行。本轮没有调用 LLM/TTS，也没有产生新的供应商费用。

## 2026-08-27 · QUESTION-REVIEW-PRIORITY-001

- 目标：移除正常生题批次中占据大量首屏空间的 Worker 流程表，把候选题审核提升为任务详情的首要内容；失败恢复能力继续保留，但只展示真正失败且可操作的最少信息。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：调整 React 批次详情的信息层级，保留运行态紧凑进度、状态命令和失败项重试，隐藏成功的规划/分片/合并明细；更新行为测试、生产 bundle、开发进度/路线图与本日志，并在真实工作台验证候选题进入首屏。
- 不变量：后端 tasks、stop/resume/retry 合同和审计事实不删除；只是减少 UI 暴露，失败分片仍能单独重试，候选题详情/编辑/删除/单题导入/批量导入保持不变。
- 实际修改：`QuestionGenerationBatchPanel` 删除正常态 Worker 子任务表，把候选题审核移动到批次标题正下方；queued/generating/stopping/importing 继续显示紧凑阶段文案和进度条，failed 只显示带错误原因和重试按钮的失败项，人工控制历史仍保留。同步增加精简失败项样式、更新 React 行为测试、重建 production bundle，并修正文档中的工作台信息层级说明。
- 修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证：Vitest `12 passed`，Vite production build 与 `git diff --check` 通过。真实工作台刷新后 DOM 确认不存在“Worker 子任务”，候选题审核和第一道候选题均直接可见，浏览器 console 0 warning/error；未点击任何业务操作。
- 未完成事项：无仓库内功能缺口。本次只调整信息层级，没有修改后端合同、数据或供应商调用，也没有产生外部费用。

## 2026-08-27 · MODEL-ADMIN-UX-001

- 目标：把模型服务从资源堆叠页面改造成“厂商连接 → 模型配置 → 业务用途”三步任务流，修复选择切换后名称不联动和用途/能力可错配的问题，并让管理员直接看到正式面试所需路由的配置完成度。
- 关联问题：模型服务首屏信息层级倒置、Provider/模型切换保留旧显示名称、路由用途为自由文本且默认可能与模型能力冲突、缺少必需业务用途 readiness 清单和资源关系说明。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：`app/web/src/features/models/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/styles.css`、`app/web/src/App.test.jsx`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 不变量：继续复用 Provider manifest 动态表单、ProviderConnection/ModelConfiguration/ModelRoute 后端资源和现有 REST 请求语义；只有 enabled + ready 且支持目标 capability 的模型可成为路由目标；不在前端复制模型网关执行、健康或 readiness 业务判断。
- 实际修改：模型服务页重排为连接、模型、业务用途三段连续任务流，并增加顶部完成度卡片；业务用途覆盖表固定展示 7 项核心用途和 2 项可选用途、缺失影响、目标模型与可操作状态。路由表单不再接受自由文本用途或手填 capability，而是由用途自动推导 capability 并过滤兼容的 ready 模型；Provider、模型类型和模型切换会同步建议显示名称。已安装插件目录移至折叠区，未实现项明确标记“暂未接入”。后端资源、路由请求体与模型网关执行 seam 均未改变。
- 修改文件：`app/web/src/features/models/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/styles.css`、`app/web/src/App.test.jsx`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 验证命令与结果：`cd app/web && npm test -- --run` 通过，Vitest `14 passed`；`npm run build` 通过并重建 production bundle；目标文件 `git diff --check` 通过。真实工作台 `/web/#models` 验证三步导航、完成度、用途覆盖与兼容模型过滤均可见，回答评分表单只展示支持 `llm.chat_json` 的 ready 模型，浏览器 console 0 warning/error；未提交配置、未调用供应商。
- 未完成事项或恢复说明：无仓库内功能缺口。当前页面显示的未配置用途仍需管理员按实际供应商凭据建立兼容模型和路由；本次没有修改现有模型数据，也没有产生外部费用。

## 2026-08-27 · MODEL-PROVIDER-DRILLDOWN-001

- 目标：把模型服务改为与题库一致的父子层级：首页只展示已接入厂商，点击厂商进入子页面，并在当前厂商上下文中完成模型增删改查、模型类型查看和模型测试。
- 关联问题：当前模型服务把所有厂商、所有模型和全部业务用途铺在同一长页面，模型与所属厂商的层级关系不够直接，管理员添加模型时还需要重复选择厂商。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：`app/web/core/router.js`、`app/web/src/features/models/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 不变量：继续使用现有 ProviderConnection/ModelConfiguration/ModelRoute 接口和动态 manifest 表单；子页面只筛选当前 provider connection 下的模型，不改变后端资源归属、测试、删除级联或路由 readiness 语义。
- 实际修改：路由新增 `#models/{provider_connection_id}` 厂商子页面；模型服务首页改为已接入厂商卡片目录，展示厂商状态、模型总数、测试就绪数和已配置模型类型，点击卡片进入厂商管理。子页面展示连接状态与当前厂商模型摘要，模型列表直接展示中文模型类型、原始 `model_type`、厂商模型 ID、支持能力和测试状态，并提供查看、编辑、测试、删除操作。添加模型从厂商子页面发起，所属厂商以只读信息展示且请求固定写入当前 connection，避免重复选择或跨厂商误配；连接编辑、校验和级联删除仍在子页面提供。业务用途与插件清单保留在首页折叠区。
- 修改文件：`app/web/core/router.js`、`app/web/src/core/WorkbenchProvider.jsx`、`app/web/src/features/models/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 验证命令与结果：`cd app/web && npm run build && npm test -- --run` 通过，Vite production build 成功，Vitest `14 passed`；目标文件 `git diff --check` 通过。真实工作台验证首页显示 3 个已接入厂商，点击 DeepSeek 进入 `#models/{connection_id}`，子页面只显示该厂商的 DeepSeek V4 Pro，模型类型显示“大语言模型 / llm”，查看、编辑、测试、删除按钮齐全；“添加模型”弹窗固定显示所属厂商 DeepSeek 且不存在厂商选择框，浏览器 console 0 warning/error。验收未提交、测试或删除任何真实配置。
- 未完成事项或恢复说明：无仓库内功能缺口。本次没有修改后端模型资源或现有配置，也没有调用外部模型供应商或产生费用。

## 2026-08-27 · QUESTION-GENERATION-CREATE-MODAL-001

- 目标：把智能生题任务配置从页面展开卡改为点击“新建生题任务”后打开的弹窗，删除“收起配置”状态，让任务历史和候选题始终保持页面主体。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：重构 QuestionGenerationWorkbench 的创建入口和 Modal 生命周期，更新 React 行为测试、移除废弃展开式样式、重建 production bundle，并同步进度/路线图和本日志。
- 不变量：创建请求、模型/数量/定位/标签/可选要求字段、幂等键和成功后导航到批次详情的行为不变；打开或关闭弹窗不创建任务、不调用模型。
- 实际修改：QuestionGenerationWorkbench 删除 `creating` 展开状态和内联 `generation-create-card`，顶部固定显示“新建生题任务”；点击后由统一 Modal 承载原 QuestionGenerationForm，提交成功先关闭弹窗再导航到新批次。移除废弃 CSS，更新 React 行为测试和工作台进度/路线图说明，重建 production bundle。
- 修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证：Vitest `14 passed`，Vite production build 与 `git diff --check` 通过。真实工作台刷新后确认“新建生题任务”按钮可见、页面无“收起配置”和内联表单；点击后弹窗包含生题模型、生成数量与题库定位，关闭后没有创建任务，浏览器 console 0 warning/error。
- 未完成事项：无仓库内功能缺口。本次没有提交生题请求、调用模型或修改现有批次数据。

## 2026-08-27 · QUESTION-DRAFT-IMPORT-CONCURRENCY-001

- 目标：修复连续单题导入时，上一题推进 QuestionGenerationBatch version、下一题仍携带旧 expected_version 而返回 `PERSISTENCE_CONFLICT` 的竞态。
- 状态：`verified`（仓库测试与本机运行态）。
- 计划修改：把单题导入从整批版本硬拒绝调整为草稿级条件命令，在目标草稿仍未导入且幂等键未冲突时基于事务内最新批次版本提交；前端刷新完成前保持确认弹窗，补齐连续导入/真正冲突测试、接口与领域文档，重建并重启本机服务。
- 不变量：同一草稿不能重复导入；编辑/删除仍使用批次 CAS；批量导入与在途单题导入仍互斥；正式 Question 继续以 generation batch/draft 来源去重，不因放宽无关批次版本而丢失更新。
- 初始诊断：用户请求携带 expected_version 23，而持久批次已是 24，说明第一题命令或 Worker 完成事实已经推进聚合版本。首次只读复查时本机 8000 端口已不再监听，未产生数据副作用，待代码修复后统一重启。
- 实际修改：GeneratedQuestionDraft 增加独立 `version`，PATCH 草稿只推进目标草稿与批次版本；单题导入请求新增可选 `expected_draft_version`，服务端兼容旧 `expected_version` 但不再用无关的批次旧版本拒绝目标草稿，而是在事务内以读取到的最新批次版本写入。前端发送草稿版本，并把刷新完成放在关闭确认弹窗之前，避免短暂暴露旧页面状态。批量导入、编辑、删除的原批次 CAS 规则保持不变。
- 修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/question_generation.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`tests/test_question_generation.py`，以及接口、领域、数据库、开发进度和本日志。
- 验证：定向 QuestionGeneration `8 passed`；全量 Python `161 passed, 5 skipped`；Vitest `14 passed`；Vite production build、Python compileall 与 `git diff --check` 通过。回归合同使用同一个旧 batch version 紧接提交两个不同草稿，均返回 202；随后编辑第三个草稿并以旧 draft version 导入，仍正确返回 `PERSISTENCE_CONFLICT`；最终单题与剩余批量组合共生成 3 个且无重复 Question。
- 运行验收：API 已以 PID `18150` 启动，Celery Worker/Beat 已连接 Redis DB 2 且 dispatcher 无待处理工作。只读确认用户批次当前 version 24、前两题 imported、目标 `question_draft_c07a9995b9ce4542` 仍为 pending/version 1；没有替用户重放失败请求或导入该题。
- 失败与恢复：第一轮定向测试发现 `expected_draft_version` 参数误落在相邻的批量导入方法签名，导致路由 TypeError；精确移动到单题方法后定向、全量与前端测试全部通过，没有持久化或外部调用副作用。
- 未完成事项：无仓库内功能缺口。本轮没有调用 LLM/TTS，也没有替用户修改现有题库；目标生产 PostgreSQL 多实例的真正并发提交仍按环境清单做部署验收。

## 2026-08-27 · KNOWLEDGE-BASE-VOICE-LABEL-001

- 目标：移除题库卡片上面向用户暴露的内部 `model_configuration_id`，把卡片底部改为只展示具有明确“读题语音”标识的声音配置与配置状态。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：调整题库目录卡片的语音信息结构和样式，补充 React 行为测试，重建 production bundle，并在真实题库目录进行只读浏览器验收。
- 不变量：不改变题库、TTS 模型、声音配置或语音构建数据；模型配置详情仍在题库详情/模型服务中管理；本项只修正目录投影的用户界面表达，不新增接口或供应商调用。
- 实际修改：题库卡片删除 `model_configuration_id · voice_profile_id` 原始拼接，改为独立“读题语音”信息块；已配置时只显示声音 profile 标识和“已配置”，未配置时显示“未配置/待配置”。同步补充清晰的层级、边框和状态样式，并重建生产 bundle。
- 修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/styles.css`、`app/web/src/App.test.jsx`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 验证：Vitest `15 passed`，Vite production build 与目标文件 `git diff --check` 通过。本机 API 以 PID `20751` 重新启动；真实 `#questions` 页面卡片显示“读题语音 / tongtong / 已配置”，DOM 中 `model_cfg_` 数量为 0，浏览器 console 0 warning/error。
- 失败与恢复：首次浏览器验收时 8000 端口尚未监听，按既有 development 配置启动 API 后恢复；一次最终构建命令误在仓库根目录执行并因没有 `package.json` 返回 `ENOENT`，随后在 `app/web` 重跑成功且没有文件副作用。sandbox 内 curl 因本机网络隔离显示连接失败，使用获批的本机只读健康检查得到 `{"status":"ok"}`。
- 未完成事项：无仓库内功能缺口。本项没有修改后端接口或持久数据，没有调用 TTS/LLM，也没有产生供应商费用。

## 2026-08-27 · POSITION-KNOWLEDGE-BASE-ASSIGNMENT-001

- 目标：把招聘流程中的“添加题库”从重复创建题库并手填语言/音色，改为从组织已有题库中选择并关联；岗位直接复用题库的题目、KnowledgeBaseSpeechProfile、音色和现有语音资产。
- 状态：`verified`（仓库测试、生产构建与本机运行态）。
- 计划修改：为 JobPosition 增加显式 `knowledge_base_ids` 关联并提供幂等关联 API；Question Catalog 与 Interview Plan Assembly 按岗位关联验证题库范围；React 招聘流程改为已有题库下拉选择并展示题库语音摘要；补齐后端/React 合同测试，更新架构、接口、领域模型、检索、统一语言、开发进度、路线图和本日志，重建生产 bundle。
- 不变量：题库仍由题库模块统一维护，关联操作不复制、不改写题目或语音配置，不调用 TTS/LLM；组织隔离、题库 readiness、计划冻结、检索 scope 和历史面试快照继续保持；旧数据中 `KnowledgeBase.job_position_id` 继续作为初始/兼容关联读取。
- 实际修改：JobPosition 新增 `knowledge_base_ids` 显式关联，创建题库时自动建立初始关联，旧 `KnowledgeBase.job_position_id` 继续投影为兼容关联；新增 `POST /job-positions/{id}/knowledge-base-assignments` 幂等命令并保留岗位 version CAS。Question Catalog 和 Interview Plan Assembly 先验证目标岗位已关联全部题库，再按租户、题库、活动状态、结构化条件和语音 readiness 查询；底层不再用 Question 的创建岗位阻断共享题库。招聘流程弹窗改为仅选择尚未关联的已有题库，选项展示题库声音，删除名称/说明/语言/音色输入；岗位卡片展示已关联题库及声音。计划页同步按岗位关联验证题库。关联过程不复制或修改题目、speech profile 或语音资产，也不调用供应商。
- 修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/catalog.py`、`app/services/plan_assembly.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/persistence/postgresql.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/features/plans/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/test_position_resume_appointment_flow.py`、`tests/test_persistence_contract.py`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`CONTEXT.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md` 和本日志。
- 验证：定向跨岗位关联合同 `1 passed`；全量 Python `161 passed, 5 skipped`；Vitest `16 passed`；Vite production build、Python compileall 和 `git diff --check` 通过。本机 API 已重启为 PID `28417`，`/healthz` 返回 `ok`，OpenAPI 已包含新的 assignment POST 路径。
- 失败与恢复：首次组合验证误在 `app/web` 工作目录调用根目录 `.venv`，未执行测试；首次全量测试暴露两条持久层合同仍按 Question 创建岗位过滤，更新为题库关联边界后通过；一次测试补丁多出单行缩进导致收集失败，修正后全量通过；sandbox 内本地 curl 受网络隔离，改用获准的本机只读检查成功。以上失败均无业务数据或外部调用副作用。
- 未完成事项：无仓库内功能缺口。本轮未修改现有题库、题目、岗位关联或语音配置，未调用 TTS/LLM，也未产生供应商费用；目标生产 PostgreSQL 规范化关联表仍按既有部署迁移流程实施和验收。

## 2026-08-27 · POSITION-LIFECYCLE-CASCADE-001

- 目标：补齐岗位新增、查看、编辑、删除工作流；删除岗位前展示明确的级联影响并要求输入岗位名称确认，确认后清除该岗位关联的全部候选人敏感数据并从活动工作区隐藏岗位和候选人。
- 状态：`completed`。
- 领域决策：规范术语为 Position Candidate Membership（岗位候选关系）。新录入候选人显式选择一个应聘岗位；旧数据通过 CandidateProfile、ResumeReview、InterviewPlan、InterviewAppointment 和 InterviewSession 的岗位引用推导归属。岗位删除对命中的候选人执行隐私 purge，而非破坏审计/历史引用的物理级联；共享 KnowledgeBase 不随岗位删除。
- 计划修改：扩展 CandidateProfile 岗位归属与岗位删除影响预览/确认命令；复用 RetentionService 清除候选人私有文件和敏感投影，归档岗位及相关要求/计划并取消预约；React 增加岗位编辑、危险删除弹窗、候选人岗位选择和展示；补齐合同/行为测试并同步架构、接口、领域、存储、统一语言、开发进度、路线图和本日志。
- 不变量：未经岗位名称精确确认不得删除；删除影响必须限于当前组织和目标岗位；历史面试与审计保留不可识别占位；题库内容、题库语音及其他岗位不受影响；删除命令不调用模型供应商。
- 实际修改：`app/schemas/api.py`、`app/api/routes.py`、`app/services/catalog.py`、`app/services/talent.py`、`app/services/retention.py` 增加候选人显式岗位归属、岗位删除影响预览、名称确认命令、候选人敏感数据清除、岗位/要求/计划归档、预约取消和删除审计；`app/web/src/features/workflow/Page.jsx` 增加岗位编辑/危险删除、候选人岗位选择和展示，弹框明确列出实际影响并使用“永久删除岗位及候选人”按钮；`app/web/src/App.test.jsx` 与 `tests/test_position_resume_appointment_flow.py` 覆盖编辑、弹框、精确确认、级联范围、其他岗位候选人与共享题库保留。同步更新 `CONTEXT.md`、架构、API、领域、存储、开发进度、路线图及生产前端 bundle。
- 验证：针对性 Vitest `19 passed`，岗位/留存 Python `4 passed`；最终 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-position-lifecycle-pyc .venv/bin/python -m compileall -q app tests`、`.venv/bin/python -m pytest -q`（`165 passed, 5 skipped`）、`npm --prefix app/web test -- --run`（`19 passed`）、`npm --prefix app/web run build` 和 `git diff --check` 全部通过。开发服务已重启为 PID `32351`，`GET /healthz` 返回 `{"status":"ok"}`。
- 失败与恢复：首次全量回归发现 CatalogService 在普通岗位读取时提前初始化 RetentionService，生产测试因无文件签名密钥失败；改为仅在实际删除命令中惰性初始化后全量通过。首次在 sandbox 内停止旧 PID 被权限拒绝，获准后仅停止该进程并成功重启，无数据副作用。
- 未完成事项：无仓库内功能缺口。目标生产环境仍须按既有流程验证私有存储删除权限与事务故障恢复；本轮没有删除现有业务数据，也未调用模型供应商。

## 2026-08-27 · CANDIDATE-SCREENING-CRUD-001

- 目标：在候选人添加简历时按目标岗位执行可解释初筛，在候选人列表展示符合性、入选/淘汰依据和人工复核状态，提供简历查看及候选人完整增删改查，并让未通过初筛的数据在 7 天后进入自动留存清理范围。
- 关联问题：候选人当前只有创建/列表/修改和简历上传，缺少岗位维度初筛、人工复核、简历展示、候选人删除入口及未通过初筛的差异化留存规则。
- 状态：`verified`。
- 计划修改：扩展 ResumeReview 的岗位初筛结论与证据、人工复核命令和候选人筛选投影；增加候选人归档删除与筛选接口；调整留存策略、React 招聘流程、Prompt 合同和测试；同步架构、接口、领域、检索、数据库、统一语言、进度与路线图文档并重建生产前端。
- 安全与产品约束：初筛只使用脱敏简历和工作能力要求，不使用受保护属性；AI 结论是可复核的岗位匹配建议，不是自动录用决定；人工覆盖保留 AI 原结论和审计；简历继续通过短期受控地址展示；清理沿用现有可审计留存删除 seam，不在页面或读取请求中直接物理删除。
- 实际修改：`app/core/prompt/contracts.py` 将简历审阅升级为 `resume_review.v2`，统一校验初筛枚举、分数、摘要、命中项和缺口；`app/providers/mock/provider.py` 提供离线确定性初筛。`app/services/talent.py`、`app/schemas/api.py`、`app/api/routes.py` 增加最新岗位初筛投影、人工复核、候选人逻辑删除和审计；`app/services/retention.py`、`app/workers/{retention.py,celery_app.py}` 增加仅处理到期 `screening_unqualified` 的周期清理。`app/web/src/features/workflow/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/styles.css` 增加候选人增删改查、符合性列、依据详情、人工复核及受控简历查看，并重建 `app/web/dist/`。新增 `tests/test_candidate_screening.py`，扩展 Prompt 和 React 行为测试；同步更新 `CONTEXT.md` 及架构、API、领域、检索/评分、Provider、存储、进度和路线图文档。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_screening_pyc .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/python -m pytest -q` 为 `165 passed, 5 skipped in 7.49s`；`cd app/web && npm run build && npm test -- --run` 生产构建成功、Vitest `19 passed`；`git diff --check` 通过。定向初筛/Prompt/简历/留存测试此前为 `15 passed`。
- 未完成事项或恢复说明：无仓库内功能缺口；未调用真实模型供应商，也未清除现有业务数据。目标部署必须运行 Celery worker 与 Beat 才会执行 7 天自动清理，并用企业授权的真实简历样本校准初筛质量、公平性及误淘汰率；真实 OSS 删除权限仍按既有环境验收流程验证。

## 2026-08-27 · POSITION-INITIAL-REQUIREMENT-001

- 目标：把首版岗位要求合并到“新建岗位”流程，确保岗位创建完成后即可用于候选人简历初筛。
- 状态：`verified`。
- 计划修改：先更新岗位创建 API 合同，再增加岗位与首版要求的原子创建命令；React 新建岗位弹窗补齐要求说明、必备/加分技能、级别和面试时长；增加 API 与前端行为测试并同步领域、进度和路线图文档。
- 不变量：岗位与首版要求必须同组织、同事务创建，任一字段校验或持久化失败不得留下无要求岗位；技能输入规范化后再形成解析画像；本功能不调用模型供应商。
- 实际修改：`app/schemas/api.py` 为岗位创建合同增加受严格校验的 `initial_requirement`；`app/services/roles.py` 抽出统一岗位要求 document 构建逻辑，`app/services/catalog.py` 在一个持久化事务中创建 JobPosition 与首版 RoleRequirement，并在岗位响应中返回 `initial_role_requirement`。`app/web/src/features/workflow/Page.jsx` 将岗位要求标题、职责与要求、必备/加分技能、目标级别和面试时长合并到新建岗位弹窗，技能支持中英文逗号、顿号和换行分隔，成功后刷新岗位要求数据；岗位卡片按现有版本数展示“添加岗位要求/新增要求版本”，兼容升级前岗位。`tests/test_candidate_screening.py` 与 `app/web/src/App.test.jsx` 覆盖原子 API 合同、无效请求不留岗位、完整新建表单和既有岗位补要求；同步更新 API、架构、领域、进度和路线图并重建生产前端。
- 验证：定向 Python `3 passed`、最终 Vitest `21 passed`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_position_requirement_pyc .venv/bin/python -m compileall -q app tests` 通过，`.venv/bin/python -m pytest -q` 为 `166 passed, 5 skipped in 7.71s`，`npm test -- --run` 为 `21 passed`，最终 `npm run build` 成功，`git diff --check` 通过。
- 失败与恢复：首次定向命令误在 `app/web` 目录查找 `.venv/bin/python`，立即以“文件不存在”结束，没有执行测试写入或产生业务数据副作用；随后在仓库根目录重跑通过。
- 未完成事项：无仓库内功能缺口；兼容 API 仍允许旧客户端省略首版要求，新 React 工作台始终提交。未调用模型供应商，也未修改现有岗位数据。

## 2026-08-27 · POSITION-KB-MANAGEMENT-UX-001

- 目标：修复岗位已关联组织内全部题库时按钮显示“暂无可选题库”且被禁用造成的误解，让岗位题库关系始终可查看和继续管理。
- 状态：`verified`。
- 诊断证据：本地运行页当前只有一个题库；第一个同名岗位尚未关联且“选择题库”可用，另外两个岗位已显示该题库标签，因此没有第二个可新增题库，旧 UI 将入口禁用。
- 计划修改：岗位卡片改用“关联题库/管理题库”常驻入口；弹窗区分已关联和可关联题库，无新增项时提供明确说明与题库页入口；补充 React 行为测试、生产构建和文档留痕。不改变岗位—题库关联 API 或已有业务数据。
- 实际修改：`app/web/src/features/workflow/Page.jsx` 删除按 `availableCount` 禁用题库入口的逻辑，岗位未关联时显示“关联题库”、已关联时显示“管理题库”；弹窗同时展示已关联标签和可新增下拉项，现有题库已全部关联或组织尚无题库时显示对应原因与“前往题库管理”。`app/web/src/App.test.jsx` 更新原关联流程断言，并新增“全部题库已关联时入口仍可用”的回归测试；同步开发进度和本日志，重建生产 bundle。
- 验证：`npm test -- --run` 为 `22 passed`，`npm run build` 成功，`git diff --check` 通过。刷新 `http://127.0.0.1:8000/#workflow` 后在实际运行数据验证：两个岗位均显示可用“管理题库”；弹窗展示“测试岗位题库 · tongtong”、已全部关联说明、题库管理入口和 disabled 的提交按钮，控制台交互无阻塞。本轮未写入或修改岗位/题库业务数据。
- 未完成事项：无仓库内功能缺口；岗位题库解除关联仍不是当前 API 合同，本轮没有擅自新增删除关系能力。

## 2026-08-27 · RESUME-REVIEW-CHUNKING-001

- 目标：把简历审阅改成真正后台执行的深模块，并为长 PDF 增加页码感知、Token 预算驱动的分块证据抽取与最终聚合，避免整份长简历一次塞入 LLM。
- 状态：`verified`。
- 领域决策：`ResumeEvidenceChunk` 是 ResumeReview 内部可追溯的分页证据单元，不是候选人结论；`CandidateScreening` 只能由所有成功分块的规范化证据和岗位要求聚合形成。短简历可走单次快速路径，但输出保持相同领域合同。
- 深模块 interface：调用方只使用“排队审阅、读取审阅状态”；字符/Token 预算、分页分段、Map 调用、证据归并、Reduce 调用、重试和错误处理均属于 Resume Review implementation，不暴露到 React 或 API 请求体。
- 计划修改：保留 PDF 页码结构；新增版本化 Map/Reduce Prompt 合同和严格响应 Schema；让 `POST resume-reviews` 返回 `202 + review/job` 而不执行 LLM；Worker 后台按预算选择单次或分块策略并保存进度/证据；React 提交后关闭弹窗并在候选人投影显示处理状态；补齐单元、合同、Worker、API 和 React 测试，同步架构/API/领域/检索/Provider/存储/进度/路线图。
- 安全约束：分块前完成脱敏；不记录完整 Prompt/模型响应；不能静默丢页或截断；部分分块失败不得生成确定性淘汰结论；模型路由缺失/上下文超限必须形成可观察、可重试错误。
- 实际修改：新增 `app/services/resume_review.py` 深模块和 `ResumeEvidenceChunk`，按保守 Token 估算选择 `single_pass/map_reduce`；长简历保留 PDF 页边界，连续页贪心组块，超长单页继续无损切分，并发 Map 只抽项目/技能证据，证据超出 Reduce 预算时分层压缩，最终基于全部证据生成初筛。新增 `resume_review.v3`、`resume_evidence_map.v1`、`resume_evidence_compaction.v1`、`resume_review_reduce.v1` 严格合同，四阶段复用现有 `resume_review` 模型路由。`TalentService` 的创建审阅只排队并由 API 返回 `202 {review,job}`，Worker 保存阶段/分块进度、页码、Prompt/Provider/用量摘要和结构化错误；任一分块失败不写 CandidateScreening。PDF/URL 上传可同时携带岗位与要求，摄取成功事务原子创建后续审阅工作项。React 移除 30 秒阻塞轮询，提交后立即关闭并在候选人列表/详情展示摄取、证据抽取、聚合或失败状态；存在处理中的候选人时每 2.5 秒自动刷新投影，完成后停止；处理中不启动 7 天清理。
- 修改文件：`app/core/prompt/contracts.py`、`app/services/{resume_review,talent,resume_ingestion}.py`、`app/{api/routes,schemas/api}.py`、`app/providers/mock/provider.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/{test_candidate_screening,test_position_resume_appointment_flow,test_prompt_governance}.py`、`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,development-progress,implementation-roadmap,change-log}.md`。
- 验证：新增三页长简历回归把必备 Python 证据只放在第 3 页，确认上传立即返回、摄取和审阅分两次 Worker 执行、最终策略为 `map_reduce`、所有分块完成且匹配证据保留 `source_pages=[3]`；现有同步审阅测试改为验证 `202/queued -> Worker -> ready_for_review`。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-resume-review-final-pyc .venv/bin/python -m compileall -q app tests` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `168 passed, 5 skipped in 7.82s`；`npm run build` 成功，`npm test -- --run` 为 `23 passed`；`git diff --check` 通过。
- 失败与恢复：首次直接 `py_compile` 尝试写 macOS 默认缓存目录被权限拒绝，改用 `/private/tmp` 缓存后通过；首次定向测试暴露移除旧脱敏 helper 时误删 `re` import，导致候选人手机号规范化 `NameError`，恢复 import 后 `16 passed` 并完成全量回归。最终只读状态检索命令把 Markdown 反引号置于双引号 shell 参数中，shell 尝试执行不存在的 `verified` 命令；命令前半段 `git diff --check` 已通过，随后改用无反引号的安全检索确认日志状态。以上失败均无业务数据、外部模型调用或供应商费用。
- 未完成事项：仓库内无遗留。生产仍需根据所选模型上下文窗口校准三个输入预算和并发数，并用授权真实长简历做证据准确率、误淘汰率、延迟和成本验收；部署必须运行 DurableWorkItem/Celery Worker，API 进程本身不会执行 LLM。

## 2026-08-27 · POSITION-CARD-OVERFLOW-MENU-001

- 目标：把岗位卡片底部并排的“编辑 / 删除”收进右侧三点展开菜单，改善操作区排版并保留危险操作语义与原删除确认流程。
- 状态：`completed`。
- 计划修改：调整 React 岗位卡片、菜单样式与行为测试，重建生产前端 bundle；不修改岗位、候选人或删除 API。
- 实际修改：`app/web/src/features/workflow/Page.jsx` 将岗位卡片操作区改为“选择题库 + 三点菜单”，菜单包含编辑岗位和红色删除岗位，失焦或 Esc 自动关闭；`app/web/styles.css` 增加靠右、向上展开且不撑破卡片的菜单样式；`app/web/src/App.test.jsx` 改为先展开菜单再验证编辑和危险删除流程；重建 `app/web/dist/`。
- 验证：`npm --prefix app/web test -- --run` 为 `19 passed`，`npm --prefix app/web run build` 成功，`git diff --check` 通过。本地浏览器在实际招聘流程数据上确认三点按钮与卡片右边缘对齐、菜单包含“编辑岗位 / 删除岗位”、删除项使用危险色，Esc 后菜单关闭。
- 未完成事项：无；未修改或删除任何业务数据。
## 2026-08-28 · RESUME-DOCUMENT-CRUD-001

- 目标：补齐候选人简历版本的增查改删闭环，并按用户明确要求删除候选人 `candidate_400e16c5c49f470a` 的最新简历版本 `resume_35faf6ea40b147b8`；保留更早版本。
- 状态：`verified`。
- 领域决策：ResumeDocument 的 PDF 内容与版本继续不可变；“修改”只允许修改展示文件名，替换内容必须上传新版本。删除命令取消未领取的摄取/审阅工作、清理隔离文件和私有文件对象、保留最小审计事实；已被计划或面试历史引用的版本禁止删除。
- 计划修改：增加简历 PATCH/DELETE API、乐观并发与引用保护；让候选人投影忽略已删除简历/审阅；在简历详情增加重命名和删除操作；补齐后端与 React 测试并同步 API、领域、存储、进度和路线图文档。
- 删除前只读证据：当前候选人有两个 processing 版本；最新版本为 `resume_35faf6ea40b147b8`（version 2，创建于 `2026-08-28T06:52:13Z`），其 `resume.ingest` 工作项 `work_496b51dfcef4446b` 为 `pending/attempt_count=0`，尚未产生 ResumeReview，适合取消并清理；旧版本 `resume_095cc59e373049a7` 不在删除范围。
- 实际修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/resume_ingestion.py`、`app/services/talent.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`tests/test_resume_ingestion.py`、`docs/api-design.md`、`docs/domain-model.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际实现：新增简历展示名 PATCH 与带 `expected_version` 的 DELETE；删除前检查计划和面试快照引用、拒绝运行中工作，取消未领取的摄取/审阅工作，清理隔离文件、原 PDF/解析文本/未入历史的派生资产，清空审阅证据并保留 `deleted` 审计占位。候选人列表、简历列表与初筛投影忽略删除中/已删除版本，删除审阅后同步校正 7 天初筛留存状态。React 候选人详情可按版本查看、改名和删除，并明确 PDF 内容替换通过重新上传形成新版本。
- 业务数据操作与结果：重启后端加载新路由后，以 `expected_version=1` 删除 `resume_35faf6ea40b147b8`，响应为 `status=deleted/version=3/deleted_at=2026-08-28T08:16:02Z`；工作项 `work_496b51dfcef4446b` 为 `cancelled/attempt_count=0` 且隔离文件不存在。列表只剩 `resume_095cc59e373049a7`（resume_version 1、processing），候选人初筛投影回落到该旧版本。
- 验证命令与结果：`.venv/bin/python -m pytest tests/test_resume_ingestion.py -q` 为 `8 passed`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-resume-crud-pyc .venv/bin/python -m compileall -q app tests` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `170 passed, 5 skipped`；`npm test -- --run` 为 `24 passed`；`npm run build` 成功；`git diff --check` 通过；删除后通过 API 与隔离路径只读检查验证旧版本保留、目标版本删除、工作取消和隔离文件移除。
- 失败与恢复：首次定向验证误用系统不存在的 `python` 命令且在仓库根执行 `npm test`，分别因命令不存在和根目录无 `package.json` 失败，未修改数据；改用 `.venv/bin/python` 并在 `app/web` 执行后通过。首次沙箱内 `curl` 无法连接宿主机 8000 端口，未发出请求、未删除数据；切换到获批的本机网络上下文后先重新核对精确 ID/version，再执行一次删除。
- 未完成事项：按用户范围保留的旧版本 `resume_095cc59e373049a7` 仍有一个未领取的摄取工作项，因此界面会继续显示该旧版本“处理中”；本工作项未启动 Worker，避免未经本次请求授权触发 LLM 调用与费用。真实 OSS 删除失败恢复和跨实例并发仍随目标部署环境验收。

## 2026-08-28 · RESUME-REVIEW-RETRY-001

- 目标：为失败的简历初筛增加面向招聘工作台的安全重试机制，修复当前 `resume_review_5b4a107888104f43` 因模型超时、断路和 dead-letter 后无法由业务界面恢复的问题。
- 状态：`verified`。
- 诊断证据：PDF 摄取工作 `work_bfeaaa128e1f46f4` 已完成；审阅工作 `work_0fe0350905f84095` 连续 5 次调用 DeepSeek `deepseek-v4-pro` 均约 30 秒 `provider_timeout`，达到路由阈值 3 后出现 `provider_circuit_open`，最终为 `dead_letter/attempt_count=5`。当前 `resume_review` 路由 timeout 为 30 秒、无 fallback；错误不是候选人不符合结论。
- 计划修改：新增版本化 ResumeReview retry 命令与审计、只允许 failed/dead-letter 工作恢复；候选人失败投影和 React 详情提供“重新初筛”；补充 API/领域/进度文档与后端/React 测试。经用户授权把当前路由超时提高到适合简历任务的值，验证模型后重放当前工作并观察最终状态；没有第二个已验证真实 LLM 时不使用 mock 作为招聘决策 fallback。
- 实际修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/talent.py`、`app/services/resume_review.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/test_candidate_screening.py`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际实现：新增 `POST /api/v1/resume-reviews/{review_id}/retry`，以审阅 version 校验并在同一事务恢复 `failed/dead_letter` 审阅和工作项、清空错误/进度、attempt 归零、递增 `replay_count` 并写 `resume.review.retried` 审计；源简历、岗位或要求无效时拒绝恢复。候选人详情在失败态显示中文错误和“重新初筛”，调用领域重试接口而非通用管理员 Outbox。Resume Review 工作租约从通用 60 秒提高到 330 秒，覆盖 Celery 300 秒 hard limit；单次/Reduce 输出预算默认提高到 6000 tokens，Map/压缩提高到 4000 tokens，并提供四个独立环境变量，防止推理模型在完整 JSON 前耗尽输出预算。
- 运行配置与真实恢复：经用户授权将 live route `route_27e636f528f94fe3` timeout 从 30 秒调整为 120 秒（version 2），真实 route probe 三次均成功且未配置 mock fallback。最终以原因“扩大结构化输出预算并修复长任务租约后重试”重放同一工作项；Worker 单次 attempt 在 63.9 秒完成，工作项为 `completed/attempt_count=1/replay_count=3`。审阅 `resume_review_5b4a107888104f43` 为 `ready_for_review/version=30`，策略 `single_pass`、分数 50、建议 `manual_review`、模型用量 3311 input + 4552 output；候选人投影同步为待人工复核且不设置 7 天清理期限。
- 验证命令与结果：`.venv/bin/python -m pytest tests/test_candidate_screening.py tests/test_prompt_governance.py -q` 为 `15 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `171 passed, 5 skipped`；`npm test -- --run` 为 `25 passed`；`npm run build` 成功；真实模型探测、业务 retry、Worker 日志、审阅/候选人/工作项 API 均完成核对；`git diff --check` 通过。
- 失败与恢复留痕：首次调整 timeout 后探测时后端不在线，请求未触发模型；重启后探测成功。第一次 live replay 暴露 60 秒租约短于约 86–90 秒模型流程，Beat 重复领取并耗尽 attempt；修复 330 秒租约后第二次 replay 不再抢占，但真实响应暴露 `provider_schema_invalid`：2400-token 输出预算导致 `finish_reason=length` 或推理后正文为空，故停止 Worker 避免继续费用。提高输出预算、等待断路冷却并再次探测后，第三次 replay 一次成功。曾在仓库根误执行 `npm test`，因无 `package.json` 失败且无仓库副作用，随后在 `app/web` 正确执行。历史失败 invocation 和 replay 审计按事实保留。
- 未完成事项：当前只有一个已验证真实 LLM 配置，因此未设置供应商 fallback；生产环境仍应增加第二个真实模型并按真实简历集校准超时、输出预算、费用上限和断路策略。

## 2026-08-28 · CANDIDATE-SCREENING-SCORE-BANDS-001

- 目标：把候选人初筛分数转换为统一、可审计的符合性标准：0–59 分不符合，60–74 分待人工复核，75–100 分符合；人工复核仍可覆盖 AI 建议并保留原始结论。
- 状态：`verified`。
- 边界决策：用户描述的“60–75 需要人工”和“75 分以上符合”在 75 分重叠；本工作项采用无重叠区间并将 75 分归入符合，即 `score < 60 -> unqualified`、`60 <= score < 75 -> manual_review`、`score >= 75 -> qualified`。
- 计划修改：集中定义 CandidateScreening 分数带并在模型结果写入领域对象前强制归一化 recommendation；更新版本化 Prompt 合同、Mock Provider、候选人工作台标准说明、领域语言及 API/领域/检索/进度文档，增加边界值和模型建议冲突测试并重建前端。
- 实际修改文件：新增 `app/domain/candidate_screening.py`、`tests/test_candidate_screening_policy.py`；修改 `app/services/talent.py`、`app/services/retention.py`、`app/core/prompt/contracts.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/src/App.test.jsx`、`tests/test_candidate_screening.py`、`tests/test_prompt_governance.py`、`CONTEXT.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`，并重建 `app/web/dist/`。
- 实际实现：以 `candidate_screening_score.v1` 集中定义 0–59/60–74/75–100 三个分数带；LLM 结果通过统一 Schema 后、写入 ResumeReview 前再次由领域策略强制归一化 recommendation 并记录策略版本，模型枚举与分数冲突时以分数为准。候选人投影、审阅读取和 7 天留存判断都从分数派生 AI 建议，人工决定继续优先且不修改 AI 分数与证据。周期 Retention Worker 会先校正存量记录的期限，旧记录首次命中新规则时从校正时起给足 7 天，不由 GET 隐式写入、更不会追溯立即删除。单次 Prompt 升级为 `resume_review.v4`，长简历 Reduce 升级为 `resume_review_reduce.v2`；Mock Provider 保持供应商协议职责，不复制领域分数带。React 候选人列表和详情展示完整分数标准，并统一使用“待人工复核”。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate_screening_policy.py tests/test_candidate_screening.py tests/test_prompt_governance.py -q` 为 `29 passed`；全量 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `185 passed, 5 skipped`；`npm test -- --run` 为 `25 passed`；`npm run build` 成功；`git diff --check` 通过。测试覆盖 0/59/60/74/75/100、非法分数、模型建议冲突、人工覆盖、单次与 Map-Reduce 写入策略版本、候选人投影、存量期限校正和工作台文案。
- 本机运行验收：启动 Redis、FastAPI（PID `46923`，`127.0.0.1:8000`）、Vite（`127.0.0.1:5173/web/`）与 Celery Worker/Beat；`GET /healthz` 返回 `{"status":"ok"}`。Worker 首轮 dispatch 为 0，留存任务为 `candidate_count=0/reconciled_candidate_ids=[]`，只写入批次审计 `audit_78109d98c98744f2`，未触发 LLM 或候选人删除。浏览器实页确认列表和王凯 15 分详情均显示三段标准，15 分为不符合、展示证据/缺口/人工复核和 7 天清理日期；页面还原并保留在招聘流程。
- 失败与恢复留痕：服务启动前的沙箱内 `ps` 被系统拒绝，沙箱内本机 `curl`/`redis-cli` 也因本地网络隔离无法连接；这些只读检查未产生副作用。随后在获批的本机执行上下文启动服务，并分别通过 HTTP、Celery 日志和浏览器实页完成验证。
- 未完成事项：无仓库内遗留；本工作项未调用真实 LLM、未重跑历史简历、未直接修改候选人业务数据。存量期限校正会在下一次周期 Retention Worker 运行时按上述规则发生并进入现有留存审计链路。

## 2026-08-28 · RESUME-REVIEW-OUTBOX-IDEMPOTENCY-001

- 目标：修复重新上传相同 PDF 后 ResumeReview 长期显示 queued、但 Worker 没有可执行 `resume.review` 工作项的问题，并恢复当前张文君孤儿审阅。
- 状态：`verified`。
- 诊断证据：Celery 以 prefork concurrency 10 运行，`inspect active/reserved` 均为空，Beat 连续返回 `dispatched=0`，因此并非单 Worker 串行瓶颈。最新审阅 `resume_review_0311403cea72404d` 为 queued，但 Outbox 中没有指向它的工作项；其 `input_hash=sha256:49924...` 与已删除旧审阅相同，旧幂等键 `resume.review:{input_hash}` 命中了已完成工作 `work_0fe0350905f84095`，导致新审阅创建成功而新任务被错误折叠。
- 实际修改：`app/services/resume_review.py` 新增版本级工作幂等键 `resume.review:{resume_document_id}:{input_hash}` 和统一 `_enqueue_review_work` seam；排队入口现在会校验 Outbox 工作项 `aggregate_id` 必须指向当前 ResumeReview，并在同一 queued 审阅缺少工作项时自愈补建。这样，同一 ResumeDocument 的重复请求仍保持幂等，而内容相同但版本不同的 ResumeDocument 会各自获得独立工作项。
- 回归覆盖：`tests/test_candidate_screening.py` 新增“相同 PDF 的两个不同简历版本生成不同审阅工作项”和“重复排队命令修复 queued 孤儿审阅”测试；同步更新 `docs/api-design.md`、`docs/database-and-vector-storage.md`、`docs/architecture.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`，明确上传幂等、审阅工作身份边界和自愈语义。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate_screening.py -q` 为 `8 passed`；候选人/摄取/持久化扩展集为 `34 passed`；完整后端测试为 `187 passed, 5 skipped`；`npm test -- --run` 为 `25 passed`；`npm run build` 成功；`git diff --check` 通过。
- 数据恢复证据：通过现有领域 API 重排当前张文君审阅 `resume_review_0311403cea72404d`，创建工作 `work_7ab7a59d1ed04da1`，新幂等键包含 `resume_d7255a18f1224515` 且 `aggregate_id` 指向当前审阅。Worker 仅执行一次，约 `108.3s` 完成；审阅状态变为 `ready_for_review/completed`、策略 `single_pass`、进度 `1/1`、无错误，最终匹配分 `65`，按 `candidate_screening_score.v1` 进入 `manual_review`。恢复后查询不存在 queued 且无对应 Outbox 工作项的审阅。
- 运行状态：后端已重新启动并在 `127.0.0.1:8000` 返回 `healthz={status: ok}`；前端已重新启动并在 `127.0.0.1:5173/web/` 返回 HTTP 200；Celery Worker/Beat 已重新启动，prefork concurrency 为 10，`celery inspect active` 显示 1 个节点在线且当前为空。Redis 本次未重启，因为本机 broker 已在线且 Worker 已成功连接和消费。
- 失败与恢复留痕：首轮新增回归测试未给两次相同文件上传设置不同上传幂等键，因此被摄取 API 按预期折叠为同一 ResumeDocument；修正测试以两个显式 `Idempotency-Key` 表达“相同内容、不同版本”后通过，未影响业务数据。尝试停止旧终端会话时返回 unknown process id；只读进程检查确认旧 API/Vite/Worker 已退出，随后完成全新启动。最终检查中沙箱内 Celery 连接本机 Redis 被拒绝，改用获批的本机只读检查后确认 Worker 正常；首次孤儿查询误把 JSON 字段当作物理列，按实际表结构修正后返回空集。这些检查失败均未产生业务副作用。
- 未完成事项：无。

## 2026-08-28 · QUESTION-GENERATION-TRUNCATION-RECOVERY-001

- 目标：修复智能生题分片因 `finish_reason=length` 输出截断而被误报为通用 JSON Schema 错误、同参数重复计费，并为双槽位分片提供保留成功结果的自适应单槽位恢复。
- 状态：`verified（仓库）`。
- 计划修改：在 Provider seam 增加不泄露正文的输出截断错误与失败用量诊断；让 Model Invocation 和 DurableWorkItem 区分同请求可重试与终止错误；由 QuestionGenerationService 在双槽位截断后拆成两个单槽位工作；升级版本化生题 Prompt/Schema 的长度和数量边界；增加 Provider、网关、持久工作和生题工作流合同测试，并同步架构、领域、Provider、检索、数据库、进度与路线图文档。
- 实际修改文件：修改 `app/providers/openai_compatible/provider.py`、`app/model_gateway/gateway.py`、`app/persistence/interface.py`、`app/services/question_generation.py`、`app/core/prompt/contracts.py`、`tests/test_openai_compatible_provider.py`、`tests/test_model_invocation.py`、`tests/test_persistence_contract.py`、`tests/test_prompt_governance.py`、`tests/test_question_generation.py`、`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际实现：OpenAI-compatible adapter 在 JSON 解析前把 `finish_reason=length` 映射为不可同参数重试的 `provider_output_truncated`，只附带 finish reason、请求预算、content length 和 usage/reasoning token；ModelInvocationLog 在失败 attempt 保存这些脱敏诊断。DurableWorkItem 对 `retryable=false` 首次失败立即 dead-letter，并从自动 claimable 集合排除，人工 replay 仍保留。智能生题 route 去掉网关内嵌重试，一个 work attempt 最多调用一次 Provider；生成预算调整为单槽位 4000、双槽位 8000 tokens。双槽位截断时原工作以 `split_into_single_slot_chunks` 完成、原 chunk 进入 superseded，并在同一事务创建两个单槽位替代工作；单槽位仍截断则终止并保留显式人工重试入口。生题 Prompt/Schema 升级为 v2，并收紧标题、题干、答案、关键点、别名和技能边界。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_openai_compatible_provider.py tests/test_model_invocation.py tests/test_persistence_contract.py tests/test_prompt_governance.py tests/test_question_generation.py -q` 为 `53 passed`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `193 passed, 5 skipped`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-question-truncation-pycache .venv/bin/python -m compileall -q app tests` 成功；`git diff --check` 通过。合同覆盖 Provider 分类/脱敏诊断、网关不重复调用与失败 usage、Memory/SQLite 非重试终止、双槽位拆分继续完成及单槽位首轮终止。
- 未完成事项或恢复说明：未自动重放截图中的历史批次，未调用真实 LLM，也未修改现有业务数据；目标 DeepSeek 账户的真实截断恢复、10 题并发、费用和限流仍属于部署环境验收。仓库在本工作项开始前已有大量用户修改和未跟踪文件，本次均保留，未执行 reset/checkout 或清理。

## 2026-08-28 · QUESTION-SPEECH-DEFAULT-PREVIEW-001

- 目标：修复新题库在已有真实默认 TTS 路由时仍绑定开发 mock、题目显示语音 ready 却无法试听的问题，并把未配置、模拟资产和私有音频异常转换为普通用户可理解且可操作的提示。
- 状态：`completed（仓库验证）`。
- 诊断证据：用户请求的 `speech_7daf78a0419f437e` 使用 `model_cfg_mock_tts_synthesize`，`audio_uri=mock-tts://...`、`file_object_id=null`、`production_ready=false`，因此试听签名接口返回 `QUESTION_SPEECH_ASSET_NOT_PRIVATE`。创建该题库前组织已经存在 enabled 的 `tts.synthesize + question_speech_generation` 路由，primary 指向 ready 的 `GlM-TTS`，但 `CatalogService.create_knowledge_base` 在非生产环境无条件覆盖为开发 mock profile。当前题目后来通过显式题库配置已生成新的私有资产，本工作项不改写或删除历史资产。
- 计划修改：新题库优先从明确的 `question_speech_generation` route 冻结 ready TTS 模型和默认音色，仅在没有真实默认路由的本地测试路径保留开发 mock；试听接口返回专门的模拟资产错误和操作建议；React 将开发 mock 视为不可试听配置并展示“配置语音后可试听”；增加 route/profile、资产访问和页面行为测试，同步接口、领域、Provider、进度与路线图文档。
- 实际修改文件：`app/services/catalog.py`、`app/services/knowledge_base_speech.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/test_knowledge_base_speech.py`、`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_knowledge_base_speech.py tests/test_documented_gap_apis.py -q` 为 `8 passed`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `194 passed, 5 skipped`；前端 `npm test -- --run`（`app/web`）为 `26 passed`；`npm run build`（`app/web`）成功生成生产 bundle；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-question-speech-pycache .venv/bin/python -m compileall -q app tests` 成功；`git diff --check` 通过。曾在仓库根目录误运行一次 `npm test -- --run`，因根目录没有 `package.json` 返回 `ENOENT`，未产生仓库副作用，随后在 `app/web` 正确执行并通过。
- 未完成事项或恢复说明：未调用真实 TTS、未自动重建旧题库语音，也未改写或删除历史 mock 资产；旧 `speech_7daf78a0419f437e` 本身没有音频字节，仍会按设计返回明确的“开发模拟语音不可试听”提示。已有题库不会因组织默认路由变化而被静默改配；可通过题库语音配置显式切换并生成新私有资产。真实供应商音质、费用、限流和对象存储签名仍属于部署环境验收。仓库在本工作项开始前已有大量用户修改和未跟踪文件，本次均保留，未执行 reset/checkout 或清理。

## 2026-08-28 · DOMESTIC-REALTIME-MEDIA-AND-CALIBRATION-001

- 目标：接入国内真实流式 STT 与实时数字人 WebRTC/SFU 路由；在不把厂商协议写入面试业务 module 的前提下，为候选人房间提供实时语音识别和数字人会话生命周期；增加只接受脱敏真实候选人样本的评分一致性与公平性校准工作流。
- 状态：`completed（仓库验证；environment/data pending）`。
- 计划修改：扩展 DashScope Provider 的实时/批量 ASR；新增腾讯云智能数智人 WebRTC adapter 与会话关闭语义；补齐统一 Avatar 会话 schema、路由配置和 React 播放/回收；增加脱敏校准数据 schema、导入/运行/报告接口与自动化测试；同步架构、接口、领域、检索评分、Provider、数据库、问题、进度和路线图文档。所有账号、AppID、Secret、资产 ID 与项目 ID 仅通过管理员连接/模型配置或环境 Secret 注入，仓库默认留空。
- 实际修改文件：`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、新增 `app/providers/tencent_cloud_avatar/`、`app/model_gateway/schemas.py`、`app/services/model_admin.py`、`app/services/avatar.py`、`app/services/fairness.py`、`app/schemas/api.py`、`app/api/routes.py`、新增 `app/web/candidate/pcm-stream.js` 与 `app/web/public/web/webrtc-player.html`、`app/web/src/features/candidate/Page.jsx`、`app/web/styles.css`、重建 `app/web/dist/`、`tests/test_dashscope_provider.py`、新增 `tests/test_tencent_cloud_avatar_provider.py`、`tests/test_model_configuration_v2.py`、`tests/test_fairness_evaluation.py`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。本轮没有新增持久聚合、表、索引或迁移，校准仅解析现有 current evaluation 并写最小审计，因此 `docs/database-and-vector-storage.md` 无结构变更。
- 实际实现：DashScope 新增 `qwen-audio-3.0-asr-flash-streaming` duplex WebSocket 和 `qwen3-asr-flash` 私有音频 batch adapter，候选人浏览器通过 Web Audio 输出 16kHz/单声道/16-bit PCM 到服务端 STT WebSocket；服务端继续持久化同一录音，stream final 失败时走已有 batch repair。腾讯云数智人插件按官方协议用 HTTPS create-by-asset/stat/start/close、带 `requestid=SessionId` 的 HMAC-SHA256 签名 WSS command channel 发送 `SEND_TEXT` 并等待同 ReqId 播报状态；失败、换流、离场和后台模型/路由探测都会关闭会话释放并发。腾讯 WebRTC/SFU 媒体通过同源 TCPlayerLite 页面播放，业务 WebSocket 不承载视频帧。数字人 route 失败时只降级到已冻结的真实私有题目音频，关闭操作固定 primary 且不跨供应商。评分校准 API 只接受 2–5000 条 current `evaluation_id + human_score + opaque cohort`，拒绝姓名/联系方式/简历/转写等额外字段，输出 MAE、RMSE、有符号偏差、±5/±10 一致率、题型/语言/STT 质量/cohort 分层、样本量告警和永不自动生效的线性拟合。
- 协议核验：对照阿里云官方实时 ASR WebSocket、客户端/服务端事件与 Qwen3-ASR 文档，以及腾讯云云渲染会话概览、建长连接、文本驱动、下行状态和官方 H5 Demo。初版腾讯实现把 `SEND_TEXT` 误建模为 HTTP；提交前核验发现后，已改为官方要求的 WSS command channel，并增加“WSS 驱动失败仍关闭计费会话”的合同测试。
- 验证命令与结果：定向媒体/路由/公平性回归为 `26 passed`；最终完整后端 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-domestic-release-pyc .venv/bin/python -m pytest -q` 为 `203 passed, 5 skipped`；前端 `npm test -- --run` 为 `29 passed`；`npm run build` 成功；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-domestic-compile-pyc .venv/bin/python -m compileall -q app tests` 成功；`git diff --check` 通过。测试使用 MockTransport/Fake WebSocket，不调用厂商、不产生云端费用。
- 失败与恢复留痕：首次不指定缓存目录运行 `compileall` 时，macOS 默认 `__pycache__` 写入被沙箱拒绝；改用 `/private/tmp` 缓存后成功。一次只读 `rg` 检索把 Markdown 反引号放入双引号 shell 参数，触发 `zsh: command not found: SEND_TEXT`；同条命令中的测试仍通过，随后用安全参数完成检索，无仓库或外部副作用。
- 未完成事项或恢复说明：目标阿里云 Workspace/API Key/模型授权、腾讯 AppKey/AccessToken/形象资产/会话并发均按用户要求留空，所以没有创建 ready 的生产 route，也没有真实验证 WER、partial 延迟、口型、浏览器网络、并发和费用；提供后需在模型服务页创建连接、模型配置并通过探测，再为 `candidate_answer_transcription`、`candidate_answer_repair`、`interview_question_delivery` 建 route。用户尚未提供经授权的真实候选人脱敏 current evaluation 金标，实际评分/公平性校准未运行，不能形成业务结论；建议至少 30 条且每个 opaque cohort 至少 10 条。TCPlayerLite 当前从腾讯 CDN 加载，生产内网部署还需验证 CDN 可达或按腾讯授权包提供本地 fallback。
