import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.conversation_reception import RECEPTION_SPEECH, RECEPTION_SPEECH_VERSION, validate_reception_response
from app.core.prompt.validation import StructuredResponseValidationError
from test_prepared_turn_decision import Gateway, _service


def test_reception_distinguishes_unrelated_narration_from_a_social_request():
    contract = prompt_contract("conversation_reception", {"text": "我昨天去山里看了日出。我说完了。"})
    assert contract.version == "conversation_reception.v9"
    system = contract.messages[0].content
    assert "叙述跑题本身不是向你提出请求" in system
    assert "内容无关不等于转写不清" in system
    assert "最后明确说完或不再回答，必须other" in system
    assert "所有偏题发言都由正式理解路径统一处理" in system


def test_off_topic_guidance_is_versioned_and_does_not_claim_a_transcription_error():
    assert RECEPTION_SPEECH_VERSION == "conversation_reception_speech.v4"
    assert "回到当前这道题" in RECEPTION_SPEECH["off_topic"]
    assert "如果不会或已经说完" in RECEPTION_SPEECH["off_topic"]
    assert "识别" not in RECEPTION_SPEECH["off_topic"]


@pytest.mark.anyio
async def test_completed_unrelated_narration_leaves_completion_to_semantic_path():
    text = "我最近开始练习素描，周末画了一棵树。我说完了。"
    gateway = Gateway({"kind": "other", "confidence": .98, "evidence_id": "E1", "reply_text": ""})
    decision = await _service(gateway).classify_reception(text, "org_default", question="如何处理任务重试？")
    assert decision["kind"] == "other"
    assert decision["reply_text"] == ""
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("reply", ["你能回到当前问题吗？", "请讲讲你的任务重试方案？"])
def test_unrelated_social_redirect_cannot_become_an_unapproved_question(reply):
    contract = prompt_contract("conversation_reception", {"text": "能聊聊绘画吗？"})
    with pytest.raises(StructuredResponseValidationError, match="conversation contract") as exc:
        validate_reception_response({"kind": "interview_dialogue", "confidence": .98,
                                     "evidence_id": "E1", "reply_text": reply}, contract.response_schema)
    assert exc.value.reason_code == "reception_cannot_ask_question"


def test_completed_answer_cannot_be_intercepted_as_a_wait_request():
    text = "我说完了。"
    contract = prompt_contract("conversation_reception", {"text": text})
    with pytest.raises(StructuredResponseValidationError) as exc:
        validate_reception_response({"kind": "wait", "confidence": .98, "evidence_id": "E1",
                                     "reply_text": "好的，继续想吧。"}, contract.response_schema,
                                    current_speech=text)
    assert exc.value.reason_code == "reception_current_control_conflict"
