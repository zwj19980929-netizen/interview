"""Normal answers require the runtime confirmation at the final service gate."""
from copy import deepcopy
from dataclasses import replace

import pytest

from app.core.errors import ApiError
from app.model_gateway.schemas import TranscriptSegment
from test_content_clarification_066 import prepare_unresolved
from test_off_topic_semantics_068 import off_topic_response
from test_prepared_decision_binding import _payload, _setup
from test_prepared_turn_decision import Gateway


TECHNICAL = "我使用请求标识记录处理结果，避免重试时重复创建。"
IRRELEVANT = "周末我和朋友一起玩桌游。"


async def _ordinary(*, intent="answer", tail="", followup=False, confirmed=False):
    service, original_final = _setup()
    if followup:
        with service.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            turn = session["turns"][0]
            root = deepcopy(turn)
            root.update(id="turn_root", root_turn_id="turn_root", status="completed", order=1)
            turn.update(is_followup=True, root_turn_id="turn_root", parent_turn_id="turn_root",
                        followup_depth=1, order=2)
            session["turns"].insert(0, root)
            session["turn_ids"].insert(0, root["id"])
            tx.interview_sessions.update(session, expected_version=session["version"])
    text = (TECHNICAL if intent == "answer" else IRRELEVANT) + tail
    final = original_final.model_copy(update={"text": text, "segments": [
        TranscriptSegment(text=text, confidence=.95, start_ms=0, end_ms=2000)]})
    wire = off_topic_response(text, ["幂等键"], technical=intent == "answer",
                              relevant=intent == "answer")["understanding"]
    service.conversation.gateway = Gateway(wire)
    prepared = await service.prepare_streaming_decision(
        "iv_binding", "turn_binding", final, snapshot_ref="private-file://synthetic_prefix",
        semantic_first=True, completion_confirmed=confirmed,
    )
    assert prepared.understanding.problem is None
    assert prepared.understanding.turn_intent == "finish_answer"
    return service, final, prepared


@pytest.mark.anyio
@pytest.mark.parametrize("intent", ["answer", "off_topic"])
@pytest.mark.parametrize("tail", ["", "我已经说完了。"])
@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("entry", ["preflight", "commit"])
async def test_unconfirmed_normal_finish_cannot_advance_main_or_followup(intent, tail, followup, entry):
    service, final, prepared = await _ordinary(intent=intent, tail=tail, followup=followup)
    before = service.get_interview("iv_binding")
    # Both a fabricated finish label on a technical sentence and an actual
    # spoken finish keep their evidence; neither supplies the runtime receipt.
    assert prepared.understanding.completion_basis.evidence_quotes
    assert not service.conversation.can_complete_without_confirmation(prepared.understanding, final.text)
    with pytest.raises(ApiError) as caught:
        if entry == "preflight":
            service.assert_prepared_streaming_decision(prepared, final, "org_default")
        else:
            await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert caught.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before
    assert before["current_turn_id"] == "turn_binding" and not before["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("intent", ["answer", "off_topic"])
async def test_internal_confirmed_path_accepts_the_complete_answer_and_new_reply(intent):
    # This exercises the service boundary after runtime confirmation. The
    # spoken question/new-reply sequence is covered by the endpoint tests.
    service, final, prepared = await _ordinary(intent=intent, tail="没有补充了。", confirmed=True)
    assert prepared.completion_confirmed
    assert not service.conversation.can_complete_without_confirmation(prepared.understanding, final.text)
    service.assert_prepared_streaming_decision(prepared, final, "org_default")
    result = await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert result["accepted"] is True and result["answer"]["final_transcript"] == final.text
    assert len(service.get_interview("iv_binding")["answers"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("turn_intent", [None, "unrecognized_intent", "answering"])
async def test_unknown_or_nonending_shape_cannot_reuse_a_completion_basis(turn_intent):
    service, final, prepared = await _ordinary(tail="我已经说完了。")
    changed = prepared.understanding.model_copy(update={"turn_intent": turn_intent})
    prepared = replace(prepared, understanding=changed)
    before = service.get_interview("iv_binding")
    assert not service.conversation.can_complete_without_confirmation(changed, final.text)
    with pytest.raises(ApiError) as caught:
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert caught.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_confirmed_answer_cannot_be_reused_for_new_words():
    service, final, prepared = await _ordinary(tail="没有补充了。", confirmed=True)
    changed = final.model_copy(update={"text": final.text + "不对，我还需要补充。"})
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as caught:
        await service.submit_streaming_answer("iv_binding", _payload(changed), prepared_decision=prepared)
    assert caught.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
@pytest.mark.parametrize("receipt", ["missing", "unknown_shape", "old_reply"])
async def test_unresolved_exception_still_requires_the_current_delivered_clarification(receipt):
    service, final, prepared = await prepare_unresolved()
    assert service.conversation.can_complete_without_confirmation(prepared.understanding, final.text)
    if receipt == "missing":
        prepared = replace(prepared, content_clarification=None)
    elif receipt == "unknown_shape":
        prepared = replace(prepared, content_clarification={**prepared.content_clarification, "extra": True})
    else:
        basis = prepared.understanding.completion_basis.model_copy(update={"evidence_quotes": ["我的介绍完毕。"]})
        prepared = replace(prepared, understanding=prepared.understanding.model_copy(update={"completion_basis": basis}))
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError):
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
@pytest.mark.parametrize("turn_intent,tail", [("finish_topic", "下一题吧。"), ("decline_topic", "这题我不会。")])
async def test_explicit_topic_boundary_remains_a_distinct_unconfirmed_service_path(turn_intent, tail):
    service, original_final = _setup()
    text = IRRELEVANT + tail
    final = original_final.model_copy(update={"text": text, "segments": [
        TranscriptSegment(text=text, confidence=.95, start_ms=0, end_ms=2000)]})
    data = off_topic_response(text, ["幂等键"], intent=turn_intent)["understanding"]
    service.conversation.gateway = Gateway(data)
    prepared = await service.prepare_streaming_decision("iv_binding", "turn_binding", final,
        snapshot_ref="private-file://synthetic_prefix", semantic_first=True)
    assert prepared.understanding.problem is None
    assert service.conversation.can_complete_without_confirmation(prepared.understanding, text)
    service.assert_prepared_streaming_decision(prepared, final, "org_default")
    result = await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert result["accepted"] and result["answer"]["final_transcript"] == text
