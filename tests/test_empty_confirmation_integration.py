"""A complete no-word suffix rejoins the same preparation through real fences."""
import asyncio

from app.core.prompt.contracts import SUPPLEMENT_SPEECH
from app.model_gateway.schemas import StreamingSTTEvent
from app.providers.mock.provider import MockSTTStream
from app.services.conversation_understanding import ConversationUnderstandingService
from test_answer_endpoint import _Clock
from test_automatic_turn_integration import (
    _automatic_session, _current, _feed, _assert_one_automatic_answer,
    _VOICE, _CONTINUATION, _PREFIX,
)
from test_evidence_owner_recovery_integration import _wait_until
from test_spoken_supplement_integration import _synthetic_tts, _speaking, _finish_playback


def test_no_word_noise_after_finish_does_not_reask_or_recompute_or_duplicate_recording(tmp_path, monkeypatch):
    original_finish = MockSTTStream.finish
    empty_completions, replies = [], []
    async def finish(stream):
        if stream.request.metadata.get("development_transcript"):
            return await original_finish(stream)
        stream.sequence += 1
        stream.closed = True
        empty_completions.append(stream.stream_id)
        return [StreamingSTTEvent(stream_id=stream.stream_id, sequence=stream.sequence,
            type="transcript.empty", is_final=True, provider=stream.provider)]
    async def classify(self, reply, organization_id):
        replies.append(reply)
        return {"intent": "finish", "confidence": .98, "evidence_quote": reply}
    monkeypatch.setattr(MockSTTStream, "finish", finish)
    monkeypatch.setattr(ConversationUnderstandingService, "classify_supplement_reply", classify)
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, "没有补充了。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            endpoint, clock = managed._answer_endpoint, _Clock()
            endpoint.clock = clock
            prepared, release = [], asyncio.Event()
            original_prepare = managed.chain.prepare_decision
            async def slow(final, **kwargs):
                prepared.append(final.text)
                await release.wait()
                return await original_prepare(final, **kwargs)
            managed.chain.prepare_decision = slow
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _speaking(endpoint, runtime)
            await _finish_playback(channel, runtime, managed)
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(prepared) == 1)
            # Acoustic input revokes completion. The actual server stream must
            # finish cleanly before the old preparation may rejoin the commit.
            await _feed(managed._ingress, _VOICE, 5)
            assert not endpoint.confirmation.confirmed and not _current(runtime)["answers"]
            clock.value += 1
            await _wait_until(lambda: endpoint.confirmation.confirmed)
            assert empty_completions and len(prepared) == 1
            release.set()
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            assert len(empty_completions) == 1 and replies == ["没有补充了。"]
            assert spoken == [SUPPLEMENT_SPEECH["check"]] and len(prepared) == 1
            _assert_one_automatic_answer(store, runtime, _PREFIX + "没有补充了。", voice_frames=10, continuation_frames=5)
    asyncio.run(scenario())
