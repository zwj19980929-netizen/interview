"""Automatic turns through real ownership, evidence, STT, and commit boundaries.

Only the room transport, audio-end model and provider's synthetic input are
controlled. No browser finish hints, real candidate audio, or network calls.
"""

import asyncio
import io
import wave
from array import array
from contextlib import asynccontextmanager
from dataclasses import replace

from app.adapters.audio_turn_detector import AudioTurnPrediction
from app.adapters.livekit_audio_ingress import LiveKitIngressAudioFrame
from app.adapters.livekit_media import LiveKitMediaPlane
from app.adapters.private_media import read_managed_audio
from app.core.time import utc_now
from app.domain.interview_agent import ClientSignal
from app.file_storage.provider import reset_private_file_storage_for_tests
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider
from app.repositories.memory import InMemoryStore
from app.services.interview_agent import InterviewAgentRuntime
from app.services.interview_evidence import InterviewEvidenceChain
from app.services.livekit_evidence_ingress import LiveKitEvidenceIngressSupervisor
from app.services.streaming_stt import StreamingInterviewSTT

from test_evidence_owner_recovery_integration import (
    INTERVIEW_ID,
    ORGANIZATION_ID,
    TURN_ID,
    _FakeIngress,
    _configuration,
    _opened,
    _session,
    _ticket,
    _wait_until,
)


_PREFIX = "我使用持久检查点和幂等提交恢复任务。"
_SUFFIX = "另外补充，恢复前检查所有权，防止重复提交。"
_VOICE = b"\x00\x20" * 320
_CONTINUATION = b"\x00\x40" * 320
_SILENCE = bytes(640)


class _CertainAudioEndpoint:
    async def predict(self, pcm_s16le, **kwargs):
        return AudioTurnPrediction("ready", 0.95, 0)

    async def close(self):
        pass


class _SyntheticIngress(_FakeIngress):
    sequence = 0

    async def push(self, pcm):
        assert self.connected
        self.sequence += 1
        await self.on_audio_frame(LiveKitIngressAudioFrame(
            sequence=self.sequence,
            track_sid="synthetic_microphone",
            sample_rate_hz=16_000,
            channels=1,
            samples_per_channel=len(pcm) // 2,
            captured_at=utc_now(),
            pcm_s16le=pcm,
        ))

    async def drain(self):
        # push awaits the real sink, so every frame is already acknowledged.
        return self.sequence


async def _feed(ingress, pcm, frames):
    for _ in range(frames):
        await ingress.push(pcm)
        await asyncio.sleep(0.02)


async def _silence_until(ingress, predicate, *, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "automatic turn did not advance"
        await _feed(ingress, _SILENCE, 1)


@asynccontextmanager
async def _automatic_session(tmp_path, monkeypatch, transcripts, *, stable_previews=False, spoken_confirmation=False):
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path / "media"))
    monkeypatch.setenv("INTERVIEWER_TURN_MIN_SILENCE_SECONDS", "0.3")
    monkeypatch.setenv("INTERVIEWER_TURN_END_THRESHOLD", "0.6")
    monkeypatch.setenv("INTERVIEWER_TURN_VOICE_RMS", "0.006")
    reset_private_file_storage_for_tests()
    if not spoken_confirmation:
        # These tests exercise the lower-level reversible proposal seam.
        # Production always installs the spoken confirmation policy; its
        # complete workflow is covered separately through this same chain.
        monkeypatch.setattr("app.services.livekit_evidence_ingress.SpokenSupplementConfirmation", lambda **kwargs: None)

    # Preserve actual gateway/ValidatedSTTStream/MockSTTStream contracts. Each
    # new provider stream gets a server-owned synthetic final, never a browser
    # transcript. Empty subsequent streams represent silence, not an answer.
    pending_transcripts = iter(transcripts)
    original_open_stream = MockProvider.open_stream

    async def open_synthetic_stream(provider, request, context):
        request = request.model_copy(update={"metadata": {
            **request.metadata,
            "development_transcript": next(pending_transcripts, ""),
            "confidence": 0.96,
            "duration_ms": 200,
        }})
        stream = await original_open_stream(provider, request, context)
        if not stable_previews:
            # This legacy fixture assigns a different transcript at each
            # recognition opening. Keep coverage of adapters without preview;
            # new nonclosing tests drive stable sentences on one live stream.
            stream.preview = None
        return stream

    monkeypatch.setattr(MockProvider, "open_stream", open_synthetic_stream)
    store = InMemoryStore()
    _session(store)
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        session = transaction.interview_sessions.get(INTERVIEW_ID)
        session["turns"][0]["status"] = "asking"
        session["updated_at"] = utc_now()
        transaction.interview_sessions.update(session, expected_version=session["version"])
    _ticket(store, "connection_automatic")
    supervisor = LiveKitEvidenceIngressSupervisor(
        store,
        media_plane=LiveKitMediaPlane(replace(
            _configuration(), authoritative_ingress_lease_seconds=30,
        )),
        ingress_factory=_SyntheticIngress,
        turn_detector=_CertainAudioEndpoint(),
        instance_id="instance_automatic_turn",
        command_poll_seconds=0.005,
    )
    runtime = InterviewAgentRuntime(store)
    runtime.evidence_ingress = supervisor
    channel = None
    try:
        channel = await runtime.open(_opened("connection_automatic"))
        await channel.send(ClientSignal(
            type="evidence.stream.open",
            idempotency_key="automatic_open",
            turn_id=TURN_ID,
            payload={"content_type": "audio/pcm", "sample_rate_hz": 16_000,
                     "channels": 1, "language": "zh-CN"},
        ))
        managed = channel._evidence_session
        assert managed is not None and managed.evidence_open
        assert type(managed.chain) is InterviewEvidenceChain
        assert type(managed.chain._stt) is StreamingInterviewSTT
        assert isinstance(managed._ingress, _SyntheticIngress)
        yield store, runtime, channel, managed
    finally:
        if channel is not None:
            await channel.close("test_complete")
        await supervisor.shutdown()
        reset_private_file_storage_for_tests()


def _current(runtime):
    return runtime.interviews.get_interview(INTERVIEW_ID, ORGANIZATION_ID)


def _assert_one_automatic_answer(store, runtime, text, *, voice_frames, continuation_frames=0, extra_pcm=b""):
    current = _current(runtime)
    assert len(current["answers"]) == 1
    answer = current["answers"][0]
    assert answer["final_transcript"] == text
    assert answer["transcript_source"] == "server_streaming"
    assert answer["media_evidence"]["complete"] is True
    assert not [item for item in current["agent_runtime"].get("problems", [])
                if item.get("recoverable") is False]
    seals = [item for item in store.evidence_commands.values()
             if item["command_type"] == "evidence.seal"]
    assert len(seals) == 1
    assert seals[0]["payload"]["endpoint"] == "prepared_turn"
    assert seals[0]["status"] == "completed"

    # Validate actual sealed recording, not only the mock transcript. Both
    # voiced runs must survive rotation/cancellation exactly once.
    recording = read_managed_audio(persistence_for(store), ORGANIZATION_ID, answer["audio_uri"])
    with wave.open(io.BytesIO(recording), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16_000, 1, 2)
        samples = array("h", wav.readframes(wav.getnframes()))
    assert samples.count(8192) == voice_frames * 320
    assert samples.count(16384) == continuation_frames * 320
    extra = array("h", extra_pcm)
    assert set(samples) <= {0, 8192, 16384} | set(extra)
    if extra_pcm:
        assert samples.tobytes().endswith(extra_pcm)
        for value in set(extra) - {0, 8192, 16384}:
            assert samples.count(value) == extra.count(value)


def test_automatic_audio_endpoint_commits_one_durable_server_answer(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            await _feed(managed._ingress, _VOICE, 10)
            assert _current(runtime)["answers"] == []
            await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1)
            await _wait_until(lambda: all(item["status"] == "completed"
                                         for item in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _PREFIX, voice_frames=10)

    asyncio.run(scenario())


def test_new_voice_cancels_held_preparation_without_closing_capture_or_losing_tail(tmp_path, monkeypatch):
    async def scenario():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        prepared_texts = []
        original_prepare = InterviewEvidenceChain.prepare_decision

        async def held_prepare(chain, final):
            decision = await original_prepare(chain, final)
            prepared_texts.append(final.text)
            if len(prepared_texts) == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # Simulate an upstream operation returning a late result
                    # even after cancellation: revision guards must reject it.
                    cancelled.set()
            return decision

        monkeypatch.setattr(InterviewEvidenceChain, "prepare_decision", held_prepare)
        try:
            async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, _SUFFIX]) as (store, runtime, channel, managed):
                capture_id = managed._capture_id
                await _feed(managed._ingress, _VOICE, 10)
                await _silence_until(managed._ingress, entered.is_set)
                assert managed.evidence_open and managed.chain.is_open
                assert _current(runtime)["agent_runtime"]["floor"] == "candidate"
                assert _current(runtime)["answers"] == []

                await _feed(managed._ingress, _CONTINUATION, 10)
                await _wait_until(cancelled.is_set)
                assert managed._capture_id == capture_id
                assert managed.evidence_open and managed.chain.is_open
                assert _current(runtime)["agent_runtime"]["floor"] == "candidate"
                assert _current(runtime)["answers"] == []
                assert not [item for item in store.evidence_commands.values()
                            if item["command_type"] == "evidence.seal"]

                await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1)
                await _wait_until(lambda: all(item["status"] == "completed"
                                             for item in store.evidence_commands.values()))
                assert len(prepared_texts) == 2
                assert prepared_texts[0] == _PREFIX
                assert _PREFIX in prepared_texts[1] and _SUFFIX in prepared_texts[1]
                _assert_one_automatic_answer(store, runtime, prepared_texts[1],
                                             voice_frames=10, continuation_frames=10)
        finally:
            release.set()

    asyncio.run(scenario())
