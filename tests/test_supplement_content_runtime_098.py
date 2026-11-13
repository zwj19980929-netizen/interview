"""Content correction is a continuation of the same bounded, current reply."""
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.validation import StructuredResponseValidationError
from test_answer_endpoint import _final
from test_answer_reuse_078 import _append
from test_semantic_tail_runtime_096 import exchange
from test_supplement_completion_proof_096 import prepared_harness
from test_supplement_format_correction_096 import service_with_wire_responses, valid


def rejected(reason="reception_current_control_conflict", path="$.kind"):
    return StructuredResponseValidationError("PRIVATE_RESPONSE", reason_code=reason, schema_path=path)


@pytest.mark.anyio
@pytest.mark.parametrize("reason,path", [
    ("reception_current_control_conflict", "$.kind"),
    ("reception_cannot_ask_question", "$.reply_text"),
])
async def test_content_failure_changes_second_request_without_new_budget(reason, path):
    endpoint, confirmation, clock, notices, commits, spoken = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        raise rejected(reason, path)

    endpoint.capture.classify_supplement_reply = classify
    final = _final("合成回答结束。合成补充控制回复。")
    try:
        await confirmation._reply(endpoint, final)
        await confirmation._reply(endpoint, final)
        assert calls[0].get("correction_reason") is None
        assert calls[1].get("correction_reason") == reason
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2 and endpoint.preparation_blocked
        assert endpoint._supplement_failures == 2 and endpoint._prepare_failures == 0
        await confirmation.control_step(endpoint, quiet=10)
        assert spoken == ["reception_recovery_clarification"]
        assert not commits and endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["revision", "epoch", "final", "boundary", "question"])
async def test_content_correction_is_not_reused_after_source_changes(change):
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        raise rejected()

    endpoint.capture.classify_supplement_reply = classify
    final = _final("合成回答结束。合成当前回复。")
    try:
        await confirmation._reply(endpoint, final)
        if change == "revision":
            endpoint.speech_started(source="audio")
        elif change == "epoch":
            assert endpoint.retry_answer_processing()
        elif change == "final":
            final = final.model_copy(deep=True)
            final.provider.request_id = "new-provider-source"
        elif change == "boundary":
            confirmation._boundary_final = confirmation._boundary_final.model_copy(deep=True)
            confirmation._boundary_final.provider.request_id = "new-boundary-source"
        else:
            confirmation._reply_question = "不同的合成确认问题。"
        endpoint._proposal_revision = endpoint.revision
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2 and calls[1].get("correction_reason") is None
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [
    rejected("reception_cannot_ask_question", "$.kind"),
    rejected("reception_current_control_conflict", "$.reply_text"),
    rejected("supplement_confirmation_proof_required", "$.intent"),
    ApiError("reception_current_control_conflict", "PRIVATE_EXCEPTION", status_code=409),
])
async def test_unknown_or_untrusted_failure_cannot_select_correction(failure):
    endpoint, confirmation, _, _, _, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        raise failure

    endpoint.capture.classify_supplement_reply = classify
    try:
        final = _final("合成回答结束。合成当前回复。")
        await confirmation._reply(endpoint, final)
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2 and all(call.get("correction_reason") is None for call in calls)
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [None, "reason", "source", "context", "owner"])
async def test_evidence_layer_binds_correction_to_actual_failure_and_current_source(tmp_path, monkeypatch, change):
    harness = await prepared_harness(tmp_path, monkeypatch)
    calls = []

    async def classify(reply, organization_id, **context):
        calls.append(context)
        raise rejected()

    harness.stream.interviews.conversation.classify_supplement_reply = classify
    reply = "合成测试，我已经说完了。"
    final = _append(harness.final, reply)
    harness.stream.stream._final = final
    async def invoke(reason=None):
        return await harness.chain.classify_supplement_reply(reply, final=final,
            boundary_final=harness.final, correction_reason=reason)

    with pytest.raises(StructuredResponseValidationError):
        await invoke()
    reason = "reception_current_control_conflict"
    if change == "reason":
        reason = "reception_cannot_ask_question"
    elif change == "source":
        final.provider.request_id = "new-provider-source"
    elif change == "context":
        with harness.chain.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            session["turns"][0]["question_spoken_text"] = "新的合成题目。"
            tx.interview_sessions.update(session, expected_version=session["version"])
    elif change == "owner":
        harness.coordinator.release(harness.owner)
    if change == "owner":
        with pytest.raises(ApiError):
            await invoke(reason)
        assert len(calls) == 1
    else:
        with pytest.raises(StructuredResponseValidationError):
            await invoke(reason)
        assert len(calls) == 2
        assert calls[-1]["correction_reason"] == (reason if change is None else None)
    assert not harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
async def test_real_adapter_control_conflict_corrects_same_reply_and_preserves_finish_proof(tmp_path, monkeypatch):
    harness = await prepared_harness(tmp_path, monkeypatch)
    service, calls, store = service_with_wire_responses([
        json.dumps(valid(intent="presence", reply_text="我在。")), json.dumps(valid()),
    ])
    harness.stream.interviews.conversation = service
    endpoint, confirmation, _, _, commits, _ = exchange()
    confirmation._remember_boundary(harness.final)
    endpoint.capture.classify_supplement_reply = harness.chain.classify_supplement_reply
    accepted = []

    async def accept(_endpoint, final, **kwargs):
        prepared = harness.chain.confirm_prepared_decision(harness.prepared, harness.final,
            harness.final, final, reply_text=kwargs["reply_text"], decision=kwargs["decision"],
            question_delivered=True)
        assert prepared is not None and prepared.completion_confirmed
        assert prepared.text == final.text
        assert prepared.understanding.claims == harness.prepared.understanding.claims
        accepted.append(prepared)

    confirmation._accept_finish = accept
    final = _append(harness.final, "我已经说完了。")
    harness.stream.stream._final = endpoint.capture.current_final = final
    try:
        await confirmation._reply(endpoint, final)
        assert len(calls) == 1 and not accepted
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2 and len(accepted) == 1
        normal, corrected = [call["payload"]["messages"] for call in calls]
        assert normal != corrected
        assert [m for m in normal if m["role"] == "user"] == [m for m in corrected if m["role"] == "user"]
        assert "reception_current_control_conflict" in json.dumps(corrected)
        assert [row["attempt"] for row in store.model_invocations] == [1, 1]
        assert not commits and endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["delivered", "interrupted", "unavailable", "new_input", "owner_changed"])
async def test_repaired_presence_uses_normal_delivery_and_never_commits(tmp_path, monkeypatch, outcome):
    from test_supplement_dialogue_084 import _setup, _reply, _deliver, _select

    harness = await _setup(tmp_path, monkeypatch)
    original_text = harness.capture.current_final.text
    original_boundary = harness.confirmation.boundary
    selected_before = len(harness.selected)
    spoken = []
    harness.gateway.intent = "presence"
    harness.gateway.reply_text = "我在，你还需要补充什么吗？"

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append((kind, kwargs))
        if outcome == "unavailable":
            return False
        if outcome == "new_input":
            harness.endpoint.speech_started(source="audio")
            guard()
        if outcome == "owner_changed":
            harness.coordinator.release(harness.owner)
            harness.stream._assert_commit_allowed()
        harness.spoken.append(kind)
        return _select(harness, kind, guard, kwargs)

    harness.confirmation.speak = speak
    try:
        if outcome in {"new_input", "owner_changed"}:
            with pytest.raises(ApiError):
                await _reply(harness, "你在听吗？")
        else:
            await _reply(harness, "你在听吗？")
        assert len(spoken) == 1 and spoken[0][0] == "reception_presence"
        phrase = spoken[0][1]["reply_text"]
        assert phrase.strip() and "？" not in phrase and "?" not in phrase
        assert len(harness.gateway.requests) == 1 and harness.gateway.kinds == ["small"]
        if outcome in {"delivered", "interrupted"}:
            assert len(harness.selected) == selected_before + 1
            await _deliver(harness, reason="agent_finished" if outcome == "delivered" else "interrupted")
            session = harness.stream.interviews.get_interview("iv_binding")
            receipt = session["turns"][0]["conversation_acts"][-1]["selection_receipts"][-1]["delivery"]
            assert receipt["status"] == outcome
        else:
            assert len(harness.selected) == selected_before
            assert harness.confirmation.boundary == original_boundary
        session = harness.stream.interviews.get_interview("iv_binding")
        assert session["status"] == "in_progress" and not session["answers"]
        assert not harness.committed and not harness.confirmation.confirmed
        assert harness.capture.is_open and harness.capture.current_final.text.startswith(original_text)
        assert harness.capture.current_final.text.endswith("你在听吗？")
    finally:
        await harness.endpoint.close()
