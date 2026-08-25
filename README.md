# Interviewer

实时数字人面试系统后端 MVP。

当前实现是可运行的本地 MVP：FastAPI API、本地 SQLite repository、模型网关、题库/岗位/计划/面试/评分/报告闭环，以及候选人摄像头预览、录音上传、实时事件和数字人读题降级链路。生产级 PostgreSQL、服务端 STT 和真实视频数字人供应商仍待接入。

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

当前 Web 工作台包含总览、题库、面试计划、面试会话、监控与报告、模型供应商配置等页面，直接调用同源 `/api/v1` 接口。创建并启动面试后，可从面试会话列表打开带 token 的候选人房间。

面试计划生成后先保持草稿状态；在计划页审批后，才能基于冻结的计划和题目快照创建正式面试。

候选人房间需要浏览器允许摄像头和麦克风权限。录音默认写入 `data/media/`；浏览器支持 Speech Recognition 时显示实时转写，否则候选人可在提交前手动填写最终回答文本。数字人当前通过 `avatar.speak` 网关返回浏览器语音朗读方案，不是生产级视频数字人。

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

## Outbox worker

请求流程会立即处理题目索引、评分和报告工作项；如果进程在中途退出，独立 worker 会从 SQLite 中恢复失败任务或过期租约：

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
