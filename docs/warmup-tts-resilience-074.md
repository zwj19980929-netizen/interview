# 试音确认提示超时与恢复（074）

状态：`verified（仓库全量回归与本机加载；真人长场次及弱网未验收）`。

## 已确认的现场证据

- 截图场次在 2026-10-18 14:19:06 UTC 选择试音字幕确认提示，14:19:18 UTC 记录 `AGENT_AUDIO_FIRST_PCM_TIMEOUT`，阶段为 `tts_stream_open`、预算 12000ms；此前没有正式题或正式答案。
- 同场开场白 TTS 504ms 打开并完成播放。14:37:06 UTC 同配置最小短句探测 HTTP 200、432ms 首音、469ms 完成。本轮另外 6 次串行短句均完整成功，首音 401–592ms，完成 481–633ms；仅发送无个人信息的“你好。”。
- 原 API/worker 日志未出现 Traceback、数据库锁、未读取 Task 异常或音频背压；这不证明不存在未记录的瞬时阻塞。
- 历史失败没有 TCP/TLS/响应头分段记录，无法事后断言究竟是 DNS、网络、代理链路还是供应商等待，也无法由零 token 用量推断账户余额。探测成功只说明测试时可调用。
- 当前表达路由配置为 1 次重试、250ms 退避、熔断阈值 3、恢复 30 秒，没有备用模型。单目标配置保留；本轮没有擅自新增模型或修改凭据。已验证备用路由机制不等于当前具备多供应商容灾。

## 确认的实现缺口与修复范围

| 实现缺口 | 本项修复 |
| --- | --- |
| 首个 TTS 打开请求可消耗整个 12 秒总预算，配置重试实际无剩余时间 | 在原总期限内给剩余可用尝试预留时间，音频暴露后不透明重放 |
| 重试前 continue 绕过失败计数；外层取消可能漏记调用 | 每次实际失败与取消分别留痕，正常取消不算供应商故障 |
| 同次调用触发熔断后覆盖真实超时，导致仅允许超时切换的备用路由失效 | 当前调用保留最后真实失败用于备用路由决策；下一独立调用仍受熔断保护 |
| HTTP 状态粗分，SSE 业务 code 丢失 | 有界读取和白名单分类，保存安全网络阶段/耗时 |
| 未发声的试音确认提示失败直接暂停整场 | 当前所有权和选择下保留字幕确认/重新试音；不自动确认或进入正式面试 |
| `provider_bad_request` 被当作流式不支持再次调用批量接口 | 仅明确 `provider_streaming_not_supported` 允许兼容回退 |

## 状态与权限边界

恢复只适用于无正式题、`awaiting_confirmation`、同 owner/control/capture 和最新选择、输出已清理且没有 PCM 发送的试音确认。发可恢复 `WARMUP_CONFIRMATION_AUDIO_UNAVAILABLE`，保留候选人显式确认或重新试音。部分音频、已暂停/接管、所有权改变、新确认/重试或新选择不能被旧失败覆盖；不能伪造播放回执，也不重放真实录音。

试音守卫所需的既有 calibration 状态和更新时间加入精简数据库投影，避免每段 PCM 校验时解码完整历史。会话级批准行为复用选择/送达回执，未播出的失败选择标为 failed；正式评分证据选择仍只遍历正式题。

模型错误分类保留欠费、免费额度停用与 TPS/TPM 限流的区别。错误体和网络 trace 只存固定字段和有限数字，不输出候选内容、URL 查询参数、原始供应商 message 或凭据。原始 API key 不写诊断文件。

## 验证与加载

- 修复后的真实 DashScope adapter + 模型网关在独立内存容器中合成无个人信息短句：441ms 开流、536ms 首音、540ms 完成，完整终态审计成功；不写业务库。
- 15:00:50 UTC 同链路合成实际固定试音确认提示（不含候选信息）：458ms 开流、459ms 首音、1479ms 完成，288000 字节 PCM，成功终态审计；日志 `/private/tmp/interviewer-074-live-confirmation-result.json`。
- 三层组合故障注入覆盖 HTTP 头等待超时后有效重试、明确欠费不重试、已发 PCM 后 SSE 欠费不重放；与 provider/gateway 新用例共 73 项通过。
- 首轮后端全量为 3714 passed / 1 failed / 8 skipped。旧 067 用例仍要求配置三次重试却仅调用一次，已改为四次尝试共享期限并独立设置熔断阈值；此检查同时发现并修复同调用熔断覆盖超时原因的备用路由问题，最终复验结果另补。初轮失败日志保留。

- 最终后端全量：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`，**3717 passed / 8 skipped / 238.86s**，日志 `/private/tmp/interviewer-074-backend-final.log`；8 项需要外部环境的用例仍跳过。熔断原因修复后受影响 8 文件另有 294 项联合通过，与全量重叠。
- 前端全量：在 `app/web` 执行 `npm test -- --run`，**473 passed / 26 files**；`npm run build -- --outDir /private/tmp/interviewer-074-web-dist` 成功，保留既有大 bundle 提示。文档链接与 `git diff --check` 通过。
- 空闲窗口再次确认唯一 worker、active/reserved 为 0、LiveKit 房间为 0；沿原环境正常 TERM 旧服务后启动 API **69745**、worker/Beat **69746**、Vite **69772**。8000 的 health/ready、5174 的入口/健康代理均为 200；两端 Agent WebSocket 均完成升级并拒绝无效合成票据，不进入真实面试。
- 发布 `index-DikQW806.js` 并保留历史 bundle。实际静态入口和 JS 哈希匹配验证构建，5174 源码包含本次恢复提示。worker 已成功执行 `interviewer.dispatch_due_work`；启动窗口没有 Traceback、数据库锁、未读取任务异常、FFI Panic 或输出背压记录。
- 加载及前端验证后全部面试核心业务摘要保持一致，截图场次仍为 paused、0 份正式答案。未自动恢复、确认、重评分或改写业务记录。加载结果、源码哈希与后续校验分别在 `/private/tmp/interviewer-074-reload-result.json`、`/private/tmp/interviewer-074-web-result.json`、`/private/tmp/interviewer-074-loaded-verification.json`。

## 尚不能承诺的范围

过去那次网络延迟的具体子阶段不可追溯；本项诊断为后续失败提供足够信息。短句实测不是长场次或并发 SLA。固定提示音预生成/缓存、部分播放的自动恢复、跨进程试音字幕恢复仍须单独设计和验收。

原暂停场次需要通过工作台既有人工恢复入口再进入，不能靠重启自动改写暂停状态。回退时仅撤销 074 增量并在空闲窗口重新加载，静态入口可从 `/private/tmp/interviewer-074-previous-index.html` 恢复；无需数据库迁移或业务数据回滚，069–073 和历史构建均保留。
