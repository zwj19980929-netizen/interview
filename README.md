# Interviewer

实时数字人面试系统后端 MVP。

当前实现是可运行且有自动化验收的本地闭环：FastAPI API、SQLite/Memory/PostgreSQL persistence adapter、模型网关、岗位题库批量构建、PDF/URL 简历安全摄取、私有文件存储、候选人专属计划、预约匹配、稳定随机抽题、服务端 streaming/batch STT、逐题评分、报告导出和企业复核。仓库已实现 OpenAI-compatible、DeepSeek、智谱 BigModel 与阿里云百炼千问的 LLM HTTP adapter，以及 OpenAI-compatible、智谱 GLM-TTS 与 DashScope 的 TTS 子集；生产部署仍需提供 PostgreSQL、Redis、恶意文件扫描器、对象存储、Provider 凭据及真实 STT/数字人服务，代码在这些依赖未通过健康检查时失败关闭。

## 启动

```bash
.venv/bin/python main.py
```

默认服务地址：

```text
http://127.0.0.1:8000
```

面试官 Web 工作台：

```text
http://127.0.0.1:8000/
```

交互式 API 文档：

```text
http://127.0.0.1:8000/docs
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
```

当前 Web 工作台包含总览、招聘流程、题库、面试计划、预约/面试会话和模型供应商配置等页面，直接调用同源 `/api/v1` 接口。招聘流程页可创建岗位/岗位题库、录入候选人，并通过“本地 PDF / 公开 HTTPS URL”双入口查看安全摄取进度后触发岗位审阅；计划批准后可在工作台创建预约、生成 `/#invite/{token}` 邀请页，候选人完成身份匹配、明确同意和麦克风检查后 self-start。

“模型服务”页可配置、编辑 `openai_compatible`、`deepseek`、`zhipuai` 或 `dashscope`，再按能力和 purpose 创建路由。Provider manifest 声明默认地址、模型目录和 predefined/customizable 选择模式，测试弹窗按能力选择模型：智谱 LLM 使用 `glm-5.2`，TTS 使用 `glm-tts`；DeepSeek/智谱 Chat 复用统一 OpenAI-compatible runtime，智谱另支持官方 GLM-TTS，DashScope 提供千问 Chat/Embedding、Qwen3-TTS/CosyVoice。模型名只存在于 provider/route 边界，不写死在业务服务。生产环境必须为每个业务 purpose 显式创建 route，不会隐式回退到 mock。

面试计划生成后先保持草稿状态；在计划页审批后，才能基于冻结的 execution v2 槽位创建预约。不存在管理员直接创建/START 会话或客户端文本答案入口。

候选人房间需要浏览器允许摄像头和麦克风权限。录音默认写入 `data/media/`；浏览器 Speech Recognition 只显示 partial 并为本地 mock STT 提供显式开发输入，保存的音频回答仍通过服务端 `stt.batch` interface 形成 final 后评分。数字人当前通过 `avatar.speak` 网关返回浏览器语音朗读方案，不是生产级视频数字人。

默认 `INTERVIEWER_RUNTIME_ENV=development`，允许 mock STT/TTS 闭环。设置为 `production` 后，联系人/供应商凭证加密、Bearer RBAC、至少 32 字符的 `INTERVIEWER_CANDIDATE_TOKEN_SECRET`、Redis 公开端点限流、文件签名密钥、恶意文件扫描器和非 mock 且近期健康的 `stt.streaming`、`stt.batch`、TTS/评分路由均成为硬门槛；不会静默使用浏览器 final、mock 语音或公开文件路径。

## 本地数据库

默认使用 SQLite：

```text
data/interviewer.sqlite3
```

可通过环境变量切换：

```bash
INTERVIEWER_DB_BACKEND=sqlite INTERVIEWER_SQLITE_PATH=data/interviewer.sqlite3 .venv/bin/python main.py
```

临时内存模式：

```bash
INTERVIEWER_DB_BACKEND=memory .venv/bin/python main.py
```

PostgreSQL adapter：

```bash
INTERVIEWER_DB_BACKEND=postgresql \
INTERVIEWER_POSTGRES_DSN=postgresql://user:password@127.0.0.1/interviewer \
.venv/bin/python main.py
```

`migrations/001_postgresql_persistence.sql` 会创建持久文档、Outbox、凭证、调用日志、唯一约束及强制租户 RLS。部署前还需配置生产密钥、`INTERVIEWER_REDIS_URL`、私有文件后端和真实 Provider route；完整清单见 [数据库与向量存储设计](docs/database-and-vector-storage.md) 与 [模型供应商插件化设计](docs/model-provider-plugins.md)。

从旧计划结构升级时，先停写并检查 execution v2 迁移，再执行正式迁移；运行时不会自动兼容旧 `items`：

```bash
.venv/bin/python -m app.migrations.plan_execution_v2 --dry-run
.venv/bin/python -m app.migrations.plan_execution_v2
```

## Outbox worker

题库导入/重建、语音、PDF 摄取、简历审阅、评分和报告均使用持久 Outbox；独立 worker 会恢复失败任务或过期租约，并按配置指数退避，达到最大次数后进入 dead-letter，可从管理员 API 审计后重放：

```bash
.venv/bin/python -m app.workers.outbox
```

安装项目脚本后也可以运行 `interviewer-outbox-worker`。数据库是任务真相来源，未来接入消息队列时只用于唤醒 worker。

## 测试

```bash
.venv/bin/python -m pytest -q
```

如果需要单独做编译检查，并且当前环境限制写入用户缓存目录，可以指定 pycache 目录：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall app tests
```

## 协作入口

先读 [AGENTS.md](AGENTS.md)，再读 [开发进度](docs/development-progress.md)。
