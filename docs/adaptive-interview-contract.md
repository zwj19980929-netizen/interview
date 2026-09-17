# Execution v3 自主考察合同

## 2026-09-14：短问题生成v2（048）

批次边界：每批只含一个原题的最多6个评分点；响应Schema的题目ID、知识点ID、能力与参考片段枚举只来自该题，unit数量等于该批实际点数。

新模型调用使用interview_inquiry_units.v2，最多6个知识点一批。原答案切成无丢失的连续编号片段，模型选首尾编号，经统一Schema及同源/顺序/长度校验还原standard_answer_quote。单元仍含相同原关键点范围、引用原文、稳定ID及内容哈希，presentation_policy仍为approved_inquiry_units.v1；历史冻结合同不重新生成或改写。跨批合并必须覆盖全部原评分点且不重复。

## 047：题库准备合同

由 prepare 创建的 v3 合同以 assessment_basis 冻结题库范围和岗位身份，role_requirement_id/version 为空；源问题、评分点、单元与合同哈希仍完整保留。能力只取实际单元支持的映射，自动预算使用每能力至少一个考察点，并按可用单元/时长/最低覆盖数确定上下界。旧 generate 合同仍保留岗位身份和显式预算。新入口自动通过生成/来源/语音检查后保存 approved，approval_source 表明其为自动准备，不表示用户人工审阅。


工作项：ENTERPRISE-INTERVIEWER-IMPLEMENTATION-038；可选上下文纠正：OPTIONAL-AGENT-CONTEXT-039。此文先于接口与领域实现登记合同，现已随代码更新。

## 请求与冻结

043：原题候选映射在付费单元生成前进行覆盖诊断。缺失正权重能力返回422 `ASSESSMENT_SOURCE_COVERAGE_MISSING`，details包含missing_competency_ids、missing_role_competency_ids、missing_requested_competency_ids。判断针对已经通过范围/版本校验的实际可用短名单；无模型调用、无计划写入。用户需要补齐相符题目/标签或调整对应要求，不能靠重试同一请求或仅补充Skill/公司资料绕过。已冻结合同验证、单元子集与评分范围规则不变。

`POST /api/v1/interview-plans/generate` 增加 `execution_schema_version: 2 | 3`，省略仍为 2；工作台自主面试显式发送 3并以approve:false生成草稿，展示实际单元/范围后通过现有PATCH与expected_version批准启用。v3 可传 `adaptive_policy`：`min_root_questions`、`max_root_questions`、`max_followups_per_root`、`max_total_followups`、`closing_reserve_seconds`、`min_evidence_units_per_competency`（默认2，范围1–5）。单个能力实际可用单元只有1个时最低证据要求为1。题数表示实际呈现的单元根题数；两道长题可以提供六个单元，不受原始题数限制。预算无法满足所有必要能力的最低证据数时返回 `ASSESSMENT_COVERAGE_BUDGET_INVALID`，中文说明实际所需最低单元数，并通过details提供最低数和当前上限。API 禁止额外字段。多能力映射的最低证据数使用有界精确覆盖计算：最多10,000个搜索节点或150毫秒；耗尽返回 `ASSESSMENT_COVERAGE_SEARCH_LIMIT`，提示缩小范围或简化映射，不将贪心估计当作确定的不可行结论。可传独立可选的 `skill_id` 和 `company_context`。新自由格式Skill保存即active，选择后草稿冻结其具体revision/hash；旧 `enterprise_skill_id` 仅为兼容入口，与skill_id同时传入且不一致时拒绝。计划批准时复核同一版本可用性，不随新的Skill版本漂移。企业资料非空时随计划冻结到会话并参与上下文指纹；两项都缺省不阻塞面试，资料全文不进入候选投影。Skill写法无需审核，计划本身的考察范围仍保留上述审阅与批准。

v3 计划保留 bank_slots 供企业审阅候选范围，但不把槽位物化为整个问题队列。`assessment_contract` 冻结岗位版本、固定能力维度权重、各维度最低有效根题证据数、题目原文及完整评分标准、题池版本、题数和追问预算；内容散列校验防止批准后的静默变更。新生成计划使用 `approved_inquiry_units.v1`：计划生成时通过集中Prompt `interview_inquiry_units.v1`，把每个冻结关键点转成一个最多160字的单焦点口头问题，单元仅对应一个原关键点，参考答案必须是原标准答案的逐字连续片段。每个单元的 `competency_ids` 必须是原题能力范围的非空子集，按该单元实际考察内容选择，不能继承原长题的全部标签冒充多个能力证据。统一Schema先验证类型、非空、枚举、长度和额外字段，再验证完整分区、归属、单问句、原文引用及散列。失败不落计划、不回退为长题。

模型调用沿用 question_generation 路由，最多4道原题一批、并发3、每批60秒和整体180秒上限；原题候选清单在调用前按能力均衡裁成有界、可审核范围，最多60道岗位原题，加最多3道已批准简历题。任务取消或任一批失败都会取消仍在途批次。冻结时再次校验岗位、题目版本，避免模型生成期间的并发修改。修改草稿新增尚无单元的问题需要重新生成并审核计划。

企业批准的内容包含新生成单元；运行时仅选择已批准单元。历史已冻结的 `complete_approved_question.v1` 合同继续呈现完整原题并按完整范围评分，不静默裁减历史标准。

## 生命周期与接口 seam

最低证据单元数使用带剪枝与记忆化的精确覆盖计算，重复能力映射按可用单元数量合并；单次搜索最多10,000个节点、150毫秒。贪心结果只用作可行上界，不作为“至少需要”的证明。预算耗尽时返回 `ASSESSMENT_COVERAGE_SEARCH_LIMIT` 并说明需要缩小候选范围或简化映射，不谎称无解或给出未经证明的最低数。

`initialize_adaptive_snapshot(plan)` 为会话快照提供冻结合同与空的 question_snapshots/question_selections。v3 允许以零题创建会话，START 后进入 `dialogue_state=awaiting_next_decision`。

`SELECT_NEXT` 必须携带 `decision_id`、`expected_decision_revision` 和批准题池中的 `question_id`；新单元合同还必须提供该题的 `inquiry_unit_id`。只有 in_progress、候选输入未结束、无活跃题时才能追加一题；按 `(question_id, inquiry_unit_id)` 去重、校验题数和剩余时间。同一 decision_id、相同语义请求重复执行无副作用，不同请求复用编号拒绝。成功冻结 question_snapshot、呈现范围、root turn 和 QuestionSelection，推进 decision_revision 并激活题目。

每个单元拥有独立根题、答案和评估组。question_snapshot 的 question_text、spoken_text、standard_answer、key_points 只包含当前批准单元；原长题音频不会被复用，短题走既有批准表达/TTS链路。保存 `source_rubric_point_ids`、`assessed_rubric_point_ids`、`not_assessed_rubric_point_ids` 与实际 `presented_unit_ids`。预算可行性、证据覆盖、turn/selection以及报告维度映射均使用单元自身的competency_ids。原题虽然带Python/数据库两个标签，只考察Python的单元不能增加数据库覆盖或分数。候选人明确拒绝、结束该源问题话题或跳过后，不能换同源另一个单元继续追问。

ANSWER_SUBMITTED 保留现有评分 Outbox；没有批准下一题时进入 awaiting_next_decision，评分完成或空队列均不得隐式结束候选人输入。FOLLOWUP_REQUESTED 仍必须为零权重，并受冻结每根题、全场和时间预算限制；v3深度上限取冻结每根题追问数，旧v2的4次全场/2层上限只适用于v2。单元追问的目标、关键点、参考答案和rubric必须属于当前根单元，不能通过追问偷偷扩大考察范围。追问只能接在最新已呈现轮次且当前无活跃题时插入；迟到命令不能覆盖新问题，已提交追问的幂等回放仍无副作用。任意子追问中的明确拒绝或话题结束均回溯原始源题，统一关闭其未问单元。

`END_CANDIDATE_INPUT` 必须携带 decision_id、expected_decision_revision、reason。正常 `evidence_sufficient` 要达到最低已回答根题数及所有必要维度证据要求，且没有活跃题。`budget_exhausted` 必须由真实题数/时间预算或已无可选择的批准单元证明；仅达到选题数上限而本题仍有时间不能跳过正在回答的题。`candidate_requested`、`appointment_window_expired` 和 `media_failure` 为可信运行时的明确控制原因，可以在证据不足时结束。未提交活跃题标明跳过原因，不能伪造答案。记录输入结束时间、原因、覆盖缺口；已收答案仍异步评分，全部评分就绪后请求报告。截止watchdog对v3同事务保存END与报告Outbox，v2仍走原CANCEL。结束后迟到选择不能重新打开会话。

## 报告

v3 按冻结能力维度权重汇总每道 root 的最新有效评估；追问只进入根题证据组，不重复贡献权重。各能力内对实际考察根题等权聚合。单元评分调用 `answer_evaluation.v6`：只传当前短问题、当前关键点和逐字参考片段，响应Schema把covered/missing的关键点ID枚举收紧到该单元，禁止将未问点写成缺失。历史完整题继续使用原评分Prompt版本。

未考察能力 `score=null/evidence_status=insufficient_evidence`；已有题分与已有维度分正常展示。整体总分仅在全部有权重维度达到冻结证据数量要求、有有效评分、且已回答根题数达到冻结全局最低数时发布，避免把缺失能力按零分或重分配分母。若各维度采样已满足但全局最低数未达，仍保留各维度分并将总分标为空；assessment_coverage保留已回答根题数，不能将此情形错误描述成某能力未考察。覆盖不足保持 `job_fit_level=insufficient_evidence` 与明确覆盖缺口。`source_question_scopes` 按原题记录已考察/未考察关键点及partial/complete/not_assessed状态；达到能力采样要求不等于原长题已全部考察，报告须明确区分。识别不确定性沿用027的提示语义，合法评分不因识别提示而被阻塞。

复核页显示固定维度权重、已有数值、未考察范围、结束原因和单元范围。只读review投影保留最近100条已提交决策的安全元数据：动作、原因、工具名、模型调用次数、Prompt版本、上下文散列和稳定编号；不包含原始Prompt或模型完整响应。每条决策回执冻结对应last_planning元数据，不能用最后一次的推理替代历史选择。

execution v2 的正常物化、逐题权重和自动结束语义保持不变。为尊重存量面试中的明确退出意愿，v2仅额外允许 `END_CANDIDATE_INPUT/candidate_requested`；未完成题标明未考察，已有题分保留，尚有计划范围未考察时不发布可能误导的总分。GET、历史投影和导出遵守同一缺证据规则。
