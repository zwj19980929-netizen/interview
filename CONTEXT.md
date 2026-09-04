# Interviewer Domain

本领域描述从岗位题库与候选人简历形成计划和预约，执行语音面试并形成可复核岗位评价的核心概念。

## Language

**JobPosition**:
企业可复用的岗位，是 KnowledgeBase、RoleRequirement、ResumeReview、InterviewPlan 和 InterviewAppointment 的共同上层边界。
_Avoid_: Role、JobRequirement、PositionText

**KnowledgeBase**:
由组织统一维护、可显式关联到多个 JobPosition 的知识库式题库，包含 Question、评分依据、索引与读题语音构建状态；岗位复用题库时同时复用其 KnowledgeBaseSpeechProfile 和读题资产。
_Avoid_: GlobalQuestionPool、QuestionList、CopiedBank、PositionVoiceOverride

**PositionKnowledgeBaseAssignment**:
JobPosition 对组织内既有 KnowledgeBase 的显式引用；它决定搜索和计划范围，但不拥有或复制题目、音色及语音资产。
_Avoid_: CreateBankForm、CopiedKnowledgeBase、VoiceOverride

**KnowledgeBaseSpeechProfile**:
一个 KnowledgeBase 当前生效的读题语音选择，冻结 TTS ModelConfiguration、声音、语言、格式和语速并拥有独立 revision；新题库可从组织的 question_speech_generation route 初始化，之后只由题库显式配置推进。
_Avoid_: GlobalTTSSetting、VoiceDropdownValue、ModelRouteAlias

**KnowledgeBaseSpeechBuild**:
面向一个 KnowledgeBaseSpeechProfile revision 的整库语音重建，汇总题目级生成进度、失败和重试结果；新 revision 不接受旧构建结果成为当前资产。
_Avoid_: ForLoopTTS、BulkButtonRequest、CeleryJob

**QuestionSpeechAsset**:
由题目版本和 KnowledgeBaseSpeechProfile revision 确定、异步生成的不可变读题语音，供数字人或音频降级路径读取；真实 WAV 只有通过 RIFF 完整性校验与安全的流式长度规范化后才能成为 ready 资产。
_Avoid_: BrowserSpeech、TemporaryTTS、AudioURL

**AvatarDelivery**:
按 InterviewAppointment 冻结的 `avatar_mode` 把批准话语表达给候选人的统一交付边界；LocalAvatarDelivery 使用可中断的本地 3D 形象，CloudAvatarDelivery 使用远端实时形象，两者共享 AvatarPerformance 合同。
_Avoid_: StaticPortrait、AvatarProviderSwitch、ReactAudioFallback、TencentService

**RealtimeSpeechDialogue**:
面试实时表达轨的供应商无关语音到语音流；它与权威 STT 并行接收候选人 PCM，原始音频 delta 先隔离缓冲，只有 Provider final 与已冻结 ApprovedConversationAct 逐字一致后才私有落盘并表达，输出音频和 transcript 不构成评分证据。
_Avoid_: ScoringS2S、ProviderConversation、AudioTruth

**ConversationUtterance**:
实时面试中一次具有明确起止和意图的候选人话语；它可以是回答、重读请求、澄清请求、继续补充或暂停请求，只有被批准为回答时才形成 CandidateAnswer。
_Avoid_: AudioChunk、BrowserTranscript、CandidateAnswer

**TurnUnderstanding**:
针对一个 ConversationUtterance 形成的版本化结构化理解，记录原文证据、能力点覆盖、歧义、矛盾、置信度与建议动作；它服务于对话决策，不等同于 AnswerEvaluation。
_Avoid_: Score、LLMThought、FollowUpText

**ApprovedConversationAct**:
实时面试策略在预算、安全与证据校验后批准的唯一下一动作及其可播报文本；追问必须绑定冻结根轮次、非空原文证据、能力点和正深度，表达层不得临时补建追问；所有 AI 动作恒为非评价性，数字人和语音 Provider 只能表达它，不能自行决定追问或评价。
_Avoid_: ProviderReply、FreeChat、RawModelOutput

**InteractionFloor**:
实时面试中当前被授权发言的一方，只能是数字人、候选人、接管中的企业面试官或无人；打断、恢复和接管都必须先改变该事实。
_Avoid_: PlayingFlag、MicrophoneState、SpeakerCSS

**AvatarPerformance**:
一个 ApprovedConversationAct 对应的可中断数字人表达，冻结私有音频引用、`pre_generated/cascade/s2s` 交付类型、口型时间线、姿态动作和播放身份；候选人端只允许当前播放代次的回调改变状态，已被打断或替换的旧播放器没有领域效果；VRM 标准元音 preset 与 custom 表情共同满足口型合同，它不拥有对话内容决策权。
_Avoid_: AudioURL、SpeakingAnimation、StaticImage

**CandidateRuntimeProblem**:
候选人页面以短期会话 token 报告的 allow-list 致命运行故障；主动 barge-in、表达替换或关闭及其迟到播放器回调不属于故障；服务端把稳定 code 映射为去敏原因并推进真实生命周期暂停，只有持久暂停确认后 UI 才显示“已暂停”。
_Avoid_: BrowserError、StackTrace、ToastPause、ClientOnlyFailure

**AgentExpressionAudio**:
经批准数字人话语对应的私有 FileObject；领域只持久化 `agent-expression://file_id`，每次角色安全投影才签发短期读取地址。S2S 缓冲和动态 TTS 都必须先通过音频容器完整性门禁并落入此边界，不能把 data URI、Provider 临时 URL 或长期签名 URL写入会话与事件历史。
_Avoid_: PublicTTSURL、ProviderAudioDelta、DurableSignedURL

**FollowUpTurn**:
针对一个根 InterviewTurn 的原文证据、歧义、矛盾或缺失能力点形成的受控澄清子轮次；最多深入到会话冻结策略允许的深度 2，权重 0，并受每题、全场和时间预算约束，不能改变批准计划分值。
_Avoid_: NewPlanQuestion、LLMFreeQuestion、IndependentScore

**QuestionGenerationBatch**:
一次面向指定 KnowledgeBase、基于题库定位、标签和可选要求形成的候选题生成与人工审核集合；确认导入前不属于正式题库。
_Avoid_: AutoImport、PromptResult、QuestionList

**GeneratedQuestionDraft**:
QuestionGenerationBatch 内可由面试官修改或删除的候选题，具有完整题干、答案、关键点和评分标签，但尚不能用于计划或面试。
_Avoid_: Question、GeneratedQuestion、TemporaryQuestion

**QuestionGenerationExecution**:
QuestionGenerationBatch 的一个可停止执行代次；规划、分片和合并工作冻结 execution revision，旧代次的迟到结果不能进入当前批次；多槽位输出截断可在同一代次原子拆成单槽位替代工作。
_Avoid_: CeleryTask、BrowserProgress、KillWorker

**QuestionBlueprint**:
智能生题规划阶段冻结的单个题目槽位，使用 topic、scenario 和 focus 定义互斥考察方向；它约束 Worker 生成，但不是候选题或正式题目。
_Avoid_: QuestionType、DraftQuestion、PromptFragment

**CandidateProfile**:
企业上传到组织简历库、录入时明确一个应聘 JobPosition、可参与后续面试流程的候选人记录，拥有联系方式和 ResumeDocument 版本；它不是某次面试的冻结快照。
_Avoid_: InterviewCandidate、ApplicantForm、ResumeRow

**PositionCandidateMembership**:
CandidateProfile 对一个应聘 JobPosition 的明确归属；删除岗位会清除该归属内候选人的敏感数据，但保留不可识别的审计与历史面试占位。
_Avoid_: CandidateRow、GlobalCandidate、UISection

**ResumeDocument**:
候选人简历 PDF 的不可变版本，可由本地文件上传或 URL 导入形成；原始文件与解析文本都由系统托管并可追溯。
_Avoid_: ResumeText、URLResume、UploadedFile

**FileObject**:
系统私有文件的受控元数据，记录用途、后端、对象键、哈希、大小、MIME、扫描和生命周期状态；业务资源只引用其 ID。
_Avoid_: PublicURL、RawPath、BucketBlob

**PrivateFileStorage**:
统一保存、回读、签发短期访问和删除私有文件的深模块；本地文件系统与阿里云 OSS 是可替换 adapter。
_Avoid_: StaticMediaDirectory、OSSHelper、FileURLBuilder

**ResumeIngestion**:
把本地上传或公开 HTTPS URL 收敛为同一 ResumeDocument 的持久流水线：隔离、校验、扫描、私有存储、解析和失败恢复。
_Avoid_: UploadHandler、URLParser、ResumeTextImport

**ResumeReview**:
一个 ResumeDocument 面向一个 JobPosition 和 RoleRequirement version 的异步 AI 审阅，只产出可追溯项目/技能证据与 CandidateScreening 建议，不直接作出录用决定。只有生效结论为符合时，后续独立工作才可生成 ExperienceQuestion 草稿。
_Avoid_: ResumeScore、HiringDecision、GenericSummary

**ResumeEvidenceChunk**:
ResumeReview 内部按 PDF 页边界和模型输入预算形成的完整证据提取单元，记录来源页、处理状态和 Prompt/Provider 元数据；它只产出项目/技能证据，不能单独形成 CandidateScreening 结论。
_Avoid_: PartialScreening、PageDecision、TruncatedResume

**CandidateScreening**:
ResumeReview 基于脱敏简历与岗位能力要求形成的可解释初筛建议，包含匹配分、分数带结论、命中项和缺口；0–59 分为不符合、60–74 分为待人工复核、75–100 分为符合，人工复核可以覆盖生效结论但必须保留 AI 原始建议与审计。
_Avoid_: HiringDecision、AutoReject、ResumeRank

**CandidateQuestionBank**:
一个 CandidateProfile 的简历问答集合视图，只在生效初筛结论为符合时汇总 AI/人工 ExperienceQuestion；每题必须绑定并点名 ResumeReview 中真实存在的证据快照。它不复制岗位题库，也不是第二套可执行题目实体。
_Avoid_: PersonalKnowledgeBase、CandidateKnowledgeBase、CopiedQuestionBank

**ExperienceQuestion**:
绑定 CandidateProfile，由符合资格后的独立生成工作或面试官基于该候选人简历证据创建，经批准后用于核验过往经历的问题；批准表示可入计划，不提前产生全局读题语音。没有证据快照或题干未点名证据标签的题不得展示或组卷。
_Avoid_: FollowUp、ResumeGuess、AutoApprovedQuestion、PersonalQuestion

**InterviewPlanAssembly**:
依据岗位要求、岗位题库候选池和 ResumeReview 形成候选人专属 InterviewPlan 草稿的结果，包含抽题槽位、冻结候选池、经历问题、覆盖、权重、时长与告警。
_Avoid_: TopNQuestions、SearchResult、GeneratedList

**InterviewPlan**:
面向一个候选人和岗位的唯一可执行面试定义，以抽题槽位、冻结候选池、经历问题、权重、阶段顺序和所选题库共同的 speech profile snapshot 为领域真相；批准后不可原地修改。
_Avoid_: FixedQuestionList、EditableItems、DualPlanRepresentation

**InterviewAppointment**:
绑定候选人、岗位、岗位题库、已批准计划、时间窗和 `local/cloud` AvatarDelivery 策略的预约与一次性邀请；拥有本场简历题语音准备状态，只有通过 start readiness 且位于允许窗口内才能消费并创建 InterviewSession。
_Avoid_: InterviewSession、CalendarEvent、JoinLink

**AppointmentSpeechPreparation**:
候选人确认预约后创建的预约级简历题读题语音准备聚合，按计划冻结的 TTS 模型、音色、语言、格式和语速跟踪每个 ExperienceQuestion version 的工作与资产；不把预约结果回写为 ExperienceQuestion 的全局语音。
_Avoid_: ExperienceQuestionSpeechStatus、DefaultVoiceGeneration、PlanApprovalSpeech

**CandidateIntake**:
候选人通过一次性邀请提交姓名、邮箱、手机号、明确隐私同意和音频/视频分别授权的媒体同意形成的登记记录，用于和该预约绑定的 CandidateProfile 精确匹配；成功事务也是预约级简历题语音的成本触发点。
_Avoid_: CandidateProfile、RegistrationForm、AnonymousSignup

**QuestionCandidatePool**:
InterviewPlan 按岗位、题库、技能、难度、题型、状态和语音 readiness 冻结的 Question ID/version 集合，是随机抽题的唯一输入，不要求向量索引。
_Avoid_: VectorSearchResult、GlobalQuestionPool、LiveSearchResult

**QuestionSelection**:
InterviewSession 为一个岗位题库槽位在已冻结 QuestionCandidatePool 内按会话种子作出的不可变随机选择事实；断线恢复必须复用。
_Avoid_: OrderByRandom、SearchResult、CurrentQuestion

**InterviewSession**:
从已登记并消费的 InterviewAppointment 创建的一次真实面试，拥有 InterviewCandidate、计划/题目快照、QuestionSelection、问答轮次、生命周期事实与报告。
_Avoid_: InterviewAppointment、WebSocketSession

**InterviewLifecycleEvent**:
InterviewSession 对已接受生命周期命令形成的不可变领域事实；同一会话内按 sequence 追加，用于审计、恢复和实时投影。
_Avoid_: WebSocketEvent、StatusLog、MutableEvent

**InterviewCandidate**:
从 CandidateProfile、ResumeDocument 和 CandidateIntake 冻结、只属于一次 InterviewSession 的最小候选人快照。
_Avoid_: CandidateProfile、GlobalCandidate、MutableResume

**CandidateSessionProjection**:
候选人以短期签名 token 访问某次 InterviewSession 时得到的 allow-list 视图，只包含完成当前面试所需的姓名、状态和安全轮次字段；不包含标准答案、rubric、候选池、未来题干、评分 revision 或后台凭据。
_Avoid_: InterviewDetail、AdminSession、PublicInterview

**InterviewQuestionSnapshot**:
问题被选入 InterviewSession 时冻结的朗读与评分依据，可来源于岗位 Question 或 ExperienceQuestion；来源后续修改不影响快照。
_Avoid_: QuestionCopy、CurrentQuestion

**InterviewPlanSnapshot**:
InterviewSession 创建时从已批准 InterviewPlan 冻结的岗位、题库版本、候选池哈希、抽题槽位、经历问题、权重与阶段顺序。
_Avoid_: PlanCopy、CurrentPlan

**CandidateAnswer**:
某一轮候选人回答及其音频与权威服务端 final transcript；partial 或浏览器识别不能成为生产评分输入。
_Avoid_: BrowserTranscript、PartialAnswer、ClientScoreInput

**AuthoritativeMediaBinding**:
InterviewSession 首次正式媒体连接时冻结的 LiveKit provider、room、服务端签发 candidate identity 与原始 connection 事实；控制通道重连不得替换它，也不得从客户端 track name/metadata 推断。
_Avoid_: CurrentWebSocketIdentity、ClientTrackBinding、ReconnectToken

**AuthoritativeEvidenceIngress**:
由当前 EvidenceOwnershipEpoch 所有者运行的 receive-only 候选人麦克风订阅与 Evidence 链，拥有私有录音、音频序号、StreamingSTTSession、端点计时和 batch repair；生命周期独立于 Agent 控制 WebSocket。`LiveKitEvidenceIngress` 是该领域能力的当前媒体 Adapter，不是领域真相名称。
_Avoid_: BrowserPCMUpload、LiveKitRoomState、AgentSocketAudio、LiveKitDomainOwner

**EvidenceOwnershipEpoch**:
某个服务实例取得一场 AuthoritativeEvidenceIngress 所有权时，由持久层单调递增的 fencing token；续租不变，owner 更换必须递增。只有当前 epoch 可以形成正式 final、CandidateAnswer 和其领域副作用。
_Avoid_: TakeoverLeaseVersion、WebSocketGeneration、RedisLockToken

**EvidenceControlGeneration**:
候选人控制通道每次 attach 时单调递增的命令所有权；新控制连接建立后，旧连接命令与迟到 detach 都必须被拒绝。它不代表媒体 owner 更换。
_Avoid_: EvidenceOwnershipEpoch、AgentRecoveryCursor、ControlReconnectGrace

**EvidenceCommandJournal**:
权威 Evidence 控制命令与最小安全结果的数据库持久日志；以面试范围的确定性命令 ID 和请求指纹支持同幂等键回放，以 claim TTL 支持 at-least-once 重投，并同时绑定 EvidenceControlGeneration 与 EvidenceOwnershipEpoch。它不保存音频、转写、票据、候选人 identity 或 Provider 对象；Redis 只能发送唤醒提示，不能替代该日志。
_Avoid_: RedisCommandQueue、AgentSignalHistory、AudioReplayBuffer

**ControlReconnectGrace**:
候选人控制 WebSocket 断开后保留同一 AuthoritativeEvidenceIngress 与发言端点的有限窗口；新控制连接只能接管命令与安全投影，不能重建或替换权威音频链。当前正式值为 30 秒。
_Avoid_: MediaReconnect、AnswerTimeout、NewEvidenceSession

**InterviewMediaCapture**:
一次 InterviewSession 内经明确媒体同意形成的私有音频或视频捕获事实，记录轨道、完整性、存储保护（生产为可验证加密，本地开发为受控私有目录）、文件引用、保留期限和人工访问审计；它本身不构成评分证据。
_Avoid_: CameraPreview、PublicRecordingURL、EmotionSignal

**EvidenceMediaSegment**:
AuthoritativeEvidenceIngress 为一个 InterviewTurn 按 capture revision 顺序封存的私有候选人音频片段；只有当前 revision 的连续完整集合可进入修复，较旧 revision 一经 reset 即成为待清理数据。
_Avoid_: BrowserAudioChunk、CandidateAnswer、PermanentRecording

**EvidenceMediaGarbageCollection**:
按持久 capture revision 识别并物理删除已放弃 EvidenceMediaSegment 的周期隐私清理；对象删除成功后才删除 segment 事实并 tombstone FileObject，重复执行不重复删除当前 revision。
_Avoid_: AudioRepair、DatabaseVacuum、BestEffortTempCleanup

**StreamingSTTSession**:
由 ModelGateway 打开的服务端流式识别会话，拥有音频序号、partial/final 校验和断流后 batch 修复语义；每个会话只能提交一个权威 final。
_Avoid_: BrowserRecognition、WebSocketTranscript、ProviderSocket

**RoleSafeAgentReplay**:
AgentEvent 的有界共享历史，只保存按事件类型 allow-list 收敛的最小载荷；最新 snapshot、接管/媒体特权字段和内部能力点必须从当前领域状态按角色重新投影，不能跨角色复用。
_Avoid_: WebSocketBacklog、PrivilegedSnapshotCache、RawRedisEvent

**AnswerEvaluation**:
基于 CandidateAnswer 的指定 transcript revision 与 InterviewQuestionSnapshot 形成的不可变评分 revision；重评不覆盖旧评分。
_Avoid_: CurrentScore、OverwrittenEvaluation

**InterviewReport**:
基于 InterviewPlanSnapshot 和生成时每个答案的当前 AnswerEvaluation revision 形成的不可变岗位匹配报告；历史评分只属于历史报告，它提供证据但不代表企业录用决定。
_Avoid_: HiringDecision、MutableReport、AutoReject

**ModelInvocation**:
一次为特定业务 purpose 和能力发起的逻辑模型调用；可以按同一 ModelRoute 产生多次 Provider 尝试，但只形成一个最终成功或统一失败。
_Avoid_: ModelCall、ProviderRequest、SDKCall

**ProviderPluginDefinition**:
安装期的厂商插件声明，负责定义连接、凭证和各模型类型的动态表单，以及运行时 adapter；它不保存组织数据。
_Avoid_: ProviderConfig、FrontendVendorForm、PluginInstance

**ProviderConnection**:
一个组织到模型厂商或兼容网关的连接，只保存 API Key 引用、Base URL、区域等连接级信息，不选择具体模型。
_Avoid_: ModelProviderConfig、ModelCredential、ProviderModel

**ModelConfiguration**:
基于一个 ProviderConnection 配置的具体模型，拥有模型类型、厂商模型标识、厂商专属设置、统一默认参数、支持能力和健康状态。
_Avoid_: ModelName、RouteTarget、ProviderConfig

**ModelRoute**:
组织在特定 capability 与 purpose 下对 ModelConfiguration 的主备选择及调用策略，不复制厂商连接或模型参数。
_Avoid_: ProviderRoute、ModelAlias、DefaultModel

**DurableWorkItem**:
数据库作为真相来源的异步工作项，拥有幂等键、租约、退避、最大尝试、dead-letter 和人工重放事实。
_Avoid_: BackgroundTask、FireAndForgetJob、QueueMessage

**AuditEvent**:
对鉴权、敏感访问、导出、人工修正、供应商和运维动作形成的元数据级不可变审计事实；不得包含 URL token、联系方式或正文载荷。
_Avoid_: DebugLog、RequestBodyLog、MutableHistory
