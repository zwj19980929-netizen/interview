"""The supplement exchange retains evidence, never a revoked submission grant."""
import asyncio
from types import SimpleNamespace

import pytest

from app.model_gateway.errors import ProviderError
from test_answer_endpoint import _final, _until
from test_spoken_supplement import setup


def exchange():
    endpoint, clock, notices, commits, spoken = setup()
    confirmation = endpoint.confirmation
    base = endpoint.capture.current_final
    confirmation._remember_boundary(base)
    confirmation._supplement_question_delivered = True
    confirmation._reply_question = "还有需要补充的吗？"
    confirmation.phase = "awaiting_reply"
    confirmation.reply_seen = True
    endpoint._proposal_revision = endpoint.revision
    return endpoint, confirmation, clock, notices, commits, spoken


def malformed():
    return ProviderError("provider_schema_invalid", "synthetic non-object", retryable=True,
        details={"json_failure_kind": "invalid_json_object", "json_decode_reason": "non_object"})


@pytest.mark.anyio
async def test_same_supplement_evidence_gets_one_targeted_retry_then_one_recovery_invitation():
    endpoint, confirmation, clock, notices, commits, spoken = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append((reply, context))
        raise malformed()

    endpoint.capture.classify_supplement_reply = classify
    final = _final(endpoint.capture.current_final.text + "合成补充控制句。")
    endpoint.capture.current_final = final
    try:
        await confirmation._reply(endpoint, final)
        assert len(calls) == 1 and not endpoint.preparation_blocked
        clock.value += 2
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2 and endpoint.preparation_blocked
        assert calls[0][1].get("correction_reason") is None
        assert calls[1][1]["correction_reason"] == "invalid_json_object"
        assert calls[0][0] == calls[1][0]
        assert endpoint._recovery_reason == "invalid_result"
        await confirmation.control_step(endpoint, quiet=10)
        assert spoken == ["reception_recovery_clarification"]
        assert "understanding_clarification" not in notices
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        assert "understanding_clarification" in notices
        assert confirmation.phase == "awaiting_reply"
        assert confirmation._supplement_question_delivered
        assert confirmation.boundary == "合成回答结束。"
        for _ in range(3):
            await confirmation._reply(endpoint, final)
            await confirmation.control_step(endpoint, quiet=20)
        assert len(calls) == 2 and len(spoken) == 1 and not commits
        assert endpoint.capture.is_open and endpoint._prepare_failures == 2
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["revision", "epoch", "final", "boundary", "capture"])
async def test_correction_never_crosses_changed_source_or_retry_epoch(change):
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        raise malformed()

    endpoint.capture.classify_supplement_reply = classify
    final = _final("合成回答结束。合成控制句。")
    try:
        await confirmation._reply(endpoint, final)
        if change == "revision":
            endpoint.speech_started(source="audio")
        elif change == "epoch":
            assert endpoint.retry_answer_processing()
        elif change == "final":
            final = final.model_copy(deep=True)
            final.provider.request_id = "revised-provider-source"
        elif change == "boundary":
            confirmation._boundary_final = confirmation._boundary_final.model_copy(deep=True)
            confirmation._boundary_final.provider.request_id = "revised-boundary-source"
        else:
            from test_spoken_supplement import Capture
            endpoint.capture = Capture()
            endpoint.capture.classify_supplement_reply = classify
        endpoint._proposal_revision = endpoint.revision
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2
        assert all(context.get("correction_reason") is None for context in calls)
        assert endpoint._prepare_failures == (1 if change == "epoch" else 2)
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [
    ProviderError("provider_schema_invalid", "syntax", retryable=True,
        details={"json_failure_kind": "invalid_json_object", "json_decode_reason": "syntax_error"}),
    ProviderError("provider_unavailable", "transport", retryable=True,
        details={"json_failure_kind": "invalid_json_object", "json_decode_reason": "non_object"}),
    ValueError("not a trusted provider failure"),
])
async def test_only_verified_non_object_provider_failure_uses_the_format_correction(problem):
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        raise problem

    endpoint.capture.classify_supplement_reply = classify
    final = _final("合成回答结束。合成控制句。")
    try:
        await confirmation._reply(endpoint, final)
        await confirmation._reply(endpoint, final)
        assert len(calls) == 2 and endpoint.preparation_blocked
        assert all(context.get("correction_reason") is None for context in calls)
        assert not commits and not confirmation.confirmed
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["complete", "zero_audio", "failed", "interrupted", "new_words"])
async def test_recovery_output_retains_delivered_supplement_and_prior_fact_without_authority(outcome):
    endpoint, confirmation, _, notices, commits, spoken = exchange()
    prior = _final("合成回答结束。没有补充。")
    confirmation._confirmed_final = prior
    confirmation._confirmation_origin = (confirmation.boundary, confirmation._boundary_final)
    confirmation._confirmation_proof = object()
    confirmation._remember_boundary(prior)
    old_state = (confirmation.boundary, confirmation._boundary_final,
                 confirmation._confirmed_final, confirmation._confirmation_origin,
                 confirmation._confirmation_proof)
    endpoint._use_preparation_budget(prior.text)
    for _ in range(2):
        endpoint._record_preparation_failure()
    original = confirmation.speak

    async def speak(kind, guard):
        checkpoint = confirmation.begin_unplayed_speech()
        await original(kind, guard)
        if outcome == "zero_audio":
            assert confirmation.restore_unplayed_speech(checkpoint)
            return False
        return outcome != "failed"

    confirmation.speak = speak
    try:
        assert await confirmation._offer_processing_clarification(endpoint)
        if confirmation.speaking:
            if outcome == "new_words":
                endpoint.speech_started(source="audio")
            await confirmation.complete_playback(endpoint,
                reason="interrupted" if outcome == "interrupted" else "agent_finished")
        assert confirmation.phase == "awaiting_reply"
        assert confirmation._supplement_question_delivered
        assert old_state == (confirmation.boundary, confirmation._boundary_final,
                             confirmation._confirmed_final, confirmation._confirmation_origin,
                             confirmation._confirmation_proof)
        assert not confirmation.confirmed and not commits
        assert ("understanding_clarification" in notices) == (outcome == "complete")
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 2
        endpoint._proposal_revision = endpoint.revision
        assert not await confirmation._offer_processing_clarification(endpoint)
        assert spoken == ["reception_recovery_clarification"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_classification_cancelled_by_audio_never_grants_finish_or_retries_internally():
    endpoint, confirmation, _, _, commits, _ = exchange()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        entered.set()
        await release.wait()
        return {"intent": "finish", "answer_effect": "unchanged", "confidence": .95}

    endpoint.capture.classify_supplement_reply = classify
    task = asyncio.create_task(confirmation._reply(endpoint, _final("合成回答结束。没有补充。")))
    try:
        await entered.wait()
        endpoint.speech_started(source="audio")
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert len(calls) == 1 and not commits and not confirmation.confirmed
        assert confirmation.phase == "awaiting_reply"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
async def test_live_endpoint_after_exhaustion_invites_once_then_routes_fresh_supplement_normally():
    endpoint, confirmation, clock, _, commits, spoken = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(reply)
        if len(calls) <= 2:
            raise malformed()
        return {"intent": "continue", "answer_effect": "updated", "confidence": .95}

    endpoint.capture.classify_supplement_reply = classify
    endpoint.capture.current_final = _final("合成回答结束。需要再说明。")
    endpoint.speech_started(source="audio")
    endpoint.start()
    try:
        clock.value += 5
        await _until(lambda: endpoint._prepare_failures == 1)
        clock.value += 2
        await _until(lambda: endpoint._prepare_failures == 2)
        clock.value += 3
        await _until(lambda: spoken == ["reception_recovery_clarification"])
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.current_final = _final("合成回答结束。需要再说明。我会补充超时处理。")
        endpoint.observe_transcript(endpoint.capture.current_final.text)
        clock.value += 5
        await _until(lambda: spoken[-1] == "continue")
        assert len(calls) == 3 and calls[-1].endswith("我会补充超时处理。")
        assert endpoint._prepare_failures == 2 and not commits and endpoint.capture.is_open
        assert confirmation._confirmation_proof is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["missing", "rejected"])
async def test_confirmation_unchanged_needs_both_issued_proof_and_successful_composition(invalid):
    endpoint, confirmation, _, _, commits, spoken = exchange()
    confirmation._prepared_answer = object()
    confirmation._prepared_answer_final = endpoint.capture.current_final
    endpoint.capture.confirm_prepared_decision = lambda *args, **kwargs: None if invalid == "rejected" else object()
    decision = {"intent": "confirmation_unchanged", "answer_effect": "unchanged", "confidence": .95}
    if invalid != "missing":
        decision["completion_proof"] = object()
    try:
        await confirmation._accept_finish(endpoint, _final("合成回答结束。无关的尾句。"),
                                           reply_text="无关的尾句。", decision=decision)
        assert not confirmation.confirmed and not commits
        assert confirmation._confirmation_proof is None
        assert spoken == ["clarify"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_new_tail_revokes_permission_and_requires_fresh_semantics_before_using_root_preparation():
    endpoint, confirmation, _, _, commits, spoken = exchange()
    base, root_prepared, proof1, proof2 = endpoint.capture.current_final, object(), object(), object()
    confirmation._prepared_answer, confirmation._prepared_answer_final = root_prepared, base
    proposals, classifications, compositions = [], [], []

    async def propose(**kwargs):
        endpoint.assert_current()
        proposals.append(kwargs)

    def compose(prepared, base_final, boundary, final, **kwargs):
        assert prepared is root_prepared and base_final is base
        compositions.append(kwargs)
        return SimpleNamespace(composed=True)

    async def classify(reply, **context):
        classifications.append((reply, context))
        return {"intent": "confirmation_unchanged", "answer_effect": "unchanged", "confidence": .95,
                "completion_proof": proof2}

    endpoint._propose = propose
    endpoint.capture.confirm_prepared_decision = compose
    endpoint.capture.classify_supplement_reply = classify
    finish = _final(base.text + "我已经说完了。太搞笑了吧。")
    try:
        await confirmation._accept_finish(endpoint, finish,
            reply_text="我已经说完了。太搞笑了吧。",
            decision={"intent": "finish", "answer_effect": "unchanged", "confidence": .95,
                      "completion_proof": proof1})
        assert confirmation.confirmed and len(proposals) == 1
        endpoint.speech_started(source="audio")
        assert not confirmation.confirmed and confirmation._confirmation_proof is proof1
        endpoint._proposal_revision = endpoint.revision
        fresh = _final(finish.text + "今天外面有点吵。")
        await confirmation._reply(endpoint, fresh)
        assert classifications[0][0] == "今天外面有点吵。"
        assert classifications[0][1]["prior_confirmation_proof"] is proof1
        assert len(compositions) == 2 and len(proposals) == 2
        assert confirmation.confirmed and confirmation._confirmation_proof is proof2
        assert not endpoint.capture.prepares and not commits and not spoken
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("invalidation", ["new_input", "controller"])
async def test_supplement_recovery_notice_rechecks_delivery_when_scheduled_callback_resumes(tmp_path, monkeypatch, invalidation):
    from app.services.evidence_coordination import EvidenceOwnershipCoordinator
    from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _PREFIX
    from test_spoken_supplement_integration import _synthetic_tts, _finish_playback

    async with _automatic_session(tmp_path, monkeypatch, [_PREFIX], spoken_confirmation=True) as (store, runtime, channel, managed):
        _synthetic_tts(runtime)
        endpoint, chain, capture_id = managed._answer_endpoint, managed.chain, managed._capture_id
        confirmation = endpoint.confirmation
        await _feed(managed._ingress, _VOICE, 3)
        endpoint._proposal_revision = endpoint.revision
        final = await endpoint.listening_snapshot()
        confirmation._remember_boundary(final)
        await confirmation._say(endpoint, "check", afterwards="awaiting_reply")
        await _finish_playback(channel, runtime, managed)
        assert confirmation.phase == "awaiting_reply" and confirmation._supplement_question_delivered
        for _ in range(2):
            endpoint._record_preparation_failure()
        await endpoint._understanding_failed(record_failure=False)
        assert await confirmation._offer_processing_clarification(endpoint)
        notify, entered, release = endpoint.notify, asyncio.Event(), asyncio.Event()

        async def delayed(reason):
            if reason == "understanding_clarification":
                entered.set()
                await release.wait()
            await notify(reason)

        endpoint.notify = delayed
        task = asyncio.create_task(_finish_playback(channel, runtime, managed))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if invalidation == "new_input":
                endpoint.speech_started(source="audio")
            else:
                grant = managed.ownership
                EvidenceOwnershipCoordinator(store, lease_seconds=30).attach_control(
                    interview_id=managed.interview_id, organization_id=managed.organization_id,
                    connection_id="successor_096", local_instance_id=grant.owner_instance_id)
            release.set()
            await task
            await _until(lambda: managed._reply_audio_handoff is None)
            assert _current(runtime)["agent_runtime"]["answer_preparation"]["status"] == "retry_required"
            assert not _current(runtime)["answers"] and not confirmation.confirmed
            assert managed.chain is chain and managed._capture_id == capture_id and chain.is_open
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_audio_cancellation_spends_started_allowance_and_cannot_launch_a_third_call():
    endpoint, confirmation, clock, _, commits, spoken = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(context)
        await asyncio.Event().wait()

    endpoint.capture.classify_supplement_reply = classify
    endpoint.capture.current_final = _final("合成回答结束。没有补充。")
    endpoint.speech_started(source="audio")
    endpoint.start()
    try:
        for index in range(2):
            clock.value += 5
            await _until(lambda: len(calls) == index + 1)
            endpoint.speech_started(source="audio")
            await _until(lambda: endpoint._inference_task is None)
        clock.value += 5
        await asyncio.sleep(.15)
        assert len(calls) == 2, "Cancelled calls already spent their shared same-evidence allowance"
        assert endpoint.preparation_blocked and not confirmation.confirmed and not commits
        await _until(lambda: spoken == ["reception_recovery_clarification"])
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        assert endpoint.retry_answer_processing()
        clock.value += 5
        await _until(lambda: len(calls) == 3)
        assert calls[-1].get("correction_reason") is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_corrected_reception_uses_the_actual_validated_prompt_version():
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls, responses = [], []

    async def classify(reply, **context):
        calls.append(context)
        if len(calls) == 1:
            raise malformed()
        return {"intent": "wait", "answer_effect": "uncertain", "confidence": .95,
                "reply_prompt_version": "supplement_reply.v10"}

    async def respond(endpoint, final, reply, decision, **kwargs):
        responses.append(kwargs)

    endpoint.capture.classify_supplement_reply = classify
    confirmation._respond_reception = respond
    final = _final("合成回答结束。稍等，我还在想。")
    try:
        await confirmation._reply(endpoint, final)
        await confirmation._reply(endpoint, final)
        assert calls[1]["correction_reason"] == "invalid_json_object"
        assert responses == [{"reply_prompt_version": "supplement_reply.v10"}]
        assert not confirmation.confirmed and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_runtime_classifies_mixed_finish_then_new_aside_with_real_source_proofs(tmp_path, monkeypatch):
    from app.services.spoken_supplement import SpokenSupplementConfirmation
    from test_answer_endpoint import _endpoint
    from test_answer_reuse_078 import _append
    from test_supplement_completion_proof_096 import prepared_harness, _wire

    harness = await prepared_harness(tmp_path, monkeypatch)
    endpoint, _, _, commits = _endpoint(capture=harness.chain)
    spoken, proposals = [], []

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        return True

    async def held_proposal(**kwargs):
        endpoint.assert_current()
        prepared, final = kwargs["prepared_decision"], kwargs["final_snapshot"]
        harness.stream.interviews.assert_prepared_streaming_decision(prepared, final, "org_default")
        proposals.append(prepared)

    endpoint.confirmation = confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint._propose = held_proposal
    confirmation._prepared_answer, confirmation._prepared_answer_final = harness.prepared, harness.final
    confirmation._remember_boundary(harness.final)
    confirmation._supplement_question_delivered = True
    confirmation.phase = "awaiting_reply"
    endpoint._proposal_revision = endpoint.revision
    first = _append(harness.final, "我已经说完了。太好笑了。")
    harness.stream.stream._final = first
    try:
        await confirmation._reply(endpoint, first)
        assert confirmation.confirmed and confirmation._confirmation_proof is not None
        first_proof = confirmation._confirmation_proof
        endpoint.speech_started(source="audio")
        assert not confirmation.confirmed and confirmation._confirmation_proof is first_proof
        endpoint._proposal_revision = endpoint.revision
        harness.small.data = _wire("confirmation_unchanged")
        second = _append(first, "窗外的鸟真有趣。")
        harness.stream.stream._stream = object()
        harness.stream.stream._final = second
        await confirmation._reply(endpoint, second)
        assert len(proposals) == 2 and confirmation.confirmed
        assert all(item.completion_receipt.source is harness.prepared for item in proposals)
        assert proposals[-1].understanding.claims == harness.prepared.understanding.claims
        assert proposals[-1].understanding.completion_basis.evidence_quotes == ["我已经说完了。"]
        assert proposals[-1].text == second.text
        assert len(harness.small.requests) == 2 and not spoken and not commits
        assert not harness.stream.interviews.get_interview("iv_binding")["answers"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["continue", "retry"])
async def test_explicit_continue_revokes_old_finish_while_processing_retry_preserves_it(action):
    endpoint, confirmation, _, _, commits, spoken = exchange()
    base, prepared = endpoint.capture.current_final, object()
    final = _final(base.text + "没有补充了。")
    reused, proof = object(), object()
    confirmation._prepared_answer, confirmation._prepared_answer_final = prepared, base
    confirmation._confirmation_origin = (base.text, base)
    confirmation._remember_boundary(final)
    confirmation._confirmed_final, confirmation._confirmed_reused_prepared = final, reused
    confirmation._confirmation_proof = proof
    confirmation.confirmed, confirmation.phase = True, "confirmed"
    endpoint._use_preparation_budget(final.text)
    endpoint._record_preparation_failure()
    proposals = []

    async def propose(**kwargs):
        endpoint.assert_current()
        proposals.append(kwargs)

    endpoint._propose = propose
    try:
        if action == "continue":
            endpoint.continue_speaking()
            assert not confirmation.confirmed
            endpoint._proposal_revision = endpoint.revision
            await confirmation._reply(endpoint, final)
            assert not proposals and not confirmation.confirmed
            assert confirmation._confirmed_final is None
            assert confirmation._confirmation_origin is None
            assert confirmation._confirmed_reused_prepared is None
            assert confirmation._confirmation_proof is None
        else:
            assert endpoint.retry_answer_processing()
            assert confirmation._confirmation_proof is proof
            await confirmation.step(endpoint, quiet=5)
            assert len(proposals) == 1 and proposals[0]["prepared_decision"] is reused
            assert confirmation.confirmed
        assert confirmation._prepared_answer is prepared and confirmation._prepared_answer_final is base
        assert confirmation._supplement_question_delivered and endpoint.capture.is_open
        assert not commits and not spoken
    finally:
        await endpoint.close()
