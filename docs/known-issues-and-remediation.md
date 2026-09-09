# 已知问题与修复设计

## CONFIRMED-PREPARATION-WAIT-024（verified：仓库、真实合成决策与本地加载）

iv_25335313fb1b4dea确认结束后同输入18次模型调用、234.399秒模型耗时；均通过原始wire后被后续校验拒绝，旧日志不足以追认具体字段。已修可选追问拒绝连带废弃有效理解、声音刷新失败预算、孤儿失败未计数、完成缓存到期/可取消等待和陈旧整理投影。相同文字3次失败后停止模型重试但保留采集；新文字/显式重试依合同恢复。完整1568项、前端190项通过，另新状态清理与安全日志目标回归通过；真实新合成两段均1次决策（8.506/5.336秒）。无活动房间时已加载PID70161和index-C_WF3WKO.js，023同次生效，历史答案保留。详细失败和验证见日志024；真实麦克风长会话/供应商尾部时延仍pending，不标closed。

## EXPLICIT-FINISH-NO-ANSWER-023（verified：仓库与本地加载）

iv_3bdec492ad8f4247在10:23:58、10:24:34、10:25:28三次finish/0.95之后，完整理解因没有技术内容改为clarification_request/clarify，再被播报“没有完全听清”并重开同题；这是缺少明确未作答业务分支，区别于021确认上下文和022空识别/字幕问题。新增answer_declined并经原有证据事务保存实际响应、进入下一题；全未作答正常0分，混合证据保留既有技术回答。完整回归1530 passed/6 skipped；真实新合成7组完整理解正确，v2补充语义3/3正确，慢请求仍有超在线10秒预算。另修可取消采集清理泄漏，相关131项以RuntimeWarning为错误通过。已随024在无活动房间时加载，详细文件/失败/结果见日志023/024；真实麦克风仍待实机验收，不标closed。

## FOLLOWUP-LATENCY-AND-CAPTURE-022（仓库/合成真实链与本地加载验证，实机pending）

iv_58da3095bac941c6中09:18:05/30已两次识别finish，但声音取消后同一无字音频段重复重开两分钟；最终组合模型12.497秒，TTS4.317秒。追问随后又在同一无字区间不断重放直至snapshot超时、缓冲耗尽。截图上一题305字final被前端recent无条件带入追问；追问本身新建流/start_byte=0，没有证据表明此处后端串题。修复将正常无文字完成与缺失结果区分、保留同证据纯准备并按题隔离字幕，增加finish预算及恢复缓冲一致性。最终后端1483 passed/6 skipped、前端185 passed；真实合成误触发恢复1次询问/1次准备/1次提交，全部录音保留。API49796及新bundle已加载，详情见022日志；真实麦克风长会话及生产仍pending，不标closed。

## SUPPLEMENT-CONFIRMATION-LOOP-021（仓库/合成真实链验证，实机环境pending）

iv_2449bbc1de8940b3同题7次补充询问，6句截图明确结束用语均与厂商sentence_final散列匹配；不是无转写。旧逻辑把结束确认后的任意新活动变回listening，随后重新冻结含否定答复的边界并重问；最终final未覆盖旧partial的输入水位还会产生错误撤销。现保留问答上下文，重复否定在同一问答处理，真实补充仍撤销提交；只有新完整final指纹一致才复用确认，无final不借用旧前缀。

先运行新回归2项均失败，修复后重点39项、整链和陈旧表达30项通过。完整后端1422 passed/6 skipped；真实合成ASR+补充语义+慢准备中重复否定为1次询问/2次finish/1次提交、3个STT流，1,035,520字节录音完全一致。专用INFO诊断补全意图与撤销/播放来源。原会话最终另因TTS_ASSET_DOWNLOAD_FAILED暂停，历史数据未改写；旧TTS下载失败的具体下游原因无法由已保存的泛化错误还原，仍保留问题，不宣称所有外部故障消失。真实麦克风长会话仍待验，不标记closed。

## SPEECH-ACTIVITY-AND-AUDIBLE-CONFIRMATION-020（仓库验证，实机听感pending）

当前会话iv_acbc69fa54e24915中发现服务端0.001 RMS低于既有端点配置，新增转写通知错误挂到open事件。已统一门槛、修正实际收音事件通知、无有效回复时语音澄清和真实字幕活跃状态；修复前端语音未开始/卡顿/被阻止时缺少独立恢复的问题。补充询问私有WAV实际6.48秒、RMS−23.7dBFS，且存在播放结束回执；无法据此追认为用户扬声器已发声，也不能确认历史具体未听见原因。

完整后端1416 passed/6 skipped，前端181 passed；低背景、持续语音、低声新增ASR经真实ingress、重复ASR、空回复语音澄清、播放受阻恢复/旧句隔离、诊断权限和严格Schema均已回归。新增播放开始/受阻的安全日志帮助后续定位。真实麦克风噪声、耳语识别率、浏览器及输出设备听感和生产仍environment/data pending，不标记closed。服务加载与合成厂商验证见工作项020。

## ASR-REPLAY-AND-TRANSCRIPT-QUALITY-019（背压/语义门禁已验证，真实语音质量pending）

iv_01b96d6b40314e2f 于06:00:49Z至06:00:57Z重复 PROVIDER_BACKPRESSURE_EXCEEDED。真实 Adapter + 慢 socket 已复现；旧测试下一拍清空所有缓存，遗漏真实网络排空时延。修复补送容量等待、恢复预算及继续补充后的无转写循环；新增术语提示和语义歧义门禁。红框字幕源于 ASR 投影路径，但旧逐句响应未保留，不能断言是模型虚构、回声或某个声源；新增散列/时间来源诊断。真实麦克风质量及生产仍 pending，不以合成测试标记 closed。

## CONFIRMED-ANSWER-FINALIZATION-018（verified：仓库、真实合成音频链与本地加载）

已确认新会话iv_bd12fa2e99b34f05进入supplement_reply.v1和确认后的decision.v2，仍无答案；端点用单帧RMS撤销确认，ContinuousSTT用更低RMS无限保留无转写后缀，确认final后还重开空识别。修复持有同一final、共享本地语音活动判断、待定起音守卫、新ASR假设撤销以及阶段准确的提示；另通过回归修复已覆盖半帧误阻塞、静音send在途导致重复理解。局部录音统计只能证明能量尖峰，不能断言声源。

最终完整后端1392 passed/6 skipped、前端176 passed、build/compile/diff通过；真实合成TTS→STT→语义finish→收口仅2次识别开流，428160字节录音逐字节一致。API16195及index-Dtd5Burt.js加载，healthz/readyz正常；原会话仍in_progress/answers0，未补造答案。详细命令、两轮失败与对应修复见日志018。真实麦克风及生产仍为data_pending/environment_pending，兼容未移除，未closed。

## SPOKEN-SUPPLEMENT-CONFIRMATION-016（verified：仓库、真实合成合同与本地加载）

证据：iv_2c8b3c6b909d4d0c已转写但答案0，10:05:33Z起理解请求连续provider_bad_request。历史厂商错误正文不可追溯；本轮真实合成Schema均成功，不伪称唯一参数根因已复现。已增加原生400的单次受控JSON Object兼容、严格统一校验与安全诊断；正式5秒补充询问、服务端口头意图、明确完成后提交、续说撤销、回声隔离、播放超时、状态及题干投影已通过回归，另修动态表达错误复用题目音频。

验收：完整后端1376 passed/6 skipped，后续影响面285 passed、最后确认/集成/合同/表达守卫39 passed，前端176 passed，build/compile/diff通过；真实Qwen合成肯定、否定、含糊答复与确认后完整答案理解通过。本地API8004及新bundle已加载，原会话保持paused/answers0。命令、失败及恢复留痕见操作日志016；真实麦克风质量和目标环境保持data_pending/environment_pending，兼容路径未删除，未标记closed。

本文记录 2026-08-25 深度审查确认的实现问题、目标设计、迁移步骤和验收测试。它是修复工作的执行清单；目标领域不变量仍以 `docs/domain-model.md` 为准，接口语义以 `docs/api-design.md` 为准。

状态约定：`open` 表示已确认、尚未修复；`in_progress` 表示实现或验收仍在进行；`verified` 表示仓库代码、迁移/配置保护和可执行自动化测试全部通过；`closed` 表示相关兼容路径也已删除。外部服务另用 `environment_pending`，真实业务金标或校准样本缺失另用 `data_pending`；两者都不能因 adapter 的离线合同测试通过而冒充完成。

## 修复顺序

### NONCLOSING-STT-SNAPSHOT-015（verified：仓库与本地加载，真实麦克风/生产pending）

- 已确认的设计问题：正常自动提案为拿转写先finish/reopen，决定提交后再finish；continue_listening也已切流。稳定句末事实在Adapter里被降为partial，增加不必要的连接生命周期与延迟；不追认为014历史错误的唯一原始原因。
- 实现：独立非final预览、严格句段/来源/版本校验、原连接上可撤销预计算、真正提交前一次最终收口；尾句/置信度/上下文变化仍要重新准备，未稳定内容不形成答案。同内容继续听不反复模型调用，无稳定句等待、显式提前结束仍可单次final兜底，试音保留自动结束。不支持预览的Adapter保留原路径。
- 回归补缺：预览异常必须标记故障后真实重建；final修订后理解暂时失败，重开仍能用已确认前缀重试，但新未识别有声后缀阻止旧前缀提交。所有PCM只录一次，owner/输入/题目/complete gates不减；预览轮询100ms且不反复读取完整会话，实际准备才持久封存前缀。新增安全无PII的识别开流/finish/预览准备/final修订计数。
- 验收：完整后端1352 passed/6 skipped、前端173 passed、compile/diff通过；API96567加载新后端，healthz/readyz正常。完整命令与中途失败留痕见工作项015。真实麦克风中文多轮/丢包/轻声、时延p50/p95与生产仍pending，012实验TTS仍关闭，无预览Adapter兼容未删除。

### CONTINUOUS-CAPTURE-RECOVERY-014（verified：仓库与本地加载，真实麦克风/生产 environment_pending）

- 事故与根因：iv_4d9730f843ec4ca7正式开流/三次重开成功后，CONTINUOUS_CAPTURE_FAILED只保存ApiError并暂停。合成复现retryable snapshot失败先pause+abort，finally因capture已关闭从不resume。另证实补送积压会挤爆未获调度的sender，以及finish取消漏清理；后两项不能追认为这场事故已证原始错误。
- 修复：独立安全错误分类、有界自动重建/未确认PCM有序补送/一次录音、30秒新积压、失败耗尽同题重答且不提交残缺答案。状态持久化与短UI投影拆开，critical状态失败必须撤销输入；主动暂停取消恢复，开流前后校验当前会话/题目/owner；过期owner只自撤销，不能暂停继任者。Provider正常finish/abort共享有界回收；提交后UI故障不漏调度下一步，批准表达不使用短UI期限。
- 验收范围：最终后端1198 passed/6 skipped、前端173 passed，构建/compile/diff通过；合成故障全链集成、Endpoint、Provider、ContinuousSTT、重开fence和Memory/SQLite同题重试事务测试，以及前端恢复/重连/capture scope均通过。实际命令、本地health/readiness与新bundle加载证据见操作日志014。新retry开流已修服务端PCM格式、跨会话owner绑定和失败ACK解锁，保留中途失败记录。
- 不变量与未完成：试音仍自动结束，正式仍智能可撤销接话，不强制按钮；不改历史paused会话、不发邀请、不外发历史回答、不启用实验TTS。真实中文停顿/长回答/网络抖动及生产验收仍pending，不宣称端到端稳定率或1–2秒达标，012剩余目标不因此关闭。

### ROUTE-READINESS-REFRESH-013（verified：仓库与本地服务，生产 environment_pending）

- 根因：五条必需路由的 healthy 记录超过 24 小时，邀请 admission 将“过期”和真实故障混为 configured_route_unhealthy；只有手动 route test 更新证据，管理页又缺四种用途。
- 修复：统一分状态投影与命令触发的有界刷新、跨实例探针租约/配置指纹、失败冷却、探针取消清理；邀请/start 最终事务不放宽；补齐用途和安全反馈。真实探针另复现 STT 已握手但关闭等待误判超时，已改为共享独立 abort、强制底层回收及清理错误分类，正式 finish 不变；旧健康证据在配置修改前绑定旧配置，不能借兼容窗口绕过重测。
- 验收：完整后端962 passed/6 skipped，前端158 passed，构建/compile/diff通过。`2026-09-07T07:47:56Z` 本机只刷新原预约，0.92秒后五路由 healthy/can_invite=true；重复刷新0.10秒且 invocation 85→85，预约仍 scheduled/version1，未签发邀请。已覆盖过期恢复、失败/超时、独立 SQLite 连接去重、配置/凭据变化、公网前置准入、最终复核与并发幂等；命令和证据见操作日志013。旧无指纹证据仍保留原TTL内兼容，未closed；探针成功不代表业务识别质量或生产验收。

### INTERRUPTIBLE-AUTOMATIC-TURNS-012（自动轮次已启用，流式 TTS 默认关闭，目标验收 pending）

- 根因与目标：011 将正式收口改成强制按钮仅规避抢话，不满足智能面试要求。原设计将语音句段 final、完整答案、语义处理和关闭 Evidence 耦合，续说容易排在慢推理后；修复必须拆开这些边界，不能靠扩大静音秒数。
- 已实现并启用的自动轮次：持续收音＋可撤销音频 EOT 提案；识别轮换有序缓冲、缺 final 的有声段重放；后台合并理解/追问和有界重试；新输入/补传/最终置信度或上下文变化失效旧决策；锁内前置校验再提交唯一答案；暂停/换题/owner 变化后丢弃迟到播报；前端准备阶段保留输入，按钮仅可选兜底，试音仍自动结束。
- 已实现但默认关闭的流式 TTS：统一 TTS PCM、DashScope SSE、独立最小权限 publisher、完整私有归档、ready/provider EOF/客户端 ACK 协议、前端精确绑定和断线新代重播已有代码与合同测试。`INTERVIEWER_STREAMING_TTS_ENABLED` 默认 false，当前默认仍使用预生成或完整合成后的私有音频；代码和合同完成不代表端到端验收通过。
- 未完成的播放证据：Chrome 的 2 秒 PCM＋3 秒停供实测中，`currentTime` 仍增长到 5.122 秒且无 `waiting`，EOF 后立即报告 drained；该媒体时钟不能作为源样本排空或可靠口型的证据。源样本到实播位置映射／可计数的 PCM 消费时钟仍待完成并验证，不能用固定尾音等待或历史抖动均值冒充准确 ACK；此前不默认启用实验输出。
- 当前环境修复证据：LiveKit `Cmd` 广播 `192.168.0.104`，但主机 `en0` 已为 `192.168.0.108`，两个无候选数据的浏览器探针 ICE 失败且响应数为 0。确认 `rooms=0/participants=0` 后运行 `local-media.sh up` 更新地址，Chrome 正常及 3 秒停供探针均完成并删除临时房间；不追认该问题为所有历史事故原因。新增 `local-media.sh doctor` 只读比对地址，漂移时非零且给人工操作指引，不自动中断运行中的房间。
- 回归证据：本地 worker/概率协议、连续 STT、可撤销 endpoint、合并严格合同、Prepared 证据绑定、迟到表达/owner 围栏、阶段指标、流式输出生命周期/默认关闭门禁、候选端状态与只读 doctor 均有用例；最终本地全量后端 `858 passed, 6 skipped`、前端 `131 passed`，构建成功。原生离线合成 PCM smoke `31 passed`，冷/热运行开销不等同于中文轮次准确率。具体命令与服务状态见操作日志 012；这些是仓库与本地验证，不是生产验收。
- 尚未关闭：真实中文思考停顿/填充词/轻声续说/长回答、并发模型 worker 负载、可靠 PCM 播放时钟、首音 p50/p95 与真实网络故障仍待样本校准和目标环境验收，保持 `data_pending/environment_pending`，不能宣称 1–2 秒端到端达标。批量补偿配置/生产 readiness 不绕过，旧 paused 会话不自动恢复。
- 独立环境风险：本轮上线时证实 Docker/LiveKit 曾比签发主机慢约 15 分 24.5 秒，导致 probe JWT `not valid yet`；最新采样恢复到约 0.73 秒。不扩大 nbf 容差、不关鉴权、不改系统时间；真实运行需保证主机/VM 对时，初始 500 不据此强行追认根因。原生拒绝日志的 JWT 已在应用 logger 上脱敏。

| 顺序 | ID | 优先级 | 问题 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `PLAN-001` | P1 | InterviewPlan 双执行表示导致人工编辑被预约路径忽略 | closed |
| 2 | `CONSENT-001` | P1 | Candidate Intake 未验证明确的隐私与录音同意 | closed |
| 3 | `APPOINTMENT-001` | P1 | 候选人 start 未校验预约时间窗和设备准入事实 | closed（仓库） |
| 4 | `REPORT-001` | P2 | 当前报告的 `manual_review` 被历史评分 revision 污染 | closed |
| 5 | `SEARCH-001` | P2 | 公开题库搜索仍使用旧向量兼容路径 | closed |
| 6 | `CANDIDATE-ACCESS-001` | P1 | 候选人页面生产鉴权不可用且管理员投影泄漏标准答案 | closed |

五项已在进入 PDF/对象存储等里程碑 13A 前按同一修复批次完成本地验证，避免继续在错误 interface 上扩展生产能力。

## PLAN-001：InterviewPlan 使用了两套执行真相

### 修复前现状与影响

- `app/services/plan_assembly.py` 同时持久化固定 `items`、随机抽题 `bank_slots` 和经历题快照。
- `app/services/plans.py` 的人工编辑只更新 `items`。
- 旧管理员直建路径读取 `items`，预约 start 则读取 `bank_slots` 和经历题快照。
- 临时回归探针已经复现：人工编辑题目后，预约实际执行的题目不是编辑后的题目。

因此，当前 InterviewPlan interface 泄漏了内部 representation，调用方必须知道应该读取哪一份数据；面试官修改并批准的计划可能被正式预约静默忽略。

### 目标设计

把 Interview Plan Assembly 加深为唯一拥有计划编辑、校验、批准和执行物化规则的 deep module。canonical execution plan 只包含：

- `bank_slots`；
- 每个槽位的 `QuestionCandidatePool`；
- 已批准的 `ExperienceQuestion` 快照；
- 权重、时长、阶段顺序和 `selection_policy`；
- `assembly_summary` 和 readiness 结果。

旧 `items` 不再是可写领域真相。迁移期间可作为只读 compatibility projection；任何旧固定题目都转换成“候选池仅包含一个题目”的槽位，从而保持原执行语义。直接创建会话和预约创建会话必须跨同一个 Plan Assembly seam 读取同一份 canonical execution plan。

### 修改步骤

1. 在 Plan Assembly module 内增加 draft revision/approval 命令，吸收人工编辑后的候选池重建、权重归一、时长守恒和 readiness 校验。
2. 修改 `PATCH /api/v1/interview-plans/{plan_id}`：输入 `bank_slots`、`experience_question_ids` 或 selection policy；旧 `items` 输入在兼容期内立即转换为单候选槽位，不直接持久化为第二份执行表示。
3. 修改 `InterviewService.create_interview` 和 `AppointmentService.start`，让两条路径都从 canonical execution plan 形成 `InterviewPlanSnapshot` 和 `QuestionSelection`。
4. 为已有计划编写一次性迁移：有 `items` 但没有有效槽位时，为每个 item 生成单候选槽位；已有 `bank_slots` 时以槽位为准，并记录迁移告警。
5. 迁移稳定后删除旧 direct-create 的 `items` 分支和相关浅层测试，用 Plan Assembly interface 的行为测试替代。

### 实现与验证（2026-08-25）

- `InterviewPlanAssembly.patch_plan()` 独占草稿编辑、经历题快照、权重/时长再平衡和审批校验；已批准计划拒绝原地编辑。
- `materialize_execution()` 只读取 `execution_schema_version=2` 的冻结槽位和经历题快照；运行时遇到旧 `items` 会返回 `INTERVIEW_PLAN_MIGRATION_REQUIRED`，不再懒迁移或返回 projection。
- 一次性命令 `python -m app.migrations.plan_execution_v2 [--dry-run]` 在部署前把旧固定题目转换为单候选槽位并删除 `items`；当前 SQLite 数据检查为 0 份计划，无数据需要改写。
- 管理员 `POST /api/v1/interviews`、`POST /interviews/{id}/start` 和计划 `items` schema 均已物理删除；正式会话只由 Appointment Admission 创建并 START。
- `tests/test_plan_assembly.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_production_compatibility.py` 验证 v2 物化、迁移语义、批准不可变和旧 interface 不在 OpenAPI 中。状态改为 `closed`。

### 验收测试

- 人工替换、删除、重排题目后，预约 start 执行批准的同一计划 revision。
- 人工编辑跨岗位题目、空候选池、权重不为 1 或时长不守恒时审批失败。
- 已批准计划不可原地修改；复制为新草稿后产生新的 candidate pool hash。
- 断线恢复复用原 `QuestionSelection`，不会因兼容 projection 再次抽题。
- 旧计划迁移前后的题目、顺序、权重和阶段语义一致。

## CONSENT-001：Candidate Intake 没有明确同意证据

### 修复前现状与影响

当前请求和持久化只包含 `consent_version`。客户端即使没有明确接受隐私告知或录音，也能登记并开始本地面试；系统无法证明候选人同意了什么内容。

### 目标设计

Candidate Matching module 只在以下条件全部满足时形成 CandidateIntake：

- `privacy_accepted=true`；
- 预约要求录音时 `recording_accepted=true`；
- `consent_version` 非空且属于服务端当前允许版本；
- 服务端保存 `notice_hash`、`consented_at` 和匹配方法，不信任客户端时间。

规范化、查找哈希、明确同意验证和最小化候选人快照收进同一个 module implementation，调用方只消费“已匹配且已授权”的结果，提升 locality。

### 修改步骤

1. 把 Candidate Intake 请求改成 `consent.accepted/version/recording_accepted` 的结构化对象。
2. 为 `CandidateIntake` 增加 `privacy_accepted`、`recording_accepted`、`notice_hash` 和服务端 `consented_at`。
3. 根据 `InterviewAppointment.settings.record_audio` 决定是否必须接受录音；缺失隐私同意返回 `CONSENT_REQUIRED`，需要录音但未同意返回 `RECORDING_CONSENT_REQUIRED`，两者都不创建 intake、不推进预约状态。
4. 旧 intake 标记为 `legacy_unverified`；生产 start 要求重新同意，开发数据可通过显式迁移开关保留。
5. 日志只记录预约 ID、告知版本/hash 和结果，不保存告知正文或联系方式。

### 实现与验证（2026-08-25）

- 预约只接受服务端 `CONSENT_NOTICE_CATALOG` 中允许的版本，冻结实际隐私/录音告知正文并对规范化内容生成 SHA-256；公开邀请返回候选人实际需要确认的正文、版本和 hash。
- intake 使用 `consent.accepted/version/recording_accepted`，只有姓名与至少一个规范化联系方式匹配且授权满足预约设置时，才保存 `match_method`、授权布尔值、notice hash 和服务端时间并推进状态。
- 会话创建时把已验证授权证据冻结到 `InterviewCandidate`；重复 intake 不会弱化已验证授权。
- `tests/test_position_resume_appointment_flow.py` 验证拒绝隐私同意、错误版本、拒绝必需录音均失败且预约保持 `invited`，同时验证不录音预约允许 `recording_accepted=false`。
- 管理员直建会话入口已删除，所有新会话都要求已验证 Candidate Intake；状态改为 `closed`。

### 验收测试

- 隐私同意缺失或为 false 时登记失败且无持久副作用。
- 预约要求录音而录音同意为 false 时登记失败。
- 不录音预约允许 `recording_accepted=false`，但仍要求隐私同意。
- 服务端保存的时间、版本和 notice hash 可在 InterviewCandidate 快照中追溯。
- 重复 intake 幂等返回同一同意事实，不能覆盖为更弱授权。

## APPOINTMENT-001：start 没有预约时间窗准入

### 修复前现状与影响

公开 start 只校验邀请 token 状态和 `invitation_expires_at`，没有校验 `scheduled_start_at/scheduled_end_at`，也没有消费已持久化的设备 readiness。现有端到端测试甚至使用未来预约并立即开始，因此没有覆盖真实准入不变量。

### 目标设计

Appointment module 提供一个小而稳定的 admission interface，在同一事务内吸收：token、登记状态、明确同意、预约时间窗、设备 readiness、模型 readiness、一次性消费和唯一 InterviewSession 创建。时间由注入的 Clock dependency 提供，测试使用固定 clock；不让 HTTP 或 WebSocket 调用方自行判断时间。

默认 start 窗口为 `[scheduled_start_at, scheduled_end_at]`。若组织需要提前/延后宽限，使用显式 `early_start_grace_seconds/late_start_grace_seconds` 策略，并冻结到预约；邀请过期时间仍是独立条件，不能替代预约窗口。

### 修改步骤

1. 为公开 readiness 保存浏览器、麦克风、音频格式、检查时间和有效期；start 只接受未过期的 readiness fact。
2. 在 Appointment module 注入 Clock，并实现预约窗口和宽限策略判断。
3. 把准入判断、预约 `registered -> consumed`、唯一 session 创建和首次生命周期 START 放进同一事务/命令；重复 start 返回同一会话。
4. 时间窗外分别返回 `APPOINTMENT_TOO_EARLY` 或 `APPOINTMENT_WINDOW_CLOSED`；公开响应不返回候选人或计划敏感数据。
5. 更新现有测试，使用固定时钟而不是远期硬编码日期。

### 实现与验证（2026-08-25）

- 新增 `AppointmentAdmission`，统一校验已验证授权、默认或带宽限的预约窗口、带 TTL 的设备事实和即时模型 readiness；`AppointmentService` 与最终 `InterviewService` 事务共用该规则和可注入 Clock。
- readiness 上报会持久化浏览器支持、麦克风授权、音频 MIME、服务端检查时间和失效时间。
- 最终事务再次校验全部准入条件，并原子创建/首次 START 会话与 `registered -> consumed`；相同预约的重复或并发 start 返回同一会话。
- `tests/test_known_issue_remediations.py` 用固定时钟覆盖窗口前/后、设备过期、模型 readiness 过期和授权缺失；端到端测试以两个并发 start 验证只生成一个 interview ID。
- 仓库准入缺口及旧直接 start 路由均已删除，状态改为 `closed（仓库）`；真实 Provider 与 PostgreSQL 仍分别按 `environment_pending` 验收，不能借此宣称生产环境通过。

### 验收测试

- 窗口前、窗口内、窗口后和宽限边缘均有固定时钟测试。
- readiness 缺失或过期时不能 start。
- 两个并发 start 只创建一个 session，并只消费预约一次。
- 创建 session 后进程中断，重试仍返回相同 session 和 `QuestionSelection`。
- Memory、SQLite 和未来 PostgreSQL adapter 通过同一 admission contract tests。

## REPORT-001：历史评分 revision 污染当前报告

### 修复前现状与影响

报告分数从每个答案的 `current_evaluation_id` 读取，但 `manual_review` 判定扫描全部 `evaluation_revisions`。一个已经被新 revision 替代的低置信度评分仍会让当前报告保持 `manual_review`，违反“报告只引用当前 revision”的领域不变量。

### 目标设计

Report module 在生成开始时先物化唯一的 `current_evaluations` 集合，分数、维度、证据、风险、`manual_review` 和 `evaluation_ids` 全部只从该集合计算。旧 revision 只属于历史报告，不参与新报告。

### 修改步骤

1. 通过每个 CandidateAnswer 的 `current_evaluation_id` 构建 current evaluation map，缺失指针时形成显式报告告警。
2. 用同一集合计算总分、维度、复核标记、证据和 `evaluation_ids`，禁止再次遍历全部历史 revision。
3. 重评完成后原子更新当前指针并请求新报告；旧 InterviewReport 保留旧 `evaluation_ids`。
4. 在报告生成时断言 `report.evaluation_ids` 与生成时的当前指针集合一致。

### 实现与验证（2026-08-25）

- `ReportService.build_report()` 开始时按每个答案的 `current_evaluation_id` 物化唯一 current evaluation 集合；总分、维度、证据、复核标记和 `evaluation_ids` 全部从该集合产生。
- `tests/test_known_issue_remediations.py` 验证被替代的低置信度旧 revision 不再污染当前报告，同时当前 revision 低置信度时仍返回 `manual_review`。
- 历史评分/报告 revision 是预期审计事实而非兼容执行路径；当前报告只读 current pointers，状态改为 `closed`。

### 验收测试

- 被高置信度新 revision 替代的低置信度旧评分不再触发当前 `manual_review`。
- 当前 revision 低置信度或带 review flag 时必须触发 `manual_review`。
- 每个报告只引用生成时的 current evaluation IDs；历史报告仍可重放。
- 并发重评与报告生成通过 version 冲突重试，不产生混合 revision 报告。

## SEARCH-001：公开题库搜索与主链路语义不一致

### 修复前现状与影响

计划装配调用 Catalog module 的无向量结构化搜索，公开 `POST /api/v1/questions/search` 却调用旧 Question module 的 embedding/余弦搜索。两个 module 读取相同 Question 数据但暴露不同 interface；公开路径还把部分过滤留在 Python 全量列表之后。

### 目标设计

把 Question Catalog 加深为唯一的查询 module。它的 interface 接收租户派生的 scope、岗位、题库集合、结构化条件和查询 purpose；实现隐藏关系查询、关键词搜索、readiness 过滤和可选相似度 projection。计划装配与公开后台搜索跨同一个 seam，调用方不感知 embedding adapter。

`POST /api/v1/questions/search` 固定为无向量的关键词/结构化查询。若保留相似题实验，使用显式的后台治理入口或 `purpose=similarity_governance`，其不可用不得影响候选池、计划或评分。

### 修改步骤

1. 定义 `QuestionSearchScope`：服务端组织、必填岗位和非空题库 IDs；验证所有题库属于岗位和组织。
2. 把 `status=active`、`validation_status=valid`、题库/岗位/技能/难度/题型过滤下推到 Persistence module 的查询 interface，不先读取全量 Question。
3. 让公开 route 和 Plan Assembly 都调用 Question Catalog interface；删除旧 route 到 QuestionService 的向量分支。
4. 若保留向量能力，将其放在可删除、可重建的内部 projection/adapter 后，并使用不同的显式治理语义。
5. 兼容期内对旧请求返回弃用提示，随后删除旧搜索实现和只验证向量路径的测试。

### 实现与验证（2026-08-25）

- 公开 `/questions/search` 与 Plan Assembly 现在都调用 `CatalogService.search_questions()`；公开 schema 强制岗位和非空题库 scope，不再触发 embedding。
- `QuestionRepository.search_catalog()` 把租户、岗位、题库、active、valid、speech-ready、技能、难度和题型条件交给 Persistence backend；SQLite 用 `json_extract/json_each`，PostgreSQL 用 JSONB 条件，Memory adapter 遵守同一 contract。
- Web 工作台搜索增加岗位题库范围选择，避免前端继续发送旧无 scope 请求。
- 端到端测试验证无 embedding 的结构化搜索、跨岗位拒绝和计划/预约闭环；`tests/test_persistence_contract.py` 对 Memory/SQLite 验证未就绪、跨租户/岗位/题库和结构化条件均不会泄漏。
- `app/services/questions.py`、`VectorDocumentRepository`、Memory/SQLite 向量集合和 `question.index` worker 分支均已删除；全局题目创建/列表路由也已删除。状态改为 `closed`；真实 PostgreSQL `EXPLAIN` 仍属于 `DATA-001` 环境验收。

### 验收测试

- 未提供岗位或题库 scope 时请求失败。
- 跨岗位、跨组织、inactive、invalid 或不满足候选池 readiness 的题目不会泄漏。
- 未配置 embedding route 时结构化搜索、计划装配和预约仍全部通过。
- 公开搜索和计划候选池对相同 scope/filters 返回一致题目集合。
- PostgreSQL 查询计划证明过滤在数据库执行；Memory/SQLite adapter 通过同一查询 contract。

## CANDIDATE-ACCESS-001：候选人生产访问与安全投影不成立

### 修复前现状与影响

预约 start 返回 `/#candidate/{interview_id}?token=...`，但候选人页面随后调用需要后台 Bearer/RBAC 的 `/api/v1/interviews/{id}`、`audio-answers` 和 `avatar/speak`。开发模式因默认管理员身份掩盖了问题，生产环境会返回 401。更严重的是后台会话详情包含所有轮次的 `question_snapshot`，其中有标准答案、rubric 和后续题目，不能作为候选人投影。

### 修改方法

1. 新建 `/api/v1/public/interviews/{id}` 候选人窄接口，所有操作必须同时验证 `candidate_session_token`。
2. 返回 allow-list 投影：候选人姓名、会话状态、轮次 ID/顺序/状态；只向当前或已完成轮次返回题干，不返回计划、候选池、标准答案、rubric、报告 revision 或 token。
3. 将候选人录音提交和读题移动到同一 public namespace；录音 URI 必须属于当前 `interview_id/current_turn_id`，阻止跨会话引用。
4. 将公开候选人路由纳入 Redis fail-closed 限流；管理员详情、复核、报告等端点继续使用 Bearer/RBAC。
5. 前端邀请页执行姓名/联系方式匹配、明确同意、麦克风 readiness 和公开 start，随后只调用候选人窄接口。

### 实现与验证（2026-08-25）

- `candidate_session_token` 由生产密钥对会话 ID/创建时间做 HMAC-SHA256 派生，数据库和后台详情不持久化/返回明文；`InterviewService.get_candidate_interview()` 使用 allow-list 构造安全投影并以常量时间比较验证 token。
- `submit_candidate_audio_answer()` 限制录音路径到当前会话/轮次；候选人 REST 文本答案和 WebSocket `candidate.answer.text/candidate.transcript.final` 均已删除。
- React `candidate` feature 已实现 `/#invite/{token}` 登记页和候选人 public API 调用，生产不再依赖后台 Bearer 身份。
- `tests/test_realtime_media.py` 验证错误 token、跨会话媒体和标准答案投影均被拒绝，并完成 public 音频转写评分闭环。状态为 `closed`。

### TURN-COMPLETION-UNDERSTANDING-011（verified（仓库与本地服务、真实模型合成合同），真实麦克风复验 pending）

- 会话 `iv_f5c41141820c4b3c` 的追问转写已完整返回有效长文本；05:06:03.620Z stop 后 05:06:06.169Z 当前 capture 正常自动 seal，05:06:07.329Z 续说排队到 05:06:18.923Z。真正暂停在 05:06:18Z：真实 LLM 调用耗时 11,468ms，JSON schema 已过、领域内容被拒绝。旧日志未留具体规则，不追认到底是原文引用、能力分区还是 claim 声明错误；这次没有旧 capture 串流或 mock STT 成功证据。
- 根因修复：正式静音与回答完成分开，显式完成前持续同一录音；处理期间投影真实状态、抑制 VAD/重复提交/旧快照重开；理解 v2 改为服务端原文/能力点编号，严格检验后精确还原；一次有界纠正与去敏拒绝分类；重大暂停不再被可恢复次生问题覆盖。百炼 wire 不接受 uniqueItems 的差异留在 Adapter，网关严格去重校验保留。
- 完整后端 `525 passed, 5 skipped`、前端 `84 passed`，build/compile/diff 检查通过；本地 API 已重启 PID `21509`，新 bundle `index-jehEcGBW.js`。历史敏感转写外发复验被安全检查拒绝，未执行，待用户明确同意。仅无个人信息合成材料的真实模型已通过（5,268ms、两条精确证据），不代表事故原回答/真实浏览器或生产验收通过。历史 paused 会话不自动恢复，需刷新后在新测试场次核验明确完成、多轮和长停顿。

### ENDPOINT-CAPTURE-SCOPE-010（verified（仓库与本地服务），真实麦克风停顿复验 pending）

- 事故会话 `iv_618dd870f4a54270` 已完整提交第一题；没有 turn 的 speech.stopped 于追问 open 前进入 journal，旧端点在 2.5 秒后取当时新流的 turn，封存了仅 2.18 秒的低音量录音。补偿路由落到 mock，None 经字符串化被送给理解，最终 `UNDERSTANDING_RESULT_REJECTED` 安全暂停。
- 修复为 ready/VAD 携带 capture ID/turn，端点另绑定每次停顿 ID，旧/无 scope/无匹配 start 的停止信号与迟到 seal 无副作用；正常 barge-in 和显式收口保留。Provider 无 final 时保留录音、原子标记转写失败并释放采集，提示继续说或重说；不虚构 utterance、不调用理解/评分。网关禁止无显式开发文本 fixture 的 mock batch，adapter 不再字符串化 None。
- 全量后端 `514 passed, 5 skipped`，前端 `84 passed`，构建、编译与 diff 检查通过；覆盖跨题/同题采集作用域、已取消但排队的 seal、无 final 后重说及唯一答案、mock fixture、暖场与 owner 接管。接管测试改用可控数据库到期故障注入，不依赖在 200ms 内完成初始化，生产租约规则不变。本地 API PID `15757` 已加载修复，health/readiness 正常，首页提供新 bundle `index-C6hUuIMG.js`。
- 原安全暂停会话不自动恢复；本地真实批量补偿路由尚未配置，缺少此能力时明确失败而非假成功。目标浏览器强制刷新后的实际停顿/续说、多轮识别、断流补偿和生产验收仍 pending，不标记 closed。

### EVIDENCE-NONANSWER-RECAPTURE-009（verified（仓库与本地状态修复），真实麦克风复验 pending）

- 会话 `iv_4b8a18d54e7e4747` 在 05:18:44Z 成功打开正式 STT，封存 1,025 帧、656,000 字节 PCM（20.5 秒）；05:19:16Z 理解结果 confidence=0.3、suggested_action=clarify，服务端要求重说。生命周期把题目恢复 asking，但 EvidenceMediaStream 仍 complete，造成后续 13 次 `EVIDENCE_MEDIA_ALREADY_COMPLETE`，界面卡在准备识别。这次不是流式模型未配置或麦克风全程没有传入。
- 修复让非答案提交与采集 revision 归档/递增原子提交；流式、断线 batch、持久 repair 统一携带 revision。完整但未判定/已采纳的录音仍不能普通重置，迟到 writer、转写状态、语义结果和等长度跨 revision repair 失败关闭，旧录音/转写不被覆盖。
- Memory/SQLite 定向 `27 passed`，后端全量 `495 passed, 5 skipped`。本机修复前备份数据库，校验目标完整 checkpoint、同一拒绝事件、无答案与 owner 过期后，通过短暂 maintenance owner 正常 claim/fence/release 将该会话 capture 1 推进到 2；保留 control generation、会话/题目状态和所有历史证据，写入 `evidence.nonanswer_capture.released` 审计。API PID `2644` 的 health/readiness 正常。浏览器刷新后真实重说复验仍为 pending，不声称 STT 准确率或生产验收通过。

### FORMAL-EVIDENCE-DRAIN-SQLITE-008（verified（仓库与本地服务），目标新会话复验 pending）

- 会话 `iv_581d8635efa8413b` 的暖场和正式回答均路由到 DashScope `qwen-audio-3.0-asr-flash-streaming`，因此不是“试音模型正确、正式模型不同”。正式 Evidence 从打开到暂停约 51 秒，但持久 segment 只有 125×20ms 帧、80,000 字节 PCM，即应用 sink 实际仅接收 2.5 秒；最终 `answers=0`，几个字只是中断前的 transient partial。
- 同期浏览器产生 13 组 VAD start/stop。每条 durable Evidence command 和会话状态事务提交后，SQLite adapter 都调用 `_load_from_db()`，反序列化当前一万多条 documents/audit；这些同步工作与 LiveKit receive/sink/STT 共用事件循环，持续饿死有界 sink 并触发 `LIVEKIT_INGRESS_SINK_BACKPRESSURE`。此前扩大到五秒的队列只能容忍短抖动，不能修复这个确定性的 O(全库×命令数) 热点。
- SQLite 现记录事务内 document/work item/secret/invocation delta，仅在 commit 后深拷贝实际变化到兼容缓存；rollback 不同步，事务读取/CAS、索引过滤和 reopen 仍以 SQLite 为权威。LiveKit ingress 同时维护 accepted/delivered 水位，warm-up/formal seal 必须等命令点之前的已接收前缀全部通过 Evidence sink，超时走原失败关闭/暖场 retry seam，不能截断 final。
- 候选端默认连续静音 800ms 才发 stop，减少自然短停顿产生的命令风暴；`warmup.confirm` 立即清空暖场字幕，发送失败则恢复，snapshot 跨连接进入 completed 也会清理。定向后端 `44 passed`、前端 `35 passed`，完整后端 `475 passed, 5 skipped`、完整前端 `83 passed`，production build、compileall 与 diff check 均通过；修复后的本地 API/全部 readiness 检查已 ready。事故会话保持历史暂停，必须用全新邀请完成至少 60 秒正式长回答并核对 Evidence 帧、私有录音、唯一 final/CandidateAnswer 后才能移除环境 pending。

### STT-FINAL-PARTIAL-TAIL-007（verified（仓库），目标新会话复验 pending）

- 新会话 `iv_8120d4147ef14c67` 的暖场流从 `2026-09-04T06:18:32Z` 正常打开，约 46 秒后正常 seal；没有 Provider 背压、LiveKit 断流、面试暂停或 Evidence owner 失效，但最终确认字幕缺少尾部，因此与上一项背压事故不是同一故障。
- DashScope reader 对实时 partial 使用 `committed + partial`，但旧 finish 在已有 committed 分句时只选择 committed，把厂商在 `task-finished` 前最后交付、尚未标记 `sentence_end` 的非空片段丢弃。前端收到 final 后会按权威结果收口并移除活动 partial，于是候选人看到“实时出现过、最终却少一截”。
- adapter 现以单一 transcript projection 同时生成 partial 与 final：复制全部 committed segments，再追加有效 partial segment；最终 text 和 segments 来自同一集合。没有任何 Provider 文本时仍返回 `provider_final_transcript_missing`，多个 final、浏览器 final、跨 Provider 拼接和猜补未识别内容仍被禁止。试音面板展开态明确标为“完整服务端字幕”。
- DashScope 合同新增 committed + uncommitted tail + task-finished 场景并验证唯一 final 和两个 segment；完整后端为 `471 passed, 5 skipped`，完整前端为 `81 passed`，production build、compileall、manifest JSON 与 diff check 均通过。仍须用新会话对真实 Provider 复验尾句，正式回答还要核对 CandidateAnswer 与录音，完成前保持目标环境 pending。

### WARMUP-BACKPRESSURE-RECOVERY-006（verified（仓库），目标新会话复验 pending）

- 会话 `iv_68419ac0bb784ee5` 的暖场 STT 已打开并形成大部分字幕，`2026-09-04T05:31:22Z` 因 `PROVIDER_BACKPRESSURE_EXCEEDED` 报 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED`；浏览器到 `05:33:39Z` 才断开，Evidence owner 续租全部成功，因此不是客户端先离线或租约失效。两秒 DashScope PCM 积压预算与实测约 1.76 秒调度尖峰距离过小，是直接稳定性风险。
- 旧路径把任何 `audio_stream_failed` 都当正式 Evidence 致命错误暂停，但暖场是不录音、不评分、不能形成 CandidateAnswer 的临时校准。同时旧 endpoint seal 在断流后仍能等待 Provider final 并把 calibration 推进到 `awaiting_confirmation`，造成“服务器已暂停”和“试音请确认”相互矛盾。
- 每个暖场 open 现分配本地单调 epoch；断流先同步失效 epoch，再由单一恢复 seam 取消端点、abort 临时流、清 partial 并持久进入显式 retry gate。seal 在调用 Provider 前后都检查 track failure/epoch；失败前已在等待的迟到成功结果也只能 rejected，不能发 final 或确认表达。正式流没有此可重试降级，仍走暂停、checkpoint repair 或人工接管。
- LiveKit receive-only iterator 在候选人显式 `warmup.retry` 时可针对同一已发布 microphone track 重启，失败才重建 subscriber。LiveKit sink 和 DashScope sender 均改为默认五秒无损有界窗口、硬上限 30 秒；前者可由环境变量调整，后者由 STT 模型配置调整，任一超限仍明确断流而非丢帧。仓库专项回归通过；真实浏览器、真实 Provider、超过五秒的故障与多轮正式回答仍需新会话环境验收，事故会话保持暂停。

## REALTIME-AGENT-001：正式面试没有单一实时智能体运行时

### 修复前现状与影响

- 候选人 React 页面直接编排 `/live`、`/stt-stream`、`audio-answers`、`avatar/speak`、录音备份和播放；发言权、重连幂回放、幂等性和打断规则分散在浏览器与多个 WebSocket 中。
- 本地正式数字人是静态图片，缺少可中断的音频时钟、viseme/姿态契约；摄像头媒体同意、企业订阅权限和私有录像生命周期也没有形成同一证据链。
- 字面关键点比对和模板追问不能稳定区分回答、重读、继续补充、暂停和澄清请求；根题与多层追问的证据还可能被拆成独立分数。

### 目标设计

以 `InterviewAgentRuntime.open/send/events/close` 为唯一正式控制面，它拥有 Interaction Floor、幂等信号、可回放事件序列、断线恢复、证据编排和人工接管 lease；候选人与企业前端只通过 facade/monitor 消费稳定 AgentChannel。音视频媒体使用自托管 LiveKit WebRTC，WebSocket PCM 只作开发/恢复 adapter；候选人唯一服务端 final 仍是正式证据。AvatarPerformance 只能表达 ApprovedConversationAct，不拥有对话或评分决策。

### 实现与仓库合同验证（2026-09-02）

- 新增结构化 `AgentEvent/ClientSignal/ConversationUtterance/TurnUnderstanding/ApprovedConversationAct/AvatarPerformance/TakeoverLease` 合同；AgentChannel 对信号幂等、音频 epoch/sequence、事件回放、发言权、barge-in 和断线恢复拥有单一规则。一次性 agent ticket 不持久化 bearer，候选人只能发已同意的 microphone/camera，复核人媒体 grant 只读订阅。
- 入场先经不评分、不持久原音的 warm-up STT，候选人确认“系统确实听懂”后才正式读题。正式转写形成带录音 URI 的权威 utterance；`interview_turn_understanding.v1` 与 `controlled_followup.v1` 统一在 Prompt seam 做 Schema 和逐字证据校验，元意图有确定性规则，追问最多深度 2 且受根题/全场/剩余时间预算、敏感属性和标准答案泄漏门禁约束。Provider 失败时只允许证据绑定的安全确定性 probe。
- 根题与深度 1/2 追问的权威答案合并为一个 root evidence group，只生成一个当前评分 revision 和一条报告项；追问继续为零权重。LiveKit Participant Egress 按音频/视频独立明确同意启动，完成后校验私有对象哈希/字节数和真实存储保护并写审计；本地仅在 development 显式开关下使用私有目录，生产仍须证明 OSS AES256/KMS，必需录制启动失败时暂停面试。
- 正式 React 候选人页面使用 `CandidateInterviewExperience` + AudioWorklet/VAD + 加密环形恢复缓冲，企业页面使用 subscribe-only monitor 与需续租的 takeover lease；正式页面不再调用旧通道、不再展示静态图片。本地 VRM 1.0 渲染只消费服务端批准音频、viseme 和姿态 cue。仓库当前专属资产及去敏 license manifest 已通过 hash、VRM 1.0、preset/custom 15-viseme、blink/lookAt/humanoid 与许可合同；致命加载/渲染故障通过候选人窄接口推进真实生命周期 pause，不能用前端文案冒充暂停。
- receive-only LiveKit candidate microphone subscriber、连接独立的 `InterviewEvidenceChain` 和 30 秒 control reconnect grace 已接入 `AuthoritativeEvidenceIngress`。数据库时钟 ownership/fence、持久 command journal、连接无关 owner executor、DB polling/Redis wake hint、remote receipt proxy、私有 segment/checkpoint、owner-loss batch repair 与服务端授权 gap 的浏览器 backfill 构成同一权威链；旧 owner、旧 control、重发帧和重复 finish 均不能形成重复 CandidateAnswer。
- S2S Provider 的原始 PCM delta 在批准前只进入有界私有缓冲；只有 final transcript 与冻结 `ApprovedConversationAct` 逐字一致才会物化为加密 `AgentExpressionAudio` 并投影短期读取地址，不一致或断流则丢弃并按已批准文本走 cascade。动态追问必须复用冻结能力点与证据，所有 AI conversation act 都禁止评价性表述。
- VRM runtime 验证 GLB/VRM 1.0、preset/custom 表情并集中的 15 个 viseme、blink/lookAt/humanoid 与实际使用范围/肖像授权 manifest；本地开发和 production 都执行资产 readiness，避免开发绕过掩盖错误。真实 AudioWorklet 本地反馈、LiveKit `RTCStats` 和渲染后的 Avatar FPS 覆盖预检占位结果。生产 acceptance v2 必须绑定当前 `deployment_id + release_revision`、三浏览器矩阵和显式组织灰度名单，邀请/start、票据签发及票据消费均失败关闭。
- 当前仓库验证基线：完整后端 `382 passed, 5 skipped`，Vitest `54 passed`，Vite production build 成功。这些只证明仓库合同，不是真实媒体、模型质量或候选人试点验收。
- 2026-09-03 本机实流程回归继续修复：候选人刷新后的票据保持冻结 LiveKit identity，开场/主问题缺少可播放预生成资产时统一走受管 TTS，浏览器不再自行伪造 2.5 秒理解状态。真实合成语音又暴露握手期间静音争锁、暖场 seal 误要正式 turn、duplex send/recv 按帧阻塞和 final 等待期间证据门未关闭，现分别由无锁关闭门、暖场分支、两秒字节预算后台收发和“先关门后 final”修复并有并发回归。五条 DashScope route 真实探针均 healthy，开场、服务端收音、暖场/正式 final 已在隔离数据库与本机 LiveKit 跑通；Qwen 实时理解默认关闭深度思考的修复仍待一次外部追问复验，因此整体生产验收状态不变。
- 2026-09-03 随后的真实候选人事故确认了表达面的两个完整性缺口：本地 VAD 正常 barge-in 清空旧 `<audio>` 后，旧元素的迟到 `error` 被误报为 `CANDIDATE_RUNTIME_FAILED`；受管 TTS 的非 seek WAV 又可能保留 signed-limit RIFF/data 占位头，GLM-TTS 样本还存在外层 RIFF 恰好漏计 WAVE form type。CandidateInterviewExperience 现以 playback identity 隔离替换/打断/关闭后的全部迟到回调，并按 `performance_id` 拒绝旧 interrupt；PrivateAssetImporter 在私有落盘前严格走 RIFF chunks，只规范化这两种有实证模式、以新字节重算 checksum，其余畸形容器失败关闭。自动化已覆盖正常 barge-in 不暂停、当前真实错误仍暂停、自然结束唯一确认和 WAV 完整性；历史冻结资产与已暂停会话不原地修改。

### REALTIME-WARMUP-VAD-RANGE-003（verified（仓库），目标环境复验 pending）

- 实流程还暴露了四个互相关联但不改变业务答案语义的运行时缺口：浏览器媒体元素的单 Range 没有完整 200/206/416 合同；客户端把 `evidence.stream.open` 已请求误当成服务端已 ready；按回调帧计数的低门槛 VAD 会把扬声器回声和约几十毫秒静音抖动当成讲话边界；VRM 健康 gate 对同一 29.x FPS 与界面显示整数判定不一致。表现可以是私有音频请求在 DevTools 显示取消、开场刚播即被 barge-in、页面显示“正在听”却没有权威字幕，或 30 FPS 画面被错误失败关闭。
- 本地私有 `GET|HEAD /api/v1/private-files/{token}` 现对无 Range、合法单一 closed/open-ended/suffix Range、不可满足/畸形/多 Range 分别冻结 `200/206/416`，HEAD 与 GET 状态及长度头一致，仍执行原 grant、租户和审计边界。
- Evidence open 现分为 `requested` 和服务端 `ready`：服务端建立/确认 existing-open 后用同 causation transient floor 事件应答；候选人端只接受本连接当前命令的匹配 ACK，断线才有界 reassert。ready 前不显示“正在听”也不发普通 VAD；若握手时已经持续说话，ready 后承接该 speaking 状态。多连接广播中不匹配 ACK 只推进 cursor，不开 gate、不 flush speech，也不替其他标签页自动开流。
- AudioWorklet VAD 改按 `sampleCount/sampleRate` 累计毫秒；agent-speaking 使用更严格的能量和持续门槛抑制回声，真实 barge-in 仍在 200ms 内静音，speech stop 使用稳定静音窗口避免约 37ms 抖动。Avatar FPS 使用与 UI 一致的 `Math.round(fps) >= minimum_fps`，30 FPS 门槛下 29.5 通过、29.4 失败。
- 暖场 final/seal 失败对原流只执行一次 destructive finalize，保留首次真实错误并持久写入 `calibration_retry_required`。服务端 gate 未清时拒绝新 Evidence open；候选人只在显式点击“重新试音”后发送 `warmup.retry`。owner 执行 reset 后的 live transient ACK 或 snapshot `retrying + retry_required=false` 均允许当前 control 用新 causation 恢复；客户端忽略同 retry cause 的重复 ACK，服务端用 generation fence 与 existing-open 幂等 ACK 避免刷新、heartbeat、断线或多标签页重复创建付费 STT。
- 本工作项不修改抽题、计划/题目冻结、权威证据形成、答案、评分、受控追问、S2S/cascade 或人工接管业务逻辑。全量仓库验证已通过，因此状态为 `verified（仓库）`；新会话 Chrome/Edge/Safari、LiveKit/STT、扬声器回声/真实打断和私有媒体 seek/Range 仍为目标环境待办，在这些证据齐备前不得标记 `closed` 或生产验收通过。

### EVIDENCE-LEASE-TELEMETRY-004（verified（仓库），目标新会话复验 pending）

- 会话 `iv_2dd0eecc8dd8491f` 的暖场 STT 已成功打开并完成识别，正式题目也已播放；但正式候选人 Evidence/STT 从未打开。事故时间线显示 owner 最后一次续租后超过 15 秒未续上，随后以 `EVIDENCE_OWNER_FENCED` 暂停。这不是正式 STT Provider “听不懂”，而是正式收音命令尚未执行前权威 ingress 已自围栏。
- 根因是候选端逐 viseme 发送约 400 条 `telemetry.observe`。旧 AgentChannel 把每条遥测当作普通领域信号，依次执行幂等读取、`processed_signal_keys` 写入和 SQLite 整会话 reload；积压占用事件循环，使 owner 续租任务错过 15 秒 lease。暖场发生在积压前，所以“试音能识别、正式面试不能识别”的表象与该时间线一致。
- `telemetry.observe` 现为 candidate-only、无 PII 的 process-only 旁路：合法固定词汇样本只更新进程指标并显式让出调度，不获取领域信号锁、不执行幂等读写、不修改 InterviewSession/事件/Evidence journal/takeover；非法、未知或越界样本静默丢弃，不能形成 problem 或暂停。其他非遥测信号的幂等和领域路径保持原样。
- 候选端把 `avatar_viseme_drift_ms`、`avatar_freeze_ms` 按 metric 独立聚合为 1 秒最大值窗口，每窗至多发送一次；socket 未打开、窗口中断、播放停止或体验关闭时不排队、不重放，其他 latency 指标即时上报。Evidence owner 续租增加 `evidence_owner_renew_scheduler_lag_ms/evidence_owner_renew_db_latency_ms/evidence_owner_renew_success` 三项无标识符观测，用于区分调度延迟、数据库耗时和续租结果。
- 续租安全没有放宽：过期 owner 仍 self-fence，有无 successor 都不能迟到复活；owner/lease/epoch fence、独立的 control-generation 命令 fence 和 CandidateAnswer commit fence 均保持严格。仓库回归覆盖 SQLite 下 512 条重复遥测不改变任何领域指纹、调度让步、非遥测幂等、多个 lease 周期续租以及过期 owner 自围栏，状态为 `verified（仓库）`。必须以全新预约/会话在目标浏览器和实际 LiveKit/STT 路由复验暖场后正式 Evidence open、字幕和持续多轮续租，完成前不得标记 `closed` 或 production accepted。
- 本项不改变抽题、计划/题目冻结、权威证据、CandidateAnswer、评分、受控追问、S2S/cascade 或人工接管业务逻辑；事故会话保持暂停，不原地恢复。

### FORMAL-STT-PARTIAL-BACKPRESSURE-005（verified（仓库与本地服务），目标新会话复验 pending）

- 会话 `iv_31e593c1ef5b4098` 中候选人讲话持续到进入 LiveKit，Egress 记录 4,371 个 audio buffer 且 dropped 为 0，但应用的权威 Evidence 只保存 1,143 个 20ms 帧（22.86s）；其中前 19.195s 为静音，只剩约 3.66s 有效语音，对应页面上的短 partial。随后没有 formal final/CandidateAnswer，服务端以 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED` 严格暂停。这排除了候选人没说、麦克风先停、VAD 提前 seal、LiveKit 丢轨和 Evidence lease 失效。
- 事故同时确认旧暖场播放的迟到 `avatar.performance.stopped` 在新正式题 performance 已建立后仍继续切换 floor，使正式 STT 在题目播放期间提前打开并摄入长静音。现在 stop 必须携带当前 ID 并在事务中 compare-and-clear 成功；缺失、迟到、重复或不匹配的 stop 不再产生事件、改 floor 或推进暖场/结束。
- 失败版本把每个 `transcript.partial` 同步放在 `_receive_audio` 下游；即使是 transient，也会更新 SQLite 会话并做跨实例投影，可以耗尽两秒 LiveKit sink。现在 partial 由独立、有界、按 `(kind, turn_id)` latest-wins 的 projector 处理；501 帧高压测试在投影故意阻塞时仍全部进入 Evidence，final/reset/stop 屏障又阻止迟到 partial。非 partial 仍是同步无损路径。
- DashScope adapter 现在把 16kHz/mono/PCM 默认合并为约 100ms/3,200B 后发送，finish 刷出尾包、abort 清空缓冲，并把所有未发字节纳入两秒背压预算。已安装的 `websockets>=15` 会默认发现环境代理；adapter 现严格落实已有 `use_environment_proxy=false` 配置为 `proxy=None`，仅显式开启才使用环境代理。
- LiveKit track task 不再把所有原因压成单一 `RuntimeError`；adapter 只保留经校验的错误代码和异常类型，特权诊断在统一业务问题下记录 `cause_code/cause_type`。URL、token、凭据和转写均不落库，候选人投影也不包含诊断字段。
- 这些修改仅改变播放回调的时序验证、可丢字幕投影、相同 PCM 的网络分包、连接路径和故障观测；没有改动抽题、冻结计划、权威 final/Evidence、CandidateAnswer、评分、追问、S2S/cascade 或人工接管规则。事故会话保持暂停，必须用新邀请复验长回答、final 和多轮。

### 真实状态与关闭条件

- 状态保持 `in_progress（仓库实现已收口，目标环境/数据验收 pending）`。旧 `/api/v1/interviews/{id}/live`、`/stt-stream`、候选人/企业 `audio-answers`、`avatar/speak`、avatar close 及多通道前端 runtime 已物理删除，OpenAPI/静态资源合同阻止回归；仓库不会在故障时退回静态肖像或问卷式文本提交。
- 当前仓库 `model/interviewer.vrm` 与 manifest 已按权利人确认的 `personalProfit` 范围通过本地合同，不再是本机演示阻塞；企业法人、客户交付或其他超出该范围的部署仍须取得匹配使用范围的许可。目标设备真实帧率/口型同步、LiveKit/TURN/Egress/私有对象存储、外部 STT/TTS/LLM route 凭据与健康、真实浏览器权限/断线/抖动/首音/barge-in/A/V 同步/并发指标仍为 `environment_pending`。正式 ingress 只接受 `database_fenced`；仓库已完成多实例协调与恢复代码，但只有目标 PostgreSQL/Redis/LiveKit/OSS 的故障注入和绑定当前 release 的签名 acceptance v2 报告才能证明该部署可用。
- 真实脱敏对话金标、意图/证据覆盖/歧义/矛盾识别准确率、追问有效性与 AI/人工评分一致性仍为 `data_pending`；在获得经授权样本前不宣称业务质量已校准。

## 生产化审查问题（DOC-GAPS-001）

| ID | 优先级 | 状态 | 问题 | 修复与验证 | 剩余边界 |
| --- | --- | --- | --- | --- | --- |
| `FILE-001` | P1 | `verified` | 简历仍依赖 `resume_text`/调用方 URI，没有 PDF、扫描和私有文件真相 | 新增 PrivateFileStorage、FileObject、multipart/URL、SSRF/隔离/扫描/PDF 解析；原件和解析文本分别私有化；旧 JSON 入口删除；官方 OSS SDK 已纳入依赖，command/clamd INSTREAM 扫描合同、本地/OSS contract 与恶意/环回/幂等测试通过；官方 ClamAV arm64 daemon 的 PING/干净样本/EICAR 集成测试已通过 | 真实 OSS bucket/RAM、目标 clamd 完整签名库更新与告警 `environment_pending` |
| `ASYNC-001` | P1 | `verified` | 题库构建/PDF/worker 缺少退避、dead-letter、监控和重放 | import/rebuild/build 与 PDF 均返回 job；Outbox 增加最大尝试、指数退避、dead-letter、指标和审计重放；故障矩阵通过 | 外部告警平台由部署环境接入 |
| `DURABLE-MODEL-RETRY-001` | P1 | `verified（仓库）` | 模型调用首次可重试失败时，部分服务提前写领域 failed；TTS 因此推进 Question version，下一次成功响应被自身的 source-version guard 判成 superseded，出现 6/10 假终态 | 统一 `Outbox failed=重试等待 / dead_letter=领域终态`；SpeechBuild 把可领取失败计入 pending，TTS 保持 Question version，题库导入、Resume Review/经历题、评分和报告同步采用终态门禁；后端故障注入与 React 部分完成试听回归通过 | 真实供应商限流/超时率、目标 Celery/Redis/PostgreSQL 的长时间故障恢复仍为部署环境验收 |
| `STREAM-001` | P1 | `verified（仓库）` | 没有 `stt.streaming/open_stream`、唯一 final 和断流修复 | 新增 streaming schema、网关开流、序号/大小/唯一 final 校验、WebSocket、私有录音和 batch repair；DashScope 提供低延迟 partial/final，`media_http` 保留通用 batch-final stream；端到端测试通过 | 阿里云真实凭据、目标网络、WER/延迟/费用 `environment_pending` |
| `FORMAL-EVIDENCE-DRAIN-SQLITE-008` | P1 | `verified（仓库与本地服务）` | SQLite 每个 VAD/journal 事务后全库回载，饿死正式 LiveKit Evidence sink；seal 又缺少已接收帧排空屏障，事故 51 秒流只进入 2.5 秒 PCM | SQLite commit 按事务 delta 同步兼容缓存；LiveKit accepted/delivered 水位在 warm-up/formal seal 前排空；VAD stop 800ms、确认试音清字幕；定向与全量后端/前端、build、compileall、diff check、本地 readiness 通过 | 必须用新邀请复验 60 秒正式回答、唯一 final/CandidateAnswer 和私有录音，完成前保持目标环境 pending |
| `STT-FINAL-PARTIAL-TAIL-007` | P1 | `verified（仓库）` | DashScope 已有 committed 分句时，finish 以二选一逻辑丢弃最后未 `sentence_end` 的有效 partial，导致 final 尾部截断 | partial/final 共用单一 transcript projection；final text/segments 合并 committed 与尾部 partial，空结果和唯一 final 规则不变；Provider/React 回归通过 | 用新会话复验真实 Provider 尾句，并在正式回答核对 CandidateAnswer 与录音 |
| `MODEL-STREAM-PROBE-001` | P1 | `verified（仓库与本机）` | 流式 STT 模型测试用静音 WAV 强求 final，真实握手成功仍误报 `provider_final_transcript_missing`；实时语音对话没有统一测试 schema | 模型配置与 route 复用 stream handshake probe；STT/实时语音收到 ready 后主动关闭并返回 `probe_mode=handshake`；Qwen 3.5 session、输入转写模型、音色和受控指令更新顺序按当前协议校正；合同测试与本机百炼真实握手通过 | WER、final/首音延迟、打断、音质和费用仍为独立环境验收 |
| `SECURITY-001` | P1 | `verified` | 联系人/凭证明文、无 RBAC/公开限流、敏感访问/失败请求无审计，URL token 可能进入日志 | 联系人 Fernet+租户 HMAC、ProviderSecretVault、生产 Bearer RBAC、Redis fail-closed 公开限流、未认证/成功请求审计、签名文件/媒体、媒体实际下载审计；Auth audit 与 Uvicorn access log 均遮蔽私有文件/媒体/邀请 path token 及 `grant/token/ticket/signature/access_token` 查询值，过滤器合同与启动安装路径有回归 | 外部 IdP/企业 SSO 尚未选择 |
| `DATA-001` | P1 | `in_progress` | 只有 SQLite JSON，缺少生产租户 RLS 和数据库唯一约束 | 已提供显式 owner migration 与最小权限 runtime adapter；本机 PostgreSQL 16 真实验证事务/CAS、RLS、Outbox/预约约束、题库索引 EXPLAIN，离线 SQL 不变量继续通过 | 目标生产 PostgreSQL 的角色、并发负载、备份恢复与 EXPLAIN 仍为 `environment_pending`，完成前不改 verified |
| `MEDIA-001` | P1 | `verified（仓库）` | mock TTS URI/公开 media 不能作为生产资产 | 非 mock TTS 和生产候选人录音使用 PrivateFileStorage+FileObject；移除 `/media` 静态挂载；`media_http` 支持 HTTPS audio/video 数字人；腾讯云专属 adapter/TCPlayerLite 已实现 WebRTC 云渲染与会话回收；签名访问与下载审计通过 | 真实账号、形象授权、并发和媒体播放质量 `environment_pending` |
| `AVATAR-DELIVERY-001` | P1 | `verified（仓库与本机资产）` | 数字人曾只能隐式走云 route，创建人无法按预约选择低成本自研方案；云降级与前端播放容易复制分支 | 预约 settings 增加强类型 `local/cloud`；新预约默认 local，历史缺字段按 cloud；AvatarDelivery 以 Local/Cloud 两个 adapter 复用冻结 TTS 签名访问、统一响应、React play/stop 和云 session 回收；正式 Agent 接入本地 VRM 1.0/15-viseme、统一音频时钟和私有 AgentExpressionAudio；`interviewer.vrm` 及 manifest 已通过实际二进制/hash/许可合同，preset 元音不会再被误判缺失；候选人致命故障真实暂停且自我介绍只在会话快照后出现 | 当前资产仅确认 `personalProfit` 使用范围；目标设备 FPS/A-V 同步、超出该范围的部署许可及腾讯真实媒体质量/费用仍为 `environment_pending` |
| `AGENT-AUDIO-PLAYBACK-001` | P1 | `verified（仓库与本机资产语料）` | 正常 barge-in/替换清理旧 `<audio>` 时，迟到 error/ended/play reject 可被误当成当前正式播放故障；真实 TTS WAV 的流式占位或固定 outer-size 偏差又在私有落盘前未校验 | 播放代次与 `performance_id` 隔离所有迟到回调，当前真实错误继续 fail-closed；共享 PrivateAssetImporter 严格解析 RIFF/WAVE，只规范化 exact signed-limit stream pair 与完整 EOF 下恰好漏计 WAVE form 的 outer size，其他畸形失败关闭并按规范化字节重算 checksum；前端、后端及 64 个本机 WAV 语料验证通过 | 历史冻结 FileObject 不原地改写；受影响题目须经既有 regenerate 形成新资产并建立新预约，目标 Chrome/Edge/Safari 的完整媒体矩阵仍随里程碑 18 验收 |
| `MODEL-PROVIDER-001` | P1 | `verified（仓库）` | 只有 OpenAI-compatible LLM/Embedding，没有真实 TTS adapter；DashScope manifest 声明能力但不可执行；路由 UI 漏掉 TTS 并把 `retry_count` 误写为 `max_retries` | OpenAI-compatible 增加 Speech TTS；DashScope 增加 Qwen Chat/Embedding、Qwen3-TTS/CosyVoice 与 streaming/batch ASR；Volcengine 增加 Ark Chat/Embedding、Seed ASR streaming/batch、Seed-TTS 与 Seeduplex adapter，并隔离 Ark/Speech 两种凭据；腾讯云增加数智人 WebRTC adapter；路由请求改为强类型、生产缺 route 失败关闭；供应商合同、registry、路由与私有资产哈希测试通过 | 阿里/火山/腾讯真实账号、区域、API Key、Resource ID、模型/形象授权、音质/WER/延迟/费用验收为 `environment_pending` |
| `MODEL-PROVIDER-002` | P1 | `verified（仓库）` | manifest 没有可执行模型目录/默认配置语义，路由只能手输模型；DeepSeek/智谱未成为独立插件，OpenAI-compatible 厂商容易复制 runtime | 增加 defaults、predefined/customizable 与模型目录校验；DeepSeek/智谱薄 adapter 复用共享 runtime；千问目录/UI 显式化；配置默认值、目录路由约束、厂商 HTTP 合同和全量回归通过 | 三家真实 API Key、区域/账号授权、模型可用性、延迟/费用与结构化输出稳定性为 `environment_pending` |
| `MODEL-PROVIDER-003` | P1 | `verified（仓库）` | 智谱只声明 LLM，单一 `test_model` 会把 `glm-tts` 当成 LLM；配置 UI 无编辑/按能力测试，HTTPX 初始化时缺 SOCKS 依赖会裸抛 500 | 增加 GLM-TTS adapter/模型目录、`capability + model` 测试协议、配置编辑 UI、显式环境代理开关和结构化 transport 错误；厂商 HTTP 合同、API、UI 与全量回归通过 | 真实智谱凭据、模型授权、音色、音质/延迟/费用为 `environment_pending` |
| `MODEL-CONFIG-V2-001` | P1 | `closed` | 厂商账号、API Key、具体模型和 route target 混在 `ModelProviderConfig`，前端硬编码厂商字段，多个模型会复制凭证和参数 | 拆分 ProviderConnection/ModelConfiguration/ModelRoute；v2 manifest 提供动态表单；route 只引用 ready model；旧 API、collection 和运行时 target 已删除；显式迁移与回归测试通过 | 真实厂商模型仍需逐个测试，健康状态为部署环境事实 |
| `QUESTION-GEN-TRUNCATION-001` | P1 | `verified（仓库）` | `finish_reason=length` 曾被归为通用 JSON 错误，网关与 Outbox 会用相同参数嵌套重试，双槽位长响应反复计费且不能保留批次进度 | 新增 `provider_output_truncated` 与失败 token 诊断；非重试工作首轮 dead-letter；生题 attempt 只调用一次 Provider；双槽位截断原子拆成两个单槽位工作；Prompt/Schema v2、Provider/网关/Memory/SQLite/工作流合同测试通过 | 未自动重放历史失败批次；目标 DeepSeek 账户的 10 题并发、费用与真实截断恢复仍为 `environment_pending` |
| `RESUME-REVIEW-TRUNCATION-001` | P1 | `verified（仓库）` | 真实 `deepseek-v4-pro` 单次简历审阅的 6000 输出 token 中有 4081 个 reasoning token，以 `finish_reason=length` 结束并留下未完整 JSON | 最终 Prompt/Schema 继续升级为 v6/v4：审阅只输出初筛与有限证据，不再耦合经历题；单次截断时丢弃半截输出、自动转为完整 Map/Reduce；问题在符合资格后由独立 v1 合同生成。Prompt 合同、截断降级和资格门禁测试通过 | 不自动消费真实模型重放历史失败审阅；目标账户重试质量/时延/成本仍为 `environment_pending` |
| `QUESTION-SPEECH-DEFAULT-001` | P1 | `verified（仓库）` | 非生产环境创建题库时无条件绑定开发 mock，即使组织已有 ready 的真实题目 TTS route；mock 资产被标为语音 ready，点击试听只得到难理解的“非私有生产资产”错误 | 新题库优先解析并冻结 enabled/ready `question_speech_generation` route primary 与默认音色；题目投影增加 `speech_preview`；mock 试听返回专门错误和配置建议；React 明示“开发模拟语音（不可试听）”并禁用无效按钮；后端/前端合同测试通过 | 不批量改写旧题库或删除历史 mock 资产；目标 TTS 的真实音质、费用和对象存储仍为环境验收 |
| `TEST-ISOLATION-001` | P0 | `verified` | `reset_store_for_tests()` 曾取得默认 SQLite 并执行 `reset()`，全量测试会删除本地开发数据 | helper 改为直接替换成全新 `InMemoryStore`；回归测试证明临时 SQLite 不被重置，99 项全量测试前后开发 DB SHA-256 不变 | 本轮误删前数据无法从 SQLite `.recover`/本地快照恢复；已恢复可确认的智谱非秘密配置，API Key 需管理员重填 |
| `REALTIME-001` | P2 | `verified（仓库）` | 多实例事件、断路器、心跳和厂商实时媒体曾缺失 | Redis bus/共享断路器/心跳已验证；新增浏览器 16k PCM → DashScope duplex ASR、腾讯数智人 create/stat/start/drive/close 与 TCPlayerLite WebRTC/SFU 播放 | 目标 Redis 集群、阿里/腾讯真实凭据、形象并发、WER/延迟/费用仍为 `environment_pending` |
| `REALTIME-DIALOGUE-001` | P1 | `verified（仓库）` | 追问曾依赖“完整 STT → 完整 LLM → 完整 TTS”的串行链路，评分同步阻塞下一轮；追问预算、父子轮次、PCM 录音和断线恢复也没有形成同一运行时合同 | 新增 `speech.dialogue_realtime` 深模块及 OpenAI Realtime、DashScope Qwen Realtime、Volcengine Seeduplex adapter；预约可选 `cascade/s2s`。S2S 原始音频只在内存预生成，final transcript 与冻结 ApprovedConversationAct 逐字一致后才物化私有音频并下发；豆包使用 `speech_text_buffer.replacement` 提交同一批准文本。权威转写仍由 STT 形成 CandidateAnswer，完整评分由 Outbox 异步执行。追问为零权重子轮次并受深度、次数、时长、证据、能力点和安全门禁约束；浏览器恢复、batch repair、心跳和角色安全事件均有合同/端到端测试 | OpenAI/百炼/火山真实凭据、模型权限、Resource ID、区域、网络抖动、首音延迟/打断/音质/费用为 `environment_pending`；任一真实 route 未通过当前环境探针前不得标记 ready |
| `REALTIME-AGENT-001` | P1 | `in_progress（本机 VRM 与 LiveKit/Egress 已验证，生产验收 pending）` | 正式面试曾由多个路由/WebSocket 和 React 直接编排，没有单一发言权、权威证据、语义理解、可中断 3D 表达、私有录制与人工接管合同 | 新增 AgentChannel/InterviewAgentRuntime、最小权限 LiveKit ticket/Egress、`database_fenced` receive-only Evidence ingress、数据库时钟 ownership/fence、持久 journal＋连接无关 owner executor/remote receipt、私有 segment/checkpoint＋owner-loss repair＋浏览器 gap backfill、warm-up、Floor/VAD/barge-in、角色安全回放、版本化理解、冻结证据两层追问、root evidence group、私有 AgentExpressionAudio、VRM AvatarPerformance、takeover lease 与 acceptance v2 release/org gate；旧实时 routes 和前端多通道实现已删除。本机 VRM preset/custom 合同、真实暂停回执和会话快照前 gate 已有定向回归；单参数本地 LiveKit/Egress/Redis 已生成含音视频的真实 Participant Egress MP4，媒体与权威音频 readiness 均通过 | 当前 VRM 许可只确认 `personalProfit`；生产目标 LiveKit/TURN/OSS/PostgreSQL/Redis、真实 STT/TTS/LLM、浏览器/网络/并发硬指标和分组织试点为 `environment_pending`；脱敏对话/评分金标为 `data_pending`，绑定当前 release 的签名报告形成前不得 production ready |
| `EVIDENCE-LEASE-TELEMETRY-004` | P1 | `verified（仓库）` | 逐帧 avatar telemetry 走 SQLite 领域幂等/整会话写入，饿死 Evidence owner 续租，使暖场成功后正式 STT 尚未打开就自围栏暂停 | 遥测改为 candidate-only process-only 旁路并显式让出调度；高频 avatar 指标按 key 做 1 秒 max 聚合；续租增加无 PII scheduler/DB/success 观测且严格过期 self-fence 不变；SQLite、幂等、公平调度、连续续租和过期 fencing 回归通过 | 必须用目标浏览器、LiveKit/STT 和全新会话复验正式 Evidence/字幕及持续续租，完成前状态为目标新会话 pending，不能称 production accepted |
| `WARMUP-BACKPRESSURE-RECOVERY-006` | P1 | `verified（仓库）` | 暖场大部分语音已识别后，两秒 Provider 背压触发整场暂停；旧 seal 又迟到完成，形成 pause 与 awaiting-confirmation 冲突 | 暖场 epoch 失效和单一 retry seam 阻止迟到 seal；显式 retry 先恢复 LiveKit iterator；LiveKit/DashScope 默认五秒无损有界窗口，正式 Evidence 断流仍失败关闭；竞态/背压/恢复合同测试通过 | 必须用新邀请在目标浏览器和真实 Provider 连续试音、重试并进入正式题；事故会话不恢复，完成前保持环境 pending |
| `FAIRNESS-001` | P2 | `verified（仓库）` | 只有抽题分布，没有 AI/人工评分一致性与 cohort 差异校准 | 保留抽题公平性投影；新增只接受 current evaluation ID、人工分和 opaque cohort 的校准 API，输出 MAE/RMSE/偏差/分层/样本量告警且绝不自动改分 | 用户尚未提供真实脱敏金标；实际公平性结论为 `data_pending` |
| `RETENTION-001` | P1 | `verified` | 敏感数据只有设计中的到期字段，没有可执行删除边界 | 新增默认 dry-run 的管理员留存服务；显式执行删除私有文件/录音并清空联系人、审阅、经历题、转写、评分/报告敏感内容，审计测试通过 | 企业实际留存天数与 legal hold 策略由部署配置决定 |
| `COMPAT-001` | P2 | `closed` | 计划 `items`、管理员直建会话、文本答案和旧向量 QuestionService 曾同时存在 | 提供显式 v2 迁移命令；运行时旧表示、旧 schema、旧路由、旧 service/repository/worker 分支和前端 fallback 均已删除；OpenAPI/端到端测试通过 | 部署已有旧数据时必须先运行 v2 迁移；当前 SQLite 检查无计划数据 |
| `KB-SPEECH-001` | P1 | `verified（仓库与本机）` | 题库页曾平铺全部题目，题库只能保存声音且 HTTP 会直接等待 TTS；运行中切换模型还会与进度 version 写入竞争 | 已实现 KnowledgeBaseSpeechProfile、voice catalog、整库 SpeechBuild、revision 防旧写、分层 React UI/API、模型引用删除保护与 Celery+DurableWorkItem；切换改为 profile revision 语义 CAS 并原子取消旧任务，前端增加进度自动刷新、取代提示和失败重试 spinner；全量回归、Celery eager 和前端行为测试通过 | 真实外部 TTS、目标 Redis/PostgreSQL 属部署环境验收，不回退本项仓库状态 |

### KB-SPEECH-001 修改步骤与批量重建不变量

1. React `#questions` 改为只加载当前组织 KnowledgeBase 摘要；点击进入 `#questions/{knowledge_base_id}`，详情路由再加载题目、当前语音配置、TTS 模型/声音目录和构建进度。
2. KnowledgeBase 增加 `speech_profile` 与独立 revision，绑定已 ready 的 TTS ModelConfiguration、voice、language、format 和 speaking rate；旧 `language/voice_profile_id` 通过显式迁移转成 profile，无法解析模型时标记 `configuration_required`。
3. `PUT /knowledge-bases/{id}/speech-profile` 使用 CAS 和 Idempotency-Key；配置变化原子创建 `knowledge_base.speech.rebuild` 父工作项并立即返回 202，不在请求线程调用 Provider。
4. 所有 Celery task 和异步执行编排放在 `app/workers/`。父 task 冻结 Question ID/version manifest、分批 fan-out 子工作项；子 task 调用显式 TTS 模型并复制到 PrivateFileStorage。
5. 子 task 提交前验证 Question version 与 speech profile revision；旧 revision 只能成为历史资产或 superseded，不能覆盖当前指针。历史计划/会话资产保持可读。
6. Celery 只调度 `organization_id + work_item_id`；DurableWorkItem 继续提供租约、幂等、退避、dead-letter、进度和人工重放。Beat dispatcher 补发发布失败与过期租约。
7. 自动化验收至少覆盖：路由级加载无全量题目首屏；无效/非 TTS 模型与非法 voice 拒绝；切换模型或声音整库 fan-out；重复投递不重复资产；运行中再次切换时旧结果不覆盖；部分失败只重试失败项；worker crash 恢复；已批准计划和历史会话继续播放旧资产。

### FILE-001 修改步骤与恢复语义

1. API 在创建 `ResumeDocument(processing)`、`FileObject` 和 `resume.ingest` 工作项前校验上传 PDF magic/MIME/大小；URL import 强制 `Idempotency-Key`。
2. URL worker 禁用环境代理，在初始 URL 和每个重定向重新解析 DNS，拒绝所有非全局地址、嵌入凭据和生产 HTTP。
3. 文件只在隔离区读取；扫描失败、感染、加密 PDF、空文本或解析失败时原子标记 Resume/File/WorkItem 错误，不能触发 Resume Review。
4. clean PDF 和解析文本分别写 PrivateFileStorage，成功事务只保存受控对象键/哈希；API projection 移除正文与键。
5. worker 进程中断可从同一 work item 重试；相同幂等键返回同一简历版本，不重复创建对象。dead-letter 只能经管理员写明原因后重放。

### STREAM-001 修改步骤与最终转写不变量

1. `ModelGateway.open_stream(StreamingSTTRequest)` 根据组织/capability/purpose 解析 route，只允许在任何音频被接受前 fallback。
2. `ValidatedSTTStream` 限制单 chunk、总字节、单调 sequence，并拒绝多个 final、final 后继续发送或 provider 缺失 final。
3. 正式 `AuthoritativeEvidenceIngress` 从冻结的 LiveKit candidate microphone 轨保存私有录音；唯一服务端 final 通过 `submit_streaming_answer()` 进入生命周期与评分，不接受浏览器 final。旧 `stt-stream` 候选人入口已删除。
4. provider 中断或 finish 无 final 时，录音进入 `stt.batch/candidate_answer_repair`；修复成功前轮次不能进入 evaluating。
5. 生产 readiness 要求 streaming/batch route 为非 mock、implemented 且 `last_health` 未超过 TTL。

### MODEL-PROVIDER-001 修改步骤与外部边界

1. `openai_compatible` 在原 Chat/Embedding deep module 内增加 `/audio/speech` 二进制响应适配，统一产出 `TTSSynthesizeResponse`，不把音频协议泄漏到题库或面试编排。
2. `dashscope` 复用 OpenAI-compatible Qwen Chat/Embedding，在 TTS seam 内分别转换 Qwen3-TTS multimodal-generation 与 CosyVoice/Qwen-Audio `SpeechSynthesizer` 请求。
3. 非 mock 远程音频必须复制到 PrivateFileStorage，最终内容哈希以实际私有文件为准，不能信任临时 URL 或供应商元数据。
4. ModelRoute API 拒绝未知 policy/target 字段和同组织重复 capability/purpose；生产缺失精确 route 返回 `provider_route_missing`，development/test 才可使用 mock fallback。
5. CI 使用离线 HTTP fake 验证请求形状、鉴权、响应归一化、音频格式和哈希，不依赖真实 API Key。只有在目标区域用真实凭据完成 route test、私有音频播放、延迟/费用和 readiness TTL 验收后，外部状态才能从 `environment_pending` 改为健康。

### MODEL-PROVIDER-002 修改步骤与 Dify 参考边界

1. 参考 Dify 官方插件的声明式 Provider/Model schema 和 runtime adapter 分工，把 `defaults`、`model_selection`、模型 ID/能力/default 元数据收进现有 `provider.json`；不复制 Dify 面向多租户插件市场的庞大 ProviderManager。
2. Registry 拒绝空 predefined 目录、重复模型 ID、模型越权能力和同一能力多个默认模型；Provider 配置先合并非秘密 defaults 再按 schema 校验。
3. ModelRoute 对 predefined provider 强制校验 `model + capability`，customizable provider 保留自定义模型 ID；管理页面从 catalog 预填地址并按目标能力给出模型候选。
4. `DeepSeekProvider` 与 `ZhipuAIProvider` 继承 `OpenAICompatibleProvider`，只声明 provider ID 和 `json_object` 结构化输出策略；共享 runtime 统一完成 HTTP、Bearer、错误、usage 与响应归一化。
5. `tests/test_openai_compatible_vendor_providers.py` 用离线 HTTP fake 验证两家 URL、鉴权、JSON Schema prompt、`response_format=json_object` 和 Provider 元数据；registry/API 测试验证目录与默认配置，全量 `93 passed`。
6. 真实供应商仍需分别创建配置和 route 后执行连接测试；账号未授权、模型退市或厂商兼容差异属于外部健康事实，不能因离线合同通过而标记生产 healthy。

### MODEL-PROVIDER-003 修改步骤与能力模型边界

1. 智谱 manifest 同时声明 `llm.chat_json`、`llm.chat_text` 与 `tts.synthesize`；`glm-5.2` 只属于 LLM，`glm-tts` 只属于 TTS，每项能力各有默认模型。
2. `ZhipuAIProvider` 继续复用 OpenAI-compatible Chat/二进制语音 runtime，只在厂商 seam 校验官方 `/audio/speech` 的 `glm-tts`、WAV/PCM、1024 字符上限和默认 `tongtong` 音色。
3. Provider test body 可选 `capability + model`；解析顺序为请求、`test_models[capability]`、能力匹配的旧 `test_model`、manifest 默认模型。`predefined` 模型/能力不匹配时调用前返回 `MODEL_PROVIDER_MODEL_UNAVAILABLE`。
4. 管理页面新增编辑和按能力测试弹窗；编辑以 `expected_version` 提交，空 API Key 不发送 `credentials`，测试切换到 `tts.synthesize` 时模型自动切为 `glm-tts`。
5. 共享 HTTP transport 默认 `trust_env=false`，只有显式配置环境代理才继承代理变量；transport 初始化失败映射为 `provider_transport_unavailable`。离线合同、API/UI 与全量 99 项测试已通过，真实账号联调保持 `environment_pending`。

### MODEL-CONFIG-V2-001 修改步骤与模型配置边界

1. `ProviderPluginDefinition` 通过 v2 manifest 声明连接、凭证、模型类型和厂商模型参数表单；服务端严格校验，前端使用同一组通用控件渲染。
2. `ProviderConnection` 只保存 API Key 引用、Base URL、区域等连接级信息；`ModelConfiguration` 保存一个 `llm/embedding/tts/stt/avatar` 模型、专属 settings、统一 defaults、能力和健康事实。
3. 模型测试允许 `untested/failed` 配置执行隔离探针，成功后置 ready；普通调用和 route 创建仍拒绝未就绪模型。
4. ModelRoute target 只保存 `model_configuration_id/timeout_s/pricing`；网关在一次深模块调用内解析模型、连接、凭证和 adapter，并应用调用显式参数优先的模型默认值。
5. `app.migrations.model_configuration_v2` 支持 SQLite dry-run/一次性迁移；旧 API、`provider_configs` collection 和 `provider_config_id + model` 运行时表示已删除，动态表单、迁移、API、网关和 UI 回归测试覆盖该边界。

### TEST-ISOLATION-001 修复与数据恢复记录

1. 根因是 `reset_store_for_tests()` 调用 `get_store()`，默认后端为 SQLite，随后 `SQLiteStore.reset()` 执行四张表的 `DELETE`；这违反测试不得触碰开发数据的边界。
2. helper 现直接替换全局 store 为新 `InMemoryStore`，不再打开或 reset 默认数据库；新增测试先向临时 SQLite 写入 sentinel，再执行 helper 并重开数据库验证 sentinel 仍存在。
3. 修复后执行全量 99 项测试，`data/interviewer.sqlite3` 测试前后 SHA-256 均为 `816ab277130d9c53aa1d0032b8331d4d13e68bfd3ce75ab1f661cec6abac3498`。
4. 对受影响数据库先复制到 `/private/tmp/interviewer-reset-20260825.sqlite3`，再用 SQLite `.recover` 和本地 APFS snapshot 检查；未找回已删除凭证或业务记录。不能猜测密钥，已把可确认的 `mpc_11b4d940e3134074` 非秘密智谱配置按原 ID 恢复为 disabled，等待管理员在新编辑 UI 中补 API Key 后启用。

### SECURITY-001 修改步骤与审计边界

1. CandidateProfile 只保存邮箱/手机号密文、掩码和租户 HMAC lookup hash；匹配使用 hash，生产拒绝旧明文。
2. Provider credentials 在 repository seam 密封；生产缺少密钥或读取旧未密封值时失败关闭。
3. HTTP 中间件把 Bearer token 映射为组织 Principal，并按 admin/interviewer/reviewer 拦截；候选人 WebSocket 继续使用预约短期 token。
4. 所有 API 结果记录 route/method/status/actor，不记录 body；未认证请求也记审计，invitation/file/media URL token 先替换为 `{token}`。
5. 简历/音频 grant 只有分钟级，音频实际打开另记 `answer.audio.downloaded`；报告 CSV/JSON 导出记 `interview.report.exported`。
6. invitation 与签名文件路由按 IP/操作组限流；生产缺少 Redis 或 Redis 故障时失败关闭，429/503 也写脱敏审计。

### DATA-001 环境验收清单

`DATA-001` 只有完成以下真实数据库测试才可从 `in_progress` 改为 `verified`：

本机隔离 PostgreSQL 16 已完成 migration owner/runtime role 分离、RLS 跨租户拒绝、事务回滚、CAS、Outbox 幂等、预约唯一约束、结构化题库过滤和索引 `EXPLAIN (ANALYZE, BUFFERS)`；以下清单仍需在目标生产集群按实际角色、参数和负载复验。

- 在非表 owner 应用角色上运行 migration，并验证 `FORCE RLS` 对跨租户读写均拒绝。
- 运行 Memory/SQLite 同一套 Persistence contract，再对 PostgreSQL 跑事务回滚、CAS、Outbox 租约/幂等和结构化题库过滤。
- 并发执行两个 appointment start，证明数据库唯一约束只保留一个 InterviewSession。
- 对结构化题库查询执行 `EXPLAIN (ANALYZE, BUFFERS)`，确认租户/岗位/题库/readiness 条件在数据库执行。
- 验证 provider secrets 物理列只有密文，审计/调用日志不含候选人正文或 token。

## 完成与关闭规则

每个问题修复后必须同时完成：代码、数据迁移、自动化测试、对应接口/领域/进度文档更新。兼容代码仍存在时状态最多为 `verified`；只有旧 representation、旧路由或旧输入完全删除后才改为 `closed`。
