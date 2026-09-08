# 检索与评分设计

## 明确不作答与评分（023）

理解v6/v7、组合决策v5/v6增加answer_declined/next：候选人已经明确结束本题、完整原文没有实质回答时，保留客观摘要和原文证据，claims/covered为空、missing完整，不因知识缺失将理解置信度降为“没听清”。候选人仍在思考或提出未解决的转写争议时不能据此结束；技术回答后附带结束语仍为answer，不能丢弃已经表达的知识。

评分使用declined_answer.v1规则，从持久化会话重新核验服务端答案、understanding_id、utterance_id、原始文本、音频和理解合同；传入的intent标签不能授权0分。全组均为明确未作答时，正常生成0分评分revision，列出全部缺失关键点，技术证据/错误主张为空，记录规则及理解来源，不让LLM补造技术表现。主回答有内容、追问未作答时，保留主回答的评分输入；若其他轮有技术响应，则只评分这些实际内容。证据组原始答案/发言引用仍保留审计，全部未作答的正式题继续参与既有权重汇总，不以skip移除分母。公平性复核和人类最终决策维持原有流程。

## 技术术语与转写争议（019）

理解 v4/v5、组合决策 v3/v4 和评分 answer_evaluation.v2 明确区分可由上下文唯一理解的误拼（例如 redios/Redis 缓存）、真实不同术语（RADIUS 认证）与未解决的转写争议。保留原文及 E 编号引用；明确撤回或纠正的旧话语不能作为当前主张，不用标准答案补造候选人知识。服务端对理解结果 ambiguities 执行澄清门禁，即使模型同时提议 next/followup 也不得推进；评分存在未解决歧义时要求 transcription_ambiguity 复核标记。模型成功不等于字词准确，本场事故 answers=0，没有产生评分。

## 018：确认与评分之间的边界

补充意图与完整答案的版本化Prompt/严格Schema保持016版本；已确认回复final直接用于原有理解与受控后续动作，减少重复切流。音频活动检测只保护准备/提交时序，不参与专业能力评分、不生成文本、不决定录用。新ASR假设即使低于声学检测阈值也须保留并撤销旧提案；转写缺失不能通过沿用旧评分证据绕过。

## 口头完成确认与评分证据（016）

5秒静音只触发询问是否补充。独立supplement_reply.v1将本次服务端final答复分类为continue/finish/supplement/pause/unclear；网关校验类型、枚举、数量/长度边界、禁止额外字段及非空内容，业务再校验逐字证据和置信度。ASR回复置信度不足0.65或意图置信度不足0.75只澄清；“嗯/好的/可以”不能单独判为finish。只有当前capture明确完成后，完整转写进入v3/v2理解合同，先前“尚未说完”的控制表态不使累积回答永久阻塞，控制对话不能充当能力主张。评分、rubric、抽题、报告和原文引用门禁不变，暖场保留原2.5秒自动收口。

## 015：非关闭预计算

正式自动轮次可使用已校验稳定句段预览作可撤销准备，不为准备而结束识别；它不是权威Utterance或评分输入。仍复用既有版本化Prompt、严格响应校验与全部追问门禁，不新增词库/关键词评分；提交前真实final及冻结上下文必须匹配，否则重新准备或继续听。最终评分/检索/rubric/权重/答案唯一性均不变。

## 自动轮次准备（INTERRUPTIBLE-AUTOMATIC-TURNS-012）

本地音频 EOT 只提议何时准备理解，不做能力评分或判断录用。正式累计服务端 final 进入 `interview_turn_decision.v1`，一次返回 understanding 与 followup，先由网关校验完整嵌套 schema，再精确还原 E/P 引用并执行原文证据、能力分区、敏感性、泄题、难度和追问次数/深度/时长预算检查；不通过不形成领域答案。无追问预算/低 STT 置信度等分支仍用单次既有 understanding，确定控制意图可零调用。准备可撤销，只有重新核验 final 与冻结上下文后才落库；评分仍为答案提交后的 Outbox 异步任务，rubric/权重/抽题与报告规则未改变。

本文描述岗位题库候选池、简历审阅与经历问题、计划装配、面试中随机选择、服务端转写、逐题 AI 评分和最终岗位匹配报告的算法边界。MVP 的抽题与评分都不依赖向量数据库。

当前实现说明：岗位题库路径和公开 `/questions/search` 已共用结构化 Question Catalog，Memory/SQLite/PostgreSQL backend 均有租户、岗位、题库、状态/readiness 和结构化条件查询实现，不依赖 embedding。`QuestionSelection` 采用 `HMAC-SHA256(session_seed, slot + question + version)` 排序选出稳定结果，而不是数据库 `ORDER BY random()`。服务端已实现 `stt.streaming` 的 open/chunk/partial/唯一 final 协议、权威 final 提交及断流 `stt.batch` 修复；本地 mock 需要显式开发输入，生产 readiness 只接受非 mock 且近期健康的 route。单题评分传入冻结 rubric、标准答案、关键点、岗位要求和服务端 final，并区分岗位题与经历题 profile；报告和 JSON/CSV 导出的分数、证据、风险与 `manual_review` 只读取各答案当前评分 revision。验证证据见 [已知问题与修复设计](known-issues-and-remediation.md)。

## 岗位题库入库、候选池与题目语音

题目上传后进入持久异步构建流水线：

1. 验证 `KnowledgeBase` 属于当前组织且已关联到目标 `JobPosition`，并校验题干、标准答案、关键点、rubric、技能和难度。
2. 规范化技能标签和语言；内容相同的导入通过文件/条目哈希幂等处理。
3. 校验技能、难度、题型和状态等结构化抽题字段；缺失时进入人工补全，或由 `llm.chat_json` 生成建议标签后由面试官确认。
4. 校验标准答案、关键点权重和 rubric，确保 AI 评分输入完整。
5. 通过题库 KnowledgeBaseSpeechProfile 冻结的 `model_configuration_id + voice_profile_id + language + format + speaking_rate` 调用 `tts.synthesize`，为每个活动题生成 `QuestionSpeechAsset`。
6. 分别更新 `validation_status` 和 `speech_status`，汇总题库构建状态。

只有 `active + valid + speech_ready` 且评分依据完整的题目进入正式候选池。服务端即时 TTS 只用于面试期间语音资产无法播放时的受控降级，不能把缺少预生成语音的题库标为 `ready`。校验或语音生成失败不影响题目草稿保存，但会阻止题库进入 `ready`。

题干或 KnowledgeBaseSpeechProfile revision 变更必须新建语音资产，不能覆盖历史资产。切换模型或声音会创建整库 KnowledgeBaseSpeechBuild，父工作项冻结活动题 ID/version manifest，再由 Celery fan-out 为每题执行独立工作项；内容哈希相同且模型配置版本/声音/输出参数完全一致的资产可安全复用。

批量重建遵循以下顺序：

1. API 事务以 `expected_version` 更新题库 speech profile、增加 revision、把题库从 `ready` 置为 `building`，并写入唯一 DurableWorkItem；HTTP 返回 `202`。
2. `app/workers/knowledge_base_speech.py` 的 Celery task 领取父工作项，冻结活动题 manifest 并分批创建 `question.speech.generate` 子工作项，不在 Web 进程循环调用 TTS。
3. 题目 worker 在数据库事务外调用 ModelGateway，使用 profile 指定的具体 TTS ModelConfiguration，不回退到另一个未声明模型。
4. 提交结果前重新读取 Question version 和题库 speech profile revision；任一已变化时把子工作标为 `superseded`，保留不可变资产但不更新当前指针。
5. 父工作项聚合 `ready/failed/superseded`。只有当前 revision 全部活动题 ready 才恢复题库 `ready`；失败项可单独重试。

Celery delivery 可以重复，数据库幂等键、租约和内容哈希必须使重复执行得到同一当前结果。Celery broker/result backend 不是题库构建真相。

## 岗位要求解析

`RoleRequirement` 属于稳定的 `JobPosition`，一版解析画像示例：

```json
{
  "skill_weights": {
    "python": 0.25,
    "database": 0.2,
    "redis": 0.15,
    "system_design": 0.2,
    "debugging": 0.2
  },
  "seniority": "senior",
  "business_scenarios": ["高并发服务", "线上故障排查"],
  "avoid_topics": ["与岗位无关的前端细节"],
  "target_difficulty": "senior"
}
```

规则和 `llm.chat_json` 可以共同完成解析；模型输出经 schema 校验，失败回退到规则结果。岗位要求不能把学校、年龄、性别等非工作能力属性变成评分维度。

## 简历审阅与经历问题生成

Resume Review 只能读取已完成摄取和解析的 `ResumeDocument`。本地 PDF 上传与 PDF URL 导入必须先收敛成同一种系统托管文件，再面向指定 `JobPosition + RoleRequirement version` 执行异步任务：

1. 摄取本地上传流或受控下载的公开 HTTPS URL，计算 SHA-256，并校验大小、`application/pdf` 和 PDF 文件签名。
2. 文件先进入隔离区完成恶意文件扫描，再写入系统私有存储；外部 URL 不能成为后续解析和审阅的长期真相来源。
3. 解析 PDF 并以页分隔符保存全部可提取文本；调用模型前去除联系方式及照片、性别、年龄、婚育等无关字段。
4. 估算脱敏文本 Token：预算内使用 `resume_review.v6` 单次审阅；超预算则按页贪心组块，单页仍超限时再按段落/字符安全切分，任何文本都不能静默截断。单次审阅若以 `provider_output_truncated` 结束，丢弃未完整输出并自动切换到完整 Map/Reduce，不使用相同参数盲目重试。
5. 长简历的每个 `ResumeEvidenceChunk` 通过 `resume_evidence_map.v1` 只抽取项目、职责、技能、量化结果和来源页，不允许输出岗位符合性；分块在受控并发数内执行。
6. 合并重复证据；若证据本身超过 Reduce 预算，用 `resume_evidence_compaction.v1` 分层压缩并保留来源页。达到最大压缩轮次仍超限则结构化失败，不截断。
7. 只有全部 Map 成功后，`resume_review_reduce.v4` 才把完整规范化证据映射到岗位能力，并形成 `qualified/unqualified/manual_review`、0–100 辅助分、命中要求和缺口；Resume Review 本身不再返回问题。
8. 生效结论为 `qualified` 时才创建 `resume.experience_questions.generate` 工作，使用 `resume_experience_question_generation.v1` 和该审阅的项目/技能证据生成 1–3 个问题；AI 不符合/待复核不调用该模型，人工改判符合时才排队。
9. 每题 `evidence_refs` 必须精确引用输入证据标签，题干必须点名至少一个标签，服务端再解析为不可变证据快照；任一引用未知、缺少证据或题干不点名均整体失败，不保存通用技术题。

模型输入预算与结构化输出预算必须分开配置。默认单次审阅/最终 Reduce 输出上限为 6000 tokens，Map/证据压缩为 4000 tokens，独立问题生成默认为 3000 tokens，可分别通过 `INTERVIEWER_RESUME_SINGLE_PASS_OUTPUT_TOKENS`、`INTERVIEWER_RESUME_MAP_OUTPUT_TOKENS`、`INTERVIEWER_RESUME_COMPACTION_OUTPUT_TOKENS`、`INTERVIEWER_RESUME_REDUCE_OUTPUT_TOKENS`、`INTERVIEWER_RESUME_QUESTION_OUTPUT_TOKENS` 调整。审阅最终 Schema 只限制摘要、证据和岗位要求；问题生成 Schema 独立限制 1–3 题、核验点与引用。Provider 以 `finish_reason=length` 截断或返回空正文时必须结构化失败，不得保存半截 JSON 或部分结果。

初筛仅比较简历中的明确能力证据与岗位要求。模型负责生成 0–100 匹配分和证据，服务端在写入 CandidateScreening 前用版本化策略强制归一化建议：0–59 为 `unqualified`，60–74 为 `manual_review`，75–100 为 `qualified`；模型建议与分数冲突时以该映射为准。生效结论为人工复核优先、归一化 AI 建议次之；候选人列表同时展示两者及证据，分数带只形成筛选建议，不构成自动录用决定。同一候选人在不同岗位的最新审阅分别计算；只有所有最新生效结论均为 `unqualified` 时才设置 7 天留存期限，任何符合、待复核或处理中结论都会取消该初筛期限。周期 worker 先用当前策略校正存量审阅对应的期限；首次命中从本次校正时间起完整保留 7 天，不追溯立即删除，之后再由 RetentionService 到期清理并审计。

经历问题示例：

```json
{
  "question_text": "你在订单系统重构中具体负责哪一部分？请说明一次容量或一致性方面的技术取舍。",
  "project_ref": {
    "project": "订单系统重构",
    "evidence_span": "负责核心交易链路重构……"
  },
  "evaluation_focus": ["本人职责", "技术权衡", "结果证据"],
  "rubric": {
    "specificity": 0.35,
    "technical_depth": 0.35,
    "evidence_consistency": 0.2,
    "reflection": 0.1
  }
}
```

简历只提供提问上下文，不能把简历中声称的成果直接当作候选人已证明的能力。经历问题评分以面试回答中的具体证据为主，并把与简历的矛盾标为“待人工核验”，不能直接判定不诚信。

`CandidateQuestionBank` 是按 `candidate_profile_id` 聚合 `ExperienceQuestion` 的读取与管理投影，不另建一套 Question 真相。它只对生效结论 `qualified` 开放，并在候选人列表通过独立“简历问答”弹窗进入。AI 生成项记录 `source_type=ai_generated`；面试官可基于符合审阅人工创建 `source_type=manual` 草稿，必须选择简历证据，题干也必须写出所选项目/技能名称。人工与 AI 题共用版本化编辑、批准/拒绝、计划冻结和 `resume_experience.v1` 评分。归档题、无有效证据快照的旧题以及不符合审阅的问题从读取和新计划中消失，但已批准计划与历史面试继续读取冻结快照。人工题也不能绕过审核：`approved + grounded evidence` 即可进入计划，批准时不生成语音。

## 岗位题候选池筛选

计划装配用关系库字段筛选题目，不进行向量召回：

```text
organization_id = current_organization
knowledge_base_id is assigned to plan.job_position_id
knowledge_base_id IN plan.knowledge_base_ids
question_version IN approved_candidate_manifest
status = active
validation_status = valid
speech_status = ready
skills overlaps slot.skills
difficulty IN slot.allowed_difficulties
type IN slot.allowed_types
```

服务层先在同一租户事务中验证岗位—题库关联，数据库查询再强制组织、题库、活动状态和 readiness 边界；不能先查全组织题目再在应用层丢弃。结构化排序信号可以包括：

- 技能标签与槽位覆盖目标的重合。
- 难度与岗位级别适配。
- 题型与计划阶段适配。
- 关键点多样性与历史已问题目惩罚。
- 历史质量：数据足够后才使用题目区分度和评分稳定性。

计划装配使用的优先级示例：

```text
candidate_priority =
  0.45 * skill_overlap +
  0.30 * difficulty_fit +
  0.20 * type_fit +
  0.05 * quality_score
```

MVP 无质量数据时去掉 `quality_score` 并归一化。这个优先级只用来形成合格候选集合或加权随机分布，不把最高分题固定选入每场面试。

后台题库页面仍可用 PostgreSQL 全文/关键词搜索帮助面试官查题。只有当题量大到结构化标签和全文搜索无法满足题库治理时，才考虑可选 embedding 索引；它不进入正式面试运行依赖。

## 面试计划装配

Interview Plan Assembly 接收岗位、岗位要求、岗位题库、候选人和 `ready` Resume Review，输出计划草稿而不是固定全部岗位题目：

1. 把必备和加分技能按默认 3:1 合并为覆盖权重。
2. 为 `position_question_count` 分配 `bank_slots`，每个槽位包含阶段、能力维度、允许难度、权重和预计时长。
3. 对每个槽位按岗位、题库、技能、难度、题型、状态和语音 readiness 过滤候选题，形成 `QuestionCandidatePool`；它保存筛选条件、题目 ID/version 和集合哈希，不把标准答案暴露到计划 API。
4. 冻结所有题库版本、岗位候选题的 QuestionSpeechAsset ID、候选清单和集合哈希；所选题库的 KnowledgeBaseSpeechProfile 输出参数必须完全一致，并冻结为计划级 `speech_profile_snapshot`。
5. 把面试官已批准的 `ExperienceQuestion` 文本/证据/评分版本固定在 `position_bank` 槽位之后，语音资产留空并标记 `deferred`。
6. 题目权重与经历问题权重用万分单位最大余数法归一，精确合计为 1；预计时长必须守恒。
7. 候选池不足、覆盖不足或放宽去重/难度时写入 `assembly_summary.warnings`，不能伪造完整覆盖。

计划批准前必须满足：题库和候选清单 ready、Resume Review ready、经历问题已批准且证据有效、题目/经历问题总数与时长合法，并且多个题库没有 speech profile 冲突。计划批准后这些来源版本与语音特征不可变；需要调整时复制新草稿。

## 面试中可审计的随机检索

用户所需“随机检索”由 Question Selection deep module 实现，不等于数据库 `ORDER BY random()`：

1. `InterviewSession` 创建时生成密码学安全随机种子，并只保存种子或受控哈希。
2. 每个 `bank_slot` 只从批准计划冻结的候选集合中选择，排除已问题目和超过重复阈值的关键点。
3. 先按覆盖、难度和去重约束形成合格集合，再用 `HMAC(session_seed, slot_id)` 派生的稳定随机值在合格集合内加权抽样。
4. 选择后原子保存 `QuestionSelection`、`InterviewQuestionSnapshot` 和生命周期事件，再播放题目语音。
5. 同一 `(interview_id, slot_id)` 只能存在一个选择。断线、重试或 worker 接管必须复用已有结果。
6. 若主集合为空，按批准计划的补位层级放宽约束，并把放宽项写入选择原因；所有补位都失败时暂停面试并请求人工处理。

这样既能让不同候选人的题目有随机性，又能保证岗位范围、覆盖、难度、公平性和历史审计。初版不根据候选人前一题得分自适应提高/降低后续难度，避免不同人受到不可解释的测试条件；自适应策略必须经过单独公平性验证后再启用。

## 题目朗读

- 岗位题优先播放 `InterviewQuestionSnapshot.speech_asset_id` 指向的预生成语音；该资产已冻结题库 speech profile revision 和实际 TTS 模型/声音。
- 经历问题批准时不生成语音。候选人完成 Candidate Intake、预约进入 `registered` 的同一事务按预约和题目版本排队，使用计划冻结的 TTS 模型、音色、语言、格式和语速；匹配资产可复用。
- 邀请 readiness 只要求计划有可执行的冻结 profile；start readiness 要求本预约所有经历题资产 ready 且来源版本和 profile 精确匹配。TTS 失败保留 `registered`，阻止 start 并允许重试；取消预约取消未完成工作。
- 数字人视频层可根据相同音频驱动口型；视频供应商失败时仍播放语音资产。
- 只有预约明确允许时才能在语音资产失败后即时调用 `tts.synthesize`，该次调用和实际朗读文本必须审计。
- 实际朗读题干来自冻结快照，客户端不能提交任意文本让数字人读取。

## 服务端语音识别

生产回答链路固定为：

```text
candidate LiveKit microphone track -> receive-only server subscriber
-> private recording + InterviewEvidenceChain -> stt.streaming
-> transcript.partial/final -> CandidateAnswer -> evaluation
```

- partial 只用于界面显示，不持久化为权威答案，也不评分。
- final 必须来自服务端 Provider，包含 text、language、confidence、segments/timestamps 和 provider metadata。
- 浏览器 SpeechRecognition 只能本地开发预览，服务器忽略客户端提交的 final 和置信度。
- 流式 STT 失败时先保存完整音频，将轮次保持 `transcribing` 并排队 `stt.batch`；补转写 final 到达后再评分。
- 不同 Provider 的 partial 不能拼接。fallback 只能在流会话边界重开，或用 batch 对完整音频修复。
- 不提供客户端文本兜底；服务端 streaming 失败后只能用完整录音执行 `stt.batch` 修复。补转写仍失败时保持 `transcribing`/可恢复失败态并请求人工处理，不能伪造 CandidateAnswer。
- `stt_confidence` 低于语言/Provider 校准阈值时可继续评分，但评分置信度设上限并进入人工复核。
- 正式 Agent ticket 选择 `livekit_server_subscriber` 时，浏览器 PCM 不再复制到控制 WebSocket；服务端用冻结 candidate identity 精确订阅麦克风轨。该 subscriber、录音、StreamingSTTSession 和 endpoint timer 独立于控制连接，在 30 秒重连 grace 内连续工作；重复 publication/finish 或旧连接命令不得生成重复 CandidateAnswer。
- 正式 Evidence 链在建流时冻结 `EvidenceCommitFence`。STT 与 TurnUnderstanding 可在事务外运行，但形成非答案 utterance 或 CandidateAnswer 前必须在同一事务重新验证数据库 lease/epoch；旧 owner 的迟到 final 只可被拒绝，不得进入评分 Outbox。
- 每个 PCM 帧同时写入有界 `EvidenceMediaSegment`，只有已 seal、序号连续且 checksum 通过的 checkpoint 能由新 ownership epoch 重建为 batch repair 录音。内存中未 seal suffix 不得标记持久。
- 浏览器的 30 秒 AES-GCM 环形缓冲只用于服务端授权的精确 gap。ticket 冻结 source connection/audio epoch/2 MiB/32 KiB 上限；JSON `evidence.recovery.begin/chunk/complete` 中每帧先写私有 FileObject，journal 只引用 file ID/hash/epoch/sequence，由当前 owner 以 `ack_through` 去重后注入原 Evidence chain。它不是常态 WebSocket PCM 备用通道。

正式回答结束由候选人点击“回答完毕”明确确认；静音仅提示继续补充，不关闭 Evidence。暖场保留 2.5 秒可取消端点，资源最大时限和人工结束边界保持不变。服务端必须在音频 flush 与私有录音持久化完成后等待 final；不能在客户端 `speech.stopped/evidence.finish` 到达时直接拿浏览器文本评分。

## 受控澄清追问与低延迟双轨

正式追问不等待 AnswerEvaluation，也不让 S2S 模型自行决定问什么，统一使用 `TurnUnderstanding -> controlled_followup` 两步合同；追问子轮次保存 parent/root、目标能力点和证据来源用于企业审计，候选人投影只返回安全题干与父子关系。旧深度 1 模板 runtime 及其候选人调用链已删除，不维护双策略。

`cascade` 模式使用 `权威 STT -> 追问策略 -> Avatar/TTS`；`s2s` 模式从录音开始就维持第二条 Realtime Speech Dialogue 流，但 Provider PCM delta 在有界内存中隔离，直到 final transcript 与已冻结 ApprovedConversationAct 逐字一致才复制成私有表达音频并下发。不一致时整段丢弃，断流或缺 route 只触发批准文本的 cascade 降级；两者都不能写 CandidateAnswer、改变关键点判定或给分。

CandidateAnswer、`answer.evaluate` DurableWorkItem 和生命周期事件在同一事务提交。HTTP/WebSocket 随即返回 `evaluation.status=pending` / `evaluation.queued`；完整 LLM 评分由 worker 执行，完成后才写 append-only AnswerEvaluation 并广播安全摘要。因此评分吞吐或模型抖动不会延长追问首包语音延迟，也不会丢失证据链。

REALTIME-AGENT-001 将追问提升为两步结构化合同。当前 `interview_turn_understanding.v2` 从服务端 final 的冻结原文片段 ID 和能力点 ID 生成意图、摘要、主张、证据引用、覆盖/缺失、歧义、矛盾、置信度和建议动作；统一 wire 检验、精确引用还原和完整分区/证据/claim 检查通过后才进入原有领域对象。历史 v1 保持可读。确定性中英文元意图优先识别重读、未说完、暂停和澄清，这些话语不进入 CandidateAnswer。低置信度不得自动提交或追问，只能澄清或重说。合同不合法最多重新生成一次（每次 20 秒），不使用非法响应做证据。Provider 不可用时形成 `UNDERSTANDING_PROVIDER_UNAVAILABLE`，两次合同失败形成 `UNDERSTANDING_RESULT_REJECTED`；只投影去敏 `UnderstandingProblem`，仍不持久原始模型输出、不让无效结果进入评分。

只有理解通过 Schema、原文证据和冻结能力点校验后，`controlled_followup.v1` 才能建议并持久化追问。gate 固定最多深度 2、每个 root 最多 2 次、全场最多 `min(4, 主问题数)`、剩余不足 90 秒停止新增，并校验难度、180 字长度、敏感属性、标准答案泄漏、暗示性正误评价和已追问能力点。Expression 必须精确复用带 root/depth/非空证据/能力点的冻结 act，找不到时失败关闭，不能临时补建。安全模型失败时只允许证据绑定的确定性 probe；无安全 probe 直接下一题，绝不自由聊天。

每个 follow-up 仍是零权重子轮次。评分输入按 `root_turn_id` 合并根回答与所有权威追问回答，保留各自音频/转写引用和逐字证据，产生新的 root evaluation revision；报告不得把追问当独立题加权。完整评分通过 Outbox 异步执行，下一对话动作不等待评分 worker。

离线 shadow 评估必须使用取得授权并脱敏的历史录音与人工标注，至少报告普通话技术语料 WER、英文技术实体召回、meta-intent macro F1、能力点覆盖 macro F1、无关追问率、泄题/敏感/超预算计数和 AI/人工评分一致性。运维 acceptance v2 runner 同时 gate 延迟、Avatar FPS/音画偏差/冻结、30 秒恢复无丢失/重复答案、未同意视频上行字节为 0、Chrome/Edge/Safari 桌面矩阵与试点问卷；报告必须带数据集 SHA-256、样本下限、时间、精确 deployment/release scope 和 HMAC 签名，30 天后失效。仓库只提供合同/合成样本和验收模块；真实金标、目标设备与试点仍为 `data_pending`，没有鲜活合格签名报告时不得声称达标。

## 岗位题逐题评分

用户提出的“把标准答案和面试者答案一起交给 AI”是正式评分主方案，但不能只问模型“语义是否相近”。评分输入读取 `InterviewQuestionSnapshot`、当前 `CandidateAnswer.final_transcript revision` 和冻结岗位要求，并同时提供：

- 题干，避免标准答案脱离问题语境。
- 标准答案和关键点/权重。
- rubric 与岗位能力维度。
- 候选人服务端最终转写。
- 输出 JSON schema，要求逐项给出命中点、缺失点、错误或矛盾陈述、回答证据和置信度。

向量余弦相似度不用于评分，因为否定句、关键词堆砌和概念相近但结论错误的答案也可能取得高相似度。最终分数由结构化 LLM 判断与规则校验共同形成：

```text
position_question_score =
  45% semantic_correctness +
  30% key_point_coverage +
  10% reasoning_depth +
  10% role_relevance +
   5% communication
```

题目快照有 rubric 时以其权重为准。输出必须包含 0-100 分、置信度、各维度分、命中/缺失关键点、回答证据片段与时间戳、错误陈述、摘要和复核标记。

```json
{
  "score": 88,
  "confidence": 0.79,
  "dimension_scores": {
    "semantic_correctness": 90,
    "key_point_coverage": 85,
    "reasoning_depth": 80,
    "role_relevance": 90,
    "communication": 85
  },
  "covered_key_points": [
    {
      "key_point_id": "kp_01J...",
      "evidence": "同一时刻只有一个线程执行 Python 字节码",
      "start_ms": 4200,
      "end_ms": 7800
    }
  ],
  "missing_key_points": [
    {"key_point_id": "kp_02J...", "reason": "未提到多进程替代方案"}
  ],
  "incorrect_claims": [],
  "summary": "覆盖核心概念，但替代方案说明不足",
  "review_flags": []
}
```

## 经历问题逐题评分

经历问题没有通用“标准答案”，使用冻结的 `project_evidence + evaluation_focus + rubric`：

```text
experience_question_score =
  35% specificity +
  35% technical_depth +
  20% evidence_consistency +
  10% reflection
```

- `specificity`：是否说明本人职责、上下文和具体行动。
- `technical_depth`：是否解释方案、边界和权衡。
- `evidence_consistency`：回答与简历项目事实是否一致；矛盾只生成复核标记。
- `reflection`：是否能说明结果验证、问题和改进。

评分不得因为公司名、学校、项目规模或简历写得华丽而加分，也不得把无法验证的推断当作事实。

## 模型评分约束与 revision

所有评分通过 `llm.chat_json`，响应经 JSON schema 校验。prompt 必须包含冻结题目、rubric、岗位要求摘要和服务端最终转写；禁止包含候选人姓名、邮箱、手机号、照片和受保护属性。

低置信度条件包括：转写过短、STT 低置信度、回答无关、经历证据冲突或模型 schema 不稳定。低置信度进入人工复核，不自动推断岗位不匹配。

`AnswerEvaluation` 为 append-only revision。补转写、人工修正转写、rubric 修订或模型版本变化只能新增评分，并原子更新当前指针；旧评分、旧转写和旧报告保留到留存期限。

## 总分与客观岗位匹配报告

报告使用 `InterviewPlanSnapshot` 冻结权重：

```text
overall_score =
  sum(current_evaluation.score * frozen_item.weight)
  / sum(valid_completed_weights)
```

- 候选人未回答：按计划策略计 0 或 `not_answered`。
- 面试官主动跳过：不计完成权重，并说明原因。
- 技术故障：`invalid_turn`，不计分并进入报告告警。
- 低 STT/评分置信度：分数可展示，但 `job_fit_level=manual_review` 或附显著复核标记。

报告不使用“录用/淘汰”作为 AI 结果，采用证据化等级：

| 等级 | 含义 |
| --- | --- |
| `strong_match` | 多数核心维度有高置信度证据且明显达到岗位阈值 |
| `match` | 主要要求达到，存在可接受的局部不足 |
| `partial_match` | 部分核心要求证据不足，需要补面或人工判断 |
| `insufficient_evidence` | 未完成、有效题量不足或无法形成可靠结论 |
| `manual_review` | STT、评分或经历一致性存在显著待复核项 |

`job_fit_evidence` 必须逐条关联岗位维度、题目、当前评分 revision 和回答证据。企业可回听语音、查看 final transcript、修正转写并重评；最终人员决定是独立业务动作，不能由报告自动写入。

## AI 题目创作不参与运行时检索

智能生题只扩展题库创作能力：输入题库定位、标签和面试官可选要求，先形成互斥 QuestionBlueprint，再由独立
子工作输出并合并为可审核 GeneratedQuestionDraft。规划层用 topic/scenario/focus 互斥键控制覆盖面；合并层对活动
正式题目和批次候选执行规范化完全匹配与高阈值文本相似去重，缺失槽位最多定向补生成两轮。该规则属于创作
治理，不替代运行时检索、embedding 或评分语义。生成阶段不能把草稿加入 Question Catalog、候选池、计划装配或
评分。只有人工确认导入且 Question 完成现有评分依据校验后，才取得正式 ID 并进入后续结构化候选池；因此
Question Selection、批准计划快照和实时评分算法无需新增“AI 题目”分支。

完整题目生成使用 `question_blueprint_generation.v2`：Schema 将标题、题干、标准答案、关键点、别名和技能的长度/
数量限制在可审核范围，单槽位输出预算为 4000 tokens、双槽位为 8000 tokens。Provider 明确报告
`finish_reason=length` 时不得保存半截 JSON，也不得在 Model Gateway 内原样重试；双槽位由创作工作流拆成两个单槽位
工作继续，单槽位再次截断才形成需要人工处理的失败项。该恢复只改变创作子任务，不改变正式题库检索或评分规则。

报告生成必须先从每个答案的 `current_evaluation_id` 物化 current evaluation 集合，`overall_score`、维度、证据、风险、`manual_review` 和 `evaluation_ids` 全部只从该集合计算。历史 revision 仅保留给引用它的历史报告，不能影响新报告。

## 质量与公平性评估

当前 `FairnessEvaluationService` 和 `/api/v1/admin/evaluations/question-selection-fairness` 已按岗位输出样本量、每会话题量、平均难度、技能覆盖计数和分布差异告警。`POST /api/v1/admin/evaluations/score-calibration` 另接收现有 current evaluation 的脱敏人工金标，计算 MAE、RMSE、平均有符号误差、±5/±10 一致率、线性校准候选，以及题型、语言、STT 质量和不透明 cohort 分层。两者都记录最小审计，不读取或生成录用结论；线性拟合永不自动写回生产分数。

校准样本不得携带姓名、邮箱、手机号、简历、音频或转写；API 的 `extra=forbid` 会拒绝这些字段。少于 30 条的整体样本、少于 10 条的 cohort 和 cohort MAE 差超过 5 分都会告警。仓库测试只验证计算和隐私边界，真实一致性/公平性结论必须等企业提供已脱敏且经人工复核的候选人金标；在此之前不能宣称已完成校准。

- 每个岗位维护人工标注的题目、答案和经历问题评分样本。
- 比较 AI 与人工评分一致性，按题型、语言、STT 置信度和 Provider 监控漂移。
- 检验随机抽题后不同候选人题目难度和覆盖分布是否可比。
- 监控 Resume Review 的证据引用准确率和幻觉率。
- 对低置信度、经历矛盾和企业改分样本进行复盘，更新 rubric；不把人员最终录用结果直接当作无偏训练标签。
