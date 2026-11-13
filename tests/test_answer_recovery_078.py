import asyncio
import time
from types import SimpleNamespace

from app.services.answer_endpoint import AnswerEndpoint
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final


def test_processing_retry_preserves_confirmed_answer_and_revision():
    async def scenario():
        endpoint, _, notices, _ = _endpoint()
        async def speak(*args, **kwargs):
            raise AssertionError('No new question is needed')
        endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        confirmation = endpoint.confirmation
        final = _final('回答。没有补充。')
        confirmation.confirmed = True
        confirmation.phase = 'confirmed'
        confirmation._confirmed_final = final
        endpoint.cover_transcript(final)
        endpoint._record_preparation_failure()
        endpoint._record_preparation_failure()
        revision = endpoint.revision
        endpoint.retry_answer_processing()
        assert confirmation.confirmed and confirmation.phase == 'confirmed'
        assert confirmation._confirmed_final is final
        assert endpoint.revision == revision
        assert not endpoint.preparation_blocked
        await endpoint._publish_preparation_failure()
        assert 'understanding_retrying' in notices
        await endpoint.close()
    asyncio.run(scenario())


def test_first_deadline_failure_gets_one_bounded_recovery_window():
    endpoint, _, _, _ = _endpoint()
    endpoint.cover_transcript(_final())
    started = endpoint._interaction_started_at
    endpoint._interaction_deadline = time.monotonic() - .01
    endpoint._record_preparation_failure(interaction_expired=True)
    assert not endpoint.preparation_blocked
    assert endpoint._interaction_deadline == started + 40
    endpoint.speech_started(source='audio')
    assert endpoint._interaction_deadline == started + 40
    endpoint._record_preparation_failure(interaction_expired=True)
    assert endpoint.preparation_blocked
    assert endpoint._interaction_deadline == started + 40


def test_terminal_problem_never_opens_automatic_recovery():
    endpoint, _, _, _ = _endpoint()
    endpoint.cover_transcript(_final())
    deadline = endpoint._interaction_deadline
    endpoint._record_preparation_failure(problem=SimpleNamespace(retryable=False, code='provider_authentication'))
    assert endpoint.preparation_blocked
    assert endpoint._interaction_deadline == deadline
    assert endpoint.preparation_recovery_details['retryable'] is False


def test_confirmed_timeout_recovers_automatically_without_reasking_or_losing_confirmation(monkeypatch):
    from test_spoken_supplement import Capture
    from test_answer_endpoint import _until
    import app.services.answer_endpoint as module
    async def scenario():
        monkeypatch.setattr(module, '_INTERACTION_PREPARATION_TIMEOUT', .03)
        monkeypatch.setattr(module, '_INTERACTION_RECOVERY_TIMEOUT', .5)
        capture = Capture()
        endpoint, clock, notices, commits = _endpoint(capture=capture)
        async def speak(*args, **kwargs):
            raise AssertionError('Already confirmed answer must not ask again')
        endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint.confirmation.confirmed = True
        endpoint.confirmation.phase = 'confirmed'
        endpoint.confirmation._confirmed_final = capture.current_final
        endpoint._last_voice = clock.value - 1
        async def prepare(final):
            if len(capture.prepares) == 1:
                await asyncio.sleep(.05)
            return capture.prepared(final)
        capture.prepare_impl = prepare
        endpoint.start()
        await _until(lambda: endpoint._prepare_failures == 1)
        assert endpoint.confirmation.confirmed
        assert not endpoint.preparation_blocked
        clock.value += 1.1
        await _until(lambda: commits)
        assert len(capture.prepares) == 2 and len(commits) == 1
        assert endpoint.confirmation.confirmed
        assert 'understanding_recovered' in notices
        await endpoint.close()
    asyncio.run(scenario())


def test_preview_and_authoritative_model_work_share_same_two_call_allowance():
    from app.core.answer_processing_budget import consume_answer_model_call
    async def scenario():
        endpoint, clock, _, _ = _endpoint()
        calls = []
        async def prepare(final, **kwargs):
            calls.append(consume_answer_model_call())
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_decision = prepare
        final = _final()
        from test_stable_preview_preparation import _preview
        clock.value += 3.1
        endpoint.prepare_stable_preview(_preview(final))
        await endpoint.preparation.take(final)
        await endpoint._call_prepare_decision(final, completion_confirmed=True)
        endpoint.speech_started(source='audio')
        revised_provenance = final.model_copy(deep=True)
        revised_provenance.provider.request_id = 'reconnected-request'
        await endpoint._call_prepare_decision(revised_provenance)
        assert calls == [True, True, False]
        endpoint._understanding_recovery_active = True
        endpoint.retry_answer_processing()
        await endpoint._call_prepare_decision(final)
        assert calls[-1] is True
        await endpoint.close()
    asyncio.run(scenario())


def test_budget_exhaustion_allows_explicit_recovery_but_auth_failure_does_not():
    endpoint, _, _, _ = _endpoint()
    endpoint.cover_transcript(_final())
    endpoint._record_preparation_failure(problem=SimpleNamespace(retryable=False, reason_code='call_budget_exhausted'))
    assert endpoint.preparation_blocked
    assert endpoint.preparation_recovery_details['retryable'] is True
    endpoint.retry_answer_processing()
    assert not endpoint.preparation_blocked
    endpoint._record_preparation_failure(problem=SimpleNamespace(retryable=False, reason_code='provider_auth_failed'))
    endpoint._use_preparation_budget('new server words do not fix authentication')
    assert endpoint.preparation_blocked
    epoch = endpoint._preparation_budget_epoch
    endpoint.retry_answer_processing()
    assert endpoint._preparation_budget_epoch == epoch


def test_speculation_waits_for_longer_pause_and_is_once_per_capture():
    from test_stable_preview_preparation import _preview
    async def scenario():
        endpoint, clock, _, _ = _endpoint()
        calls = []
        async def prepare(final, **kwargs):
            from app.core.answer_processing_budget import consume_answer_model_call
            assert consume_answer_model_call()
            calls.append(final.text)
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_decision = prepare
        final = _final()
        endpoint._last_voice = clock.value
        clock.value += 2.9
        endpoint.prepare_stable_preview(_preview(final))
        await asyncio.sleep(0)
        assert not calls
        clock.value += .2
        endpoint.prepare_stable_preview(_preview(final))
        await endpoint.preparation.take(final)
        for index in range(5):
            endpoint.speech_started(source='audio')
            clock.value += 5
            endpoint.prepare_stable_preview(_preview(_final(final.text + str(index))))
            await asyncio.sleep(0)
        assert calls == [final.text]
        await endpoint._call_prepare_decision(_final(final.text + '真实新回答'))
        assert len(calls) == 2, 'Final evidence still gets its own bounded analysis'
        await endpoint.close()
    asyncio.run(scenario())


def test_all_terminal_provider_failures_block_new_words_but_schema_can_recover():
    for reason in ['provider_route_invalid', 'provider_output_truncated', 'provider_capability_missing']:
        endpoint, _, _, _ = _endpoint()
        endpoint.cover_transcript(_final())
        endpoint._record_preparation_failure(problem=SimpleNamespace(
            retryable=False, source='provider', reason_code=reason))
        endpoint._use_preparation_budget('different words do not repair a disabled provider')
        assert endpoint.preparation_blocked
        assert endpoint.retry_answer_processing() is False
    endpoint, _, _, _ = _endpoint()
    endpoint.cover_transcript(_final())
    endpoint._record_preparation_failure(problem=SimpleNamespace(
        retryable=False, source='schema_or_content', reason_code='evidence_not_verbatim'))
    endpoint._use_preparation_budget('new evidence can repair a content conflict')
    assert not endpoint.preparation_blocked


def test_stale_composed_commit_discards_reuse_but_keeps_real_confirmation():
    from app.core.errors import ApiError
    from test_spoken_supplement import Capture
    async def scenario():
        capture = Capture()
        attempts = []
        async def commit(prepared, guard):
            attempts.append(prepared)
            if len(attempts) == 1:
                raise ApiError('TURN_DECISION_STALE', 'policy changed', status_code=409)
        endpoint, _, _, commits = _endpoint(capture=capture, commit_impl=commit)
        endpoint.confirmation = SpokenSupplementConfirmation(speak=None)
        confirmation = endpoint.confirmation
        confirmation.confirmed = True
        confirmation.phase = 'confirmed'
        confirmation._confirmed_final = capture.current_final
        reused = capture.prepared(capture.current_final)
        confirmation._confirmed_reused_prepared = reused
        endpoint._proposal_revision = endpoint.revision
        await endpoint._propose(final_snapshot=capture.current_final, prepared_decision=reused)
        assert confirmation._confirmed_reused_prepared is None
        assert confirmation.confirmed and not commits
        await confirmation.step(endpoint, 1)
        assert len(capture.prepares) == 1
        assert len(commits) == 1 and attempts[1] is not reused
        await endpoint.close()
    asyncio.run(scenario())


def test_speculative_call_does_not_get_a_second_format_repair_allowance():
    from app.core.answer_processing_budget import consume_answer_model_call
    from test_stable_preview_preparation import _preview
    async def scenario():
        endpoint, clock, _, _ = _endpoint()
        charges = []
        async def prepare(final, **kwargs):
            charges.append((consume_answer_model_call(), consume_answer_model_call()))
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_decision = prepare
        clock.value += 3.1
        final = _final()
        endpoint.prepare_stable_preview(_preview(final))
        await endpoint.preparation.take(final)
        assert charges == [(True, False)]
        await endpoint._call_prepare_decision(final)
        assert charges[-1] == (True, False)
        await endpoint.close()
    asyncio.run(scenario())


def test_failed_preview_opens_recovery_before_second_full_call():
    from app.core.answer_processing_budget import consume_answer_model_call
    from app.core.interaction_budget import remaining_interaction_seconds
    from test_stable_preview_preparation import _preview
    async def scenario():
        endpoint, clock, notices, _ = _endpoint()
        release, started = asyncio.Event(), asyncio.Event()
        budgets = []
        async def prepare(final, **kwargs):
            assert consume_answer_model_call()
            budgets.append(remaining_interaction_seconds())
            if len(budgets) == 1:
                started.set()
                await release.wait()
                raise asyncio.TimeoutError()
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_decision = prepare
        final = _final()
        clock.value += 3.1
        endpoint.prepare_stable_preview(_preview(final))
        await started.wait()
        endpoint.cover_transcript(final)
        # Simulate 16s spent awaiting the preview: only 4s remain initially.
        endpoint._interaction_started_at = time.monotonic() - 16
        endpoint._interaction_deadline = endpoint._interaction_started_at + 20
        original_start = endpoint._interaction_started_at
        endpoint._proposal_revision = endpoint.revision
        release.set()
        prepared = await endpoint._prepare_transcript(final, semantic_first=True)
        assert prepared.understanding.problem is None
        assert len(budgets) == 2 and 23 < budgets[1] <= 24
        assert endpoint._interaction_deadline == original_start + 40
        assert endpoint._prepare_failures == 1
        assert 'understanding_unavailable' in notices
        await endpoint.close()
    asyncio.run(scenario())


def test_terminal_preview_failure_blocks_formal_call_for_same_or_new_words():
    from app.core.answer_processing_budget import consume_answer_model_call
    from app.core.errors import ApiError
    from test_stable_preview_preparation import _preview
    from app.model_gateway.errors import ProviderError
    import pytest
    async def scenario():
        endpoint, clock, _, _ = _endpoint()
        calls = []
        async def prepare(final, **kwargs):
            assert consume_answer_model_call()
            calls.append(final.text)
            raise ProviderError('provider_output_truncated', 'synthetic', retryable=False)
        endpoint.capture.prepare_decision = prepare
        final = _final()
        clock.value += 3.1
        endpoint.prepare_stable_preview(_preview(final))
        endpoint._proposal_revision = endpoint.revision
        with pytest.raises(ApiError):
            await endpoint._prepare_transcript(final, semantic_first=True)
        assert endpoint.preparation_blocked and len(calls) == 1
        endpoint._use_preparation_budget('new words cannot fix provider failure')
        assert endpoint.preparation_blocked
        await endpoint.close()
    asyncio.run(scenario())
