# 同音色语音稳定性（075）

状态：`verified（仓库全量回归；末次追加已随077空闲加载）`。

## 本次处理

用户未配置备用，并要求保持音色。本项不修改供应商、模型路由或凭据，沿当前 TTS 目标完成以下处理。

| 问题 | 实现与边界 |
| --- | --- |
| 试音确认、补充询问等固定句每次依赖即时合成 | 版本化白名单5句，后台独立预算预热，完整且有效的PCM才缓存。API启动及候选会话profile就绪时调度，缺失时走现有合成路径。 |
| 逐次创建HTTP客户端重复建立连接 | 仅API显式生命周期复用；8条并发流/配置、32配置/loop、30秒空闲回收。请求认证与timeout独立，单流取消不关闭其它流。worker和注入factory不跨loop复用。 |
| 正式追问未发声就失败导致整场暂停 | 无活跃AnswerEndpoint、零PCM/无不确定写入、两侧清理完成且owner/control/turn/selection仍有效时，保留原题与已提交答案，发`QUESTION_AUDIO_RETRY_REQUIRED`。 |
| 重连时按钮永久禁用或误显示正在收音 | 丢弃旧连接未确认的本地重试意图，按服务端marker恢复；等待题目播放时不采集答案，也不显示绿色收音。 |

## 缓存与权限

固定提示注册在`app/core/prompt/fixed_speech.py`：试音确认`warmup_confirmation.v1`、已有补充三句v2、退出确认v1。动态题目、解释、候选内容不缓存。组织、当前精确purpose路由、provider/model、配置和凭据指纹、voice/language/rate/format、文案及版本进入键；生成前后复核配置。禁用、轮换、过期或损坏只造成miss，不能绕过权限使用旧声音。

每缓存最多64项/16MiB、单项1MiB、TTL30分钟、2项生成并发、单项8秒。内存缓存随进程关闭丢弃；无新表、共享音频URL或候选数据文件。启动预热不等待网络才开放API。命中创建全新流，经过既有`ApprovedSpeechOutput`，仍须真实媒体发布与客户端实际排空。

每次预热作业最多首轮加一次补齐：首轮完成后等待2秒，仅重试可重试错误造成的缺失项，已成功项不重复合成，认证/额度等终止错误不补试。补齐使用独立上下文并重新检查当前配置；关闭或配置变化取消旧补齐，不合作取消继续占用原并发位，不能新增重复工作。没有后台无限循环；已有60秒调度冷却后，新候选会话或实际缓存缺失可触发新的作业。

每项每轮仍使用既有网关重试规则和8秒期限。供应商流、内外层校验流保留只读标准终止错误；已知认证/额度错误不能因后续abort清理超时而变成可补齐错误。该机制只判断缓存预热的重试资格，不改变实际回答提交或播放完成规则。

网关有界返回后仍在运行的开流/清理通过当前调用完成信号继续归属原缓存key/slot，迟到返回的stream也须实际abort成功。清理异常或取消只隔离本scope原占位，不能启动该key的新补齐；不会把未关闭连接当成可用容量，API关闭仍有界。

## 正式追问恢复状态

`question_audio_recovery`保存原`act_id`、`turn_id`、最新`selection_event_id`和状态。候选投影只含状态/turn/selection。沿用`request_repeat`，要求当前turn和`payload.question_audio_recovery_event_id`，题干只取服务端原批准行为。重试生成新选择和输出身份，不新建题目、不再次提交上一题答案。

重试中状态为`retrying`，实际播放排空后先清marker快照，再开放candidate floor。直接采集、VAD/barge-in、重复点击、旧ACK、旧控制连接均不能越过此门禁。`retry_required`重连不自动重试；重试期间断线或无法证明输出情况仍暂停，清除不确定回放引用，保留原题供人工恢复后重读。

## 验证证据

- HTTP池与原流式诊断联合157项通过；缓存/prompt/pipeline及scope集成59项通过；路由快照27项通过；追问恢复及相邻链路133项通过。数字存在交集，不累加为独立总数。
- 真实现有`qwen3-tts-flash`、0备用目标，2026-10-19 03:48 UTC仅合成5条系统固定提示，无候选材料、无业务库写入。8342ms完成全部预热，完整PCM共1463040字节；5个HTTP请求仅1次TCP连接和TLS握手。
- 随后5条均命中，新增供应商调用0；首段PCM可读取1.489–1.946ms。此值是服务端缓存查找与取流，不能当成浏览器端首声或整轮对话延迟。结果`/private/tmp/interviewer-075-live-cache-result.json`。
- 前端最终`npm test -- --run`：482 passed /26 files；临时构建成功，保留既有大bundle提示。日志`/private/tmp/interviewer-075-frontend-final.log`、`/private/tmp/interviewer-075-build.log`。后端与运行版本见下。
- 首轮后端3842通过/2失败/8跳过，暴露053旧测试夹具缺少新门禁，以及061旧控制连接关闭时新断言被当作清理失败。前者绑定真实门禁，后者保持本地清理并禁止失权连接写回新owner状态；9文件233项复验通过，未放宽控制门禁。
- 真实后端事件到前端严格校验首次12/13，发现通用提前发言拒绝的action不一致；统一公共投影后13/13通过。保留`/private/tmp/interviewer-075-contract-crosscheck.json`及`-final.json`，没有以放宽Schema掩盖问题。
- 补齐改动前完整后端复验3846 passed /8 skipped /245.41s（`/private/tmp/interviewer-075-backend-verified.log`）。首次空闲加载API78573、worker/Beat78574成功，health/ready及5174入口/代理200、两端合成无效票据WebSocket校验通过；业务摘要完全一致，目标场已是report_ready/0答案，这一状态在加载前已存在。
- 实际API启动预热捕获供应商超时：2026-10-19 04:10:54 UTC第一条固定提示首次尝试4003ms超时，其中等待响应头3770ms；第二次3743ms超时、无子阶段证据。后续4条完整成功。未返回欠费或额度耗尽错误，不能据此判断账户余额；服务正常且未改面试状态。保留`/private/tmp/interviewer-075-startup-prewarm.json`和`-phases.json`，此发现促成本次一次性后台补齐，补齐回归见下。
- 最后补齐及清理修复的6文件257项通过（`/private/tmp/interviewer-075-fixed-refill-late-terminal.log`），独立8项通过（`/private/tmp/interviewer-075-refill-independent-closure.log`，与前者重叠）。覆盖真实adapter→gateway→cache的HTTP/SSE拒绝、迟到billing/auth、同Task新调用隔离、非合作open及late abort、失败abort占位隔离。原13项独立取消回归与8项也有重叠，不累加。
- 最终完整后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`，**3878 passed /8 skipped /241.85s**，`/private/tmp/interviewer-075b-backend-verified.log`。441份Python代码/测试哈希与最终回归冻结快照一致；`git diff --check`及11份本项文档的本地链接检查通过。8项外部环境测试跳过，未据此宣称生产验收。

## 运行版本与加载状态

2026-10-20T01:09Z更新：用户随后在077明确授权空闲加载，075末次追加及076/077已一并加载至API92921、worker92922；5条固定提示音真实启动合成全部成功，业务摘要不变。以下记载原075交付时的保留服务决定，不能再据此推断当前仍未加载。

首次075代码已加载API78573、worker/Beat78574，Vite69772与`index-2zdDc3yN.js`包含追问恢复界面；它已具备固定提示缓存、连接复用和正式追问显式重试。之后追加的一次性补齐、终止错误保留及后台清理所有权修复尚未加载。三次只读空闲检查均因Codex自动审批服务`Selected model is at capacity`而未执行，无二次重启；这是工具审批服务故障，不能当成面试系统的供应商错误或宣称最后代码已生效。用户随后明确选择“先交付结果，保留当前服务”，本轮停止重载，仅交付实现和验证结果。未来加载仍须重新确认媒体/worker空闲及运行环境；本轮不自动恢复任何历史场次。

## 验收限制与回退

不能承诺供应商或网络不再超时。动态追问仍需实时合成；缓存重启/过期/配置变化可能miss；部分播放、真实长场次/并发与弱网尚未验收。本项不回写历史面试、不自动恢复暂停场次。回退仅撤销075增量并在空闲窗口重新加载；保留069–074及全部业务数据，无数据库迁移或数据回滚。
