"""Versioned semantic-first turn policy and its exact evidence contract.

These fields describe candidate meaning, independently of technical coverage.
Only the runtime may turn a validated, current final into an answer commit.
"""

from copy import deepcopy
import re


SEMANTIC_TURN_INSTRUCTION = (
    "本轮采用语义优先接话合同semantic_turn.v23。服务端尚未授予本次回答的独立结束许可，"
    "不得假定已发生独立确认。分别判断answer_content与turn_intent，不按关键词匹配。"
    "是否询问过补充以服务端supplement_context为准，不从候选原文或旧对话猜测。"
    "not_delivered表示当前补充问题没有有效送达证明，不能把选中、播放中或中断当作已经问完；"
    "awaiting_reply表示当前补充问题已送达，正在等待本人回复，不能再声称尚未询问。"
    "已送达不等于已确认：提问术语、重读、普通交流、继续补充或含糊回复都不构成结束许可。"
    "confirmed只能来自本次调用既有的服务端确认授权；阶段上下文本身不能替代确认门禁，"
    "也不能补造completion_basis、抹掉最新问题、等待请求或实际技术内容。"
    "answer_content描述是否能从原文提取含义确定的专业claims，不是得分：technical/partial必须有真实claims；"
    "none表示没有可确定的技术主张，允许原文有无法理解的技术尝试，不等于没有开口、不会或拒答。"
    "另外，清楚陈述与本题无关的内容时使用intent=off_topic；跑题是相关性判断，不是识别失败、语义不明或知识拒答。"
    "例如技术题后清楚讲周末爱好再说‘我的话说完了’，可为off_topic+none+finish_answer，"
    "claims/covered为空、missing包含全部能力点、ambiguities为空、clarification_target=null，保留真实正文和完成语证据。"
    "off_topic的answer_content仍按是否有真实专业主张判断；没有就none，不把爱好、游戏情节或结束控制编成技术claims。"
    "与本题无关但确实存在的专业主张可用partial/technical并保留，covered仍为空；"
    "off_topic只允许整份回答没有任何与题目相关的实际技术内容。必须逐条检查全文claims的相关性，不按最后一段或篇幅占比判断。"
    "例如题目问数据库一致性，候选人先说‘多张表的写操作会放进同一事务’，再讲一大段种花，最后‘我说完了’，"
    "仍必须intent=answer、answer_content=partial、turn_intent=finish_answer；保留事务claim及有证据支持的覆盖点，followup_allowed=true。"
    "不完整、没有命中全部能力点或只有一句相关做法仍是answer；summary应先概括这份技术作答，不能只概括无关尾段。"
    "只有逐项都与题目无关的真实专业claims才可off_topic+partial/technical；禁止以跑题尾段将相关作答改成off_topic。"
    "清楚跑题后明确说完可用finish_answer；明确换题或不再答本题用finish_topic/decline_topic，"
    "每种收尾都需当前真实completion_basis、confidence至少0.75，followup_allowed=false；"
    "这只结束本题回答，不结束整场，也不代表答案正确或得分为零，原话交评分环节判断相关性。"
    "尚未说完仍answering，明确等一下仍thinking，不能因跑题替候选人结束或反复要求解释已听懂的内容。"
    "明确退出整场则off_topic+stop_interview并由运行时独立确认；不把完成本题改成退出。"
    "只有控制请求或寒暄不能虚构一份off_topic回答；实际转写有未解决技术疑点仍按下述澄清合同，"
    "不能用off_topic掩盖听不清、转写投诉或跳过内容澄清和退出确认。"
    "已听懂的无关内容不能要求换一种说法或拼出术语；即使先前误问过澄清，也应根据完整原文及当前新发言纠正理解，"
    "不能因旧澄清记录继续制造歧义；清楚内容加当前新完成语仍可用off_topic正常收尾。"
    "先判断answer_content和本人最新有效的turn_intent，再据此选择suggested_action。"
    "intent=answer只表示已有专业内容，不表示候选人已经说完，不能从intent=answer推导为next。"
    "内容抽取优先于结束意图分类：先保留全文中所有未撤回的实际做法、选择或技术判断，"
    "只要这项做法或判断的含义能够确定，哪怕只说了一句、没有解释原因、未覆盖任何冻结能力点，也属于partial。"
    "累计原文保留交流记录，但概念提问、重读请求、思考、不会和结束控制本身都不构成技术claim；"
    "面试官的解释也不能变成候选人的技术主张。只从候选人实际陈述的做法、经验或判断提取claims。"
    "回答不充分不等于没有回答；结尾拒绝继续只关闭后续交流，不能抹掉前面的实际回答。"
    "回答不充分、缺少实施步骤或没有解释原因，不等于技术含义无法确定；这些缺口属于missing或后续技术追问，"
    "不能因此要求解释整段回答或拼写术语。结合题目及全文已经能确定含义的口语、省略或音近词，"
    "保留实际原文并正常提取claims，不机械要求候选人拼写。只有影响理解的真实歧义才澄清，"
    "clarification_target只选最小实际疑点，不把整段可理解回答当作焦点，也不提供标准答案、替换选项或解题步骤。"
    "有可确定的实际内容时claims及evidence_ids必须非空，通常intent=answer；若满足下述待核实收尾条件，"
    "则使用answer_unresolved并同样保留这些claims。如果同时要求换题，使用answer+partial/technical+finish_topic。"
    "只有全文没有实际回答、没有未解决技术疑点且本人明确不再作答，才可能answer_declined。"
    "先判断实际话语是否可理解、是在陈述还是疑问，再判断本人当前是否明确授权结束。"
    "无法理解的残片、不连贯的转写、未完成的疑问都不是拒绝作答；没有提取到技术claim不等于本人不会。"
    "若技术发言含义无法确定，应先对最小实际疑点作clarify；没有可靠claims用clarification_request，已有则保留answer，followup_allowed=false、"
    "completion_basis=null并提供clarification_target，不替含糊语音补造‘不会’或‘下一题’，confidence低于0.65。"
    "‘我说完了’、‘我的介绍完毕’只说明话轮结束，不证明正文可以理解，更不证明本人不会或拒答。"
    "先查看本轮内容澄清阶段，再决定可执行动作。首次技术残片加明确完成语允许"
    "intent=clarification_request、turn_intent=finish_answer、suggested_action=clarify、confidence低于0.65、"
    "followup_allowed=false、completion_basis=null，并保留非空ambiguities及原文clarification_target。"
    "这是本人表示说完但内容待澄清的非提交状态：finish_answer记录其表达的意图，clarify表示此刻实际动作，"
    "不产生结束许可；已有可靠claims仍保留。不得为满足结束动作虚构claims或改为answer_declined。"
    "服务端content_clarification证明具体疑点已询问且播完后，候选人新的话仍明确坚持说完、"
    "而该技术疑点仍未能消除时，允许intent=answer_unresolved、turn_intent=finish_answer、suggested_action=next。"
    "这表示本人结束这次回答，但技术转写待核实，不表示不会、拒答或得分为零。"
    "这类finish_answer只在该服务端上下文存在时使用answer_unresolved；记录客观answer_summary、非空ambiguities，"
    "clarification_target仍引用同一实际疑点；confidence至少0.75只表示对当前明确完成意图的把握，"
    "completion_basis必须引用提问播放完成后新回复中的真实完成语，followup_allowed=false。"
    "保留全文能够确定的全部claims及evidence，answer_content按实际为partial或technical；"
    "确实没有可确定技术主张才为none且claims/covered为空，不补造、不删除、不判拒答。"
    "这一收尾不能由静音、嗯、没有等含糊回复授权，不能用于候选人仍在投诉识别、改口继续或等待；"
    "新回复若仍是疑问/转写争议则继续clarify/correction。若技术正文存在未澄清疑点但本人明确请求退出整场，"
    "优先使用answer_unresolved+stop_interview+next，followup_allowed=false，保留ambiguities、实际疑点target和全部可靠claims。"
    "这一退出请求不要求先有content_clarification，不能强迫候选人先解释技术残片才允许提出退出。"
    "confidence至少0.75只表示退出范围明确，completion_basis引用本人现在的退出请求；模型此时只提议询问独立退出确认。"
    "真正结束仍必须由运行时完成独立退出确认；确认后仍保留技术疑点，不能把同意退出改写为承认不会。"
    "无论当前是退出还是等待，都先检查完整正文：候选人已经尝试说明做法，但某个对象或状态无法确定，"
    "这段不确定的技术尝试必须保留为ambiguities/clarification_target，不能清空疑点后声称完全未作答。"
    "仅作字段配合示例：假设E1为‘先转到卡拉布，卡拉布那个状态。’，E2为‘我不想继续面试了。’，冻结点仅P1，"
    "且E1没有可确定主张，则合法理解为"
    '{"answer_content":"none","turn_intent":"stop_interview","intent":"answer_unresolved",'
    '"answer_summary":"候选人请求退出，技术片段含义待核实。","claims":[],"evidence_ids":["E1","E2"],'
    '"covered_point_ids":[],"missing_point_ids":["P1"],"ambiguities":["卡拉布所指状态不明确。"],"contradictions":[],"confidence":0.95,'
    '"clarification_target":{"evidence_id":"E1","focus_quote":"卡拉布"},"followup_allowed":false,'
    '"completion_basis":{"source":"explicit_server_intent","evidence_ids":["E2"]},"suggested_action":"next"}。'
    "实际输出必须用本次原文和编号；不要仅因出现技术词便选partial，也不能为满足partial而补造claim。"
    "询问题意或术语含义是在寻求解释，不是声称没有这项能力；仅对仍未解决的疑问使用clarify，"
    "若后文已经自行说明并继续作答，保留全部实际技术内容，不能继续按早先疑问判断。"
    "turn_intent=answering/thinking/finish_answer/finish_topic/decline_topic/stop_interview/repeat/clarify/correction/pause。"
    "必须区分完成一次回答和拒绝继续这个话题。已有技术内容后说‘我讲完了’、‘就这些’、"
    "‘没有需要补充的了’只表示finish_answer：保留已有claims和证据，followup_allowed=true；"
    "面试官仍可围绕这份回答追问具体做法或依据，不能推断候选人拒绝追问。"
    "回答后主动邀请核实、请面试官提出尚有疑问的地方，是把当前话轮交给面试官，"
    "不是要求结束话题，也不是请求解释题意或询问公司信息。全文有实际回答且明确交还话轮时，"
    "使用finish_answer、followup_allowed=true，completion_basis引用这次交还话轮的原文；"
    "已有claims全部保留，正式追问决策可以据此继续核实。"
    "这条规则只适用于本人当下向面试官发出的邀请，不适用于引用、假设、技术方案中的用户提问，"
    "也不能覆盖其随后明确继续讲、等待、拒绝追问或要求换题的请求。"
    "只有明确要求‘下一题’、‘这个方向不用再问’、‘换个话题’才是finish_topic。"
    "本人明确说‘我不会’、‘这题我不会’、‘这道题答不上来’，且没有同时要求继续想或继续讲，已经表达本题不再作答，"
    "不需要再要求候选人重复说‘下一题’才承认；没有技术内容用none+decline_topic，"
    "已有实际内容则保留claims并用partial或technical+finish_topic。旧的‘我想一下’不能覆盖后来明确的不会或说完。"
    "已有技术内容后说下一题用partial或technical"
    "+finish_topic，保留技术claims和证据，绝不能将前文丢掉。"
    "finish_topic/decline_topic/stop_interview必须confidence至少0.75，suggested_action=next，"
    "followup_allowed=false，并提供completion_basis={source:explicit_server_intent,evidence_ids:[E编号]}。"
    "finish_answer只有建议next收尾时才必须confidence至少0.75，并用completion_basis逐字引用"
    "实际完成回答的原文；可理解的技术回答使用intent=answer、partial或technical；清楚但完全无关的陈述使用off_topic；满足上述待核实例外时仍用answer_unresolved。"
    "这里只能引用当前完整原文中实际表达结束意图的证据，不把静音、普通陈述结束或模型把握当结束授权。"
    "‘我不想继续面试了’、‘我不想面了’、‘我们结束面试吧’明确结束整场时使用stop_interview，不能误解成只跳过本题。"
    "同一发言既说本题不会又要求结束面试，范围是整场，必须stop_interview；不由answer_content决定结束范围。"
    "这时相关技术正文可理解且有实际内容仍intent=answer，清楚无关陈述仍off_topic，无内容且明确未作答才answer_declined；"
    "技术转写仍有未解决疑点则answer_unresolved，原文、疑点和已答内容全部保留。"
    "除实际建议next的finish_answer/finish_topic/decline_topic/stop_interview外completion_basis必须null；"
    "上述finish_answer+clarification_request+clarify非提交组合也必须null。疑问、转述、否定、引用的不会都不是本人拒答。"
    "技术案例里的客户、同事或系统说‘结束面试吧’属于引用，不是候选人要求结束；"
    "例如‘客户说结束面试时，我会记录请求并关闭采集。我的回答说完了’必须保留记录与关闭采集的技术claims，"
    "本人最新意图是finish_answer而非stop_interview，不能引用案例角色的话作为结束整场依据。"
    "‘Celery我不会，不过我用Redis做过队列’仍应提取实际经验，不能凭前半句判结束。"
    "‘同事说他不会，我就接手了’是在讲经历；‘让我想一下’、‘等一下我还没说完’"
    "是thinking与continue_listening，必须继续听，不能问是否还有补充。"
    "短暂思考与暂停整场必须分开：本人说‘稍等’、‘稍等。没有。嗯。’而没有明确撤回等待，"
    "使用intent=not_finished、turn_intent=thinking、suggested_action=continue_listening，"
    "answer_content=none、claims=[]、covered_point_ids=[]、followup_allowed=false、completion_basis=null。"
    "已有专业回答后要求等一下仍保留claims及partial/technical，turn_intent=thinking，"
    "suggested_action=continue_listening；不能因等待将已有专业回答清空。"
    "正文技术疑点也保留为ambiguities和原文clarification_target：即使此前已追问该疑点，"
    "候选人最新说还要想或没讲完，也先继续听，不能再用旧疑点打断等待。"
    "没有可靠专业主张时使用not_finished+none+thinking；已有可靠主张则answer+partial/technical+thinking。"
    "此时confidence表示对当前等待意图的把握，技术疑点不能被误当为没听清等待请求。"
    "只有明确请求暂停整场面试、离开后再回来，才使用intent=pause、turn_intent=pause、"
    "suggested_action=pause、followup_allowed=false、completion_basis=null；暂停不是结束回答，"
    "不得为暂停生成explicit_server_intent结束依据。绝不能返回pause与continue_listening的矛盾组合。"
    "‘没有’或‘嗯’不能撤销更早的等待请求，也不能在未询问补充时单独充当结束确认。"
    "最新明确改口优先于更早结束意图，累计原文含‘下一题，不对我还没说完’不能结束。"
    "明确纠正或投诉转写必须先处理争议，turn_intent=correction或clarify、completion_basis=null，"
    "只有未解决疑点才clarify；上下文已消除疑点仍保留原文并正常理解。"
    "repeat/pause分别使用repeat/pause动作。普通answering没有结束依据时可以建议accept，"
    "运行时会在需要时确认，不得凭此建议捏造结束依据。"
    "followup_allowed描述是否允许继续本话题；可理解回答的finish_answer允许追问，answer_unresolved禁止技术追问，"
    "off_topic、关闭话题、拒答、结束整场、思考或控制请求均为false。"
)

# A real supplement reply permits completing this answer. It must still be
# understood semantically so a request to stop the whole interview survives.
SEMANTIC_CONFIRMED_TURN_INSTRUCTION = SEMANTIC_TURN_INSTRUCTION.replace(
    "本轮采用语义优先接话合同semantic_turn.v23。服务端尚未授予本次回答的独立结束许可，"
    "不得假定已发生独立确认。",
    "本轮采用语义接话合同semantic_turn.v24。服务端已取得本次回答的真实结束确认，"
    "确认来源由服务端保证，不能从静音或普通陈述伪造。"
    "‘没有补充了’只结束本次回答；已有技术回答时，确认回复的‘没有’、‘补充完了’使用"
    "finish_answer且followup_allowed=true。不能因为否定补充就写finish_topic或拒绝后续追问。"
    "若确认来源是按钮且原文没有口头完成证据，保留answering、completion_basis=null，不能伪造口头证据。"
    "如果确认回复明确要求结束整场面试，必须stop_interview；明确跳过话题则finish_topic。",
)


SEMANTIC_CONTROL_CORRECTION_INSTRUCTION = (
    "本次需纠正语义控制字段：先根据原文判断本人最新有效的turn_intent，再选择动作，"
    "不能从intent=answer推导为next。thinking必须suggested_action=continue_listening、"
    "followup_allowed=false、completion_basis=null；保留已有技术claims及其原文证据。"
    "即使前文说过已经讲完，后文明确改口需要继续思考，仍按最新意图等待。"
    "repeat/pause必须使用对应动作且followup_allowed=false；clarify/correction也禁止追问。"
    "不得为匹配动作将原文中的等待意图改成结束，不能清空已有专业回答来规避校验。"
)


def semantic_content_correction_instruction(reason_code):
    """Only fixed diagnostics may select centrally governed recovery guidance."""
    return {
        "unresolved_clarification_source_invalid": (
            "上次待核实收尾的澄清来源没有绑定实际已送达的原焦点。"
            "若服务端提供C1且同一原疑点仍未解决，clarification_target只选{resolution:same_focus_unresolved,source_ref:C1}，"
            "不要重写原焦点、扩大旧证据或把新回复当成旧疑点来源。C1不能用于claims或completion_basis。"
            "先在联合对象中明确resolution：不能把不同新疑点绑定same_focus_unresolved/C1。"
            "新回复已说明或纠正原疑点且无其它疑点则选{resolution:resolved}、ambiguities=[]，正常理解并保留全部可靠claims；"
            "若出现不同的新疑点，选{resolution:new_focus_unresolved,evidence_id:该疑点E编号,focus_quote:新疑点原词}，"
            "继续澄清，不能借旧送达记录收尾，也不能为过校验给描述补入已解决的旧词。"
            "completion_basis只能选播完后的新回复中真实完成语；仍在等待、提问或继续作答不得结束。"
            "没有有效C1时不得自造来源，未解决疑点使用clarify并等待真实澄清。"
        ),
        "clarification_targets_conversation_control": (
            "上次把清楚的开始回答或等待请求误当作技术疑点。按当前交流语境重新判断，"
            "不能要求候选人解释或拼写这些流程用语；没有真实疑点时clarification_target=null、ambiguities=[]。"
            "保留先前实际技术claims；仅准备开始回答用answering，请求思考用thinking。"
            "这两者都不授权完成回答或退出面试，不能编造completion_basis。"
        ),
        "completion_has_unresolved_ambiguity": (
            "本次字段冲突位于$.ambiguities：普通回答完成不能同时保留未解决的语义歧义。"
            "先区分知识点缺失、上下文已能唯一确定的音近词和真正影响含义的歧义；知识缺失属于missing，"
            "音近词保留原文并仅提取含义确定的claims，不能机械要求拼写；真实歧义不能为通过校验而删除。"
            "没有本轮有效content_clarification已送达证明时，首次真实歧义加finish_answer应输出"
            "clarification_request+finish_answer+clarify、confidence低于0.65、followup_allowed=false、"
            "completion_basis=null，保留可靠claims、ambiguities和原文clarification_target。"
            "已有有效证明时不能重复追问同一疑点；只有播完后的新回复明确坚持结束，才可"
            "answer_unresolved+finish_answer并引用新回复作为completion_basis。新回复纠正了疑点则按纠正后含义理解，"
            "仍在思考则继续听；不得编造送达记录、删除可靠claims或将本题结束改成退出整场。"
        ),
        "unresolved_answer_contract_invalid": (
            "本次字段冲突位于$.intent：answer_unresolved只用于仍有具体语义歧义的待核实收尾，"
            "不是知识缺失、一般低置信度或所有澄清回复的兜底。保留可靠claims、非空原文evidence、"
            "真实ambiguities及有原文依据的clarification_target；不确定的残片不能伪造成claim。"
            "finish_answer必须有本轮有效content_clarification已送达证明，completion_basis只能引用播完后的新回复，"
            "不能复用澄清前说完。新回复仍在思考或纠正时按thinking/correction处理；"
            "原文确实finish_answer且仍有真实歧义、但没有有效证明时先用"
            "clarification_request+finish_answer+clarify、confidence低于0.65、followup_allowed=false、completion_basis=null。"
            "若疑点已解决则正常answer，不保留虚假的ambiguities或answer_unresolved；矛盾claims仍须按原文重新核对。"
            "stop_interview只限本人明确要求结束整场，仍须独立退出确认，不能从待核实收尾推导退出许可。"
        ),
        "technical_content_claims_missing": (
            "本次字段冲突位于$.claims：answer_content为partial/technical却没有claims。"
            "重新检查完整原文：存在含义确定的真实做法或判断时，保留partial/technical并引用对应E编号提取claims；"
            "若只有无法解释的技术残片、没有可确定主张，则使用answer_content=none、claims=[]、covered_point_ids=[]。"
            "none仍可保留ambiguities/clarification_target，满足待核实收尾条件时使用answer_unresolved；"
            "不改写成不会或拒答，不得编造claim来通过校验，也不能删除原文中已有的可靠主张。"
        ),
        "content_none_has_technical_evidence": (
            "本次字段冲突位于$.answer_content：none不能同时有claims或covered_point_ids。"
            "原文确有含义确定的技术主张时应保留claims并用partial/technical；"
            "若所列内容只是控制请求或无法理解的残片，则去除这些非技术主张，保持none并保留真实疑点。"
            "不得清空有效技术主张来满足none，也不得将无法理解等同不会。"
        ),
        "unresolved_clarification_required": (
            "本次字段冲突位于$.content_clarification：服务端没有提供有效的已送达内容澄清证明，"
            "不能输出answer_unresolved+finish_answer。首次残片即使候选人说完了，也应选择clarification_request+"
            "finish_answer+clarify、confidence低于0.65、followup_allowed=false、completion_basis=null，"
            "并保留已有可靠claims、非空ambiguities和原文target。不得从示例、历史选中回应或完成语推断已播放澄清。"
            "只有原文明确要求退出整场时，才可用answer_unresolved+stop_interview提议独立退出确认；"
            "不能为规避澄清条件将完成本题改为退出整场。"
        ),
    }.get(reason_code, "")


def content_clarification_instruction(clarification_context=None):
    """State the actual server phase; these prompt cues never grant a commit."""
    value = clarification_context
    delivered = (
        isinstance(value, dict)
        and set(value) == {"version", "request_text", "focus_quote", "reply_text", "question_act_id", "delivered"}
        and value.get("version") == "content_clarification.v1"
        and value.get("delivered") is True
        and all(isinstance(value.get(key), str) and value[key].strip()
                for key in ("request_text", "focus_quote", "reply_text", "question_act_id"))
    )
    if delivered:
        return (
            "本轮内容澄清阶段：服务端已提供已送达的content_clarification记录。"
            + _CONTENT_CLARIFICATION_REASONING_ORDER +
            "现在先比较原focus_quote与reply_text：新回复已解释原词含义时，旧疑点已解决，不能再选C1；"
            "若没有别的真实疑点，选{resolution:resolved}、ambiguities=[]并正常理解。"
            "若原焦点仍未解决，才可选{resolution:same_focus_unresolved,source_ref:C1}；保留旧请求中可独立理解的全部claims，"
            "不能只概括当前解释或结束语，也不能因同一句里另有疑点而丢掉已确定的内容。"
            "若新回复解释了旧词却引入不同未知词，旧C1不适用：改用新疑点所在E编号和最小focus_quote，"
            "保留已确定claims、completion_basis=null、suggested_action=clarify，不得借旧问题的送达证明结束新疑点。"
            "例如旧话‘先登记订单编号，再交给古帕’，问过古帕后新话‘古帕是复核队列，接着还要过莫达，我说完了’，"
            "必须保留登记编号和复核队列两项已确定主张；新疑点是莫达，"
            "clarification_target使用{resolution:new_focus_unresolved,evidence_id:新回复的E编号,focus_quote:莫达}而不是C1，先澄清莫达。"
            "相反，若新话‘古帕暂时解释不清，我已经说完了’，原焦点仍未解才选C1，"
            "answer_unresolved+partial并保留登记编号的claim，不得清空为none。"
            "仅当同一原焦点仍未解决、该记录reply_text确为澄清播完后的新回复且本人现在明确坚持结束，"
            "才可answer_unresolved+finish_answer收尾，completion_basis仅引用这个新回复。"
            "新回复仍在思考或没讲完就thinking/continue_listening；仍在纠正识别就correction/clarify，不能因已有记录强制结束。"
            "明确退出整场则stop_interview并仍需独立退出确认；不要把这次内容澄清视作退出确认。"
        )
    return (
        "本轮内容澄清阶段：服务端尚未提供有效的已送达content_clarification记录。"
        + _CONTENT_CLARIFICATION_REASONING_ORDER +
        "可理解的partial回答按正常answer处理，明确说完可finish_answer/next；不要因为当前还没有内容澄清记录就先制造疑点。"
        "本轮禁止answer_unresolved+finish_answer；先前选中过回应、提示中的示例和候选人说完了都不证明澄清已实际播完。"
        "仅在已确认存在影响原话含义的真实歧义时，首次技术残片加完成语应具体clarify，合法非提交组合是clarification_request+finish_answer+clarify、"
        "confidence低于0.65、followup_allowed=false、completion_basis=null，保留非空ambiguities、原文target和可确定的claims；"
        "不能猜答案、改成拒答或要求重复整段。这个限制仅针对结束本次回答："
        "本人明确请求退出整场时仍可answer_unresolved+stop_interview提议独立退出确认，无须先解释技术残片。"
    )


_CONTENT_CLARIFICATION_REASONING_ORDER = (
    "请严格按以下顺序理解，而不是先判断回答够不够好："
    "第一，从累计完整原文逐项提取可独立理解且未撤回的做法、经验或判断为claims；同一句的一个词不明，"
    "不妨碍保留其余已经清楚的主张。第二，区分知识缺失和语言歧义："
    "没有说明组件品牌、实现细节、机制、原因、步骤或如何满足题目要求，只属于回答不充分，"
    "放入missing或后续技术追问；只要能理解实际陈述，就不能将这些缺失列为ambiguities或降低语义confidence。"
    "‘缓存’‘数据库’‘队列’等一般技术类别本身含义清楚，没给出具体产品或作用细节不构成歧义。"
    "例如被问并发安全，答‘我把订单编号存入数据库，我说完了’，已经有一项明确做法，应answer+partial、"
    "保留存储编号的claim、ambiguities=[]、target=null；没说锁或事务是知识缺口，不能要求解释‘数据库’或整句话。"
    "只有无法确定原话到底指什么的片段才是真实歧义，例如‘登记编号后交给古帕’中的古帕；"
    "登记编号仍是可靠claim，最小疑点仅古帕。第三，逐个核对当前新回复是否消除了旧疑点，"
    "不要机械沿用历史焦点或把不同新疑点绑定旧来源。最后才根据当前真实完成语、等待或交流请求决定动作；"
    "完成语不能证明技术含义清楚，澄清记录也不能替代本人当前完成许可。"
)


CURRENT_SPEECH_INSTRUCTION = (
    "面试官上下文的input_context将本题累计证据与当前交流请求分开。current_speech是最后一次已选回应之后的新发言，"
    "turn_intent及completion_basis应根据这段当前发言判断；此前话语仍可证明技术内容，但旧思考、重读、已解释的问题"
    "不能再次成为当前等待或澄清请求。current_speech为空时，只复核responded_exchanges最后一条request_text的含义，"
    "不能把更早的问题重开；这条接话可能误判，实际选择回应并不代表其语义判断正确。"
    "responded_exchanges只记录服务端选中的交流回应，不证明候选人已听完，也不是技术答案或完成许可。"
    "历史delivery_status只有delivered表示完整送达；selected/playing/interrupted/failed/unknown不能称候选人已听完。"
    "送达不确定本身不能把旧候选请求再判为新的疑问；仅按当前新发言决定是否补说未听到的内容。"
    "技术claims从全部累计原文保留，禁止因为交流边界切换而只保留最后一句控制话语。"
    "候选人主动再次提问、明确表示没听到或纠正原话时，依据新的请求回应，不能机械排除同样的词。"
    "clarification_target只能指向当前尚未解决的发言疑点；先前概念提问已有解释且当前没有重新提出时，"
    "不能说先前提问不确定、要求重说或拼写。结束依据仍必须引用权威原文编号，历史回应本身不能授权结束。"
)


LEGACY_CURRENT_SPEECH_INSTRUCTION = (
    "面试官上下文的input_context仅补充当前交流边界，不改变本次输出字段、提交条件或结束授权。"
    "current_speech是最后一次已选回应之后的新发言；responded_exchanges包含已选中的请求与回应。"
    "current_speech为空时，结合最后一条request_text理解当前请求；历史回应不证明已听完，也不是技术答案。"
    "delivery_status只有delivered可称完整解释；其它状态不证明候选人已听完，也不能因此重新打开旧候选请求。"
    "先前概念问题已经解释且新发言没有重新提问或纠正时，不能将该旧问题再作为clarification_target。"
    "只对当前尚未解决的真实疑点澄清；完整累计原文继续用于技术主张，不得因为交流边界丢失先前回答。"
    "上述记录不能授权结束面试、补造技术证据或添加本合同没有的字段。"
)


CURRENT_SPEECH_CORRECTION_INSTRUCTION = (
    "本次需纠正当前话语与语义结果的冲突：重新依据input_context.current_speech（空时最后一条已接请求）"
    "识别最新意图及结束范围，不沿用旧思考或已解释的问题。明确本题不会时允许拒答，明确结束面试时必须区分整场，"
    "不要为了通过校验伪造证据或抹掉累计技术claims；引用、否定、假设和后续改口仍按整句真实含义理解。"
)


class SemanticContractError(ValueError):
    """Fixed, non-sensitive diagnostic from centralized semantic validation."""

    def __init__(self, reason_code, schema_path="$", /):
        self.reason_code = reason_code
        self.schema_path = schema_path
        super().__init__(reason_code)


class SemanticControlContractError(SemanticContractError):
    """A fixed, non-sensitive cross-field error for logging and one correction."""

    reason_code = "semantic_control_contract_invalid"

    def __init__(self):
        super().__init__(self.reason_code)


class CurrentSpeechContractError(SemanticControlContractError):
    reason_code = "semantic_current_speech_conflict"


def _current_speech_scope(transcript, input_context):
    """Accept only mechanically verifiable server ranges, never model offsets."""
    if not isinstance(input_context, dict) or input_context.get("version") != "conversation_input_context.v1":
        return None
    start, text = input_context.get("current_speech_start"), input_context.get("current_speech")
    if type(start) is not int or not isinstance(text, str) or not 0 <= start <= len(transcript):
        return None
    if transcript[start:].strip() != text.strip():
        return None
    if text.strip():
        return start, len(transcript), text.strip()
    exchanges = input_context.get("responded_exchanges") or []
    if not exchanges or not isinstance(exchanges[-1], dict):
        return None
    last = exchanges[-1]
    start, end, text = last.get("request_start"), last.get("request_end"), last.get("request_text")
    if (type(start) is not int or type(end) is not int or not isinstance(text, str)
            or not 0 <= start < end <= len(transcript) or transcript[start:end].strip() != text.strip()):
        return None
    return start, end, text.strip()


def _explicit_control_conflict(text, intent, content):
    """Reject a few exact contradictions; this cannot supply an intent or permit.

    Deliberately use complete direct sentences, not a keyword classifier. A
    quote, hypothetical, negation or trailing elaboration is left to semantic
    inference. Any detected conflict goes through the existing bounded retry.
    """
    # A quoted example earlier in the answer must not suppress validation of
    # the candidate's own later, independent completion sentence.
    sentences = [part.strip() for part in re.split(r"[。.!！\n]", _without_quotes(text)) if part.strip()]
    if not sentences:
        return False
    latest = re.sub(r"[\s，,]", "", sentences[-1])
    if any(mark in latest for mark in ('"', "'", "“", "”", "‘", "’", "「", "」", "：", ":", "?", "？")):
        return False
    # A request can be followed by a brief politeness formula, not an arbitrary
    # tail which might retract it. Never discard real additional speech.
    if latest in {"谢谢", "谢谢你", "谢谢您"} and len(sentences) > 1:
        latest = re.sub(r"[\s，,]", "", sentences[-2])
    if re.fullmatch(r"(?:(?:这(?:一)?题)(?:我)?(?:不会|答不上来))?(?:请)?(?:我们|咱们)?(?:现在)?(?:就)?(?:结束|停止)(?:这场|本次|这次|整个|整场)?面试(?:吧|了)?|"
                    r"我(?:真的)?不想(?:再)?(?:继续)?(?:面试|面)了|(?:我(?:现在)?)?不面了", latest):
        return intent != "stop_interview"
    if re.fullmatch(r"(?:这(?:一)?题|这(?:一)?道题)(?:我)?(?:真的|确实|实在)?(?:不会(?:回答|答)?|答不上来|想不出来)(?:了|啊|呀)?|"
                    r"我(?:真的|确实|实在)?不会(?:这(?:一)?题|这(?:一)?道题|回答)?(?:了|啊|呀)?", latest):
        allowed = {"decline_topic", "finish_topic"} if content == "none" else {"finish_topic"}
        return intent not in allowed
    if re.fullmatch(r"我(?:的回答)?(?:已经)?(?:说|讲|回答|补充)完了(?:呀|啊)?|(?:已经)?没有(?:需要)?补充(?:的)?了|我(?:的)?(?:介绍|回答)(?:已经)?完毕(?:了)?", latest):
        # Completion meaning may be clear while the technical speech is not.
        # A concrete clarification is a legitimate first response; this guard
        # must never force empty content to become a knowledge refusal.
        allowed = {"finish_answer", "clarify", "correction"}
        return intent not in allowed
    if re.fullmatch(r"(?:不对)?(?:我还没说完|我还没有说完|让我再想想|我再想想|等一下我还没说完)", latest):
        return intent != "thinking"
    return False


def _without_quotes(text):
    """Mask mechanically delimited quotations only; retain original offsets."""
    return re.sub(r'“[^”]*”|‘[^’]*’|「[^」]*」|"[^"\n]*"|(?<!\w)\'[^\'\n]*\'(?!\w)',
                  lambda match: " " * len(match.group()), text)


def _completion_only_quotes_control(data, transcript):
    basis = data.get("completion_basis")
    if not basis:
        return False
    signals = {
        "stop_interview": r"(?:结束|停止)[^。！？.!?]{0,8}面试|不想[^。！？.!?]{0,8}面试|不面了",
        "decline_topic": r"不会|答不上|下一题|换个话题",
        "finish_topic": r"不会|答不上|下一题|换个话题|不用再问",
        "finish_answer": r"说完|讲完|回答完|补充完|没有补充|介绍完毕",
    }
    signal = signals.get(data.get("turn_intent"))
    if not signal:
        return False
    # Resolve quotes in the complete transcript: an E sentence may start
    # inside a quotation opened by an earlier sentence. Duplicate text cannot
    # prove which occurrence was cited after reference resolution; require at
    # least one unambiguously unquoted piece of completion evidence.
    masked = _without_quotes(transcript)
    evidence = basis["evidence_quotes"]
    quoted_support = False
    for quote in evidence:
        if not re.search(signal, quote):
            continue
        sources = [masked[match.start():match.start() + len(quote)]
                   for match in re.finditer("(?=" + re.escape(quote) + ")", transcript)]
        if sources and all(re.search(signal, source) for source in sources):
            return False
        if sources and any(not re.search(signal, source) for source in sources):
            quoted_support = True
    # This rejects only a source mismatch; it never infers intent from a word.
    return quoted_support


def validate_current_speech(data, transcript, input_context=None):
    """Validate scope after wire/schema checks; never rewrite model output."""
    if data.get("turn_intent") and _completion_only_quotes_control(data, transcript):
        raise CurrentSpeechContractError()
    scope = _current_speech_scope(transcript, input_context)
    if scope is None:
        return
    start, end, current = scope
    target = data.get("clarification_target")
    if (target and data.get("intent") != "answer_unresolved" and data.get("turn_intent") != "thinking"
            and target["focus_quote"] not in current):
        exchanges = input_context.get("responded_exchanges") or []
        if any(target["focus_quote"] in item.get("request_text", "")
               for item in exchanges if isinstance(item, dict)):
            raise CurrentSpeechContractError()
    if not data.get("turn_intent"):
        return
    if _explicit_control_conflict(current, data["turn_intent"], data["answer_content"]):
        raise CurrentSpeechContractError()
    basis = data.get("completion_basis")
    if basis and not any(
        position < end and position + len(quote) > start
        for quote in basis["evidence_quotes"]
        for match in re.finditer(re.escape(quote), transcript) for position in [match.start()]
    ):
        raise CurrentSpeechContractError()


def validate_conversation_clarification(data):
    """Reject exact conversational sentences miscast as unclear terminology.

    This veto never supplies an intent, claim or completion permission. Quotes,
    negations, mixed technical sentences and genuine recognition complaints
    remain with semantic inference and the normal evidence validators.
    """
    target = data.get("clarification_target")
    if not isinstance(target, dict):
        return
    sentence = re.sub(r"[\s，。！？,.!?]", "", target.get("evidence_quote", ""))
    ready = r"(?:那|好|好的|嗯)?(?:我)?(?:现在)?(?:(?:可以|能)?(?:开始|准备开始))?回答(?:啦|了|吧|啊|吗)?"
    wait = r"(?:请|你)?(?:稍等(?:一下)?|等一下)|我(?:先|再|来)?(?:思考|想)(?:一想|一下)(?:啊|呀)?"
    if re.fullmatch(ready, sentence) or re.fullmatch(wait, sentence):
        raise SemanticContractError("clarification_targets_conversation_control", "$.clarification_target")


def validate_unresolved_completion(data, transcript, clarification_context=None):
    """Require server-proven clarification and fresh completion evidence.

    This validates a private input projection, never model-asserted history.
    The runtime must recheck the selected act, actual playback and capture at
    preparation and commit. This function cannot manufacture that authority.
    """
    if data.get("intent") != "answer_unresolved":
        return
    if data.get("turn_intent") == "stop_interview":
        # Technical uncertainty cannot trap a candidate in the interview.
        # This authorizes only an exit proposal; the runtime independently
        # requires the delivered withdrawal question and a fresh confirmation.
        validate_semantic_turn(data, transcript)
        validate_current_speech(data, transcript, {
            "version": "conversation_input_context.v1", "current_speech_start": 0,
            "current_speech": transcript, "responded_exchanges": [],
        })
        return
    context = clarification_context
    fields = {"version", "request_text", "focus_quote", "reply_text", "question_act_id", "delivered"}
    if (not isinstance(context, dict) or set(context) != fields
            or context.get("version") != "content_clarification.v1" or context.get("delivered") is not True
            or any(not isinstance(context.get(key), str) or not context[key].strip()
                   for key in fields - {"delivered"})
            or len(context["question_act_id"]) > 128):
        raise SemanticContractError("unresolved_clarification_required", "$.intent")
    request, reply, focus = context["request_text"], context["reply_text"], context["focus_quote"]
    target = data.get("clarification_target") or {}
    if not transcript.startswith(request):
        raise SemanticContractError("unresolved_clarification_source_invalid", "$.content_clarification.request_text")
    if transcript[len(request):].strip() != reply.strip():
        raise SemanticContractError("unresolved_clarification_source_invalid", "$.content_clarification.reply_text")
    if focus not in request or target.get("focus_quote") != focus:
        raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.focus_quote")
    if not target.get("evidence_quote") or target["evidence_quote"] not in request:
        raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.evidence_quote")
    start = len(request)
    while start < len(transcript) and transcript[start].isspace():
        start += 1
    # Use the same scope rules even when no general conversation projection is
    # available. Earlier completion statements are not a reply to the question.
    validate_current_speech(data, transcript, {
        "version": "conversation_input_context.v1", "current_speech_start": start,
        "current_speech": transcript[start:], "responded_exchanges": [],
    })


SERVER_CONTROL_DERIVATION_INSTRUCTION = (
    "动作与追问权限采用服务端派生合同semantic_controls.v3。请先判断本人当前有效的turn_intent、实际技术内容、"
    "未解决歧义和真实completion_basis，再填写兼容输出字段；不能为了让suggested_action/followup_allowed匹配而"
    "改变其真实意图、删除技术claims、编造结束依据或抹掉歧义。服务端根据这些已验证事实重新计算动作与追问权限："
    "thinking继续听，repeat重读，pause暂停，clarify/correction澄清，ask_company交资料问答；"
    "有合法依据的完成意图才进入next，首次技术歧义加说完仍须澄清；一般作答且无未解决歧义为accept。"
    "off_topic仅描述清楚跑题，不由缺少技术claims推断不理解；不授权技术追问，实际说完仍须当前完成依据。"
    "若同时输出off_topic和已覆盖本题能力点，只有原文绑定的真实专业claims与合法能力点分区均成立时，"
    "服务端才将这个矛盾标签归一为answer；没有覆盖证据的无关专业主张仍是off_topic。"
    "首次回答同时声明finish_answer及具体未解歧义时，服务端仅在当前完成依据与疑点原文校验通过后，"
    "撤销提交权限并保留claims进入具体澄清；已经提供内容澄清记录时不得借此重复开启同一问题。"
    "这些派生字段不是提交或退出授权，独立结束确认和服务端送达记录仍必须满足。"
)


def normalize_ambiguous_completion(data, transcript, *, input_context=None, content_clarification=None,
                                   new_clarification_scope=False, completion_ranges=None):
    """Revoke a conflicting first completion, never infer a new permission.

    Canonical shape validation precedes this projection. Validate the original
    completion sources before dropping them, so stale/quoted/fabricated control
    cannot disappear from validation. The full semantic and evidence validators
    still run on the projected result. Delivered clarification is handled by
    its separate, source-bound unresolved-completion contract, not re-opened.
    """
    if new_clarification_scope:
        return _normalize_new_clarification_completion(data, transcript, input_context=input_context,
            content_clarification=content_clarification, completion_ranges=completion_ranges)
    target, basis = data.get("clarification_target"), data.get("completion_basis")
    if (data.get("turn_intent") != "finish_answer"
            or data.get("intent") not in {"answer", "clarification_request"}
            or not data.get("ambiguities") or not target or not basis
            or data["confidence"] < .75 or content_clarification is not None
            or (data["intent"] == "answer" and data.get("answer_content") == "none")):
        return data
    try:
        validate_semantic_turn(data, transcript)
    except SemanticContractError as exc:
        if exc.reason_code != "completion_has_unresolved_ambiguity":
            raise
    else:
        return data
    if any(not quote.strip() or quote not in transcript for quote in basis["evidence_quotes"]):
        raise SemanticContractError("semantic_completion_evidence_invalid", "$.completion_basis.evidence_quotes")
    validate_current_speech(data, transcript, input_context)
    if (not target["evidence_quote"].strip() or target["evidence_quote"] not in transcript
            or not target["focus_quote"].strip() or target["focus_quote"] not in target["evidence_quote"]):
        return data  # The unchanged result must still fail the domain validator.
    result = deepcopy(data)
    result.update(intent="clarification_request", completion_basis=None,
                  confidence=min(data["confidence"], .64))
    return derive_semantic_controls(result)


def _normalize_new_clarification_completion(data, transcript, *, input_context, content_clarification,
                                          completion_ranges):
    """Revoke only completion of a separately source-validated new doubt.

    The caller has validated the joint wire branch and resolved its exact E
    evidence. Preserve all meaning/source fields. A delivered question about
    another focus cannot authorize this one, even if the speaker has finished.
    """
    if (data.get("turn_intent") != "finish_answer"
            or data.get("intent") not in {"answer", "clarification_request", "answer_unresolved"}):
        return data
    target, basis = data.get("clarification_target"), data.get("completion_basis")
    context = content_clarification or {}
    request, reply = context.get("request_text"), context.get("reply_text")
    if (not isinstance(request, str) or not isinstance(reply, str) or not reply.strip()
            or not transcript.startswith(request) or transcript[len(request):].strip() != reply.strip()
            or not target or not data.get("ambiguities")
            or target.get("focus_quote") == context.get("focus_quote")
            or not target.get("focus_quote") or not target.get("evidence_quote")
            or target["evidence_quote"] not in transcript or target["focus_quote"] not in target["evidence_quote"]):
        raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.resolution")
    if basis is not None:
        # Check the original ending claim before revoking it. Only the known
        # new-ambiguity/ordinary-answer mismatch may be normalized away.
        try:
            validate_semantic_turn(data, transcript)
        except SemanticContractError as exc:
            if exc.reason_code != "completion_has_unresolved_ambiguity":
                raise
        if any(not quote.strip() or quote not in transcript or quote not in data["evidence_quotes"]
               for quote in basis["evidence_quotes"]):
            raise SemanticContractError("semantic_completion_evidence_invalid", "$.completion_basis.evidence_quotes")
        # Quote equality cannot distinguish an old E from the same words said
        # again. Require every selected occurrence to follow the real receipt.
        current_scope = _current_speech_scope(transcript, input_context)
        scope_start = max(len(request), current_scope[0]) if current_scope else len(request)
        scope_end = min(len(transcript), current_scope[1]) if current_scope else len(transcript)
        if (not completion_ranges or any(not (scope_start <= start < end <= scope_end)
                                         for start, end in completion_ranges)):
            raise CurrentSpeechContractError()
    validate_current_speech(data, transcript, input_context)
    validate_current_speech(data, transcript, {
        "version": "conversation_input_context.v1", "current_speech_start": len(request),
        "current_speech": reply, "responded_exchanges": [],
    })
    result = deepcopy(data)
    result.update(intent="clarification_request", completion_basis=None,
                  confidence=min(data["confidence"], .64))
    return derive_semantic_controls(result)


def normalize_answer_relevance(data, transcript, capability_points):
    """Reconcile a label with the model's own source-bound relevance judgment.

    No words are classified locally. A covered frozen point already asserts
    relevance; an off-topic label cannot erase that same verified evidence.
    Unbound claims, unknown points and genuine ambiguity are never repaired.
    The complete domain validators still run after this narrow projection.
    """
    if (data.get("intent") != "off_topic" or not data.get("turn_intent")
            or data.get("answer_content") not in {"partial", "technical"}
            or not data.get("covered_capability_points") or not data.get("claims")
            or data.get("ambiguities") or data.get("clarification_target")):
        return data
    covered, missing = data["covered_capability_points"], data["missing_capability_points"]
    allowed = set(capability_points)
    if (len(set(covered)) != len(covered) or len(set(missing)) != len(missing)
            or set(covered) & set(missing) or set(covered) | set(missing) != allowed):
        return data
    declared = data.get("evidence_quotes") or []
    if (not declared or any(not quote.strip() or quote not in transcript for quote in declared)
            or any(not claim["claim"].strip() or claim["evidence_quote"] not in declared
                   for claim in data["claims"])):
        return data
    result = deepcopy(data)
    result["intent"] = "answer"
    return derive_semantic_controls(result)


def derive_semantic_controls(data):
    """Derive redundant output controls; never invent meaning or authority.

    The unchanged wire fields remain readable across provider/schema versions.
    All primary facts and completion evidence must pass the canonical semantic
    and current-speech validators after this deterministic projection.
    """
    intent = data.get("turn_intent")
    if intent is None:
        return data
    result = deepcopy(data)
    clarification_after_finish = (
        intent == "finish_answer" and result.get("intent") == "clarification_request"
        and bool(result.get("ambiguities")) and result.get("completion_basis") is None
    )
    continuing = intent == "answering" and result.get("intent") == "not_finished"
    result["followup_allowed"] = (intent == "answering" and not continuing) or (
        intent == "finish_answer" and result.get("intent") != "answer_unresolved"
        and not clarification_after_finish
    )
    if result.get("intent") == "off_topic":
        result["followup_allowed"] = False
    if clarification_after_finish:
        action = "clarify"
    elif continuing:
        action = "continue_listening"
    elif intent in {"finish_answer", "finish_topic", "decline_topic", "stop_interview"}:
        action = "next"
    else:
        action = {"thinking": "continue_listening", "repeat": "repeat", "pause": "pause",
                  "clarify": "clarify", "correction": "clarify", "ask_company": "respond_company"}.get(intent)
        if action is None:
            action = "clarify" if result.get("ambiguities") else "accept"
    result["suggested_action"] = action
    return result


def semantic_turn_fields(*, evidence_ids=None):
    reference_name = "evidence_ids" if evidence_ids is not None else "evidence_quotes"
    reference = ({"type": "string", "enum": list(evidence_ids)} if evidence_ids is not None else
                 {"type": "string", "minLength": 1, "maxLength": 300})
    return {
        "answer_content": {"type": "string", "enum": ["technical", "partial", "none"],
                           "description": "technical/partial必须有至少一个含义确定且有原文依据的claims；一句确定做法即使未命中能力点也可partial。只有无法解释的技术残片时用none、claims=[]，保留ambiguities；none不代表不会或拒答。"},
        "turn_intent": {"type": "string", "enum": [
            "answering", "thinking", "finish_answer", "finish_topic", "decline_topic", "stop_interview", "repeat", "clarify", "correction", "pause",
        ], "description": "当前交流意图，与已有回答独立；实际回答后要求换题为finish_topic，保留claims，不能降为无内容拒答。"},
        "followup_allowed": {"type": "boolean"},
        "completion_basis": {
            "type": ["object", "null"], "required": ["source", reference_name],
            "properties": {
                "source": {"type": "string", "enum": ["explicit_server_intent"]},
                reference_name: {"type": "array", "minItems": 1, "maxItems": 4,
                                 "uniqueItems": True, "items": reference},
            }, "additionalProperties": False,
        },
    }


def with_semantic_turn_schema(schema, *, evidence_ids=None, company_questions=False):
    result = deepcopy(schema)
    fields = semantic_turn_fields(evidence_ids=evidence_ids)
    result["properties"].update(fields)
    result["required"].extend(fields)
    result["properties"]["claims"]["description"] = (
        "仅列候选人实际表达且含义可确定的技术做法或判断，并引用其原文证据；控制请求和无法解释的技术残片不是claim。"
        "partial/technical必须非空，none必须为空；answer_unresolved也遵守此对应关系。保留所有可靠主张，不能为凑claims猜测残片含义。"
    )
    if company_questions:
        from app.core.prompt.company_questions import company_question_field
        result["properties"]["company_question"] = company_question_field(evidence_ids=evidence_ids)
        result["properties"]["turn_intent"]["enum"].append("ask_company")
        result["properties"]["intent"]["enum"].append("company_question")
        result["properties"]["suggested_action"]["enum"].append("respond_company")
    # Produce meaning before choosing the action. Keep exactly the same fields
    # and validators; ordering cannot grant authority to an invalid result.
    first = ["answer_content", "turn_intent"]
    order = first + [name for name in result["properties"]
                     if name not in {*first, "suggested_action"}] + ["suggested_action"]
    result["properties"] = {name: result["properties"][name] for name in order}
    result["required"] = [name for name in order if name in result["required"]]
    return result


def validate_semantic_turn(data, transcript):
    """Cross-field and verbatim constraints after wire reference resolution."""
    if "turn_intent" not in data or data["turn_intent"] is None:
        return
    intent, content, basis = data["turn_intent"], data["answer_content"], data["completion_basis"]
    company = data.get("company_question")
    if intent == "ask_company":
        if (not company or basis or data["followup_allowed"] or data["suggested_action"] != "respond_company"
                or data["confidence"] < .75 or data["ambiguities"]
                or data["intent"] != ("company_question" if content == "none" else "answer")
                or transcript[company["start"]:company["end"]] != company["quote"]
                or company["end"] <= company["start"]):
            raise SemanticContractError("company_question_contract_invalid", "$.company_question")
        # E references are sentences and can contain BOTH technical evidence
        # and a question. Require a verbatim technical claim when they share
        # one reference so its preservation is mechanically provable.
        normalized = lambda value: "".join(c.casefold() for c in value if c.isalnum())
        for claim in data["claims"]:
            evidence, quote = claim["evidence_quote"], company["quote"]
            if evidence in quote:
                raise SemanticContractError("company_question_covers_technical_evidence", "$.company_question")
            if quote in evidence:
                technical = normalized(claim["claim"])
                remaining = normalized(evidence.replace(quote, "", 1))
                if not technical or technical not in remaining or technical in normalized(quote):
                    raise SemanticContractError("company_question_covers_technical_evidence", "$.company_question")
    elif company is not None or data["suggested_action"] == "respond_company" or data["intent"] == "company_question":
        raise SemanticContractError("company_question_contract_invalid", "$.company_question")
    unresolved = data["intent"] == "answer_unresolved"
    if data["intent"] == "off_topic" and (
        intent not in {"answering", "thinking", "finish_answer", "finish_topic", "decline_topic", "stop_interview"}
        or not str(data.get("answer_summary") or "").strip() or not data["evidence_quotes"]
        or data["covered_capability_points"] or data["ambiguities"] or data.get("clarification_target")
        or data["followup_allowed"]
    ):
        raise SemanticContractError("off_topic_contract_invalid", "$.intent")
    if unresolved and (intent not in {"finish_answer", "stop_interview"} or not data["ambiguities"] or data["clarification_target"] is None):
        raise SemanticContractError("unresolved_answer_contract_invalid", "$.intent")
    # Candidate meaning and permission to act are separate: the candidate may
    # clearly finish speaking while the system must clarify a source ambiguity.
    # Only this complete, non-committing combination can omit ending authority.
    target = data.get("clarification_target")
    clarification_after_finish = (
        data["intent"] == "clarification_request" and intent == "finish_answer"
        and data["suggested_action"] == "clarify" and data["confidence"] < .65
        and data["followup_allowed"] is False and basis is None and bool(data["ambiguities"])
        and isinstance(target, dict)
        and isinstance(target.get("evidence_quote"), str) and bool(target["evidence_quote"].strip())
        and target["evidence_quote"] in transcript
        and isinstance(target.get("focus_quote"), str) and bool(target["focus_quote"].strip())
        and target["focus_quote"] in target["evidence_quote"]
    )
    ending = intent in {"finish_answer", "finish_topic", "decline_topic", "stop_interview"} and not clarification_after_finish
    if bool(basis) != ending:
        raise SemanticContractError("completion_basis_required" if ending else "completion_basis_unexpected", "$.completion_basis")
    if content == "none" and (data["claims"] or data["covered_capability_points"]):
        raise SemanticContractError("content_none_has_technical_evidence", "$.answer_content")
    if content != "none" and not data["claims"]:
        raise SemanticContractError("technical_content_claims_missing", "$.claims")
    if data["intent"] == "answer_declined" and content != "none":
        raise SemanticContractError("declined_answer_has_technical_content", "$.intent")
    if data["intent"] == "answer_declined" and intent not in {"finish_topic", "decline_topic", "stop_interview"}:
        raise SemanticContractError("declined_answer_completion_scope_invalid", "$.turn_intent")
    if ending:
        if data["confidence"] < .75:
            raise SemanticContractError("completion_confidence_insufficient", "$.confidence")
        if data["suggested_action"] != "next":
            raise SemanticContractError("completion_action_invalid", "$.suggested_action")
        if data["followup_allowed"] != (intent == "finish_answer" and not unresolved and data["intent"] != "off_topic"):
            raise SemanticContractError("completion_followup_permission_invalid", "$.followup_allowed")
        if intent == "finish_answer" and content == "none" and not unresolved and data["intent"] != "off_topic":
            raise SemanticContractError("finish_answer_without_content", "$.answer_content")
        if data["ambiguities"] and not unresolved:
            raise SemanticContractError("completion_has_unresolved_ambiguity", "$.ambiguities")
        if not unresolved and data["intent"] != "off_topic" and (data["intent"] not in {"answer", "answer_declined"} or (content == "none") != (data["intent"] == "answer_declined")):
            raise SemanticContractError("completion_intent_content_mismatch", "$.intent")
        if any(not quote.strip() or quote not in transcript for quote in basis["evidence_quotes"]):
            raise SemanticContractError("semantic_completion_evidence_invalid", "$.completion_basis.evidence_quotes")
    if intent in {"thinking", "repeat", "pause", "clarify", "correction"} and data["followup_allowed"]:
        raise SemanticControlContractError()
    if intent == "thinking" and data["suggested_action"] != "continue_listening":
        raise SemanticControlContractError()
    if intent in {"repeat", "pause"} and data["suggested_action"] != intent:
        raise SemanticControlContractError()
