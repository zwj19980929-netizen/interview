"""Spoken confirmation through the real fenced owner, recording and journal."""

import asyncio
import base64
import hashlib
import pytest
from array import array

from app.adapters.private_media import pcm_wav_header
from app.model_gateway.schemas import TTSSynthesizeResponse, ProviderMeta

from app.core.prompt.contracts import SUPPLEMENT_SPEECH
from app.domain.interview_agent import ClientSignal
from app.services.conversation_understanding import ConversationUnderstandingService
from test_answer_endpoint import _Clock
from test_automatic_turn_integration import (
    _automatic_session, _current, _feed, _assert_one_automatic_answer,
    _VOICE, _CONTINUATION, _SILENCE, _PREFIX,
)
from test_evidence_owner_recovery_integration import TURN_ID, _wait_until


@pytest.mark.parametrize("payload", [
    {"performance_id": "p", "status": "done"},
    {"performance_id": "", "status": "playing"},
    {"performance_id": "p", "status": "playing", "text": "untrusted"},
    {"performance_id": 1, "status": "playing"},
    {"performance_id": "p"},
])
def test_playback_reports_have_a_strict_contract(payload):
    with pytest.raises(ValueError):
        ClientSignal(type="avatar.performance.playback", idempotency_key="report", payload=payload)


def _synthetic_tts(runtime):
    original = runtime.gateway.invoke
    spoken = []
    async def invoke(capability, request, **kwargs):
        if capability != "tts.synthesize":
            return await original(capability, request, **kwargs)
        spoken.append(request.text)
        pcm = bytes(3200)
        audio = pcm_wav_header(len(pcm), sample_rate_hz=16000, channels=1) + pcm
        return TTSSynthesizeResponse(
            audio_uri="data:audio/wav;base64," + base64.b64encode(audio).decode(),
            duration_ms=100, content_type="audio/wav",
            content_hash="sha256:" + hashlib.sha256(audio).hexdigest(),
            provider=ProviderMeta(provider_id="synthetic_tts", model="test", request_id="test", latency_ms=0),
        )
    runtime.gateway.invoke = invoke
    async def forbidden(*args, **kwargs):
        raise AssertionError("Supplement speech must not fetch the original question audio")
    runtime.avatar.speak = forbidden
    return spoken


async def _finish_playback(channel, runtime, managed):
    try:
        await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")), timeout=5)
    except AssertionError:
        raise AssertionError(str({"status": _current(runtime)["status"], "runtime": _current(runtime)["agent_runtime"],
                                  "events": _current(runtime)["agent_events"]})) from None
    performance_id = _current(runtime)["agent_runtime"]["active_performance_id"]
    await channel.send(ClientSignal(type="avatar.performance.stopped", turn_id=TURN_ID,
        idempotency_key="stopped_" + performance_id, payload={"performance_id": performance_id}))
    await _wait_until(lambda: not managed._answer_endpoint.confirmation.speaking)


async def _speaking(endpoint, runtime):
    try:
        await _wait_until(lambda: endpoint.confirmation.speaking)
    except AssertionError:
        raise AssertionError(str({"phase": endpoint.confirmation.phase,
            "status": _current(runtime)["status"], "problems": _current(runtime)["agent_runtime"].get("problems")})) from None


def test_real_ingress_notifies_quiet_new_words_and_ignores_duplicate_hypotheses(tmp_path, monkeypatch):
    from app.providers.mock.provider import MockSTTStream
    original_send = MockSTTStream.send_audio
    async def repeated_partial(stream, pcm):
        stream.partial_sent = False
        return await original_send(stream, pcm)
    monkeypatch.setattr(MockSTTStream, "send_audio", repeated_partial)
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, ""], spoken_confirmation=True) as (_, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            quiet = array("h", [60, -60] * 160).tobytes()
            await _feed(managed._ingress, quiet, 5)
            assert not endpoint.speech_activity.since(0), "Amplitude alone is below speech threshold"
            assert endpoint.revision > 0 and endpoint._last_server_transcript == _PREFIX[:len(_PREFIX) // 2]
            revision = endpoint.revision
            clock.value = 4
            await _feed(managed._ingress, quiet, 5)
            assert endpoint.revision == revision, "Repeated ASR text cannot keep restarting quiet time"
            clock.value = 5
            await _speaking(endpoint, runtime)
            await _wait_until(lambda: len(spoken) == 1)
            await _finish_playback(channel, runtime, managed)
            await managed._capture_recovery_notice("capture_recovered", managed._capture_id, TURN_ID, channel)
            await _wait_until(lambda: _current(runtime)["agent_runtime"]["floor_reason"] == "supplement_awaiting_reply")
            assert endpoint.confirmation.phase == "awaiting_reply"
            assert not _current(runtime)["answers"]
    asyncio.run(scenario())


def test_playback_status_is_scoped_diagnostic_not_completion(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, ""], spoken_confirmation=True) as (_, runtime, channel, managed):
            _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 5
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")))
            pid = _current(runtime)["agent_runtime"]["active_performance_id"]
            for index, (turn, performance, status) in enumerate([
                (TURN_ID, "stale", "playing"), ("other", pid, "playing"),
                (TURN_ID, pid, "blocked"), (TURN_ID, pid, "playing"),
            ]):
                await channel.send(ClientSignal(type="avatar.performance.playback", turn_id=turn,
                    idempotency_key=f"playback_{index}", payload={"performance_id": performance, "status": status}))
            session = _current(runtime)
            reports = [e for e in list(channel._queue._queue) if getattr(e, "type", None) == "avatar.performance.playback"]
            assert [e.payload["status"] for e in reports] == ["blocked", "playing"]
            assert endpoint.confirmation.speaking and not session["answers"]
            assert session["agent_runtime"]["floor"] == "agent"
            await _finish_playback(channel, runtime, managed)
    asyncio.run(scenario())


def test_five_second_check_then_spoken_no_submits_original_answer_with_no_button(tmp_path, monkeypatch):
    async def classify(self, reply, organization_id):
        assert reply == "没有补充了。"
        return {"intent": "finish", "confidence": 0.96, "evidence_quote": reply}
    monkeypatch.setattr(ConversationUnderstandingService, "classify_supplement_reply", classify)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, "没有补充了。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            tts_texts = _synthetic_tts(runtime)
            clock = _Clock()
            endpoint = managed._answer_endpoint
            endpoint.clock = clock
            capture_id = managed._capture_id
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 4.99
            await _feed(managed._ingress, _SILENCE, 8)
            assert not [e for e in _current(runtime)["agent_events"] if e["type"] == "conversation.act.selected"]
            clock.value = 5
            await _speaking(endpoint, runtime)
            await _finish_playback(channel, runtime, managed)
            acts = [e["payload"] for e in _current(runtime)["agent_events"] if e["type"] == "conversation.act.selected"]
            assert acts[-1]["act_type"] == "supplement_check" and acts[-1]["text"] == SUPPLEMENT_SPEECH["check"]
            assert tts_texts == [SUPPLEMENT_SPEECH["check"]]
            assert not _current(runtime)["answers"]
            assert managed._capture_id == capture_id
            assert _current(runtime)["agent_runtime"]["supplement_confirmation"]["status"] == "awaiting_reply"
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _PREFIX + "没有补充了。", voice_frames=5, continuation_frames=5)
            assert _current(runtime)["turns"][0]["current_understanding"]["prompt_version"] in {
                "interview_turn_understanding.v9", "interview_turn_decision.v8",
            }
    asyncio.run(scenario())


def test_repeated_no_with_continuous_pcm_and_slow_preparation_commits_once_without_reasking(tmp_path, monkeypatch):
    replies = []
    async def classify(self, reply, organization_id):
        replies.append(reply)
        return {"intent": "finish", "confidence": 0.98, "evidence_quote": reply}
    monkeypatch.setattr(ConversationUnderstandingService, "classify_supplement_reply", classify)

    async def scenario():
        parts = [_PREFIX, "我这边没有需要补充的了。", "没有的，进入下一题吧。", ""]
        async with _automatic_session(tmp_path, monkeypatch, parts, spoken_confirmation=True) as (store, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            prepares = []
            release = asyncio.Event()
            original_prepare = managed.chain.prepare_decision
            async def slow_prepare(final, **kwargs):
                prepares.append(final.text)
                await release.wait()
                return await original_prepare(final, **kwargs)
            managed.chain.prepare_decision = slow_prepare
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _speaking(endpoint, runtime)
            await _finish_playback(channel, runtime, managed)
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(prepares) == 1)
            await _feed(managed._ingress, _SILENCE, 12)
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(prepares) == 2, timeout=5)
            release.set()
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            assert replies == parts[1:3]
            assert spoken == [SUPPLEMENT_SPEECH["check"]]
            _assert_one_automatic_answer(store, runtime, "".join(parts), voice_frames=5, continuation_frames=10)
    asyncio.run(scenario())


def test_yes_then_supplement_stays_on_same_capture_until_spoken_no(tmp_path, monkeypatch):
    replies = []
    async def classify(self, reply, organization_id):
        replies.append(reply)
        return {"intent": "continue" if reply == "有补充。" else "finish", "confidence": 0.96, "evidence_quote": reply}
    monkeypatch.setattr(ConversationUnderstandingService, "classify_supplement_reply", classify)

    async def scenario():
        parts = [_PREFIX, "有补充。", "另外要核验健康状态。", "没有补充了。", ""]
        async with _automatic_session(tmp_path, monkeypatch, parts, spoken_confirmation=True) as (store, runtime, channel, managed):
            _synthetic_tts(runtime)
            clock = _Clock()
            endpoint = managed._answer_endpoint
            endpoint.clock = clock
            capture_id = managed._capture_id
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _speaking(endpoint, runtime)
            await _finish_playback(channel, runtime, managed)
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: replies == ["有补充。"] and endpoint.confirmation.speaking)
            await _finish_playback(channel, runtime, managed)
            assert not _current(runtime)["answers"] and managed._capture_id == capture_id
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _speaking(endpoint, runtime)
            await _finish_playback(channel, runtime, managed)
            assert not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, "".join(parts), voice_frames=10, continuation_frames=10)
            assert replies == ["有补充。", "没有补充了。"]
    asyncio.run(scenario())


def test_prompt_echo_is_excluded_but_scoped_barge_in_restores_onset_once(tmp_path, monkeypatch):
    replies = []
    async def classify(self, reply, organization_id):
        replies.append(reply)
        return {"intent": "finish", "confidence": 0.96, "evidence_quote": reply}
    monkeypatch.setattr(ConversationUnderstandingService, "classify_supplement_reply", classify)
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, "没有补充了。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _synthetic_tts(runtime)
            clock = _Clock()
            endpoint = managed._answer_endpoint
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")))
            revision = endpoint.revision
            await _feed(managed._ingress, _CONTINUATION, 5)
            assert endpoint.revision == revision and not replies and not _current(runtime)["answers"]
            await channel.send(ClientSignal(type="speech.started", turn_id=TURN_ID,
                idempotency_key="barge_confirmation", payload={"capture_id": managed._capture_id}))
            assert endpoint.confirmation.phase == "awaiting_reply"
            clock.value += 1
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _PREFIX + "没有补充了。", voice_frames=5, continuation_frames=5)
    asyncio.run(scenario())


def test_missing_playback_ack_cancels_only_that_output_and_waits_for_a_reply(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _synthetic_tts(runtime)
            clock = _Clock()
            endpoint = managed._answer_endpoint
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")))
            clock.value += 31
            await _wait_until(lambda: endpoint.confirmation.phase == "awaiting_reply")
            assert _current(runtime)["agent_runtime"]["active_performance_id"] is None
            assert not _current(runtime)["answers"] and managed.chain.is_open
            assert _current(runtime)["status"] == "in_progress"
    asyncio.run(scenario())


def test_nonzero_microphone_frames_and_single_impulse_during_preparation_keep_spoken_finish(tmp_path, monkeypatch):
    entered = release = None
    original = ConversationUnderstandingService.prepare_decision
    async def slow_prepare(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)
    async def classify(self, reply, organization_id):
        return {"intent": "finish", "confidence": 0.96, "evidence_quote": reply}
    monkeypatch.setattr(ConversationUnderstandingService, "prepare_decision", slow_prepare)
    monkeypatch.setattr(ConversationUnderstandingService, "classify_supplement_reply", classify)
    async def scenario():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, "没有了。"], spoken_confirmation=True) as (store, runtime, channel, managed):
            _synthetic_tts(runtime)
            clock = _Clock()
            endpoint = managed._answer_endpoint
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value += 5
            await _speaking(endpoint, runtime)
            await _finish_playback(channel, runtime, managed)
            await _feed(managed._ingress, _CONTINUATION, 5)
            await _feed(managed._ingress, _SILENCE, 8)
            clock.value += 1
            await asyncio.wait_for(entered.wait(), 3)
            assert managed.chain._stt.stream.paused
            revision = endpoint.revision
            noise = array("h", [(i % 7) - 3 for i in range(320)]).tobytes()
            impulse = array("h", [2000] * 160 + [-2000] * 160).tobytes()
            await _feed(managed._ingress, noise, 160)
            await _feed(managed._ingress, impulse, 1)
            await _feed(managed._ingress, noise, 15)
            assert endpoint.revision == revision and endpoint.confirmation.confirmed
            release.set()
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _PREFIX + "没有了。", voice_frames=5, continuation_frames=5,
                                         extra_pcm=noise * 160 + impulse + noise * 15)
            acts = [e["payload"]["act_type"] for e in _current(runtime)["agent_events"] if e["type"] == "conversation.act.selected"]
            assert acts.count("supplement_check") == 1
    asyncio.run(scenario())
