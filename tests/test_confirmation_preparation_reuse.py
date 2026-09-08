"""Acoustic revocation may reuse pure work, never completion authority."""

import asyncio

import pytest

from app.core.errors import ApiError
from test_answer_endpoint import _final, _until
from test_spoken_supplement import ask, setup


def test_unchanged_complete_final_reuses_work_after_sound_but_none_cannot_commit():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def prepare(final):
            try:
                await release.wait()
                return endpoint.capture.prepared(final)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        endpoint.capture.prepare_impl = prepare
        final = _final("合成回答结束。没有补充了。")
        endpoint.capture.current_final = final
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 1)
        endpoint.speech_started(source="audio")
        endpoint.capture.current_final = None
        clock.value += 1
        await _until(lambda: "transcript_unavailable" in notices)
        release.set()
        await _until(lambda: endpoint._confirmed_preparation.done())
        assert not cancelled.is_set() and not commits
        endpoint.capture.current_final = final
        clock.value += 3
        await _until(lambda: len(commits) == 1)
        assert len(endpoint.capture.prepares) == 1
        assert commits[0].text == final.text and spoken == ["check"]
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("server_notice", [True, False])
def test_new_words_discard_old_work_even_if_only_fresh_final_delivers_them(server_notice):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        original = _final("合成回答结束。没有补充了。")
        changed = _final(original.text + "等等，失败任务还需要幂等重试，现在讲完了。")
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def prepare(final):
            if final.text == original.text:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            await release.wait()
            return endpoint.capture.prepared(final)

        endpoint.capture.prepare_impl = prepare
        endpoint.capture.current_final = original
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 1)
        endpoint.capture.current_final = changed
        if server_notice:
            endpoint.observe_transcript(changed.text)
        else:
            endpoint.speech_started(source="audio")
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 2)
        assert cancelled.is_set() and not commits
        release.set()
        await _until(lambda: len(commits) == 1)
        assert commits[0].text == changed.text
        await endpoint.close()

    asyncio.run(scenario())


def test_acoustic_retry_keeps_original_preparation_deadline(monkeypatch):
    monkeypatch.setattr("app.services.answer_endpoint._CONFIRMED_PREPARATION_TIMEOUT", .18)

    async def scenario():
        endpoint, _, _, _, _ = setup()
        endpoint.confirmation.confirmed = True
        cancelled = asyncio.Event()

        async def prepare(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        endpoint.capture.prepare_impl = prepare
        final = _final("合成回答结束。没有了。")
        first = asyncio.create_task(endpoint._prepare_transcript(final))
        await _until(lambda: len(endpoint.capture.prepares) == 1)
        deadline = endpoint._confirmed_preparation_deadline
        await asyncio.sleep(.10)
        endpoint.speech_started(source="audio")
        with pytest.raises(ApiError, match="New input"):
            await first
        endpoint.confirmation.confirmed = True
        second = asyncio.create_task(endpoint._prepare_transcript(final))
        await asyncio.sleep(0)
        assert endpoint._confirmed_preparation_deadline == deadline
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second, .14)
        await _until(cancelled.is_set)
        assert len(endpoint.capture.prepares) == 1
        assert endpoint._confirmed_preparation is None
        await endpoint.close()

    asyncio.run(scenario())


def test_close_cancels_retained_work_after_its_waiter_was_revoked():
    async def scenario():
        endpoint, _, _, _, _ = setup()
        endpoint.confirmation.confirmed = True
        cancelled = asyncio.Event()

        async def prepare(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        endpoint.capture.prepare_impl = prepare
        first = asyncio.create_task(endpoint._prepare_transcript(_final()))
        await _until(lambda: endpoint.capture.prepares)
        endpoint.speech_started(source="client")
        with pytest.raises(ApiError):
            await first
        assert not cancelled.is_set()
        await endpoint.close()
        assert cancelled.is_set()
        assert not endpoint._preparation_tasks and endpoint._confirmed_preparation is None

    asyncio.run(scenario())


def test_uncertain_results_are_not_reused():
    async def scenario():
        endpoint, _, _, _, _ = setup()
        endpoint.confirmation.confirmed = True
        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        final = _final()
        await endpoint._prepare_transcript(final)
        assert endpoint._confirmed_preparation is None
        endpoint.capture.problem = None
        await endpoint._prepare_transcript(final)
        assert len(endpoint.capture.prepares) == 2
        await endpoint.close()

    asyncio.run(scenario())


def test_semantic_continue_cancels_preparation_and_keeps_listening():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        cancelled = asyncio.Event()

        async def prepare(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        endpoint.capture.prepare_impl = prepare
        final = _final("合成回答结束。没有补充了。")
        endpoint.capture.current_final = final
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: endpoint.capture.prepares)
        endpoint.speech_started(source="audio")
        endpoint.capture.current_final = _final(final.text + "等一下，我还想解释超时后的重试。")
        endpoint.capture.intent = "supplement"
        clock.value += 1
        await _until(lambda: spoken == ["check", "continue"])
        await _until(cancelled.is_set)
        endpoint.confirmation.floor_returned(endpoint)
        assert endpoint.confirmation.phase == "listening"
        assert not commits and endpoint._confirmed_preparation is None
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("changed_field", ["confidence", "segments", "provider"])
def test_identical_words_with_revised_provenance_cannot_reuse_work(changed_field):
    async def scenario():
        endpoint, _, _, _, _ = setup()
        endpoint.confirmation.confirmed = True
        original = _final()
        await endpoint._prepare_transcript(original)
        changed = original.model_copy(deep=True)
        if changed_field == "confidence":
            changed.confidence = .8
        elif changed_field == "segments":
            changed.segments[0].end_ms = 1001
        else:
            changed.provider.request_id = "different-provider-request"
        await endpoint._prepare_transcript(changed)
        assert len(endpoint.capture.prepares) == 2
        await endpoint.close()

    asyncio.run(scenario())


def test_cached_success_still_requires_frozen_context_at_commit():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        endpoint.confirmation.confirmed = True
        endpoint.confirmation.phase = "confirmed"
        final = _final()
        await endpoint._prepare_transcript(final)
        attempts = []

        async def reject_context(prepared, guard):
            guard()
            attempts.append(prepared)
            raise ApiError("TURN_DECISION_STALE", "Frozen question changed.", status_code=409)

        endpoint.commit = reject_context
        endpoint._proposal_revision = endpoint.revision
        await endpoint._propose(final_snapshot=final)
        assert len(attempts) == 1 and len(endpoint.capture.prepares) == 1
        assert not commits and endpoint._confirmed_preparation is None
        await endpoint.close()

    asyncio.run(scenario())
