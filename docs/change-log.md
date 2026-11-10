# 操作变更日志

本文件记录所有会修改仓库内容的工作项。只读检查不单独登记；同一目标下的代码、测试、迁移和文档修改合并为一个工作项。状态使用 `in_progress`、`verified`、`failed` 或 `cancelled`，历史条目只追加或补充结果，不删除。

## 2026-11-10 · SPEECH-HANDOFF-AND-FAREWELL-092

- 状态：`verified（仓库全量与本机加载；真人即时回复和长场次待验）`。
- 授权与基线：用户报告主动退出时结束语被截断，以及补充提示后立即回答丢失首字；沿用本会话已授权的排查修复、Git回退点与验证后空闲加载。首次修改前工作区干净，基线`1c80cc7`已提交，091及更早提交保持。已按顺序复核入口文档，本项覆盖代码、测试、文档和经核验的本机加载。
- 现场证据：`iv_c2e73a9cde80427e`的结束音频为10240ms；08:32:05Z进入closing，08:32:15Z因farewell_timeout关闭。现有watchdog把音频时长加余量强制截为最多10000ms，早于音频本身结束。补充确认在实际播放停止后仍保持speaking并等待STT快照/重开；LiveKit ingress此时仅保留200ms供打断使用、普通完成会清空，存在立即回复丢前缀窗口。具体现场首字是否采集/识别丢失仍需证据核对，不将代码缺陷等同于音频逐字证明。
- 修复边界与验证：以实际播放完成回执为正常结束依据，音频时长和有限启动/排空余量决定失联兜底，保留失权/断线/旧回执保护。补充交接仅缓存真实播放结束后的当前capture音频，冻结提示前转写后顺序补送，禁止将面试官回声或旧来源当确认。先复现长结束语及慢STT切流丢帧，再验证正常补充、独立退出确认、打断、取消、失权和缓冲上限。保留历史业务数据和原始音频；完成后补实际文件/命令/结果及未完成事项。
- 阶段证据：告别首次夹具直接调用内部展示触发终态回收竞态，9项setup失败；改用真实正式收尾入口后有效红测试7失败/2通过，初修9通过。独立审查追加失权/控制连接变化先2失败、异常极晚口型先1失败，改为可信素材跨度加固定余量且到期重验原权限后12项通过。收音首批复现1失败/7通过（回执后音频没有进入采集），初修与旧确认/游标组合35通过；仍在补慢事件投影、VAD并发、取消和溢出反例，不将阶段通过当最终验收。
- 发布准备：/private/tmp的092 runner适配现API20027/worker20028和运行摘要f999885d21eab059624b7aeab35a88874ae4170f1aeb52c706916a10d0c3387e，独立保留090前端证明来源，19组隔离检查通过。只读preflight成功、services_changed=false，77场核心摘要1debb66b16d690782a83fe58df0cead39d28873cdddd505e55c2ff6bfc2fbd5e；媒体配置一致，活动房间/参与者/egress/owner/命令/后台任务/采集均0。尚未执行重启，正式加载仍须新完整测试证明和即时空闲核验。
- 独立审查补边界：可信播放ACK之后、停止/floor事件投影之前即同步开启交接；回执前已经被LiveKit接收但在队列中迟到的帧不得混入回复，因此纳入adapter只读接收sequence水位。取消若仍为同一有效capture，先撤销送达/确认许可，再有界顺序保存PCM；失权不能借用新capture，无法保存必须关闭输入并提示恢复。告别最终8文件169通过；交接仍在补真实channel慢投影与迟到队列测试，未运行最终全量。
- 交接审查复现：原取消分支确会静默丢弃FIFO且保留delivered，已改为保留同来源PCM并撤销权限；只在handoff期间过滤旧帧仍会让快切流后的迟到回声通过，已改为同ingress/capture水位持续到序号越界。两个独立红例均复验通过。中间真实通道组合73通过/1失败/5个未await警告，原因是取消清理改为异步后AgentChannel调用未加await；已补这一接线，失败日志保留，不算最终验收。
- 最终定向与冻结：新告别13项、新交接22项，后者含真实LiveKit adapter队列→AgentChannel→ContinuousSTT录音/转写的正常、慢投影和取消路径。告别相关8文件169通过/27.83秒；交接相关9文件125通过/18.45秒、无警告；独立复审5文件76通过/9.72秒、无阻断。`git diff --check`及273个本地文档链接通过。已冻结601份源码/测试文件，235份后端摘要a722a679284c4454278994ea231793798b2f3e7488b217cfcedef3518a83fd81；完整回归正在执行，证据保存/private/tmp/interviewer-092-backend-full.log与tested-source.json，服务尚未切换。
- 首次全量失败：5 failed /4737 passed /8 skipped /267.33秒，未生成通过证明、未发布。5例均来自流式输出共用测试替身：fixture把ManagedLiveKitEvidenceSession类型替换成SimpleNamespace，却没有新增begin/cancel接口，可信drain后AttributeError使测试发言权没有回候选人。独立核实生产唯一attach路径始终构造真实Managed类，不存在缺hook旧实现。仅补两处输出专用fixture的no-op begin和async cancel，不放宽生产接口或旧断言，另覆盖实际Managed无确认端点的正常drain。首次日志与601文件快照复制保留为/private/tmp/interviewer-092-first-backend-full.log及first-tested-source.json；随后重新冻结并跑完整回归。
- 替身修正复验：首次定向命令拼错测试文件名，exit4/no tests ran，无数据副作用；正确四文件组合97 passed /4.76秒，独立重复97 passed /4.72秒、无警告。生产摘要保持a722a679284c4454278994ea231793798b2f3e7488b217cfcedef3518a83fd81，仅test_streaming_expression_lifecycle.py初始化和farewell092新增真实对象回归不同。重新冻结601文件，重跑完整回归；新告别测试现14项、交接22项。
- 最终完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q`为4743 passed /8 skipped /268.75秒、exit0。601文件集合/内容全部保持冻结值，235份后端仍为a722a679284c4454278994ea231793798b2f3e7488b217cfcedef3518a83fd81；生成/private/tmp/interviewer-092-test-attestation.json（v2）。前端源码与63构建文件逐hash保持090版本，复用552项前端证明，不称本轮重跑。无Prompt/供应商协议/模型配置修改，未发送候选材料作外部探针；下一步保存修复提交并即时空闲加载。
- Git与首次加载保护：修复提交078eade，原1c80cc7保持，未push。首次execute初检通过（77场核心摘要保持、所有活动计数0），再次worker_idle检查被GateError拒绝；services_changed=false、new_pids为空，没有停服务或改业务数据。未记录到具体子检查失败，不能断言是哪项任务。随后独立只读ping/active/reserved/scheduled/stats逐项核对：单一worker20028、pong、三个任务计数均0；按原规则重新执行全部门禁，不增加任务豁免或删除失败记录。
- 实际加载：第二次全部门禁通过，正常TERM原API20027/worker20028，2026-11-10T09:17:11Z启动API26537、worker/Beat26538（本地11月10日01:17）。两者235份后端SHA均为a722a679284c4454278994ea231793798b2f3e7488b217cfcedef3518a83fd81，与完整证明一致；媒体配置摘要dcbaf54ac496b9217094dae97dfcd6b2ffec665d23f934bfaae2895c5ecefb0f保持、local ready且无issues。runner退出0，77场核心摘要1debb66b16d690782a83fe58df0cead39d28873cdddd505e55c2ff6bfc2fbd5e保持。healthz/readyz、5174页面及代理均200；代理实际返回API26537与新摘要。前端未改构建或重启。
- 实际文件与剩余事项：生产改interview_agent.py、livekit_evidence_ingress.py、spoken_supplement.py及LiveKit adapter只读sequence属性；新增farewell092十四例、reply_handoff092二十二例，并更新流式输出替身；同步架构/API/092设计/已知问题/进度/路线图/本日志。无新公共字段、持久表/DDL、Prompt/Schema或模型路由；未重放真实回答、改历史评分或改写原音频。服务端收到可信ACK之前的网络窗口保持原回声/真实打断边界，模型识别准确率和真人首音节逐字归因不由本次合成测试证明；真人立即回复及长会话继续待验，不标closed。

## 2026-11-09 · RECEPTION-FAILURE-RETRY-091

- 状态：`verified（仓库全量、本机加载与同场录像恢复；真人长场次及批量STT配置待验）`。
- 授权与基线：用户要求继续处理新截图中的回答重试故障，沿用已授权的修复、先保存Git基线及验证后空闲加载。工作区干净，基线`4702859`，090及更早提交保留；本项覆盖代码、测试、文档及本机加载。已按原顺序复核相关设计文档，保留086独立退出确认与078/087证据和预算边界。
- 现场证据：`iv_541249dca9b747c9`于06:26:48Z启动conversation_reception.v10，4.004秒后provider_timeout；代码把异常缓存成other并推进已读转写。06:27:40Z的evidence.retry_processing确已applied，但没有再次调用轻量接话，只重做完整答案分析；后者先超时，06:28:18Z返回后因unresolved_answer_contract_invalid（$.intent）被拒绝。未保存原模型响应，不能断言具体伴随字段错误。会话已于07:14:04Z因appointment_window_expired结束，0答案/0评分；不恢复终态、补交回答或伪造退出确认。
- 修复边界：区分有效other与识别失败，失败不得消耗已处理发言游标；同一当前服务端来源只做有限自动恢复，显式重试能重做仍未处理的识别。保留成功结果缓存、已送达历史、当前来源/owner/capture及退出二次确认，不用关键词替代语义、不增加完整分析额度、不放宽响应合同。正常技术回答与供应商持续故障均须有界。
- 验证计划：先合成复现超时后退出识别丢失及显式重试无效，再覆盖自动恢复、重复/VAD/metadata不续购、改口/旧来源/失权、成功other不重复、无回答伪造及原退出整链。完成相关与全量回归后，检查空闲和源码身份再加载；不对已结束现场重放语音。实际修改、失败与结果随后补记。
- 同场附带发现：07:14:05Z录像worker已成功封存76319467字节并记录完整哈希，07:14:07Z较晚返回的StopEgress错误又被_mark_failed覆盖为failed。纳入本项的窄修复是保护已封存终态免受迟到停止错误覆盖，保留真实失败和完整性校验；先合成并发复现。另有断线批量转写未配置真实stt.batch路由而落mock后被安全拒绝，这是后于截图发生的独立配置缺口；不伪装为最初重试根因，不擅自改用streaming型号作为batch模型。
- 阶段验证：接话新回归先6失败/1通过，修后相关285通过、真实Evidence退出整链组合40通过；媒体并发先6失败/5通过，修后相关47通过并经独立审查21通过。仅修改spoken_supplement/media_capture与新增两份测试；前端、Prompt、模型路由不改。使用现有模型路由、内存审计的三条虚构语句全部一次通过：退出2448ms、否认退出1804ms、普通技术回答1678ms；没有发送候选材料或写真实业务数据。
- 审查与中断：独立审查另复现同文字两次VAD取消会耗尽接话次数却未留下可显式恢复标记，正在补边界。已正常INT本轮pytest18888（不操作API/worker）；首次全量为2304通过/1失败/4跳过/136.13秒，失败为公司问答集成用例，另有明确KeyboardInterrupt，因此不算验收。日志和首次599文件冻结保留为interviewer-091-backend-interrupted.log/source-first.json；修正后重新完整验证，不修改原安全断言。
- 运行与数据恢复计划：091发布runner只准备并完成18组隔离验证，保留API/worker配置，不再调整090媒体开关；尚未重启。同场录像状态如需纠正，只能在新ListEgress COMPLETE、精确绑定、76319467字节及原SHA全部验真后调用既有finalizer，保留失败审计；所有面试/回答/评分/报告和其他录像必须保持。此项不重试已结束的语音回答或复活预约。
- 取消边界收尾：新增3例先2失败/1通过，修后8文件156通过；计费启动即保留待完成来源，取消不退款、失权不可续、改口不复活旧退出。独立复审无阻断。公司问答首轮异常确认由人工INT打断MemoryPersistence提交的clear/update造成，独立整文件12通过，未修改业务或旧断言。重新冻结599文件，235份后端摘要f999885d21eab059624b7aeab35a88874ae4170f1aeb52c706916a10d0c3387e；最终全量日志interviewer-091-backend-full-final.log。
- 发布只读预检：原API10055/worker10056身份及090摘要匹配，媒体配置摘要dcbaf54ac496b9217094dae97dfcd6b2ffec665d23f934bfaae2895c5ecefb0f保持且ready；LiveKit活动egress/面试房间/参与者均0，owner/命令/后台任务/活动capture均0。76场核心摘要fed250c643cc21f5d036f61a4edbb7e749090f6feb982cf2dbc13a1c18fe58e4；尚未停止或替换服务。269个变更文档本地链接检查通过。
- 最终完整回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q`为4707 passed /8 skipped /266.56秒，退出0；599份源码/测试集合及hash均与冻结值一致。前端与63构建文件逐一确认等同090已验收版本，复用其552项前端通过证据，不称本轮重跑。091-test-attestation.json记录本轮后端结果及独立的旧前端证明hash。新增恢复脚本19项隔离测试通过；首次fixture缺时间字段导致12 setup errors，仅测试夹具修正，无真实库副作用。准备保存实现提交后空闲加载。
- Git与实际加载：实现提交3cb5643，基线4702859保留，未push。2026-11-10T08:08:50Z正常TERM原API10055/worker10056并启动API20027、worker/Beat20028；两者235份后端摘要均为f999885d21eab059624b7aeab35a88874ae4170f1aeb52c706916a10d0c3387e，与全量冻结证明一致，原媒体配置摘要保持，runner退出0。76场核心摘要保持，health/ready及5174页面和代理均200，代理已确认新PID/源码；前端未重启或改构建。
- 录像恢复前失败留痕：两次只读baseline在fresh_provider_and_object_proof被GateError拒绝，write_attempted=false，未修改目标capture。分段复核表明Egress COMPLETE、绑定、大小、独立SHA和保护全部通过；失败是核验自身的GET /admin/media-runtime新增正常api.request审计，使过严的全审计相等断言失效，非录像损坏。只读诊断也正常产生访问审计，未删除这些记录。正在将三个精确只读诊断端点的GET/200审计纳入白名单，旧审计和所有业务记录仍须逐项保持；不豁免未知或写入类审计。
- 同场录像实际恢复：恢复工具补充严格只读GET审计扩展后32项隔离测试通过，包含旧审计删除/改写及非白名单负例；首次foreign fixture被tenant guard拒绝，修正测试的创建组织后通过。真实baseline和execute均退出0：指定capture恢复completed，76319467字节，独立SHA与原封存SHA均eb88b8d2124b6144c955e23f57a5929740a5fc3ec20bf87db84a9c234bf0083a；全部其他文档（含完整面试）、Outbox、模型调用和文件字节/hash/mtime保持。原审计保留，仅新增1条封存审计及5条明确只读核验审计。结果保存interviewer-091-recording-recovery-result.json；5174实际review API返回recording.available=true、status/processing_status=completed、failure_reason=null、can_retry=false。
- 实际文件与剩余事项：生产仅spoken_supplement.py、media_capture.py；新增test_reception_retry_091.py（16项）与test_media_terminal_race_091.py（12项），同步架构/API/091设计/已知问题/进度/路线图/本日志。无新公共字段、Prompt/Schema、模型配置、数据库迁移或前端变更。恢复工具只在/private/tmp、无凭据持久化；既有finalizer仍按原设计保存标准provider元数据。本次没有复活已预约到期的会话或重放回答；断线批量转写真实路由缺口与真人长期供应商表现仍待后续，不标closed。

## 2026-11-09 · ASSESSMENT-SCOPE-AND-MEDIA-RECOVERY-090

- 状态：`verified（仓库全量、本机加载与指定录像恢复；真人/生产待验）`。
- 用户授权：修复上一轮已查清的考察范围/题数/总分不一致、API与worker媒体配置漂移、录像失败误显示处理中及笼统结束原因；允许完成验证后恢复指定旧场次录像封存。修复前工作区干净，Git基线`9f41d4c`，保留`ef5d277`和`4639d2b`；代码、测试、配置、构建、文档、运行加载与指定封存恢复均归本项。
- 只读事实：`iv_a304dabcf1a243c7`实际45分51秒，触发时间上限；14主题+2追问均已评分，但旧41必需能力仅覆盖24，旧最低28主题未满足，完整总分为空。当前089新合同虽限制8–10主题，仍把41能力全设required，内存复现最低完整覆盖仍28，因此必须在面试前冻结可行的本场范围，不能事后删掉缺失维度凑总分。
- 录像事实：API99185启用本地媒体且ready，worker99186缺该开关、默认OSS且4项配置缺失，`LIVEKIT_RECORDING_NOT_READY`在ListEgress前抛出；指定capture的1,051,629,048字节本地mp4存在，原egress已结束，封存重试失败。UI只看stopping，未投影实际失败。此前089仅保存该异常，未恢复封存。
- 实施边界：新计划预先冻结8–10主题可完成的考察范围和权重、验证时间/题数可行性；既有终态合同/回答/评分/报告保持，不用改写历史或零分填补未考察。结束原因细分并保留旧兼容；配置缺失明确归类，API/worker同配置检查；实际恢复仅作用于指定录像封存工作，不触发重新评分或补交回答。遵守既有组织、来源、存储保护、真实哈希与幂等门禁。
- 验收计划：合成大题池的范围可行性、完整10主题可出范围明确总分、缺关键证据/提前退出不伪造完整分、追问不独立计权与旧合同不变；媒体配置差异、可恢复/不可恢复失败、真实临时文件哈希及安全回放；前后端合同与全量回归。空闲加载后核对源码及配置身份，再恢复指定录像并比对答案/评分/报告摘要。首个日志补丁因定位文本错误未写入，随后在正确标题前登记；后续失败与结果持续保留。

- 阶段进展：v2本场范围合同与严格校验、选题双围栏、完成原因事实、录像错误分类和配置身份、前端范围/失败展示均已实现。相关回归通过；独立审查发现提前预约截止仍显示完整时长、自动退避误显示失败、永久死信误称重试耗尽三处，已修并补负例。新增测试中一次foreign-capture断言误放入另一fixture导致1失败，移回正确测试后18项通过。README首个补丁因完整段落不匹配失败无写入，随后精确替换成功。未对真实业务或服务执行写入；全量及加载另补。

- 扩展边界与审查：新范围在预约创建/改期、readiness和最终START事务核验可完成时间；追问为剩余主问题留时，min3简历配额适配3、不可行策略提前拒绝；候选时间不足明确改期而非等待。真实prepare入口默认阈值由1改2并补端到端合成证据，最终相关21文件304项通过。前端新范围/故障/入场提示共552项通过，构建成功（原大bundle提示保持）；报告JSON/CSV都携带范围，28项相关回归通过。新增模板文档补丁另两次因整段匹配失败均无写入。
- 发布准备：090 runner复用089全部空闲/身份/正常TERM/TTY恢复围栏，仅显式development统一LOCAL_MEDIA开关，再比较API/worker安全配置摘要；17项隔离检查通过，0真实环境读取/网络/信号。随后只读真实预检通过：worker/media/owner/非终态work皆0，75场核心摘要仍为3c84f7f19b9f5c2bfb364338d953a33f2721ea8b75344425577f51973b4f347b；两份修正媒体配置摘要dcbaf54ac496b9217094dae97dfcd6b2ffec665d23f934bfaae2895c5ecefb0f一致且ready，尚未重启。原环境只保存在内存未输出。
- 冻结：597份源码/测试文件已冻结到/private/tmp/interviewer-090-tested-source.json。第一轮全量在补边界期间执行，仅用于发现兼容失败，不作为最终冻结证明；最终全量与加载随后记录。指定恢复baseline已保存75场完整文档、评分Outbox及其他capture摘要，原文件1051629048字节/mtime保持；当前16份回答、18个评分历史版本、1份报告，封存work8次失败dead_letter。实际恢复尚未执行。

- 首轮完整回归：4670 passed /1 failed /8 skipped /276.42秒（interviewer-090-backend-first-full.log）。唯一失败为旧企业E2E题库fixture固定循环两题，而两题估时7+8分钟占满15分钟、无告别预留，新范围合法仅冻结1题；正在改为足量合成材料，保留真实准入/STT/评分/HTTP报告且加强双能力最低证据与本场总分断言，不放宽生产时间门禁。尚未发布。

- 最终完整回归：修正旧E2E后33项通过并重冻597文件；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q`得到4679 passed /8 skipped /267.13秒，日志interviewer-090-backend-full-final.log。前端最终26文件552项通过与临时构建成功；源码集合/hash不变，401本地文档链接和diff检查通过。静态前端按先不可变bundle、后原子index发布并保留旧bundle；090-test-attestation绑定已测源码/测试及构建集合，实际API/worker加载进行中。恢复脚本追加预POST报告重放拒绝、组织/唯一capture/幂等键绑定，10项隔离检查通过；现场基线复核仍完全一致，未发恢复请求。

- 实际加载：2026-11-10T03:23:27Z（本地11月9日19:23）正常TERM旧API99185/worker99186后启动API10055、worker/Beat10056，无强杀、无恢复分支，runner退出0。235份后端源码SHA=144b9b7e2b4fb0abbca832dd68e7c893851d0ef6b89222593cf1cd67e1d223c6，与597文件测试证明一致；63构建文件受证明校验。API/worker媒体摘要均dcbaf54ac496b9217094dae97dfcd6b2ffec665d23f934bfaae2895c5ecefb0f，local/ready/noissues。健康、就绪、媒体空闲及75场核心摘要全部通过。Vite进程未重启，5174新源码及代理已读取新后端，8000静态index与bundle HEAD核验通过。
- 指定录像恢复：既有processing/retry API仅重放work_c5bd61cf32cf407b封存，answer/report work_count=0。capture与work均completed，1051629048字节，真实存储保护local_private_development；独立流式SHA与封存SHA均81da8dd6fcc5033738b6b9504d0032d320283d3b0a850ced599daa59109798ce，签名Range返回206且1024字节与原文件相同。75场完整面试文档、全部评分/报告Outbox、其他capture及原文件大小/mtime均与修前baseline完全相同；目标16份回答、18个评分历史版本、1份报告保留。旧overall_score仍为空且14主题+2追问、45分51秒时间结束事实正确投影，没有事后重配权重。安全结果保存在/private/tmp/interviewer-090-recording-recovery-result.json，不保存签名回放URL。
- 实际文件与边界：新增media_runtime、local_runtime、session_assessment_scope、interview_completion和5份090测试；修改自适应合同/准入/生命周期、计划/预约/追问/监督器目录、媒体/复核/报告服务、system/Celery诊断，前端计划/报告/入场提示与测试、三份旧后端fixture、local-media脚本及README/CONTEXT和对应设计文档。无Prompt/provider协议、DDL或历史合同迁移；旧v1继续兼容，新的考察范围需重新准备计划才生效。时间预算是事前估计，真人长场次及外部供应商长期稳定性仍需验收；不标closed。

- Git收尾：实现、测试、构建及文档已提交cb850bb，基线9f41d4c保留，未push。最后文档排版脚本首次按空段落查找失败（StopIteration、无写入），改为按完整行匹配后成功；只调整090验收段位置及补充089死信已在090恢复的说明，不删除历史记录。

## 2026-11-08 · INTERVIEW-FLOW-CONTINUITY-089

- 状态：`verified（仓库全量与本机加载；真人长场次待验）`。
- 用户要求：查明截图“下一话题暂时没准备好”的重试原因，恢复面试结束礼貌用语，将岗位题库与简历题库合计控制为8–10道主题，追问另计。开始时工作区干净，已提交的`ef5d277`作为本轮修复前基线，保留此前`4639d2b`与所有业务历史；相关源码、测试、前端和文档修改均归本项。
- 只读现场：截图精确场次为`iv_a304dabcf1a243c7`，不是最初按截图辨识的省略字符串。07:10:45Z第9道主题答案已提交，07:10:59Z下一题规划调用出现`provider_timeout`（14203ms、单次尝试）；07:11:48Z重试规划成功，评分独立成功，未见本次回答丢失。计划9道岗位原题与3道简历原题拆成56个单元，41项能力强制覆盖需要28单元，代码将请求9题扩大为28–28；实际14道主题加2追问、约46分钟后因时间预算结束。
- 结束语证据：07:26:30Z结束候选输入后，`_plan_next`先发带completion的snapshot，再进入告别；前端收回执立即关闭，07:26:31Z媒体断开，未创建closing act。另需覆盖评分提前完成时终态清理只等待presentation task、漏掉planning task的竞态。
- 实施计划：在现有Supervisor seam增加仅限可恢复模型选题故障的有界连续性处理，复用已批准候选和最终事务围栏；正常结束先完成礼貌告别的送达/有界排空再发最终完成回执，刷新和断线仍能正确终态恢复；统一各入口总主题上限，禁止覆盖要求自动扩容，追问独立计数、未考察能力明确证据不足。同步接口/领域/检索评分及相关文档，先补故障复现与负例，再执行相关、完整后端与前端回归。
- 操作边界：真实场次、录音、回答与冻结历史合同保持；不自动重放、重试、补交或重新评分截图场次。任何运行加载须在验证通过后复核媒体/owner/worker空闲并沿用安全配置；实际文件、失败、验证、加载与未完成事项结束时补记。


- 实施与阶段验证：Supervisor可恢复四类结构化供应商故障或本次模型await超时后，重算预算，按完整批准目录现有排序提出合法单元；保留原15秒/3次、最终事务围栏与安全诊断收据。正常告别新增仅连接级closing投影和独立准备任务，收音/动作关闭而播放保留；终态清理等待实际准备，旧owner/controller晚返拒绝。统一primary_question_budget，新增冻结抽样覆盖策略`sampled_answered_roots.v1`，历史执行覆盖不重写hash；v2预算跳过报告未考察，不零分、不重配原权重。针对回退28项、预算138项、告别后端53项/前端211项分别通过，红测试据保留。前端完整529项通过，临时目录构建通过（既有大chunk提示）；完整后端仍在执行。
- 首轮发布预检：媒体room/participant/active egress、owner、Evidence命令及非终态Outbox均0；因截图场次旧capture仍stopping而安全阻断，未停服。只读核实精确egress `EG_wVWgzaD9wTvb`已EGRESS_COMPLETE，文件封存任务8次失败后dead_letter；录制结束不等于封存成功。本轮不重放死信或改写capture。发布脚本仅对该精确capture/session/egress/deadletter绑定，在场次report_ready且输入已封闭、LiveKit按ID确认终态及全部活动为0时作不活动分类，其余记录仍阻断。
- 发布准备：原088 runner复制为`/private/tmp/interviewer-089-release.py`，限定原API86662/worker86663及原运行摘要，配置仅内存、TTY恢复、正常TERM与无重复启动保持。本轮无Prompt/provider协议改动，发布证据改为完整后端/前端/构建与全文件集合hash，而非复用088真实模型证明；停服前、启动前、恢复及最终验收再次核验。11项隔离检查通过，无真实进程信号或环境读取。只读SQL初查使用不存在的kind列失败后改为JSON字段；一次定向测试命令引用不存在文件，随后纠正；均无业务副作用。

- 首轮全量失败如实保留：588文件冻结后后端`4585 passed /32 failed /8 skipped /280.38秒`，日志`/private/tmp/interviewer-089-backend-full.log`，冻结证据另存`interviewer-089-first-tested-source.json`。没有发布。9项是旧v2测试仍把question_count当“岗位数另加简历”的合同迁移；其余大部分发现pending题遗留started_at被误计作已问题，正在修预算计数并覆盖v2混合旧字段；另3项为轻量channel替身缺新投影接口及告别独立任务的测试时间预算seam，需要保留原权限/排空断言并更新测试。不得用删除简历/下一题断言掩盖失败。

- 全量失败修复：pending蓝图无论残留started_at都不计作已呈现；v2预算只取固定根题，不读取遗留自适应字段。补2项公共生命周期回归，9文件106项通过；两简历fixture仅改合计请求数1→4/1→2，保留原音频、评分、重试和下一题断言。远端轻量channel替身明确无告别权限，旧超时测试同时限定真实独立TTS准备期限，相关6文件153通过/1既有跳过。再次独立审查无阻断，588文件重冻后启动第二轮全量。

- 最终冻结与验收：第二轮`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q`为`4619 passed /8 skipped /261.88秒`、退出0，日志`/private/tmp/interviewer-089-backend-full-final.log`；588文件集合与内容hash保持。前端`npm test -- --reporter=dot`为26文件529项通过，`npm run build -- --outDir /private/tmp/interviewer-089-web-dist`通过；未改变前端源码后不重复验收。构建按先不可变bundle、后原子index发布，保留全部旧bundle；61构建文件集合/内容受发布证明校验。后端231文件SHA=`d0a7f7769506bea87ad9f55cd4363f5e7b7e291d3ee4162c8ab342966a64806f`。新证明`/private/tmp/interviewer-089-test-attestation.json`绑定完整回归、构建、源码集合及原Prompt/provider无变更。340本地文档链接和diff --check通过，实际加载进行中。

- 实际加载：2026-11-09T08:05:31Z（本地11月9日00:05）空闲窗口正常TERM原API86662/worker86663，启动API99185、worker/Beat99186，无强杀、无恢复分支、runner退出0。两个进程均返回231份源码及已测SHA `d0a7f7769506bea87ad9f55cd4363f5e7b7e291d3ee4162c8ab342966a64806f`，health/ready通过、worker/媒体均空闲。75份面试核心摘要前后同为`3c84f7f19b9f5c2bfb364338d953a33f2721ea8b75344425577f51973b4f347b`；例外仅精确历史未封存capture，不伪造成功。运行日志为`/private/tmp/interviewer-089-{api,worker}.log`。
- 前端实际生效：8000静态HTML与已构建index字节一致；5174开发页面确认新的closing逻辑与合计10题限制，健康代理及runtime-release端点200并指向同一新源码摘要。前端Vite进程继续使用既有开发热更新，未声称其PID已重启；生产式静态入口已经发布新bundle，旧bundle保留。
- 实际文件：新增`app/domain/primary_question_budget.py`；修改`adaptive_interview.py`、`interview_lifecycle.py`、`scoring_quality.py`、`schemas/api.py`，服务`interviewer_supervisor/{context,service,tools}.py`、`interviews.py`、`interview_agent.py`、`livekit_evidence_ingress.py`、`plan_assembly.py`、`reports.py`；前端candidate、agent-event-runtime、plans及对应测试、构建入口与2个新bundle。新增4个089后端测试文件，修改4个旧测试fixture/seam。同步CONTEXT、架构、接口、领域、检索评分、供应商、数据库、已知问题、开发进度、路线图及本日志。没有DDL、Prompt、供应商路由、STT或旧答案/评分改写。
- 交付边界：588文件及61构建文件再次核对通过，保存独立Git修复提交，不push、不重写历史。已解决本次三条路径；真人语音长场次、真实网络故障与外部供应商长期表现仍待复试。历史录音`media_capture_5da560601220413b`录制已结束，但文件封存死信仍未处理；保留原记录与失败，不重放真实业务。源码回退可用`ef5d277`及更早`4639d2b`，代码回退不等于数据库回滚。

## 2026-11-08 · INTERVIEW-CONTINUITY-RELEASE-088

- 状态：`verified（仓库全量、真实模型虚构样例与本机加载；真人长场次待验）`。
- 授权与顺序：用户要求先将当前代码提交为 Git 回退基线，再开始面试连续性修复。先完整保留当前 081–087 源码、测试和文档，不修改业务实现；本条登记后执行基线提交，提交号及结果随后补记。提交仅保存代码，不包含被忽略的数据库、录音、凭据和运行日志，也不等同于正在运行的 085 服务版本。
- 现场依据：`iv_61e524acabdf44bb` 第三交流的临时一字识别假设取消 4.199 秒完整分析，假设随后撤回；重算剩余 8.922 秒而超时，恢复被 `call_budget_exhausted` 拒绝。运行日志仍为旧取消行为及 reception v9；仓库 087 已修、086/087 尚未加载。本轮只读复验 087 两份测试为 25 passed/1.04 秒。
- 第一批范围：保存可回退基线，复核既有 086/087 修复及源码一致性，补齐实际运行版本核验和必要验收；只有确认媒体/证据/后台任务空闲且具备允许的启动配置后才更新运行服务。保持原始语音、评分、供应商、时间/调用额度和最终提交门禁。延期分析推进与进程交流检查点属后续独立交付，不混入本次发布。
- 操作约束与验收：不自动恢复、重试、提交或重放用户真实场次，不读取或输出未授权的进程环境/凭据，不在活动面试中重启。完成相关及全量回归、独立审查、运行状态核验后补记实际文件、命令、结果与未完成项；失败和审批限制如实保留。
- 基线已完成：`4639d2b307c2c3223e876c802a60ba6e50b8c5e6`（`dev`）保存 107 个文件，提交后工作区干净；随后创建 `codex/interview-continuity-088` 修复分支。路径与凭据模式检查无命中，`git diff --check`通过。首轮 Git 写入被沙箱拒绝（index.lock），未修改索引；按用户提交授权升级后提交成功，没有 push、reset 或历史重写。
- 运行核验方案：保留 `/healthz` 与 readiness 语义；新增管理员只读运行身份端点，以及同源的 worker inspect 身份。源码摘要只在进程引导时冻结，查询不重新读取磁盘，不包含环境、凭据、候选资料或源码正文；发布时对照已测试 manifest。身份只是启动源码快照，仍要求发布期间源码不变，不能宣称是任意动态模块的内存证明。
- 阶段实施与验证：新增独立运行身份 module、离线 manifest CLI、管理员只读端点、Celery inspect及启动安全日志；首轮针对性 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider tests/test_runtime_release_088.py tests/test_runtime_release_api_088.py tests/test_auth_audit.py tests/test_api_responses.py tests/test_deployment_readiness.py tests/test_provisional_preparation_087.py tests/test_provisional_ingress_087.py tests/test_conversation_control_086.py tests/test_terminal_cleanup_086.py -q` 为103 passed/5.90秒，日志`/private/tmp/interviewer-088-targeted.log`。独立审查要求补API日志handler异常不阻断启动，以及真实新解释器导入API/worker后的冻结摘要对照，正在补齐后再冻结全量；未改变业务模型或会话状态。
- 只读运行预检：旧API20700/worker20701仍在线，health/ready为200，唯一worker active/reserved/scheduled=0，LiveKit rooms/participants/active egress=0，数据库有效owner和非终态命令/Outbox=0。截图场次已由原运行流程进入report_ready，本项未操作其生命周期。旧重载脚本和源码验收临时文件已不存在，不能假设Git基线087就是正在运行085的精确快照；原配置安全复用和最终加载仍待完成。
- 最终源码与回归：独立审查的两项补充已完成，API日志handler异常红绿验证及真实新解释器API/worker导入对照覆盖在088共24项测试中。冻结487份app/tests/main Python文件到`/private/tmp/interviewer-088-tested-source.json`后，完整`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q`为`4565 passed /8 skipped /260.10秒`、退出码0，日志`/private/tmp/interviewer-088-backend-full.log`。冻结后所有文件内容一致；后端230份源码摘要为`4bf99b0f8240174607abb1b1e7230effca2f385fea800b5dfdf5f25d9c745d3e`。8项为既有环境跳过；前端未改，不重复构建。
- 发布预检失败与修正：本轮明确限定两旧PID、仅内存保留原配置的工具升级已获批。临时脚本最初遇到Darwin环境终止符/解释器名称检查不匹配，均在停止服务之前退出；修正解析边界与允许的解释器名称后继续。随后媒体门禁发现一个0参与者、0录制房间；核对现有readiness实现证明是`/readyz`自行创建并离开的`readiness-evidence-ingress`。只排除该精确名称且无人房间，所有其他房间、任何参与者和录制继续阻断，不删除媒体房间。修正后只读预检通过：真实房间/参与者/录制、worker三种任务计数、有效owner、未完成证据命令和Outbox均为0，74份面试核心文档摘要`c2beb8bd2b310051dacaa7ea1f9c4c1897157ea4da13fb056052c5511b95ff55`。以上预检未重启服务或提交答案。
- 探针审查：真实模型探针尚未执行时，独立审查指出直接`get_store()`会让虚构样例写真实调用审计/熔断状态；正在改为只读加载最小供应商配置后以内存存储运行，避免探针影响现网熔断。旧脚本未运行，未发生该副作用。
- 探针隔离与首轮结果：临时脚本已改为SQLite只读查询唯一`interview_turn_understanding`真实路由及所需配置/连接/密封secret，加载到InMemoryStore，拒绝缺失路由或mock；所有调用审计/熔断仅在内存。合成SQLite验证源库字节摘要不变。首轮8条虚构语句7通过/1语义预期不匹配（15.977秒），未生成发布通过证明、未停止服务；失败为“否认退出并继续回答”，首轮未保留具体返回kind，不能反推该次值。一次单例诊断返回`interview_dialogue/confidence=0.95/1989ms`，中央合同允许此类面试交流，运行时回复后返回listening，不进入退出确认。将该探针允许值校正为`continue|interview_dialogue`、增加正常意图confidence>=0.75及固定kind/confidence记录后重新验收；不修改生产Prompt、预算、重试或源码。首次详情保留`/private/tmp/interviewer-088-model-smoke-details.json`。
- 最终真实模型虚构验收：8/8通过、14.586秒，正常意图置信度0.95–1.0，“嗯”保持uncertain且置信度0.3；结果为`/private/tmp/interviewer-088-model-smoke-final-details.json`，源码绑定证明为`interviewer-088-model-smoke-attestation.json`。保持现有4秒接话与8秒退出分类预算、原路由/fallback，未重放真实候选材料。含首轮及单例诊断共17个虚构请求，失败历史保留；面试核心摘要不变，真实调用/熔断状态无探针写入。
- 切换恢复保障：独立审查发现临时发布脚本在停止旧服务后若退出，会丢失唯一内存启动配置；已在实际停止前修正为TTY持有配置，异常只接受`status/resume`。续接仅允许已测SHA及本runner已知进程，空闲和数据校验继续有效，不重复启动已有新服务、不重复TERM、不强杀，重试日志另建0600文件。10项隔离检查通过，脚本`/private/tmp/interviewer-088-release.py`、检查`interviewer-088-recovery-isolation-check.py`；未向磁盘序列化环境或凭据。旧服务没有发布准入锁，因此在重复空闲检查及短暂切换期间仍需保持无人新入场。
- 实际加载：执行`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-088-release.py --execute --expected-sha256 4bf99b0f8240174607abb1b1e7230effca2f385fea800b5dfdf5f25d9c745d3e`（TTY），重复空闲/源码/身份/数据检查后正常TERM旧API20700与worker20701及等待其子进程退出，无强杀、无恢复分支。新API86662和worker/Beat86663于2026-11-09T04:12:49Z（UTC）启动；管理员端点及worker inspect均返回230文件、同一已测摘要，角色/PID正确。health/ready为200且ready=true，worker active/reserved/scheduled及媒体活动为0，74份面试核心摘要与前值一致，脚本exit0。运行日志`/private/tmp/interviewer-088-{api,worker}.log`均含新身份且无ERROR/Traceback；独立检查8000健康、5174页面/健康代理/版本端点全部200，前端代理显示新API86662及同摘要。
- 实际文件：新增`app/core/runtime_release.py`、`app/operations/runtime_release.py`、`tests/test_runtime_release_088.py`、`tests/test_runtime_release_api_088.py`；修改`app/main.py`、`app/api/routers/system.py`、`app/workers/celery_app.py`。文档修改为接口、架构、已知问题、开发进度、路线图、本日志及086/087说明。无前端、业务Prompt/Schema、供应商路由、数据库结构或评分规则修改；086/087既有代码保留在修复前基线并通过本次实际加载生效。
- 交付与边界：487份Python文件集合及内容与全量验收冻结值一致，258个修改文档的本地链接有效，`git diff --check`通过。保留基线`4639d2b`与独立修复分支，修复另行提交，不push、不重写历史、不回滚业务数据。核心摘要排除的仅是根级version/updated_at/heartbeats/last_activity_at/agent_events/agent_runtime/lifecycle_events，不声称所有数据库字节不变；探针和发布均未重放、重试、恢复或提交用户场次。第一批已完成；真人长场次、真实麦克风/网络故障验证、模型不可用时延期分析推进、完整进程交流检查点与生产部署验收仍待后续，不能据本次通过承诺任意故障都不中断。

## 2026-11-03 · PROVISIONAL-TRANSCRIPT-PREPARATION-087

- 状态：`verified（仓库全量与本机加载；真人待验）`。后续088于2026-11-09T04:12:49Z完成空闲加载和源码身份核验；以下保留087实施当时的审批限制与验证历史。
- 目标与依据：用户要求解决临时转写增加一个字后又撤回却取消完整分析的问题。现场`iv_284b5421f1304100`第6交流09:15:54Z的非final一字假设触发transcript取消，7.952秒分析丢失；09:15:55Z该假设撤回，正式重算仅剩8.596秒而超时，恢复被两次调用额度拒绝。修复临时假设与权威证据变化的生命周期区分；沿用已读11份设计文档与模块边界，保留081–086增量及Git基线。
- 实施边界：临时输入立即撤销旧等待者及推进/确认权限，但纯分析可在原截止时间内保留；只有重新取得完整且匹配的权威final、来源/语境/owner/capture复核后才能复用。真实内容改变、失权、关闭及模式改变仍不得使用旧结果。不得以文本相同代替完整证据身份，不退款、不增加模型次数/超时，不修改ASR、Prompt、评分或供应商配置。
- 验证与运行：先用纯合成的临时增加→撤回和真实新增对照复现，再覆盖未决假设期间禁止提交、来源失效、预算与有界清理，进行相关和完整回归。真实库只读、不重放或提交现场材料；服务环境读取许可仍未收到，本轮不绕过审批读取环境或重启。独立worker评分超时不纳入此窄修复。实际文件、失败、结果及未完成事项随后补记。
- 初步实现：生产仅改`app/services/answer_endpoint.py`与`app/core/model_diagnostics.py`。字幕不再取消owned准备或选择新预算key；完整final与无未决尾巴的稳定preview共享精确身份核对，差异取消来源为白名单stable_evidence。旧等待者、确认、纠错推进资格仍即时撤销；完整来源、语境、owner/capture与事务门禁保持。
- 阶段验证：新增`tests/test_provisional_preparation_087.py`改前15失败，修改后扩展16通过；红绿日志provisional-complete-red.log/provisional-green-second.log保留。相关首轮187通过/4失败，四项旧测试把仅文字通知视为确定内容变化；在三份旧测试文件里明确提供新权威final或推进实际snapshot时机后，保留取消/唯一提交/旧结果拒绝断言，相关第二轮219通过/7.43秒。诊断去敏与取消、087组合48通过/1.00秒，覆盖新固定source允许、任意source文字丢弃。相对086仅两份生产源码变化，独立只读审查未发现权限或提交阻断；真实识别适配器集成与剩余验收另补。
- 整链与边界验证：新增`tests/test_provisional_ingress_087.py`7项，采用真实DashScope→ValidatedSTTStream→ContinuousSTT→Endpoint和合成消息/模型替身；formal/speculative撤回均复用一次调用，真正新final拒绝旧结果，退出确认不因撤回恢复。零PCM可能使旧preview看似稳定，但真实task-finished ACK前绝不提交；未撤回partial在ACK后进入新final并重新分析。首个零PCMfixture对preview稳定性的预期不成立，保留最终取证门禁验证并补这两个反例，没有改ASR。该文件与相关4文件101通过/3.72秒。
- 扩大检查与修正：额外相关首轮110通过/8失败（`/private/tmp/interviewer-087-fences-related.log`）。其中5项旧预期在`test_preparation_failure_budget.py`、`test_confirmed_preparation_liveness.py`、`test_parallel_conversation_052.py`中改为partial保留、权威不同final才取消，安全断言保留。另3项`test_recovery_episode_066.py`暴露实际兼容回归：无轻量接话能力时，处理耗尽后无法再核验真正新回答；没有改这3项断言。修正`spoken_supplement.py`为只在新文字提示和原静音门槛后核验一次完整final，麦克风噪声不得反复切流，只有新final可选择新预算key，连续失败次数保持。新增边界先1失败后扩展两种semantic模式通过；最终8文件105通过/8.22秒，日志`interviewer-087-fences-final.log`，独立审查无阻断。
- 冻结与全量：最终相对086仅`app/core/model_diagnostics.py`、`app/services/answer_endpoint.py`、`app/services/spoken_supplement.py`三份生产文件变化，9份测试变化（含2份新增），无删除旧源码。483份app/tests/main Python文件冻结至`/private/tmp/interviewer-087-tested-source-sha256.json`；正在运行`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`，日志`/private/tmp/interviewer-087-backend-full.log`。此后只补文档，不改待验收源码。以上失败均发生于合成测试，未读启动环境、外呼真实模型、重启或修改业务记录。
- 首轮完整结果：`4540 passed /1 failed /8 skipped /265.64秒`，唯一失败是`test_automatic_turn_integration.py::test_new_voice_cancels_held_preparation_without_closing_capture_or_losing_tail`在活动音频仅产生partial时就等待纯任务取消，合成时钟尚未进入静音取证。独立单项同样复现，确认是旧取消时机预期；不能通过提前取消生产任务迁就它。保留首轮日志`interviewer-087-backend-first.log`与首轮源码摘要`interviewer-087-first-tested-source-sha256.json`。仅调整该测试为活动语音不取消、不seal/提交，静音后真实snapshot证明新增内容再取消，并保留原完整PCM和唯一答案断言；生产源码不改，定向通过后重新冻结并验收。
- 最终重新验收：上述自动提交整链文件2通过/2.08秒，日志`interviewer-087-automatic-integration-green.log`。483份重新冻结源码与首轮相比仅该测试文件不同，三份生产代码完全一致；相对086现在10份测试变化（含2份新增）。第二轮同命令完整后端正在运行，未在运行中修改源码，也未加载服务。
- 最终完整结果：`4541 passed /8 skipped /260.79秒`、退出码0，日志`/private/tmp/interviewer-087-backend-full.log`；8项为既有环境跳过。25项新增087测试覆盖临时撤回、真实新增、来源/语境/owner/capture门禁、原期限、调用数、真实协议ACK和兼容取证；诊断参数化额外覆盖来源白名单。未改前端，不重复构建。最终483份Python源码与冻结摘要、文档链接和diff检查结果保存`/private/tmp/interviewer-087-verification.json`。
- 交付与未完成事项：本轮完成上述三份生产文件和十份测试文件，新增[087设计](provisional-transcript-preparation-087.md)并同步架构、供应商、已知问题、进度、路线图和本日志。未提交或推送Git，基线和081–086增量保留；未改变真实场次/语音/模型配置，未读取启动环境或重启API/worker。086与087仍未加载；此前读取含凭据启动环境的自动审批拒绝仍待用户明确许可。加载和真人响应表现未验收，不标closed；若撤销仅撤087增量并保留早期工作与业务记录。

## 2026-11-02 · CONVERSATION-CONTROL-AUTONOMY-086

- 状态：`verified（仓库全量、真实模型虚构样例与本机加载；真人待验）`。后续088完成8例真实模型虚构验收并于2026-11-09T04:12:49Z空闲加载；以下保留086实施当时的审批限制与验证历史。
- 授权与目标：用户要求交流更自主、不要因非预期发言僵硬失败；以当前纯退出请求被完整技术答案合同阻断为入口，将来源有效的交流控制与技术内容分析解耦，沿用已有等待、解释、重读等接话能力。已按序复核设计文档并沿用codebase-design技能；保留Git基线`6e6daed`与081–085增量，ASR、评分、原始转写及真实供应商配置不改。
- 现场：`iv_0c8e968bd904440e`的当前退出原话与STT最终文本SHA完全一致。预分析2.757秒后因新临时转写取消且占用一次调用；正式分析5.782秒正常返回却选用不自洽的`answer_unresolved`组合，被`unresolved_answer_contract_invalid`拒绝，剩余7.681秒时两次实际调用已耗尽。worker同期正常。无原始响应，不断言具体哪一伴随字段有误，也不把Prompt表述风险当作已证实因果。
- 实施与验证边界：复用轻量接话识别当前控制请求，完整Schema、原文来源及当前final/owner/capture验证后只允许发起独立退出确认，不授予退出或提交许可；取消/改口/引用/否定/假设及混合技术内容必须保留原门禁。补合同、真实入口/临时SQLite、失败和调用次数回归，先复现再实施；正常回答不增加串行模型调用。真实DB只读，不重放候选材料、不自动重试或提交现场场次。实际修改、测试、失败和加载结果随后补记。

- 实际实现：独立确认后的退出也脱离完整技术分析，使用PreparedCandidateExit和完整Evidence事务；当前未完成题保存exit_evidence，报告新增unassessed_retained_evidence、Retention删除覆盖。已先更新接口和领域文档，再实现内部提交接口，无新增外部客户端写权限、无DDL。
- 阶段测试：新runtime红16/绿7→23绿，审查补充故障后停止切流回归后24绿；接话合同红44/绿1→45绿，相关16文件367绿；新临时SQLite24项及旧提交/报告组合48绿，旧runtime六文件106绿，真实媒体/LiveKit3项绿。日志均保留/private/tmp/interviewer-086-*。SQLite首轮12项失败为同步测试缺event loop，改测试async后通过；整链首轮失败是终态事件名/故意失权的teardown断言，均保留。未加载中间版本。
- 审查修复：退出分类两次失败后虽然不再外呼但仍高频切STT快照，已按当前revision停止切流，新输入/显式重试才能续接；未返还答案分析额度。另复核原文撤回反例与真实媒体提交围栏。
- 自动审批限制：真实模型合成探针及原配置加载前需要读取API/worker启动环境，工具审查拒绝，指出环境含凭据且缺明确授权。整条命令未执行，无环境读取/模型外呼/服务修改；已说明原因并提出明确选择，继续不依赖该许可的本地实现与测试。

- 首轮全量进行中发现旧整场退出测试仍期待创建当前答案，正在保留核心断言并按独立未评估留存迁移。同时发现真实终态回收竞态：无新评分时直接进入report_generating，journal裸异步stop可抢在告别送达前释放owner，清理失败还可能泄露异常消息。此项不能通过改旧期待掩盖，正在统一终态回收调度并补无pending评分的延迟播完/超时/清理失败回归；首次全量及只读诊断日志保留。

- 首轮完整结果：冻结480份源码/测试，`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`得到4480 passed /12 failed /8 skipped /261.55秒。保留first-backend-full.log与first-source-sha256.json。9项失败是旧退出应创建答案或pending评分的测试前提，按未评估证据保留且不放宽PCM/送达/唯一seal门禁；其中原退出终态清理错误另证实真实竞态。两项runtime预算回归发现无reception能力或只有麦克风噪音仍切final，已改仅有轻量能力且新稳定证据才检查：旧068+新086组合33绿。旧退出4文件与真实Evidence文件组合73绿。终态回收实际修复及第二轮全量另补。

- 终态竞态首轮修复及核心回归45通过（terminal-race-green-third.log），覆盖有/无存量pending评分、drain/timeout、清理失败不重复receipt和无controller异常去敏。control-step独立复查补同文字由unstable变stable仍可触发、噪音不反复切流，068+086共34通过。第二轮481份源码全量运行到828通过/2跳过/52.83秒，独立审查发现owner失效但provider抑制取消时仍可能无界等待，于是仅正常Ctrl-C中止该pytest，不操作API/worker；interrupted-backend-full.log及源码摘要保留。补本地有界释放且不借successor权限，再验收；未将中断算通过。

- 终态清理收尾：统一有界等待、取消与本地释放，修复失权自清理以及已发送完成通知后channel waiter仍等待旧provider任务的两处死等；保留告别实际播完/超时流程，不允许旧owner或旧controller写入新会话，也不重复发送完成通知。扩大验证时5条旧投影测试因正常完成task引用被过早清空失败，已收紧为只分离仍抑制取消的未完成任务，原投影测试未改。最终终态与投影组合68 passed/25.12秒，控制与预算组合35 passed/3.03秒，两组独立审查无剩余阻断；日志receipt-cleanup-bounded-final.log与control-budget-final.log。退出回复额度按确认问题和原始final绑定，只有metadata变化不能续购调用。
- 最终回归开始：重新冻结481份app/tests/main Python源码至`/private/tmp/interviewer-086-tested-source-sha256.json`，再次执行完整后端，日志`/private/tmp/interviewer-086-backend-full.log`。此阶段仅文档继续补记；未发真实模型请求、未读取进程环境、未重启API/worker。

- 最终完整回归：同命令得到`4515 passed /8 skipped /254.87秒`、退出码0；481份源码集合及内容与测试前冻结摘要完全一致，323个本地文档链接有效，`git diff --check`通过。前端未改，本轮未重复前端测试或构建；本地语义替身与故障注入不等于真实模型或真人响应时间验收。最终源码一致性及文档检查结果保存到`/private/tmp/interviewer-086-verification.json`。
- 实际修改范围：本项生产改动涉及`app/core/prompt/{candidate_exit,conversation_reception}.py`、gateway/mock版本注册，以及`app/services/{answer_endpoint,candidate_exit_confirmation,interview_agent,interview_evidence,interviews,livekit_evidence_ingress,reports,retention,spoken_supplement,streaming_stt}.py`；新增五个086测试文件并适配旧退出与v10合同测试。新增086设计说明，同步架构、接口、领域、评分、供应商、CONTEXT、问题、进度、路线图和本日志。没有新增公共候选写接口、数据库迁移或ASR识别算法调整。
- 剩余事项与副作用：自动审批要求的环境读取授权尚未收到，因此未做真实模型探针、未加载API/worker；运行中的版本仍是此前版本。真实场次未重放、补写、恢复、重试或提交，现有Git基线与081–085工作区增量保留，未新增commit/push。后续需授权后完成真实模型虚构样例、空闲预检、原配置重启及健康验证；若回退，仅撤086增量并保留历史日志与业务数据，不能覆盖前序修复。

## 2026-10-27 · FOLLOWUP-CLARIFICATION-085

- 状态：`verified（仓库全量、真实模型合成与本机加载；真人及生产待验）`。
- 授权与约束：用户明确要求修复追问澄清后失败并重启服务；沿用已按序阅读的11份项目文档，保留基线`6e6daed`及081–084全部增量。语音识别、原始转写、评分标准和供应商配置保持；正常成功路径不增加模型调用，单次18秒、初始20秒、恢复总40秒、同来源两次完整调用上限保持。
- 现场证据：`iv_6ce6c03808984157`的追问针对“问推理结果”发出澄清，13:09:27Z送达；解释后两次完整模型调用6.391秒/6.143秒均成功返回，但均被`unresolved_clarification_source_invalid`拒绝，尚余7.112秒时被投影为服务不可用。原模型响应未保存，不推测具体错误字段。上一场`iv_d4a3cfcb93b449df`则为首次字段冲突后内部纠错仅剩7.368秒被20秒总期限取消，随后虽开启40秒恢复窗口却已耗尽两次调用。
- 实施范围：把已送达澄清来源与回复边界由服务端稳定绑定，完整合同保留证据、真实新完成语及送达门禁；补齐版本化针对性纠错和最小疑点交流规则，区分理解不充分与真正无法理解；统一有界纠错续接预算，区分模型结果拒绝与供应商不可用。新增合同、真实入口/临时SQLite及恢复回归，合成材料验证，不重放真实候选回答。
- 验证与加载：先复现再修复，完成相关及全量检查、前端构建与源码冻结后正常重启API/worker；加载前核验实际服务和活动会话，保留原环境、日志及业务记录，不自动提交/重试/恢复现场场次。实际文件、命令、失败、结果与未完成边界结束时补充。

- 修前复现与初步验证：固定澄清来源新合同回归先15失败/5通过（`/private/tmp/interviewer-085-source-red.log`）；真实临时SQLite新链先3失败/2通过，已送达/owner失效门禁保持拒绝。前端源码未改，现有完整回归`npm test -- --reporter=dot`为521通过/26文件/2.67秒（`interviewer-085-frontend.log`）。只读运行预检首次仍有有效owner，随后自然释放；最新唯一worker空闲、LiveKit0参会者、有效owner0，正式加载前会复核。此时尚未重启。

- 真实模型首轮失败留痕：6类纯虚构材料全部在约4.7–6.8秒返回且无合同错误，但仅2/6满足语义预期：两条清楚但不充分的回答被误澄清、未解决旧疑点时丢既有claim、新疑点误选旧C1。4个失败例再次诊断均复现，确认模型把知识机制缺失当含义不明，并将已消除的旧focus用于新歧义；日志`interviewer-085-model-probe.log`、`interviewer-085-diagnostic-probe.log`及结果均保留。仅只读加载模型配置、隔离内存审计，没有真实候选内容/业务写入。未加载此中间版本；正在按阶段重排判断指引，不能把Schema通过当语义验收。前端临时构建通过，原有大bundle提示保持。


- 第二轮与传输对照：重排阶段指引后4例只有保留旧claim的1例通过，日志`interviewer-085-phase2-probe.log`，仍未加载。对相同虚构材料仅合并全部system指令为连续首条后，两个清楚但不充分的回答转为正常answer、未解决旧焦点保留claim，3/4通过；新疑点仍错误引用旧C1。日志`interviewer-085-single-system-probe.log`。确认需要修复指令排列并继续核查结构化来源分支，不能继续仅追加Prompt；所有对照仍在内存、无候选数据或服务重启。


- 后续真实模型证据：反转target的anyOf分支、给C1附带固定focus两次内存对照都未修复新疑点误选旧来源，未采用这两项无效变更（`interviewer-085-reversed-union-probe.log`、`interviewer-085-visible-focus-probe.log`）。单system布局和C1描述自洽检查落盘后，按实际optional上下文的v120六例5/6通过；新疑点第二次仍把“旧词已解释但新词未知”并在一段歧义描述中而错误结束，证明仅要求描述提及旧focus不足（`interviewer-085-final-probe.log`，原结果保留）。继续验证先判明确澄清状态再选intent的wire合同，禁止扩展关键词猜测或程序替换模型焦点；此时尚未重启。


- 首轮全量：476份应用/测试/main源码冻结，`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`得到`4373 passed /7 failed /8 skipped /248.04s`。保留`interviewer-085-first-backend-full.log`与首轮哈希；4项旧阶段布局断言已按单system中的严格顺序适配并整文件76绿，另外3项正在核对布局及末次诊断原因。未把失败标为完成、尚未加载。


- 首轮全量失败收尾：其余3项核实为旧测试替身/期待：059组合指令按分散message定位、optional_followup包装函数没透传新增clarification_context、079把真实末次合同原因仍期待为额度耗尽。只适配四个旧测试文件，保留单次/两次调用上限、后续追问隔离、日志去敏和严格顺序等安全断言；后三文件40通过，066文件76通过。生产不因这些失败改动。字段前置、独立clarification_resolution两个内存实验仍失败（日志evidence-first/resolution-first及纯合成raw诊断均保留）；正在将澄清关系与来源合为严格判别联合对象，防止状态与焦点各自合法却组合矛盾。


- 联合来源与控制根因：将resolution和来源置于同一个判别联合target后，实际模型三例的来源全部正确；旧词已解释/仍不明各一次约6.4秒通过。新疑点首条raw已给new_focus_unresolved+新E/新focus、completion_basis=null和clarify，却因服务端仅凭finish_answer派生next再次冲突；第二次才错凑待核实结束。日志`interviewer-085-joint-object-network-probe.log`，纯合成raw留作证据。正在接入严格联合target和“新疑点尚未澄清只撤销提交权限”的中央投影；不猜换焦点、不添加完成依据，已有依据必须验证后才可撤销。此前联合探针沙箱无网络得到provider_unavailable，保留日志，不当模型结论；经工具审查准许的真实网络探针无真实业务写入。


- 最终生产候选与真实模型：联合target三态严格解析后，新scope只撤销完成权限并保留原文/主张/完成话轮意图；撤销前验证原完成依据，raw E发生位置必须晚于已送达澄清及当前speech边界，包含旧/新同文结束语回归。后置歧义策略保留pause/repeat/等待/独立退出优先。删除中间无效的“歧义描述包含旧词”门禁，不以关键词猜测新旧关系。独立审查未发现阻断。
- 实际v120六类虚构追问最终全部通过：清楚但不充分、简短回答、解释成功、同一旧疑点仍不明、仍在思考、旧词已解但新词未知；全部一次调用，4.846–6.610秒，日志`interviewer-085-accepted-probe.log`及结果JSON。新疑点保留2项claim并明确clarify、无problem。228份应用/main源码已冻结到`interviewer-085-accepted-probe-source-sha256.json`，真实配置只读、审计隔离内存。样例不保证任意自然语言识别或长期延迟；最终全量与实际加载结果另补。
- 真实临时SQLite最终14项通过（`interviewer-085-joint-sqlite-final.log`）：simple/composite新疑点只澄清不提交、旧/新可靠主张保留、旧basis及同文旧E拒绝、owner撤销，以及等待/退出/暂停/重读。原联合结构未实现时7失败/3通过红测保留。安全绑定和真正provider故障的恢复接口测试继续保留。


- 最终合同的旧fixture迁移：source54项和SQLite14项通过，但联合回归发现066旧成功替身仍输出旧target，缺少resolution（202通过/9失败）。刚启动的全量在11.07秒又复现072两条同类旧wire（248通过/2失败），因此通过该pytest会话Ctrl-C正常中止，未操作API/worker。中止日志/源码摘要保留`interviewer-085-interrupted-*`。正在定向迁移全部content_clarification成功替身；负例和真实语义/唯一提交/送达断言不放宽。生产及已通过的真实模型验收源码仍冻结，全部替身迁移完成再重跑全量。


- 旧测试全部收尾：066语义恢复文件76项通过；涉及content_clarification的另外10文件262项通过（`interviewer-085-legacy-source-green.log`），仅迁移三个文件的有效成功wire，新增严格断言请求Schema确实提供same分支。所有负例与原有行为门禁保持。统一源合同54项通过；冻结476份应用/测试/main源码后已重新启动完整后端同命令，最终结果另补。
- 实际生产文件（相对084冻结）：`app/core/answer_processing_budget.py`，`app/core/prompt/{contracts,semantic_turn,understanding_references,validation}.py`，`app/domain/interview_agent.py`，`app/model_gateway/gateway.py`，`app/providers/mock/provider.py`，`app/services/{answer_endpoint,conversation_understanding}.py`。理解118–125、组合149–156、semantic_turn23/24登记保留历史版本；Provider真实协议、ASR及原始转写、前端和评分标准未改。新增四个085测试文件；旧合同版本/单system布局/成功wire/错误类别测试按新合同适配，保留原有安全与业务断言。同步架构、接口、领域、供应商、CONTEXT、问题、进度、路线图与085专项说明。

- 第二次完整回归与最后fixture：全量得到`4389 passed /1 failed /8 skipped /251.22s`，唯一失败为068旧成功替身在已送达澄清上下文仍返回null target；按严格联合Schema改为`resolution=resolved`，保留原无重复澄清、新完成语、独立补充确认和真实提交断言，整文件`6 passed /3.97s`（`interviewer-085-off-topic-integration-green.log`）。失败全量及摘要保留为`interviewer-085-second-{backend-full.log,tested-source-sha256.json}`。228份已通过真实模型验收的应用源码保持一致，重新冻结476份源码/测试并启动最终完整回归，尚未重启服务。

- 最终全量通过：同命令最终`4390 passed /8 skipped /249.23s`、exit0（`interviewer-085-backend-full.log`），476份源码/测试集合及摘要完全匹配，228份真实模型验收的生产源码也保持一致；验收记录`interviewer-085-verification.json`。前端521项及临时目录构建已通过；8项为原有环境跳过。独立只读审查无阻断；10份相关文档297个本地链接与`git diff --check`通过。新一轮加载预检确认唯一worker空闲、媒体0参与者、有效owner0，API健康就绪。接下来按用户授权重启API/worker并记录实际结果；不自动重试或提交真实场次。

- 实际加载完成：2026-10-29T04:52Z（本地10月28日21:52）再次确认无媒体参与者、有效owner0、唯一worker空闲；保留各自原环境，正常TERM旧worker91720及12个相关进程、旧API91719及2个相关进程，无强杀。新API20700、worker/Beat20701启动并一次探测通过：health/ready均200、ready=true、前端5174页面与代理200、worker唯一pong且active/reserved/scheduled均0。日志确认API startup complete、worker ready，无ERROR或Traceback。
- 数据与运行证据：71份面试/答案/评分业务文档的核心逐字段摘要前后完全一致；仅排除根级`version/updated_at/heartbeats/last_activity_at/agent_events/agent_runtime/lifecycle_events`，不声称整文档无变化。媒体最终1空房间/0参与者，未自动重试、恢复、提交或重放现场回答。脚本/结果/逐字段摘要保存在`/private/tmp/interviewer-085-reload.py`、`interviewer-085-reload.log`、`interviewer-085-reload-result.json`、`interviewer-085-core-{before,after}-fields.json`；运行日志为`interviewer-085-{api,worker}.log`。旧日志及所有中间失败记录保留。
- 交付与边界：085已加载，ASR、原始转写、评分规则与供应商配置未改；正常首次成功不增加调用，两次实际调用及18/20/40秒限时保持。真人追问复试、供应商长期稳定性及8项外部环境验收待完成，不标closed，不承诺任意自然语言或固定端到端速度。Git基线仍为`6e6daed`，081–085增量保留工作区，未另提交或push。若回退只撤085增量并于空闲时重新加载，保留此前修复和业务数据。

## 2026-10-27 · SUPPLEMENT-DIALOGUE-ROUTING-084

- 状态：`verified（仓库全量、真实模型合成样例与本机加载；真人及生产待验）`。
- 授权与约束：用户要求从根源处理补充确认后的任意反问、跑题和不确定回复，明确先不改语音识别，沿用响应速度要求。按顺序复核11份项目文档与已使用的codebase-design技能，先登记再编辑；保留基线`6e6daed`和081–083全部增量。不得改写候选转写、扩展自动提交权限或靠延长18/20/40秒及增加完整调用次数绕过问题。
- 现场依据：`iv_abdc524d35de41a9`补充问题08:30:27Z已送达；接话3.237秒未选中、补充分类2.238秒合法返回unclear，完整理解仅剩14.195秒后超时；第二次11.199秒返回被`completion_basis_required`拒绝后两次额度耗尽，剩8.641秒。恢复后同revision/同回复哈希又重复补充分类。083已生效，但本次首失败是timeout、第二次才有格式原因，不能套用预分析校验失败接续的既有验收。worker无关联任务/异常；不从未保存的模型响应猜测具体误分类。
- 根因范围：补充交流路由与累计答案理解耦合，合法unclear落入昂贵全量分析；真实阶段已询问补充但未确认结束时，未确认Prompt仍固定声明尚未询问。计划用当前新回复和真实服务端语境统一有界交流决策，非完成的提问/澄清/跑题保持对话，只有需要正式结束/题目控制的请求进入原有提交校验；恢复接回已有准备不重复分类。统一Prompt/Schema必须集中、版本化、校验后使用，原owner/capture/送达/final/退出确认及答案事务门禁保留。
- 验证与运行：补完整实时入口的红→绿复现，覆盖小调用计时、未知/非法输出、结束/新技术补充/反问/公司问题/跑题/退出/跳题、重复恢复、输入与owner变化、真实临时SQLite唯一提交。现有业务闭环全量与源码冻结后，按既有重启授权仅在零参会者/零有效owner/worker空闲时加载。真实库只读，不重放用户材料、不自动恢复/重试/提交该场；实际文件、失败、验证、加载及未完成边界结束时补齐。

- 实施中验证：新增真实补充阶段回归先19项失败，补齐投影、Prompt和指纹后已通过；确认内部asking→transcribing不应改变已送达事实并加入专门回归。root初轮路由18项中14通过/4失败，均为082旧测试仍期待多余reception调用，按新单次路由更新精确次数；首次接口拼接未完成时旧整链5失败/43通过，失败保留于`interviewer-084-routing-related.log`。未加载服务、未操作真实场次。
- 实际模型首轮：只读加载现有模型配置到隔离内存，对7句虚构话语调用v6；术语定义、一般词义、跑题、明确结束、退出及公司问题6项符合预期，耗时1.573–2.554秒。最近话语为术语解释时“嗯，好吧”被误判finish，证明仅把最近话语放入Prompt不足；此失败没有送达、提交或真实业务写入。保留`/private/tmp/interviewer-084-model-probe.log`及result.json，正在完善确认对象约束，不能将首轮6/7当验收通过。

- 独立整链对照与审查：使用保存的083原Spoken路由单独加载到临时测试进程，12条新增正常交流场景均红，表现为多余reception/full或错误路由；当前新路由通过，原文件从未覆盖回仓库。额外发现resume期间final已变但分类前仅冻结各自旧值、以及本地送达旗标过期时可让新未送达问题之后的finish进入confirmed准备，两处都有临时SQLite红复现；现中央chain要求起点final与识别器final完整一致、真实stage=awaiting_reply，并在等待后复核来源/owner/context。未绕过安全门禁。
- 模型与确认对象修正：中央上下文增加服务端reply_target与最近实际送达语句，明确历史补充问题不等于当前回应对象，孤立附和不得finish；无需新增关键词意图分类或额外网络调用。最终相同7类虚构发言全部通过；解释后附和为interview_dialogue，普通词义为question_clarification。第二轮日志`interviewer-084-model-probe-second.log`，多数约2秒，明确结束一项7.149秒；真实模型延迟有波动，不能用这批样例保证任意真人会话均在2秒。源配置只读加载，所有审计及熔断写入隔离内存，无真实业务写入。
- 阶段整合验证：`test_supplement_dialogue_084`、`test_supplement_reply_contract_084`、`test_supplement_phase_084`、真实spoken ingress及081/082相关组合`134 passed/11.42s`，日志`interviewer-084-final-related.log`；合同与历史兼容84项另绿，与前项重叠不累加。旧测试中metadata变更原设.9被真实保守聚合归一到相同值，改成.81并断言完整identity确实变化，保留“变化不可复用”断言；先前失败日志保留。正在完成最后旧fixture兼容及全量冻结，尚未加载084。

- 冻结与最终检查：相对083的467份源码，新增中央supplement_reply模块、supplement_context投影及3个084测试文件，共472份冻结到`/private/tmp/interviewer-084-tested-source-sha256.json`。084生产修改仅涉及Prompt/contracts/semantic、领域版本登记、gateway/mock版本登记、understanding/evidence/supervisor/interviews、spoken路由和ingress表达版本；ContinuousSTT/StreamingSTT、Provider实现、18/20/40秒与AnswerEndpoint源文件相对083未改变。059旧裸替身已换真实临时SQLite上下文并为异步等待加1秒上限，5项通过。独立审查未发现剩余安全阻断；不确定的低置信回复保留原固定补充澄清，不凭自然附和创建许可。
- 加载前只读预检：API health/ready=200且ready=true，唯一worker pong且active/reserved/scheduled均0，LiveKit一间空房/0参会者、有效未释放owner=0。日志`interviewer-084-preflight.log`。完整后端正以`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`运行，日志`interviewer-084-backend-full.log`；此时尚未重启，加载前还将复核空闲。

- 首次完整回归：472份冻结源码运行全量得到`4281 passed/26 failed/8 skipped/266.84s`，保留日志`interviewer-084-backend-first.log`及首轮摘要`interviewer-084-first-tested-source-sha256.json`，未加载服务。16项为072旧测试的当前semantic版本仍断言98–105/129–136；6项为059旧精确参数断言没有新增的完整final/boundary；4项为064真自动链的合成MockProvider仍输出v5三字段，缺v6必填reply_text，统一校验正确拒绝后测试等待答案超时。分别修正三个测试文件的版本、完整来源断言和finish合成响应字段，生产保持冻结；历史兼容、唯一答案/单seal及完成证据断言不删减。定向与完整第二轮结果另补。

- 最终仓库验收：三个旧测试文件各自32/10/8项通过；与首轮冻结比较，仅此三个测试改变，生产源码完全一致。第二轮完整同命令`4307 passed/8 skipped/247.74s`、exit0，日志`/private/tmp/interviewer-084-backend-full.log`。472份源码集合及内容与最终摘要完全相同，验收记录`interviewer-084-verification.json`；文档本地链接和`git diff --check`通过。前端未改，不重复构建。正在按既有授权再次空闲复核并正常重启API/worker，真实合成样例通过仍不代表真人或生产全部验收。

- 实际加载与复核：2026-10-27T09:29Z确认API91719、worker/Beat91720已运行；加载前再次media0参会者、有效owner0、唯一worker空闲，保留原进程环境，正常TERM旧83114及12个相关进程、旧83113及2个相关进程，无强杀。新API health/ready=200且ready=true、前端5174页面/代理200、唯一worker pong且active/reserved/scheduled=0、media0参会者；新API/worker日志ERROR/Traceback均0。worker启动首轮探测未就绪，第二次通过，未再次重启。69份面试/答案/评分核心字段摘要前后完全一致（仅排除既有运行事件、心跳和版本等易变根字段），没有自动恢复、重试、提交或改写用户场次。
- 最终证据与边界：运行日志`/private/tmp/interviewer-084-api.log`、`interviewer-084-worker.log`；加载脚本/结果/逐字段摘要`interviewer-084-reload.py`、`interviewer-084-reload-result.json`、`interviewer-084-core-{before,after}-fields.json`；失败首轮和原083日志均保留。源码没有改ASR/转写、模型路由、音色、评分规则或超时额度。新模型Prompt与小路由已加载；真人完整会话与外部服务长期可靠性仍待复试，不标closed。Git基线仍为用户先前要求保存的`6e6daed`，084及081–083增量保留在工作区，未额外提交或push；若需回退只撤084增量并保留历史记录及业务数据。

## 2026-10-26 · BOUNDED-CORRECTION-CONTINUATION-083

- 状态：`verified（仓库全量回归与本机加载；真人响应时间及外部供应商待验）`。
- 授权与目标：用户要求在保证响应速度的同时修复预分析到正式准备之间丢失纠错原因的问题。沿用已按顺序阅读的11份项目文档与codebase-design技能；保留Git基线`6e6daed`及081/082全部增量。先登记工作项，再实施和验证，不重放真实候选材料。
- 现场依据：`iv_e0f5eb6e754442fe`首题`turn_a55e887caf1a4c99`在2026-10-27T04:39:36Z和04:39:49Z两次模型请求成功（13.040秒、12.593秒），均被`completion_has_unresolved_ambiguity`拒绝；预分析局部一次额度阻止纠错、原错误被预算错误覆盖，正式请求又重建原合同，两次完整请求哈希相同。最终共享额度耗尽时仍剩14.962秒，尚无答案/评分/outbox。本场未进入082补充回复分支。两个独立内存合成复现当前与旧基线相同行为，单次服务内部的纠错对照可成功；具体实际疑点未被日志保存，不作推断。
- 范围与速度约束：在统一准备/预算seam内保留有精确请求及语境约束的安全纠错原因，使剩余一次真实调用执行已有版本化纠错合同；维持预分析最多一次、同文本共享两次完整调用、18/20/40秒及实际提交门禁，不通过延长限时、增加调用或跳过校验换成功。正常路径不得新增请求、等待或持久化I/O；过期、输入/语境/模式变化不可误用纠错状态。
- 验证计划：先写端点→真实理解服务→合成网关的失败复现，验证第二次实际带纠错指令及有界时延；覆盖两种理解合同、纠错再失败、证据/语境/确认模式变化、取消/owner/capture失效、真实临时SQLite与唯一提交。相关回归及完整后端后再核对冻结源码；仅在参会者为0且worker空闲时按既有授权加载，运行、失败和未完成结果结束时补齐。
- 实际实现：仅在082冻结源码上修改`app/core/{answer_processing_budget,model_diagnostics}.py`及`app/services/{answer_endpoint,conversation_understanding,interview_evidence,interviews}.py`六文件。现有共享额度保留一次性固定纠错原因，实际capture/owner、完整STT/语境/模式与原请求摘要须匹配；成功扣次后领取，晚返旧请求沿首次generation拒绝内部重试，不退款。新增安全`model.correction_applied`事件；保留原Prompt版本/内容、Provider配置、外部接口、Schema、数据库、评分和18/20/40秒限制。普通首次请求不增加数据库守卫读取；原因保留不使用独立的20秒失效期，实际外呼仍受当时剩余期限约束。
- 定向验证：新增`tests/test_correction_{budget,continuation,continuation_sqlite}_083.py`共43项。独立完整链路改前先7失败/4通过，扩展后9失败/9通过；SQLite独立改前4失败/8通过，失败均指向第二次未收到纠错。修复后三个新文件全绿，最后追加晚返旧任务generation回归后与相关14文件共`278 passed/3.53s`，日志`/private/tmp/interviewer-083-related.log`。独立组合117通过及审查30通过与上述重叠，不累加。合成网关只有实际收到既有纠错指令和固定原因才返回合法内容；两种合同均验证真实临时SQLite唯一提交、失败无第三次、正常一次、第一拒绝后第二调用在0.5秒内接续且不等一秒退避、当前到期禁止外呼及来源失效。全部使用合成材料。
- 审查与冻结：审查补齐两项既有领域原因白名单、跨预算/重复领取拒绝、confirmation语境仅存摘要、抗取消晚返代次保护；无未解决阻断。与082的464份源码逐一比较，除上述六生产文件及三新增测试外完全相同，081/082增量保持。467份Python源码/测试冻结至`/private/tmp/interviewer-083-tested-source-sha256.json`；`git diff --check`通过，完整后端正在运行。只读空闲预检health/ready均200且ready=true、唯一worker pong且active/reserved/scheduled均0、媒体房间0/参会者0，记录`/private/tmp/interviewer-083-preflight.log`；加载时必须再查。
- 首次完整回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`为`4201 passed/2 failed/8 skipped/247.55s`，未加载服务。两失败均为082补充顺序测试同一时间断言的两个参数，执行顺序、唯一提交、单次分析和共同deadline断言已通过；实际`257.798447375 - 237.798447375`产生浮点舍入而不严格等于20。生产仍直接设置`deadline = started_at + 20`且源码未改变。保留失败日志为`/private/tmp/interviewer-083-backend-first.log`与初次源码摘要为`interviewer-083-first-tested-source-sha256.json`；将测试改为严格比较`deadline == started_at + 20`，不放宽时限、调用次数或业务校验，定向再验证后重跑全量。所有失败均为合成测试，无运行服务或真实数据副作用。
- 首轮修正与速度证据：独立审查确认差值误差分别为±2.842e-14秒，仅修改`tests/test_supplement_reply_order_082.py`一处绝对deadline比较；该整文件与三个083新文件共`47 passed/1.56s`（`interviewer-083-final-target.log`）。467份最终源码摘要相对首轮只有该测试变化，生产文件完全一致，完整第二轮进行中。另以148643字节纯合成中央合同请求测量新请求摘要及额度记录，100次预热后1000次P50=0.311ms/P95=0.344ms，未发网络请求；结果`/private/tmp/interviewer-083-local-overhead.json`只代表这段本地运算，不包含业务语境、STT、数据库、真实模型或端到端时延。初次临时测量脚本误从包根导入`prompt_contract`而ImportError，未运行测量或产生副作用；改用实际contracts模块后完成。
- 最终仓库验收：完整第二轮同命令得到`4203 passed/8 skipped/246.53s`、exit0，日志`/private/tmp/interviewer-083-backend-full.log`。8项仍为既有环境跳过；467份Python源码/测试的集合及内容与最终冻结摘要完全一致，验收记录`/private/tmp/interviewer-083-verification.json`。文档本地链接检查及`git diff --check`通过；未改前端，不重复前端构建。正在按既有重启授权再次核验零参会者、零未释放有效owner与worker空闲后加载，实际运行结果随后补记。
- 实际加载：2026-10-27T05:12Z再次确认媒体0参会者、有效未释放owner=0、唯一worker空闲，保留各自原环境并正常TERM旧worker75028及其12个相关进程、API75027及其2个相关进程，无强杀。新API83113、worker/Beat83114于05:12:50Z复核：health/ready=200且ready=true、前端5174页面/代理200、唯一worker pong且active/reserved/scheduled均0、媒体仍0参会者；两份新日志无ERROR或Traceback。启动后首次worker探测未确认就绪（细节受抑制、未定位具体原因），第二次探测通过，未二次重启；保留此探测失败。执行记录`/private/tmp/interviewer-083-reload.log`、结果`interviewer-083-reload-result.json`、运行日志`interviewer-083-{api,worker}.log`。
- 数据与交付边界：加载前后68份业务文档的逐字段核心摘要相同，排除的仅为根级`version/updated_at/heartbeats/last_activity_at/agent_events/agent_runtime/lifecycle_events`，字段摘要保存在`interviewer-083-core-{before,after}-fields.json`，不声称整文档完全不变。截图场次仍in_progress、首题asking、0答案，owner已由原流程释放；本轮未点击重试、恢复或提交真实场次，未重放候选材料。467份运行源码再次核对一致，Git基线`6e6daed`及081/082增量均保留，本次未另提交或push。新增[083说明](bounded-correction-continuation-083.md)并同步架构、领域、供应商、CONTEXT、已知问题、开发进度、路线图和本日志。真实模型纠错成功率、真人端到端速度及长期可靠性仍须实际复试，不标closed；若回退仅撤083增量并于空闲时重新加载，保留业务数据与早期修复。

## 2026-10-26 · SUPPLEMENT-REPLY-ORDER-082

- 状态：`verified（仓库全量回归与本机加载；真人复试及外部生产待验）`。
- 现场与目标：用户复试`iv_a9a74c4b4b88434f`第7交流`turn_582a5eb2b2574701`仍显示处理重试。081日志明确02:04:36Z语义分析v131成功耗11497ms，02:04:39Z补充分类finish后再次v132完整分析，仅剩约5647ms；02:04:44Z交互超时，02:04:45Z恢复时同文本调用额度耗尽。前6份回答已评分完成，worker无本场失败。本场后由用户主动暂停，不自动恢复或提交。
- 根因与范围：已送达补充问题后发生题目解释交流，旧准备与新边界/语境不匹配而正确拒绝复用；`listening`分支仍先完整语义分析再分类补充回复，导致确认模式下重复推理。保留081增量及Git基线`6e6daed`；沿用已读设计文档与codebase-design边界，修正编排顺序和对应预计算门槛，先判断已送达补充问题后的新回复，再按现有确认/不确定/退出/公司问题分支处理完整证据，不扩展receipt权限、不增加时限或次数。
- 验证与运行：先用合成材料复现，再覆盖新技术补充+结束、纯结束、未送达问题、空回复、低置信度、退出/澄清、旧来源及真实提交围栏；运行相关与完整回归，同步架构、问题、进度、路线图。真实DB只读，先完成验证；若按既有授权加载，仅在无参会者且worker空闲时进行并保留实际加载记录。实际文件与结果结束时补充。
- 实际文件：082仅增量修改`app/services/spoken_supplement.py`，新增`tests/test_supplement_reply_{order,guards,sqlite}_082.py`及[082说明](supplement-reply-order-082.md)，同步架构、已知问题、进度、路线图和本日志。081既有增量保留，API、Prompt、Schema、数据库结构、评分及调用预算均未变化。
- 定向验证：order新增4项改前全部失败、改后通过，红测试日志`/private/tmp/interviewer-082-order-red.log`保留；与guards及原补充确认、078收据、073提交、语义端点、接话和081恢复共9文件`152 passed/17.20s`，日志`/private/tmp/interviewer-082-order-target.log`。新增SQLite4项经过真实临时数据库、EvidenceChain、持久owner和提交事务；隔离进程模拟旧顺序明确多一次未确认分析而预期失败，恢复当前源码后与078、073、081围栏共`84 passed/2.28s`。两组数字重叠，不累加；082新测试共26项，未重放真实场次。
- 独立审查：逐项与081的460份冻结文件核对，唯一既有生产文件差异为`spoken_supplement.py`的资格判断及两个调用点；剔除该增量后源码摘要精确还原081。接话优先、静音、退出/定向澄清、问题实际送达、完整final、低置信度、新输入撤销及最终事务门禁保留，无未解决阻断项。`git diff --check`通过；含`main.py`的464份Python文件冻结至`/private/tmp/interviewer-082-tested-source-sha256.json`后运行完整后端，结果另补。
- 完整回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`首次全量即`4160 passed/8 skipped/269.50s`，日志`/private/tmp/interviewer-082-backend-full.log`；464份Python文件与测试前冻结摘要一致。新增26项全通过，8项为既有环境跳过；无前端修改，不重复前端构建。空闲预检API健康与就绪200、唯一worker pong且active/reserved/scheduled均0、1个空媒体房间/0参会者，记录`/private/tmp/interviewer-082-preflight.log`。
- 实际加载：沿用此前用户重启授权，再次确认空闲并保留原进程环境后，优雅退出API64521与worker64522及其子进程，无强制终止。2026-10-27T02:23Z复核新API75027、worker75028，health/ready及前端5174页面/代理均200、唯一worker pong且三种队列计数为0、媒体0参会者；新API/worker日志无ERROR或Traceback，464份源码摘要仍匹配。记录`/private/tmp/interviewer-082-reload.log`及`interviewer-082-reload-result.json`，运行日志`interviewer-082-api.log`、`interviewer-082-worker.log`。
- 数据及边界：加载前后67份业务文档的逐字段核心摘要完全一致；明确排除会被连接/心跳正常更新的根字段`version/updated_at/heartbeats/last_activity_at/agent_events/agent_runtime/lifecycle_events`，逐字段哈希留存在`/private/tmp/interviewer-082-core-{before,after}-fields.json`，不声称整文档不变。现场场次仍paused、6份答案、第7交流asking；未重放候选内容、点击重试、恢复面试或提交答案。082新代码全量与空闲加载无失败；改前及模拟旧顺序的合成失败作为回归证据保留。待用户真人复试核验本路径时延和长期外部供应商表现，不标记closed；既有Git基线`6e6daed`不变，081/082增量留在工作区，未推送远端。

## 2026-10-26 · ANSWER-PREPARATION-CANCELLATION-081

- 状态：`verified（仓库全量回归与本机加载；真人长场次及外部生产待验）`。
- 授权与起点：用户要求先提交既有代码再实施前轮优化；基线已独立提交`6e6daed`（142文件），提交后工作区干净。沿用并按顺序复核11份设计文档，应用codebase-design技能集中准备任务生命周期与复用检查。
- 现场依据：`iv_262fb51631ee45e7`的两次分析在1966ms/678ms被`input_changed/source=audio`撤销，随后20秒窗口耗尽；恢复提示与静音等待消耗约15秒，下一次因共享两次调用额度耗尽而失败。已加载079诊断明确了原因，不归为供应商18秒超时或worker失败。
- 实施范围：音频活动立即撤销推进/提交资格，但为不变证据保留唯一有界准备；新转写、完整证据/语境/采集/所有权变化废弃旧结果。成功结果同步复用；恢复通知与纯后台准备并行，通知后免除无必要的再次静音等待。保留18/20/40秒、两次实际调用上限、真实补充/退出确认及最终提交fence，不退款、不无界续期。
- 验证与运行边界：增加音频干扰、真实补充、上下文变化、后台失败计数、播放期间恢复及关闭清理的故障注入/集成验收，再运行相关和全量回归。真实业务库只读，不重放候选材料或自动重试/提交场次；先完成代码验证，活动面试期间不重启。实际文件、失败记录、验证结果与加载状态结束时补齐。
- 实际文件：修改`app/services/{answer_endpoint,speculative_turn_preparation,spoken_supplement,interview_evidence,interviews}.py`；新增`tests/test_{answer_preparation_cancellation,preparation_context,preparation_fences,recovery_overlap}_081.py`，更新`tests/test_answer_endpoint.py`中音频与真实转写的取消合同。同步架构、领域、CONTEXT、问题/进度/路线图和本日志，新增[081说明](answer-preparation-cancellation-081.md)。无前端、API、Prompt、Provider、评分规则或DDL改动。
- 故障复现与修复留痕：保留准备首批6项改前5失败/1通过；复用seam的15项改前失败；通知选中/播放各状态5项曾因对话指纹变化失败；并行恢复2项改前失败，通知选择前/后异常追加2项先失败后通过。独立审查另复现正式与投机反向重复调用、到期旧任务误被恢复复用、已取消投机清理未等待及旧任务回调错误归属，均加入回归后修复。旧endpoint测试原要求audio取消Provider，已改为音频保留且真实转写变化后取消，仍验证旧结果不能提交。故障注入全为合成材料，没有外部调用或真实业务数据副作用。详细临时日志保留在`/private/tmp/interviewer-081-*.log`。
- 阶段验收：四组081新增测试独立复核`75 passed/1.55s`；包含079诊断、078预算与恢复、确认复用、非关闭端点、语义端点和通知组合的13文件命令`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_answer_preparation_cancellation_081.py tests/test_answer_diagnostics_079.py tests/test_understanding_diagnostics_079.py tests/test_confirmation_preparation_reuse.py tests/test_confirmed_preparation_liveness.py tests/test_preparation_failure_budget.py tests/test_nonclosing_answer_endpoint.py tests/test_answer_endpoint.py tests/test_semantic_turn_endpoint.py tests/test_answer_recovery_078.py tests/test_answer_budget_semantics_078.py tests/test_preparation_context_081.py tests/test_recovery_overlap_081.py`为`184 passed/11.41s`（数字与75重叠，不累加），日志`/private/tmp/interviewer-081-cancellation-target4.log`。`git diff --check`通过；460份Python源码/测试冻结到`/private/tmp/interviewer-081-tested-source-sha256.json`，完整后端回归正在运行。
- 加载预检：只读确认API54007健康200、worker54008唯一节点pong且active/reserved/scheduled均0、LiveKit一空房间/0参会者。沿用用户之前重启授权，全量通过后再次确认空闲再加载；临时脚本保留原进程环境且不输出凭据。当前预检未重启或提交真实场次。
- 首轮完整回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`得到`4128 passed/6 failed/8 skipped/266.84s`，日志`/private/tmp/interviewer-081-backend-full.log`。失败涉及3项旧音频取消断言，以及权威final更正后的恢复、接话处理deadline和快速失败恢复时序；未视为验收通过，未加载运行服务。逐项核对真实逻辑与旧测试时序后再跑全量，修正结果另补。
- 首轮修正：生产源码未再变更。`tests/test_nonclosing_capture_integration.py`及`tests/test_parallel_conversation_052.py`更新audio保留但禁止推进、真实转写/关闭取消、完整音频唯一答案断言，两文件`24 passed/6.73s`；`tests/test_nonclosing_continuous_stt.py`、`tests/test_reception_arbitration_060.py`与`tests/test_recovery_episode_066.py`等待后台失败完成/退避建立后再推进合成时钟，容许并行恢复快速跨过中间计数，仍严格核验最终调用及取消上限。后者与新增准备/通知测试共`69 passed/13.13s`（`/private/tmp/interviewer-081-full-regression-fixes.log`），独立三整文件`33 passed/12.90s`，无未解决审查项。首次源码快照保留为`/private/tmp/interviewer-081-first-tested-source-sha256.json`；最终快照460份仅上述5测试文件变化，完整第二轮回归进行中。
- 最终仓库验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`得到`4134 passed/8 skipped/261.95s`，exit0，日志`/private/tmp/interviewer-081-backend-verified.log`。8项为既有外部环境跳过；前端未修改，不重复构建。下一步再次核对460份源码及空闲状态后按既有授权加载，运行验收结果另补。
- 实际加载：2026-10-26T10:01Z再次确认唯一worker空闲、LiveKit无参会者，保留原进程环境并优雅关闭API54007/worker54008，启动API64521/worker64522。首次启动后子进程探测exit1（敏感输出受抑制，未获得具体异常）；进程持续存活且日志显示startup/worker ready，未再次重启。随后独立复核health/ready/前端/前端代理均200、ready=true、无失败检查；worker唯一节点pong且active/reserved/scheduled均0，媒体2空房间/0参会者。460份源码/测试仍与全量通过的哈希一致。日志`/private/tmp/interviewer-081-{api,worker}.log`，验证摘要`/private/tmp/interviewer-081-reload-result.json`。
- 数据核对与限制：重启前后均66份interview记录，但整文档SHA不一致；观察到浏览器自动重连在10:01:55Z追加`interview.participant_ready`及持续心跳/version更新。预检仅保存整文档摘要，不能据此声称逐字段完全不变；截图场次仍0答案/1个asking轮次，新的API日志没有该场次模型处理事件。本轮没有主动重放、重试、确认或提交真实候选答案，没有变更凭据。保留此差异与首次探测失败记录，不把整文档对比记为通过。
- 交付：基线`6e6daed`已独立提交，本次081优化保留工作区供审阅，未push。仓库及本机加载完成；真人复试和外部供应商长期可靠性仍待观察，不标closed。回退应只撤081增量并在空闲时重新加载，保留基线及所有业务记录。

## 2026-10-26 · BASELINE-COMMIT-080

- 状态：`verified`。
- 授权与目标：用户要求先提交之前代码，再实施音频取消与恢复流程优化。本工作项只将当前069–079的源码、测试、文档及已有前端构建资源保存为独立Git基线，不包含后续优化。
- 起点与验证：当前分支`dev`，HEAD `bde6e2c`；当前456份Python源码/测试与079全量4059通过/8跳过时的哈希一致。保留既有全部修改，按明确文件范围暂存，检查暂存diff，不纳入运行数据库、录音、凭据或临时日志；不推送远端、不重启服务。提交结果随后在本日志补记。
- 实际结果：`git add -- CONTEXT.md app docs tests`、`git diff --cached --check`及暂存路径检查通过；`git commit -m "fix: preserve interview recovery and diagnostics baseline"`生成`6e6daed`，共142文件，提交后工作区干净。未推送、未改变业务数据与运行服务；本条完成状态随后续081日志更新保存，提交本身包含原in_progress登记。

## 2026-10-26 · ANSWER-DIAGNOSTICS-RELOAD-079

- 状态：`verified（本机已加载；用户真人复试待验）`。
- 授权与目标：用户明确要求重启后自行重试；加载已通过全量回归的079诊断代码，沿用现有API及worker各自的运行环境，保留前端及媒体服务。
- 操作范围：先核对源码哈希、目标进程、worker活动任务和LiveKit房间；空闲后正常TERM并重启本项目API与worker/Beat，再检查healthz/readyz、前端代理、worker响应与日志。保留历史日志及业务记录，不代用户重试、提交或恢复面试。
- 初始检查：API37084、worker37085和Vite37086仍是10月25日启动的旧进程。旧078临时重载脚本已不存在；一次普通沙箱ps被拒绝，已通过只读提权核对进程。均未影响运行服务。最终PID、日志路径、验证与未完成事项随后补充。
- 预检与加载：456份源码/测试与079全量验收哈希一致；读取各进程原环境并仅输出配置变量名。API保持本地媒体开关，worker保持原broker；唯一worker pong，active/reserved/scheduled均0、LiveKit房间0后，正常TERM旧worker及其12个相关进程、旧API及其2个相关进程，未使用强杀。启动API **54007**、worker/Beat **54008**；前端37086、媒体服务及供应商配置保持。
- 验证结果：`/healthz`和`/readyz`均200、ready=true且无失败检查；5174的`/web/`及`/healthz`代理均200。新worker唯一节点pong、active/reserved/scheduled均0，ready后dispatch持续成功。面试/答案/评分相关65份持久文档的前后摘要一致；末次LiveKit检查1个空房间、0参与者，未对真实场次发送操作。API启动默认启用079诊断；真实回答的新日志由用户复试产生，不把健康检查当作真人链路验收。
- 操作失败留痕：macOS进程环境sysctl初用4MiB缓冲区失败，改256KiB后成功；预检假设安装livekit.api SDK触发ImportError，改用仓库已有HTTP/JWT协议只读ListRooms后通过。以上均发生在停止服务前。首次加载尾部worker inspect未获得完整结果，服务已ready且正常派发；独立再次执行所有检查通过，无二次重启。
- 实际文件与证据：仓库仅更新本日志及079问题/进度/路线图加载状态，未修改应用源码。临时重载脚本`/private/tmp/interviewer-079-reload.py`、结果`/private/tmp/interviewer-079-reload-result.json`；新日志`/private/tmp/interviewer-079-api.log`与`/private/tmp/interviewer-079-worker.log`，旧20261025日志完整保留。验证命令为重载脚本预检/`--restart`、结果中的独立health/ready/worker/media/只读SQLite摘要复核及`git diff --check`；无代码变化，不重复全量测试。
- 未完成与恢复：待用户真人复试后关联新scope与取消/失败事件；历史缺失日志无法回填，限时策略未调整。不自动重试、提交或恢复历史场次。若进程后续退出，按新PID核对并沿原环境恢复，保留现有业务库与日志；未提交/push。

## 2026-10-25 · ANSWER-PROCESSING-DIAGNOSTICS-079

- 状态：`verified（仓库全量回归；运行服务尚未加载）`。
- 授权与目标：用户要求先补齐回答处理的请求开始、实际剩余时间、取消来源和失败原因记录。本项只补可观测性及验证，保留现有18/20/40秒期限、重试次数、模型路由、Prompt、评分与提交规则。
- 现场依据：`iv_f618e4aa1f9045db`第六话题在2026-10-26T01:41:54Z、01:42:10Z记录理解失败与额度耗尽，但缺对应非流式模型取消审计；01:44:14Z新语音后准备成功并提交，worker首次评分成功。无法据现存日志区分外层期限、供应商等待和主动取消，不能补造历史失败原因。
- 起点与范围：按顺序复核11份设计/记录文档中现行恢复、模型调用、持久化及日志约束；保留所有既有未提交修改。计划修改统一诊断seam、非流式网关审计、实时准备/理解取消来源及失败记录，增加故障注入和去敏测试，同步相关设计与问题/进度/路线图。
- 数据与运行边界：真实业务库只读，不重放候选语音或回答，不变更供应商凭据；本轮先完成仓库实现和回归，不重启活动API/worker。实际文件、测试结果与未完成事项结束时补充。
- 初轮验证与修复留痕：理解/调用额度/安全合同82项通过；真实取消链初轮测试的合成adapter缺`provider_id`，先触发入口拒绝而非期望超时，修正夹具后暴露Python3.9无`Task.cancelling()`的问题。改为弱引用集合去重取消记录后，端点/理解诊断20项通过。所有失败均为本地合成测试，未调用外部模型、未改真实业务库或运行服务；独立复核继续核对审计失败时终态闭合与后台scope隔离。
- 独立复核与追加修复：成功响应后审计写入失败会漏终态，已先闭合模型attempt再单独报告审计失败，避免重复terminal；confirmed/preview的延迟失败回调保存原scope，shield等待者取消不污染后台准备归因。网关测试的等待型adapter同样漏`provider_id`导致合成测试挂起，已正常中止相关pytest、修夹具并为等待加1秒上限；没有外部进程或业务数据副作用。局部wait_for先取消内层再返回TimeoutError是现有语义，使用父scope终态关联归因，不修改取消算法或猜测供应商状态。
- 实际文件：新增`app/core/model_diagnostics.py`及`tests/test_{model,answer,understanding}_diagnostics_079.py`；修改`app/model_gateway/gateway.py`、`app/core/speech_diagnostics.py`、`app/services/{answer_endpoint,conversation_understanding,speculative_turn_preparation,livekit_evidence_ingress}.py`（ingress只传内部诊断归属）。同步架构、领域、供应商、存储、CONTEXT、问题/进度/路线图与本日志。无Prompt/API/评分策略/前端/DDL变更。
- 阶段验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_answer_diagnostics_079.py tests/test_model_diagnostics_079.py tests/test_understanding_diagnostics_079.py tests/test_model_invocation.py tests/test_interaction_budget_067.py tests/test_runtime_budget_068.py tests/test_answer_recovery_078.py tests/test_answer_budget_semantics_078.py tests/test_prepared_turn_decision.py tests/test_stable_preview_preparation.py tests/test_nonclosing_answer_endpoint.py` **188 passed /6.31s**，日志`/private/tmp/interviewer-079-combined-final.log`。最后gateway单独42项、三份079文件独立复核35项通过（与前述有重叠，不累加）；`git diff --check`及文档链接检查通过。456份运行/测试Python源码冻结到`/private/tmp/interviewer-079-tested-source-sha256.json`，完整后端回归进行中，结果另补。
- 最终验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` **4059 passed /8 skipped /244.13s**，exit0，日志`/private/tmp/interviewer-079-backend-full.log`。包括新增35项诊断测试、临时SQLite取消审计重开验证、真实端点→理解→网关故障注入及现有完整业务回归；8项外部环境跳过不代表生产验收。456份运行/测试源码与冻结哈希一致；最终diff与文档链接检查通过。前端未修改，不重复构建。
- 交付与未完成：本轮代码与仓库验证完成，18/20/40秒及调用额度、路由、Prompt、评分、提交规则保持。API/worker未重启，因此新诊断须后续加载后生效；真实会话、音频、答案、评分、模型凭据和配置均未变更。历史缺失原因不能回填；真人长场次的新日志观察和随后限时策略调整仍未实施，不标closed。无Git提交/push。回退应只撤079增量，保留078及更早未提交修改和全部业务记录。

## 2026-10-21 · ANSWER-RECOVERY-AND-REUSE-078

- 状态：`verified（仓库全量回归与本机加载；真人长场次及外部生产待验）`。
- 授权与目标：用户同意实施前轮六项问题及低成本恢复方案，允许自行解决实现冲突。保留069–077既有修改、现有模型与音色；正常回答仍须实际询问补充及取得新回复，整场退出仍独立二次确认。
- 现场证据：`iv_9361bc5500834881`在01:28:18Z成功准备追问，01:28:35Z确认finish后再次完整决策，12秒超时且20秒共享预算耗尽；按钮错误复用continue_speaking并撤销确认。补充询问仍在snapshot阶段执行，其预算超时被误分类为识别恢复的代码缺陷已合成复现；本场“回答啦”被当作技术疑点并实际播出。
- 实施计划：在完整证据与新确认绑定下复用理解/追问；统一有限自动恢复及分阶段期限，保留已接受确认；独立处理重试操作与跨重连状态；修复普通交流澄清误判，补安全分段诊断。预计算/格式修复/供应商重试的成本归属统一核对，禁止叠加无界请求。最终实际实现、测试与限制另补，不以本地回归证明外部供应商永不超时。
- 起点：沿用前轮按顺序核对的11份设计文档及codebase-design技能，保留HEAD和全部未提交工作。真实业务数据只读；完成验证后仅在确认服务空闲时加载，不自动恢复或确认历史候选场次。
- 初轮验证与独立复核：理解/语义/调用额度316项、endpoint关联187项通过；新增恢复首轮5项在修改前全部失败、修改后通过。前端完整521项/26文件与临时构建通过。复用已补真实临时SQLite事务与重开存储检查，不仅使用内存替身。独立复核发现并追加处理：不同稳定前缀造成多次投机调用、不可重试配置错误被新词再次触发、已送达提示pending残留串到后续输出、stale准备提交重复复用，以及DashScope协议兼容在单次attempt限制下仍隐藏重发。最终覆盖结果另补，不将这些中间版本加载到后端。
- 运行检查：普通沙箱`ps`被系统拒绝，获准只读进程检查后确认本项目API92921/worker92922/Vite69772；未操作其它项目进程。`/private/tmp/interviewer-078-reload.py --check`确认worker单节点、active/reserved均0、LiveKit rooms=0；尚未执行加载，执行前必须再次确认。
- 最终实现：复用采用进程内`PreparedCompletionReceipt`，原技术分段/质量、实际送达与新补充回复、capture/owner/控制权及事务内上下文同时有效才组合；不冒充新模型调用。每capture最多一次实际投机，门槛3秒；同一未变化文本共享两次完整调用尝试，格式纠正/追问修正计入。单次18秒、外层20秒，首次可恢复失败使用原起点40秒上限；同证据投机失败先开启恢复窗再发第二次。新技术内容需重新分析，不能承诺整场/整题只有两次或固定金额。
- 恢复与协议：新增`retry_answer_processing→evidence.retry_processing`，保留有效确认，当前题/capture/owner及单任务限制不变。补充询问独立期限与精确零PCM清理才可自动重播，送达清pending；stale提交清held reuse再准备。不可重试provider错误跨新词阻断、额度耗尽保留显式兜底；事件/快照安全投影真实stage/reason，不把失败次数当实时进度。理解98–109/组合129–140与统一校验否决清楚流程用语的技术误澄清；原历史版本可读取。DashScope显式单次attempt不再内部隐藏HTTP重发，无该限制的协议兼容仍保留。
- 实际代码：新增`app/core/answer_processing_budget.py`、`app/services/{prepared_answer_completion,answer_preparation_recovery}.py`；修改`app/core/{interview_agent_metrics,speech_diagnostics}.py`、`app/core/prompt/{contracts,semantic_turn}.py`、`app/domain/{interview_agent,evidence_coordination}.py`、`app/model_gateway/gateway.py`、`app/providers/{mock,dashscope}/provider.py`及`app/services/{conversation_understanding,interviews,interview_evidence,answer_endpoint,spoken_supplement,speculative_turn_preparation,interview_agent,livekit_evidence_ingress,evidence_command_journal}.py`。前端修改候选`agent-experience/presentation`及interviews `agent-event-runtime`与对应测试；新增078的复用、预算语义、endpoint恢复、提示阶段、接口及DashScope预算六个测试文件，更新相关Prompt版本与时限旧夹具。同步11份设计/记录文档及078专项说明。保留069–077增量，不修改真实模型路由/音色/业务记录。
- 冻结验证：后端补充定向211项、语义316项、接口117项、DashScope86项均通过，数字有重叠不累加；SQLite/复用交叉56项通过。前端最终`npm test -- --reporter=dot`521项/26文件、`npm run build -- --outDir /private/tmp/interviewer-078-web-dist`通过（现有大bundle提示保留）。源文件288份已冻结到`/private/tmp/interviewer-078-tested-source-sha256.json`；后端完整`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`运行中，结果另补。
- 首轮完整回归：`/private/tmp/interviewer-078-backend-full.log`，**20 failed /4003 passed /8 skipped /261.54s**。未执行加载。失败集中在旧1秒投机的并行接话/确认夹具、旧三次失败额度、指标集合精确预期及一处拼接式Prompt版本预期；先逐项核对真实行为和原安全断言，再修夹具或代码，最终结果另补。`test_recognition_vocabulary`已确认只是预期v94/v95未随合同升级为v106/v107，修正版本后原文保留/ASR争议断言继续通过。
- 首轮失败核对结果：20项均为本轮明确策略/合同更新后遗留的测试预期，生产代码无需追加修改。parallel13项等待旧1.1秒投机、semantic1项仍要求第3次失败，修后两文件25项通过，旧final修正/撤销/提交切片等断言保留。reception4项修后31项通过，新增1.1秒仍自然接话且不调用完整分析、3.1秒分析挂起时接话不阻塞的参数化验证。指标保留严格集合与隔离断言，扩大真实SQLite合同为尝试上报所有内部指标均不计数，16项通过；词汇17项通过。当前源码与首轮288份运行源码哈希一致，最终全量运行至`/private/tmp/interviewer-078-backend-final.log`。
- 最终仓库验收：上述完整后端命令**4024 passed /8 skipped /243.38s**，exit0，日志`/private/tmp/interviewer-078-backend-final.log`；前端最终521项/26文件和临时构建通过。运行源码288份哈希在最终回归前后完全一致，`git diff --check`及12份本项文档链接检查通过。8项跳过仍不表示外部生产环境已验收；没有真实候选原文模型调用、真实面试状态修改或路由/音色变更。按照用户已授权的空闲加载执行，实际运行结果另补。
- 本机加载（2026-10-21T15:52:28Z）：执行前重新确认单worker pong、active=0、reserved=0、LiveKit rooms=0及288份已测源码完全一致。正常停止旧API92921/worker92922，旧worker子进程完全退出后启动API**17484**、worker/Beat**17485**，沿用原运行环境；Vite69772保持且已提供新恢复操作。原子发布验证构建`index-CFrYG7Qh.js`/`VrmAvatar-ZFWQGM0t.js`，保留旧bundle、旧首页备份`/private/tmp/interviewer-078-previous-index.html`。health/ready=200，唯一worker正常响应，全部既有面试核心业务摘要前后一致，目标场仍report_ready/0份正式答案；没有自动恢复、确认或修改该场。
- 加载验证：8000/5174健康与页面均200，实际静态入口SHA256与临时验证构建一致，Vite实际源码包含`retry_answer_processing`，源码哈希仍完全一致。新进程日志检查无Traceback、database is locked、未取任务异常、FFI Panic或OUTPUT_BACKPRESSURE；这只覆盖加载窗口，不是长期SLA。加载脚本/结果、HTTP验收证据：`/private/tmp/interviewer-078-reload.py`、`interviewer-078-reload-result.json`、`interviewer-078-loaded-verification.json`；API/worker日志为同前缀api.log/worker.log。
- 恢复与剩余范围：本项完成本地代码、回归及空闲加载；未修改供应商/模型/音色/账户设置，无DDL、真实答案重写或Git提交/push。现有音频未知/部分播出和持续外部失败仍按明确失败恢复，不能承诺无人工或零超时；真人长场次、实际账单节省比例及生产依赖联调未验收，不标closed。回退仅撤078代码增量并在空闲窗口重载，可恢复已备份静态入口，保留069–077及全部业务记录。


## 2026-10-19 · WARMUP-SQLITE-CONFIRMATION-077

- 状态：`verified（仓库集成、关联回归及本机加载；真人语音复验待完成）`。
- 目标：修复首轮试音已识别但确认按钮消失的实际回归，覆盖真实SQLite存储、Evidence控制权和确认输出完整链路；保留既有控制权、暂停和显式确认门禁。
- 起点：保留HEAD `bde6e2c`及069–076所有既有修改；沿用已按顺序阅读的11份文档。本项遵守用户“先交付结果，保留当前服务”，不重启、不操作真实候选场次、不改供应商/音色、不伪造试音通过。
- 现场证据：会话`iv_192811db4fcc4cc2`两次STT final成功，00:22:19Z与00:22:50Z选择warmup_confirmation，约10秒后出现本地OperationalError，被投影为WARMUP_STT_PROBLEM/retry_warmup；随后WARMUP_RETRY_REQUIRED覆盖提示。确认阶段无TTS供应商调用，未发现欠费/额度错误。
- 合成复现：expression_is_current在SQLite读事务内调用assert_controller，后者经Evidence coordinator开启BEGIN IMMEDIATE写事务，提交等待外层读锁；真实临时SQLite复现database is locked。默认5秒等待在首次输出和恢复检查各发生一次，与现场10秒一致。内存测试未覆盖此存储行为。
- 实施计划：消除嵌套事务并保留事务内fence校验；分开转写完成与确认输出错误边界；完善安全恢复提示。新增真实SQLite生命周期回归及前端连续事件/快照回归，修改文件、验证命令、结果和未完成范围结束时补记。
- 修前自动化证据：新增真实临时SQLite/Evidence/cache组合3项全部复现失败，成功final被降为retrying（`/private/tmp/interviewer-077-warmup-before.log`，3 failed/1.34s）；核心修复后相同3项通过，完整播放及权限/清理边界仍在补验，未据此宣布完成。
- 前端修复与复核：恢复明确试音重试提示、门禁拒绝后的恢复语义及快照重建；确认仍受awaiting_confirmation限制。独立审查追加当前open causation匹配的拒绝用例，修前复现pending残留（`/private/tmp/interviewer-077-current-open-before.log`），修复后定向197项通过。最终`npm test -- --reporter=dot` **514 passed/26 files**（`/private/tmp/interviewer-077-frontend-final.log`）；`npm run build -- --outDir /private/tmp/interviewer-077-web-dist`通过（`/private/tmp/interviewer-077-build-final.log`），仅临时输出，保留既有bundle大小提示。
- 现场状态补记：后续只读复查原会话已paused、0正式题、0答案；本任务未改写该状态。前端源码可能由现有Vite按其开发模式更新，未重启或发布dist；后端修复尚未加载。
- 实施：控制连接检查移出只读会话事务，事务内owner/control/Skill/校准版本/capture守卫保留；STT完成返回确认修订后才进入独立输出阶段。无选中输出的可选提示故障保留awaiting_confirmation；已选中但无法证明安全结束的故障复用同一事务内生命周期PAUSE，清理精确输出并即时投影暂停。零PCM恢复还要求底层流清理确认，不能把abort正常返回当作成功。未更改SQLite超时/journal_mode、模型、音色、Prompt、数据库结构或候选记录。
- 审查失败留痕：初版新输出边界用409表示未知已选中输出，真实合成channel.send复现被通用problem投为可恢复等待；已改为明确持久暂停和不可恢复problem，未加载此中间版本。新增关闭证明后074旧fake直接抛异常导致1项失败，补其零transport已关闭前提，生产门禁不放宽。独立审查另复现ValidatedTTSStream吞abort异常而output.cleanup_complete仍true，追加底层cleanup_confirmed检查及回归。所有场景仅合成临时存储。
- 最终后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_warmup_sqlite_077.py tests/test_warmup_tts_resilience_074.py tests/test_warmup_output_transition_061.py tests/test_livekit_evidence_supervisor.py tests/test_expression_stale_fence.py tests/test_streaming_expression_lifecycle.py tests/test_approved_speech_output.py tests/test_fixed_prompt_audio_075.py tests/test_fixed_speech_preparation_075.py tests/test_followup_audio_recovery_075.py tests/test_followup_expression_failure_071.py tests/test_interview_agent_contracts.py tests/test_dispatcher_read_only.py tests/test_sqlite_store.py` **354 passed/14.62s**，`/private/tmp/interviewer-077-backend-final.log`。新增077共7项，覆盖SQLite真实控制权、固定缓存hit/miss、ready→PCM→drained→显式confirm、输出阶段失败、过期控制权及流清理不确定；另独立复核51项通过，与354有重叠，不累加。前端514项与临时构建通过，`git diff --check`通过。
- 实际文件：`app/services/{interview_agent,livekit_evidence_ingress}.py`；`app/web/src/features/candidate/{agent-experience,presentation}.js`及对应测试；新增`tests/test_warmup_sqlite_077.py`、修正`tests/test_warmup_tts_resilience_074.py`夹具；同步架构/接口/领域/数据库/问题/进度/路线图/操作日志。保留069–076全部既有修改，无提交/push、真实场次操作或服务重启。
- 未完成与恢复：本项是针对已复现本地锁冲突的仓库验收，真实供应商/浏览器复验及后端加载仍待授权窗口；不能承诺外部服务永不超时。现有暂停场次仍需原人工恢复流程，不自动确认字幕、写答案或重试邀请。回退只撤077增量，保留其它工作项及业务记录。
- 加载授权：用户在本次结果交付问题中明确选择“确认空闲后加载修复”。后续先验证实际进程环境、LiveKit空房间与worker active/reserved为空，再正常停止/启动当前服务并验证健康、源文件身份与业务摘要。旧暂停场次不自动恢复、试音不自动确认；准备临时077加载脚本及只读检查，结果另补。
- 实际加载（2026-10-20T01:09Z）：只读检查和执行前重复检查均为1个worker、active=0、reserved=0、LiveKit rooms=0。正常停止旧API78573/worker78574，启动API92921/worker92922；Vite69772保持运行并提供最新源码。原子发布已验证`index-CiYtlZ-Y.js`与`VrmAvatar-D1uvfuBC.js`，保留旧bundle；旧首页备份`/private/tmp/interviewer-077-previous-index.html`。075末次追加、076与077在同一源文件快照下加载，无配置/音色变更。health/ready=200、worker响应正常、所有既有面试核心业务摘要完全一致，目标场仍paused/0答案。证据：`/private/tmp/interviewer-077-reload.py`及`/private/tmp/interviewer-077-reload-result.json`。
- 加载后验证：8000 health/ready/页面、5174 health/页面均200，线上静态bundle与已验证构建SHA256相同；Vite两份实际源码含077恢复逻辑。8000与5174控制通道均成功升级并以AGENT_TICKET_INVALID/4403拒绝合成无效票据，未消费真实邀请或创建会话；已加载全部Python/前端源文件哈希保持。初次HTTP辅助检查使用了拼错的前端变量名而返回false，源码核对后以实际rejectedWarmupOpen复验通过；保留初次`interviewer-077-loaded-http.json`与最终`interviewer-077-loaded-verification.json`，未把检查脚本标记错误当作产品故障。
- 启动真实TTS结果：01:09:48Z至01:09:56Z，5条固定系统提示全部stream_success，分别2499/1572/2128/1373/1728ms；无本轮error_code，沿用原供应商和音色。此为固定提示预热成功，不等同真人整场或长期SLA。没有自动恢复、确认或修改目标场。最终diff检查通过；运行修复已生效，剩余是用户实际试音/长场次验收。新增加载文件及075运行状态文档更新纳入本077工作项；不提交/push。

## 2026-10-19 · INVITATION-READINESS-DIAGNOSTICS-076

- 状态：`verified（仓库定向回归；按用户选择保留当前服务，后端未加载）`。
- 目标：定位“预约已保留，邀请待重试”的真实阻塞项，修复动态TTS探测与运行协议不一致及弹窗丢失具体错误原因；保留当前模型、音色和准入门禁。
- 起点：保留HEAD `bde6e2c`及069–075全部既有修改；沿用已顺序阅读的11份设计文档，重新核对预约准入、模型探针及前端错误投影。用户已选择保留当前服务，本项不重启API/worker、不签发真实邀请、不改预约或模型配置。
- 现场只读证据：`appointment_27019df5a08b428f`于2026-10-19T23:50:12Z创建成功，status=scheduled/version=1/invited_at=null，随后两次invite返回409。四项STT/理解/追问探测成功；表达TTS路由`route_2997239361174007`于23:50:28.143Z记录provider_timeout、可重试、30秒冷却。原24小时健康结果已过期，触发自动刷新；不是以API在线代替供应商健康。无欠费或额度错误证据，未查询账户余额。
- 代码证据：TTS路由探针走批量gateway.invoke，而正式动态表达走open_tts_stream；探针外层15秒、路由目标30秒，取消未产生该次TTS model_invocation记录。前端接住任意invite异常后仅短暂toast，固定弹窗没有接收code/details。
- 计划与边界：动态表达探针验证与实际配置一致的合成协议、有效PCM及有界资源回收；错误不冒充健康，不靠延长健康有效期通过门禁。弹窗按结构化错误展示具体阻塞项，重试失败更新原因，避免把权限/预约过期/网络问题统称readiness。通过合成故障测试验证，实际文件、结果和未验收范围结束时补记。
- 排查期间的新事实：23:55:36Z出现后续invite 200，预约变为invited/version=2，同一`qwen3-tts-flash`批量TTS成功、640ms。此操作不是本任务发起，排查只读。说明现场故障为一次临时超时；仍无网络子阶段证据，不能断言供应商容量、线路或账余额原因，也不能将后续成功归因于尚未加载的076代码。
- 验证进行中：前端定向29项通过（新增22项），readiness/预约/流关闭129项通过；日志`/private/tmp/interviewer-076-invitation-ui-tests.log`、`/private/tmp/interviewer-076-readiness-tests.log`。完整前端与独立临时目录构建在运行，不发布静态首页；TTS探针新增验证及最终相关后端回归另记。
- 合成故障复核：新探针初轮15项中5项失败，3项为批量测试夹具缺request_id，2项暴露既有安全错误白名单缺音频终片/格式类别；已修夹具，并补两个标准安全分类及页面映射，未增加原始异常输出。另发现统一TTS流与DashScope abort有意吞关闭异常，探针不能仅凭await abort返回宣称回收成功；追加只读cleanup确认/错误观测，保持原取消、重试及连接池行为。上述为合成复现，不是新增真实账户或候选故障。
- 前端最终验证：`npm test -- --reporter=dot` **506 passed /26 files**（`/private/tmp/interviewer-076-frontend-final.log`）；`npm run build -- --outDir /private/tmp/interviewer-076-web-dist`通过（`/private/tmp/interviewer-076-build-final.log`），保留既有bundle大小提示，未发布dist或操作当前服务。
- 实施与复核：动态表达探针改用实际流式合成、完整PCM/终片/清理确认与单一截止；明确不支持流式的既有模型，沿用无PCM且清理确认后的同路由批量兼容，不新增供应商/音色，迟到鉴权/计费错误阻止兼容。模型网关/适配器只增加只读清理结果，原播放及abort行为不变；readiness保留截止时已知标准错误及取消传播。弹窗保留安全失败项并更新重试结果，真实邀请门禁与防重复创建保持。
- 本项实际文件：`app/services/{model_admin,model_route_readiness}.py`、`app/domain/model_route_readiness.py`、`app/model_gateway/tts_streaming.py`、`app/providers/dashscope/tts_streaming.py`；`app/web/src/features/interviews/{Page.jsx,Page.test.jsx,invitation-failure.js}`；`tests/test_model_route_readiness.py`和新增`tests/test_tts_readiness_076.py`；同步架构/接口/供应商/问题/进度/路线图/操作日志。保留所有069–075既有修改，无迁移、模型参数或候选数据写入。本轮不提交/push，不重启API/worker、不签发邀请、不发布静态dist。
- 验证阶段记录：新增流式探针24项及相邻文件111项通过后，最终兼容复核发现原批量模型不应因全局流式开关而被拒绝；补同截止兼容及迟到错误门禁，新增文件现36项，三文件123项通过（`/private/tmp/interviewer-076-tts-compatibility-final.log`）。此前13文件439项通过为兼容追加前结果，最后冻结后重跑同集合，结果另补。
- 未完成与恢复边界：当前后端仍按用户要求保持原运行版本，076代码尚未加载，未进行新探针的真实供应商复验。已有同路由批量兼容路径被外层取消时仍可能没有单次invocation记录，route health会记录失败；本项未改通用批量审计。供应商临时超时无法由本地代码保证不再发生，未将后续640ms成功归因于修复。回退仅撤076增量并保留既有改动；未来加载需另行确认运行环境和空闲窗口。
- 最终冻结验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_tts_readiness_076.py tests/test_model_route_readiness.py tests/test_appointment_readiness_refresh.py tests/test_probe_stream_cleanup.py tests/test_model_configuration_v2.py tests/test_model_invocation.py tests/test_tts_streaming.py tests/test_tts_pipeline_074.py tests/test_tts_budget_resilience_074.py tests/test_fixed_prompt_audio_075.py tests/test_fixed_speech_preparation_075.py tests/test_tts_http_pool_075.py tests/test_prompt_governance.py` **451 passed /17.08s**，日志`/private/tmp/interviewer-076-backend-verified.log`，exit0。前端506项与临时构建通过，`git diff --check`通过；这是本项影响范围回归，不将075旧全量3878计入本次通过数。

## 2026-10-18 · SAME-VOICE-TTS-STABILITY-075

- 状态：`verified（仓库全量；末次追加按用户选择暂不加载）`。
- 目标：按用户确认保留当前TTS模型和音色，实施固定提示音预生成/缓存、受控HTTP连接复用，以及正式追问失败后对原批准文本的恢复；不配置备用供应商。
- 起点：保留HEAD `bde6e2c`和069–074全部未提交修改；沿用已按顺序阅读的11份架构文档并重新核对相关章节。074已加载API69745、worker69746、Vite69772，既有3717后端/473前端通过证据不能代替本项验证。
- 设计边界：固定提示采用版本化白名单和精确组织/路由模型/音色/语言/合成参数身份，完整音频才可缓存，候选内容不进入共享缓存；命中仍须实际播放和原权限守卫。连接复用按进程/事件循环及连接配置隔离，每次请求独立认证，取消单流不能关闭其它流。追问恢复复用同一批准文本和当前题目，不能重新生成题、重交答案、伪造送达或覆盖已暂停/接管/切题。
- 验证及加载计划：覆盖缓存身份/损坏/并发、连接复用/取消/关闭、零音频追问恢复/部分发声/迟到结果/幂等；联合回归后在无活动媒体和worker任务窗口加载。接口/持久字段先同步文档，实际修改文件、命令、结果、失败与未验收项结束时补齐。不外发真实候选材料、不自动操作历史场次。
- 实施：新增版本化5句固定音频白名单、完整PCM缓存与API生命周期预热；精确主路由身份和请求参数校验；HTTP池按PID/loop/配置隔离；正式追问未播放恢复marker、精确request_repeat与owner/capture/播放门禁；前端安全投影、重试按钮及连接恢复。scope按persistence独立，单个取消与配置变更不污染其它缓存/流。
- 真实固定文案验证：2026-10-19 03:48 UTC同现有模型`qwen3-tts-flash`、0备用目标，5条预热全部成功，完整PCM1463040字节，耗时8342ms；仅1次TCP/TLS连接。随后5条缓存命中、新增供应商调用0，服务端首段PCM读取1.489–1.946ms，非端到端首声。只在独立内存容器审计，不写业务库、不发候选内容；`/private/tmp/interviewer-075-live-cache-result.json`。
- 定向验证：HTTP池/旧流式诊断157项，缓存/生命周期/prompt/pipeline59项，路由快照27项，追问恢复及相邻链路133项；数字重叠。前端最终482项/26文件通过，临时正式构建成功（既有大bundle提示）。12份文档链接与diff检查通过。
- 失败留痕：新initialize初稿误用Principal.role（实际roles）已修；缓存集成测试首次3项因async生成器join夹具写法失败，已修测试helper；恢复测试首次缺hub注册导致读到空snapshot，补真实注册后通过。前端独立复核重现断线后重试pending卡死并修正，新增重连/收音显示用例。首次真实跨端13事件检查12通过/1失败：提前发言拒绝的通用problem action与前端合同不一致，修公共投影后复验。原日志保留，未改变真实会话。
- 首轮后端全量：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`，3842 passed /2 failed /8 skipped /241.14s；`/private/tmp/interviewer-075-backend-full.log`。053打断用例的SimpleNamespace缺新门禁方法需更新真实绑定；061旧control的close遇新增无条件controller断言而抛错，是清理兼容缺口，修复后复验。未将首轮视为验收成功或加载运行服务。
- 上述修复后9文件233项通过，真实跨端13/13通过。第二次后端全量3845 passed /1 failed /8 skipped /260.37s（`/private/tmp/interviewer-075-backend-final.log`）：仅074的160ms墙钟重试测试在第二次尝试前耗尽总预算。原例独立12/12通过，改为1s合成预算、按比例检查首尝试分配并把总截止断言覆盖到完整音频后6/6通过，两个完整关联文件44项通过；未改真实12s策略或生产gateway。保留原失败与重复日志，最后全量复验另记。
- 最终完整后端复验：同命令 **3846 passed /8 skipped /245.41s**，`/private/tmp/interviewer-075-backend-verified.log`；外部环境8项仍跳过。前端482通过/26文件、正式构建与跨端13事件校验通过。最后空闲检查单worker、active/reserved0、LiveKit房间0；已原子发布`index-2zdDc3yN.js`并保留旧bundle，API/worker空闲重载和业务摘要核验进行中。
- 首次本机加载完成：API78573、worker/Beat78574，Vite69772保留；health/ready、5174入口/代理200，静态bundle实际HTTP哈希匹配构建，两端WebSocket升级并拒绝合成无效票据；全部面试核心业务摘要一致。目标场加载前已report_ready/0答案，不能沿用074的paused描述。加载证据`/private/tmp/interviewer-075-{reload-result,loaded-verification}.json`；无真实会话操作。
- 启动真实失败留痕：第一条固定提示在04:10:54 UTC首次尝试4003ms超时，等待供应商响应头3770ms；第二尝试3743ms超时、无子阶段证据。后续4条全部成功，没有返回欠费/额度错误；API正常且业务状态未变化。`/private/tmp/interviewer-075-startup-prewarm{,-phases}.json`保留证据。本项追加每次预热作业最多首轮加一次2秒后补齐，仅重试可重试缺失项，成功/终止错误不重做，保持并发和配置/关闭围栏；不增加永久拒绝或无限后台循环。追加改动后的验证与加载另记。
- 补齐交叉审查用合成故障复现：已收到非重试billing/auth错误，但abort清理耗尽外层期限时，被误记为可补齐超时。追加流式校验seam的只读`terminal_error`保留先发生的标准错误，内外层校验流均检查，终止错误禁止补齐；不更改取消传播或输出成功判断。该复现不代表真实账户欠费，真实启动记录只有超时。补齐回归与全量结果另补。
- 追加改动加载准备：两次执行`/private/tmp/interviewer-075b-reload.py --check`的只读空闲检查均被自动审批服务容量故障阻断（`Selected model is at capacity`），不是安全性否决；命令未执行，无重启或业务数据副作用。保留当前API78573/worker78574运行，继续不受影响的代码验证；追加修复是否实际加载须另记，不能以首次075加载代替。
- 补齐独立复核：`/private/tmp/interviewer-075-refill-independent-review.log`初次1 failed /9 passed，真实HTTP拒绝在开流尚未返回source时仍会被清理取消遮蔽；将标准错误在等待abort前绑定当前provider task，网关保留取消传播并把已知错误交给当前collector，下一调用清除本方法旧注记。另用真实gateway复现非合作open在有界detach后仍存活、cache过早释放slot的缺口；补当前调用真实清理完成信号，保持key/slot直到晚开流及其abort结束。这些均是合成故障，不是新发生的真实供应商/候选故障；最终复验另补。
- 上述定向255项通过后启动追加全量，独立复核又确认迟到开流返回终止错误被cleanup回调丢弃，需让当前调用完成信号保留标准错误、释放slot前撤销补齐资格；不得晚写已复用caller Task。主动中断该轮全量（668 passed /2 skipped /61.52s，exit2 KeyboardInterrupt，`/private/tmp/interviewer-075b-backend-final.log`），不将部分通过当最终验收；修复冻结后重跑。第三次只读空闲检查仍被相同自动审批容量故障阻断，无服务操作。
- 最终补齐修复冻结：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_fixed_prompt_audio_075.py tests/test_fixed_speech_preparation_075.py tests/test_tts_pipeline_074.py tests/test_dashscope_tts_diagnostics_074.py tests/test_tts_streaming.py tests/test_tts_budget_resilience_074.py` **257 passed /5.64s**，`/private/tmp/interviewer-075-fixed-refill-late-terminal.log`；独立8项通过（与上述有重叠），`/private/tmp/interviewer-075-refill-independent-closure.log`。没有剩余代码审查阻断，最终全量日志`/private/tmp/interviewer-075b-backend-verified.log`另补。已向用户说明审批容量故障并询问保留服务或手动加载的选择，不经其它工具绕过审批。
- 实际修改文件：新增`app/core/prompt/fixed_speech.py`、`app/services/{fixed_prompt_audio,fixed_speech_preparation}.py`、`app/providers/dashscope/tts_http_pool.py`；更新`app/main.py`、`app/model_gateway/{gateway,tts_streaming}.py`、`app/providers/dashscope/tts_streaming.py`、`app/services/{interview_agent,approved_speech_output,livekit_evidence_ingress}.py`、`app/domain/interview_lifecycle.py`；更新候选`agent-experience.js`、`presentation.js`、`Page.jsx`与`agent-event-runtime.js`及各自测试。新增`tests/test_{fixed_prompt_audio,fixed_speech_preparation,tts_http_pool,tts_routing_snapshot,followup_audio_recovery}_075.py`，补053夹具真实门禁、074预算测试抗调度抖动及075真实链路故障测试；同步架构/接口/领域/供应商/数据库/统一语言/问题/进度/路线图/操作日志和075说明，更新静态首页及`index-2zdDc3yN.js`、`VrmAvatar-Br5Qlty3.js`。保留069–074全部既有修改，无数据库迁移、模型配置或音色变更。
- 恢复与限制：最后一次后台补齐及清理修复尚未加载，三次自动审批失败无进程副作用。已准备`/private/tmp/interviewer-075b-reload.py`（重新检查媒体/worker空闲、核对原运行环境、正常停止旧进程后启动并验证健康与业务摘要）；静态首页备份`/private/tmp/interviewer-075-previous-index.html`。不得自动恢复真实会话。回退只撤销075增量，保留069–074和全部业务记录；真实长场次、并发弱网、部分播出恢复及外部生产环境未验收。本轮未提交/push。
- 用户最终选择：自动审批容量故障说明后，用户明确回复“先交付结果，保留当前服务”。本轮不再重载API/worker，保留首次075运行版本；最后追加的补齐/错误保留/清理所有权仅交付仓库代码和验证证据，不能表述为已在当前进程生效。
- 最终完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` **3878 passed /8 skipped /241.85s**，exit0，`/private/tmp/interviewer-075b-backend-verified.log`。441份Python代码/测试与`/private/tmp/interviewer-075b-tested-source-sha256.json`冻结哈希一致；前端代码未再改变，482通过/26文件与构建、13个真实跨端事件仍对应本次交付。diff检查及11份本项文档本地链接通过。按用户最终选择结束本轮代码交付，运行中追加未加载和外部环境限制如上保留。

## 2026-10-18 · WARMUP-TTS-RESILIENCE-074

- 状态：`verified（仓库全量回归与本机加载；真人长场次及弱网未验收）`。
- 目标：继续区分试音确认提示的 TTS 偶发超时、账户错误和本地编排故障；修复一次未发声的确认提示失败导致整场暂停，以及流式错误分类、调用预算与诊断缺口。
- 起点与证据：保留 HEAD `bde6e2c` 及 069–073 未提交修改，沿用已按顺序阅读的 11 份文档并复核相关协议。截图场次在 14:19:18 UTC 记录 12 秒 `tts_stream_open` 超时，尚无正式答案；14:37:06 UTC 同配置合成短句 HTTP 200，432ms 首音、469ms 完成，不能据此反推历史网络阶段或账户余额。首个受限沙箱探测在 TCP 阶段失败，获准联网后成功，不能混作产品故障。
- 计划：试音确认零音频失败保留显式确认/重试操作及所有权围栏；流式 TTS 增加安全供应商错误分类和网络阶段诊断；网关在总预算内为重试留出机会并记录取消/超时，保持真实取消传播；增加故障注入、并发边界和端到端合成回归。任何协议/字段变动同步文档，不外发真实候选内容、不自动确认或恢复历史面试。
- 验收与加载：完成代码及测试后统一回归；服务加载前检查媒体与 worker 活动，保留原运行环境和历史业务摘要。实际文件、验证结果、失败记录和剩余外部限制结束时补齐。
- 实际实现：试音确认的零音频准备/播放失败在精确 owner、control generation、capture、当前选择与校准版本下恢复显式操作；部分 PCM/不确定已发送仍走原暂停，不自动重播。session act 复用选择与送达回执，失败不形成送达事实或评分证据。两项已有 calibration 字段加入精简 SQL/内存投影，避免音频守卫读取整份历史。仅明确流式不支持才调用批量兼容路径。
- 网关与供应商：总期限内预留实际重试/可达备用目标时间；每次真实可重试失败计熔断，正常取消不计失败。显式任务等待约束开流和清理，同截止异常与迟到未交付流分别保留/回收。HTTP/SSE 业务错误安全分类，错误正文限 16KiB/0.5s；外层取消通过标准错误与安全阶段诊断保留已知拒绝，不回显正文。`Arrearage`、免费额度停用、TPS/TPM 限流分别保留，不把用量当剩余额度。
- 定向验证：provider 137 passed；网关/供应商/实际适配器组合及071共260 passed；试音/真实PCM证据/控制竞争/送达6文件111 passed；精简投影53 passed。数字有重叠，不相加。前端全量473 passed /26files，临时构建成功，保留既有大bundle提示。12份文档链接检查与diff检查通过。后端全量正在运行，结果另补。
- 实测：额外6次串行无个人信息短句均HTTP200，首音401–592ms；修复后的真实adapter+gateway内存验证536ms首音、540ms完成。只写去敏诊断到临时文件，未写业务库。只读预检当前API61736/worker61737，单worker、active/reserved0、LiveKit房间0；5174前端进程已退出，计划在通过验证后恢复正确入口，不将此混同历史TTS超时。
- 失败与恢复留痕：provider初轮5个取消诊断断言失败，发现Python3.9重建CancelledError丢自定义属性，改为异常/受控Task双路径；一个清理测试等待已安全停止并改有界等待。试音测试初始夹具将development_transcript放入持久命令被既有白名单拒绝，改用provider合成输入和真实PCM；未放宽生产协议。一次同名局部import插入错误导致NameError，已纠正并通过联合复验。网关一次测试文件名拼错导致0tests/exit4，修正命令后260通过。这些失败均无真实会话修改。
- 首轮后端全量：3714 passed /1 failed /8 skipped /237.22s，日志`/private/tmp/interviewer-074-backend-full.log`。唯一失败为旧067预算测试仍预期三次重试配置只调用一次；改为显式较高熔断阈值、四次共享0.12s总期限，074网关及组合与067合计74项通过。评审同时发现当前调用触发熔断覆盖最后超时原因会跳过timeout-only备用路由，追加修复及对应回归后再跑全量；不把旧测试修正当作该生产缺口已不存在。
- 固定提示实测：15:00:50 UTC以真实adapter+gateway合成现有固定试音确认文案，首音459ms、完整结束1479ms，成功审计，不发送真实转写、不写业务库；日志`/private/tmp/interviewer-074-live-confirmation-result.json`。
- 最终复验：熔断后保留当前调用真实原因、下一独立调用仍预检熔断，8文件联合294 passed /7.76s。最终完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` **3717 passed /8 skipped /238.86s**，日志`/private/tmp/interviewer-074-backend-final.log`。前端全量473 passed /26files与临时构建成功；本次前端源文件在该验证后未再变更，`git diff --check`和相关文档链接通过。当前实际表达路由只有1次重试和0个备用目标，未修改供应商/凭据/路由。
- 实际修改文件：`app/model_gateway/{gateway,tts_streaming}.py`、`app/providers/dashscope/{tts_streaming,tts_diagnostics}.py`、`app/persistence/realtime_state.py`、`app/services/{interview_agent,conversation_delivery}.py`；`app/web/src/features/candidate/{agent-experience,presentation}.js`及对应两份测试；新增`tests/test_{dashscope_tts_diagnostics,tts_budget_resilience,tts_pipeline,warmup_tts_resilience}_074.py`，更新`tests/test_audio_isolation_067.py`与`tests/test_realtime_session_projection_056.py`；`CONTEXT.md`、架构/接口/领域/供应商/数据库/问题/进度/路线图/操作日志和本项说明；`app/web/dist/index.html`及本轮构建资源。未改Prompt/Schema或新增DDL，保留069–073全部既有修改。
- 本机加载：再次只读确认单worker pong、active/reserved0、LiveKit房间0，正常TERM旧API61736和worker/Beat61737，旧worker子进程退出后启动API **69745**、worker/Beat **69746**，沿各自原运行环境，恢复Vite **69772**于5174。API health/ready与前端入口/代理均200；唯一worker pong且`interviewer.dispatch_due_work`已成功执行。加载前后全部面试核心业务摘要一致。脚本和日志`/private/tmp/interviewer-074-{reload,api,worker,frontend}.*`及`reload-result.json`保留源码哈希与诊断。
- 前端加载：临时构建复制到dist、原子替换首页，保留旧bundle，旧首页备份`/private/tmp/interviewer-074-previous-index.html`。当前`index-DikQW806.js`的实际HTTP哈希与构建一致；5174实际模块包含074提示，8000/5174均成功升级Agent WebSocket并拒绝无效合成票据。`/private/tmp/interviewer-074-loaded-verification.json`记录验证和前端加载后核心业务摘要仍一致；原场仍paused/0份正式答案。各新服务启动窗口未见Traceback/数据库锁/未读取任务异常/FFI Panic/OUTPUT_BACKPRESSURE。一次临时日志文件glob检索无匹配、一次派发成功统计使用旧任务名得0，均为只读检查，后者按实际`interviewer.dispatch_due_work`核对成功，无服务故障或数据写入。
- 未完成与恢复：历史超时没有网络子阶段证据，不能事后确定外部根因或宣称额度余额已查明；真实长场次、弱网、并发、部分播出恢复及跨进程字幕恢复仍未验收。原暂停场需既有人工恢复入口，不自动操作。本轮未提交/push；回退仅撤销074增量、空闲重载并可恢复备份静态入口，保留所有业务记录，无数据库回滚。

## 2026-10-18 · SUPPLEMENT-CONFIRMATION-STABILITY-073

- 状态：`verified（全量检查、受影响用例联合复验及本机加载；真人语音与外部供应商未验收）`。
- 目标：恢复普通作答后先询问是否补充的固定流程，查明概念提问后再次显示处理失败的独立原因，联合验证072/073而非只验局部函数。
- 起点：HEAD `bde6e2c`；保留069–072所有未提交改动，沿用已按顺序阅读的架构文档并复核领域/接口/证据/确认语义。当前场模型版本仍为v107/v76，证实运行服务未加载072。
- 只读证据：本场主答及追问均已提交，均无supplement_check行为；理解将普通技术尾句作为finish_answer依据，运行时直接调用提交。当前第三个交流轮次为概念提问，重复接话后出现理解预算阻断。它们必须分开诊断，不能将“已连接”视为处理成功。
- 计划：正常自然作答需先完成补充询问/新回复再提交；按钮明确结束、明确拒答/换题、独立整场退出按各自权限处理。最终服务提交门禁同步收紧，模型将任意原文句标记完成不能跳过产品确认。概念交流只处理新请求并核查已处理请求/过期预览的错误污染。增加真实本地合成链路回归、版本加载检查和失败留痕。
- 验证与现场边界：不外发真实候选材料、不重放真实录音、不自动重写本场答案或退出状态。先完成实现和验证；任何服务加载都先检查活动场次与媒体，避免丢失当前未提交回答。结果、文件、命令及加载情况结束时补齐。
- 实际实现：普通answer/off_topic结束由运行时及最终服务门禁共同要求补充确认；只有实际播放完成并切得服务端final后形成新回复边界，播放中/打断/超时/旧回复不能确认。继续补充保留原题完整证据和一次问答上下文。补充分类不确定时最多一次完整语义复核，允许在此阶段进入既有独立退出确认，普通回答不得递归分类。主答、追问和最终转写修订共用门禁，明确按钮/拒答/关闭话题/有实际内容澄清证明的待核实收尾仍各自检查。
- 提醒隔离：概念回应后的可选空闲提醒及补充无回复提醒使用独立12秒预算；失败消耗本次提醒资格，不记作回答理解失败、不续正式理解预算。输入过时、所有权失效、父任务取消继续传播；子推理取消沿既有stale结果路径处理。未修改Prompt或Schema版本，072版本改动保持。
- 首轮完整回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`为3579 passed /12 failed /8 skipped，280.14秒，日志`/private/tmp/interviewer-073-backend-full.log`。失败集中7份既有测试：旧普通finish直接提交预期、按打开STT流消耗回复而非按音频到达分配的夹具，以及最终转写修正后新增补充询问的切口次数。全量后仅修改对应测试，生产代码保持冻结；12个失败用例均已修正并纳入下述联合复验。
- 诊断/验证失败留痕：idle负例早期将子推理取消错当父任务取消，现拆开验证stale传播和实际父任务取消，9项通过。运行时初轮因旧继续补充后重问预期、误用不存在owner错误码、静音仅3秒及开流脚本夹具失败；修正后负责5文件84通过。只读加载预检初次误假设worker与API环境相同，断言在停止服务前中止；实际worker仅覆盖本地broker，已私下保留各自原配置，复核单worker pong、active/reserved0、LiveKit房间0。一次文档patch上下文未匹配，原子失败无写入，修正后成功；所有初轮日志保留。
- 最终联合验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_conversation_flow_064.py tests/test_declined_answer_integration.py tests/test_empty_confirmation_integration.py tests/test_exit_confirmation_flow_065.py tests/test_followup_rejection_integration.py tests/test_followup_repair_060.py tests/test_parallel_conversation_052.py tests/test_candidate_end_lifecycle_064.py tests/test_spoken_supplement_integration.py tests/test_planning_runtime_fences.py tests/test_off_topic_integration_068.py tests/test_formal_followup_056.py tests/test_supplement_confirmation_073.py tests/test_supplement_commit_gate_073.py tests/test_idle_recovery_073.py -q`为 **227 passed /44.38秒**，覆盖全部原失败及共享语音夹具消费者，日志`/private/tmp/interviewer-073-integrated-final.log`。因此3591项有效用例均有通过证据，8项外部环境测试仍跳过；没有将分批复验写成一次全量零失败。073新增55项，涵盖最终提交门禁、主答/追问、实际播完、新回复、打断、补充、退出接受/拒绝、提醒超时及失权/取消。文档11文件本地链接与`git diff --check`通过。本轮不改前端生产代码。
- 额外夹具修正留痕：064整文件首轮3失败只在旧current_speech最终断言，实际提交已成功；区分未接话回应的正文+新回复、已接话后的仅新回复，保留原纠错和技术证据断言后8通过。追问两文件43通过、退出/空音频三文件29通过、并行准备19通过，均与最终227项重叠，不累计。
- 实际修改：`app/services/{spoken_supplement,answer_endpoint,conversation_understanding}.py`；新增`tests/test_{supplement_confirmation,supplement_commit_gate,idle_recovery}_073.py`；更新`tests/test_{spoken_supplement,spoken_supplement_integration,formal_followup_056,conversation_handoff_059,off_topic_semantics_068,ambiguous_completion_072,off_topic_integration_068,parallel_conversation_052,conversation_flow_064,declined_answer_integration,empty_confirmation_integration,exit_confirmation_flow_065,followup_rejection_integration,followup_repair_060}.py`；`CONTEXT.md`与架构/接口/领域/检索评分/已知问题/开发进度/路线图/操作日志，以及新增073说明、072加载状态说明。未新增DDL或外部依赖。
- 本机加载：最终再次检查单worker pong、active/reserved0、LiveKit房间0，原API51677及worker43662沿各自实际环境正常TERM；旧worker/池/内嵌Beat退出后启动API **61736**、worker/Beat **61737**。`healthz`/`readyz`均200，唯一worker pong，Beat已连续派发且dispatch成功。072/073一起加载，前端继续使用070已验证版本。脚本`/private/tmp/interviewer-073-reload.py`、日志`/private/tmp/interviewer-073-{reload,api,worker}.log`与源码SHA256记录`/private/tmp/interviewer-073-reload-result.json`留存。加载前后全部面试核心业务摘要一致，本场仍in_progress、2份已提交答案；未自动重试、恢复、重交答案或退出。新服务启动窗口Traceback、数据库锁、未读取任务异常、FFI Panic及OUTPUT_BACKPRESSURE计数均0。
- 未完成与恢复：真人麦克风/回声/弱网及真实模型/TTS长场次仍待验证；本场后续另有一次接话问句内容合同拒绝，不能以本次本地用例声称外部模型永不出错。旧版本兼容保留，不标closed；未改写历史问答、未提交/push本轮改动。回退只撤销073增量并在空闲窗口加载，保留069–072、原数据和失败记录，不回滚数据库。

## 2026-10-18 · AMBIGUOUS-COMPLETION-RECOVERY-072

- 状态：`verified（仓库定向回归；活动场次期间未加载服务，真人及外部模型未验收）`。
- 目标：定位当前回答处理进入重试的原因，修复结束意图与内容歧义冲突的非提交恢复路径，补充针对性合同纠正指令与回归证据。
- 起点：HEAD `bde6e2c`；保留069/070/071既有未提交修改。沿用已按顺序阅读的11份架构文档并复核语义控制、内容澄清、实时失败恢复章节。
- 只读证据：本场控制连接正常、状态in_progress且尚无CandidateAnswer；本地模型及API日志先出现`completion_has_unresolved_ambiguity`，实际播送一次术语澄清后又出现`unresolved_clarification_required`和`unresolved_answer_contract_invalid`。TTS均成功；媒体持久分片保留，未提交字幕仍属采集中的转写。模型调用success仅表示网关层通过，不代表领域语义合同通过。
- 计划：在统一语义控制seam中验证可安全收敛的矛盾输出，只撤销提交权限并进入有原文依据的澄清，保留claims和证据校验；为相关错误补明确、可追踪版本的纠正指令。检查澄清证明与推测准备的上下文边界，不凭空补造送达证明或强行提交答案。用合成输入验证首次歧义、已送达后新回复、非法引文及正常结束的边界。
- 验证边界：不向外部模型发送真实候选材料、不重放真实录音、不自动操作本场重试/恢复/提交。当前场仍进行中，代码验证完成前及活动场次期间不重启API。运行服务是否加载与仓库测试结果分开记录。
- 实际实现：统一语义 seam 新增首次歧义收敛，仅接纳原语义校验的`completion_has_unresolved_ambiguity`；清空完成权限之前校验原basis原文、当前发言及target，随后再次完整schema/领域校验。保留claims/证据/能力点/歧义，结果只可具体澄清，不能提交。任何已有content_clarification上下文不进入该投影。补两种固定纠正指令；理解版本v86–97、组合v117–128、派生说明v3，领域/网关/Mock保留旧版本兼容。
- 预处理改进：针对性内容澄清开始时取消旧推测；`content_clarification_active`封装当前capture的澄清交流状态，后续不再发起缺少证明的预览。正式final仍携带严格来源绑定的实际送达/新回复；播放中、被打断、无新回复、前缀被改写均不能补造证明。
- 最终整合验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_ambiguous_completion_072.py tests/test_clarification_preview_072.py tests/test_understanding_prompt_versions_072.py tests/test_semantic_recovery_066.py tests/test_semantic_turn_policy.py tests/test_semantic_evidence_quality_067.py tests/test_off_topic_semantics_068.py tests/test_off_topic_integration_068.py tests/test_content_clarification_066.py tests/test_recovery_episode_066.py tests/test_stable_preview_preparation.py tests/test_prepared_turn_decision.py tests/test_prepared_decision_binding.py tests/test_answer_declined_understanding.py tests/test_declined_answer_integration.py tests/test_company_questions.py tests/test_followup_rejection_integration.py tests/test_formal_followup_056.py tests/test_latest_turn_intent_064.py tests/test_spoken_supplement_integration.py tests/test_supplement_contracts.py tests/test_prompt_governance.py tests/test_recognition_vocabulary.py tests/test_interview_agent_contracts.py tests/test_followup_expression_failure_071.py -q` **704 passed /26.78s**；日志`/private/tmp/interviewer-072-integrated-verified.log`。新增63项覆盖非提交投影、澄清预处理隔离与24个版本分支的新旧兼容/纠正合同。另有预览相邻8文件137通过及Prompt定向106通过，与整合集有重叠，不相加。
- 失败与恢复留痕：新语义用例修前12失败/8通过，修后及补负例22通过；并行版本同步中相邻测试进程读入尚未完成的领域Literal，121失败/82通过，版本完整后相同集205通过。整合首轮670通过/19失败，确认共用文件WAV测试夹具只模拟invoke却未声明不支持流式，071并行fail-fast先遇到未配置媒体；仅修夹具显式抛流式不支持，保留生产并行失败处理，最终704通过。各初轮日志保留。子任务另一次全局禁流式诊断不适配流式用例，95通过/1失败后主动中止；一次shell误用zsh只读变量status导致外层退出1，测试本身106通过，改独立变量后干净复验退出0。这些失败没有修改业务数据、供应商配置或运行进程。
- 修改文件：`app/core/prompt/{contracts,semantic_turn}.py`、`app/domain/interview_agent.py`、`app/model_gateway/gateway.py`、`app/providers/mock/provider.py`、`app/services/{conversation_understanding,answer_endpoint,spoken_supplement}.py`；新增`tests/test_{ambiguous_completion,clarification_preview,understanding_prompt_versions}_072.py`，17份既有版本断言测试及其中共享文件TTS夹具；`docs/{ambiguous-completion-recovery-072,architecture,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。未改本轮生产前端。
- 加载及未完成：最终只读查询目标仍为in_progress、0份答案，更新时间仍10:42:41 UTC。未重启API/worker/前端，运行API仍为071；072代码需空闲窗口加载，不能声称当前页面已经使用修复。未自动点击重试或修改真实面试，未调用外部模型或重放录音，未提交/push。真实模型是否正确消解音近术语、被外层取消调用的观测、终止错误与预算耗尽的诊断区分仍待验证；旧版本兼容保留，不标closed。无需迁移；回退仅撤销072增量并在空闲窗口加载，保留069–071及所有业务记录。

## 2026-10-18 · FOLLOWUP-SPEECH-TIMEOUT-DIAGNOSTICS-071

- 状态：`verified（仓库定向回归与本机API加载；外部TTS可靠性及真人恢复未验收）`。
- 目标：对齐追问报错的模型调用、会话事件与媒体日志，修正表达准备阶段错误被遮蔽和暂停状态同步遗漏。
- 起点：HEAD `bde6e2c`；保留069/070既有未提交修改。沿用已按规定顺序阅读的11份架构文档并复核实时表达、供应商与失败恢复章节。
- 只读证据：追问在10:05:00 UTC生成并选择，上一题答案已持久保存；LiveKit在同秒完成表达音轨发布；10:05:12模型调用记录为`tts.synthesize / interview_agent_expression / provider_timeout / 11999ms`，会话随后因`agent_expression_unavailable`暂停，记录为空消息的`TIMEOUTERROR`。同一时刻媒体发布者主动离开，后续DTLS关闭警告不能反推成最初原因。评分任务首次Schema长度校验失败、随后重试成功，上一题目前评分completed。
- 计划：保留并行TTS/媒体准备最早的明确错误，首音预算超时转换为结构化且带安全阶段信息的错误；表达准备失败持久暂停后立即发送权威快照。保留12秒总首音预算、冻结追问、原答案和播放身份保护；不把未播放追问自动判完成。
- 验证计划：使用合成TTS/媒体故障覆盖并行失败、首音超时、取消清理、流式不支持回退及暂停快照；无外部模型调用、真实音视频重放或真实场次恢复。本轮先完成代码与测试，运行服务是否加载单独记录。
- 诊断限制与失败留痕：API标准输出为匿名管道，任务无已挂接终端；使用数据库模型调用/会话记录及经权限批准的Docker只读日志交叉核对。首次媒体过滤误用原始会话ID而无命中，改用已有媒体绑定中的房间名后取得对应记录。只读`ps`被沙箱禁止、两次搜索含不存在文件/未匹配glob，均未产生业务数据修改。
- 实际修改：`_open_streamed_expression`在媒体先失败时立即保留原因；同截止竞态同步检查已完成子任务异常，保留供应商原错误及已明确的流式不支持回退；没有底层明确原因的超时转换为`AGENT_AUDIO_FIRST_PCM_TIMEOUT`，固定消息/服务端日志留下stage和budget。清理异常不覆盖原故障，新取消仍传播。`_select_act`持久暂停后立即发实时角色安全快照，旧turn/owner围栏保持；前端仅增加facade回归，无生产前端改动。
- 后端验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_followup_expression_failure_071.py tests/test_audio_isolation_067.py tests/test_streaming_expression_lifecycle.py tests/test_expression_stale_fence.py tests/test_approved_speech_output.py tests/test_tts_streaming.py -q`为 **188 passed**；另跑`tests/test_interview_agent_contracts.py -q`为 **82 passed**，合计 **270项且不重叠**。新增15项包含先失败、同截止、兼容回退、阶段诊断、清理取消、候选真实hub快照与主答保留；日志`/private/tmp/interviewer-071-expression-race-final.log`和`/private/tmp/interviewer-071-contracts.log`。
- 前端与跨端验证：前端目录`npm test -- --run src/features/candidate/agent-experience.test.js src/features/candidate/presentation.test.js`为 **164 passed /2 files**，新增真实facade收到fatal problem再收到暂停快照的回归，21秒内不重连/重开采集；日志`/private/tmp/interviewer-071-frontend-facade-final.log`。后端真实角色投影产生的5条纯合成事件经过JS `validateAgentEvent`与`OrderedAgentEventStream`均合法且apply，最后为paused；合成数据`/private/tmp/interviewer-071-synthetic-events.json`不含真实候选材料。`git diff --check`通过。
- 测试失败留痕：新故障用例修前7失败/3通过，同截止追加4项修前全失败；修后夹具曾把transient快照当持久事件、误用sequence字段，改为真实候选队列和session_sequence后通过。前端首轮夹具用了final，导致原有合同正确关闭采集而断言失败，改为采集中partial后通过；均保留失败日志，不改生产门禁去迁就测试。
- 本机加载：只读核对运行API唯一`INTERVIEWER_`覆盖项为`INTERVIEWER_LOCAL_MEDIA=true`；LiveKit仅目标已暂停场有候选连接，没有其它进行中媒体。通过`/private/tmp/interviewer-071-reload-api.py`正常TERM旧API **43624**，沿当前覆盖配置启动 **51677**，`healthz`/`readyz`均200。暂停原因、原答案、评分、当前追问、生命周期等业务摘要完全一致，仍paused/1份答案/evaluation completed。前端生产代码和worker未改，保留原进程及070静态资源。新API日志`/private/tmp/interviewer-071-api.log`，加载日志`/private/tmp/interviewer-071-reload.log`，没有自动恢复场次。
- 加载前探针失败与恢复：LiveKit Python API扩展未安装、一次错误模块导入均在联网前退出；尝试用配置文件作本机查询时发现与实际进程覆盖项不同，本地端点断言在联网前阻止访问，随后仅在内存中读取当前进程的相关设置并复用既有适配器。未打印/保存凭据，未改变配置文件、模型路由或供应商。
- 修改文件：`app/services/interview_agent.py`、`tests/test_followup_expression_failure_071.py`、`app/web/src/features/candidate/agent-experience.test.js`、`docs/{followup-speech-timeout-071,api-design,architecture,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`；保留069/070既有变更，本轮没有提交/push。
- 未完成与恢复：外部TTS慢响应根源、零PCM下冻结追问的有界自动恢复及真人恢复流程仍未验收；12秒首音预算与失败关闭保持，不保证供应商永不超时。原场继续走工作台人工恢复入口。无需迁移或数据库回滚；若回退，仅撤销071代码增量并在合适窗口重启API，保留所有业务记录和069/070变更。

## 2026-10-18 · VITE-AGENT-CHANNEL-AND-PAUSED-ENTRY-070

- 状态：`verified（前端回归、开发代理与本机静态加载；人工恢复后的真人流程未验收）`。
- 目标：修复通过5174开发页入场时控制连接超时并暂停，以及暂停后页头/摄像头仍宣称正在连接的问题。
- 起点：HEAD `bde6e2c`；保留已有069的`/healthz`代理和操作日志未提交变更。沿用已读架构文档，按项目规定顺序复核当前相关章节后修改；不改真实会话状态、答案、录音或评分。
- 只读证据：目标新场创建15秒后由候选运行错误上报暂停，0题0答案、试音pending、agent ticket未消费。LiveKit日志证明音视频轨道发布成功，随后客户端主动离开；实际Agent WebSocket路径在`/api/v1/interviews/.../agent`，Vite只给`/ws`开启升级转发。使用不存在的合成会话与无效票据，直连8000握手101，经过5174等待3秒超时，未进入真实面试。
- 计划：为`/api`启用WebSocket代理；统一启动失败/已暂停状态显示，初始已暂停场次不再次启动设备和控制连接；验证真实本地代理握手与前端回归。真实场次保持暂停，人工恢复仍走既有入口；不伪造播放或答题证据。
- 初步诊断限制：浏览器只读标签页工具超时，未刷新或操作用户页面；Docker只读访问经权限批准后仅输出相关时间段的连接元数据，没有读取或外发候选音视频。
- 实施：`/api`改为`{target: "http://127.0.0.1:8000", ws: true}`，保留069健康代理。Page与presentation共用停止状态，区分暂停确认中、服务端已暂停、未确认的本地故障；页头/摄像头占位/麦克风/入场提示一致。初始paused禁止Agent open及重复上报，并释放预检音视频；暂停快照保留原题和转写，致命故障优先显示。
- 验证：前端目录`npm test -- --run` **468 passed /26 files**，此前定向72 passed；`npm run build -- --outDir /private/tmp/interviewer-070-web-dist`成功，保留既有大bundle提示；`git diff --check`通过。日志`/private/tmp/interviewer-070-frontend-{targeted,full,build}.log`。合成无效票据经8000与5174均能升级WebSocket并收到`AGENT_TICKET_INVALID`；健康代理200，两端现有公共接口对相同无效请求均返回422 JSON，原HTTP代理保持。
- 本机加载：Vite自动加载新配置与源码，HTTP读取5174实际模块确认已含初始paused门禁与统一停止投影。同步发布8000静态首页与`index-CqMn5Lru.js`、`VrmAvatar-CyJjDe1K.js`，原bundle保留，旧首页备份`/private/tmp/interviewer-070-previous-index.html`；实际首页和JS哈希匹配通过验证的构建。无后端/worker源码改动，无需重启它们。
- 自动重载副作用：更新Vite代理触发既有标签页自动重载，在新paused入口门禁落盘前，旧页面再次申请并消费一张Agent ticket，控制连接已能连通，服务端以`INTERVIEW_NOT_IN_PROGRESS`拒绝媒体命令。随后前端热更新清理该连接；原会话`updated_at`与运行诊断元数据更新，因此首次“更新时间完全不变”的检查断言失败，不能声称整条记录未变。已如实保留诊断，没有回滚/删除记录。最终仍为原paused，生命周期仅created/started/paused三项、原暂停时间保持，0题0答案、0 evidence ownership/stream/command；未恢复、评分或形成候选作答。
- 其它失败与恢复：一次只读浏览器工具超时，一次Docker访问被沙箱拦截后经审批只读成功，一次路径glob无匹配；一次探针使用未定义`/api/v1/system/status`返回404，改用已有公共接口验证HTTP转发。均未改配置凭据或绕过权限。无新增依赖、API或数据库迁移；本轮未提交/push，保留069既有未提交改动。
- 修改文件：`app/web/vite.config.js`、`app/web/src/features/candidate/{Page.jsx,presentation.js,presentation.test.js,session-isolation.test.jsx}`、`app/web/dist/index.html`及上述两份新bundle、`docs/{architecture,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 未完成与恢复：原场次需在工作台“人工确认后恢复 AI”后重新进入；未主动恢复真实场次或执行真人作答验证，自动重载产生的连接已如实记录。若回退，仅撤销070增量并以本项首页备份恢复静态入口，保留069健康代理和业务记录；Vite重新加载配置，后端与worker不需回滚。

## 2026-10-18 · VITE-PREFLIGHT-HEALTH-PROXY-069

- 状态：`verified（开发代理、现有前端回归及本机 HTTP 采样；完整真人面试未验收）`。
- 目标：修复通过 Vite 开发端口访问邀请页时，设备预检错误显示“网络检测未通过 / 未能连接面试服务”的问题。
- 诊断：候选页初次预检和网络重测均请求同源 `/healthz`；Vite 仅代理 `/api` 与 `/ws`，在 `/web/` base 下直接返回 404。实际 `http://127.0.0.1:5174/healthz` 为 404，后端 `http://127.0.0.1:8000/healthz` 与 `/readyz` 均为 200。
- 计划：补齐开发服务器的 `/healthz` 代理，运行现有预检测试，并使用实际前端端口执行四次网络采样验证。此项仅调整开发代理，不新增 API、领域字段或迁移。
- 实际修改文件：`app/web/vite.config.js`、`docs/change-log.md`。新增 `/healthz` 到本机后端 8000 的代理，运行中的 Vite 已自动加载；原四次采样、超时、失败关闭和准入阈值保持。
- 验证命令与结果：前端目录 `npm test -- --run src/features/candidate/preflight.test.js src/features/candidate/InvitationPage.test.jsx` 为 **42 passed / 2 files**；`git diff --check` 通过。以 `node --input-type=module` 导入实际 `runCandidateNetworkCheck`，通过 `http://127.0.0.1:5174/healthz` 完成四次带 `preflight_probe` 参数的真实 HTTP 请求，每次均为 200 且 JSON 为 `{"status":"ok"}`；本次平均 RTT 7.2ms、相邻波动 6.3ms，仅表示本机 HTTP 采样。
- 未完成与恢复：未自动点击候选人进入面试或采集音视频，完整设备及真人面试仍由候选人重新检测；没有新增迁移或修改业务数据。恢复此配置可撤销新增代理行，Vite 自动重新加载；本次仅更改开发代理，无需重建生产 bundle 或重启后端与 worker。

## 2026-09-30 · OFF-TOPIC-AND-RECONNECT-068

- 状态：`verified（仓库回归与本机加载；模型最终、真人及生产未验收）`。
- 目标：修复用户新截图中清楚但跑题的回答触发处理失败、结束本题无法推进，以及题中重连被显示为首次进入面试的问题；保留067及独立退出二次确认。
- 起始状态：`dev`分支HEAD `76d226f`，工作区干净。按项目顺序复核架构、接口、领域、检索评分、供应商、存储、CONTEXT、已知问题、进度、路线图及日志；本机日志及目标会话只读调查。
- 初步证据：接话调用一次结构化校验失败，随后出现理解重试预算耗尽和接话超时；前端重连无条件设置connecting，题中映射为“正在进入面试”。清楚跑题与理解不清、不会、结束本题必须分开验证，不能只改提示语掩盖后端错误。
- 实施计划：先复现真实故障对应的合成语义/时限/重连用例，再修合同、恢复和状态投影，补端到端负例与相关回归，更新对应文档；完成时补实际文件、验证、服务加载与未完成事项。
- 边界：真实录音、转写、问答及评分保持；测试使用合成材料，不向外部模型发送真实候选内容。暂停/退出/结束回答权限不扩展，服务重启前检查活动候选与任务。本轮所有相关编辑均归本工作项。
- 实施：见[068说明](off-topic-and-reconnect-068.md)。清楚跑题成为合法理解类型；已说完沿既有权威语音提交及普通评分路径，未说完最多一次回题提示。模型声明的覆盖点与逐字来源均合法时，将矛盾的跑题标签归一为回答，不猜词、不补证据。稳定预览/正式final期限分开，已过期输入停止自动处理，恢复播报一次独立有界尝试；阻断推理时保留播放超时维护。前端保留题目/转写/业务阶段，快照握手与稳定窗口控制重连，最多6次重试。
- 阶段验证：前端全量460 passed/26 files，临时构建`/private/tmp/interviewer-068-web-dist`通过。语义及相邻571 passed（其中两份068新语义/实际音频提交与评分用例43 passed）；运行时320 passed，watchdog修复后相关63 passed、独立复验39 passed；root接话/诊断8 passed。`compileall`、`git diff --check`及11份文档链接检查通过。实现冻结后已启动最终后端全量，结果待补。
- 合成模型及审批：两轮经批准的虚构材料调用分别7/10、9/10。第一轮无关接话越过不提问约束、混合回答错标跑题；第二轮接话三项均通过，混合回答被严格合同拒绝。随后修正共同seam的有依据矛盾归一，新增双路径和六组负例通过。子任务进一步读取本机服务环境/模型凭据并外连验证被自动审批拒绝，已停止且未规避；最后归一仅完成本地验证，不宣称外部模型最终10/10。原日志保留`/private/tmp/interviewer-068-model-first.log`和`/private/tmp/interviewer-068-model-final.log`。
- 失败与修正：独立审查复现自动处理阻断后跳过播放ACK watchdog，现已修复且原反例证明超时回监听、失败只计一次、无新模型调用和无提交。定向测试早期旧Prompt版本及不含prompt_version的act事件断言已按真实协议修正。一次Chrome只读标签页枚举超时，未打开/刷新候选页；若干只读文件路径/SQL路径错误及一处文档patch未匹配，均未写业务数据，改正后完成检查。磁盘旧前端与源码构建相同，未取得旧标签页资源证据；不能将首次断开归因于缓存。
- 最终后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **3457 passed /8 skipped /1 failed /222.32s**。唯一失败为066测试fixture依据提前消耗的一次性标记跳过播放ACK的异步竞态；仅修fixture等待真实speaking，保留同词/新词/一次失败及不提交断言。完整恢复及预算相邻61 passed，原失败用例3个独立进程均通过；独立审核23 passed并确认仅partial变化不能绕过权威final。生产源码未改，全部3458个有效用例均有通过证据，不把分批结果写成一次零失败全量。日志`/private/tmp/interviewer-068-backend-final.log`、`/private/tmp/interviewer-068-recovery-fixture.log`；失败原日志保留。
- 最终前端与静态检查：`npm test -- --run` **460 passed /26 files**；`npm run build -- --outDir /private/tmp/interviewer-068-web-dist`成功，既有大bundle提示保留。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-068-pycache .venv/bin/python -m compileall -q app tests`、`git diff --check`及11份文档本地链接检查通过。只读文件扫描未发现新增凭据、候选原始材料、数据库或意外日志；所有新测试使用合成材料。
- 本机加载：两次确认LiveKit房间/候选0、worker active/reserved0后，正常TERM旧API46597/worker46598及其Beat，沿原配置启动API **66205**、worker/Beat **66206**。发布`app/web/dist/index.html`及`bundles/index-DGsP_VWc.js`、`bundles/VrmAvatar-4sKRX99M.js`，旧bundle保留，首页备份`/private/tmp/interviewer-068-previous-index.html`。日志`/private/tmp/interviewer-068-{restart,publish,verify}.log`。
- 加载验证：healthz/readyz均200、ready=true；实际首页和JS/CSS哈希与已验证构建一致；worker单节点pong，Beat启动后派发成功，active/reserved0。五场旧会话分别19/1/6/3/2份答案、核心状态与业务哈希相同；候选GET均200且私有字段未暴露。当前截图场次仍第三轮asking、in_progress、2份已保存答案、未结束。维护窗口API/worker新增Traceback、数据库锁、未读取Task异常、OUTPUT_BACKPRESSURE、FFI Panic、ReadyForRoomEventRequest均0。
- 未完成与恢复：未自动恢复/结束旧场次、补造回答或重评分；无依赖/DDL迁移。最后混合内容归一的外部模型复验受自动审批阻止；真人噪声、口音、回声、长轮次和弱网仍待实测，第一次断线的具体原因仍未确证。旧兼容读取保持，不标closed。需回退时撤销068源码并在空闲时重启，用本项备份恢复首页；保留业务库和原始媒体，不通过回滚数据库覆盖历史。
- 提交前核对：`git fetch origin`成功，`HEAD...origin/dev`为0/0；60个变更文件（含3个发布静态文件）无新增凭据模式命中，diff检查通过。首次文件清单脚本匹配到多个同日标题而断言退出，未写文件，改为工作项完整ID后成功；未删除历史记录。

- Changed files (068): `CONTEXT.md`, `app/core/prompt/candidate_exit.py`, `app/core/prompt/contracts.py`, `app/core/prompt/conversation_reception.py`, `app/core/prompt/semantic_turn.py`, `app/core/speech_diagnostics.py`, `app/domain/interview_agent.py`, `app/model_gateway/gateway.py`, `app/providers/mock/provider.py`, `app/services/answer_endpoint.py`, `app/services/conversation_understanding.py`, `app/services/interviews.py`, `app/services/spoken_supplement.py`, `app/web/dist/bundles/VrmAvatar-4sKRX99M.js`, `app/web/dist/bundles/index-DGsP_VWc.js`, `app/web/dist/index.html`, `app/web/src/features/candidate/Page.jsx`, `app/web/src/features/candidate/agent-experience.js`, `app/web/src/features/candidate/agent-experience.test.js`, `app/web/src/features/candidate/presentation.js`, `app/web/src/features/candidate/session-isolation.test.jsx`, `docs/api-design.md`, `docs/architecture.md`, `docs/change-log.md`, `docs/development-progress.md`, `docs/domain-model.md`, `docs/implementation-roadmap.md`, `docs/known-issues-and-remediation.md`, `docs/model-provider-plugins.md`, `docs/off-topic-and-reconnect-068.md`, `docs/retrieval-and-evaluation.md`, `tests/test_answer_declined_understanding.py`, `tests/test_company_question_integration.py`, `tests/test_company_questions.py`, `tests/test_conversation_delivery_067.py`, `tests/test_conversation_reception.py`, `tests/test_conversation_reception_integration.py`, `tests/test_declined_answer_integration.py`, `tests/test_dialogue_focus_055.py`, `tests/test_followup_rejection_integration.py`, `tests/test_formal_followup_056.py`, `tests/test_interaction_budget_067.py`, `tests/test_interview_agent_contracts.py`, `tests/test_latest_turn_intent_064.py`, `tests/test_off_topic_integration_068.py`, `tests/test_off_topic_semantics_068.py`, `tests/test_prepared_decision_binding.py`, `tests/test_prepared_turn_decision.py`, `tests/test_prompt_governance.py`, `tests/test_reception_context_062.py`, `tests/test_reception_off_topic_068.py`, `tests/test_recognition_vocabulary.py`, `tests/test_recovery_episode_066.py`, `tests/test_runtime_budget_068.py`, `tests/test_semantic_evidence_quality_067.py`, `tests/test_semantic_turn_policy.py`, `tests/test_speech_budget_diagnostics.py`, `tests/test_spoken_supplement_integration.py`, `tests/test_stable_preview_preparation.py`, `tests/test_supplement_contracts.py`.

## 2026-09-28 · AGENT-CHAIN-IMPROVEMENTS-067

- 状态：`verified（仓库回归、合成真实模型、本机加载；真人语音及生产未验收）`。
- 用户要求：先将当前代码提交并推送到GitHub，再开始解决本轮Agent链路审查列出的改进项。
- 基线范围：当前dev分支已有057–066交互修复、退出独立确认、前端及文档/测试，并包含现有题库停止试听改动；整体作为实施前基线保留。起始HEAD为`1dda624`，远端为`origin`，基线推送成功前不修改业务实现。
- 计划：推送基线后分阶段处理验收/指标、控制证据质量和对话送达、状态与合同收拢、时延/异步归档、规划上下文、耐久确认凭证及S2S隔离。按实际代码证据设计接口并补回归，同步相关文档；实际提交、文件、验证与未完成事项在本项补齐。
- 边界：保持服务端权威语音、逐次播放授权、独立退出确认、原始录音/回答与人工最终决策。环境凭据、本地数据库及私有媒体继续排除在Git外。当前基线`git diff --check`通过；本次审查复现使用纯内存合成材料，不改真实会话。
- 基线提交前检查：`git fetch origin`成功，HEAD与origin/dev无领先/落后；159个变更文件扫描未发现私钥/访问令牌模式或私有数据路径。现有题库试听相邻`npm test -- --run src/App.test.jsx` **38 passed**，日志`/private/tmp/interviewer-067-baseline-frontend.log`；057–066完整验证证据沿用下方历史工作项。本轮全量回归在改进完成后执行。
- 基线已完成：提交`2f3cdff`（159 files，13377 insertions，632 deletions），`git push origin dev`成功，远端由`1dda624`推进到`2f3cdff`。从此提交开始实施067，后续差异可独立审查和回退。
- 实施详情：见[067说明](agent-chain-improvements-067.md)。真实模型使用7个虚构材料场景，7/7通过；路由只读复制到内存，未发真实候选材料或写业务数据。前端447通过，临时构建成功且index与现有dist一致；compileall及diff检查通过。最终后端全量3401 passed、8 skipped，216.54秒。
- 失败与恢复留痕：首轮全量3360 passed/19 failed/8 skipped，发现粗ASR分段时间交叠使来源映射丢弃，保留原064的8条真实多轮断言修复后全通过；其余更新新派生控制、版本与工具投影断言并保留禁止越权负例。独立复核又修复零首音本地speaking/游标、同播放重试送达、旧controller预生成结束回执和mock专家重复题ID。一次测试命令引用不存在的speculative文件，未运行测试，已改正确文件并通过；一次compileall默认缓存被沙箱拒写，改临时缓存成功。一次中文stdin文档脚本编码失败，未写文件，改apply_patch与纯ASCII脚本。进程只读预检首轮缓冲区尺寸不符合macOS要求（EINVAL），按kern.argmax修正后通过，未停止服务。
- 验证命令：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q`（日志`/private/tmp/interviewer-067-backend-final.log`）；前端目录`npm test -- --run`、`npm run build -- --outDir /private/tmp/interviewer-067-web-dist`（日志`/private/tmp/interviewer-067-{frontend,build}.log`）；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-067-pycache .venv/bin/python -m compileall -q app tests`；`git diff --check`。合成真实模型结果`/private/tmp/interviewer-067-model-results.json`，单场语义调用约4.3–6.8秒，不等于生产时延SLA。
- 本机加载：重启前再次确认LiveKit房间/候选人0、worker active/reserved0，正常TERM旧API98728与worker98729及其Beat，沿用原配置启动API46597、worker/Beat46598。前端首页`index-CRBIjWnO.js`/`index-CBS0pomz.css`与本轮临时构建一致，无需覆盖。脚本日志`/private/tmp/interviewer-067-{restart,verify}.log`及重启结果`/private/tmp/interviewer-067-restart-result.json`。
- 加载验证：healthz/readyz均200且ready=true，worker单节点pong且Beat就绪后派发成功、active/reserved0；新聚合指标、退出确认、恢复和人工核验界面均已加载。四场截图会话分别保留19/1/6/3份回答，状态与核心业务hash不变，候选GET均200且私有答案字段未暴露。维护窗口API/worker新增Traceback、数据库锁、未读取task异常、OUTPUT_BACKPRESSURE、FFI Panic、ReadyForRoomEventRequest均0。
- 提交前检查：82个候选变更文件（71修改、11新增）独立只读扫描通过，未发现新增凭据、私钥或私有数据引用，无数据库、录音、截图、日志等意外文件；`git diff HEAD --check`通过。全部实现冻结后才执行最终全量，随后仅补文档验收结果。
- 未完成与恢复：真人噪声/口音/回声、弱网、长场次、数据库并发成本及生产依赖仍待实测，8项环境测试跳过；兼容读取保留，未标closed。独立退出二次确认与人工最终决策保持；没有改写或自动恢复真实旧面试。无新增依赖或DDL迁移。需回退时仅撤销本次067提交，保留已推送基线`2f3cdff`，空闲时重启服务；不能回滚业务库或覆盖历史问答。

- Changed files (067): `CONTEXT.md`, `app/adapters/livekit_audio_output.py`, `app/api/routers/system.py`, `app/core/interaction_budget.py`, `app/core/interview_agent_metrics.py`, `app/core/prompt/contracts.py`, `app/core/prompt/conversation_reception.py`, `app/core/prompt/interviewer_supervisor.py`, `app/core/prompt/semantic_turn.py`, `app/core/prompt/understanding_references.py`, `app/domain/interview_agent.py`, `app/domain/speech_quality.py`, `app/model_gateway/dialogue.py`, `app/model_gateway/gateway.py`, `app/model_gateway/tts_streaming.py`, `app/operations/interview_agent_acceptance.py`, `app/providers/mock/provider.py`, `app/services/agent_expression_audio.py`, `app/services/answer_endpoint.py`, `app/services/approved_speech_output.py`, `app/services/candidate_exit_confirmation.py`, `app/services/content_clarification.py`, `app/services/conversation_delivery.py`, `app/services/conversation_input_context.py`, `app/services/conversation_understanding.py`, `app/services/dialogue_history.py`, `app/services/interview_agent.py`, `app/services/interview_evidence.py`, `app/services/interviewer_supervisor/context.py`, `app/services/interviewer_supervisor/service.py`, `app/services/interviewer_supervisor/tools.py`, `app/services/interviews.py`, `app/services/speculative_turn_preparation.py`, `app/services/spoken_supplement.py`, `app/services/streaming_stt.py`, `docs/agent-chain-improvements-067.md`, `docs/api-design.md`, `docs/architecture.md`, `docs/change-log.md`, `docs/database-and-vector-storage.md`, `docs/development-progress.md`, `docs/domain-model.md`, `docs/implementation-roadmap.md`, `docs/known-issues-and-remediation.md`, `docs/model-provider-plugins.md`, `docs/retrieval-and-evaluation.md`, `tests/test_agent_acceptance_067.py`, `tests/test_agent_metrics_067.py`, `tests/test_answer_declined_understanding.py`, `tests/test_audio_isolation_067.py`, `tests/test_company_question_integration.py`, `tests/test_company_questions.py`, `tests/test_conversation_delivery_067.py`, `tests/test_conversation_handoff_059.py`, `tests/test_conversation_reception.py`, `tests/test_conversation_reception_integration.py`, `tests/test_conversation_source_boundary_067.py`, `tests/test_declined_answer_integration.py`, `tests/test_dialogue_focus_055.py`, `tests/test_dialogue_history_055.py`, `tests/test_followup_rejection_integration.py`, `tests/test_formal_followup_056.py`, `tests/test_historical_evidence_060.py`, `tests/test_interaction_budget_067.py`, `tests/test_interview_agent_acceptance.py`, `tests/test_interview_agent_contracts.py`, `tests/test_interviewer_supervisor.py`, `tests/test_latest_turn_intent_064.py`, `tests/test_optional_interview_context.py`, `tests/test_planning_context_067.py`, `tests/test_prepared_decision_binding.py`, `tests/test_prepared_turn_decision.py`, `tests/test_prompt_governance.py`, `tests/test_reception_context_062.py`, `tests/test_recognition_vocabulary.py`, `tests/test_semantic_evidence_quality_067.py`, `tests/test_semantic_turn_policy.py`, `tests/test_spoken_supplement_integration.py`, `tests/test_stable_preview_preparation.py`, `tests/test_streaming_expression_lifecycle.py`, `tests/test_supervisor_version_060.py`, `tests/test_supplement_contracts.py`.

## 2026-09-22 · LOCAL-RESTART-063

- 状态：`verified（本机进程与健康检查）`。
- 目标：按用户要求重新构建并加载最新前端，重启本机 API 与 worker，检查服务健康和任务/会话状态。
- 实际修改文件：仅本操作日志；前端以 `npm run build -- --outDir /private/tmp/interviewer-063-dist` 重建，输出 bundle 与已发布 `app/web/dist` 的 index 引用一致，无需再次覆盖。未改业务源码和数据。
- 重启：旧 API 已不在监听，旧 worker PID 10602 仍运行；重启前检查 LiveKit 房间/候选人 0、worker active/reserved 0。复用旧 worker 环境，正常 TERM 后启动 API PID 31838、worker/Beat PID 31839；媒体容器保持运行。过程记录 `/private/tmp/interviewer-063-restart-result.json`。
- 验证：`/healthz` HTTP 200，`/readyz` HTTP 200 且 `ready=true`（数据库、Redis、本地媒体及服务端收音探针通过）；首页引用 `index-CrIrnajx.js` 和 `index-CBS0pomz.css`，与最新构建一致；Celery `inspect ping` 一个节点回应 pong，两个进程均在运行。
- 未完成事项：本次只做本机服务重启与健康检查，未进行真人面试或生产环境验收；保留此前未提交源码、候选人数据与原有暂停状态。

## 记录格式

每个工作项必须包含：日期、ID、目标、关联问题、状态、实际修改文件、验证命令与结果、未完成事项或恢复说明。

## 2026-09-24 · INTERACTION-RECOVERY-066

- 状态：`verified（仓库回归、合成模型、本机加载；真人语音及生产未验收）`。
- 目标：修复第三题真人反馈暴露的理解校验失败、重复道歉、已说完被阻塞、碎片转写处理和候选页状态冲突；保留065独立退出确认与全部已有成果。
- 已有证据：会话`iv_f387adcfaa944657`第三题16条理解校验拒绝，固定兜底道歉同一act被选择4次；重复完成语使全文预算与道歉identity同时更新。错误分型未被运行时处理，转写存在碎片但原话误识别尚需声学来源核实。
- 边界：原始回答/录音/评分只读，不把真实候选材料发送给外部模型，不绕过权威语音、统一结果校验或退出二次确认；不通过放宽证据约束强行提交。首次修改前保留`/private/tmp/interviewer-066-before.patch`及未跟踪源码归档`/private/tmp/interviewer-066-untracked-before.tar`。
- 计划：并行处理合同/诊断、失败恢复和UI一致性，核查STT流恢复；补碎片回答与重复收尾的端到端反例，跑必要回归，再在本机空闲时构建和加载。实际修改、失败过程与结果结束时补齐。
- 阶段实现：同流全部有身份的临时假设明确撤回可可信empty完成；旧失败流的未确认词继续保护，recovery_origin逐层透传，VAD不重置同音频起点预算。理解失败区分是否可重试，连续故障只播报一次；新词只获得一次新请求，不续三次自动预算。页面恢复状态持久化并严格绑定turn/capture。新增content_clarification实际送达证明及answer_unresolved，退出依然独立确认；未核实题目score=null、报告人工复核，人工核验新修订后重评可恢复分数。
- 阶段验证：前端全量`npm test -- --run` **444 passed /26 files**；临时目录构建通过，既有大bundle提示保留。STT相邻230 passed；新增恢复预算8 passed、内容澄清36 passed、后端恢复投影等44 passed；运行时重验100 passed。评分新增18项及worker→报告→核验→重评闭环通过。
- 失败保留：首次写文档的`python`命令不存在，随后stdin编码失败，均未写文件，改用apply_patch。STT早期测试wrong_id预期错误、命令文件名拼写错误及旧empty断言已修正。运行时首轮12 failed/159 passed，其中旧版本fixture未走真实结束语分支、旧VAD重置预期及缺retryable字段导致假对象异常，修正测试输入/新预算期望后定向100 passed；真实透传曾漏StreamingInterviewSTT层，已补充并由真实链路origin测试验证。
- 合成模型阶段：首轮5/7，发现not_finished保留旧疑点被误报引文错误，以及退出语义抹掉技术歧义；增加可等待/可退出的合法组合与针对性原因码。第二轮6/7，未核实退出被填partial却无claims，再次定位为Prompt字段含义冲突，正在修正；两轮日志保留`/private/tmp/interviewer-066-model*.log`。仅合成材料外部调用，诊断原始响应也只含合成内容；不写真实业务记录。
- 后续模型审查：退出合成17/17通过；新七场景阶段仍6/7，原始合成响应证实首次碎片场景准确保留finish_answer意图、动作clarify、低置信度/具体疑点且无completion_basis，旧结束合同却强迫其拥有提交依据。将该严格非提交组合合法化，保持动作必须clarify、结束权限为空；运行时仍仅澄清、不提交。提示同步明确当前是否存在实际送达证明与字段组合；所有失败记录保留，不以重复重跑掩盖矛盾。
- 全量首轮：**3192 passed /8 skipped /8 failed /204.06s**，`/private/tmp/interviewer-066-backend.log`。七项为旧测试用dict/不完整假对象缺retryable或沿旧预算假定，改用正式UnderstandingProblem并按新预算验证；一项旧v8/v9版本断言更新。相邻71 passed；新增实际未核实退出集成首次确认无提交，新回复才结束并回收录音，连旧准备测试65 passed。前端报告明确人工核验后最终**447 passed /26 files**及新构建通过。
- 最终合成模型：最新首次理解即通过**7/7**，无校验重试，覆盖首次碎片+完成语、已送达澄清后完成、部分可靠claims保留、继续等待、纠正后正常完成、思考后明确不会、碎片后要求退出。`/private/tmp/interviewer-066-model-final-acceptance.log`；独立退出回归**17/17**，`/private/tmp/interviewer-066-exit-model-regression.log`。Python/当前合同专项290 passed，文档11份本地相对链接无缺失。前置维护检查LiveKit房间/候选0、worker active/reserved0，API60119/worker60120归属一致。
- 实际源码：新增`app/services/content_clarification.py`；修改`app/core/prompt/{contracts,semantic_turn,understanding_references,conversation_reception,candidate_exit}.py`、`app/domain/{interview_agent,scoring_quality}.py`、`app/model_gateway/{gateway,streaming}.py`、`app/providers/{mock,dashscope}/provider.py`、`app/services/{answer_endpoint,spoken_supplement,continuous_stt,streaming_stt,interview_evidence,livekit_evidence_ingress,interview_agent,interviews,conversation_understanding,evaluation,reports,review}.py`及`app/services/interviewer_supervisor/service.py`。前端为`app/web/src/features/candidate/{Page.jsx,presentation.js,agent-experience.js}`和`features/interviews/{agent-event-runtime.js,Review.jsx}`及测试。
- 实际测试/文档：新增`tests/test_{stt_retraction_completion,recovery_episode,content_clarification,recovery_projection,semantic_recovery,unresolved_answer_evaluation}_066.py`；同步既有准备、确认、接话、语义、评分、转写空结果、重连投影、Prompt版本测试。新增`docs/interaction-recovery-066.md`，同步架构/API/领域/检索评分/供应商/CONTEXT/已知问题/进度/路线图及本日志。未新增依赖或DDL迁移，旧兼容读取保留。
- 最终后端验收：生产源码冻结后的全量`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **3232 passed /8 skipped /1 failed /201.85s**；唯一失败为057测试仍断言旧字段描述的完整短句，新的描述已明确“含义确定且有原文依据”“未命中能力点也可partial”“none不代表不会或拒答”。仅更新该旧文字断言，不再改生产源码，`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_conversation_semantics_057.py`整文件**23 passed /0.28s**（`/private/tmp/interviewer-066-last-assertion.log`）。全套3233个有效用例均已取得通过证据；不将分批复验写成一次零失败全量。全量原日志`/private/tmp/interviewer-066-backend-final.log`保留。
- 最终前端验收：`npm test -- --run` **447 passed /26 files**，`npm run build -- --outDir /private/tmp/interviewer-066-web-dist`通过，既有大bundle提示保留。日志`/private/tmp/interviewer-066-frontend-final.log`、`/private/tmp/interviewer-066-build-final.log`。手动重试ACK补验覆盖故障提示实际播完前不能开放话权；新增恢复预算文件最终9项通过。
- 本机加载：前置及重启前核对LiveKit房间/候选人0、worker active/reserved0和进程归属；正常TERM旧API60119、worker60120及其Beat，沿用原配置，启动API **98728**、worker/Beat **98729**。发布`app/web/dist/index.html`、`bundles/index-BQO4VWNg.js`、`bundles/index-CBS0pomz.css`、`bundles/VrmAvatar-C1nU2zqY.js`；旧bundle保留，旧首页备份`/private/tmp/interviewer-066-previous-index.html`。过程日志`/private/tmp/interviewer-066-{preflight,restart,publish,verify}.log`。
- 加载验证：healthz/readyz均200且ready=true；首页、JS/CSS哈希与验收构建相同，恢复/人工核验/独立退出确认文案均已加载；worker单节点pong，Beat就绪后派发成功，active/reserved0。四场截图对应会话分别保留19/1/6/3份回答，状态及核心业务哈希完全相同；候选GET均200，私有答案字段未暴露。维护窗口API/worker新增Traceback、数据库锁、未读取task异常、OUTPUT_BACKPRESSURE、FFI Panic、ReadyForRoomEventRequest均0。
- 未完成与恢复：真人噪声、口音、回声、弱网、长场次及外部生产依赖仍待实际验收，8项环境测试跳过；不将合成用例通过承诺为所有语义/识别准确。兼容读取保留，未标closed。未自动淘汰、改写真实问答或评分、恢复或结束旧场、发邀请、提交或push。无新依赖或DDL迁移。回退须保留065及之前未提交成果，使用本轮修改前patch/未跟踪归档定位066差异，撤销对应源码后空闲重启，首页可恢复本轮备份；不能reset仓库或回滚整个业务库。

## 2026-09-23 · CANDIDATE-EXIT-CONFIRMATION-065

- 状态：`verified（仓库回归、合成真实模型、本机加载；真人语音及生产未验收）`。
- 目标：按用户纠正，将首次明确结束请求改为独立二次确认，确认后结束整场；改口继续保留当前回答与录音，静音/不确定不视为确认。提前退出客观记录为候选人确认主动退出并供人工复核，不自动作出录用或淘汰决定。
- 边界：保留064及全部既有未提交成果；Prompt集中版本化、响应统一校验、仅权威新语音可确认，确认问题的实际播放与当前owner/输入代次绑定。真实历史数据不重写，不自动恢复或结束旧面试。首次修改前差异保存`/private/tmp/interviewer-065-before.patch`。
- 计划：先核对既有补充确认和提交路径，再补独立退出确认及绕过保护、反例/流程测试，更新文档，验证后在本机空闲时加载。实际文件、失败与结果在本项补齐。
- 阶段证据：集中合同/来源/报告95 passed，回复与既有语义相邻203 passed，旧收尾流程适配18 passed；真实配置模型17/17合成场景通过，未外发真实候选材料、未写业务审计。前端首轮421 passed/26 files及构建通过，后续退出确认状态提示仍需最终复跑。早期生命周期10项失败源于选中act绑定，修复后通过；合同首轮一项测试来源分段错误已修正，失败日志保留。审计另发现播放前/期间的肯定可能被播放完成借用，正在补输入边界及回归，不带缺陷发布。
- 播放边界复核：确认问题实际播完后，在收音闸门仍关闭时取得权威final切片，把提问前/期间的语句归入旧范围；之后必须有新语音。独立重放早到肯定、按钮与噪声均0提交；新确认才提交，stale owner/取消异常也无许可。新增专用退出UI状态和重连安全投影。前端最终425 passed/26 files，构建成功（原有大bundle提示保留），真实模型按实际完成标志再跑17/17。
- 首轮全量：3023 passed/8 skipped/19 failed/242.01s，日志`/private/tmp/interviewer-065-backend.log`保留。12项退出测试仍使用调整中的脚本流/播放时序，实际播放切片增加了识别流，已等待问题act及恢复stream后报告drained；1项旧supervisor测试缺独立确认收据，改为有/无收据双断言；6项旧062轻量fixture缺_stopped，补真实字段而不改生产或降低断言。最新真实退出入口2 passed、组合35 passed、收尾18 passed；正在定向重验全部失败文件后重跑全量。

- 实际源码：新增`app/core/prompt/candidate_exit.py`、`app/services/candidate_exit_confirmation.py`；修改`app/core/prompt/contracts.py`、`app/domain/{interview_agent,scoring_quality}.py`、`app/model_gateway/gateway.py`、`app/providers/mock/provider.py`，以及`app/services/{conversation_understanding,interviews,interview_agent,spoken_supplement,answer_endpoint,livekit_evidence_ingress,interview_evidence}.py`、`app/services/interviewer_supervisor/service.py`。前端修改`candidate/{Page.jsx,agent-experience.js}`、`interviews/{Review.jsx,agent-event-runtime.js}`（均位于`app/web/src/features/`）及其测试、candidate/session-isolation.test.jsx。
- 实际测试与文档：新增`tests/test_exit_confirmation_{commit,contract,flow}_065.py`、`tests/test_exit_report_notice_065.py`；同步`test_semantic_turn_policy.py`、`test_declined_answer_integration.py`、`test_conversation_flow_064.py`、`test_candidate_end_lifecycle_064.py`、`test_interviewer_supervisor.py`、`test_reception_cursor_062.py`。更新架构/API/领域/检索评分/供应商/CONTEXT/已知问题/进度/路线图/本日志与064说明，新增`docs/candidate-exit-confirmation-065.md`。
- 最终验证：全部首轮失败所在文件重验**62 passed**；冻结后`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **3045 passed / 8 skipped / 208.35s**，`/private/tmp/interviewer-065-backend-final.log`。`npm test -- --run` **425 passed / 26 files**，`npm run build -- --outDir /private/tmp/interviewer-065-dist`通过；真实模型**17/17**，独立播放竞态审查通过。Python语法解析、diff检查及11份文档相对链接检查通过。旧062轻量fixture只补真实生命周期字段，所有原断言保留。首次UI定向失败为新测试调用不存在的封装方法，改经真实capture回调后152 passed；失败日志保留。
- 本机加载：前置及重启时再次检查LiveKit房间/候选人0、worker active/reserved0。正常TERM旧API48632、worker48633及其Beat，保留原配置，启动API **60119**、worker/Beat **60120**。发布`app/web/dist/index.html`、`bundles/index-_D802nuj.js`、`bundles/VrmAvatar-caIw9BOi.js`，旧bundle保留，旧首页备份`/private/tmp/interviewer-065-previous-index.html`。过程日志`/private/tmp/interviewer-065-{preflight,restart,publish,verify}.log`。
- 加载验证：healthz/readyz均200且ready=true；首页与验证构建完全一致、JS/CSS哈希一致，退出确认专用文案已加载；worker单节点pong、Beat派发成功、active/reserved0。原三场19/1/6份问答、状态与核心哈希完全相同；候选权限GET200且私有答案字段未暴露。维护窗口API/worker新增Traceback、FFI Panic、ReadyForRoomEventRequest、数据库锁、未读取task异常、OUTPUT_BACKPRESSURE均0。
- 未完成与恢复：真人听感、长场次、口音/弱网和外部生产依赖仍待验收，8项外部环境测试skip；不将17条合成表达保证为所有语义准确。未自动淘汰、修改旧问答/评分、恢复或结束旧面试、发邀请、提交或push。无新增依赖/DDL迁移。回退须保留064及此前未提交改动，撤销065源码后在空闲时重启，首页使用本轮备份；不能回滚业务库覆盖历史。

## 2026-09-23 · CONVERSATION-INTENT-AND-END-064

- 状态：`verified（仓库回归、合成模型/媒体、本机加载；真人长轮次及生产未验收）`。
- 目标：根据用户提供的三场截图和本机记录，修复已解释概念被重复澄清、不会/讲完被识别为继续思考，以及明确不愿继续时无法自主结束面试的问题。
- 关联：059–062交流识别和记忆修复的真人反馈；调查截图会话 `iv_01ba46b5cfa0427d`、`iv_077e71ada895445c`、`iv_c64fc81cbb35477c`。
- 边界：保留所有现有未提交成果，真实记录仅本机只读分析；新增Prompt集中版本化与合同测试；使用合成场景验证，不重写历史答案/评分。先定位再修复，实际文件、失败过程、验证和本机加载结果完成后补齐。
- 已核实原因：RAGFlow/Dify/OOM已获解释后，完整理解又引用累计转写中的旧问句；重复等待也来自完整理解，不能只修快速接话。实际“我们结束面试吧”被判decline_topic并继续问；图3同时命中LiveKit ReadyForRoomEventRequest超时、FFI Panic及API退出。原native connect被取消会遗留握手，改为调用方停止等待、原生握手成功后回收，迟到结果不能发布/订阅。
- 实施：完整证据和当前发言分离；有来源验证的六组交流投影覆盖preview、prepare和直接权威转写，绑定回执/act/事件指纹；明确控制句冲突只触发一次模型纠正，不由规则授予结束；完整原文范围检查引用/改口，技术claims保留。结束回执/owner/录制回收独立于评分，刷新/重连与迟到媒体初始化不得重新收音；历史转写标签改为完整发言。
- 阶段验证与失败：第一次全量2890 passed/8 skipped/12 failed，4项optional-context旧fixture用字符串冒充ConversationUtterance，已改合法领域输入；其余8项语义fixture在真实口头“没有补充了”后仍伪造answering，正修正合成模型语义而保留提交/录音断言。真实模型首轮16/17，引用案例里的“结束面试”误终止；补完整原文的引号位置校验与技术内容保留指令后复跑17/17。记录`/private/tmp/interviewer-064-{backend,model,model-final}.log`，失败记录均保留。
- 本机媒体合成：三个独立无音视频Room连接在握手期间撤销等待，18秒后进程仍存活、pending_cleanup=0，`/private/tmp/interviewer-064-native-connect.log`。此测试没有候选音视频。阶段前端410 passed/26 files及构建成功；后续完成刷新修复后须重新全量与构建。
- 维护预检：API31838/worker31839归属一致，LiveKit房间1/候选0、active/reserved任务0。三场核心状态/原问答哈希已只读记录，19/1/6份原答案；未恢复、结束或重评分旧会话。待最终验证后复查并加载新版本。
- 补充审查：结束刷新投影首次前端1项失败，原因是严格session.snapshot白名单未同步新增completion，已同步验证同场ID/字段边界；又补旧ticket的ready/media命令禁止重开、断线期间结束后ticket409只读获取回执，以及播放器同步释放异常也必须停止设备。定向后端35 passed、前端148 passed。最终前端`npm test -- --run` **419 passed / 26 files**；`npm run build -- --outDir /private/tmp/interviewer-064-dist`成功，保留既存大bundle提示。第二轮后端2928 passed / 8 skipped / 1 failed；旧062可选历史写失败测试在已记录失败但游标未处理完时提前断言，正在检查异步边界。日志均保留。
- 竞态复核：第二轮唯一失败是旧062测试观察子任务清理时机早于父任务推进游标，补等待接话任务结束后仍独立断言游标、收音门禁、一次台词和一次失败；没有改生产或放宽断言。第二轮日志保留为`/private/tmp/interviewer-064-backend-second.log`，最新全量正在进行。
- 实际源码：新增`app/adapters/livekit_connection.py`、`app/services/conversation_input_context.py`；修改`app/adapters/livekit_audio_{ingress,output}.py`、`app/core/prompt/{contracts,semantic_turn,conversation_reception}.py`、`app/domain/interview_agent.py`、`app/model_gateway/gateway.py`、`app/providers/mock/provider.py`、`app/services/{agent_ticket,conversation_understanding,interview_agent,interview_evidence,interviews,media_capture}.py`及`app/services/interviewer_supervisor/service.py`、`app/transport/http/fields/public_interview.py`。前端修改`candidate/{Page.jsx,QuestionHistory.jsx,agent-experience.js}`和`interviews/agent-event-runtime.js`及相应测试；路径均位于`app/web/src/features/`。
- 实际测试/文档：新增`tests/test_{candidate_end_lifecycle,conversation_flow,conversation_input_context,latest_turn_intent,livekit_connection}_064.py`，同步旧接话/语义/准备/媒体/可选context测试及`test_reception_live_history_062.py`的异步观察。更新架构、API、领域、检索评分、供应商、CONTEXT、已知问题、进度、路线图、本日志，新增`docs/conversation-intent-and-end-064.md`。未升级依赖、迁移数据库或修改真实候选材料。




- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **2929 passed / 8 skipped / 200.91s**，`/private/tmp/interviewer-064-backend-final.log`；此前失败均保留。`npm test -- --run` **419 passed / 26 files**，构建成功，`compileall`、`git diff --check`及11份文档链接检查通过。真实模型17/17、本机无媒体连接取消3次通过；专用多轮/结束/媒体/反例回归均包含在全量中。旧062异步竞态修正后29项相邻测试及10次独立进程20个原参数case通过。
- 本机加载：最终再次确认LiveKit房间1/候选0、worker active/reserved0，正常TERM API31838/worker31839及所属Beat，启动API **48632**、worker/Beat **48633**；原环境及媒体容器保持。发布`app/web/dist/index.html`、`bundles/index-DZsVKgLG.js`、`bundles/VrmAvatar-Bf0P78Gw.js`，先写资源后原子替换index、旧bundle保留，原index备份`/private/tmp/interviewer-064-previous-index.html`。
- 加载后核对：healthz/readyz均200且ready=true；线上JS/CSS哈希匹配构建；worker单节点pong、Beat派发成功、active/reserved0。三场候选权限GET均200、公共completion投影已加载且无私有回答字段，19/1/6份原问答与会话核心哈希完全相同；状态仍in_progress/paused/in_progress。API/worker新增Traceback、FFI Panic、ReadyForRoomEventRequest、数据库锁、未读取task异常和OUTPUT_BACKPRESSURE均0。过程`/private/tmp/interviewer-064-{restart,publish,verify}.log`。
- 未完成与恢复：真人长轮次、口音/弱网、真实扬声器听感、外部生产依赖未验收，模型所有表达不能由17条合成用例保证。未自动恢复/结束旧面试、重评分、发邀请或提交/push。回退应基于064前差异撤销本轮源码并在空闲时重启，静态首页用保留备份，不能回滚业务库覆盖历史；保留059–062其它未提交成果。没有新增依赖和DDL迁移。

## 2026-09-22 · MULTITURN-CONTEXT-062

- 状态：`verified（仓库回归、真实模型合成路由检查、本机服务加载；真人长对话与生产未验收）`。
- 目标：定位新场`iv_a60b0a9dd291491a`同题多轮概念澄清、重读后上下文/角色错位，以及无新发言时重复旧回应的问题；对齐该题全部生成台词、语音资产、服务端转写和调用/播放日志后，修复可复现根因并做多轮回归。
- 边界：已保存起始差异`/private/tmp/interviewer-062-before.patch`，保留全部057至061未提交成果。真实材料只用于本机只读调查，不重新发给外部模型、不改历史问答或评分、不自动恢复/结束面试、不发邀请；新Prompt集中版本化并补合同测试，语音权威final、媒体输出代次、父子评分与可选Skill/企业资料保留。修改文件、失败、验证与空闲发布结果在本项补齐。


- 调查证据：目标题7条不同台词、14次选择，同一旧澄清7次；14:07:02至14:12:00无新STT文字，仍有14次音频输入失效和4次完整决策。整场18个完成WAV校验完整，约50秒输出阶段由首连30.004秒超时、重试及18.72秒内容构成；两次评分失败为供应商HTTP402。只读本机摘要`/private/tmp/interviewer-062-audio-audit.json`。原场手动暂停，保留真实内容。
- 已实施：当前正式题/子题与解释分离，v5显式角色上下文/重读目标，最近六组候选请求与实际选中回应持久关联，独立handled-final游标阻止噪声重播；接话与完整记忆均排除未来预分配题。供应商402单独分类、禁自动重试并显示账单提示。完整说明及仍待改进事项见`docs/multiturn-context-062.md`。
- 过程与失败留痕：首次root接线测试在history方法尚未落盘时5failed/68passed，方法完成后78passed。子任务新测试初次Python3.9无current loop导致fixture报错，改anyio异步fixture后相关152passed；402新测试一次误用event_type而实际为type，纠正后77passed。首次后端全量2792passed/8skipped/2failed，均旧测试检查question字段/无repeat_target形状，按v5更新后定向52passed。首次compileall因系统Python默认写入受限Library缓存失败，改`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-062-pycache`后通过；未改变系统权限。没有业务数据副作用。
- 实际模型检查：只读配置复制到内存，用虚构材料两轮各10例多轮/子题交流，路由与重读目标均10/10；第二轮模型耗时1.24–2.51秒，不含STT/TTS。两例澄清在收紧后仍自行列举策略或推断考察重点，已如实写入后续P1计划，不称内容安全全绿。两轮结果分别保留`/private/tmp/interviewer-062-model-reception{,-refined}-results.json`；未发送真实候选资料。


- 最后审查发现并修复P1：可选记忆写入的PersistenceError/SQLite OperationalError原本会在已选播报后传播，令speaking退回listening并漏记已处理游标。两例故障注入先红后绿，现在可选读写失败安全跳过、清理stage、记录固定error_type，不吞Cancel、不绕过owner；相关52passed/11.10s，证据`/private/tmp/interviewer-062-optional-memory-{red,green}.log`。这是本轮新增功能引入风险，已在发布前修复。
- 修改文件：`services/{dialogue_focus,dialogue_history,interview_evidence,spoken_supplement,livekit_evidence_ingress,conversation_understanding,interviews}.py`、`services/interviewer_supervisor/context.py`、`core/{errors,speech_diagnostics}.py`、`core/prompt/conversation_reception.py`、`domain/model_route_readiness.py`、`model_gateway/gateway.py`、`providers/{mock,openai_compatible}/provider.py`；工作台模型页/评分页和测试；新增`tests/test_{reception_context,reception_cursor,reception_history,reception_live_history,repeat_targets,provider_billing}_062.py`，更新现有接话版本/投影及历史焦点测试；062说明和架构、API、领域、评分、供应商、问题、进度、路线图、变更日志。无表迁移，无音频协议/缓冲改动。
- 最终验证：`.venv/bin/python -m pytest -q` **2800 passed / 8 skipped，192.11秒**（跳过外部环境依赖）；前端 `npm test -- --run` **410 passed / 26 files**；`npm run build -- --outDir /private/tmp/interviewer-062-dist` 通过，保留既有大包警告；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-062-pycache .venv/bin/python -m compileall -q app tests`、文档链接及 `git diff --check` 通过。完整输出在 `/private/tmp/interviewer-062-{backend-final,frontend-full,build,compile-final}.log`。
- 本机加载：两次检查LiveKit房间/候选人均0，worker active/reserved均0；保留旧静态资源，原子发布 `index-CrIrnajx.js` / `VrmAvatar-CunGEnn1.js`。正常TERM旧API234/worker235及Beat，复用原环境启动API **10601**、worker **10602**。healthz/readyz均200且ready=true，实际首页资源SHA256匹配；worker单节点pong，Beat已派发，已有音频指标完整。维护后API/worker新增Traceback、数据库锁、未读取Task异常、OUTPUT_BACKPRESSURE均0；结果 `/private/tmp/interviewer-062-{preflight-final,restart,verify}.log`。
- 数据保留：维护前后原会话核心业务数据SHA256一致，仍为手动暂停、4个turn、3份答案；候选人HTTP投影200、答案逐字匹配数据库且无私有字段泄漏。没有恢复面试、补造问答或再次评分。
- 未完成：真实模型仍偶尔给出超出中性释义的策略提示；TTS首次打开30秒等待及交流实际送达状态尚待后续实现，详见062计划。供应商402账单本身未解决，管理员需处理账户后再重评。旧字段/响应兼容保留，因此不标closed；未把自动化或本地readiness当成真人/生产验收。

## 2026-09-22 · FIRST-QUESTION-AUDIO-061

- 状态：`verified（仓库回归、本机LiveKit合成及服务加载；真人听感/生产未验收）`。
- 目标：定位060加载后新场`iv_e6ae9d148eec4db8`首题即因`AUDIO_TRACK_LOST`暂停的问题；以事件、媒体日志和可复现测试确认原因，再修复真实播放/取消/连接生命周期，保留既有理解与追问改进。
- 边界：保存开始差异`/private/tmp/interviewer-061-before.patch`，保留所有未提交成果；只读真实记录与日志，不外发候选材料、不改写答案或自动恢复会话。避免将音轨正常释放、表达取消与真实连接故障混为一谈，也不能吞掉真正播放失败。首次测试、失败、实际文件、最终验证及本机更新情况在本项补齐。


- 定位证据：故障场11:45:06选择warmup_confirmation，11:45:07.524发布对应音轨；11:45:12进入preparing_next_topic，11:45:12.568旧表达以CLIENT_REQUEST_LEAVE主动断开，同秒AUDIO_TRACK_LOST暂停；候选断连晚至11:45:27，正式题/答案均0。原试音确认没有取消仍在播放的试音表达；floor使围栏失效，runner静默撤轨而前端缺少精确取消。未将后续断连倒置为起因。
- 实施与相邻审查：试音确认先撤销精确旧输出再录制/规划，旧确认在暂停、重试、当前题、控制权、连接关闭或evidence实例变化后不能生效；managed重试和继续补充同样先撤销并在await后复核原owner/control/capture/turn/校准与后继act。通用失效输出在底层清队列后、撤轨前补精确通知，与显式撤销共享一次有界任务；正常drained无取消，真实provider/transport异常仍fatal。顺手修复native清理在unpublish/source.close被取消后遗留source/room的问题，每阶段有界释放后传播取消。
- 失败留痕：试音首次5 failed，修正后新before_ready竞态仍1 failed/4 passed，底层通知seam补齐后通过；最早晚回执测试误写completed而非stopped，纠正为真实协议并通过。通用输出首次6 failed/1 passed，managed reset另1 failed；独立审查复现await后暂停仍录制，已补拒绝；新守卫误读不含calibration_status的精简投影，经审查与正常路径回归改稀疏控制操作完整get，媒体热路径不变。初次只读搜索若干过期路径未命中，改实际路径；Docker初读包含过多底层协议诊断，后续严格用时间/类型/身份/原因白名单输出，无外发、无业务写入。所有失败日志保留/private/tmp/interviewer-061-*。
- 阶段验证：真实试音→Supervisor选择→问题表达完整合成入口、三播放阶段确认、重复确认、迟到回执、录制失败与12个等待中变更场景；相关37 passed。通用通知/正常drained/真实失败/managed continue与reset、Memory和SQLite、取消清理专项31 passed；早期11文件组合226 passed。前端全量408 passed/26 files；compileall和diffcheck通过。最终全量后端运行中。
- 本机媒体冒烟：真实本地LiveKit随机独立房间、合成PCM，无候选材料或外部模型；旧输出撤销通知一次、过期PCM被拒绝、后继音轨保留并正常发送4800样本，结束后参与者0。证据/private/tmp/interviewer-061-native-smoke.log。该冒烟未使用真人扬声器，不能代替听感验收。
- 维护准备：只读预检rooms/candidates=0、worker active/reserved=0；故障场状态/暂停原因/候选/计划/题目/回答摘要已保存。尚未在本项停止服务，回归结束后再检查空闲并重启。本轮无前端生产源码变化，沿060已发布静态资源，不重复构建/替换。

- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **2714 passed / 8 skipped / 186.54s**，日志`/private/tmp/interviewer-061-backend-final.log`；`npm test -- --run` **408 passed / 26 files**，日志`/private/tmp/interviewer-061-frontend.log`。新增专项31项、试音与Supervisor37项、相邻12文件265项通过；`compileall`、`git diff --check`和8份文档本地链接检查通过。本轮只修改前端测试，无需重建沿用060线上资源。
- 实际文件：`app/adapters/livekit_audio_output.py`；`app/services/{approved_speech_output,speech_output_interruption,interview_agent,livekit_evidence_ingress}.py`；新增`tests/test_{speech_interruption,warmup_output_transition}_061.py`；更新`tests/test_streaming_expression_lifecycle.py`、`tests/test_realtime_expression_fence_056.py`、`app/web/src/features/candidate/agent-experience.test.js`；新增`docs/first-question-audio-061.md`及同步架构、API、供应商、已知问题、进度、路线图、本日志，共18份文件。没有新增Prompt、表、字段、迁移或供应商配置。
- 本机更新：再次确认rooms/candidates=0、worker active/reserved=0，正常TERM API93786/worker93787及所属Beat，启动API **234**、worker **235**，原环境与媒体Docker保持。healthz/readyz均200、ready=true；前端原060静态SHA一致，worker单节点pong、Beat派发成功；候选权限接口200。故障场次仍paused、0正式题/0答案，状态/暂停原因/计划/原问答摘要与维护前一致。新增Traceback、数据库锁、未读取Task异常、OUTPUT_BACKPRESSURE均0。日志`/private/tmp/interviewer-061-{restart,verify}.log`。没有自动恢复、结束、重评分或发送邀请，也没有commit/push/reset。
- 未完成/恢复说明：真人听感、长期连续交流、弱网及生产验收仍需单独实测；真实provider失败的原始原因通知晚于撤轨的诊断顺序可继续完善，本次保留其fatal保护，未以正常取消掩盖。旧场次可在“面试会话”中由用户点击“人工确认后恢复 AI”后继续；不应刷新就自动开面试。回退须按061增量恢复源码并在空闲时重启，不能覆盖数据库回退历史。一次只读ps检查被sandbox拒绝，未改权限或停进程；后续合法维护脚本按同用户项目归属检查正常完成。

## 2026-09-22 · AGENT-AUTONOMY-060

- 状态：`verified（仓库回归、真实模型合成与本机加载；真人整场/生产未验收）`。
- 目标：落实059遗留的追问提案有界修复、快速接话与完整理解的冲突裁决，并审查长回答、重连、子题和评分证据链的遗漏；在现有有效实现上改进自主交流，不增加用户填写步骤。
- 边界：保留057/058/059未提交成果（起始差异`/private/tmp/interviewer-060-before.patch`）；不重放真实候选材料到外部模型、不改写既有问答或评分、不发邀请、不自动结束或恢复真实会话。权威final、输入版本/owner/capture围栏和父子评分继续生效。Prompt/Schema集中版本化，安全或泄题拒绝不得通过修复绕过；修复失败仍保留已验证的理解和回答。
- 计划：先做确定性红测与相邻审查，实施受时间/次数限制的提案修复与接话裁决，补合成模型和回归，同步设计/问题/进度/路线图。前端与后端验证后检查真实运行空闲再发布；仍有候选人时不以“ok改进”推断允许打断。实际文件、验证结果、失败和剩余事项本项补齐。
- 确定缺陷与已实施：快wait/continue取消完整理解、相同final被锁住；复核不确定结果未进入补充握手；长回答4000字资格断崖；历史滚动窗口之外主张无法按turn补查；跨API控制连接重连只收到ready、原owner的识别等待提示丢失。补一次绑定final/revision/capture的完整复核、可修复提案的一次3秒措辞修复（共享15秒）、全文理解后有界追问、当前会话来源限定的历史工具与来源哈希指纹、连接代次等待提示恢复和去重。独立追问失败时旧scope备用路径也能忽略新过期预算/关闭话题，新增6项红测后补异常路径重新校验。
- 阶段证据与失败：接话先3 failed/9 passed，再补不确定结果红测1 failed/1 passed；旧wait/continue fixture仅伪造快速结果、完整模型不一致，改为真实合法thinking/continue并保留相反结果回归，最终8文件121 passed。长答审计4 failed/1 passed，修复及版本/安全等23文件538 passed；备用路径6个红测修后相关149 passed。历史工具先3 failed/8 passed；新增真实工具调用后30 passed/1 failed暴露规划审计硬编码v2，改用集中v3常量后31 passed。双Runtime复现旧owner已有等待、远端只ready/0notice，修后24项重连/ownership/识别活性回归通过。原失败日志均保留`/private/tmp/interviewer-060-*`，没有修改真实回答。
- 模型与前端阶段验收：虚构材料、只读路由/凭据、内存审计的真实模型16项意图/内容均通过；6条可探测回答6条合法追问；另2条注入坏形状的合成提案经实际模型修复成功，约1364/1171ms，两者保留理解且仅一条子题提案。当前语义中位5808ms、最高6840ms，不是端到端首声或零延迟。日志`/private/tmp/interviewer-060-semantic-model.log`及结果JSON；前端406 passed/26 files，构建`/private/tmp/interviewer-060-dist`成功（既存大bundle提示）。compileall与diff检查通过。全量过程中审查补出上述备用路径遗漏，因此初轮仅作中间验证，最终源码需要再跑一次全量。
- 本机检查：只读维护预检LiveKit rooms/candidates=0、worker active/reserved=0；API54320/worker54321仍为旧运行，三个媒体Docker持续running。尚未停止或发布，最终回归后复查空闲再更新。
- 完整源码初轮：2658 passed/8 skipped/185.30s；该轮启动后补入备用路径6项回归，故另跑最终全量。两个4500字以上虚构回答真实模型复验通过：4552字末尾交回话轮获合法追问（9968ms）；4550字末尾改口继续思考保留claims且不追问（5218ms）。全文与引用未截断，日志`/private/tmp/interviewer-060-model-long.log`。
- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **2664 passed / 8 skipped / 183.74s**，`/private/tmp/interviewer-060-backend-final.log`；`npm test -- --run` **406 passed / 26 files**，`npm run build -- --outDir /private/tmp/interviewer-060-dist`成功；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-060-compile-cache .venv/bin/python -m compileall -q app tests`、`git diff --check`与12份文档本地链接检查通过。最终额外备用路径只改异常分支，前述实际模型正常/修复路径验证仍适用。所有跳过项保留，未声称外部生产依赖通过。
- 实际060生产文件：`app/core/prompt/{contracts,interviewer_supervisor}.py`、`app/domain/interview_agent.py`、`app/model_gateway/gateway.py`、`app/providers/mock/provider.py`、`app/services/{conversation_understanding,spoken_supplement,livekit_evidence_ingress,interviews}.py`、`app/services/interviewer_supervisor/{context,tools}.py`。原059的semantic_turn、answer_endpoint等修改保持，无060新增媒体改动。新增测试`test_{followup_repair,supervisor_version,agent_audit,historical_evidence,reception_arbitration,remote_wait_reconnect}_060.py`；更新旧测试`test_{answer_declined_understanding,company_questions,declined_answer_integration,followup_rejection_integration,formal_followup_056,prepared_turn_decision,semantic_turn_policy,spoken_supplement_integration,stable_preview_preparation,supplement_contracts,conversation_semantics_057,conversation_handoff_059,optional_followup_isolation,conversation_reception,conversation_reception_integration,interviewer_supervisor}.py`。同步12份设计/问题/进度/路线图/规划/日志文档。前端源码沿059，本轮发布`app/web/dist/index.html`及`bundles/{VrmAvatar-CFlcnuY8,index-Do_SE3Ed}.js`，保留所有旧bundle。
- 本机更新完成：最终维护检查rooms/candidates=0、worker active/reserved=0后正常TERM旧API54320/worker54321及所属Beat，启动API **93786**、worker **93787**；原环境保留，媒体容器无需重建。资源先复制再原子替换index，旧index备份`/private/tmp/interviewer-060-previous-index.html`。healthz/readyz均200、ready=true；线上index-Do_SE3Ed.js及CSS与磁盘SHA一致，worker单节点pong、Beat派发成功。候选安全接口200，原场18题/17份已提交回答与更新前内容哈希完全一致，仍保持原paused状态；未恢复、结束、重评分或发送邀请。更新窗口API/worker新增Traceback、数据库锁、未读取Task异常和OUTPUT_BACKPRESSURE均0，证据`/private/tmp/interviewer-060-{restart,publish,verify}.log`。本次同时加载之前等待空闲的059后端及前端改动。
- 未完成/恢复说明：临时“对”的具体声学来源仍不能从旧日志确定；新假设诊断、真人听感、弱网和生产并发需后续实测。进程完全重建的最小会话检查点、跨题主动查证质量与评估矩阵详见`docs/agent-autonomy-improvements-060.md`。已知兼容路径保留，不标closed。回退使用保留index和按本项差异回退源码后空闲重启，不能回滚数据库覆盖历史记录；没有commit/push/reset。工具打开文档返回queued，未声称已完成浏览器视觉或真人语音验收。

## 2026-09-22 · CONVERSATION-CONTEXT-059

- 状态：`verified（仓库回归及合成模型，060维护窗口已加载本机；真人/生产未验收）`。
- 目标：定位新场`iv_26bdd558406c4214`末尾“对”后接话异常、首次明确说完不生效、邀请追问后直接换题、长会话思考请求异常；修复后产出有证据、优先级与验收标准的Agent改进规划。
- 边界：保留未提交057后端与058报告页面成果；只读真实记录/日志，不将真实候选回答重放给外部模型，不改写历史问答、不发邀请、不自动恢复或结束真实会话。Prompt及Schema集中版本化，保留权威语音、输入/所有权围栏、父子评分与流式播放。首次修改前代码差异保存于`/private/tmp/interviewer-059-before.patch`，验证与本机更新待空闲检查后执行。
- 初始判断：截图只能证明末尾出现“对”及仍处在收听状态，不能把单个字直接当根因；沿真实ASR分段、接话分类、补充确认与理解/追问门禁逐层核对。失败过程、文件、验证和未完成事项在本项补齐。
- 已核实证据：第15题最终6段/146字均能与STT句源哈希对应，最终答案无截图尾字“对”，唯一提问台词也无该字；没有发现LLM/TTS台词写入候选回答的通路。仅能判断更可能为临时识别假设，旧日志不足以辨别声学误识别、回声或静音幻觉。该题同一音频起点反复重开17次以上，约两分钟；空sentence_begin从未产生文字时仍被视为丢失final，可合成复现。用户说完后的短尾丢失前次确认上下文、稳定预览准备遗漏semantic_first、未播接话提前消费游标也分别复现。17次正式决定中12次模型不追问、4次非答案/拒答、1次提案校验拒绝，没有预算耗尽；第4题邀请追问已允许追问，但模型仍未选择。
- 已实施范围：有来源约束的补充确认上下文、准备路径语义模式一致、接话实际获准后才消费请求；Prompt区分结束回答/关闭话题/交还话轮，并先选一项核实目标再生成追问。空ASR占位正常收尾，无final自动切流最多三次后保留录音/当前识别流等待新音频或重试。暂定字幕始终标明可修正，累计final替换旧稿，识别未完成不再显示普通“还在听”。新增有界、无明文的假设来源/修正/撤回诊断；无PCM分片、播放缓冲、供应商配置或数据库迁移。
- 阶段验证：确认/游标/有界恢复/取消清理172 passed；来源围栏5 passed；空占位及网关/连续识别47 passed；日志与供应商相关82 passed；语义合同379 passed。前端`npm test -- --run`为406 passed/26 files，`npm run build -- --outDir /private/tmp/interviewer-059-dist`成功，只有既存大bundle提示。首次全量在子任务版本矩阵更新前运行，88 failed/2466 passed/8 skipped；矩阵和旧fake参数修正后第二轮2560 passed/8 skipped/1 failed。剩余为清理错误优先级fixture仍将正常空占位当丢字；第一次改非空假设仍能被正常ACK定稿，改为先观察文字再撤回为空，保留原错误优先级断言，单文件37 passed。端点取消清理曾因对已完成task额外await引入fixture竞态，改同步消费已完成异常后172项通过。所有失败过程保留临时日志，未改业务数据。
- 合成模型与剩余核查：所有输入为虚构材料，只读路由/凭据、审计在内存，无候选数据重放。追问形状初轮存在多个问号，未放宽门禁或截断输出；聚焦Prompt最后三条对照均获合法追问。最终16例复验15通过，6条正式追问获批、3条补充确认正确；明确做法后继续思考仍有一次understanding_content_invalid，继续诊断，不能宣称全绿。详细证据`/private/tmp/interviewer-059-semantic-model-final.log`；弱“知道需要任务编号”是否构成技术claim作为观察，不用宽松断言掩盖真实做法丢失。
- 本机状态：两次只读发布前检查均发现LiveKit仍有1个候选人，维护脚本在任何TERM前退出；API/worker和058页面仍保留现有运行版本。未强制断线、未自动结束或恢复会话。最终发布/验收待完成回归并确认空闲，阶段构建尚未发布。
- 最终补修：上述思考用例实际保留了claim，却返回thinking+next的矛盾动作。新增固定`SemanticControlContractError`，日志与已有一次纠正保真原因；Schema先输出含义、最后输出动作，保留全部门禁/原有调用次数。版本最终为semantic v13/v14、understanding v36–v39、decision v59–v64、controlled_followup v5、supplement_reply v5、conversation_reception v4。9条新合同/真实纠正请求断言红→绿，相关497 passed；三条合成真实模型均一次调用通过：继续思考/改口保留claim并等待，正常说完仍可追问。另修同一capture断线时丢失无final等待提示，attach只重放仍有效的当前提示，公共投影保留capture_id；新音频已解除或旧题/旧capture不重放，相关62 passed（首次暴露公共投影缺allowlist，补固定安全文案后通过）。
- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` **2578 passed / 8 skipped / 173.61s**，日志`/private/tmp/interviewer-059-backend-full-release.log`；前端 **406 passed / 26 files**，最终Vite构建成功；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-059-compile-cache .venv/bin/python -m compileall -q app tests`、`git diff --check`、10份文档本地链接检查通过。最终版本16条真实模型合成复验 **意图/内容16/16、实际技术内容9/9**；6条可探测回答中3条合法追问（含邀请追问），2条question_shape_invalid被安全拒绝，1条model_declined，没有为了追问数量放宽门禁或重复运行挑选结果。语义中位5823ms/max6627ms。虚构材料、配置只读、内存审计，日志`/private/tmp/interviewer-059-semantic-model-release.log`。这不代表真人麦克风、回声、供应商并发或生产环境已验收。
- 实际文件：`app/core/prompt/{contracts,semantic_turn,conversation_reception}.py`、`app/core/speech_diagnostics.py`、`app/domain/interview_agent.py`、`app/model_gateway/gateway.py`、`app/providers/{dashscope,mock}/provider.py`、`app/services/{answer_endpoint,spoken_supplement,interview_evidence,conversation_understanding,livekit_evidence_ingress,interview_agent}.py`；候选页面`Page.jsx`、`agent-experience.js`、`presentation.js`及测试；新增059的`test_conversation_context`、`test_conversation_handoff`、`test_evidence_context`、`test_missing_final_liveness`、`test_stt_hypothesis_diagnostics`、`test_stt_placeholder`、`test_transcript_wait_reconnect`与相关合同/确认/空结果/清理测试；10份对应设计、问题、进度、路线图及本日志。保留057与058未提交成果，未执行commit/push/reset。
- 未完成项：修复已完成仓库及合成模型验收；本机加载需要候选人退出或明确允许短暂断开，已向用户说明影响并询问，尚未重启或发布059构建。三个媒体Docker只读检查均持续running。临时“对”的声学来源仍需新版诊断复现，不声称已证明是回声或已经彻底消除供应商误识别；快速接话冲突仲裁、长回答资格、检查点与旧主张检索详见`docs/agent-conversation-improvement-plan-059.md`。
- 后续完成：上述加载等待已在060无活动面试的维护窗口解除，059源码和候选页面随060一起发布并通过健康/资源/数据核对。原过程与限制保留，新的运行状态及其余规划以060记录为准。

## 2026-09-21 · REPORT-QUESTION-PLAYBACK-058

- 状态：`verified（前端回归、构建、历史媒体投影及本机浏览器布局/媒体加载）`。
- 目标：统一面试报告逐题列表与复核详情的展示位置，让每道题的回答、评分、独立音频和按题视频入口显示在对应题目内，并解释不可用媒体。
- 关联问题：截图场次 `iv_3f0679a769cf4abd` 上方题目列表内嵌转写、下方另有默认选中题目的详情，造成板式不一致和上方无法直接找到回放入口。
- 实际修改：`app/web/src/features/interviews/Review.jsx` 将转写、评分、媒体按钮和播放器放入被选中题目的卡片，题号按钮可展开/收起并明确提示“查看回答与回放”；按题显示媒体不可用原因，切题清理旧播放器与在途签名响应。`app/web/styles.css` 统一卡片/播放器样式；`app/web/src/features/interviews/Review.test.jsx` 覆盖逐题媒体归属、折叠、追问和切题；`app/web/dist/index.html` 与新 hash 资源发布最终构建，保留旧 bundle 供已打开页面使用。没有 API、数据结构、录音、录像、签名授权或历史面试数据修改。
- 验证：只读 SQLite 核对截图场次 18 题均有答案、独立录音路径和实际文件，既有 `_turn_playback` 对 18 题均返回音频/视频可用；`npm test` 为 **401 passed/26 files**，最后一次折叠交互修改后的定向 `npm test -- --run src/features/interviews/Review.test.jsx` 为 **17 passed**；`npm run build -- --outDir /private/tmp/interviewer-058-dist` 通过，最终发布的 7 个构建文件 SHA 与临时构建逐一相同；`git diff --check` 通过。真实本机浏览器打开原截图场次后，第18题在其卡片内展开，两个媒体按钮均可点击；录音 `readyState=4/error=null` 且时长113秒，视频 `readyState=4/error=null` 并定位到该题时间段，截图目视没有原先上下题目错配。构建仅有既存大 bundle 提示。
- 失败与恢复、剩余事项：首次定向测试 1 项旧断言仍要求未选中追问卡片直接显示转写，改为验证选中后才显示并通过；首次发布的中间 JS/Avatar bundle 被最终构建取代并清理，旧已发布 bundle 保留。命令行环境的 `curl http://127.0.0.1:8000/healthz` 连接失败，但本机浏览器实际成功加载同源 API 与新页面，不能由该 curl 失败推断 API 停止。已关闭临时验证页，未核验真人设备的听感；无需数据库恢复。未向候选人发邀请、重评分或改写原始问答。

## 2026-09-21 · CONVERSATION-RELIABILITY-057

- 状态：`verified（仓库回归、合成真实模型与本机加载；真人整场/生产待验）`。
- 目标：针对新场次正常回答/疑问被误换题、偶发重复读题、细节追问不足、长静音后无反馈，沿权威语音→理解→动作→播放的真实事件链定位并修复；审查相邻自主交互缺陷。
- 初始证据：用户截图场次iv_3f0679a769cf4abd当前report_ready，18个turn/18份答案，44次act选择；需要逐题区分语义误判、播放重放与端点失活，不能仅凭总量推断没有追问。开始时git工作区干净，保留当前已提交056实现。
- 边界：只读真实会话/日志，不将真实候选材料重放给外部模型，不重写历史问答、不发邀请、不恢复或结束真实会话；音频实时精简读取、权限围栏和父子评分链保持。新增Prompt集中版本化并补合同；实时修复注明状态、超时、重试与恢复；空闲检查后更新本机服务。实际文件、失败过程、验证和剩余验收在本项补齐。
- 已确认根因：Docker与OOM回答的理解已是finish_answer，但转场读取更早拒答或携带旧declined键；第9题同一act在13:47:38/13:47:56/13:48:15选择三次，后两次紧跟participant_ready，之前的缺序恰逢Outbox评分刷新。API和worker均未配置跨进程Redis，worker空Hub刷新占了共享序号，触发前端4001重连重播。普通接话和明确思考共用无限等待；组合追问Prompt存在无条件清空提案字段的冲突、证据只取前四项；英文age子串还会误伤message/image等技术词。原日志不足以证明所有零追问或每次静音均由同一因素造成。
- 已实施：转场仅参考紧邻已提交回答；本地无订阅者worker不分配不可投递快照序号，新连接快照按权限广播给已有peer并兼容Redis；普通候选等待约15秒允许一次轻提示，明确wait/thinking继续等，新权威话语才重置资格，主问题播报不算候选静音；模糊转写/术语疑问先澄清，简短实际做法与关闭话题独立保存；修复选中/未选中追问字段条件，后段声明证据可用，一个聚焦问题仍受原门禁；英文属性采用ASCII词边界，中文混排敏感问题仍拒绝。增加prepared/selected/committed去敏诊断，WebSocket握手短期凭据在uvicorn.error单独过滤。
- 专项证据：转场红→绿3例；共享SQLite双进程Hub模拟修前事件序列[1,3]、修后[1,2]，新连接多角色快照缺口同样复现修复，播放/恢复等141 passed、交付专项9 passed。端点初始8项红测后，收音/恢复/补充/父子边界等206 passed；中间fixture未消费idle播放和按STT重开分配模拟回答导致失败，改为明确消费获批表达、idle使用稳定preview不切识别后通过。英语Unicode边界审查发现混合中文绕过，改ASCII边界并新增3例后语义专项23 passed。日志过滤及静音文本合同11 passed。证据均保留在`/private/tmp/interviewer-057-*.log`。
- 合成真实模型：首轮10例9项语义通过，但“一句实际做法后换题”被误当无答案，另partial拒答用例初版断言过宽；保留首轮结果，加强Prompt与断言后最终10/10语义通过，所有实际技术内容均保留，一条简短回答提出获批细节追问。另一条可选追问因question_shape_invalid被拒，追加去敏探针确认为32字/两个问号；没有削弱门禁、截断问题或追加串行模型重试，原回答保留。最终响应4.4–6.2秒，不宣称零延迟。仅虚构问题与回答送模型，路由配置只读、审计留在内存，业务库无探针写入；结果`/private/tmp/interviewer-057-semantic-model-final-results.json`。
- 验证过程：首轮完整后端2514 passed/8 skipped/8 failed（166.96s），8项为旧测试仍要求普通澄清/接话后永久无提示，需改为一次idle但不循环、不提交。前端400 passed/26 files，构建成功且7个产物与当前服务目录逐字相同，无需发布新前端；既存大bundle提示保留。初次对全会话JSON做SHA核对因仍打开的控制页ping/快照使version和last_sequence变化而不相等；不覆盖原基线，改为维护前后对status/answers/turns/plan_snapshot/report独立SHA校验，并明确只证明业务内容未改写。维护前业务基线建立后的18份答案和题目内容已核对一致。
- 实际文件：`core/access_log.py`、`core/speech_diagnostics.py`、`core/prompt/{contracts,semantic_turn,conversation_reception}.py`、`domain/interview_agent.py`、`model_gateway/gateway.py`、`providers/mock/provider.py`；`services/{conversation_understanding,interview_agent,answer_endpoint,spoken_supplement,livekit_evidence_ingress}.py`；新增`tests/test_{conversation_liveness,conversation_semantics,question_delivery}_057.py`及相关Prompt/语义/确认/恢复/日志回归；架构、API、领域、检索评分、供应商、已知问题、进度、路线图与本日志。无数据库迁移、音频分片格式或媒体缓冲调整。
- 最终回归：更新上述两个fixture文件，明确一次idle、消费播报后继续听新文字、600秒不循环/不交卷、wait不催促，定向62 passed。最终`.venv/bin/pytest -q` **2525 passed/8 skipped/167.97s**，`npm test` **400 passed/26 files**，`npm run build -- --outDir /private/tmp/interviewer-057-dist`通过；`git diff --check`与9份修改文档本地链接检查通过。最终证据`/private/tmp/interviewer-057-backend-full-final.log`、`interviewer-057-liveness-fullsuite-fixtures.log`、`interviewer-057-frontend-{tests,build}.log`。数次只读检索路径或尚未产生的临时日志不存在，纠正后完成，没有业务副作用。
- 本机加载：维护检查和重启前复查均LiveKit rooms/candidates=0、worker active/reserved=0；正常TERM API28944/worker28945及其Beat，启动API **54320**、worker **54321**。三个媒体Docker持续running，无需重建；前端重建产物与当前7文件一致，不替换资源。`healthz/readyz`均200且ready=true，在线资产SHA一致，单worker pong及新启动后Beat派发通过；维护窗口API/worker所查Traceback、数据库锁、未读取Task异常、输出背压均0。验证`/private/tmp/interviewer-057-verify.log`。
- 原始数据与剩余验收：候选权限HTTP返回18份回答，与数据库逐项一致且没有私有答案字段；status/answers/turns/plan_snapshot/report在维护前后SHA相同，原场次维持report_ready。未发邀请、恢复/结束场次、重评分或改写原问答。真人长时连续交流、真实弱网、多浏览器及8项外部环境依赖仍待验收；模型可选追问仍可能因格式不符被丢弃，不能把10类语义通过称为所有追问/语音均完美。回退需针对057代码增量在空闲时重启，保持数据库与历史问答；本轮无Git提交、推送或生产部署。

## 2026-09-18 · INTERVIEW-REGRESSIONS-056

- 状态：`verified（仓库回归、同快照媒体负载、本机加载；真人听感与生产待验）`。
- 目标：修复候选端已保存回答误显示未提交、真实面试语音严重断续、临时细节追问未产生的问题。先核对实际持久化/HTTP/媒体指标，不把前次单元测试通过当作真人体验验收。
- 初始证据：新场 iv_46f27141b235465a 已有5份非空已评分回答；service投影包含final_transcript，但HTTP PUBLIC_ANSWER_FIELDS将其删除，前端空文案误报未提交。音频和正式追问原因继续分别定位。
- 范围与边界：保留055及此前未提交成果、真实录音与业务记录；不重放真实候选材料给模型、不自动恢复/结束面试、不发邀请。保留媒体所有权和输入revision围栏；本机更新前检查LiveKit活动候选人及worker任务。接口、状态、Prompt和评分若调整同步对应文档。实际文件、命令、失败、结果和未验收事项在本项补齐。
- 过程记录：9/18执行额度中断，9/19按用户“继续”恢复，未发布未验证代码。只读追问调查确认5题均将完成回答判为finish_topic/followup_allowed=false，且已覆盖知识点被排除出细节核实目标，预算仍未消耗。候选资料不外发。当前运行音频16次输出内容最大21.52秒、发送累计最大45.128秒、供应读取累计最大151ms、调度延迟最大275ms；这些聚合值不能逐条配对，继续以同场形状负载验证。全会话读取/复制每次约3.6ms，多次每帧执行耗尽实时预算。浏览器CUA inventory超时，未取得真人播放验收。

- 056阶段证据：HTTP最终白名单回归先红后绿，后端关联31 passed、前端128 passed。当前真实模型仅发送6条自编回答，完成回答两例均保留正式细节追问，明确换题、思考、引用结束词及拒答均不误追问，6/6通过；记录`/private/tmp/interviewer-056-semantic-model-results.json`，未写业务数据库。
- 056音频对照：相同新场只读内存快照、16秒384000样本PCM、LiveKit独立随机房间，叠加20ms空命令/终态读取、1s接管发现和每帧完整所有权/Skill校验。旧全记录读取19.735秒排空，新精简状态读取16.005秒；发送间隔p95由41.475降为21.593ms，最大66.355降为23.641ms；读取围栏p50由4.122降为0.784ms，样本完整且无协议错误。证据`/private/tmp/interviewer-056-audio-{before,after}.json`。这是媒体与所列读取负载验收，未包含真人设备、全部写入/STT/录像并发，不能据此保证任意负载无卡顿。

- 056最终回归阶段：第一次后端全量2463 passed/8 skipped/3 failed，失败来自旧fixture仍将已覆盖点追问视为非法；调整真实mock决策与确实违规的追问目标/理由后，定向68 passed，最终全量 **2466 passed/8 skipped/161.09s**（`/private/tmp/interviewer-056-backend-full-final.log`）。前端补充主/子题编号独立显示时先出现两项稀疏历史编号回归，保留历史order并扣除已知子题后 **400 passed/26 files**，production build通过；`git diff --check`通过。构建仍有既存大bundle告警，不记作构建失败。
- 056同一快照严格音频对照：固定source_version=1693、同一内存源与所列并发读取负载，旧全记录16秒音频排空19.499秒，精简投影16.006秒；发送间隔p95 **41.336→21.600ms**、最大 **101.156→26.223ms**，两次均384000样本完整、无协议错误。围栏p50 **4.106→0.861ms**，没有移除每帧所有权/Skill检查。证据`/private/tmp/interviewer-056-audio-same-snapshot.json`，本项以该配对结果为最终数据。未覆盖真人设备、完整STT/录像/写入并发。
- 056真实模型最终复验：加强claim证据绑定后再用同六条自编话语检查，语义6/6正确，一条完成回答选择细节追问，另一条保留追问资格但模型主动不选。原脚本要求两条都选追问，故输出5/6；保留原结果，不把合法自主不追问改成强制追问，也不伪称该脚本全绿。最终记录`/private/tmp/interviewer-056-semantic-model-results.json`，前次结果另存`interviewer-056-semantic-model-results-before-final-gate.json`。响应耗时约4.6–6.9秒，不宣称近零延迟；没有发送真实候选资料。
- 056过程失败留痕：旧临时process helper已被清理，同快照探针首次因FileNotFoundError在连接房间前退出；重建只读、同用户/项目归属校验helper后成功。一次测试文件名无效导致未收集测试，纠正后完成相关测试；一次只读搜索shell通配符无匹配，改显式路径完成。无业务数据写入或候选会话恢复。最终审查另发现补充话术v2审计仍标v1，部署前修正并补合同测试。
- 056实际文件：`app/transport/http/fields/public_interview.py`、候选`QuestionHistory`及测试、共享`question-tree`及测试；`app/core/prompt/{contracts,semantic_turn}.py`、`app/domain/interview_agent.py`、网关审计版本白名单、MockProvider、`services/conversation_understanding.py`；新增`persistence/realtime_state.py`及`interface/memory/sqlite/postgresql/read_only`适配、`services/{interview_agent,interviews,livekit_evidence_ingress}.py`；`core/interview_agent_metrics.py`、前端`agent-experience/live-speech-playback`及测试；新增后端`test_{candidate_history_http,formal_followup,realtime_expression_fence,realtime_session_projection}_056.py`与相关语义/Prompt/企业流程/可选追问/补充确认测试；对应API、架构、领域、检索评分、供应商、存储、已知问题、进度、路线图及本日志。前端dist同步发布，保留055其他未提交修改。无数据库迁移、新表或历史问答改写。
- 056最终版本补丁：新补充确认/继续/澄清act统一采用集中`SUPPLEMENT_SPEECH_VERSION`；三例先红复现旧v1标记后修复，旧冻结v1不重写。`pytest -q`对应5组Prompt/集成测试 **158 passed/15.75s**（`/private/tmp/interviewer-056-supplement-version-tests.log`）。该补丁在全量2466项完成后加入，用相关测试验证，不伪称全量已覆盖末次新增测试。
- 056最后追问版本复核：`followup.selected`实际复用决策层冻结的act，保留原`controlled_followup.v1/v2`、组合决策v22或历史空版本；无冻结act会拒绝，下面的旧fallback不可达，不是实际版本覆盖故障，因此未追加业务修改或再次重启。扩展两个复用/实际播报测试的版本矩阵，子任务额度中断后由主线程接续，**107 passed/2.50s**（`/private/tmp/interviewer-056-frozen-version-final.log`）。10份本轮文档本地链接检查与最终`git diff --check`通过。数次只读搜索使用了不存在的glob/文件路径，纠正后完成，未造成仓库或业务数据副作用。
- 056本机更新：两次维护检查均LiveKit rooms/candidates=0、worker active/reserved=0，正常TERM旧API5572/worker5573及Beat后启动API **28944**、worker **28945**。发布`index-DiazyS9e.js`、`VrmAvatar-BzhO4IQ0.js`，CSS沿用`index-CvBCMNRE.css`，资源先复制、index原子切换，旧bundle保留且旧index备份于`/private/tmp/interviewer-056-previous-index.html`。healthz/readyz均200、ready=true，在线静态SHA一致，worker单节点pong、Beat派发通过；三个媒体Docker持续running无需重建。新增欠载指标已加载。维护窗口API/worker新增Traceback、数据库锁、未读取Task异常、输出背压计数均0，证据`/private/tmp/interviewer-056-verify.log`。
- 056真实记录验证：截图场次在此次更新之前已因原预约窗口到期成为report_ready（deadline 2026-09-19T04:50Z，updated 04:53:36Z）；5份答案仍在。更新后用候选token在本机GET实际公开接口，5份final_transcript与数据库逐项一致、没有私有评分字段；原会话原始JSON SHA与更新前基线完全一致。没有恢复/结束面试、重评分旧答案或发邀请，不将此前自然到期归因本次更新。
- 056未完成与恢复：浏览器工具连接超时，未取得本轮真人听感/视觉验收；真实PostgreSQL及全量STT/录像/写入并发仍需环境验证，8项外部依赖skip不算通过。音频指标及媒体负载改善不保证任意网络和设备无卡顿。旧页面需刷新加载新资源；原场次已结束，后续按正常预约入口验证。回退可恢复保留的静态index并按056增量回退代码后空闲重启，不能覆盖数据库回退历史；没有Git提交、推送或生产部署。

## 2026-09-17 · FOLLOWUP-CONTEXT-055

- 状态：`verified（仓库回归、本机加载；真人整场与浏览器视觉待验）`。
- 目标：修复多轮交流后结束回答意图失效、重复补充确认和重读定位错误；确保实际追问与独立子回答即时持久化、候选端和报告可见，并按父子问题证据链评价而不混淆考察方向。
- 初始依据：用户提供当前会话长转写，明确多次说完/下一题仍陷入补充确认，要求重述追问时却回到主问题。先对照真实只读记录与实现区分正式追问和未入题目状态的交流表达，不凭截图断言所有追问均未保存。
- 边界：保留054语音连续性、播放/采集所有权和输入代次校验；不重放真实候选人资料给外部模型，不重写历史问答，不自动恢复/结束真实会话或发送邀请。业务Prompt/格式合同必须集中版本化；API、领域、评分与文档按实际调整同步。
- 验证计划：长回答末尾明确结束、确认后的新补充与改口、当前追问重读及恢复、父子独立证据/评分/报告、前端实时展示及现有全链路回归；本机更新须先确认无活动候选人/worker任务。实际文件、结果、失败及剩余边界在本项补齐。

- 055过程记录：9/17—18因执行额度多次中断，保留所有在途修改后继续，未伪称完成或抹除工作记录。只读源会话1个主turn/0正式子turn/0答案，3条v2接话实为技术追问；历史混合录音没有擅自拆分。
- 055阶段验证：回复边界先9 failed/4 passed→151 passed；置信度限定新回复后67 passed；对白事件历史先5 failed→35 passed；父子评分/复核/更正链145 passed。第一次全套2362 passed/8 skipped/6 failed，其中5项在并行修复前已导入旧memory代码，1项旧报告缺turn_id的兼容读取已修复；保留日志，进行最终全套。
- 055真实模型：只从只读配置构建内存模型网关，18条合成话语16直接通过；流程时长误分为题意澄清已修提示并复验通过，长结束/重读追问通过；1条面试外闲聊回应生成问句被v3合同拦截，未播出，不计自然回应通过。未发送真实候选文字，未写业务数据库。
- 055视觉边界：合成React fixture已build，候选父子/后台复核组件及折叠交互测试通过；Chrome连接超时、IAB unavailable，未取得截图，不标视觉验收通过。临时5178服务已清理。证据/private/tmp/interviewer-055-ui-acceptance.json。

- 055实际文件：`app/services/{spoken_supplement,dialogue_focus,dialogue_history,interview_evidence,livekit_evidence_ingress,interview_agent,interviews,evaluation,reports,review}.py`、`app/services/interviewer_supervisor/context.py`；`app/domain/{evaluation_evidence,interview_lifecycle}.py`；`app/core/prompt/{conversation_reception,evaluation_evidence,contracts}.py`、网关版本白名单与mock provider；候选Page/QuestionHistory/agent-experience、后台Page/Review/agent-monitor、共享question-tree、styles及对应前端测试；后端新增`test_{dialogue_focus,dialogue_history,evaluation_revision_fence,followup_evaluation,followup_regrade,followup_review_report,spoken_reply_boundaries}_055.py`及相关旧合同fixture；本节涉及10份设计/进度文档和前端dist。无数据库表迁移，054媒体播放器/分片/缓冲不变。
- 055最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2373 passed/8 skipped/160.59s**（`/private/tmp/interviewer-055-backend-final.log`）；全套采集后新加版本围栏测试与相关专项 **27 passed**。`npm test -- --run` **394 passed/26 files**；production build成功；独立pycache compileall、10份文档链接和`git diff --check`通过。8项外部环境skip不计通过。
- 055本机加载：空闲检查两次均LiveKit rooms=0/candidate=0、worker active/reserved=0。原API80893已有正常shutdown日志、进程已退出，worker80894仍存活；从已确认归属的worker只在内存复用原本机环境，未沿用无关production配置。正常TERM旧worker/Beat后启动API **5572**、worker **5573**；前端先复制资源、index原子切换，新增index-DU-_iu7F.js/index-CvBCMNRE.css/VrmAvatar-B9UroJwO.js并保留旧bundle与旧index备份。healthz/readyz均200且ready=true、在线静态SHA一致、单worker pong与Beat派发通过。3个媒体Docker running，未重建。当前API/worker日志Traceback、database locked、未读取Task异常、输出背压均0。
- 055已观察副作用：原截图会话iv_7ac3608dc0504fdb基线仍in_progress/1turn/0answers，但预约截止为2026-09-17T12:59:00Z。9/18 API恢复后既有截止巡检按appointment_window_expired跳过未提交题、结束会话并生成无已提交答案的报告，现report_ready/1turn/0answers；因此不能宣称整份会话JSON不变。没有人工拆分旧转写、伪造子答案、恢复会话、重评分已有答案或发邀请。已向用户说明沿用计划新建会话验证。元数据证据`/private/tmp/interviewer-055-session-{baseline,after,transition}.json`。
- 055剩余边界/恢复：后续正式追问按新链路运行，历史v2接话仅只读重读兼容，不据语句猜测重写父子关系。问句保护和提示约束不代表任意自然语言都不可能误判；真实模型合成测试的面试外闲聊被保护拦截，未计自然回应验收成功。Chrome/IAB不可用，尚无真实浏览器截图或真人长时验收。静态可恢复保留的index与旧bundle；代码按055增量回退后空闲重启，保留历史事件/报告，不能覆盖数据库恢复旧状态。没有Git提交、推送或生产部署。

## 2026-09-17 · STEADY-AUDIO-054

- 状态：`verified（本机负载、浏览器及完整回归；真人长时/生产未验收）`。
- 目标：修复053后真实试音仍有小停顿的问题，保持持续收音、字幕、即时打断、暂停恢复与严格播放完成边界，不以实时性为由接受声音碎裂。
- 初始证据：最新试音两次输出内容23360/5920ms，媒体发布累计24744/6219ms，供应读取累计43/14.5ms；输出监控调度延迟333/316ms，仍超过240ms缓冲。此指标指向残留共享事件循环阻塞，不足以单独归因某个函数或证明浏览器欠载次数。
- 范围：先只读定位残留同步热路径并构造带并发业务负载的可重复验收，再针对证据修复。保留此前全部未提交工作、真实会话与录音；不重放候选资料、不自动恢复面试、不清空历史命令。接口/状态/存储若变化同步文档及回归，发布前检查活动候选人/worker。
- 验证计划：覆盖中文长音频连续性、抖动与短尾、首声取消/暂停/换题/旧输出、收音及字幕；执行相关与完整回归，并仅在空闲时更新本地前后端及worker。实际文件、失败过程、测量与恢复说明在本项补齐。

- 054定位：严格只读重演40场旧全量巡检，中位297.192ms/最大354.550ms；新定向读取在真实库尚未建索引时已降为7.373/11.238ms。完整复制成本仅在内存，未改真实会话。
- 054先红后绿：持SQLite写锁的巡检4项失败→服务联合125 passed；空command/get读先4 failed/12 passed→124 passed/2 skipped；定向候选接口10 failed→10 passed，PG合同7 failed→存储联合73 passed。命令误写不存在测试文件导致一次no tests，探针首次构建缺少 /web/src alias失败，均已修正且保留/private/tmp/interviewer-054-*日志。子任务py_compile写系统缓存遭sandbox限制，改为内存compile通过，未出现审批拒绝。
- 054组合证据：同一16秒/384000样本LiveKit PCM，叠加20ms空poll+terminal读、1s接管巡检和每帧完整只读fence；旧全量finish16.933秒、发包最大310.703ms、loop lag285.029ms，新定向finish16.011秒、发包最大49.244ms、lag28.664ms，两个变体样本全齐。新巡检p50 .079/max .187ms。范围仅列出的读取负载，未包含真实写入、STT和录制，不替代完整API或真人验收。证据/private/tmp/interviewer-054-combined-watchdog-results.json。

- 054实际文件：`app/services/{interviews,interview_agent,evidence_command_journal}.py`；`app/persistence/{interface,memory,sqlite,postgresql,read_only}.py`、`app/repositories/sqlite.py`、`migrations/007_interview_watchdog_candidates.sql`；`tests/{test_idle_evidence_reads,test_interview_watchdog_query,test_watchdog_realtime_054,test_postgresql_adapter}.py`；诊断 `app/web/streaming-audio-probe.html`、`scripts/streaming_audio_browser_probe.py`；`docs/{architecture,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,steady-audio-054,change-log}.md`。没有业务Prompt、模型路由、用户API或状态字段改动。
- 054最终回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2307 passed/8 skipped/159.16s**；`npm test -- --run` **384 passed/24 files**；`npm run build -- --outDir /private/tmp/interviewer-054-dist` 成功，7份构建文件与当前服务产物hash一致，无需静态切换。独立pycache compileall、7份文档链接和 `git diff --check`通过。没有在通过后无故重复全量测试。
- 054浏览器验收：正式压缩probe BUNYaMhh，叠加当前空poll、终态读、接管巡检及每帧完整只读校验；20秒PCM/118字公开中文21.52秒，worklet渲染跨度均等于内容时长，分别480000/516480样本完整，欠载0/静音0。3秒停供恢复48000样本，72000静音样本与实际3秒gap一致；首声取消0.2ms进入cleanup，无drained。结果 `/private/tmp/interviewer-054-browser-acceptance.json`；主动取消场景server probe_failed表示诊断连接被主动中止，非业务异常。4个随机房间、临时5178/5179服务及验收标签均清理。
- 054加载与核验：重复确认候选人0、worker active/reserved0后正常TERM旧API76101/worker76102及Beat，复用原环境启动API **80893**、worker **80894**。healthz200/ok、readyz200/ready、原前端bundlehash匹配；worker单节点pong、Beat正常派发。SQLite两个新部分索引实际EXPLAIN命中，3个媒体Docker running/OOMfalse。维护窗口API/worker新增锁错误、音频背压、Traceback、未读取Task异常均0。原试音JSON摘要与本轮初始及维护前均相同，仍in_progress/0答案；没有恢复面试、重写材料或发邀请。
- 054收尾失败留痕：临时runtime-check初次缺repo模块路径报ModuleNotFoundError，修正sys.path后通过；原日志保留，成功日志 `interviewer-054-runtime-check-final.log`。源代码未因这个工具路径问题更改。
- 054范围与恢复：已修复并验证本轮定位的历史巡检阻塞和空轮询锁竞争；没有削弱播放授权、延长缓冲或将断供计入真实消费。读取负载探针不等同完整API写入/上行STT/录制并发；真人长时、弱网多浏览器及真实PG007仍另验。回退仅054增量并空闲重启，保留053及以前未提交工作和业务记录；新增索引不更改业务行可保留。未Git提交/推送、未部署生产。

## 2026-09-17 · WARMUP-PLAYBACK-053

- 状态：`verified（本机故障回归、负载与真实TTS浏览器；真人长时/生产未验收）`。
- 目标：修复052上线后真实试音语音碎裂及前端运行错误导致整场暂停的问题，补足连续播放质量及试音打断验收。
- 初步证据：目标试音在05:04:48选择确认表达，05:04:49发生barge_in并被记为candidate_requested_pause，随后有runtime-problems上报；实际媒体断订阅/离线在05:07:21，晚于暂停，不能倒置为直接原因。浏览器fatal路径先发通用pause，后端忽略原因，使诊断丢失；当前持久记录不能确定原始前端错误分支。
- 播放调查：052播放器首包即播、欠载直接补零，没有启动/恢复缓冲。实际worklet确定性模拟4秒音频，20ms包每21ms到达产生约201ms静音及151次断点。旧验证核对样本完整性但未统计听感连续性，本轮补充抖动、发送节奏与取消竞态验证；不以首声时间或drained成功代替可听质量。
- 范围与边界：保留052及之前未提交改动；真实会话、转写和音频只读，不外发重放、不自动恢复或重写。先登记后修改，接口/状态/协议若调整同步文档与合同测试；本地更新前检查活动候选人和worker。实际修改、失败过程、验证结果与恢复方式在本项追加。

- 053继续定位：原22.08秒开场消费墙时69.052秒包含下游背压。隔离发布基准新旧均22.09秒，不能据此宣称改并行已解决3倍慢播。105字公开中文真实TTS快速消费21.6秒音频仅4.821秒；带完整只读表达校验的LK路径23.2秒音频23.656秒完成。真实API每20ms扫描全部3353条组织历史命令，空轮询p50 96.693ms/p95 130.223ms；owner调度延迟p95 249.397ms，命令定向仓储查询修复纳入053。只读诊断未领取真实命令或外发候选材料。
- 053中间回归：双路并行发布新增测试修前3 failed，消费者并发失败修前1 failed；修复后发布/输出/阶段指标组合73 passed。播放器抖动先红后绿9项通过。暂停分支新增测试首次21 pass/1 fail因fixture空turn在resume自动完成，修正真实asking状态后通过；后续相关后端162 passed、前端167 passed。保留全部失败日志，不修改恢复策略以迁就测试。
- 053生产压缩探针：20秒合成轻微抖动480000样本，播放20016ms/欠载0/插入静音0；118字公开中文真实TTS533760样本，内容22240ms，播放22251.9ms/欠载0/插入静音0。随机LiveKit房间已清理，真实会话只读校验2749次；结果在 `/private/tmp/interviewer-053-{production-acceptance,tts-production-acceptance}.json`。仅代表已验证的本机播放链路，真人长时与共享API负载另行核验。
- 053负载因果验证：同一6秒PCM与真实LiveKit并发20ms控制命令轮询，旧全量扫描12.409秒排空、新定向索引6.016秒，均收齐144000样本；循环lag p95从98.317降至7.625ms。采用只读来源的进程内SQLite副本，不落磁盘敏感副本、不领取真实命令。证据 `interviewer-053-combined-poll-results.json`；性能基准包含最终每个发送子任务启动时的权限检查。
- 053独立审查补漏：异步发送排队后、真正启动前撤销仍可能发送一帧；新增call_soon撤销及两路间撤销用例先2 failed，再为每个发送子任务加启动时校验。75项发布/输出/数值指标回归通过，独立复查通过。前端全量384 passed/24 files、主页面正式构建成功；已有大bundle提示保留。首声取消无drained、3秒断供一次完整缓冲后48000样本排空，5178/5179临时进程、房间、标签均清理。
- 053实际文件：`app/adapters/livekit_audio_output.py`、`app/core/interview_agent_metrics.py`；`app/domain/{candidate_runtime,interview_lifecycle}.py`；`app/persistence/{interface,memory,sqlite,postgresql,read_only}.py`、`app/repositories/sqlite.py`、`migrations/006_evidence_command_poll.sql`；`app/schemas/api.py`；`app/services/{approved_speech_output,evidence_command_journal,interview_agent,interviews,livekit_evidence_ingress,speech_output_interruption}.py`；`app/web/src/features/candidate/{Page.jsx,agent-experience.js,approved-pcm-player.js,live-speech-playback.js,presentation.js}`及同名测试；`app/web/src/features/interviews/agent-event-runtime.js`及同名测试；`app/web/streaming-audio-probe.html`、`scripts/streaming_audio_browser_probe.py`；`tests/{test_approved_speech_output,test_livekit_audio_output,test_interview_agent_stage_metrics,test_candidate_audio_failure_053,test_streaming_expression_lifecycle,test_evidence_command_query,test_postgresql_adapter}.py`；`docs/{architecture,api-design,domain-model,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log,warmup-playback-053}.md`及下述静态产物。保留052及此前未提交代码；未修改业务Prompt/模型路由/凭据。
- 053最终完整回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2263 passed /8 skipped /159.34s**，日志 `/private/tmp/interviewer-053-backend.log`；`npm test -- --run` **384 passed /24 files**，日志 `interviewer-053-frontend.log`；`npm run build -- --outDir /private/tmp/interviewer-053-dist` 成功，日志 `interviewer-053-build.log`。独立pycache compileall、10份文档链接与 `git diff --check` 通过。命令仓储定向测试先红，最终70 passed/2 skipped；8项完整套件外部环境skip不计为已通过。
- 053本机加载：再次确认活动候选人0、worker active/reserved均0后，资源先复制、index原子切换，保留旧bundle及 `/private/tmp/interviewer-053-previous-index.html`。新增 `index-DRdpRMQ5.js`、`VrmAvatar-IfCK3Aro.js`；CSS保持 `index-BDdseLuw.css`。正常TERM原API65506/worker65507及Beat，复用原环境启动API **76101**、worker **76102**。healthz=200/ok、readyz=200/ready，线上JS/CSS hash匹配，worker单节点pong、Beat正常派发，四项新音频内部指标已加载。日志 `interviewer-053-{publish,restart,verify}.log`。
- 053数据与运行核验：SQLite幂等建立命令部分索引，实际EXPLAIN确认命中；PostgreSQL仅新增006迁移文件，未在目标生产数据库执行。原试音会话整体JSON摘要相对本轮初始及维护前均不变，仍paused/0答案；未自动恢复、重新评分、发邀请或重写材料。三个媒体容器running/OOMfalse，未无故重建；维护窗口API/worker新增数据库锁、音频背压、Traceback、未读取Task异常均0。证据 `/private/tmp/interviewer-053-runtime-summary.json`。
- 053边界与恢复：已修复可复现的阻塞和取消竞态，原场次具体浏览器fatal原因因旧日志缺失不能恢复，也不把6秒合成负载当作完整重现69秒或真人长时验收。有限缓冲不能弥补持续低速供给；弱网、多浏览器、目标PostgreSQL与真人整场仍另验，不标closed。静态回退可恢复保留index和旧bundle；代码仅回退053增量并在空闲时重启，保留历史资料/052。新增索引不改变业务行，可保留；未Git提交/推送或生产部署。

## 2026-09-17 · CONTINUOUS-CONVERSATION-052

- 状态：`verified（完整回归、真实合成媒体及本机加载；真人长时/生产未验收）`。
- 目标：按用户要求实际落地持续识别/字幕、可撤销提前准备、减少串行模型调用和流式播报，并补全面试范围内的自然接话。
- 设计范围：沿既有Evidence、AnswerEndpoint、ApprovedConversationAct与Expression seam拆开即时交流和较慢证据处理，保持服务器final、所有权、输入revision和提交围栏。预理解不等于提交；新发言、换题、断线或暂停撤销旧准备/播报。Skill及企业资料独立可选，不增加用户配置步骤。
- 验证计划：先做带可控延迟/取消的合成回归，覆盖持续字幕、补充/改口撤销、未预设交流、流式首声/欠载/打断与最终完整性；再做全量回归、前端构建和本机无活动面试/任务时更新。允许用合成材料验证现有模型路由，不重放真实候选资料，不代替真人整场延迟验收。
- 关联：051报告中仍开放的交互与媒体时延问题；保留037–051未提交成果与原暂停会话。实际修改、失败过程、验证、加载及尚未完成事项在本项追加。

- 052过程证据：连续识别底层新增回归修前3 failed/1 passed，修后4项通过；并行理解与提交边界新增18项通过，包含final仅末尾新增话语、旧口头/按钮结束许可撤销、恢复识别期间新输入不启动过时推理。TTS准备阶段续说仅服务端PCM即可取消旧合成，全部音频仍进入同一capture；接话/公司问答24项通过。
- 052失败留痕：初次端点组合11 failed/69 passed，主要旧合同要求推理时关闭ASR或模拟器按开流消耗下一句；分别复核后更新为有声PCM绑定话语并保留逐字转写/录音完整性断言。首次全量在旧SimpleNamespace预览缺少真实协议字段处9 failed/558 passed/2 skipped并中止挂起用例；修正模拟器为真实预览类型及有界等待。第二次全量2200 passed/8 skipped/8 failed，7项旧final修正不重开识别断言和1项跨追问模拟脚本空流消耗，正在逐项复验。原失败日志保留/private/tmp/interviewer-052-{endpoint-first,backend,backend-final}.log。
- 052播放证据：真实IAB Chromium/LiveKit通过3秒停供源时钟不增长、6秒突发不超缓冲、首声取消不drain；真实已配置TTS两次纯合成短句从请求到首次播放420.8/454.5ms（不包含1.1–1.4秒冷媒体连接及STT/语义理解）。正常归档样本109440/101760，未重放真人内容。自然接话真实模型14/14通过。明细/private/tmp/interviewer-052-streaming-acceptance.json，限制见[052说明](continuous-conversation-052.md)。
- 052环境与发布前检查：发现LiveKit仍广播192.168.0.108而当前网卡192.168.0.106；确认房间0/候选人0后执行scripts/local-media.sh up，仅重建LiveKit，Redis/Egress保持运行。首次sandbox进程检查被系统权限阻止，使用已授权本地诊断的提升权限重试成功；未出现自动审批拒绝。静态源前端363项与构建先通过，但独立复核发现class.toString生成worklet在生产minify后语法失效，保留失败证据并修复，不以源码测试替代产物测试。
- 052数据基线：本轮重启前只读核对原iv_808c4900d30342a4已经是report_ready、5份答案、updated_at=2026-09-17T03:27:10Z，与051当时paused基线不同；不能宣称仍暂停或与051摘要相同。维护前另存052摘要作为重启前后对照，未主动恢复、重写或重新评分该会话。

- 052最终补验过程：生产压缩worklet问题先红后绿，固定序列化类名绑定后，前端364 passed/24files，正式构建成功；新增minified bundle→实际worklet渲染与drained回归。父任务IAB成功验证Vite正式产物：停供2443–5444.3ms期间源时钟1000ms，恢复后消费48000样本，6560.9ms device-drained，6617.1ms清理；子任务无浏览器控制权限时交回父任务操作，没有改用未授权控制方式。证据/private/tmp/interviewer-052-production-acceptance.json，临时5178/5179、房间和标签已清理。
- 052第三次全量2210 passed/8 skipped/1 failed：唯一失败暴露语音已选定后 `_say` 返回再次重置播放超时起点的时序缺陷；删除重复计时，只以同步playback_selected事实起算，补可控延迟回归。该新增测试首次漏设proposal revision而失败（非业务错误），补齐正确状态后重跑；日志保留interviewer-052-playback-deadline*.log。最终全量正在复验，不把前次失败记为通过。
- 052第四次全量2211 passed/8 skipped/1 failed：重试预算测试在失败计数增加后、异步恢复播报开始前就检查speaking，漏掉播放完成确认，假时钟不再前进导致超时。用通知gate稳定复现后，仅修正测试为等待实际播报再确认结束，保留三次预算、噪声不重置及不提交断言；相关100项与预算9项通过。没有扩大业务超时或改重试次数；证据interviewer-052-{backend-release,semantic-budget-race,semantic-budget-after,preparation-budget-after}.log，随后完整复验。
- 052第五次全量2211 passed/8 skipped/1 failed：全部交互项通过，旧会话过期测试把固定deadline与另建runtime的系统时钟比较，跨秒得到04:11:24/25差异。`tests/test_interview_session_aggregate.py`仅让runtime复用已有固定时钟的InterviewService，保留严格过期时间及状态断言；该文件8项通过。证据interviewer-052-{backend-acceptance,session-clock}.log，业务过期策略未改。
- 052实际文件：`.env.example`；`app/adapters/{livekit_audio_output,livekit_media}.py`；`app/core/prompt/conversation_reception.py`、`app/model_gateway/gateway.py`、`app/providers/mock/provider.py`；`app/services/{answer_endpoint,approved_speech_output,continuous_stt,conversation_understanding,interview_agent,interview_evidence,livekit_evidence_ingress,spoken_supplement,speculative_turn_preparation}.py`；`app/web/src/features/candidate/{agent-experience,approved-pcm-player,live-speech-playback}.js`及同名测试、`livekit-output-connection.test.js`；`app/web/streaming-audio-probe.html`、`scripts/streaming_audio_browser_probe.py`；`tests/{test_approved_speech_output,test_company_question_integration,test_continuous_listening_052,test_conversation_reception,test_conversation_reception_integration,test_declined_answer_integration,test_livekit_audio_output,test_natural_reception_052,test_nonclosing_answer_endpoint,test_nonclosing_capture_integration,test_parallel_conversation_052,test_semantic_turn_endpoint,test_spoken_supplement,test_spoken_supplement_integration,test_streaming_expression_lifecycle}.py`；`docs/{api-design,architecture,change-log,continuous-conversation-052,development-progress,domain-model,implementation-roadmap,known-issues-and-remediation,model-provider-plugins,retrieval-and-evaluation}.md`。静态发布文件及进程、最终验收在后续记录。

- 052最终完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2212 passed /8 skipped /155.48s (0:02:35)**，日志 `/private/tmp/interviewer-052-backend-complete.log`。前端 `npm test -- --run` **364 passed /24 files**，`npm run build -- --outDir /private/tmp/interviewer-052-dist` 成功，保留既有大bundle提示；日志 `interviewer-052-frontend-verified.log`、`interviewer-052-build.log`。独立pycache compileall、10份文档链接及 `git diff --check` 通过。8项外部环境测试未计入通过。
- 052本机加载：再次确认LiveKit活动候选人0、worker active/reserved=0；资源先复制、index最后原子切换，加载 `index-DJBosd8n.js` / `index-BDdseLuw.css`，旧bundle及 `/private/tmp/interviewer-052-previous-index.html` 保留。正常TERM原API/worker及Beat后，按原环境启动API **65506**、worker **65507**，流式TTS采用新的默认启用设置。healthz=200/ok、readyz=200/ready、线上资源hash等于构建产物；worker单节点pong、Beat继续派发。证据 `interviewer-052-{publish,restart,verify}.log` 及对应结果JSON。
- 052更新后只读核验：三个媒体Docker均running且OOMfalse，LiveKit当前广播地址与本机一致；维护窗口API/worker未新增数据库锁、音频背压或Traceback。原会话相对052维护前整体JSON摘要相同，仍report_ready、5份答案；此处不沿用051的paused状态。证据 `/private/tmp/interviewer-052-runtime-summary.json`。没有恢复真实面试、重写转写、发邀请、执行数据库迁移或Git提交/推送。
- 052完成范围与恢复：本地自动化、真实供应商合成材料、正式压缩产物浏览器及运行加载已验收，状态不标closed或生产通过。普通回答保守等待、过渡语与题目分段资产复用、真人长时/弱网/多浏览器和足量端到端p50/p95仍待后续。回退语音可显式关闭 `INTERVIEWER_STREAMING_TTS_ENABLED` 并在空闲时重启；前端可恢复保留index和bundle。代码回退只针对052增量，保留037–051及历史资料。具体能力与边界见[052说明](continuous-conversation-052.md)。

## 2026-09-16 · REALTIME-ERRORS-AND-LATENCY-051

- 状态：`verified（故障回归与本机加载；真人语音及交互优化未验收）`。
- 目标：按用户指定优先排除真实面试后台报错与暂停，再以证据分析交互、切题、字幕和低延迟改进。
- 初步诊断：本场第6话题02:51:01Z因 LIVEKIT_INGRESS_SINK_BACKPRESSURE 触发音频采集失败暂停；后续02:51:23Z断订阅/离线发生在暂停之后。worker同时存在SQLite database is locked；第5话题有两次理解canonical evidence_quotes重复校验拒绝。现有确认路径在理解期间关闭识别流，存在真实字幕等待；各问题因果仍分别复核，不混为单一网络故障。
- 计划：追踪采集背压、SQLite事务和引用解析错误，补充真实形状的合成回归；修复后核验前后端/worker/容器，保留原暂停面试和已有答案。延迟与泛化交互先形成可核查分析，不以扩大超时、静默丢音频或绕过权威证据掩盖故障。
- 边界：日志、数据库与容器只读诊断不输出凭据或完整转写；不把真实候选资料重放到外部模型。代码与相关文档、验证、部署及未完成事项由本工作项统一留痕。

- 051引用阶段：合成重复口述用例在修复前稳定复现 understanding_canonical / unique_items / completion_basis.evidence_quotes，1 failed；统一引用解析去重后，语义/证据/公司问答/Prompt合同定向125 passed（1.85s）。原句和位置不变、非法重复ID仍拒绝。日志 /private/tmp/interviewer-051-quotes-{before,after}.log。

- 实际修复：音频入口保留250帧队列及原5秒预算，将瞬时满队列改为按首次饱和时已接受水位有界等待；持续慢消费不能逐帧续期，失败/关闭唤醒等待，不丢弃或重排PCM。dispatcher 扫描使用统一只读事务，SQLite不再为扫描执行BEGIN IMMEDIATE；实际claim、租约、CAS仍用原写事务。不同证据编号解析为相同原文时只规范canonical引用列表，原转写、编号、位置、公司反问排除和wire校验不变。
- 实际文件：`app/adapters/livekit_audio_ingress.py`、`app/core/prompt/understanding_references.py`、`app/workers/dispatcher.py`、`app/persistence/{interface,errors,read_only,sqlite,memory,postgresql}.py`；`tests/{test_livekit_audio_ingress,test_semantic_turn_policy,test_speech_quality_governance,test_dispatcher_read_only,test_persistence_contract,test_postgresql_adapter,test_postgresql_integration}.py`；`docs/{architecture,database-and-vector-storage,retrieval-and-evaluation,known-issues-and-remediation,development-progress,implementation-roadmap,change-log,realtime-interview-incident-051}.md`。没有改业务Prompt/模型路由、前端源码/构建、数据库配置或历史业务数据。
- 修前证据：600帧合成突发、每帧仅两次协作调度，旧入口在10.52ms误报背压，已交付249帧；回归先失败再修复通过。两个真实SQLite连接的扫描回归修前2 failed（写锁），修后通过；不能因此将数据库竞争和本场音频失败断言为单一因果链。
- 定向验证：语义/证据/公司问答/Prompt合同125 passed；`tests/test_livekit_audio_ingress.py tests/test_continuous_stt.py tests/test_livekit_evidence_supervisor.py` 75 passed/5.69s；持久化/dispatcher45 passed、2项外部PostgreSQL环境skip。音频新增用例覆盖600帧逐字节顺序、容量不变、慢消费不续期、失败穿透、关闭取消和水位，日志 `/private/tmp/interviewer-051-ingress-tests.log`。并行只读复核未发现新增丢帧、永久等待或证据误放行。
- 完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2153 passed /8 skipped /154.96s**，日志 `/private/tmp/interviewer-051-backend.log`；`app/web` 下 `npm test -- --run` **361 passed /23 files**，日志 `/private/tmp/interviewer-051-frontend.log`。独立pycache的compileall、8份文档本地链接、`git diff --check`通过。本轮全量没有失败，定向修前失败证据保留；未调用外部模型或重放候选人内容。
- 本机更新：确认LiveKit活动候选人0、worker active/reserved均0后正常TERM原API30857/worker30858及Beat，复用原环境启动API53158/worker53159。healthz200/ok、readyz200/ready、worker单节点pong、Beat继续派发；静态index-DlDiYUe5.js / index-BDdseLuw.css与磁盘hash一致，051未改前端产物。三个媒体Docker保持running/restarts0/OOMfalse，不作无依据重启。日志 `/private/tmp/interviewer-051-{restart,verify}.log`。
- 数据与观察边界：只读核对原暂停会话整体JSON摘要完全相同，仍5份答案；更新窗口未见新database is locked、异常栈或背压超时。诊断汇总 `/private/tmp/interviewer-051-runtime-summary.json`；当前为空闲冒烟，不等于真人并发负载/语音整场验收。没有自动恢复原面试、重写转写、发邀请或Git提交推送。
- 未完成与恢复：接话固定分类、理解时字幕停顿、承接语绕过预生成音频仍是开放问题，方案和分阶段验收见[051报告](realtime-interview-incident-051.md)，不标closed。真实PostgreSQL与真人长时语音需独立验收；同步事件写放大也未解决。若需回退，只恢复051增量、保留037–050既有工作，并在无活动面试/任务后按原环境重启API/worker；未改schema或业务资料，无数据迁移回退。维护脚本 `/private/tmp/interviewer-051-restart.py` 和结果记录保留。

## 2026-09-16 · WARMUP-CAPTURE-PHASE-050

- 状态：`verified（回归、合成界面及本机加载；真人复验待进行）`。
- 目标：修复新版面试试音时错误显示“暂未收音”、音量条不更新及本地语音起止信号被抑制。
- 诊断：本场服务端已收到音频并连续产生暖场 ASR 句段；v3 暖场尚无正式题、dialogue_state=awaiting_next_decision，前端未检查 calibration 已完成便进入 planning，清除了 Evidence 就绪状态并屏蔽 VAD 信号。另音量回调只更新内部状态、不通知订阅页面。
- 计划：限定正式规划阶段的进入条件；保留暖场 capture 与服务端确认边界；有界发布音量变化并准确显示停止原因。补充 v3 暖场/快照/VAD/重试与音量订阅回归，更新问题、进度、路线图，重建并仅发布静态资源。
- 验证与边界：使用合成媒体/事件测试，不重放真实录音或转写、不写候选人数据、不重启正在使用的 API/worker、不操作用户面试标签。完成时补记命令、结果、实际文件及真人收音仍需复验的限制。

- 实际文件：app/web/src/features/candidate/agent-experience.js、agent-experience.test.js、Page.jsx、Page.test.jsx；docs/architecture.md、known-issues-and-remediation.md、development-progress.md、implementation-roadmap.md、change-log.md；app/web/dist/index.html 与新 bundle。未改服务端代码、协议、数据库、Prompt、评分或硬件权限。
- 回归证据：新增测试在修复前9 failed/84 passed（/private/tmp/interviewer-050-regression-before.log），对应阶段误判、缺少开流与音量无通知/非法值；修复后定向107 passed，追加真实VAD回调与重试快照后全前端 `npm test -- --run` 361 passed/23files（interviewer-050-frontend.log）。后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_interview_agent_contracts.py tests/test_livekit_evidence_supervisor.py tests/test_planning_runtime_fences.py tests/test_enterprise_interviewer_flow.py --tb=short` 122 passed/14.52s（interviewer-050-backend.log）。未调用真实模型或重放候选人音频。
- 构建与加载：`npm run build -- --outDir /private/tmp/interviewer-050-dist` 通过，仅保留既有大bundle提示。先发布资源再原子切换index，在线 index-DlDiYUe5.js / index-BDdseLuw.css hash一致，healthz=ok；API/worker未重启，旧bundle保留，原index备份/private/tmp/interviewer-050-previous-index.html。/private/tmp/interviewer-050-{build,publish}.log。
- 页面验证：隔离预览使用真实CandidateSignalList组件与合成状态，浏览器确认正在试音有绿色音量条、等待开口及字幕确认/规划/暂停原因准确、停止状态音量归零。未请求设备权限、连接真实面试或刷新用户标签，验收标签已关闭。
- 边界与恢复：真人当前浏览器仍运行旧脚本，需要本人刷新后重新试音以加载修复；自动化不等于真实麦克风/LiveKit/供应商整链路已复验。静态发布可恢复上述index备份，源码按050增量恢复且保留此前工作；没有业务数据副作用、Git提交/推送或生产部署。

## 2026-09-16 · PLAN-PREPARATION-REUSE-PROGRESS-049

- 状态：`verified（回归、真实资料离线复用、界面与本机加载；生产单独验收）`。
- 目标：减少相同资料重复创建时的模型等待，让首次问题整理展示真实进度。
- 诊断：本次计划 plan_e42eb5b6aad34593 已于11:12:12Z批准，起始11:09:42Z，耗时150秒；12道来源题、56个考察点、3道简历题。请求200且未卡死；每次全量整理、页面没有阶段/数量反馈导致误判。
- 设计：复用同组织已批准题库计划内、当前Prompt版本、相同来源版本/内容/能力范围的校验通过单元，简历题额外绑定候选人及审查；未命中的题目继续原有有限生成。prepare保留JSON响应，新增可选SSE实际进度，最终仍通过原装配/版本/批准事务，完成后才返回计划。未新增预约或面试入口。
- 验证计划：复用与失效、租户/简历隔离、错误/取消、不保存半成品、流式认证及真实进度、前端保留输入和超时；实际已保存资料仅在禁用模型的内存副本检验，不重复向外部发送候选资料。完成时补记文件、命令、结果、本机加载及限制。

- 049阶段结果：原有生成回归35通过；新增复用/流式用例首次10通过、2失败（测试hash漏sha256前缀、Python3.9不支持anext内建），修正测试后12通过，追加组织上下文延迟绑定后计划组合48通过。前端350通过/23files，构建成功（保留既有大bundle提示）。
- 049全量首次：2123 passed/8 skipped、1 failed/150.91s。失败为旧Node smoke用data URL加载HTTP单文件，新增相对模块import无法解析；改为加载真实文件模块图，保留认证/角色/超时/取消断言，不改业务实现来迁就测试。
- 049实际资料隔离验收：只读读取本机题库/已有计划到InMemoryStore，网关强制禁止任何模型调用，12题/56点全部复用、approved，123/127ms；没有外发资料、真实业务DB写入或预约。日志/private/tmp/interviewer-049-reuse-check.log。该数值是内存装配测量，不代表生产端到端延迟。
- 049界面：真实PlanCreate+Shell在合成隔离页1280/462宽检查完成，18/56实际回调、12点复用、等待秒数和按钮显示完整，窄屏scrollWidth=clientWidth=462；未连接业务API。首次预览监听被sandbox拒绝，按本机只读预览授权重试成功；未发生审批拒绝或数据发送。临时标签已关闭，原用户页未刷新。

- 049实际文件：app/services/plan_assembly.py、app/api/routers/plans.py、新增app/transport/http/progress.py；app/web/core/http.js、新增progress-stream.js/http.test.js、src/features/plans/PlanCreate.jsx/PlanCreate.test.jsx/plans.css；新增tests/test_plan_preparation_progress.py、修改tests/test_web_frontend_modules.py；api-design/domain-model/retrieval-and-evaluation/known-issues-and-remediation/development-progress/implementation-roadmap/change-log及app/web/dist构建。没有Prompt/模型路由/凭据/数据库迁移修改。
- 049本机加载：LiveKit候选人0、worker active/reserved均0后正常TERM旧API12736/worker12737及Beat，复用原环境启动API30857/worker30858。前端index-RZ6csOiK.js、index-BDdseLuw.css在线hash匹配；healthz200/ok、readyz200/ready，worker单节点pong、Beat继续派发。/private/tmp/interviewer-049-{restart,publish,verify}.log。旧bundle及/private/tmp/interviewer-049-previous-index.html保留供恢复。
- 049在线流式验证：只提交不存在的合成来源ID，6ms收到progress和JOB_POSITION_NOT_FOUND终止error（404语义），没有模型调用/计划或预约；/private/tmp/interviewer-049-stream-smoke.log。只读打开本次原计划确认已启用、简短题存在、按钮仍去面试会话安排；验收页关闭，用户原标签未操作。
- 049边界/恢复：首次或更改资料仍需模型整理，未新增持久后台任务或承诺刷新后继续生成；断线/超时提示先看列表再重试，已提交计划不会因断线回滚。只复用批准计划中的成功结果，失败请求的未保存结果不跨请求复用。源码保留此前工作、不Git提交/推送、不生产部署；静态入口可恢复上述备份，代码需按049增量回退且保留历史计划。生产和真人长时语音仍单独验收。

- 049最终验收：后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2125 passed/8 skipped，150.47s**（/private/tmp/interviewer-049-backend-final.log）；前端 `npm test -- --run` **350 passed/23files**；`npm run build -- --outDir /private/tmp/interviewer-049-dist`、compileall、git diff --check通过。旧模块smoke及流式组织绑定定向18项通过，问题已验证且兼容JSON/generate继续保留，不标closed。8个外部依赖skip不计为生产通过；历史失败记录保留。

## 2026-09-14 · PLAN-GENERATION-TIMEOUT-048

- 状态：`verified（最终回归、真实模型合成体量、本机加载；真实候选人与生产环境另行验收）`。
- 目标：修复实际创建计划时模型请求超时及错误归因，明确创建计划与进入面试的区别。
- 诊断：最近一次 inquiry_units.v1 三批调用中一批耗时58984ms、输出5011tokens，另两批在60004/60002ms以 provider_timeout 失败；此前两批同样60秒超时。原按4道长题批处理，逐知识点重复输出参考答案，且所有异常被统一显示为考察范围错误；失败未保存计划或创建会话。
- 方案：按知识点数量限制模型批次；v2只返回原答案片段的范围编号，由统一Prompt校验seam还原逐字引用，保留原题、所有评分点与后续版本检查；区分超时、供应商不可用、结构/来源校验错误。创建按钮及等待文案明确计划用途，流程仍为计划→预约→邀请进入。
- 验证计划：真实规模题库的完整生成、单题跨批合并、不遗漏/重复评分点、超时取消/不落半成品、错误脱敏、历史冻结执行与前端保留输入；完成后补记实际文件、测试结果、本机加载和失败恢复。
- 048实际修改：app/core/prompt/inquiry_units.py、app/services/plan_assembly.py、app/model_gateway/gateway.py、app/providers/mock/provider.py；app/web/src/features/plans/PlanCreate.jsx及PlanCreate/Page测试、app/web/src/App.test.jsx；新增tests/test_inquiry_preparation.py，调整test_inquiry_unit_assessment/test_plan_coverage_diagnostics/test_optional_interview_context；api-design/model-provider-plugins/retrieval-and-evaluation/adaptive-interview-contract/known-issues-and-remediation/development-progress/implementation-roadmap/change-log文档及app/web/dist构建。没有数据库迁移、模型路由/凭据修改或真实计划/预约写入。
- 048最终模型验证：按单一原题分批的最终版本使用当前配置真实模型，在纯合成10题/51点上完整创建approved成功，116.568秒，12次请求全部首次通过（无需修正），每次18.099–36.280秒，输出1275–2587tokens；全部51点评分范围保留。日志/private/tmp/interviewer-048-synthetic-model-source-batches.log。没有真实候选人资料读取/出站、业务数据库写入或新面试/预约。
- 048最终回归过程：最终批次定向93 passed/4.97s；第二次全量2111 passed/8 skipped、1项既有semantic_turn_endpoint时序等待超时。该模块单独复查6 passed/1.45s，没有放宽断言或改动语音实现；取消并行模型压测后再次全量验收，结果待补。
- 048模型完整验证阶段：补强问句并增加一次局部修正后，纯合成10题/51点完整创建approved成功，131.952秒，10次真实模型调用（其中1批内容分区重做），单次约28.8–52.2秒；全部评分点保留，未写真实业务数据库/未创建面试。随后将每批进一步限定为一个原题，Schema直接约束该题评分点ID和确切数量，避免跨来源编号串用；最终版本再次验收记录待补。
- 048修正后定向回归：计划装配、单元评分、可选上下文与预约→执行→报告共93 passed/4.62s；覆盖只修正失败批次一次、成功批次不重复生成，失败仍不落计划。compileall及git diff --check通过。
- 048过程记录：首次定向51项通过，增加体量/取消/引用测试后71项通过。首次前端1 failed/337passed，为Page旧按钮断言遗漏，修正后338 passed/22files。后端全量2110 passed/8 skipped（148.98s）；后续单问句修正重试和诊断细化另做定向回归。
- 048真实模型发现：真实资料重放被自动审批拒绝（候选资料向外部模型再次发送未明确授权），没有执行。改用纯合成10题/51点评估；旧临时process辅助脚本丢失导致首次启动失败，重建仅验证自有项目进程的只读辅助后继续。两次纯合成完整创建在73.704s/38.223s遇到单问句校验失败；模型调用本身均在约32–40秒返回，没有将失败记为通过。进一步明确单问句示例并只修正无效小批一次，总预算不延长。过程日志保留在/private/tmp/interviewer-048-*。


- 048最终验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2112 passed/8 skipped，150.29s**，日志/private/tmp/interviewer-048-backend-acceptance.log；前端 **338 passed/22files**，/private/tmp/interviewer-048-frontend-final.log；构建成功，仅既有大bundle提示，/private/tmp/interviewer-048-build.log。此前一次时序失败及独立复查保留，不删除或用放宽断言掩盖。
- 048本机加载：活动候选人0、worker active/reserved均0后正常TERM旧API99078/worker99079及Beat，复用原环境启动API **12736**、worker **12737**。healthz200/ok、readyz200/ready，单worker pong、Beat继续派发；JS `index-BhK_DERN.js`、CSS `index-B-JpSqLW.css` 在线hash匹配。记录/private/tmp/interviewer-048-{restart,publish,verify}.log；旧bundle与/private/tmp/interviewer-048-previous-index.html保留，可恢复前端首页指向。
- 048界面核验：本机/#plans/new实际渲染1280视口，标题与按钮为“创建面试计划”，卡片明确面试会话预约→邀请进入，无按钮溢出；未提交表单，临时验收标签11已关闭，用户原/#skills标签未操作。刷新计划页即可使用新版本。
- 048边界：没有Git提交/推送、生产部署、真实候选人资料出站或代建预约。模型验收为纯合成资料，未重放被审批拒绝的真实资料请求；当前保留180秒整体预算和失败关闭，大规模异步任务/跨请求结果复用未在本次引入。回退代码需与对应生成协议同步，不修改历史冻结计划或候选人记录。

## 2026-09-14 · BANK-FIRST-PREPARATION-047

- 状态：`verified（仓库回归、合成真实模型/界面/执行闭环、本机加载；生产与真人长时语音另行验收）`。
- 目标：简化面试准备，岗位、候选人和题库即可创建；删除重复技能、题数/深度填写及新计划的二次启用步骤。新计划按可用题库实际考察内容生成，岗位要求不再是前置条件，Skill/企业资料继续有则使用、无则忽略。
- 设计：在 PlanAssembly 内提供简洁 prepare 入口，复用来源隔离、知识点评分依据、短问题生成、版本检查与不可变执行合同。自动确定范围与预算并记录自动校验通过；保留历史 generate 和人工草稿的原有语义，不改写已有计划/真实候选人数据。
- 验证计划：题库含 Python、旧岗位含 Flask、无岗位要求、少量题目、不可用/跨租户来源、生成并发变化、预约执行及前端单页流程；相关与全量回归、构建、合成浏览器验收后空闲更新本机。实际文件、结果、失败和恢复方式完成时补记。

- 047阶段验证：后端全量 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2090 passed,8 skipped/150.52s**；前端 `npm test -- --run` **338 passed/22files**。三步创建用例替换为单页用例，历史审批/CAS/越权/迟到结果用例保留。独立合成 UI 真实渲染桌面1440与iframe462宽，后者 `clientWidth=scrollWidth=462`，截图 `/private/tmp/interviewer-047-{desktop,mobile-frame}.png`，无真实业务页面填写或邀请发送。
- 047失败记录：首个5分钟测试误期望覆盖全部题库两项能力，实际合理筛选成一项，改为验证真实有界范围；后端首次1 failed/14passed后修正通过。前端旧Page/App三步断言分别失败，改为新单页行为后全量通过。最初直接headless462截图被Chrome最小视窗裁切，改用准确462 iframe验证，不将原裁切截图当成布局通过。
- 047追加发现：本机没有 llm.chat_json/question_generation 路由，合成验收最初执行开发mock（16ms、真实调用0），随后两次增加真实调用断言仍失败，保留 `/private/tmp/interviewer-047-real-model*.log`。脱敏诊断确定用途路由缺失，不能宣称真实模型通过。拟先在内存复用现有 interview_turn_understanding 的语言模型做合成准备验收，再通过已校验模型管理API补齐该用途；此配置变更可按新路由ID撤销，不改候选人或历史计划。

- 047实际修改：`app/schemas/api.py`、`app/api/routers/plans.py`、`app/services/{plan_assembly,interviews}.py`、`app/domain/adaptive_interview.py`；`app/web/src/features/plans/{PlanCreate,Page}.jsx`、`plans.css`、`{PlanCreate,Page}.test.jsx`、`app/web/src/App.test.jsx`；`tests/test_bank_preparation.py`、`tests/test_enterprise_interviewer_flow.py`；11份相关文档（architecture/api-design/domain-model/retrieval-and-evaluation/model-provider-plugins/adaptive-interview-contract/known-issues-and-remediation/development-progress/implementation-roadmap/change-log、CONTEXT）；重建 app/web/dist。没有新增业务Prompt或Schema协议版本，复用既有集中化 inquiry_units.v1，新增API请求Schema与合同basis字段有对应测试。
- 047真实模型最终验证：先内存补用途路由，完整prepare一次真实调用，26.84秒得到Python/Redis的4个单元、approved计划且无Flask；再补齐本机路由后，完全从当前配置只读加载（不使用测试路由）再次通过，27.62秒/1次真实调用。日志`/private/tmp/interviewer-047-{real-model-configured,live-route-acceptance}.log`。两次均仅合成题库/候选人，审计留内存，不产生真实面试计划。
- 047本机配置：通过已带审计的 POST /admin/model-routes 新增 `route_ac769e6d712246a3`（llm.chat_json/question_generation），复用原 `route_906bd7fa08f54420` 的语言模型 `model_cfg_64fd050506a04b8c`，没有改其他路由；标准路由测试HTTP200。操作结果 `/private/tmp/interviewer-047-route-result.json`，凭据未输出或另存。此项是本机配置/审计写入，区别于合成模型验证的业务零写入。
- 047本机加载：LiveKit候选人0、worker active/reserved均0后正常TERM旧API/worker/Beat，复用原环境启动 API **99078**、worker **99079**。healthz=200/ok，readyz=200/ready=true，worker单节点pong且Beat继续派发任务。新prepare契约已在运行API注册，必填仅岗位/候选人/题库，空请求422未写计划。静态资源先复制后原子替换入口，保留旧bundle和 `/private/tmp/interviewer-047-previous-index.html`。证据 `/private/tmp/interviewer-047-{restart,publish,verify}.log`；最后仅修正两处CSS主题变量并重建静态资源，不再重启业务进程。
- 最后CSS构建首次在仓库根目录执行，因该目录无package.json返回254，未发布该次产物；随后在app/web执行构建。失败日志`/private/tmp/interviewer-047-build-theme.log`保留。
- 最终静态入口发布通过，证据`/private/tmp/interviewer-047-publish-final.log`。新建独立浏览器页打开实际8000/#plans/new，界面已自动带入现有岗位、候选人与就绪题库，显示单页“准备面试”按钮；只查看页面，未提交真实计划，原面试定制页未刷新。
- 047恢复/限制：可恢复上述旧index；代码回退应只处理047增量，保留037–046未提交工作。路由配置回退须按新增route ID通过带审计的维护事务撤销，不能覆盖其他路由或抹除审计。未迁移或修改历史计划、候选人数据，未发送真实邀请/邮件，未部署生产或Git提交推送。旧generate及草稿审批兼容仍在，不标closed；全量8项外部环境skip、既有大bundle提示保留。本次并未代替真人多轮语音和生产验收。

## 2026-09-14 · CONVERSATION-RECEPTION-046

- 状态：`verified（仓库回归、合成真实模型/语音与本机加载；真人长时交互仍待验收）`。
- 用户目标：面试官应理解并回应“稍等”等交流请求，能处理重说、没听清、改口、暂停及意图不明等情况，不能一直停在整理回答而没有交互。
- 只读定位：目标会话已由候选人暂停；服务端收到短句转写，首轮理解发生 understanding_content 合同拒绝，后续多次新输入取消准备。语义 continue_listening 分支仅通知界面、不生成语音；取消分支也没有撤下 preparing 提示。这场执行的是历史 v2 九题计划，其基本对话能力仍须修复，不改写该冻结计划或历史回答。
- 实施方向：把低风险交流请求的轻量语义识别与完整答案/追问准备分开，短回应继续走既有审批表达与可打断语音链路；保持当前回答、同意、证据和提交门禁。补齐等待回应、重复请求去重、实际播放后等待、迟到结果撤销、有限失败反馈和准备态恢复；所有 Prompt/响应 Schema 放入 app/core/prompt/，版本化并增加合同测试。
- 验证计划：真实模型仅使用合成话语与合成题目；集成覆盖服务端语音→理解→实际表达→恢复倾听/打断/暂停及评分排除，保留失败记录。完成后按无活动候选人和无 worker 任务条件更新本机；不恢复或重写本次已暂停会话。实际文件与结果见下文。

- 实际修改（046）：`app/core/prompt/{conversation_reception.py,semantic_turn.py,contracts.py}`、`app/domain/interview_agent.py`、`app/model_gateway/gateway.py`、`app/providers/mock/provider.py`、`app/services/{conversation_understanding.py,interview_evidence.py,answer_endpoint.py,spoken_supplement.py,livekit_evidence_ingress.py,interviews.py}`；`tests/{test_conversation_reception.py,test_conversation_reception_integration.py,test_semantic_turn_policy.py,test_semantic_turn_endpoint.py,test_declined_answer_integration.py,test_spoken_supplement_integration.py,test_company_question_integration.py}`；`app/web/src/features/candidate/agent-experience.test.js`；同步`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,interviewer-agent-architecture,change-log}.md`。
- 已验证：接话合同与整链91项通过，日志`/private/tmp/interviewer-046-reception-integration-final.log`；前端`npm test -- --run` **353 passed /22 files**；`npm run build -- --outDir /private/tmp/interviewer-046-dist`成功，产物hash与045相同（本次前端只扩展行为测试），既有大bundle提示保留。`compileall`使用独立`PYTHONPYCACHEPREFIX`通过，10份文档本地链接与`git diff --check`通过。最终后端全量/本机更新结果见下段记录。
- 真实模型仅合成材料：10个轻量接话场景全部正确，约1–2秒；最初完整语义两句等待仍被理解为pause+continue_listening而严格拒绝，随后暂停又错误携带completion_basis，均保留诊断日志。明确互斥动作和空结束依据后，完整“稍等”“稍等。没有。嗯。”“暂停整场”三项通过，约3.1–3.8秒，日志`/private/tmp/interviewer-046-semantic-acceptance.log`。一次固定等待回应TTS批量947ms/2880ms音频，默认完整PCM路径首片466ms、收齐1048ms/142080字节，日志`/private/tmp/interviewer-046-{tts-smoke,buffered-tts-smoke}.log`。路由/加密凭据仅只读复制至InMemoryStore，模型审计与音频计数留在内存，不发送真实候选资料或写真实业务数据库；这不是端到端用户时延验收。
- 失败留痕：初次shell使用不存在的python、文档header未匹配，未产生该次写入；最初2项整链失败暴露提前关闭普通识别流和打断补充回复时丢失阶段，改为稳定preview仅预判并保留真实awaiting_reply后47项通过。Prompt升版初次漏领域Literal/审计白名单，25项合同失败，补齐后通过。连续接话4项失败暴露等待后再次切断空识别流，保留同前缀健康流后91项通过。首次后端全量2073 passed/1 failed/8 skipped，唯一失败为旧“思考必须无播报”断言，改为实际播报一次后继续听；补充owner事务测试初次误断言异常字符串/代码以及测试遗漏播放helper导入，均只影响合成测试，已修正并最终复验。所有失败日志保留，不删除或覆盖失败证据。
- 范围：标准等待/在线确认等是经语义选择的版本化短回应，普通回答/明确拒答/结束/公司反问继续走完整语义链；并未宣称能处理所有未知情况。Skill与企业资料均保持可选。历史v2题目和已暂停目标会话不迁写；无生产部署或真实候选音视频再处理。

- 最终后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2076 passed, 8 skipped in148.84s**，日志`/private/tmp/interviewer-046-backend-acceptance.log`。真实PG/Redis/恶意文件扫描器/独立原生turn detector等未配置外部项保持skip；未将其计入通过。新增暂停事务owner校验与TTS准备期间插话撤销也包含在本次全量中。
- 首次更新检查：LiveKit房间0/候选人0，但worker active=1，脚本按门禁退出；没有停止任何API/worker或发布静态资源。日志`/private/tmp/interviewer-046-restart.log`保留；等待任务完成后重新检查，不强制中断业务工作。

- 本机最终加载：worker任务完成后重新检查，LiveKit活动候选人0、active/reserved均0，正常TERM API61608/worker61609及Beat，再复用原环境启动API **89807**、worker **89808**。`/private/tmp/interviewer-046-restart-retry.log`记录成功；日志沿用025，凭据不输出或落盘。静态资源先检查/复制、index最后原子替换，046构建与045的JS/CSS hash一致，无独立Vite进程；旧资源和`/private/tmp/interviewer-046-previous-index.html`保留。
- 在线核验：healthz=200/ok、readyz=200/ready=true，首页与JS/CSS内容hash一致；worker单节点pong、active/reserved=0，Beat启动后继续派发任务。证据`/private/tmp/interviewer-046-{publish,verify}.log`。目标会话再次只读确认仍paused、9个turn、0个answer、updated_at仍为2026-09-14T10:24:43Z；首次用领域名interview_sessions查通用文档未命中，改为实际interviews集合确认，没有写入或恢复真实面试。
- 完成与恢复：无生产部署、数据库迁移、Git提交/推送或真实录音再次外发。未重新加载可能含未保存表单的工作台。前端可恢复保留index（本次资源内容相同）；后端如需回退，应只回退046相关增量并保留037–045已有工作与历史Prompt版本兼容，再按空闲门禁正常重启。真实网络、麦克风/浏览器听感、口音与长时多轮仍未验收；不宣称所有突发场景已解决。

## 2026-09-10 · INVITATION-ENTRY-045

- 状态：`verified（相关仓库回归、合成浏览器场景与本机加载；未代替真人入场验收）`。
- 用户目标：修复邀请页连接检测通过、入场却统一提示网络异常的矛盾显示。
- 只读定位：截图对应预约仍为 invited，未找到该预约的 candidate_intake；设备网络记录 RTT 4.2ms、jitter 3.1ms，readiness 请求均 HTTP 200，没有 start 请求。邀请路由切换时旧 registered 投影可用于初始化新页面 confirmed；readiness 吞掉准入错误，前端统一归因网络。
- 计划：邀请投影绑定当前 token、异步登记回执隔离；公开 readiness 提供安全的 entry_blocker.code；前端持久展示具体入场原因，实际请求断连/超时才使旧连接测量失效，保留服务端登记、同意、时间窗和媒体准入门禁。
- 验证与恢复：覆盖跨邀请迟到响应、登记回执、准入原因、断连与重测、完整相关回归；仅用合成数据交互，不代填真实同意或启动面试。本机更新前检查活动会话与 worker，保留原始日志和旧静态资源。实际结果待补充。

- 实际修改：`app/services/appointments.py`、`app/web/src/core/WorkbenchProvider.jsx`、`app/web/src/features/candidate/{Page.jsx,presentation.js,InvitationPage.test.jsx,presentation.test.js}`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`tests/test_appointment_readiness_refresh.py`；同步`docs/{api-design,domain-model,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。静态更新为`app/web/dist/index.html`及`bundles/index-BT6JSygj.js`、`index-Dh5aCmPF.css`、`VrmAvatar-yCoDN175.js`。
- 实现结果：邀请 GET 投影与请求 token 原子绑定，加载新邀请期间不显示旧确认；intake 必须返回同一 appointment_id、matched=true、registered 才显示成功，迟到回执不影响新邀请；切换/返回登记释放预检媒体，成功进入才交接媒体。readiness 仅新增安全 entry_blocker.code，不放松 start 门禁。持久错误卡片区分登记/同意、时间窗、设备、面试服务和实际传输失败，并自动进入视野；HTTP 测量标签为“服务连接”，不代表实时音视频已通过。入场请求断连/超时会清除内存和缓存中的旧网络结果，重测成功恢复按钮。浏览器/录制格式与原始 FPS 边界和后端保持一致。
- 后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_appointment_readiness_refresh.py tests/test_position_resume_appointment_flow.py tests/test_api_responses.py --tb=short` **50 passed in4.51s**，日志`/private/tmp/interviewer-045-backend-targeted-final.log`。覆盖未登记/同意缺失/窗口前后/设备失败/服务失败/未批准计划/可入场的精确代码，以及未知内部错误的安全降级；真实数据只读诊断，没有修改实际登记状态。
- 前端：最终`npm test -- --run` **353 passed /22 files**，日志`/private/tmp/interviewer-045-frontend-acceptance.log`。覆盖真实 Workbench 路由切换、延迟投影/回执、未登记与连接正常同时出现、超时/断连后失效与重测、29.8 FPS 不被四舍五入成通过、录制能力缺失、会话授权隔离。`npm run build -- --outDir /private/tmp/interviewer-045-dist`成功，只有既有大 bundle 提示；最终构建日志`/private/tmp/interviewer-045-build-acceptance.log`。
- 合成页面：实际 InvitationPage 配合隔离内存 API、空 MediaStream 和模拟试音，在18045验证服务未就绪独立提示、请求超时取消绿勾、重新检测恢复，以及462px错误卡片显示。首次发现错误在当前滚动位置上方，增加焦点/滚动定位后截图确认可见。未访问真实摄像头/麦克风、未填写同意、未调用真实业务 POST；临时标签8已关闭，18045服务正常退出。
- 本机加载：LiveKit活动候选人0、worker active/reserved均0后，正常TERM原API49430/worker49431及Beat，复用原环境启动API **61608**、worker **61609**。资源先复制、index最后原子替换，保留044资源及`/private/tmp/interviewer-045-previous-index.html`。healthz=200/ok、readyz=200/ready=true，新JS/CSS线上hash与构建一致，worker单节点pong、active/reserved=0、Beat在启动后继续派发。证据`/private/tmp/interviewer-045-{restart,publish,verify}.log`及对应结果文件；没有重载用户可能正在填写的页面。
- 失败留痕：首次测试追加命令使用不存在的python，未发生写入，改用.venv/bin/python完成；新增后端fixture删除缺expected_version导致2项失败，按CAS合同补齐后50项通过；两次前端全量暴露旧会话错误文案断言，保留“暂时无法进入面试/重新打开邀请链接”且移除无依据网络归因后全部通过。临时HTTP监听首次受沙箱限制，经批准的本机监听成功；compileall首次写系统缓存被拒绝，改用`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-045-pycache`后通过。合成浏览器同批点击跨React更新导致等待提示超时，读取实际按钮状态后继续完成；iframe跨文档只读查询不可用，改用截图核验。均未作用于真实候选数据。
- 最终检查及恢复：compileall与`git diff --check`通过。无数据库迁移、Git提交/推送或生产部署。回退前端可恢复045 previous-index及保留bundle；若回退后端须保持对应接口兼容。当前邀请须由本人完成真实登记并在有效窗口内进入；本次只验证显示与准入诊断，不把HTTP预检或合成媒体结果视为正式RTC/STT/TTS质量验收。

## 2026-09-10 · INTERVIEW-CUSTOMIZATION-044

- 状态：`verified（仓库回归、合成页面、真实模型合成场景及本机加载；真人多轮/生产环境仍待验收）`。
- 用户目标：将面试官Skill改为面试定制，提供Skill定制和企业资料两个独立可选模块；一次保存，后续新面试有就默认使用，没有就略过。企业资料主要供Agent在候选人询问公司业务时有依据答复，不编造未提供事实。
- 设计：组织级InterviewCustomization配置，由小接口封装版本/隔离/私有存储/既有Skill生命周期和默认解析；前端两个独立保存模块；计划装配默认继承已保存内容并冻结本场版本。已有Skill作为兼容默认来源可读取，Skill编辑沿用自由正文，不新增审批或必填企业资料。
- 运行链路：梳理候选人询问公司→语义理解→基于已提供资料的答复→继续当前面试，保留音频/所有权/版本/评分边界；资料不足明确不知道，不将公司问答计入技术答案或能力评分。
- 验证：租户/RBAC、版本冲突、并发/私有字段、空/部分配置、默认继承与明确覆盖、历史快照不漂移、公司问答有据/缺资料/未知问题/提示注入、前端保存/保留输入和响应式，相关全量及本机加载。真实业务配置不代填、不自动创建真实预约或开启设备。
- 后端全量：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **2030 passed, 8 skipped in140.65s**，日志/private/tmp/interviewer-044-backend-full.log。真实PostgreSQL、Redis、ClamAV与独立原生turn detector等未配置项保持跳过；没有用Mock将外部验收标记通过。受影响运行组合205项、最终公司组37项另行通过，最后Prompt补齐动作互斥后合同25项通过。
- 分段验证：前端`npm test -- --run` **326 passed/22 files**，`npm run build -- --outDir /private/tmp/interviewer-044-dist`成功，产物index-CO-h3Lhc.js、index-CoDDvuCR.css、VrmAvatar-cJ7Tcjiu.js，只有既有500kB体积提示。配置/默认继承/可选上下文/覆盖95 passed/1 skipped；配置与旧Skill兼容154 passed/2 skipped；新增保留期与API目录9 passed。真实PostgreSQL缺少配置的用例跳过，不计通过。
- 合成页面：实际Shell、SkillsPage、PlansPage/PlanCreate以隔离内存数据在18044验收；1238px双卡片、462px纵向布局无横向溢出，分别保存/保留另一侧草稿、只填公司、清空不使用、窄屏模块跳转及计划默认状态通过。无真实业务POST，合成标签已关闭、服务已停止。
- 真实模型合成检查：只读复制本机模型路由和加密凭据至InMemoryStore，在内存中运行/审计，不读写候选人资料或业务数据库。主营业务逐字引用、未知福利不编造、要求无资料也说有仍返回not_found、补充阶段反问分类共4项通过，耗时约1.2–1.7秒（不含真人语音/STT/TTS）。日志/private/tmp/interviewer-044-model-smoke.log。
- 审查修复：公司答复区分pending/playing/delivered，播报失败复用已验证答复可重试；抽取期间保持候选音频接收；同句问题范围不能覆盖声明的技术证据；保留期去除问答转写前缀、scoring_transcript/non_scoring_spans。首次真实模型mixed检查漏提technical claims，原文仍保留；强化后结构诊断发现模型提取了正确claim/问题却选择answering/accept，与ask_company/respond_company合同冲突，两次被严格拒绝。已补齐示例与待答反问动作优先规则，保持原文/动作校验不放松，最终真实复验见后续结果。
- 失败留痕：前端一次测试写入路径错误跑到旧库断言，纠正后通过；默认继承与resolver关键字在并行合拢前有短暂接口不匹配，合拢后95项通过；新增runtime测试的outbox/计划字段与等待时序fixture由定向回归暴露并修正。所有失败均未作用于真实配置或候选记录。本条保留全部失败过程，最终验证和加载结果如下。

- 最终增量与真实模型：最后Prompt/播放状态变更后，company_questions、company_question_integration、customization_plans、retention及api_responses **59 passed in8.52s**，日志/private/tmp/interviewer-044-final-targeted.log。真实模型纯反问与同句mixed最终均正确ask_company/准确问题span；mixed同时保留技术claim，耗时4.42秒/5.22秒，日志/private/tmp/interviewer-044-semantic-final.log。此前失败诊断保留在semantic-smoke、semantic-smoke-recheck、semantic-diagnostic日志，不删去失败样本，也不以单次通过承诺模型永不误解。
- 本机加载：无活动LiveKit候选人、Celery active/reserved均0后，复用原始环境并正常TERM旧API41522/worker41523（含Beat），启动API **49430**、worker **49431**；不输出/落盘凭据，PID记录沿用/private/tmp/interviewer-025-pids.json，原日志继续保留。资源先复制、index最后原子替换，保留043旧bundle，前端无独立Vite进程。运行资料见/private/tmp/interviewer-044-restart.log、publish.log、verify.log。
- 在线核验：healthz=200/ok、readyz=200/ready=true；customization GET=200/version0、两个模块均未填写；OpenAPI包含GET/PATCH与use_customization_defaults默认true。首页及JS/CSS HTTP hash与新构建一致；worker单节点pong、active/reserved为0，Beat在ready后继续调度。原工作台刷新并打开/#skills，侧栏/标题均为面试定制，两个空表单可用，未代填任何真实资料或创建面试。
- 最终检查：compileall、14份更新文档的本地链接和git diff --check通过。没有Git提交/推送、生产部署、真实数据库迁移、候选录音外发、清空历史数据或自动审批拒绝。
- 未完成与恢复：真人STT/TTS/打断/长时多轮和生产基础设施质量仍按P4验收，8项外部依赖未配置不记为通过。005迁移已登记且有静态/可选真实PG测试，本次未执行生产DDL。更新用于后续新计划；已有计划/活动会话维持冻结版本。恢复前端可使用/private/tmp/interviewer-044-previous-index.html及已保留旧bundle；回退代码时需匹配API接口，不重写历史面试或默认配置。

<details>
<summary>044实际修改文件</summary>

- `CONTEXT.md`
- `app/api/routers/interview_customization.py`
- `app/api/routers/plans.py`
- `app/api/routes.py`
- `app/core/auth.py`
- `app/core/prompt/company_questions.py`
- `app/core/prompt/contracts.py`
- `app/core/prompt/semantic_turn.py`
- `app/core/prompt/understanding_references.py`
- `app/domain/interview_agent.py`
- `app/model_gateway/gateway.py`
- `app/persistence/interface.py`
- `app/persistence/memory.py`
- `app/persistence/postgresql.py`
- `app/providers/mock/provider.py`
- `app/repositories/memory.py`
- `app/repositories/sqlite.py`
- `app/schemas/api.py`
- `app/schemas/interview_customization.py`
- `app/services/answer_endpoint.py`
- `app/services/company_questions.py`
- `app/services/conversation_understanding.py`
- `app/services/evaluation.py`
- `app/services/interview_customization.py`
- `app/services/interview_evidence.py`
- `app/services/interview_skills.py`
- `app/services/interviews.py`
- `app/services/interviewer_supervisor/context.py`
- `app/services/livekit_evidence_ingress.py`
- `app/services/plan_assembly.py`
- `app/services/retention.py`
- `app/services/spoken_supplement.py`
- `app/transport/service_locator.py`
- `app/web/src/App.test.jsx`
- `app/web/src/features/skills/Page.jsx`
- `app/web/src/features/skills/Page.test.jsx`
- `app/web/src/features/skills/skills.css`
- `app/web/src/features/skills/index.js`
- `app/web/src/features/plans/PlanCreate.jsx`
- `app/web/src/features/plans/PlanCreate.test.jsx`
- `app/web/src/features/plans/Page.jsx`
- `app/web/dist/index.html`
- `app/web/dist/bundles/index-CO-h3Lhc.js`
- `app/web/dist/bundles/index-CoDDvuCR.css`
- `app/web/dist/bundles/VrmAvatar-cJ7Tcjiu.js`
- `migrations/005_interview_customization.sql`
- `tests/test_api_responses.py`
- `tests/test_company_questions.py`
- `tests/test_company_question_integration.py`
- `tests/test_interview_customization.py`
- `tests/test_interview_customization_plans.py`
- `tests/test_semantic_turn_policy.py`
- `tests/test_prepared_decision_binding.py`
- `tests/test_followup_rejection_integration.py`
- `tests/test_declined_answer_integration.py`
- `tests/test_spoken_supplement_integration.py`
- `tests/test_supplement_contracts.py`
- `tests/test_retention.py`
- `docs/architecture.md`
- `docs/api-design.md`
- `docs/domain-model.md`
- `docs/retrieval-and-evaluation.md`
- `docs/model-provider-plugins.md`
- `docs/database-and-vector-storage.md`
- `docs/known-issues-and-remediation.md`
- `docs/development-progress.md`
- `docs/implementation-roadmap.md`
- `docs/change-log.md`
- `docs/interviewer-agent-architecture.md`
- `docs/interview-skills-api.md`
- `docs/company-question-runtime.md`

</details>

## 2026-09-10 · PLAN-COVERAGE-DIAGNOSTICS-043

- 状态：`verified（后端/前端回归、合成页面、原失败请求与本机运行核验；真实业务标签缺口保持待维护）`。
- 用户问题：generate v3返回409 ASSESSMENT_CONTRACT_INVALID / Every required competency needs an approved candidate question，details为空。
- 已确认：请求重点技能python/系统设计与岗位parsed skill_weights python/flask/redis合并；所选10道活动且语音就绪题均无flask和系统设计标签。初次冻结原题合同即拒绝，尚未调用单元生成模型。Skill和企业资料为空合法，题数和网络不是该错误原因。
- 范围：在v3生成前检查所选可用题池的能力覆盖，返回422明确缺失能力、来源和恢复提示；工作台展示缺口/入口并保留设置。保留实际能力与评分依据约束，不自动改岗位、题目标签或虚构覆盖，不移除缺失必需维度；旧v2和已冻结合同校验不放松。
- 验证计划：真实数据只读诊断；合成领域/装配/API回归覆盖缺失岗位能力、额外要求、补齐来源后成功、无模型调用/无计划写入、可选资料不影响；前端缺口显示与恢复、全量/构建，安全加载到本机最新服务并以原失败请求核验（提前覆盖检查不调用模型、不生成计划）。
- 实现：仅在v3尚无inquiry_unit_snapshots的原题阶段，以已经完成范围/版本校验的冻结候选映射检查正权重能力。缺口422 ASSESSMENT_SOURCE_COVERAGE_MISSING返回完整/岗位/本次三类有序ID，允许岗位和本次来源重叠，中文说明“当前可用题池”，不冒称整个题库永久没有内容。domain冻结校验、最终单元覆盖、评分范围及v2语义保持原样。
- 前端：分别展示岗位与本次追加缺口，保留未分类缺口；仅解析字符串数组，异常details/message保持可渲染。提供题库/招聘流程入口与返回设置，保留预算、coverage、Skill和企业资料，不自动删维度、不重复请求。错误出现滚到可见步骤条，462px合成页面验证缺口、入口、返回设置和原值保留，无水平溢出。临时18043服务及标签6已关闭，不访问真实设备。
- 目标后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_plan_coverage_diagnostics.py tests/test_plan_assembly.py tests/test_inquiry_unit_assessment.py tests/test_adaptive_interview.py tests/test_enterprise_interviewer_flow.py` **62 passed in3.37s**，新增诊断文件8例。覆盖实际flask+系统设计缺口、仅移除额外设置仍缺flask、来源重叠、补齐真实映射后同预算生成草稿、未选题库不掩盖缺口、最终单元拒绝假覆盖、v2兼容和API422重复失败无模型/计划写入。
- 全量后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **1913 passed, 7 skipped in134.05s**，日志/private/tmp/interviewer-043-backend-full.log。7项仍为外部环境门控，不当成通过。没有真实外部模型请求。
- 前端：PlanCreate目标 **22 passed**；`npm test -- --run`（app/web）**317 passed / 22 files in2.77s**，日志/private/tmp/interviewer-043-web-full.log；`npm run build -- --outDir /private/tmp/interviewer-043-dist`通过，2.67秒，只有既有大chunk提示；git diff --check与文档本地链接检查通过。
- 加载：沿原进程启动环境检查LiveKit rooms=0/active_candidates=0、worker active/reserved=0后，正常TERM并等待原API/worker/Beat树结束，启动API41522、worker41523。PID文件仍为/private/tmp/interviewer-025-pids.json，业务日志仍025-api.log/worker.log。Redis和媒体服务未重启，未清空数据库或改环境配置。静态产物先新bundle后原子index，保留042旧bundle；入口index-DWYzu0Wy.js、样式index-ABpb76Qb.css与在线SHA256一致，VrmAvatar-DEXb59pQ.js已提供。首页、healthz、readyz均200，ready=true、单节点worker pong和Beat调度通过。用户原8000/#plans处于列表无未提交输入，已刷新加载新版。
- 原请求实测：按用户给出的实际role/job/candidate/kb、coverage=[python,系统设计]、v3与原预算仅重发一次；HTTP422及三类缺口精确匹配预期，计划ID集合不变、数量4。记录/private/tmp/interviewer-043-live-result.json；此运行结果不等于已解决真实题库标签缺口。修改输入或补齐内容前同请求仍应拒绝。
- 失败与恢复：首次重启脚本只接受相对celery路径，正确在停进程前因ownership检查中止；只读核验实际绝对路径仍属于本项目后兼容精确绝对路径并保留cwd检查，再次执行成功。失败日志追加保留。只读搜索曾引用不存在app/core/config.py、tests/conftest.py及尚未创建测试路径，改用实际provider/现有fixtures；无业务副作用、无自动审批拒绝。没有错误状态伪装成功或凭据输出。
- 留痕：/private/tmp/interviewer-043-restart.log、interviewer-043-restart-result.json、interviewer-043-runtime-verify.log、interviewer-043-publish-result.json、interviewer-043-build.log；旧首页备份interviewer-043-previous-index.html可原子恢复，旧静态资源保留。
- 实际修改文件：app/services/plan_assembly.py、tests/test_plan_coverage_diagnostics.py；app/web/src/features/plans/PlanCreate.jsx、PlanCreate.test.jsx、plans.css；docs/api-design.md、architecture.md、adaptive-interview-contract.md、retrieval-and-evaluation.md、known-issues-and-remediation.md、development-progress.md、implementation-roadmap.md、change-log.md；app/web/dist/index.html及bundles/index-DWYzu0Wy.js、index-ABpb76Qb.css、VrmAvatar-DEXb59pQ.js。
- 边界：用户仍需按实际内容补齐匹配题目/正确标签，或明确调整不需要的岗位要求与额外重点；本轮未替用户改业务数据。无Skill与无企业资料保持合法。旧v2兼容未退役，不标closed。无Prompt、数据结构、数据库迁移或生产发布变更。

## 2026-09-10 · PLANS-WORKSPACE-REDESIGN-042

- 状态：`verified（311项前端回归、桌面/窄屏交互及8000实际页面加载；无服务端与数据库变更）`。
- 用户目标：全面整改面试计划页的可读性、交互和窄屏渲染，清楚区分计划对象，能理解如何创建、查看和审阅启用。
- 根因：列表仅以岗位要求为标题、缺少plan-card样式，多个候选人计划无法区分；历史说明/简历提示逐条铺满，生成表单无分步或关联选择反馈，考察详情混在列表。
- 实施：候选人为主的计划列表、搜索/状态筛选、独立详情、分步创建；选岗联动题库，预算和上下文渐进展开；岗位要求独立入口；细节/评分依据/版本追溯分层展示，适配462px及桌面。前端路由/#plans、/#plans/new、/#plans/{id}。
- 保留：现有API和权限；创建approve:false/execution v3，实际问题审阅后以expected_version明确批准；冲突重读/重复操作/迟到响应保护；Skill与企业资料独立可选；旧v2计划只读，不以当前题库冒充历史冻结问题，不自动创建预约。
- 验证：列表辨识/搜索筛选/空状态、创建前后步骤和关联校验、审批与可选输入回归、前端全量与构建；合成数据窄屏/桌面页面验收及当前8000只读加载。不改实际计划、邀请或答案，不重启业务服务。
- 最终实现：候选人姓名、岗位、创建日期/时分与状态组成列表；搜索和状态筛选、无结果恢复；新建分三步，选岗联动候选人与题库；预算和两类独立可选上下文折叠。生成请求等待210秒以覆盖既有后端180秒准备预算，不自动重试。详情GET最新记录后分层展示问题/评分/能力权重，明确启用才PATCH所见版本。缺失合同、版本、问题单元或被选Skill快照时拒绝启用，冲突和不确定结果需要重读，旧路由响应不串场。
- 历史展示：v2保存的bank_slots方向、分钟、难度与候选版本引用，以及experience_question_snapshots冻结简历问题均可查看；不向当前data.questions取正文替换旧计划。历史固定题序保持只读。真实旧计划页面读取正常，已有记录没有被迁写或批准。
- 交互验收：临时18042只替换Workbench数据/请求依赖，使用实际PlansPage、PlanCreate、Shell及CSS。验证搜索、状态筛选、评分依据展开、合成草稿明确启用及列表状态同步、三个创建步骤、独立企业资料输入、生成后进入待审阅详情。1180px桌面、462px与360px窄屏截图检查通过，360px DOM viewport/scrollWidth均为360；没有横向溢出。发现并修复切换步骤停留在中段的问题：仅步骤变化时定位步骤条并留88px顶栏空间，标题focus不抢滚动；计划子路由变化也返回页首。验收标签5与18042服务均已关闭，合成文件只位于/private/tmp/interviewer-042-*。
- 自动化：四组目标测试router/Page/PlanCreate/App **76 passed**；最终`npm test -- --run`（app/web）**311 passed / 22 files in2.84s**，日志/private/tmp/interviewer-042-web-full.log。包含候选人辨识与时间、搜索/状态/空状态、岗位关联、三步输入保留与失败恢复、可选上下文缺省和列表失败、210秒生成超时、生成不批准、GET最新版本和明确启用、CAS冲突/重复/迟到响应、权限及旧计划冻结字段。无测试stderr警告；`git diff --check`通过。服务端未改，本次不重复039后端全量。
- 构建与发布：`npm run build -- --outDir /private/tmp/interviewer-042-dist`通过，2.76秒，只有既有大chunk提示。先放置新bundle再原子替换index，保留旧脚本/懒加载资源。在线入口为index-CG96N24_.js、样式index-mh4Lbh8F.css，HTTP响应SHA256与新dist一致，旧041入口与VRM仍200，/healthz=ok。记录/private/tmp/interviewer-042-build.log及interviewer-042-publish-result.json；未重启API或worker。
- 真实页面：刷新用户原有8000/#plans标签，4份既有计划正常显示；只读打开一份旧计划核对6个岗位方向及3道冻结简历问题，再返回列表。用户原标签保留新版，未生成真实计划、修改预约/回答、开启设备或发出通知。
- 失败与恢复：首次发布已完成文件切换，但校验脚本错误把URL的/web/前缀当成dist子目录，导致本地文件比对失败；修正只读核验路径后所有首页、资源及健康检查通过，没有重新写业务数据。旧index保留/private/tmp/interviewer-042-previous-index.html，可原子复制恢复。CUA一次误用click参数、一次跨iframe只读DOM不可用、一次数字标签ID定位失败均无业务副作用，分别按接口参数、合成页DOM诊断和实际字符串标签ID修正；无自动审批拒绝。
- 实际修改文件：app/web/src/features/plans/Page.jsx、Page.test.jsx、PlanCreate.jsx、PlanCreate.test.jsx、plan-presentation.js、plans.css；app/web/core/router.js、router.test.js；app/web/src/core/WorkbenchProvider.jsx、app/web/src/App.test.jsx；docs/api-design.md、architecture.md、known-issues-and-remediation.md、development-progress.md、implementation-roadmap.md、change-log.md；app/web/dist/index.html及bundles/index-CG96N24_.js、index-mh4Lbh8F.css、VrmAvatar-C6YsEibf.js。
- 未完成与边界：当前整改已完成本机验收；没有把合成计划请求当成真实模型质量或生产验收。旧v2兼容继续保留，真实供应商、并发与真人效果仍遵循现有Agent验收边界。本次没有Git提交、数据库迁移或生产发布。

## 2026-09-10 · PREFLIGHT-NETWORK-FEEDBACK-041

- 状态：`verified（前端回归、合成页面验收和本机静态资源已加载；历史浏览器采样值不可回溯）`。
- 用户问题：邀请页设备检查只有“网络连接”灰色感叹号，无失败解释，无法判断或恢复。
- 已确认：当前4次/healthz请求均成功才会显示该结果；均值RTT>500ms或邻接差均值>100ms会显示灰色感叹号，不能等同断网。网络测量与WebGL初始上下文创建并发，第一样本可能混入主线程初始化耗时；页面无实测值和独立重测。
- 范围：分开画面初始化与网络测量，保留真实4样本和500/100门槛；增加有限超时、明确数值/状态与单独网络重测，复用当前设备流与扬声器确认，防止过期检测结果入场。保持后端准入和正式RTCStats检查，不伪造绿灯、不触发用户真实面试。
- 验证计划：网络采样/异常/超时、页面偏高/恢复/失败/重测并发及入场使用最新报告；前端全量和构建、同源静态产物在线验证。只改前端时不重启活动API/worker。
- 诊断实测：从本机进程读取/healthz四次耗时3.2/0.9/0.7/0.7ms，均值1.4ms、波动0.8ms，服务当时可达。该值来自维护探针，不冒充用户浏览器截图那次的数值，也不能据此确定历史高延迟根因。用户截图已完成4次成功响应，只能确定当次延迟或波动超门槛。
- 页面验收：将实际InvitationPage编译到18041隔离预览，仅替换设备/请求依赖为合成数据，不访问真实邀请或麦克风。验证720/145ms显示响应慢和波动大、重测期间禁止入场、12/3ms成功恢复且保留扬声器确认，详情文本与按钮正常排版；未点击进入面试。预览标签与18041服务均已关闭，临时脚本和日志仅在/private/tmp/interviewer-041-*。
- 最终测试：目标采样/邀请/候选页面 **37 passed**，日志/private/tmp/interviewer-041-preflight-tests.log；app/web中`npm test -- --run`全量 **280 passed/20 files in3.01s**，日志/private/tmp/interviewer-041-web-full.log。覆盖原4样本公式、边界、缺输入/HTTP失败/网络拒绝/3秒超时/取消、WebGL先完成、重测重复/互斥/失败与最新报告、邀请切换迟到结果、首次网络错误固定安全提示；后端门槛和真实RTCStats实现未改，不重复后端全量。
- 构建与加载：`npm run build -- --outDir /private/tmp/interviewer-041-dist`通过（2.94s，只有既有大chunk提示）。先拷贝新bundle，再原子替换index.html，保留旧bundle避免已打开页面的懒加载404。当前首页引用index-yjnKN7Pl.js及index-JQS6uAkD.css，HTTP 200且SHA256与最新dist一致；新VrmAvatar-DYzcrq-n.js已提供，旧039入口/懒加载仍200，/healthz=ok。API仍PID29464，没有重启API/worker、打开用户麦克风或操作真实邀请。
- 失败与恢复：只读定位中曾引用不存在的InvitationPage.jsx与无匹配admission测试glob，改用实际Page.jsx和已发现文件，无写入副作用。所有测试/构建通过；没有自动审批拒绝、清空数据或放宽门槛。缺测量输入现在明确失败，保留四次真实采样，未剔除冷启动样本或编造0ms。
- 未完成与范围：刷新现有邀请页才能使用新脚本和重测；本次未替用户重新授权设备/进入面试，未测得截图当时的真实浏览器延迟。HTTP响应检测不代表完整音视频网络质量或供应商生产验收。没有接口/领域结构/评分/数据库迁移变更。
- 实际修改文件：`app/web/src/features/candidate/preflight.js`、`app/web/src/features/candidate/Page.jsx`、`app/web/src/features/candidate/InvitationPage.test.jsx`、`app/web/src/features/candidate/preflight.test.js`、`app/web/styles.css`、`docs/architecture.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`、`app/web/dist/bundles/index-JQS6uAkD.css`、`app/web/dist/bundles/VrmAvatar-DYzcrq-n.js`、`app/web/dist/bundles/index-yjnKN7Pl.js`、`app/web/dist/index.html`。

## 2026-09-10 · LOCAL-LATEST-RESTART-040

- 状态：`verified（最新本机前端/API/worker/Beat已加载并通过在线健康核验）`。
- 授权与目标：用户明确要求前端、后端与worker重启到最新代码，加载038/039面试官与可选上下文实现。
- 范围：重建app/web/dist，沿用当前8000 API与Celery/Beat的实际工作目录、数据库和启动环境，正常TERM后启动最新版本；前端由同源后端提供，无独立Vite进程。保持Redis和本地媒体服务正常运行。
- 验证：重启前只读核实LiveKit候选人参与状态与worker工作状态；启动后检查healthz/readyz、首页bundle与最新API合同、Celery pong和Beat调度。不创建真实预约、不修改历史回答或评分，不通过重置数据库来启动。
- 起点：后端PID1400、worker主进程PID1402，记录/private/tmp/interviewer-025-pids.json；运行日志沿用025-api.log/worker.log。

- 实际执行：`npm run build`（app/web）成功，1.15秒；通过/private/tmp/interviewer-040-restart.py在内存中读取并复用两个进程的原始环境，确认development/SQLite/local_media=true，不加载生产env文件、不输出或额外保存凭据。LiveKit rooms=0/candidates=0，Celery active=0/reserved=0后TERM，等待旧API及worker/Beat进程树退出再启动，避免重复Beat。
- 当前进程：API **29464**、worker主进程 **29465**；PID记录继续更新/private/tmp/interviewer-025-pids.json。没有独立Vite进程，8000同源静态前端随最新构建和后端切换；Redis/LiveKit/Egress没有重启。
- 在线验证：/healthz=200/status=ok、/readyz=200/ready=true、首页200；首页引用index-D5aPd0-L.js与index-B3VPHEKp.css，HTTP资源SHA256逐个匹配最新dist。运行中OpenAPI包含skill_id/company_context与interview-skills路由。Celery仅1节点pong，active=0/reserved=0，worker ready之后存在Beat dispatch_due_work调度记录。
- 留痕：/private/tmp/interviewer-040-build.log、interviewer-040-restart.log、interviewer-040-restart-result.json、interviewer-040-verify.log；临时操作脚本仅位于/private/tmp。首次ps受沙箱限制后按用户重启授权自动审批放行；首次进程环境sysctl缓冲区超出系统参数大小而EINVAL，按SC_ARG_MAX纠正后成功，失败时未停止任何进程。两次只读源码路径不存在后改用实际main.py和repositories/provider.py，没有改写配置或数据。未发生自动审批拒绝。
- 实际仓库文件：docs/change-log.md；重建app/web/dist/index.html及其bundle与039已验证产物内容一致。没有业务代码、数据库迁移或持久配置变更；不重复已通过的业务全量测试，本轮以构建和真实运行核验为依据。
- 恢复与边界：现有数据、环境和原日志位置保留，运行进程使用项目venv与当前工作区。浏览器已打开的旧页面需要刷新获取新脚本；没有替用户开启麦克风或重新入场。本次只确认最新本机版本在线，不把服务健康等同真人质量或生产验收。

## 2026-09-10 · OPTIONAL-AGENT-CONTEXT-039

- 状态：`verified（仓库全量回归、构建与隔离本机页面验收；生产部署和真人效果未验收）`。
- 用户纠正：企业级要求针对Agent的可靠性、维护性和运行质量；Skill由用户自由编写，属于可选扩展，不应要求企业审核/固定方法模板；企业资料也独立可选，缺省直接按岗位面试。
- 目标：简化Skill为自由文本/Markdown保存即用，保留内容版本、权限隔离与基本大小/结构校验，移除正常使用中的企业审批流程与写作形式限制；企业资料独立于Skill输入并按需使用；无Skill/无企业资料完成原面试闭环。
- 接口预声明：保留`/api/v1/interview-skills`入口，新建/修改自由格式`interview_skill.v2`包（name、instructions，description/resources等可选），保存后active，不需validate/approve；旧包和已冻结版本继续兼容读取。计划请求新增可选`skill_id`和`company_context`文本，旧enterprise_skill_id只作兼容输入；同时传两个不一致ID拒绝。Skill是可选配置而不是企业身份，company_context为空不要求补录，也不虚构企业事实。
- 实施范围：Skill schema/compiler/service/API/UI，计划请求/展示/冻结，Supervisor上下文与资料工具，对应Prompt版本、合同测试、端到端缺省组合回归及架构文档。保留038的语义、动态单元、评分、事务、预算和故障恢复，不修改真实历史面试或部署服务。
- 验证计划：保存即用与自由Markdown、可选Skill/资料四种组合、明确公司资料缺省策略、敏感投影、完整面试到报告、旧版本读取、相关后端与前端全量/构建。
- 已实现：新interview_skill.v2为名称与自由正文，保留Markdown、链接、代码示例及原文空白；保存即active，正常页面移除固定风格/方法与校验/审批步骤。Skill和企业资料在计划中分别可选，缺省完全不访问Skill服务、不要求企业信息。company_context纳入会话/准备指纹，正文按需通过company:context读取，无资料不公开读取工具；候选人投影不返回全文。新Prompt和编译器均有独立版本，旧v1编译文本/hash/冻结授权保持兼容。修复038的inquiry_units.v1和answer_evaluation.v6调用审计版本白名单遗漏。
- 后端全量：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -o faulthandler_timeout=30 --tb=short` **1909 passed, 7 skipped in130.71s**，日志`/private/tmp/interviewer-039-backend-full.log`。7项仍为未配置PostgreSQL三项、Redis两项、ClamAV与独立原生turn-detector，不记为通过。
- 全量运行期间的最后审计补正另行验证：`pytest -q tests/test_optional_interview_context.py tests/test_enterprise_interviewer_flow.py tests/test_semantic_turn_policy.py` **63 passed in3.63s**，包含真实计划生成、outbox评分及新理解Prompt调用的审计版本断言。此前语义/入口/四组合112项和治理/端点/总控36项通过；Skill相关123 passed/1 skipped。四种组合与legacy兼容都通过真实PlanAssembly→准入→自主话题→答案→评分→报告链路。
- 前端全量：在`app/web`运行`npm test -- --run`，**256 passed/19 files in2.32s**，日志`/private/tmp/interviewer-039-web-full.log`；`npm run build`成功（1.33s），入口`index-D5aPd0-L.js`、样式`index-B3VPHEKp.css`，日志`/private/tmp/interviewer-039-build.log`。只有既有超过500kB的构建体积提示。编辑已有Skill继续保留其显式工具范围，包括空数组；加载Skill列表失败仍能生成基础计划。
- 最终补充检查：`pytest -q tests/test_model_invocation.py tests/test_inquiry_unit_assessment.py` **59 passed in1.07s**，日志`/private/tmp/interviewer-039-audit-final.log`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-039-pycache .venv/bin/python -m compileall -q app tests`、五份架构/合同文档本地链接检查及`git diff --check`通过。
- 页面验收：独立18039内存服务、Mock题库与合成候选资料。通过页面创建含Markdown、Python代码围栏和普通URL的自由Skill，保存后立即显示“可用”，没有审批步骤；默认不选Skill且企业资料留空，成功生成基础面试官草稿，审阅区明确显示两项未提供。浏览器检查页面排版与反馈正常；未声称真实麦克风、厂商语音或真人多轮已验收。
- 失败与恢复：并行编辑中第一次根定向回归23 passed/1 failed（旧fixture缺少显式v1导致language键缺失），已将历史测试明确绑定v1，相关123项验证通过。新合同测试曾对所有intent错误要求answer摘要非空，16项失败；改为测试wire长度上界，并通过真实service验证answer空摘要拒绝，生产校验未放松。一次npm命令目录错误、一次python命令未安装、一次文档patch上下文不匹配，均未产生仓库损坏，分别改为app/web、项目venv与精确替换。浏览器绑定变量失效后选回已创建的验收标签，没有重复操作或真实数据副作用。
- 临时环境：18039首次绑定经自动审批允许；仅使用memory、无Redis与tmp媒体/私有目录。验收标签已关闭，预览服务已正常停止，日志与合成文件只留在/private/tmp。本轮不重启原8000服务、不发真实预约、不改写历史录音/答案/报告，没有Git提交或生产部署。
- 恢复与未完成：PostgreSQL部署需顺序执行003与`004_optional_interview_skills.sql`并验证目标环境；本轮未执行真实数据库DDL。旧v1 Skill和v2会话兼容仍保留，不标closed。真实基础设施、模型/语音供应商、长时延迟/并发与真人效果继续按Agent的P4验收；Skill或企业资料均不作为这些验收的必填输入。

<details>
<summary>039实际修改文件（覆盖部分038文件，含重建前端产物）</summary>

- `CONTEXT.md`
- `app/api/routers/plans.py`
- `app/core/prompt/contracts.py`
- `app/core/prompt/interview_skills.py`
- `app/core/prompt/interviewer_supervisor.py`
- `app/domain/interview_agent.py`
- `app/model_gateway/gateway.py`
- `app/persistence/interface.py`
- `app/persistence/postgresql.py`
- `app/providers/mock/provider.py`
- `app/schemas/api.py`
- `app/schemas/interview_skills.py`
- `app/services/interview_skills.py`
- `app/services/interviewer_supervisor/context.py`
- `app/services/interviewer_supervisor/service.py`
- `app/services/interviewer_supervisor/tools.py`
- `app/services/interviews.py`
- `app/services/plan_assembly.py`
- `app/web/src/App.test.jsx`
- `app/web/src/features/plans/Page.jsx`
- `app/web/src/features/plans/Page.test.jsx`
- `app/web/src/features/skills/Page.jsx`
- `app/web/src/features/skills/Page.test.jsx`
- `app/web/src/features/skills/skills.css`
- `app/web/dist/index.html`
- `app/web/dist/bundles/index-D5aPd0-L.js`
- `app/web/dist/bundles/index-B3VPHEKp.css`
- `app/web/dist/bundles/VrmAvatar-_u_pdLXZ.js`
- `docs/adaptive-interview-contract.md`
- `docs/adr/0005-goal-directed-interviewer-supervisor.md`
- `docs/api-design.md`
- `docs/architecture.md`
- `docs/change-log.md`
- `docs/database-and-vector-storage.md`
- `docs/development-progress.md`
- `docs/domain-model.md`
- `docs/implementation-roadmap.md`
- `docs/interview-skills-api.md`
- `docs/interviewer-agent-architecture.md`
- `docs/interviewer-supervisor-contract.md`
- `docs/known-issues-and-remediation.md`
- `docs/model-provider-plugins.md`
- `docs/retrieval-and-evaluation.md`
- `migrations/004_optional_interview_skills.sql`
- `tests/test_declined_answer_integration.py`
- `tests/test_enterprise_interviewer_flow.py`
- `tests/test_followup_rejection_integration.py`
- `tests/test_interview_skills.py`
- `tests/test_interviewer_supervisor.py`
- `tests/test_optional_interview_context.py`
- `tests/test_prepared_decision_binding.py`
- `tests/test_semantic_turn_policy.py`
- `tests/test_spoken_supplement_integration.py`

旧038中间构建产物随本次构建替换，历史日志保留原文件名。

</details>

## 2026-09-10 · ENTERPRISE-INTERVIEWER-IMPLEMENTATION-038

- 状态：`verified（后端/前端全量回归、构建与隔离本机页面验收；真实部署与真人试点未验收）`。
- 目标与授权：用户明确要求按企业级产品落实037架构，实施语义优先接话、总控与受控工具、企业Skill治理、自适应考察及对应报告/运行恢复；保留现有证据、权限、隐私和人工最终决策。
- 关联问题：SEMANTIC-INTERVIEWER-DECISION-037；037设计与既有016/023/034验收范围保留历史记录。
- 实施方法：按独立文件责任并行开发，先声明接口/数据/Prompt合同，再接入实际运行入口；所有代码、测试、配置、迁移、前端和文档编辑归属本工作项。完成各模块验证、相关集成与全量回归后更新实际状态，未通过真实部署验收不称生产完成。
- 计划文件：语义轮次相关domain/prompt/services/tests；新的interviewer_supervisor与企业Skill模块及受控API；v3计划、生命周期、报告、持久化保护、管理/候选页面；相关设计、进度、路线图和问题文档。
- 验证计划：真实入口合成语音回归、严格schema/权限/版本/幂等/故障测试、Memory/SQLite与PostgreSQL合同、题库到报告端到端冒烟、前端交互测试、构建和git diff检查；外部部署/真实口音与金标结果分开记录。
- 已实现：语义首判断、真实完成依据与补充后整场结束；总控/六工具/有界证据专家；v3空题启动、批准单元选择、覆盖/预算收口与固定能力权重报告；企业Skill加密版本、审核/撤销、精确草稿快照；候选动态进度与规划恢复、Skill管理和计划单元审核页面。所有运行效果继续由既有领域事务、owner/控制代次围栏及Outbox提交。
- 中途完整回归：第一轮后端1801 passed/7 skipped/2 failed，第二轮1813 passed/7 skipped；前端先后240、241、244 passed，生产构建成功。第二轮之后又补齐追问关闭源话题、最低根题覆盖、Skill正文损坏撤销、补充确认后主动结束、草稿Skill版本冻结及单元审核入口；最终结果将在下方记录，不以中途结果冒充最终版本通过。
- 失败与恢复：早期指纹错误地绑定asking→transcribing过渡使准备失效，改为稳定证据/控制事实；过早构造加密器导致无Skill生产票据失败，改为按需加载；新测试时钟/本地媒体开关/候选token和Mock输出合同遗漏已修正。全量第一轮的API文件集合未登记新路由，同时将路由内撤销通知业务移入Skill service并增加薄路由合同；连续语音用例的Mock会在静音提前发出预设续答，受控时钟与真实有声触发稳定复现后修复fixture，仍保留完整尾音、同capture、唯一提交断言，目标用例连续10次通过。新增控制连接代次围栏修复旧控制结果迟到；明确硬预算结束不调用模型。所有失败均保留原证据，未修改真实候选人会话来“修复”测试。
- 工具/运行留痕：一次前端命令误用不存在的web目录、一次使用未安装的python命令、数次只读路径搜索不存在，均无仓库副作用，改用app/web与项目venv；compileall默认缓存目录被沙箱阻止，改用临时PYTHONPYCACHEPREFIX后成功。独立18038预览第一次绑定被沙箱拒绝，自动审批允许后以memory存储、无Redis及tmp媒体目录启动；只创建合成Skill草稿/岗位/题库/候选资料，没有真实邀请、录音或外发候选数据。浏览器初次文档输出截断后重置CUA并恢复同一验收标签；未操作用户现有候选页面。子代理曾受使用额度中断，用户要求继续后保留全部修改恢复执行，没有消费使用重置额度。
- 验收环境与边界：本轮不重启或部署用户原8000服务，不改写真实历史回答、转写、评分、媒体或预约。外部PostgreSQL/Redis/OSS/扫描器/STT/TTS/数字人和真人口音、长时交互、时延/成本/公平性仍需部署凭据与真实验收；v2兼容入口保留，问题不得标closed。

- 最终后端：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1840 passed, 7 skipped in133.76s**，日志`/private/tmp/interviewer-038-backend-final3.log`。7项分别依赖未配置的PostgreSQL（三项含新Skill合同）、Redis（两项）、ClamAV及独立原生turn-detector；没有将跳过项记为通过。覆盖题库→批准Skill/计划→预约明确同意→空题开场→动态单元→服务端答案→异步评分→报告/导出/复核的完整合成流程；此流程还验证草稿Skill在发布新版本后不会漂移。
- 最终前端：在`app/web`执行`npm test -- --run`，**251 passed/19 files in2.52s**，日志`/private/tmp/interviewer-038-web-final-verified.log`。`npm run build`通过，入口`index-BbHSO_E_.js`、样式`index-DMQhA0eZ.css`，仅保留现有超过500kB的构建体积提示；日志`/private/tmp/interviewer-038-build-final-verified.log`。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-038-pycache .venv/bin/python -m compileall -q app tests`、新架构/合同文档链接检查与`git diff --check`通过。
- 最终页面：独立18038内存预览验证Skill创建/保存/规则校验/审核入口；使用Mock合成题库完成新计划生成草稿、展开6个真实提问单元/评分依据、显式批准版本1后返回版本2，以及重载显示“已批准”。只操作合成验收数据，未创建任何预约或通知。发现小屏审核表格引起横向溢出后增加容器最小宽度和局部表格/正文样式；复核documentWidth=viewportWidth=462，实际问题可正常阅读。未声称新候选房间使用真实麦克风/厂商语音实机验收。
- 最终审查补正：补充握手中明确结束整场也走真实语义合同；追问拒答关闭同源剩余单元；全局最低根题数不足不发布完整总分；迟到追问不得覆盖新话题；v3全部追问限额来自冻结契约且v2旧限额保留。多能力覆盖以最多10,000节点/150ms的精确搜索替换会误拒可行范围的贪心估计；超限明确告知无法确认，不假称不可行。Skill密文损坏只降级为元数据查看，不阻塞撤销/通知重试，但禁止批准或模型加载。计划界面不再在生成单元前自动批准，草稿即冻结精确Skill，CAS冲突要求重新阅读。
- 临时环境清理：验收标签已关闭，独立18038服务已正常停止；日志与合成媒体只保留在本次/private/tmp验收目录，用户原服务未改动。
- 恢复与未完成：本轮没有提交Git、部署生产、重启原8000服务或迁写用户数据库。PostgreSQL部署需先执行003迁移并验证现有加密/RLS/readiness；旧v2活动会话及历史只读保留，回退仅影响后续新计划版本，已开始v3不得静默套回旧题序/权重。真实基础设施、模型/语音质量、长时并发、不同企业Skill效果和评分可比性仍按P4试点完成，SEMANTIC-INTERVIEWER-DECISION-037为本地verified而非closed。

<details>
<summary>实际修改文件（含新增文件、更新文档与重建前端产物）</summary>

- `CONTEXT.md`
- `app/api/routers/interview_skills.py`
- `app/api/routers/plans.py`
- `app/api/routes.py`
- `app/core/prompt/contracts.py`
- `app/core/prompt/inquiry_units.py`
- `app/core/prompt/interview_skills.py`
- `app/core/prompt/interviewer_supervisor.py`
- `app/core/prompt/semantic_turn.py`
- `app/core/prompt/understanding_references.py`
- `app/domain/adaptive_interview.py`
- `app/domain/interview_agent.py`
- `app/domain/interview_lifecycle.py`
- `app/domain/scoring_quality.py`
- `app/model_gateway/gateway.py`
- `app/model_gateway/schemas.py`
- `app/persistence/interface.py`
- `app/persistence/memory.py`
- `app/persistence/postgresql.py`
- `app/providers/mock/provider.py`
- `app/repositories/memory.py`
- `app/repositories/sqlite.py`
- `app/schemas/api.py`
- `app/schemas/interview_skills.py`
- `app/services/answer_endpoint.py`
- `app/services/conversation_understanding.py`
- `app/services/evaluation.py`
- `app/services/interview_agent.py`
- `app/services/interview_evidence.py`
- `app/services/interview_skills.py`
- `app/services/interviewer_supervisor/__init__.py`
- `app/services/interviewer_supervisor/context.py`
- `app/services/interviewer_supervisor/service.py`
- `app/services/interviewer_supervisor/tools.py`
- `app/services/interviews.py`
- `app/services/livekit_evidence_ingress.py`
- `app/services/plan_assembly.py`
- `app/services/plans.py`
- `app/services/reports.py`
- `app/services/review.py`
- `app/services/spoken_supplement.py`
- `app/transport/service_locator.py`
- `app/web/core/router.js`
- `app/web/dist/bundles/VrmAvatar-C5QqKbly.js`
- `app/web/dist/bundles/VrmAvatar-DV7oEIzn.js`
- `app/web/dist/bundles/index-6jgz8ceW.js`
- `app/web/dist/bundles/index-7qeam4aU.css`
- `app/web/dist/bundles/index-BbHSO_E_.js`
- `app/web/dist/bundles/index-DMQhA0eZ.css`
- `app/web/dist/index.html`
- `app/web/src/App.jsx`
- `app/web/src/App.test.jsx`
- `app/web/src/core/ui.jsx`
- `app/web/src/features/candidate/Page.jsx`
- `app/web/src/features/candidate/Page.test.jsx`
- `app/web/src/features/candidate/agent-experience.js`
- `app/web/src/features/candidate/agent-experience.test.js`
- `app/web/src/features/candidate/presentation.js`
- `app/web/src/features/candidate/presentation.test.js`
- `app/web/src/features/interviews/Review.jsx`
- `app/web/src/features/interviews/Review.test.jsx`
- `app/web/src/features/interviews/agent-event-runtime.js`
- `app/web/src/features/plans/Page.jsx`
- `app/web/src/features/plans/Page.test.jsx`
- `app/web/src/features/registry.js`
- `app/web/src/features/skills/Page.jsx`
- `app/web/src/features/skills/Page.test.jsx`
- `app/web/src/features/skills/index.js`
- `app/web/src/features/skills/skills.css`
- `app/web/styles.css`
- `docs/adaptive-interview-contract.md`
- `docs/adr/0005-goal-directed-interviewer-supervisor.md`
- `docs/api-design.md`
- `docs/architecture.md`
- `docs/change-log.md`
- `docs/database-and-vector-storage.md`
- `docs/development-progress.md`
- `docs/domain-model.md`
- `docs/implementation-roadmap.md`
- `docs/interview-skills-api.md`
- `docs/interviewer-agent-architecture.md`
- `docs/interviewer-supervisor-contract.md`
- `docs/known-issues-and-remediation.md`
- `docs/model-provider-plugins.md`
- `docs/retrieval-and-evaluation.md`
- `migrations/003_interview_skills.sql`
- `tests/test_adaptive_followup_scope.py`
- `tests/test_adaptive_interview.py`
- `tests/test_api_responses.py`
- `tests/test_automatic_turn_integration.py`
- `tests/test_declined_answer_integration.py`
- `tests/test_empty_confirmation_integration.py`
- `tests/test_enterprise_interviewer_flow.py`
- `tests/test_followup_rejection_integration.py`
- `tests/test_inquiry_unit_assessment.py`
- `tests/test_interview_skills.py`
- `tests/test_interviewer_supervisor.py`
- `tests/test_model_invocation.py`
- `tests/test_planning_runtime_fences.py`
- `tests/test_prepared_decision_binding.py`
- `tests/test_semantic_turn_endpoint.py`
- `tests/test_semantic_turn_policy.py`
- `tests/test_spoken_supplement.py`
- `tests/test_spoken_supplement_integration.py`

</details>

## 2026-09-10 · INTERVIEWER-AGENT-ARCHITECTURE-037

- 状态：`verified（仅架构文档、链接检查与当前行为基线；运行时代码改造未开始）`。
- 目标：根据用户对机械补充确认、固定选题和缺少自然交互的反馈，审查真实执行链，设计目标驱动的面试官总控、按需专家、受控工具和企业 Skill，并给出可以分阶段实施的架构改造。
- 关联问题：新增 SEMANTIC-INTERVIEWER-DECISION-037；已有023未作答理解、016补充确认及034稳定性合同保留历史状态，不将设计完成冒充行为修复。
- 交付范围：先完成代码证据、目标架构、领域/接口/评分/模型治理/持久化设计和迁移验收清单；本项不修改运行时代码、真实会话、模型配置或已冻结计划。
- 计划文件：新增 `docs/interviewer-agent-architecture.md`、`docs/adr/0005-goal-directed-interviewer-supervisor.md`，同步 `CONTEXT.md` 及架构、接口、领域、检索评分、供应商、数据库、已知问题、进度和路线图文档。
- 验证计划：核对源码定位、检查新文档本地链接和设计一致性、运行 `git diff --check`；纯设计交付不重复执行未修改的全套业务测试。
- 实际修改文件：新增 `docs/interviewer-agent-architecture.md`、`docs/adr/0005-goal-directed-interviewer-supervisor.md`；修改 `CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。未修改app/tests、配置、数据库或服务运行状态。
- 设计结果：确认正式入口的固定补充握手先于意图理解，且测试固化该次序；规划Supervisor/按需专家/受控工具/企业Skill、真实CompletionBasis、execution v3考察契约、议程与证据账本。独立评审补充等待下一决策状态、合法答案独立提交、实际拆题评分范围、三次以内动态委派调用预算及Skill撤销授权epoch；所有目标均标为未实现，ADR状态proposed，问题037保持open。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_declined_answer_integration.py tests/test_spoken_supplement.py -q -p no:cacheprovider` 为 **22 passed in 9.42s**，这是旧行为基线，不能证明“第一次不会不问补充”已修复。`git diff --check`通过；Python只读检查13份设计文件、16处本地链接及主方案源码引用均通过，代码围栏闭合。新增文档另检查无尾随空白。
- 资料核验：只读取Anthropic Building effective agents、Agent Skills specification及LangGraph Persistence官方文档，用于核对通用模式；具体模块与预算为本项目设计判断，未向供应商发送候选人数据或执行模型调用。
- 失败与恢复留痕：只读定位期间使用了不存在的单数文件路径（question_selection/conversation_decision/report/interview/interview_session），产生sed文件不存在错误；随后使用rg定位实际domain或复数service文件并核对源码，无写入/外部副作用。较长历史文档输出曾被截断，随后针对当前合同和目标相关章节补读；没有用缺失输出作为实现证据。
- 未完成事项：P0–P4代码、Prompt合同测试、Skill注册/API、v3迁移/报告、灰度与真实多轮体验验收均待实施；截图中的机械行为尚未修改。设计文件已完成，不重启服务、不改变真实面试、不提交或推送Git；后续按路线图登记独立实施工作项。

## 2026-09-10 · MERGE-AND-PUSH-DEV-MAIN-036

- 状态：`verified（本地快进合并、远端dev/main原子推送与引用一致性检查）`。
- 目标与授权：用户在确认远端目标的上下文中明确要求“dev合并到main上并且dev和main都提交并推送到远程”，授权向既有origin（git@github.com:zwj19980929-netizen/interview.git）的dev/main同步本轮修复；035的目的地授权阻塞已解除，历史拒绝记录保留。
- 起点：工作区干净，dev=af310e0，main=origin/main=56b232d；本轮不修改业务逻辑，沿用后端1668项、前端225项及构建验收证据。
- 计划：拉取最新远端引用，核对祖先关系，在可快进时将dev合并main，再同步两个分支；不强制推送，不丢弃远端提交。完成后核验两个本地分支、两个远端分支与干净工作区。
- 实际文件：仅修改docs/change-log.md及Git分支/提交/跟踪引用，没有业务代码变更。
- 执行结果：git fetch --no-tags origin成功；远端main仍为56b232d，dev无分歧。先在dev提交授权登记d93dbe4，再git switch main / git merge --ff-only dev成功，main由56b232d快进到d93dbe4，包含全部修复af310e0，无冲突且保留历史。
- 远端验证：git push --atomic -u origin dev main成功，main从56b232d更新到d93dbe4，远端dev新建于同一提交；两分支upstream均已设置。git diff main dev --quiet通过，两组本地/远端rev-list均为0/0，工作区干净。
- 结束归档：将本条实际执行结果补充提交，再以快进和原子push同步到两个分支；最终提交号由包含本项的Git历史及远端引用确定。本轮未强推、未改写已发布提交，无新增测试失败。035自动审批拒绝在本轮明确授权后解除，没有绕过审批。
- 后续：无业务变更待提交；后端1668项、前端225项和构建证据仍对应本次归档代码，纯日志/分支操作不重复运行全套测试。

## 2026-09-10 · GIT-DELIVERY-035

- 状态：`verified（本地提交与暂存检查）`；当时的远端授权阻塞已通过用户后续明确授权及036双分支同步解除，以下历史失败记录保留。
- 目标：按用户要求归档并提交本轮025–034已完成的面试修复；当前分支dev，远端origin为现有项目仓库。
- 范围：现有业务代码、合同/回归测试、文档与受版本管理的前端dist；不纳入被忽略的真实配置、数据库、录音、私有文件或临时运行日志。
- 提交前依据：沿用034对当前代码的完整验证（后端1668 passed/6 skipped、前端225 passed、生产构建通过）；本轮只做归档检查和日志登记，没有再次改业务代码。
- 实际检查：115个暂存变更，新增内容仅为实现、测试、诊断设计文档与构建文件；112个现存待归档文件的私钥/GitHub/provider key模式检查无命中，无运行数据/媒体/真实配置。git ls-remote确认远端只有main，指向修改前HEAD 56b232d，当前dev尚无upstream；按用户要求将本地dev作为远端分支同步，不覆盖main、不强制推送。
- 实际修改文件：本工作项登记`docs/change-log.md`，并清理`app/transport/http/media.py`最后一个多余空行；没有业务行为变更，不重复已通过的全量测试。
- 失败留痕：初次upstream/origin/dev查询因引用未配置失败，无写入；暂存检查首次发现新增media.py的EOF空行（未暂存diff未覆盖新文件），已清理。之后以git diff --cached --check为准，保留前序验证证据。
- 提交结果：已在dev创建修复提交，包含115个文件、代码/测试/文档/构建产物；提交前git diff --cached --check通过。随后将本条实际执行结果并入同一尚未发布的本地提交，具体提交号以包含本项的Git历史为准。
- 远端结果与恢复：`git push -u origin dev`在进程执行前被自动审批拒绝，原因是用户“提交到Git”的指令未被审批器视为对GitHub仓库zwj19980929-netizen/interview这一具体敏感代码目的地的明确授权。没有发生远端代码上传、分支创建或强制覆盖。已向用户说明并请求确认该仓库dev；获得明确授权后可原样重试常规push，本地提交完整保留。

## 2026-09-10 · FOLLOWUP-STABILITY-AND-SPEECH-LATENCY-034

- 状态：`verified（仓库回归、新合成模型实测、本机加载）`；真实长时面试/并发未重新验收，不标closed。
- 目标：定位 iv_e71152c5e7814a20 追问阶段的理解告警，修复长补充回答分类、重试/熔断和诊断缺陷，按实际耗时优化追问语音等待，保留前序025–033修改。
- 初始证据：已接受7题，当前为第8轮追问；07:05起失败记录的实际Prompt为supplement_reply.v2，错误为provider_schema_invalid，继而provider_circuit_open。端点同capture失败预算从3继续增长到10，声音revision变化后仍重试；没有依据归因到麦克风、评分或TTS失败。完整原文/音频与凭据不进入诊断输出。
- 计划：核对Prompt/schema与长回复输出预算，补分类专属有界重试与元数据诊断，测量并优化批准追问的TTS准备/播放路径；补模型合同、真实整链回归和前端提示，同步设计文档。按无活动候选人与worker空闲门禁加载本机，不强行结束面试或改变历史回答。
- 用户追加体验要求：候选人页面不显示模型、通道、底层故障、FPS等实现术语；等待与可恢复故障使用简短面试用语，只有确实需要用户处理时显示清楚的下一步。保留如实的暂停/录制/同意信息及后台诊断，不能把“掩盖故障”当作修复。
- 验证与未完成：诊断中；本轮未再次外发真实候选人录音，任何外部模型验证优先用新的合成文本/音频；首次日志查询包含过多STT记录造成截断，后续限定用途和字段。

- 实际修改文件：`.env.example`、`app/core/prompt/contracts.py`、`app/model_gateway/gateway.py`、`app/providers/{mock/provider,dashscope/tts_streaming}.py`、`app/services/{conversation_understanding,spoken_supplement,answer_endpoint,livekit_evidence_ingress,agent_expression_audio,interview_agent}.py`；前端 `candidate/{presentation.js,Page.jsx,VrmAvatar.jsx}` 与 `presentation.test.js`、Page/InvitationPage/VrmAvatar/session-isolation回归及生产dist；后端 `tests/{test_supplement_contracts,test_spoken_supplement,test_agent_expression_audio,test_tts_streaming,test_interview_agent_stage_metrics,test_declined_answer_integration,test_interview_agent_contracts}.py`；同步CONTEXT及架构/API/领域/评分/供应商/存储/问题/进度/路线图文档。025–033未提交修改保留。
- 分类修复：supplement_reply.v3只输出intent/confidence/evidence_id，服务端恢复逐字证据并再次校验；350输出token、8秒预算、0次provider内部重试，同一原文最多3次端点失败。声学活动不重置，新文字或明确continue_speaking才恢复；重试保持补充边界，不提交旧答案或伪造finish。成功用既有事件清除临时告警，网关记录schema规则与路径用于后续区分实际失败原因。
- 表达与体验：候选人错误只用受控通俗文案、重试操作和如实停答信息；移除FPS/viseme/底层通道/模型错误正文及重复诊断卡，保留同意说明、录制范围与真实暂停门禁。追问只朗读已校验的问题，原quote保留为证据。动态表达默认收齐受管PCM生成私有完整WAV，省掉供应商URL二次下载，失败/取消/Mock不写正式资产；默认关闭的浏览器实时PCM播放未开启。
- 真实合成检查：新写的长技术补充、明确结束、犹豫继续三例经配置理解路由分别 **1411/1468/1292ms**，意图均正确且quote精确来自原文。未读取或上传真实录音/简历/候选人回答。初始TTS直取校验失败后，用合成短句仅观察字段/长度/容器头，确认Qwen字符串null、固定44字节WAVE头及stop前usage空通知；已在adapter严格兼容并加故障合同，未知原因/格式和缺final仍拒绝。协议依据及观察边界见供应商设计文档。
- 修复后TTS对照：同一新写测试句交替三轮，旧batch+下载 **2334/2092/2511ms**，完整PCM **2671/2048/1965ms**，三次均有效final；中位数 **2334→2048ms（约12%）**，平均值仅约4%改善，第一轮新路径更慢。旧下载单独约260–288ms；PCM首片约423–623ms不是用户听到首音，完整播放仍等待final。合成音频长度本身有波动（约7.5–9.2秒），小样本不能保证每次提速或代表真实长追问。日志 `/private/tmp/interviewer-034-benchmark.log`、`interviewer-034-tts-benchmark-final.log`。
- 验证：定向分类/补充/资产/拒答 **55 passed**；TTS协议/资产/指标定向 **103 passed**。最终后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1668 passed/6 skipped in124.33s**（`/private/tmp/interviewer-034-backend-final.log`）；前端 `npm test -- --run` **225 passed/17 files**（`interviewer-034-web-3.log`）。`npm run build` 成功，只有既有大chunk提示；入口index-6jgz8ceW.js、数字人VrmAvatar-C5QqKbly.js；`git diff --check`通过。
- 失败留痕：初次合同升级后10个后端旧fixture失败，更新v3wire和历史v1/v2canonical分别验证后通过；候选人文案改动使17项旧文案断言失败，保留交互/隔离/禁用行为断言并更新文案，后续3项遗漏文案修正后全过。首次全后端1649通过、2项batch指标fixture因新增默认PCM路径失败，为旧batch用例显式关闭开关并新增PCM指标用例后全过。供应商合成探针两轮各3次直取失败均在校验前终止，无正式音频落盘；保留失败日志，未静默将失败视为成功。只读搜索两次使用不存在文件路径、部分长输出截断，均改为精确路径；无业务数据副作用。
- 本机加载：LiveKit只读检查ROOMS=0/ACTIVE_CANDIDATES=0，worker空闲门禁通过后重启API/worker为PID1400/1402；重启后healthz/readyz均200且ready=true，首页实际引用`/web/bundles/index-6jgz8ceW.js`、脚本200。未刷新用户候选人标签、重开摄像头麦克风或重写真实历史回答。模型验证仅留下合成调用的元数据审计与指标，未保存合成音频资产。
- 未完成/恢复：浏览器旧页需要刷新载入新脚本；真实长时面试、并发下供应商抖动及完整端到端首音验收未宣称通过。需要回退直取可设置INTERVIEWER_BUFFERED_TTS_ENABLED=false恢复原完整文件下载路径；其他修复按上述文件撤回并重建dist，不修改历史数据。旧流式实验与供应商兼容分支尚保留，因此不标closed。

## 2026-09-09 · CANDIDATE-SESSION-ISOLATION-033

- 状态：`verified（前端竞态回归、生产构建、本机脚本加载）`；真实候选人重新入场未执行，不标 closed。
- 目标：修复从旧面试切换至新预约时混用旧selectedInterview与新candidateToken、旧403/暂停异步结果污染新页面的问题，隔离会话数据/数字人/实时运行状态并如实展示停止状态。
- 证据：新会话iv_64f16cae229144db于2026-09-10T06:07:06Z创建，状态in_progress，试音warmup_listening，正式答案0；日志中其public GET/avatar-config/avatar-model/agent-ticket全部200、WS accepted。同期旧iv_775785321827419d的avatar-config为409/403，runtime-problems为403。WorkbenchProvider在await前先写token且保留旧selectedInterview；CandidateRoom复用本地startingProblem/avatarReady，没有路由绑定门禁。
- 计划文件：WorkbenchProvider、Candidate Page/VrmAvatar/资产加载、前端竞态与清理回归、dist及架构/问题/进度/路线图文档。不变更后端认证规则、凭据、历史答案或评分；保留025–032全部未提交修改。
- 实际修改：`app/web/src/core/WorkbenchProvider.jsx`、`app/web/src/features/candidate/{Page.jsx,VrmAvatar.jsx,vrm-avatar.js,agent-experience.js,VrmAvatar.test.jsx,agent-experience.test.js}`、新增 `session-isolation.test.jsx`、生产 `app/web/dist`；同步 `docs/{architecture,api-design,domain-model,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。无后端代码、数据库、Prompt、供应商或评分变更，025–032修改保留。
- 实现：公共投影与其请求凭证原子绑定；候选人房间拒绝不匹配路由，旧加载成功/错误按路由和代次失效，局部错误与ready按会话/凭证重建；旧VRM资产可取消且迟到renderer释放。facade open接收取消signal并在启动步骤间检查，旧请求不能领取新页面预检设备；已领取资源在取消后关闭。当前致命错误取消在途启动、调用真实暂停并停止本地表达/答题，旧暂停回执不影响新页，授权错误文案不再武断标为过期。
- 验证：最初11项页面/VRM定向通过；增加启动取消和当前发言故障保护、保留原VRM两项合同后，最终 `npm test -- --run` 为 **218 passed/16 files**（新增14项，`/private/tmp/interviewer-033-web-final.log`）。`npm run build` 成功（`/private/tmp/interviewer-033-build.log`），入口 `index-2uYMAki_.js`、数字人 `VrmAvatar-Db-S2mU3.js`，仅既有大 chunk 提示；`git diff --check` 通过。前端单独变更，未重复运行无改动后端全量测试。
- 本机核验：只读打开后台测试标签，列表正常显示31场面试，DOM script实际为 `/web/bundles/index-2uYMAki_.js`。只读持久层核对原新会话仍in_progress，updated_at仍06:07:55Z，正式答案0；未创建预约、重启API、刷新用户候选人标签、请求真实暂停或再次外发录音。测试标签用完关闭。
- 失败留痕：初次只读搜索命中dist造成截断，随后限源码；日志读取隐藏ticket。新增启动取消测试fixture最初遗漏视频轨，导致一次媒体校验断言失败、另一次等待超时及相关未处理断言；补齐合成轨后全量无失败。新增VRM测试时覆盖了既有两个测试，复核git diff及时发现并恢复原用例，只为新增signal扩展其断言，再加四项竞态测试，未丢失已有覆盖。只读状态检查首次误用不存在的tx.interviews，改为tx.interview_sessions后成功，无数据写入。
- 未完成/恢复：浏览器已打开的旧页面仍持有旧脚本，需要刷新当前候选人页加载修复；没有替用户触发新的麦克风/摄像头采集。真实重新入场与生产长时媒体验收未宣称通过。撤回代码需恢复上述前端变更并重建dist，既有数据库和历史证据不受影响。

## 2026-09-09 · INTERVIEW-LIST-FILTER-SEARCH-032

- 状态：`verified（前端全量回归、生产构建与本机页面目测）`
- 目标：在面试会话列表增加候选人名称搜索和状态筛选，并清晰反馈当前匹配数量。
- 关联问题：当前列表只能滚动浏览；会话较多时无法快速定位候选人，也无法聚焦报告就绪、正在面试、已超时等特定状态。
- 计划修改：在 React 列表加入即时名称搜索、基于页面展示状态的分组筛选、匹配数、清除筛选和无结果空状态；补前端交互回归与响应式样式，重建生产 bundle。纯客户端筛选，不修改后端接口、会话状态或业务数据。
- 实际修改文件：`app/web/src/features/interviews/Page.jsx`、`app/web/src/features/interviews/Page.test.jsx`、`app/web/styles.css`、重建后的 `app/web/dist`，以及本日志。
- 实际实现：列表上方新增带搜索图标的候选人姓名即时搜索、状态下拉和结果数；搜索会去除首尾空格且不区分拉丁字母大小写。状态筛选复用列表实际展示状态，区分真正进行中、候选人已提交后的评分中、预约待开始、暂停、报告处理中/就绪、超时、普通取消和失败；名称与状态可组合。启用任一条件后页头显示“当前/总数”，提供一键清除；无匹配时保留筛选栏并显示可恢复的空状态。小屏下搜索和状态控件自动堆叠。
- 验证命令与结果：定向 `npm test -- --run src/features/interviews/Page.test.jsx` 为 **7 passed**；完整前端 `npm test -- --run` 为 **204 passed/15 files**；`npm run build` 成功生成 `index-DYQSnFFU.js` 与 `index-7qeam4aU.css`，只有既有 VRM 大 chunk 提示；`git diff --check` 通过。本机 `127.0.0.1:8000/#interviews` 目测搜索框、状态下拉、结果徽标与表格对齐正常；实际选择“已超时结束”后显示 **27 / 30**，每行均为“已超时结束”，清除筛选恢复30条。
- 失败留痕与恢复说明：新增测试首次用直接赋值触发 React 受控搜索框，JSDOM 的 value tracker 将其视为未变化，导致两项断言失败；改为通过原生 `HTMLInputElement.value` setter 模拟真实输入后，定向和全量测试均通过。失败只发生在合成测试 DOM，无业务数据或外部副作用。本轮只读打开本地页面、选择状态并清除，没有点击查看、移除、刷新或创建预约；未修改后端接口、状态机或任何真实会话数据。

## 2026-09-09 · INTERVIEW-DEADLINE-RECONCILIATION-031

- 状态：`verified（仓库回归、生产构建、本机补偿与页面目测）`
- 目标：让超过预约结束时间、且候选人尚未完成全部输入的面试会话自动结束，避免浏览器中途退出后长期残留为“进行中”；统一列表操作列宽度，并在真正进行中的会话不可移除位置明确显示“正在面试中”。
- 关联问题：当前只在候选人开始时校验预约时间窗，没有后台截止时间回收；`InterviewSession.status=in_progress` 因页面退出不会自然迁移。列表只为终态渲染三点菜单，进行中行的操作区因此留白且与其他行不对齐。
- 计划修改：通过既有 InterviewSession 生命周期 seam 将超过 `scheduled_end_at` 的未完成会话收口为带明确截止原因的终态，周期任务负责进程重启后的补偿，并停止相关媒体采集/权威 Evidence、发布实时快照；已完成候选人输入、正在评分/报告的会话不误终止。前端以“已超时结束”区分截止收口，并固定次级操作槽，进行中显示“正在面试中”。补领域、服务、后台任务、React 与回归测试，重建生产 bundle，同步架构、接口、领域、存储、统一术语、进度和路线图；不修改回答、评分内容或删除历史证据。
- 实际修改文件：`.env.example`、`app/main.py`、`app/domain/interview_lifecycle.py`、`app/services/{interviews,interview_agent,livekit_evidence_ingress}.py`、`app/web/src/core/ui.jsx`、`app/web/src/features/interviews/{Page.jsx,Page.test.jsx}`、`app/web/styles.css`、`tests/test_interview_session_aggregate.py`、重建后的 `app/web/dist`；同步 `CONTEXT.md` 与 `docs/{architecture,api-design,domain-model,database-and-vector-storage,development-progress,implementation-roadmap,change-log}.md`。无 DDL、Prompt、评分规则或供应商路由修改。
- 实际实现：周期 watchdog 默认每15秒调用唯一截止协调入口；使用会话冻结 `scheduled_end_at`，旧会话缺字段时从绑定预约兼容读取。只有 `scheduled/waiting/in_progress/paused` 且没有 `candidate_input_completed_at` 的会话经既有 `CANCEL` 命令收口，写 `termination_reason=appointment_window_expired`、`expired_at`、单条生命周期事件和 metadata-only 审计；随后停止进程内 Evidence/全场媒体并发布快照。已提交输入的评分/报告不误关。列表将该终态显示为“已超时结束”，将提交后的 `in_progress` 显示为“已提交，后台评分中”；操作列保留92px次级槽，可移除终态显示三点菜单，真正进行中显示“正在面试中”，移动端隐藏重复状态文字但保留菜单对齐。
- 验证命令与结果：后端定向首次 `15 passed`，Evidence/Agent扩展定向 `121 passed`；完整 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` 为 **1635 passed/6 skipped in 124.77s**。历史会话 appointment fallback 加入后最终定向 `tests/test_interview_session_aggregate.py` 为 **8 passed**；`compileall` 通过。前端定向 **5 passed**、全量 **202 passed/15 files**；`npm run build` 成功生成 `index-Dd2sueU2.js` 与 `index-K983yCcx.css`，只有既有 VRM 大 chunk 提示；`git diff --check` 通过。本机 API/Worker 为 PID `78516/78517`，`readyz.ready=true`。
- 本机状态补偿与页面：重载前只读核对有28条 `in_progress/paused` 且无输入完成标记的会话，均已超过预约结束时间（其中一条会话缺冻结结束时间，但绑定预约可提供）；LiveKit无候选人、Worker无活动任务后启动新服务。15秒内28条全部变为 `cancelled + appointment_window_expired`，活动列表归零，31条会话总数和全部证据保留。截图目标 `iv_3bdec492ad8f4247`、`iv_58da3095bac941c6` 各只有1条截止事件。实际1440×900页面目测候选人/时间/状态/操作列对齐，“已超时结束”徽标和右侧三点菜单位置一致；未点击任何移除项。
- 失败留痕与恢复说明：前端首次定向命令误用仓库根相对路径，Vitest报告未找到文件且无副作用；改为包内路径后5项通过。第一次本机重载误加载 `.env.production.local`，指向已停6天的验证 PostgreSQL/Redis/ClamAV，readiness失败且截止任务未连接数据库、没有改变会话；随即按原本地 SQLite 配置再次通过空闲门禁重载，readiness恢复。截止收口是用户要求的正式生命周期终态，没有自动恢复为进行中的入口；它不删除已接受回答、录音、评分、报告或审计，仍可查看或从列表逻辑移除。

## 2026-09-09 · INTERVIEW-LIST-VISUAL-POLISH-030

- 状态：`verified（前端全量回归、生产构建与本机页面目测）`
- 目标：优化面试会话列表的视觉层级和操作密度，保留029的列表移除语义与安全约束。
- 关联问题：当前大白卡与重复描边按钮占用空间过多；禁用的红色“移除”在大量进行中/暂停会话上形成视觉噪声，候选人、时间、状态和操作缺少清晰列对齐。
- 计划：改为带列标题的紧凑工作台列表，增加候选人头像/会话短编号，将“查看详情”降为轻量操作；只有可移除终态显示三点菜单，危险操作收纳为“移出列表”。补响应式样式、前端交互回归、生产构建与本机目测；不修改后端接口、状态机或任何真实会话数据。
- 实际修改文件：`app/web/src/features/interviews/Page.jsx`、`app/web/src/features/interviews/Page.test.jsx`、`app/web/styles.css`、重建后的 `app/web/dist`，以及本日志。未修改029后端接口、会话数据或领域状态。
- 验证命令与结果：定向 `Page.test.jsx` **5 passed**；前端全量 **202 passed/15 files**；`npm run build` 成功，入口 `index-C_Wubcuu.js`、样式 `index-C9pmD4Jx.css`，仅既有 VRM 大 chunk 提示；`git diff --check` 通过。本机 `/#interviews` 实际加载后，桌面列表表头、行分隔、头像、时间/状态对齐、“查看详情”轻操作及终态三点菜单均目测正常。
- 页面操作与数据边界：只展开并关闭第一条报告就绪会话的三点菜单，确认“移出列表”浮层位置与颜色；没有点击该危险菜单项或确认按钮，31场现有会话保持不变。进行中/暂停会话不渲染三点菜单，报告就绪/已取消会话保留入口。
- 失败留痕：首次构建工具调用参数字符串不完整，命令未执行；随后使用正确工作目录构建成功。前序临时浏览器标签已脱离当前会话，未复用或操作用户标签，改为新建隐藏本机验收标签；无数据副作用。
- 未完成事项或恢复说明：本轮仅优化当前桌面与既有小屏断点，没有新增筛选、分页或回收站；这些不是本次视觉调整目标。恢复可回退上述 JSX/CSS 并重建 dist，不涉及数据恢复。

## 2026-09-09 · INTERVIEW-LIST-REMOVAL-029

- 状态：`verified（仓库全量回归、本机页面加载与确认框目测）`
- 目标：在企业面试会话列表为管理员/面试官增加“移除”入口；采用会话列表逻辑归档，不物理删除候选人资料、回答、评分、报告、录音或审计证据。
- 关联问题：当前列表只能查看，已取消或报告就绪的会话无法从日常工作区清理；直接删除候选人或会话证据会破坏历史可追溯性。
- 计划：仅允许移除`cancelled`、`report_ready`终态会话，使用版本并发控制并记录真实操作者审计；默认列表过滤已移除会话，详情仍可按ID读取。前端提供二次确认、状态约束、成功刷新和错误提示；补后端/前端回归并同步接口、领域与存储说明。
- 实际修改文件：`app/services/interviews.py`、`app/api/routers/interviews.py`、`app/web/src/features/interviews/{Page.jsx,Page.test.jsx}`、`tests/test_interview_session_aggregate.py`，以及生产前端 `app/web/dist` 重建；同步 `docs/{architecture,api-design,domain-model,database-and-vector-storage,development-progress,implementation-roadmap,change-log}.md` 与 `CONTEXT.md`。无 DDL、Prompt、供应商、评分或保留策略变更。
- 验证命令与结果：定向后端 `tests/test_interview_session_aggregate.py + test_auth_audit.py + test_persistence_contract.py` 为 **31 passed**；前端全量为 **202 passed/15 files**；`npm run build` 成功，入口 `index-BfFVzpTq.js`，只有既有 VRM 大 chunk 提示。第二次完整后端回归为 **1634 passed/6 skipped in 121.54s**；`compileall`、`git diff --check` 通过。本机 `/#interviews` 已加载卡片、可用/禁用移除按钮及二次确认文案；只打开后关闭确认框，未提交真实移除。
- 本机加载：既有守护重启脚本先确认 LiveKit 无候选人参与者且 Celery 无活动工作，再正常 TERM 并启动 API **72866**、worker **72867**；`healthz=ok`、`readyz.ready=true`，运行中 OpenAPI 的 `/api/v1/interviews/{interview_id}` 同时列出 GET/DELETE。重载后工作台仍显示 31 场原会话和正确状态约束，没有执行任何真实移除；PID/日志继续使用 `/private/tmp/interviewer-025-{pids.json,api.log,worker.log}`。
- 失败与修正留痕：首次多文件 patch 因存储文档标题上下文不匹配整体未写入，拆分后成功。新后端测试首次沿用取消响应版本，但取消后的实时快照发布已推进聚合版本，正确改为重新读取最新版本后提交，与前端并发 helper 一致。首轮全量为 1633 passed/6 skipped/1 failed，既有自动收音时序用例超过截止约 21ms；该用例隔离复跑 1 passed，第二次全量无失败。沙箱 shell 首次无法直连本机 8000、浏览器直接打开 openapi.json 被客户端拦截；随后按网络权限读取 OpenAPI 并确认旧进程未加载 DELETE，执行有空闲门禁的既有重启脚本后再次读取确认 GET/DELETE 均已加载。以上失败均无数据副作用。
- 未完成事项或恢复说明：本工作项不提供“已移除”回收站或 UI 恢复入口；详情和证据仍按 ID 保留，敏感数据清除继续走 retention purge。没有修改任何现有会话状态、候选人资料或历史媒体；如需撤回代码，移除新增 DELETE 路由/服务字段过滤/前端入口并重建 dist，已有数据中的可选标记不会破坏旧读取。

## 2026-09-09 · RESUME-TAIL-AND-CHINESE-ASR-028

- 状态：`verified（仓库回归、本机API、新计划与合成ASR）`；真实口音对照和锁屏后的页面目测仍pending，不标closed。
- 目标：修复计划漏选简历题；默认选同候选人同岗位最新合格审核的最多3道已批准、有简历证据的问题，岗位题之后提问。候选人确认预约后异步生成专属语音，开场不等简历TTS，到尾部逐题校验就绪，未就绪明确跳过且不计0分。补中文术语与流式接口场景识别词表，继续直接语义评分。
- 证据：iv_775785321827419d 已report_ready，六题全position_bank；冻结plan.resume_review_id为空，而同候选人同岗位存在有效审核及3题approved。PlansPage请求漏传审核，后端也不自动关联。Q5最终服务端转写确有流失/流逝/刘四/Steam，现有热词只提取ASCII；不覆盖中文流式术语。保留历史原文、录音及025–027未提交修改。
- 计划文件：计划装配、预约准入、会话与生命周期、识别词表/网关合同、计划与预约页面；增加迟到TTS、跳过、组织/候选人边界和中文热词回归，并同步设计文档。
- 验证/结果/未完成：实施中，尚未加载本机；不修改已经结束的面试题目或伪造历史答案。
- 数据修复范围补充：为当前候选人/岗位通过既有生成API创建一份新批准计划（原6道岗位槽位数量+3道简历尾题），供后续预约使用。旧计划和已结束会话不变；核对无新增简历TTS任务、无新增预约/通知。先记录再执行，结果与新计划ID随后补充。
- 实际文件：`app/services/{plan_assembly,interviews,avatar,agent_expression_audio,reports,review,recognition_vocabulary}.py`、`app/domain/{appointment_admission,appointment_speech,interview_lifecycle,recognition_lexicon}.py`、`app/model_gateway/schemas.py`、`app/providers/dashscope/provider.py`、`app/web/src/features/{plans/Page.jsx,interviews/Review.jsx,interviews/Review.test.jsx}`与dist；新增`tests/test_resume_tail.py`并更新position_resume_appointment_flow/recognition_vocabulary。同步架构、API、领域、评分、供应商、存储、CONTEXT、问题、进度、路线图及`docs/resume-tail-and-asr-review.md`。无DDL或供应商路由修改。
- 行为验收：真实候选人已有3道approved有证据问题；新计划`plan_e173aa922a004cf5`已approved，6岗位+3简历，绑定`resume_review_e0c546ac45f74142`。已有会话JSON前后完全一致，简历TTS任务集合及预约集合均未增加；日志/private/tmp/interviewer-028-plan.log。未向候选人发邮件/邀请。
- 回归：定向35 passed in3.54s（/private/tmp/interviewer-028-focused-2.log）；完整`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1633 passed/6 skipped in125.07s**（/private/tmp/interviewer-028-full.log）。前端`npm test -- --run` **201 passed/15files**（/private/tmp/interviewer-028-web.log）；`npm run build`通过，index-CKBK5E0m.js，既有大chunk提示。git diff --check通过。
- 覆盖：默认最新同组织/候选人/岗位合格审核，保留显式选项、最多3题、未批准/删除排除、缺题警告；确认预约后排队且重复登记不重复工作；TTS未就绪直接开场、2题迟到可问/第3题跳过、版本/音色/owner/取消/生产mock不匹配拒绝、已跳过不复活、迟到资产签名播放归属、全简历未就绪时报告只按实际答案计分。中文词表来源/边界、400字符上下文合同及不同模型范围也覆盖。
- 本机加载：无活动候选人且worker空闲后正常重启API53276/worker53277；healthz/readyz/home均200，LiveKit rooms0。运行日志/PID仍沿用025位置，重启记录/private/tmp/interviewer-028-restart.log。CUA尝试查看页面时Mac锁屏且无法自动解锁，未声称页面目测通过；前端自动化与构建通过。
- 合成ASR：本机Tingting生成独立14.040375秒示例，经当前真实ASR识别，15.28秒完成，识别出流式响应/流式返回/streaming/RAGFlow/REST API/向量召回/重排；/private/tmp/interviewer-028-synthetic-asr-2.log。只证明此合成例可识别，不等于非标准发音准确率提升已量化。
- 失败留痕：初次patch上下文不匹配未写入；新测试fixture遗漏组织、错误码断言不符、mock资产无file_id、PCM类型遗漏及报告已自动完成后重复领取，修正后全通过。首次本地房间检查遗漏INTERVIEWER_LOCAL_MEDIA=true导致401，补开关后成功。沙箱系统语音首次产生零长度WAV（仅开流、无识别内容，不计准确率通过），经批准调用本机say重新生成14秒有效音频后完成真实合成识别。
- 明确待授权事项：候选人既有录音末60秒的再次外发对照被自动审批拒绝，理由为敏感录音重新发送到外部ASR需要明确授权；脚本未执行、未发送该录音，已通过异步问题询问用户，尚未收到答复。没有绕过拒绝，合成测试只读取新的synthetic WAV。原始转写/音视频/分数未改。


## 2026-09-09 · DIRECT-SCORING-AND-TERM-RECOGNITION-027

- 状态：`verified（仓库、真实ASR热词接入、本机直接重评与页面）`；真实口音识别质量与生产验收仍pending，不标closed。
- 用户调整：直接评分和生成报告，不以转写疑点要求人工核验；尽量优化识别，接受候选人术语发音不标准。此项明确替代026的强制待核验产品策略，保留未知置信度真实性、录音、语义容错和可选纠错。
- 计划：统一质量投影保留有效数值、疑点改为非阻断提示，恢复历史暂定数值并按冻结权重汇总；前端与导出同步。评分Prompt升级v5，加入当题术语表及逐词/字母/近音的上下文理解规则；识别热词补参数自然读法，原文证据不改写。增加回归、真实模型评分及本地加载验证，保留025/026全部未提交工作。
- 实际文件：`app/domain/scoring_quality.py`、`app/services/{review,reports,evaluation,recognition_vocabulary}.py`、`app/core/prompt/contracts.py`、`app/model_gateway/{schemas,gateway}.py`、`app/web/src/features/interviews/{Review.jsx,Review.test.jsx}`及dist。测试`tests/test_{speech_quality_governance,recognition_vocabulary,interview_processing_recovery,declined_answer_evaluation}.py`。同步架构、API、领域、评分、供应商、存储、CONTEXT、问题、进度、路线图、两个复盘文档与本日志。无DDL、供应商路由、凭据、麦克风采集策略改动。
- 实现：valid_score验证有限0–100的真实数值；低置信度和转写歧义只保留recognition_warning/flags，旧provisional_score与维度只读恢复，报告按冻结权重恢复总分，缺少实际分数仍unavailable/processing，绝不以失败凑0分。企业完成复核不再要求解决识别提示；原回听纠错接口保持可选、版本/身份/并发绑定。网页和JSON/CSV正常输出分数及非阻断识别提示；不显示null分或强制待核验。
- 识别与评分：当题原标识优先，再追加下划线/驼峰自然读法，最多100项/64字符/6词，标准答案和候选发言不作为词表来源；原DashScope词权重2和支持模型范围不改。answer_evaluation.v5直接传入术语表，要求按概念/作用/操作语义认可非标准读音，不按发音或置信度额外扣分，疑点仍可提示且证据保留原文。官方接口依据已记录于供应商文档。
- 自动化：定向 **88 passed in1.40s**（/private/tmp/interviewer-027-focused.log）；完整`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1621 passed/6 skipped in121.84s**（/private/tmp/interviewer-027-full.log）；前端 **200 passed/15files**（/private/tmp/interviewer-027-web.log），build通过（/private/tmp/interviewer-027-build.log，入口index-DhKweXm3.js，既有大chunk提示）；compileall和git diff --check通过。新增覆盖低STT仍出分、可选人工纠错、旧暂定值按3:1权重恢复、不改历史、无效值不能造分、热词来源/边界、页面与导出。
- 真实ASR合同验收：`interviewer-027-asr-terms.py`对现有qwen-audio-3.0-asr-flash-streaming成功开流并立即关闭；8个词包含worker/task配置的原标识及自然读法，未发送候选录音或创建CandidateAnswer；日志/private/tmp/interviewer-027-asr-terms.log。此项只证明供应商接受新词表，不等于口音识别准确率已测量。
- 本机加载与原题重评：restart再次确认无活动候选人及空闲worker后正常切换至API **41827**/worker **41828**（/private/tmp/interviewer-027-restart.log，运行日志与PID沿用025文件）。healthz/readyz/home均200；先确认旧第三题66/总分76已可直接读取，再调用已有regrade接口用v5重评第三题，HTTP200，真实DeepSeek成功一次、107.051秒。新第三题 **55**、全场 **75**，其余84/88/81/59/77不变，报告available，transcription_ambiguity仅提示。JSON/CSV均输出数字；原始转写/录音/核验事实散列及所有旧评分/报告内容保持一致，新建1评分revision与1报告revision（共7/2），未伪造回听。结果/private/tmp/interviewer-027-regrade.log。
- 新分数说明：55是新Prompt下的实际模型重评结果，未手动提分；模型认可CPU/IO调优、prefetch/超时限流相关内容，列出的缺失为broker/result backend选型理由、指数退避/最大次数等重试细节及水平扩展方案。评分变化也说明单次LLM输出仍有波动，不能把本次重评当作公平性已完全校准。
- 浏览器验收：新版原会话显示第3题55分、总分75/100、6/6完成、无待核验门禁，逐题音视频入口保留、纠错文字明确为可选。旧CUA标签已被关闭，重新定位同一本地URL后验收，无候选表单写入。一次只读诊断误用系统Python缺cryptography，改用项目venv完成；一次文件搜索命中不存在路径/误入dist导致输出截断，均无数据修改。
- 未完成与边界：真实不同口音、背景噪声和专业术语准确率仍需新实机语音样本验证；未重新转写或改写历史音频/原文。录用决定仍由人负责，可选回听功能继续保留；按用户本次要求，识别不确定性不阻断评分与报告。

## 2026-09-09 · SPEECH-UNDERSTANDING-FAIRNESS-026

- 状态：`verified（仓库、真实合成语义与本机加载/回放）`；真实麦克风长场景和生产验收仍pending，不标closed。
- 目标：补齐未知识别置信度、基于原文疑点的同题澄清、口语/术语容错与争议评分待核验闭环。
- 证据：025原会话6题DashScope置信度均被适配器写为1.0；第三题transcription_ambiguity仍以66分计入76分总分。普通评分未按识别质量执行统一保护，澄清话术笼统且重新采集可能丢失此前回答上下文。
- 计划：统一STT允许未知置信度并保留来源；未知不等于低可信，不阻塞正常语义理解。新增严格原文绑定的澄清焦点，在正式连续采集中保留完整录音/前文；Prompt版本化约束停顿/口头重复/可理解误拼不得扣技术分。统一评分质量门禁、待核验报告及导出、权限/版本绑定的回听确认或修正转写后异步重评；保留历史revision。补充供应商、完整采集、评分/复核/报告与前端回归。
- 修改与验收：进行中；沿用025全部未提交文件，禁止覆盖其已验证修复。最终记录实际文件、失败尝试、验证结果及本机加载情况。
- 实现：STT四类适配器不再伪造高可信值；统一schema与聚合保留None/真实低分段，答案记录provider/partial/unavailable/synthetic来源。understanding v8/v9、decision v7/v8强制wire显式返回可空澄清焦点，逐字证据校验；answer_clarification.v1在同一连续采集内播报、保留前文，静音不能重复澄清或提交。answer_evaluation.v4送入实际识别来源与人工核验事实，口语习惯不作为技术/表达扣分依据。
- 评分与复核：scoring_quality集中处理低可信、原理解歧义和模型争议，保留暂定值、置空确定分数和总分；未知本身不判错。新增版本绑定的transcription-verification接口202，真实登录身份、回听声明、同题并发保护、修正和Outbox同事务；保留原录音、评分及报告revision。旧transcript修正入口也将改文和重评分排队改为同事务。报告/导出/读取/完成回执一致投影；未决评分禁止完成复核。Review页展示待核验、未知置信度、回听表单和重评旧版本提示，前端event codec接受null。
- 中途验证与留痕：最初定向收集因media_http漏Optional导入失败，修复后运行；一次误用不存在的测试文件导致exit4无测试；SQLite新断言错误地依赖数组顺序，已按evaluation_id核验。首次全量19 failed/1584 passed/6 skipped，均为新Prompt版本断言未同步，后更新；141项定向仅1项旧整段0.95断言未体现分段0.9，按保留低分段合同调整。其后完整1606 passed/6 skipped（123.69秒，/private/tmp/interviewer-026-final-full.log）。
- 真实合成：沙箱首次网络失败触发短期供应商熔断，未改路由/凭据，获准网络后真实评分2/2成功（同义流畅/口语均98，表达维度100/95，不能宣称完全无偏差）；待正常冷却后真实理解3/3成功。发现可选焦点虽在Prompt要求仍被结构化生成省略，改为wire必填但可null，同步Mock和8个测试fixture的显式字段；最终真实3/3且歧义焦点为原文“它到底是进程内还是独立服务”，解释后next。日志分别为/private/tmp/interviewer-026-semantics.log、-semantics-network.log、-understanding.log、-understanding-focus.log、-understanding-required.log。没有外发旧录音或写入候选答案/回听声明，仅新增常规模型调用审计。
- 收尾检查：补新Prompt版本审计白名单和5项合同测试；必填焦点调整后定向200 passed（11.86秒，/private/tmp/interviewer-026-required-impact.log）。前端最终200 passed/15files（/private/tmp/interviewer-026-final-web2.log），构建成功（仅既有VRM大chunk提示）；compileall、git diff --check通过。完整最终回归与加载结果待追加。


- 实际文件：新增`app/domain/{speech_quality,scoring_quality}.py`、`tests/test_speech_quality_governance.py`、`docs/speech-understanding-review.md`；修改`app/domain/interview_agent.py`、`app/model_gateway/{schemas,gateway}.py`、`app/core/prompt/{contracts,understanding_references}.py`、`app/providers/{dashscope,openai,media_http,volcengine,mock}/provider.py`、`app/schemas/api.py`、`app/api/routers/interviews.py`、`app/services/{continuous_stt,conversation_understanding,spoken_supplement,answer_endpoint,interviews,interview_agent,livekit_evidence_ingress,evaluation,reports,review,fairness}.py`。前端`features/candidate/agent-experience.js`、`features/interviews/{agent-event-runtime.js,agent-event-runtime.test.js,Review.jsx,Review.test.jsx}`、styles.css及dist重建。同步`tests/test_{answer_declined_understanding,declined_answer_evaluation,declined_answer_integration,interview_agent_contracts,model_invocation,optional_followup_isolation,prepared_decision_binding,prepared_turn_decision,prompt_governance,recognition_vocabulary,spoken_supplement_integration,stable_preview_preparation,supplement_contracts,understanding_safety,interview_processing_recovery}.py`；架构、API、领域、评分、供应商、存储、CONTEXT、问题、进度、路线图、025复盘及本日志。025前序改动完整保留，无DDL变更。
- 必填字段切换留痕：首轮完整运行在Mock和fixture尚未补齐新字段时已加载旧输入，112 failed/1499 passed/6 skipped in253.93s（/private/tmp/interviewer-026-required-target.log）；显式null仅加入完整理解fixture，不放宽字段校验，另增加缺字段拒绝合同测试。全部修改完成后的最终`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short`为 **1612 passed/6 skipped in126.37s**（/private/tmp/interviewer-026-release-full.log）。前端最终 **200 passed**，build入口`index-CxNy0BFP.js`，compileall及git diff --check通过。
- 本机加载：只读LiveKit为0活动房间，restart脚本再次核验无candidate且Celery空闲后正常TERM并启动API **34432**、worker **34433**（/private/tmp/interviewer-026-restart.log；日志沿用025-api.log/025-worker.log，PID文件沿用025-pids.json）。healthz/readyz/home均200；真实原会话review显示6题评分完成、第三题pending_verification、总分null、6题音频/视频均可用，JSON/CSV导出同样待核验。/private/tmp/interviewer-026-live-check.log记录历史答案/转写/录音引用/评分/报告散列完全不变。
- 浏览器验收：重新加载真实新版bundle，原会话第三题待核验、原建议66分有明确暂定标识、历史伪1.0显示未知；未勾选真实回听声明时保存按钮禁用。独立WAV时长188.98秒、currentTime17.26、readyState4、无错误且正常播放；本题MP4自动定位到391.82秒后继续播放，1280×720/1056.996秒/readyState4/无错误。已停止验收播放，保留第三题及展开的核验表单，没有点击真实回听声明或提交任何候选更正。
- 未完成事项：需真人回听第三题并核验实际术语后产生新的正式评分；不能代签回听或给历史答案补知识。真实麦克风长停顿、口音/噪声、专有词逐词准确率、生产外部服务仍待实机验收；当前规则与小样本真实文本探针不保证所有模型评分完全一致。缺陷、操作入口与验证边界见`docs/speech-understanding-review.md`。

## 2026-09-09 · INTERVIEW-REPORT-PLAYBACK-025

- 状态：`verified（仓库、原会话评分恢复与本机浏览器回放）`；生产外部服务验收仍pending，不标closed。
- 目标：修复面试提交后的评分截断、失败恢复与自动报告闭环；补齐企业全场/逐题音视频回放和录像最终校验。
- 关联问题与证据：目标会话 `iv_b83f260b6a8744f1` 已于06:51:20Z收齐6份答案，6项评分均dead_letter，每项5次共30次真实调用；DeepSeek输出预算1200且reasoning_tokens=1200、正文为空。6份逐题WAV存在（159.30/130.52/188.98/104.12/116.14/88.06秒），MP4存在（410511004字节），capture却停留hash_pending；StopEgress返回EGRESS_ENDING，缺少异步最终校验。企业页只渲染实时监看，不读取复核与历史媒体，且以private_uri存在误报hash已校验。
- 计划：评分独立输出预算、有限自适应截断恢复与正确错误分类/租约；持久化录像收尾工作、私有签名与Range回放；真实完成/失败/处理中投影、逐题选择和后台重试；新增合同与集成回归并同步相关设计文档。保留冻结答案、转写和历史评分，恢复仅经过领域/工作接口。
- 首轮完整验证：1589 passed/6 skipped（117.92秒），前端195 passed/15文件，build成功。初轮2处失败分别为未知RuntimeError需保留可重试语义及Prompt旧版本断言，修正后通过；新增执行预算测试首轮误用了不存在的registry属性，改为既有provider_clients注入后30项通过。
- 运行恢复留痕：读取进程/本地Python网络受沙箱限制后按授权重试；误套生产环境文件导致本地录制readiness拒绝，随后使用与当前服务一致的本地开发环境，未改变持久配置。只读ListRooms首次使用roomAdmin而无roomList权限返回401，按独立只读roomList grant纠正。重启前确认仅2个企业监看连接、无候选人且worker空闲；首次新API24624/worker24625，数据库先备份至权限0600的/private/tmp/interviewer-025-before-recovery.sqlite3。第一轮恢复已完成录像校验，但6项评分暴露原路由timeout30/retry1：12次真实超时及24次本地熔断拒绝，均未生成评分或改变答案，保留此失败历史。继续补齐通用InvocationExecutionBudget，评分120秒/网关重试0、暂时故障Outbox至少35秒；重新加载API25306/worker25307，先重放一题验证真实模型，避免再次批量失败。
- 录像验收：LiveKit最终EGRESS_COMPLETE，Provider size与本地410511004字节一致；ffprobe为AAC+H264/1280×720/1056.996秒。浏览器第三题独立WAV duration188.98、readyState4、currentTime递增无错误；对应视频readyState4、1280×720、currentTime383.619从第三题窗口播放无错误。旧视频窗口含读题，不宣称精确剪辑。
- 最终原会话恢复：首题真实模型90696ms成功后，仅将其余5项通过`POST /api/v1/interviews/iv_b83f260b6a8744f1/processing/retry`重放；6项均在本轮第1次尝试完成，真实耗时52228–90696ms，全部answer_evaluation.v3。最后一题于07:56:08Z落库后自动生成`report_631629298d6c49f7`，session=report_ready、总分76、manual_review；6题顺序分数84/88/66/81/59/77。浏览器无需手动“完成”即自动显示“报告已生成、6/6、待处理0、失败0”。媒体收尾及报告工作也均completed。对比备份确认6份原答案的id/turn_id/audio_uri/final_transcript/created_at/duration_seconds完全一致，未改写历史回答或录音。
- 最终验证命令及结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1590 passed、6 skipped、119.83秒**（`/private/tmp/interviewer-025-acceptance.log`）；`npm --prefix app/web test -- --run` **195 passed/15files**（`/private/tmp/interviewer-025-web-final.log`）；`npm --prefix app/web run build`成功（`/private/tmp/interviewer-025-build-final.log`），仅既有VRM大chunk提示。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-025-pycache .venv/bin/python -m compileall -q app`与`git diff --check`通过。新增回归覆盖Memory/SQLite失败单次终止和幂等恢复/自动报告、单次扩展预算、关键点入Prompt、严格嵌套Schema、路由执行预算、ENDING部分文件拒绝/无webhook补偿、视频分块Range/HEAD/416/许可撤销/租户隔离，以及前端切题竞态、时间窗口、处理失败与自动刷新。
- 实际修改文件：`.env.example`、`CONTEXT.md`；`app/adapters/{livekit_media,local_media,private_media}.py`、`app/api/routers/{interviews,talent}.py`、`app/core/{auth,prompt/contracts}.py`、`app/domain/interview_lifecycle.py`、`app/file_storage/{interface,local,aliyun_oss}.py`、`app/model_gateway/{schemas,gateway}.py`、`app/persistence/interface.py`、`app/services/{evaluation,interviews,media_capture,review,streaming_stt}.py`、`app/transport/http/media.py`、`app/workers/outbox.py`；`app/web/src/core/ui.jsx`、`app/web/src/features/interviews/{Page,Review,Review.test}.jsx`、`app/web/styles.css`及重建`dist/index.html`/3份bundle；`tests/{test_interview_processing_recovery,test_interview_agent_contracts,test_model_invocation,test_recognition_vocabulary}.py`；系统架构/API/领域/检索评分/供应商/存储/已知问题/进度/路线图/本日志及新增`docs/interview-report-playback-review.md`。无需DDL迁移；未改持久化模型路由或凭据。
- 运行状态与恢复方式：本机API25306和worker25307持续运行新代码，日志分别在`/private/tmp/interviewer-025-{api,worker}.log`；数据库备份保留，不回滚成功评分。将来失败可在复核页“重试未完成处理”沿同一工作ID恢复，成功工作不重复评分；短期媒体许可过期后重新点击播放。旧浏览器页面刷新后加载`index-B5Hiz8-U.js`。
- 未完成事项与边界：历史MP4仅按题目服务端时间窗口导航；新录音开始/结束时间与时长有自动化覆盖，但尚未另开真实候选人完成一场新面试验证精确回答窗口。真实生产OSS/跨实例/外部凭据与浏览器录制质量仍需部署验收；不能将本机恢复称为生产验收。后续可加入评分尾延迟、dead-letter和录像收尾超时告警；此次不新建监控自动化。

## 2026-09-08 · CONFIRMED-PREPARATION-WAIT-024

- 状态：`verified（仓库、真实合成决策与本地加载）`；真实麦克风长会话与外部模型尾部时延仍pending，不标closed。
- 目标：追查并修复iv_25335313fb1b4dea在正式Celery题收到结束确认后长期整理的问题，说明实际等待阶段。
- 初始证据：在线仍PID49796/022，023未获得活动面试重启选择，未加载。当前turn_271399205b6b47d9 asking、0个已接受utterance，全会话3份CandidateAnswer；日志反复client/audio input_invalidated后understanding阶段TURN_DECISION_STALE。不能把此等待当作已在评分或生成下一题，不能在未核查时声称单个模型推理用了数分钟。
- 计划：按会话关联模型耗时、ASR新文字、准备缓存及取消窗口，复现同证据长时间不能提交的路径；保留新话语撤销、完整服务端final、所有权和PCM不丢失约束。改善后台状态可观测性并验证；历史证据不补交、不改写。
- 深入证据：14:38:25/41两次finish/0.95；14:38:54–14:43:38同输入18次decision.v4（两种request_hash为10次原请求/8次纠正），每次12.328–14.164秒，累计模型234.399秒，处理跨度297秒。它们全部通过网关原始复合Schema后，在后续校验被记为wire_schema_invalid；旧日志未区分reference/canonical/domain/followup gate且未存原响应，不能断言准确失败字段。14:38:38–14:43:51没有新ASR句子，却27次invalidated、25次understanding STALE。
- 确认的放大缺陷：audio/client会清零_prepare_failures；孤儿后台problem未消费就被丢弃也不计失败；可选追问后置校验失败会连带废弃已验证理解；完成缓存仍建立可取消waiter，45秒后连已成功结果也丢弃。UI另有同快照结果依赖旧local phase的可复现缺陷，准备退出事件丢失后权威awaiting_reply仍显示整理。
- 实际文件：`app/services/{answer_endpoint,spoken_supplement,conversation_understanding,livekit_evidence_ingress,interview_agent}.py`、`app/core/{speech_diagnostics,prompt/validation}.py`；前端candidate/agent-experience、interviews/agent-event-runtime及其测试，重建dist。新增`tests/{test_confirmed_preparation_liveness,test_preparation_failure_budget,test_optional_followup_isolation,test_answer_preparation_projection,test_followup_rejection_integration,test_speech_budget_diagnostics}.py`；修改`tests/{test_answer_endpoint,test_prepared_turn_decision}.py`相关旧预期。同步架构/API/领域/评分/供应商/存储/CONTEXT/已知问题/进度/路线图。未改模型路由、凭据、DDL、历史答案或024 Prompt版本。
- 实现：成功同完整证据缓存同步返回，45秒只限制未完成推理；复用路径去掉可取消waiter和重复UI通知。相同服务端文字最多3次失败，provider元数据/音频/client不重置；后台孤儿失败只记一次，新文字按独立预算、回到旧文字保留失败数，显式retry才清理代际。耗尽保留采集并提示原因，不再自动调用模型。完整wire/引用/canonical/业务理解通过后，可选追问被拒绝只返回no_followup，不丢弃理解；真正理解无效仍严格拒绝。日志固定stage/reason/Schema路径及安全会话/轮次/request ID，未知标识回退“-”，无原文和异常正文。
- 状态恢复：当前turn/capture的answer_preparation在通知之前持久化；snapshot恢复真实准备或已退出状态，前端拒绝旧capture并保留独立evidence.ready门禁。暂停/关闭/换capture/提交后的状态清理失败不会阻止必要的媒体关闭。未新增不准确的准备计时，不以计时授权提交。
- 前后验证：liveness新8项修前4失败、修后全过；失败预算新回归修前4失败，修后包含100次audio/client活动仅3次实际prepare。预算相关旧测试原本期待新音频自动获得第4次模型调用，按新合同改为新服务端文字才可恢复，保留错误/超时/异常检查。模型侧旧用例将无效wire与合法理解后的可选追问拒绝混为一类，拆分后保留真正合同拒绝。前端首轮93项仅旧“缺状态仍保留preparing”预期失败，更新为权威状态并增加缺事件/错capture回归。
- 局部结果：端点及预算影响面176 passed/45.40秒（`/private/tmp/interviewer-024-failure-budget-integration-final.log`），-W error::RuntimeWarning通过；可选追问/准备/安全/Prompt/模型等254项通过；安全日志关联补充52项通过。状态新9项及spoken/declined整链28 passed/13.12秒。root真实managed拒绝泄露/已覆盖目标追问整链2 passed，均仅1次模型、PCM和答案唯一、自动下一题（`/private/tmp/interviewer-024-probe-integration.log`）；含预算安全日志3项通过（`/private/tmp/interviewer-024-root-focused.log`）。
- 完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short`为**1568 passed/6 skipped in120.09秒**（`/private/tmp/interviewer-024-full.log`），无023的abort警告；后加的投影清理/日志关联另按上述目标测试通过。`npm --prefix app/web test -- --run` **190 passed/14files**（`/private/tmp/interviewer-024-web.log`）；build成功，仅既有VRM大chunk提示（`/private/tmp/interviewer-024-build.log`）。最终compileall与git diff --check通过。
- 真实模型：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-024-real-decision.py`仅发两段新合成文本、不发历史候选原文/录音。技术正文+结束→answer/next，一次决策8.506秒；明确不会+结束→answer_declined/next，一次决策5.336秒，均无problem和重试（`/private/tmp/interviewer-024-real-decision.log`）。这不是原始事故录音的A/B，也不是所有真实长会话时延保证。
- 本地加载：两次临近检查LiveKit均room_count0；核对PID49796与当前仓库main.py后正常TERM，没有强制kill或打断活动房间。用`/private/tmp/interviewer-024-start.py`启动独立进程**70161**（日志`/private/tmp/interviewer-024-api.log`），保持本地媒体启用、实验流式TTS关闭及既有路由。healthz/readyz/home均200，ready=true；实际首页与资源**index-C_WF3WKO.js**均200且包含answer_preparation协议。023修复同次加载。
- 历史状态核对：iv_25335313fb1b4dea仍in_progress/version2117/answers3，原当前题asking，updated_at14:50:34Z；iv_3bdec492ad8f4247仍in_progress/version623/answers0，updated_at10:27:02Z。未补交、重置或生成历史评分。旧页面需刷新加载新bundle。
- 恢复与留痕：所有023未提交修改继续保留；首次只读过程中上一工具回合被系统中断，已重新启动两个只读分析agent，未产生仓库编辑或服务重启。此前023等待切换的记录保留；现已在无活动面试时一并加载，不用合成或健康检查冒充真实长会话体验。

## 2026-09-08 · EXPLICIT-FINISH-NO-ANSWER-023

- 状态：`verified（仓库、真实合成语义与本地加载）`；真实麦克风长会话及生产仍pending，不标closed。
- 目标：修复 iv_3bdec492ad8f4247 明确“没有补充/进入下一题”仍被播报没听清并反复询问的问题。
- 初始证据：10:23:58、10:24:34、10:25:28三次补充语义均finish/0.95；完整理解却因无技术回答返回clarification_request/clarify并重开同题capture。第一份73字原文含思考、表示不知道和要求下一题，后两份15/23字为重复明确结束；未发现技术正文被删或022跨题字幕过滤误触发。
- 计划：补齐明确不会/不再作答的独立语义，区分完成意图、理解置信度和知识覆盖。仅基于服务端完整发言记录真实无技术回答并推进，不伪造claims、不将空识别当否定，保留改口补充/暂停/重读/真实转写争议与提交fence。更新版本化Prompt/Schema、领域及相关接口/评分文档，增加自然语言、真实模型与整链回归。
- 实际代码：`app/core/prompt/contracts.py`、`app/domain/interview_agent.py`、`app/services/{conversation_understanding,interviews,interview_agent,evaluation}.py`、`app/providers/mock/provider.py`、`app/model_gateway/gateway.py`；新增`tests/{test_answer_declined_understanding,test_declined_answer_integration,test_declined_answer_evaluation}.py`，更新Prompt/准备/补充相关精确版本断言。同步架构、API、领域、评分、供应商、存储、CONTEXT、已知问题、进度和路线图。无前端源码、DDL、路由、凭据或历史数据变更。
- 实现：understanding v6/v7、decision v5/v6新增answer_declined/next，要求非空逐字证据、客观摘要、confidence≥0.75、空claims/covered/ambiguities/contradictions及完整missing。低语音置信度不能推进；无实质内容和低理解把握分开。明确未作答经过原有owner/STT/context/完整录音fence提交CandidateAnswer，不走管理员skip、不生成技术追问。进度统计不同正式题，追问不额外增加计数。评分重新核验持久化答案/话语/理解绑定，declined_answer.v1对全组未作答生成正常0分revision，全missing且无技术证据；混合组只保留实际技术响应，原始审计引用不删。读取持久化失败直接报错，不降为0分。
- 局部回归：语义/Prompt/准备两组110 passed、146 passed；真实managed owner/journal/PCM/主答→追问→下一题集成6 passed（4.93秒），含重复proposal、旧capture及过期owner阻止多交；评分/报告/公平性/SQLite重开及持久化失败影响面50 passed（`/private/tmp/interviewer-023-evaluation.log`）。
- 真实合成首轮：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-023-semantics.py`仅发全新合成文本，使用现有qwen3.7-plus，完整理解7/7正确；综合6/7，保留失败日志`/private/tmp/interviewer-023-semantics.log`。失败为原supplement_reply.v1将含字幕投诉的回复判supplement，虽后续理解正确澄清也不计综合通过；因此补v2投诉边界及57项回归，真实v2复测3/3语义通过（自然否定finish/3.507秒、双重否定continue/4.288秒、识别投诉unclear/12.641秒），日志`/private/tmp/interviewer-023-supplement-v2.log`；投诉一项仍超在线10秒期限，不计为线上时限通过。首轮补充分类2.742–12.665秒，其中两次超过在线10秒预算；完整理解6.602–16.284秒。本轮未擅自改变路由或扩大采集/推理预算，不能宣称秒级时延目标通过。
- 失败留痕：新集成fixture首轮遗漏ChatJSONResponse.usage导致失败，补齐后6项通过；几条只读路径查询不存在，sandbox内ps不可用，后续核验将使用只读提权。所有历史会话及录音保留，不补造历史提交。
- 首次全量：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short`为1529 passed/6 skipped/1 failed，119.07秒（`/private/tmp/interviewer-023-full.log`）。失败是术语回归仍断言旧v4/v5，更新为v6/v7后术语/合同/新整链/评分50 passed（`/private/tmp/interviewer-023-focused-final.log`）；原文/争议断言保留。另复现一条既有ValidatedSTTStream.abort未await警告。第二次完整回归1530 passed/6 skipped/同一警告，174.15秒（`/private/tmp/interviewer-023-final-full.log`）。
- 清理警告有独立可复现证据：ContinuousSTT.abort在detach供应商流后，wait_for子协程首次运行前被owner取消，裸abort协程未执行且重试已找不到流；最小取消调度中raw_provider_closed=False/capture_closed=True。改`app/services/continuous_stt.py`为capture持有唯一shield清理Task，所有close共享join；每个provider abort预先创建Task，逐流2秒预算且失败不跳过其他流。新增`tests/test_continuous_abort_cleanup.py`的立即取消、并发关闭、异常和有界等待回归；同调度现在关闭为True。识别及提交逻辑不改。
- 清理验证：`PYTHONTRACEMALLOC=5`定向5 passed无警告（`/private/tmp/interviewer-023-cleanup-after.log`）；连续STT/empty/非关闭/owner/recovery/reopen/probe/declined整链131 passed in18.59s，使用`-W error::RuntimeWarning`仍通过（`/private/tmp/interviewer-023-cleanup-integration.log`）。最终compileall和git diff --check通过。前端未改，不重复重建022 bundle。
- 本地加载：修复/验证完成，等待活动面试切换选择。两次只读LiveKit均为1房间/2个参与端（iv_6e3a3972ec8d4cc1仍in_progress）；已询问现在重启还是结束后加载，不能默认打断有效收音，也不将代码完成冒充在线生效。
- 后续加载完成：024验收结束时LiveKit连续room_count0，已正常切换至PID70161、index-C_WF3WKO.js；healthz/readyz/home均200。023全部修复随024生效，原会话仍version623/answers0/updated_at10:27:02Z，未补造提交。

## 2026-09-08 · FOLLOWUP-LATENCY-AND-CAPTURE-022

- 状态：`verified（仓库、真实合成ASR/语义、前端构建与本地加载）`；真实麦克风长会话及生产仍pending，不标closed。
- 目标：修复 iv_58da3095bac941c6 追问等待数分钟、上一题字幕混入追问以及追问识别卡住并要求重试的问题。
- 初始证据：09:18:05/30 两次 finish 意图均为0.95，09:18:07/38被client/audio活动撤销；09:20:51最终提交，09:20:52追问selected，09:20:57开始播放。追问阶段相同音频起点反复开流且没有新增句子，09:31:19 snapshot超时、09:31:29恢复缓冲耗尽。截图第一行与上一正式题已提交305字final一致；前端每次partial无条件带入上一条full final且切题不清字幕。历史私有音频不外发、不强制补交历史答案。
- 计划：分别复现字幕跨题、供应商正常结束但无文字被误判缺失final导致无限重放、确认后等待瓶颈；以严格的空识别完成合同区分成功无文字和异常缺失结果，保留所有PCM与真实新话语撤销，按题目隔离字幕；补协议/恢复/整链回归、同步文档并验证本地加载。
- 实际文件：`app/model_gateway/{schemas,streaming}.py`、`app/providers/dashscope/provider.py`、`app/services/{continuous_stt,streaming_stt,answer_endpoint}.py`、`app/web/src/features/candidate/{agent-experience,agent-experience.test}.js`及重建dist。新增`tests/{test_stt_empty_completion,test_continuous_empty_completion,test_confirmation_preparation_reuse,test_empty_confirmation_integration}.py`，修改`tests/{test_continuous_stt,test_confirmation_context,test_capture_recovery_integration,test_evidence_media_recovery,test_probe_stream_cleanup}.py`。同步API、架构、领域、供应商、CONTEXT、已知问题、进度、路线图及本日志；无Prompt、路由、凭据、历史答案或DDL修改。
- 实现：严格空完成事件经schema/网关/供应商正常结束共同校验，单流只能一个final或empty；同流及跨重连已观察的partial、preview和厂商后台文字均为sticky veto，不能被empty擦除。正常无字推进音频水位、保留全部PCM，不再把同一段无限重放；首段empty及legacy finish仍是非答案，不调用语义或batch补造。已确认全指纹相同的准备保留单份底层计算，声音只撤销waiter/提交；新文字/新final/继续补充/失败/关闭/旧上下文拒绝都会清理，45秒期限从首次开始计算。STT finish预算10秒；接收缓冲统一60秒，覆盖收口及有界恢复且不在错误时缩小。字幕selected和snapshot切题清当前recent/full，旧turn partial/final不能改变当前证据状态，同题重读/补充仍保留。
- 失败与修复留痕：新增供应商协议测试修复前25 failed/8 passed，修复后33 passed，扩展至37项及供应商影响面214 passed。连续采集首轮47 passed/3 failed，三个旧用例仍按30秒容量构造overflow，按60秒新合同调整输入量/缩小测试字节率并保留缺口不可提交断言。整链首轮78 passed/2 failed是新增empty fixture遗漏必填request_id，补齐后进入全量。cleanup旧测试将正常empty当失败，改为真正未决hypothesis继续验证原始错误优先级，未降低保护。
- 复查补缺：发现只有常规send记文字，preview/重放/finish失败前后台文字可能未记入跨流保护；增加单向has_observed_text veto及3条丢字回归，同时防止旧段已final成功后重开失败污染新段。最终相关影响面238 passed（`/private/tmp/interviewer-022-observed-text-final.log`）。
- 初版完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short`为1478 passed/6 skipped in117.07s（`/private/tmp/interviewer-022-full.log`）；sticky补缺后最终同命令完整回归 **1483 passed/6 skipped in110.35s**（`/private/tmp/interviewer-022-final-full.log`）。前端`npm --prefix app/web test -- --run`185 passed/14files（`/private/tmp/interviewer-022-web.log`），`npm --prefix app/web run build`通过，入口`index-Bnp-4_sK.js`（仅原有VRM大chunk提示）。compileall与git diff --check通过。
- 真实合成验证：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-022-real.py`仅使用019生成的合成PCM、现有真实ASR及supplement_reply语义模型，未外发历史候选录音、未写候选答案；模拟确认后client声音触发并继续发送静音，等待新的正常empty结果再复用准备。结果1次询问、1次finish语义、1次准备、1次提交、3次开流，882560字节录音逐字节一致，声音撤销到完成1.338秒（准备计算用受控gate，不能冒充真实LLM端到端时延）。结果`/private/tmp/interviewer-022-real.log`，exit0。
- 历史时延核对：本场最终组合LLM12.497秒、TTS4.317秒；09:18:38后同起点6116480的32余个空流直到09:20:36才再次识别9字。追问09:21:12重新开流start_byte=0。旧题305字final与红框散列一致，前端问题有直接数据/代码证据。未将修复后的合成1.338秒说成真实追问必定1秒出现。
- 服务加载：两次只读LiveKit均room_count0；核对PID39390的main.py和本仓库cwd后正常TERM，以`/private/tmp/interviewer-022-start.py`启动独立PID **49796**，日志`/private/tmp/interviewer-022-api.log`。healthz/readyz/home及实际`/web/bundles/index-Bnp-4_sK.js`均200，ready=true，资源含新turn过滤。重启前后原会话仍in_progress/version1727/answers2/updated_at09:33:10Z，未重写或补交历史追问。
- 未完成：旧页面需刷新以加载新版。当前故障会话已经断开，历史在内存中的未提交确认不伪造恢复；保留所有已持久录音/答案。真实麦克风噪声、自然长回答及生产端到端延迟仍待实机验收；模型与TTS实际耗时仍存在，不能承诺所有追问1秒出现。

## 2026-09-08 · SUPPLEMENT-CONFIRMATION-LOOP-021

- 状态：`verified（状态循环复现修复、仓库、真实合成ASR/语义链与本地加载）`；真实麦克风长会话及外部TTS历史故障仍pending，不标记closed。
- 目标：修复iv_2449bbc1de8940b3反复询问是否补充，明确回复后仍不能推进的问题。
- 初始证据：08:11:21至08:17:49同题7次supplement_check；ASR安全散列与用户截图原句匹配，08:14:20/59两次“我这边没有需要补充的了。”、08:15:59“没有的。”、08:16:01“进入下一题吧。”、08:16:33“没有了。”、08:16:41“我这边没有了。”均为厂商sentence_final。08:15:15有interview_turn_decision.v4成功，证明已进入结束确认后的处理，仍answers0。08:17:58最终因TTS_ASSET_DOWNLOAD_FAILED暂停，随后快照遇到INTERVIEW_NOT_IN_PROGRESS形成衍生报错；不把这一末尾故障误判成前面全部重复的原因。
- 代码发现：confirmed后的任意活动直接把确认状态重置到listening，5秒后再次冻结包括否定答复在内的新边界并重问；现有续说测试竟要求回到listening，未覆盖重复“没有”的交互闭环。快照final与已显示partial没有对齐，也会把final已覆盖的文字修订误当新话语。普通模块INFO日志默认未启用，确认决策/撤销原因未留下本场完整轨迹，需要接入已有专用安全诊断日志。
- 计划：先复现结束确认后新活动/重复明确答复造成二次询问；保留确认问答上下文，按新回复语义继续处理，既有已覆盖final不算新输入；无新语义不能循环重问，真实补充仍可撤销。补慢理解、连续PCM、重复否定、改口补充及失效所有权回归，完善安全状态诊断与相关文档。历史会话/录音/答案不改写，不外发历史录音。
- 实际文件：`app/services/{spoken_supplement,answer_endpoint,livekit_evidence_ingress,interview_agent}.py`、`app/core/speech_diagnostics.py`；新增`tests/test_confirmation_context.py`，更新`tests/{test_spoken_supplement,test_spoken_supplement_integration}.py`。同步API、领域、架构、CONTEXT、已知问题、进度、路线图及本日志。未改前端/构建产物、Prompt、模型路由、凭据、DDL和历史答案。
- 实现：confirmed输入变化后回awaiting_reply并保留已询问上下文，不返回listening重问；确认final推进回复边界，重复明确否定继续按语义处理，真实continue/supplement清除旧确认并进入补充。完整final覆盖旧partial，迟到相同前缀不撤销。声学中断没有新文字时，仅新完整final与已确认全部指纹一致才复用结束语义；None/未识别尾部/指纹变化仍不可提交。TTS生成被新输入撤销时保留问答阶段；准备退出投影同步awaiting_reply。没有删除真实续说、所有权或唯一录音门禁。
- 诊断：确认意图、准备被撤销的source/revision、提案拒绝stage/code及播放状态改用已配置的独立INFO日志，解决普通模块INFO未开启导致旧控制日志缺失。答复只记sha256和字数，测试验证额外模型响应/凭据不会输出；没有把本次新日志冒充旧会话已经存在的证据。
- 复现与中途结果：`tests/test_confirmation_context.py`在修复前2项均失败（重复否定后再次准备超时、旧partial导致revision从2变4），日志`/private/tmp/interviewer-021-repro.log`。修复后13 passed/2 failed，失败是旧续说用例断言应回listening；调整为保留awaiting_reply，同时增加真正补充仍撤销和进入继续阶段的完整行为断言。重点四文件39 passed（`/private/tmp/interviewer-021-target2.log`）；新增慢准备/持续PCM/重复否定真实owner、journal、录音整链及表达陈旧fence共30 passed（`/private/tmp/interviewer-021-integration.log`），原回答所有有声样本只出现一次、仅一个seal与答案。
- 完整验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1422 passed, 6 skipped in200.66s**，`/private/tmp/interviewer-021-full.log`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-021-pyc .venv/bin/python -m compileall -q app tests`、`git diff --check`通过。前端未改，沿用020已验证的181项与index-DF-bzNRB.js，无无效重建。
- 真实合成链：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-021-real.py` exit0，使用019已生成的合成PCM、真实STT与supplement_reply.v1，第一次答案准备刻意挂起，期间再输入一遍明确否定。结果 **1次check、2次finish、2次准备（第一次撤销）、1次commit、3次STT开流，1,035,520字节录音逐字节一致**，日志`/private/tmp/interviewer-021-real.log`。未外发历史候选音频或创建候选答案；不是实际扬声器/麦克风现场验收。
- 操作留痕：只读SQL首次遗漏JSON路径$前缀，报错后纠正；文档patch一次因CONTEXT标题不同被整体拒绝，无部分写入，纠正标题后完成；沙箱ps被操作系统拒绝后在获批环境核验。均无历史数据写入。
- 加载：只读LiveKit仅1个房间/2参与者，绑定本场paused会话；核验8000进程PID34845的main.py和cwd，再次查询paused后正常TERM，以独立进程 **39390** 启动修复版（`/private/tmp/interviewer-021-start.py`、`/private/tmp/interviewer-021-api.log`）。healthz/readyz/home均200且ready=true，前端沿用index-DF-bzNRB.js；原会话仍paused/answers0，未补交、恢复或重写录音。
- 未完成事项：该历史会话末尾的TTS_ASSET_DOWNLOAD_FAILED下游细因没有完整记录，不能用循环修复宣称外部下载问题消失；原暂停状态保留。真实麦克风长会话及生产仍需实际验收，新控制诊断可用于核对后续每次确认/撤销原因。

## 2026-09-08 · SPEECH-ACTIVITY-AND-AUDIBLE-CONFIRMATION-020

- 状态：`verified（仓库回归、真实合成识别与本地加载）`；历史未听见的具体输出原因、真实麦克风及生产仍pending，不标记closed。
- 目标：排查 iv_acbc69fa54e24915 低音量仍触发说话、补充询问只见文字的反馈，修正静音判断、实际转写通知与播报状态。
- 初始证据：服务端VAD最低RMS 0.001，端点配置0.006在共享VAD路径未生效，浏览器普通起音0.018；新增转写通知误放open事件循环，实际receive_audio未通知端点。当前会话已有1个答案；追问补充询问06:58:39Z创建performance，06:58:45Z收到结束回执。只读核对私有WAV为6.48秒/RMS−23.7dBFS，不是空白音频，但旧日志缺少级联播放开始/受阻事实，不能据此断言用户听到了。候选PCM仅本地校验/能量统计，未外发。
- 计划：有持续时长的语音特征+音量门槛，保留低声新转写撤销能力；在实际音频结果路径连接文字事件；无有效补充回复时可再次语音澄清；区分音频通道、声音活动、字幕更新及播报状态，补播放受阻恢复与回归，同步相关文档并核对本地加载。
- 范围：保留全部前序修改、历史录音/答案，不伪造历史恢复，不把客户端文字或播放回执当答案；无数据库迁移。播放证据不能证明系统/标签页扬声器未静音。
- 实际文件：`.env.example`、`app/adapters/speech_activity.py`、`app/services/{livekit_evidence_ingress,spoken_supplement,interview_agent}.py`、`app/domain/interview_agent.py`、`app/web/src/features/candidate/{audio-worklet-capture,agent-experience}.js`、`Page.jsx`、`app/web/src/features/interviews/agent-event-runtime.js`及dist。测试为`tests/{test_server_speech_activity,test_spoken_supplement,test_spoken_supplement_integration}.py`和候选人`{audio-worklet-capture,agent-experience,Page}.test.*`。同步API、架构、领域、CONTEXT、已知问题、进度、路线图和本日志；无模型Prompt、路由、凭据、历史回答或DDL改动。
- 实现：服务端VAD共用配置0.006 RMS下限，近120ms中至少100ms具有语音特征；浏览器普通起音需0.006/120ms，agent floor包含生成和缓冲期并使用0.05/160ms的打断门槛。实际receive_audio通知当前capture/turn新增ASR，重复字幕不重置静音，极轻语音仍完整送ASR。字幕更新灯2秒无新文字关闭；界面区分等待开口、声音活动、静音也传输的音频通道。询问后无final回复持续5秒会语音澄清一次，仍不把声音/沉默当肯定或否定。识别恢复保持awaiting_reply投影，不错误回到普通作答提示。
- 播报：新增严格、受当前控制器/题目/performance约束的playing/blocked/failed/buffering诊断回执，安全日志只有身份和枚举，不授权提交或改变floor；transient事件不持久重放。级联加载/卡顿8秒有明确提示，播放受阻保留原题和当前批准音频，可通过“播放这句话”恢复，旧句/暂停/离场清理计时器和音频。服务端补充播报30秒超时预算保留。不能绕过浏览器用户激活规则或断言操作系统/标签页未静音；未知历史声源/听感仍如实保留。
- 自动化：重点初稿27 passed/2 failed，分别是Mock只发半段partial却断言全文、transient诊断误从持久history取值；修正fixture并注入重复partial后29 passed。完整`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short`为 **1416 passed, 6 skipped in96.01s**（`/private/tmp/interviewer-020-full.log`）。最后补识别恢复阶段修正后，补充整链/恢复/端点影响面 **71 passed in36.42s**（`/private/tmp/interviewer-020-recovery.log`；命令误重复一个测试文件参数，保留实际结果，没有因此放宽断言）。
- 前端：首次177 passed/2 failed，旧测试要求当前音频错误暂停整场，以及80ms旧起音时长；按新独立恢复合同改为明确断言不暂停、不伪造结束、可恢复同一句，起音测试增加低背景和短峰值拒绝。最终`npm --prefix app/web test -- --run` **181 passed（14files）**，`/private/tmp/interviewer-020-web3.log`；`npm --prefix app/web run build`成功，仅现有VRM大chunk提示，`/private/tmp/interviewer-020-build.log`。覆盖NotAllowedError恢复、8秒卡顿、旧句隔离、播放结束唯一回执、重复字幕活跃期、按钮和状态展示。
- 真实合成识别：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-020-real.py` exit0，仅使用019已生成的合成PCM，不外发历史候选数据。原音量511个声学帧、3%音量0个声学帧，两次真实ASR均产生88字且识别Redis，录音逐字节一致，日志`/private/tmp/interviewer-020-real.log`。验证低于声学门槛仍可通过新文字参与判断；不是所有耳语/口音/噪声场景准确率证明。
- 操作失败留痕：若干只读旧路径不存在、一次多文件patch上下文拒绝且无部分写入；临时中文文档脚本直接执行报source UTF-8解码错误，逐字节严格UTF-8校验通过后改为显式read_text/compile执行，文档只插入一次。无历史数据删除、重放或改写。
- 本地加载：两次只读LiveKit均room_count0；核验PID29269为本仓库main.py后正常TERM，以独立进程 **34845**启动修复版（`/private/tmp/interviewer-020-start.py`，服务日志`/private/tmp/interviewer-020-api.log`）。healthz/readyz/home均200，ready=true；实际首页引用 **index-DF-bzNRB.js**，该资源200且包含新播放协议。原会话仍in_progress/version1824/answers1/updated_at07:07:43Z。生产仍未验收；已打开的旧页面需刷新加载新bundle。
- 最终校验：compileall与git diff --check通过。真实浏览器/扬声器听感及自然长会话仍待实机验收；本轮没有把播放事件、合成测试或服务健康冒充为用户已听见的证明。

## 2026-09-08 · ASR-REPLAY-AND-TRANSCRIPT-QUALITY-019

- 状态：`verified（背压缺陷、会话门禁、合成链与本地加载）`；红框话语的具体声源、真实麦克风识别质量及生产仍为 `data_pending/environment_pending`，不标记 closed。
- 目标：追查 iv_01b96d6b40314e2f 未说字幕、技术术语误识别与反复收音失败；修复补送背压并改善有证据约束的识别/判断。
- 初始证据：turn_7821affb4a0c48e5 尚无提交答案及评分。06:00:49Z snapshot 触发 PROVIDER_BACKPRESSURE_EXCEEDED，随后三次恢复在 resume 阶段重复相同错误，06:00:57Z retry_required；这些时间的 STT 开流均成功。ContinuousSTT 补送只 sleep(0)，不会等待 DashScope 实际网络发送容量；既有测试的 sender 下一拍全部清空，未覆盖真实慢 socket。现有配置没有词表/即时术语提示；字幕路径只投影 STT，全文数据库与仓库未发现 x-delayed-message 句子，尚不能断言具体声源或某段音频中是否存在该句。
- 计划：先用真实 Adapter + 慢 socket 复现，再增加独立于实时收帧的有界补送流控；术语仅提供词级偏置、保留原始转写与证据引用，不用标准答案生成候选话语。补长回答、恢复期间持续 PCM、语义纠正及专有名词验证，并核对本地实际加载。
- 范围：保留全部既有修改和事故录音；不强制提交、重评、补造旧答案。临时只读日志检查均过滤 ticket/token；若原始厂商片段未持久化，明确说明追溯限制。
- 实际代码：`app/model_gateway/{schemas,streaming,gateway}.py`、`app/providers/{dashscope,mock}/provider.py`、`app/domain/interview_agent.py`、`app/core/prompt/contracts.py`、`app/main.py`、`app/services/{continuous_stt,answer_endpoint,spoken_supplement,streaming_stt,conversation_understanding}.py`；新增 `app/services/recognition_vocabulary.py`、`app/core/speech_diagnostics.py`。同步系统架构、API、领域、检索评分、供应商、存储、CONTEXT、已知问题、进度、路线图及本日志。未改前端源码、模型路由、凭据、历史答案/评分或DDL。
- 实现：默认5秒发送队列不放大；快照排队尾音和恢复补送等待实际发送容量，取消前不重复记发送量，abort/网络错误唤醒等待者。单次发送无进展≤5秒，恢复单次总预算15秒、仍最多3次。继续补充后遇到无final片段可再次询问，不再只循环重开空流；旧边界不能用于提交，原始未识别音频保留/重放。所有接收PCM仍只写一份。
- 识别/语义：冻结题干、skills、key_points提取至多100个ASCII技术词；不读取standard_answer正文、候选转写或客户端词表。统一请求规则校验，当前Qwen流式模型映射即时热词权重2和zh/en；其他模型兼容。理解v4/v5、组合决策v3/v4、评分v2保留原文证据，分清可理解误拼、自我纠正、真实不同术语和未解决投诉。服务端对已校验ambiguities强制clarify/confidence≤0.64，明确暂停/重读不被覆盖。旧Prompt版本仍可读取。
- 字幕追溯：前端仅显示服务端STT投影；当前ASR请求未传标准答案/context，仓库及数据库未查到红框句子文本，旧逐句厂商响应未持久化，不能把源头说成已确认的回声、噪声或模型幻觉。新增仅含散列、字数、流/题目身份、句子相对时间和录音起始字节的安全诊断；不记录原文、凭据或PCM。76个私有录音片段逐一验证checksum和连续frame_sequence，合计6,669,440字节/208.42秒，未外发或重放历史音频。
- 复现与中途失败：新增真实Adapter慢socket用例最初fixture缺少HTTPS base_url，补齐后稳定复现 provider_backpressure_exceeded；修容量等待后24秒积压加恢复期间持续新帧一次重开成功，录音与厂商实际收取PCM逐字节一致。旧4秒上限断言更新为新的15秒契约。第一轮全量1403 passed/6 skipped/1 failed，失败是遗漏的Prompt版本断言；另出现一次既有abort协程未await警告，使用PYTHONTRACEMALLOC=5定向53项及后续完整回归均未复现，不能据此声称已单独定位其原始原因。补丁一次上下文不匹配原子拒绝，未产生部分修改；只读查询若干旧路径/字段不存在，已纠正。
- 真实模型验证：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-quality019-real.py` 使用全新合成TTS和现有真实路由。初稿误引memory store返回mock媒体，未外发；纠正为provider store。A/B均识别Redis、RabbitMQ、Celery，不能声称本样本证明了提升幅度；新路径24秒突发补送、口头自然否定、静音后收口通过，3次开流，1,583,360字节录音一致，无额外x-delayed-message句子，结果 `/private/tmp/interviewer-quality019-real.log`。
- 语义验证保留失败：原始规则在“那句话我没说过”案例产生高置信度followup，增加确定性歧义门禁及确认不认可字幕的明确语义；后一稿又对可理解的redios误拼过度澄清，补清唯一上下文含义边界。最终 `SEMANTICS_ONLY=1 PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-quality019-real.py` **exit0**，误拼、明确纠正、Redis/RADIUS不同术语均正常理解，否认未说内容为clarification_request/clarify/confidence0.5。日志 `/private/tmp/interviewer-quality019-semantics3.log`。这是四个合成样本，不能视为所有真实口音/噪声情况保证。
- 自动化验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1406 passed, 6 skipped in109.89s**，`/private/tmp/interviewer-quality019-full2.log`；新增最后一项诊断隐私用例后，`tests/test_recognition_vocabulary.py tests/test_stt_replay_flow_control.py` **13 passed**，含容量等待取消/abort、不泄露正文及统一词表合同。补充、决策、非收口整链和背压53项通过（含tracemalloc），早期重点76项通过。`git diff --check`、`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-quality019-pyc .venv/bin/python -m compileall -q app tests`通过。前端未改，不重复构建原bundle。
- 测试文件：新增 `tests/test_stt_replay_flow_control.py`、`tests/test_recognition_vocabulary.py`；修改 `tests/{test_capture_endpoint_recovery,test_spoken_supplement,test_prompt_governance,test_supplement_contracts,test_spoken_supplement_integration,test_prepared_turn_decision,test_interview_agent_contracts,test_stable_preview_preparation}.py`。
- 实际加载：两次只读LiveKit均room_count0，核对8000所属本项目PID16195后正常TERM；经 `/private/tmp/interviewer-quality019-start.py` 以独立进程 **29269** 启动，服务日志 `/private/tmp/interviewer-quality019-api.log`。healthz=ok、readyz=ready/true、首页200，现有bundle **index-Dtd5Burt.js**。原事故会话保持in_progress/version618/answers0、retry_required，updated_at=06:19:14Z，未重置或伪造已成功。
- 未完成/恢复：具体红框声源缺少当时逐句响应，无法完整追溯；历史中断状态不会因升级伪装成已修复的答案。真实麦克风ASR准确率、未说字幕率及噪声/口音长会话验收仍pending。新代码已加载但不承诺外部ASR绝不误识别；再次发生时可凭散列和音频水位关联来源。录音完整性、所有权及不可恢复故障门禁继续保留。

## 2026-09-07 · CONFIRMED-ANSWER-FINALIZATION-018

- 状态：`verified（仓库、真实合成音频链与本地加载）`；真实麦克风及生产仍为 `data_pending/environment_pending`，兼容路径保留，未closed。
- 目标：修复候选人明确说“没有”后仍提示转写无效、重复询问及不推进的问题；验证真实收音而非仅意图句子。
- 初始证据：新会话 `iv_bd12fa2e99b34f05` 的 `supplement_reply.v1` 于11:25:56Z成功，随后确认后的 `interview_turn_decision.v2` 两次返回，证明已进入确认完成后的理解。仍无答案；11:26:32/44/58Z继续重开STT，之后重复询问。日志另有一次 `wire_schema_invalid` 后修正调用。只读本地录音统计发现无浏览器speech.started期间也有孤立20ms能量尖峰；现有端点每一高能帧都会撤销确认，而收口用更低0.001 RMS将无转写音频一直判作未解决后缀。需分别复现，不把所有波形尖峰断言为某种噪声来源。
- 计划：复现确认final后重新开空识别流与收口阻塞，保存并复用仍有效的权威确认快照；改善服务端语音活动判断与未识别后缀的边界，续说/迟到文字仍必须撤销或重审。错误提示按真实阶段显示并记录安全原因；补持续PCM、瞬态非语音、轻声续说、推理期间续说及唯一录音/答案回归。
- 范围：不外发历史候选数据、不补造或强制提交旧答案，保留所有前序修改。先完成复现与验收；服务加载前核对活动会话。
- 实际文件、验证与失败恢复：待补。只读搜索中若干旧路径不存在及shell通配符无匹配，已缩小查询，无数据修改；曾读取服务日志含既有短期ticket字段，后续检查只投影安全字段，不复述凭据。
- 实际实现文件：新增 `app/adapters/speech_activity.py`、`tests/test_server_speech_activity.py`；修改 `pyproject.toml`、`app/services/{answer_endpoint,continuous_stt,streaming_stt,spoken_supplement,livekit_evidence_ingress,interview_agent}.py`。测试修改 `tests/{test_spoken_supplement,test_spoken_supplement_integration,test_automatic_turn_integration,test_nonclosing_continuous_stt}.py`；前端修改 `app/web/src/features/candidate/{Page.jsx,Page.test.jsx}` 并重建dist。同步CONTEXT、架构、接口、领域、检索评分、供应商、存储、已知问题、进度、路线图及本日志，无Prompt/Schema/模型路由/凭据/DDL变更。
- 最终设计：回复final使用resume=false保持识别收口，意图和答案准备使用同一个已验证final，不再先重开空识别再收口；输入/所有权/状态与完整STT/上下文指纹仍复核。继续/澄清或失败恢复采集，暂停/关闭不重开。正常已确认final的主动准备窗口最多60秒PCM，覆盖10+45秒推理预算；故障/未确认仍30秒。所有接受PCM仍只录一次，真正续说或服务端新增字幕撤销原确认；partial仅用于撤销，不能提交。
- 声学边界：固定安装 `webrtcvad-wheels==2.0.14`，本地20ms/mode3；近5帧至少3帧语音特征+RMS≥0.001才构成持续语音。单帧起音先为待定，下一帧解析前保护提交；待定状态按字节水位排除已被final覆盖的音频。未知/非法格式、非零DC或检测异常按有声保守处理，不静默降为静音。已有新ASR假设但无final仍阻塞，不能只凭VAD舍弃。纯静音PCM在途不再让稳定预览失效；有声或待定在途仍阻止提案。
- 真实输入排查范围：只在本地读取并校验本场私有录音片段checksum、统计20ms能量与语音活动数，未播放/外发/重放候选数据。缺失过程内状态不能完全追溯每次撤销；可证的是旧代码会按单帧尖峰撤销，实际波形存在这种能量尖峰且同期无浏览器speech.started，不能仅靠波形统计断言声源。
- 真实合成链验证：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-confirmed018-real-audio.py` **exit 0**。两段合成中文经现有TTS、真实流式ASR、生产补充语义入口识别为finish，之后非零底噪/一次短尖峰持续输入仍成功收口；仅2次识别开流，54字server final，**428160字节录音逐字节一致**。相同合成音频原音量、10%及3%音量均有语音检测，不能据此声称所有耳语/噪声场景都准确。输出 `/private/tmp/interviewer-confirmed018-real-audio.log`，合成PCM仅存 `/private/tmp/interviewer-confirmed018-synthetic-{0,1}.pcm`，未新增历史候选答案。
- 中途失败与对应修复：临时目录及本项目venv安装固定VAD wheel成功，仅有既有pip升级提示，未升级其他依赖。新测试最初在Python3.9事件循环外创建Event失败，移入async场景；录音断言在journal结算前读取、旧helper只允许纯零底噪两项fixture不足均补齐，增加非零PCM后缀完整顺序和样本唯一断言，未放宽证据门禁。前端旧“收音仍在继续”文字断言调整为仍可开口/无需重复确认。第一次全量1388 passed/6 skipped/1 failed，复现已覆盖半帧仍阻塞，修字节水位并通过断线补送专测；第二次1390 passed/6 skipped/1 failed，复现静音send在途让预览失稳并多调用理解，增加有声/静音在途阻塞的确定性用例后修复，不靠重复运行掩盖。
- 阶段验证：结束意图后真实续说/新增ASR撤销与声学单元20 passed；连续PCM/尖峰/唯一录音、原非收口及自动轮次30 passed；半帧修复后断线补送/声学/整链15 passed；最终静音在途修复后的连续STT、非收口整链、补充整链、owner恢复、声学影响面30 passed。前端最终 **176 passed（14 files）**，`npm run build`成功，提供 **index-Dtd5Burt.js**；仅原有VRM大chunk提示。compileall/diff通过。完整最终回归及服务加载结果待补。
- 本地服务初查：11:46:07Z无8000监听，原API已不在运行，不能推断其退出原因；当前会话仍in_progress/version689/answers0/updated_at=11:27:08Z。只读LiveKit核验 **room_count=0**。准备使用独立会话进程启动修复版，避免将开发服务生命期绑定到一次工具调用；启动结果及健康检查待补。
- 最终验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1392 passed, 6 skipped in 104.34s**，完整输出 `/private/tmp/interviewer-confirmed018-verified-full.log`。在 `app/web` 执行 `npm test -- --run` **176 passed（14 files）**，输出 `/private/tmp/interviewer-confirmed018-final-web.log`；`npm run build`成功，输出 `/private/tmp/interviewer-confirmed018-final-build.log`。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-confirmed018-pyc .venv/bin/python -m compileall -q app tests`、`git diff --check`通过。没有通过放松唯一答案、最终转写、所有权或录音断言消除失败。
- 实际加载：通过 `/private/tmp/interviewer-confirmed018-start.py`，使用项目venv及既有 `INTERVIEWER_LOCAL_MEDIA=true`、`INTERVIEWER_STREAMING_TTS_ENABLED=false` 启动独立session进程 **16195**，日志 `/private/tmp/interviewer-confirmed018-api.log`。`/healthz=200/ok`、`/readyz=200/ready=true`、首页200且实际提供 **index-Dtd5Burt.js**。未停止其他进程或在线房间。
- 状态及未完成：启动后原 `iv_bd12fa2e99b34f05` 仍in_progress/version689/answers0/updated_at=11:27:08Z，未强制提交、恢复、重新评分或外发历史数据。刷新页面重新连接即可加载修复版；当前owner进程内已丢失的确认事实不伪造恢复，原私有证据保留。真实麦克风轻声、回声、自然噪声、长回答与端到端等待时间仍需候选会话复验；合成链通过和development readiness不等于生产验收。固定VAD依赖须随项目安装，非语音分类并非绝对准确，新增ASR与音频完整性守卫继续存在。

## 2026-09-07 · SUPPLEMENT-SEMANTIC-VERIFICATION-017

- 状态：`verified（现有语义实现与真实模型合成用例）`。
- 目标：按用户补充要求，核对补充答复按整句语义理解，不限定“有/继续补充”等固定说法；补测同义表达、直接补充、否定完成、转折、引用与含糊答复。
- 初始核对：生产入口 `ConversationUnderstandingService.classify_supplement_reply` 将完整服务端答复交给现有LLM路由；`supplement_reply.v1` 已明确整句、语境、否定/引用和不确定性，无关键词匹配分支。上轮回复中的“有/继续补充”是举例，并非可接受口令集合。
- 范围：本轮先验证现有行为，只发送合成句子，不重放候选数据、不修改会话、不重载正在使用的服务；若结果符合要求，不无谓改动已有实现。
- 实际修改：仅本操作日志；临时合成探针位于 `/private/tmp/interviewer-semantic017.py`，未修改生产代码、Prompt、Schema、配置或候选会话，未重载服务。
- 验证命令：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/interviewer-semantic017.py`，使用现有Qwen路由和生产分类入口、并发上限2、每次10秒，**12/12通过，exit 0**。随后 `git diff --check`通过。正常模型调用审计为本轮唯一运行数据写入，未产生答案。
- 语义结果：“我再讲一点”“还有个情况没提到”“稍等，我想想还有什么遗漏”“我没有说完，刚刚只是停了一下”均为continue；直接补充连接排空、“本来想说没有了，不过还有个问题…”、业务示例中引用“结束”均为supplement；“差不多就这些”“我能想到的都讲了，我们往下吧”“不用再补了，刚才我把该说的都说了”均为finish；“嗯”为unclear；“先暂停一下，我接个电话”为pause。置信度0.90–0.98；肯定词不是必需条件，出现“没有/结束”也不会直接触发跳题。
- 结果与边界：现有实现满足本次要求，无需增加口令词表或修改已运行代码；这些是指定合成样例的实际模型结果，不代表所有自然语言或噪声场景都能正确识别。016真实麦克风及生产待验事项保持不变，本轮无失败操作或待恢复副作用。

## 2026-09-07 · SPOKEN-SUPPLEMENT-CONFIRMATION-016

- 状态：`verified（仓库、真实模型合成合同与本地加载）`；真实麦克风体验及生产分别保持 `data_pending/environment_pending`，兼容路径仍保留，未标记closed。
- 目标：定位本次字幕正常而自动轮转报错的原因，修复理解请求兼容性；正式回答静音5秒后由面试官询问是否补充，根据服务端语音意图继续收听或结束本题，不再要求手动提交。
- 初始证据：会话 `iv_2c8b3c6b909d4d0c` 的正式STT已成功开流，答案数0；10:05:33Z起理解请求连续失败为 `provider_bad_request`，不是STT识别失败。现有端点失败最多3次后阻塞同输入版本，且没有补充确认状态；进一步核验供应商具体拒绝原因。
- 计划修改：集中版本化确认Prompt和严格结果合同；保留同题累计录音及权威final，补充确认绑定当前owner/turn/capture，续说撤销旧决定；确认前不评分，未知或未答不默认跳题；修复安全错误提示和状态恢复，补供应商/端点/真实服务链/前端回归并同步文档。
- 范围：保留开始前全部未提交改动。仅使用合成输入验证厂商合同，不重放历史候选人音频/文本、不补造答案、不发邀请。服务重载前核查活动采集并明确记录影响。无既定生产验收承诺。
- 最终诊断：识别成功与理解成功是两条独立链路；历史理解调用被厂商以400拒绝，原始错误正文未保留，无法追溯具体拒绝参数。真实路由的短/长、关键点、修正上下文等合成原生Schema探针均成功，不将兼容风险伪称为已复现的唯一历史根因。另确认原错误投影掩盖故障阶段，重试耗尽留下准备状态；动态确认表达还可能错误复用本题预生成音频，均已修复。
- 实际后端文件：新增 `app/services/spoken_supplement.py`；修改 `app/services/{answer_endpoint,livekit_evidence_ingress,interview_evidence,interviews,conversation_understanding,interview_agent}.py`、`app/domain/interview_agent.py`、`app/core/prompt/contracts.py`、`app/providers/{openai_compatible,dashscope,mock}/provider.py`、`app/model_gateway/gateway.py`。新增 `tests/{test_spoken_supplement,test_spoken_supplement_integration,test_supplement_contracts}.py`；修改自动轮次集成、表达音频、Agent合同、模型调用记录及Prompt治理测试。保留开始前全部未提交改动，无DDL、凭据或路由配置变更。
- 实际前端与文档：修改 `app/web/src/features/candidate/{Page.jsx,Page.test.jsx,agent-experience.js,agent-experience.test.js}`、`app/web/src/features/interviews/{agent-event-runtime.js,agent-event-runtime.test.js}`，重建 `app/web/dist/`。同步CONTEXT、架构、接口、领域、检索评分、Provider、存储、已知问题、进度、路线图及本日志。
- 实现边界：正式回答静音5秒时先固定服务端final边界，再询问补充；新服务端语音独立识别肯定/补充/否定/暂停/不明确。明确完成才走完整答案理解与原有提交/追问规则；肯定继续同题同capture，含糊、低置信度或无答复不默认跳题，无回复15秒仅提醒一次。新声音撤销推理；owner/turn/capture、最终证据及唯一答案门禁保留，PCM只录一次。播问期间隔离回声，确认打断仅补送200ms前滚音频一次，播放ACK缺失30秒撤销当前输出并恢复答复收听。候选快照只暴露白名单确认状态，题干不被确认话术替换。
- 供应商与Prompt：`supplement_reply.v1`、`supplement_confirmation.v1`、确认后的 `interview_turn_understanding.v3`/`interview_turn_decision.v2` 集中治理并经严格合同校验；确认上下文不把控制答复作为专业能力证据。DashScope实时理解的原生Schema遭HTTP400时仅追加一次JSON Object兼容请求，仍执行原完整Schema校验；其他HTTP错误不触发该降级。调用记录仅新增批准版本、HTTP状态和安全拒绝分类，不保存原始错误正文或敏感Prompt。
- 自动化验收：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1376 passed, 6 skipped in 131.20s**（完整输出 `/private/tmp/interviewer-confirmation016-pytest.log`）；后续影响面回归 **285 passed in 141.44s**（`/private/tmp/interviewer-confirmation016-final-tests.log`）。最后播放时钟与批准守卫细化后，`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_spoken_supplement.py tests/test_spoken_supplement_integration.py tests/test_supplement_contracts.py tests/test_expression_stale_fence.py` **39 passed**。真实服务链合成覆盖无需按钮的否定提交、肯定后补充再否定、回声/打断及播放超时，完整录音与唯一答案均断言。
- 前端及静态验收：在 `app/web` 执行 `npm test -- --run` **176 passed（14 files）**；`npm run build` 成功，保留原有VRM大块提示。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-confirmation016-pyc .venv/bin/python -m compileall -q app tests`、`git diff --check`通过。人工代码复核确认 `awaiting_supplement` 仍接受浏览器VAD及候选语音，按钮仅为可选兜底。
- 真实模型合成验收：通过现有Qwen路由运行 `/private/tmp/interviewer-confirmation016-real-contract.py`，肯定→continue/0.98、否定→finish/0.98、“嗯，好的”→unclear/0.90；含补充控制语的完整合成回答经v3理解正常进入next。未创建候选答案，仅产生正常模型调用审计；未重放历史候选人数据。原生Schema诊断脚本 `/private/tmp/interviewer-confirmation016-probe.py` 的合成样例全部成功，历史400具体参数仍不可还原。
- 中途失败与恢复：初始只读命令 `python` 不存在，改用 `python3`/venv；真实探针首次受sandbox DNS限制失败，经授权网络执行成功。集成fixture起初使用Mock正式音频被正确拒绝，改为显式合成有效WAV并补齐content_hash；新合同断言原预期“schema”与实际“unexpected fields”不符，改为断言结构化错误码。一次混合回归的旧非收口时序用例多出理解调用，随后完整回归及影响面回归通过，未因此放宽生产门禁；前端旧EOT提示断言改为5秒确认合同后通过。所有失败保留，无删除历史、补造答案或破坏性副作用。
- 本机加载：只读核实会话 `iv_2c8b3c6b909d4d0c` 已由当前使用流程暂停，LiveKit唯一在线房间/1参与者属于该暂停会话，无其他活动作答房间。核实旧PID96567为本仓库后正常TERM，以 `INTERVIEWER_LOCAL_MEDIA=true INTERVIEWER_STREAMING_TTS_ENABLED=false PYTHONDONTWRITEBYTECODE=1 .venv/bin/python main.py` 启动API **8004**（执行会话89551），输出 `/private/tmp/interviewer-confirmation016-api.log`。healthz=200/ok、readyz=200/ready=true、首页200并加载 **index-0HGT-bC1.js**。这是development加载证据，不是生产验收。
- 未完成与恢复说明：重载后原会话保持paused/version3305/answers0，未自动恢复、补交或重放；本轮未发邀请、未更改模型凭据、未启用实验流式TTS。候选人刷新并恢复面试后可使用新流程；真实中文长回答、轻声/噪声、麦克风回声和实际TTS首音/端到端延迟仍需实机会话验收。保留本日志及已有数据；遇到新厂商拒绝可按安全HTTP分类与Prompt版本追踪，不能承诺历史400不会再次发生。

## 2026-09-07 · NONCLOSING-STT-SNAPSHOT-015

- 状态：`verified（仓库与本地服务加载）`；真实麦克风/中文轮次及生产仍为 `data_pending/environment_pending`，未标记closed。
- 目标与关联问题：优化正式智能轮次正常提案先 finish/reopen、确认再 finish 的切流路径，减少非故障重连与握手延迟；014 的有界故障恢复仍作兜底。历史事故底层错误已丢失，不将此设计风险追认为全部事故的唯一触发原因。
- 计划修改：新增供应商无关、只读且非 final 的稳定转写预览；DashScope 句末事件/Mock 显式 fixture 接入；统一校验与连续采集累积、准备决策使用预览，真正提交前一次 final 和完整指纹复核。迟到尾句、续说、无稳定内容不提前提交；不支持预览的 Provider 保留明确兼容路径。补合同/并发/开关流计数/录音唯一回归，同步领域与接口文档。
- 安全范围：不改历史会话、录音、答案、路由或凭据，不发邀请，不外发历史候选数据，不启用实验流式 TTS，不改变试音自动结束、评分或最终权威证据门禁。本轮测试仅合成输入；服务重载前检查在用会话。技能 codebase-design/domain-modeling 用于分开只读预览与最终证据边界。
- 实际源码：`app/model_gateway/{schemas,streaming}.py`、`app/providers/{dashscope,mock}/provider.py`、`app/services/{continuous_stt,answer_endpoint,streaming_stt,interview_evidence,interviews,conversation_understanding}.py`、`app/core/interview_agent_metrics.py`。新增 `tests/{test_stt_stable_preview_gateway,test_stt_stable_preview_providers,test_stable_preview_preparation,test_nonclosing_answer_endpoint,test_nonclosing_continuous_stt,test_nonclosing_capture_integration}.py`，更新 `tests/{test_automatic_turn_integration,test_interview_agent_acceptance}.py`。同步CONTEXT、架构/接口/领域/Provider/存储/检索评分/已知问题/进度/路线图与本日志；未改前端源码/包、Prompt、DDL、模型路由或凭据。
- 实现结果：非final预览在网关集中强校验，DashScope按句ID/保守时间键去重与保留尾部，有界64个未确认句/100000字，不能修订已稳定前缀。预览不消耗实时sequence、不发finish、不等网络；普通理解入口不放宽，预览内部utterance保持非final/nonauthoritative且不落库。正常提案在原连接准备，continue_listening同内容只理解一次，100ms轮询不反复读取完整会话；实际准备时封存complete=false的录音前缀。提交前只做一次final，尾句/时标/置信度/Provider变化重新准备，续说与owner失效撤销。显式提前结束在无可用预览时仍可一次final兜底；没有稳定句的自动提案继续听。未支持能力的Adapter保留旧兼容路径，试音不变。
- 审查补修：合成复现预览异常未记recovery_error导致recover不重建却发成功，现记录失败并验证真实重开/补送；复现final尾句修订后理解暂时失败、空流重开隐藏已确认前缀导致永远不重试，现保留前缀作为可撤销准备输入，同时未确认有声后缀阻止旧前缀提交。补跨句迟到/重复尾句保留，新增开流/finish/预览准备/final修订纯数值指标；所有录音仍只写一次。
- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1352 passed, 6 skipped in 105.42s**；`cd app/web && npm test -- --run` **173 passed（14 files）**。真实服务链合成5项覆盖连续3次思考停顿前两次不finish/reopen/写utterance、第三次1次finish/1个答案且WAV声段各只保存一次；无新VAD的迟到稳定句、尾部修订重新理解、续说取消、owner失效都通过。Gateway58项、Provider37项、准备35项、Endpoint13项、Continuous6项均在完整回归中。测试辅助commit回调清理后另跑Continuous+指标验收 **13 passed**；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-nonclosing015-pyc .venv/bin/python -m compileall -q app tests`、`git diff --check`通过。前端未改变，无需重建bundle。
- 中途失败留痕：Provider新fake漏base_url、轮询节奏调整后的测试等待计数不匹配均补齐后通过；旧自动续说fixture按每次重开分配文本，不适用于新固定Mock预览，改为默认明确测试无预览Adapter兼容链，新增真实不断流fixture按同流稳定句段推进，不弱化生产门禁。首轮完整回归 **1349 passed/6 skipped/1 failed**，仅新增4个内部计数未同步旧精确白名单；更新合同后最终全量通过。首次文档patch标题不匹配未写入。独立LiveKit只读脚本未经过应用本地代理初始化，继承SOCKS导致缺socksio，随后明确回环直连成功；未改代理配置或安装依赖，不能追认为历史ASR故障。
- 本机加载：只读确认最新会话仍paused、旧房间有1候选连接及1EGRESS（非新作答），不删除房间或停止录音；PID89405 cwd核实为本仓库后正常TERM，启动最终API **96567**（执行会话38585），保持 `INTERVIEWER_LOCAL_MEDIA=true`、`INTERVIEWER_STREAMING_TTS_ENABLED=false`。`2026-09-07T09:46:43Z` healthz=200/ok、readyz=200/ready=true、首页200且仍提供 **index-_IXKL-hH.js**。这些仅是development基础设施与加载证据，不是模型/生产验收。
- 未完成与数据保护：旧事故仍paused/version349/answers0/updated_at=2026-09-07T08:08:59Z，未恢复、未补交答案；无邀请、无真实候选数据外发或重放。真实中文长回答/轻声/自然停顿/网络故障与端到端p50/p95需新测试会话验收，不承诺永不失败或1–2秒。有时标/ID缺口的Provider投影保守等待稳定内容，显式结束可兜底；无预览Adapter兼容路径未删除。012实验TTS/可靠播放时钟与生产未完成事项仍保留。

## 2026-09-07 · CONTINUOUS-CAPTURE-RECOVERY-014

- 状态：`verified（仓库与本地服务加载）`；真实麦克风/中文轮次质量及生产保持 `data_pending/environment_pending`，未标记closed。
- 目标：修复正式自动面试将可重试识别/快照异常立即暂停并 abort、导致恢复分支永远无法执行的问题；保留音频证据、自动有界恢复、清晰恢复/重答状态及原始安全故障分类，不要求企业人员守候。
- 证据：`iv_4d9730f843ec4ca7` 于08:01:18Z正式开流，26/28/30秒重开均成功；08:01:30Z以CONTINUOUS_CAPTURE_FAILED暂停，原错误被替换为ApiError，持久音频前缀10.24秒但未形成答案。只读内存复现可重试provider_timeout→pause→abort，resume次数0。另复现缺final的6秒音频立即重放使sender未获调度即背压，以及finish先closed后取消导致transport未释放；不能追认这两项为本次已证原始异常。
- 计划修改：统一安全错误分类与自动恢复状态；持续采集/有限缓冲、未确认段有序补送、有限重连与取消/owner/capture保护；快照失败先恢复不abort，耗尽后明确保留证据并引导同题重答，禁止以断线批补偿接口自动提交残缺答案。修复Provider正常finish后的有界清理与重放调度；补前端作用域/重连快照状态和真实原因持久化。
- 安全范围：不改历史会话/录音/答案，不恢复用户旧paused会话，不发邀请，不外发历史候选数据，不打开012实验流式TTS，不放宽鉴权/证据/评分门禁。新增测试使用内存/合成音频与故障注入；本地重载前检查监听者/房间，真实服务验证只用明确合成输入。
- 实际源码：新增 `app/services/capture_recovery.py`，更新本工作区已有 `app/services/{answer_endpoint,continuous_stt,streaming_stt,interview_evidence,livekit_evidence_ingress,evidence_media,interview_agent,interviews}.py`、`app/providers/dashscope/provider.py`。前端更新 `app/web/src/features/candidate/{Page.jsx,agent-experience.js}`、`app/web/src/features/interviews/agent-event-runtime.js` 及各自测试；重建 `app/web/dist/`。同步CONTEXT、架构/接口/领域/Provider/数据库/已知问题/进度/路线图和本日志；无DDL、Prompt、评分、模型配置或凭据修改。技能codebase-design/domain-modeling促成安全分类/重建/候选重答的集中边界和明确领域术语，未扩大Provider业务接口。
- 自动恢复：白名单含真实Adapter的stream_open_failed/server_error/timeout等，最多3次/单次4秒并短退避；配置/鉴权类不盲目重建。PCM接收时记录一次，保留未确认段、补送主动让出调度，新积压30秒，超限为sticky缺口，耗尽seal complete=false再关闭本代并提示候选同题重答，绝不走断线批量补交。重试使用服务端16k mono PCM、原causation/new capture ACK；事务验证目标interview的owner/current turn/capture/revision，旧片段保留且不混合，失败open不重复advance。
- 并发/资源收口：Provider finish/abort共享独立有界回收，取消仍关transport；主动pause同步撤销本地输入和endpoint，reopen前后检查会话/题目/答案/takeover/owner，失效新连接及recorder/writer回收。critical恢复状态失败撤销收音并记录安全错误，纯UI投影最多1秒且不杀健康恢复；旧owner自撤销，不能以过期身份暂停继任者。提交后纯UI投影故障不阻止批准表达任务调度；followup/clarification业务处理不套1秒UI期限。前端重连恢复状态、旧scope隔离、失败重试ACK解锁与严重暂停优先级都有回归。
- 测试文件：新增 `tests/{test_capture_endpoint_recovery,test_capture_failure_classification,test_capture_recovery_integration,test_capture_reopen_fence,test_candidate_capture_retry_fence,test_committed_capture_projection}.py`；扩展 `tests/{test_continuous_stt,test_probe_stream_cleanup,test_livekit_evidence_supervisor,test_streaming_expression_lifecycle}.py`。新测试仅合成音频、fake Provider/transport与Memory/SQLite，不使用真实候选资料。
- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30 --tb=short` **1198 passed, 6 skipped in 152.17s**；`cd app/web && npm test -- --run` **173 passed（14 files）**；`npm run build` 成功，仅既有VRM大chunk告警。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-recovery014-pyc .venv/bin/python -m compileall -q app tests` 和 `git diff --check` 通过。专项含真实全链9项、重试事务62项、重开fence27项、错误分类78项、端点25项及提交后投影9项，均包含在全量中。
- 中途失败与恢复：同题重试首次使用STT默认WebM被EVIDENCE_MEDIA_FORMAT_NOT_RECOVERABLE拒绝，修为服务端PCM后通过；跨会话有效owner能重置目标题的矩阵在Memory/SQLite均复现，增加ownership_id目标绑定后通过；失败retry的snapshot+problem不能解锁前端pending，精确cause/scope ACK后通过。首轮完整后端1104 passed/6 skipped/3 failed：两项旧测试要求过期owner暂停会话，更新为自撤销但不能写继任者；一项暂停测试替身补齐pause_capture后通过。另新增critical timeout测试等待窗口短于真实5秒期限，调整测试窗口；中间postcommit UI期限误覆盖批准TTS，排除有业务副作用分支并用1.1秒追问/澄清回归。后续1120全量通过，再补实际adapter错误码矩阵后1198通过。只读路径定位/文档上下文匹配失败未产生写副作用；没有删除失败日志或业务证据。
- 本机加载验证：首次检查8000无监听，启动API88900；LiveKit只读doctor确认node-ip=192.168.0.108正确。最终重载前发现1房间/1参与者，经房间归属与只读数据库确认是原iv_4d9730f843ec4ca7的paused残留，不是新作答；没有删除房间或重启LiveKit。正常TERM本次88900并启动最终API **89405**（执行会话14600），`INTERVIEWER_LOCAL_MEDIA=true`、`INTERVIEWER_STREAMING_TTS_ENABLED=false`。`/healthz=ok`、`/readyz.ready=true`，首页实际提供 **index-_IXKL-hH.js**。这些是development基础设施与代码加载证据，不等同于模型质量/生产验收。
- 未完成与数据保护：原事故仍paused/version349/answers0/updated_at=2026-09-07T08:08:59Z，未恢复或补造答案；未发邀请、未重放/外发历史音频或转写。真实麦克风长回答/轻声续说/停顿/网络抖动需用新测试会话验收，不能宣称永不失败或端到端1–2秒达标。持久存储/证据完整性/所有权失效仍安全停止，不以自动恢复掩盖错误；012实验播放时钟/时延金标等未完成目标仍保留。

## 2026-09-07 · ROUTE-READINESS-REFRESH-013

- 状态：`verified（仓库与本地服务）`；生产及真实业务质量验收仍为 `environment_pending`，保留旧证据兼容窗口，未标记 closed。
- 目标：修复邀请被过期模型路由健康记录阻断且不能自动恢复的问题；明确健康、未测、过期、检测中和失败状态，统一自动刷新/手动探测的限时、并发去重与配置变更防护，补齐管理页面用途与诊断。
- 证据：预约 `appointment_37636cb3813d498f` 已于 `2026-09-07T07:08:10Z` 创建为 scheduled；正式/试音 STT、理解、追问、表达 TTS 路由最后 healthy 为 `2026-09-05T05:01:45Z`–`49Z`，有效期均 86400 秒，已过期而统一返回 configured_route_unhealthy。不是实际五项 Provider 同时失败；当前仅手动 route test 更新该事实，模型测试不刷新 route。
- 计划修改：新增统一 Route Readiness module，邀请前按需并发刷新；过期/未测与失败分开，失败冷却、总时限、租户/路由去重和旧配置探测结果 fencing；仍在邀请事务内复核全部准入门禁。补显式只刷新预约 readiness 的命令用于安全诊断，不签发邀请；GET 保持只读。补齐模型服务页面缺失用途、状态和操作中反馈，同步接口/领域/Provider/存储/统一术语/已知问题/进度/路线图。
- 安全范围：所有验证先使用合成测试；必要的真实连通性重测只发已有集中 probe Prompt 或空音频握手，不发送候选人资料/历史回答，不执行 invite、不修改预约状态、不放宽 TTL/禁用 gate、不打开实验 TTS。保留现有工作区与 012 的未完成验收。
- 实际修改文件：新增 `app/domain/model_route_readiness.py`、`app/services/model_route_readiness.py`，修改 `app/services/{model_admin,appointments}.py`、`app/domain/appointment_admission.py`、`app/core/readiness.py`、`app/api/routers/plans.py`；探针取消/资源回收修改 `app/model_gateway/gateway.py`、`app/providers/dashscope/provider.py`。前端修改 `app/web/src/features/{models,interviews,candidate}/Page.jsx` 并重建 `app/web/dist/`。同步 CONTEXT 及架构/接口/领域/Provider/存储/已知问题/开发进度/路线图/本日志；无 DDL，不调整业务 Prompt、评分、路由目标或凭据。
- 测试文件：新增 `tests/{test_model_route_readiness,test_appointment_readiness_refresh,test_probe_stream_cleanup}.py`，更新 `tests/test_livekit_tenant_avatar_security.py`；新增前端 `models/Page.test.jsx`、`interviews/Page.test.jsx`、`candidate/InvitationPage.test.jsx`。覆盖独立 SQLite 连接租约去重、CAS 配置 fencing、取消回收、失败分类、租户/RBAC、公网前置准入与最终事务、幂等邀请/start、前端局部超时/防重复与安全诊断。
- 初轮实现与验证：新增统一路由状态/持久探针租约与配置指纹、async 邀请/候选人开场刷新；保留 final admission 事务。审查复现并修复 can_start 忽略时间窗/设备过期、探测期间计划归档仍邀请、并发 start 绕过 consumed 幂等的问题。前端补齐用途、服务端状态投影、检测/邀请/开场局部超时与同步防重复，并保存表单内已创建预约/已签发结果，展示刷新失败不重复创建。初轮完整后端 `941 passed, 6 skipped`，前端 `158 passed`；后端另补独立 SQLite 连接租约/CAS 回归，最终全量待补。
- 本机首次验收与真实失败：确认 LiveKit `rooms=0/participants=0` 后正常 TERM 当前仓库 API `68713`（先前 PID51910 已退出），重启为 `74079`，构建 `index-Wp-L5SBs.js`。仅对指定预约调用新增 `/readiness/refresh`，2.93 秒返回 HTTP200，理解/追问/TTS healthy；两条 STT 网关日志分别 `stream_opened` 319/219ms，但探针 finally 的 0.5 秒清理期限误将关闭等待记为 provider_timeout，故 can_invite 仍 false。没有伪造成功；正在修正 abort 有界回收及独立清理错误分类。预约仍 scheduled/version1，未写 invited_at/邀请 token。
- 收尾修复：STT abort 改为共享独立清理任务，重复调用等待同一结果；最多 0.5 秒优雅关闭，取消/超时先强制回收 transport，再回收等待任务，避免先置 closed 后取消导致后续跳过清理。探针有独立 2 秒清理预算（仍受单探针 15 秒总预算约束），回收未确认返回 `provider_probe_cleanup_failed`，不把握手成功冒充完整探针成功；正式 finish/尾音路径未改。连接/模型配置修改前，将受影响旧无指纹证据绑定到修改前配置；保留原 checked_at/TTL，防止旧健康记录在改凭据或改模型后错误复用，普通健康更新不使配置失效。
- 最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30` 为 **962 passed, 6 skipped in 60.87s**；`cd app/web && npm test -- --run` 为 **158 passed（14 files）**；`npm run build` 成功，仅既有 VRM 大 chunk 告警。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-route013-pyc .venv/bin/python -m compileall -q app tests` 与 `git diff --check` 通过。定向路由健康45项、预约刷新32项、流回收27项均包含在全量结果中。
- 最终本机验收：再次确认 `rooms=0/participants=0`，正常 TERM `74079` 后启动最终 API **76743**（执行会话70743），保持 `INTERVIEWER_STREAMING_TTS_ENABLED=false`。`2026-09-07T07:47:56Z` 对同一预约执行只刷新命令，**HTTP200/0.92秒，五条必需路由全部 healthy，local_ready/can_invite=true**；两条 STT 新证据到次日同一时刻到期，未放宽 TTL。再次刷新 **0.10秒**，这五条路由的 invocation 数 **85→85**，健康结果复用且没有重复外部调用。预约仍 **scheduled/version1/invited_at=null**，未签发邀请。`/healthz=ok`、`/readyz.ready=true`，首页提供最终 `index-CU2XVjAr.js`；development 的 infrastructure ready 不等于 production ready，预约 production_ready 仍 false。
- 中途失败与恢复：最初新增预约测试夹具缺三个记录的 organization_id，导致22项失败；仅补齐合成夹具后通过，没有业务数据副作用。首次真实探针的两条 STT 关闭误判保留为历史健康/调用事实，修复后经真实重测覆盖为新证据，不删除失败记录。少数只读定位命令使用不存在路径/空 glob 返回错误，后用 `rg --files`/准确路径定位；没有文件副作用。
- 技能影响与未完成事项：codebase-design/domain-modeling 促成统一路由健康边界及清晰的证据/配置/准入术语，不在各入口复制探针策略。该工作项解决邀请过期证据阻塞，不宣称完成012的真实中文轮次、长回答、网络抖动和可靠流式播放时钟验收；不恢复旧 paused 会话、不外发历史候选内容。生产凭据/资源、跨实例生产部署及业务质量仍需独立验收；旧证据兼容窗口未删除，不标记 closed。

## 2026-09-06 · INTERRUPTIBLE-AUTOMATIC-TURNS-012

- 状态：`in_progress`。
- 目标：替代 011 的强制手动完成，建立持续权威收音、可撤销轮次判断、非阻塞理解和输入版本保护；保留暖场自动完成与显式完成兜底，改善接话时延与阶段观测。
- 关联问题：011 事故中续说排在 11,468ms 理解调用之后；Evidence 在 final/理解前关闭，旧决策无法撤销。用户明确要求智能自动接话，拒绝固定静音提交和强制按钮。
- 计划：以独立 Continuous Capture/Turn Decision seam 分开识别句段与回答提交；本地 CPU 音频轮次 adapter 与隔离 Python worker、未知判断保持等待；权威转写快照只供可撤销准备，提交仍校验 final、媒体/owner/capture fence 与当前输入版本；后台推理不占控制命令执行器；候选端保持续说能力并明确自动/可选完成状态；补 TTS 时延及受管流适配能力、端到端计时与回归。
- 不变量：不外发历史候选人数据、不恢复旧 paused 会话、不伪造 final 或评分、不绕过私有媒体与表达授权；保留工作区已有变更。模型/流式播放不能通过真实环境验证时保持明确 pending，不声称已启用或达到 1–2 秒。
- 实际修改文件：新增 `app/adapters/audio_turn_detector.py`、`scripts/audio_turn_detector_worker.py`、`requirements-turn-detector.txt` 与隔离的 `.runtime/turn-detector`（git ignored）；`.env.example/.gitignore` 记录配置及运行时边界。新增 `app/services/{continuous_stt,answer_endpoint}.py`；修改 `app/services/{streaming_stt,interview_evidence,livekit_evidence_ingress,interviews,conversation_understanding,interview_agent,evidence_command_journal}.py`，分开持续采集、可撤销准备、锁内 final/context/owner 检查与短提交，正常理解＋追问合并一次，准备失败有界重试，后台表达及结束回执受过期/暂停/owner 守卫。`app/core/{interview_agent_metrics,access_log}.py` 增加阶段指标和 LiveKit 原生日志 JWT 脱敏；`app/core/prompt/contracts.py`、`app/providers/mock/provider.py`、`app/domain/interview_agent.py` 增加严格合并合同与新版本。前端 `app/web/src/features/candidate/{Page.jsx,Page.test.jsx,agent-experience.js,agent-experience.test.js}` 保留准备期收音、可选提前结束、scoped 状态/警告恢复，重建 `app/web/dist/`。同步 CONTEXT 与架构/接口/领域/存储/评分/Provider/已知问题/进度/路线图/本日志；无 DDL、评分标准或真实 Provider route 配置修改。
- 测试文件：新增 `tests/{test_audio_turn_detector,test_continuous_stt,test_answer_endpoint,test_automatic_turn_integration,test_prepared_turn_decision,test_prepared_decision_binding,test_expression_stale_fence,test_prepared_finish_lifecycle,test_interview_agent_stage_metrics}.py`；更新 `tests/{test_agent_expression_audio,test_interview_agent_acceptance,test_evidence_owner_recovery_integration,test_access_log}.py` 与上述前端测试。无按钮真实服务组合（Mock Provider）覆盖自动唯一提交、理解期间续说取消、前后两段 PCM 在最终 WAV 各出现恰好一次；只用合成输入，无历史候选人数据外发。
- 技能影响：codebase-design/domain-modeling 将识别句段、权威前缀、结束提议与答案提交分成明确边界，并立即维护领域语言；它们没有绕过媒体/授权/Prompt 合同，也没有将未接通的流式 TTS 标记完成。
- 已完成验证：首轮完整后端 `668 passed, 6 skipped`；随后核心/收尾完整回归 `680 passed, 6 skipped in 36.52s`。最终前端 `npm test -- --run` 为 `103 passed`，`npm run build` 成功（仅既有 VRM chunk 大小告警），生成 `index-BGSt01mk.js`。启用 `INTERVIEWER_TEST_TURN_DETECTOR_PYTHON` 的本机原生合成 smoke 为 `31 passed`；初次冷加载约 1.9 秒，文件缓存后另次启动 97ms、连续推理约 9.7–9.9ms，均非真实中文准确率或有效首音证明。最终全量与服务状态在下面补充。
- 中途问题与恢复：并发测试先复现并修复了旋转 orphan、缺 final 复用旧答案、尾帧丢弃、同文本置信度变化、异常概率/检测器崩溃、模型异常误暂停、context 检查过晚、旧 TTS 影响新轮、终态提前取消告别丢回执；一次取消测试挂起后用 Ctrl-C 停止（仅合成内存测试），修复 whole-task cancellation 被误认为续说撤销，并加入 shutdown 回归。组合 patch 上下文不符/重复目标被拒绝后按小 patch 重做；只读 ps/默认 TERM 权限被拒绝，无状态副作用，TERM 后经审核执行。依赖首次网络/CA 安装失败后使用系统 CA 重试，没有关闭 TLS 校验。运行验证额外发现 LiveKit 原生日志会包含拒绝的 JWT，已补日志脱敏测试，不在仓库日志复制任何令牌。
- 环境发现：两次 LiveKit readiness-probe 被拒绝为 `token not valid yet`；去敏比对得当时主机比容器快约 924.55 秒（15 分 24.5 秒），最新采样恢复到约 0.73 秒。最初 500 没有足够证据精确归因，不能全部声称是沙箱限制。未修改主机/VM 时钟、未重启 Docker/LiveKit、未放宽 nbf/鉴权。一次全量因此/或宿主时间跳变在旧 owner fixture 建立时提前失去 lease（`681 passed, 1 failed, 6 skipped`）；仅该内存恢复测试使用单调时间偏移的 DB clock 以保留时序确定性，仍推进/到期，生产时钟/lease 实现不改。
- 未完成事项：这是自动收音/轮次稳定性的第一阶段，不是八项优化全部完成。真实中文停顿/填充词/轻声续说/噪声、多轮长回答、并发共享 worker 与真实网络需验收和校准；真正 TTS PCM 首块发布尚缺统一 chunk/独立最小权限 publisher/私有分段归档/前端播放时钟，现有动态语音仍完整合成后播放，不承诺 1–2 秒端到端时延。Docker VM 对时稳定性需作为本机运行前提；若漂移复发，应处理宿主/VM 对时，不能扩令牌容差掩盖。真实批量修复路由和生产 acceptance 门禁不绕过。旧 paused 会话、历史录音/转写/答案未恢复、删除或补造。

- 第一阶段最终验证与运行：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30` 为 `682 passed, 6 skipped in 35.38s`；前端 `103 passed`、build 成功，compileall 与 diff check 通过。旧 API 21509 经审核正常 TERM；初次启动 38430 探针失败后正常停止；38588 经实际本机网络视图检查 ready，随后为加载日志脱敏正常重启为最终 PID `39990`（执行会话 29216）。最终 `/healthz=ok`、`/readyz.ready=true`，包括数据库、Redis、VRM、LiveKit/Egress 与权威收音；首页提供 `index-BGSt01mk.js`。未改业务会话或 Docker 时钟/服务。
- 用户继续授权：第一阶段验证后用户要求“继续”，工作项仍 `in_progress`，接着实现批准文本之后的真正流式 TTS/低时延播放，不把第一阶段局部完成当作整体八项优化完成。
- 第二阶段实现：新增 `app/model_gateway/tts_streaming.py`、`app/providers/dashscope/tts_streaming.py`、`app/adapters/livekit_audio_output.py`、`app/services/approved_speech_output.py`；修改 gateway、DashScope（manifest 0.6.0）、Mock、LiveKit token adapter、Agent domain/runtime、managed barge-in 和阶段 metrics。原 TTS route 增加可选严格 PCM transport，首 PCM 后禁止重试重播；已批准动态表达使用独立发布身份、200ms 队列/20ms帧、候选订阅 ready 握手，Provider final/本机排空/客户端排空分离，完整 PCM 私有归档；独立超时/失权清理与旧回调守卫。控制断线新增原批准事件引用恢复，清理旧 active，重新 ready 时仅当前题与批准仍匹配才用新轨重播。全量 backend 一轮为 `794 passed, 1 failed, 6 skipped`，确定性复现并修复了跨总 deadline 的完成仍归档；后续一轮 `814 passed, 1 failed, 6 skipped` 暴露新 gateway 主动取消 stalled read 后的异常传播，已在取 consumer.result 前重新检查关闭/fence。均为合成测试，无业务数据写入。
- 第二阶段前端：新增 `live-speech-playback.js` 及测试，接入 agent-experience/agent-event-runtime 的精确 live_audio 绑定、producer_finished、ready/drained 与候选快照 active_performance_id；未知轨道仅有界缓存，不自动出声。首音使用 playing，媒体时钟停滞/倒退/未知不能伪造排空，晚回调/暂停/换代撤销播放。此阶段前端 `131 passed`、build 成功；新增后端 `tests/{test_tts_streaming,test_livekit_audio_output,test_approved_speech_output,test_streaming_expression_lifecycle}.py` 持续验证。初始恢复审查发现已撤轨但 active 残留造成重连卡住，已修并补回归。最终完整命令/本地真实合成媒体 probe/服务状态待下面补充。
- 第二阶段边界：不调用真实外部模型、不重放历史候选数据、不将供应商 URL/PCM/凭据写入事件。Qwen 字符计费未纳入既有 token 单价，不伪造成本数；真实中文字词/思考停顿、模型性能、网络抖动后的浏览器媒体时钟、精确口型和端到端延迟仍独立验收，不能将新增离线合同通过标为生产已验收。
- 真实浏览器合成探针发现独立环境断链：IAB 与 Chrome 的 receive-only 测试房间均信令成功但 ICE 失败，服务端向浏览器广播 `192.168.0.104:7882/7881`，当前 `ipconfig getifaddr en0` 为 `192.168.0.108`，Docker 启动参数仍 `--node-ip 192.168.0.104`。这能解释此次合成媒体不通，不能追认为所有历史停顿事故的原因。探针两个临时房间已自动删除；只读 RoomService.ListRooms 确认为 `rooms=0/participants=0`，将通过既有 local-media up 流程更新地址并补 doctor 检查。新增 `scripts/streaming_audio_browser_probe.py` 与 `app/web/streaming-audio-probe.html` 仅允许本机 development，只有合成双音，无麦克风/摄像头/数据库/历史音频或真实外部模型调用。
- 第二阶段环境修复结果：确认零参与者后，执行既有 `bash scripts/local-media.sh up`，仅重建 `local-media-livekit-1`，广播地址更新为 `192.168.0.108`；Redis/Egress 保持运行。新增 `scripts/local-media-doctor.py`、`scripts/local-media.sh doctor` 与 `tests/test_local_media_doctor.py`，只读比较精确容器参数和本机地址，不自动重启。实际执行 doctor 为 `LOCAL_MEDIA_DOCTOR_OK`；错误地址、未运行、格式异常和超时均有隔离测试。初次测试夹具因带空格路径的 shebang 失败，改为 `/usr/bin/env python3` 后通过，仅影响临时夹具。
- 真实 Chrome 播放验收及发布门禁：修正地址后，24kHz/单声道/48000 样本（2 秒）的合成音轨正常播放；中间暂停供应 PCM 三秒后也恢复。但停供期间 `HTMLMediaElement.currentTime` 从约 1.032 增至 3.802 秒，最终为 5.122 秒，未触发 `waiting`，证明它不能作为批准 PCM 的实际消费时钟。接收统计也不能可靠区分发送端补静音，因此不能用它或固定等待伪造尾音排空。没有证据证明已发生听觉截尾，但尚不能保证无截尾/正确口型。**实验流式 TTS 默认关闭**（代码、`.env.example` 和运行进程均为 false）；门禁回归覆盖未设置/false/非法值与显式启用。自动轮次、持续收音、合并理解和既有完整音频播放仍正常启用。后续需建立源 PCM→实际播放位置的可靠映射或有界实际 PCM 消费通道，再通过真实网络抖动与首音验收后开放。
- 第二阶段最终验证：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -o faulthandler_timeout=30` 最终为 `858 passed, 6 skipped in 53.50s`；前端 `npm test -- --run` 为 11 文件 `131 passed`；`npm run build` 成功，生成 `index-3eD3DxQi.js`（仅既有 VRM 大 chunk 告警）。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-turn012-pyc .venv/bin/python -m compileall -q app scripts tests`、`bash -n scripts/local-media.sh` 与 `git diff --check` 通过。新合同/媒体发布/输出生命周期/门禁/诊断与重连回归全部为合成输入；没有真实模型准确率、生产 readiness 或端到端 1–2 秒达标结论。
- 第二阶段运行与清理：再次只读确认 `rooms=0/participants=0` 后，正常 TERM 旧 API `39990` 与本次探针 `47875`、Vite `47903`，关闭本次创建的 IAB/Chrome 两个临时标签页；探针房间均已删除，没有删除业务会话。API 以 `INTERVIEWER_LOCAL_MEDIA=true INTERVIEWER_STREAMING_TTS_ENABLED=false PYTHONDONTWRITEBYTECODE=1 .venv/bin/python main.py` 重启为 PID `51910`（执行会话 52384）；实际本机 `/healthz=ok`、`/readyz.ready=true`，首页提供新构建。基础就绪不代表所有预约模型路由健康；未执行邀请、真实 Provider 调用、历史重放或暂停会话恢复。
- 当前交付范围与剩余事项：自动轮次与本机 RTC 地址问题已具备代码、自动化和本机运行证据；流式输出实现仍属于默认关闭的实验阶段，故整体工作项保持 `in_progress`。真实中文停顿/轻声/长回答/噪声与并发 EOT 校准、可靠 PCM 播放时钟、精确口型、有效首音 p50/p95、真实 Provider 字符成本和生产环境验收仍待完成；不能将本轮局部完成表述为八项全部完成。历史证据保持原状。

## 2026-09-05 · TURN-COMPLETION-UNDERSTANDING-011

- 目标：修复候选人思考/续说被自动收口，以及有效长转写因理解合同脆弱而安全暂停；根治证据/能力点由模型自由重抄与规则缺乏去敏诊断的问题。
- 关联问题：`iv_f5c41141820c4b3c` 追问于 05:03:55Z 开流，05:06:03.620Z 同 capture stop，05:06:06.169Z 自动 seal；05:06:07.329Z 再次开口排队至 05:06:18.923Z。转写持久化后，11,468ms 的真实 LLM 调用通过 JSON Schema，但领域内容校验失败，以 `UNDERSTANDING_RESULT_REJECTED/source=schema_or_content` 暂停。历史未存具体拒绝分类，不能追认精确失败规则；本次无旧 capture 串流或 mock STT 证据。
- 状态：`verified（仓库与本地服务，真实模型合成合同）`；目标浏览器/真实麦克风、历史回答复验为 `environment_pending`，未关闭。
- 计划：将理解引用转换为服务端冻结原文片段/能力点 ID，统一合同校验后还原领域数据；有限合同纠正重试与去敏错误分类，保留无效语义不得形成答案的规则；明确正式回答完成与静音不是同一事实，修复处理中可继续说的虚假 UI 与严重错误被次生提示覆盖；补根因级回归并同步文档、重建和重启本地服务。结束方式已向用户询问，尚待选择。
- 不变量：不保存原始模型响应/敏感日志，不模糊匹配或臆造证据，不绕过 owner/capture fence，不自动恢复旧 paused 会话，不改变评分和录音隐私策略。
- 实际修改：`app/core/prompt/{contracts,understanding_references}.py` 增加 v2 冻结引用 wire 合同、完整规则、中心化纠正 Prompt、精确还原与 canonical schema；`app/services/conversation_understanding.py` 增加最多两次/每次 20 秒的合同生成、严格内容检查和固定去敏分类；`app/domain/interview_agent.py` 保留历史 v1 可读并新增内部 reason_code/attempts；`app/providers/mock/provider.py` 更新 v2 fixture。`app/providers/dashscope/provider.py` 仅对厂商 wire schema 副本剔除不支持的 array.uniqueItems，网关原始完整校验不变。`app/services/{livekit_evidence_ingress,interview_agent}.py` 将正式静音与完成分离，保留暖场自动端点和 scoped 显式 seal 的 owner 崩溃修复，发布 answer_processing 状态；`app/web/src/features/candidate/{agent-experience.js,Page.jsx}` 增加回答完毕、抑制处理中 VAD/重复提交/旧快照重开、发送失败回滚和严重 problem 优先级。测试为 `tests/{test_interview_agent_contracts,test_prompt_governance,test_dashscope_provider,test_livekit_evidence_supervisor,test_evidence_owner_recovery_integration}.py` 与 `app/web/src/features/candidate/agent-experience.test.js`；同步架构/接口/领域/评分流程/Provider/存储/已知问题/进度/路线图/本日志，重建 `app/web/dist/`。使用 codebase-design 技能将 Provider wire 引用与 canonical 领域数据的转换留在统一 Seam，业务调用 Interface 不扩大。
- 验证：完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q --tb=short` 最终 `525 passed, 5 skipped in 69.69s`（首轮 `524 passed, 5 skipped`）；前端 `npm test -- --run` 为 9 文件 `84 passed`，包含 10 秒静音不提交、显式完成/重复抑制、处理中旧快照不重开以及次生 problem 不遮盖暂停。Provider/owner 定向 `27 passed`。`npm run build` 成功（仅既有 VRM chunk >500 kB 告警），`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-completion011-pyc .venv/bin/python -m compileall -q app tests`、`git diff --check` 通过。回归保留长音频完整、跨实例 scope/fence、scoped seal 未知 receipt 恢复、唯一答案、恶意/未知引用、错误分区/claim、纠正失败仍安全暂停等合同。
- 真实合成验证：仅使用新编写、无个人信息的技术回答调用当前配置的 qwen3.7-plus，不读取/提交任何真实面试回答。前两次分别形成 `mil_8e636dc663144250`、`mil_e4ce19aecf6246a9`，HTTP 400/invalid_parameter_error，厂商去敏提示 array.uniqueItems 不受支持；修正 Adapter 后第三次 `mil_a2ec6acb5de94bf6` 成功，5,268ms、v2、accept、2 条原文证据，未使用纠正重试。数据库仅增加正常模型调用诊断/熔断事实，没有创建 synthetic InterviewSession/CandidateAnswer。该证明不能替代实际候选人长回答、多轮和麦克风验收。
- 安全边界与失败留痕：尝试对事故历史转写做真实模型复验时，执行安全审查拒绝敏感候选人文本再次向外部模型发送，命令没有执行、数据没有因此外发；未以其他工具绕过。随后改用独立构造的无个人信息合成测试。首次测试有 9 项因历史 prompt_version Literal 未接纳新版本而失败，已保留 v1 可读、新写 v2 后通过；新 ingress 用例误用不存在的方法，改经真实 fixture ingress 回调后通过。一个 20ms grace 的既有跨实例接管测试在混合回归中失败，隔离复跑及最终全量通过，未改生产租约/控制 grace。只读查询中一次歧义 JSON value 和不存在文件名被拒绝，无业务数据副作用。
- 本地发布：旧 API PID `15757` 以 TERM 正常停止，PID `21509` 使用 `INTERVIEWER_LOCAL_MEDIA=true .venv/bin/python main.py` 启动；首页已实际提供新 bundle `index-jehEcGBW.js`。`/healthz.status=ok`、`/readyz.ready=true`，数据库/Redis/VRM/LiveKit/Egress/权威收音入口检查通过（不代表生产或所有模型路线验收）。未改本地模型/路由、DDL、录音同意或评分规则，原事故仍为 paused/1 个已提交答案，当前追问录音与转写保留，不自动恢复或补造答案。
- 未完成事项：正式结束方式已询问用户，未收到选择时按明确告知的稳妥方式实现“停顿不提交、回答完毕确认”；暖场自动流程保留。用户需刷新新 bundle 并在新测试会话实测长停顿/续说/显式完成/换追问。若需将事故真实转写再发给既有百炼模型校验，须取得用户明确同意；不因此自动恢复旧会话或提交原回答。真实批量 STT 补偿路由仍未配置；生产、长时/网络故障与实际准确率验收未完成。

## 2026-09-05 · ENDPOINT-CAPTURE-SCOPE-010

- 目标：修复停顿/换追问时旧 VAD 计时器封存新流，以及真实音频批量兜底使用 mock 并把 None 当作转写造成安全暂停的问题。
- 关联问题：会话 `iv_618dd870f4a54270` 第一题已提交；04:07:48.119Z 无 turn 的 speech.stopped 先于追问 open，旧计时器 04:07:50.692Z 收口追问，录音仅 2.18 秒；mock-stt 将 None 转成字符串后传入理解，触发 `UNDERSTANDING_RESULT_REJECTED`。
- 状态：`verified（仓库与本地服务）`；真实浏览器/麦克风停顿复验为 `environment_pending`，未关闭。
- 计划修改：ready/VAD/自动 seal 绑定服务端 capture ID 和 turn；失效旧计时器与迟到排队命令；无有效转写保留录音、原子释放本次采集并继续当前题；真实音频不允许 mock STT 补偿，mock 仅允许明确非空字符串 fixture；补前后端、权威录音/状态与网关回归，同步协议、架构、领域、供应商、已知问题/进度/路线图并重建前端。
- 不变量：不把空/partial/客户端文本变成答案，不降低语义校验，不删除历史证据，不自动恢复安全暂停会话；fence、隐私、评分与受控追问原则不变。
- 实际修改文件：`app/services/livekit_evidence_ingress.py` 绑定 ready/capture/turn/endpoint 作用域并忽略失效端点；`app/services/evidence_command_journal.py` 增加控制字段白名单和长度/格式校验；`app/web/src/features/candidate/agent-experience.js` 与 `app/web/src/features/interviews/agent-event-runtime.js` 传递采集标识、重置旧 VAD 并支持无转写重开；`app/services/{interviews,evidence_media,streaming_stt,interview_agent}.py` 增加无转写的原子失败/归档/重收音与安全提示；`app/model_gateway/gateway.py`、`app/providers/mock/provider.py` 禁止真实音频 mock 假成功与 None 字符串化。回归为 `tests/{test_livekit_evidence_supervisor,test_evidence_media_recovery,test_model_invocation,test_evidence_owner_recovery_integration}.py` 与 `app/web/src/features/candidate/agent-experience.test.js`；同步 `docs/{architecture,api-design,domain-model,database-and-vector-storage,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md` 并重建 `app/web/dist/`。没有 DDL、Prompt、评分规则或本地 Provider/路由配置修改。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q --tb=short` 为 `514 passed, 5 skipped in 20.28s`；`cd app/web && npm test -- --run` 为 9 文件 `84 passed`；`npm run build` 成功，保留既有 VRM chunk 大于 500 kB 非阻断告警；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-endpoint010-pyc .venv/bin/python -m compileall -q app tests` 与 `git diff --check` 通过。回归覆盖换题/同题新采集/恢复说话后的旧 seal、无匹配 start 的 stop、Memory/SQLite 流式与 batch 无 final 后继续当前题及唯一答案、无假 utterance/理解调用、mock 显式 fixture 边界、暖场和 owner 接管。
- 本地服务：旧 API PID `2644` 以 TERM 正常停止；新 API PID `15757` 使用 `INTERVIEWER_LOCAL_MEDIA=true .venv/bin/python main.py` 启动并监听 `127.0.0.1:8000`。`/healthz.status=ok`、`/readyz.ready=true`，数据库/Redis/VRM/LiveKit/Egress/权威收音入口 ready；首页实际引用新 bundle `index-C6hUuIMG.js`。这些是本地基础依赖检查，不代表真实模型或生产验收通过。没有调用外部付费模型、创建邀请/面试、恢复历史暂停会话或改写历史录音、转写、答案。
- 中间失败与恢复：初轮定向回归暴露新增 scope 尚未通过 journal 白名单及旧测试无 scope 手动触发自动端点，已补白名单并把手动 finish 测试标为 explicit，自动端点另用完整 start/stop/scope 覆盖。新增竞态测试一度引用不存在的 controller，修正 fixture 后通过。全量一轮为 `505 passed, 5 skipped, 3 failed`，三项 owner 接管测试在慢环境中因 200ms 初始租约提前过期而未到预定崩溃点；改为测试专用 30s 初始租约，在完整 checkpoint 后以数据库时间显式注入到期（其余场景仍正常 release），定向 `4 passed`、最终全量通过。生产 lease/续租/fence 行为未改；中途停止的测试与上下文不匹配的 patch 未修改业务数据。
- 未完成事项或恢复说明：用户需强制刷新候选页加载新协议，并在管理端正常处理已暂停会话后，实际测试说话→停顿→续说与换追问。原会话仍保留已提交第一题和历史暂停；不把旧无效转写补造成答案。真实 `stt.batch/candidate_answer_repair` 路由仍未配置，网络断流的批量补偿需要独立配置和实测；无 final 的正常重收音不依赖 mock。真实 Provider 准确率、长回答、多轮/网络故障和生产验收仍 pending。

## 2026-09-04 · EVIDENCE-NONANSWER-RECAPTURE-009

- 目标：修复正式回答被语义理解判为需要澄清/重说后，同一题重新开流持续报 `EVIDENCE_MEDIA_ALREADY_COMPLETE` 的状态衔接错误。
- 关联问题：会话 `iv_4b8a18d54e7e4747` 正式开流成功并封存 1,025 帧、656,000 字节 PCM（20.5 秒）；05:19:16Z 的理解结果要求澄清，题目回到 asking，但 durable capture 仍 complete，随后 13 次重新开流被拒绝。不是此次麦克风或 STT 握手未成功。
- 状态：`verified（仓库与本地状态修复）`；真实浏览器/麦克风复验 `environment_pending`，未关闭。
- 计划修改：在权威非答案事务内将已封存采集归档为未采纳并推进 capture revision，让同题重新收音获得独立采集；保留完整/已采纳录音不可无条件重置的约束，补同 epoch 旧 writer 和跨 revision repair 防护，流式/批量统一传递 capture revision；增加澄清→重新收音→唯一答案、事务回滚和迟到写入回归，并同步架构、接口、领域、存储、已知问题、进度及路线图。
- 不变量：不降低语义理解阈值，不把 partial/客户端文本变成答案，不混入上一段音频，不删除历史音频/转写，不绕过 ownership/control fence；未获得明确非答案结果的完整采集仍只能恢复处理，不能任意重开。
- 实际修改文件：`app/services/evidence_media.py` 增加权威非答案事务内采集释放、revision 固定 writer 和 repair 双阶段校验；`app/services/interviews.py` 将非答案生命周期/采集归档原子化，并为转写状态和答案提交增加 capture 校验；`app/services/streaming_stt.py` 的流式、断线 batch、持久 repair 统一传递 `capture_revision`；`tests/test_evidence_media_recovery.py` 增加 Memory/SQLite 澄清后重新开流、唯一答案/不串录音、事务回滚、同 owner 旧 writer、等长度跨 revision repair、迟到语义结果和无拒绝事实不可释放回归；同步 `docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。没有前端/Provider/Prompt/评分/路由修改，也没有 DDL 迁移或历史答案补写。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_evidence_media_recovery.py` 为 `27 passed in 0.77s`；全量后端为 `495 passed, 5 skipped in 20.12s`（第一轮含 14 个新增 case 时为 `489 passed, 5 skipped`）；`cd app/web && npm test -- --run` 为 `83 passed`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-nonanswer009-pyc .venv/bin/python -m compileall -q app tests` 和 `git diff --check` 通过。本轮前端源码未变，无需重建 bundle。
- 本地状态修复：执行只针对上述 interview/turn/utterance/stream 的 `/private/tmp/interviewer-nonanswer-recapture-009.py`，先 dry-run 校验完整 checkpoint version=8/revision=1、7 segments/1,025 frames/656,000 bytes、同一 authoritative utterance/recording、明确澄清事件、其后无新转写/答案及 owner lease 过期。SQLite `.backup` 保存到 `/private/tmp/interviewer-before-nonanswer009.sqlite3`（权限 0600）；apply 使用正常 `claim_owner`/commit fence/release，保留 control generation，将 capture revision 推进为 2/open/零字节，并新增一条 `evidence.nonanswer_capture.released` 审计。未改会话/题目状态、历史录音/转写/13 条错误，不创建 CandidateAnswer；维护 owner 已 released。对照备份确认会话 JSON 与七个旧 segment 文档逐字一致。备份只用于人工诊断/按记录恢复，不能直接覆盖后续业务写入。
- 本地服务：检查时 `8000` 无监听；已以 `INTERVIEWER_LOCAL_MEDIA=true .venv/bin/python main.py` 启动 API PID `2644`，`/healthz.status=ok`、`/readyz.ready=true`，数据库、Redis、VRM、LiveKit/Egress 与权威收音入口全部 ready。未调用外部模型、未自动录制用户语音、未重新发邀请、未启动新面试或恢复 paused 会话。
- 中间失败与恢复：只读定位中若干行范围/文件名没有命中，SQLite 首次 JSON path 漏 `$` 被拒绝；两次文档组合 patch 的上下文验证失败，均未写入文件，按完整原文重试成功。没有由这些失败引入业务数据副作用。
- 未完成事项或恢复说明：用户需刷新当前候选页，等服务端识别 ready 后实际重说并核对完整 final/录音及后续题目；真实 Provider 准确率、多轮长回答、网络抖动与生产环境验收仍为 pending。未知 seal receipt 的崩溃边界仍遵守既有失败关闭策略，不能猜测/恢复错代录音。

## 2026-09-04 · FORMAL-EVIDENCE-DRAIN-SQLITE-008

- 目标：修复正式面试在长回答期间因 SQLite 实时命令事务反复全量回载文档、阻塞权威音频消费而触发 `LIVEKIT_INGRESS_SINK_BACKPRESSURE`，并保证封存命令在已接收音频全部通过 sink 后才结束本轮；同时清除试音字幕，避免正式回答与暖场 final 混在同一面板。
- 关联问题：会话 `iv_581d8635efa8413b` 的试音与正式回答使用同一 DashScope `qwen-audio-3.0-asr-flash-streaming` 配置，但正式 Evidence stream 在约 51 秒生命周期中只封存 125 个 20ms 帧、80,000 字节 PCM（2.5 秒），随后以 `LIVEKIT_INGRESS_SINK_BACKPRESSURE` 暂停且 `answers=0`。正式链路同期生成 13 组 VAD start/stop；SQLite 每个事务提交后执行全库 `_load_from_db()`，在当前万级 documents/audit 数据量下持续占用事件循环。试音确认后前端未清除暖场字幕，进一步造成“长段已识别、正式只剩几个字”的显示混淆。
- 状态：`verified（仓库与本地服务）`；目标浏览器/真实 Provider 新会话为 `environment_pending`，未关闭。
- 计划修改：把 SQLite 事务提交后的缓存同步收敛为事务实际改动文档的增量 refresh，保留 compare-and-swap、索引表、幂等和跨实例可见性合同；为 LiveKit ingress 增加有界 drain watermark/barrier，使 evidence finish/seal 不得越过已入队音频；减少重复 VAD 状态命令的同步持久化压力而不改变端点语义；正式阶段开始时清空暖场字幕；增加大数据量 SQLite + 长正式音频 + 密集 VAD 的回归，并同步架构、接口、领域、数据库、已知问题、进度、路线图和本日志。
- 不变量：不静默丢音频、不接受浏览器文本 final、不以扩大队列替代根因修复；正式 final、CandidateAnswer、Evidence ownership/control fence、checkpoint/batch repair、评分和人工接管语义不变；SQLite、Memory、PostgreSQL 继续通过同一持久化合同。
- 实际修改文件：`app/persistence/sqlite.py` 记录并提交后同步 transaction delta，移除每事务全库 reload；`app/adapters/livekit_audio_ingress.py` 增加 accepted/delivered 水位、排空等待与去敏背压故障；`app/services/livekit_evidence_ingress.py` 在 warm-up/formal seal 前执行 drain，正式失败暂停、暖场失败进入既有 retry seam，并去重重复 problem；`app/web/src/features/candidate/{audio-worklet-capture.js,agent-experience.js}` 将默认 speech stop 调为 800ms、确认试音/完成快照时清字幕且发送失败可回滚；回归为 `tests/{test_sqlite_store,test_livekit_audio_ingress,test_livekit_evidence_supervisor}.py` 与 `app/web/src/features/candidate/{audio-worklet-capture.test.js,agent-experience.test.js}`；同步 `docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md` 并重建 `app/web/dist/`。没有数据库迁移、API schema、Prompt、Provider 路由、评分或 CandidateAnswer 数据结构变化。
- 验证命令与结果：定向后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_sqlite_store.py tests/test_livekit_audio_ingress.py tests/test_livekit_evidence_supervisor.py` 为 `44 passed in 2.81s`，覆盖 2,000 条历史审计下单会话事务只解析两行、真实 ingress 排空水位、seal 顺序与排空失败零答案/暂停；定向前端为 `35 passed`。完整后端为 `475 passed, 5 skipped in 19.84s`，完整前端为 9 个文件 `83 passed`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_formal_drain_pyc .venv/bin/python -m compileall -q app tests`、`npm run build` 和 `git diff --check` 通过，build 仅有既存 VRM chunk 大于 500 kB 的非阻断告警。旧 API PID `83663` 正常停止，新 API PID `88367` 已以 `INTERVIEWER_LOCAL_MEDIA=true` 监听 `127.0.0.1:8000`；`/healthz=ok`、`/readyz.ready=true`，database、Redis、VRM、LiveKit/Egress 与权威音频 ingress 全部 ready。
- 中间失败与恢复：首次新增 seal barrier 测试在 Python 3.9 中于运行事件循环外创建 `asyncio.Event`，定向集为 `42 passed, 1 failed`；已把 Event 构造移入 async scenario，随后增加 drain 失败关闭断言并重跑定向/全量全部通过。没有业务数据、外部 Provider 调用、付费请求、历史会话恢复或证据改写副作用。
- 未完成事项或恢复说明：事故会话保持暂停，不改写历史 Evidence、不把 transient partial 或 Egress 旁路补造答案。必须使用全新邀请连续正式回答至少 60 秒，核对 ingress/Evidence 帧连续、seal 水位已排空、私有录音、唯一 final/CandidateAnswer 与多轮表现；取得目标浏览器/真实 Provider 证据前不得标记 `closed` 或 production accepted。

## 2026-09-03 · STT-FINAL-PARTIAL-TAIL-007

- 目标：修复实时 STT 已显示尾部 partial，但 Provider 结束时 final 只保留此前已确认分句、静默丢弃最后未 `sentence_end` 片段的问题，保证试音与正式回答的唯一权威 final 包含厂商在 `task-finished` 前交付的全部有效文本。
- 关联问题：新会话 `iv_8120d4147ef14c67` 的暖场流从 `2026-09-04T06:18:32Z` 正常打开，连续接收约 46 秒音频并于 `06:19:23Z` 正常完成，没有背压、断流、暂停或 owner 失效；候选人确认最终字幕缺少尾部。代码审计确认 DashScope adapter 的实时 partial 使用 `committed + partial_text`，但 finish 在已有 committed 时使用 `committed or partial_text`，因此最后一个未被厂商单独标记 `sentence_end` 的假设会在 final 投影时确定性消失，前端随后按 final 收口并隐藏 partial。
- 状态：`verified（仓库与本地服务）`；真实 Provider 尾句的新会话复验为 `environment_pending`，未关闭。
- 计划修改：在 DashScope Provider 内建立单一 final transcript 聚合 seam，把已确认分句与有效尾部 partial 无损合并并生成一致 segments；补“多条 committed + 尾部 partial + task-finished”合同测试，保留空 final 失败关闭与唯一 final 不变量；修正试音完整字幕面板的工具栏文案并增加前端断言；同步 Provider、已知问题、开发进度、路线图和本日志，重建 production bundle。
- 安全与边界：不接受浏览器文本 final，不改变权威 LiveKit 音频、Evidence fence、CandidateAnswer、评分或模型路由；只合并同一已验证 Provider stream 在结束前交付的文本，不猜补未识别内容；试音原音仍不持久化。
- 实际修改文件：`app/providers/dashscope/provider.py` 以单一 transcript projection 聚合 committed 与尾部 partial，`app/providers/dashscope/provider.json` 将实现版本推进为 `0.5.4`；`tests/test_dashscope_provider.py` 增加未封句尾部的唯一 final/segments 合同；`app/web/src/features/candidate/{Page.jsx,Page.test.jsx}` 修正展开态标题并加行为断言，随后重建 `app/web/dist/`；同步 `docs/{model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。没有修改 API、数据库、Prompt、Evidence/CandidateAnswer 或评分结构。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_dashscope_provider.py` 为 `22 passed`，候选试音 React 为 `2 passed`；完整后端为 `471 passed, 5 skipped in 20.95s`，完整前端为 9 个文件 `81 passed`；`npm run build` 成功，仅有既有 VRM chunk 大于 500 kB 的非阻断告警；compileall、DashScope manifest JSON 校验与最终 `git diff --check` 通过。旧 API PID `81022` 正常停止，修复后的 API 已以 PID `83663` 启动；`/healthz=ok`、`/readyz.ready=true`，数据库、Redis、VRM、LiveKit/Egress 与权威音频入口全部 ready。
- 中间失败与恢复：首次只读定位使用了已迁移前的 `app/providers/dashscope_stt.py` 路径并立即返回“文件不存在”，随后用 `rg --files` 定位到 `app/providers/dashscope/provider.py`；没有仓库或业务数据副作用。
- 未完成事项或恢复说明：事故会话的试音原音按隐私设计未持久化，不能事后猜补或改写其字幕。需用全新邀请朗读带明确尾句的长文本，确认展开区保留尾句；进入正式题后再核对私有录音、唯一 final 与 CandidateAnswer 一致。完成真实 Provider/浏览器证据前不得标记 `closed` 或 production accepted。

## 2026-09-03 · WARMUP-BACKPRESSURE-RECOVERY-006

- 目标：修复试音期间大部分语音已被服务端识别，但权威音频入口短暂背压先把整场面试暂停，随后迟到的暖场 seal 又继续完成并形成冲突状态的问题；在不丢帧、不接受浏览器文本 final、不放宽正式回答失败关闭的前提下，提高实时链路对短时调度抖动的容忍度。
- 关联问题：会话 `iv_68419ac0bb784ee5` 于 `2026-09-04T05:31:03Z` 打开 DashScope 暖场流并持续收到多段 speech；`05:31:22Z` 以 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED` / `PROVIDER_BACKPRESSURE_EXCEEDED` 暂停，而 `05:31:24Z` 创建的旧 `evidence.seal` 仍在约七秒后完成并把 calibration 推进到 `awaiting_confirmation`。浏览器直到 `05:33:39Z` 才断开，因此不是页面先断线；同期 Evidence owner 续租成功，说明直接触发点是两秒 Provider 音频积压预算及暖场失败/seal 竞态。
- 状态：`verified（仓库与本地服务）`；目标浏览器/真实 Provider 新会话复验为 `environment_pending`，未关闭。
- 计划修改：将暖场流失败统一收敛到可重试恢复 seam，原子失效旧暖场代次、取消端点并 abort 原流，设置 durable `calibration_retry_required`，但不暂停整个会话；旧代次 seal 即使已在等待 Provider final 也不得完成 calibration。正式回答的权威 Evidence 断流继续失败关闭。把 DashScope sender 和 LiveKit sink 的默认无损背压窗口从约两秒提高到五秒并保持有界、可配置，补暖场失败/seal 竞态、正式流暂停和短时突发回归；同步实时架构、接口、领域、Provider、已知问题、开发进度、路线图与本日志。
- 实际修改文件：暖场 epoch/retry/迟到 seal 防护及 iterator 恢复编排为 `app/services/livekit_evidence_ingress.py`；receive-only track 重启与五秒 sink 预算为 `app/adapters/{livekit_audio_ingress,livekit_media}.py`，环境模板为 `.env.example`；DashScope 五秒 sender 预算与可配置表单为 `app/providers/dashscope/{provider.py,provider.json}`；回归为 `tests/{test_livekit_evidence_supervisor,test_livekit_audio_ingress,test_dashscope_provider}.py`；同步 `docs/{architecture,api-design,domain-model,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。没有前端源码、数据库结构、Prompt、评分或正式答案逻辑变化。
- 实际实现：每个暖场 open 建立单调本地 epoch；`audio_stream_failed` 在异步恢复前即失效当前 epoch，单一锁保护的恢复 seam 取消端点、abort 临时流、清 partial、持久写入 `retrying + calibration_retry_required=true` 和一条含白名单根因的特权 problem，但不暂停 InterviewSession。seal 在 Provider finish 前后检查 ingress failure/epoch；失败前已开始且稍后成功的 final 也会被丢弃并写 rejected receipt，故不会出现暂停后又 `awaiting_confirmation`。显式 `warmup.retry` 先重启同一 microphone publication 的 lossless AudioStream，不能复用时重建 subscriber，成功后才清 gate。LiveKit sink 与 DashScope sender 的默认无损预算均为五秒、硬上限 30 秒且可配置，超过预算仍失败关闭，不静默丢帧；正式 Evidence 流继续保持暂停/checkpoint repair/人工接管语义。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_livekit_evidence_supervisor.py tests/test_livekit_audio_ingress.py tests/test_dashscope_provider.py` 为 `59 passed in 2.56s`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `470 passed, 5 skipped in 20.15s`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_warmup_backpressure_pyc .venv/bin/python -m compileall -q app tests`、Provider manifest `python3 -m json.tool` 与 `git diff --check` 通过。旧 API PID `73514` 以 TERM 正常停止，新 API PID `81022` 已用 `INTERVIEWER_LOCAL_MEDIA=true` 启动并监听 `127.0.0.1:8000`；`/healthz` 为 `ok`，`/readyz` 为 `ready=true`，database、Redis、VRM、LiveKit/Egress 与权威音频 ingress 全部 ready。
- 中间失败与恢复：首轮定向回归中，既有“暖场 final 成功但确认表达失败”场景停在 `awaiting_confirmation`；原因是初次重构只包住 chain finish、没有把 completion callback 保留在恢复边界。已立即修正为 completion 异常也走同一 epoch retry seam，复跑定向与全量回归均通过。受沙箱限制的本机 `ps/curl` 首次只读检查被拒绝；随后在授权的本机执行边界完成旧进程替换与探针。没有外部付费 Provider 调用、创建面试、恢复历史会话、重放业务任务或改写历史证据。
- 未完成事项或恢复说明：事故会话保持历史暂停，不自动恢复、不复用已暴露邀请、不把 partial 或录制旁路补写为答案。需用全新邀请在目标浏览器和真实 Provider 连续试音，至少覆盖一次短抖动/显式重试、旧 seal 无 final/确认、重试后进入正式题和 60 秒正式回答；取得环境证据前不得标记 production accepted 或 `closed`。

## 2026-09-03 · FORMAL-STT-PARTIAL-BACKPRESSURE-005

- 目标：修复正式回答期间服务端已持续收到 LiveKit 音频、但同步投影临时字幕阻塞权威收音消费，导致只显示短 partial 后 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED` 暂停的问题；保持正式 final、CandidateAnswer、Evidence fence、评分、追问与人工接管规则不变。
- 关联问题：会话 `iv_31e593c1ef5b4098` 的暖场成功；正式 `evidence.open` 于 `2026-09-04T04:07:15.626Z` 提交、`04:07:35.597Z` 完成，DashScope 打开耗时 `18,919ms`。权威流只封存 `1,143` 个 20ms 帧（`22.86s`）并约在 `04:07:58Z` 停止，浏览器 VAD 仍持续到 `04:08:16Z`，`04:08:17Z` 服务端以 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED` 暂停；同期 Evidence owner 续租 `69/69` 成功，排除上次租约问题。完整 LiveKit Egress 含 `184.58s` 音频且故障后仍有连续语音，排除候选人未说话、麦克风停止或当时离房。页面短句只是 transient partial，turn 仍为 `asking`，`answers=0`，没有形成 final/CandidateAnswer。
- 状态：`verified（仓库与本地服务）`；目标浏览器新会话与真实 Provider 长回答复验为 `environment_pending`，未关闭。
- 计划修改：对 `avatar.performance.stopped` 执行原子 compare-and-clear，缺失或不匹配当前 performance ID 的迟到事件完整忽略，不再提前打开正式 STT；将高频 `transcript.partial` 从权威音频消费调用栈中解耦，按 turn 只保留最新待投影值并在 final/停止边界消除迟到 partial；按照 Provider 推荐的约 100ms 音频包合并 20ms PCM，降低 WebSocket 小包压力，并让 `use_environment_proxy=false` 真正禁用 WebSocket 环境代理；让 LiveKit track task 保留并输出无 PII 的实际故障分类，避免所有底层错误退化为 `RuntimeError`。补旧播放停止事件、媒体背压、partial 合并/顺序、Provider 代理/打包/flush/错误和严格失败关闭回归，同步实时架构、接口、领域、已知问题、进度、路线图与本日志，重建前端仅在确有前端源码变化时执行。
- 实际修改文件：播放 compare-and-clear 为 `app/services/interview_agent.py`；partial 投影与断流诊断为 `app/services/livekit_evidence_ingress.py`；LiveKit 适配器故障分类为 `app/adapters/livekit_audio_ingress.py`；特权 problem 存储白名单字段为 `app/services/interviews.py`；DashScope 代理/分包为 `app/providers/dashscope/{provider.py,provider.json}`。回归为 `tests/{test_interview_agent_contracts,test_livekit_audio_ingress,test_livekit_evidence_supervisor,test_dashscope_provider}.py`；同步 `docs/{architecture,api-design,domain-model,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。当前工作项没有前端源码变化或数据库迁移。
- 实际实现：服务端只在非空 stop ID 与当前 performance 事务内匹配并成功清除后才切候选人 floor，旧/缺失/重复 stop 无状态效果。partial 使用独立 projector，每 `(kind, turn_id)` 最多一个 in-flight 和一个 latest pending，总 pending key 上限 16；final/reset/stop 关门及 delivery barrier 消除迟到 partial，非 partial 仍无损。DashScope 对 PCM 默认 100ms 合包且可在 20–200ms 配置，finish 刷尾包、abort 清队列，缓冲/队列/在途字节共用两秒背压预算；`use_environment_proxy=false/true` 在现代 WebSocket runtime 分别映射 `proxy=None/True`，旧 header 签名仅在明确 unexpected-keyword 时兼容。track 错误只映射固定语义白名单 code/type，特权诊断保留 `cause_code/cause_type`，候选人不可见且不保存原始异常文本。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `467 passed, 5 skipped in 20.02s`；DashScope/LiveKit/Agent 相关四文件集合为 `123 passed in 4.02s`；`cd app/web && npm test -- --run` 为 9 个文件 `80 passed`；`npm run build` 成功，仅有既有 VRM chunk 大于 500 kB 的非阻断告警；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_formal_stt_pyc .venv/bin/python -m compileall -q app tests` 和 `git diff --check` 通过。专项回归包含真实 `LiveKitCandidateAudioIngress` 100 帧队列在 partial 投影持续阻塞时消费 600×20ms（12s）音频：全部进入 Evidence、无 `audio_stream_failed`。旧 API PID `63803` 以 TERM 正常停止，新 API PID `73514` 已以 `INTERVIEWER_LOCAL_MEDIA=true` 监听 `127.0.0.1:8000`；`/healthz` 为 `ok`，`/readyz` 为 `ready=true`，数据库、Redis、VRM、LiveKit/Egress 与权威收音均 ready。
- 中间失败与恢复：旧事故记录把 track task 真实异常压成 `RuntimeError`，因此无法从历史数据再区分是 Provider 还是 sink 的最后一击；本项通过新的语义白名单诊断修复后续可观测性，不猜造、不改写历史。没有调用外部付费 Provider、创建新面试、恢复暂停会话、重放业务任务或更改历史证据。
- 未完成事项或恢复说明：事故会话已持久暂停且仅保存故障前完整 Evidence 前缀，不恢复、不把 transient partial 冒充 final、不把后续 Egress 音轨绕过正式 Evidence 写成答案。修复后必须使用新邀请复验至少 60 秒长回答、连续字幕、端点唯一 final、CandidateAnswer 与多轮续租；完成前不得标记 production accepted 或 `closed`。

## 2026-09-03 · EVIDENCE-LEASE-TELEMETRY-004

- 目标：修复暖场识别成功后，正式题播放期间候选人数字人高频遥测阻塞服务端续租，导致权威 Evidence owner 被误判失效、正式 STT 尚未打开即暂停的问题；同时保留跨实例 fencing、权威音频、答案形成、评分与人工接管语义。
- 关联问题：事故会话 `iv_2dd0eecc8dd8491f` 于 `2026-09-04T02:45:35Z` 成功打开暖场 STT 并完成试音，正式题于 `02:46:04Z` 开始播放、`02:46:47Z` 才结束；Evidence owner 最后续租在 `02:46:15Z`，租约 `02:46:30Z` 到期，`02:46:50Z` 续租时收到 `EvidenceOwnershipLost`，会话以 `EVIDENCE_OWNER_FENCED/authoritative_audio_ingress_failed` 暂停。期间没有正式 `evidence.open`、正式 STT invocation 或正式转写，说明不是识别 Provider 拒绝，而是识别门尚未打开。候选人通道把每个 `avatar_viseme_drift_ms` 遥测都执行幂等查重与持久化，SQLite 每次事务又全量重载文档，约四百条逐帧遥测与正式题播放时段重合并饿死续租。
- 状态：`verified（仓库与本地服务）`；目标浏览器新会话与真实 Provider 复验为 `environment_pending`，未关闭。
- 计划修改：将纯观测遥测从领域命令幂等账本与会话持久化中剥离并保证事件循环公平，限制浏览器逐帧 viseme 指标发送频率；为续租增加无 PII 的调度、数据库耗时和成功率观测，同时继续严格拒绝过期或 owner/lease/epoch 已变化的旧 owner，control generation 独立保护候选控制命令；补高频遥测、租约竞态和正式 Evidence 门回归，重建前端 bundle，同步实时架构、接口、领域、已知问题、开发进度与路线图。面试抽题、冻结计划、候选人回答证据、评分、追问、S2S/cascade 和人工接管业务逻辑不变。
- 实际修改文件：服务端遥测边界与候选指标白名单为 `app/services/interview_agent.py`、`app/core/interview_agent_metrics.py`；Evidence owner 续租观测为 `app/services/livekit_evidence_ingress.py`；浏览器指标聚合与生命周期为 `app/web/src/features/candidate/{agent-experience.js,agent-experience.test.js}`，并重建 `app/web/dist/`；后端回归为 `tests/{test_interview_agent_acceptance,test_interview_agent_contracts,test_livekit_evidence_supervisor}.py`；同步 `docs/{architecture,api-design,domain-model,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。没有数据库迁移，也没有修改抽题、计划/题目冻结、权威 Evidence/CandidateAnswer、评分、受控追问、S2S/cascade 或人工接管服务。
- 实际实现：候选人 `telemetry.observe` 只允许显式浏览器指标集合，有限数值在进程内聚合并在完成前主动让出一次事件循环；该路径不获取会话 command lock，不执行 signal/takeover/problem 处理，也不写 processed key、序号、事件、问题或会话文档。内部 lease 指标与候选上报集合完全分离，浏览器不能伪造；viseme drift 与 freeze 各自按一秒窗口只发送最大值，socket 关闭、停止或失败时清空窗口且不跨重连补发。Evidence owner 续租新增单调时钟调度滞后、同步数据库耗时与成功率指标，指标失败不能干扰续租；既有 15 秒 TTL、过期/替换/fence 后旧 owner 自我终止及正式 Evidence 门均保持不变。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `446 passed, 5 skipped in 21.13s`；`cd app/web && npm test -- --run` 为 9 个文件 `80 passed`；`npm run build` 成功生成 `bundles/index-CJKMP5t5.js` 与 `bundles/VrmAvatar-H1q5vKS5.js`，仅有既有 VRM chunk 大于 500 kB 的非阻断告警；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_evidence_lease_telemetry_pyc .venv/bin/python -m compileall -q app tests` 通过。定向服务端 91 个、前端 31 个回归均通过，其中真实 SQLite 以 512 条同 key 候选遥测证明会话 version、updated_at、processed key、last sequence、events 与 problems 全部不变，并覆盖 lock 外执行、显式调度让步、非候选拒绝、未知/内部/非法指标丢弃、连续续租、观测后端异常不影响续租及严格过期自我 fencing。修复后 API 已替换旧进程并以 PID `63803` 监听 `127.0.0.1:8000`，`/healthz` 为 `ok`，`/readyz` 为 `ready=true` 且数据库、Redis、VRM、LiveKit/Egress 与权威音频入口全部 ready；根页面已提供新 bundle，既有 Worker/Beat PID `49782` 保持在线。
- 中间失败与恢复：首次从前端目录运行定向测试时仍使用仓库根相对路径，Vitest 报未找到文件；改用前端目录相对路径后通过。诊断时受执行沙箱限制的本机进程和 HTTP 只读检查先被拒绝，随后在用户授权的本机执行边界完成；旧 API 以 TERM 正常退出并由上述新进程替换。没有外部 Provider 调用、创建新面试、恢复暂停会话、重放业务任务或改写历史证据。
- 未完成事项或恢复说明：事故会话已持久暂停，且候选人 token 已出现在对话中；不恢复旧会话、不复用 token、不改写历史证据。管理员需使用新邀请在目标浏览器复验暖场 final、正式 `evidence.open`、正式字幕、连续多轮续租和回答封口；取得该环境证据前保持 `environment_pending`，不得标记 production accepted 或 `closed`。

## 2026-09-03 · REALTIME-WARMUP-VAD-RANGE-003

- 目标：修复真实候选人会话中开场音频被扬声器回声误打断、试音 STT 握手前音频丢失且 finalize 错误被二次覆盖、临界 30 FPS 被误判为 renderer fatal，以及私有音频读取不支持标准 Range 的问题。
- 关联问题：会话 `iv_ddf3b566505742cf` 的动态开场音频已成功生成，私有 FileObject 与落盘 WAV 长度、checksum、RIFF 和 ffprobe 解码均一致，服务端读取两次为 HTTP 200；但播放开始约一秒即收到本地 `speech.started` 并清空播放器。当前 AudioWorklet 以两个约 2.7ms level block 判定开口、14 个 block 判定停顿，产生大量亚秒级 speech start/stop；warm-up gate 又在真实 STT WebSocket 约 2.5 秒握手完成前丢弃开头音频，随后无有效 final 的第一次 seal 已消费流，journal 第二次执行把真实 Provider 错误覆盖为 `EVIDENCE_TURN_REQUIRED`。截图显示 30 FPS 后约一秒收到 `AVATAR_RENDERER_FAILED`，与页面四舍五入显示 30、gate 仍按原始略低于 30 的浮点值失败关闭一致。另有运行配置偏差：API 使用本地 SQLite，而上一工作项重启的 Celery 错误加载 PostgreSQL 环境并持续连接不可用的 55432 端口。
- 状态：`verified（仓库）`；目标浏览器与真实 Provider 新会话复验为 `environment_pending`，未关闭。
- 计划修改：以持续时间和 agent-speaking 高门槛替代 5ms VAD 抖动，同时保持确认后 200ms 内静音；区分 Evidence “请求打开/服务端 ready”，在流未 ready 前不给候选人虚假“正在听”状态，并保证试音 final 失败不会被不可重入重试覆盖；让 FPS gate 与用户可见的整数 30 FPS 判定一致；私有本地音频实现单 Range 的 200/206/416、`Accept-Ranges`、`Content-Range` 与 HEAD/GET 一致长度；补前后端合同与事故时间线回归，重建 bundle、同步实时架构/接口/领域/已知问题/进度/路线图，并用与 API 相同的本地配置重新启动 Worker。面试抽题、冻结计划、权威音频来源、答案形成、评分、追问、S2S/cascade 与人工接管规则不变。
- 实际修改文件：私有文件传输为 `app/api/routers/talent.py`、`tests/test_resume_ingestion.py`；访问日志 path/query 凭据过滤为 `app/core/access_log.py`、`tests/test_access_log.py`；Evidence owner/reset/stale-controller 与 destructive-once 暖场恢复为 `app/services/{interview_agent,livekit_evidence_ingress}.py`、`tests/{test_interview_agent_contracts,test_livekit_evidence_supervisor}.py`；候选人握手、VAD、FPS 与恢复状态机为 `app/web/src/features/candidate/{Page.jsx,Page.test.jsx,agent-experience.js,agent-experience.test.js,audio-worklet-capture.js,audio-worklet-capture.test.js,vrm-avatar.js}`、`app/web/src/features/interviews/{agent-event-runtime.js,agent-event-runtime.test.js}`，并重建 `app/web/dist/`；同步 `docs/{architecture,api-design,domain-model,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。没有数据库迁移，也没有修改抽题、计划/题目冻结、权威答案、评分、追问、S2S/cascade 或人工接管服务。
- 实际实现：私有本地文件对 GET/HEAD 实现单一 closed/open-ended/suffix Range 及精确 200/206/416 头；Uvicorn access log 同时遮蔽私有文件、私有媒体和邀请 URL 的 path token 与敏感 query。AudioWorklet 改用采样时间窗，agent-speaking 使用更高门槛且保留 200ms 内确认打断；Evidence open 只有 owner 建立 Provider stream 后才按当前 causation ACK ready，ready 前普通 VAD 不进入服务端。暖场 finalize 对原流只执行一次，首个 Provider 错误和 durable retry gate 不再被二次 seal 覆盖；显式 reset 的 abort、状态、floor 与 ACK 全由单一 owner 在 journal complete 前串行执行，同 key duplicate 无副作用，旧 controller 在 preflight fence 后只关闭自身且不污染 problem/history。断线恢复接受 owner 已执行 reset 的 live/snapshot 事实，用新 causation 有界 reassert；重复/foreign ACK 不开 gate，也不重复创建付费流。VRM gate 与 UI 统一使用显示整数 FPS。审查期间评估过 pre-open PCM buffer，但因可能截断回答并扩大暂停/隐私竞态而完整撤回，没有残留该路径。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `439 passed, 5 skipped in 18.96s`；`cd app/web && npm test -- --run` 为 9 个文件 `74 passed`；`npm run build` 成功生成当前 production bundles，仅有既有 VRM chunk 大于 500 kB 的非阻断告警；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_warmup_range_pyc .venv/bin/python -m compileall -q app tests` 与 `git diff --check` 通过。独立只读审查另复跑后端相关集合 `90 passed`、前端状态机 `25 passed`，确认无代码、协议或安全 blocker，且 `rg` 无 pre-open buffer/generation 残留。修复后 API 以本地媒体配置监听 `127.0.0.1:8000`，`/healthz` 为 `ok`，`/readyz` 为 `ready=true` 且 database、Redis、VRM、LiveKit/Egress、权威 LiveKit ingress 全部 ready；假 token 请求的真实 access line 显示 `/api/v1/private-files/{token}` 而不显示 path secret。Celery Worker/Beat 以同一 `INTERVIEWER_LOCAL_MEDIA=true` 配置连接 Redis DB 2，`inspect ping` 为一个在线节点，active/reserved 均为空。
- 中间失败与恢复：诊断开始时先停止旧 API；前一工作项遗留的 Worker 曾错误加载 `.env.production.local` 并尝试连接本机不可用的 PostgreSQL `55432`，本项没有沿用该配置。首次按本地配置启动的新 Worker 因执行沙箱禁止本机 Redis socket 而只产生连接重试、未消费任务，已用 Ctrl-C 停止；随后在允许访问本机 Redis 的执行边界以相同本地业务配置重启并由上述 inspect 证明恢复。没有外部 Provider 调用、重放业务任务、创建新面试或改写旧会话/历史资产。
- 未完成事项或恢复说明：事故会话已因候选人端 renderer fatal 持久暂停，且对话中贴出的私有 grant 已暴露；不恢复旧会话、不复用 grant、不改写历史 FileObject。需由管理员使用新邀请在目标 Chrome/Edge/Safari 复验完整/seek Range、LiveKit/STT 首帧与字幕、扬声器回声和真实 200ms 内打断；这些环境证据完成前不得标记 `closed` 或 production accepted。

## 2026-09-03 · AGENT-AUDIO-PLAYBACK-001

- 目标：修复正式数字人语音在候选人本地 VAD 打断后被误报为致命播放故障，以及流式 TTS WAV 占位长度头未经规范化而形成损坏私有音频的问题。
- 关联问题：当前候选人端在主动 `barge_in` 时通过清空 `audio.src` 停止旧播放，但旧元素随后发出的 `error`、`ended` 或延迟 `play()` 拒绝仍会触发 `CANDIDATE_RUNTIME_FAILED`；PrivateAssetImporter 又会原样保存 RIFF/data 长度为流式占位值的 WAV，导致浏览器在实际 EOF 处可能再次报错。事故会话已暂停且其候选人 token 已暴露，不作为恢复对象。
- 状态：`verified（仓库与本机资产语料）`。
- 计划修改：为候选人正式播放建立带 performance ID/代次的可取消生命周期，忽略已取消或已替换播放器的迟到回调，同时保留当前播放真实错误的 fail-closed；在共享私有音频导入 seam 严格解析并只修复可证明完整的流式 WAV 长度头，拒绝截断、重复或结构非法容器；增加前后端回归测试，重建 production bundle，并同步架构、接口、领域、存储、已知问题、开发进度与路线图。
- 不变量：不改变面试状态机、题目冻结、回答证据、评分、受控追问、S2S/cascade 路由或人工接管语义；主动打断仍立即停止当前表达，真正的当前播放故障仍暂停面试；不静默改写历史冻结资产或恢复已暂停会话。
- 实际修改文件：播放生命周期为 `app/web/src/features/candidate/agent-experience.js` 与 `agent-experience.test.js`；私有音频导入为 `app/services/private_assets.py`、新增 `tests/test_private_assets.py`，并把 `tests/test_documented_gap_apis.py` 的伪 RIFF fixture 替换为完整 WAV；重建 `app/web/dist/`；同步 `CONTEXT.md`、`docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。未修改 Provider adapter、面试状态机、证据/评分/追问服务或数据结构。
- 实际实现：CandidateInterviewExperience 为每次播放创建对象 identity，play/ended/error、RAF tick 与异步 play rejection 都必须匹配当前 identity；stop 先失效全局引用再解绑监听、pause、移除 src/load，清理副作用只能成为 stale callback。服务端 interrupt 带 `performance_id` 时只停止同一表达，旧 interrupt 不影响替换后的新表达；自然结束只发送一次 stopped，当前真实错误继续提交 `CANDIDATE_RUNTIME_FAILED`。PrivateAssetImporter 在私有落盘前严格 walk RIFF/WAVE，校验唯一有序 fmt/data、完整 chunk/padding、PCM/float geometry 与 block alignment；只规范化 exact `0x7fffffbf/0x7fffff9b` signed-limit 流式占位组合，以及所有 child chunk 完整到 EOF 后 outer RIFF 恰好漏计四字节 WAVE form 的已知编码器模式；其他截断/长度偏差/重复/错序/尾随/MIME 冲突失败关闭，checksum 基于规范化字节。
- 验证命令与结果：播放定向 Vitest `16 passed`，前端完整 7 个文件 `62 passed`；WAV/私有资产定向 `23 passed`，扩展的语音/Avatar/Agent 后端集合 `100 passed`；完整后端 `431 passed, 5 skipped in 46.00s`。`npm run build` 成功生成 `index-BifhkKpy.js` 等 production bundles，仅报告既有 VRM chunk 大于 500 kB；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-agent-audio-playback-pyc .venv/bin/python -m compileall -q app tests` 与 `git diff --check` 通过。本机只读遍历 64 个私有 WAV，确认 23 个 signed-limit、41 个漏计 form size，全部内存规范化后声明帧字节等于完整实读字节；没有写回文件或数据库。修复后 API 已替换旧进程并以 PID `24473` 监听 `127.0.0.1:8000`，`/healthz` 返回 `ok`，`/readyz` 返回 `ready=true` 且数据库、Redis、VRM、LiveKit/Egress 与权威音频入口全部 ready；Celery Worker/Beat 已重新启动，最终 `inspect active reserved --timeout=5` 返回一个在线节点且为空。
- 中间失败与恢复：首次从 `app/web` 复核时仍给 `rg/sed` 加了 `app/web/` 前缀，两条只读命令报文件不存在，但同一命令末尾的定向 Vitest 仍为 `16 passed`；随后使用正确相对路径完成源码复核。第一次完整 pytest 调用在 30 秒 yield 后只显示进度且包装输出未保留 session ID，因此等待后重新执行并取得上述完整结果；两轮均为测试隔离路径，没有外部供应商调用或业务数据副作用。重启 Worker 时首次 `celery control shutdown` 没有收到 reply 并以状态 69 返回；随后的只读 ping 与进程检查确认旧 Worker 已退出，再启动新 Worker/Beat 并由上述 inspect 证明在线，没有重放任务或业务数据副作用。
- 未完成事项或恢复说明：仓库修复完成，但事故会话已持久暂停且候选人 token 已在对话中暴露，不能复用或恢复。历史 FileObject/QuestionSpeechAsset 按不可变规则未原地改写；管理员需通过现有题目语音 regenerate 生成新资产，再创建新预约/邀请复验。目标 Chrome/Edge/Safari、真实网络抖动、首音/barge-in/A-V 指标和生产 acceptance v2 仍属于 `REALTIME-AGENT-001` 的环境验收，不因本项改为 production ready。

## 2026-09-03 · REALTIME-OPENING-EVIDENCE-002

- 目标：修复本地正式面试开场白无语音、候选人麦克风虽发布到 LiveKit 但服务端不识别、2.5 秒端点倒计时与暂停状态冲突，以及“正在决定追问”没有真实 final/追问结果的问题。
- 关联问题：本地准入把缺少 `interview_agent_expression`、`warmup_calibration`、`candidate_answer_transcription` 等真实 route 误标为可开始，而正式运行时拒绝 mock TTS；候选人刷新后签发新的 LiveKit identity，但权威订阅器继续过滤到冻结的旧 identity；暂停状态下浏览器 VAD 仍能发送 speech 信号并在本地制造端点/理解状态；正式根题缺少可播放预生成资产时没有回落到同一正式 TTS seam。
- 状态：`in_progress（真实开场、LiveKit 收音、暖场与正式 STT final 已验证；关闭 Qwen 深度思考后的追问外部复验待完成）`。
- 计划修改：让开发准入与真实表达/识别能力一致；候选人新票据复用服务端冻结媒体 identity，同时保留独立控制连接和 backfill epoch；根题只在预生成私有音频可用时复用，否则调用 `interview_agent_expression`；暂停/致命问题清空端点并禁止本地 VAD推进；把 2.5 秒文案明确为“连续静音后收口当前回答”；为本机已有且验证通过的 DashScope TTS/STT/LLM/S2S 配置建立用途明确的 route，并完成真实浏览器媒体闭环复验。
- 实际修改文件：后端为 `app/services/{agent_ticket,interview_agent,interview_evidence,livekit_evidence_ingress}.py`、`app/adapters/livekit_audio_ingress.py`、`app/domain/appointment_admission.py`、`app/providers/{dashscope/provider.py,dashscope/provider.json,openai_compatible/provider.py}`；候选人端为 `app/web/src/features/candidate/{Page.jsx,agent-experience.js}` 及重建后的 `app/web/dist/`；测试为 `tests/{test_interview_agent_contracts,test_livekit_tenant_avatar_security,test_dashscope_provider,test_livekit_audio_ingress,test_livekit_evidence_supervisor,test_interview_evidence_group}.py`、`app/web/src/features/candidate/agent-experience.test.js`；设计与进度为 `docs/{architecture,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。本机运行配置库另新增 `interview_agent_expression`、`warmup_calibration`、`candidate_answer_transcription`、`interview_turn_understanding`、`controlled_followup` 五条精确 DashScope route；不包含密钥写入仓库。
- 实现结果：候选人重连票据复用冻结 LiveKit participant identity，只更换控制连接与 backfill epoch；暂停/完成会话为只读重连，不重新触发开场或 Evidence，前端也不会继续发送 VAD speech 命令。主问题预生成语音若为开发占位，会回落到 `interview_agent_expression` 受管 TTS；浏览器朗读仍被禁止。2.5 秒只由服务端 `speech.stopped` 权威事件展示，表示连续静音后收口当前回答并开始 final/理解，继续说话会取消，不表示直接下一题；客户端 2475ms 假“理解中”切换已删除。LiveKit 静音在 STT 握手前无锁丢弃，暖场端点不再误要正式 turn，端点成立先关闭 Evidence 门再等待 final；DashScope duplex 使用两秒字节预算的后台 sender/reader，不再按帧等待网络。Qwen 实时理解/受控追问默认关闭深度思考，模型配置可用中文开关显式覆盖。开发环境显式配置实时 route 后必须具有近期健康结果，防止配置错误被 mock 静默掩盖。
- 验证命令与结果：新增并发/背压/暖场端点/Qwen 请求定向回归为 `40 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `413 passed, 5 skipped in 18.10s`；`cd app/web && npm test -- --run` 为 7 个文件 `58 passed`；`npm run build` 成功生成 `index-88cwaGXL.js`，仅有既有 VRM chunk 大于 500 kB 的非阻断告警；compileall、Provider manifest JSON 与 `git diff --check` 通过。真实隔离链路使用本机 LiveKit 和百炼生成的中文 PCM，确认开场私有音频、服务端首帧、暖场 partial/final、2.5 秒端点、正式题音频与正式 authoritative final。旧 API PID `63770` 与旧 Celery worker 已正常关闭；用户了解本地监听和读取环境配置的风险并再次明确授权后，API/worker 首次重启并通过健康核验，但两个运行会话随后均已退出。用户再次授权后，最新 API 已按 `.env.production.local` 重启为 PID `5420`、监听 `127.0.0.1:8000`，`/healthz` 返回 `ok`，`/readyz` 返回 `ready=true` 且数据库、Redis、VRM 检查全部 ready；最新 Celery worker/Beat 已连接 `redis://127.0.0.1:6379/2`，`celery inspect ping --timeout=5` 返回一个在线节点和 `pong`。
- 中间失败与恢复：首次真实 route 探测在受限网络中失败；用户随后明确授权少量百炼费用，五条 route 的真实 TTS/STT/LLM 探针均成功并记录 healthy。隔离端到端测试依次发现并修复 STT 按帧 receive、握手锁、暖场无 turn seal、socket send 抖动和 final 持锁；每次使用全新或隔离 SQLite 副本，没有改写正式面试。最终真实链路已到达正式 authoritative final，但 `qwen3.7-plus` 理解连续四个 attempt 都在 30 秒超时；加入 `enable_thinking=false` 后的外部复测被执行权限服务异常拒绝，未绕过执行。
- 未完成事项或恢复说明：现有用户历史暂停会话不会被恢复或冒充全新验收。最新 API/worker 已完成重启和健康核验；仍需在全新面试会话中确认 Qwen 非思考理解与受控追问/下一题分支。生产目标网络、浏览器矩阵、WER/实体召回、并发和长期稳定性仍按里程碑 18 保持 environment/data pending。

## 2026-09-02 · LOCAL-LIVEKIT-EGRESS-001

- 目标：让本地开发环境在保留 LiveKit 权威音频链路的前提下，将 Participant Egress 录音录像写入本机私有目录；生产环境继续强制使用加密阿里云 OSS，不允许静默降级。
- 关联问题：`INTERVIEWER_FILE_STORAGE_BACKEND=local` 只控制普通 PrivateFileStorage，正式候选人票据仍无条件要求 OSS，导致开发准入先放行、`agent-ticket` 后返回 `LIVEKIT_RECORDING_NOT_READY` 并暂停会话；现有环境文件不会被 `main.py` 自动加载，错误提示也未说明本地 LiveKit/Egress 与存储分别缺少什么。
- 状态：`verified（仓库与本机开发媒体栈）`；生产环境验收仍为 `environment_pending`。
- 计划修改：在 `LiveKitMediaPlane` 深模块 seam 内统一解析本地/OSS Egress 输出并保持现有业务调用接口；增加本地 LiveKit/Egress 运行配置和中文环境注释；让预约准入、`/readyz` 与票据签发使用同一 readiness 事实；增加本地路径安全、生产失败关闭、Egress 请求和前端中文错误合同，并执行真实本机媒体栈冒烟。
- 实际修改文件：运行配置与本地基础设施为 `.env.example`、`infra/local-media/docker-compose.yml`、`infra/local-media/{livekit,egress}.yaml`、`scripts/{local-media.sh,smoke-local-media.py}`；媒体与存储深模块为 `app/adapters/livekit_media.py`、`app/file_storage/{interface,local,aliyun_oss,provider}.py`、`app/services/{media_capture,interviews}.py`；统一准入与探针为 `app/domain/appointment_admission.py`、`app/core/readiness.py`、`app/main.py`；测试为 `tests/{test_local_livekit_media,test_interview_agent_contracts,test_deployment_readiness,test_interview_session_aggregate,test_mvp_flow,test_position_resume_appointment_flow}.py`；说明与设计为 `README.md`、`CONTEXT.md`、`docs/{architecture,api-design,database-and-vector-storage,domain-model,development-progress,implementation-roadmap,known-issues-and-remediation,change-log}.md`、`docs/adr/0002-self-host-livekit-media-plane.md`。
- 实现结果：开发环境只需 `INTERVIEWER_LOCAL_MEDIA=true` 一个业务开关，即可统一启用本地 LiveKit、Egress、Redis、`database_fenced` 权威音频入口和本机私有录制目录；不需要 `.env.local`，`.env.example` 明确只作为中文模板。`LiveKitMediaPlane` 隐藏本地文件与 OSS 请求差异，路径经过规范化与目录边界校验；`PrivateFileStorage` 将本地开发保护与生产 OSS 服务端加密证明明确区分。预约准入、`/readyz`、票据签发与录制完成使用同一 readiness/protection 事实，不再出现开发准入放行后才在 `agent-ticket` 误报必须配置 OSS。生产环境拒绝本地开关并继续强制私有对象存储及加密证明，不会静默降级。
- 验证命令与结果：`docker compose -f infra/local-media/docker-compose.yml config` 通过；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_local_egress_final_pyc .venv/bin/python -m compileall -q app tests scripts/smoke-local-media.py` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `402 passed, 5 skipped in 18.08s`；`git diff --check` 通过。真实执行 `INTERVIEWER_LOCAL_MEDIA=true .venv/bin/python scripts/smoke-local-media.py` 后，Participant Egress 在 `data/private-files/interview-captures/local-smoke/1788408720/capture.mp4` 写入 `130227` 字节，`ffprobe` 确认 H.264 1280×720 视频与 AAC 44.1 kHz 双声道音频；本机应用 `http://127.0.0.1:8000/readyz` 返回 200，数据库、Redis、VRM、LiveKit/Egress 与权威音频入口全部为 ready。
- 中间失败与恢复：首次真实冒烟因脚本直接执行时缺少项目根路径而导入失败，已在脚本内显式加入项目根目录；随后原生 LiveKit SDK 在系统 HTTP/SOCKS 代理下出现 `Handshake not finished`，实测 `NO_PROXY` 不生效，最终只在本地媒体模式移除 `HTTP_PROXY/http_proxy/ALL_PROXY/all_proxy`，保留外部模型调用需要的 HTTPS 代理，并增加回归测试。收紧开发准入后有 8 个旧测试未显式声明本地媒体能力，已改为在夹具中声明真实前置条件，全量回归通过。原 8000 端口旧进程已正常退出并由新配置进程替换，没有修改或复用任何业务面试与候选人 token。
- 未完成事项或恢复说明：当前 LiveKit/Egress/Redis 容器与 8000 端口应用保留运行，三份忽略版本控制的合成冒烟 MP4 保留作本机证据；Celery worker 未重启，因为本次 `agent-ticket`/Egress readiness 位于同步 Web 进程且 worker 不拥有 LiveKit 房间。用户曾粘贴的候选人 token 已视为暴露，旧会话也已因先前错误暂停，验收时必须新建或重新签发邀请，不能复用该 token。生产仍需独立配置 TURN、加密 OSS、目标 PostgreSQL/Redis、供应商凭据并完成浏览器矩阵、网络、并发和故障注入验收；本地 Docker 的 UDP receive-buffer 告警不作为生产通过证据。

## 2026-09-02 · VRM-ASSET-RUNTIME-001

- 目标：将用户提供的 `model/interviewer.vrm` 作为本地正式面试数字人资产，生成不含联系方式的授权 manifest，修复 VRM 1.0 preset/custom 口型误判、开发准入绕过资产 readiness、候选人页过早显示自我介绍和致命资产故障只在浏览器声称“已暂停”的状态不一致。
- 关联问题：`REALTIME-AGENT-001` / 里程碑 18 的授权 VRM 外部边界；当前 `inspect_licensed_vrm()` 只在 `expressions.custom` 查找 15 个口型，而合法 VRM 1.0 将 `aa/ih/oh/ou` 放在 `expressions.preset`，导致真实资产被误拒；开发准入与运行时资产检查另有分裂。
- 状态：`verified（仓库、本机资产与本地浏览器）`；`REALTIME-AGENT-001` 整体仍因目标媒体环境/金标/试点保持 `in_progress`。
- 计划修改：保存资产 SHA-256 与用户明确提供的 VRM 授权元数据；加深资产检查并统一 readiness/admission/runtime；新增候选人致命问题服务端暂停命令与稳定错误投影；在 Agent 会话真正建立前不显示自我介绍试音；增加后端/前端合同测试并同步架构、API、领域、已知问题、进度和路线图。
- 实际修改文件：资产/配置为 `.env.example`、`model/interviewer-license.json`（用户提供的 `model/interviewer.vrm` 保持原二进制）；后端为 `app/core/{access_log,readiness}.py`、`app/domain/{avatar_asset,appointment_admission}.py`、`app/schemas/api.py`、`app/services/interviews.py`、`app/api/routers/plans.py`、`app/main.py`；前端为 `app/web/src/features/candidate/{Page,VrmAvatar,agent-experience,vrm-avatar}.*` 及重建的 `app/web/dist/`；测试为 `tests/{test_access_log,test_livekit_tenant_avatar_security,test_realtime_media,test_web_frontend_modules}.py` 与 `app/web/src/features/candidate/{VrmAvatar,agent-experience}.test.*`；文档为 `CONTEXT.md`、`docs/{architecture,api-design,domain-model,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 实现结果：默认本地资产路径与去敏许可 manifest 已接入，SHA-256 为 `31618ee757bca4cf33a65f5baf1a107845e267214b7687401669dc5500f32e9f`；检查器按 VRM 1.0 preset/custom 并集验证 15 个口型，并逐项核对名称、版本、作者、版权、`personalProfit`、肖像权限、署名与禁止修改，不保存联系方式。development readiness/admission 不再绕过本地 VRM，cloud 模式不受无关检查阻断。候选人致命故障通过 token 绑定、allow-list 的 `runtime-problems` 接口真实暂停生命周期，UI 只在服务器回执后称已暂停；session snapshot 前显示安全会话 gate，不再误把自我介绍当生成结果。真实浏览器复验后又修复导出 T-Pose/远镜头为自然胸像姿态，并为 Uvicorn access log 增加查询凭据遮蔽。
- 验证命令与结果：真实 `inspect_licensed_vrm()` 返回 `ready=true`，VRM 1.0、15-viseme、blink/lookAt/humanoid、hash 与完整去敏许可字段全部通过；定向后端 `17 passed`、定向前端 `13 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `395 passed, 5 skipped in 17.59s`；`npm test -- --run` 为 `57 passed`；`npm run build` 成功，只有既有 VRM chunk 大于 500 kB 的非阻断告警；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_vrm_complete_pyc .venv/bin/python -m compileall -q app tests` 与 `git diff --check` 通过。修复后真实进程 `/readyz` 返回 ready，面试 `iv_73b0b62cc9f0458e` 的 avatar-config 为 ready/15 visemes、私有模型端点为 200/GLB/hash 匹配；access log 只显示 `grant=REDACTED`。本地浏览器实际渲染 60 FPS、自然胸像、无 warning/error，且会话快照前不显示自我介绍。
- 中间失败与恢复：首次从 `app/web` 使用错误相对 Python 路径未执行测试，随后回到仓库根目录重跑；一次合同增强漏写布尔连接符，compileall 当场发现并修正，同时使用显式 `PYTHONPYCACHEPREFIX` 避免 macOS 缓存目录权限问题，最终全量通过；浏览器连接不支持 `networkidle`，改用 `domcontentloaded`。首次视觉迭代把 VRM 手臂轴方向设反而呈举手姿势，浏览器复验后翻转并增加骨骼测试。无预检媒体的临时浏览器页按设计调用失败关闭、使实际本地会话暂时进入 `paused`；关闭测试页后已用生命周期 `resume` 恢复为原始 `in_progress`，重启进程并再次读取确认，没有遗留测试状态。
- 未完成事项或恢复说明：当前资产由权利人确认的范围为 `personalProfit`，企业法人、客户交付或其他范围部署前仍须取得匹配许可；真实 TTS 时间戳/中英 G2P、目标 Chrome/Edge/Safari 的摄像头/麦克风授权与 FPS/A-V 指标、LiveKit/TURN/Egress/OSS、多实例故障注入、签名 acceptance v2、金标和受控试点仍是独立环境/数据验收事实。本轮没有启动 Celery，因为根因与修复均位于同步 VRM/readiness/候选人 runtime 链路。

## 2026-09-02 · VOLCENGINE-DOUBAO-PROVIDER-001

- 目标：把火山引擎豆包从 `implemented=false` 配置清单补齐为可执行 Provider adapter，通过现有 ModelGateway seam 支持正式面试所需的 LLM、流式/批量 STT、TTS 与受控实时语音对话能力，不把厂商二进制协议泄漏到 InterviewAgentRuntime。
- 关联问题：千问/百炼 Provider 已可执行完整语音链路，但 `volcengine` 只能展示和保存配置，不能创建活动 route；Ark 与豆包语音两类凭据未隔离，Seed ASR 二进制帧、Seed-TTS 分块、Seeduplex API v3 JSON 会话、鉴权、错误映射、关闭/打断及“只表达 ApprovedConversationAct”合同尚未实现。
- 状态：`verified（仓库）`；真实火山账户与目标网络为 `environment_pending`。
- 计划修改：依据火山引擎官方协议更新 provider manifest/目录并实现 adapter；复用统一 Chat/Embedding/批量媒体与 RealtimeSpeechDialogue interface，在 adapter 内封装 Seed ASR WebSocket 二进制帧、Seeduplex JSON session 生命周期和 vendor event 映射；补协议、网关、探针、错误、安全与 S2S 批准文本合同测试，更新接口/架构/Provider/进度/路线图/已知问题和本日志。
- 实际修改文件：`app/providers/volcengine/{__init__,provider,protocol}.py`、`app/providers/volcengine/provider.json`；`tests/{test_volcengine_provider,test_provider_registry,test_model_configuration_v2}.py`；`docs/{architecture,api-design,model-provider-plugins,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 实现结果：`volcengine@1.0.0` 声明并执行 `llm.chat_json`、`llm.chat_text`、`embedding.text`、`stt.streaming`、`stt.batch`、`tts.synthesize` 和 `speech.dialogue_realtime`。Ark Bearer 只使用 `ark_api_key`；Seed ASR/Seed-TTS/Seeduplex `X-Api-Key` 只使用 `speech_api_key`。ASR adapter 封装官方 Gzip 二进制 framing、唯一 final 与文本/JSON 错误帧；batch 只接收服务端解析的私有音频字节；TTS 收敛分块 JSON/Base64、格式和时长；Seeduplex 以 `speech_text_buffer.replacement` 提交冻结批准文本，支持 PCM、cancel、close 与统一事件，不把厂商协议泄漏给 InterviewAgentRuntime。manifest 增加当前 Ark/Seed 模型目录、Resource ID、音色、热词、端点及动态表单，并删除没有实现的 `avatar.speak` 声明；本地 VRM 继续复用 TTS/S2S 表达 seam。
- 验证命令与结果：`.venv/bin/pytest -q tests/test_volcengine_provider.py tests/test_provider_registry.py tests/test_model_configuration_v2.py` 为 `33 passed in 2.38s`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `391 passed, 5 skipped in 16.34s`；`npm test -- --run` 为 `54 passed`；`npm run build` 成功；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_volcengine_final_pyc .venv/bin/python -m compileall -q app tests`、Provider registry 加载与 `git diff --check` 均通过。Vite 仍只有既有 VRM chunk 大于 500 kB 的非阻断告警。
- 未完成事项或恢复说明：没有真实火山引擎 Ark/Speech API Key、语音 Resource ID、模型授权、配额或目标网络，未发起任何付费调用，也未把任何模型/route 标记为 ready。部署方仍须逐个执行连接、ModelConfiguration 和 ModelRoute 探针，并用脱敏录音验证 WER、英文技术实体召回、partial/final、首音、barge-in、音质、并发、成本与长期稳定性；通过前继续由生产 readiness 失败关闭。

## 2026-08-31 · CANDIDATE-REALTIME-EXPERIENCE-001

- 目标：把候选人面试房间从“静态图片读题”升级为可感知的实时交互：本地数字人增加音频驱动口型与听说状态；摄像头始终提供本地自检预览；麦克风提供会议式音量/声音检测反馈；答题区用明确的轮次、倾听和处理反馈形成对话节奏。
- 关联问题：当前本地数字人只有静态图片和四条音量柱；`record_video=false` 时前端完全不请求摄像头；候选人只能看到题目和转写框，缺少“我在听/已听到/正在整理”的交互反馈；服务端 STT 是否收到语音没有可见的本地声学反馈。
- 状态：`cancelled`。
- 计划修改：保持服务端 STT 为正式答案来源、浏览器不上传未授权摄像头画面的边界；新增可测试的本地口型驱动和麦克风电平监测；候选人房间自动打开设备并显示本地摄像头预览、声音检测和识别状态；补 React/运行时回归，更新架构、已知问题、进度与路线图并重建生产 bundle。
- 实际修改文件：仅本日志。
- 验证命令与结果：未执行代码验证；取消前没有修改任何业务代码、测试、配置或生产 bundle。
- 未完成事项或恢复说明：用户明确拒绝“静态图补口型、页面补状态”的局部方案，要求改为完整实时面试智能体；本工作项被 `REALTIME-INTERVIEW-AGENT-001` 取代，历史记录保留且无业务副作用需要恢复。

## 2026-08-31 · REALTIME-INTERVIEW-AGENT-001

- 目标：把候选人进入面试到结束面试重构为完整实时面试智能体：统一 AgentChannel、自动轮转与打断、服务端权威语音证据、结构化语义理解和受控动态追问、自研 3D 数字人表达、摄像头明确同意与私有录像、企业监看/审计式接管、断线恢复和生产 readiness。
- 关联问题：当前候选人页由 React 直接编排 `/live`、`/stt-stream`、`/avatar/speak`、PCM、录音备份和播放；本地数字人是静态图片；回答依赖手动开始/结束；追问主要由字面关键点匹配和模板生成；`record_video=false` 时摄像头甚至不提供本地预览；候选人无法区分本地声学活动、服务端收音和权威 STT；缺少统一发言权、自然元意图、两层追问、录像实体和人工接管 lease。
- 状态：`in_progress`。
- 计划修改：新增高 depth 的 `InterviewAgentRuntime.open/send/events/close` interface 和候选人 `CandidateInterviewExperience` facade；扩展领域状态、同意/readiness、Prompt/Schema、受控追问与根题合并证据评价；接入自托管 LiveKit media-plane adapter 和显式失败关闭 readiness；使用 Three.js/VRM 本地 rig 表达并移除静态图正式路径；升级候选人/企业 live UI、自动 VAD/倒计时/barge-in、录像与接管；补合同、端到端、隐私、视觉和真实环境验收，同步全部相关设计文档并在等价验收后删除旧候选人调用链。
- 生产化追加设计（2026-09-01）：将权威候选人 Evidence 执行面继续收敛为 `attach / dispatch / detach` 深模块；首先落独立所有权记录、数据库时钟 lease、单调 fencing epoch、control generation 和 CandidateAnswer 提交前的二次 fence 校验。本切片不开放多实例 production readiness；可靠命令 journal、独立持久媒体 checkpoint/repair 与 chaos 验收完成前继续失败关闭。
- 生产化追加设计（二，2026-09-01）：新增数据库持久 `EvidenceCommandJournal`，使用确定性命令 ID、control generation、owner fence、claim TTL、执行 deadline 和安全 payload/result allow-list，建立 at-least-once 执行与同幂等键结果回放的存储合同。先验证 journal 的并发、旧控制拒绝、owner epoch 迁移、claim 超时重投和敏感字段阻断；在连接无关的 owner executor/remote proxy 真正接通前继续返回明确不可用，不把“有表”冒充跨实例可用。
- 一次性生产收口计划（2026-09-01）：以 `AuthoritativeEvidenceIngress.attach/dispatch/detach` 为唯一 Interface，同时接通持久 command journal 的 owner executor/remote proxy、独立媒体 segment/checkpoint 与 owner-loss repair；随后删除 AgentChannel 对 concrete LiveKit/STT/录音分支的编排泄漏和已无正式调用者的候选人 compatibility 路径，补双实例/崩溃/重投/隐私/接管端到端合同并全量更新文档。真实商用 VRM、目标 LiveKit/TURN/Egress/对象存储/Provider 凭据与经授权金标仍属于外部验收输入，仓库不得伪造。
- 实际修改文件：配置/依赖 `.env.example`、`pyproject.toml`；领域与合同 `app/domain/{interview_agent,avatar_asset,appointment_admission,interview_lifecycle}.py`、`app/core/{prompt/contracts,readiness}.py`、`app/schemas/api.py`；媒体/证据/对话服务 `app/adapters/{livekit_media,livekit_audio_ingress}.py`、`app/services/{interview_agent,interview_evidence,livekit_evidence_ingress,agent_ticket,media_capture,warmup_calibration,conversation_understanding,avatar_performance,appointments,interviews,reports,streaming_stt}.py`；API/持久化/传输 `app/api/routers/{interviews,plans,realtime,system}.py`、`app/{main,persistence/interface,persistence/memory,repositories/memory,repositories/sqlite,providers/mock/provider}.py`、`app/transport/{realtime,service_locator}.py`、`app/transport/http/fields/public_interview.py`；候选人与企业 Web `app/web/package*.json`、`src/features/candidate/{Page,VrmAvatar,agent-experience,audio-worklet-capture,encrypted-audio-ring,preflight,vrm-avatar}.*`、`src/features/interviews/{Page,agent-monitor}.*`、`src/App.test.jsx`、`styles.css` 与重建后的 `dist/`；测试 `tests/{test_interview_agent_contracts,test_interview_evidence_group,test_livekit_audio_ingress,test_livekit_evidence_supervisor,test_web_frontend_modules}.py` 及前端 agent experience 测试；文档 `CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`、ADR-0002/0003。
- 实际实现：正式候选人/企业控制面已收敛为 AgentChannel，包含 Floor、自动端点、barge-in、暖场、元意图、结构化理解、受控两层追问、根题合并评分 revision、角色安全事件回放和审计接管；候选人 facade 统一设备预检、自拍、三层收音状态、AudioWorklet/VAD、字幕、30 秒加密环形缓冲与资源关闭。LiveKit adapter 已具备最小权限 ticket、Egress/webhook，以及 receive-only candidate microphone subscriber；它冻结 room/identity，把 16 kHz mono PCM 送入连接独立的私有录音/StreamingSTT/2.5 秒 endpoint，在 control 断开后的 30 秒内继续运行，正式票据不再重复发送 WebSocket PCM。当前只在显式 `embedded_singleton + singleton acknowledgement + native RTC probe` 时开放。VRM 1.0 本地 renderer、15 viseme/动作/统一音频时钟和 WebGL/FPS fail-closed 已进入正式页面，静态肖像不再是正式候选人路径。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_agent_pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/python -m pytest -q` 为 `284 passed, 5 skipped`；`.venv/bin/python -m pytest -q tests/test_web_frontend_modules.py` 为 `6 passed`；`cd app/web && npm test -- --run` 为 `43 passed`；`npm run build` 成功生成 `index-DdYgHt3T.js`、`index-CwYrCW8s.css`、`VrmAvatar-BnfWeMBD.js`、LiveKit 与 VRM 独立 bundle，Vite 仅报告 VRM chunk 大于 500 kB 的非阻断告警；`git diff --check` 通过。另以临时 memory backend 启动 API，浏览器确认首页、最新 JS/CSS 和 `/auth/session` 均为 200，控制台无 warning/error；未把该 smoke 当作需要真实候选人票据、设备许可、VRM 资产和 LiveKit 的面试端到端验收。
- 生产化切片实际修改文件（2026-09-01）：`.env.example`；`app/domain/evidence_coordination.py`；`app/services/{evidence_coordination,interview_agent,interview_evidence,interviews,livekit_evidence_ingress,streaming_stt}.py`；`app/adapters/livekit_media.py`、`app/realtime_bus.py`；`app/persistence/{interface,memory,sqlite,postgresql}.py`；`app/repositories/{memory,sqlite}.py`；`tests/{test_evidence_ownership,test_livekit_evidence_supervisor}.py`；`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md` 与 ADR-0002/0004。
- 生产化切片实际实现（2026-09-01）：新增按 `(organization_id, interview_id)` 独立版本化的 Evidence ownership 事实，使用数据库时间、可续租 lease、单调 ownership epoch 和 control generation；流式 final、batch repair、转写状态及最终 CandidateAnswer 事务均携带并二次校验 commit fence，旧 owner 的迟到 Provider 回包不能形成答案。旧连接命令和迟到 detach 被 generation 拒绝；本地 owner 失租会立即停止 RTC/STT side effect、丢弃未提交流并持久化问题后暂停会话。连接无关 owner executor 尚未完成时，落到 remote owner 的控制连接明确返回 `503 EVIDENCE_OWNER_UNAVAILABLE` 并暂停，绝不以 Redis Pub/Sub 假装可靠路由。
- 生产化切片验证（2026-09-01）：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_evidence_fence_pycache .venv/bin/python -m compileall -q app tests` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_evidence_ownership.py tests/test_livekit_evidence_supervisor.py` 为 `7 passed`；最终完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `289 passed, 5 skipped`；`git diff --check` 通过。前端源码和 bundle 未在本切片修改，沿用上一条已记录的 `43 passed`、Python 前端合同 `6 passed` 与 production build 结果。
- Command journal 切片实际修改文件（2026-09-01）：`app/domain/evidence_coordination.py`、`app/services/{evidence_coordination,evidence_command_journal}.py`、`app/persistence/{interface,memory}.py`、`app/repositories/{memory,sqlite}.py`、`tests/test_evidence_command_journal.py`；`CONTEXT.md`、`docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md` 与 ADR-0004。
- Command journal 切片实际实现（2026-09-01）：新增 canonical `evidence.open/speech.started/speech.stopped/evidence.continue/evidence.seal/evidence.reset` 命令与严格 Pydantic 合同；命令 ID 由 tenant/interview/idempotency key 确定性哈希生成，原始 key 不落库，请求指纹阻止同 key 换请求。submit 在返回前持久化；claim 同时验证当前 owner fence 和 control generation，保存 deadline/claim TTL/attempt，过期 claim 可由新 epoch at-least-once 重领；complete/fail 再校验 fence/claim，终态只缓存 allow-list outcome。音频、转写、ticket/token、participant identity、私有 URI 和 Provider 对象均不能进入 payload/result。新增不改变 control generation 的后台 `claim_owner`，让 owner 恢复与 candidate control reconnect 保持不同领域语义。
- Command journal 切片验证（2026-09-01）：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_evidence_command_journal.py tests/test_evidence_ownership.py` 为 `11 passed`，覆盖 Memory/SQLite round-trip、同 key 并发、payload 隐私门禁、结果回放、旧 control、owner epoch 迁移、claim 超时和 retryable redelivery；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_command_journal_pycache .venv/bin/python -m compileall -q app tests` 通过；最终完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `296 passed, 5 skipped`；`git diff --check` 通过。前端未修改，沿用已记录的 `43 passed`、Python 前端合同 `6 passed` 与 production build 结果。
- 最终生产收口实际修改文件（2026-09-02）：新增 `app/core/{interview_agent_metrics,interview_agent_release}.py`、`app/domain/{avatar_asset,evidence_media,interview_agent}.py`、`app/operations/interview_agent_acceptance.py`、`app/services/{agent_expression_audio,avatar_asset_access,avatar_performance,browser_audio_backfill,conversation_understanding,evidence_command_journal,evidence_coordination,evidence_media,interview_agent,interview_evidence,livekit_evidence_ingress,livekit_room_binding,media_capture,meta_intent,warmup_calibration}.py` 与 `app/adapters/{livekit_audio_ingress,livekit_media}.py`；扩展 core/prompt/readiness、lifecycle/admission、模型网关、私有存储、Persistence/Repository、API/router、Outbox 与角色安全投影；删除 `app/services/realtime.py`、`app/transport/realtime.py`、`app/web/candidate/{runtime,pcm-stream,avatar-runtime}*` 及旧 hash bundle；新增/扩展 Evidence、Agent、Avatar、理解安全、acceptance、留存、S2S 与前端合同测试；更新全部必读文档和 ADR-0002/0003/0004，并重建 `app/web/dist/`。
- 最终生产收口实际实现（2026-09-02）：`AuthoritativeEvidenceIngress` 已把数据库时钟 ownership/fence、持久 command journal、连接无关 owner executor、DB polling/Redis wake hint、remote terminal receipt、私有 segment/checkpoint、owner-loss batch repair 和服务端授权 gap 的浏览器 backfill 接成单一 interface；正式模式只接受 `database_fenced`。旧 owner/control、重投、重复 finish 和重复 backfill 不能形成第二份 CandidateAnswer，Egress start 的未知崩溃窗口暂停而不盲目重复创建。旧实时 routes 和前端多通道编排已物理删除，致命故障不回退静态图或问卷。
- 最终语义与表达安全（2026-09-02）：TurnUnderstanding/ControlledFollowup 只允许绑定冻结能力点和逐字证据的最多两层追问；runtime 拒绝未冻结追问、标准答案泄漏、敏感属性、超预算及全部 AI 评价性称赞，人工接管发言必须在同一数据库事务复核当前 lease 并标记 `unscored_intervention`。S2S PCM 在 final transcript 与 ApprovedConversationAct 逐字一致前只存在于有界内存，批准后才复制为加密 `AgentExpressionAudio`；持久事实只保存 `agent-expression://file_id`，按当前参与者签发短期地址。VRM loader 检查真实 GLB/VRM 1.0、15 viseme、blink/lookAt/humanoid、hash 和商用/肖像权 manifest；预检使用 AudioWorklet 实测、本次 LiveKit publication 的 RTCStats 和真实 renderer FPS。
- 最终发布门禁（2026-09-02）：acceptance runner 升级为 `realtime-interview-agent.acceptance.v2`，要求报告精确绑定安全格式的 `deployment_id + release_revision`、Chrome/Edge/Safari 每类至少 10 个全部通过案例、全部硬指标样本下限、数据集 hash、30 天有效期与 HMAC；生产组织灰度必须逐个列 ID，拒绝 `*`。`/readyz`、邀请、start、候选人 ticket 签发及 ticket 消费均复核同一门禁；票据状态/到期和 takeover 全链改用数据库时钟，门禁在签发后失效会原子撤销未消费票据。
- 最终验证命令与结果（2026-09-02）：`git diff --check` 通过；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_final_pycache .venv/bin/python -m compileall -q app tests` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `382 passed, 5 skipped`；release/ticket 定向合同为 `16 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_web_frontend_modules.py` 为 `5 passed`；`cd app/web && npm test -- --run` 为 7 个文件 `54 passed`；`npm run build` 成功生成 `index-B6i45zSz.js`、`index-CwYrCW8s.css`、`VrmAvatar-CVLINwc1.js`、LiveKit 与 VRM 独立 bundle，仅有 VRM chunk 大于 500 kB 的非阻断告警。另以临时 memory backend 启动最终 bundle，浏览器确认首页、JS/CSS、`/auth/session` 与聚合 API 为 200，页面结构/视觉正常且 console 无 warning/error，随后正常关闭服务；没有把该首页 smoke 冒充需要真实票据、设备、VRM 与 LiveKit 的候选人 E2E。
- 未完成事项或恢复说明：工作项状态继续为 `in_progress`，但仓库内计划实现与兼容链删除已经收口。缺少的是不可由仓库伪造的交付输入与目标环境证据：授权商用专属 VRM/license manifest、目标 LiveKit/TURN/Egress/OSS/PostgreSQL/Redis、真实 STT/TTS/LLM 凭据与健康 route、Chrome/Edge/Safari 真机/网络/并发及全部延迟/A-V 指标、经授权脱敏金标和分组织候选人试点；因此当前没有可签发给生产的 acceptance v2 报告，也不得标记 production ready/closed。实施中曾遇到默认字节码缓存权限、错误目录执行 npm、LiveKit SDK callback/取消/订阅差异、沙箱绑定端口、浏览器错误页策略、浏览器 smoke 首次请求不受支持的 `networkidle` 等待、shell 查询中的反引号/引号错误、补丁上下文不匹配，以及末轮 `git diff --check` 发现 `plans.py` EOF 空行；均已改用安全路径或修正后通过，无外部调用、业务数据改写或待恢复副作用。

## 2026-08-31 · DASHSCOPE-LLM-CATALOG-001

- 目标：核对阿里云百炼千问 Plus 的真实模型 ID，扩充 DashScope 头部与常用 LLM 目录，并让可自定义的模型类型在管理页可见、可选官方候选项。
- 关联问题：页面当前只预填 `qwen-plus`，容易让用户误以为模型名不存在；DashScope LLM 虽然允许手填任意 ID，但 manifest 中已声明的候选模型没有在表单中形成可发现建议。
- 状态：`verified（仓库）`。
- 计划修改：更新 DashScope provider manifest 及版本；为 customizable 模型输入增加目录建议但保留自由输入；增加后端 manifest/API 与 React 行为回归，同步 Provider 设计文档并重建生产 bundle。
- 实际修改文件：`app/providers/dashscope/provider.json`、`app/web/src/features/models/Page.jsx`、`app/web/src/App.test.jsx`、`tests/test_model_configuration_v2.py`、`docs/model-provider-plugins.md`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-{7m08dfhY.js,FhSZ_GOI.css}`、本日志。
- 实际实现：根据阿里云百炼官方模型资料确认 `qwen-plus` 是真实可用的官方模型 ID，保留为默认项并把展示名改为“千问 Plus（官方模型 ID）”。DashScope manifest 升级为 `0.5.0`，新增千问 3.8 Max/Flash、3.7 Plus/Flash、Flash、Turbo、Long 和 3 Coder Plus，以及百炼托管的 DeepSeek V4 Pro/Flash、GLM-5.2、Kimi K2.7 Code、MiniMax M3 和 MiMo V2.5 Pro。托管第三方模型默认使用集中 Prompt 约束 + 网关 Schema 校验，不假定它们原生支持 OpenAI JSON Schema。React 对 customizable 模型类型使用 datalist 展示 manifest 建议，选中已知模型时同步可读名称，同时继续允许已授权的快照或新模型 ID；新建配置仍必须独立测试成功才能用于正式路由。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_model_configuration_v2.py tests/test_provider_registry.py tests/test_dashscope_provider.py` 为 `30 passed`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `236 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `38 passed`；`npm run build` 成功生成 `index-7m08dfhY.js/index-FhSZ_GOI.css`；`git diff --check` 通过。
- 未完成事项或恢复说明：未使用真实百炼 API Key 发起付费调用；具体区域、业务空间授权和模型可用性仍由“测试模型”探针确认。首次 React 新回归直接设置受控 input 的 DOM value，未触发 React 状态更新而失败 1 条；改用原生 value setter 模拟真实输入后全量通过，该失败仅影响测试代码。一次从 `app/web` 目录读取根目录相对路径 `docs/change-log.md` 返回文件不存在，回到仓库根目录后读取成功。两次失败均无业务数据或外部调用副作用。

## 2026-08-31 · KB-SPEECH-SWITCH-CANCEL-UX-001

- 目标：语音整库构建期间切换模型/声音时，原子取代旧构建、协作取消旧子任务并启动新 revision，不再因后台进度高频推进 KnowledgeBase version 而暴露乐观锁冲突；为整库/单题失败重试提供可见的转圈与防重复提交状态。
- 关联问题：客户端最多两次“重读 version 再提交”仍可在多个 TTS 子工作密集完成时连续撞上 CAS；服务端缺少“期望 speech profile revision”的语义 CAS。旧 revision 虽有迟到结果 guard，但未主动取消 pending/failed 子任务。已有重试按钮没有本地 busy/spinner，快速重复点击时反馈不清晰。
- 状态：`verified（仓库与本机运行态）`。
- 计划修改：扩展 speech-profile 命令的语义 revision guard；在同一事务取消旧 revision 未完成工作并审计 supersede 关系；修正 SpeechBuild cancelled 投影；增加整库/单题重试 busy spinner；补并发、取消、迟到结果及 React 防重回归，同步 API/领域/存储/进度文档和生产 bundle。
- 实际修改文件：`app/schemas/api.py`、`app/api/routers/catalog.py`、`app/services/{knowledge_base_speech,catalog}.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-{Cvf-GtxJ.js,FhSZ_GOI.css}`、`tests/test_knowledge_base_speech.py`、`docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与诊断：API 日志确认同一页面先有一次 speech-profile `PUT 202`，随后密集出现四次 `409 PERSISTENCE_CONFLICT`，之后再次 `202`；根因是旧页面用会被语音子任务进度推进的 KnowledgeBase 通用 version 保护“切换语音配置”命令。现新增 `expected_speech_profile_revision` 语义 CAS：仅后台进度导致通用 version 变化时允许切换，真实的并发模型/声音配置变更仍返回冲突。切换在同一事务中创建新 revision、新整库构建并对旧 revision 的 pending/failed/running 父子工作分别执行取消或 `cancel_requested`；供应商调用迟到后在资产提交前再次校验取消标志、Question source version 和当前 profile revision，旧结果只记为 superseded，不覆盖新语音。页面配置弹窗明确提示会停止旧任务；整库和单题人工重试都增加 spinner、禁用与严格防双击；活动构建自动刷新进度。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `235 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `37 passed`；`npm run build` 成功生成 `index-Cvf-GtxJ.js/index-FhSZ_GOI.css`；`git diff --check` 通过。新增后端回归覆盖“旧通用 version + 当前 profile revision”切换成功、旧 running 工作收到取消、旧构建投影为 superseded、真实旧 profile revision 仍 409；前端回归覆盖整库/单题重试期间 spinner、disabled 和双击仅发一个请求。本机 API 已重启为 PID `10336`，`GET /healthz` 返回 ok，首页引用新 bundle；Celery 已重启并连接 Redis DB 2，dispatcher 返回 `dispatched: 0`。目标题库 `kb_a9f8e46e4bdf4d20` 当前为 speech profile revision 7、Qwen/Cherry，投影 `ready 10/10`、失败数 0。
- 未完成事项或恢复说明：无代码未完成事项。首次 React 回归因编辑时多出一个 `};` 发生语法失败，删除后全量通过；一次新增后端断言误选了最后一条 HTTP 审计而不是目标领域审计，改为按 action 查找后通过；两次失败均仅发生在测试环境，无持久数据副作用。浏览器若仍缓存旧 bundle，需要强制刷新后再验证新交互。

## 2026-08-31 · DURABLE-MODEL-RETRY-PROJECTION-001

- 目标：修复模型调用发生可重试异常时，持久任务尚未耗尽却提前把领域对象和批量构建投影标记为终态失败的问题；确保修复依赖统一 Outbox 终态语义，而非绑定 Qwen、GLM 或某一模型类型。
- 关联问题：题库 TTS 子任务首次失败后会被 Outbox 自动重试，但 Question 同时写入 `speech_status=failed` 并推进 version；下一次供应商调用即使成功，也会因 source version 过期被判为 `superseded`，形成 6/10 生成且无法自然恢复。构建投影还把仍可 claim 的 `failed` 工作误计为终态失败。
- 状态：`verified（仓库与本机运行投影）`。
- 计划修改：以 Outbox 返回的 `dead_letter` 作为领域失败唯一判据；可重试 `failed` 保持领域对象生成态，并在构建投影中归入 pending/retrying；修正题目行级展示与试听能力；审计其他模型驱动任务是否存在相同的提前终态写入；补后端/前端回归、生产 bundle 和相关设计文档。
- 实际修改文件：`app/services/{catalog,interviews,knowledge_base_speech,talent}.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`tests/test_{knowledge_base_speech,interview_session_aggregate}.py`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-kBIiQffi.js`、`docs/{architecture,api-design,domain-model,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与诊断：持久 SQLite 显示 revision 5 的 10 个 Qwen TTS 子工作中，4 个首轮错误为 `Provider rate limited the request.`，Outbox 正确启动第 2 次 attempt；旧代码却先把这 4 道 Question 写成 failed 并把 version 9 推进到 10，导致成功重试被 source-version guard 判为 superseded，最终稳定为 6 ready / 4 failed。现在仅 `dead_letter` 可推进领域失败；可重试 `failed` 在 SpeechBuild 计为 pending，TTS 不修改 source Question，题库导入、Resume Review/经历题、评分和报告同步使用该终态门禁。整库人工重试以当前 build manifest 与 Question failed 真相交集选题，因此能恢复旧 worker 已误标 completed/superseded 的 4 条历史数据。React 以 KnowledgeBase 终态显示整库重试入口，构建期间不再禁用已 ready 题目的试听。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `234 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `37 passed`；`npm run build` 成功生成 `index-kBIiQffi.js/index-cXhBMl4B.css`；`git diff --check` 通过。新增故障注入回归验证任意 Provider 的 retryable TTS 首次失败后 Question version/source_version 保持一致，第 2 次 attempt 产生 ready 资产且不是 superseded；另有回归验证历史 self-superseded 子工作可新建重试批次。本机重启 API 为 PID `5799`，`GET /healthz` 返回 ok，首页已引用 `index-kBIiQffi.js`，当前 revision 5 投影为 `6 ready / 4 superseded`而不再假装运行中，KnowledgeBase 仍如实保留 `4 failed` 供页面显示恢复按钮。旧 Celery 主进程 PID `2781` 已优雅停止，新 worker/beat 主进程 PID `6585` 以 prefork concurrency 10 连接 Redis DB 2，连续 dispatcher 均返回 `dispatched: 0`，确认未隐式触发任何外部模型任务。
- 未完成事项或恢复说明：未自动提交用户现有 4 条失败语音的真实 Qwen 重生成，避免未经用户点击就消耗外部模型额度；页面硬刷新后可点击“重试失败语音”只排队这 4 道。首次前端测试在仓库根目录执行因无 `package.json` 返回 ENOENT，一次定向 pytest 引用不存在的 `test_mvp_closed_loop.py` 而未运行；两者均无文件/数据副作用，在正确目录/测试集重跑后全部通过。

## 2026-08-31 · MODEL-AGNOSTIC-CONCURRENCY-001

- 目标：把模型任务的版本冲突防护从题库 TTS 个案提升为供应商无关、模型类型无关的统一并发合同，覆盖 LLM、Embedding、STT、TTS、实时语音和数字人配置，以及当前有人工命令的模型驱动异步工作流。
- 关联问题：模型/连接探针和后台模型工作会合法推进 ModelConfiguration、ProviderConnection、QuestionGenerationBatch、ResumeReview、Question 等聚合 version；页面弹窗或确认框若长期持有旧 version，会产生与具体供应商无关的 `PERSISTENCE_CONFLICT`。部分命令已有幂等回放优先检查，但前端获取最新 version 和语义变更 guard 仍散落在业务页面。
- 状态：`verified（仓库）`。
- 计划修改：增加通用 latest-version command helper；模型连接/所有模型类型配置的编辑删除、智能生题控制/审核/导入、简历初筛重试和题库语音命令统一采用最新资源 version，并在语义身份变化时失败关闭；审计服务端人工重试是否在 version 校验前识别同一幂等命令；补跨模型类型回归、文档和生产 bundle。
- 实际修改文件：`app/web/core/{concurrency.js,concurrency.test.js}`、`app/services/{model_admin,talent,catalog,knowledge_base_speech}.py`、`app/api/routers/talent.py`、`app/web/src/features/{models,questions,workflow}/Page.jsx`、`app/web/src/App.test.jsx`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-z_CNemdA.js`、`tests/test_{model_configuration_v2,candidate_screening,documented_gap_apis,knowledge_base_speech}.py`、`docs/{architecture,api-design,domain-model,model-provider-plugins,database-and-vector-storage,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与审计：新增供应商/能力无关的 `requestWithLatestVersion`，统一执行“重读最新资源 → 比较命令相关语义身份 → 用最新 version 提交 → 极窄 CAS 冲突再重读一次”。ProviderConnection/ModelConfiguration 增加 `configuration_revision`，配置修改递增，凭据/模型健康探针只推进通用 version；管理页的连接/模型编辑和删除因此覆盖全部当前可执行 `llm/embedding/stt/tts/realtime_speech/avatar` 类型，而非 Qwen/GLM 特例。智能生题 stop/resume/retry/chunk、草稿编辑/删除/单题及整批导入按 execution/draft 身份吸收后台 LLM 进度 version；题目、候选人、简历、初筛复核/重试按各自内容身份处理后台 TTS/摄取/LLM 写入。服务端人工恢复审计确认 QuestionGenerationBatch 已在 version 前检查控制幂等；补齐 KnowledgeBaseSpeechBuild、Question speech regenerate 和 ResumeReview retry，相同命令返回原工作，不同旧命令继续失败关闭。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `232 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `36 passed`；`npm run build` 通过并生成 `index-z_CNemdA.js/index-cXhBMl4B.css`；`git diff --check` 通过。参数化后端回归逐一执行 LLM、Embedding、batch STT、TTS、数字人和 realtime speech 探针，均验证通用 version 增加但 `configuration_revision` 保持 1，配置 PATCH 后才变为 2；前端单元回归覆盖运行态 version 吸收、读写间单次重试和配置语义变化失败关闭，React 集成回归验证连接探针从 version 3 推进到 5 后删除提交 version 5。
- 未完成事项或恢复说明：仓库内无未完成项；未对真实外部供应商发起调用，额度、网络、音质、WER 和数字人会话仍属部署环境验收。首次在仓库根目录串接 npm 命令因无 `package.json` 返回 ENOENT，无文件副作用；切换 `app/web` 后全量通过。本合同覆盖当前 API/React 中存在人工 CAS 命令的模型驱动流程；未来新增聚合命令必须复用该 helper 与“幂等回放先于 version 校验”合同。

## 2026-08-31 · KB-SPEECH-CONFLICT-RETRY-001

- 目标：修复题库语音配置弹窗因后台语音构建推进 KnowledgeBase version 而提交陈旧 `expected_version` 的冲突，并让整库/单题语音生成失败后可从题库详情真正重新排队生成。
- 关联问题：用户选择 Qwen TTS 时请求携带 version 54，而持久题库已由后台进度更新到 56；已有失败重试会复用 `question.speech:{question}:{version}:{profile_revision}` 幂等键，可能再次得到原 failed/dead-letter 工作项而没有新的可执行任务，React 题库页也没有暴露现有整库或单题重试 API。
- 状态：`verified（仓库）`。
- 计划修改：配置提交前重新读取 KnowledgeBase，仅在 speech profile 未发生并发语义变更时采用最新 version；极窄竞态最多重新校验并重试一次。整库失败重试创建新的工作身份，单题失败暴露既有重新生成命令，补充失败项投影与 React 重试入口；更新 API/领域/存储/进度文档、自动化测试和生产 bundle。
- 实际修改文件：`app/services/knowledge_base_speech.py`、`app/web/src/features/questions/Page.jsx`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-B7aTUrcS.js`、`tests/test_knowledge_base_speech.py`、`app/web/src/App.test.jsx`、`docs/{api-design,domain-model,database-and-vector-storage,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现与诊断：持久 SQLite 事实显示 Qwen profile revision 3 已保存，10 个题目语音子工作最终全部 completed；报错请求仍携带页面旧 version 54，而后台构建已把 KnowledgeBase 推进到 56，因此该次失败是 CAS 防覆盖生效，不是 Qwen/Cherry 被 Provider 拒绝。React 保存前重读题库，只在 profile 语义身份未变时吸收新 version，极窄竞态按相同 guard 重试一次；真正并发修改 profile 时刷新并要求确认。SpeechBuild 投影返回 `failed_items`，题库/题目分别提供失败重试按钮；整库重试使用当前 failed Question version，并以 retry parent 区分新子工作，避免旧 source version 被 guard 跳过或复用 dead-letter 幂等项。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `225 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `34 passed`；`npm run build` 通过并生成 `index-B7aTUrcS.js/index-cXhBMl4B.css`；`git diff --check` 通过。新增后端回归验证失败落库推进 Question version 后，重试 manifest 使用当前 version、新 child 使用 `question.speech.retry:*` 且最终 ready；React 回归验证页面 version 54、后台 version 56 时提交 56，以及整库/单题失败重试请求。首次在仓库根目录执行 npm 测试因无 `package.json` 返回 ENOENT，无文件副作用；切换 `app/web` 后通过。
- 未完成事项或恢复说明：仓库内无未完成项；本次未调用真实 Qwen/GLM TTS，也未启动本地 8000 服务。目标部署仍需用真实凭据验证音频质量、限流和网络稳定性；这些外部验收不改变本次 CAS/重试根因与仓库修复。

## 2026-08-31 · APPOINTMENT-DEFERRED-RESUME-SPEECH-001

- 目标：统一岗位题与简历经历题的读题语音特征；简历题批准时不再调用 TTS，改为计划冻结所选题库的唯一语音特征，候选人完成身份核验和明确同意、预约进入 `registered` 后才异步生成本场简历题语音。
- 关联问题：ExperienceQuestion 当前使用 `voice_default_cn` 和组织默认 TTS 路由，不能保证与题库 KnowledgeBaseSpeechProfile 一致；批准即生成会为拒绝邀请或不参加面试的候选人产生无效成本。现有邀请与开始共用语音 readiness，若只移动调用时机会造成邀请前等待尚未触发的语音任务。
- 状态：`verified（仓库）`。
- 计划修改：为 InterviewPlan 冻结单一语音特征并拒绝多题库 profile 冲突；ExperienceQuestion 批准后进入延迟生成状态；CandidateIntake 成功时原子创建预约范围语音工作，复用匹配资产；拆分 `can_invite/can_start` 门禁并在取消预约时协作取消未完成工作；会话只消费预约准备完成的冻结资产。同步后端/前端测试及架构、接口、领域、检索、Provider、存储、进度、路线图和统一语言文档。
- 实际修改文件：`app/domain/{speech_profile,appointment_speech,appointment_admission}.py`、`app/services/{plan_assembly,talent,appointments,catalog,interviews}.py`、`app/web/src/core/ui.jsx`、`app/web/src/features/{workflow,candidate}/Page.jsx`、重建后的 `app/web/dist/index.html` 与 bundle、`tests/test_{plan_assembly,appointment_reminders,candidate_screening,position_resume_appointment_flow}.py`、`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,development-progress,implementation-roadmap}.md`、本日志。
- 实际实现：ExperienceQuestion 新建为 `not_requested`、批准为 `deferred`，不再保存默认音色或在批准请求中调用 TTS。InterviewPlan 冻结包含题库 revision 映射的唯一 speech profile 指纹并拒绝多题库冲突；Candidate Intake 成功事务创建预约级幂等语音工作，worker 只更新 `InterviewAppointment.speech_preparation`，按完整 profile 复用资产且不覆盖 ExperienceQuestion。邀请/start readiness 分离，取消预约协作取消工作；会话创建只接受预约 ready 资产并冻结 profile/preparation 证据。React 同步展示“预约后生成”和确认后的语音准备提示。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `224 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `33 passed`；`npm run build` 通过并生成 `index-VnSd0Y0L.js/index-cXhBMl4B.css`；`git diff --check` 通过。端到端测试验证确认前无简历题 TTS、确认后 queued、完成前禁止 start、完成后资产的模型/version/音色/语言/格式/语速/题库 revision 指纹与计划一致，且会话快照引用预约资产。
- 未完成事项或恢复说明：无仓库内未完成项；本次未执行数据迁移或真实外部 TTS 调用。真实供应商费用、延迟、失败率和目标部署 worker 调度仍按既有生产环境验收边界执行。

## 2026-08-31 · MODEL-STREAM-PROBE-001

- 目标：修复 DashScope `stt.streaming` 模型配置测试把静音样本误判为模型故障，以及 `speech.dialogue_realtime` 缺少统一测试协议的问题；同步校正 Qwen 3.5 Omni Realtime 当前官方 session 事件结构。
- 关联问题：`qwen-audio-3.0-asr-flash-streaming` 已完成鉴权和 `task-started` 仍因静音没有 final transcript 而失败；`qwen3.5-omni-flash-realtime` 在发起厂商调用前直接返回 `MODEL_CAPABILITY_NOT_IMPLEMENTED`。旧 Qwen session payload 还使用兼容字段、旧输入转写模型名和不适用于 Qwen 3.5 的默认音色。
- 状态：`verified（仓库与本机）`。
- 设计边界：模型配置/路由测试验证真实端点、凭据、模型授权及 session 握手，返回 `probe_mode=handshake` 后主动关闭，不把静音伪装成识别质量样本；WER、final、首音、音质和打断仍由真实录音/面试端到端验收。Realtime Prompt 继续由 `app/core/prompt/` 提供，Provider 只转换厂商协议。
- 实际修改文件：`app/services/model_admin.py`、`app/providers/realtime_speech.py`、`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、`tests/test_model_configuration_v2.py`、`tests/test_dashscope_provider.py`、`docs/api-design.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、本日志。
- 实际实现：模型配置测试和 route 测试共享 `_probe_stream_handshake`，`stt.streaming` 与 `speech.dialogue_realtime` 只消费统一 ready 事件并安全 abort；补齐实时语音对话探针请求。DashScope Qwen 3.5 session 改为嵌套 PCM format，输入转写默认 `qwen3-asr-flash-realtime`、音色默认 `Tina`，历史 `qwen3-asr-flash`/`Cherry` 在 Provider seam 兼容；每个受控追问先更新 session instruction，再提交音频并创建 response。
- 验证命令与结果：定向 Realtime/DashScope/模型配置回归 `23 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `221 passed, 5 skipped`；`cd app/web && npm test -- --run` 为 `33 passed`；`npm run build` 通过并生成 `index-DPD89s-E.js/index-cXhBMl4B.css`；`git diff --check` 通过。重启 API 后，对 `model_cfg_47b17fd770184f74` 的真实 DashScope 测试返回 `probe_mode=handshake + stream.ready`，对 `model_cfg_4b6f183ae80c44e3` 返回 `probe_mode=handshake + dialogue.ready`；两项 ModelConfiguration 均已保存为 `ready`。
- 未完成事项或恢复说明：本次真实探针只确认百炼端点、当前 API Key、模型授权和 session 参数可用，不声称已完成 WER、真实 final/首音延迟、打断、音质、长连接稳定性或费用验收；这些仍需脱敏语音样本和候选人端到端压测。既有 API Key 未输出、未改写。

## 2026-08-31 · MODEL-FORM-HELP-UX-001

- 目标：在模型服务动态表单的字段标签旁增加圆形问号帮助入口；为阿里云百炼实时语音对话 WebSocket 等容易误填的模型配置字段提供鼠标悬停/键盘聚焦说明，降低管理员配置成本。
- 关联问题：用户在添加 DashScope `realtime_speech` 模型时不知道 `Realtime WebSocket` 字段用途和填写规则。
- 状态：`verified`。
- 实际修改文件：`app/web/src/core/ui.jsx`、`app/web/styles.css`、`app/web/src/features/models/Page.jsx`、`app/providers/dashscope/provider.json`、`app/web/src/App.test.jsx`、重建后的 `app/web/dist/index.html` 与 `app/web/dist/bundles/index-DPD89s-E.js/index-cXhBMl4B.css`、本日志。
- 实际实现：`Field` 的 `hint` 统一渲染为标签旁可聚焦圆形 `?`，hover/focus 时显示 tooltip，并保留 `title/aria-describedby/role=tooltip`。DashScope `workspace_id` 与 `realtime_speech` 模型配置字段补充面向管理员的填写说明，明确 `Realtime WebSocket` 是 Qwen Realtime 服务端地址、何时可留空、北京/新加坡地址格式以及系统会自动追加模型参数。模型服务页同步把 `speech.dialogue_realtime` 和 `realtime_speech` 显示为中文能力/类型名称。
- 验证命令与结果：`cd app/web && npm test -- --run`：`33 passed`；`cd app/web && npm run build`：通过，生成 `index-DPD89s-E.js/index-cXhBMl4B.css`；浏览器实测 `http://127.0.0.1:5173/web/#models/provider_conn_42860516bcdd45a0` 添加 realtime 模型时，`Realtime WebSocket` 标签旁出现 `?`，tooltip 从隐藏变为可见并展示新说明；`curl /api/v1/admin/model-provider-connections/provider_conn_42860516bcdd45a0/model-catalog` 已返回 `help` 文案；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_model_form_help_pycache .venv/bin/python -m compileall -q app` 通过；`git diff --check` 通过。
- 未完成事项或恢复说明：无。此变更只影响动态表单展示文案和生产 bundle，不新增 API、数据结构、模型路由或供应商调用逻辑，因此无需更新接口、领域模型或路线图状态。

## 2026-08-30 · REALTIME-SPEECH-DIALOGUE-001

- 目标：在保留现有固定题、流式/批量 STT、TTS、自研/云数字人和完整评分链路的前提下，新增供应商无关的实时语音对话（S2S/STS）能力；接入 OpenAI Realtime 和阿里云百炼千问 Realtime，评估火山引擎豆包实时语音并在无法完整实现其官方二进制协议时保留明确 TODO；实现低延迟、受预算约束、可审计的动态澄清追问，并修复候选人实际面试中的 PCM 录音、断线修复、自动播题、状态同步、心跳、媒体权限和重复提交问题。
- 关联问题：动态追问元数据未进入 execution、评分建议没有运行时消费者；评分/报告同步阻塞下一题；浏览器 `audio/pcm` 与录音存储 MIME 合同冲突；STT 断线未按文档执行 batch repair；企业 `/live` 收不到正常转写/评分广播；数字人不自动主持；候选人忽略会话/题目事件且没有心跳；`record_video=false` 仍强制摄像头；读题与录音可并发、实时与降级提交语义不一致。
- 状态：`verified（仓库）`；真实厂商联调为 `environment_pending`，豆包 adapter 为显式 `TODO/not implemented`。
- 设计边界：实时语音对话只负责回合检测、低延迟澄清追问和音频输出；权威转写、题目快照、CandidateAnswer、异步完整评分与报告继续作为证据链真相。追问是原题的子轮次，必须有父轮次、原因、目标关键点、深度/总量/时间预算，不能独立增加计划权重或根据受保护属性改变难度。Provider 凭据只通过现有 ProviderConnection/ModelConfiguration/ModelRoute 注入，仓库默认留空。
- 计划修改：先新增统一实时语音对话 schema、stream interface、路由能力和 Provider manifest/adapter；随后扩展 Plan Assembly、Interview Lifecycle、Interview Service、WebSocket 与候选人/企业 React runtime；补齐 Prompt 合同、离线 Provider 合同、生命周期/端到端/前端测试，并同步架构、API、领域、检索评分、Provider、存储、问题、进度、路线图、统一语言及 ADR。
- 实际修改文件：
  - 统一能力、Schema 与 Prompt：`app/model_gateway/{capabilities,schemas,gateway,registry,dialogue}.py`、`app/core/prompt/realtime_dialogue.py`。
  - Provider：新增 `app/providers/openai/` 与共享 `app/providers/realtime_speech.py`；扩展 `app/providers/{dashscope,mock}/provider.py` 和 manifest。OpenAI 实现官方 HTTP Chat/Embedding/TTS/batch STT 与 Realtime WebSocket；DashScope 实现 Qwen Realtime 路由与 PCM 事件归一化；火山引擎未修改为 implemented。
  - 领域与编排：`app/domain/interview_lifecycle.py`、`app/services/{evaluation,interviews,streaming_stt,plan_assembly,appointments}.py`、`app/workers/outbox.py`、`app/schemas/api.py`。新增父子追问轮次/预算、确定性批准、异步评分、状态/事件和 `cascade/s2s` 预约设置。
  - 媒体与 Web：`app/adapters/{local_media,private_media}.py`、`app/transport/realtime.py`、`app/api/routers/realtime.py`、`app/web/candidate/{pcm-stream,runtime}.js`、`app/web/src/features/candidate/Page.jsx`、预约表单、行为测试、样式和重建后的 `app/web/dist/`。
  - 测试：新增 `tests/test_{realtime_speech_dialogue,openai_realtime_provider}.py`；扩展 DashScope、实时媒体、私有候选人媒体、生命周期、预约长流程和 MVP 闭环测试。
  - 文档：`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 实际实现：同一候选人 PCM 可并行进入权威 STT 与可选 S2S 表达轨；S2S 只流式逐字播报服务端已批准的追问，不自行决定问题，也不成为评分证据。权威 STT final 原子保存 CandidateAnswer 与 `answer.evaluate` 工作项后立即返回；worker 完整评分并追加 evaluation/report revision。追问固定零权重、深度 1、每根题最多 1 次、全场默认最多 2 次，并检查文本长度与剩余时间。浏览器实现 PCM delta 播放、完整本地备份、断线 batch repair、自动播题、心跳、媒体权限降级和防重复；角色投影不泄漏追问目标、标准答案或评分。Realtime transport 还把 commit 超时/断连统一映射为可观察 ProviderError；任何已输出音频必须同时提供可与批准题干比对的 final transcript，否则立即关闭 S2S 并走 cascade。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_realtime_dialogue_pycache .venv/bin/python -m compileall -q app tests` 通过；Realtime/OpenAI/媒体定向回归为 `12 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 最终为 `220 passed, 5 skipped in 10.02s`；`cd app/web && npm test` 为 `32 passed`；`npm run build` 成功，生成 `index--8cvjCid.js/index-Kmpcg04A.css`；`git diff --check` 通过。
- 失败与恢复留痕：首次前端测试误加 Vitest 不支持的 `--runInBand` 参数，去掉后通过；首次后端全量回归有 1 个旧 MVP 断言仍同步读取 score，按新合同改为验证 `pending/work_item_id`、执行 worker 后读取 append-only evaluation，复跑全量通过。一次只读 `rg` 命令中的 Markdown 反引号被 shell 当成命令替换并报告 `command not found: 106`，未写入仓库或业务数据，随后使用安全查询完成检查。首次暂存审计发现 4 个拆分后的 router 文件末尾多一个空白行，`git diff --cached --check` 失败；删除多余空白、重新暂存后通过，不影响业务数据。
- 未完成事项或恢复说明：OpenAI、阿里云和火山引擎真实凭据、区域/模型权限、实际首音、打断、网络抖动、费用与音质尚未提供；OpenAI/DashScope route 在管理员完成连接、探针和目标环境验收前不得视为生产 ready。火山引擎豆包虽有官方端到端实时语音能力，但其二进制 StartConnection/StartSession adapter 和“严格保持批准追问文本”尚未完成，本轮明确保留 TODO，没有伪造兼容实现或开放活动 route。

## 2026-08-29 · QUALIFIED-RESUME-QUESTIONS-UX-001

- 目标：只为生效初筛结论为“符合”的候选人生成简历问答；AI 不符合/待复核时不生成，人工复核改为符合后再排队生成。每道 AI/人工题必须绑定来自该份 ResumeReview 的具体简历证据快照。将个人题库从初筛报告长弹窗移出，在候选人表格外层提供“简历问答”弹窗入口，编辑/删除收入题目三点菜单。
- 关联问题：现有 Resume Review 无论符合与否都在同一模型响应中生成并持久化 ExperienceQuestion，且 `evidence_refs` 可为空；React 把 CandidateQuestionBank 放在初筛/简历详情弹窗中部，查找成本高，题目操作按钮又全部平铺。
- 状态：`verified`（仓库与本地运行时）。
- 设计决策：将经历题生成从 Resume Review 最终响应拆为独立的 `resume.experience_questions.generate` 持久工作，复用已配置的 `resume_experience_question_generation` purpose；候选人生效结论不是 `qualified` 时不排队，转为符合时原子排队。生成 module 只接受审阅中的项目/技能证据，Schema 强制每题至少一个精确证据标签，持久前再解析为不可变证据快照。
- 实际修改文件：`app/core/prompt/contracts.py`、`app/providers/mock/provider.py`、`app/services/{resume_review,talent,plan_assembly}.py`、`app/workers/outbox.py`、`app/schemas/api.py`；`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css` 及重建后的 `app/web/dist/`；`tests/test_{candidate_screening,prompt_governance,resume_review_pipeline,position_resume_appointment_flow}.py`；同步 `CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 实际实现：Resume Review 升级为 `resume_review.v6` / `resume_review_reduce.v4`，只返回证据与初筛，彻底移除同响应经历题；只有生效结论 `qualified` 才原子排入独立 `resume_experience_question_generation.v1` 工作，AI 不符合/待复核停在资格门禁，人工改判符合或重试失败/存量未生成状态才排队。AI Schema 强制 1–3 题和精确 evidence label，Talent module 继续校验引用归属、题干点名并冻结证据文本/来源页；人工新建/编辑同样必须提交证据标签并在题干写出标签。读取、语音和计划装配过滤不符合审阅、无证据快照或题干未点名的存量题；冻结计划同时保留 `evidence_refs`。React 从初筛详情移除题库，在候选人表格增加独立“简历问答”弹窗入口和生成状态；新建表单必须选择简历依据，卡片展示依据，题目及候选人的编辑/删除收入三点菜单。
- 验证命令与结果：定向资格/Prompt/长流程回归最终 `24 passed in 2.25s`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `212 passed, 5 skipped in 9.26s`；前端 `npm test -- --run` 为 `32 passed`；`npm run build` 成功；`compileall` 与 `git diff --check` 通过。停止旧服务 PID `23678`，最终以最新代码启动 PID `28105`；`GET /healthz` 为 200，用户指定候选人的个人题库 API 为 200 空集合，符合其当前生效结论 `unqualified` 和 `question_generation_status=not_eligible`；首页实际加载新 bundle `index-C3iURwrH.js/index-Kmpcg04A.css`。
- 失败与恢复留痕：首次定向长流程测试仍假定审阅完成后无条件已有题，改为显式人工复核符合并运行独立工作；首次 React 断言把折叠菜单中仍存在但不可见的 DOM 按钮误判为平铺按钮，改为验证 details 默认关闭、点击三点后展开。两次均只影响测试断言，没有业务数据副作用。文档批量补丁有两次因上下文不匹配未应用，随后拆成小补丁完成，没有产生部分写入。
- 未完成事项或恢复说明：没有自动调用付费真实模型为历史合格审阅补生成问题；再次“复核为符合”会为旧的未生成状态排队。真实 DeepSeek 的新问题质量、时延和费用仍为 `environment_pending`。上传失败日志已确认是旧调用 `deepseek-v4-pro` 在 6000 输出 token 中消耗 4081 reasoning token 后 `finish_reason=length` 导致 JSON 截断；该历史审阅后来已恢复为 `ready_for_review`，本轮没有重放付费审阅。

## 2026-08-29 · RESUME-REVIEW-TRUNCATION-001

- 目标：在保留最新 API router/transport 拆分的前提下，修复真实简历审阅因模型输出用尽限额、JSON 未完整而失败的问题；重启本地 API 使新增候选人个人题库路由实际生效。
- 关联问题：`candidate_400e16c5c49f470a` 的个人题库源码路由已存在但 8000 端口运行旧进程，OpenAPI 未加载该路由；`resume_review_0324bc54908047cb` 的真实调用在 `deepseek-v4-pro` 上以 `finish_reason=length` 结束，6000 输出 token 中有 4081 reasoning token，只留下 4905 字符的未完整 JSON。
- 状态：`verified`（仓库与本地运行时）。
- 计划修改：为 Resume Review 最终结构化响应增加数量/长度上界和精简指令、提升 Prompt 版本并补充合同测试；同步检索/Provider 文档；完成后重启 8000 服务并用实际 HTTP 验证健康检查、OpenAPI 与个人题库路由。
- 实际修改文件：`app/core/prompt/contracts.py`、`app/services/resume_review.py`、新增 `tests/test_resume_review_pipeline.py`、修改 `tests/test_prompt_governance.py`；同步 `docs/architecture.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md` 和本日志。用户已有的 `app/api/routers/`、`app/transport/` 及统一响应改造全部保留。
- 实际实现：最终审阅 Prompt 升级为 `resume_review.v5` / `resume_review_reduce.v3`，对摘要、项目/技能证据、命中/缺失要求、告警和 1–3 个经历题同时增加 `maxItems/maxLength`。预算内单次阶段若返回 `provider_output_truncated`，流水线丢弃半截 JSON，自动复用全文的页感知 evidence Map/final Reduce，并记录 `map_reduce_after_output_truncation + fallback_reason`；其他 Provider 错误和 Map/Reduce 内部截断仍保持结构化失败，不做无界成本重试。
- 验证命令与结果：定向 Prompt/降级/初筛测试 `20 passed in 1.57s`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_resume_review_pycache .venv/bin/python -m compileall -q app tests` 通过；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `211 passed, 5 skipped in 9.22s`；`git diff --check` 通过。停止 2026-08-28 22:57 启动的旧 PID `10252`，以 `.venv/bin/python main.py` 启动最新代码 PID `23678`；`GET /healthz` 为 200，OpenAPI 已注册候选人个人题库 GET/POST，用户给出的 `GET /api/v1/candidate-profiles/candidate_400e16c5c49f470a/experience-questions` 实测为 200 并返回 4 道经历题。
- 未完成事项或恢复说明：未自动调用付费真实模型重放已失败的 `resume_review_0324bc54908047cb`，避免在没有用户明确确认时产生额外调用与费用；候选人页保留“重新初筛”入口，下次重试会使用本轮修复。目标 DeepSeek 账户的实际恢复质量、时延和费用仍为 `environment_pending`。

## 2026-08-29 · API-ROUTER-MODULE-SPLIT-001

- 目标：修正 `app/api/routes.py` 仍集中全部路由、且 transport 支撑实现混入 `app/api` 的结构问题；让 `app/api` 只承担按业务域组织的路由声明和总装。
- 关联问题：上一工作项虽然抽离了 response、module 定位和 realtime implementation，但仍把这些文件放在 `app/api`，并保留约 1340 行单体路由注册表，没有达到用户期望的维护性和目录清晰度。
- 状态：`verified`（仓库）。
- 计划修改：把 fields/responses、module locator、实时连接管理移动到 `app/transport/`；把路由按 system、admin、catalog、talent、plans、interviews、realtime 拆入 `app/api/routers/`；`app/api/routes.py` 只保留 router 总装；保持所有路径、状态码、字段和错误合同不变。
- 实际修改文件：新增 `app/api/routers/{__init__,system,admin,catalog,talent,plans,interviews,realtime}.py`；把 `app/api/dependencies.py` 移为 `app/transport/service_locator.py`、`app/api/realtime.py` 移为 `app/transport/realtime.py`、`app/api/responses.py` 与 `app/api/fields/` 移为 `app/transport/http/responses.py` 与 `app/transport/http/fields/`，并新增 transport package 初始化文件；把 `app/api/routes.py` 重写为 13 行总装入口；同步修改 `app/main.py`、`app/core/{auth,errors,rate_limit}.py`、`tests/test_api_responses.py`、`docs/{architecture,api-design,development-progress,change-log}.md`。
- 实际实现：141 个 HTTP/WebSocket route 声明按 system、admin、catalog、talent、plans、interviews、realtime 七个业务 router 物理拆分；总装入口只按原顺序 include 子 router。`app/api` 现在只含 route package 和总装，不再含 fields、response factory、module locator 或实时连接 implementation。新增结构合同测试约束 router 清单、总装文件规模和 transport 文件不得回流 `app/api`；所有 URL、状态码、公开字段白名单和错误格式保持不变。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_router_split_pycache .venv/bin/python -m compileall -q app tests` 通过；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `210 passed, 5 skipped`；OpenAPI 成功生成 `107` 个 HTTP path，应用共注册 `150` 个含静态资源/WebSocket 的 route；`npm test -- --run` 为 `32 passed`；`npm run build` 成功；`git diff --check` 通过。
- 失败与恢复留痕：首次结构测试发现移动源码后遗留了空目录 `app/api/fields/`，按目标结构用 `rmdir` 删除空目录后全量测试通过；首次 OpenAPI 单行检查因 shell 中 f-string 转义产生 SyntaxError，随后改用 `%` 格式化通过；一次在 `app/web` 工作目录误用根目录相对 `.venv/bin/python` 返回“文件不存在”，随后回到仓库根目录执行。三次失败均未修改业务数据或外部状态。
- 未完成事项或恢复说明：无仓库内遗留。现有未提交的 `CANDIDATE-QUESTION-BANK-001` 与 `API-RESPONSE-MARSHAL-001` 修改全部保留，未执行 reset、checkout 或清理。

## 2026-08-29 · API-RESPONSE-MARSHAL-001

- 目标：审查并改善 `app/api` 的维护性与可读性；参考 `zfsoft-agent-platform/api/fields` 的声明式白名单投影，为 FastAPI 建立统一 response/marshal seam，在不破坏现有成功响应兼容形状的前提下统一集合、异步和错误响应，并让公开候选人投影不再由路由手工拼字段。
- 关联问题：`app/api/routes.py` 同时承担 REST 路由、WebSocket 连接管理、module 定位和响应序列化；JSON 返回普遍使用 `Dict[str, Any]`，缺少可复用字段合同；业务错误使用 `error` 包络，而请求校验错误仍使用 FastAPI 默认 `detail`；同类列表和 202 响应存在重复拼装。
- 状态：`verified`（仓库）。
- 计划修改：新增 `app/api/fields/` 声明式字段与 marshal 实现、统一 `app/api/responses.py` 响应工厂和错误处理；迁移重复列表/异步响应及安全敏感公开投影；增加合同测试并更新接口设计与架构说明。
- 实际修改文件：新增 `app/api/dependencies.py`、`app/api/realtime.py`、`app/api/responses.py`、`app/api/fields/__init__.py`、`app/api/fields/core.py`、`app/api/fields/common.py`、`app/api/fields/public_interview.py`、`tests/test_api_responses.py`；修改 `app/api/routes.py`、`app/main.py`、`app/core/errors.py`、`app/core/auth.py`、`app/core/rate_limit.py`、`docs/api-design.md`、`docs/architecture.md`、`docs/development-progress.md` 和本日志。
- 实际实现：以 `ApiJSONResponse` 作为 API router 默认 JSON transport；`api_response/accepted_response/collection_response/error_response` 统一资源编码、202、`items + next_cursor` 和 `error.code/message/details`。声明式 Field/Nested/ListOf 支持 key/attribute、嵌套 source、默认值、类型转换和显式 allow-list；公开候选人详情与答题响应已用 fields 投影，路由不再手写评分裁剪。FastAPI RequestValidationError 固定映射为 `422 REQUEST_VALIDATION_FAILED`，仅返回字段位置、消息和类型，不回显原输入；Provider、Persistence、认证和限流复用同一错误工厂。ServiceLocator 与实时连接/Redis fan-out/候选人事件裁剪分别移出 routes，并在 routes 中增加领域分区标识。
- 兼容决策：没有给成功资源强加 `data` 二次包络，既有前端仍直接读取资源字段；列表只统一补齐 `next_cursor`，二进制/音频/CSV 不进入 JSON marshal。这样获得统一 seam 与字段白名单，同时避免对现有 `/api/v1` 客户端做破坏性版本迁移。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_api_response_pycache .venv/bin/python -m compileall -q app tests` 通过；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `209 passed, 5 skipped`；定向 API/主流程/实时/限流/鉴权/候选人回归为 `31 passed`；`npm test -- --run` 为 `32 passed`；`npm run build` 成功；`git diff --check` 通过。
- 未完成事项或恢复说明：仓库开始前已有 `CANDIDATE-QUESTION-BANK-001` 的代码、文档和前端 bundle 修改，本工作项全部保留，未执行 reset、checkout 或清理。`routes.py` 仍作为全部路径的注册表，后续可按 admin/catalog/talent/interview 物理拆成子 router；本轮已先抽离 response、依赖构造和 realtime 三个高变化 implementation，合同与运行行为无仓库内遗留。

## 2026-08-29 · CANDIDATE-QUESTION-BANK-001

- 目标：把现有 ResumeReview 自动生成的 ExperienceQuestion 提升为候选人可见、可维护的个人题库；保留 AI 基于简历项目/技术描述自动生成追问题的能力，补齐人工新建、候选人范围查询、编辑、批准/拒绝和归档删除，并让正式面试继续复用既有 `resume_experience` 计划、语音和评分链路。
- 关联问题：现有 ExperienceQuestion 已绑定 CandidateProfile 并能由 AI 生成、编辑和进入计划，但只能按 ResumeReview 查询，缺少候选人个人题库入口、人工创建和删除合同，React 候选人详情也未展示这些题目。
- 状态：`verified`（仓库）。
- 计划修改：先更新 API、领域模型和统一语言；随后扩展 ExperienceQuestion schema/TalentService/API，增加 CandidateQuestionBank React 管理区和后端/前端测试，重建生产 bundle；最后运行定向与全量验证并补充实际文件、结果和未完成事项。
- 不变量：CandidateQuestionBank 是 ExperienceQuestion 的候选人范围投影，不复制岗位 KnowledgeBase/Question；人工题必须绑定候选人及其 ResumeReview，评分依据完整后才可批准；归档删除不破坏已批准计划和历史 InterviewQuestionSnapshot；AI 草稿仍需人工批准后才能进入计划。
- 实际修改文件：
  - 合同与领域：`app/schemas/api.py`、`app/domain/enums.py`、`app/api/routes.py`、`app/services/talent.py`、`app/services/catalog.py`。
  - React 与生产 bundle：`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/index.html`、`app/web/dist/bundles/index-B_0U1sv3.js`、`app/web/dist/bundles/index-CWyy4anR.css`；构建替换旧 hash bundle `index-jppRnCoU.js` 和 `index-Nk8y8ETG.css`。
  - 测试：`tests/test_candidate_screening.py`。
  - 同步文档：`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、本日志。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_candidate_bank_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`205 passed, 5 skipped in 9.46s`。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_candidate_screening.py -x`：最终定向复验 `9 passed in 1.23s`；覆盖 AI/人工来源、候选人范围查询、新建、编辑、批准与语音 ready、拒绝、归档、审阅范围隐藏和审计。
  - `cd app/web && npm test`：`32 passed`；新增行为测试覆盖 AI 题展示、人工新建、批准和删除。
  - `cd app/web && npm run build`：通过，生产 bundle 已更新。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：个人题库复用现有 Resume Review Prompt，本轮未新增或修改业务 Prompt。真实简历追问题质量、真实 TTS 音质和目标模型/对象存储仍沿用既有 `environment_pending` 边界；仓库内功能无未完成项。首次直接调用 `python` 因当前环境仅提供 `.venv/bin/python` 而失败；首次从 `app/web` 使用根目录相对测试过滤 `app/web/src/App.test.jsx` 未匹配文件，改为 `src/App.test.jsx` 后通过。两次失败都未修改业务数据或产生额外仓库副作用。

## 2026-08-28 · DUAL-AVATAR-DELIVERY-001

- 目标：保留现有腾讯云数字人 WebRTC/SFU 链路并标注后续扩展 TODO；新增预约级“自研数字人 / 云数字人”选择。自研模式复用计划冻结的题目 TTS 私有音频，在浏览器本地完成形象渲染和口型状态，不创建云数字人会话；两种模式共用 Avatar Delivery interface、候选人播放 runtime、失败降级与会话清理。
- 关联问题：云数字人按会话/并发计费过高；现有预约只能隐式走单一数字人 route，无法按场次选择低成本本地渲染；冻结题目音频的内部 URI 不能直接作为候选人浏览器播放地址。
- 状态：`verified`（仓库）；高精度本地口型/3D 与腾讯真实环境均按下述边界继续扩展。
- 计划修改：预约 settings 合同与会话安全投影、Avatar Delivery 深模块、候选人本地数字人播放/动画、预约选择 UI、后端与 React 测试、生产 bundle，以及架构/API/领域/Provider/进度/路线图文档。
- 兼容与安全边界：不删除或改写腾讯云 Provider、WSS 命令通道、TCPlayerLite 页面和关闭接口；历史预约没有 `avatar_mode` 时继续按云模式解释。新预约默认自研模式但可显式选择云模式。自研模式只播放当前轮次冻结且已进入 PrivateFileStorage 的题目语音，签发短期受控地址；不暴露对象键、供应商凭据、标准答案或未来题目。
- 实际修改文件：
  - 合同与服务：`app/schemas/api.py`、`app/model_gateway/schemas.py`、`app/services/appointments.py`、`app/services/avatar.py`、`app/services/interviews.py`、`app/api/routes.py`。
  - React 与生产 bundle：`app/web/candidate/avatar-runtime.js`、`app/web/src/features/candidate/Page.jsx`、`app/web/src/features/interviews/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/candidate/avatar-runtime.test.js`、`app/web/dist/index.html`、`app/web/dist/bundles/index-jppRnCoU.js`；构建删除旧 hash bundle `index-BAySj2hk.js`。
  - 验收：`tests/test_realtime_media.py`、`tests/test_production_compatibility.py`。
  - 文档：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`CONTEXT.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、本日志。
- 验证命令与结果：定向 Python 合同/实时媒体 `8 passed in 1.64s`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/python -m pytest -q` 为 `204 passed, 5 skipped in 9.42s`；`cd app/web && npm test -- --run` 为 `31 passed`；`npm run build` 成功并生成生产 bundle；`git diff --check` 通过。测试覆盖预约默认/显式/非法模式、本地浏览器语音、本地冻结音频五分钟签名访问与审计、云不可用明确降级、统一前端音频/浏览器语音/WebRTC lifecycle、云 session close 和预约表单 payload。
- 未完成事项或恢复说明：自研模式当前是低成本 2D 浏览器形象、冻结 TTS 音频、呼吸/说话状态和音量条，不声称已有音素级嘴型或 3D 实时驱动；后续在保留 AvatarDelivery interface 下扩展 Live2D/3D、viseme 或自建 WHEP/SFU。腾讯真实账号、授权形象、并发、媒体质量和费用仍为 `environment_pending`，本轮未写入凭据或发起真实供应商调用。预约 settings 已是 JSON 文档，不需要数据库迁移。首次从仓库根目录执行 `npm test` 因该目录没有 `package.json` 返回 ENOENT，随后在 `app/web` 重跑通过；失败命令没有仓库或业务数据副作用。

## 2026-08-28 · CANDIDATE-BOOKING-REMINDER-001

- 目标：把候选人邀请页从“核验后立即开始面试”拆成“核验身份并确认预约”与“到预约时间后检查设备并进入面试”两步；预约确认后创建面试前 30 分钟的持久邮件提醒任务。
- 关联问题：候选人提前打开邀请链接时会被立即要求设备检查并尝试开始；预约缺少候选人确认态和邮件提醒。
- 状态：`verified`（仓库）；SMTP 真实投递为 `environment_pending`。
- 安全与运行边界：提醒工作项只保存预约 ID，不保存邮箱明文；SMTP 授权码只从环境变量读取且仓库默认留空；未配置时保留可重试事实，不记录或伪造发送成功。
- 实际修改文件：
  - 预约与提醒：`app/services/appointments.py`、`app/services/appointment_reminders.py`、`app/services/interviews.py`、`app/adapters/email.py`、`app/workers/outbox.py`。
  - 候选人页面与构建：`app/web/src/features/candidate/Page.jsx`、`app/web/styles.css`、`app/web/dist/index.html`、`app/web/dist/bundles/index-Cw9rJDVe.js`、`app/web/dist/bundles/index-By4Tl22c.css`。
  - 配置与说明：`.env.example`、`README.md`。
  - 测试：`tests/test_appointment_reminders.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_interview_session_aggregate.py`、`app/web/src/App.test.jsx`。
  - 同步文档：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_appointment_reminders.py tests/test_position_resume_appointment_flow.py -q`：`5 passed in 1.35s`。
  - `npm test -- --run`：首次新增邀请页测试的 GET mock 未匹配 HTTP client 显式的 `method=GET`，导致表单未渲染；修正 mock 后 `29 passed in 0.85s`，无业务数据副作用。
  - `npm run build`：通过，生产 bundle 已更新。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - 首次全量 pytest：`195 passed, 5 skipped, 1 failed`；旧断言要求所有 Outbox 均完成，未包含新增的预约提醒。补充预约消费时协作取消未发送提醒并收紧断言后重跑。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`196 passed, 5 skipped in 8.77s`。
  - 本地浏览器重载 `/#invite/{invalid-token}`：生产 bundle 正常挂载，公开邀请不可用投影正确，控制台无 error/warn；身份确认与不触发媒体/start 的状态迁移由 Vitest mock API 行为测试覆盖，未用真实候选人资料或改写现有预约。
  - 本地运行实例已加载最终代码：旧 8000 监听进程在检查期间已退出，使用 `.venv/bin/python main.py` 启动新 API（PID `87974`），`GET /healthz` 返回 `{"status":"ok"}`。
  - 启动 Celery Worker/Beat 前只读检查 claimable DurableWorkItem 为 `[]`。首次沙箱内启动因不允许连接本机 Redis 而失败并 warm shutdown，无任务副作用；获批后重新启动成功，Worker ready，连续 dispatcher 均为 `dispatched: 0`。Beat 按既有计划运行一次筛选留存扫描，结果 `candidate_count=0`、未删除候选人，但新增一条批次审计 `audit_b5ed4233b5334619`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：SMTP 主机、账号、发件地址和 `INTERVIEWER_SMTP_PASSWORD` 授权码按用户要求保持空白，未发送真实邮件。部署时填写 `.env.example` 对应变量并保持 Celery worker/Beat 运行，即可对随后到期的提醒执行真实投递；还需用目标邮件服务完成发件域名、退信、送达率和合规验收。

## 2026-08-28 · PLAN-INVITATION-UX-001

- 目标：去掉面试计划“创建后再由同一人审批”的重复操作，让 React 工作台的“生成计划”作为一次明确确认并原子产生已批准计划；在候选人邀请弹窗增加一键复制和成功/失败反馈。
- 关联问题：面试计划自审批交互冗余；邀请链接缺少复制操作。
- 状态：`verified`。
- 实际修改文件：
  - 计划生成合同与事务：`app/schemas/api.py`、`app/services/plan_assembly.py`、`app/api/routes.py`。
  - React 交互与样式：`app/web/src/features/plans/Page.jsx`、`app/web/src/features/interviews/Page.jsx`、`app/web/styles.css`。
  - 测试与生产 bundle：`tests/test_plan_assembly.py`、`app/web/src/App.test.jsx`、`app/web/dist/index.html`、`app/web/dist/bundles/index-B94I_kXD.js`、`app/web/dist/bundles/index-CW8Qvsa4.css`。
  - 同步文档：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - 首次从 `app/web` 目录组合运行后端与前端定向测试时，`.venv/bin/python` 因相对路径错误未找到；未执行测试、无仓库副作用，改回仓库根目录后重跑。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_plan_assembly.py -q`：`4 passed in 0.64s`。
  - `npm test -- --run`（`app/web`）：`28 passed in 1.00s`，覆盖生成即启用、无自审批按钮和一键复制邀请链接。
  - `npm run build`（`app/web`）：通过，生产 bundle 已更新。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`194 passed, 5 skipped in 8.53s`。
  - 本地浏览器验证 `http://127.0.0.1:8000/#plans`：页面无“审批计划”按钮；生成弹窗显示一次确认说明和“生成并启用计划”；控制台无 error/warn。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：无。API 默认仍生成 `draft`，供确实需要编辑或职责分离的客户端使用；React 工作台显式传入 `approve=true`，在同一事务完成就绪校验与启用。

## 2026-08-25 · CORE-FIX-001

- 目标：把问题与操作日志加入 AI 强制索引，修复并验证 `PLAN-001`、`CONSENT-001`、`APPOINTMENT-001`、`REPORT-001`、`SEARCH-001`。
- 关联问题：`PLAN-001`、`CONSENT-001`、`APPOINTMENT-001`、`REPORT-001`、`SEARCH-001`。
- 状态：`verified`。
- 实际修改文件：
  - 协作与记录：`AGENTS.md`、`docs/change-log.md`、`docs/known-issues-and-remediation.md`。
  - 计划与执行：`app/services/plan_assembly.py`、`app/services/plans.py`、`app/services/interviews.py`、`app/domain/question_selection.py`、`app/schemas/api.py`、`app/api/routes.py`。
  - 同意与预约准入：`app/services/appointments.py`、`app/domain/appointment_admission.py`。
  - 报告与搜索：`app/services/reports.py`、`app/services/catalog.py`、`app/persistence/interface.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/web/app.js`。
  - 同步文档：`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`。
  - 验收测试：`tests/test_known_issue_remediations.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_mvp_flow.py`、`tests/test_persistence_contract.py`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m pytest -q`：`64 passed in 2.41s`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：五个问题均达到本地 `verified`，但未标记 `closed`。旧 `items` 请求/存量迁移、旧 QuestionService 向量实验和管理员直建会话仍是兼容路径；真实 streaming STT/TTS、PostgreSQL 唯一约束与查询计划、RBAC、审计、签名媒体和 PDF 私有文件链路仍属于后续生产化里程碑。

## 2026-08-25 · DOC-GAPS-001

- 目标：实现强制索引文档中所有尚未完成的能力，补齐代码、API、适配器、迁移/兼容处理、测试和 UI，并按实际验收证据更新完成标记。
- 关联问题：里程碑 0-13 的全部未满足验收项；重点覆盖 Private File Storage、PDF 本地/URL 摄取、题库批量构建、缺失 CRUD、流式 STT/TTS、生产 readiness、RBAC/审计、签名访问、Outbox 加固、PostgreSQL 约束、导出、公平性评估和兼容路径清理。
- 状态：`verified`（仓库范围）。
- 实际修改文件：
  - 协作、领域语言与入口：`AGENTS.md`、`CONTEXT.md`、`README.md`。
  - 设计与进度：`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/known-issues-and-remediation.md`、`docs/change-log.md`。
  - API、领域与 schemas：`app/api/routes.py`、`app/schemas/api.py`、`app/domain/enums.py`、`app/domain/interview_lifecycle.py`、`app/domain/appointment_admission.py`、`app/domain/question_selection.py`。
  - 安全与文件：`app/core/auth.py`、`app/core/rate_limit.py`、`app/core/sensitive_data.py`、`app/file_storage/`、`app/services/private_assets.py`、`app/services/resume_ingestion.py`、`app/services/retention.py`。
  - 业务服务：`app/services/appointments.py`、`app/services/catalog.py`、`app/services/fairness.py`、`app/services/operations.py`、`app/services/review.py`、`app/services/session_monitor.py`、`app/services/streaming_stt.py`、`app/services/talent.py`、`app/services/avatar.py`、`app/services/evaluation.py`、`app/services/interviews.py`、`app/services/model_admin.py`、`app/services/plan_assembly.py`、`app/services/plans.py`、`app/services/realtime.py`、`app/services/reports.py`、`app/services/roles.py`。
  - 模型、实时与 worker：`app/model_gateway/gateway.py`、`app/model_gateway/schemas.py`、`app/model_gateway/streaming.py`、`app/providers/mock/provider.json`、`app/providers/mock/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/openai_compatible/provider.py`、`app/realtime_bus.py`、`app/workers/outbox.py`、`app/main.py`。
  - 数据层与迁移：`app/persistence/interface.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/persistence/postgresql.py`、`app/persistence/provider.py`、`app/repositories/memory.py`、`app/repositories/sqlite.py`、`app/repositories/postgresql.py`、`app/repositories/provider.py`、`migrations/001_postgresql_persistence.sql`。
  - Web 与依赖：`app/web/app.js`、`app/web/index.html`、`pyproject.toml`。
  - 测试：`tests/test_auth_audit.py`、`tests/test_documented_gap_apis.py`、`tests/test_fairness_evaluation.py`、`tests/test_known_issue_remediations.py`、`tests/test_model_invocation.py`、`tests/test_mvp_flow.py`、`tests/test_openai_compatible_provider.py`、`tests/test_persistence_contract.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_postgresql_adapter.py`、`tests/test_production_compatibility.py`、`tests/test_rate_limit.py`、`tests/test_realtime_media.py`、`tests/test_resume_ingestion.py`、`tests/test_retention.py`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `.venv/bin/python -m pytest -q`：`88 passed in 2.64s`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：仓库内可实现项已完成。真实 PostgreSQL/Redis 集群、阿里云 OSS、恶意文件扫描器、真实 STT/TTS/数字人、企业邮件短信通道、WebRTC/SFU 和企业金标数据仍需相应账号、部署环境、供应商选择或业务样本，统一保持 `environment_pending`/`external_choice_required`，不得解释为生产已验收。`DATA-001` 需真实数据库完成 RLS、并发与 `EXPLAIN` 后才能改为 `verified`；`COMPAT-001` 需先迁移旧 SQLite 数据和客户端再删除兼容代码，当前不能标 `closed`。

## 2026-08-25 · COMPAT-CLOSE-001

- 目标：继续完成尚未关闭的仓库工作，迁移并删除旧计划 `items`、管理员直建会话、客户端文本答案和旧向量题库 interface；在本机能力允许时补做 PostgreSQL 真实 adapter 验证。
- 关联问题：`PLAN-001`、`CONSENT-001`、`APPOINTMENT-001`、`SEARCH-001`、`COMPAT-001`、`CANDIDATE-ACCESS-001`、`DATA-001`。
- 状态：`verified`（仓库范围）；`COMPAT-001` 与六项核心问题已 `closed`，`DATA-001` 保持 `in_progress/environment_pending`。
- 实际修改文件：
  - 协作与领域文档：`AGENTS.md`、`CONTEXT.md`、`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/known-issues-and-remediation.md`、`docs/change-log.md`。
  - 计划 execution v2 与迁移：`app/schemas/api.py`、`app/services/plan_assembly.py`、`app/services/plans.py`、`app/migrations/__init__.py`、`app/migrations/plan_execution_v2.py`、`app/services/interviews.py`、`app/services/reports.py`。
  - 旧题库 interface 删除：删除 `app/services/questions.py`；修改 `app/api/routes.py`、`app/workers/outbox.py`、`app/persistence/interface.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/repositories/memory.py`、`app/repositories/sqlite.py`，移除全局题目创建/列表、VectorDocument repository/collection 和索引 worker 分支。
  - 预约、候选人与实时边界：`app/services/appointments.py`、`app/services/realtime.py`、`app/core/rate_limit.py`、`app/web/app.js`。新增 `/#invite/{token}` 登记/同意/设备检查页、候选人 public 窄接口、安全投影、当前轮次媒体范围校验和 HMAC 会话 token；删除直接创建/直接 START、REST/WebSocket 文本答案及前端 fallback。
  - 测试：`tests/test_plan_assembly.py`、`tests/test_interview_session_aggregate.py`、`tests/test_realtime_media.py`、`tests/test_mvp_flow.py`、`tests/test_position_resume_appointment_flow.py`、`tests/test_known_issue_remediations.py`、`tests/test_persistence_contract.py`、`tests/test_sqlite_store.py`、`tests/test_production_compatibility.py`、`tests/test_rate_limit.py`。
- 验证命令与结果：
  - `docker info --format '{{.ServerVersion}}'`：失败，Docker daemon socket 不存在；无仓库副作用，不能执行真实 PostgreSQL 容器验收。
  - SQLite 持久数据只读检查：0 份 InterviewPlan；未改写用户数据。
  - `.venv/bin/python -m app.migrations.plan_execution_v2 --dry-run`：通过，`migrated_plan_ids=[]`、`migrated_session_ids=[]`。
  - 首次 `.venv/bin/python -m compileall -q app tests`：因系统 Python pycache 指向沙箱外用户缓存目录而失败；未修改仓库。改用 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pyc` 后通过。
  - `node --check app/web/app.js`：通过。
  - `.venv/bin/pytest -q`：`84 passed in 2.93s`。
  - `git diff --check`：通过。
- 未完成事项或恢复说明：仓库中不存在待完成的兼容代码或未标记的本地功能。已有旧部署必须先停写，运行 v2 `--dry-run`，备份后再执行正式迁移；当前工作区因没有旧计划/会话而无需执行写迁移。真实 PostgreSQL/RLS/并发/`EXPLAIN`、Redis 双实例、OSS、扫描器、真实 STT/TTS/数字人、邮件短信与 WebRTC 仍需要外部服务、凭据或供应商选择，继续保持文档中的 `environment_pending/external_choice_required`，不标记为生产已验收。

## 2026-08-25 · RUNTIME-RESTART-001

- 目标：按用户要求重启本地前后端服务，并验证后端健康检查、前端工作台与静态资源可访问。
- 关联问题：无；本工作项属于本地运行环境操作，不改变产品功能状态。
- 状态：`verified`。
- 实际修改文件：`docs/change-log.md`。
- 验证命令与结果：
  - `lsof -nP -iTCP -sTCP:LISTEN` 与本地健康检查：确认 5001/8000 均无旧实例；8001 属于 `/Users/zhangwenjun/zwj_project/mbti_generate`，未停止或修改该进程。
  - `.venv/bin/python main.py`：启动成功，Uvicorn 进程 PID `88732` 监听 `127.0.0.1:8000`；前端由同一 FastAPI 进程通过 `/` 与 `/web` 同源提供。
  - `GET /healthz`：`HTTP 200 application/json`，响应 `{"status":"ok"}`。
  - `GET /`：`HTTP 200 text/html`，页面包含 `Interviewer 面试工作台` 与 `/web/app.js`。
  - `GET /web/app.js`：`HTTP 200 text/javascript`。
  - Codex 内置浏览器：成功打开 `http://127.0.0.1:8000/`，标题、主导航、总览与工作区指标完成渲染，浏览器控制台错误数为 0。
- 未完成事项或恢复说明：无。当前服务保持运行，浏览器标签页已保留供用户查看；停止服务时可向启动会话发送 `Ctrl-C`。

## 2026-08-25 · MODEL-PROVIDERS-001

- 目标：实现首批同时覆盖 LLM 与 TTS 的真实 Provider adapter；扩展 OpenAI-compatible 语音合成，实现 DashScope 的 Qwen LLM/Embedding 与 Qwen/CosyVoice TTS，并修复模型路由配置界面，使基础语音链路可配置、可测试、可审计；视频数字人留在后续阶段。
- 关联问题：真实 LLM/TTS `environment_pending`；模型路由前端 `max_retries` 与后端 `retry_count` 字段不一致、路由能力列表未覆盖 TTS。
- 状态：`verified（仓库范围）`；`MODEL-PROVIDER-001` 已标记 `verified（仓库）`。
- 实际修改文件：
  - Provider 与网关：`app/providers/openai_compatible/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/dashscope/__init__.py`、`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、`app/model_gateway/gateway.py`。
  - 路由、资产与 UI：`app/schemas/api.py`、`app/services/model_admin.py`、`app/services/catalog.py`、`app/web/app.js`。
  - 自动化测试：`tests/test_openai_compatible_provider.py`、`tests/test_dashscope_provider.py`、`tests/test_provider_registry.py`、`tests/test_model_invocation.py`、`tests/test_mvp_flow.py`、`tests/test_documented_gap_apis.py`。
  - 同步文档：`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m pytest -q`：`89 passed in 2.74s`。
  - `git diff --check`：通过。
  - 重启 `.venv/bin/python main.py`：成功，Uvicorn PID `92458` 监听 `127.0.0.1:8000`；`GET /healthz` 返回 `{"status":"ok"}`。
  - `GET /api/v1/admin/model-providers/catalog`：`openai_compatible@0.2.0` 与 `dashscope@0.2.0` 均为 `implemented=true`，并声明 `llm.chat_json`、`llm.chat_text`、`embedding.text`、`tts.synthesize`。
- 未完成事项或恢复说明：当前没有真实 OpenAI-compatible/DashScope API Key、百炼业务空间、区域和外部联调样本，因此只完成官方 HTTP 合同和仓库验收，不宣称生产 route 健康。启用生产前仍需配置凭据、模型与 capability/purpose route，并验证 schema、TTS 音质/延迟/费用、私有资产复制和 readiness TTL。STT 与视频数字人仍按用户优先级留待后续供应商选型；现有 `avatar.speak -> TTS/文字` 降级链路保持可用。

## 2026-08-25 · MODEL-PROVIDERS-002

- 目标：参考 Dify 的声明式 Provider/模型 schema 与共享 runtime 分层，加深现有 Model Invocation seam；接入智谱 GLM 与 DeepSeek，并把已接入的阿里云百炼千问模型显式呈现在 catalog/UI，减少 OpenAI-compatible 厂商 adapter 的重复实现。
- 关联问题：`MODEL-PROVIDER-002`；当前 Provider manifest 缺少可执行模型 schema 约束，路由 UI 只能手输模型名；智谱与 DeepSeek 尚未安装为独立 Provider。
- 状态：`verified（仓库范围）`；`MODEL-PROVIDER-002` 已标记 `verified（仓库）`。
- 实际修改文件：
  - Manifest 与共享 runtime：`app/model_gateway/registry.py`、`app/providers/openai_compatible/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/dashscope/provider.json`。
  - 新 Provider：`app/providers/deepseek/__init__.py`、`app/providers/deepseek/provider.py`、`app/providers/deepseek/provider.json`、`app/providers/zhipuai/__init__.py`、`app/providers/zhipuai/provider.py`、`app/providers/zhipuai/provider.json`。
  - 配置与界面：`app/services/model_admin.py`、`app/web/app.js`。
  - 测试：`tests/test_provider_registry.py`、`tests/test_mvp_flow.py`、`tests/test_openai_compatible_vendor_providers.py`。
  - 同步文档：`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider-pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - 定向 Provider/API 测试：`20 passed in 1.10s`。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider-pycache .venv/bin/python -m pytest -q`：`93 passed in 3.11s`。
  - `git diff --check`：通过。
  - 服务重启成功：Uvicorn PID `95153` 监听 `127.0.0.1:8000`；`GET /healthz` 返回 `{"status":"ok"}`；catalog 返回 `deepseek@1.0.0`、`zhipuai@1.0.0`、`dashscope@0.2.0` 且均 `implemented=true`。
  - 本地浏览器验收：模型服务页显示 `11 个 Provider · 5 个可调用`；DeepSeek/智谱/千问卡片和模型数正确；配置弹窗分别预填 DeepSeek `https://api.deepseek.com + deepseek-chat`、智谱 `https://open.bigmodel.cn/api/paas/v4 + glm-5.2`、千问 `https://dashscope.aliyuncs.com/compatible-mode/v1 + qwen-plus`；控制台无 warning/error，未提交假配置。
- 失败与恢复记录：首次直接执行 `compileall` 时，macOS Python 尝试把 pyc 写到工作区外的用户缓存并被 sandbox 拒绝；仓库没有副作用。随后显式设置任务专用 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider-pycache` 重跑并通过。旧 exec session 已不存在且 8000 端口无监听，因此直接启动新服务，无进程需要强制终止。文档收口后的独立即时健康探针曾两次短暂返回 connection refused，但同一时刻 PID `95153` 仍在监听且 Uvicorn 无异常；随后 health/catalog 请求和三次连续 health 探针均返回 200，无数据副作用且无需再次重启。
- 未完成事项或恢复说明：当前没有 DeepSeek、智谱或阿里云百炼真实 API Key、账号/区域和模型授权，因此只完成官方协议适配、声明式目录和仓库验收，不宣称生产 route 健康。启用生产前仍需逐家创建配置/route，执行真实连接测试并验收结构化输出稳定性、延迟、费用、限流和模型生命周期；STT 与视频数字人仍保持后续外部选型边界。

## 2026-08-25 · MODEL-PROVIDERS-003

- 目标：为智谱 Provider 增加官方 GLM-TTS 能力，拆分 LLM/TTS 的按能力测试模型；补齐 Provider 配置编辑与测试模型选择界面，并把 HTTP 客户端初始化/代理依赖异常映射为结构化 Provider 错误，避免测试接口裸返回 500。
- 关联问题：`MODEL-PROVIDER-003`；智谱 manifest 只声明 LLM、单一 `test_model` 会把 `glm-tts` 误用于 LLM、管理 UI 无法编辑配置、环境 SOCKS 依赖缺失会泄漏为 Internal Server Error。
- 状态：`verified（仓库范围）`；`MODEL-PROVIDER-003` 已标记 `verified（仓库）`。
- 实际修改文件：
  - Provider/runtime：`app/providers/zhipuai/provider.py`、`app/providers/zhipuai/provider.json`、`app/providers/openai_compatible/provider.py`、`app/providers/openai_compatible/provider.json`、`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、`app/providers/deepseek/provider.json`。
  - API、服务与界面：`app/schemas/api.py`、`app/api/routes.py`、`app/services/model_admin.py`、`app/web/app.js`。
  - 自动化测试：`tests/test_openai_compatible_vendor_providers.py`、`tests/test_provider_registry.py`、`tests/test_mvp_flow.py`。
  - 同步文档：`README.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：
  - 智谱官方文档核对：确认 `POST https://open.bigmodel.cn/api/paas/v4/audio/speech`、`model=glm-tts`、WAV/PCM、系统/复刻音色、`speed 0.5-2.0` 和输入最长 1024 字符。
  - Provider/API 定向测试：`16 passed in 0.69s`；新增 GLM-TTS HTTP 合同、模型/能力错配、输入上限、transport 初始化错误和空 body 兼容测试。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider3-pycache .venv/bin/python -m compileall -q app tests`：通过。
  - `node --check app/web/app.js`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-provider3-pycache .venv/bin/python -m pytest -q`：`99 passed in 3.01s`。
  - `git diff --check`：通过。
  - 服务重启成功：Uvicorn PID `98856` 监听 `127.0.0.1:8000`；`GET /healthz` 返回 `{"status":"ok"}`。
  - 旧空 body curl 对已恢复但停用的配置返回结构化 `502 provider_config_disabled`，不再出现原始 SOCKS traceback/500；实际外部调用需补 API Key 后验证。
  - 内置浏览器验收：智谱 catalog 显示 `tts.synthesize` 与 6 个模型；测试弹窗在 LLM 默认 `glm-5.2`、切换 TTS 后自动选择 `glm-tts`；编辑弹窗包含状态、Base URL、默认音色、显式环境代理和“留空保留密钥”，控制台无 warning/error。
- 未完成事项或恢复说明：真实智谱账号 API Key 在 `TEST-ISOLATION-001` 数据副作用中无法恢复，已恢复该配置的非秘密字段并停用；管理员补密钥并启用后，仍需完成真实 LLM/TTS、音色、音质、延迟、费用、限流与私有资产复制验收，状态保持 `environment_pending`。视频数字人不在本工作项范围。

## 2026-08-25 · TEST-ISOLATION-001

- 目标：修复测试辅助函数误用默认开发 SQLite 的隔离缺陷，保证自动化测试只重置新建的内存 store，不再删除 `data/interviewer.sqlite3` 中的本地开发数据。
- 关联问题：`TEST-ISOLATION-001`；执行 Provider 全量回归时发现 `reset_store_for_tests()` 调用默认 `SQLiteStore.reset()`，导致本地数据库内容被测试清空。
- 状态：`verified`（隔离修复）；本轮数据删除副作用不可逆，恢复边界如下保留。
- 实际修改文件：`app/repositories/provider.py`、`tests/test_sqlite_store.py`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/change-log.md`；本地 `data/interviewer.sqlite3` 因误 reset 及后续恢复非秘密智谱配置而发生数据变化。
- 验证命令与结果：
  - 根因验证：`SQLiteStore.reset()` 会 `DELETE` documents、model_invocations、provider_secrets、outbox；旧 `reset_store_for_tests()` 确实通过默认 backend 取得该 SQLiteStore。
  - 修复：`reset_store_for_tests()` 现在直接替换为全新 `InMemoryStore`；`test_reset_store_for_tests_never_resets_development_sqlite` 用临时 SQLite sentinel 验证不再触碰持久库。
  - 修复后的首次全量：`99 passed in 2.97s`；测试前后 `data/interviewer.sqlite3` SHA-256 同为 `816ab277130d9c53aa1d0032b8331d4d13e68bfd3ce75ab1f661cec6abac3498`。
  - 最终全量：`99 passed in 3.01s`；Python 编译、前端语法和 `git diff --check` 均通过。
  - 恢复检查：先复制为 `/private/tmp/interviewer-reset-20260825.sqlite3`，SQLite `.recover` 仍只得到 reset 后数据；`tmutil listlocalsnapshots /` 没有可用用户数据快照。
- 未完成事项或恢复说明：误删前的 Provider secret、其它 Provider 配置/路由和可能存在的开发业务记录无法从 SQLite 或本地快照恢复，不能伪造。已按可确认信息将 `mpc_11b4d940e3134074` 恢复为 disabled 智谱配置，Base URL、LLM/TTS 按能力模型和 `tongtong` 音色已补回；管理员需在“模型服务 → 编辑”重新输入 API Key、启用并重建需要的 route。两个 `/private/tmp/interviewer-*-20260825.sqlite3` 恢复副本保留，未删除。

## 2026-08-25 · MODEL-CONFIG-V2-001

- 目标：把模型服务配置重构为“厂商连接 → 模型配置 → 能力路由”，由 Provider 插件声明厂商与模型类型特有的后端表单 schema，并由前端通用渲染；路由只引用已验证的模型配置，删除 `provider_config_id + model` 双字段执行表示。
- 关联问题：`MODEL-CONFIG-V2-001`；当前 `ModelProviderConfig` 混合账号凭据、连接参数和模型参数，具体模型不是独立资源，前端硬编码厂商字段。
- 状态：`closed`（仓库实现与旧运行时边界删除完成；真实厂商健康仍为 `environment_pending`）。
- 实际修改文件：`app/model_gateway/{capabilities,forms,gateway,registry,schemas}.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/domain/appointment_admission.py`、Persistence/repository 的 Memory/SQLite/PostgreSQL 实现、五个已实现 Provider manifest、`app/web/app.js`、`app/migrations/model_configuration_v2.py`、`migrations/001_postgresql_persistence.sql`、`migrations/002_model_configuration_v2.sql`、相关既有测试与 `tests/test_model_configuration_v2.py`；同步 `CONTEXT.md`、ADR 及架构/API/领域/Provider/数据库/问题/进度/路线图文档。
- 验证命令与结果：
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pyc .venv/bin/python -m compileall -q app`：通过。
  - `node --check app/web/app.js`：通过；所有 Provider manifest 通过 JSON 解析和 registry 加载，模型类型覆盖符合能力声明。
  - `.venv/bin/pytest -q`：`106 passed in 3.63s`。
  - `git diff --check`：通过。
  - `.venv/bin/python -m app.migrations.model_configuration_v2 data/interviewer.sqlite3 --dry-run`：输出 `2` 个 ProviderConnection、`2` 个 ModelConfiguration、`0` 个 ModelRoute；dry-run 未修改开发数据库。
- 未完成事项或恢复说明：未执行开发 SQLite 的实际单向迁移，部署升级前需先复核 dry-run，再去掉 `--dry-run` 执行；真实供应商 API Key、模型授权、费用/延迟及音质仍需逐个 ModelConfiguration 联调，不能由离线测试标记生产健康。

## 2026-08-25 · MODEL-CONFIG-V2-LOCAL-MIGRATION

- 目标：修复模型配置 v2 发布后本地工作区所有业务 API 因旧 SQLite `provider_secrets` 字段而返回 500 的启动故障，备份并执行已验证的一次性数据迁移。
- 关联问题：`MODEL-CONFIG-V2-001`；服务端日志确认 `sqlite3.OperationalError: no such column: provider_connection_id`。
- 状态：`complete`。
- 实际操作：停止 PID `13643`；将 `data/interviewer.sqlite3` 原样备份到 `/private/tmp/interviewer-pre-model-v2-20260825.sqlite3`；执行模型配置 v2 迁移；因工具 PTY 会暂停进程，最终通过 macOS Terminal 持续启动为 PID `14840`。
- 验证命令与结果：
  - 迁移前数据库与备份 SHA-256 均为 `56cbde2321bbf707d6696bd5590d33217011e6955d34fe10deeb42d48af26bb2`。
  - 迁移输出：`2` 个 ProviderConnection、`2` 个 ModelConfiguration、`0` 个 ModelRoute。
  - SQLite `PRAGMA integrity_check` 返回 `ok`；旧 `provider_configs` collection 已删除，`provider_secrets.provider_connection_id` 已存在。
  - `/healthz`、工作区业务集合、Provider catalog、ProviderConnection、ModelConfiguration 和 ModelRoute API 均返回 HTTP 200；工作区首屏所需接口不再出现 500。
- 未完成事项或恢复说明：迁移前备份保留在 `/private/tmp/interviewer-pre-model-v2-20260825.sqlite3`，可在服务停止后恢复。迁移得到的智谱 LLM/TTS 模型状态为 `untested`，需补有效 API Key 并分别测试后才能创建生产路由。

## 2026-08-26 · DEEPSEEK-JSON-PROBE-001

- 目标：修复 DeepSeek V4 Pro 模型配置测试将普通文本响应当作 JSON 解析，从而误报 `provider_schema_invalid` 的问题。
- 关联问题：模型配置的 `llm.chat_json` 连通性探针未提供 JSON Schema，导致 OpenAI-compatible/DeepSeek adapter 不会启用结构化输出。
- 状态：`verified`。
- 实际修改文件：`app/services/model_admin.py`、`app/providers/openai_compatible/provider.py`、`app/providers/mock/provider.py`、`tests/test_model_configuration_v2.py`、`tests/test_openai_compatible_vendor_providers.py`、`docs/model-provider-plugins.md`、`docs/change-log.md`。
- 实现结果：`llm.chat_json` 配置/路由探针现在携带要求 `message` 字段的最小 JSON Schema；OpenAI-compatible 的 `json_object`/`prompt` 路径会向厂商同时传入 schema 和由 schema 生成的合法 JSON 示例；空内容与非 JSON 内容保留结构化错误并附带非敏感 `finish_reason`。Mock provider 也返回符合同一探针 schema 的 `pong`。
- 验证命令与结果：
  - Provider/配置定向回归：`23 passed in 0.91s`。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-deepseek-pyc .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-deepseek-pyc .venv/bin/python -m pytest -q`：`106 passed in 3.35s`。
  - `git diff --check`：通过。
  - 停止旧 PID `14840` 并通过 macOS Terminal 启动修复版 PID `16697`；`GET /healthz` 返回 `{"status":"ok"}`。
  - 真实调用 `POST /api/v1/admin/model-configurations/model_cfg_ef64e33ec0b641a8/test`：HTTP 请求成功，DeepSeek `deepseek-v4-pro` 返回 `{"message":"pong"}`，用量 `173 + 38 = 211 tokens`，模型配置标记为 `ready`。
- 未完成事项或恢复说明：无；此次真实探针产生一次 DeepSeek 调用计费/配额用量，调用日志按现有 Model Invocation 规则保留。

## 2026-08-26 · PROVIDER-CREDENTIAL-PREFLIGHT-001

- 目标：在管理员添加模型厂商连接时立即执行真实 API Key 鉴权探针，而不是只校验字段格式或等到具体模型测试。
- 关联问题：ProviderConnection 的 `/validate` 当前只返回 `model_required`，不会访问厂商；前端添加连接后也不会自动校验密钥。
- 状态：`verified`。
- 实际修改文件：`app/providers/openai_compatible/provider.py`、`app/providers/zhipuai/provider.py`、`app/providers/mock/provider.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/web/app.js`、`tests/test_model_configuration_v2.py`、`tests/test_openai_compatible_vendor_providers.py`、`docs/api-design.md`、`docs/model-provider-plugins.md`、`docs/change-log.md`。
- 实现结果：Provider adapter seam 新增 `validate_credentials`。OpenAI-compatible、DeepSeek 和 DashScope 通过 Bearer 认证的 `/models` 执行无文本生成费用探针；智谱插件使用 `glm-4.7-flash` 的单 token 最小探针；Mock 使用本地结果。管理服务将远程结果持久化为 `valid` / `invalid` / `model_required`，前端创建连接后会立即执行该校验；失败连接保留供管理员编辑修复。连接层校验不替代每个 ModelConfiguration 的模型权限与输出契约测试。
- 验证命令与结果：
  - Provider/配置定向测试：`20 passed in 0.93s`。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-credential-pyc .venv/bin/python -m compileall -q app tests`：通过。
  - `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-credential-pyc .venv/bin/python -m pytest -q`：`109 passed in 3.63s`。
  - `node --check app/web/app.js` 与 `git diff --check`：通过。
  - 停止旧 PID `16697` 并启动更新版 PID `18926`，监听 `127.0.0.1:8000`。
  - 真实调用 `POST /api/v1/admin/model-provider-connections/provider_conn_f4b6040cac554a6e/validate`：DeepSeek API Key 鉴权成功，`credential_status=valid`，厂商返回 `3` 个当前可访问模型。
- 未完成事项或恢复说明：添加厂商连接的请求会先保存加密凭证，再执行远程校验；远程失败不删除连接，以便修正 Base URL 或 API Key。智谱凭证探针会产生极小的模型调用配额/计费；其余已实现的 OpenAI-compatible 列表探针不生成 token。

## 2026-08-25 · WEB-ARCH-001

- 目标：按生产优先级把内置 Web 前端重构为 React 工程，在保持既有 REST 路径、请求体和响应语义兼容的前提下，完整迁移现有后台与候选人功能，并补齐后台认证/RBAC、浏览器 WebSocket 鉴权、候选人录音断线恢复、统一请求与路由级加载。
- 关联问题：前端生产认证不可用、非管理员整页加载失败、录音断线丢失/卡死、预约创建后的邀请失败不可恢复、异步状态文案漂移、候选人评分泄漏、首屏 N+1 与前端行为测试缺失。
- 状态：`verified`（2026-08-26）。
- 计划修改：`docs/api-design.md`、`docs/architecture.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`、`app/core/auth.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/main.py`、`app/web/`（React + Vite）、相关前端/认证/实时测试。
- 实际修改：后端认证/实时/聚合查询边界 `app/core/auth.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/services/catalog.py`；React/Vite 工程与托管 `app/main.py`、`app/web/{package.json,package-lock.json,vite.config.js,index.html,src/,dist/}`；框架无关 `app/web/{core/,candidate/runtime.js,interviews/appointment.js}`；前端样式、API/架构/进度/路线图/README 和认证、媒体、React 行为测试。模型配置保留 manifest 驱动的预定义/自定义模型 ID，候选人房间保留音视频设备切换；`/web/` 启用静态 HTML 入口。旧 `app/web/app.js`、六个 HTML 字符串 view module、DOM schema renderer 与未使用的 presentation helper 已删除。
- 最终验证：`cd app/web && npm run build && npm test`（生产构建成功、Vitest `3 passed`）；`npm audit --audit-level=high`（0 漏洞）；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-react-complete-pyc .venv/bin/python -m compileall -q app tests`（通过）；`.venv/bin/python -m pytest -q`（`114 passed in 3.83s`）；`git diff --check`（通过）。应用内浏览器逐页验证六个后台 feature、React 表单弹窗、模型动态 schema 表单、公开邀请及候选人路由；最终生产 bundle 通过 `http://127.0.0.1:8766/web/#models` 加载，模型页与 React shell 可见、控制台 0 error、DOM 无旧 `app.js`，服务日志确认总览/题库聚合请求边界。
- 未完成事项或恢复说明：仓库内 React 迁移无未完成项。真实摄像头/麦克风权限、外部模型凭据、STT/TTS/数字人和生产 WebSocket 网络仍属于既有部署环境验收，不改变本工作项 `verified` 结论。

## 2026-08-26 · BACKEND-MEDIA-001

- 目标：补齐 React 工作台所依赖的生产后端媒体能力，区分已存在但待部署验收的 PostgreSQL/Redis/OSS/WebSocket adapter 与尚缺实现的真实 STT、数字人 Provider；在保持现有 REST/WebSocket interface 兼容的前提下实现可配置的非 mock 媒体 Provider、生产 readiness、私有媒体复制和契约测试。
- 关联问题：真实 STT 与视频数字人仅有 mock/清单占位，前端摄像头与录音链路缺少可部署的供应商 adapter；生产环境验收项描述未明确“已有实现”和“尚缺实现”。
- 状态：`verified`。
- 计划修改：`app/model_gateway/`、`app/providers/`、`app/services/`、必要的 schema/API、Provider 与实时媒体测试，以及 `docs/architecture.md`、`docs/api-design.md`、`docs/model-provider-plugins.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/known-issues-and-remediation.md`、`docs/change-log.md`。
- 实际修改：新增 `app/providers/media_http/`，以 HTTPS/Bearer 合同实现真实 multipart batch STT、安全缓存后统一 final 的 streaming adapter，以及 `audio/video` 数字人响应；新增 `app/adapters/private_media.py`，生产候选人录音通过 PrivateFileStorage 保存为绑定组织/面试/轮次的 `candidate_answer_audio` FileObject。`BatchSTTRequest.audio_bytes` 只在服务端内存传递并从序列化、调用哈希和日志排除；Interview、Realtime、Streaming STT、Enterprise Review、Retention 与 Appointment Admission 已统一支持私有录音、签名回听、删除和生产失败关闭。React 候选人页面新增真实数字人音频/视频播放，REST/WebSocket 路径和请求/响应兼容保持不变。同步更新 README、架构、API、领域模型、Provider、数据库、问题状态、进度和路线图。
- 最终验证：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests`（通过）；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q`（`124 passed in 3.96s`）；`cd app/web && npm run build && npm test -- --run`（Vite 生产构建成功、Vitest `3 passed`）；`git diff --check`（通过）。新增离线合同覆盖 multipart 音频、不安全/含凭据媒体 URL、唯一 final、健康探针、私有录音读回/签名复核、生产本地存储拒绝和 OSS readiness。
- 未完成事项或恢复说明：仓库内通用 STT/HTTPS 数字人 adapter 与生产私有录音已完成。仍需在部署环境提供 PostgreSQL、Redis、阿里云 OSS、扫描器、`media_http` 目标端点/API Key/模型和真实录音视频，验证 RLS、多实例网络、WER、延迟、费用、浏览器摄像头/麦克风权限及生产 WebSocket；这些是已有 adapter 的环境验收。若目标供应商要求低延迟 partial STT、WebRTC 信令或实时口型同步，需要在现有 provider seam 内增加其专属协议实现。

## 2026-08-26 · DEPLOY-VALIDATION-001

- 目标：开始执行生产环境验收，使用隔离的本机 PostgreSQL 16 与 Redis 7 实例验证迁移、RLS、持久化契约、跨实例事件和生产失败关闭；盘点可复用的真实模型连接，并明确 OSS、扫描器、STT/数字人和浏览器媒体仍缺少的外部条件。
- 状态：`verified`（本机隔离环境；目标云环境继续 pending）。
- 已执行环境动作：启动 `interviewer-validation-postgres`（仅本机 `127.0.0.1:55432`）和 `interviewer-validation-redis`（仅本机 `127.0.0.1:56379`）隔离容器；未改写现有 SQLite 业务数据，未启动临时 Web 服务。
- 计划修改：部署验收脚本/测试（如发现仓库缺少可重复入口）、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际修改：`PostgreSQLPersistence` 移除运行时自动 DDL，改为 schema 只读校验；新增 `python -m app.migrations.postgresql`，由 migration owner 在 advisory lock 内显式迁移。新增 `/readyz` Deployment Readiness module，区分进程存活与数据库、Redis、生产密钥、私有 OSS bucket 只读鉴权、扫描器就绪，结果不包含密钥值且不写数据/调用付费模型。修复 Redis subscriber 正常关闭时冒泡连接异常和 deprecated close。新增 clamd TCP `PING/INSTREAM` scanner adapter，保留 command scanner；官方 `oss2 2.19.1` 加入项目依赖。新增 PostgreSQL、Redis、clamd 与 readiness 测试，并同步 README、架构、API、数据库、问题、进度和路线图。
- 本机环境验收：PostgreSQL 16 migration 成功；非 owner `interviewer_app` 角色跨租户读取为 0、跨租户写被 RLS 拒绝；事务回滚、CAS、Outbox 幂等、预约唯一约束和结构化题库过滤通过；`EXPLAIN (ANALYZE, BUFFERS)` 使用 `idx_question_catalog_scope`，样本执行约 `0.038 ms`。Redis 7 鉴权 PING、两个独立 bus 实例 Pub/Sub、正常关闭和生产公开限流通过。生产 `/readyz` 对隔离 PostgreSQL/Redis 返回 ready，并准确把未配置的安全密钥、OSS 和扫描器列为 not ready。现有 SQLite 仅做非敏感状态盘点，未改写；DeepSeek 连接仍为 valid/模型 ready，智谱配置仍 untested。
- 最终验证：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests`（通过）；常规 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q`（`130 passed, 5 skipped in 4.06s`）；设置隔离 DSN 后 `tests/test_postgresql_integration.py tests/test_redis_integration.py`（`4 passed in 0.25s`）；`git diff --check`（通过）。5 个默认 skip 是仅在提供 PostgreSQL/Redis/clamd 真实地址时启用的环境测试。
- 未完成事项与恢复说明：目标阿里云 OSS endpoint/bucket/RAM、生产安全密钥、`media_http` STT/数字人 endpoint/API Key/模型、真实浏览器/生产 WebSocket、目标 PostgreSQL/Redis 集群仍未提供，不能宣称目标生产环境 ready。官方 ClamAV stable 预装库镜像没有 arm64 manifest；改用 amd64 仿真后下载长时间未完成，已中止前台 pull，保留 Docker 缓存层，真实 daemon/EICAR 测试待目标 clamd 或镜像就绪后运行。`interviewer-validation-postgres` 与 `interviewer-validation-redis` 容器继续运行以便后续验收，分别仅监听 `127.0.0.1:55432/56379`；如需恢复空间可停止并删除这两个具名容器。

## 2026-08-26 · DEPLOY-VALIDATION-002

- 目标：继续部署前验收，建立不泄露密钥、可重复执行的生产配置生成与静态检查入口，并完成本机 clamd/EICAR 真实协议测试及组合 readiness 探针。
- 关联问题：生产安全配置尚依赖人工拼装；ClamAV daemon/EICAR 与接近生产的组合 `/readyz` 尚无真实证据。
- 状态：`verified`（本机部署前配置与 clamd 协议；目标 OSS/云环境继续 pending）。
- 计划修改：`.gitignore`、生产部署配置 module/命令与测试、`README.md`、相关部署文档和 `docs/change-log.md`。
- 实际修改：`.gitignore` 忽略所有本地 `.env.*` 并保留 `.env.example` 例外；新增 `app.operations.production_config`，以 `generate_production_config`/`inspect_production_config` 两个主要 interface 统一生成和静态检查。生成操作原子创建、拒绝覆盖、固定 `0600`，生成三种角色独立 Bearer token、两把 Fernet key 与独立候选人/WebSocket/文件/媒体签名密钥；检查只返回已配置变量名、缺项和无密钥值的无效分组，不导出环境或连接外部服务。`/readyz` 复用同一安全配置校验，删除重复实现。新增 `tests/test_production_config.py`，并同步 README、架构、问题、进度和路线图。
- 本机环境动作与证据：生成 `.env.production.local`（Git 已忽略、权限 `-rw-------`，没有输出密钥），对隔离 PostgreSQL/Redis/clamd 配置静态检查得到 `18` 个已配置变量、仅缺 4 个 OSS 参数、无格式错误。官方 `clamav/clamav-debian:stable_base` 原生 arm64 镜像成功下载；`interviewer-validation-clamav` 仅绑定 `127.0.0.1:53310`，挂载 `/private/tmp/interviewer-clamav-db/local.ndb` 的 EICAR-only 验收签名库。完整预装库镜像下载曾长时间无进度并安全中止；首次未提权 pytest 因沙箱禁止本机 socket 而 PING false，允许本机连接后相同测试通过，这两次失败均未改业务数据。
- 验证命令与结果：配置/readiness 定向测试 `7 passed`；常规全量 `134 passed, 5 skipped in 3.99s`；真实 clamd `tests/test_clamd_integration.py` 为 `1 passed in 0.19s`；隔离 PostgreSQL、Redis、clamd 组合测试为 `5 passed in 0.36s`；组合生产 readiness 为 `database=true`、`database_backend=true`、`redis=true`、`security_secrets=true`、`malware_scanner=true`、`object_storage=false`，因此整体准确保持 `not_ready`；Python compileall 与 `git diff --check` 通过。
- 未完成事项或恢复说明：当前唯一的基础 `/readyz` 缺项是目标阿里云 OSS endpoint/bucket/RAM 凭据；目标 PostgreSQL/Redis、带完整且持续更新签名库的生产 clamd、`media_http` STT/数字人、生产域名/WebSocket 和真实浏览器权限仍必须在目标环境复验。`.env.production.local` 含本机验收密钥并保留供下一步使用，不纳入 Git；三个验收容器继续运行且只绑定本机。EICAR-only 库只证明真实 daemon 协议与 adapter 错误映射，不能作为生产恶意样本覆盖率证据。

## 2026-08-26 · WEB-UI-REMEDIATION-001

- 目标：修复 React 工作台无法从空环境建立首道题目，以及模型服务、招聘流程、搜索工具栏和通用列表/表格因样式契约缺失而出现的错位、挤压和原始 HTML 退化。
- 关联问题：题目页在无岗位题库时禁用唯一创建入口；`list-card`/`panel`/岗位卡片缺少样式实现；候选人表格漏用统一表格 class；搜索网格列数与实际控件不一致；技术状态直接暴露英文枚举。
- 状态：`verified`。
- 计划修改：React questions/models/workflow 与通用 UI module、全局样式、浏览器行为测试和开发文档；保持现有后端接口、请求体和响应语义不变。
- 实际修改：`app/web/src/features/questions/Page.jsx` 保持“新建题目”始终可用；无岗位题库时以两阶段引导先创建或选择岗位并建立题库，再自动打开首题表单，继续调用既有岗位、题库和题目接口。`app/web/src/core/ui.jsx` 新增中文状态映射与 ResourceCard interface；模型页的厂商连接、模型配置和能力路由统一使用该 interface。招聘流程补齐 panel 标题层级、统一 `data-table`，把 `[retention_purged]` 安全占位显示为不可操作的“已清除候选人”。`app/web/styles.css` 新增 panel、岗位卡片、资源列表、表单引导和响应式五控件搜索网格；生产 bundle 已更新。`app/web/src/App.test.jsx` 新增空工作区首题入口行为测试；同步开发进度与本日志。
- 验证命令与结果：`npm run build && npm test` 通过，Vite 生成生产 bundle，Vitest `4 passed`；Python 全量 `134 passed, 5 skipped in 4.09s`；compileall 与 `git diff --check` 通过。真实浏览器在 1440×900 桌面视口验证题库、招聘流程和模型服务：首题按钮 enabled、前置引导 dialog 可见、搜索工具栏高度 42px 单行、模型 6 条实际资源进入统一卡片、已清除候选人不可上传简历，三个页面控制台均无 warning/error；移动默认视口同时验证引导表单和插件卡片可读。
- 未完成事项或恢复说明：未替用户创建测试岗位、题库或题目，避免污染现有业务数据；现有 REST 请求体与响应语义未改变。后续若继续视觉精修，可基于真实业务数据补充长标题、更多路由和表格横向溢出场景，但本次报告的不可创建与明显错位已修复。

## 2026-08-26 · MODEL-ADMIN-CRUD-001

- 目标：补齐模型厂商连接和具体模型配置的查看、创建、修改、删除闭环；删除厂商时事务性删除其全部模型，删除单个模型时保留同厂商其他模型，并清理引用已删除模型的能力路由。
- 关联问题：当前管理 API 和 React 模型服务页只有创建、列表、修改与测试/校验，缺少单项读取和删除能力，无法完成模型资源生命周期管理。
- 状态：`verified`。
- 实际修改文件：`app/persistence/{interface,memory,sqlite,postgresql}.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/web/src/{core/ui.jsx,features/models/Page.jsx,App.test.jsx}`、`app/web/styles.css`、`app/web/dist/`、`tests/{test_model_configuration_v2,test_persistence_contract}.py`、`docs/{api-design,domain-model,model-provider-plugins,change-log}.md`。
- 实现结果：ProviderConnection 和 ModelConfiguration 均具备创建、列表/单项查看、修改和删除接口。两个 DELETE 使用 `expected_version`；厂商删除在同一事务清理加密凭证、全部子模型、引用路由和对应模型能力断路器状态，单模型删除保留厂商和兄弟模型，仅清理引用该模型的路由/断路器状态；历史脱敏调用日志保留。React 模型服务页新增查看、删除与明确影响范围的二次确认，沿用原有 provider manifest 动态表单及所有现有模型对接协议。
- 验证命令与结果：`tests/test_model_configuration_v2.py` 为 `10 passed`；Memory/SQLite 持久化合同 `tests/test_persistence_contract.py` 为 `16 passed`；Python compileall 与全量 pytest 为 `138 passed, 5 skipped`；`npm run build && npm test -- --run` 生产构建成功、Vitest `5 passed`；`git diff --check` 通过。测试未删除或修改开发 SQLite 中现有 DeepSeek/智谱/Mock 配置。已在 Terminal 重启为 PID `52786`，监听 `127.0.0.1:8000`；`GET /healthz` 返回 `ok`，首页返回 HTTP 200，OpenAPI 确认两个单项资源均加载 `GET/PATCH/DELETE`。
- 失败与恢复说明：首次从 `app/web` 工作目录运行根目录 `.venv/bin/python` 因相对路径错误立即失败，没有副作用；首次级联测试发现 circuit hash 编码括号优先级错误，修正后再次运行；测试对系统自动保留的开发 Mock 连接假设为空，调整为验证目标厂商 ID 和密钥确实消失。工具隔离网络首次无法访问 Terminal 中的本机服务，改用获准的本机连接执行相同只读探针后通过。以上失败均未触碰开发业务数据。
- 未完成事项：无仓库内遗留。

## 2026-08-27 · KNOWLEDGE-BASE-TTS-DESIGN-001

- 目标：拆解“题库列表 → 题库详情/题目 → 题库级 TTS 模型与声音配置 → 配置变化后整库异步重建语音”的产品与技术方案，并把 Celery 调度、worker 执行、幂等和历史资产保护写入正式文档。
- 关联问题：当前题库页面平铺全部题目；KnowledgeBase 只有 `language + voice_profile_id`，没有显式绑定 TTS ModelConfiguration；题库语音重建已有持久工作项语义，但尚未定义 Celery 只负责调度、数据库工作项仍是真相来源的部署合同。
- 状态：`complete（设计）`；运行时实现仍由 `KB-SPEECH-001` 追踪为 `open`，没有代码、迁移和验收证据前不得标记 `verified`。
- 实际修改文件：`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,known-issues-and-remediation,development-progress,implementation-roadmap,change-log}.md`。
- 设计结果：题库一级页面只加载题库摘要，详情页按单题库加载题目、TTS 配置和构建历史；KnowledgeBaseSpeechProfile 显式冻结 TTS ModelConfiguration 版本、声音、语言、格式和语速，任何影响音频输出的变化都会递增 revision 并创建整库 KnowledgeBaseSpeechBuild。父构建冻结活动题 manifest，题目级工作项支持部分失败重试，旧 revision 的完成结果不能覆盖当前资产，历史计划/会话继续引用不可变旧资产。HTTP 只创建持久工作项并返回 202；所有 Celery task 和执行入口位于 `app/workers/`，Redis/Celery 只负责调度，DurableWorkItem/Outbox 继续作为真相来源，Celery Beat 负责补发 due/expired 工作。被题库当前配置引用的 TTS 模型禁止直接删除，必须先切换题库配置。
- 验证命令与结果：`rg` 交叉核对领域术语、接口路径、Celery 文件边界和问题编号；`git diff --check` 通过。文档明确区分“设计完成”和“运行时未实现”。
- 失败与恢复说明：一次只读 `rg` 校验命令把 Markdown 反引号直接放入 shell 双引号，zsh 将其中两个状态文本误作命令并报告 `command not found`；没有文件或运行时副作用，随后改用不含命令替换语义的查询完成核对。
- 未完成事项：尚未实现数据迁移、TTS voice catalog、题库语音 deep module、Celery 基础设施、异步 API、React 分层页面和自动化验收；按实施路线图里程碑 15 顺序继续。

## 2026-08-27 · KNOWLEDGE-BASE-TTS-IMPLEMENTATION-001

- 目标：实现题库级 TTS 模型/声音配置、配置 revision、整库异步语音重建、Celery worker 调度和题库列表/详情分层 React 页面，并保持现有业务接口兼容。
- 关联问题：`KB-SPEECH-001`。
- 状态：`verified`（仓库与本地 Redis/Celery/API/UI）。
- 计划修改：领域 schema、Memory/SQLite/PostgreSQL 持久化与迁移、模型声音目录、Catalog/Question Speech Build module、REST 接口、`app/workers/` Celery 调度、React questions feature、依赖/部署配置、自动化测试及对应正式文档。
- 安全与兼容约束：HTTP 不直接调用外部 TTS；Celery 消息只携带组织和持久工作项 ID；旧题库字段和现有调用方保留兼容读取；旧 profile revision 的迟到结果不得覆盖当前语音；历史资产不可变；现有用户业务数据不清空。
- 实际修改：新增 `app/services/knowledge_base_speech.py`、`app/workers/celery_app.py`、`app/workers/dispatcher.py`、`app/workers/knowledge_base_speech.py` 和 `tests/test_knowledge_base_speech.py`；修改 `app/services/catalog.py`、`app/services/model_admin.py`、`app/api/routes.py`、`app/schemas/api.py`、`app/persistence/interface.py`、`app/workers/outbox.py`、Mock provider manifest、`pyproject.toml`；重构 `app/web/src/features/questions/Page.jsx`、路由/数据加载/UI 状态/CSS/前端测试并重建 `app/web/dist/`；同步更新 README、接口、领域、数据库、供应商、架构、已知问题、进度和路线图文档。
- 实现结果：题库首页只加载并展示题库摘要；详情页按单一题库加载题目、TTS 模型/声音选项和构建进度。题库 profile 采用 revision + optimistic version；切换模型、声音或输出参数冻结全题清单并创建父构建，worker 再扇出可重试的题目子工作项。单题变化只重建该题；旧 revision 的迟到结果标记 `superseded` 且不能覆盖当前资产。删除被题库引用的 TTS 模型/厂商返回 `409 MODEL_CONFIGURATION_IN_USE`。旧题库不猜测模型，按读取投影显示 `configuration_required`，首次显式配置时完成非破坏性按需迁移。
- 异步边界：HTTP 题目创建/语音重建返回 `202` 和持久工作项；Celery Beat 只扫描 durable work，消息只包含 `organization_id` 与 `work_item_id`；worker 重新认领并执行，Redis 不作为业务事实源。Beat schedule 文件落到 `/tmp`，不污染仓库。
- 验证：`PYTHONPYCACHEPREFIX=/tmp/interviewer-pycache .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/pytest -q` 为 `141 passed, 5 skipped`；`npm run build` 通过；`npm test -- --run` 为 `6 passed`；`git diff --check` 通过。真实本地 Redis `PING` 返回 `PONG`，Celery 5.6.3 worker/Beat 成功连接 `redis://127.0.0.1:6379/2` 并周期派发 durable scanner；重启 API 后 `/healthz` 为 `ok`。浏览器复验题库列表、详情、禁用无可用 TTS 的配置按钮及旧题库/题目“待配置语音”投影通过。
- 失败与恢复：首次 compileall 因 macOS 默认 pycache 目录无权限失败，未修改仓库，改用 `/tmp` pycache 后通过；首次全量 pytest 因尚未安装新增 Celery 依赖而收集失败，安装已声明依赖后通过；过渡测试仍断言旧同步语音契约，更新为 `202 + worker` 契约后通过；一次页面整文件 patch、一次 CSS patch 因上下文不匹配未生效，均以更小补丁重试；浏览器 `networkidle` 等待能力不可用，改用 DOM ready 与显式短等待；sandbox 内 `redis-cli` 连接受限，经批准在本机只读验证成功。上述失败均无业务数据损坏。
- 未完成事项：尚未用真实供应商凭据执行会产生外部成本的 TTS 合成，也未在目标生产 PostgreSQL/Redis 网络和多实例部署中验收；仓库内实现与本地基础设施闭环已完成，不将其宣称为生产环境验收。

## 2026-08-27 · QUESTION-CRUD-TTS-ELIGIBILITY-001

- 目标：修复用户已添加 TTS 模型但题库详情仍不允许配置语音的问题，并补齐题目查看、新建、编辑、删除和语音试听的完整管理闭环；题目编辑继续只重建单题语音，题目删除不触发整库外部调用。
- 关联问题：题库详情把无“ready”模型直接表现为不可操作；题目列表仅有详情入口，缺少编辑与删除。
- 状态：`verified`（仓库与本地 API/React 浏览器）。
- 计划修改：模型 TTS 可选性诊断/反馈、Question 删除领域接口及 HTTP 合同、受控语音资产试听、React 题目操作与确认交互、行为/后端测试、接口/领域/进度文档；保持 Question Catalog 和 Knowledge Base Speech Build 两个既有深模块 seam，不在页面复制生成规则。
- 安全与兼容约束：不删除用户已有业务数据；删除题目必须带 expected version、组织隔离并保留历史面试快照/不可变语音资产；模型未校验或声音目录不完整时给出可操作原因，不把失败配置伪装为 ready。
- 根因与修复：用户新增的 `GlM-TTS/glm-tts` 能力正确，但健康状态为 `untested`；旧 `speech-options` 只返回 ready 模型，页面又直接禁用配置按钮，导致资源看似“消失”。接口现在保留兼容 `items`（仅可保存）并新增 `candidates`（所有 TTS、状态、声音和不可选原因）；配置入口始终可用，弹窗展示该模型并提供“测试并启用”，测试成功后原位刷新模型/声音选择。未替用户自动执行真实智谱 TTS 探针，避免未经确认产生外部调用或费用。
- 实际修改：`app/services/knowledge_base_speech.py` 增加 TTS eligibility projection；`app/services/catalog.py` 增加带版本归档删除、活动列表过滤、归档任务 supersede guard，并避免未变化题干重复生成语音；`app/api/routes.py` 新增 Question DELETE。React 题库详情增加未测试 TTS 诊断/测试、题目查看/编辑/删除和受控试听，通用 ModalForm 支持不可提交态并补充样式；更新后端/前端测试、生产 bundle 与接口/领域/Provider/进度/路线图文档。
- 删除与试听语义：删除只把 Question 归档并从活动列表移除，历史面试快照和不可变语音资产保留；并发必须通过 `expected_version`。归档前已经排队的语音工作由 worker 标记 `superseded`，不调用外部 TTS。试听只在题目有当前 ready `speech_asset_id` 时启用，通过 `/question-speech-assets/{id}/content-url` 获取五分钟受控地址。
- 验证：Python compileall 通过；题库语音/文档缺口定向测试 `7 passed`；全量 pytest `142 passed, 5 skipped`；Vite 生产构建通过；Vitest `8 passed`；`git diff --check` 通过。重启 API（PID `74263`）和 Celery worker/Beat 后，只读接口确认现有 `GlM-TTS` 作为 `untested` candidate 返回、音色 `tongtong` 可见。浏览器在现有题库验证配置按钮可点击、测试入口/不可提交保护、题目试听/查看/编辑/删除按钮、预填编辑表单和归档确认文案；未修改现有题目或触发真实 TTS 测试。
- 未完成事项：现有智谱模型仍需用户在配置弹窗点击“测试并启用”完成一次真实 Provider 探针；探针成功后才能保存题库 profile 并启动整库 Celery 语音重建。目标生产供应商、对象存储与网络验收仍按既有环境清单执行。

## 2026-08-27 · REAL-TTS-CHAIN-VALIDATION-001

- 目标：按真实可用标准验收题库 TTS 全链路，修复未就绪模型无法在下拉框选中、测试失败反馈不足和 Provider 可重试错误只调用一次的问题；以真实智谱请求、题库配置、Celery 资产生成和试听接口作为验收链。
- 状态：`verified`（仓库、本地 API/UI 与真实错误链）；真实智谱生成 `environment_blocked`。
- 已执行真实验证：`POST /api/v1/admin/model-configurations/model_cfg_45037aa67bd448da/test` 已实际到达智谱，返回结构化 `provider_rate_limited`，模型未被错误标记为 ready，当前未创建题库 profile 或语音任务。
- 计划修改：配置表单允许选择所有已添加 TTS 并预选声音，但只允许 ready 模型保存；模型测试增加有界退避重试和中文可操作错误；补齐自动化合同、文档、重启与真实链路复验。
- 安全约束：不绕过 ready gate，不把 429/额度不足伪装成成功；外部重试有严格次数上限；不输出 Provider 密钥或请求正文；未通过测试不得触发整库付费生成。
- 实际修改：Model Administration 探针改为 `retry_count=2 + retry_backoff_ms=250`，统一 Provider 错误处理把 `provider_rate_limited` 映射为 HTTP 429；题库语音表单改为所有已添加 TTS 均可在下拉框选中并联动声音，选项明确展示待测试/测试失败/已就绪，只有 ready 选项允许提交。测试失败后原位刷新状态并把 429 解释为检查智谱账户余额/并发限制，未吞掉结构化错误。同步更新 React 初始状态、后端/前端测试、生产 bundle 和 Provider/开发进度文档。
- 自动化验证：Model Administration + Knowledge Base Speech 定向测试 `14 passed`，其中探针前两次返回 retryable 429、第三次成功并验证三次尝试；全量 pytest `142 passed, 5 skipped`；Vitest `8 passed`；Python compileall、Vite production build、`git diff --check` 均通过。
- 真实验证证据：首次真实 GLM-TTS 探针返回 `provider_rate_limited`；启用有界重试并重启后再次调用，响应 HTTP `429`、`attempts=3`、invocation `model_invocation_7c6ab23146224165`。同一智谱 ProviderConnection 的真实凭据校验同样返回 HTTP 429，说明请求已越过本地路由/凭据读取/adapter/网络边界并被厂商账户侧拒绝。`speech-options` 实际返回模型、状态和 `tongtong` 声音；浏览器验证模型/声音下拉均可选，保存按钮因模型失败保持禁用。
- 当前边界与恢复：仓库接口不是占位实现；模型列表、声音目录、测试、profile gate、Celery 工作项、资产访问和题目 CRUD/试听均有执行实现与合同测试。但当前智谱账户在厂商校验和 TTS 两条请求上都返回 429，无法诚实完成“真实音频生成/试听”验收。用户处理智谱余额、套餐、并发或风控限制后，点击“测试并启用”；成功会把模型置为 ready，随后保存题库配置即可触发整库生成，无需再改代码。

## 2026-08-27 · AI-QUESTION-GENERATION-001

- 目标：实现基于题库定位、标签和可选要求的智能批量生题，提供生成批次、候选草稿逐题修改/删除、人工确认后批量导入正式题库的完整闭环。
- 状态：`verified`。
- 计划修改：统一领域语言、QuestionGenerationBatch 持久资源、LLM 选择与结构化生成、Celery worker、草稿审核/导入接口、React 审核工作台、题库来源追踪、自动化测试和架构/API/领域/Provider/数据库/进度文档。
- 不变量：AI 结果永远先进入草稿批次，不自动成为正式 Question；HTTP 不等待或执行 LLM；只有 ready 且支持 `llm.chat_json` 的具体模型可生成；草稿编辑/删除和确认导入使用 optimistic version；确认导入后批次冻结且幂等；正式 Question 仍必须通过完整评分依据校验，语音继续由现有 Celery 工作项生成。
- 安全与成本：生成数量限制在 1–30；可选要求和题库上下文长度受限；Celery 消息只携带组织和持久工作项 ID；不记录 Provider 凭据或完整 prompt；失败不产生部分正式题目。
- 实际修改：新增 `QuestionGenerationService` 深模块、批次/草稿持久模型与 Memory/SQLite/PostgreSQL 文档集合、生成选项/批次/草稿 CAS/确认导入 API、`question_generation.generate` worker dispatch、Mock 结构化生题和正式题目来源追踪；KnowledgeBase 增加定位/标签。React 题库详情新增模型/数量/定位/标签/可选要求表单、自动刷新、候选题编辑/删除和明确确认导入；状态统一中文展示。OpenAI-compatible adapter 增加受限的推理前缀/Markdown 包裹 JSON 兼容解析，生成 work lease 调整为 180 秒并启用至少 5 秒持久退避，防止慢响应重复领取。
- 修改文件：`CONTEXT.md`、`app/schemas/api.py`、`app/services/question_generation.py`、`app/services/catalog.py`、`app/api/routes.py`、`app/workers/outbox.py`、`app/providers/mock/provider.py`、`app/providers/openai_compatible/provider.py`、Memory/SQLite/PostgreSQL persistence/repository、`app/web/core/workspace.js`、`app/web/src/core/{WorkbenchProvider,ui}.jsx`、`app/web/src/features/questions/Page.jsx`、`app/web/styles.css`、前后端测试与架构/API/领域/Provider/数据库/检索/进度/路线图文档。
- 自动化验证：全量 pytest `148 passed, 5 skipped`；Vitest `9 passed`；Vite production build 通过；Python compileall、持久化合同、worker/service/API/React 行为和 `git diff --check` 通过。环境未安装 `ruff`，因此该命令不可用，未以此替代现有测试证据。
- 真实验收：API、Redis、Celery worker/Beat 使用最终代码运行；当前 ready DeepSeek V4 Pro 经 Celery 实际生成批次 `question_gen_2abbe405a2d44939`，一次调用完成，耗时 33.493 秒，token 用量 438/2626，状态为 `reviewing`。浏览器确认页面显示“待审核 · 1 道”、候选题编辑/删除、刷新和“确认导入题库”；编辑表单完整回填题干、答案、关键点、技能、难度和题型。未点击确认导入，正式题库保持 1 道，证明草稿隔离成立。
- 失败与恢复留痕：首次真实调用暴露推理模型返回说明/围栏 JSON、输出预算不足，以及旧 60 秒 lease 与约 60 秒两次 Provider 调用重叠造成重复领取/熔断；曾产生两个失败验证批次，但没有产生正式 Question。通过受限 JSON parser、提高结构化输出预算、180 秒 lease、持久退避和 `generating` crash recovery 修复，并增加回归测试；最终真实批次一次完成。历史失败批次按审计语义保留，不删除用户数据。曾在错误目录执行 npm、从 `app/web` 调用不存在的 `.venv/bin/python`、沙箱禁止 `ps`/端口/Redis，均改在正确目录或批准的本机运行方式重试；compileall 首次因 macOS 默认 pycache 目录不在可写边界而失败，改用 `/tmp/interviewer-python-cache` 后通过，均无仓库副作用。
- 未完成事项：本工作项的仓库实现、自动化与当前 DeepSeek 单次真实闭环已完成。生产环境仍需对目标模型账户执行并发、费用、长时间限流和多种题目数量的稳定性验收；候选题是否导入由用户在审核后决定，不属于自动验收动作。

## 2026-08-27 · PROMPT-GOVERNANCE-001

- 目标：建立 `app/core/prompt/` 统一 Prompt 维护 seam，迁移仓库内全部 LLM Prompt；在模型网关统一校验所有声明 JSON Schema 的 AI 响应，避免格式或内容不合规进入业务模块；把两项规则写入根 `AGENTS.md`。
- 状态：`verified`。
- 计划修改：盘点并迁移智能生题、简历审阅、答案评分和模型探针 Prompt；新增集中模板接口和结构化响应校验器；让所有 `ChatJSONRequest` 在网关返回前执行 schema/业务基础规则校验；补齐单元与回归测试，并同步架构、Provider、开发进度和操作留痕。
- 不变量：Provider 仍负责协议与 JSON 文本解析，业务 Prompt 不进入 Provider；格式校验失败必须返回结构化、可观察且不会泄露完整响应的错误；业务模块仍可在统一格式校验之后执行更严格的领域规则。
- 实际修改：新增 `app/core/prompt/contracts.py` 与 `validation.py`。`prompt_contract(name, context)` 集中提供智能生题、答案评分、简历审阅、JSON/text 模型探针和智谱 credential probe 的版本化消息及响应 Schema；OpenAI-compatible JSON Object 强化指令也移入该目录。QuestionGeneration/Evaluation/Talent/ModelAdmin/Zhipu Provider 删除内嵌 Prompt，成功结果或请求 metadata 记录 prompt version。Model Gateway 删除内嵌校验实现，统一调用 `validate_structured_response`，覆盖类型、required、enum、min/max items、min/max length、数值边界、unique items、additionalProperties 和仅空白字符串，并将失败映射为 `provider_schema_invalid` 后再执行 route 重试/fallback。
- 项目规则：根 `AGENTS.md` 新增“Prompt 与 AI 响应治理”，明确所有 Prompt/响应 Schema 必须位于 `app/core/prompt/`，指定格式 AI 响应校验通过前不得写领域对象或触发后续任务；推荐目录树同步增加该 package。AST 治理测试会拒绝在该目录外新增 `ChatMessage(...)` Prompt 构造。
- 修改文件：`AGENTS.md`、`app/core/prompt/{__init__,contracts,validation}.py`、`app/model_gateway/gateway.py`、`app/providers/{openai_compatible,zhipuai}/provider.py`、`app/services/{question_generation,evaluation,talent,model_admin}.py`、`tests/test_prompt_governance.py`、`tests/test_model_configuration_v2.py`，以及架构、领域、Provider、开发进度和本变更日志。
- 验证：`PYTHONPYCACHEPREFIX=/tmp/interviewer-python-cache .venv/bin/python -m compileall -q app tests` 通过；Prompt/网关/生题/模型配置/简历闭环定向测试通过；全量 pytest `155 passed, 5 skipped`；`git diff --check` 通过。代码扫描确认业务代码中的 `ChatMessage` 构造只存在于 `app/core/prompt/contracts.py`（类型声明除外）。
- 运行验收：最终代码已重启 API（PID `89420`）和 Celery Worker/Beat；`GET /healthz` 返回 `ok`，worker 连接本机 Redis 并进入 ready，durable dispatcher 正常且无待派发任务。
- 失败与恢复：一次组合 `rg` 因 shell 引号不匹配只读失败；一次大 patch 因 OpenAI-compatible helper 实际上下文不同未应用，拆成精确补丁后成功；首次定向测试仍期待宽松探针 schema，更新为严格 `message == pong` 合同后通过。为载入最后的 prompt version 修改做 warm shutdown 时 Celery 退出码为 1，并把一个无业务 payload 的周期 dispatcher 唤醒消息重新入队；新 worker 启动后消费两个 dispatcher 消息，均 `dispatched: 0`，没有业务数据或外部模型副作用。
- 未完成事项：当前仓库内已有 LLM Prompt 均已迁移并受静态治理测试保护；未来新增 Prompt 和指定格式响应必须按 `AGENTS.md` 规则扩展同一合同 seam。目标生产模型仍需持续验收不同厂商对严格 Schema 的遵循稳定性。

## 2026-08-27 · QUESTION-GENERATION-FANOUT-001

- 目标：把智能生题从单次大响应改造成“AI 蓝图规划 → 小批量 Celery 子任务 → 父批次统一校验/去重/补槽 → 人工审核”，解决一次生成 10 道题的 Provider 超时，并降低并行生成的相似/重复题。
- 状态：`verified`。
- 计划修改：在 `app/core/prompt/` 新增蓝图规划与按槽位生成合同；扩展 QuestionGenerationBatch 和 DurableWorkItem 父子状态；worker 支持规划、分片、合并与定向补生成；建立蓝图互斥键、规范化哈希、词元相似度和单写者合并规则；保持现有创建/查询/审核/导入接口兼容，补齐失败恢复、幂等和测试，并同步架构/API/领域/Provider/进度文档。
- 不变量：HTTP 仍只创建父批次和 durable work；所有外部模型调用仍仅由 `app/workers/` 执行；子任务不得直接写 GeneratedQuestionDraft；Prompt/Schema 只位于 `app/core/prompt/`；任何候选结果必须先通过统一结构化校验和父批次去重；已导入正式题目和历史失败批次不改写。
- 实际修改：新增 `question_blueprint_planning.v1` 与 `question_blueprint_generation.v1` 两个集中 Prompt/Schema 合同；父批次改为 `question_generation.plan -> question_generation.generate_chunk -> question_generation.merge` 持久工作流。规划结果冻结与目标数量一致的 QuestionBlueprint，生成工作每项最多两个槽位并独立使用 90 秒 route，merge 是唯一 GeneratedQuestionDraft 写入者；它按槽位顺序合并、与活动正式题/已接受题做规范化完全匹配和高阈值文本相似检查，并对缺失槽位最多定向补生成两轮。旧 `question_generation.generate` work kind 可由新规划处理器读取，历史批次与接口路径不迁移、不改写。
- 前端与可观察性：批次投影新增 `generation_progress`，公开 phase、规划数、子任务完成数、接受/过滤数和补生成轮次；React 审核卡显示规划、并行生成、合并、补生成四阶段以及真实子任务进度条，保留现有自动轮询、草稿编辑/删除和确认导入行为。生产 bundle 已重建。
- 修改文件：`app/core/prompt/contracts.py`、`app/services/question_generation.py`、`app/providers/mock/provider.py`、`app/workers/outbox.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`tests/test_question_generation.py`、`tests/test_prompt_governance.py`、`CONTEXT.md` 及架构/API/领域/检索/Provider/进度/路线图文档。
- 自动化验证：Mock 10 题链路证明先产生 10 个蓝图，再形成 5 个不超过 2 题的子工作，第三轮 dispatcher 完成 merge 并得到 10 道唯一候选题；定向 Python `12 passed`，全量 Python `157 passed, 5 skipped`，Vitest `10 passed`，Vite production build、Python compileall 和 `git diff --check` 均通过。编译检查首次未指定 pycache 时被 macOS 用户缓存目录权限拒绝，随后以 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-python-cache` 通过，无仓库副作用。
- 真实运行与历史失败：改造前真实批次 `question_gen_55b25541a5674c0d` 的 10 题大请求连续命中 60 秒 Provider timeout，最终 dead-letter，未生成草稿或正式 Question；该历史失败按审计语义保留且未自动重放，避免额外模型费用。最终代码已重启本地开发模式 API（PID `94092`）与 Celery Worker/Beat，`GET /` 和 `/healthz` 返回 200，Redis dispatcher 正常。首次重启误沿用生产 runtime，首页按预期返回 401；随即 warm shutdown 并显式覆盖为 development，同时保留现有 SQLite、Provider 凭据和数据。
- 未完成事项：仓库内规划、扇出、合并、补生成、前端进度和自动化证据已完成。新的真实 DeepSeek 10 题调用会产生外部费用，未擅自提交；需要由用户从页面创建新批次后验收目标账户的并发限流、耗时与成本。生产 PostgreSQL/Redis 多实例并发仍属于部署环境验收。

## 2026-08-27 · QUESTION-GENERATION-WORKBENCH-001

- 目标：把智能生题从题库详情内联卡片改造成独立任务工作台，提供批次历史、规划/分片/合并进度、单个或全部失败任务重试、持久化停止、停止后继续未完成项，以及候选题审核与导入。
- 状态：`verified`（仓库与本机 API/Redis/Celery/React 浏览器）。
- 计划修改：扩展 QuestionGenerationBatch 生命周期、执行 revision、控制事实和子任务投影；新增停止、继续、重试失败项、单分片重试接口；所有 Worker 在外部模型调用前和结果提交前执行停止/revision guard；增加 `#questions/{knowledge_base_id}/generation[/{batch_id}]` React 路由级页面；补齐后端/前端行为测试与架构、接口、领域、数据库、进度和路线图文档。
- 不变量：数据库批次与 DurableWorkItem 仍是任务真相来源，不能用 Celery result 或 `terminate=True` 作为停止事实；停止后不再发起新模型调用，已经在途且不可撤销的 Provider 请求允许返回但其旧 revision 结果必须丢弃；重试只补失败/未完成槽位并保留成功结果；前端不直接调用通用管理员 Outbox replay；草稿在人工确认前仍不能成为正式 Question。
- 安全与成本：停止、继续和重试都要求 optimistic version 与幂等保护并记录操作者/原因；页面只展示结构化错误、attempt、时间和受限 Provider 摘要，不返回完整 Prompt、AI 原始响应或凭据。浏览器验收没有创建、停止、恢复或重试任何批次。
- 实际修改：`app/persistence/interface.py` 增加协作式 Outbox cancel、开始/结束时间和结构化错误元数据；`app/services/question_generation.py` 增加 execution revision、stop/resume/retry-failed/retry-chunk、Worker 双重 guard、控制历史和 `tasks/available_actions` 投影；`app/schemas/api.py` 与 `app/api/routes.py` 增加四个控制合同。React router/workspace query 增加独立生成子路由，`QuestionsPage` 增加批次历史、任务详情、分片表、错误与人工操作记录，并把题库详情入口改为导航；同步更新样式、生产 bundle、前后端测试、`CONTEXT.md` 及架构/API/领域/数据库/进度/路线图文档。
- 自动化验证：`git diff --check`、Python compileall 通过；全量 pytest `160 passed, 5 skipped`；Vitest `12 passed`；Vite production build 通过。新增测试覆盖排队停止/恢复、在途旧 revision supersede、单失败分片重试不重做成功项，以及 React 停止/分片重试只调用领域接口而不访问通用 `/admin/work-items/*`。
- 运行与浏览器验收：development API 运行于 `127.0.0.1:8000`，Celery Worker/Beat 连接本机 Redis DB 2。真实 React 页面列出 5 个历史批次；历史 timeout 批次展示 dead-letter、`5/5` 和“重试失败项”；运行批次展示 10 个规划方向、5 个 `slot_01..slot_10` 分片与各自 attempt。该既有批次最终完成 5/5 分片与 merge，进入 `reviewing`，生成 10 个待审核候选且开放“导入”操作。浏览器仅做读取和路由切换，未提交控制命令，并把工作台页面保留给用户。
- 失败与恢复：首次在 sandbox 内启动 API/Worker 分别因本机端口绑定和 Redis 连接权限被拒绝；停止该 Worker 后使用已批准的本机开发命令成功启动。首次新增前端路由后两个旧行为测试仍假设内联弹窗，更新为子页面合同后通过；补充控制行为测试后最终 Vitest 全绿。启动 Celery 时数据库原本已有一条 claimable 的 10 题批次，Beat 按既有持久事实自动派发，DeepSeek 规划请求返回 200 并继续扇出 5 个分片；这不是本轮新建/重试任务，但确实可能产生该既有任务的供应商费用，未删除或伪造其审计事实。
- 未完成事项：仓库功能已完成。Provider 已接受的在途请求无法由通用停止命令保证立即撤销，系统只保证停止后不发起新调用且旧 revision 结果不入库；目标生产 PostgreSQL/Redis 多实例、Provider 并发/限流/费用仍按环境清单验收。

## 2026-08-27 · QUESTION-DRAFT-DETAIL-SINGLE-IMPORT-001

- 目标：让智能生题候选列表的非操作区域可点击查看完整题目详情，并支持把单个候选题独立导入正式题库，同时保留编辑、删除与批量导入。
- 状态：`verified`（仓库与本机 API/Redis/Celery/React 浏览器）。
- 计划修改：先补充单候选导入 API/领域合同和幂等、版本语义，再实现候选行点击详情、操作按钮事件隔离与单题导入交互，最后补齐前后端测试、生产 bundle、接口/领域/进度文档和本地运行验收。
- 不变量：单题导入仍必须通过正式 Question 评分依据校验；已导入候选不能重复导入；其他草稿继续保留可审核，批次不因一次单题导入而冻结；编辑、删除、导入按钮不得冒泡触发行详情；历史批次和正式题目不被改写。
- 实际修改：新增 `POST /api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}/import`；QuestionGenerationService 为单题导入冻结草稿、写入幂等 `knowledge_base.import` 工作并投影单题任务，Catalog worker 成功后只把对应草稿置为 imported，失败按 DurableWorkItem 是否还能重试投影 importing/failed。批量导入排除已导入题并拒绝与在途单题导入并发，正式 Question 继续以 generation batch/draft 来源去重。React 候选行改为鼠标/键盘可打开详情，展示题干、标准答案、技能、难度、题型和评分关键点；操作区隔离冒泡并新增“单独导入”，导入中/成功后冻结编辑与删除。
- 修改文件：`app/services/question_generation.py`、`app/services/catalog.py`、`app/api/routes.py`、`app/web/src/features/questions/Page.jsx`、`app/web/styles.css`、`app/web/dist/`、`tests/test_question_generation.py`、`app/web/src/App.test.jsx`，以及接口、领域、数据库、开发进度、路线图和本日志。
- 验证：定向 QuestionGeneration 测试 `8 passed`；全量 Python `161 passed, 5 skipped`；Vitest `12 passed`；Vite production build、Python compileall 和 `git diff --check` 通过。新增后端合同覆盖单题导入提交/幂等、批次保持 reviewing、成功草稿冻结、剩余两题批量导入且最终无重复；React 行为覆盖行点击详情、编辑按钮不触发详情和单题导入领域接口。
- 运行验收：API 已以 PID `8890` 重启，`/healthz` 返回 ok，OpenAPI 包含单题导入路径；Celery Worker/Beat 主进程 PID `8934` 连接 Redis DB 2 且 dispatcher 无待处理工作。真实页面显示 10 个可点击候选行、每行“单独导入/编辑/删除”和详情提示；只打开并关闭详情、编辑和单题导入确认弹窗，没有提交导入、编辑或删除，浏览器 console 0 warning/error。
- 未完成事项：仓库闭环已完成；目标生产 PostgreSQL/Redis 多实例、真实对象存储和 TTS 仍按既有环境验收清单执行。本轮没有调用 LLM/TTS，也没有产生新的供应商费用。

## 2026-08-27 · QUESTION-REVIEW-PRIORITY-001

- 目标：移除正常生题批次中占据大量首屏空间的 Worker 流程表，把候选题审核提升为任务详情的首要内容；失败恢复能力继续保留，但只展示真正失败且可操作的最少信息。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：调整 React 批次详情的信息层级，保留运行态紧凑进度、状态命令和失败项重试，隐藏成功的规划/分片/合并明细；更新行为测试、生产 bundle、开发进度/路线图与本日志，并在真实工作台验证候选题进入首屏。
- 不变量：后端 tasks、stop/resume/retry 合同和审计事实不删除；只是减少 UI 暴露，失败分片仍能单独重试，候选题详情/编辑/删除/单题导入/批量导入保持不变。
- 实际修改：`QuestionGenerationBatchPanel` 删除正常态 Worker 子任务表，把候选题审核移动到批次标题正下方；queued/generating/stopping/importing 继续显示紧凑阶段文案和进度条，failed 只显示带错误原因和重试按钮的失败项，人工控制历史仍保留。同步增加精简失败项样式、更新 React 行为测试、重建 production bundle，并修正文档中的工作台信息层级说明。
- 修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证：Vitest `12 passed`，Vite production build 与 `git diff --check` 通过。真实工作台刷新后 DOM 确认不存在“Worker 子任务”，候选题审核和第一道候选题均直接可见，浏览器 console 0 warning/error；未点击任何业务操作。
- 未完成事项：无仓库内功能缺口。本次只调整信息层级，没有修改后端合同、数据或供应商调用，也没有产生外部费用。

## 2026-08-27 · MODEL-ADMIN-UX-001

- 目标：把模型服务从资源堆叠页面改造成“厂商连接 → 模型配置 → 业务用途”三步任务流，修复选择切换后名称不联动和用途/能力可错配的问题，并让管理员直接看到正式面试所需路由的配置完成度。
- 关联问题：模型服务首屏信息层级倒置、Provider/模型切换保留旧显示名称、路由用途为自由文本且默认可能与模型能力冲突、缺少必需业务用途 readiness 清单和资源关系说明。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：`app/web/src/features/models/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/styles.css`、`app/web/src/App.test.jsx`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 不变量：继续复用 Provider manifest 动态表单、ProviderConnection/ModelConfiguration/ModelRoute 后端资源和现有 REST 请求语义；只有 enabled + ready 且支持目标 capability 的模型可成为路由目标；不在前端复制模型网关执行、健康或 readiness 业务判断。
- 实际修改：模型服务页重排为连接、模型、业务用途三段连续任务流，并增加顶部完成度卡片；业务用途覆盖表固定展示 7 项核心用途和 2 项可选用途、缺失影响、目标模型与可操作状态。路由表单不再接受自由文本用途或手填 capability，而是由用途自动推导 capability 并过滤兼容的 ready 模型；Provider、模型类型和模型切换会同步建议显示名称。已安装插件目录移至折叠区，未实现项明确标记“暂未接入”。后端资源、路由请求体与模型网关执行 seam 均未改变。
- 修改文件：`app/web/src/features/models/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/styles.css`、`app/web/src/App.test.jsx`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 验证命令与结果：`cd app/web && npm test -- --run` 通过，Vitest `14 passed`；`npm run build` 通过并重建 production bundle；目标文件 `git diff --check` 通过。真实工作台 `/web/#models` 验证三步导航、完成度、用途覆盖与兼容模型过滤均可见，回答评分表单只展示支持 `llm.chat_json` 的 ready 模型，浏览器 console 0 warning/error；未提交配置、未调用供应商。
- 未完成事项或恢复说明：无仓库内功能缺口。当前页面显示的未配置用途仍需管理员按实际供应商凭据建立兼容模型和路由；本次没有修改现有模型数据，也没有产生外部费用。

## 2026-08-27 · MODEL-PROVIDER-DRILLDOWN-001

- 目标：把模型服务改为与题库一致的父子层级：首页只展示已接入厂商，点击厂商进入子页面，并在当前厂商上下文中完成模型增删改查、模型类型查看和模型测试。
- 关联问题：当前模型服务把所有厂商、所有模型和全部业务用途铺在同一长页面，模型与所属厂商的层级关系不够直接，管理员添加模型时还需要重复选择厂商。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：`app/web/core/router.js`、`app/web/src/features/models/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 不变量：继续使用现有 ProviderConnection/ModelConfiguration/ModelRoute 接口和动态 manifest 表单；子页面只筛选当前 provider connection 下的模型，不改变后端资源归属、测试、删除级联或路由 readiness 语义。
- 实际修改：路由新增 `#models/{provider_connection_id}` 厂商子页面；模型服务首页改为已接入厂商卡片目录，展示厂商状态、模型总数、测试就绪数和已配置模型类型，点击卡片进入厂商管理。子页面展示连接状态与当前厂商模型摘要，模型列表直接展示中文模型类型、原始 `model_type`、厂商模型 ID、支持能力和测试状态，并提供查看、编辑、测试、删除操作。添加模型从厂商子页面发起，所属厂商以只读信息展示且请求固定写入当前 connection，避免重复选择或跨厂商误配；连接编辑、校验和级联删除仍在子页面提供。业务用途与插件清单保留在首页折叠区。
- 修改文件：`app/web/core/router.js`、`app/web/src/core/WorkbenchProvider.jsx`、`app/web/src/features/models/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 验证命令与结果：`cd app/web && npm run build && npm test -- --run` 通过，Vite production build 成功，Vitest `14 passed`；目标文件 `git diff --check` 通过。真实工作台验证首页显示 3 个已接入厂商，点击 DeepSeek 进入 `#models/{connection_id}`，子页面只显示该厂商的 DeepSeek V4 Pro，模型类型显示“大语言模型 / llm”，查看、编辑、测试、删除按钮齐全；“添加模型”弹窗固定显示所属厂商 DeepSeek 且不存在厂商选择框，浏览器 console 0 warning/error。验收未提交、测试或删除任何真实配置。
- 未完成事项或恢复说明：无仓库内功能缺口。本次没有修改后端模型资源或现有配置，也没有调用外部模型供应商或产生费用。

## 2026-08-27 · QUESTION-GENERATION-CREATE-MODAL-001

- 目标：把智能生题任务配置从页面展开卡改为点击“新建生题任务”后打开的弹窗，删除“收起配置”状态，让任务历史和候选题始终保持页面主体。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：重构 QuestionGenerationWorkbench 的创建入口和 Modal 生命周期，更新 React 行为测试、移除废弃展开式样式、重建 production bundle，并同步进度/路线图和本日志。
- 不变量：创建请求、模型/数量/定位/标签/可选要求字段、幂等键和成功后导航到批次详情的行为不变；打开或关闭弹窗不创建任务、不调用模型。
- 实际修改：QuestionGenerationWorkbench 删除 `creating` 展开状态和内联 `generation-create-card`，顶部固定显示“新建生题任务”；点击后由统一 Modal 承载原 QuestionGenerationForm，提交成功先关闭弹窗再导航到新批次。移除废弃 CSS，更新 React 行为测试和工作台进度/路线图说明，重建 production bundle。
- 修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证：Vitest `14 passed`，Vite production build 与 `git diff --check` 通过。真实工作台刷新后确认“新建生题任务”按钮可见、页面无“收起配置”和内联表单；点击后弹窗包含生题模型、生成数量与题库定位，关闭后没有创建任务，浏览器 console 0 warning/error。
- 未完成事项：无仓库内功能缺口。本次没有提交生题请求、调用模型或修改现有批次数据。

## 2026-08-27 · QUESTION-DRAFT-IMPORT-CONCURRENCY-001

- 目标：修复连续单题导入时，上一题推进 QuestionGenerationBatch version、下一题仍携带旧 expected_version 而返回 `PERSISTENCE_CONFLICT` 的竞态。
- 状态：`verified`（仓库测试与本机运行态）。
- 计划修改：把单题导入从整批版本硬拒绝调整为草稿级条件命令，在目标草稿仍未导入且幂等键未冲突时基于事务内最新批次版本提交；前端刷新完成前保持确认弹窗，补齐连续导入/真正冲突测试、接口与领域文档，重建并重启本机服务。
- 不变量：同一草稿不能重复导入；编辑/删除仍使用批次 CAS；批量导入与在途单题导入仍互斥；正式 Question 继续以 generation batch/draft 来源去重，不因放宽无关批次版本而丢失更新。
- 初始诊断：用户请求携带 expected_version 23，而持久批次已是 24，说明第一题命令或 Worker 完成事实已经推进聚合版本。首次只读复查时本机 8000 端口已不再监听，未产生数据副作用，待代码修复后统一重启。
- 实际修改：GeneratedQuestionDraft 增加独立 `version`，PATCH 草稿只推进目标草稿与批次版本；单题导入请求新增可选 `expected_draft_version`，服务端兼容旧 `expected_version` 但不再用无关的批次旧版本拒绝目标草稿，而是在事务内以读取到的最新批次版本写入。前端发送草稿版本，并把刷新完成放在关闭确认弹窗之前，避免短暂暴露旧页面状态。批量导入、编辑、删除的原批次 CAS 规则保持不变。
- 修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/question_generation.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`tests/test_question_generation.py`，以及接口、领域、数据库、开发进度和本日志。
- 验证：定向 QuestionGeneration `8 passed`；全量 Python `161 passed, 5 skipped`；Vitest `14 passed`；Vite production build、Python compileall 与 `git diff --check` 通过。回归合同使用同一个旧 batch version 紧接提交两个不同草稿，均返回 202；随后编辑第三个草稿并以旧 draft version 导入，仍正确返回 `PERSISTENCE_CONFLICT`；最终单题与剩余批量组合共生成 3 个且无重复 Question。
- 运行验收：API 已以 PID `18150` 启动，Celery Worker/Beat 已连接 Redis DB 2 且 dispatcher 无待处理工作。只读确认用户批次当前 version 24、前两题 imported、目标 `question_draft_c07a9995b9ce4542` 仍为 pending/version 1；没有替用户重放失败请求或导入该题。
- 失败与恢复：第一轮定向测试发现 `expected_draft_version` 参数误落在相邻的批量导入方法签名，导致路由 TypeError；精确移动到单题方法后定向、全量与前端测试全部通过，没有持久化或外部调用副作用。
- 未完成事项：无仓库内功能缺口。本轮没有调用 LLM/TTS，也没有替用户修改现有题库；目标生产 PostgreSQL 多实例的真正并发提交仍按环境清单做部署验收。

## 2026-08-27 · KNOWLEDGE-BASE-VOICE-LABEL-001

- 目标：移除题库卡片上面向用户暴露的内部 `model_configuration_id`，把卡片底部改为只展示具有明确“读题语音”标识的声音配置与配置状态。
- 状态：`verified`（React 自动化与本机浏览器）。
- 计划修改：调整题库目录卡片的语音信息结构和样式，补充 React 行为测试，重建 production bundle，并在真实题库目录进行只读浏览器验收。
- 不变量：不改变题库、TTS 模型、声音配置或语音构建数据；模型配置详情仍在题库详情/模型服务中管理；本项只修正目录投影的用户界面表达，不新增接口或供应商调用。
- 实际修改：题库卡片删除 `model_configuration_id · voice_profile_id` 原始拼接，改为独立“读题语音”信息块；已配置时只显示声音 profile 标识和“已配置”，未配置时显示“未配置/待配置”。同步补充清晰的层级、边框和状态样式，并重建生产 bundle。
- 修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/styles.css`、`app/web/src/App.test.jsx`、`app/web/dist/`、`docs/development-progress.md`、`docs/change-log.md`。
- 验证：Vitest `15 passed`，Vite production build 与目标文件 `git diff --check` 通过。本机 API 以 PID `20751` 重新启动；真实 `#questions` 页面卡片显示“读题语音 / tongtong / 已配置”，DOM 中 `model_cfg_` 数量为 0，浏览器 console 0 warning/error。
- 失败与恢复：首次浏览器验收时 8000 端口尚未监听，按既有 development 配置启动 API 后恢复；一次最终构建命令误在仓库根目录执行并因没有 `package.json` 返回 `ENOENT`，随后在 `app/web` 重跑成功且没有文件副作用。sandbox 内 curl 因本机网络隔离显示连接失败，使用获批的本机只读健康检查得到 `{"status":"ok"}`。
- 未完成事项：无仓库内功能缺口。本项没有修改后端接口或持久数据，没有调用 TTS/LLM，也没有产生供应商费用。

## 2026-08-27 · POSITION-KNOWLEDGE-BASE-ASSIGNMENT-001

- 目标：把招聘流程中的“添加题库”从重复创建题库并手填语言/音色，改为从组织已有题库中选择并关联；岗位直接复用题库的题目、KnowledgeBaseSpeechProfile、音色和现有语音资产。
- 状态：`verified`（仓库测试、生产构建与本机运行态）。
- 计划修改：为 JobPosition 增加显式 `knowledge_base_ids` 关联并提供幂等关联 API；Question Catalog 与 Interview Plan Assembly 按岗位关联验证题库范围；React 招聘流程改为已有题库下拉选择并展示题库语音摘要；补齐后端/React 合同测试，更新架构、接口、领域模型、检索、统一语言、开发进度、路线图和本日志，重建生产 bundle。
- 不变量：题库仍由题库模块统一维护，关联操作不复制、不改写题目或语音配置，不调用 TTS/LLM；组织隔离、题库 readiness、计划冻结、检索 scope 和历史面试快照继续保持；旧数据中 `KnowledgeBase.job_position_id` 继续作为初始/兼容关联读取。
- 实际修改：JobPosition 新增 `knowledge_base_ids` 显式关联，创建题库时自动建立初始关联，旧 `KnowledgeBase.job_position_id` 继续投影为兼容关联；新增 `POST /job-positions/{id}/knowledge-base-assignments` 幂等命令并保留岗位 version CAS。Question Catalog 和 Interview Plan Assembly 先验证目标岗位已关联全部题库，再按租户、题库、活动状态、结构化条件和语音 readiness 查询；底层不再用 Question 的创建岗位阻断共享题库。招聘流程弹窗改为仅选择尚未关联的已有题库，选项展示题库声音，删除名称/说明/语言/音色输入；岗位卡片展示已关联题库及声音。计划页同步按岗位关联验证题库。关联过程不复制或修改题目、speech profile 或语音资产，也不调用供应商。
- 修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/catalog.py`、`app/services/plan_assembly.py`、`app/persistence/memory.py`、`app/persistence/sqlite.py`、`app/persistence/postgresql.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/features/plans/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/test_position_resume_appointment_flow.py`、`tests/test_persistence_contract.py`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`CONTEXT.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md` 和本日志。
- 验证：定向跨岗位关联合同 `1 passed`；全量 Python `161 passed, 5 skipped`；Vitest `16 passed`；Vite production build、Python compileall 和 `git diff --check` 通过。本机 API 已重启为 PID `28417`，`/healthz` 返回 `ok`，OpenAPI 已包含新的 assignment POST 路径。
- 失败与恢复：首次组合验证误在 `app/web` 工作目录调用根目录 `.venv`，未执行测试；首次全量测试暴露两条持久层合同仍按 Question 创建岗位过滤，更新为题库关联边界后通过；一次测试补丁多出单行缩进导致收集失败，修正后全量通过；sandbox 内本地 curl 受网络隔离，改用获准的本机只读检查成功。以上失败均无业务数据或外部调用副作用。
- 未完成事项：无仓库内功能缺口。本轮未修改现有题库、题目、岗位关联或语音配置，未调用 TTS/LLM，也未产生供应商费用；目标生产 PostgreSQL 规范化关联表仍按既有部署迁移流程实施和验收。

## 2026-08-27 · POSITION-LIFECYCLE-CASCADE-001

- 目标：补齐岗位新增、查看、编辑、删除工作流；删除岗位前展示明确的级联影响并要求输入岗位名称确认，确认后清除该岗位关联的全部候选人敏感数据并从活动工作区隐藏岗位和候选人。
- 状态：`completed`。
- 领域决策：规范术语为 Position Candidate Membership（岗位候选关系）。新录入候选人显式选择一个应聘岗位；旧数据通过 CandidateProfile、ResumeReview、InterviewPlan、InterviewAppointment 和 InterviewSession 的岗位引用推导归属。岗位删除对命中的候选人执行隐私 purge，而非破坏审计/历史引用的物理级联；共享 KnowledgeBase 不随岗位删除。
- 计划修改：扩展 CandidateProfile 岗位归属与岗位删除影响预览/确认命令；复用 RetentionService 清除候选人私有文件和敏感投影，归档岗位及相关要求/计划并取消预约；React 增加岗位编辑、危险删除弹窗、候选人岗位选择和展示；补齐合同/行为测试并同步架构、接口、领域、存储、统一语言、开发进度、路线图和本日志。
- 不变量：未经岗位名称精确确认不得删除；删除影响必须限于当前组织和目标岗位；历史面试与审计保留不可识别占位；题库内容、题库语音及其他岗位不受影响；删除命令不调用模型供应商。
- 实际修改：`app/schemas/api.py`、`app/api/routes.py`、`app/services/catalog.py`、`app/services/talent.py`、`app/services/retention.py` 增加候选人显式岗位归属、岗位删除影响预览、名称确认命令、候选人敏感数据清除、岗位/要求/计划归档、预约取消和删除审计；`app/web/src/features/workflow/Page.jsx` 增加岗位编辑/危险删除、候选人岗位选择和展示，弹框明确列出实际影响并使用“永久删除岗位及候选人”按钮；`app/web/src/App.test.jsx` 与 `tests/test_position_resume_appointment_flow.py` 覆盖编辑、弹框、精确确认、级联范围、其他岗位候选人与共享题库保留。同步更新 `CONTEXT.md`、架构、API、领域、存储、开发进度、路线图及生产前端 bundle。
- 验证：针对性 Vitest `19 passed`，岗位/留存 Python `4 passed`；最终 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-position-lifecycle-pyc .venv/bin/python -m compileall -q app tests`、`.venv/bin/python -m pytest -q`（`165 passed, 5 skipped`）、`npm --prefix app/web test -- --run`（`19 passed`）、`npm --prefix app/web run build` 和 `git diff --check` 全部通过。开发服务已重启为 PID `32351`，`GET /healthz` 返回 `{"status":"ok"}`。
- 失败与恢复：首次全量回归发现 CatalogService 在普通岗位读取时提前初始化 RetentionService，生产测试因无文件签名密钥失败；改为仅在实际删除命令中惰性初始化后全量通过。首次在 sandbox 内停止旧 PID 被权限拒绝，获准后仅停止该进程并成功重启，无数据副作用。
- 未完成事项：无仓库内功能缺口。目标生产环境仍须按既有流程验证私有存储删除权限与事务故障恢复；本轮没有删除现有业务数据，也未调用模型供应商。

## 2026-08-27 · CANDIDATE-SCREENING-CRUD-001

- 目标：在候选人添加简历时按目标岗位执行可解释初筛，在候选人列表展示符合性、入选/淘汰依据和人工复核状态，提供简历查看及候选人完整增删改查，并让未通过初筛的数据在 7 天后进入自动留存清理范围。
- 关联问题：候选人当前只有创建/列表/修改和简历上传，缺少岗位维度初筛、人工复核、简历展示、候选人删除入口及未通过初筛的差异化留存规则。
- 状态：`verified`。
- 计划修改：扩展 ResumeReview 的岗位初筛结论与证据、人工复核命令和候选人筛选投影；增加候选人归档删除与筛选接口；调整留存策略、React 招聘流程、Prompt 合同和测试；同步架构、接口、领域、检索、数据库、统一语言、进度与路线图文档并重建生产前端。
- 安全与产品约束：初筛只使用脱敏简历和工作能力要求，不使用受保护属性；AI 结论是可复核的岗位匹配建议，不是自动录用决定；人工覆盖保留 AI 原结论和审计；简历继续通过短期受控地址展示；清理沿用现有可审计留存删除 seam，不在页面或读取请求中直接物理删除。
- 实际修改：`app/core/prompt/contracts.py` 将简历审阅升级为 `resume_review.v2`，统一校验初筛枚举、分数、摘要、命中项和缺口；`app/providers/mock/provider.py` 提供离线确定性初筛。`app/services/talent.py`、`app/schemas/api.py`、`app/api/routes.py` 增加最新岗位初筛投影、人工复核、候选人逻辑删除和审计；`app/services/retention.py`、`app/workers/{retention.py,celery_app.py}` 增加仅处理到期 `screening_unqualified` 的周期清理。`app/web/src/features/workflow/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/styles.css` 增加候选人增删改查、符合性列、依据详情、人工复核及受控简历查看，并重建 `app/web/dist/`。新增 `tests/test_candidate_screening.py`，扩展 Prompt 和 React 行为测试；同步更新 `CONTEXT.md` 及架构、API、领域、检索/评分、Provider、存储、进度和路线图文档。
- 验证命令与结果：`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_screening_pyc .venv/bin/python -m compileall -q app tests` 通过；`.venv/bin/python -m pytest -q` 为 `165 passed, 5 skipped in 7.49s`；`cd app/web && npm run build && npm test -- --run` 生产构建成功、Vitest `19 passed`；`git diff --check` 通过。定向初筛/Prompt/简历/留存测试此前为 `15 passed`。
- 未完成事项或恢复说明：无仓库内功能缺口；未调用真实模型供应商，也未清除现有业务数据。目标部署必须运行 Celery worker 与 Beat 才会执行 7 天自动清理，并用企业授权的真实简历样本校准初筛质量、公平性及误淘汰率；真实 OSS 删除权限仍按既有环境验收流程验证。

## 2026-08-27 · POSITION-INITIAL-REQUIREMENT-001

- 目标：把首版岗位要求合并到“新建岗位”流程，确保岗位创建完成后即可用于候选人简历初筛。
- 状态：`verified`。
- 计划修改：先更新岗位创建 API 合同，再增加岗位与首版要求的原子创建命令；React 新建岗位弹窗补齐要求说明、必备/加分技能、级别和面试时长；增加 API 与前端行为测试并同步领域、进度和路线图文档。
- 不变量：岗位与首版要求必须同组织、同事务创建，任一字段校验或持久化失败不得留下无要求岗位；技能输入规范化后再形成解析画像；本功能不调用模型供应商。
- 实际修改：`app/schemas/api.py` 为岗位创建合同增加受严格校验的 `initial_requirement`；`app/services/roles.py` 抽出统一岗位要求 document 构建逻辑，`app/services/catalog.py` 在一个持久化事务中创建 JobPosition 与首版 RoleRequirement，并在岗位响应中返回 `initial_role_requirement`。`app/web/src/features/workflow/Page.jsx` 将岗位要求标题、职责与要求、必备/加分技能、目标级别和面试时长合并到新建岗位弹窗，技能支持中英文逗号、顿号和换行分隔，成功后刷新岗位要求数据；岗位卡片按现有版本数展示“添加岗位要求/新增要求版本”，兼容升级前岗位。`tests/test_candidate_screening.py` 与 `app/web/src/App.test.jsx` 覆盖原子 API 合同、无效请求不留岗位、完整新建表单和既有岗位补要求；同步更新 API、架构、领域、进度和路线图并重建生产前端。
- 验证：定向 Python `3 passed`、最终 Vitest `21 passed`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer_position_requirement_pyc .venv/bin/python -m compileall -q app tests` 通过，`.venv/bin/python -m pytest -q` 为 `166 passed, 5 skipped in 7.71s`，`npm test -- --run` 为 `21 passed`，最终 `npm run build` 成功，`git diff --check` 通过。
- 失败与恢复：首次定向命令误在 `app/web` 目录查找 `.venv/bin/python`，立即以“文件不存在”结束，没有执行测试写入或产生业务数据副作用；随后在仓库根目录重跑通过。
- 未完成事项：无仓库内功能缺口；兼容 API 仍允许旧客户端省略首版要求，新 React 工作台始终提交。未调用模型供应商，也未修改现有岗位数据。

## 2026-08-27 · POSITION-KB-MANAGEMENT-UX-001

- 目标：修复岗位已关联组织内全部题库时按钮显示“暂无可选题库”且被禁用造成的误解，让岗位题库关系始终可查看和继续管理。
- 状态：`verified`。
- 诊断证据：本地运行页当前只有一个题库；第一个同名岗位尚未关联且“选择题库”可用，另外两个岗位已显示该题库标签，因此没有第二个可新增题库，旧 UI 将入口禁用。
- 计划修改：岗位卡片改用“关联题库/管理题库”常驻入口；弹窗区分已关联和可关联题库，无新增项时提供明确说明与题库页入口；补充 React 行为测试、生产构建和文档留痕。不改变岗位—题库关联 API 或已有业务数据。
- 实际修改：`app/web/src/features/workflow/Page.jsx` 删除按 `availableCount` 禁用题库入口的逻辑，岗位未关联时显示“关联题库”、已关联时显示“管理题库”；弹窗同时展示已关联标签和可新增下拉项，现有题库已全部关联或组织尚无题库时显示对应原因与“前往题库管理”。`app/web/src/App.test.jsx` 更新原关联流程断言，并新增“全部题库已关联时入口仍可用”的回归测试；同步开发进度和本日志，重建生产 bundle。
- 验证：`npm test -- --run` 为 `22 passed`，`npm run build` 成功，`git diff --check` 通过。刷新 `http://127.0.0.1:8000/#workflow` 后在实际运行数据验证：两个岗位均显示可用“管理题库”；弹窗展示“测试岗位题库 · tongtong”、已全部关联说明、题库管理入口和 disabled 的提交按钮，控制台交互无阻塞。本轮未写入或修改岗位/题库业务数据。
- 未完成事项：无仓库内功能缺口；岗位题库解除关联仍不是当前 API 合同，本轮没有擅自新增删除关系能力。

## 2026-08-27 · RESUME-REVIEW-CHUNKING-001

- 目标：把简历审阅改成真正后台执行的深模块，并为长 PDF 增加页码感知、Token 预算驱动的分块证据抽取与最终聚合，避免整份长简历一次塞入 LLM。
- 状态：`verified`。
- 领域决策：`ResumeEvidenceChunk` 是 ResumeReview 内部可追溯的分页证据单元，不是候选人结论；`CandidateScreening` 只能由所有成功分块的规范化证据和岗位要求聚合形成。短简历可走单次快速路径，但输出保持相同领域合同。
- 深模块 interface：调用方只使用“排队审阅、读取审阅状态”；字符/Token 预算、分页分段、Map 调用、证据归并、Reduce 调用、重试和错误处理均属于 Resume Review implementation，不暴露到 React 或 API 请求体。
- 计划修改：保留 PDF 页码结构；新增版本化 Map/Reduce Prompt 合同和严格响应 Schema；让 `POST resume-reviews` 返回 `202 + review/job` 而不执行 LLM；Worker 后台按预算选择单次或分块策略并保存进度/证据；React 提交后关闭弹窗并在候选人投影显示处理状态；补齐单元、合同、Worker、API 和 React 测试，同步架构/API/领域/检索/Provider/存储/进度/路线图。
- 安全约束：分块前完成脱敏；不记录完整 Prompt/模型响应；不能静默丢页或截断；部分分块失败不得生成确定性淘汰结论；模型路由缺失/上下文超限必须形成可观察、可重试错误。
- 实际修改：新增 `app/services/resume_review.py` 深模块和 `ResumeEvidenceChunk`，按保守 Token 估算选择 `single_pass/map_reduce`；长简历保留 PDF 页边界，连续页贪心组块，超长单页继续无损切分，并发 Map 只抽项目/技能证据，证据超出 Reduce 预算时分层压缩，最终基于全部证据生成初筛。新增 `resume_review.v3`、`resume_evidence_map.v1`、`resume_evidence_compaction.v1`、`resume_review_reduce.v1` 严格合同，四阶段复用现有 `resume_review` 模型路由。`TalentService` 的创建审阅只排队并由 API 返回 `202 {review,job}`，Worker 保存阶段/分块进度、页码、Prompt/Provider/用量摘要和结构化错误；任一分块失败不写 CandidateScreening。PDF/URL 上传可同时携带岗位与要求，摄取成功事务原子创建后续审阅工作项。React 移除 30 秒阻塞轮询，提交后立即关闭并在候选人列表/详情展示摄取、证据抽取、聚合或失败状态；存在处理中的候选人时每 2.5 秒自动刷新投影，完成后停止；处理中不启动 7 天清理。
- 修改文件：`app/core/prompt/contracts.py`、`app/services/{resume_review,talent,resume_ingestion}.py`、`app/{api/routes,schemas/api}.py`、`app/providers/mock/provider.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/{test_candidate_screening,test_position_resume_appointment_flow,test_prompt_governance}.py`、`CONTEXT.md`、`docs/{architecture,api-design,domain-model,retrieval-and-evaluation,model-provider-plugins,database-and-vector-storage,development-progress,implementation-roadmap,change-log}.md`。
- 验证：新增三页长简历回归把必备 Python 证据只放在第 3 页，确认上传立即返回、摄取和审阅分两次 Worker 执行、最终策略为 `map_reduce`、所有分块完成且匹配证据保留 `source_pages=[3]`；现有同步审阅测试改为验证 `202/queued -> Worker -> ready_for_review`。`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-resume-review-final-pyc .venv/bin/python -m compileall -q app tests` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` 为 `168 passed, 5 skipped in 7.82s`；`npm run build` 成功，`npm test -- --run` 为 `23 passed`；`git diff --check` 通过。
- 失败与恢复：首次直接 `py_compile` 尝试写 macOS 默认缓存目录被权限拒绝，改用 `/private/tmp` 缓存后通过；首次定向测试暴露移除旧脱敏 helper 时误删 `re` import，导致候选人手机号规范化 `NameError`，恢复 import 后 `16 passed` 并完成全量回归。最终只读状态检索命令把 Markdown 反引号置于双引号 shell 参数中，shell 尝试执行不存在的 `verified` 命令；命令前半段 `git diff --check` 已通过，随后改用无反引号的安全检索确认日志状态。以上失败均无业务数据、外部模型调用或供应商费用。
- 未完成事项：仓库内无遗留。生产仍需根据所选模型上下文窗口校准三个输入预算和并发数，并用授权真实长简历做证据准确率、误淘汰率、延迟和成本验收；部署必须运行 DurableWorkItem/Celery Worker，API 进程本身不会执行 LLM。

## 2026-08-27 · POSITION-CARD-OVERFLOW-MENU-001

- 目标：把岗位卡片底部并排的“编辑 / 删除”收进右侧三点展开菜单，改善操作区排版并保留危险操作语义与原删除确认流程。
- 状态：`completed`。
- 计划修改：调整 React 岗位卡片、菜单样式与行为测试，重建生产前端 bundle；不修改岗位、候选人或删除 API。
- 实际修改：`app/web/src/features/workflow/Page.jsx` 将岗位卡片操作区改为“选择题库 + 三点菜单”，菜单包含编辑岗位和红色删除岗位，失焦或 Esc 自动关闭；`app/web/styles.css` 增加靠右、向上展开且不撑破卡片的菜单样式；`app/web/src/App.test.jsx` 改为先展开菜单再验证编辑和危险删除流程；重建 `app/web/dist/`。
- 验证：`npm --prefix app/web test -- --run` 为 `19 passed`，`npm --prefix app/web run build` 成功，`git diff --check` 通过。本地浏览器在实际招聘流程数据上确认三点按钮与卡片右边缘对齐、菜单包含“编辑岗位 / 删除岗位”、删除项使用危险色，Esc 后菜单关闭。
- 未完成事项：无；未修改或删除任何业务数据。
## 2026-08-28 · RESUME-DOCUMENT-CRUD-001

- 目标：补齐候选人简历版本的增查改删闭环，并按用户明确要求删除候选人 `candidate_400e16c5c49f470a` 的最新简历版本 `resume_35faf6ea40b147b8`；保留更早版本。
- 状态：`verified`。
- 领域决策：ResumeDocument 的 PDF 内容与版本继续不可变；“修改”只允许修改展示文件名，替换内容必须上传新版本。删除命令取消未领取的摄取/审阅工作、清理隔离文件和私有文件对象、保留最小审计事实；已被计划或面试历史引用的版本禁止删除。
- 计划修改：增加简历 PATCH/DELETE API、乐观并发与引用保护；让候选人投影忽略已删除简历/审阅；在简历详情增加重命名和删除操作；补齐后端与 React 测试并同步 API、领域、存储、进度和路线图文档。
- 删除前只读证据：当前候选人有两个 processing 版本；最新版本为 `resume_35faf6ea40b147b8`（version 2，创建于 `2026-08-28T06:52:13Z`），其 `resume.ingest` 工作项 `work_496b51dfcef4446b` 为 `pending/attempt_count=0`，尚未产生 ResumeReview，适合取消并清理；旧版本 `resume_095cc59e373049a7` 不在删除范围。
- 实际修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/resume_ingestion.py`、`app/services/talent.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/styles.css`、`app/web/dist/`、`tests/test_resume_ingestion.py`、`docs/api-design.md`、`docs/domain-model.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际实现：新增简历展示名 PATCH 与带 `expected_version` 的 DELETE；删除前检查计划和面试快照引用、拒绝运行中工作，取消未领取的摄取/审阅工作，清理隔离文件、原 PDF/解析文本/未入历史的派生资产，清空审阅证据并保留 `deleted` 审计占位。候选人列表、简历列表与初筛投影忽略删除中/已删除版本，删除审阅后同步校正 7 天初筛留存状态。React 候选人详情可按版本查看、改名和删除，并明确 PDF 内容替换通过重新上传形成新版本。
- 业务数据操作与结果：重启后端加载新路由后，以 `expected_version=1` 删除 `resume_35faf6ea40b147b8`，响应为 `status=deleted/version=3/deleted_at=2026-08-28T08:16:02Z`；工作项 `work_496b51dfcef4446b` 为 `cancelled/attempt_count=0` 且隔离文件不存在。列表只剩 `resume_095cc59e373049a7`（resume_version 1、processing），候选人初筛投影回落到该旧版本。
- 验证命令与结果：`.venv/bin/python -m pytest tests/test_resume_ingestion.py -q` 为 `8 passed`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-resume-crud-pyc .venv/bin/python -m compileall -q app tests` 通过；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `170 passed, 5 skipped`；`npm test -- --run` 为 `24 passed`；`npm run build` 成功；`git diff --check` 通过；删除后通过 API 与隔离路径只读检查验证旧版本保留、目标版本删除、工作取消和隔离文件移除。
- 失败与恢复：首次定向验证误用系统不存在的 `python` 命令且在仓库根执行 `npm test`，分别因命令不存在和根目录无 `package.json` 失败，未修改数据；改用 `.venv/bin/python` 并在 `app/web` 执行后通过。首次沙箱内 `curl` 无法连接宿主机 8000 端口，未发出请求、未删除数据；切换到获批的本机网络上下文后先重新核对精确 ID/version，再执行一次删除。
- 未完成事项：按用户范围保留的旧版本 `resume_095cc59e373049a7` 仍有一个未领取的摄取工作项，因此界面会继续显示该旧版本“处理中”；本工作项未启动 Worker，避免未经本次请求授权触发 LLM 调用与费用。真实 OSS 删除失败恢复和跨实例并发仍随目标部署环境验收。

## 2026-08-28 · RESUME-REVIEW-RETRY-001

- 目标：为失败的简历初筛增加面向招聘工作台的安全重试机制，修复当前 `resume_review_5b4a107888104f43` 因模型超时、断路和 dead-letter 后无法由业务界面恢复的问题。
- 状态：`verified`。
- 诊断证据：PDF 摄取工作 `work_bfeaaa128e1f46f4` 已完成；审阅工作 `work_0fe0350905f84095` 连续 5 次调用 DeepSeek `deepseek-v4-pro` 均约 30 秒 `provider_timeout`，达到路由阈值 3 后出现 `provider_circuit_open`，最终为 `dead_letter/attempt_count=5`。当前 `resume_review` 路由 timeout 为 30 秒、无 fallback；错误不是候选人不符合结论。
- 计划修改：新增版本化 ResumeReview retry 命令与审计、只允许 failed/dead-letter 工作恢复；候选人失败投影和 React 详情提供“重新初筛”；补充 API/领域/进度文档与后端/React 测试。经用户授权把当前路由超时提高到适合简历任务的值，验证模型后重放当前工作并观察最终状态；没有第二个已验证真实 LLM 时不使用 mock 作为招聘决策 fallback。
- 实际修改文件：`app/schemas/api.py`、`app/api/routes.py`、`app/services/talent.py`、`app/services/resume_review.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/test_candidate_screening.py`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际实现：新增 `POST /api/v1/resume-reviews/{review_id}/retry`，以审阅 version 校验并在同一事务恢复 `failed/dead_letter` 审阅和工作项、清空错误/进度、attempt 归零、递增 `replay_count` 并写 `resume.review.retried` 审计；源简历、岗位或要求无效时拒绝恢复。候选人详情在失败态显示中文错误和“重新初筛”，调用领域重试接口而非通用管理员 Outbox。Resume Review 工作租约从通用 60 秒提高到 330 秒，覆盖 Celery 300 秒 hard limit；单次/Reduce 输出预算默认提高到 6000 tokens，Map/压缩提高到 4000 tokens，并提供四个独立环境变量，防止推理模型在完整 JSON 前耗尽输出预算。
- 运行配置与真实恢复：经用户授权将 live route `route_27e636f528f94fe3` timeout 从 30 秒调整为 120 秒（version 2），真实 route probe 三次均成功且未配置 mock fallback。最终以原因“扩大结构化输出预算并修复长任务租约后重试”重放同一工作项；Worker 单次 attempt 在 63.9 秒完成，工作项为 `completed/attempt_count=1/replay_count=3`。审阅 `resume_review_5b4a107888104f43` 为 `ready_for_review/version=30`，策略 `single_pass`、分数 50、建议 `manual_review`、模型用量 3311 input + 4552 output；候选人投影同步为待人工复核且不设置 7 天清理期限。
- 验证命令与结果：`.venv/bin/python -m pytest tests/test_candidate_screening.py tests/test_prompt_governance.py -q` 为 `15 passed`；`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `171 passed, 5 skipped`；`npm test -- --run` 为 `25 passed`；`npm run build` 成功；真实模型探测、业务 retry、Worker 日志、审阅/候选人/工作项 API 均完成核对；`git diff --check` 通过。
- 失败与恢复留痕：首次调整 timeout 后探测时后端不在线，请求未触发模型；重启后探测成功。第一次 live replay 暴露 60 秒租约短于约 86–90 秒模型流程，Beat 重复领取并耗尽 attempt；修复 330 秒租约后第二次 replay 不再抢占，但真实响应暴露 `provider_schema_invalid`：2400-token 输出预算导致 `finish_reason=length` 或推理后正文为空，故停止 Worker 避免继续费用。提高输出预算、等待断路冷却并再次探测后，第三次 replay 一次成功。曾在仓库根误执行 `npm test`，因无 `package.json` 失败且无仓库副作用，随后在 `app/web` 正确执行。历史失败 invocation 和 replay 审计按事实保留。
- 未完成事项：当前只有一个已验证真实 LLM 配置，因此未设置供应商 fallback；生产环境仍应增加第二个真实模型并按真实简历集校准超时、输出预算、费用上限和断路策略。

## 2026-08-28 · CANDIDATE-SCREENING-SCORE-BANDS-001

- 目标：把候选人初筛分数转换为统一、可审计的符合性标准：0–59 分不符合，60–74 分待人工复核，75–100 分符合；人工复核仍可覆盖 AI 建议并保留原始结论。
- 状态：`verified`。
- 边界决策：用户描述的“60–75 需要人工”和“75 分以上符合”在 75 分重叠；本工作项采用无重叠区间并将 75 分归入符合，即 `score < 60 -> unqualified`、`60 <= score < 75 -> manual_review`、`score >= 75 -> qualified`。
- 计划修改：集中定义 CandidateScreening 分数带并在模型结果写入领域对象前强制归一化 recommendation；更新版本化 Prompt 合同、Mock Provider、候选人工作台标准说明、领域语言及 API/领域/检索/进度文档，增加边界值和模型建议冲突测试并重建前端。
- 实际修改文件：新增 `app/domain/candidate_screening.py`、`tests/test_candidate_screening_policy.py`；修改 `app/services/talent.py`、`app/services/retention.py`、`app/core/prompt/contracts.py`、`app/web/src/features/workflow/Page.jsx`、`app/web/src/core/ui.jsx`、`app/web/src/App.test.jsx`、`tests/test_candidate_screening.py`、`tests/test_prompt_governance.py`、`CONTEXT.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`，并重建 `app/web/dist/`。
- 实际实现：以 `candidate_screening_score.v1` 集中定义 0–59/60–74/75–100 三个分数带；LLM 结果通过统一 Schema 后、写入 ResumeReview 前再次由领域策略强制归一化 recommendation 并记录策略版本，模型枚举与分数冲突时以分数为准。候选人投影、审阅读取和 7 天留存判断都从分数派生 AI 建议，人工决定继续优先且不修改 AI 分数与证据。周期 Retention Worker 会先校正存量记录的期限，旧记录首次命中新规则时从校正时起给足 7 天，不由 GET 隐式写入、更不会追溯立即删除。单次 Prompt 升级为 `resume_review.v4`，长简历 Reduce 升级为 `resume_review_reduce.v2`；Mock Provider 保持供应商协议职责，不复制领域分数带。React 候选人列表和详情展示完整分数标准，并统一使用“待人工复核”。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate_screening_policy.py tests/test_candidate_screening.py tests/test_prompt_governance.py -q` 为 `29 passed`；全量 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `185 passed, 5 skipped`；`npm test -- --run` 为 `25 passed`；`npm run build` 成功；`git diff --check` 通过。测试覆盖 0/59/60/74/75/100、非法分数、模型建议冲突、人工覆盖、单次与 Map-Reduce 写入策略版本、候选人投影、存量期限校正和工作台文案。
- 本机运行验收：启动 Redis、FastAPI（PID `46923`，`127.0.0.1:8000`）、Vite（`127.0.0.1:5173/web/`）与 Celery Worker/Beat；`GET /healthz` 返回 `{"status":"ok"}`。Worker 首轮 dispatch 为 0，留存任务为 `candidate_count=0/reconciled_candidate_ids=[]`，只写入批次审计 `audit_78109d98c98744f2`，未触发 LLM 或候选人删除。浏览器实页确认列表和王凯 15 分详情均显示三段标准，15 分为不符合、展示证据/缺口/人工复核和 7 天清理日期；页面还原并保留在招聘流程。
- 失败与恢复留痕：服务启动前的沙箱内 `ps` 被系统拒绝，沙箱内本机 `curl`/`redis-cli` 也因本地网络隔离无法连接；这些只读检查未产生副作用。随后在获批的本机执行上下文启动服务，并分别通过 HTTP、Celery 日志和浏览器实页完成验证。
- 未完成事项：无仓库内遗留；本工作项未调用真实 LLM、未重跑历史简历、未直接修改候选人业务数据。存量期限校正会在下一次周期 Retention Worker 运行时按上述规则发生并进入现有留存审计链路。

## 2026-08-28 · RESUME-REVIEW-OUTBOX-IDEMPOTENCY-001

- 目标：修复重新上传相同 PDF 后 ResumeReview 长期显示 queued、但 Worker 没有可执行 `resume.review` 工作项的问题，并恢复当前张文君孤儿审阅。
- 状态：`verified`。
- 诊断证据：Celery 以 prefork concurrency 10 运行，`inspect active/reserved` 均为空，Beat 连续返回 `dispatched=0`，因此并非单 Worker 串行瓶颈。最新审阅 `resume_review_0311403cea72404d` 为 queued，但 Outbox 中没有指向它的工作项；其 `input_hash=sha256:49924...` 与已删除旧审阅相同，旧幂等键 `resume.review:{input_hash}` 命中了已完成工作 `work_0fe0350905f84095`，导致新审阅创建成功而新任务被错误折叠。
- 实际修改：`app/services/resume_review.py` 新增版本级工作幂等键 `resume.review:{resume_document_id}:{input_hash}` 和统一 `_enqueue_review_work` seam；排队入口现在会校验 Outbox 工作项 `aggregate_id` 必须指向当前 ResumeReview，并在同一 queued 审阅缺少工作项时自愈补建。这样，同一 ResumeDocument 的重复请求仍保持幂等，而内容相同但版本不同的 ResumeDocument 会各自获得独立工作项。
- 回归覆盖：`tests/test_candidate_screening.py` 新增“相同 PDF 的两个不同简历版本生成不同审阅工作项”和“重复排队命令修复 queued 孤儿审阅”测试；同步更新 `docs/api-design.md`、`docs/database-and-vector-storage.md`、`docs/architecture.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`，明确上传幂等、审阅工作身份边界和自愈语义。
- 验证命令与结果：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate_screening.py -q` 为 `8 passed`；候选人/摄取/持久化扩展集为 `34 passed`；完整后端测试为 `187 passed, 5 skipped`；`npm test -- --run` 为 `25 passed`；`npm run build` 成功；`git diff --check` 通过。
- 数据恢复证据：通过现有领域 API 重排当前张文君审阅 `resume_review_0311403cea72404d`，创建工作 `work_7ab7a59d1ed04da1`，新幂等键包含 `resume_d7255a18f1224515` 且 `aggregate_id` 指向当前审阅。Worker 仅执行一次，约 `108.3s` 完成；审阅状态变为 `ready_for_review/completed`、策略 `single_pass`、进度 `1/1`、无错误，最终匹配分 `65`，按 `candidate_screening_score.v1` 进入 `manual_review`。恢复后查询不存在 queued 且无对应 Outbox 工作项的审阅。
- 运行状态：后端已重新启动并在 `127.0.0.1:8000` 返回 `healthz={status: ok}`；前端已重新启动并在 `127.0.0.1:5173/web/` 返回 HTTP 200；Celery Worker/Beat 已重新启动，prefork concurrency 为 10，`celery inspect active` 显示 1 个节点在线且当前为空。Redis 本次未重启，因为本机 broker 已在线且 Worker 已成功连接和消费。
- 失败与恢复留痕：首轮新增回归测试未给两次相同文件上传设置不同上传幂等键，因此被摄取 API 按预期折叠为同一 ResumeDocument；修正测试以两个显式 `Idempotency-Key` 表达“相同内容、不同版本”后通过，未影响业务数据。尝试停止旧终端会话时返回 unknown process id；只读进程检查确认旧 API/Vite/Worker 已退出，随后完成全新启动。最终检查中沙箱内 Celery 连接本机 Redis 被拒绝，改用获批的本机只读检查后确认 Worker 正常；首次孤儿查询误把 JSON 字段当作物理列，按实际表结构修正后返回空集。这些检查失败均未产生业务副作用。
- 未完成事项：无。

## 2026-08-28 · QUESTION-GENERATION-TRUNCATION-RECOVERY-001

- 目标：修复智能生题分片因 `finish_reason=length` 输出截断而被误报为通用 JSON Schema 错误、同参数重复计费，并为双槽位分片提供保留成功结果的自适应单槽位恢复。
- 状态：`verified（仓库）`。
- 计划修改：在 Provider seam 增加不泄露正文的输出截断错误与失败用量诊断；让 Model Invocation 和 DurableWorkItem 区分同请求可重试与终止错误；由 QuestionGenerationService 在双槽位截断后拆成两个单槽位工作；升级版本化生题 Prompt/Schema 的长度和数量边界；增加 Provider、网关、持久工作和生题工作流合同测试，并同步架构、领域、Provider、检索、数据库、进度与路线图文档。
- 实际修改文件：修改 `app/providers/openai_compatible/provider.py`、`app/model_gateway/gateway.py`、`app/persistence/interface.py`、`app/services/question_generation.py`、`app/core/prompt/contracts.py`、`tests/test_openai_compatible_provider.py`、`tests/test_model_invocation.py`、`tests/test_persistence_contract.py`、`tests/test_prompt_governance.py`、`tests/test_question_generation.py`、`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 实际实现：OpenAI-compatible adapter 在 JSON 解析前把 `finish_reason=length` 映射为不可同参数重试的 `provider_output_truncated`，只附带 finish reason、请求预算、content length 和 usage/reasoning token；ModelInvocationLog 在失败 attempt 保存这些脱敏诊断。DurableWorkItem 对 `retryable=false` 首次失败立即 dead-letter，并从自动 claimable 集合排除，人工 replay 仍保留。智能生题 route 去掉网关内嵌重试，一个 work attempt 最多调用一次 Provider；生成预算调整为单槽位 4000、双槽位 8000 tokens。双槽位截断时原工作以 `split_into_single_slot_chunks` 完成、原 chunk 进入 superseded，并在同一事务创建两个单槽位替代工作；单槽位仍截断则终止并保留显式人工重试入口。生题 Prompt/Schema 升级为 v2，并收紧标题、题干、答案、关键点、别名和技能边界。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_openai_compatible_provider.py tests/test_model_invocation.py tests/test_persistence_contract.py tests/test_prompt_governance.py tests/test_question_generation.py -q` 为 `53 passed`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `193 passed, 5 skipped`；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-question-truncation-pycache .venv/bin/python -m compileall -q app tests` 成功；`git diff --check` 通过。合同覆盖 Provider 分类/脱敏诊断、网关不重复调用与失败 usage、Memory/SQLite 非重试终止、双槽位拆分继续完成及单槽位首轮终止。
- 未完成事项或恢复说明：未自动重放截图中的历史批次，未调用真实 LLM，也未修改现有业务数据；目标 DeepSeek 账户的真实截断恢复、10 题并发、费用和限流仍属于部署环境验收。仓库在本工作项开始前已有大量用户修改和未跟踪文件，本次均保留，未执行 reset/checkout 或清理。

## 2026-08-28 · QUESTION-SPEECH-DEFAULT-PREVIEW-001

- 目标：修复新题库在已有真实默认 TTS 路由时仍绑定开发 mock、题目显示语音 ready 却无法试听的问题，并把未配置、模拟资产和私有音频异常转换为普通用户可理解且可操作的提示。
- 状态：`completed（仓库验证）`。
- 诊断证据：用户请求的 `speech_7daf78a0419f437e` 使用 `model_cfg_mock_tts_synthesize`，`audio_uri=mock-tts://...`、`file_object_id=null`、`production_ready=false`，因此试听签名接口返回 `QUESTION_SPEECH_ASSET_NOT_PRIVATE`。创建该题库前组织已经存在 enabled 的 `tts.synthesize + question_speech_generation` 路由，primary 指向 ready 的 `GlM-TTS`，但 `CatalogService.create_knowledge_base` 在非生产环境无条件覆盖为开发 mock profile。当前题目后来通过显式题库配置已生成新的私有资产，本工作项不改写或删除历史资产。
- 计划修改：新题库优先从明确的 `question_speech_generation` route 冻结 ready TTS 模型和默认音色，仅在没有真实默认路由的本地测试路径保留开发 mock；试听接口返回专门的模拟资产错误和操作建议；React 将开发 mock 视为不可试听配置并展示“配置语音后可试听”；增加 route/profile、资产访问和页面行为测试，同步接口、领域、Provider、进度与路线图文档。
- 实际修改文件：`app/services/catalog.py`、`app/services/knowledge_base_speech.py`、`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/`、`tests/test_knowledge_base_speech.py`、`CONTEXT.md`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/model-provider-plugins.md`、`docs/database-and-vector-storage.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。
- 验证命令与结果：定向 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_knowledge_base_speech.py tests/test_documented_gap_apis.py -q` 为 `8 passed`；完整后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` 为 `194 passed, 5 skipped`；前端 `npm test -- --run`（`app/web`）为 `26 passed`；`npm run build`（`app/web`）成功生成生产 bundle；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-question-speech-pycache .venv/bin/python -m compileall -q app tests` 成功；`git diff --check` 通过。曾在仓库根目录误运行一次 `npm test -- --run`，因根目录没有 `package.json` 返回 `ENOENT`，未产生仓库副作用，随后在 `app/web` 正确执行并通过。
- 未完成事项或恢复说明：未调用真实 TTS、未自动重建旧题库语音，也未改写或删除历史 mock 资产；旧 `speech_7daf78a0419f437e` 本身没有音频字节，仍会按设计返回明确的“开发模拟语音不可试听”提示。已有题库不会因组织默认路由变化而被静默改配；可通过题库语音配置显式切换并生成新私有资产。真实供应商音质、费用、限流和对象存储签名仍属于部署环境验收。仓库在本工作项开始前已有大量用户修改和未跟踪文件，本次均保留，未执行 reset/checkout 或清理。

## 2026-08-28 · DOMESTIC-REALTIME-MEDIA-AND-CALIBRATION-001

- 目标：接入国内真实流式 STT 与实时数字人 WebRTC/SFU 路由；在不把厂商协议写入面试业务 module 的前提下，为候选人房间提供实时语音识别和数字人会话生命周期；增加只接受脱敏真实候选人样本的评分一致性与公平性校准工作流。
- 状态：`completed（仓库验证；environment/data pending）`。
- 计划修改：扩展 DashScope Provider 的实时/批量 ASR；新增腾讯云智能数智人 WebRTC adapter 与会话关闭语义；补齐统一 Avatar 会话 schema、路由配置和 React 播放/回收；增加脱敏校准数据 schema、导入/运行/报告接口与自动化测试；同步架构、接口、领域、检索评分、Provider、数据库、问题、进度和路线图文档。所有账号、AppID、Secret、资产 ID 与项目 ID 仅通过管理员连接/模型配置或环境 Secret 注入，仓库默认留空。
- 实际修改文件：`app/providers/dashscope/provider.py`、`app/providers/dashscope/provider.json`、新增 `app/providers/tencent_cloud_avatar/`、`app/model_gateway/schemas.py`、`app/services/model_admin.py`、`app/services/avatar.py`、`app/services/fairness.py`、`app/schemas/api.py`、`app/api/routes.py`、新增 `app/web/candidate/pcm-stream.js` 与 `app/web/public/web/webrtc-player.html`、`app/web/src/features/candidate/Page.jsx`、`app/web/styles.css`、重建 `app/web/dist/`、`tests/test_dashscope_provider.py`、新增 `tests/test_tencent_cloud_avatar_provider.py`、`tests/test_model_configuration_v2.py`、`tests/test_fairness_evaluation.py`、`docs/architecture.md`、`docs/api-design.md`、`docs/domain-model.md`、`docs/retrieval-and-evaluation.md`、`docs/model-provider-plugins.md`、`docs/known-issues-and-remediation.md`、`docs/development-progress.md`、`docs/implementation-roadmap.md`、`docs/change-log.md`。本轮没有新增持久聚合、表、索引或迁移，校准仅解析现有 current evaluation 并写最小审计，因此 `docs/database-and-vector-storage.md` 无结构变更。
- 实际实现：DashScope 新增 `qwen-audio-3.0-asr-flash-streaming` duplex WebSocket 和 `qwen3-asr-flash` 私有音频 batch adapter，候选人浏览器通过 Web Audio 输出 16kHz/单声道/16-bit PCM 到服务端 STT WebSocket；服务端继续持久化同一录音，stream final 失败时走已有 batch repair。腾讯云数智人插件按官方协议用 HTTPS create-by-asset/stat/start/close、带 `requestid=SessionId` 的 HMAC-SHA256 签名 WSS command channel 发送 `SEND_TEXT` 并等待同 ReqId 播报状态；失败、换流、离场和后台模型/路由探测都会关闭会话释放并发。腾讯 WebRTC/SFU 媒体通过同源 TCPlayerLite 页面播放，业务 WebSocket 不承载视频帧。数字人 route 失败时只降级到已冻结的真实私有题目音频，关闭操作固定 primary 且不跨供应商。评分校准 API 只接受 2–5000 条 current `evaluation_id + human_score + opaque cohort`，拒绝姓名/联系方式/简历/转写等额外字段，输出 MAE、RMSE、有符号偏差、±5/±10 一致率、题型/语言/STT 质量/cohort 分层、样本量告警和永不自动生效的线性拟合。
- 协议核验：对照阿里云官方实时 ASR WebSocket、客户端/服务端事件与 Qwen3-ASR 文档，以及腾讯云云渲染会话概览、建长连接、文本驱动、下行状态和官方 H5 Demo。初版腾讯实现把 `SEND_TEXT` 误建模为 HTTP；提交前核验发现后，已改为官方要求的 WSS command channel，并增加“WSS 驱动失败仍关闭计费会话”的合同测试。
- 验证命令与结果：定向媒体/路由/公平性回归为 `26 passed`；最终完整后端 `PYTHONPYCACHEPREFIX=/private/tmp/interviewer-domestic-release-pyc .venv/bin/python -m pytest -q` 为 `203 passed, 5 skipped`；前端 `npm test -- --run` 为 `29 passed`；`npm run build` 成功；`PYTHONPYCACHEPREFIX=/private/tmp/interviewer-domestic-compile-pyc .venv/bin/python -m compileall -q app tests` 成功；`git diff --check` 通过。测试使用 MockTransport/Fake WebSocket，不调用厂商、不产生云端费用。
- 失败与恢复留痕：首次不指定缓存目录运行 `compileall` 时，macOS 默认 `__pycache__` 写入被沙箱拒绝；改用 `/private/tmp` 缓存后成功。一次只读 `rg` 检索把 Markdown 反引号放入双引号 shell 参数，触发 `zsh: command not found: SEND_TEXT`；同条命令中的测试仍通过，随后用安全参数完成检索，无仓库或外部副作用。
- 未完成事项或恢复说明：目标阿里云 Workspace/API Key/模型授权、腾讯 AppKey/AccessToken/形象资产/会话并发均按用户要求留空，所以没有创建 ready 的生产 route，也没有真实验证 WER、partial 延迟、口型、浏览器网络、并发和费用；提供后需在模型服务页创建连接、模型配置并通过探测，再为 `candidate_answer_transcription`、`candidate_answer_repair`、`interview_question_delivery` 建 route。用户尚未提供经授权的真实候选人脱敏 current evaluation 金标，实际评分/公平性校准未运行，不能形成业务结论；建议至少 30 条且每个 opaque cohort 至少 10 条。TCPlayerLite 当前从腾讯 CDN 加载，生产内网部署还需验证 CDN 可达或按腾讯授权包提供本地 fallback。
## 2026-09-28 · QUESTION-SPEECH-PREVIEW-STOP-001

- 目标：题库试听读题语音支持点击停止，避免只能等待播放结束。
- 状态：`completed（前端验证）`。
- 计划修改：调整题库前端播放器的播放/停止与异步请求防竞态逻辑，补充前端回归验证。
- 实际修改文件：`app/web/src/features/questions/Page.jsx`、`app/web/src/App.test.jsx`、`app/web/dist/index.html`、新构建 bundle、`docs/change-log.md`。试听按钮播放时变为“停止试听”；停止会暂停并归零播放器，令在途签名地址请求失效，换题和离开页面也回收播放器。
- 验证命令与结果：`npm test -- --run src/App.test.jsx` 为 38 passed；`npm run build` 成功；`git diff --check` 通过。
- 构建副作用与恢复：Vite 清空 `dist/bundles` 时删除了仓库原有的历史 bundle 及工作区内未跟踪的旧 bundle；已从 HEAD 逐项恢复被删除的已跟踪文件。未跟踪的旧 hash bundle 无法从 Git 恢复；当前 `dist/index.html` 指向本次新生成的 bundle。未改动用户其他源文件。
- 未完成事项：未用真实浏览器和真实 TTS 资产做手动试听验收。
