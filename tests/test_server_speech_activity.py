import asyncio
from array import array

import pytest

from app.adapters.speech_activity import ServerSpeechActivity
from app.services.continuous_stt import ContinuousSTT
from test_answer_endpoint import _endpoint
from test_continuous_stt import _RawStream, _validated

SILENCE = bytes(640)
IMPULSE = array("h", [2000] * 160 + [-2000] * 160).tobytes()
QUIET_VOICE = array("h", [100, -100] * 160).tobytes()


class PossibleSpeech:
    def is_speech(self, frame, rate):
        return any(frame)


def test_endpoint_and_completeness_vad_use_configured_floor(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_TURN_VOICE_RMS", "0.08")
    activity = ServerSpeechActivity(vad=PossibleSpeech())
    for _ in range(20):
        assert not activity.observe(IMPULSE)
    assert not activity.since(0)
    monkeypatch.setenv("INTERVIEWER_TURN_VOICE_RMS", "nan")
    with pytest.raises(ValueError):
        ServerSpeechActivity()


def test_one_loud_impulse_cannot_revoke_confirmation_but_unsettled_onset_blocks_commit():
    endpoint, clock, _, _ = _endpoint()
    activity = ServerSpeechActivity(vad=PossibleSpeech())
    endpoint.speech_activity = activity
    endpoint.observe_audio(IMPULSE)
    assert endpoint.revision == 0
    assert activity.since(0), "Unsettled onset cannot authorize a commit yet"
    endpoint.observe_audio(SILENCE)
    assert endpoint.revision == 0 and not activity.since(0)


def test_subthreshold_activity_does_not_revoke_but_sustained_voice_and_new_words_do():
    endpoint, _, _, _ = _endpoint()
    endpoint.speech_activity = ServerSpeechActivity(vad=PossibleSpeech())
    for _ in range(300):
        endpoint.observe_audio(QUIET_VOICE)
    assert endpoint.revision == 0 and not endpoint.speech_activity.since(0)
    for _ in range(4):
        endpoint.observe_audio(IMPULSE)
    assert endpoint.revision == 0
    endpoint.observe_audio(IMPULSE)
    assert endpoint.revision == 1
    assert endpoint.speech_activity.since(0)


def test_partial_audio_frames_are_pending_until_resolved_and_dc_or_vad_faults_fail_conservatively():
    activity = ServerSpeechActivity(vad=PossibleSpeech())
    assert not activity.observe(IMPULSE[:320])
    assert activity.since(0)
    assert not activity.since(320), "An existing provider final already covers that incomplete VAD frame"
    activity.observe(IMPULSE[320:])
    assert activity.since(320), "New bytes after the covered final must remain guarded"
    activity.observe(SILENCE)
    assert not activity.since(0)
    assert activity.observe(b"\0\x20" * 320), "DC fault must not certify silence"
    class FailedVad:
        def is_speech(self, frame, rate):
            raise RuntimeError("synthetic classifier failure")
    failed = ServerSpeechActivity(vad=FailedVad())
    assert failed.observe(QUIET_VOICE) and failed.since(0)


def test_actual_webrtc_classifier_does_not_turn_a_single_impulse_into_new_speech():
    activity = ServerSpeechActivity()
    for _ in range(10):
        activity.observe(SILENCE)
    assert not activity.observe(IMPULSE)
    for _ in range(10):
        assert not activity.observe(SILENCE)
    assert not activity.since(0)


@pytest.mark.parametrize("new_text,continuous_voice", [(False, False), (True, False), (False, True)])
def test_empty_recognition_after_noise_keeps_final_but_new_voice_or_hypothesis_cannot_be_dropped(new_text, continuous_voice):
    async def scenario():
        first = _RawStream("answer", text="完整回答。没有了。")
        second = _RawStream("after_confirmation", text=None)
        async def receive(chunk):
            second.chunks.append(chunk)
            if new_text:
                return await _RawStream.send_audio(second, chunk)
            return []
        second.send_audio = receive
        async def reopen():
            return _validated(second)
        recorded = []
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append,
                                bytes_per_second=32000, speech_activity=ServerSpeechActivity(vad=PossibleSpeech()))
        await capture.send_audio(QUIET_VOICE)
        assert (await capture.snapshot()).text == "完整回答。没有了。"
        await capture.send_audio(SILENCE)
        await capture.send_audio(IMPULSE)
        for _ in range(4):
            await capture.send_audio(IMPULSE if continuous_voice else SILENCE)
        result = await capture.snapshot(resume=False)
        if new_text or continuous_voice:
            assert result is None and capture._unresolved
        else:
            assert result.text == "完整回答。没有了。"
            assert capture.commit_final().text == result.text
        assert len(recorded) == 7, "No retry may record the same accepted PCM twice"
        await capture.abort()
    asyncio.run(scenario())


def test_new_server_hypothesis_revokes_even_if_acoustic_vad_missed_it():
    endpoint, _, _, _ = _endpoint()
    endpoint.observe_transcript("原回答。")
    revision = endpoint.revision
    endpoint.observe_transcript("原回答。")
    assert endpoint.revision == revision
    endpoint.observe_transcript("原回答。还有一点。")
    assert endpoint.revision == revision + 1


def test_valid_held_final_has_room_for_bounded_semantic_inference_without_false_recovery_failure():
    async def scenario():
        first = _RawStream("answer", text="完整回答。没有了。")
        async def reopen():
            raise AssertionError("Held final should not reopen for silence")
        recorded = []
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32000)
        await capture.send_audio(QUIET_VOICE)
        await capture.snapshot(resume=False)
        await capture.send_audio(bytes(32000 * 55))
        capture.assert_can_commit()
        assert len(recorded) == 2
        with pytest.raises(Exception) as rejected:
            await capture.send_audio(bytes(32000 * 6))
        assert rejected.value.code == "provider_audio_recovery_buffer_exceeded"
        await capture.abort()
    asyncio.run(scenario())
