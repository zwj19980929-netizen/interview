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

**ResumeReview**:
一个 ResumeDocument 面向一个 JobPosition 和 RoleRequirement version 的异步 AI 审阅，产出可追溯项目/技能证据与 ExperienceQuestion 草稿，不直接作出录用决定。
_Avoid_: ResumeScore、HiringDecision、GenericSummary

**ExperienceQuestion**:
ResumeReview 依据候选人项目证据生成、经面试官批准后用于核验过往经历的问题。
_Avoid_: FollowUp、ResumeGuess、AutoApprovedQuestion

**InterviewPlanAssembly**:
依据岗位要求、岗位题库候选池和 ResumeReview 形成候选人专属 InterviewPlan 草稿的结果，包含抽题槽位、冻结候选池、经历问题、覆盖、权重、时长与告警。
_Avoid_: TopNQuestions、SearchResult、GeneratedList

**InterviewAppointment**:
绑定候选人、岗位、岗位题库、已批准计划和时间窗的预约与一次性邀请；通过 readiness gate 后才能邀请候选人。
_Avoid_: InterviewSession、CalendarEvent、JoinLink

**CandidateIntake**:
候选人通过一次性邀请提交姓名、邮箱、手机号和授权形成的登记记录，用于和该预约绑定的 CandidateProfile 精确匹配。
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

**InterviewQuestionSnapshot**:
问题被选入 InterviewSession 时冻结的朗读与评分依据，可来源于岗位 Question 或 ExperienceQuestion；来源后续修改不影响快照。
_Avoid_: QuestionCopy、CurrentQuestion

**InterviewPlanSnapshot**:
InterviewSession 创建时从已批准 InterviewPlan 冻结的岗位、题库版本、候选池哈希、抽题槽位、经历问题、权重与阶段顺序。
_Avoid_: PlanCopy、CurrentPlan

**CandidateAnswer**:
某一轮候选人回答及其音频与权威服务端 final transcript；partial 或浏览器识别不能成为生产评分输入。
_Avoid_: BrowserTranscript、PartialAnswer、ClientScoreInput

**AnswerEvaluation**:
基于 CandidateAnswer 的指定 transcript revision 与 InterviewQuestionSnapshot 形成的不可变评分 revision；重评不覆盖旧评分。
_Avoid_: CurrentScore、OverwrittenEvaluation

**InterviewReport**:
基于 InterviewPlanSnapshot 和一组 AnswerEvaluation revision 形成的不可变岗位匹配报告；它提供证据，不代表企业录用决定。
_Avoid_: HiringDecision、MutableReport、AutoReject

**ModelInvocation**:
一次为特定业务 purpose 和能力发起的逻辑模型调用；可以按同一 ModelRoute 产生多次 Provider 尝试，但只形成一个最终成功或统一失败。
_Avoid_: ModelCall、ProviderRequest、SDKCall
