"""Length limits for model inputs must not revoke grounded interview probes."""
import pytest

from app.core.prompt.understanding_references import understanding_references
from app.core.prompt.validation import validate_structured_response
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from test_formal_followup_056 import completed_answer
from test_prepared_turn_decision import Gateway, TRANSCRIPT, _input, _service


@pytest.mark.anyio
@pytest.mark.parametrize("answer_chars", [4000, 4001, 8000])
async def test_long_answer_keeps_same_verified_followup_scope(answer_chars):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview.update(_semantic_first=True, _answer_completion_confirmed=True)
    service = _service(Gateway(completed_answer(selected=False)))
    understanding, _ = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.followup_allowed
    # Retain identical grounded claims and their source quotes while adding
    # synthetic answer detail. This changes length, not scope or permission.
    padding = ("恢复过程还保留审计记录。" * answer_chars)[:answer_chars - len(utterance.text)]
    longer = utterance.model_copy(update={"text": padding + utterance.text})
    assert len(longer.text) == answer_chars
    assert all(quote in longer.text for quote in understanding.evidence_quotes)
    scope = service._followup_scope(interview, turn, longer, understanding)
    assert "targets" in scope, "A model-input budget must use a bounded probe, not disable it: %s" % scope.get("reason")
    assert scope["targets"] == service._followup_scope(interview, turn, utterance, understanding)["targets"]
    assert scope["evidence"] == understanding.evidence_quotes


@pytest.mark.anyio
@pytest.mark.parametrize("answer_chars", [4001, 8000])
async def test_long_answer_uses_complete_understanding_then_bounded_grounded_probe(answer_chars):
    utterance, turn, interview = _input()
    marker = "synthetic_background_060"
    ending = "。" + TRANSCRIPT + "我讲完了。"
    padding = (marker + "审计背景" * answer_chars)[:answer_chars - len(ending)]
    text = padding + ending
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _answer_completion_confirmed=True)

    class LongAnswerGateway:
        def __init__(self):
            self.requests = []

        async def invoke(self, capability, request):
            self.requests.append(request)
            if request.purpose == "interview_turn_understanding":
                assert request.metadata["prompt_version"].startswith("interview_turn_understanding.")
                assert request.metadata["transcript"] == text
                content = "\n".join(message.content for message in request.messages)
                assert marker in content and TRANSCRIPT in content and "我讲完了。" in content
                references = understanding_references(text, ["幂等键", "确认序号"])["evidence"]
                claim_id = next(key for key, value in references.items() if value == TRANSCRIPT)
                finish_id = next(key for key, value in references.items() if value == "我讲完了。")
                data = completed_answer(selected=False)["understanding"]
                data["claims"][0]["evidence_id"] = claim_id
                data["evidence_ids"] = [claim_id]
                data["completion_basis"]["evidence_ids"] = [finish_id]
            else:
                assert request.purpose == "controlled_followup"
                content = "\n".join(message.content for message in request.messages)
                assert marker not in content and TRANSCRIPT in content
                assert len(content) < 4000, "Probe input must not repeat the full long answer"
                data = {"selected": True, "evidence_quote": TRANSCRIPT,
                    "target_capability_points": ["确认序号"],
                    "question_text": "你刚才说会记录恢复位置，这个位置是在什么时机更新的？",
                    "rationale": "尚未说明位置更新和处理完成的先后关系。", "difficulty": "mid",
                    "sensitive_attribute_inference": False, "leaks_answer": False}
            validate_structured_response(data, request.json_schema)
            return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="audit-060", request_id="synthetic", latency_ms=0))

    gateway = LongAnswerGateway()
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert followup["selected"] and followup["parent_turn_id"] == turn["id"]
    assert followup["evidence_quotes"] == [TRANSCRIPT]
    assert [request.purpose for request in gateway.requests] == ["interview_turn_understanding", "controlled_followup"]
