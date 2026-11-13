"""Auxiliary expression faults never become answer/evidence failures."""
import asyncio
import json
import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import AgentEvent
from app.model_gateway.dialogue import OptionalSpeechDialogueInput
from app.model_gateway.errors import ProviderError
from app.repositories.memory import InMemoryStore
from app.services.streaming_stt import StreamingInterviewSTT
from app.services.interview_agent import _AGENT_CHANNEL_HUB
from test_audio_isolation_067 import Dialogue, until
from test_expression_stale_fence import _harness, _mutate


def stream_with_audio(optional):
    recorded, recognized = [], []
    async def send(chunk):
        recognized.append(chunk)
        return []
    stt = StreamingInterviewSTT(InMemoryStore(), "iv_synthetic")
    stt.turn_id = "turn_1"
    stt.stream = SimpleNamespace(send_audio=send)
    stt.recording = SimpleNamespace(append=recorded.append)
    stt.dialogue = optional
    return stt, recorded, recognized


def diagnostic_records(caplog):
    result = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except (ValueError, TypeError):
            continue
        if data.get("event") == "dialogue.input_degraded":
            result.append(data)
    return result


@pytest.fixture
def speech_log_capture(caplog, monkeypatch):
    # Production installs a non-propagating handler. Restore that setting after
    # each test while allowing pytest's root capture to observe these records.
    monkeypatch.setattr(logging.getLogger("interviewer.speech_evidence"), "propagate", True)
    caplog.set_level(logging.INFO, logger="interviewer.speech_evidence")
    return caplog


@pytest.mark.anyio
async def test_burst_overflow_is_observable_once_and_authoritative_audio_continues(speech_log_capture):
    caplog = speech_log_capture
    underlying = Dialogue()
    optional = OptionalSpeechDialogueInput(underlying, max_pending_bytes=8)
    stt, recorded, recognized = stream_with_audio(optional)
    events = []
    for chunk in (b"12345678", b"90", b"ab"):
        events.extend(await stt.send_audio(chunk))
    await asyncio.gather(*stt._dialogue_cleanup_tasks)
    assert recorded == recognized == [b"12345678", b"90", b"ab"]
    assert stt.dialogue is None and underlying.aborted == 1
    failures = [event for event in events if event["type"] == "dialogue.error"]
    assert len(failures) == 1
    records = diagnostic_records(caplog)
    assert len(records) == 1
    assert records[0]["reason_code"] == "provider_dialogue_input_overflow"
    assert records[0]["pending_bytes"] == records[0]["limit_bytes"] == 8
    assert records[0]["chunk_bytes"] == 2 and records[0]["sent_bytes"] == 0
    assert "12345678" not in caplog.text


@pytest.mark.anyio
async def test_slow_consumer_has_bounded_timeout_and_send_timing(speech_log_capture):
    caplog = speech_log_capture
    underlying = Dialogue(block=True)
    optional = OptionalSpeechDialogueInput(underlying, max_pending_bytes=4096, send_timeout=.02)
    stt, recorded, recognized = stream_with_audio(optional)
    await asyncio.wait_for(stt.send_audio(b"12"), .01)
    await until(lambda: underlying.aborted == 1)
    events = await stt.send_audio(b"34")
    await asyncio.gather(*stt._dialogue_cleanup_tasks)
    assert recorded == recognized == [b"12", b"34"]
    assert any(event["error_code"] == "provider_dialogue_input_timeout" for event in events)
    records = diagnostic_records(caplog)
    assert len(records) == 1 and records[0]["last_send_ms"] >= 10
    assert records[0]["max_send_ms"] >= records[0]["last_send_ms"]
    assert records[0]["sent_bytes"] == 0


@pytest.mark.anyio
async def test_diagnostic_failure_cannot_change_audio_or_fallback(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic logging failure")
    monkeypatch.setattr(logging.getLogger("interviewer.speech_evidence"), "info", broken)
    optional = OptionalSpeechDialogueInput(Dialogue(), max_pending_bytes=2)
    stt, recorded, recognized = stream_with_audio(optional)
    await stt.send_audio(b"12")
    events = await stt.send_audio(b"34")
    await asyncio.gather(*stt._dialogue_cleanup_tasks)
    assert events[0]["error_code"] == "provider_dialogue_input_overflow"
    assert recorded == recognized == [b"12", b"34"]


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["record", "recognize", "optional"])
async def test_owner_and_authoritative_failures_never_become_auxiliary_degradation(stage):
    denied = ApiError("EVIDENCE_OWNER_FENCED", "synthetic owner loss", status_code=409)
    async def fail(*args):
        raise denied
    class Guarded(Dialogue):
        async def send_audio(self, chunk):
            raise denied
    optional = OptionalSpeechDialogueInput(Guarded() if stage == "optional" else Dialogue(), max_pending_bytes=64)
    stt, _, _ = stream_with_audio(optional)
    if stage == "record":
        stt.recording.append = lambda _: (_ for _ in ()).throw(denied)
    elif stage == "recognize":
        stt.stream.send_audio = fail
    try:
        if stage == "optional":
            await stt.send_audio(b"12")
            await until(lambda: optional._failure is not None)
        with pytest.raises(ApiError) as caught:
            await stt.send_audio(b"34")
        assert caught.value is denied
    finally:
        await optional.abort()


@pytest.fixture
async def projection():
    runtime, channel, coordinator, grant = _harness()
    stt, _, _ = stream_with_audio(None)
    stt.interview_id = channel.interview_id
    channel._stt = stt
    # This fixture uses the direct STT path, without native room ownership.
    channel._evidence_session = None
    _AGENT_CHANNEL_HUB.register(channel)
    yield runtime, channel, stt
    _AGENT_CHANNEL_HUB.unregister(channel)


@pytest.mark.anyio
async def test_auxiliary_degradation_deduplicates_and_preserves_independent_answer_failure(projection):
    runtime, channel, stt = projection
    _mutate(runtime, channel, lambda session: session["agent_runtime"].update(
        answer_preparation={"status": "retry_required", "turn_id": "turn_1", "capture_id": "capture_1"},
        problems=[{"code": "UNDERSTANDING_RESULT_REJECTED"}], active_performance_id="performance_1"))
    before = deepcopy(runtime.interviews.get_interview(channel.interview_id))
    raw = stt._dialogue_error("provider_dialogue_input_overflow", "not a public message")
    await channel._project_evidence_event(raw, None)
    await channel._project_evidence_event(dict(raw), None)
    events = list(channel._queue._queue)
    assert [event.type for event in events] == ["capability.degraded"]
    assert events[0].payload == {"capability": "speech.dialogue_realtime", "fallback": "cascade",
                                "reason_code": "provider_dialogue_input_overflow"}
    assert events[0].replayability.value == "transient"
    after = runtime.interviews.get_interview(channel.interview_id)
    for key in ["answers", "turns", "status", "current_turn_id", "agent_events"]:
        assert after[key] == before[key]
    for key in ["answer_preparation", "problems", "active_performance_id", "floor"]:
        assert after["agent_runtime"][key] == before["agent_runtime"][key]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source", "generation", "turn", "closed", "unbound", "paused"])
async def test_stale_or_unbound_auxiliary_notice_does_not_reach_current_connection(projection, change):
    runtime, channel, stt = projection
    raw = stt._dialogue_error("provider_dialogue_input_overflow", "synthetic")
    if change == "source":
        channel._stt = stream_with_audio(None)[0]
    elif change == "generation":
        stt._capture_generation += 1
    elif change == "turn":
        _mutate(runtime, channel, lambda session: session.update(current_turn_id="turn_2"))
    elif change == "closed":
        channel._closed = True
    elif change == "paused":
        _mutate(runtime, channel, lambda session: session.update(status="paused"))
    else:
        raw.pop("_dialogue_degradation_id", None)
    await channel._project_evidence_event(raw, None)
    assert channel._queue.empty()


@pytest.mark.anyio
async def test_authoritative_stream_error_still_projects_problem(projection):
    _, channel, _ = projection
    await channel._project_evidence_event({"type": "stream.error", "error_code": "STT_STREAM_FAILED",
                                           "message": "synthetic authoritative failure"}, None)
    event = channel._queue.get_nowait()
    assert event.type == "problem" and event.payload["code"] == "STT_STREAM_FAILED"


@pytest.mark.anyio
async def test_degradation_preserves_real_continuous_final_and_both_recorders():
    from test_capture_reopen_fence import _harness as capture_harness, _PAYLOAD, _INTERVIEW
    from test_continuous_stt import _RawStream, _validated

    stt, persistence, _, _, recordings, writers = capture_harness()
    with persistence.transaction("org_default") as transaction:
        session = transaction.interview_sessions.get(_INTERVIEW)
        session["settings"]["speech_dialogue_mode"] = "s2s"
        transaction.interview_sessions.update(session, expected_version=session["version"])
    raw, optional = _RawStream("synthetic_final", text="合成完整发言。"), Dialogue(block=True)
    async def open_stt(request):
        return _validated(raw)
    async def open_dialogue(request):
        return optional
    stt.gateway.open_stream = open_stt
    stt.gateway.open_speech_dialogue = open_dialogue
    events = []
    try:
        await stt.open(_PAYLOAD)
        assert stt.dialogue._limit == 16000, "The existing half-second PCM limit is unchanged"
        frames = [b"\1\0" * 320 for _ in range(26)]
        for frame in frames:
            events.extend(await stt.send_audio(frame))
        final = await stt.transcript_snapshot(resume=False)
        assert final.is_final and final.text == raw.text
        assert recordings[0].chunks == writers[0].chunks == raw.chunks == frames
        assert sum(item["type"] == "dialogue.error" for item in events) == 1
        assert stt.dialogue is None and not stt.recovery_required
    finally:
        await stt.close()
        await asyncio.gather(*stt._dialogue_cleanup_tasks)


@pytest.mark.anyio
@pytest.mark.parametrize("guard", ["owner", "controller"])
async def test_current_auxiliary_receipt_cannot_swallow_ownership_errors(projection, guard):
    _, channel, stt = projection
    denied = ApiError("EVIDENCE_OWNER_FENCED", "synthetic owner loss", status_code=409)
    def fail(*args):
        raise denied
    if guard == "owner":
        stt.commit_guard = fail
    else:
        channel._evidence_session = SimpleNamespace(_stopped=False, assert_controller=fail,
                                                    chain=SimpleNamespace(_stt=stt), _capture_id="capture_1")
    with pytest.raises(ApiError) as caught:
        await channel._project_evidence_event(stt._dialogue_error("provider_timeout", "synthetic"), None)
    assert caught.value is denied and channel._queue.empty()
    assert not stt._dialogue_degradation_projected


@pytest.mark.anyio
async def test_diagnostics_are_numeric_bounded_and_never_copy_provider_details(speech_log_capture):
    caplog = speech_log_capture
    optional = OptionalSpeechDialogueInput(Dialogue(), max_pending_bytes=16,
        diagnostic_context={"interview_id": "iv_synthetic", "turn_id": "secret\nbody", "response": "secret-body"})
    optional._pending = 10 ** 400
    optional._enqueued_bytes = -10
    optional._sent_bytes = True
    optional._last_send_ms = float("nan")
    optional._max_send_ms = float("inf")
    optional._diagnose_failure("secret-provider-code", chunk_bytes=10 ** 400)
    optional._diagnose_failure("provider_timeout", chunk_bytes=1)
    records = diagnostic_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["pending_bytes"] == record["chunk_bytes"] == 1_073_741_824
    assert record["sent_bytes"] == record["last_send_ms"] == record["max_send_ms"] == record["enqueued_bytes"] == 0
    assert record["reason_code"] == "provider_unavailable"
    assert "secret" not in caplog.text and "turn_id" not in record
    for key, value in record.items():
        if key not in {"event", "at", "interview_id", "reason_code"}:
            assert type(value) is int and 0 <= value <= 1_073_741_824


@pytest.mark.parametrize("mutation", [
    {"replayability": "replayable"},
    {"payload": {"capability": "stt.streaming", "fallback": "cascade", "reason_code": "provider_timeout"}},
    {"payload": {"capability": "speech.dialogue_realtime", "fallback": "cascade", "reason_code": "secret-provider-code"}},
    {"payload": {"capability": "speech.dialogue_realtime", "fallback": "cascade", "reason_code": "provider_timeout", "pcm": "secret-body"}},
])
def test_server_degradation_contract_rejects_non_auxiliary_or_unbounded_payload(mutation):
    envelope = {"event_id": "event_1", "session_sequence": 1, "type": "capability.degraded",
                "turn_id": "turn_1", "occurred_at": "2026-11-13T00:00:00Z", "replayability": "transient",
                "payload": {"capability": "speech.dialogue_realtime", "fallback": "cascade", "reason_code": "provider_timeout"}}
    with pytest.raises(ValueError):
        AgentEvent.model_validate({**envelope, **mutation})

