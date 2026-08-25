# 开发进度

## 2026-08-25 完成快照

仓库内可独立完成的里程碑 0-13 能力已经实现并通过自动化验证：岗位题库构建、PDF/URL 简历安全摄取、私有文件、候选人/计划/预约、明确同意、可审计随机抽题、服务端 streaming/batch STT、评分、报告导出、企业复核、RBAC/审计、Outbox 加固、PostgreSQL/RLS adapter、Redis 事件 adapter、心跳监控和抽题公平性评估均已有代码与测试。

这里的“完成”只表示仓库实现与本地/离线验收完成，不等于外部生产环境已经通过。OpenAI-compatible、DeepSeek、智谱与 DashScope/千问的 LLM HTTP adapter 已落地，OpenAI-compatible、智谱 GLM-TTS 与 DashScope 还覆盖 TTS 子集；但真实 PostgreSQL/Redis、阿里云 OSS、恶意文件扫描器、外部模型/语音账号和视频数字人仍需要部署环境、区域、凭据和测试数据；生产 readiness 在这些依赖缺失或健康检查过期时失败关闭。

当前统一验证基线：

- `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
- `node --check app/web/app.js`：通过。
- `.venv/bin/python -m pytest -q`：`106 passed in 3.63s`。
- `git diff --check`：通过。

## 里程碑对账

| 里程碑 | 仓库状态 | 已完成证据 | 外部/兼容边界 |
| --- | --- | --- | --- |
| 0 项目骨架 | ✅ verified | FastAPI、统一错误、健康检查、启动/worker 命令、自动化测试 | 生产观测平台由部署环境选择 |
| 1 题库管理 | ✅ verified | CRUD/归档、JSON 批量 import、rebuild/build job、语音重建 | 批量 UI 仍以 API 为主 |
| 2 模型网关 | ✅ closed（配置 v2） | `chat_json/chat_text/embedding/STT/TTS/avatar` schema、invoke/open_stream、重试/fallback/超时/共享断路器、加密凭证；ProviderConnection/ModelConfiguration/ModelRoute、后端动态表单与一次性迁移；OpenAI-compatible、DeepSeek、智谱与 DashScope/千问 adapter | 真实凭据、区域、模型授权和健康测试待联调；STT/数字人仍需选型 |
| 3 结构化题库查询 | ✅ closed | Question Catalog、Memory/SQLite/PostgreSQL 下推实现、跨岗位拒绝；旧 QuestionService/向量 repository 已删除 | 真实 PostgreSQL 查询计划待环境验收 |
| 4 岗位要求与计划 | ✅ closed | execution v2 canonical slots、显式一次性迁移、候选池冻结、覆盖/难度/去重、权重/时长守恒、审批不可变 | 部署旧数据时先运行迁移命令 |
| 5 会话与实时事件 | ✅ verified | 生命周期、持久事件、WebSocket、Redis 跨实例 adapter、心跳超时恢复 | WebRTC 媒体仍为外部集成项 |
| 6 数字人与语音 | ✅ verified（TTS adapter/语音协议） | streaming/batch STT、OpenAI-compatible/智谱 GLM-TTS/DashScope TTS、私有资产复制、avatar seam、断流 batch 修复 | TTS 真实凭据与生产 route 未验收；STT/视频 adapter 待选型 |
| 7 评分与报告 | ✅ verified | 可解释评分、append-only revision、current-only 汇总、JSON/CSV 导出 | 真实 LLM 金标校准待业务数据 |
| 8 岗位题库构建 | ✅ verified | import/rebuild/build、结构校验、Outbox、语音版本/readiness | 真实 TTS 音质与区域策略待联调 |
| 9 企业简历库 | ✅ verified | multipart/URL、SSRF、隔离/扫描、PDF 解析、原件/解析文本私有 FileObject、本地/OSS contract、加密联系人 | 真实 OSS/扫描器待环境验收 |
| 10 预约与填报 | ✅ verified | PATCH、哈希 token、邀请 UI、服务端告知/明确同意、强匹配、时间/设备/model gate、原子幂等 start、候选人安全投影 | 邮件/短信发送未选择通道 |
| 11 可审计语音闭环 | ✅ verified | HMAC 选题、唯一选择事实、streaming final、batch 修复、两阶段评分 | 真实 STT WER/延迟待录音集 |
| 12 企业复核 | ✅ verified | reviewer 权限、签名音频、授权与实际下载审计、转写/评分/报告 revision、导出 | ATS 人工决定集成不属于 AI 报告 |
| 12.5 核心一致性 | ✅ closed | `PLAN/CONSENT/APPOINTMENT/REPORT/SEARCH/CANDIDATE-ACCESS` 回归矩阵；旧 runtime interface 已删除 | 外部环境验收独立列示 |
| 13 生产化/公平性 | ✅ verified（仓库） | PostgreSQL/RLS migration、RBAC、审计、加密、Outbox dead-letter、共享断路器、Redis bus、心跳、公平性 API | 外部服务均标 `environment_pending` |

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

### 模型、语音与 readiness

- `llm.chat_text` 已加入统一 schema；OpenAI-compatible 支持 Chat/Embedding/Speech TTS，DeepSeek/智谱 Chat 复用共享 runtime 并适配 JSON Object，智谱另实现官方 GLM-TTS，DashScope 支持 Qwen Chat/Embedding 以及 Qwen3-TTS/CosyVoice，均有离线 HTTP 合同测试。
- 模型管理已拆分为 `ProviderConnection → ModelConfiguration → ModelRoute`：Provider manifest 声明连接/凭证及 `llm/embedding/tts/stt/avatar` 模型表单，管理 UI 通用渲染；模型测试更新健康事实，route target 只引用 ready 的模型配置。未知字段返回 422、同组织重复 capability/purpose 返回冲突、predefined 目录外模型返回冲突；生产缺少精确 route 时返回 `provider_route_missing`。
- OpenAI-compatible 共享 transport 默认不继承环境代理，只有显式 `use_environment_proxy=true` 才读取代理变量；缺少 SOCKS transport 等初始化错误映射为 `provider_transport_unavailable`，不再泄漏原始 500。测试 helper 强制新建内存 store；当前全量 `106` 项测试不会触碰开发 SQLite。
- `ModelGateway.open_stream()` 只在音频接受前允许 fallback；`ValidatedSTTStream` 校验 chunk/总量、事件序号和唯一 authoritative final。
- `stt-stream` WebSocket 保存音频，stream final 直接提交评分，final 缺失/断流时使用 `stt.batch` 修复。
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
- 报告始终 `human_decision_required=true`，不写录用/淘汰；真实人工金标、STT WER、评分一致性和漂移评估需企业样本。

## 外部环境待验收

| 项目 | 状态 | 为什么不能在仓库内宣称完成 | 已提供的验收入口 |
| --- | --- | --- | --- |
| PostgreSQL 集群/RLS/查询计划 | `environment_pending` | 当前环境没有可用 DSN/服务端 | adapter、migration、缺 DSN fail-fast、SQL 不变量测试 |
| Redis 多实例广播 | `environment_pending` | 当前环境没有 Redis 集群和第二实例 | `RedisRealtimeEventBus` 与 lifespan subscriber |
| 阿里云 OSS | `environment_pending` | 没有 bucket、RAM 凭据和区域 | OSS adapter、SSE/签名 fake-bucket contract |
| 恶意文件扫描器 | `environment_pending` | 没有 ClamAV/企业扫描服务 | production 必配 command、超时/返回码 fail-closed、EICAR 测试 |
| OpenAI-compatible/DeepSeek/智谱/DashScope 模型服务 | `environment_pending` | HTTP adapter 已实现，但当前没有真实 API Key、区域/模型授权、费用/延迟和结构化输出稳定性数据 | 离线 HTTP 合同、声明式模型目录、动态 route UI、私有 TTS copy/hash 与 readiness |
| 真实 STT/视频数字人 | `external_choice_required` | 尚未指定厂商、账号、区域、模型和测试录音/视频协议 | 统一 schema、provider manifest、stream/batch repair、avatar 降级 seam |
| 邮件/短信邀请 | `external_choice_required` | 未选择发送通道、域名、模板和合规策略 | 一次性邀请 token/API 已完成，当前由企业安全通道分发 |
| WebRTC/视频口型同步 | `external_choice_required` | 依赖 SFU/数字人厂商会话协议 | WebSocket 音频闭环、avatar seam 和可替换实时网关已稳定 |

## 已关闭的兼容边界

- 管理员直接创建/启动会话、客户端 REST/WebSocket 文本答案、计划运行时 `items`、旧全局题目创建/列表和 `QuestionService`/向量 repository/worker 分支均已物理删除，不再用 production 条件分支隐藏。
- 旧计划只能在应用升级前通过 `python -m app.migrations.plan_execution_v2 --dry-run` 检查，再执行无 `--dry-run` 的显式一次性迁移；运行时不会懒迁移。
- 旧模型配置只能在升级前通过 `python -m app.migrations.model_configuration_v2 data/interviewer.sqlite3 --dry-run` 检查，再执行同命令去掉 `--dry-run`；运行时不读取旧 collection 或 route target。
- 当前仓库 SQLite 数据检查为 0 份 InterviewPlan，未产生数据改写。接口删除和迁移行为由 `tests/test_production_compatibility.py`、`tests/test_plan_assembly.py` 覆盖，`COMPAT-001` 已标 `closed`。
