# Interviewer

实时数字人面试系统后端 MVP。

当前实现是可运行且有自动化验收的本地闭环：FastAPI API、SQLite/Memory/PostgreSQL persistence adapter、模型网关、岗位题库批量构建、PDF/URL 简历安全摄取、私有文件存储、候选人专属计划、预约匹配、稳定随机抽题、服务端 streaming/batch STT、逐题评分、报告导出和企业复核。仓库已实现 OpenAI-compatible、DeepSeek、智谱 BigModel 与阿里云百炼千问的 LLM HTTP adapter，OpenAI-compatible、智谱 GLM-TTS 与 DashScope 的 TTS 子集，以及可配置的 `media_http` 真实 STT/数字人 HTTP adapter；生产部署仍需提供 PostgreSQL、Redis、恶意文件扫描器、对象存储、Provider 凭据和目标厂商端点，代码在这些依赖未通过健康检查时失败关闭。

## 启动

首次启动或前端源码变更后，先构建 React 工作台：

```bash
cd app/web
npm install
npm run build
cd ../..
```

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
curl http://127.0.0.1:8000/readyz
```

`/healthz` 只表示进程存活；`/readyz` 只读检查数据库、Redis，以及生产模式下的密钥、私有 OSS 和恶意文件扫描器，任一硬依赖缺失时返回 `503`。

## 生产配置预检

生产配置由显式运维命令生成，不会由应用启动时隐式补默认值。下面的命令一次生成三种角色的独立 Bearer token、Fernet 密钥和各类签名密钥；输出文件权限固定为 `0600`，已被 Git 忽略，命令行只报告变量名与缺项，不打印密钥值：

```bash
.venv/bin/python -m app.operations.production_config init \
  --postgres-dsn 'postgresql://runtime_app:password@db.example/interviewer' \
  --redis-url 'rediss://:password@cache.example/0' \
  --clamd-host 'scanner.internal' \
  --oss-endpoint 'https://oss-cn-hangzhou.aliyuncs.com' \
  --oss-bucket 'interviewer-private' \
  --oss-access-key-id 'replace-with-ram-key-id' \
  --oss-access-key-secret 'replace-with-ram-key-secret'

.venv/bin/python -m app.operations.production_config check
```

生成命令拒绝覆盖已有文件；密钥轮换应生成新文件并通过部署平台 Secret 管理器切换，不能直接覆盖旧密钥。静态检查不导出变量、不访问网络；启动前仍须加载配置并以 `/readyz` 完成 PostgreSQL、Redis、OSS bucket 和 clamd 的真实只读/协议探针：

```bash
set -a
source .env.production.local
set +a
.venv/bin/python main.py
```

当前 Web 工作台使用 React 19 + Vite，包含总览、招聘流程、题库、面试计划、预约/面试会话和模型供应商配置等页面，生产构建由 FastAPI 同源托管并直接调用 `/api/v1` 接口。招聘流程页可创建岗位/岗位题库、录入候选人，并通过“本地 PDF / 公开 HTTPS URL”双入口查看安全摄取进度后触发岗位审阅；可用计划可在工作台创建预约、生成 `/#invite/{token}` 邀请页，候选人先完成身份匹配、明确同意并确认预约，系统安排提前 30 分钟邮件提醒，到预约时间后再检查设备并 self-start。

“模型服务”页可配置、编辑 `openai_compatible`、`deepseek`、`zhipuai`、`dashscope` 或 `media_http`，再按能力和 purpose 创建路由。Provider manifest 声明默认地址、模型目录和 predefined/customizable 选择模式；`media_http` 用 Bearer API Key 对接 HTTPS 健康探针、multipart STT 和 JSON 数字人朗读端点。模型名只存在于 provider/route 边界，不写死在业务服务。生产环境必须为每个业务 purpose 显式创建 route，不会隐式回退到 mock。

React 工作台一次确认生成并启用计划，基于冻结的 execution v2 槽位创建预约；底层 API 仍可显式生成草稿供职责分离客户端编辑审批。不存在管理员直接创建/START 会话或客户端文本答案入口。

候选人房间需要浏览器允许摄像头和麦克风权限。开发模式录音默认写入 `data/media/`；生产模式强制通过 `PrivateFileStorage` 形成 `candidate_answer_audio` FileObject，并要求阿里云 OSS 等私有对象存储。浏览器 Speech Recognition 只显示开发预览，保存的音频回答仍通过服务端 STT final 后评分。`avatar.speak` 可返回浏览器语音、音频或 HTTPS 视频；React 候选人房间会播放真实 `audio/video` 响应。

默认 `INTERVIEWER_RUNTIME_ENV=development`，允许 mock STT/TTS 闭环。设置为 `production` 后，联系人/供应商凭证加密、Bearer RBAC、至少 32 字符的 `INTERVIEWER_CANDIDATE_TOKEN_SECRET`、Redis 公开端点限流、文件签名密钥、恶意文件扫描器、`INTERVIEWER_MEDIA_RECORDING_BACKEND=private`、阿里云 OSS，以及非 mock 且近期健康的 `stt.streaming`、`stt.batch`、TTS/评分路由均成为硬门槛；不会静默使用浏览器 final、mock 语音、本地录音或公开文件路径。

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
INTERVIEWER_POSTGRES_MIGRATION_DSN=postgresql://migration_owner:password@127.0.0.1/interviewer \
.venv/bin/python -m app.migrations.postgresql

INTERVIEWER_DB_BACKEND=postgresql \
INTERVIEWER_POSTGRES_DSN=postgresql://runtime_app:password@127.0.0.1/interviewer \
.venv/bin/python main.py
```

显式迁移入口会在 advisory lock 内应用 `migrations/`；Web/worker 运行账号只校验 schema 并执行 DML，不需要也不应拥有 DDL 权限。迁移会创建持久文档、Outbox、凭证、调用日志、唯一约束及强制租户 RLS。部署前还需配置生产密钥、`INTERVIEWER_REDIS_URL`、私有文件后端和真实 Provider route；完整清单见 [数据库与向量存储设计](docs/database-and-vector-storage.md) 与 [模型供应商插件化设计](docs/model-provider-plugins.md)。

从旧计划结构升级时，先停写并检查 execution v2 迁移，再执行正式迁移；运行时不会自动兼容旧 `items`：

```bash
.venv/bin/python -m app.migrations.plan_execution_v2 --dry-run
.venv/bin/python -m app.migrations.plan_execution_v2
```

## Celery worker

题库导入/重建、题库语音、PDF 摄取、简历审阅、评分和报告均先写入持久 DurableWorkItem/Outbox。Celery 只携带 `organization_id + work_item_id` 并负责调度；数据库继续保存租约、幂等、退避、dead-letter、进度和人工重放事实。

本地 Redis 与 Celery worker/beat：

```bash
redis-server
INTERVIEWER_CELERY_BROKER_URL=redis://127.0.0.1:6379/2 \
.venv/bin/celery -A app.workers.celery_app:celery_app worker --beat --loglevel=INFO
```

安装项目脚本后也可以运行 `interviewer-celery`。Celery Beat 每两秒补发 due/租约过期工作项；Web 请求不直接执行 TTS。`app.workers.outbox` 保留为不依赖 broker 的故障恢复/测试入口，但生产部署使用 Celery。

邮件提醒通过 SMTP 环境变量配置。授权码先留空时，提醒任务会保持可重试状态且不会伪造发送成功：

```bash
INTERVIEWER_SMTP_HOST=
INTERVIEWER_SMTP_PORT=587
INTERVIEWER_SMTP_USERNAME=
INTERVIEWER_SMTP_PASSWORD=
INTERVIEWER_SMTP_FROM_EMAIL=
INTERVIEWER_SMTP_STARTTLS=true
```

`INTERVIEWER_SMTP_PASSWORD` 填邮件服务商提供的 SMTP 授权码，不要提交到 Git；完整空白样例见 `.env.example`。

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
