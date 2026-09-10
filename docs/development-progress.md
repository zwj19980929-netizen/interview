# 开发进度

## 2026-09-10 · 追问稳定性、语音准备与候选人文案（034）

FOLLOWUP-STABILITY-AND-SPEECH-LATENCY-034 / verified（仓库、新合成调用、本机加载）。已完成补充分类v3编号合同、按原文有界重试、明确重试恢复与安全诊断；候选人只显示通俗操作提示，设备状态精简，致命故障停答语义保留。动态语音通过既有网关收齐完整PCM，保留文件播放和私有资产；已定位并修复真实Qwen SSE协议差异。新合成长补充、finish和continue各一次真实路由通过（约1.3–1.5秒）。回归、构建与本机加载结果最终记录于日志034，真实面试体验不等同合成测试。

最终验收：后端1668 passed/6 skipped、前端225 passed/17 files、构建与diff检查通过；无活动候选人/worker空闲后加载API与worker，ready=true，服务引用新入口index-6jgz8ceW.js。同一句合成TTS三轮完整准备中位数2334→2048ms，首轮新路径更慢，不能保证逐次改善；原始指标、失败留痕和回退办法见[操作日志034](change-log.md)。未重采真实候选人录音，不标closed。

## 2026-09-09 · 候选人页面会话隔离（033）

CANDIDATE-SESSION-ISOLATION-033 / verified（前端回归、生产构建、本机脚本加载）：已按日志定位旧编号/新凭证混用及旧故障串页，完成原子绑定、迟到响应过滤、组件重建、数字人和实时启动取消、真正致命错误的表达与控制停止。新增14项竞态/清理用例，前端 **218 passed/16 files**，构建与 diff 检查通过；本机实际加载 index-2uYMAki_.js。当前原会话仍 in_progress、正式答案0，未刷新候选人标签或调用真实暂停；重新入场及长时真实媒体验收不在已验证结果内。详见日志033。

## 2026-09-09 · 面试截止时间收口（031）

INTERVIEW-DEADLINE-RECONCILIATION-031 / verified（仓库回归、生产构建、本机补偿与页面目测）：已实现服务端截止扫描、生命周期收口原因/审计、API 进程周期补偿、Evidence/媒体停止与实时快照，并将列表中的超时取消显示为“已超时结束”；操作区固定次级槽，真正进行中的行显示“正在面试中”。后端完整 `1635 passed/6 skipped`，最终增量定向 `8 passed`，前端 `202 passed` 与构建通过；本机 28 条已过预约时间且未提交完成的活动/暂停会话已幂等收口，截图中的 `3BDEC492`、`58DA3095` 均只有一条截止生命周期事件，服务 readiness 恢复为 ready，31 条历史会话及证据仍保留。

## 2026-09-09 · 面试列表移除（029）

INTERVIEW-LIST-REMOVAL-029 / verified（仓库全量回归与本机页面）：已实现管理员/面试官对已取消、报告就绪会话的列表逻辑移除、版本冲突保护、操作者审计、默认列表过滤、详情证据保留及前端确认入口；进行中/暂停中等非终态不可移除。后端1634 passed/6 skipped、前端202项及生产构建通过；守护确认无候选人/worker活动后已重载 API72866/worker72867，health/readiness/OpenAPI 和页面目测通过，未提交真实移除，不改变现有数据。

## 2026-09-09 · 简历尾题与中文识别（028）

RESUME-TAIL-AND-CHINESE-ASR-028 / verified（仓库、本机API与合成ASR；真实口音及页面目测pending）：已定位计划漏绑定简历审核及英文热词缺少中文流式术语。实现自动关联最多3道简历尾题、预约后生成/开场不阻塞/尾部检查/有原因跳过、迟到语音授权播放及中文术语上下文。定向35项、后端1633 passed/6 skipped、前端201项通过，构建及本机加载完成（API53276/worker53277）；新计划plan_e173aa922a004cf5为6岗位+3简历，未预生成TTS。14秒合成语音能识别目标术语；Mac锁屏，页面目测未做。真实录音对照被自动审批拒绝（敏感录音再次外发需明确授权），等待用户选择，不影响其它验证；真实口音准确率与生产验收不标verified/closed。

## DIRECT-SCORING-AND-TERM-RECOGNITION-027

状态：verified（仓库、真实ASR热词接入、本机原题重评与页面），真实口音质量及生产仍pending。识别争议不再卡住数值分数/总分，未知置信度与可选纠错保留；新增参数逐词读法热词、评分Prompt v5。后端1621 passed/6 skipped、前端200 passed，构建/静态检查通过；API41827/worker41828已加载。第三题真实重评55分、全场75分，JSON/CSV一致，原转写/录音与历史版本保持不变。以下026为历史记录，其强制核验策略已由027取代。详见[说明](speech-understanding-review.md)与[操作日志](change-log.md)。

## SPEECH-UNDERSTANDING-FAIRNESS-026

状态：verified（仓库、真实合成语义与本机加载/回放），真实麦克风长场景及生产仍pending，不标closed。已补齐未知置信度及低可信片段保护、原文焦点澄清并保留同一录音、争议分数与总分待核验、企业回听确认/修正后异步重评。后端1612 passed/6 skipped、前端200 passed、构建和静态检查通过；真实同义流畅/口语样本总分均98，明确解释后可继续，歧义可引用原文焦点。API34432/worker34433已加载，原会话第三题独立录音与视频定位播放正常、总分待核验，历史数据未改写。详见[修复说明](speech-understanding-review.md)与[操作日志](change-log.md)026。

## INTERVIEW-REPORT-PLAYBACK-025

状态：verified（仓库与本机原会话恢复），生产验收仍pending，不标closed。修复评分1200 token默认预算、30秒超时与重复重试、关键点未进Prompt、缺少完成后复核/逐题回放、ENDING立即校验且无人补偿、private_uri误报hash完成、PCM时长为0。后端1590 passed/6 skipped、前端195 passed及build通过；原会话6题真实评分全部恢复并自动生成76分报告，原回答/转写/录音引用保持一致；浏览器独立音频与视频跳转播放成功。历史视频按题目窗口定位，新采集时间窗口尚待新场真实录制验收；完整证据与优化项见[问题复盘](interview-report-playback-review.md)及[操作日志](change-log.md)。

## 2026-09-08 结束确认后重复处理（024，仓库与本地加载验证）

完成有效理解/可选追问失败隔离、同文字有界失败预算及孤儿结果计数、成功准备复用、真实准备快照和陈旧前端状态纠正。完整后端1568项、前端190项及新增状态/日志影响面通过，真实新合成答复各一次决策。当前后端PID70161、前端index-C_WF3WKO.js已加载，healthz/readyz均正常；此前023也已生效。未改历史答案/评分，未把合成样本当作真实麦克风延迟保证，详细结果见日志024。

## 2026-09-08 明确结束但无技术答案（023，仓库与本地加载验证）

已定位三次语义finish成功后又因无技术内容被转成澄清的循环。补齐answer_declined/next及严格原文证据合同，接入既有答案事务、可追溯未作答评分和正式题进度统计。不会/重复否定、思考等待、有效正文、追问未作答、所有权变化、唯一提交和评分报告均已验证；完整回归1530项通过，清理取消泄漏新增及相关131项无RuntimeWarning通过。新合成完整理解7/7正确，补充v2复测3/3；真实慢调用仍存在。已随024加载，本地验收维护于change-log的023/024。

## 2026-09-08 追问等待与空转恢复（022，仓库与本地加载验证）

完成字幕按turn隔离、正常无字STT完成协议、同完整证据准备复用及10秒finish/60秒恢复接收预算。核心目标是消除结束确认后重复开空流和重复LLM计算；不改变评分、追问策略、模型路由和历史答案。已验证跨流已见文字保护、真实owner/journal唯一提交、真实合成ASR及本地加载；后端1483 passed/6 skipped，前端185 passed，build/compile/diff通过。API49796与index-Bnp-4_sK.js在线。真实麦克风长会话仍pending；详细结果维护于change-log的022。

## 021 明确结束后反复询问修复

已复现并修复确认后的输入变化清空问答上下文、final已覆盖partial仍撤销准备两个问题。重复否定保持同一补充问答，真实补充依旧按语义处理；缺final和所有权失效不能提交。完整后端1422 passed/6 skipped，真实合成ASR+语义+慢准备链为1次询问、2次明确结束、1次提交，录音完整一致。未改前端或Prompt；专用安全日志补齐历史缺失的控制状态证据。本地加载见日志021，真实麦克风及生产仍pending。

## 020 静音与播报状态修复

已统一持续语音音量门槛，修正实际LiveKit收音后的新ASR通知，补充未识别回复的语音澄清，区分通道/声音活动/新字幕；级联语音新增播放状态、8秒卡顿恢复及当前句播放按钮，生成/缓冲期使用防回声打断门槛。完整后端1416 passed/6 skipped、前端181 passed。历史询问音频非空，但用户实际未听见的输出链原因未能追溯；实机长会话与生产验收仍pending，加载和合成验证见日志020。

## 019 本场收音与转写修复

已复现并修复真实发送队列下24秒积压的重复恢复失败；单次恢复15秒、有界等待发送进展，独立收帧持续保存。真实合成TTS→ASR验证已通过24秒补送与口头结束确认，1,583,360字节录音一致。术语提示已接入当前流式请求；语义误拼/纠正/真实不同术语/投诉四类合成案例已通过真实模型验证。完整后端1406 passed/6 skipped，最后新增诊断隐私等13项通过，API29269已加载且健康/就绪正常。逐句历史源头缺少厂商响应记录，不能声称完全查清红框话语的声源。最终完整回归与本机加载结果见019操作日志。

## 018：已确认答复的收口（verified：仓库、真实合成音频链与本地加载）

结束回复server final直接进入理解/提交，录音继续；新增本地语音活动适配器区分持续语音与瞬态尖峰，待定起音、低声ASR新增文字和缺final仍保守保护。正常准备窗口缓冲60秒、故障恢复30秒；已覆盖半帧不误阻塞，静音send在途不重复理解。候选界面明确“已收到结束确认”，自然表达无需固定口令。最终后端1392 passed/6 skipped、前端176 passed、build/compile/diff通过；真实合成中文TTS→ASR→语义finish→收口仅2次识别开流、428160字节录音逐字节一致，原音量/10%/3%音量样例均检测到语音。API16195与新bundle已加载，原会话未补造答案；真实麦克风与生产仍data_pending/environment_pending，详见日志018。

## 016：静音询问与口头确认（verified：仓库、真实合成合同与本地加载）

已实现正式5秒询问是否补充、服务端答复意图、同题累计补充与明确否定后的原有提交链；新增供应商400有界兼容和安全诊断，修复动态表达复用题目音频。完整服务链合成覆盖否定、肯定后补充、一次完整录音、答案唯一、打断/回声及播放超时，候选界面和重连投影回归通过。完整后端1376 passed/6 skipped、后续影响面285 passed、最终关键39 passed；前端176 passed，build/compile/diff通过。真实Qwen合成肯定/否定/含糊及确认后完整回答合同通过，API8004与新bundle已加载，原会话仍暂停且无补造答案。历史400具体参数不可追溯；真实中文麦克风体验、时延与生产仍为data_pending/environment_pending，详见操作日志016。

## 2026-09-07：正常提案不断流（NONCLOSING-STT-SNAPSHOT-015，verified：仓库与本地加载）

已分开稳定句段预览与最终证据。DashScope/Mock支持原识别连接上的预计算，正常继续听不finish/reopen，确定接话后一次final复核再提交；尾句修订重新理解、续说撤销、旧owner不能提交。独立预览不伪造authoritative/is_final、不持久化Utterance；PCM封存前缀/唯一答案门禁保留，显式提前结束只是可选兜底。无预览能力Adapter保留兼容路径，试音/前端协议/Prompt/评分/路由不变。最终后端1352 passed/6 skipped、前端173 passed、compile/diff通过；API96567 healthz/readyz正常，旧会话未恢复。具体边界/真实服务链合成测试与命令见操作日志015；真实中文质量及生产、012播放时钟/时延目标仍pending。

## 2026-09-07：正式采集自动恢复（CONTINUOUS-CAPTURE-RECOVERY-014，verified：仓库与本地加载）

正式识别/快照暂时失败已改为有界自动恢复，保留同题收音和未确认音频；最多3次/单次4秒，30秒新积压上限，耗尽只关闭本代采集并允许候选人重试本题，不提交残缺答案、不要求企业人员守候。重试须当前turn/capture/owner/私有revision一致，旧证据保留；Memory/SQLite覆盖并发、幂等、事务回滚和跨会话隔离。前端有恢复中/重试本题状态，旧字幕/错误事件不能恢复已暂停界面。

底层补送有序让出调度、PCM接受时只录一次、finish/abort有界独立回收；暂停同步撤销输入，开流前后再检查会话/题目/所有权。critical状态失败不允许worker退出但继续收音，纯UI失败不暂停健康识别。最终后端1198 passed/6 skipped、前端173 passed、构建/compile/diff均通过；API89405的health/readiness正常且首页提供新bundle，完整结果见操作日志014。真实麦克风/网络和生产验收pending，旧会话未恢复、实验TTS保持关闭。

## 2026-09-07：路由证据过期自动恢复（ROUTE-READINESS-REFRESH-013，verified：仓库与本地服务）

已实现邀请前自动刷新过期/未测路由、健康与过期/失败分状态、探针并发去重/时限/失败冷却/配置 fencing，以及管理页缺失用途。无效候选令牌/不同意/窗口外/设备未就绪不能触发自动探针；邀请/start在探测后重新校验最终准入，已消费邀请保持幂等。前端仅邀请/检查/开场增加局部超时，防重复并保留已创建预约，刷新列表失败不重复创建或签发。真实测试补修 STT 关闭等待取消后跳过资源清理，abort 共享回收且超时强制关闭底层连接，正式 finish 不变。

完整后端962 passed/6 skipped、前端158 passed，build/compile/diff通过。最终 API76743 于 `2026-09-07T07:47:56Z` 对原预约执行无邀请健康刷新，0.92秒后五条必需路由 healthy/can_invite=true；再次刷新0.10秒，相关 invocation 85→85，无重复外部调用。预约仍scheduled/version1，未签发邀请；服务healthz/readyz正常，提供index-CU2XVjAr.js。技能codebase-design/domain-modeling用于统一证据和刷新边界。详情见操作日志013；生产/业务质量验收仍pending，旧证据兼容路径未closed。012实验流式TTS仍关闭，未扩大本项范围。

## 2026-09-06：可撤销自动轮次（INTERRUPTIBLE-AUTOMATIC-TURNS-012，自动轮次已启用，流式 TTS 默认关闭，目标验收 pending）

自动轮次与合并推理已实现并启用，替代 011 强制按钮：本地音频 EOT 提议＋后台理解/追问一次准备；思考停顿/准备期间继续收音，新语音撤销旧判断，最终权威转写和题目/预算/owner/capture 校验后才提交。按钮仅可选兜底，试音仍自动结束。ContinuousSTT 分开句段 final 与整题答案，轮换保留音频且只录一次，缺 final 的有声前缀重放；推理临时失败有界重试并继续听，真正媒体失败仍安全停止。播放前验证原 act 选择与所有权，旧 TTS 不能暂停/覆盖新轮次。分阶段指标不含候选数据。

流式 TTS 实验链路的代码与合同测试已完成：统一 TTS PCM、DashScope SSE、独立最小权限 LiveKit publisher、完整私有归档、ready/provider EOF/客户端 ACK 协议、当前批准输出断线后以新音轨重播，以及前端精确绑定。但 `INTERVIEWER_STREAMING_TTS_ENABLED` **默认 false**；本轮默认表达仍使用预生成或完整合成后的私有音频。

默认关闭的实证原因：真实 Chrome 正常合成流可完成，但仅 2 秒 PCM 中间停供 3 秒时，`currentTime` 仍增长到 5.122 秒且没有 `waiting`，EOF 后立即报告 drained。媒体时钟包含停供时间，不能证明源音频样本真正排空，也不能作为可靠口型时钟。源样本到实播位置映射／可计数的 PCM 消费时钟仍待完成；不得用固定尾音等待或历史抖动均值替代该证据，更不能将实验路径宣称为端到端验收通过。

本机另确认 LiveKit 广播旧地址 `192.168.0.104`，主机实际已为 `192.168.0.108`；确认没有参与者后以现有 `local-media.sh up` 更新，Chrome 正常与停供合成探针均可建立媒体。新增 `local-media.sh doctor` 只读核对当前 IP 与精确容器的 `--node-ip`，漂移时非零退出并给人工操作指引，不自动重启；此环境原因不追认为所有历史断流的根因。

最终本地全量后端 `858 passed, 6 skipped`、前端 `131 passed`，构建成功；本机独立 Python 3.12 原生 worker 合成 smoke `31 passed`。这些是仓库与本地验证结果，不是生产验收，具体命令和服务状态见操作日志 012。技能 codebase-design/domain-modeling 用于分开提案/权威前缀/提交边界并维护统一领域语言。没有将历史音频或转写发给外部模型，没有恢复旧会话。真实中文思考停顿/轻声续说/长回答、并发校准、可靠 PCM 播放时钟与首音 p50/p95、目标环境及生产验收仍 pending；当前不能承诺端到端 1–2 秒。

## 2026-09-05：正式完成确认与理解引用 v2（TURN-COMPLETION-UNDERSTANDING-011，verified（仓库与本地服务、合成模型合同），真实麦克风 pending）

已实现正式静音不提交、回答完毕显式收口、处理期间真实状态与停止 VAD、暂停根因不被次生提示覆盖；理解使用服务端原文片段/能力点编号，统一校验后还原，非法分区/证据仍拒绝，仅有一次纠正尝试（每次 20 秒）。百炼 strict schema 的 array.uniqueItems 兼容留在 Adapter，原网关约束不减。合成文本真实模型在 5,268ms 返回合法理解，无个人转写外发；完整后端 `525 passed, 5 skipped`，前端 `84 passed`，build/compile/diff 检查通过。API 已重启 PID `21509`，提供新 bundle `index-jehEcGBW.js`。旧会话未改，真实麦克风/原回答复验仍 pending；后者需要用户明确同意敏感转写再次发送至已配置模型。

## 2026-09-05：停顿端点作用域与无转写恢复（verified（仓库与本地服务），真实麦克风复验 pending）

`ENDPOINT-CAPTURE-SCOPE-010` 已实现 capture ID/turn 与每次停顿 endpoint ID 的控制隔离，阻止前一题、同题旧采集和取消后旧计时器封存新录音；前端 VAD 使用服务端 ready 的作用域。无 final 只保留录音和转写失败事实、原子释放采集、提示继续说或重说，不生成 None/空 utterance。真实音频的 batch 补偿禁止 mock 假成功，显式开发 fixture 仍可测试。Memory/SQLite、端点、前端 scope、网关与接管回归已通过；完整后端 `514 passed, 5 skipped`、前端 `84 passed`，build/compile/diff 检查通过。API 已重启为 PID `15757`，health/readiness 正常并提供新 bundle；旧安全暂停会话及真实批量路由未修改。仍需浏览器强制刷新后实际停顿/续说、多轮及真实批量补偿复验。

## 2026-09-04：正式澄清后的同题重新收音（verified（仓库与本地状态修复），真实麦克风复验 pending）

- `iv_4b8a18d54e7e4747` 已收到 20.5 秒正式音频，理解要求澄清后题目回 asking，而采集仍 complete；随后 13 次重新开流被 `EVIDENCE_MEDIA_ALREADY_COMPLETE` 拒绝。不是此次模型握手或麦克风完全未成功。
- `UTTERANCE_REJECTED` 与未采纳采集归档/新 revision 原子提交；流式、断线 batch、持久 repair 统一 revision。writer、转写状态和答案提交均拒绝旧采集；同 owner 等长度新采集也不能被旧 repair 覆盖。原录音/转写不改写，正常完整录音不能无条件重置。
- 定向 Memory/SQLite `27 passed`，全量后端 `495 passed, 5 skipped`。本机备份后通过严格目标校验和正常 ownership claim/fence/release 修复旧 checkpoint，写入审计、零新答案；API PID `2644` health/readiness 正常。浏览器刷新后同题实际重说与多轮闭环尚待复验，状态不升级为生产验收。

## 2026-09-04：正式 Evidence 排空与 SQLite 实时事务修复（verified（仓库与本地服务），目标新会话复验 pending）

- 会话 `iv_581d8635efa8413b` 的暖场与正式阶段使用相同 DashScope 流式 ASR；正式 Evidence 存活约 51 秒却只封存 125 个 20ms 帧（2.5 秒/80,000 字节），随后以 `LIVEKIT_INGRESS_SINK_BACKPRESSURE` 暂停且没有 CandidateAnswer。同期 13 组 VAD start/stop 每次都会触发 SQLite 事务，而旧 adapter 在每次提交后反序列化万级历史 documents/audit，阻塞事件循环和权威 sink；不是测试与正式使用了不同识别模型。
- SQLite 提交后现只增量同步本事务变更到兼容缓存，事务查询/CAS 仍直接以数据库为权威，回滚与 reopen 合同不变。2,000 条历史审计记录下更新一个会话只解析显式 get 与 CAS 的两行，不再扫描全库。
- LiveKit ingress 新增 accepted/delivered 水位排空屏障；暖场和正式 seal 都必须等已接收前缀进入录音/STT 后才能 finish，排空失败仍严格失败关闭。候选端 VAD 默认静音停止窗口调整为 800ms，并在确认试音时清除暖场字幕，降低命令抖动且避免两个阶段视觉串场。定向后端 `44 passed`、前端 `35 passed`；完整后端 `475 passed, 5 skipped`、完整前端 `83 passed`，production build、compileall 与 diff check 通过。修复后 API 已重启为 PID `88367`，`healthz=ok`、`readyz.ready=true`；目标真实 Provider/浏览器的新会话仍为 pending。

## 2026-09-03：DashScope final 尾句截断修复（verified（仓库），目标新会话复验 pending）

- 新会话 `iv_8120d4147ef14c67` 的暖场 STT 正常打开、持续接收约 46 秒并正常 seal，没有背压、断流、暂停或 Evidence owner 失效，但候选人确认最终字幕缺少尾部。根因位于 DashScope adapter 的 final 聚合：实时 partial 是 `committed + partial`，结束时却在已有 committed 后忽略最后仍未 `sentence_end` 的 partial。
- DashScope 现用单一 transcript projection 生成实时与最终视图：最终 segments 包含全部已确认分句，并追加 `task-finished` 前最后一个非空 partial segment；final text 从该 segments 集合无损拼合。空转写继续失败关闭，唯一 authoritative final、服务端 LiveKit Evidence、浏览器文本禁止、CandidateAnswer 与评分边界不变。
- 试音展开面板的静态标题从容易误解的“最近两行服务端字幕”改为“完整服务端字幕”。定向 DashScope `22 passed`、候选试音 React `2 passed`；完整后端 `471 passed, 5 skipped`、完整前端 `81 passed`，production build、compileall、manifest JSON 和 diff check 均通过。仍须以全新会话验证真实尾句，并在正式回答核对录音、final 与 CandidateAnswer 一致后才能移除环境 pending。

## 2026-09-03：试音背压与迟到 seal 恢复（verified（仓库），目标新会话复验 pending）

- 会话 `iv_68419ac0bb784ee5` 在暖场持续识别多段语音后，于 `2026-09-04T05:31:22Z` 以 `PROVIDER_BACKPRESSURE_EXCEEDED` 中断并暂停；浏览器约两分钟后才离线，Evidence owner 续租正常。随后已创建的旧 endpoint seal 又成功返回，把 calibration 写成 `awaiting_confirmation`，证明直接问题是两秒发送积压预算过窄，加上暖场断流与 seal 缺少同一代次失效规则，不是麦克风先断或识别内容全部丢失。
- 暖场 `audio_stream_failed` 现在只终止当前非评分 warm-up epoch：取消端点、abort 临时流、清理 partial 投影，原子进入 `retrying + calibration_retry_required=true` 并输出 `retry_warmup`，InterviewSession 保持 `in_progress`。已提交、正在等待或稍后到达的旧 seal 都复用第一次失败结果，不得再投影 final/确认表达；正式 Evidence 断流仍按原规则暂停或修复，不降低答案完整性。
- 候选人显式重试时，owner 先重启同一 LiveKit microphone publication 的 lossless iterator，无法复用时才重建 subscriber，成功后才清 retry gate。LiveKit sink 与 DashScope sender 的默认无损有界窗口从 2 秒提高到 5 秒，硬上限 30 秒；分别可用 `INTERVIEWER_LIVEKIT_INGRESS_BACKPRESSURE_SECONDS` 与 `stream_send_backpressure_seconds` 调整，超限仍失败关闭且不会丢帧。
- 当前 Provider/LiveKit/supervisor 定向回归为 `59 passed`，覆盖 2.5 秒供应商发送阻塞、显式两秒 sink 超限、同 microphone iterator 恢复、暖场失败后迟到 seal、seal 已在等待时的失败竞态、单一 problem/重试门，以及正式/未打开流仍暂停；后端全量为 `470 passed, 5 skipped`，compileall、Provider manifest JSON 与 diff check 通过。API 已以新代码重启为 PID `81022`，`/healthz=ok`、`/readyz.ready=true`，数据库、Redis、VRM、LiveKit/Egress 和权威音频入口全部 ready。事故会话不恢复，目标浏览器/真实 Provider 必须使用新邀请复验。

## 2026-09-03：正式 STT 短 partial 后断流修复（verified（仓库与本地服务），目标新会话复验 pending）

- 会话 `iv_31e593c1ef5b4098` 的 LiveKit/Egress 在暂停后仍持续收到候选人音频，但应用 Evidence 仅到 1,143 帧/22.86s，其中只有约 3.66s 有效发言，所以页面只出现“嗯首先……” partial 后暂停。owner 续租、VAD endpoint、LiveKit 音轨和候选人麦克风均正常；失败位于 API 进程内的 Evidence sink/STT 下游。
- 上一段暖场播放的迟到 `avatar.performance.stopped` 曾在正式题已开始后错误切换 floor，使正式 STT 提前打开。服务端现要求非空 `performance_id` 与当前值原子 compare-and-clear 成功；不匹配事件对 floor、暖场、结束和 Evidence 都无作用。
- `transcript.partial` 已从 LiveKit `_receive_audio` 调用栈解耦，按 stream/turn latest-wins 且有界投影；final、错误、对话动作、私有媒体、CandidateAnswer 与 fence 仍为无损有序链。顺序屏障保证 final/reset/stop 后无迟到 partial；投影失败仍严格暂停。
- DashScope 实时 PCM 默认以约 100ms 合包，尾包、abort 与两秒背压计数均有合同测试；`use_environment_proxy=false` 在新 WebSocket runtime 上显式使用 `proxy=None`，不再静默继承开发机代理。LiveKit 断流特权诊断保留去敏 `cause_code/cause_type`，候选人只看到统一安全信息。
- 后端全量回归为 `467 passed, 5 skipped`，相关 Provider/LiveKit/Agent 集合为 `123 passed`，前端为 `80 passed`；production build、compileall 和 diff check 通过。修复后 API 已以 PID `73514` 重启，`/healthz=ok`、`/readyz.ready=true`，数据库、Redis、VRM、LiveKit/Egress 与权威收音全部 ready。必须用全新邀请在目标浏览器复验长回答、连续 partial、唯一 final/CandidateAnswer 和多轮，才能移除 environment pending。抽题、计划/题目冻结、评分、追问、S2S/cascade 和人工接管业务规则没有变化。

## 2026-09-03：Evidence 续租被高频遥测饿死修复（verified（仓库），目标新会话复验 pending）

- 会话 `iv_2dd0eecc8dd8491f` 已完成暖场识别，但正式题目播放后没有任何正式字幕；数据库与事件时间线证明正式 STT 从未打开，owner 在最后续租 15 秒后过期并以 `EVIDENCE_OWNER_FENCED` 暂停。根因不是 STT 识别质量，而是约 400 条逐 viseme `telemetry.observe` 逐条走 SQLite 幂等读写和整会话 reload，阻塞事件循环直到错过 lease。
- AgentChannel 将 `telemetry.observe` 移到 candidate-only process-only 旁路：合法无 PII 样本仅更新固定指标并显式让出调度，不经过领域锁、幂等 key、InterviewSession/事件/Evidence journal/takeover 事务；非法或越界样本静默丢弃且不会暂停。普通控制信号的幂等行为保持不变。
- 候选端对 `avatar_viseme_drift_ms` 和 `avatar_freeze_ms` 使用按 key 独立的 1 秒最大值窗口，每窗至多一条；socket 未打开或播放/连接/体验关闭时不排队、不补发，低频 latency 指标仍即时发送。owner 续租新增不含 PII 的 scheduler lag、DB latency 和 success 指标，但指标不参与正确性。
- 数据库时钟、15 秒租约、ownership epoch/lease 与过期 self-fence、独立的 control-generation 命令 fence，以及 CandidateAnswer commit fence 均未放宽；旧 owner 仍不能迟到恢复。仓库已覆盖 SQLite 512 条重复遥测领域指纹不变、调度公平、非遥测幂等、多周期续租及过期 owner 自围栏，状态为 `verified（仓库）`。
- 抽题、计划/题目冻结、权威 Evidence/CandidateAnswer、评分、受控追问、S2S/cascade 和人工接管业务逻辑没有变化。事故会话不恢复；仍须用全新预约/会话在目标浏览器、LiveKit 与实际 STT 路由复验暖场后正式开流、字幕和持续续租，完成前不提升生产状态。

## 2026-09-03：试音握手、VAD、Range 与 FPS 收口（verified（仓库），目标环境复验 pending）

- 候选人 Evidence 控制面改为 `open requested → server ready` 两阶段因果握手。服务端实际开流或确认幂等 existing-open 后，以同 `causation_id` 的 transient floor 事件应答；客户端只接受当前连接本次命令的匹配 ACK。握手前页面显示“准备识别”而不是“正在听”，普通 VAD 不发送 speech start/stop；握手期间持续讲话会在 ready 后承接。断线只做有界 reassert，其他标签页广播的 ACK 只能推进 cursor，不能打开本地 gate 或 flush speech。
- AudioWorklet level 现在携带采样数/采样率，VAD 起止按累计毫秒而非回调次数判断；数字人播放期使用更高能量与持续门槛抑制扬声器回声，确认后的真实 barge-in 仍在 200ms 内静音，静音 stop 使用稳定窗口避免几十毫秒抖动。
- 暖场 final/seal 失败只 destructive finalize 一次并保留首个真实错误，原子持久化 `calibration_retry_required`；服务端 gate 未清时拒绝 Evidence open。UI 只在候选人显式“重新试音”后发送 reset；owner 完成 reset 后，live transient `warmup_retry` 或 snapshot 的 `retrying + retry_required=false` 都允许当前 control 用新 causation open/reassert。客户端记忆已消费 retry cause，服务端以 generation fence 与 existing-open 幂等 ACK 防止刷新、heartbeat、断线和多连接恢复重复创建付费 STT。
- 本地私有文件交付补齐 `GET/HEAD` 单 Range：无 Range 为 200，合法 closed/open-ended/suffix 为 206，不可满足、畸形或多段为无正文 416；HEAD 与 GET 的状态和相关头一致，原 token/租户/审计边界不变。VRM FPS gate 与界面共同使用显示整数 `Math.round(fps)`，门槛 30 时 29.5 通过、29.4 失败。
- 本项不改变抽题、题目/计划冻结、服务端权威证据和答案、评分、追问、S2S/cascade 或人工接管业务逻辑。后端全量 `439 passed, 5 skipped`、前端全量 `74 passed`、production build、compileall 与 diff check 均已通过，状态为 `verified（仓库）`；仍须在新会话复验目标浏览器、LiveKit/STT、私有媒体 seek/range、扬声器回声与真实打断，完成前不提升生产状态。

## 2026-09-03：正式语音打断竞态与 WAV 容器完整性修复

- 实际事故的正式开场 TTS 调用成功且私有音频请求返回 200；候选人听到姓名后本地 VAD 触发正常 barge-in，播放器在 1ms 内静音，但清空旧 `<audio>.src` 产生的迟到 `error` 被无条件映射为 `CANDIDATE_RUNTIME_FAILED`，才使服务器在约两秒后暂停。`runtime-problems` 的 200 响应是错误上报后的持久暂停回执，不是音频接口失败。
- CandidateInterviewExperience 新增单一 `activePlayback` identity。play/ended/error、RAF 与异步 `play()` rejection 只有在 identity 仍为当前项时才有作用；停止先原子失效 identity，再解绑监听、pause 并卸载 src。服务端 interrupt 携带 ID 时只匹配对应 performance；自然结束只确认一次，当前正式音频的真实加载/解码错误仍保持 fail-closed。
- QuestionSpeechAsset 与 AgentExpressionAudio 共用的 PrivateAssetImporter 现严格解析 RIFF/WAVE、fmt/data、未知 chunks、odd padding、PCM geometry 与 block alignment。只规范化实际观测到的 signed-limit RIFF/data 流式占位组合，以及全部 child chunks 完整到 EOF 后外层 RIFF 恰好漏计四字节 WAVE form type；任意其他截断、长度偏差、重复/错序 chunk、尾随垃圾及 MIME/魔数冲突返回 `TTS_ASSET_FORMAT_INVALID`。checksum 对规范化后的字节计算。
- 本机只读语料核对了 64 个私有 WAV：23 个为 signed-limit 流式占位，41 个为 GLM-TTS 的 outer-size 少四字节；全部在内存规范化后可由 Python wave 完整读取且声明帧字节等于实读字节，没有改写历史 FileObject。事故开场原文件正文为 24kHz/单声道/16-bit PCM、18.88 秒，但原头把 RIFF/data 声明为约 2GiB，证明 decoder 能播放开头不等于容器完整。
- 回归结果：WAV/题目语音/动态表达/Agent 定向后端 `100 passed`；完整后端 `431 passed, 5 skipped`；前端 7 个测试文件 `62 passed`，其中播放生命周期合同 `16 passed`；Vite production build 成功并仅保留既有 VRM chunk 体积告警。compileall 与最终 diff check 通过。API 与 Celery Worker/Beat 已加载修复重新启动；`healthz=ok`、`readyz.ready=true`，一个 Worker 节点在线且 active/reserved 为空。
- 面试状态机、题目/计划冻结、服务端权威答案、评分、S2S/cascade 决策、追问和人工接管逻辑均未改变。已暂停且 token 暴露的事故会话不恢复，历史冻结音频不原地修补；应通过既有 regenerate 生成新的不可变题目语音，并用新邀请/会话复验。

## 2026-09-03：本机实时开场、收音与端点状态修复

- 修复候选人刷新后 LiveKit 发布 identity 与服务端冻结 Evidence identity 不一致的问题：重连只更换控制连接和 backfill epoch，继续复用权威媒体身份，避免真实麦克风轨已进房间却被订阅器过滤。
- 开场和动态表达继续统一走 `interview_agent_expression`；主问题预生成结果若只是开发占位，会自动回落到相同受管 TTS，仍禁止浏览器朗读，不再直接暂停整场。
- 候选人端删除本地 2475ms 假进度切换；2.5 秒现在明确表示服务端在连续静音后收口本轮回答并进入 final/理解，继续说话会取消收口，不表示自动下一题。暂停/完成快照会清除端点与 VAD 状态，服务端也拒绝暂停会话重新触发开场或媒体命令。
- LiveKit 在 STT WebSocket 握手期间产生的静音帧现由关闭的 Evidence 门无锁丢弃；暖场 `evidence.seal` 不再错误要求正式 `turn_id`。端点成立后先关闭证据门并释放链锁再等待 final；DashScope duplex 的 PCM 发送和 vendor event 接收各由独立后台任务负责，并以两秒音频字节预算失败关闭，避免网络抖动填满 LiveKit sink。
- 本机现有 DashScope TTS、流式 ASR 与 LLM 配置已绑定到开场表达、暖场识别、正式答案识别、轮次理解和受控追问五个精确 purpose，五条真实探针均为 healthy。隔离 SQLite 副本上的真实 LiveKit/TTS/STT 合成语音闭环已验证开场私有音频、服务端收音、暖场 partial/final、2.5 秒端点、正式题目音频和正式 authoritative final。
- 同一次闭环暴露 `qwen3.7-plus` 的轮次理解连续两次命中 30 秒超时；DashScope adapter 现对实时理解/追问默认关闭 Qwen 深度思考，并保留管理员中文开关覆盖。关闭后的外部复测因执行权限服务异常未实际发出，仍需在重启后的新会话确认追问分支。
- 自动化基线已更新为后端 `413 passed, 5 skipped`、前端 `58 passed`；Vite production build、compileall、Provider manifest JSON 与 diff check 全部通过，build 仅保留既有 VRM chunk 体积告警。

## 2026-09-02：本地 VRM 资产接入与候选人失败关闭修复

- `model/interviewer.vrm` 已作为默认本地数字人资产接入；去敏 manifest 冻结 SHA-256、VRM 1.0、权利人、`personalProfit` 使用范围、肖像许可、署名与禁止修改约束，不保存截图中的联系方式。
- 资产检查修复了 VRM 1.0 表情分类误判：标准 `aa/ih/oh/ou` 允许位于 preset，辅音可位于 custom，并以两者并集校验 15 个口型；实际 18 MiB 模型同时通过 humanoid、lookAt、blink、hash 和许可字段检查。
- 本地模式不再因为 development 环境绕过 VRM readiness；`/readyz`、预约 admission 和候选人 avatar grant 使用同一事实。云模式不被无关的本地资产检查阻断。
- 候选人端新增 allow-list `runtime-problems` 命令。VRM 获取、加载、WebGL renderer 或统一 facade 致命失败时，服务端验证 token 并真实推进 lifecycle pause；UI 仅在收到持久 `paused` 回执后显示已暂停，原始浏览器异常不上传。
- “自我介绍生成异常”实为 session snapshot 到达前过早渲染 warm-up；页面现先显示实时安全会话 gate，只有服务端快照确认暖场状态后才展示自我介绍。
- 真实浏览器首次验收发现模型虽以 60 FPS 正常加载但仍保持导出 T-Pose、镜头偏远；renderer 现固定自然下垂的克制上半身姿态并改为胸像面试构图，避免“合同已过但画面仍像制作态”。
- 实际 Uvicorn access log 可能打印私有模型查询 grant 或私有文件/媒体/邀请 path token；启动期现安装统一过滤器，遮蔽敏感 path segment 与 `grant/token/ticket/signature/access_token` 查询值，同时保留路由后缀、非敏感查询和状态码，避免短期 bearer 进入日志。
- 定向回归已覆盖真实资产、许可/hash/viseme、无开发绕过、故障暂停幂等/错误 token/去敏响应、新 API、前端错误映射与暂停文案；完整验证结果见 `VRM-ASSET-RUNTIME-001` 变更日志。

## 2026-09-02：火山引擎豆包 Provider 仓库实现

- `volcengine` 已由不可路由的占位 manifest 升级为可执行 Provider，继续位于统一 Model Invocation deep module 后方；InterviewAgentRuntime、评分、题库和 React 不依赖任何火山协议类型。
- Ark Chat/Embedding 使用独立 `ark_api_key` 和官方 OpenAI-compatible API v3；Seed ASR 2.0 streaming 使用二进制 Gzip 帧、极速版录音识别使用服务端私有字节、Seed-TTS 2.0 使用单向流 HTTP，三条语音链路只使用独立 `speech_api_key`。
- Seeduplex API v3 已映射为 `speech.dialogue_realtime`：16 kHz 候选人 PCM、24 kHz 输出、受控 `speech_text_buffer.replacement`、`response.cancel` 和 `session.close`。供应商只表达冻结 ApprovedConversationAct，输出仍要经过上层逐字批准和私有 AgentExpressionAudio 门禁。
- 离线协议合同覆盖 Ark/Speech 凭据隔离、官方端点/Resource ID、批量私有音频、TTS 分块、ASR 二进制 final/error、Seeduplex 批准文本和注册表/manifest。没有真实火山账号、模型/语音资源授权、并发与网络指标，本项只标记 `repository_verified / environment_pending`。

## 2026-09-02：完整实时面试智能体仓库实现收口

- `InterviewAgentRuntime/AgentChannel` 已成为唯一正式控制面，统一拥有 Floor、barge-in、暖场、自动端点、幂等恢复、结构化理解、受控追问和人工接管；旧 `/live`、`/stt-stream`、`audio-answers`、`avatar/speak`、avatar close 及前端多通道 runtime 已物理删除。
- CandidateInterviewExperience 统一一次设备申请、真实自拍、AudioWorklet VAD、三层收音状态、两行字幕、2.5 秒可取消端点、30 秒 AES-GCM 缓冲和资源关闭。正式入场在发布麦克风后读取 LiveKit RTCStats 复核 RTT/jitter，真实 VRM 连续 FPS 覆盖基础 WebGL 探测；未同意视频的 grant 不含 camera。
- `AuthoritativeEvidenceIngress` 已接通数据库时钟 ownership/fencing、持久 command journal、连接无关 owner executor、DB polling/Redis wake hint、remote receipt proxy、私有 segment/checkpoint、owner-loss batch repair 和授权 gap 的浏览器 backfill；旧 owner、旧 control、重复 finish 与重发帧不能形成重复 CandidateAnswer。
- Participant Egress capture 具备明确同意、私有 URI、hash、存储保护/加密核验、保留与审计；本地开发可由单一 `INTERVIEWER_LOCAL_MEDIA=true` 启用仓库 LiveKit/Egress 并写入私有目录，生产仍强制对象级 AES256/KMS。`starting` 崩溃后的 provider 结果不明时不会重复启动录像，而是暂停等待人工核对。试音媒体在正式开始前删除，不进入答案/评分/报告。
- `ApprovedConversationAct` 追问必须绑定冻结 root、深度、非空原文证据与能力点，Expression 不能补建；AI 动作恒非评价性并拒绝正误暗示。根题合并主回答与最多两层追问证据，评分 revision 异步，不阻塞下一句话。
- Realtime Speech Provider 音频 delta 在 final 文本与批准动作逐字一致前隔离缓冲；批准后和动态 TTS 一样写入加密私有 AgentExpressionAudio，只在角色安全投影时签发短期地址。偏离或故障丢弃 S2S 音频并以批准文本走 cascade。
- 本地 Three.js/VRM 路径校验真实 GLB 的 VRM 1.0、15 viseme、blink、lookAt、humanoid bones、hash、商用与肖像授权；统一音频时钟、动作、barge-in、WebGL context loss 与 FPS 失败关闭均有合同，未提供授权资产时不展示静态图替代。
- 人工接管租约、发言事务复核和媒体 permit 全部使用数据库时钟；普通企业连接只订阅，接管者只获得 15 秒 microphone grant，释放/丢租/结束均移除 participant 且不自动恢复 AI。
- production admission 同时要求精确绑定 deployment/release 的签名 acceptance v2 report、Chrome/Edge/Safari 桌面矩阵、全部硬指标及显式组织灰度名单；`/readyz`、预约邀请/start 与候选人签票均失败关闭。
- 当前完整后端为 `391 passed, 5 skipped`，Vitest `54 passed`；豆包切片的定向 Provider 合同为 `33 passed`，最终 compileall、Vite build 与 diff check 以对应变更日志为准。
- 工作项仍为 `in_progress（仓库实现已收口，目标环境/数据验收 pending）`。本机专属 VRM 已在 `personalProfit` 范围通过合同；真实 G2P/TTS 时间戳、目标使用范围许可、LiveKit/TURN/Egress/OSS、真实 Provider 路由、目标 PostgreSQL/Redis 多实例故障注入、桌面浏览器矩阵、金标与候选人试点无法由仓库伪造；未生成匹配当前 release 的合格报告前不称 production ready。

## 2026-08-31：简历题预约确认后按题库音色生成

- ExperienceQuestion 批准后不再走 `voice_default_cn` 或组织默认 TTS route，而是进入 `deferred` 并可直接冻结到计划；未确认预约不创建语音工作。
- InterviewPlan 现冻结所选题库唯一 `speech_profile_snapshot`；多个题库的模型版本、音色、语言、格式或语速不一致时拒绝装配。预约 settings 的 voice/language 由该快照派生，不能另选一套声音。
- Candidate Intake 成功事务按预约、经历题版本和 profile 指纹幂等排队 TTS，可复用完全匹配的不可变资产。邀请与 start readiness 已拆分：邀请不等待未触发语音，start 必须等本预约全部简历题资产 ready；失败可重试，取消预约会协作取消未完成工作。
- InterviewSession 创建时把预约资产注入简历题快照，并冻结 plan speech profile 与 preparation 证据；生成结果不回写 ExperienceQuestion 全局资产，避免不同计划或预约互相覆盖。

## 2026-08-31：流式模型健康探针与 Qwen Realtime 协议校正

- 模型配置和路由测试现已覆盖 `stt.streaming` 与 `speech.dialogue_realtime`：两者复用统一 stream handshake probe，在真实 Provider 确认端点、凭据、模型访问和 session 后置为 ready，不再要求静音样本产生 final transcript。
- DashScope Qwen 3.5 Omni Realtime 已同步当前 session 结构、`qwen3-asr-flash-realtime` 输入转写模型和 `Tina` 默认音色；历史配置在 adapter seam 内兼容归一化，管理员不需要先迁移数据库记录。
- 健康探针只证明连接与 session 初始化；真实 WER、final 延迟、首音、打断、音质和费用仍属于部署环境的脱敏样本验收，不因本项自动标记完成。

## 2026-08-30：受控实时语音追问与异步评分

- 实际面试已形成两条可选表达链路：`cascade` 保留服务端 STT → 受控追问 → TTS/本地或云数字人的路径；`s2s` 使用统一 `speech.dialogue_realtime` stream 预生成 OpenAI Realtime 或阿里云百炼 Qwen Realtime 音频，但 PCM delta 在 final transcript 与冻结 ApprovedConversationAct 逐字一致前只存在于有界缓冲，通过后才物化私有表达音频并下发。两条路径复用同一 Interview Lifecycle、追问决策、题目快照、事件和前端播放状态机。
- S2S 不取代证据链：同一份候选人 PCM 仍进入权威 streaming/batch STT，唯一 final 形成 CandidateAnswer；完整 LLM 评分由 DurableWorkItem/Outbox 异步执行，先返回 `answer.accepted + evaluation.queued`，评分完成后再广播 `evaluation.completed` 并刷新报告。实时语音输出、浏览器 partial 和 Provider 自由生成文本都不能直接入库为答案或评分。
- 当时的 compatibility 追问是零权重子轮次，带 `parent/root/depth`，只允许深度 1、每道原题 1 次、会话默认最多 2 次，并受回答时间/长度门槛约束；2026-09-01 的正式 Agent 已用证据绑定、最多两层且全场 `min(4, 主问题数)` 的受控策略取代。候选人投影始终不暴露目标关键点、标准答案、内部原因或评分。
- 候选人运行时同时修复了自动播题、16 kHz PCM 与私有媒体格式、完整本地录音备份、断线等待后 batch repair、心跳、企业实时事件、`record_video=false` 麦克风降级、异步评分状态和重复播报/提交。
- 当时新增 OpenAI 官方 provider（Chat/Embedding/TTS/batch STT/Realtime）和 DashScope Qwen Realtime 模型目录/adapter；火山引擎豆包在 2026-09-02 的独立工作项中完成，不回写本段历史时间线。

## 2026-08-29：API response/marshal seam 与 transport 拆分

- 新增 FastAPI 适配的声明式 `fields + marshal` 白名单投影，公开候选人详情和答题响应已迁移，内部评分、题目快照、联系方式等未声明字段不会进入响应。
- 集合响应统一为 `items + next_cursor`，显式 202 响应统一走 response factory；业务、Provider、Persistence、认证、限流和 FastAPI 请求校验错误统一为 `error.code/message/details`，请求校验不会回显原输入。
- `app/api/routes.py` 已缩为 router 总装入口；全部路径按 `system/admin/catalog/talent/plans/interviews/realtime` 物理拆入 `app/api/routers/`。Service Locator、HTTP response/fields 和实时连接/跨实例广播全部位于 `app/transport/`，`app/api` 不再混放 transport implementation。

## 2026-08-29 完成快照

仓库内可独立完成的里程碑 0-13 能力已经实现并通过自动化验证：岗位题库构建、PDF/URL 简历安全摄取、岗位初筛/人工复核/7 天差异化留存、候选人 CRUD、AI/人工候选人个人题库、私有文件、候选人/计划/预约、明确同意、可审计随机抽题、服务端 streaming/batch STT、受控实时语音追问、异步评分、报告导出、企业复核、RBAC/审计、Outbox 加固、PostgreSQL/RLS adapter、Redis 事件 adapter、心跳监控和抽题公平性评估均已有代码与测试。

这里的“完成”只表示仓库实现与本地/离线验收完成，不等于外部生产环境已经通过。DashScope 已有 Qwen-Audio 3.0 实时 ASR/Qwen3-ASR batch 和 Qwen Realtime adapter，OpenAI 已有官方 Realtime adapter，腾讯云智能数智人已有 WebRTC 云渲染会话 adapter 与候选人 TCPlayerLite 播放；本机 PostgreSQL 16、Redis 7 和官方 ClamAV daemon 已完成隔离集成验收，但目标 PostgreSQL/Redis、阿里云 OSS、生产扫描签名更新、OpenAI/阿里/腾讯账号、模型/形象授权、实时首音/打断/并发指标和真实脱敏金标仍需要部署环境输入；生产 readiness 在依赖缺失或健康检查过期时失败关闭。

预约现已显式选择“自研数字人 / 云数字人”。新预约默认低成本自研模式：复用计划冻结的题目 TTS 私有资产，服务端签发短期地址，React 内置形象展示统一说话状态，不创建云会话；云模式完整保留腾讯 create/stat/start/WSS drive/WebRTC/SFU/close 链路，并在不可用时通过同一个 LocalAvatarDelivery 降级。历史无 `avatar_mode` 数据继续按云模式解释，既有链路未删除；后端和浏览器分别只有一个 delivery/playback interface，避免两套业务控制代码。

当前统一验证基线：

- `PYTHONPYCACHEPREFIX=/private/tmp/interviewer_pycache .venv/bin/python -m compileall -q app tests`：通过。
- `cd app/web && npm test -- --run && npm run build`：React 生产构建通过，Vitest `33 passed`；此前依赖审计为 0 个 high 漏洞。
- `.venv/bin/python -m pytest -q`：`224 passed, 5 skipped`；5 个环境测试仅在提供真实 PostgreSQL/Redis/clamd 地址时启用。
- `git diff --check`：通过。

### React Web 工作台

- `app/web` 已建立 React 19 + Vite 工程，FastAPI 从 `/web/bundles/*` 同源托管生产 bundle，并保留 `/web/assets/*`、`/web/vendor/*` 和既有业务 API 路径。
- 总览、题库、招聘流程、面试计划、面试会话、模型服务、公开邀请和候选人面试入口已无损接入 React shell；框架切换不改变 REST/WebSocket 路径或请求体。
- 面试计划的“生成并启用”在同一 Plan Assembly 事务内完成就绪校验和批准，工作台不再要求创建人点击“审批计划”；API 仍保留默认草稿模式供需要编辑/分权审批的客户端使用。一次性邀请弹窗提供“复制链接”和即时反馈。
- 创建预约表单增加数字人方案选择，默认“自研数字人（推荐，低成本）”，也可选择“云数字人（实时视频，需配置服务）”；候选人房间展示实际执行模式和云失败降级原因，两种模式共用播放、停止与云 session 回收 runtime。
- 候选人邀请页已把“身份核验并确认预约”与“到点检查设备并进入面试”拆开；确认事务创建提前 30 分钟的持久邮件提醒和预约级简历题语音工作，队列不携带邮箱明文，SMTP 授权码通过空置的环境变量占位等待部署配置。
- 招聘流程已支持岗位增查改删；删除前展示候选人/岗位要求/计划/预约影响并要求输入完整岗位名称，确认后清除该岗位候选人敏感数据、归档下游流程，同时保留共享题库与不可识别历史。
- 新建岗位弹窗同时采集首版岗位要求、必备/加分技能、目标级别和面试时长；后端原子创建 JobPosition 与 RoleRequirement，创建后可直接用于简历初筛。既有岗位卡片同时提供“添加岗位要求/新增要求版本”，无需删除重建。
- 岗位题库入口始终可用：未关联时显示“关联题库”，已关联时显示“管理题库”。管理弹窗列出当前关系与剩余组织题库；没有新增项时明确说明现有题库已全部关联，并提供题库管理入口，不再用 disabled“暂无可选题库”伪装成故障。
- 候选人和简历版本均支持增查改删：PDF/URL 上传创建不可变内容版本，受控查看，展示名可乐观并发修改；删除会取消未运行工作、清理隔离/私有对象，并保护已进入计划或面试历史的版本。上传接口立即返回，摄取成功后原子排队后台初筛；不同简历版本的审阅工作按 `resume_document_id + input_hash` 隔离，同一 queued 审阅缺少工作时自动补建。短简历单次审阅，长简历按页和 Token 预算执行证据 Map/分层压缩/最终 Reduce；输入与结构化输出预算独立配置，长模型调用使用覆盖 Worker hard limit 的任务租约。失败初筛可在候选人详情通过 latest version + `Idempotency-Key` 领域命令重新排队，重复传输返回同一 replay，重置 work attempt 并保留审计。列表展示处理进度、中文失败原因、符合性、来源页、命中依据和缺口并支持人工复核；服务端统一按 0–59 不符合、60–74 待人工复核、75–100 符合归一化模型建议。仅最终不符合者设置 7 天期限。
- 候选人列表外层现已提供独立“简历问答”弹窗：只有生效初筛结论符合才开放；AI 不符合/待复核不生成，人工改判符合时排入独立工作。AI/人工题都必须绑定审阅中的证据快照且题干点名证据标签；编辑/删除收在三点菜单，批准/拒绝保留为高频操作。无证据旧题、不符合审阅问题和已归档题不会进入读取或新计划。
- Resume Review 最终 Prompt/Schema 已升级为 `resume_review.v6` / `resume_review_reduce.v4`，只负责摘要、证据和岗位要求；预算内单次审阅如果仍以 `provider_output_truncated` 结束，会自动转为完整 Map/Reduce 并记录降级事实，不保存半截 JSON。问题改由 `resume_experience_question_generation.v1` 在符合资格后独立生成。
- 统一 HTTP、路由级 workspace query、schema form 和候选人录音恢复继续作为框架无关 deep modules；题库层级一次聚合加载，总览只取统计和最近 5 条，不再产生岗位/题库 N+1 或首屏完整题库下载。
- `WorkbenchProvider` 已接管认证、hash 路由、角色重定向、加载、刷新、弹窗和 toast；业务组件及命令 hooks 按 `questions/workflow/plans/interviews/models/candidate` 六个 feature 分区，React feature registry 统一导航与角色声明。
- 空工作区可从题库页直接建立岗位与题库，再无缝进入首道题目表单；招聘流程的岗位卡片从组织题库列表建立显式关联，直接复用题目、语音模型和音色，不再重复创建或手填声音。模型连接/配置/路由复用统一 ResourceCard interface，招聘流程复用 panel/table/list interface，保留期清理占位以中文只读状态呈现。搜索工具栏按实际五个控件建立响应式网格，不再换行错位。
- 模型服务已按题库式父子层级组织：首页用卡片目录展示已接入厂商、模型数量、就绪数和模型类型，点击厂商进入独立子页面，在锁定厂商上下文中完成模型增删改查与测试；业务用途和插件清单保留为首页折叠管理区。用途继续使用受控枚举并自动推导能力，只允许选择 enabled + ready 且能力兼容的模型。
- 旧 `app/web/app.js`、HTML 字符串视图和 schema-form DOM renderer 已物理删除；生产入口只加载 Vite bundle。
- 行为测试覆盖生产 Bearer/RBAC/WebSocket ticket、reviewer React 挂载且不访问 `/admin/*`、空工作区首题入口、外部取消/超时、断线录音完整重放，以及预约成功但邀请失败时保留原预约供重试。

## 里程碑对账

| 里程碑 | 仓库状态 | 已完成证据 | 外部/兼容边界 |
| --- | --- | --- | --- |
| 0 项目骨架 | ✅ verified | FastAPI、统一错误、健康检查、启动/worker 命令、自动化测试 | 生产观测平台由部署环境选择 |
| 1 题库管理 | ✅ verified | CRUD/归档、JSON 批量 import、rebuild/build job、语音重建 | 批量 UI 仍以 API 为主 |
| 2 模型网关 | ✅ closed（配置 v2 + Prompt 治理） | `chat_json/chat_text/embedding/STT/TTS/avatar/realtime_speech` schema、invoke/open_stream/open_speech_dialogue、重试/fallback/超时/共享断路器、加密凭证；已实现 OpenAI Realtime、DashScope Qwen Realtime/ASR、Volcengine Ark/Seed ASR/Seed-TTS/Seeduplex 与腾讯云数智人 WebRTC adapter | 真实凭据、区域、模型/语音 Resource ID、形象授权、并发和健康测试待联调 |
| 3 结构化题库查询 | ✅ closed | Question Catalog、Memory/SQLite/PostgreSQL 下推实现、跨岗位拒绝；旧 QuestionService/向量 repository 已删除 | 真实 PostgreSQL 查询计划待环境验收 |
| 4 岗位要求与计划 | ✅ closed | execution v2 canonical slots、显式一次性迁移、候选池冻结、覆盖/难度/去重、权重/时长守恒、审批不可变 | 部署旧数据时先运行迁移命令 |
| 5 会话与实时事件 | ✅ verified | 生命周期、持久事件、WebSocket、Redis 跨实例 adapter、心跳超时恢复、浏览器 16k PCM 实时 STT、统一 Avatar Delivery Runtime、腾讯 WebRTC/SFU 播放与会话回收 | 目标网络/浏览器与腾讯并发仍需外部验收 |
| 6 数字人与语音 | ✅ verified（仓库与本机资产） | 预约级 local/cloud、cascade/s2s 选择，冻结 TTS 签名播放、OpenAI/DashScope/Volcengine realtime speech、DashScope/Volcengine streaming/batch STT、真实 TTS、腾讯云数智人 WebRTC；本地 VRM 1.0/15-viseme 资产、hash、许可 manifest、真实失败暂停和快照前 gate 已验证 | OpenAI/阿里/火山/腾讯真实凭据、音质/WER/首音/打断/费用和生产 route 未验收；当前 VRM 仅确认 `personalProfit` 范围，真实 G2P/TTS 时间戳、目标设备指标及其他部署许可仍待验收 |
| 7 评分与报告 | ✅ verified | 可解释评分、append-only revision、current-only 汇总、JSON/CSV 导出、脱敏人工金标一致性/公平性校准 API | 真实脱敏金标尚未提供，不能形成业务校准结论 |
| 8 岗位题库构建 | ✅ verified | import/rebuild/build、结构校验、Outbox、语音版本/readiness | 真实 TTS 音质与区域策略待联调 |
| 9 企业简历库 | ✅ verified | multipart/URL、SSRF、隔离/扫描、保留页边界解析、原件/解析文本私有 FileObject、候选人及简历版本 CRUD、异步单次/Map-Reduce 可解释初筛、AI/人工个人题库 CRUD/审核/归档、版本化 0–59/60–74/75–100 分数带、人工复核/7 天自动留存、本地/OSS contract、加密联系人 | 真实 OSS/扫描器、目标模型上下文预算与真实简历初筛校准待环境验收 |
| 10 预约与填报 | ✅ verified | PATCH、哈希 token、邀请 UI、服务端告知/明确同意、强匹配、预约确认、30 分钟 SMTP 邮件提醒、时间/设备/model gate、原子幂等 start、候选人安全投影 | SMTP 凭据与目标邮件服务实发待配置；短信未选择通道 |
| 11 可审计语音闭环 | ✅ verified | HMAC 选题、唯一选择事实、streaming final、batch 修复、两阶段评分 | 真实 STT WER/延迟待录音集 |
| 12 企业复核 | ✅ verified | reviewer 权限、签名音频、授权与实际下载审计、转写/评分/报告 revision、导出 | ATS 人工决定集成不属于 AI 报告 |
| 12.5 核心一致性 | ✅ closed | `PLAN/CONSENT/APPOINTMENT/REPORT/SEARCH/CANDIDATE-ACCESS` 回归矩阵；旧 runtime interface 已删除 | 外部环境验收独立列示 |
| 13 生产化/公平性 | ✅ verified（仓库） | PostgreSQL/RLS migration、RBAC、审计、加密、Outbox dead-letter、共享断路器、Redis bus、心跳、公平性 API | 外部服务均标 `environment_pending` |
| 15 题库级 TTS 配置与 Celery 调度 | ✅ verified（仓库与本机） | KnowledgeBaseSpeechProfile、组织默认 route 初始化、voice catalog、整库 SpeechBuild、可理解的试听可用性、revision 防旧写、模型引用保护与 Celery+DurableWorkItem 已实现 | 真实外部 TTS 凭据和目标生产 Redis/PostgreSQL 仍属环境验收 |
| 17 智能生题任务工作台 | ✅ verified（仓库与本机） | 独立 React 路由、批次历史/Worker 投影、持久停止、revision 防迟到、失败/分片重试、输出截断自适应单槽位恢复和审核导入已实现 | 供应商已接受的在途请求不能保证撤销；真实截断恢复、生产并发/成本仍需目标账户验收 |

### 已完成：题库级 TTS 与层级页面

- `#questions` 只展示当前组织的题库卡片：岗位、题目数、当前 TTS 模型/声音、语音 ready/failed 计数和总体状态。
- `#questions/{knowledge_base_id}` 才加载该题库题目、完整增删改查/试听入口、KnowledgeBaseSpeechProfile 和 SpeechBuild 历史，删除题库首页的全题目平铺；删除采用保留历史快照/资产的归档语义。
- 题库配置显式选择一个已 ready 的 TTS ModelConfiguration 和该模型声音；已添加但未测试/失败/停用的 TTS 也会显示原因，并可在配置弹窗直接测试。切换模型或声音创建新 revision，并让全部活动题进入异步重建。
- 创建题目、单题语音重建和题库配置切换均返回 `202 + job_id`，不在 HTTP 请求内执行 TTS；所有 Celery task/执行入口位于 `app/workers/`。
- DurableWorkItem/Outbox 继续作为数据库真相，Celery broker 只调度；`KB-SPEECH-001` 已有代码、Celery eager、Provider fake、React 行为和全量回归证据。
- 题库配置下拉框可选择并查看尚未就绪的 TTS 及声音，但保存按钮继续以真实探针 ready 为门槛；模型探针对 429/超时等 retryable 错误执行有界退避并显示可操作中文错误。2026-08-27 对当前智谱连接的真实厂商校验和 GLM-TTS 探针均返回 HTTP 429，三次退避后仍失败，因此外部账户目前不可用，未伪造 profile/语音资产。
- 新题库优先继承组织已启用且 ready 的 `question_speech_generation` route，冻结实际模型与默认音色；没有真实默认值时才保持待配置（本地离线 mock 明确显示“不可试听”）。题目接口以 `speech_preview` 区分可试听、未配置、生成中、生成失败、开发 mock 和私有文件缺失，页面禁用无效试听并直接告诉用户下一步操作。
- 题库语音配置提交前会重读 KnowledgeBase；后台语音进度只推进聚合 version、未改变 speech profile 时安全采用最新 version，真正的并发配置变化则刷新并要求重新确认。整库失败项和单题失败行均提供重新生成入口；整库重试读取失败题目的当前 version 并创建新的重试工作，不会命中旧 failed/dead-letter 幂等项。
- 模型驱动持久任务已统一 `failed=等待自动重试 / dead_letter=领域终态失败`：可重试 TTS 不再改写 Question version，SpeechBuild 不会在下一 attempt 前误报失败；同一边界也用于题库导入、Resume Review、经历题生成、异步答案评分和报告。整库重试还能从 build manifest 恢复旧 worker 误标 completed/superseded 但 Question 仍失败的历史项；React 在批量重试期间仍允许试听已经 ready 的题目。
- 语音运行中切换已改为 profile revision 语义 CAS：后台进度可以任意推进 KnowledgeBase version 而不阻断管理员切换模型，真正的并发 profile 变化仍失败关闭。新 revision 会取消旧任务，迟到结果作废。题库页面每 1.8 秒刷新活动构建，整库/单题手动重试按钮均显示 spinner、禁止双击，提交后继续在进度区显示转圈。
- 智能生题已实现 `QuestionGenerationBatch -> QuestionBlueprint -> 1–2 题 Celery 子任务 -> 去重/补槽 merge -> GeneratedQuestionDraft -> 人工确认 -> Question` 闭环。10 道题默认拆为 5 个独立生成工作，避免单次大响应超时；规划互斥键与合并层完全匹配/高阈值相似检查共同抑制重复，缺题最多补生成两轮。独立工作台展示规划、分片、合并和补生成进度；候选行可打开完整题目详情，支持逐题编辑、删除、单题异步导入或批量导入剩余题目。AI 草稿不会进入计划或面试；正式题目保留生成批次/草稿来源。Mock/Memory/SQLite 合同和 React 行为均不依赖外网。2026-08-27 的 DeepSeek 单题真实闭环仍有效；新的多任务结构仍需在目标模型账户做真实 10 题并发、费用与限流验收。

### 已完成：智能生题任务工作台

- `#questions/{knowledge_base_id}/generation[/{batch_id}]` 是独立路由级页面；题库详情只保留入口，不再加载或内联展示整批生成状态。
- 工作台按题库列出历史批次；运行态只展示紧凑进度和可用命令，审核态把候选题直接提升到批次标题下方。正常 planning/generate chunk/merge/import 内部明细不占据页面，只有失败项才展示错误与对应恢复动作；候选题审核与导入仍复用原草稿合同。
- `stop/resume/retry-failed/retry-chunk` 由 QuestionGenerationService 深模块拥有，前端不调用通用管理员 Outbox replay。停止递增 execution revision、取消未领取工作；在途 Worker 在 Provider 调用前和结果提交前做 guard，停止后的迟到结果只能 supersede。
- 停止后继续只重建未完成槽位，人工重试保留成功分片和旧失败事实。Outbox 记录协作式取消、开始/完成时间、错误码和 retryability；Celery process/result 不成为业务真相。
- `finish_reason=length` 现在映射为 `provider_output_truncated`，失败调用仍记录脱敏 token/推理用量；生题 route 每个 DurableWorkItem attempt 只调用一次 Provider。双槽位截断自动 supersede 原 chunk 并原子创建两个单槽位替代工作，活动进度不会重复计数；单槽位仍截断则首轮终止并保留人工重试入口。生成 Prompt/Schema 已升级为 v2，并以 4000/8000 token 预算及长度/数量边界约束输出。
- 自动化覆盖排队停止/恢复、过期在途结果丢弃、单失败分片重试不重做成功项，以及 React 停止/分片重试不访问 `/admin/work-items/*`。
- 候选题列表的非操作区域支持鼠标和键盘打开完整题干、标准答案、技能与评分关键点；单题导入是独立 DurableWorkItem，成功后仅冻结该草稿，其他草稿继续审核，批量导入只处理剩余项。
- 单题导入使用草稿级 version 条件而不是要求页面批次 version 必须完全最新；连续点击不同候选题可以立即排队，第一题提交/完成推进批次 version 不再让第二题误报冲突，同一道草稿被编辑后的过期确认仍会失败关闭。
- “新建生题任务”是固定页面操作，点击后用 Modal 承载模型、数量、标签、定位和可选要求；关闭弹窗不改变页面主体，任务配置不再以内联展开/收起卡片挤占历史和审核区域。

## 本轮实现清单

### PDF 与私有文件

- `app/file_storage/` 提供 `PrivateFileStorage`、本地私有 adapter、阿里云 OSS adapter 和最长 15 分钟签名访问。
- `ResumeIngestionService` 统一 `local_upload/url_import -> quarantine -> validate -> scan -> private store -> parse -> ready`。
- URL 下载在初始地址和每个重定向逐跳做 scheme、凭据、DNS 和非全局 IP 校验，禁用环境代理，并限制重定向、连接/读取时间与字节数。
- 非 PDF、超限、加密 PDF、空文本、EICAR、环回/私网 URL 均失败关闭；幂等重试复用同一简历/工作项。
- PDF 原件和解析文本分别写入私有 FileObject；公开 API 不返回正文、对象键或本地路径；旧 JSON `resume_text` 上传已删除。
- Web 上传弹窗提供本地 PDF/公开 URL 双入口、任务状态轮询和 ready 后审阅。

### API、异步任务与运维

- 补齐 CandidateProfile/Appointment/KnowledgeBase/Question PATCH、题目归档、题库 import/rebuild/build、题目/经历题语音重建、简历列表/详情和报告导出。
- Outbox 支持最大尝试、指数退避、dead-letter、状态指标、租约恢复和带原因/主体/审计的人工重放。
- 管理员可查询工作项、审计事件、公平性分布并触发心跳超时扫描。
- 留存任务默认 dry-run；管理员显式执行后删除到期候选人的私有简历/录音并清空联系方式、审阅、转写、评分/报告敏感内容，操作全程审计。
- 初筛自动留存由 Celery Beat 周期任务执行：只扫描 `screening_unqualified` 且超过 7 天期限的候选人，复用同一可审计清理 seam；符合、待复核或处理中的候选人不设置该期限。

### 模型、语音与 readiness

- 题库目录卡片不再暴露内部 `model_configuration_id`；语音信息以独立“读题语音”区展示声音标识和“已配置/待配置”状态，模型配置细节继续留在题库详情与模型服务中。
- `llm.chat_text` 已加入统一 schema；OpenAI-compatible 支持 Chat/Embedding/Speech TTS，DeepSeek/智谱 Chat 复用共享 runtime 并适配 JSON Object，智谱另实现官方 GLM-TTS，DashScope 支持 Qwen Chat/Embedding 以及 Qwen3-TTS/CosyVoice，均有离线 HTTP 合同测试。
- 模型管理已拆分为 `ProviderConnection → ModelConfiguration → ModelRoute`：Provider manifest 声明连接/凭证及 `llm/embedding/tts/stt/avatar/realtime_speech` 模型表单，管理 UI 通用渲染；模型测试更新健康事实，route target 只引用 ready 的模型配置。连接和模型使用独立 `configuration_revision` 区分配置语义与探针 version，所有可执行模型类型共用 latest-version command，探针完成后编辑/删除不会因旧 version 误冲突，真实并发配置变化仍失败关闭。未知字段返回 422、同组织重复 capability/purpose 返回冲突、predefined 目录外模型返回冲突；生产缺少精确 route 时返回 `provider_route_missing`。
- OpenAI-compatible 共享 transport 默认不继承环境代理，只有显式 `use_environment_proxy=true` 才读取代理变量；缺少 SOCKS transport 等初始化错误映射为 `provider_transport_unavailable`，不再泄漏原始 500。测试 helper 强制新建内存 store；当前全量 `220` 项通过测试不会触碰开发 SQLite。
- `ModelGateway.open_stream()` 只在音频接受前允许 fallback；`ValidatedSTTStream` 校验 chunk/总量、事件序号和唯一 authoritative final。`open_speech_dialogue()` 使用同一 route/断路/调用日志策略，但只承载受控追问表达。
- 正式 `AuthoritativeEvidenceIngress` 从 LiveKit candidate microphone 轨保存私有音频，stream final 立即形成 CandidateAnswer 并排队完整评分；final 缺失/断流时从已 seal checkpoint 使用 `stt.batch` 修复，必要时接收服务端授权 gap 的浏览器 backfill。S2S 输出不形成答案，也不绕过评分 Prompt/Schema。
- 非 mock TTS 结果必须从 data URI/受控 HTTP(S) 复制到 PrivateFileStorage 并形成 FileObject，才可标 `production_ready`。
- 生产邀请/start 要求 streaming/batch STT、理解、受控追问、评分、表达 TTS 和 Realtime Speech route 为非 mock、已实现且健康事实未过期，并要求授权 VRM、LiveKit/私有存储、当前 release 的签名 acceptance v2 与逐组织灰度门禁全部通过。

### 数据、安全与多实例

- 联系方式采用 Fernet 密文和租户 HMAC 精确查找，API 只返回掩码；Provider credentials 在 repository seam 密封。
- 生产 Bearer token 映射为 `Principal`，按 admin/interviewer/reviewer 做 RBAC；候选人 token 由生产密钥签名且不明文持久化/返回后台详情，public 窄接口的安全投影不包含标准答案、rubric、候选池或未来题干。
- 所有 `/api/v1` HTTP 结果写元数据审计，未认证失败也记录；URL bearer token 在持久化前统一替换为 `{token}`。
- invitation、候选人会话与签名文件端点按客户端/操作组限流；生产强制 Redis，缺失或故障时通用失败关闭，429/503 同样审计。
- 回答录音和简历使用短期签名访问；音频授权与实际下载分别审计，不再挂载公开 `/media`。
- PostgreSQL migration 提供 JSONB documents、乐观并发、Outbox/凭证/调用日志、关键唯一约束和强制租户 RLS。
- Redis event bus 支持跨实例广播并忽略本实例回环；无 Redis 时本地 bus 保持相同 interface。
- 模型断路器使用 Persistence 中的租户级 `ModelCircuitState`，应用实例共享失败/恢复状态。

### 公平性与人工决策

- 公平性服务按岗位比较会话题量、平均难度和技能覆盖，超过阈值产生人工复核告警并记录审计。
- 脱敏金标校准只接受 current evaluation ID、人工分和不透明 cohort，输出 MAE/RMSE/偏差、分层差异与不自动生效的线性拟合；少于 30 条/每组少于 10 条会告警。
- 报告始终 `human_decision_required=true`，不写录用/淘汰；当前没有用户提供的真实金标，STT WER、评分一致性和漂移仍未完成实际校准。

## 外部环境待验收

| 项目 | 状态 | 为什么不能在仓库内宣称完成 | 已提供的验收入口 |
| --- | --- | --- | --- |
| PostgreSQL 集群/RLS/查询计划 | `local_integration_verified / target_pending` | 本机 PostgreSQL 16 已通过；目标集群 DSN、角色和负载不同 | 显式 owner migration、最小权限 runtime、真实事务/CAS/约束/RLS/索引集成测试 |
| Redis 多实例广播 | `local_integration_verified / target_pending` | 本机 Redis 7 已通过；目标集群拓扑/故障策略不同 | 跨实例 Pub/Sub、正常关闭和生产限流真实测试 |
| 阿里云 OSS | `environment_pending` | 官方 `oss2` SDK 与 adapter 已可加载，但没有 bucket、RAM 凭据和区域 | SDK 依赖、OSS adapter、SSE/签名 fake-bucket contract、`/readyz` 只读 bucket 鉴权探针 |
| 恶意文件扫描器 | `local_integration_verified / target_pending` | 官方 ClamAV Debian arm64 daemon 已用隔离 EICAR 签名库通过 PING、干净样本与 FOUND；目标环境完整病毒库、freshclam 更新和告警仍未提供 | command/clamd 双 adapter、生产连接 readiness、env-gated 真实 EICAR 测试 |
| OpenAI/OpenAI-compatible/DeepSeek/智谱/DashScope/Volcengine 模型服务 | `partial_real_verified / target_pending` | 当前 DeepSeek 凭据已完成一次真实智能生题；OpenAI、DashScope 与 Volcengine Realtime 只有离线协议合同，智谱 TTS 仍被账户 429 拒绝，其他目标模型的区域、授权、费用/延迟和长期稳定性数据仍不完整 | 离线 HTTP/WebSocket 合同、声明式模型目录、动态 route UI、真实 DeepSeek 候选题审核批次、私有 TTS copy/hash 与 readiness |
| OpenAI / DashScope / Volcengine 实时语音追问 | `repository_verified / environment_pending` | 统一 `speech.dialogue_realtime`、冻结追问文本约束、批准前 PCM 隔离缓冲、批准后私有 AgentExpressionAudio、打断/降级和 S2S 不等待评分 ack 的端到端合同已实现；没有 API Key/Workspace/Resource ID、模型权限和真实网络指标 | 配置 `candidate_followup_dialogue` route，实测首音、抖动、barge-in、音质、成本和长期连接；S2S 偏离/失败必须丢弃原始音频并继续批准文本 cascade |
| DashScope 真实 STT | `repository_verified / environment_pending` | Qwen-Audio 3.0 duplex streaming、Qwen3-ASR batch、16k PCM 浏览器链路与离线合同已实现；没有 Workspace/API Key/录音金标 | 配置连接与两条 purpose route，真实测 WER、partial/final 延迟、断流修复、费用和健康 TTL |
| 火山引擎豆包语音与 Ark | `repository_verified / environment_pending` | Ark Chat/Embedding、Seed ASR streaming/batch、Seed-TTS 2.0 与 Seeduplex API v3 adapter/离线合同已实现；没有真实 Ark/Speech API Key、语音 Resource ID、模型授权、配额和目标网络样本 | 逐个测试 ModelConfiguration，再配置 `candidate_answer_transcription`、`candidate_answer_repair`、`question_speech_generation`、`candidate_followup_dialogue` 等 route；实测 WER、英文技术实体召回、final/首音、打断、音质、并发和费用 |
| 腾讯云 WebRTC 数智人 | `repository_verified / environment_pending` | HTTPS create/stat/start/close、签名 WSS SEND_TEXT、TCPlayerLite 拉流和会话回收已实现；没有 AppKey/AccessToken/形象资产/并发 | 配置连接、形象与 avatar route，在目标浏览器验证建流/口型/延迟/离场回收和并发计费 |
| 评分/公平性金标 | `repository_verified / data_pending` | 脱敏 current-evaluation 校准 API、分层指标、样本量告警与审计已实现 | 企业提供经授权的真实脱敏 evaluation ID + 人工分 + opaque cohort，至少 30 条且每 cohort 至少 10 条 |
| 邮件提醒/短信邀请 | `environment_pending` / `external_choice_required` | SMTP adapter 与 30 分钟提醒已实现，但授权码、发件域名和目标服务未配置；短信通道未选择 | 一次性邀请 token/API 与手工安全分发保持可用；配置 `INTERVIEWER_SMTP_*` 后由 Celery 投递提醒 |
| 自托管 LiveKit/TURN/Egress | `repository_verified / local_stack_verified / production_environment_pending` | ADR-0002 的房间/最小权限票据/Egress/webhook、浏览器发布、receive-only Evidence subscriber、`database_fenced` ownership、连接无关 owner executor/remote receipt、DB polling/Redis wake hint、持久媒体 repair 与授权 gap backfill 均已有仓库合同；固定版本本地 LiveKit/Egress/Redis 栈已用单参数启动，真实 Participant Egress 生成含 H.264 视频和 AAC 音频的本地 MP4，应用 `/readyz` 的媒体与权威音频探针均通过 | 生产仍需最小权限 LiveKit/OSS secret、当前 deployment/revision、组织灰度名单、TURN/公网网络、30 秒断流/零重复答案、浏览器矩阵与并发验收 |

## 已关闭的兼容边界

- 管理员直接创建/启动会话、客户端 REST/WebSocket 文本答案、计划运行时 `items`、旧全局题目创建/列表和 `QuestionService`/向量 repository/worker 分支均已物理删除，不再用 production 条件分支隐藏。
- 旧计划只能在应用升级前通过 `python -m app.migrations.plan_execution_v2 --dry-run` 检查，再执行无 `--dry-run` 的显式一次性迁移；运行时不会懒迁移。
- 旧模型配置只能在升级前通过 `python -m app.migrations.model_configuration_v2 data/interviewer.sqlite3 --dry-run` 检查，再执行同命令去掉 `--dry-run`；运行时不读取旧 collection 或 route target。
- 当前仓库 SQLite 数据检查为 0 份 InterviewPlan，未产生数据改写。接口删除和迁移行为由 `tests/test_production_compatibility.py`、`tests/test_plan_assembly.py` 覆盖，`COMPAT-001` 已标 `closed`。
