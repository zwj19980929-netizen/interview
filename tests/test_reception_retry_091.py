"""A failed reception is unknown, never a consumed successful `other`."""

import asyncio

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from test_answer_endpoint import _final
from test_conversation_control_086 import setup, ask, exhaust_answer_analysis, spoken_kinds


REQUEST = "我想结束本次面试。"


def retry(endpoint):
    endpoint.cover_transcript(endpoint.capture.current_final)
    exhaust_answer_analysis(endpoint, endpoint.capture.current_final.text)
    endpoint._understanding_recovery_active = True
    endpoint._recovery_retryable = True
    assert endpoint.retry_answer_processing()
    endpoint._proposal_revision = endpoint.revision


async def timeout(_text):
    raise TimeoutError("Synthetic reception timeout")


@pytest.mark.parametrize("preview_first", [False, True])
def test_transient_reception_recovers_before_answer_analysis(preview_first):
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        calls = 0

        async def once(text):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("Synthetic reception timeout")
            return {"kind": "stop_interview", "confidence": .98,
                    "evidence_quote": text, "reply_text": ""}

        endpoint.capture.reception_impl = once
        try:
            if preview_first:
                assert not await endpoint.confirmation._receive_request(endpoint,
                    await endpoint.capture.transcript_preview(), prepare_only=True)
                assert calls == 1 and not spoken
            await ask(endpoint)
            assert calls == 2 and spoken_kinds(spoken) == ["exit_check"]
            assert not endpoint.capture.prepares and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_failed_reception_is_bounded_across_noise_and_metadata_changes():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        endpoint.capture.reception_impl = timeout
        try:
            for index in range(5):
                endpoint.speech_started(source="audio")
                endpoint._proposal_revision = endpoint.revision
                final = endpoint.capture.current_final
                endpoint.capture.current_final = final.model_copy(update={
                    "provider": final.provider.model_copy(update={"request_id": "synthetic_%s" % index})})
                assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert len(endpoint.capture.receptions) == 2
            assert endpoint.confirmation._reception_seen_text == ""
            assert endpoint.confirmation._reception_cache is None
            assert not spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_explicit_retry_recovers_failed_reception_before_reconciliation_bypass():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        endpoint.capture.reception_impl = timeout
        try:
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert len(endpoint.capture.receptions) == 2
            endpoint.confirmation._recovery_review = (endpoint.revision, endpoint.capture,
                                                      endpoint._identity(endpoint.capture.current_final))
            retry(endpoint)
            assert endpoint.retry_answer_processing(), "A duplicate control must share the pending retry"
            endpoint.capture.reception_impl = None
            await endpoint.confirmation.step(endpoint, 5)
            assert len(endpoint.capture.receptions) == 3
            assert spoken_kinds(spoken) == ["exit_check"]
            assert not endpoint.capture.prepares and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_reception_exhaustion_still_allows_normal_answer_analysis():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text="我用幂等键避免重复提交。")
        endpoint.capture.allow_answer_analysis = True
        endpoint.capture.reception_impl = timeout
        try:
            await ask(endpoint)
            assert len(endpoint.capture.receptions) == 2
            assert len(endpoint.capture.prepares) == 1
            assert spoken_kinds(spoken) == ["check"] and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_retry_does_not_restore_old_exit_request_after_a_correction():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        endpoint.capture.reception_impl = timeout
        try:
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            endpoint.capture.current_final = _final(REQUEST + "不对，我还是想继续面试，请等我想一下。")
            endpoint.speech_started(source="transcript")
            retry(endpoint)
            endpoint.capture.reception_impl = None
            endpoint.capture.kind = "wait"
            assert await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert endpoint.capture.receptions[-1][0] == endpoint.capture.current_final.text
            assert spoken_kinds(spoken) == ["reception_wait"]
            assert not endpoint.confirmation._exit_waiting and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_explicit_retry_keeps_successful_other_consumed():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text="我先说明这个方案。")
        endpoint.capture.kind = "other"
        try:
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            previous = endpoint.confirmation._reception_seen_text
            retry(endpoint)
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert endpoint.confirmation._reception_seen_text == previous
            assert len(endpoint.capture.receptions) == 1
            assert not spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_permanent_reception_error_is_not_retried_or_consumed():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)

        async def rejected(_text):
            raise ProviderError("provider_authentication_failed", "Synthetic rejection", retryable=False)

        endpoint.capture.reception_impl = rejected
        try:
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            retry(endpoint)
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert len(endpoint.capture.receptions) == 1
            assert endpoint.confirmation._reception_seen_text == ""
            assert not spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_owner_revocation_is_not_swallowed_as_a_reception_miss():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        endpoint.capture.owner_current = False
        try:
            with pytest.raises(ApiError) as exc:
                await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert exc.value.code == "EVIDENCE_OWNER_FENCED"
            assert not spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_repeated_previews_share_two_attempts_without_renewing_answer_budget():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        endpoint.capture.reception_impl = timeout
        allowance = exhaust_answer_analysis(endpoint, REQUEST)
        try:
            for _ in range(6):
                endpoint.speech_started(source="audio")
                endpoint._proposal_revision = endpoint.revision
                assert not await endpoint.confirmation._receive_request(endpoint,
                    await endpoint.capture.transcript_preview(), prepare_only=True)
            assert len(endpoint.capture.receptions) == 2 and allowance.used == 2
            assert endpoint.preparation_blocked
            assert not spoken and not commits and not endpoint.capture.prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_new_transcript_cancels_retry_without_speaking_old_exit_request():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        entered = asyncio.Event()
        calls = 0

        async def delayed_retry(_text):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("Synthetic first timeout")
            entered.set()
            await asyncio.Event().wait()

        endpoint.capture.reception_impl = delayed_retry
        task = asyncio.create_task(ask(endpoint))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            endpoint.capture.current_final = _final("不对，我继续回答这道题。")
            endpoint.speech_started(source="transcript")
            with pytest.raises(ApiError) as exc:
                await task
            assert exc.value.code == "TURN_DECISION_STALE"
            assert calls == 2 and not spoken and not commits
            assert not endpoint.confirmation._exit_waiting
        finally:
            task.cancel()
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("corrected", [False, True])
def test_explicit_retry_recovers_two_cancelled_receptions_only_for_current_speech(corrected):
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)
        entered = asyncio.Event()

        async def pending(_text):
            entered.set()
            await asyncio.Event().wait()

        endpoint.capture.reception_impl = pending
        try:
            for _ in range(2):
                entered.clear()
                endpoint._proposal_revision = endpoint.revision
                task = asyncio.create_task(endpoint.confirmation._receive_request(
                    endpoint, endpoint.capture.current_final))
                await asyncio.wait_for(entered.wait(), timeout=1)
                endpoint.speech_started(source="audio")
                with pytest.raises(ApiError) as exc:
                    await task
                assert exc.value.code == "TURN_DECISION_STALE"
            endpoint._proposal_revision = endpoint.revision
            endpoint.capture.reception_impl = None
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert len(endpoint.capture.receptions) == 2, "Cancellation must not refund automatic attempts"
            if corrected:
                endpoint.capture.current_final = _final(REQUEST + "不对，请让我继续想一下。")
                endpoint.capture.kind = "wait"
                endpoint.speech_started(source="transcript")
            retry(endpoint)
            assert await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert len(endpoint.capture.receptions) == 3
            assert spoken_kinds(spoken) == (["reception_wait"] if corrected else ["exit_check"])
            assert endpoint.capture.receptions[-1][0] == endpoint.capture.current_final.text
            assert not commits and not endpoint.capture.prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_owner_failure_cannot_be_renewed_as_an_unfinished_reception():
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=REQUEST)

        async def owner_lost(_text):
            raise ApiError("EVIDENCE_OWNER_FENCED", "Synthetic revoked owner", status_code=409)

        endpoint.capture.reception_impl = owner_lost
        try:
            with pytest.raises(ApiError):
                await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            endpoint.capture.reception_impl = None
            retry(endpoint)
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert len(endpoint.capture.receptions) == 1
            assert not spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("technical", [False, True])
def test_transient_reception_through_evidence_closes_only_after_delivered_confirmation(
        tmp_path, monkeypatch, technical):
    from app.providers.mock.provider import MockProvider
    from test_automatic_turn_integration import _automatic_session, _current
    from test_declined_answer_integration import (
        _TECHNICAL, _add_next_question, _answer_and_confirm_exit, _playable_speech,
    )
    from test_evidence_owner_recovery_integration import _wait_until
    from test_exit_evidence_086 import (
        EXIT, YES, _assert_retained_exit, _control_provider, _drain_farewell,
    )
    from test_spoken_supplement_integration import _audio_aligned_transcripts

    successful = _control_provider(monkeypatch)
    original = MockProvider.invoke
    attempts = []

    async def first_timeout(provider, capability, request, context):
        if request.metadata.get("prompt_version") == "conversation_reception.v10":
            attempts.append(request.metadata["prompt_version"])
            if len(attempts) == 1:
                raise ProviderError("provider_timeout", "Synthetic reception timeout", retryable=True)
        return await original(provider, capability, request, context)

    monkeypatch.setattr(MockProvider, "invoke", first_timeout)
    body = (_TECHNICAL if technical else "") + EXIT
    _audio_aligned_transcripts(monkeypatch, [body, YES])

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (
                store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            endpoint, _ = await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_retained_exit(store, runtime, body + YES, voice_frames=5, continuation_frames=5)
            assert _current(runtime)["candidate_input_completion_reason"] == "candidate_requested"
            assert len(attempts) == 2
            assert successful == ["conversation_reception.v10", "candidate_exit_reply.v1"]
            assert not any(item["kind"] == "answer.evaluate" for item in store.outbox_work_items.values())
            await _drain_farewell(channel, runtime)
            await _wait_until(lambda: managed._stopped)
            assert endpoint._closed
    asyncio.run(scenario())
