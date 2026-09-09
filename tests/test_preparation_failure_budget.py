"""Failed understanding retries belong to server words, not VAD revisions."""

import asyncio

import pytest

from test_answer_endpoint import _final, _until
from test_spoken_supplement import ask, setup


async def _attempt(endpoint, final):
    endpoint._proposal_revision = endpoint.revision
    await endpoint.confirmation._accept_finish(endpoint, final.model_copy(deep=True))


def test_noise_and_provider_metadata_changes_cannot_restart_exhausted_word_budget():
    async def scenario():
        endpoint, clock, notices, commits, _ = setup()
        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        final = _final("合成回答。没有需要补充了。")
        for index in range(43):
            if index:
                endpoint.speech_started(source="audio" if index % 2 else "client")
            clock.value += 10
            revised = final.model_copy(deep=True)
            revised.provider.request_id = "synthetic-noise-%d" % index
            revised.provider.latency_ms = index
            await _attempt(endpoint, revised)
        assert len(endpoint.capture.prepares) == 3
        assert endpoint._prepare_failures == 3
        assert "understanding_retry_exhausted" in notices
        assert endpoint.capture.is_open and not endpoint._closed and not commits
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["problem", "exception"])
def test_cancelled_waiters_do_not_hide_or_double_count_background_failures(failure):
    async def scenario():
        endpoint, clock, notices, commits, _ = setup()
        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        final = _final("合成回答。已经讲完了。")
        for attempt in range(3):
            release = asyncio.Event()

            async def prepare(transcript):
                await release.wait()
                if failure == "exception":
                    raise RuntimeError("synthetic preparation failure")
                return endpoint.capture.prepared(transcript)

            endpoint.capture.prepare_impl = prepare
            task = asyncio.create_task(_attempt(endpoint, final))
            await _until(lambda: len(endpoint.capture.prepares) == attempt + 1)
            for index in range(20):
                endpoint.speech_started(source="client" if index % 2 else "audio")
            await task
            release.set()
            await _until(lambda: not endpoint._preparation_tasks)
            assert endpoint._prepare_failures == attempt + 1
            clock.value += 10
        endpoint.capture.prepare_impl = None
        for _ in range(10):
            endpoint.speech_started(source="client")
            await _attempt(endpoint, final)
        assert len(endpoint.capture.prepares) == endpoint._prepare_failures == 3
        assert "understanding_retry_exhausted" in notices
        assert endpoint.capture.is_open and not commits
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("reset", ["server_words", "final_only_words", "explicit_retry"])
def test_real_new_words_or_explicit_retry_restore_exhausted_preparation(reset):
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        final = _final("合成回答。没有了。")
        for _ in range(3):
            await _attempt(endpoint, final)
        assert endpoint._prepare_failures == 3
        endpoint.capture.problem = None
        if reset == "explicit_retry":
            endpoint.request_finish()
        else:
            final = _final(final.text + "补充说明，失败任务还需要幂等重试。现在讲完了。")
            if reset == "server_words":
                endpoint.observe_transcript(final.text)
        await _attempt(endpoint, final)
        assert len(endpoint.capture.prepares) == 4
        assert len(commits) == 1 and commits[0].text == final.text
        assert endpoint._prepare_failures == 0
        await endpoint.close()

    asyncio.run(scenario())


def test_background_failure_from_superseded_words_cannot_charge_new_evidence():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        old = _final("合成回答。没有了。")
        release = asyncio.Event()

        async def prepare(transcript):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return endpoint.capture.prepared(transcript)

        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        endpoint.capture.prepare_impl = prepare
        task = asyncio.create_task(_attempt(endpoint, old))
        await _until(lambda: endpoint.capture.prepares)
        endpoint.observe_transcript(old.text + "新的真实补充。")
        await task
        release.set()
        await _until(lambda: not endpoint._preparation_tasks)
        assert endpoint._prepare_failures == 0 and not commits
        await endpoint.close()

    asyncio.run(scenario())


def test_switching_back_to_previously_failed_server_words_keeps_their_budget():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        final = _final("合成回答。没有了。")
        for _ in range(3):
            await _attempt(endpoint, final)
        for _ in range(10):
            endpoint.observe_transcript(final.text + "新的未决假设。")
            await _attempt(endpoint, final)
        assert len(endpoint.capture.prepares) == endpoint._prepare_failures == 3
        assert not commits and endpoint.capture.is_open
        await endpoint.close()

    asyncio.run(scenario())


def test_running_endpoint_publishes_orphan_failure_exhaustion_and_keeps_recording():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.problem = {"code": "UNDERSTANDING_RESULT_REJECTED"}
        releases = [asyncio.Event() for _ in range(3)]

        async def prepare(transcript):
            await releases[len(endpoint.capture.prepares) - 1].wait()
            return endpoint.capture.prepared(transcript)

        endpoint.capture.prepare_impl = prepare
        endpoint.capture.current_final = _final("合成回答结束。没有需要补充了。")
        endpoint.speech_started()
        clock.value += 1
        for attempt in range(3):
            await _until(lambda: len(endpoint.capture.prepares) == attempt + 1)
            for _ in range(20):
                endpoint.speech_started(source="audio")
            await _until(lambda: endpoint._inference_task is None)
            releases[attempt].set()
            await _until(lambda: endpoint._prepare_failures == attempt + 1)
            clock.value += 10
        await _until(lambda: "understanding_retry_exhausted" in notices)
        for _ in range(40):
            endpoint.speech_started(source="client")
        clock.value += 10
        await asyncio.sleep(.2)
        assert len(endpoint.capture.prepares) == endpoint._prepare_failures == 3
        assert not commits and endpoint.capture.is_open and not endpoint._closed
        await endpoint.close()

    asyncio.run(scenario())
