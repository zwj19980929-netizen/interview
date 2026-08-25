# Interviewer Domain

本领域描述从岗位题库与候选人简历形成计划和预约，执行语音面试并形成可复核岗位评价的核心概念。

## Language

**JobPosition**:
企业可复用的岗位，是 KnowledgeBase、RoleRequirement、ResumeReview、InterviewPlan 和 InterviewAppointment 的共同上层边界。
_Avoid_: Role、JobRequirement、PositionText

**KnowledgeBase**:
只属于一个 JobPosition 的知识库式题库，包含 Question、评分依据、索引与读题语音构建状态；一个岗位可以有多个 KnowledgeBase。
_Avoid_: GlobalQuestionPool、QuestionList、SharedBank

**QuestionSpeechAsset**:
由题目版本、语言和音色确定、异步生成的不可变读题语音，供数字人或音频降级路径读取。
_Avoid_: BrowserSpeech、TemporaryTTS、AudioURL

**CandidateProfile**:
企业上传到组织简历库、可参与多次面试的候选人记录，拥有联系方式和 ResumeDocument 版本；它不是某次面试的冻结快照。
_Avoid_: InterviewCandidate、ApplicantForm、ResumeRow

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
一个 ResumeDocument 面向一个 JobPosition 和 RoleRequirement version 的异步 AI 审阅，产出可追溯项目/技能证据与 ExperienceQuestion 草稿，不直接作出录用决定。
_Avoid_: ResumeScore、HiringDecision、GenericSummary

**ExperienceQuestion**:
ResumeReview 依据候选人项目证据生成、经面试官批准后用于核验过往经历的问题。
_Avoid_: FollowUp、ResumeGuess、AutoApprovedQuestion

**InterviewPlanAssembly**:
依据岗位要求、岗位题库候选池和 ResumeReview 形成候选人专属 InterviewPlan 草稿的结果，包含抽题槽位、冻结候选池、经历问题、覆盖、权重、时长与告警。
_Avoid_: TopNQuestions、SearchResult、GeneratedList

**InterviewPlan**:
面向一个候选人和岗位的唯一可执行面试定义，以抽题槽位、冻结候选池、经历问题、权重和阶段顺序为领域真相；批准后不可原地修改。
_Avoid_: FixedQuestionList、EditableItems、DualPlanRepresentation

**InterviewAppointment**:
绑定候选人、岗位、岗位题库、已批准计划和时间窗的预约与一次性邀请；只有通过 readiness gate 且位于允许的 start 窗口内才能消费并创建 InterviewSession。
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

**DurableWorkItem**:
数据库作为真相来源的异步工作项，拥有幂等键、租约、退避、最大尝试、dead-letter 和人工重放事实。
_Avoid_: BackgroundTask、FireAndForgetJob、QueueMessage

**AuditEvent**:
对鉴权、敏感访问、导出、人工修正、供应商和运维动作形成的元数据级不可变审计事实；不得包含 URL token、联系方式或正文载荷。
_Avoid_: DebugLog、RequestBodyLog、MutableHistory
