# 操作变更日志

本文件记录所有会修改仓库内容的工作项。只读检查不单独登记；同一目标下的代码、测试、迁移和文档修改合并为一个工作项。状态使用 `in_progress`、`verified`、`failed` 或 `cancelled`，历史条目只追加或补充结果，不删除。

## 记录格式

每个工作项必须包含：日期、ID、目标、关联问题、状态、实际修改文件、验证命令与结果、未完成事项或恢复说明。

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
