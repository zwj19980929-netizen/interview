# 检索与评分设计

本文描述岗位题库候选池、简历审阅与经历问题、计划装配、面试中随机选择、服务端转写、逐题 AI 评分和最终岗位匹配报告的算法边界。MVP 的抽题与评分都不依赖向量数据库。

## 岗位题库入库、候选池与题目语音

题目上传后进入持久异步构建流水线：

1. 验证 `KnowledgeBase` 属于目标 `JobPosition`，并校验题干、标准答案、关键点、rubric、技能和难度。
2. 规范化技能标签和语言；内容相同的导入通过文件/条目哈希幂等处理。
3. 校验技能、难度、题型和状态等结构化抽题字段；缺失时进入人工补全，或由 `llm.chat_json` 生成建议标签后由面试官确认。
4. 校验标准答案、关键点权重和 rubric，确保 AI 评分输入完整。
5. 通过 `tts.synthesize` 为 `question_text + language + voice_profile_id` 生成 `QuestionSpeechAsset`。
6. 分别更新 `validation_status` 和 `speech_status`，汇总题库构建状态。

只有 `active + valid + speech_ready` 且评分依据完整的题目进入正式候选池。服务端即时 TTS 只用于面试期间语音资产无法播放时的受控降级，不能把缺少预生成语音的题库标为 `ready`。校验或语音生成失败不影响题目草稿保存，但会阻止题库进入 `ready`。

题干、语言或音色变更必须新建语音资产，不能覆盖历史资产。内容哈希相同的资产可安全复用。

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

Resume Review 是指定 `ResumeDocument` 面向指定 `JobPosition + RoleRequirement version` 的异步任务：

1. 解析简历文件，去除页眉页脚、照片和与能力评估无关的敏感字段。
2. 抽取项目、时间范围、候选人声称的职责、技术选择、量化结果和对应原文位置。
3. 将项目证据映射到岗位技能维度，区分“简历明确写出”“模型推断”和“信息不足”。
4. 为最相关项目生成经历核验问题，覆盖本人职责、技术权衡、困难、结果验证和复盘。
5. 输出严格 JSON，保存 model/prompt/review revision；不得生成录用结论。
6. 问题默认 `draft`，面试官可编辑、拒绝或批准。批准后异步生成问题语音。

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

## 岗位题候选池筛选

计划装配用关系库字段筛选题目，不进行向量召回：

```text
organization_id = current_organization
job_position_id = plan.job_position_id
knowledge_base_id IN plan.knowledge_base_ids
question_version IN approved_candidate_manifest
status = active
validation_status = valid
speech_status = ready
skills overlaps slot.skills
difficulty IN slot.allowed_difficulties
type IN slot.allowed_types
```

筛选必须在数据库查询中强制组织、岗位和题库边界，不能先查全组织再在应用层丢弃。结构化排序信号可以包括：

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
4. 冻结所有题库版本、候选清单和集合哈希，供面试中的随机选择使用。
5. 把面试官已批准的 `ExperienceQuestion` 固定在 `position_bank` 槽位之后。
6. 题目权重与经历问题权重用万分单位最大余数法归一，精确合计为 1；预计时长必须守恒。
7. 候选池不足、覆盖不足或放宽去重/难度时写入 `assembly_summary.warnings`，不能伪造完整覆盖。

计划批准前必须满足：题库和候选清单 ready、Resume Review ready、经历问题已批准且语音 ready、题目/经历问题总数与时长合法。计划批准后这些来源版本不可变；需要调整时复制新草稿。

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

- 岗位题优先播放 `InterviewQuestionSnapshot.speech_asset_id` 指向的预生成语音。
- 经历问题在批准后预生成语音，计划 readiness 要求资产可用。
- 数字人视频层可根据相同音频驱动口型；视频供应商失败时仍播放语音资产。
- 只有预约明确允许时才能在语音资产失败后即时调用 `tts.synthesize`，该次调用和实际朗读文本必须审计。
- 实际朗读题干来自冻结快照，客户端不能提交任意文本让数字人读取。

## 服务端语音识别

生产回答链路固定为：

```text
candidate audio -> realtime gateway -> stt.streaming
-> transcript.partial/final -> CandidateAnswer -> evaluation
```

- partial 只用于界面显示，不持久化为权威答案，也不评分。
- final 必须来自服务端 Provider，包含 text、language、confidence、segments/timestamps 和 provider metadata。
- 浏览器 SpeechRecognition 只能本地开发预览，服务器忽略客户端提交的 final 和置信度。
- 流式 STT 失败时先保存完整音频，将轮次保持 `transcribing` 并排队 `stt.batch`；补转写 final 到达后再评分。
- 不同 Provider 的 partial 不能拼接。fallback 只能在流会话边界重开，或用 batch 对完整音频修复。
- 预约允许文本兜底时，答案标记 `manual_text_fallback` 并强制进入报告复核提示；它不是 STT。
- `stt_confidence` 低于语言/Provider 校准阈值时可继续评分，但评分置信度设上限并进入人工复核。

回答结束由候选人提交、服务端静音检测、最长时限或面试官结束触发。服务端必须在音频 flush 完成后等待 final；不能在 `candidate.media.stop` 到达时直接拿客户端文本评分。

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

## 质量与公平性评估

- 每个岗位维护人工标注的题目、答案和经历问题评分样本。
- 比较 AI 与人工评分一致性，按题型、语言、STT 置信度和 Provider 监控漂移。
- 检验随机抽题后不同候选人题目难度和覆盖分布是否可比。
- 监控 Resume Review 的证据引用准确率和幻觉率。
- 对低置信度、经历矛盾和企业改分样本进行复盘，更新 rubric；不把人员最终录用结果直接当作无偏训练标签。
