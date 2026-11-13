"""Source/proof failures after successful supplement classification stay observable."""
import json
from types import SimpleNamespace

import pytest

from app.core import model_diagnostics
from app.core.errors import ApiError
from app.services import supplement_completion_proof
from test_supplement_completion_proof_096 import prepared_harness, classified


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["supplement_source", "supplement_proof"])
@pytest.mark.parametrize("broken_sink", [False, True])
async def test_post_classification_failure_preserves_exception_without_private_details(
        tmp_path, monkeypatch, stage, broken_sink):
    harness = await prepared_harness(tmp_path, monkeypatch)
    rows = []

    def record(message):
        if broken_sink:
            raise OSError("PRIVATE_SINK")
        rows.append(json.loads(message))

    monkeypatch.setattr(model_diagnostics, "_LOG", SimpleNamespace(info=record))
    failure = ValueError("PRIVATE_PROOF_DETAILS")
    if stage == "supplement_source":
        original = harness.stream.interviews.conversation.classify_supplement_reply

        async def revoke(*args, **kwargs):
            result = await original(*args, **kwargs)
            harness.chain._input_revoked = True
            return result

        monkeypatch.setattr(harness.stream.interviews.conversation, "classify_supplement_reply", revoke)
        expected_type = ApiError
    else:
        def reject(*args, **kwargs):
            raise failure

        monkeypatch.setattr(supplement_completion_proof, "issue_completion_proof", reject)
        expected_type = ValueError
    with model_diagnostics.model_call_scope(interview_id="iv_binding", turn_id="turn_binding",
            capture_id="capture_synthetic", stage="supplement_reply") as scope:
        with pytest.raises(expected_type) as caught:
            await classified(harness, "我已经说完了。太好笑了。")
    assert len(harness.small.requests) == 1
    if stage == "supplement_proof":
        assert caught.value is failure
    else:
        assert caught.value.code == "TURN_DECISION_STALE"
    if broken_sink:
        return
    event, = [row for row in rows if row["event"] == "model.understanding_failed"]
    assert event["validation_stage"] == stage
    assert event["prompt_version"] == "supplement_reply.v9"
    assert event["diagnostic_scope_id"] == scope.scope_id
    assert event["error_type"] == expected_type.__name__
    assert event["failure_source"] == ("control" if stage == "supplement_source" else "internal")
    assert event["failure_reason"] == ("TURN_DECISION_STALE" if stage == "supplement_source" else "unknown")
    assert "schema_reason" not in event and "schema_path" not in event
    serialized = json.dumps(rows, ensure_ascii=False)
    for private in ("PRIVATE_", "我已经说完了", harness.final.text):
        assert private not in serialized


@pytest.mark.anyio
async def test_successful_proof_is_unchanged_without_failure_diagnostic(tmp_path, monkeypatch):
    harness = await prepared_harness(tmp_path, monkeypatch)
    rows = []
    monkeypatch.setattr(model_diagnostics, "_LOG", SimpleNamespace(info=lambda message: rows.append(json.loads(message))))
    _, result = await classified(harness, "我已经说完了。太好笑了。")
    assert result["completion_proof"] is not None and len(harness.small.requests) == 1
    assert not [row for row in rows if row["event"] in {"model.understanding_failed", "model.understanding_rejected"}]


@pytest.mark.anyio
async def test_real_adapter_four_calls_recover_new_reply_after_old_schema_and_local_failures(tmp_path, monkeypatch):
    from test_answer_reuse_078 import _append
    from test_semantic_tail_runtime_096 import exchange
    from test_supplement_format_correction_096 import service_with_wire_responses, valid, assert_single_wire

    harness = await prepared_harness(tmp_path, monkeypatch)
    service, calls, store = service_with_wire_responses([
        '[]', json.dumps(valid(reply_text="invalid silent-intent reply")),
        '[]', json.dumps(valid()),
    ])
    harness.stream.interviews.conversation = service
    endpoint, confirmation, clock, _, commits, spoken = exchange()
    confirmation._remember_boundary(harness.final)
    endpoint.capture.classify_supplement_reply = harness.chain.classify_supplement_reply
    accepted = []

    async def accept(current_endpoint, final, **kwargs):
        current_endpoint.assert_current()
        decision = kwargs["decision"]
        prepared = harness.chain.confirm_prepared_decision(harness.prepared, harness.final,
            harness.final, final, reply_text=kwargs["reply_text"], decision=decision,
            question_delivered=True)
        assert prepared is not None and prepared.completion_confirmed
        assert prepared.understanding.claims == harness.prepared.understanding.claims
        assert prepared.text == final.text
        accepted.append(prepared)

    confirmation._accept_finish = accept

    def activate(final):
        harness.stream.stream._final = final
        endpoint.capture.current_final = final
        endpoint.speech_started(source="audio")
        endpoint._proposal_revision = endpoint.revision

    try:
        first = _append(harness.final, "Well.")
        activate(first)
        await confirmation._reply(endpoint, first)
        await confirmation._reply(endpoint, first)
        assert endpoint.preparation_blocked and len(calls) == 2 and not accepted
        assert endpoint._recovery_reason == "invalid_result"
        await confirmation.control_step(endpoint, quiet=10)
        assert spoken == ["reception_recovery_clarification"]
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        clock.value += 10
        final = _append(first, "没有需要补充的了。")
        activate(final)
        await confirmation._reply(endpoint, final)
        assert len(calls) == 3 and not endpoint.preparation_blocked
        await confirmation._reply(endpoint, final)
        assert len(calls) == 4 and len(accepted) == 1
        assert not commits and endpoint.capture.is_open and endpoint._prepare_failures == 0
        assert [row["prompt_version"] for row in store.model_invocations] == [
            "supplement_reply.v9", "supplement_reply.v10", "supplement_reply.v9", "supplement_reply.v10"]
        assert [row["status"] for row in store.model_invocations] == ["failed", "success", "failed", "success"]
        assert all(row["attempt"] == 1 and row["fallback_index"] == 0 for row in store.model_invocations)
        for call in calls:
            assert_single_wire(call)
    finally:
        await endpoint.close()
