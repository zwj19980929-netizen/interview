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
7. [开发进度](docs/development-progress.md)
8. [实施路线图](docs/implementation-roadmap.md)

如果修改了接口、数据结构、检索策略、评分逻辑、模型供应商、模型路由或统一模型输入输出格式，必须同步更新对应文档。

## 当前阶段

当前仓库仍处在设计和骨架阶段，根目录只有示例 `main.py` 和基础 `pyproject.toml`。后续实现应先建立清晰的后端模块边界，再接入具体的 LLM、语音识别、TTS 或数字人供应商。

## 关键原则

- 保持供应商无关：LLM、Embedding、STT、TTS、数字人都通过模型网关和 provider 插件接入，不把业务逻辑写死到某一家服务。
- 先做可闭环 MVP：题库管理、岗位要求、题目检索、面试计划、文本/音频回答、语义评分、报告生成要先跑通。
- 实时链路要分层：WebSocket 负责状态和事件编排，音视频流优先用 WebRTC；MVP 可以先用 WebSocket 音频分片。
- 评分必须可解释：每题分数要带命中的关键点、缺失点、证据片段和改进建议，不能只给一个黑盒分。
- 人类最终决策：系统输出用于辅助面试官，不应自动做录用或淘汰决定。
- 隐私优先：音频、转写文本、候选人信息、评分报告都属于敏感数据，默认最小化采集、权限隔离、可审计。

## 推荐模块命名

后续 Python 后端可按以下目录演进：

```text
app/
  api/              # REST 和 WebSocket 路由
  core/             # 配置、鉴权、日志、错误类型
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
