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
由题目版本和 KnowledgeBaseSpeechProfile revision 确定、异步生成的不可变读题语音，供数字人或音频降级路径读取。
_Avoid_: BrowserSpeech、TemporaryTTS、AudioURL

**AvatarDelivery**:
按 InterviewAppointment 冻结的 `avatar_mode` 把当前轮次题干交付给候选人的统一边界；LocalAvatarDelivery 复用 QuestionSpeechAsset 和浏览器形象，CloudAvatarDelivery 复用模型路由与 WebRTC/SFU，两者共享响应与播放/关闭合同。
_Avoid_: AvatarProviderSwitch、ReactAudioFallback、TencentService

**RealtimeSpeechDialogue**:
面试实时表达轨的供应商无关语音到语音流；它与权威 STT 并行接收候选人 PCM，只能逐字播报业务策略批准的追问，输出音频和 transcript 不构成评分证据。
_Avoid_: ScoringS2S、ProviderConversation、AudioTruth

**FollowUpTurn**:
针对一个根 InterviewTurn 缺失关键点形成的深度 1、权重 0 澄清子轮次；有严格的每题/全场/时间预算，不能递归，也不能改变批准计划分值。
_Avoid_: NewPlanQuestion、LLMFreeQuestion、SecondScore

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
绑定 CandidateProfile，由符合资格后的独立生成工作或面试官基于该候选人简历证据创建，经批准后用于核验过往经历的问题；没有证据快照或题干未点名证据标签的题不得展示或组卷。
_Avoid_: FollowUp、ResumeGuess、AutoApprovedQuestion、PersonalQuestion

**InterviewPlanAssembly**:
依据岗位要求、岗位题库候选池和 ResumeReview 形成候选人专属 InterviewPlan 草稿的结果，包含抽题槽位、冻结候选池、经历问题、覆盖、权重、时长与告警。
_Avoid_: TopNQuestions、SearchResult、GeneratedList

**InterviewPlan**:
面向一个候选人和岗位的唯一可执行面试定义，以抽题槽位、冻结候选池、经历问题、权重和阶段顺序为领域真相；批准后不可原地修改。
_Avoid_: FixedQuestionList、EditableItems、DualPlanRepresentation

**InterviewAppointment**:
绑定候选人、岗位、岗位题库、已批准计划、时间窗和 `local/cloud` AvatarDelivery 策略的预约与一次性邀请；只有通过 readiness gate 且位于允许的 start 窗口内才能消费并创建 InterviewSession。
_Avoid_: InterviewSession、CalendarEvent、JoinLink

**CandidateIntake**:
候选人通过一次性邀请提交姓名、邮箱、手机号、明确隐私同意和所需录音同意形成的登记记录，用于和该预约绑定的 CandidateProfile 精确匹配。
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

**StreamingSTTSession**:
由 ModelGateway 打开的服务端流式识别会话，拥有音频序号、partial/final 校验和断流后 batch 修复语义；每个会话只能提交一个权威 final。
_Avoid_: BrowserRecognition、WebSocketTranscript、ProviderSocket

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
