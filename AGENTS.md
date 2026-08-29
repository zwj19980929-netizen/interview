# AI 协作入口

本项目目标是构建一个实时数字人面试系统：面试官维护题目、标准答案和评分标准作为知识库；系统根据岗位要求检索并生成面试计划；数字人在面试中读题；系统通过语音识别采集候选人回答，并基于标准答案、关键知识点和岗位要求做语义评分，最终生成评分和评价报告。

## 先读文档

AI 协作者在写代码或改设计前，按顺序阅读：

1. [系统架构](docs/architecture.md)
2. [接口设计](docs/api-design.md)
3. [领域模型](docs/domain-model.md)
4. [检索与评分设计](docs/retrieval-and-evaluation.md)
5. [模型供应商插件化设计](docs/model-provider-plugins.md)
6. [数据库与向量存储设计](docs/database-and-vector-storage.md)
7. [统一领域语言](CONTEXT.md)
8. [已知问题与修复设计](docs/known-issues-and-remediation.md)
9. [开发进度](docs/development-progress.md)
10. [实施路线图](docs/implementation-roadmap.md)
11. [操作变更日志](docs/change-log.md)

如果修改了接口、数据结构、检索策略、评分逻辑、模型供应商、模型路由或统一模型输入输出格式，必须同步更新对应文档。

## 强制操作留痕

- 任何会修改仓库内容的工作都必须作为一个工作项记录到 [操作变更日志](docs/change-log.md)：首次修改前登记 `in_progress`，结束时补充实际修改文件、验证命令、结果和未完成事项。
- 工作项可以覆盖同一目标下的一组相关编辑，不要求逐条记录只读命令；但任何代码、测试、配置、数据库迁移或文档写入都不能脱离工作项。
- 修复 [已知问题与修复设计](docs/known-issues-and-remediation.md) 中的问题时，必须同步更新问题状态、开发进度和实施路线图。没有代码、迁移与验收测试证据时不得标记 `verified`；兼容路径未删除时不得标记 `closed`。
- 操作失败或任务中止也要保留记录，写明失败原因、已产生的副作用和恢复方式，不能删除日志条目掩盖历史。

## 当前阶段

当前仓库已具备经过自动化验证的本地完整闭环：岗位题库批量构建、PDF/URL 简历安全摄取与私有存储、候选池/execution v2 计划、预约邀请页与明确同意、候选人 token 安全投影、流式/批量 STT 协议、评分、报告导出和企业复核；同时已有 PostgreSQL/RLS、Redis 跨实例事件、RBAC、字段/凭证加密、审计、签名媒体、Outbox dead-letter 与公平性评估实现。旧计划 `items`、管理员直建/直接 start、客户端文本答案和旧向量题库 interface 已删除。它仍不是“已通过生产环境验收”的版本：真实 PostgreSQL/Redis、阿里云 OSS、恶意文件扫描器及 STT/TTS/数字人供应商需要部署凭据和外部服务联调；生产 readiness 对这些能力失败关闭。继续实现时必须保留现有 deep module 边界和本页操作留痕规则。

## 关键原则

- 保持供应商无关：LLM、Embedding、STT、TTS、数字人都通过模型网关和 provider 插件接入，不把业务逻辑写死到某一家服务。
- 保持闭环：题库管理、岗位要求、题目检索、面试计划、服务端语音回答、语义评分、报告生成必须持续可运行；客户端文本或浏览器 final 不能形成答案。
- 实时链路要分层：WebSocket 负责状态和事件编排，音视频流优先用 WebRTC；MVP 可以先用 WebSocket 音频分片。
- 评分必须可解释：每题分数要带命中的关键点、缺失点、证据片段和改进建议，不能只给一个黑盒分。
- 人类最终决策：系统输出用于辅助面试官，不应自动做录用或淘汰决定。
- 隐私优先：音频、转写文本、候选人信息、评分报告都属于敏感数据，默认最小化采集、权限隔离、可审计。

## Prompt 与 AI 响应治理

- 所有业务 Prompt、系统 Prompt、探针 Prompt、结构化输出强化指令及其响应 Schema 必须集中放在 `app/core/prompt/`；业务服务、worker、模型网关和 Provider adapter 不得内嵌或散落 Prompt 文本。新增或修改 Prompt 时必须设置可追踪版本，并同步增加合同测试。
- 任何要求 AI 返回指定格式的调用，都必须在模型网关或 `app/core/prompt/` 的统一 seam 对解析后的结果执行规则校验，至少覆盖类型、必填字段、枚举、数量/长度边界、禁止的额外字段和非空业务内容；校验通过前不得写入领域对象、触发后续任务或被业务代码直接索引字段。
- Provider adapter 只负责厂商协议和原始 JSON 解析；统一格式校验失败必须转换为结构化、可观察、可重试策略明确的错误，日志不得包含完整敏感 Prompt、AI 响应或凭据。业务模块可以在统一格式校验后追加更严格的领域规则，但不能绕过统一校验。

## 推荐模块命名

后续 Python 后端可按以下目录演进：

```text
app/
  api/              # REST 和 WebSocket 路由
  core/             # 配置、鉴权、日志、错误类型
    prompt/          # 全部版本化 Prompt、响应 Schema 与统一结构化结果校验
  domain/           # 领域实体、枚举、状态机
  services/         # 题库、检索、面试编排、评分、报告服务
  model_gateway/    # 模型能力枚举、路由、插件注册、统一调用入口
  providers/        # OpenAI-compatible、Azure、语音、数字人等 provider 插件
  adapters/         # 存储、消息队列、外部非模型系统适配器
  repositories/     # 数据访问层
  schemas/          # Pydantic 请求/响应模型
  workers/          # 异步任务：索引、评分、报告生成
tests/
```

## 协作约定

- 新增 API 时，先更新 [接口设计](docs/api-design.md)，再写路由和测试。
- 新增表、集合、索引或状态字段时，同步更新 [领域模型](docs/domain-model.md)。
- 调整召回、排序、题目选择或评分标准时，同步更新 [检索与评分设计](docs/retrieval-and-evaluation.md)。
- 新增或调整模型、语音、数字人供应商时，同步更新 [模型供应商插件化设计](docs/model-provider-plugins.md)。
- 任何实时面试逻辑都必须明确会话状态迁移、超时、重试和失败恢复。
- 代码实现时保留单元测试和端到端冒烟测试，至少覆盖题库入库、岗位检索、面试计划生成、单题评分和报告生成。
