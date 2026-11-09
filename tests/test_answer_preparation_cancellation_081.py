"""Audio revokes authority while exact-evidence model work remains bounded."""
import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest

from app.core.answer_processing_budget import consume_answer_model_call
from app.core.errors import ApiError
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_stable_preview_preparation import _preview


class Capture(_Capture):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = []
        self.flags = []
        self.context = 1
        self.validations = []

    async def prepare_decision(self, final, **kwargs):
        assert consume_answer_model_call(), "No more than two calls for unchanged words"
        self.prepares.append(final.model_copy(deep=True))
        self.flags.append(kwargs)
        context = self.context
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(final.text)
            raise
        result = self.prepared(final)
        result.context = context
        result.flags = kwargs
        result.binding = self
        return result

    def assert_preparation_current(self, prepared, final, **kwargs):
        self.validations.append(kwargs)
        if prepared.context != self.context or prepared.flags != kwargs or prepared.binding is not self:
            raise ApiError("TURN_DECISION_STALE", "Synthetic context changed.", status_code=409)


def setup():
    capture = Capture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    endpoint._proposal_revision = endpoint.revision
    return endpoint, capture, clock, notices, commits


@pytest.mark.anyio
@pytest.mark.parametrize("semantic_first", [False, True])
async def test_audio_revokes_waiter_but_retains_one_call_and_ready_result(semantic_first):
    endpoint, capture, _, _, commits = setup()
    final = _final()
    task = asyncio.create_task(endpoint._prepare_transcript(final, semantic_first=semantic_first))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        original_deadline = endpoint._interaction_deadline
        for _ in range(10):
            endpoint.speech_started(source="audio")
        with pytest.raises(ApiError, match="cancelled"):
            await task
        assert capture.cancelled == []
        assert endpoint._interaction_deadline == original_deadline
        assert endpoint._prepare_failures == 0 and not commits
        capture.release.set()
        await _until(lambda: endpoint._confirmed_preparation.done())
        endpoint._interaction_deadline = time.monotonic() - 1
        endpoint._proposal_revision = endpoint.revision
        prepared = await endpoint._prepare_transcript(final, semantic_first=semantic_first)
        assert prepared.text == final.text and len(capture.prepares) == 1
        assert capture.validations and not commits
        key = hashlib.sha256(final.text.strip().encode()).hexdigest()
        assert endpoint._model_budgets[key].used == 1
    finally:
        await endpoint.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_audio_keeps_speculative_result_and_completed_read_ignores_wait_deadline():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    endpoint.speech_started(source="audio")
    clock.value += 4
    endpoint.prepare_stable_preview(_preview(final))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        endpoint.speech_started(source="audio")
        await asyncio.sleep(0)
        assert capture.cancelled == []
        capture.release.set()
        await _until(lambda: endpoint.preparation.matching(final) and endpoint.preparation._task.done())
        endpoint.cover_transcript(final)
        endpoint._interaction_deadline = time.monotonic() - 1
        endpoint._proposal_revision = endpoint.revision
        assert (await endpoint._prepare_transcript(final, semantic_first=True)).text == final.text
        assert len(capture.prepares) == 1 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_recovery_start_runs_only_preparation_and_deduplicates_waiter():
    endpoint, capture, _, _, commits = setup()
    final = _final()
    endpoint.cover_transcript(final)
    endpoint._record_preparation_failure()
    deadline = endpoint._interaction_deadline
    try:
        assert endpoint.start_recovery_preparation(final)
        assert endpoint.start_recovery_preparation(final)
        await asyncio.wait_for(capture.started.wait(), 1)
        capture.release.set()
        prepared = await endpoint._prepare_transcript(final, semantic_first=True)
        assert prepared.text == final.text and len(capture.prepares) == 1
        assert endpoint._interaction_deadline == deadline
        assert endpoint._prepare_failures == 1 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_changed_context_clears_successful_cache_without_refunding_budget():
    endpoint, capture, _, _, _ = setup()
    final = _final()
    capture.release.set()
    try:
        await endpoint._prepare_transcript(final, semantic_first=True)
        capture.context += 1
        with pytest.raises(ApiError) as exc:
            await endpoint._prepare_transcript(final, semantic_first=True)
        assert exc.value.code == "TURN_DECISION_STALE"
        assert endpoint._confirmed_preparation is None
        assert len(capture.prepares) == 1
        await endpoint._prepare_transcript(final, semantic_first=True)
        assert len(capture.prepares) == 2
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_true_new_words_cancel_retained_model_and_close_drains_it():
    endpoint, capture, _, _, commits = setup()
    task = asyncio.create_task(endpoint._prepare_transcript(_final(), semantic_first=True))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        changed = _final("合成回答新增技术内容。")
        endpoint.observe_transcript(changed.text)
        with pytest.raises(ApiError):
            await task
        assert capture.cancelled == []
        endpoint.cover_transcript(changed)
        await _until(lambda: capture.cancelled)
        assert not commits and endpoint._confirmed_preparation is None
    finally:
        await endpoint.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_retained_formal_work_prevents_a_later_parallel_speculative_call():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    waiter = asyncio.create_task(endpoint._prepare_transcript(final, semantic_first=True))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        endpoint.speech_started(source="audio")
        with pytest.raises(ApiError):
            await waiter
        clock.value += 4
        endpoint.prepare_stable_preview(_preview(final))
        await asyncio.sleep(.01)
        assert len(capture.prepares) == 1
        assert not endpoint.preparation.has_preparation and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, return_exceptions=True)
    assert capture.cancelled == [final.text]


@pytest.mark.anyio
@pytest.mark.parametrize("speculative", [False, True])
async def test_orphan_failure_counts_once_and_recovery_uses_remaining_call(speculative):
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    endpoint.cover_transcript(final)
    capture.problem = SimpleNamespace(source="provider", retryable=True, reason_code="provider_unavailable")
    if speculative:
        clock.value += 4
        endpoint.prepare_stable_preview(_preview(final))
    waiter = asyncio.create_task(endpoint._prepare_transcript(final, semantic_first=True))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        await _until(lambda: endpoint._inference_task is not None)
        endpoint.speech_started(source="audio")
        with pytest.raises(ApiError):
            await waiter
        capture.release.set()
        await _until(lambda: endpoint._prepare_failures == 1)
        await asyncio.sleep(0)
        endpoint._proposal_revision = endpoint.revision
        capture.problem = None
        result = await endpoint._prepare_transcript(final, semantic_first=True)
        assert result.understanding.problem is None
        assert len(capture.prepares) == 2 and endpoint._prepare_failures == 1
        assert not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_speculative_timeout_is_accounted_after_audio_revoked_its_waiter():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    endpoint.cover_transcript(final)
    endpoint.preparation._timeout = .025
    clock.value += 4
    endpoint.prepare_stable_preview(_preview(final))
    waiter = asyncio.create_task(endpoint._prepare_transcript(final, semantic_first=True))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        endpoint.speech_started(source="audio")
        with pytest.raises(ApiError):
            await waiter
        await _until(lambda: endpoint._prepare_failures == 1)
        assert capture.cancelled == [final.text]
        assert not endpoint.preparation_blocked and not commits
        assert endpoint._interaction_deadline == endpoint._interaction_started_at + 40
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_retained_work_keeps_original_timeout_after_audio(monkeypatch):
    monkeypatch.setattr("app.services.answer_endpoint._INTERACTION_PREPARATION_TIMEOUT", .025)
    endpoint, capture, _, _, commits = setup()
    waiter = asyncio.create_task(endpoint._prepare_transcript(_final(), semantic_first=True))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        started = endpoint._interaction_started_at
        endpoint.speech_started(source="audio")
        with pytest.raises(ApiError):
            await waiter
        await _until(lambda: endpoint._prepare_failures == 1)
        assert capture.cancelled and endpoint._confirmed_preparation is None
        assert endpoint._interaction_deadline == started + 40 and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_first_success_also_rechecks_context_changed_during_model_call():
    endpoint, capture, _, _, commits = setup()
    waiter = asyncio.create_task(endpoint._prepare_transcript(_final(), semantic_first=True))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        capture.context += 1
        capture.release.set()
        with pytest.raises(ApiError) as exc:
            await waiter
        assert exc.value.code == "TURN_DECISION_STALE"
        assert endpoint._confirmed_preparation is None and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_changed_preparation_mode_never_reuses_same_words_result():
    endpoint, capture, _, _, commits = setup()
    capture.release.set()
    try:
        await endpoint._prepare_transcript(_final(), semantic_first=True)
        endpoint.confirmation = SimpleNamespace(confirmed=True, semantic_first=True)
        await endpoint._prepare_transcript(_final())
        assert capture.flags == [{"semantic_first": True}, {"completion_confirmed": True, "semantic_first": True}]
        assert len(capture.prepares) == 2 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["confidence", "segments", "provider"])
async def test_revised_final_provenance_invalidates_retained_success(field):
    endpoint, capture, _, _, commits = setup()
    capture.release.set()
    final = _final()
    try:
        await endpoint._prepare_transcript(final, semantic_first=True)
        changed = final.model_copy(deep=True)
        if field == "confidence":
            changed.confidence = .7
        elif field == "segments":
            changed.segments[0].end_ms += 1
        else:
            changed.provider.request_id = "different-request"
        await endpoint._prepare_transcript(changed, semantic_first=True)
        assert len(capture.prepares) == 2 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_background_recovery_does_not_start_after_expiration_or_close():
    endpoint, capture, _, _, commits = setup()
    final = _final()
    endpoint.cover_transcript(final)
    endpoint._interaction_deadline = time.monotonic() - 1
    assert not endpoint.start_recovery_preparation(final)
    await endpoint.close()
    assert not endpoint.start_recovery_preparation(final)
    assert not capture.prepares and not commits


@pytest.mark.anyio
async def test_waiter_timeout_starts_recovery_before_old_cancellation_has_unwound(monkeypatch):
    monkeypatch.setattr("app.services.answer_endpoint._CONFIRMED_PREPARATION_TIMEOUT", .025)
    endpoint, capture, _, _, commits = setup()
    final = _final()
    cancelling, drained = asyncio.Event(), asyncio.Event()
    calls = []

    async def prepare(transcript, **kwargs):
        assert consume_answer_model_call()
        calls.append(True)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await drained.wait()
                raise
        return capture.prepared(transcript)

    capture.prepare_decision = prepare
    capture.assert_preparation_current = lambda *args, **kwargs: None
    try:
        with pytest.raises(asyncio.TimeoutError):
            await endpoint._prepare_transcript(final, semantic_first=True)
        assert endpoint._prepare_failures == 1
        assert endpoint.start_recovery_preparation(final)
        await asyncio.wait_for(cancelling.wait(), 1)
        await asyncio.sleep(.005)
        assert len(calls) == 1, "Recovery waits for the cancelled provider to release its task"
        drained.set()
        await _until(lambda: len(calls) == 2)
        result = await endpoint._prepare_transcript(final, semantic_first=True)
        assert result.text == final.text and endpoint._prepare_failures == 1
        assert not commits
    finally:
        drained.set()
        await endpoint.close()


@pytest.mark.anyio
async def test_recovery_adopts_matching_speculation_without_cancelling_or_second_call():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    endpoint.cover_transcript(final)
    clock.value += 4
    endpoint.prepare_stable_preview(_preview(final))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        endpoint._record_preparation_failure()
        assert endpoint.start_recovery_preparation(final)
        await asyncio.sleep(0)
        assert not capture.cancelled and len(capture.prepares) == 1
        capture.release.set()
        assert (await endpoint._prepare_transcript(final, semantic_first=True)).text == final.text
        assert len(capture.prepares) == 1 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_superseded_speculative_callback_cannot_charge_replacement_with_same_identity():
    endpoint, capture, clock, _, _ = setup()
    final = _final()
    endpoint.cover_transcript(final)
    clock.value += 4
    release_old = asyncio.Event()
    cancelled_old = asyncio.Event()

    async def resistant(transcript, **kwargs):
        capture.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_old.set()
            await release_old.wait()
        return SimpleNamespace(understanding=SimpleNamespace(problem=SimpleNamespace(
            source="provider", retryable=True, reason_code="provider_unavailable")))

    capture.prepare_decision = resistant
    endpoint.prepare_stable_preview(_preview(final))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        old_task = endpoint.preparation._task
        old_attempt = endpoint._speculative_attempt
        endpoint.preparation.cancel()
        await asyncio.wait_for(cancelled_old.wait(), 1)
        # Same fingerprint is deliberately replaced to exercise the callback
        # ownership fence, independently of once-per-capture cost policy.
        endpoint.preparation.start(_preview(final))
        release_old.set()
        await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), 1)
        await asyncio.sleep(0)
        assert not old_attempt.failure_recorded and endpoint._prepare_failures == 0
    finally:
        release_old.set()
        await endpoint.close()


@pytest.mark.anyio
async def test_formal_call_waits_for_previously_cancelled_speculative_cleanup():
    endpoint, capture, clock, _, commits = setup()
    original, changed = _final(), _final("新增真实技术内容。")
    endpoint.cover_transcript(original)
    clock.value += 4
    cancelling, release_old = asyncio.Event(), asyncio.Event()
    calls = []

    async def prepare(transcript, **kwargs):
        calls.append(transcript.text)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release_old.wait()
                raise
        return capture.prepared(transcript)

    capture.prepare_decision = prepare
    capture.assert_preparation_current = lambda *args, **kwargs: None
    endpoint.prepare_stable_preview(_preview(original))
    waiter = None
    try:
        await _until(lambda: calls)
        endpoint.observe_transcript(changed.text)
        endpoint.cover_transcript(changed)
        await asyncio.wait_for(cancelling.wait(), 1)
        endpoint._proposal_revision = endpoint.revision
        waiter = asyncio.create_task(endpoint._prepare_transcript(changed, semantic_first=True))
        await asyncio.sleep(.01)
        assert calls == [original.text] and not commits
        release_old.set()
        assert (await waiter).text == changed.text
        assert calls == [original.text, changed.text]
        assert endpoint._prepare_failures == 0
    finally:
        release_old.set()
        await endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_unowned_done_callback_does_not_use_current_matching_attempt():
    endpoint, capture, clock, _, _ = setup()
    final = _final()
    endpoint.cover_transcript(final)
    clock.value += 4
    endpoint.prepare_stable_preview(_preview(final))
    try:
        await asyncio.wait_for(capture.started.wait(), 1)
        current = endpoint._speculative_attempt

        async def old_result():
            return SimpleNamespace(understanding=SimpleNamespace(problem=SimpleNamespace(
                source="provider", retryable=True, reason_code="provider_unavailable")))

        old = asyncio.create_task(old_result())
        await old
        endpoint._speculative_finished(old, _preview(final))
        assert not current.failure_recorded and endpoint._prepare_failures == 0
    finally:
        await endpoint.close()
