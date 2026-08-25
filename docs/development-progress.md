# 开发进度

本文件记录当前实现状态。AI 协作者接手时，先读 `AGENTS.md`，再读本文，避免重复搭建或误以为某些模块已经生产可用。

## 当前阶段

当前已完成旧版 0-1 可运行闭环和核心持久化边界。存储已经从纯内存升级为本地 SQLite，业务 module 统一走事务型 Persistence seam；代码中仍有本地 JSON 向量 + Python 余弦相似度实验，但下一版正式抽题和评分已决定不依赖向量数据库。新确认的“岗位题库—简历库—预约—服务端语音面试—企业复核”目标流程尚未形成闭环，不能把旧版直接创建面试和浏览器转写演示视为新需求已完成。

## 已确认的下一版业务目标（尚未实现）

1. `JobPosition` 成为岗位题库、岗位要求、简历审阅、计划和预约的共同上层资源；每个 `KnowledgeBase` 只属于一个岗位。
2. 上传岗位题库后，通过持久异步工作项校验标准答案、关键点、rubric、技能、难度和题型，并生成每道题的 `QuestionSpeechAsset`；不生成必需向量索引。
3. 企业维护组织级简历库，上传候选人姓名、邮箱、手机号和简历；AI 针对指定岗位异步审阅简历项目并生成可人工审核的经历问题和读题语音。
4. 计划包含岗位题库抽题槽位与已批准经历问题；预约绑定岗位、候选人、题库版本、已批准计划、时间窗和一次性邀请。
5. 候选人通过邀请填报姓名、邮箱、手机号和授权，至少以邮箱或手机号与预约绑定记录强匹配后才能进入设备检查和开始面试。
6. 数字人在冻结候选池内按会话种子进行可审计随机检索，播放预生成语音；题库题逐题完成服务端 STT 和评分后，再进入简历经历问题。
7. 面试结束后输出总分与客观岗位匹配证据；企业可以查看每题答案、转写、评分证据和原始语音，修正转写并重评，最终招聘决定仍由人员完成。

## 已完成

- 新增 FastAPI 后端入口：`app/main.py`。
- 新增内置面试官 Web 工作台：
  - 静态应用目录：`app/web/`
  - 总览、题库、面试计划、面试会话和模型服务五个工作区
  - 题目创建与检索、岗位创建、计划生成、面试创建与启动、文本答题、评分和报告查看
  - Provider 配置、能力路由创建和测试入口
  - 同源挂载到 `GET /`，静态资源使用 `/web/*`
  - Lucide 图标库固定版本随应用分发，不依赖运行时 CDN
  - 响应式桌面和移动端布局，包含数字人面试官视觉资产
- 新增候选人独立面试房间：
  - 使用带候选人 token 的 `/#candidate/{interview_id}?token=...` 加入链接
  - 摄像头预览、麦克风/摄像头开关和输入设备切换
  - 使用 `MediaRecorder` 录制回答，并通过 WebSocket 发送二进制音频分片
  - 支持浏览器 Speech Recognition 实时转写；不支持时可手动编辑最终转写
  - 当前题目朗读、录音计时、评分状态、题目推进和面试完成状态
- 新增数字人读题 MVP：
  - `avatar.speak` 统一请求/响应和模型网关路由
  - `POST /api/v1/interviews/{interview_id}/avatar/speak`
  - mock provider 返回 `browser_speech` 降级计划，前端使用 Speech Synthesis 朗读并驱动说话状态
  - 保留音频、视频和 WebRTC 供应商输出模式，业务服务不绑定具体厂商
- 新增统一错误结构：`app/core/errors.py`。
- 新增可切换存储入口：`app/repositories/provider.py`。
- 新增内存存储：`app/repositories/memory.py`。
- 新增本地 SQLite 存储：`app/repositories/sqlite.py`。
- 新增数据库与向量存储设计：`docs/database-and-vector-storage.md`。
- 新增事务型持久化 seam：
  - interface 与领域化事务工作区：`app/persistence/interface.py`
  - Memory adapter：`app/persistence/memory.py`
  - SQLite adapter：`app/persistence/sqlite.py`
  - adapter 选择入口：`app/persistence/provider.py`
  - 统一并发冲突、记录冲突和未找到错误
  - Outbox 幂等键、执行状态、尝试次数、租约和失败信息
- 全部业务 module 已迁移到新持久化 seam：
  - 创建 Question 与 Outbox 工作项原子提交
  - Embedding 调用位于数据库事务之外
  - 索引成功时原子保存向量、递增 Question version 并完成工作项
  - Provider 失败时保留 Question、标记 `index_status=failed` 并保存可重试工作项
  - Question、RoleRequirement、InterviewPlan、InterviewSession、模型配置、路由、密钥和调用日志都不再直接访问 Store collection
  - 所有可变聚合从 `version=1` 开始，更新使用显式 expected version
  - 正式面试只允许从 `approved` 计划创建
  - InterviewSession 拥有 InterviewCandidate、InterviewPlanSnapshot、完整 InterviewQuestionSnapshot、轮次和答案
  - 评分、重评和报告只读取冻结快照，不读取当前题库或当前计划
  - AnswerEvaluation 与 InterviewReport 使用 append-only revision 和当前指针
  - 重评自动通过 Outbox 新增报告 revision，提供评分和报告历史查询 API
- 新增 `InterviewSessionLifecycle` 深模块：
  - `LifecycleCommand -> LifecycleDecision(session, events, effects)` 是唯一会话状态迁移接口
  - 统一创建、就绪、开始、暂停、超时、继续、恢复、跳题、取消、人工结束、回答提交、评分结果和报告结果
  - 最后一题评分完成或被跳过后自动进入完成/报告流程，传输层不再判断何时换题或生成报告
  - 暂停保留当前轮次和中断原因；暂停期间评分完成后，恢复命令可修复下一题或报告触发
  - 评分/报告失败任务被 worker 重领时先形成 `*.retry_started` 事实，再安全提交原 revision，修复失败后无法回到生成态的问题
  - 领域事件按会话 sequence 持久追加，并与聚合更新、Outbox 效果原子提交
  - REST、WebSocket、数字人和 Outbox worker 均通过该 seam 校验状态与当前轮次
- 新增独立 Outbox worker：
  - 入口：`app/workers/outbox.py` / `interviewer-outbox-worker`
  - 支持题目索引、答案评分和报告生成工作项
  - 支持失败任务与过期租约恢复，租约 token 防止陈旧 worker 提交
  - 请求进程仍会立即尝试同一处理器，以保持当前同步 API 体验
- 深化 Model Invocation module：
  - 能力枚举：`app/model_gateway/capabilities.py`
  - 统一请求/响应：`app/model_gateway/schemas.py`
  - provider manifest 扫描及 `entrypoint` adapter 加载：`app/model_gateway/registry.py`
  - 单一执行 interface：`ModelGateway.invoke(capability, request, route=None)`
  - 统一 route 解析、每 target 重试与 fallback、硬超时、进程内断路器、响应 schema 校验、成本上限和逐 attempt 审计
  - 请求日志只保存 SHA-256 脱敏哈希；失败返回 `invocation_id`、`route_id` 和 attempt 总数
  - manifest 的配置/凭证 schema 在管理员写入时执行；活动 route 只能引用已实现且声明对应能力的 adapter
- 新增 Interview Plan Assembly deep module：
  - 类型化 `PlanAssemblyRequest -> assemble()` 是唯一自动计划装配 interface
  - 岗位画像不再依赖固定技能清单；必备与加分技能按 3:1 建立优先级
  - 候选题扩召回后统一执行覆盖配额、关键点去重、单技能上限、难度曲线、权重归一和时长守恒
  - 每题选择理由与 `assembly_summary` 同源；候选池不足、未覆盖维度和约束放宽都显式返回
  - 计划审批后，装配策略和摘要随 InterviewPlanSnapshot 冻结
- 新增 provider 插件目录和 manifest：
  - `app/providers/mock/provider.json`
  - `app/providers/openai_compatible/provider.json`
  - `app/providers/azure_openai/provider.json`
  - `app/providers/anthropic/provider.json`
  - `app/providers/gemini/provider.json`
  - `app/providers/dashscope/provider.json`
  - `app/providers/volcengine/provider.json`
  - `app/providers/azure_speech/provider.json`
  - `app/providers/tencent_cloud_speech/provider.json`
- 新增 mock provider 能力：
  - 文本 embedding 的确定性向量
  - 单题评分的关键点覆盖启发式逻辑
- 新增 `openai_compatible` provider 真实 HTTP 调用：
  - `llm.chat_json` 调用 `/chat/completions`
  - `embedding.text` 调用 `/embeddings`
  - 支持 provider 错误映射、调用日志、离线 MockTransport 测试
- 新增本地 provider secret 存储：
  - 内存模式使用 `provider_secrets`
  - SQLite 模式使用 `provider_secrets` 表
  - API 响应仍只返回 `credential_ref`，不返回明文 credentials
- 新增本地向量文档集合：`vector_documents`。
- 创建题目时写入题目级和关键点级向量文档。
- 搜索题目时组合向量相似度、关键词重合度和技能重合度。
- 新增后台模型配置 API：
  - provider catalog
  - provider config 创建/列表/更新/测试
  - model route 创建/列表/测试
- 新增核心业务 service：
  - 题库创建、列表、详情、搜索
  - 岗位要求创建和基础画像解析
  - 面试计划生成和更新
  - 面试创建、启动、提交答案、重评、结束
  - 单题评分和报告生成
- 新增前端读取所需列表 API：
  - `GET /api/v1/role-requirements`
  - `GET /api/v1/interview-plans`
  - `GET /api/v1/interviews`
- 新增实时 WebSocket 会话入口：`/api/v1/interviews/{interview_id}/live`：
  - 候选人 token 校验和面试官/候选人事件广播
  - 会话恢复、录音开始/结束、partial/final 转写、评分和完成事件
  - 二进制音频分片大小、总量和 MIME 类型校验
  - 面试官 start/pause/resume/recover/next/complete/cancel 控制与 REST 共用生命周期命令；候选人控制越权会被拒绝
  - REST 生命周期控制完成后向已连接 WebSocket 广播统一状态/当前题投影
- 新增生命周期控制 API 与持久事件查询：pause、timeout、resume、recover、skip、cancel、complete 和 `GET /events`。
- 新增本地敏感媒体适配器：`app/adapters/local_media.py`，录音默认写入 `data/media/`。
- 新增主流程测试：`tests/test_mvp_flow.py`。
- 新增 SQLite 持久化测试：`tests/test_sqlite_store.py`。
- 新增 provider registry 测试：`tests/test_provider_registry.py`。
- 新增 OpenAI-compatible provider 离线测试：`tests/test_openai_compatible_provider.py`。
- 新增 Model Invocation 故障矩阵测试：`tests/test_model_invocation.py`，覆盖 manifest entrypoint、重试、schema fallback、硬超时、断路器、不可回退错误、成本和 attempt 日志。
- 新增计划装配 interface 测试：`tests/test_plan_assembly.py`，覆盖能力配额、多样性、难度曲线、权重/时长守恒、候选池不足、策略校验和开放技能画像。
- 新增实时媒体和数字人测试：`tests/test_realtime_media.py`。
- 新增持久化 contract tests：`tests/test_persistence_contract.py`，同一套测试覆盖 Memory 和 SQLite adapter 的事务回滚、租户隔离、乐观并发、Outbox 幂等、过期租约回收、租约所有权和失败恢复。
- 新增 InterviewSession 聚合测试：`tests/test_interview_session_aggregate.py`，覆盖计划审批、陈旧版本冲突、候选人所有权、计划/题目快照稳定性、append-only 评分与报告修订链。
- 新增生命周期接口测试：`tests/test_interview_lifecycle.py`，覆盖非法迁移、自动换题/报告、跳题、超时恢复、取消和 append-only revision。
- 更新 `pyproject.toml` 依赖和 pytest 配置。
- 新增开发启动说明：`README.md`。
- 新增 `.gitignore`，忽略虚拟环境、pytest 缓存和 Python 字节码。

## 已验证

- `.venv/bin/python -m pytest -q` 通过：当前 51 个测试通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall app tests` 通过。
- `.venv/bin/python main.py` 可启动服务，健康检查 `GET /healthz` 返回 `{"status":"ok"}`。
- `GET /`、`GET /web/styles.css`、`GET /web/app.js` 和数字人图片资源已通过自动化测试。
- Web 工作台已在 `1440x1000` 桌面视口和 `390x844` 移动视口完成实际浏览器截图检查。
- 数字人实时面试页已完成实际浏览器渲染检查，图片、会话状态、轮次和报告布局正常。
- 候选人房间已使用 Chromium 虚拟摄像头和麦克风完成实际浏览器端到端检查：设备授权、录音、WebSocket 二进制音频上传、转写提交、评分和自动结束均通过。
- 浏览器上传的 WebM 测试录音已成功落盘，并在答案记录中保存 `/media/...` URI。
- 默认 SQLite 后端可启动服务，并生成本地数据库 `data/interviewer.sqlite3`。
- `GET /api/v1/admin/model-providers/catalog` 可从 `provider.json` 返回 9 个 provider，其中 `mock` 和 `openai_compatible` 的 `implemented=true`。
- 直接运行 `.venv/bin/python -m compileall app tests` 会因为 macOS 用户缓存目录不在沙箱可写范围内失败；这不是代码语法问题。

## 重要限制

- 当前没有 `JobPosition` 聚合，`knowledge_base_id` 只是题目上的自由字符串；尚未实现岗位拥有多个题库、题库构建状态或跨岗位检索隔离。
- 当前题目创建只触发本地 embedding；没有 `QuestionSpeechAsset`、异步 TTS 预生成、语音版本、失败重试或题库 readiness gate。
- 当前没有企业简历库、候选人长期记录、简历文件版本、AI Resume Review、项目经历问题和人工批准流程。
- 当前计划在生成时固定题目列表；没有冻结候选题池、会话随机种子、`QuestionSelection` 事实或断线后可重放的随机检索。
- 当前面试由后台直接提交候选人信息创建；没有 `InterviewAppointment`、一次性公开邀请、Candidate Intake、姓名/邮箱/手机号匹配、授权版本和候选人 self-start。
- 当前默认数据会写入 `data/interviewer.sqlite3`，但 schema 仍是文档型 MVP，不是最终关系模型。
- 当前检索会保存和读取本地向量文档；这是旧版实验实现，不再是新主链路的前置条件。可以在迁移岗位题库后删除，或只保留为后台相似题实验；无需为完成 MVP 接入 pgvector。
- 当前计划装配是确定性启发式策略，已经输出覆盖和放宽解释，但尚未用人工金标计划校准配额、重复阈值和难度曲线。
- 当前评分是 mock provider 的关键点覆盖启发式，不是真实 LLM。
- 当前 WebSocket 已支持音频分片和实时事件，但还没有 WebRTC 媒体通道、断点续传、消息队列或多实例连接广播。
- 当前已经具备显式 `timeout` 命令和恢复语义，但尚无心跳截止时间监控器自动发出超时命令；事件日志随聚合持久化，也尚未拆成独立事件流或支持跨实例订阅。
- 当前浏览器 Speech Recognition 只作为本地 MVP 转写方案，兼容性和稳定性受浏览器影响；尚未接入服务端流式 STT provider。
- 当前数字人已接入 `avatar.speak` 网关和浏览器语音降级，但视觉仍是静态资产加说话动画；尚未接入真实视频数字人、唇形同步或供应商 WebRTC 流。
- 当前候选人链接包含随机 token，但 token 尚无过期、撤销和一次性使用机制；面试官登录鉴权和组织权限也尚未实现。
- 当前没有企业复核资源和音频签名 URL；虽能在本地保存回答音频，但不能按 reviewer 权限审计回听、修正转写或记录复核完成。
- 当前录音保存在本地文件系统，未加密、未配置自动过期或对象存储访问签名，只适合本地开发。
- 当前鉴权、租户权限、审计日志、密钥加密都还没有实现。
- 当前 provider catalog 已经从 `provider.json` 扫描生成。
- 当前 `mock` 和 `openai_compatible` provider 有实际执行逻辑；其它 provider 是 manifest 占位，尚未实现真实 SDK/API 调用。
- 当前统一执行 schema 只覆盖 `llm.chat_json`、`embedding.text`、`avatar.speak`；Chat Text、STT、TTS 和 Moderation 必须在增加统一 schema 与 adapter 后才能进入活动 route。
- 当前断路器是进程内状态，多进程/多实例之间不共享；成本估算依赖 provider config 的静态 pricing，尚未接入真实账单或指标系统。
- 当前本地 provider secrets 是 SQLite 明文 JSON 存储，仅用于本地测试；生产必须替换为密钥管理器或加密字段。
- 当前独立 Outbox worker 是 SQLite 轮询实现，尚未提供指数退避、最大尝试次数、dead-letter、任务监控指标或消息队列唤醒；数据库工作项与租约恢复语义已经具备。
- 为保持 MVP 接口兼容，请求进程会在提交工作项后立即尝试处理；生产部署可改为返回异步状态并完全交给独立 worker。
- 当前 InterviewSession 聚合以单个 JSON 文档保存；关系化 PostgreSQL 迁移后需要以外键和 revision 唯一约束保持同一语义。

## 下一步

1. 先实现 `JobPosition` 与岗位拥有的 `KnowledgeBase`，迁移现有题目并在 repository、API、检索和前端强制岗位/题库边界。
2. 扩展 Outbox 为题库导入、索引和 `tts.synthesize` 题目语音构建流水线，加入幂等、重试、dead-letter、构建状态和 readiness gate。
3. 实现 `CandidateProfile`、`ResumeDocument`、私有对象存储、AI Resume Review、项目证据、经历问题人工审核和问题语音。
4. 把 Interview Plan Assembly 调整为岗位题库抽题槽位 + 经历问题，冻结题库版本和候选清单；实现带种子、唯一约束和选择事实的 Question Selection。
5. 实现 `InterviewAppointment`、token 哈希/过期/撤销/一次性消费、Candidate Intake、邮箱/手机号强匹配、同意记录和候选人 self-start。
6. 增加 `stt.streaming` / `stt.batch` / `tts.synthesize` 统一 schema，接入一个真实服务端 STT/TTS provider；生产客户端不再提交 final transcript，断流走 batch 修复。
7. 扩展生命周期为“抽题—播放语音—录音—transcribing—逐题评分—下一题—简历阶段—报告”，补齐中断恢复和阶段切换测试。
8. 实现企业复核 API/UI、回答音频短期签名 URL、转写 revision、重评和客观 `job_fit_level`，不自动生成录用/淘汰决定。
9. 引入 PostgreSQL、API 鉴权/RBAC、组织隔离、审计、加密、媒体留存和删除，并用 Persistence contract 与端到端测试验证；不把 pgvector 列为验收依赖。
10. 最后再做 WebRTC、真实视频数字人、跨实例广播/断路器、心跳监控、指标告警和公平性离线评估。
