"""Provisional subtitles revoke progress without spending a second analysis.

All speech, provider work and clocks are synthetic. The real endpoint owns
task cancellation, exact-final validation and its unchanged two-call allowance.
"""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from test_answer_endpoint import _final, _until
from test_answer_preparation_cancellation_081 import Capture, setup
from test_stable_preview_preparation import _preview


@pytest.mark.anyio
@pytest.mark.parametrize("semantic_first", [False, True])
async def test_blocked_capture_without_reception_reconciles_hypothesis_once_without_renewing_budget(semantic_first):
    from test_recovery_episode_066 import RecoveryCapture, problem
    from test_answer_endpoint import _endpoint
    from app.services.spoken_supplement import SpokenSupplementConfirmation

    capture = RecoveryCapture(retryable=False)
    endpoint, _, _, commits = _endpoint(capture=capture)
    endpoint.confirmation = SpokenSupplementConfirmation(speak=None, semantic_first=semantic_first)
    endpoint.cover_transcript(capture.current_final)
    endpoint._record_preparation_failure(problem=problem(retryable=False))
    budget_key = endpoint._preparation_budget_key
    deadline = endpoint._interaction_deadline
    try:
        for _ in range(3):
            endpoint.speech_started(source="audio")
            await endpoint.confirmation.control_step(endpoint, 10)
        assert not capture.snapshots and endpoint.preparation_blocked

        # A text notification buys one authoritative inspection, never a
        # model retry. The provider's final retracts this provisional suffix.
        endpoint.observe_transcript(capture.current_final.text + "嗯")
        assert endpoint.preparation_blocked
        await endpoint.confirmation.control_step(endpoint, 1)
        assert not capture.snapshots, "Reconciliation must respect the existing answer pause"
        await endpoint.confirmation.control_step(endpoint, 10)
        assert len(capture.snapshots) == 1 and capture.resumes == 1
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 1
        assert endpoint._preparation_budget_key == budget_key
        assert endpoint._interaction_deadline == deadline
        for _ in range(3):
            endpoint.speech_started(source="audio")
            await endpoint.confirmation.control_step(endpoint, 10)
        assert len(capture.snapshots) == 1 and not capture.prepares and not commits
    finally:
        await endpoint.close()


async def _running(endpoint, capture, clock, final, *, speculative):
    endpoint.cover_transcript(final)
    if speculative:
        clock.value += 4
        endpoint.prepare_stable_preview(_preview(final))
    waiter = asyncio.create_task(endpoint._prepare_transcript(final, semantic_first=True))
    await asyncio.wait_for(capture.started.wait(), 1)
    await _until(lambda: endpoint._inference_task is not None)
    pure = endpoint.preparation._task if speculative else endpoint._confirmed_preparation
    assert pure is not None
    return waiter, pure


def _used(endpoint, final):
    key = hashlib.sha256(final.text.strip().encode("utf-8")).hexdigest()
    return endpoint._model_budgets[key].used


@pytest.mark.anyio
@pytest.mark.parametrize("speculative", [False, True])
async def test_provisional_one_character_revokes_waiter_but_retracted_final_reuses_original_call(speculative):
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=speculative)
    deadline, revision = endpoint._interaction_deadline, endpoint.revision
    budget_key, budget_epoch = endpoint._preparation_budget_key, endpoint._preparation_budget_epoch
    try:
        endpoint.observe_transcript(final.text + "嗯")
        with pytest.raises(ApiError) as stale:
            await waiter
        assert stale.value.code == "TURN_DECISION_STALE"
        assert endpoint.revision > revision and not commits
        with pytest.raises(ApiError):
            endpoint.assert_current()
        assert not pure.cancelled() and capture.cancelled == []
        assert not pure.done(), "Pure bounded analysis must survive a provisional subtitle"
        assert endpoint._interaction_deadline == deadline and _used(endpoint, final) == 1
        assert endpoint._preparation_budget_key == budget_key
        assert endpoint._preparation_budget_epoch == budget_epoch

        # The provider retracts its one-character hypothesis. Only the full
        # authoritative snapshot below may make the retained work reusable.
        endpoint.observe_transcript(final.text)
        endpoint.cover_transcript(final)
        endpoint._proposal_revision = endpoint.revision
        capture.release.set()
        result = await endpoint._prepare_transcript(final, semantic_first=True)
        assert result.text == final.text and capture.validations
        assert len(capture.prepares) == 1 and _used(endpoint, final) == 1
        assert endpoint._interaction_deadline == deadline
        assert endpoint._prepare_failures == 0 and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
async def test_provisional_text_cannot_move_exhausted_budget_or_unblock_processing():
    endpoint, capture, _, _, commits = setup()
    final = _final()
    capture.problem = SimpleNamespace(source="provider", retryable=True, reason_code="provider_unavailable")
    capture.release.set()
    endpoint.cover_transcript(final)
    try:
        for failures in (1, 2):
            prepared = await endpoint._prepare_transcript(final, semantic_first=True)
            assert prepared.understanding.problem is capture.problem
            await _until(lambda: endpoint._prepare_failures == failures)
        assert endpoint.preparation_blocked and _used(endpoint, final) == 2
        budget_key, budget_epoch = endpoint._preparation_budget_key, endpoint._preparation_budget_epoch
        deadline, started = endpoint._interaction_deadline, endpoint._interaction_started_at
        blocked_revision = endpoint._blocked_revision

        for suffix in ("嗯", "呃", "新增但尚未确认的字"):
            endpoint.observe_transcript(final.text + suffix)
            endpoint.prepare_stable_preview(_preview(final, has_unstable_tail=True))
            assert endpoint.preparation_blocked
            assert endpoint._preparation_budget_key == budget_key
            assert endpoint._preparation_budget_epoch == budget_epoch
            assert endpoint._interaction_deadline == deadline
            assert endpoint._interaction_started_at == started
            assert endpoint._blocked_revision == blocked_revision
            assert endpoint._prepare_failures == 2 and _used(endpoint, final) == 2
        assert len(capture.prepares) == 2 and not commits

        # Stable new evidence can select its own allowance; a partial cannot.
        endpoint.cover_transcript(_final(final.text + "新增已确认的技术说明。"))
        assert endpoint._preparation_budget_key != budget_key
        assert not endpoint.preparation_blocked
        assert len(capture.prepares) == 2 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("speculative", [False, True])
@pytest.mark.parametrize("authority", ["final", "stable_preview"])
async def test_actual_changed_evidence_cancels_old_analysis_only_after_authoritative_observation(speculative, authority):
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    changed = _final(final.text + "新增持久化事务的技术说明。")
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=speculative)
    try:
        endpoint.observe_transcript(changed.text)
        with pytest.raises(ApiError):
            await waiter
        assert not pure.cancelled() and not capture.cancelled
        if authority == "final":
            endpoint.cover_transcript(changed)
        else:
            clock.value += 4
            endpoint.prepare_stable_preview(_preview(changed))
        await _until(lambda: pure.done())
        assert pure.cancelled() and capture.cancelled == [final.text]
        assert not endpoint.preparation.matching(final, include_failed=True)
        assert endpoint._confirmed_preparation is None
        assert not commits and _used(endpoint, final) == 1

        endpoint.cover_transcript(changed)
        endpoint._proposal_revision = endpoint.revision
        capture.release.set()
        result = await endpoint._prepare_transcript(changed, semantic_first=True)
        assert result.text == changed.text
        assert [item.text for item in capture.prepares] == [final.text, changed.text]
        assert _used(endpoint, changed) == 1 and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
async def test_continuing_unstable_partials_cannot_commit_a_finished_retained_analysis():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=False)
    try:
        endpoint.observe_transcript(final.text + "嗯")
        with pytest.raises(ApiError):
            await waiter
        assert not pure.cancelled() and not capture.cancelled
        capture.release.set()
        await _until(lambda: pure.done())
        capture.supports_stable_preview = True

        async def unstable_preview():
            return _preview(final, has_unstable_tail=True)

        capture.transcript_preview = unstable_preview
        for partial in (final.text + "嗯啊", final.text + "呃", final.text + "嗯"):
            endpoint.observe_transcript(partial)
            endpoint._proposal_revision = endpoint.revision
            await endpoint._propose()
        assert not commits and not capture.snapshots
        assert len(capture.prepares) == 1 and _used(endpoint, final) == 1
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("speculative", [False, True])
async def test_partial_revisions_never_extend_original_analysis_deadline(monkeypatch, speculative):
    monkeypatch.setattr("app.services.answer_endpoint._INTERACTION_PREPARATION_TIMEOUT", .05)
    endpoint, capture, clock, _, commits = setup()
    endpoint.preparation._timeout = .05
    final = _final()
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=speculative)
    try:
        endpoint.observe_transcript(final.text + "嗯")
        with pytest.raises(ApiError):
            await waiter
        assert not pure.cancelled() and not capture.cancelled
        for suffix in ("嗯啊", "呃", "嗯"):
            endpoint.observe_transcript(final.text + suffix)
        await _until(lambda: pure.done(), timeout=.5)
        assert isinstance(pure.exception(), asyncio.TimeoutError)
        assert capture.cancelled == [final.text]
        assert len(capture.prepares) == 1 and _used(endpoint, final) == 1
        assert not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["confidence", "segments", "provider", "provider_model"])
async def test_retracted_words_with_changed_final_provenance_cannot_reuse_retained_result(field):
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=False)
    try:
        endpoint.observe_transcript(final.text + "嗯")
        with pytest.raises(ApiError):
            await waiter
        assert not pure.cancelled() and not capture.cancelled
        capture.release.set()
        await _until(lambda: pure.done())
        changed = final.model_copy(deep=True)
        if field == "confidence":
            changed.confidence = .81
        elif field == "segments":
            changed.segments[0].end_ms += 1
        elif field == "provider":
            changed.provider.provider_id = "another-synthetic-provider"
        else:
            changed.provider.model = "another-synthetic-model"
        endpoint.cover_transcript(changed)
        endpoint._proposal_revision = endpoint.revision
        result = await endpoint._prepare_transcript(changed, semantic_first=True)
        assert result.final == changed and len(capture.prepares) == 2
        assert _used(endpoint, final) == 2 and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
async def test_retracted_words_do_not_hide_capture_replacement():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=False)
    try:
        endpoint.observe_transcript(final.text + "嗯")
        with pytest.raises(ApiError):
            await waiter
        assert not pure.cancelled() and not capture.cancelled
        capture.release.set()
        await _until(lambda: pure.done())
        replacement = Capture()
        replacement.release.set()
        endpoint.capture = replacement
        endpoint.cover_transcript(final)
        endpoint._proposal_revision = endpoint.revision
        result = await endpoint._prepare_transcript(final, semantic_first=True)
        assert result.binding is replacement
        assert len(capture.prepares) == len(replacement.prepares) == 1
        assert _used(endpoint, final) == 2 and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
async def test_retracted_words_do_not_hide_changed_domain_context():
    endpoint, capture, clock, _, commits = setup()
    final = _final()
    waiter, pure = await _running(endpoint, capture, clock, final, speculative=False)
    try:
        endpoint.observe_transcript(final.text + "嗯")
        with pytest.raises(ApiError):
            await waiter
        assert not pure.cancelled() and not capture.cancelled
        capture.release.set()
        await _until(lambda: pure.done())
        capture.context += 1
        endpoint.cover_transcript(final)
        endpoint._proposal_revision = endpoint.revision
        with pytest.raises(ApiError) as stale:
            await endpoint._prepare_transcript(final, semantic_first=True)
        assert stale.value.code == "TURN_DECISION_STALE"
        assert endpoint._confirmed_preparation is None
        assert len(capture.prepares) == 1 and not commits
    finally:
        await endpoint.close()
        await asyncio.gather(waiter, pure, return_exceptions=True)
