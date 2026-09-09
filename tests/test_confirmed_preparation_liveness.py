"""Ready preparation is local work; fresh evidence still owns every commit."""

import asyncio

import pytest

from app.core.errors import ApiError
from test_answer_endpoint import _final
from test_spoken_supplement import setup


async def _prime(endpoint, final):
    endpoint.confirmation.confirmed = True
    endpoint.confirmation.phase = "confirmed"
    endpoint.cover_transcript(final)
    return await endpoint._prepare_transcript(final)


def test_completed_same_final_survives_elapsed_inference_deadline_without_repeating_model():
    async def scenario():
        endpoint, _, _, _, _ = setup()
        final = _final("合成技术回答。已经回答完毕。")
        prepared = await _prime(endpoint, final)
        # The 45-second inference budget has elapsed while callers reacquired
        # fresh complete snapshots. The computation itself already succeeded.
        endpoint._confirmed_preparation_deadline = asyncio.get_running_loop().time() - 1
        for _ in range(5):
            assert await endpoint._prepare_transcript(final.model_copy(deep=True)) is prepared
        assert len(endpoint.capture.prepares) == 1
        await endpoint.close()

    asyncio.run(scenario())


def test_ready_cache_does_not_create_a_cancellable_waiter_but_voice_still_revokes_authority():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        final = _final("合成技术回答。没有补充了。")
        prepared = await _prime(endpoint, final)
        for source in ("client", "audio", "client"):
            endpoint.confirmation.confirmed = True
            endpoint._proposal_revision = endpoint.revision
            asyncio.get_running_loop().call_soon(lambda source=source: endpoint.speech_started(source=source))
            assert await endpoint._prepare_transcript(final) is prepared
            assert endpoint._inference_task is None
            await asyncio.sleep(0)
            with pytest.raises(ApiError) as exc:
                endpoint.assert_current()
            assert exc.value.code == "TURN_DECISION_STALE"
            assert not endpoint.confirmation.confirmed
        assert len(endpoint.capture.prepares) == 1 and not commits
        await endpoint.close()

    asyncio.run(scenario())


def test_same_complete_final_can_commit_without_repeated_preparing_notification_window():
    async def scenario():
        endpoint, _, notices, commits, _ = setup()
        final = _final("合成技术回答。请进入下一题。")
        await _prime(endpoint, final)
        endpoint._confirmed_preparation_deadline = asyncio.get_running_loop().time() - 1

        async def notify(reason):
            notices.append(reason)
            if reason == "answer_preparing":
                endpoint.speech_started(source="client")

        endpoint.notify = notify
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._accept_finish(endpoint, final)
        assert len(commits) == 1 and commits[0].text == final.text
        assert len(endpoint.capture.prepares) == 1
        assert "answer_preparing" not in notices
        await endpoint.close()

    asyncio.run(scenario())


def test_new_preparation_still_projects_preparing_once():
    async def scenario():
        endpoint, _, notices, commits, _ = setup()
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._accept_finish(endpoint, _final("合成技术回答。没有了。"))
        assert notices.count("answer_preparing") == 1
        assert len(commits) == len(endpoint.capture.prepares) == 1
        await endpoint.close()

    asyncio.run(scenario())


def test_ready_cache_without_new_complete_snapshot_cannot_restore_completion():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        final = _final("合成技术回答。已经讲完了。")
        prepared = await _prime(endpoint, final)
        endpoint.confirmation._confirmed_final = final.model_copy(deep=True)
        endpoint.confirmation.boundary = final.text
        endpoint.speech_started(source="audio")
        endpoint.capture.current_final = None
        await endpoint.confirmation.step(endpoint, quiet=1)
        assert not commits and not endpoint.confirmation.confirmed
        assert endpoint._confirmed_preparation.result() is prepared
        assert len(endpoint.capture.prepares) == 1
        await endpoint.close()

    asyncio.run(scenario())


def test_new_server_words_replace_ready_cache_before_commit():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        original = _final("合成技术回答。没有了。")
        await _prime(endpoint, original)
        changed = _final(original.text + "再补充一点，失败任务需要幂等重试。现在说完了。")
        endpoint.observe_transcript(changed.text)
        assert endpoint._confirmed_preparation is None
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._accept_finish(endpoint, changed)
        assert len(endpoint.capture.prepares) == 2
        assert len(commits) == 1 and commits[0].text == changed.text
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["frozen_context", "voice_during_commit"])
def test_ready_preparation_still_requires_current_transaction_fence(revocation):
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        final = _final("合成技术回答。没有了。")
        await _prime(endpoint, final)
        attempts = []

        async def fenced_commit(prepared, guard):
            guard()
            attempts.append(prepared)
            if revocation == "voice_during_commit":
                await asyncio.sleep(0)
                endpoint.speech_started(source="audio")
                guard()
            raise ApiError("TURN_DECISION_STALE", "Frozen question or owner changed.", status_code=409)

        endpoint.commit = fenced_commit
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._accept_finish(endpoint, final)
        assert len(attempts) == 1 and not commits
        assert endpoint._confirmed_preparation is None
        assert len(endpoint.capture.prepares) == 1
        await endpoint.close()

    asyncio.run(scenario())
