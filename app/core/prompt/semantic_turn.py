"""Versioned semantic-first turn policy and its exact evidence contract.

These fields describe candidate meaning, independently of technical coverage.
Only the runtime may turn a validated, current final into an answer commit.
"""

from copy import deepcopy


SEMANTIC_TURN_INSTRUCTION = (
    "本轮采用语义优先接话合同semantic_turn.v3。服务端尚未向候选人询问是否补充，"
    "不得假定已发生独立确认。分别判断answer_content与turn_intent，不按关键词匹配。"
    "answer_content=technical/partial/none只描述是否存在实际专业回答，不是得分。"
    "turn_intent=answering/thinking/finish_topic/decline_topic/stop_interview/repeat/clarify/correction/pause。"
    "明确本人本题不会且不再作答用none+decline_topic；已有技术内容后说下一题用partial或technical"
    "+finish_topic，保留技术claims和证据，绝不能将前文丢掉。"
    "finish_topic/decline_topic/stop_interview必须confidence至少0.75，suggested_action=next，"
    "followup_allowed=false，并提供completion_basis={source:explicit_server_intent,evidence_ids:[E编号]}。"
    "这里只能引用当前完整原文中实际表达结束意图的证据，不把静音、普通陈述结束或模型把握当结束授权。"
    "‘我不想继续面试了’明确结束整场时使用stop_interview，不能误解成只跳过本题。"
    "这时有实际技术内容仍intent=answer，否则answer_declined，原文和已答内容全部保留。"
    "其他情况completion_basis必须null；疑问、转述、否定、引用的不会都不是本人拒答。"
    "‘Celery我不会，不过我用Redis做过队列’仍应提取实际经验，不能凭前半句判结束。"
    "‘同事说他不会，我就接手了’是在讲经历；‘让我想一下’、‘等一下我还没说完’"
    "是thinking与continue_listening，必须继续听，不能问是否还有补充。"
    "短暂思考与暂停整场必须分开：本人说‘稍等’、‘稍等。没有。嗯。’而没有明确撤回等待，"
    "使用intent=not_finished、turn_intent=thinking、suggested_action=continue_listening，"
    "answer_content=none、claims=[]、covered_point_ids=[]、followup_allowed=false、completion_basis=null。"
    "已有专业回答后要求等一下仍保留claims及partial/technical，turn_intent=thinking，"
    "suggested_action=continue_listening；不能因等待将已有专业回答清空。"
    "只有明确请求暂停整场面试、离开后再回来，才使用intent=pause、turn_intent=pause、"
    "suggested_action=pause、followup_allowed=false、completion_basis=null；暂停不是结束回答，"
    "不得为暂停生成explicit_server_intent结束依据。绝不能返回pause与continue_listening的矛盾组合。"
    "‘没有’或‘嗯’不能撤销更早的等待请求，也不能在未询问补充时单独充当结束确认。"
    "最新明确改口优先于更早结束意图，累计原文含‘下一题，不对我还没说完’不能结束。"
    "明确纠正或投诉转写必须先处理争议，turn_intent=correction或clarify、completion_basis=null，"
    "只有未解决疑点才clarify；上下文已消除疑点仍保留原文并正常理解。"
    "repeat/pause分别使用repeat/pause动作。普通answering没有结束依据时可以建议accept，"
    "运行时会在需要时确认，不得凭此建议捏造结束依据。"
    "followup_allowed描述是否允许继续本话题；明确结束、思考或控制请求均为false。"
)

# A real supplement reply permits completing this answer. It must still be
# understood semantically so a request to stop the whole interview survives.
SEMANTIC_CONFIRMED_TURN_INSTRUCTION = SEMANTIC_TURN_INSTRUCTION.replace(
    "本轮采用语义优先接话合同semantic_turn.v3。服务端尚未向候选人询问是否补充，"
    "不得假定已发生独立确认。",
    "本轮采用语义接话合同semantic_turn.v4。服务端已取得本次回答的真实结束确认，"
    "确认来源由服务端保证，不能从静音或普通陈述伪造。"
    "‘没有补充了’只结束本次回答；没有明确拒绝追问或结束本话题时仍可answering且允许追问。"
    "如果确认回复明确要求结束整场面试，必须stop_interview；明确跳过话题则finish_topic。",
)


def semantic_turn_fields(*, evidence_ids=None):
    reference_name = "evidence_ids" if evidence_ids is not None else "evidence_quotes"
    reference = ({"type": "string", "enum": list(evidence_ids)} if evidence_ids is not None else
                 {"type": "string", "minLength": 1, "maxLength": 300})
    return {
        "answer_content": {"type": "string", "enum": ["technical", "partial", "none"]},
        "turn_intent": {"type": "string", "enum": [
            "answering", "thinking", "finish_topic", "decline_topic", "stop_interview", "repeat", "clarify", "correction", "pause",
        ]},
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
    if company_questions:
        from app.core.prompt.company_questions import company_question_field
        result["properties"]["company_question"] = company_question_field(evidence_ids=evidence_ids)
        result["properties"]["turn_intent"]["enum"].append("ask_company")
        result["properties"]["intent"]["enum"].append("company_question")
        result["properties"]["suggested_action"]["enum"].append("respond_company")
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
            raise ValueError("company_question_contract_invalid")
        # E references are sentences and can contain BOTH technical evidence
        # and a question. Require a verbatim technical claim when they share
        # one reference so its preservation is mechanically provable.
        normalized = lambda value: "".join(c.casefold() for c in value if c.isalnum())
        for claim in data["claims"]:
            evidence, quote = claim["evidence_quote"], company["quote"]
            if evidence in quote:
                raise ValueError("company_question_covers_technical_evidence")
            if quote in evidence:
                technical = normalized(claim["claim"])
                remaining = normalized(evidence.replace(quote, "", 1))
                if not technical or technical not in remaining or technical in normalized(quote):
                    raise ValueError("company_question_covers_technical_evidence")
    elif company is not None or data["suggested_action"] == "respond_company" or data["intent"] == "company_question":
        raise ValueError("company_question_contract_invalid")
    ending = intent in {"finish_topic", "decline_topic", "stop_interview"}
    if bool(basis) != ending:
        raise ValueError("semantic_completion_contract_invalid")
    if content == "none" and (data["claims"] or data["covered_capability_points"]):
        raise ValueError("semantic_content_contract_invalid")
    if content != "none" and not data["claims"]:
        raise ValueError("semantic_content_contract_invalid")
    if data["intent"] == "answer_declined" and content != "none":
        raise ValueError("semantic_content_contract_invalid")
    if ending:
        if (data["confidence"] < .75 or data["followup_allowed"] or data["suggested_action"] != "next"
                or data["ambiguities"] or data["intent"] not in {"answer", "answer_declined"}
                or (content == "none") != (data["intent"] == "answer_declined")):
            raise ValueError("semantic_completion_contract_invalid")
        if any(not quote.strip() or quote not in transcript for quote in basis["evidence_quotes"]):
            raise ValueError("semantic_completion_evidence_invalid")
    if intent in {"thinking", "repeat", "pause", "clarify", "correction"} and data["followup_allowed"]:
        raise ValueError("semantic_control_contract_invalid")
    if intent == "thinking" and data["suggested_action"] != "continue_listening":
        raise ValueError("semantic_control_contract_invalid")
    if intent in {"repeat", "pause"} and data["suggested_action"] != intent:
        raise ValueError("semantic_control_contract_invalid")
