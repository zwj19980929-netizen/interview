# 接口设计

## 2026-09-10 · 补充确认合同与候选人提示（034）

不新增 REST、事件类型或客户端文本答案。内部模型 wire 合同 supplement_reply.v3 必须且只含 intent（continue/finish/supplement/pause/unclear）、confidence（0–1真实数值，非bool）、evidence_id（本次服务端原文编号枚举）。网关校验后，ConversationUnderstandingService 恢复原有 intent/confidence/evidence_quote 领域输出并再次校验；v1/v2审计仍可读。350输出token、8秒调用预算、0次网关内部重试；完整回答内容决定三次端点失败预算，acoustic revision不清零。

已有 continue_speaking/evidence.continue 明确用户操作会重置失败预算；若正在等待补充分类重试，保留本次回复边界重新判断，不凭按钮伪造finish。UNDERSTANDING_UNAVAILABLE与UNDERSTANDING_RETRY_EXHAUSTED等既有内部code保留，候选人只按稳定映射显示等待/重试；真实致命错误只在暂停回执后声称已暂停。成功分类用既有 supplement_awaiting_reply 清除旧告警。音频仍下发既有私有audio_uri、duration_ms、visemes及delivery=cascade，不向候选人暴露厂商URL或新PCM传输。

## 2026-09-09 · 候选人调用方会话绑定（033）

REST/WebSocket wire 合同不变。公共面试投影返回后，调用方先核验响应 id 与请求路由一致，再将投影与该请求的 `X-Candidate-Session-Token` 作为同一客户端绑定使用；加载新会话期间不得用新 token 调用旧面试的 avatar-config、agent-ticket 或 runtime-problems。旧路由迟到的成功、403 和暂停回执不得更新当前页面。内部前端 facade 的 open 增加可选 `signal`，仅取消当前启动流程，不扩大权限、不改变预约截止时间或恢复已终止面试。真正的授权失败仍阻止进入房间。

## 2026-09-09 · 面试截止时间收口（031）

本变更不新增公开命令。API 进程按 `INTERVIEWER_INTERVIEW_DEADLINE_SECONDS`（默认 15 秒）扫描会话：当前时间到达冻结的 `scheduled_end_at` 后，尚未完成候选人输入的 `scheduled/waiting/in_progress/paused` 会话通过生命周期取消命令变为 `cancelled`，响应保留 `termination_reason=appointment_window_expired` 与 `expired_at`，并写 `interview.appointment_window_expired` 审计。该收口幂等，重启后会补偿；已有 `candidate_input_completed_at` 的会话继续评分/报告，不因预约时间到达而被取消。前端将此类取消显示为“已超时结束”，仍可查看和按既有终态规则移出列表。

## 2026-09-09 · 面试列表移除（029）

`DELETE /interviews/{id}?expected_version={version}` 供管理员/面试官把单场面试从默认工作区列表逻辑移除。接口只接受 `cancelled` 或 `report_ready` 会话，使用聚合版本做乐观并发控制，并写入 `interview.removed_from_list` 审计；进行中、暂停中、等待中、评分中或报告失败的会话返回 `409 INTERVIEW_REMOVAL_NOT_ALLOWED`。默认 `GET /interviews` 不返回已有 `list_removed_at` 的会话；`GET /interviews/{id}` 仍可按 ID 读取，回答、评分、报告、录音、候选人资料及保留策略均不变。该接口不是候选人隐私清除或物理删除入口。

## 2026-09-09 · 简历尾题与中文术语（028）

生成计划省略 resume_review_id 时自动绑定同组织、同候选人、同岗位最新合格审核，选最多3道已批准且有证据的简历题（不足2道明确警告）。question_count仍指岗位题。assembly_summary增加experience_question_count/experience_question_target。预约确认登记才幂等排队简历TTS；readiness的experience_question_speech增加blocking=false，真实ready仍可为false但不阻塞can_start。正式题结束后逐题决定简历题可用性，turn.skipped携带resume_speech_not_ready原因；报告增加skipped_questions，不生成虚假答案。

## 直接评分与识别提示（027，替代026的强制待核验策略）

评分调用成功即返回score和dimension_scores，score_status=available；review_flags/quality_warnings保留不确定性，transcription_ambiguity及low_stt_confidence不阻断总分和报告。旧pending_verification中保存的provisional_score可只读恢复为数值并按冻结权重汇总，不改历史revision。真正缺失/失败的评分仍不得伪造为0分；重评进行中继续显示processing。报告新增recognition_warning_answer_ids和识别质量提示，JSON/CSV保持一致。transcription-verification作为可选回听纠错入口，不是获得分数或完成复核的前置条件。

StreamingSTTRequest.recognition_terms允许最多100项、每项2–64字符的英文技术标识或最多6词的英文读法。仅从服务端冻结的当题技术词推导，例如worker_prefetch_multiplier与worker prefetch multiplier；原标识优先、去重、边界校验。不传标准答案正文或从候选人发言生成词表。当前适配器原有模型支持范围和词权重不扩大。

## 语音质量、澄清与评分核验（026）

统一STT的confidence为可空0–1数值；null表示厂商未提供，不能用1.0或0.0冒充。新CandidateAnswer持久化stt_confidence_source（provider/partial/unavailable/synthetic）；历史未记录来源的数值仅按legacy_unverified投影。未知声学可信度不自动阻断语义理解，真实低可信仍需澄清。当前理解wire必须显式返回可空clarification_target（无歧义为null），wire为原文E编号与该句内focus_quote，精确还原并校验后才可播报；正式连续采集中的澄清不封存为答案、不丢弃前文。

`POST /interviews/{id}/answers/{answer_id}/transcription-verification`（管理员/复核人）接受expected_evaluation_id、expected_transcript_revision、audio_reviewed=true、reason及可选final_transcript。服务端核对当前评分/转写版本并记录实际认证操作者；空白理由、旧版本或仍在评分拒绝。确认原文或纠正转写均记录核验事实并通过同一事务排队新评分revision，返回202，不修改历史评分、不由客户端填写分数。核验仅绑定当次转写及录音；后续修正、追加追问或重评不得继承无关核验。

复核、报告及JSON/CSV导出增加score_status和待核验题目。存在实质转写歧义或已知低识别置信度的题保留AI建议作为provisional_score，score为null；整体overall_score及维度汇总暂为空，不能按0分或移除权重后生成总分。unknown来源本身是质量提示，不等同争议。旧报告按当前规则进行只读质量投影，历史存储不改写；核验重评通过后产生新的可用报告。复核完成不能跳过未决评分。

## 面试提交、评分恢复与回放（025）

内部ChatJSONRequest增加可选execution_budget（timeout_s: 0–300秒，max_provider_retries: 0–3，拒绝额外字段），只由受信服务代码指定，非HTTP客户端参数。评分设置120秒/0次，整体240秒且Outbox租约300秒；其他调用保持路由原配置。

`GET /interviews/{id}/review` 增加 `processing`（收齐时间、评分完成/失败/待处理数量、报告状态、可重试标志）、脱敏 `recording` 状态和逐题 `playback`（音频可用、录像片段起止秒/时间来源）。页面按服务端状态轮询；提交回执不等于评分成功。逐题评分错误使用固定错误码，不返回Provider正文。

`POST /interviews/{id}/processing/retry` 返回202，仅重排当前失败/孤立的评分、报告与录像收尾工作，保持现有答案和评分revision，不调用模型阻塞HTTP；重复点击不产生重复工作。`POST /interviews/{id}/recording-url` 签发五分钟全场录像读取许可；录像必须完成且有hash、字节数与存储保护事实。逐题音频沿用 `answers/{answer_id}/audio-url`。两种许可均绑定组织、会话、资源和操作者并审计。签名媒体GET/HEAD支持单byte Range（200/206/416）；录像分块读取，不把整场视频加载进应用内存。未完成、已清除或不匹配资源拒绝访问。

按题录像导航使用服务端采集起止时间与Provider文件时钟的交集；旧数据只能提供标明来源的轮次时间窗口，不能冒充精确的单句对齐。逐题WAV为独立答案原录音。

## 确认后等待状态与失败边界（024）

session.snapshot新增可空 `answer_preparation: {status:"preparing", turn_id:string, capture_id:string}`，仅表示当前候选录音的后台准备状态，不授权结束或形成答案。准备通知持久化当前turn/capture事实，返回收听、失败、换题、换capture、暂停时清理或不再投影；快照按当前身份过滤，不能继承旧题准备。前端以该权威字段及supplement_confirmation修正本地陈旧状态；不能因旧页面曾是answer_preparing而永久保留。evidence.ready仍只由既有采集确认决定。

准备的45秒期限约束尚未完成的模型计算；同一完整证据的成功结果可在新完整final、当前上下文/所有权/录音门禁重新验证后复用，完成结果不因声学取消后的重入时间被强制重算。同一服务端证据的失败预算不能被client/audio活动清零，真实新增文字或明确重试才重新授权预算。完整wire和回答理解均已校验后，可选追问被拒绝不再丢弃有效理解；只放弃不合格追问并记录安全阶段/类别。

## 明确结束且无技术回答（023）

TurnUnderstanding新增 `intent=answer_declined`，仅表示从完整服务端原文明确理解候选人本题不再作答（例如不会、请求下一题，或独立确认结束且无实质回答）；使用 `suggested_action=next`，非空原文证据、空claims/covered、全部能力点missing，无未解决歧义，不生成技术追问。理解置信度至少0.75，描述对这项意图的把握，不能因知识点缺失而降低为没听清。低语音置信度仍需澄清。理解Prompt为v6/v7、组合决策为v5/v6，历史版本继续可读。

supplement_reply.v2保持continue/finish/supplement/pause/unclear三字段返回结构；未解决的字幕投诉优先unclear，不算技术补充。已完成的术语纠正、技术方案中的假设或引用须按实际语义处理，不能用关键词否决。

此完整发言沿用受所有权、题目、STT/上下文指纹及完整录音保护的答案事务，保存原始转写与独立understanding，按实际未提供技术回答进入评分与下一题；不造技术主张、不调用无Evidence fence的管理员skip。未结束的思考、含糊应答、空转写、真实字幕争议仍不能据此推进。历史事件和答案不改写。

## 2026-09-08 实时识别完成语义补充（022）

供应商流式 STT 增加内部 `transcript.empty` 完成事件：仅在完整发送并收到供应商正常结束确认、整段没有非空文字或未决文字假设时产生。事件必须 `is_final=true`、空 text/segments、包含 provider 和当前 stream_id，单流只允许一个完成结果。它表示本段识别完成但没有文字，不表示候选人回答了“没有”，也不能单独形成答案。超时、断线、先前出现非空 partial 后丢失结果仍是错误，必须保留和恢复证据。

连续采集可在已验证的空完成后推进音频水位，避免同一无文字片段无限重放；已有完整答案和结束语义仍须通过当前输入、所有权和提交校验。候选人字幕面板按当前 turn_id 隔离，切入追问/下一题清空当前题字幕，旧题迟到事件不改变当前题字幕和收音状态。

确认后的纯准备可在同一capture和完整STT指纹一致时复用；新完整快照仍是恢复提交资格的前提，变化的证据和上下文会废弃准备。单份准备总预算45秒不因重新等待续期；单次STT finish最多10秒，PCM接收缓冲统一60秒以覆盖收尾和有界恢复，不能在进入恢复时缩小容量。

## 补充确认的上下文保留（021）

不新增REST或客户端答案输入。结束确认后的新活动只撤销PreparedTurnDecision，继续保留原补充问答，阶段退到awaiting_reply而非listening；新回复继续按既有supplement_reply语义合同处理。确认final划定后续回复边界，重复明确否定不再触发新supplement_check，真实继续/实质补充才返回listening。完整final覆盖其已有partial，延迟到达的同前缀字幕不产生新输入revision；超出前缀的新文字仍撤销。

复用结束语义必须重新取得完整服务端final且文本、分段、置信度及Provider指纹均与已确认快照一致；缺final、未识别尾部、所有权/题目失效仍禁止提交。turn_control安全日志接入已有独立INFO通道，记录确认意图、撤销来源、阶段与音频播放状态，文字只记散列和字数；不记录完整答复、模型响应或凭据。

## 播放事实与语音活动（020）

候选人可发送 `avatar.performance.playback`，payload严格为 `{performance_id: string(1..128), status: "playing"|"blocked"|"failed"|"buffering"}`。仅当前候选控制器、当前题目和active_performance_id匹配时接收，回投同名transient事件用于诊断；不授予答案提交、结束播放或转换floor的权限。`avatar.performance.started`仍表示服务端已交付可播放音频，实际浏览器`playing`回执才表示媒体开始推进，不能证明操作系统或标签页扬声器未静音。旧客户端没有该回执时仍兼容。

级联播放受阻保留本题与已批准音频，在界面提供“播放这句话”；加载/卡顿8秒后提示恢复，暂停、替换、离场清理旧音频及计时器，补充播报仍受服务端30秒回执预算约束。声音活动与音频帧接收分开；共享服务端VAD要求近120毫秒内至少100毫秒具有语音特征且RMS达到配置门槛（默认0.006，约−44.4dBFS）。所有PCM仍送识别；实际当前capture新增服务端文字可独立重置静音与撤销提案，重复文字不重置。连续无声5秒才询问，沉默及无有效文字均不能充当肯定/否定。

## 识别术语及补送内部扩展（019）

未新增 REST 或候选人文字输入接口。StreamingSTTRequest.recognition_terms 是服务端从冻结题干、skills、key_points 提取的技术标识符列表：至多100项，单项2–64字符、ASCII字母开头，只允许字母数字及 _+.#-，大小写去重；不接受句子或标准答案全文。ValidatedSTTStream.send_audio(chunk, wait_for_capacity=True) 仅供独立补送路径使用；可选 Provider.wait_audio_capacity(byte_count) 等待真实发送容量，未实现的插件保持兼容。等待取消不累计 byte_count、不重录。

## 已确认答复的收口反馈（018）

未新增REST/候选控制字段；内部AnswerEndpoint可接受本轮尚未恢复识别的权威final_snapshot，必须通过现有capture提交守卫。TRANSCRIPT_UNAVAILABLE表示本段最终转写尚未取得，提示“已收到音频，仍在等待本段最终转写；已有字幕和回答会保留”，不再宣称整题未识别或强迫重说。候选提示采用自然语义表达，不呈现固定口令集合。服务端partial仅可撤销旧提案，不能变成候选答案或完成许可。

确认完成后的候选准备状态明确显示“已收到结束确认，正在整理回答”，说明无需重复确认且仍可开口补充。

## 正式回答的补充确认（016）

沿用Agent事件与控制接口，不新增REST路由。新增批准act_type：supplement_check、supplement_continue、supplement_clarify，不能覆盖候选页面的当前题干；它们必须合成批准文本，只有文本与冻结题干完全一致时才能复用题目音频。floor.changed(reason=supplement_awaiting_reply,owner=candidate)携带当前turn_id/capture_id，收音保持ready。session.snapshot增加可空supplement_confirmation，仅允许status(listening/awaiting_reply)、turn_id、capture_id；不含转写边界或意图模型输入。确认来源必须为当前capture新增的服务端final，客户端仍不能提交文字答案。finish_answer/continue_speaking保持可选按钮；口头答复完成正常轮转。UNDERSTANDING_UNAVAILABLE与UNDERSTANDING_RETRY_EXHAUSTED区分重试中/耗尽，保留安全消息和scope，错误后退出answer_preparing，新准备清除旧告警。

## 正式转写的内部只读预览（015，仓库verified）

`ValidatedSTTStream.supports_stable_preview/preview()` 是服务端内部可选能力，返回独立 `StableTranscriptPreview` 或暂无稳定结果；不新增候选人提交接口，不改变 WebSocket `transcript.final` 唯一性。预览含流身份、revision、稳定text/segments、language/confidence/provider及has_unstable_tail，无is_final字段。它不关闭识别、不等待厂商完成，不声明已处理全部PCM。Evidence仅将无未定尾句的已校验预览用于可撤销准备；实际finish与持久提交仍要求完整server final和现有所有权/输入/决策指纹。未支持该能力的Adapter保持原路径，客户端无需更新协议。

自动等待不强行收口；沿用现有候选人显式提前结束命令时，即使暂无可用稳定句也允许一次真正final，仍通过语义/输入/fence后才提交。内部指标新增stt_recognition_opened、stt_recognition_finished、stt_preview_prepared、stt_preview_final_revised，均只接受数值、不包含会话/候选人标签。预览以100ms有界轮询，当前owner仍校验，完整会话读取与持久前缀封存只在真正准备时执行。

## 正式采集恢复（CONTINUOUS-CAPTURE-RECOVERY-014）

正式 STT 的短时故障进入有界自动恢复，不直接把 InterviewSession 置为 paused。沿用 `floor.changed`，当前 turn/capture 下 `answer_recovering` 保持 owner=candidate 与音频采集，恢复成功 `answer_listening`；重试耗尽 `answer_retry_required` 为 owner=none 并停止本次采集，不提交残缺回答。对应可恢复 problem 为 `CAPTURE_RECOVERING/continue_listening`、`CAPTURE_RETRY_REQUIRED/retry_answer`，这两类必须携带 capture_id 与事件 turn_id，旧 scope 不影响当前采集。

候选 snapshot 新增可选 `capture_recovery=null|{status,turn_id,capture_id,attempt,max_attempts}`，status 为 recovering/retry_required，attempt为0..3、max_attempts为3。只投影当前未完成题且会话in_progress的恢复状态，不包含原异常、Provider响应或内部stage。重连时从该事实恢复界面。

候选人点击“重试本题”复用 `continue_speaking` 并携带当前turn_id/capture_id；服务端须验证持久retry_required、当前题未作答、当前owner及同一采集证据，归档不完整前缀后开启新代收音。新open-ready使用本次signal.causation_id，晚到/重复重试不得再次清空新采集。普通open不能绕过retry_required；完整录音/已提交答案不可借此重置。

识别重连每轮至多3次、单次4秒，短退避；等待期间保留有界音频。只对白名单暂时故障恢复，owner/授权/证据完整性或未知提交结果仍禁止自动提交。原始安全错误码、阶段和次数在特权记录持久保存，不能只写通用ApiError。试音原有自动完成不变，无新增公开HTTP接口。

重试信号不接收客户端音频格式或文本：正式LiveKit重开使用服务端16kHz/mono/PCM格式，保留原signal.causation_id。恢复新积压上限30秒；上限导致缺口时直接重试本题而非静默遗漏或整场暂停。持久状态/采集门变更先于至多1秒的UI投影；真正存储/完整性故障仍安全停止。候选主动pause撤销当前采集和后台恢复，不能靠浏览器静音来保证停止。

## 路由就绪自动刷新（ROUTE-READINESS-REFRESH-013）

`POST /api/v1/interview-appointments/{id}/invite` 在签发事务之前，对本次预约需要的已配置模型路由按需执行有界合成探针；健康且未过期则复用，不在数据库事务内等待外部网络。最终邀请事务仍重新校验全部门禁，探测成功不等于已发邀请。GET 不触发付费模型调用。

新增 `POST /api/v1/interview-appointments/{id}/readiness/refresh`：仅刷新路由证据并返回当前预约 readiness，不修改预约状态或签发令牌，使用企业预约权限和审计。候选人设备 readiness/start 的自动刷新须先验证有效邀请与当前阶段，不能让无效公开令牌触发模型费用。

后台 route 列表增加服务端计算的 `readiness`：`status` 区分 healthy/expired/untested/checking/failed/configuration_invalid，提供安全的 reason_code、checked_at/expires_at/retry_at 等时间；不输出原始 Provider 异常或凭据。预约 checks 保留既有 ready/production_ready/mode，并增加 route_readiness；过期、未检测、检测中使用不同 mode。自动探针有总时限、并发上限、同租户/路由去重和失败冷却，配置或凭据发生变化后旧探针不得覆盖当前证据。超时/失败仍关闭准入，不能回落 mock 冒充恢复。

自动网络预算为每条 15 秒/整批 30 秒，取消处理另有至多 0.1 秒等待宽限；前端邀请与候选 readiness/start 局部请求上限 40 秒、手动路由测试 35 秒，均保持操作中状态并防重复。显式手动重测允许重试冷却中的失败，自动刷新遵守冷却。连接清理失败独立分类 `provider_probe_cleanup_failed`，不与开流超时混同。显式停用路由属于 configuration_invalid，不能降为开发 mock。

候选返回的 `can_start` 使用当前登记、同意、时间窗、设备 TTL、计划批准和模型证据共同计算；网络等待后再次核对，不把 device.ready 旧布尔值当作当前准入。等待期间另一请求已消费邀请时，start 仍返回原已创建会话；归档计划或取消预约不得凭较早探测结果发邀请。创建预约成功后的界面重试保留同一预约，已取得邀请但列表刷新失败也不再次签发。

## 批准语音的流式播放（工作项 012）

**实验合同，默认关闭**（`INTERVIEWER_STREAMING_TTS_ENABLED=false`）。2026-09-06 Chrome 实测2秒PCM中间停供3秒，
媒体 currentTime 在 EOF 已为5.122秒且未触发 waiting，不能证明内容样本的真实排空；以下流式播放/ACK只供显式测试，
未通过 source sample→播放位置校验前不得作为默认正式表达。默认仍用完整私有资产和原生 ended，自动轮次与合并推理不受此开关影响。

`avatar.performance.started` 新增 `delivery=streaming_tts`：`audio_uri=null`，
`live_audio={output_id,publisher_identity,track_sid,track_name,sample_rate_hz,channels}`。
绑定来自本场已批准 act 的独立 LiveKit 音轨；客户端只播放精确匹配的绑定，不播放未知 participant。
客户端在绑定音轨并请求播放后发送 `avatar.performance.ready {performance_id,output_id}`；
服务端等待当前候选人控制连接确认后才发布首个 PCM，防止首段先于订阅丢失。
`avatar.performance.producer_finished {performance_id,output_id,total_samples,sample_rate_hz}`
仅表示 Provider 唯一 final 与本机发送队列排空，不表示候选人听完；浏览器按本地媒体时钟实际排空后发送
`avatar.performance.stopped {performance_id,output_id,reason:drained}`，服务端校验当前输出与 EOF 后才移交话轮。
新流式 started/producer_finished 均为 transient，不重放过期轨道；候选快照新增可选 `active_performance_id`，
用于暂停、完成、换代时撤销播放。断开/owner 丢失/打断须取消 Provider、清空发送队列和撤销 track。
等待 ready、Provider 空闲、总时长和等待播放确认均有界；PCM 不通过 AgentEvent/Redis 传输。
流式 G2P 口型为估计，首音由客户端 `playing` 观测；未通过真实浏览器验证前不承诺精确同步或首音目标。

控制连接中断时，当前 live 输出撤轨并清空 active 标识，保存原批准 act 的事件引用为待重播标记，Floor 暂为 none。
新控制连接 `client.ready` 仅在原题/原批准仍匹配时用新 performance/output 重播该批准表达；不恢复过期轨道或从任意字节续播。
这是断线后的明确重播，不是已发 PCM 后对 Provider 失败的自动重试；暂停/接管/换题必须丢弃旧待重播标记。

## 可撤销自动轮次（INTERRUPTIBLE-AUTOMATIC-TURNS-012）

正式收音保持开放，停顿只产生可撤销 AnswerEndpointProposal。服务端本地音频轮次检测可自动提出收口，`finish_answer` 仍为可选的显式建议；两者不能把浏览器文本变成答案。准备阶段允许当前 capture 的 `speech.started`/`continue_speaking`，新输入使旧提议失效。准备与提交分离：媒体 checkpoint 不提前 complete，后台理解不会占住控制执行器；只有当前 owner/capture/输入版本、权威 final 与决策上下文一致才完成一次答案提交。未知模型判断保持收音，不按超时默认完成。暖场现有自动完成合同不变。本节替代 011 强制按钮语义。

新增 transient `floor.changed(owner=candidate, reason=answer_preparing|answer_listening|answer_detector_ready, capture_id)`；候选端只接受当前 turn/capture，准备中继续 PCM/VAD。真正提交成功后才投影 `owner=none, reason=answer_processing`（同样携带刚提交的 turn/capture）并关闭前端输入门，旧 scope 的 processing/准备事件均不得影响新采集。显式按钮文案为“提前结束回答”，不承担必经流程。模型不可用和不确定以 `problem(recoverable=true, action=continue_listening)` 提示；同轮最终转写/processing 仅清准备类可恢复提示，检测器恢复事件仅清其不可用提示，不清严重故障；不发送内部响应或证据指纹。

内部 journal `evidence.seal(endpoint=prepared_turn)` 只保存有界 `capture_id/proposal_id`，不保存转写或模型提案。执行器还必须找到当前进程内对应 PreparedTurnDecision 并执行 guard，客户端伪造引用不能触发提交；丢失 owner 后没有提案的旧命令不产生新答案，历史已落库答案仍是唯一效果事实。显式 hint 与最终 prepared seal 是两条命令，但只能产生一个 CandidateAnswer。没有新增公开 HTTP API。

## 正式回答完成确认（TURN-COMPLETION-UNDERSTANDING-011）

历史 011 的“按钮后立即停止 VAD/进入不可撤销处理中”已由 012 替代，不是当前候选端合同。turn/capture scope、服务端 final、合同校验和 ownership fence 继续有效；试音自动端点不变。

`UnderstandingProblem` 仅在特权领域记录中新增固定枚举 `reason_code` 和 `attempts`（1–2），共享事件和候选人投影不包含这些内部诊断。严重暂停 problem 不能被随后 `INTERVIEW_NOT_IN_PROGRESS` 等可重试提示覆盖。

本文定义目标业务流程的第一版 API 语义。实际实现可生成 OpenAPI，但不能改变这里的资源边界、候选人隐私和事件语义，除非同步更新本文档。

## 正式 Evidence 采集代次合同

`floor.changed` 的 open-ready 事件新增服务端生成的 `capture_id`（仅 transient）。候选端 `speech.started/stopped`、`continue_speaking` 与自动 seal 必须绑定 ready 的 capture ID 和 turn；旧/缺少 scope 的停止信号不调度端点。服务端计时器在创建、到期和执行命令时验证同一 scope，不在到期时改用新流的 turn。未开流的同题 barge-in 只打断播放，不充当新流已开始收音的事实。无有效转写的正式采集返回可恢复 `STT_TRANSCRIPT_UNAVAILABLE`/`continue_listening`，不产生虚构 utterance 或答案；新 open-ready 清除此提示。

自动 `evidence.seal` 的 `endpoint=semantic_timeout` 同时携带服务端创建的 `endpoint_id`。`speech.started`/continue 失效上一端点，后续 stop 获得新 ID，已排队的旧 seal 也只确认 no-op。journal 对 scope ID 使用长度和字符 allow-list；旧/缺 scope 自动命令不得按当前题目猜补。人工 explicit seal 的原始恢复 receipt 合同保持不变。后端、前端 bundle 应一起发布，旧浏览器需刷新以取得新 ready/scope 协议。

内部 `media_evidence`（`mode=sealed_segments`）统一携带 `capture_revision`，流式 final、断线 batch 与持久 checkpoint repair 使用同一合同。`transcription.started/failed` 和最终答案事务校验它与当前完整 stream/revision/segment/frame checkpoint 一致；不一致返回 `EVIDENCE_MEDIA_CHECKPOINT_CHANGED`，不得修改下一次采集的转写状态或提交答案。`utterance.not_accepted` 的既有事件 schema 不变，但其服务端事务同时归档未采纳采集并推进 revision，使随后同题 `evidence.open` 可重新收音。`EVIDENCE_MEDIA_ALREADY_COMPLETE` 对没有明确非答案结果的完整录音仍有效；不开放客户端重置完整录音的接口，也不接受客户端指定权威 capture revision。

## 通用约定

- Base path：`/api/v1`
- 后台 API 使用 Bearer token；公开邀请端先使用一次性 `invitation_token`，登记成功后换取短期 `candidate_session_token`。
- 所有后台资源隐含 `organization_id`，服务端从登录主体获取，不能信任客户端传入值。
- 时间使用 ISO 8601 UTC；预约额外保存展示时区。
- ID 使用不可猜测的 UUID/ULID。
- 导入、异步审阅、邀请、登记、候选人开始、答案提交和重试支持 `Idempotency-Key`。
- 所有修改聚合的请求携带 `expected_version`；陈旧写入返回 `409 PERSISTENCE_CONFLICT`。
- 交互式客户端对可能被后台模型任务推进 version 的资源使用统一 latest-version command：提交前 GET 最新资源，仅当配置 revision 或命令相关业务字段未改变时采用最新 version；读写间再次冲突最多重读一次。真实语义变化返回本地 `RESOURCE_SEMANTIC_CONFLICT` 并要求重新确认，不能按供应商或模型类型写特例。
- 明确建模为异步工作的接口（题库 import/rebuild/build、PDF 摄取等）返回 `202 Accepted` 和 `job_id`，通过工作项接口查询状态。
- 创建题目、题目语音重建和题库语音配置切换不得在 HTTP 请求内调用 TTS；它们只提交 DurableWorkItem 并由 `app/workers/` 中的 Celery task 执行。
- `GET /healthz` 只表示进程存活；`GET /readyz` 对数据库/Redis、生产密钥、OSS bucket 鉴权、扫描器和 LiveKit/Egress 执行只读探针；开发环境设置 `INTERVIEWER_LOCAL_MEDIA=true` 后也会真实探测本地 Egress 与 receive-only Evidence ingress。生产还要求 30 天内、HMAC 签名且精确绑定当前 deployment/release 的实时 Agent acceptance v2 report；任一未就绪返回 `503` 与不含凭据/候选人数据的逐项结果。预约邀请/start 和候选人 Agent ticket 同样校验媒体配置、报告与组织灰度名单，不能把 `/readyz` 当作可绕过的运维提示。
- 成功资源响应保持资源本身为顶层对象，避免为已有客户端引入破坏性的二次 `data` 包络；集合响应统一为 `{"items": [...], "next_cursor": null|string}`，即使当前实现尚未分页也保留 cursor 槽位；异步命令统一返回 `202`。二进制文件、音频和 CSV 导出不套 JSON 格式。
- JSON 输出统一经过 `app/transport/http/responses.py`；需要防止内部字段泄露的投影在 `app/transport/http/fields/` 声明 allow-list 并由 `marshal` 执行。字段声明只负责 transport 投影，不承载领域计算；服务层不能依赖 transport fields。`app/api/routes.py` 只总装 `app/api/routers/` 下按业务域拆分的 router，不放 response、module 构造或实时连接 implementation。

通用错误：

```json
{
  "error": {
    "code": "APPOINTMENT_NOT_READY",
    "message": "The appointment is not ready.",
    "details": {"failed_checks": ["stt_route"]}
  }
}
```

公开邀请、填报和匹配接口对“token 不存在、已过期、候选人不匹配”使用相同 HTTP 状态和通用消息，防止枚举候选人或预约。

请求体、路径和查询参数校验失败同样使用上述错误包络，固定为 `422 REQUEST_VALIDATION_FAILED`；`details.fields` 只返回 `location/message/type`，不回显请求原值。Provider、Persistence、认证与限流错误也跨同一 response seam，客户端不再兼容 FastAPI 默认 `detail` 形状。

## Web 工作台与候选人页面

- `GET /`：内置企业工作台。
- `GET /web/*`：静态资源。
- `GET /api/v1/auth/session`：使用现有后台 Bearer token 返回当前主体、组织和角色；不创建新的登录凭据。
- `POST /api/v1/auth/websocket-ticket`：使用现有后台 Bearer token 为指定面试签发最长 60 秒、一次用途的浏览器 WebSocket ticket。原有 WebSocket `Authorization` header 认证继续兼容，浏览器工作台使用 ticket query，避免把长期 Bearer token 放入 URL。
- `/#positions`、`/#knowledge-bases`、`/#candidates`、`/#plans`、`/#appointments`、`/#interviews`：后台工作区。
- `/#invite/{invitation_token}`：公开邀请、姓名/邮箱/手机号填报、授权和设备检查。
- start 响应首次导航可在 URL fragment 中携带 `candidate_session_token`；前端立即转存到 `sessionStorage` 并用 `history.replaceState` 清除 fragment 中的 token，随后进入 `/#candidate/{interview_id}`。
- 候选人 HTTP 请求通过 `X-Candidate-Session-Token` header 调用 public 窄接口；候选人必须先用它换取 60 秒、一次性 Agent ticket，WebSocket URL 只放 ticket，不放 candidate session token。后台 Bearer API 不接受候选人 token。
- 后台工作台只按当前角色和当前路由加载允许访问的资源；`interviewer/reviewer` 不得因为无权读取 `/admin/*` 而导致整个工作台加载失败。

当前代码已实现本文主流程中的岗位/题库批量 import/rebuild/build 查询、题目更新/归档/语音重建、候选人 PATCH、PDF multipart 与 HTTPS URL 摄取、简历版本/短期访问、经历题审核与语音重建、候选人专属计划、预约 PATCH/邀请、公开填报、readiness、self-start、服务端 streaming/batch STT、企业复核、签名音频、重评和 JSON/CSV 报告导出接口。旧 JSON `resume_text` 上传已删除，非 multipart 请求返回 `415 RESUME_MULTIPART_REQUIRED`。持久 Outbox 提供状态/指标、指数退避、dead-letter 与人工重放；题库导入、重建和 PDF 摄取默认返回 `202`。

后台 HTTP 在生产使用 Bearer RBAC，`admin/interviewer/reviewer` 权限按路由分离；成功、失败和未认证请求均写元数据审计，URL 中 invitation/file/media token 会先替换为 `{token}`。签名文件和媒体只有分钟级有效期，实际媒体下载另记审计。外部环境验收边界见 [开发进度](development-progress.md)：真实云服务未提供时不能把对应 adapter 标为生产健康。

公开 invitation/intake/readiness/start、候选人会话和签名文件访问按客户端与操作组限流；开发使用进程内时间窗，生产必须配置 `INTERVIEWER_REDIS_URL`，Redis 不可用时返回通用 `503 RATE_LIMITER_UNAVAILABLE`，超过阈值返回 `429 RATE_LIMIT_EXCEEDED`。两者都审计脱敏 route，不暴露 token 是否存在。

深度审查登记的六个接口/领域语义差距已在仓库范围关闭：公开题库搜索统一为有 scope 的 Question Catalog；计划运行时只接受 execution v2 canonical slots；Candidate Intake 验证明示同意和服务端告知版本；公开 start 校验时间窗及持久设备 readiness；报告所有字段只读取当前评分 revision；候选人只读取 token 保护的 allow-list 投影。迁移和验证证据统一记录在 [已知问题与修复设计](known-issues-and-remediation.md)。

### 已知问题修复合同（已验证）

| ID | 接口保持/调整 | 完成条件 |
| --- | --- | --- |
| `PLAN-001` | `PATCH /interview-plans/{plan_id}` 只编辑 canonical 槽位、候选池、经历问题和策略；schema 拒绝 `items` | 预约 start 执行批准的 execution v2 revision，审批重新计算完整 readiness |
| `CONSENT-001` | intake 使用 `consent.accepted/version/audio_recording/video_recording`；服务端校验允许版本、预约必需 scope，并记录告知 hash 和服务端时间 | 隐私、音频或视频 scope 缺失分别失败关闭；无同意的视频 participant grant 不含 camera，网络验收必须证明视频上行字节为 0 |
| `APPOINTMENT-001` | readiness 形成带有效期的设备事实；start 通过统一 admission command 校验 token、登记、同意、时间窗和全部 readiness | 窗口前返回 `APPOINTMENT_TOO_EARLY`，窗口后返回 `APPOINTMENT_WINDOW_CLOSED`；并发/重复 start 只返回一个会话 |
| `REPORT-001` | 报告响应结构不变，`evaluation_ids` 明确等于生成时各答案的当前评分指针集合 | 分数、证据、风险和 `manual_review` 只使用这些 current revisions |
| `SEARCH-001` | `/questions/search` 固定为有岗位/题库 scope 的关键词和结构化查询；相似题只能使用显式治理 purpose | 公开搜索与计划候选池跨同一个 Question Catalog seam，未配置 embedding 时仍可用 |
| `CANDIDATE-ACCESS-001` | `/public/interviews/{id}` 系列只接受候选人 token header 并返回 allow-list 投影 | 不返回标准答案、rubric、候选池、未来题干或 token；媒体必须属于当前会话/轮次 |

## 岗位与岗位题库 API

### 岗位

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions` | 创建岗位；工作台同时提交 `initial_requirement`，服务端在同一事务创建首版岗位要求 |
| `GET` | `/api/v1/job-positions` | 列出岗位 |
| `GET` | `/api/v1/job-positions/{position_id}` | 获取岗位及题库摘要 |
| `PATCH` | `/api/v1/job-positions/{position_id}` | 修改或归档岗位 |
| `GET` | `/api/v1/job-positions/{position_id}/deletion-impact` | 预览删除将影响的候选人、岗位要求、计划和预约数量 |
| `DELETE` | `/api/v1/job-positions/{position_id}` | 携带 `expected_version` 和与岗位名称完全一致的 `confirmation` 删除岗位 |

岗位删除必须先向操作者展示 deletion-impact 并进行名称精确确认。命令清除该岗位候选人的联系方式、简历、录音、转写、评分和报告敏感内容，归档相关岗位要求/计划、取消预约，并保留不可识别的历史面试和审计占位。组织共享的 KnowledgeBase、Question、语音配置和语音资产不随岗位删除。

新建岗位请求的 `initial_requirement` 包含 `title/description/must_have_skills/nice_to_have_skills/seniority/interview_duration_minutes`。工作台必须提交该对象；服务端先完成全部字段校验和技能规范化，再在一个租户事务内写入 JobPosition 与首版 RoleRequirement。成功响应保持岗位字段，并增加 `initial_role_requirement`；任一步失败都不得留下没有首版要求的岗位。低层 API 客户端仍可省略该字段以兼容已有集成，但这条兼容路径不用于新工作台。

创建示例：

```json
{
  "code": "backend-senior",
  "name": "资深后端工程师",
  "department": "研发中心",
  "description": "负责高并发服务和线上稳定性",
  "default_duration_minutes": 45
}
```

### 岗位题库

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions/{position_id}/knowledge-bases` | 为岗位创建题库 |
| `POST` | `/api/v1/job-positions/{position_id}/knowledge-base-assignments` | 把组织内已有题库关联到岗位；请求携带 `knowledge_base_id + expected_position_version`，不复制题目或语音配置 |
| `GET` | `/api/v1/job-positions/{position_id}/knowledge-bases` | 列出岗位题库 |
| `GET` | `/api/v1/knowledge-bases` | 列出当前组织全部题库摘要；支持岗位、状态过滤，返回题目/语音计数和当前 TTS/声音摘要 |
| `GET` | `/api/v1/workspace/question-catalog` | 后台一次读取岗位、题库与题目集合，供路由级工作台加载；不替代既有资源接口 |
| `GET` | `/api/v1/workspace/question-overview` | 总览页只读取题目计数与最近 5 项，避免首屏下载完整题库 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 查看题库、构建状态和计数 |
| `PATCH` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 修改名称、说明或归档；语音配置使用独立命令接口 |
| `PUT` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-profile` | 以 `expected_version + expected_speech_profile_revision` 设置 TTS 模型、声音和输出参数；变化时创建新 revision 并返回整库重建 job。profile revision 未变时，服务端可吸收后台进度造成的任意 KnowledgeBase version 推进；profile 真正被并发修改时仍返回 409。新 revision 与旧 revision 未完成工作的协作取消在同一事务提交 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-options` | `items` 返回可选择的已就绪 TTS 模型；`candidates` 同时返回已添加但未测试/失败/停用的 TTS 及不可选原因和归一化声音目录，不包含 Provider 凭据 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds` | 列出语音构建历史、当前进度和失败计数 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds/{job_id}` | 查询一次整库语音构建和题目级失败摘要 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds/{job_id}/retry-failed` | 以 `expected_version + Idempotency-Key` 只重试当前 profile revision 和该 build 冻结清单下当前仍为 `speech_status=failed` 的题目；按题目当前 version 创建新工作，不复用旧 dead-letter，也可恢复旧版 worker 误写为 completed/superseded 的失败子工作 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/imports` | 上传或结构化导入题目 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/rebuild` | 重建索引和缺失读题语音 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/builds/{job_id}` | 查询构建工作项 |

SpeechBuild 的 `failed` 计数和 `failed_items` 只包含已经进入 `dead_letter` 的题目子工作；仍可由 Worker 自动领取的 DurableWorkItem `status=failed` 对 API 投影为 `pending/running`。因此页面只对真正终态失败显示人工重试，自动重试期间保留已完成题目的试听能力。

导入使用 multipart 文件或 JSON items。响应立即返回：

```json
{
  "job_id": "job_kb_01J...",
  "knowledge_base_id": "kb_backend_cn",
  "status": "queued",
  "tasks": ["parse", "validate_candidate_pool", "question_speech"]
}
```

新建题库可以不指定语音配置。若组织存在 enabled 的 `tts.synthesize + question_speech_generation` route，且 primary ModelConfiguration/连接均 enabled、模型为 `ready`，服务端解析模型 `default_voice` 并保存 revision 1 的明确 speech profile（`source=model_route_default + model_route_id`）；新题随后直接按该冻结配置异步生成。没有有效默认路由时返回 `speech_build_status=configuration_required`，前端引导进入题库详情选择 TTS 模型和声音。route 后续变化不自动改写已创建题库，避免静默重建和费用。

招聘流程不在岗位卡片内创建或重新配置题库，而是调用 assignment 接口从题库列表选择。关联后的岗位直接复用题库现有 Question、KnowledgeBaseSpeechProfile、声音和不可变语音资产；重复关联为幂等成功，未关联题库不能进入该岗位的搜索或计划。

构建工作项依次校验题目、标准答案、关键点、rubric、技能、难度和题型，写入结构化候选池，并按题库的 KnowledgeBaseSpeechProfile 异步生成每道活动题的 `QuestionSpeechAsset`。部分失败时题库保持 `building` 或进入 `failed`，响应必须列出失败题目和重试入口；不能把缺少评分依据或匹配当前 profile revision 语音的题库标为 `ready`。MVP 不生成 embedding，也不依赖向量数据库。

设置语音配置请求：

```json
{
  "expected_version": 4,
  "model_configuration_id": "model_cfg_tts_01J...",
  "voice_profile_id": "tongtong",
  "language": "zh-CN",
  "audio_format": "audio/wav",
  "speaking_rate": 1.0
}
```

服务端只接受已启用、状态为 `ready` 且支持 `tts.synthesize` 的 ModelConfiguration，并校验声音属于该模型的 voice catalog。模型、声音、语言、格式或语速与当前 profile 不同则原子增加 `speech_profile.revision`、把题库语音 readiness 置为 rebuilding，并创建 `knowledge_base.speech.rebuild` DurableWorkItem；完全相同的配置和 `Idempotency-Key` 返回已有 job，不重复计费。响应为 `202`：

```json
{
  "knowledge_base_id": "kb_backend_cn",
  "speech_profile_revision": 3,
  "job_id": "work_kb_speech_01J...",
  "status": "pending",
  "question_count": 42
}
```

父工作项在 Celery worker 中冻结活动 Question ID/version 清单并分批创建题目级工作项，进度投影至少包含 `total/pending/running/ready/failed/superseded`。切换到 revision 4 后，revision 3 的迟到结果不得成为当前资产。已批准计划或历史会话引用的旧 QuestionSpeechAsset 不删除、不覆盖。

TTS 响应进入 FileObject 前必须经过共享私有音频导入合同。`audio/wav` 要求 RIFF/WAVE、唯一且完整的 fmt/data 结构和 PCM block alignment；已识别的 signed-limit RIFF/data 流式占位组合可在最终 data 直达 HTTP EOF 时安全收口，完整 chunk walker 证明正文无缺失后也可纠正恰好漏计四字节 WAVE form type 的 outer size，随后按规范化字节重算 checksum。其他截断、任意长度偏差或畸形 WAV 返回结构化 `TTS_ASSET_FORMAT_INVALID`，不得产生 ready QuestionSpeechAsset/AgentExpressionAudio。既有冻结资产不被该导入命令原地改写。

KnowledgeBase 的 version 也会因后台构建状态推进而变化。配置页面不能长期复用打开弹窗时的 version：提交前先读取最新 KnowledgeBase；若最新 speech profile 的 revision、模型配置 ID/version、声音、语言、格式和语速与打开时一致，可用最新 version 提交；若这些字段已经变化，必须刷新并要求用户重新确认。提交与读取之间的极窄竞态最多按同一规则重读并重试一次，不能无条件覆盖他人的语音配置。

### 题目

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/questions` | 创建题目并排队校验/语音；返回 `202` 和工作项，不等待 TTS |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/questions` | 列出题目 |
| `PATCH` | `/api/v1/questions/{question_id}` | 修改题目；内容变化产生新版本和新语音任务 |
| `DELETE` | `/api/v1/questions/{question_id}?expected_version={version}` | 从当前题库归档题目；保留历史面试快照和不可变语音资产，不再出现在活动题列表 |
| `POST` | `/api/v1/questions/{question_id}/speech/regenerate` | 以 `expected_version + Idempotency-Key` 按题库当前 speech profile 重建单题语音；重复命令返回同一工作，响应 `202`，不在请求内执行 TTS |
| `POST` | `/api/v1/question-speech-assets/{asset_id}/content-url` | 鉴权并审计后签发五分钟题目语音试听地址；浏览器不接触存储凭据 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-options` | 返回题库定位/标签默认值和可用于结构化生题的 ready LLM 模型，不包含凭据 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches` | 创建智能生题批次并返回 `202 + work_item_id`；请求包含模型、数量、定位、标签和可选要求，HTTP 不执行 LLM |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches` | 列出该题库最近生成批次和状态 |
| `GET` | `/api/v1/question-generation-batches/{batch_id}` | 查看批次上下文、候选草稿、失败或导入结果 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/stop` | 以 `expected_version + Idempotency-Key` 请求停止；取消未执行工作并让在途结果在提交前失效 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/resume` | 以 `expected_version + Idempotency-Key` 继续 stopped 批次的未完成槽位，保留已完成分片 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/retry-failed` | 以 `expected_version + Idempotency-Key` 重试规划、合并或全部失败分片，不重做成功分片 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/chunks/{chunk_id}/retry` | 以 `expected_version + Idempotency-Key` 只重试一个失败分片 |
| `PATCH` | `/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}` | 以批次 `expected_version` 修改一条候选草稿并重新校验评分依据 |
| `DELETE` | `/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}?expected_version={version}` | 从审核批次删除候选草稿，不影响正式题库 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}/import` | 以 `expected_draft_version? + Idempotency-Key` 异步导入单个候选题；兼容接收 `expected_version`，但其他草稿推进批次版本不阻塞本题；批次继续处于审核态 |
| `POST` | `/api/v1/question-generation-batches/{batch_id}/import` | 以 `expected_version + Idempotency-Key` 冻结剩余草稿并异步批量导入正式题库 |
| `POST` | `/api/v1/questions/search` | 后台按关键词和结构化字段查题，不用于实时抽题 |

题目读取投影增加 `speech_preview={available,reason,message}`。只有 ready QuestionSpeechAsset、
`production_ready=true` 且关联 ready FileObject 时 `available=true`；开发 mock 返回
`reason=development_mock_asset` 和“配置语音”操作提示，不再把 `mock-tts://` 展示为可试听音频。直接请求旧 mock 资产
返回 `409 QUESTION_SPEECH_PREVIEW_UNAVAILABLE`；真实 Provider 资产缺少私有文件时继续返回
`409 QUESTION_SPEECH_ASSET_NOT_PRIVATE`，但消息明确提示重新生成或检查私有存储。

智能生题创建体为 `model_configuration_id + target_count(1..30) + positioning + tags + requirements?`，
必须携带 `Idempotency-Key`。只有 `enabled + ready + llm.chat_json` 模型可选。PATCH 草稿允许修改题干、答案、
关键点、技能、难度和题型；服务端会重新校验非空答案、关键点和技能。导入接口只接受 `reviewing` 批次。
单题导入是草稿级条件命令：服务端校验目标草稿的 `expected_draft_version`（旧客户端可只传批次
`expected_version`），事务写入使用读取到的最新批次版本，因此另一个草稿刚提交/完成导入不会制造无关的
`PERSISTENCE_CONFLICT`。该草稿随后标记为 `importing -> imported/failed`，成功后不能再次编辑、删除或导入；其他草稿仍可审核，
批量导入只处理尚未导入的草稿，并拒绝与在途单题导入并发，
生成状态依次为 `queued -> generating -> reviewing`，其中 `phase` 进一步公开
`planning/generating/merging/refilling`；响应的 `generation_progress` 提供规划数、子任务完成数、已接受/过滤题数和
补生成轮次。任务控制增加 `generating/queued -> stopping -> stopped -> generating`；停止不能承诺撤销已经到达
供应商的 HTTP 请求，但会停止新调用并通过 `execution_revision` 丢弃停止前的迟到结果。批次详情返回 `tasks`、
`available_actions`、`control_history` 和结构化错误投影，客户端无需、也不能直接调度或 replay DurableWorkItem。
若双槽位生成以 `provider_output_truncated` 失败，服务端会把原 chunk 投影为 `superseded`，在其 `recovery` 中返回
`strategy/reason/replacement_chunk_ids` 与不含正文的 token/finish_reason 诊断，并创建两个单槽位替代任务；
`generation_progress.total_chunks` 只统计活动替代任务。该恢复不改变批次 ID、不重做成功分片，也不要求客户端发起
重试。单槽位仍截断时批次进入 `failed`，错误标记为不可自动重试，但既有显式分片/失败重试命令仍可由面试官执行。
导入状态为 `importing -> imported`；正式题目 ID 返回在
`imported_question_ids`。生成或导入失败时批次保留 `last_error` 和关联工作项，AI 输出永远不会因为读取接口或轮询
而自动入库。既有创建、查询、草稿审核与导入路径保持兼容。

创建题目示例：

```json
{
  "title": "Python GIL",
  "question_text": "请解释 GIL 对 CPU 密集型多线程程序的影响。",
  "standard_answer": "GIL 限制同一进程内线程同时执行 Python 字节码……",
  "key_points": [
    {"text": "解释字节码并行限制", "weight": 0.4},
    {"text": "区分 CPU 与 I/O 密集场景", "weight": 0.3}
  ],
  "skills": ["python", "concurrency"],
  "difficulty": "senior",
  "type": "open_ended",
  "rubric": {"semantic_weight": 0.5, "key_point_weight": 0.5}
}
```

后台搜索请求必须明确岗位和题库范围。`query` 使用关系库全文/关键词搜索；技能、难度和题型使用结构化过滤，不做向量召回：

```json
{
  "job_position_id": "pos_backend",
  "knowledge_base_ids": ["kb_backend_cn"],
  "query": "Python 并发与性能",
  "filters": {"difficulty": ["mid", "senior"]},
  "limit": 20,
  "include_answer": true
}
```

服务端验证所有题库属于当前组织并已显式关联到 `job_position_id`；查询底层在关联校验后强制过滤 `organization_id + knowledge_base_ids + active + valid + speech-ready` 及技能/难度/题型条件。实时 Question Selection 不调用该搜索接口，而是直接使用批准计划冻结的题目 ID/version 候选清单。

## 简历库与 AI 审阅 API

### 候选人记录和简历

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/candidate-profiles` | 企业创建候选人基本信息；新工作台同时提交 `job_position_id` 建立岗位候选关系 |
| `GET` | `/api/v1/candidate-profiles` | 搜索组织内简历库；返回最新岗位初筛投影、人工复核结果和清理期限 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}` | 查看候选人及最新岗位初筛投影 |
| `PATCH` | `/api/v1/candidate-profiles/{candidate_id}` | 更新基本信息或归档 |
| `DELETE` | `/api/v1/candidate-profiles/{candidate_id}?expected_version={version}` | 乐观并发地逻辑归档候选人并从活动列表隐藏，保留审计事实 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/experience-questions` | 读取候选人简历问答；只返回生效结论符合且证据绑定有效的非归档项 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/experience-questions` | 人工创建绑定候选人、符合的 ResumeReview 和 1–3 个简历证据标签的问题；初始为草稿 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 以 multipart 上传本地 PDF，创建新简历版本 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes/import-url` | 从公开 HTTPS URL 异步导入 PDF，创建新简历版本 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes` | 列出简历版本和摄取状态 |
| `GET` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}` | 查看解析状态和授权元数据 |
| `PATCH` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}` | 以 `expected_version + display_name` 修改展示文件名；不覆盖 PDF 内容 |
| `DELETE` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}?expected_version={version}` | 删除未被计划/面试历史引用的简历版本，同时取消待处理工作并清理私有文件 |
| `POST` | `/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}/content-url` | 鉴权并审计后获取短期下载入口 |
| `GET` | `/api/v1/file-ingestion-jobs/{job_id}` | 查看 PDF 摄取工作项、失败码和简历状态 |

创建示例：

```json
{
  "name": "张三",
  "email": "candidate@example.com",
  "phone": "+8613812345678",
  "external_ref": "ats-2048"
}
```

服务端规范化并加密邮箱和手机号；响应默认只返回脱敏值。简历只接受 PDF，限制类型和大小并做恶意文件扫描，原文件存 Private File Storage 而非数据库 BLOB。调用方不能提供可信 `storage_uri`；服务端只保存 `file_object_id` 和受控元数据。

简历的“改”只作用于展示文件名，且仍须以 `.pdf` 结尾；替换 PDF 必须再次调用创建接口形成递增的 `resume_version`，不能原地改写证据来源。删除使用乐观并发：待领取的 `resume.ingest/resume.review` 工作项转为 `cancelled`，隔离文件、PDF、解析文本和未进入历史的派生资产被清理，资源留下最小 `deleted` 审计占位并从列表隐藏。工作项已经运行时返回 `409 RESUME_DOCUMENT_PROCESSING`；已被 InterviewPlan 或 InterviewSession 快照引用时返回 `409 RESUME_DOCUMENT_IN_USE`，防止破坏历史证据。

候选人列表中的 `screening` 是最新 ResumeReview 的岗位维度投影：`ai_recommendation` 保留模型原建议，`effective_outcome` 优先采用人工复核结论，`matched_requirements/unmet_requirements` 用于解释入选或淘汰。该投影是辅助建议，不是自动录用决定。

本地文件上传使用 `multipart/form-data`：

```text
file=<candidate.pdf>
display_name=张三-后端工程师简历.pdf  # 可选
job_position_id=pos_backend           # 可选；与下一字段同时提交时，摄取成功后自动排队初筛
role_requirement_id=role_01J...       # 可选
```

服务端分块读取并在 10 MiB 默认硬上限内拒绝超限输入，随后计算 SHA-256；隔离文件写入 worker 管理的私有目录。响应 `202 Accepted`：

```json
{
  "resume_document_id": "resume_01J...",
  "ingestion_job_id": "job_resume_ingest_01J...",
  "source_type": "local_upload",
  "ingestion_status": "queued"
}
```

URL 导入请求：

```json
{
  "url": "https://files.example.com/resumes/candidate.pdf",
  "display_name": "候选人简历.pdf",
  "job_position_id": "pos_backend",
  "role_requirement_id": "role_01J..."
}
```

URL 导入也返回 `202`，`source_type=url_import`。下载由 worker 使用受控 HTTP 客户端执行：生产默认只允许 HTTPS，限制连接/总超时、重定向次数和最大字节数；初始 URL 及每次重定向都要重新解析 DNS，并拒绝环回、私网、链路本地、保留地址、云元数据地址、非 HTTP(S) scheme 和 URL 内嵌凭证。MVP 不接收需要 Cookie、Authorization 或企业内网访问的 URL。

两种入口随后执行相同流水线：`receive/download -> quarantine -> PDF signature/MIME validation -> malware scan -> private store -> page-preserving parse -> ready`。同时提交岗位和要求时，摄取完成的同一事务创建 `ResumeReview + resume.review` 工作项，浏览器不轮询等待 LLM。外部 URL 只作为摄取来源；相同上传 `Idempotency-Key` 的重试返回同一个 `ResumeDocument`。不同上传命令即使 PDF 内容相同也形成不同不可变简历版本，其审阅工作幂等键按 `resume_document_id + input_hash` 隔离，不能复用另一个版本已完成或已删除的工作项。

JSON `resume_text` 兼容请求已删除；开发和生产均以系统托管 PDF 为唯一简历文件真相。

### 异步简历审阅与经历问题

`POST /api/v1/candidate-profiles/{candidate_id}/resume-reviews`

```json
{
  "resume_document_id": "resume_01J...",
  "job_position_id": "pos_backend",
  "role_requirement_id": "role_01J...",
  "experience_question_count": 3
}
```

响应 `202`：

```json
{
  "review": {"id": "rr_01J...", "status": "queued"},
  "job": {"id": "job_resume_01J...", "status": "pending", "kind": "resume.review"}
}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/resume-reviews/{review_id}` | 返回岗位初筛、项目/技能证据、告警和问题状态 |
| `POST` | `/api/v1/resume-reviews/{review_id}/retry` | 以 `expected_version + reason + Idempotency-Key` 将 failed/dead-letter 初筛原子恢复为 queued/pending，并记录操作者审计；相同命令在 version 校验前返回原 replay 结果 |
| `PATCH` | `/api/v1/resume-reviews/{review_id}/screening-review` | 人工复核初筛；提交 `expected_version`、`decision=qualified|unqualified` 和可选说明，保留 AI 原建议并审计 |
| `GET` | `/api/v1/resume-reviews/{review_id}/experience-questions` | 仅在生效结论符合时列出证据绑定有效的问题；否则返回空集合 |
| `PATCH` | `/api/v1/experience-questions/{question_id}` | 人工编辑、批准或拒绝 |
| `DELETE` | `/api/v1/experience-questions/{question_id}?expected_version={version}` | 从候选人个人题库归档；保留已冻结计划和历史面试快照 |
| `POST` | `/api/v1/experience-questions/{question_id}/speech/regenerate` | 重试引用该题的预约级 failed/dead-letter 语音工作；未确认预约时不提前生成 |

创建与重试接口只验证并排队，绝不在 HTTP 请求内等待 LLM。同一 ready 简历的重复创建命令返回原 ResumeReview；若该审阅仍为 queued 但工作项缺失，命令会在同一事务补建指向当前审阅的 DurableWorkItem，并校验返回工作的 aggregate ID，避免界面永久显示排队。失败重试仅接受 `ResumeReview.status=failed` 且关联 DurableWorkItem 为 `failed/dead_letter` 的组合；源 ResumeDocument 必须仍为 `ready`，岗位和要求必须存在。命令使用审阅 version 防双击/并发覆盖，清空当前错误与分块进度、把工作 attempt 归零并增加 `replay_count`，但不创建第二份审阅。`GET` 在处理中返回 `status/processing_stage/processing_strategy/processing_progress`；完成后通过 `screening.recommendation/score/summary/matched_requirements/unmet_requirements` 给出可解释建议，证据包含 `source_pages`。服务端按 `candidate_screening_score.v1` 强制把 0–59 映射为 `unqualified`、60–74 映射为 `manual_review`、75–100 映射为 `qualified`，并返回 `screening_policy_version`；候选人列表的嵌套 screening 投影还返回 `question_generation_status/error/count`。模型建议与分数冲突时以分数带为准，人工 `screening-review` 决定仍可覆盖生效结论。只有生效结论为 `qualified` 时才排入独立的 `resume.experience_questions.generate` 工作；AI 不符合/待复核不生成，人工改判符合时才临时排队。AI 问题默认为 `draft`，批准后只变为可入计划的 `deferred`，不触发 TTS。不得把简历中的受保护属性或无关个人信息发送给模型。

候选人个人题库不新建第二套题目实体，而是按 `candidate_profile_id` 汇总 ExperienceQuestion。AI 生成项记录
`source_type=ai_generated` 和来源 ResumeReview；人工创建项记录 `source_type=manual`，并必须选择属于同一候选人且已完成的
ResumeReview，使岗位、简历版本和证据上下文可追溯。人工创建请求至少包含非空 `question_text`、`standard_answer`、
`key_points` 和 1–3 个 `evidence_refs` 标签；标签必须来自该审阅的项目/技能证据，且题干必须明确包含至少一个所选标签。服务端把标签解析为含证据文本和来源页的不可变快照。初始状态固定为 `draft`。编辑仍使用 `expected_version`，状态只允许
`draft/approved/rejected`；批准后写入 `speech_status=deferred` 并可进入新计划，但不创建 TTS 工作。DELETE 使用归档语义，已归档项不再出现在个人题库、
审阅问题列表或新计划中，但已批准计划和历史 InterviewQuestionSnapshot 保持不变。生效结论改为不符合后，读取、创建、编辑、语音生成和新计划组卷全部失败关闭；历史上没有有效证据快照或题干未点名证据的题也从活动读取与新计划中过滤。

同一候选人按岗位只取最新审阅决定留存：只要存在 `qualified` 或 `manual_review`/处理中结论，就不设置初筛清理期限；所有最新岗位结论均为 `unqualified` 时设置 `retention_reason=screening_unqualified` 和服务端时间加 7 天。Celery Beat 周期任务先按当前分数带校正存量候选人的期限，首次命中从该次运行起重新给足 7 天，再由 RetentionService 清除到期私有简历和敏感投影并写审计；列表读取或页面点击不产生隐式写入或物理删除。

## 岗位要求与面试计划 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/job-positions/{position_id}/role-requirements` | 创建岗位要求版本 |
| `GET` | `/api/v1/job-positions/{position_id}/role-requirements` | 列出岗位要求 |
| `POST` | `/api/v1/interview-plans/generate` | 生成候选人专属计划；默认草稿，`approve=true` 时原子校验并批准 |
| `GET` | `/api/v1/interview-plans` | 列出计划 |

React 岗位卡片根据该岗位是否已有要求，显示“添加岗位要求”或“新增要求版本”，因此升级前已有但尚无要求的岗位无需删除重建。新建岗位路径仍优先使用原子 `initial_requirement` 合同。
| `GET` | `/api/v1/interview-plans/{plan_id}` | 查看槽位、经历问题和就绪状态 |
| `PATCH` | `/api/v1/interview-plans/{plan_id}` | 编辑草稿或批准/归档 |

生成计划：

```json
{
  "job_position_id": "pos_backend",
  "knowledge_base_ids": ["kb_backend_cn"],
  "role_requirement_id": "role_01J...",
  "candidate_profile_id": "cand_01J...",
  "resume_review_id": "rr_01J...",
  "approve": true,
  "position_question_count": 6,
  "experience_question_ids": ["eq_01J...", "eq_01K..."],
  "strategy": {
    "coverage": ["python", "database", "system_design"],
    "difficulty_curve": true,
    "max_same_skill_questions": 2,
    "deduplication_threshold": 0.72,
    "randomization": "seeded_per_session"
  }
}
```

`approve` 默认为 `false`，保留 API 客户端“生成草稿—编辑—批准”的完整流程。React 工作台在面试官点击“生成并启用计划”时显式提交 `approve=true`；Plan Assembly 必须在同一事务里完成装配、readiness 校验和批准，成功响应直接为 `approved`。校验失败时不得留下需要创建人再处理的半成品草稿。

响应中的 `bank_slots` 只定义维度、难度、题型、权重和时长；计划装配通过关系库字段形成每个槽位的 `QuestionCandidatePool`，批准时冻结筛选条件、题目 ID/version 清单及哈希，岗位题由面试中的 Question Selection 在清单内随机选择。`experience_questions` 是经人工批准的固定问题，并在所有 `position_bank` 槽位之后执行。两者连同权重、阶段顺序和 `selection_policy` 构成唯一 execution v2 计划；请求、响应和持久运行时均不再包含 `items`。升级旧数据前使用 `python -m app.migrations.plan_execution_v2 --dry-run` 检查，再执行正式迁移。此过程不要求 embedding 或向量数据库。

批准计划前服务端必须验证：

- 岗位、题库、岗位要求、候选人和审阅同组织且关系一致。
- 题库为 `ready`，候选池足够并已冻结题目 ID/version 与集合哈希。
- 所选题库的 speech profile 输出参数完全相同；计划冻结唯一 `speech_profile_snapshot`，包含 TTS ModelConfiguration ID/version、音色、语言、格式、语速和指纹。题库后续切换模型/声音不改写已批准计划。
- Resume Review 为 `ready`，经历问题为 `approved` 且证据有效；其计划快照不绑定全局语音资产，状态为 `deferred`。
- 权重和为 1，时长守恒；放宽去重或覆盖约束必须写入 `assembly_summary.warnings`。

已批准计划不可编辑，只能归档或复制为新草稿。

## 预约、邀请与候选人填报 API

### 企业预约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/interview-appointments` | 创建预约草稿 |
| `GET` | `/api/v1/interview-appointments` | 列出预约 |
| `GET` | `/api/v1/interview-appointments/{appointment_id}` | 查看预约和 readiness |
| `PATCH` | `/api/v1/interview-appointments/{appointment_id}` | 修改尚未邀请的预约 |
| `POST` | `/api/v1/interview-appointments/{appointment_id}/invite` | 通过 readiness gate 并签发邀请 |
| `POST` | `/api/v1/interview-appointments/{appointment_id}/cancel` | 撤销邀请并取消预约 |

创建示例：

```json
{
  "plan_id": "plan_01J...",
  "candidate_profile_id": "cand_01J...",
  "job_position_id": "pos_backend",
  "scheduled_start_at": "2026-09-01T02:00:00Z",
  "scheduled_end_at": "2026-09-01T03:00:00Z",
  "settings": {
    "record_audio": true,
    "record_video": false,
    "avatar_mode": "local",
    "speech_dialogue_mode": "cascade",
    "avatar_id": "avatar_default_cn",
    "voice_profile_id": "voice_cn_01",
    "language": "zh-CN"
  },
  "admission_policy": {
    "early_start_grace_seconds": 0,
    "late_start_grace_seconds": 0,
    "device_readiness_ttl_seconds": 300,
    "consent_version": "v1"
  }
}
```

`settings.avatar_mode` 只接受 `local | cloud`。新预约省略时默认 `local`；`local` 复用计划冻结的 `QuestionSpeechAsset` 并由候选人浏览器渲染形象，`cloud` 调用已配置的 `avatar.speak/interview_question_delivery` route。已持久化但没有该字段的历史预约/会话按 `cloud` 解释，避免升级后改变旧场次。`PATCH` 只合并显式提供的 settings 字段。

`settings.speech_dialogue_mode` 只接受 `cascade | s2s`，默认 `cascade`。`cascade` 保留 `STT -> 受控追问策略 -> Avatar/TTS`；`s2s` 让同一 PCM 并行进入 `speech.dialogue_realtime/candidate_followup_dialogue`，但原始音频只在有界内存缓冲，Provider final 必须与已冻结动作逐字一致才会私有落盘并表达。S2S route 不可用或文本不一致时丢弃原始 delta 并回到批准文本的 cascade，不能影响 CandidateAnswer 或评分。

预约的 `settings.voice_profile_id/language` 由计划冻结的 `speech_profile_snapshot` 派生。创建或修改时显式提交另一音色返回 `409 APPOINTMENT_SPEECH_PROFILE_MISMATCH`，不能让简历题和岗位题出现两套声音。

邀请响应只在签发时返回一次明文 URL：

```json
{
  "appointment": {"id": "appointment_01J...", "status": "invited"},
  "invitation_token": "opaque_token_returned_once",
  "join_url": "/#invite/opaque_token_returned_once"
}
```

React 工作台必须在这个一次性响应弹窗中提供“复制链接”操作和成功/失败反馈，不要求用户手工选中 URL。

数据库只保存 token 哈希。邀请阶段的 `can_invite` 检查计划批准、题库/候选池、岗位题语音和可执行的冻结 speech profile，不等待尚未触发的简历题 TTS；候选人 start 的 `can_start` 还要求本预约全部简历题语音资产已 ready、来源版本与 profile 精确匹配，并继续检查服务端 STT、时间窗和录音留存策略。

### 公开邀请与填报

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/public/interview-invitations/{token}` | 返回岗位名、时间、告知版本和所需字段的安全摘要 |
| `POST` | `/api/v1/public/interview-invitations/{token}/intake` | 提交姓名、邮箱、手机号与授权，匹配成功后确认预约并安排邮件提醒 |
| `POST` | `/api/v1/public/interview-invitations/{token}/readiness` | 上报浏览器、麦克风和音频格式检查 |
| `POST` | `/api/v1/public/interview-invitations/{token}/start` | 候选人在时间窗内幂等创建/启动会话 |

填报请求：

```json
{
  "name": "张三",
  "email": "candidate@example.com",
  "phone": "+8613812345678",
  "consent": {
    "accepted": true,
    "version": "v1",
    "audio_recording": true,
    "video_recording": false
  }
}
```

姓名、邮箱和手机号三项均为必填。GET 邀请响应返回服务端冻结的隐私、音频录制和视频录制告知正文、允许版本、`required_consent_scopes` 和内容 hash；客户端只能回传同一版本和明确的 `audio_recording/video_recording` 布尔值。音频与视频不得合并成一个“录制已同意”字段；重复 intake 不能将已核验 scope 降级。服务端只与预约绑定的 `CandidateProfile` 比较；至少邮箱或手机号之一精确匹配，姓名联合校验。成功后预约进入 `registered`，同一事务创建提醒与经历题语音工作，响应不返回简历、题目、内部工作 ID 或联系方式。

登记成功响应示例：

```json
{
  "appointment_id": "appointment_01J...",
  "status": "registered",
  "matched": true,
  "scheduled_start_at": "2026-09-01T02:00:00Z",
  "scheduled_end_at": "2026-09-01T03:00:00Z",
  "email_reminder": {
    "status": "scheduled",
    "scheduled_for": "2026-09-01T01:30:00Z"
  },
  "speech_preparation": {
    "status": "queued",
    "total": 3,
    "ready": 0,
    "failed": 0
  }
}
```

邀请页必须把登记与 start 拆成两次明确操作：“核验身份并确认预约”不能申请麦克风或调用 start；确认成功后显示预约时间和邮件提醒说明，到允许开始时间后才提供“检查设备并进入面试”。SMTP 使用 `INTERVIEWER_SMTP_*` 环境变量，授权码对应 `INTERVIEWER_SMTP_PASSWORD`，仓库样例保持为空。未配置或发送失败只能形成可重试/可观察状态，不能返回或记录虚假的 `sent`。

候选人 start 必须再次检查 token、登记、明确同意、时间窗、未过期设备 readiness 和服务端 STT readiness。默认开始窗口为闭区间 `[scheduled_start_at, scheduled_end_at]`；提前/延后宽限只能来自创建预约时冻结的显式策略。成功时在同一事务原子把预约标为 `consumed`，创建唯一 `InterviewSession` 并冻结候选人、计划、岗位和简历版本；重复调用返回同一会话。

### 候选人会话窄接口

以下接口公开在认证中间件层，但都必须携带 `X-Candidate-Session-Token`。响应只包含当前候选人完成面试所需的 allow-list 字段。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/public/interviews/{interview_id}` | 返回候选人姓名、会话状态、`avatar_mode` 和安全轮次投影；未来题干和所有答案/rubric/候选池均隐藏 |
| `POST` | `/api/v1/public/interviews/{interview_id}/agent-ticket` | 用 candidate session token 换取 60 秒、一次性 Agent/LiveKit 票据及冻结 recovery capability |
| `GET` | `/api/v1/public/interviews/{interview_id}/avatar-config` | 仅对当前候选人返回授权 VRM 配置、hash 和短期模型 grant |
| `GET` | `/api/v1/public/interviews/{interview_id}/avatar-model?grant=...` | 私有、不缓存交付经 hash/许可校验的 VRM；grant 与候选人/面试绑定 |
| `POST` | `/api/v1/public/interviews/{interview_id}/runtime-problems` | 报告 allow-list 的候选人致命运行故障；校验 token 后真实暂停会话并返回最小确认，不接受浏览器错误文本 |

候选人 token 由至少 32 字符的 `INTERVIEWER_CANDIDATE_TOKEN_SECRET` 对会话 ID 与创建时间做 HMAC-SHA256 派生，不明文持久化或出现在后台详情；验证使用常量时间比较，错误 token 返回 403。正式页面用它换取一次性 Agent/LiveKit ticket。`runtime-problems` 只接受 `AVATAR_ASSET_UNAVAILABLE/AVATAR_MODEL_LOAD_FAILED/AVATAR_RENDERER_FAILED/CANDIDATE_RUNTIME_FAILED`，由服务端映射去敏原因并幂等调用生命周期 pause；响应只含状态、问题码、动作和暂停时间，原始 JS/WebGL/网络异常既不上传也不持久化。候选人端不存在可直接提交录音、转写、Avatar 播报或云会话 close 的第二套业务接口。

短期本地 grant 指向隐藏的 `GET|HEAD /api/v1/private-files/{token}`。无 `Range` 时返回 `200`；一个合法的 `bytes=start-end`、`bytes=start-` 或 `bytes=-suffix` 返回 `206`，并携带 `Accept-Ranges: bytes`、精确 `Content-Range` 和所选区间 `Content-Length`；不可满足、畸形或多段范围返回空体 `416`、`Content-Range: bytes */{total}` 与 `Content-Length: 0`。`HEAD` 与等价 `GET` 返回相同状态、媒体类型和长度头但不返回正文。该传输合同适用于受同一 grant 保护的简历和音频，不暴露 object key，也不放宽 token、审计、`private, no-store` 或租户校验。

生产签票还会重检 `INTERVIEWER_REALTIME_AGENT_ENABLED_ORGANIZATIONS`、`INTERVIEWER_DEPLOYMENT_ID`、`INTERVIEWER_RELEASE_REVISION` 和签名 acceptance v2 report；组织名单只接受逐个 organization ID，不支持 `*` 全量放开。报告不匹配或组织未启用时返回 `REALTIME_AGENT_RELEASE_NOT_READY`，且不会创建 ticket；60 秒票据在 WebSocket 消费事务中还会用数据库时钟再次验证同一门禁，期间报告失效或组织被移出名单会把票据标记为 revoked。报告的输入矩阵必须覆盖 Chrome/Edge/Safari，每类至少 10 个全部通过的桌面案例；它只输出聚合指标与数据集 hash。

## 面试会话、逐题评分与企业复核 API

不存在直接创建或直接 START 会话的后台 API。会话只能由公开 invitation start 经 Appointment Admission 创建并首次 START；`PATCH /interview-plans/{id}` 只接受 canonical `bank_slots`，额外字段包括 `items` 会在 schema 层拒绝。

音频回答通过候选人 public 接口或受 RBAC 保护的后台修复接口进入同一个服务：服务端先把轮次迁移到 `transcribing`，以 `stt.batch/candidate_answer_repair` 取得权威 final，再创建 CandidateAnswer 和评分工作项。请求中的 `development_transcript` 只供 localhost mock STT；生产环境显式拒绝。不存在客户端 `POST /answers` 文本入口。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/interviews` | 企业列出面试和复核状态 |
| `GET` | `/api/v1/interviews/{interview_id}` | 获取计划快照、当前阶段和轮次摘要 |
| `POST` | `/api/v1/interviews/{interview_id}/pause` | 暂停并保留当前抽题和转写状态 |
| `POST` | `/api/v1/interviews/{interview_id}/resume` | 继续当前轮次 |
| `POST` | `/api/v1/interviews/{interview_id}/recover` | 修复断线、STT 或 worker 中断 |
| `POST` | `/api/v1/interviews/{interview_id}/skip` | 按策略跳题并进入下一槽位 |
| `POST` | `/api/v1/interviews/{interview_id}/complete` | 人工结束；有转写/评分进行中时返回 409 |
| `POST` | `/api/v1/interviews/{interview_id}/cancel` | 取消且不生成正常报告 |
| `GET` | `/api/v1/interviews/{interview_id}/events` | 按 sequence 返回持久生命周期事实 |

所有命令进入同一个 `InterviewSessionLifecycle` seam。岗位题槽位的选择产生持久 `QuestionSelection` 和 `InterviewQuestionSnapshot`；断线重试不能重新抽题。题库阶段完成后自动切换到 `resume_experience` 阶段。

### 企业复核

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/interviews/{interview_id}/review` | 返回报告、每题题干、转写、评分证据和复核标记 |
| `POST` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/audio-url` | 生成短期、审计的音频签名 URL |
| `PATCH` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/transcript` | 人工修正最终转写并产生新 revision |
| `POST` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/regrade` | 使用冻结题目和指定转写 revision 重评 |
| `GET` | `/api/v1/interviews/{interview_id}/answers/{answer_id}/evaluations` | 列出单题评分 revision |
| `GET` | `/api/v1/interviews/{interview_id}/reports` | 列出报告 revision |
| `GET` | `/api/v1/interviews/{interview_id}/report/export?format=csv|json` | 生成带权限与审计的报告导出 |
| `POST` | `/api/v1/interviews/{interview_id}/review-complete` | 记录企业复核完成，不代表录用决定 |

管理员可调用 `POST /api/v1/admin/evaluations/score-calibration` 提交 `dataset_version` 与 2–5000 条 `{evaluation_id, human_score, fairness_cohort?}`。Schema 拒绝额外字段，接口只解析本组织当前 evaluation revision，不接收候选人姓名、联系方式、转写或简历。响应给出 MAE/RMSE/偏差、±5/±10 一致率、题型/语言/STT 质量/不透明 cohort 分层和仅供人工评审的线性校准候选；样本少于 30 或 cohort 少于 10 时明确告警，校准不会自动写回评分或作录用决定。

复核响应示例：

```json
{
  "interview_id": "iv_01J...",
  "overall_score": 82,
  "job_fit_level": "match",
  "job_fit_evidence": {
    "supports": ["Python 并发与数据库维度达到岗位阈值"],
    "gaps": ["系统设计容量估算证据不足"]
  },
  "turns": [
    {
      "phase": "position_bank",
      "question_text": "请解释 GIL……",
      "final_transcript": "……",
      "score": 86,
      "evidence": [{"text": "……", "start_ms": 4200, "end_ms": 7800}],
      "audio_available": true,
      "review_flags": []
    }
  ],
  "human_decision": null
}
```

API 不根据 `job_fit_level` 自动写入录用/淘汰结果；若未来接 ATS，人员决定必须作为独立、显式、可审计的企业动作。

每次生成报告时，`evaluation_ids` 必须与生成瞬间每个答案的 `current_evaluation_id` 集合完全一致；`overall_score`、岗位证据、风险和 `manual_review` 都只从该集合推导。历史评分仅由引用它的历史报告展示，不能污染当前 revision。

## 统一实时面试 Agent 合同

正式候选人和企业监看控制通道为 `WS /api/v1/interviews/{interview_id}/agent?ticket=...`。候选人先用 `POST /api/v1/public/interviews/{interview_id}/agent-ticket` 和 `X-Candidate-Session-Token` 换取 60 秒、一次性、只限当前房间/轨道的票据；企业人员用 `POST /api/v1/interviews/{interview_id}/agent-ticket`，reviewer 只能订阅，admin/interviewer 才能发布人工麦克风。应用 ticket 只以 SHA-256 hash 持久化，LiveKit participant token 不落库。

WebSocket 第一条消息必须是 `session.open`，只包含 `recovery_cursor` 和设备能力。之后 JSON 信号使用 `type/idempotency_key/turn_id/causation_id/payload`，单条不得超过 64 KiB；Agent WebSocket 始终拒绝二进制帧，音视频的正常路径唯一是票据授权的 LiveKit WebRTC。正式事件统一为：

```json
{
  "event_id": "agent_event_01J...",
  "session_sequence": 42,
  "type": "transcript.final",
  "turn_id": "turn_01J...",
  "causation_id": "candidate_01J...",
  "occurred_at": "2026-09-01T02:10:00Z",
  "replayability": "replayable",
  "payload": {}
}
```

稳定类型仅为 `session.snapshot`、`floor.changed`、`speech.started/stopped`、`transcript.partial/final`、`conversation.act.selected`、`avatar.performance.*`、`takeover.changed`、`problem`、`completed`。共享 replay/Redis 载荷按事件类型使用字段 allow-list；未知字段失败关闭。每次连接只接收一次按当前角色重建的最新 `session.snapshot`，旧 snapshot 不回放；cursor 早于 `replay_history_floor` 时返回恢复问题并要求重新同步。候选人 snapshot 额外投影 durable `calibration_retry_required`，使刷新、heartbeat 或 transient 事件丢失后仍能恢复“必须人工重试”的暖场 gate；它不包含暖场音频或转写。候选人、reviewer/observer 与 interviewer/admin 使用不同 projection，标准答案、rubric、内部缺失点、Provider 原始内容、未来题目、接管 actor/lease、私有媒体 URI 和证据绑定不得进入低权限投影。`speech.started` 的服务器收音确认携带 `media_transport=livekit_server_subscriber/ingress_sequence`，不能与浏览器本地 VAD 或字幕状态合并为一个图标。

候选人控制信号包括 `client.ready`、`media.published`、`speech.started/stopped`、`evidence.stream.open`、`evidence.finish`、`evidence.recovery.begin/chunk/complete`、`request_repeat`、`continue_speaking`、`warmup.confirm/retry`、`pause`、`avatar.performance.stopped` 和无 PII 的 `telemetry.observe`。票据的 `media.evidence_transport` 在候选人角色必须为 `livekit_server_subscriber`；`media.recovery.server_checkpoint` 声明服务端持久 checkpoint，`browser_backfill` 冻结 source connection、audio epoch、30 秒/2 MiB/32 KiB 限额。backfill chunk 是有界 JSON base64，每帧先写私有 FileObject，不会放宽 WebSocket 二进制禁令。人工控制包括 `takeover.acquire/renew/release` 与 `human.speech`；acquire 必须含原因，renew/release 必须含 lease ID 和 expected version，人工话语永远是 `unscored_intervention`。

`telemetry.observe` 的包络形式与其他 JSON 信号一致，但其 `idempotency_key` 不形成领域幂等承诺：服务端完成 candidate role 校验后，只把合法、有限且属于固定词汇的数值送入当前进程指标，并显式让出调度。它不进入 AgentChannel 领域锁、`processed_signal_keys`、InterviewSession/SQLite/PostgreSQL 事务、事件 replay、Evidence command journal 或 takeover 到期处理；非法、未知、NaN/Infinity、负数或越界样本静默丢弃，不能转成 `problem` 或生命周期 pause。候选端把 `avatar_viseme_drift_ms`、`avatar_freeze_ms` 分别收敛为 1 秒最大值窗口，每个窗口至多发送一次；socket 未打开或窗口结束前关闭时不排队、不跨重连补发。其余低频 latency 指标保持即时发送。

`evidence.stream.open` 是两阶段 causation handshake：客户端发送后只能标记 `requested=true/ready=false`；单一 fenced owner 在 Provider stream 已实际打开、且 command receipt 尚未完成时，必须经 Hub 投影同 causation 的 transient `floor.changed(reason=warmup_stream_open|evidence_stream_open)`，发起命令的客户端匹配后才标记 ready。同一候选人的其他 controller 也可能收到这条广播，但只能推进 cursor 和安全共享状态，不能据此打开本地 gate。即使 floor 已经是 candidate，或者幂等重提命中已打开的同一 stream，也必须重新投影这条 ready ACK；ACK 不进入 replay history。控制连接在 ACK 前断开时，新连接可对同一未确认 open 有界重提一次并等待新 causation，旧 ACK 不得打开新连接的 gate。ready 前普通 VAD 不发送 `speech.started/stopped`，页面显示“准备识别”；ready 时若本地 capture 仍持续 speaking，再承接 start。唯一例外是 agent 播放期间已经过更高幅度和持续时间确认的 barge-in，它仍须在 200ms 目标内先静音并立即通知服务端。

`avatar.performance.started` 在候选人 facade 中建立与 `performance_id` 绑定的单一当前播放代次。`avatar.performance.interrupted` 携带 ID 时只能中断同一代次；旧 ID 的迟到事件以及已因 barge-in/replaced/close 失效播放器的 `error`、`ended` 或 `play()` 拒绝全部忽略。当前代次自然结束只发送一次 `avatar.performance.stopped`；当前代次真实加载/解码/播放错误仍提交 `CANDIDATE_RUNTIME_FAILED` 并等待服务端持久暂停回执。

服务端对 `avatar.performance.stopped` 同样执行强校验：`performance_id` 缺失、当前已无活动播放或 ID 不匹配时，该信号除自身幂等记录外不得产生 `avatar.performance.stopped` 事件、改变 floor/暖场/结束状态或打开 Evidence。只有在同一租户事务中成功 compare-and-clear 当前 ID 后才可继续状态迁移。

`transcript.partial` 仍为 `TRANSIENT` 界面信号，不是答案或评分输入。Authoritative Evidence owner 不在 LiveKit 收帧回调中等待 partial 投影，而是按 `(warmup|formal, turn_id)` 只保留一个正在投影值与一个最新待投影值；不同 key 的待处理总数也有硬上限。`transcript.final`、错误、对话动作与结束边界仍同步有序；final/reset/stop 必须等待当前 partial 投影边界并丢弃已合并的旧值，不得在 final 后迟到。正式权威收音断流仍暂停且面向候选人输出统一 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED`；若同一错误发生在非评分暖场，事件为 `recoverable=true/action=retry_warmup/calibration=true`，runtime 进入 durable retry gate 而不暂停 InterviewSession。特权会话诊断可额外包含经固定语义白名单校验的 `cause_code/cause_type`，不得包含 URL、token、凭据或转写。

`evidence.finish`/内部 `evidence.seal` 不能直接越过 LiveKit sink：ingress 在命令执行点捕获已无损入队的 `accepted_sequence`，等待 `delivered_sequence` 至少达到该水位后，才调用 Evidence chain finish。水位之后的房间环境音由随后关闭的 Evidence gate 过滤，不属于已接受的本轮前缀。排空失败或超时统一映射为 `LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED`，特权根因为 `LIVEKIT_INGRESS_SINK_BACKPRESSURE`；正式轮次暂停并等待修复/人工接管，暖场则复用显式 `warmup.retry`，两者都不得生成截断 final。候选端默认连续静音 800ms 才发 `speech.stopped`，自然的短停顿保持在同一发言内；暖场保留服务端 2.5 秒 endpoint timer，正式回答改为本页 011 的显式 finish。

权威 Evidence 命令受 `EvidenceControlGeneration` 保护：新 control attach 后旧连接命令返回 `409 EVIDENCE_CONTROL_STALE`，迟到 detach 不能启动新 grace。正式 STT/final 另受 `EvidenceOwnershipEpoch` 保护；旧 owner 提交返回 `409 EVIDENCE_OWNER_FENCED`。`EvidenceCommandJournal` 在返回 receipt 前持久化安全命令，同 idempotency key 返回同一 terminal outcome；当前数据库 fenced owner executor 通过 DB polling 和 Redis wake hint 执行，非 owner 控制连接作为 remote receipt proxy，不创建第二证据链。claim 过期/崩溃时由新 epoch 重领，effect receipt 与 CandidateAnswer commit fence 阻止重复效果；Redis 永远不是命令或所有权真相。

owner 的周期续租保持数据库 fence 为唯一正确性判断，同时上报不含 interview、candidate、lease 或 connection 标识符的 `evidence_owner_renew_scheduler_lag_ms`、`evidence_owner_renew_db_latency_ms`、`evidence_owner_renew_success` 进程指标。指标异常或采集失败不得影响续租；反过来，指标也不能延长租约、放宽数据库时钟或吞掉续租异常。租约已过期或 epoch/lease/owner fence 不再匹配时仍执行原 self-fence、关闭 ingress 并暂停/等待人工处理，旧 owner 不得以迟到续租恢复；control generation 继续独立保护候选控制命令。

`GET /api/v1/public/interviews/{interview_id}/avatar-config` 只在许可清单的实际使用范围、VRM 1.0 与 SHA-256 一致时返回 ready 配置；标准元音 `aa/ih/oh/ou` 可位于 VRM preset，其余必需口型可位于 custom，检查两者并集。模型用候选人/面试绑定的短期 grant 通过 `GET /api/v1/public/interviews/{interview_id}/avatar-model` 交付，响应 `private, no-store`。LiveKit Egress webhook 为 `POST /api/v1/public/livekit/egress-webhook`，必须校验 JWT 签名、issuer、时间、body hash，再从 provider 绑定解析组织；不得相信请求体 tenant/room，随后才可幂等推进 `InterviewMediaCapture`。

`CandidateConsentCreate` 的正式字段只有 `audio_recording` 与 `video_recording`，旧的合并录制字段已从 schema 删除。`CandidateReadinessCreate` 包含 camera、microphone、speaker、WebRTC、AudioWorklet、WebGL、MediaRecorder、网络 RTT/jitter 和 avatar FPS。VRM 持续帧率 gate 与候选人可见值使用同一整数判定 `Math.round(fps) >= minimum_fps`，避免页面显示 30 FPS、底层却因 29.5 左右的原始浮点值失败；29.5/29.4 边界由前端合同固定。录像场次缺少 video consent/camera/LiveKit Egress 时 start 失败关闭；未授权视频的 participant grant 不含 camera。

`media.published` 只建立 Evidence subscriber，暖场试音不录制且不进入评分。暖场 final/seal 是 destructive-once：Provider final 或暖场音频流失败后原 stream 被消费/abort，journal 保留第一次真实错误，不能二次进入同一流并用通用 turn 错误覆盖。每个打开的暖场流有本地 epoch；音频流失败会先失效 epoch，因而已提交或正在等待 final 的旧 seal 即使随后成功返回，也只能得到同一 rejected receipt，不能发出 `transcript.final` 或 `warmup_confirmation`。此时 runtime 原子写入 `calibration_status=retrying + calibration_retry_required=true`；服务端在 durable gate 未清时拒绝 `evidence.stream.open`。候选人页面只展示显式“重新试音”；owner 执行 `warmup.retry` 时先恢复同一 LiveKit microphone 的服务端 iterator，必要时重建 subscriber，成功后才清 gate 并投影 transient 同 causation `floor.changed(reason=warmup_retry)`。任一当前 control 收到这条 live reset 事实，或从 snapshot 读到 `retrying + calibration_retry_required=false`，都可用新 causation open/reassert；相同 retry causation 的重复 ACK 不得清掉已请求/ready 的新流。旧 control 由 generation fence 拒绝，existing-open 只重新 ACK，不重复创建 Provider 流，因此刷新、heartbeat 或断线恢复不会绕过显式 retry 反复调用付费 STT。`warmup.confirm` 删除暖场音频/转写、清空候选端暖场字幕投影并启动必需 Egress；若命令发送失败，页面恢复原确认状态和字幕。启动失败进入暂停/接管。最后回答接受后 runtime 先停 Evidence/Egress，再播放 `closing` act；收到 `avatar.performance.stopped` 或有界超时后才发唯一 `completed` 回执，包含提交确认、录制留存和人工审核声明。

人工接管 lease 为 60 秒 CAS 事实。普通企业 Agent ticket 只能订阅；获得 lease 的 admin/interviewer 调用 `POST /api/v1/interviews/{interview_id}/takeover/media-permit`，以 `lease_id + expected_version` 一次换取最长 15 秒、限房间/限 microphone/禁止订阅的 LiveKit permit。获取、续租、发言复核、释放、过期与 permit 均使用数据库时钟；lease 释放、过期或会话终止立即移除 participant，并保持 AI 暂停。

旧 `/live`、`/stt-stream`、候选人/企业 `audio-answers`、`avatar/speak` 和 avatar close 路由已删除；旧候选人 runtime/PCM 发送器也已删除。安全的历史记录修复仍可在内部 `InterviewEvidenceChain -> stt.batch` seam 执行，但不是另一个候选人入口。当前仓库合同不等于生产验收；目标 LiveKit/TURN/Egress/OSS、商用 VRM、真实 Provider、金标与试点仍需生成鲜活、合格、绑定当前 release 的 HMAC 签名 `realtime-interview-agent.acceptance.v2` 报告。

`REALTIME-WARMUP-VAD-RANGE-003` 已达到“verified（仓库），目标环境复验 pending”，以上合同不得表述为目标浏览器和真实 Provider 已验收。该项不修改题目抽取、冻结证据、答案/评分、追问、S2S/cascade 或人工接管 API 与领域语义。

`EVIDENCE-LEASE-TELEMETRY-004` 已达到“verified（仓库），目标新会话复验 pending”。它只修正遥测传输成本、调度公平性和 owner 续租可观测性；抽题、计划/题目冻结、权威证据、CandidateAnswer、评分、受控追问、S2S/cascade 与人工接管 API/领域语义均未改变，也不代表生产环境已验收。

## 模型网关和后台配置 API

请求/响应式业务只依赖 `invoke`；实时 STT 通过同一 Model Invocation deep module 的 `open_stream`，不能让实时网关直接 import Provider：

```python
class ModelGateway:
    async def invoke(
        self,
        capability: str,
        request: "InvocationRequest",
        *,
        route: "ModelRoute | None" = None,
    ) -> "InvocationResponse": ...

    async def open_stream(
        self,
        request: "StreamingSTTRequest",
        *,
        route: "ModelRoute | None" = None,
    ) -> "ValidatedSTTStream": ...

    async def open_speech_dialogue(
        self,
        request: "RealtimeSpeechDialogueRequest",
        *,
        route: "ModelRoute | None" = None,
    ) -> "ValidatedSpeechDialogueStream": ...
```

业务调用按 `organization_id + capability + purpose` 解析 route。正式面试目标流程要求统一 schema 覆盖 `llm.chat_json`、`tts.synthesize`、`stt.streaming`、`stt.batch` 和需要的 `avatar.speak`；没有 schema、可执行 adapter 和通过健康测试的能力不能进入 active route。`embedding.text` 是可选的未来题库治理能力，不是题库 ready、计划批准、预约邀请、随机抽题或答案评分的前置条件；仓库不再持久化旧向量题库 projection。

低延迟追问可额外配置 `speech.dialogue_realtime/candidate_followup_dialogue`。对应 ModelConfiguration 使用 `realtime_speech` 类型；没有 schema、可执行 adapter 和通过健康测试的模型不能进入 active route，缺 route 时面试继续使用 cascade。

模型管理分为三层：`ProviderConnection` 只保存组织级厂商连接、API Key 引用和区域等连接参数；`ModelConfiguration` 选择 `llm/embedding/tts/stt/avatar/realtime_speech` 类型及厂商模型，并保存该模型的专属设置和统一默认参数；`ModelRoute` 只引用模型配置。插件 manifest 返回 `connection_form`、`credential_form` 与各模型类型的 `configuration_form`，前端使用通用控件渲染器，不内置任何厂商字段。

火山引擎连接在同一 ProviderConnection 中明确区分 `ark_api_key` 与 `speech_api_key`：前者只用于 Ark Chat/Embedding Bearer 请求，后者只用于 Seed ASR、Seed-TTS 与 Seeduplex 的 `X-Api-Key`。连接级探针验证 Ark 端点并确认语音凭据已配置；每个语音 ModelConfiguration/Route 的握手或最小调用探针才验证具体 Resource ID、模型授权和会话参数。API 永不回读两种密钥明文。

ProviderConnection 与 ModelConfiguration 响应增加单调递增的 `configuration_revision`。创建为 1；连接参数/凭据或模型 settings/default parameters/启用状态/显示名修改时增加；凭据校验和任意能力健康探针只推进通用 `version` 与健康事实，不改变 `configuration_revision`。因此 LLM、Embedding、STT、TTS、实时语音和数字人的编辑/删除页面都可以安全吸收探针造成的新 version，同时拒绝覆盖真正的并发配置修改。

`POST /admin/model-routes` 使用强类型请求：`primary`/`fallbacks` 仅接受 `model_configuration_id`、`timeout_s`、`pricing`；路由创建时校验模型配置已启用、状态为 `ready` 且支持目标 capability。`policy` 仅接受既有重试、熔断、成本和 readiness 字段。生产环境找不到精确 route 时返回 `provider_route_missing`，只有 development/test 允许离线 mock fallback。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/admin/model-providers/catalog` | 已安装插件、能力和实现状态 |
| `POST/GET/PATCH/DELETE` | `/api/v1/admin/model-provider-connections[/{id}]` | 创建、列表/单项查看、修改和删除厂商连接；凭证只写入、读取时仅返回状态 |
| `POST` | `/api/v1/admin/model-provider-connections/{id}/validate` | 通过 Provider adapter 执行真实 API Key 鉴权探针，保存 `valid` / `invalid` / `model_required` 状态 |
| `GET` | `/api/v1/admin/model-provider-connections/{id}/model-catalog` | 返回该连接可配置的模型类型、模型目录和动态表单 schema |
| `POST/GET/PATCH/DELETE` | `/api/v1/admin/model-configurations[/{id}]` | 创建、列表/单项查看、修改和删除具体模型及其参数与启用状态 |
| `POST` | `/api/v1/admin/model-configurations/{id}/test` | 以模型配置的统一能力探针进行真实调用并更新健康状态；流式 STT/实时语音对话验证真实 session 握手，不用静音伪造识别质量样本 |
| `GET` | `/api/v1/admin/model-configurations/{id}/voices` | 返回 TTS 模型可选声音目录；静态 manifest、管理员映射或 Provider 只读目录由后端统一归一化 |
| `POST/GET` | `/api/v1/admin/model-routes` | 管理能力/purpose 路由 |
| `POST` | `/api/v1/admin/model-routes/{id}/test` | 测试 schema、primary 和 fallback |
| `GET` | `/api/v1/admin/work-items` | 查看状态计数、dead-letter 指标和工作项摘要 |
| `POST` | `/api/v1/admin/work-items/{id}/replay` | 带原因和审计地人工重放失败/dead-letter 工作项 |
| `GET` | `/api/v1/admin/audit-events` | 管理员读取租户审计事件 |
| `GET` | `/api/v1/admin/evaluations/question-selection-fairness` | 比较同岗位抽题数量、难度和技能覆盖分布 |
| `POST` | `/api/v1/admin/session-monitor/run` | 扫描心跳超时并通过生命周期命令暂停会话 |
| `POST` | `/api/v1/admin/retention/run` | 默认 dry-run 预览到期候选人；显式 `dry_run=false` 才清除私有文件、联系方式和面试敏感内容并审计 |

两个 `DELETE` 都必须在查询参数携带 `expected_version`，陈旧版本返回 `409`。删除 ProviderConnection 会在同一租户事务中删除它的加密凭证、全部 ModelConfiguration，以及引用这些模型的 ModelRoute 和对应断路器状态；删除单个 ModelConfiguration 只删除该模型及引用它的路由/断路器状态，同连接下其他模型和厂商连接保持不变。历史 ModelInvocationLog 作为脱敏审计事实保留。

流式能力的管理员测试采用分层语义：`stt.streaming` 和 `speech.dialogue_realtime` 在收到 Provider 的 ready/session-created/session-updated 确认后返回 `probe_mode=handshake`，证明端点、凭据、模型访问和 session 参数可用，并立即主动关闭连接。探针不要求静音产生 final transcript，也不生成自由回答；WER、真实 final、首音、音质和打断必须继续用脱敏语音样本及候选人端到端链路验收。模型路由测试复用相同握手合同。

具体 Provider manifest、STT/TTS 请求响应和错误语义见 [模型供应商插件化设计](model-provider-plugins.md)。
