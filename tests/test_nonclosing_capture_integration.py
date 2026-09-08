"""Nonclosing proposals through real owner, recording, journal and answer gates.

Only the provider's local synthetic transcript/understanding and existing fake
LiveKit transport are controlled. No candidate data or external calls.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from app.model_gateway.schemas import StableTranscriptPreview, StreamingSTTEvent, TranscriptSegment
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider
from app.services.interview_evidence import InterviewEvidenceChain

from test_automatic_turn_integration import (
    _automatic_session, _current, _feed, _silence_until, _assert_one_automatic_answer,
    _VOICE, _CONTINUATION, _SILENCE, _PREFIX, _SUFFIX,
)
from test_evidence_owner_recovery_integration import ORGANIZATION_ID, _wait_until


_LAST = "最后核验每段音频只保存一次。"


class _ControlledTranscript:
    def __init__(self):
        self.segments = [TranscriptSegment(text=_PREFIX, start_ms=0, end_ms=200, confidence=0.96)]
        self.revision = 1
        self.tail = False
        self.final_extra = ""
        self.action = "next"
        self.raw_streams = []
        self.finishes = 0
        self.understood = []

    @property
    def text(self):
        return "".join(segment.text for segment in self.segments)

    def append(self, text):
        start = self.segments[-1].end_ms
        self.segments.append(TranscriptSegment(text=text, start_ms=start, end_ms=start + 200, confidence=0.96))
        self.revision += 1
        self.tail = False


@asynccontextmanager
async def _controlled_session(tmp_path, monkeypatch, state):
    original_open = MockProvider.open_stream
    original_invoke = MockProvider.invoke

    async def open_stream(provider, request, context):
        raw = await original_open(provider, request, context)
        state.raw_streams.append(raw)

        async def preview():
            if raw.byte_count == 0:
                return None
            return StableTranscriptPreview(
                stream_id=raw.stream_id, revision=state.revision, text=state.text,
                language="zh-CN", confidence=0.96,
                segments=[segment.model_copy(deep=True) for segment in state.segments],
                provider=raw.provider, has_unstable_tail=state.tail,
            )

        async def finish():
            state.finishes += 1
            raw.closed = True
            raw.sequence += 1
            segments = [segment.model_copy(deep=True) for segment in state.segments]
            if state.final_extra:
                end = segments[-1].end_ms
                segments.append(TranscriptSegment(text=state.final_extra, start_ms=end, end_ms=end + 200, confidence=0.96))
            return [StreamingSTTEvent(
                stream_id=raw.stream_id, sequence=raw.sequence, type="transcript.final",
                text="".join(segment.text for segment in segments), language="zh-CN",
                confidence=0.96, segments=segments, provider=raw.provider, is_final=True,
            )]

        # Install before the real gateway/ContinuousSTT inspect capability.
        raw.preview = preview
        raw.finish = finish
        return raw

    async def invoke(provider, capability, request, context):
        response = await original_invoke(provider, capability, request, context)
        if getattr(request, "purpose", None) == "interview_turn_understanding":
            state.understood.append(request.metadata["transcript"])
            data = deepcopy(response.data)
            understanding = data.get("understanding", data)
            understanding["suggested_action"] = state.action
            return response.model_copy(update={"data": data})
        return response

    monkeypatch.setattr(MockProvider, "open_stream", open_stream)
    monkeypatch.setattr(MockProvider, "invoke", invoke)
    async with _automatic_session(tmp_path, monkeypatch, [_PREFIX], stable_previews=True) as resources:
        assert resources[3].chain.supports_stable_preview
        yield resources


def _no_domain_answer(store, runtime):
    current = _current(runtime)
    assert current["answers"] == []
    assert current["turns"][0]["utterances"] == []
    assert not [command for command in store.evidence_commands.values()
                if command["command_type"] == "evidence.seal"]


async def _settled_answer(store, runtime, ingress):
    await _silence_until(ingress, lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
    await _wait_until(lambda: all(command["status"] == "completed" for command in store.evidence_commands.values()))


def test_multiple_thinking_pauses_reuse_one_stt_and_commit_all_pcm_exactly_once(tmp_path, monkeypatch):
    async def scenario():
        state = _ControlledTranscript()
        state.action = "continue_listening"
        async with _controlled_session(tmp_path, monkeypatch, state) as (store, runtime, channel, managed):
            capture_id = managed._capture_id
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: len(state.understood) == 1)
            await _feed(managed._ingress, _SILENCE, 20)
            _no_domain_answer(store, runtime)
            assert state.finishes == 0 and len(state.raw_streams) == 1
            assert len(state.understood) == 1  # same stable prefix is not re-inferred

            state.tail = True
            await _feed(managed._ingress, _CONTINUATION, 5)
            state.append(_SUFFIX)
            await _silence_until(managed._ingress, lambda: len(state.understood) == 2)
            await _feed(managed._ingress, _SILENCE, 20)
            _no_domain_answer(store, runtime)
            assert state.finishes == 0 and len(state.raw_streams) == 1
            assert managed._capture_id == capture_id and managed.chain.is_open

            state.tail = True
            await _feed(managed._ingress, _CONTINUATION, 5)
            state.append(_LAST)
            state.action = "next"
            await _settled_answer(store, runtime, managed._ingress)
            assert state.understood == [_PREFIX, _PREFIX + _SUFFIX, _PREFIX + _SUFFIX + _LAST]
            assert state.finishes == 1 and len(state.raw_streams) == 1
            _assert_one_automatic_answer(store, runtime, state.text, voice_frames=10, continuation_frames=10)

    asyncio.run(scenario())


def test_final_tail_revision_is_reunderstood_without_reopening_an_empty_stt(tmp_path, monkeypatch):
    async def scenario():
        state = _ControlledTranscript()
        state.final_extra = _SUFFIX
        inputs = []
        original_prepare = InterviewEvidenceChain.prepare_decision

        async def inspect_prepare(chain, transcript):
            inputs.append((type(transcript).__name__, transcript.text))
            return await original_prepare(chain, transcript)

        monkeypatch.setattr(InterviewEvidenceChain, "prepare_decision", inspect_prepare)
        async with _controlled_session(tmp_path, monkeypatch, state) as (store, runtime, channel, managed):
            await _feed(managed._ingress, _VOICE, 10)
            await _settled_answer(store, runtime, managed._ingress)
            assert inputs == [("StableTranscriptPreview", _PREFIX), ("StreamingSTTEvent", _PREFIX + _SUFFIX)]
            assert state.understood == [_PREFIX, _PREFIX + _SUFFIX]
            assert state.finishes == 1 and len(state.raw_streams) == 1
            _assert_one_automatic_answer(store, runtime, _PREFIX + _SUFFIX, voice_frames=10)

    asyncio.run(scenario())


def test_late_stable_sentence_reconsiders_same_quiet_audio_revision_without_a_cut(tmp_path, monkeypatch):
    async def scenario():
        state = _ControlledTranscript()
        state.action = "continue_listening"
        async with _controlled_session(tmp_path, monkeypatch, state) as (store, runtime, channel, managed):
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: len(state.understood) == 1)
            audio_revision = managed._answer_endpoint.revision
            _no_domain_answer(store, runtime)
            assert state.finishes == 0
            # The provider reader catches up after the candidate stopped. No
            # new browser hint or above-threshold PCM may be needed to retry.
            state.append(_SUFFIX)
            state.action = "next"
            await _settled_answer(store, runtime, managed._ingress)
            assert managed._answer_endpoint.revision == audio_revision
            assert state.understood == [_PREFIX, _PREFIX + _SUFFIX]
            assert state.finishes == 1 and len(state.raw_streams) == 1
            _assert_one_automatic_answer(store, runtime, state.text, voice_frames=10)

    asyncio.run(scenario())


def test_new_voice_revokes_preview_preparation_without_cutting_or_losing_tail(tmp_path, monkeypatch):
    async def scenario():
        state = _ControlledTranscript()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        original_prepare = InterviewEvidenceChain.prepare_decision
        calls = 0

        async def held_prepare(chain, transcript):
            nonlocal calls
            decision = await original_prepare(chain, transcript)
            calls += 1
            if calls == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()  # late successful upstream return must be fenced
            return decision

        monkeypatch.setattr(InterviewEvidenceChain, "prepare_decision", held_prepare)
        async with _controlled_session(tmp_path, monkeypatch, state) as (store, runtime, channel, managed):
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, entered.is_set)
            _no_domain_answer(store, runtime)
            assert state.finishes == 0
            state.tail = True
            await _feed(managed._ingress, _CONTINUATION, 10)
            await _wait_until(cancelled.is_set)
            _no_domain_answer(store, runtime)
            assert state.finishes == 0 and len(state.raw_streams) == 1
            state.append(_SUFFIX)
            await _settled_answer(store, runtime, managed._ingress)
            assert calls == 2
            assert state.finishes == 1 and len(state.raw_streams) == 1
            _assert_one_automatic_answer(store, runtime, state.text, voice_frames=10, continuation_frames=10)

    asyncio.run(scenario())


def test_owner_epoch_change_during_preview_preparation_cannot_seal_or_submit(tmp_path, monkeypatch):
    async def scenario():
        state = _ControlledTranscript()
        entered, release = asyncio.Event(), asyncio.Event()
        original_prepare = InterviewEvidenceChain.prepare_decision

        async def held_prepare(chain, transcript):
            decision = await original_prepare(chain, transcript)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
            return decision

        monkeypatch.setattr(InterviewEvidenceChain, "prepare_decision", held_prepare)
        try:
            async with _controlled_session(tmp_path, monkeypatch, state) as (store, runtime, channel, managed):
                await _feed(managed._ingress, _VOICE, 10)
                await _silence_until(managed._ingress, entered.is_set)
                _no_domain_answer(store, runtime)
                with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
                    owners = transaction.evidence_ownerships.list()
                    assert len(owners) == 1
                    owner = owners[0]
                    owner["ownership_epoch"] += 1
                    owner["owner_instance_id"] = "synthetic-successor"
                    transaction.evidence_ownerships.update(owner, expected_version=owner["version"])
                release.set()
                await _wait_until(lambda: not managed.chain.is_open)
                _no_domain_answer(store, runtime)
                assert state.finishes == 0 and len(state.raw_streams) == 1
                assert _current(runtime)["status"] == "in_progress"
        finally:
            release.set()

    asyncio.run(scenario())
