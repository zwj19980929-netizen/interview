"""Synthetic trial audio with SQLite and the real Evidence ownership boundary."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.model_gateway.gateway import ModelGateway
from app.providers.mock.provider import MockProvider
from app.repositories.sqlite import SQLiteStore
from app.services import interview_agent
from app.services.fixed_speech_preparation import start_fixed_speech_preparation, close_fixed_speech_preparation
from app.services.interview_agent import InterviewAgentRuntime
from app.services.livekit_evidence_ingress import LiveKitEvidenceIngressSupervisor
from test_approved_speech_output import _Publisher
from test_expression_stale_fence import _mutate
from test_fixed_speech_preparation_075 import SyntheticProvider, finished
from test_livekit_evidence_supervisor import _configuration, _FakeIngress, _opened, _session
from test_tts_streaming import store_and_route
from test_streaming_expression_lifecycle import _signal, _wait_until, _streaming_harness
from test_warmup_tts_resilience_074 import _make_warmup, _select_confirmation
from app.model_gateway.errors import ProviderError


@asynccontextmanager
async def harness(tmp_path, monkeypatch, *, cached=False):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    store = SQLiteStore(str(tmp_path / "synthetic.sqlite3"))
    original_connect = store._connect

    def connection():
        result = original_connect()
        # Expose nested reader/writer locks without waiting production's 5s.
        result.execute("PRAGMA busy_timeout = 30")
        return result

    monkeypatch.setattr(store, "_connect", connection)
    _session(store)
    runtime = InterviewAgentRuntime(store)
    seed, route = store_and_route()
    route["purpose"] = "interview_agent_expression"
    with runtime.persistence.transaction("org_default") as tx:
        tx.provider_connections.add(seed.provider_connections["connection_test"])
        tx.model_configurations.add(seed.model_configurations["config_test"])
        tx.model_routes.add(route)
    store.save_provider_secret("connection_test", {"api_key": "synthetic-secret"})
    provider = SyntheticProvider()
    runtime.gateway = ModelGateway(store, {"dashscope": provider}, persistence=runtime.persistence)
    scope = start_fixed_speech_preparation(runtime.gateway)
    await finished(scope)
    assert scope.cache.stats()["entries"] == 5
    if not cached:
        scope.cache.ttl_s = 0  # A genuine cache miss, followed by the live gateway seam.
    calls_before = len(provider.calls)
    supervisor = LiveKitEvidenceIngressSupervisor(
        store, media_plane=LiveKitMediaPlane(_configuration()), ingress_factory=_FakeIngress,
        command_poll_seconds=0.001,
    )
    runtime.evidence_ingress = supervisor
    channel = await runtime.open(_opened("connection_original"))
    managed = channel._evidence_session

    def trial(session):
        session.update(current_turn_id=None, turns=[], execution_schema_version=3)
        session["settings"].update(record_audio=False, record_video=False)
        session["agent_runtime"].update(calibration_status="listening", floor="candidate")

    _mutate(runtime, channel, trial)
    publishers = []

    def publisher(*args, **kwargs):
        item = _Publisher()
        item.publication = replace(item.publication, performance_id=kwargs["performance_id"],
                                   participant_identity="expression:" + kwargs["performance_id"],
                                   track_name="approved-expression:" + kwargs["performance_id"])
        publishers.append(item)
        return item

    monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", publisher)
    async def archive(**kwargs):
        return {"audio_uri": "private-file://synthetic-trial-audio"}
    monkeypatch.setattr(runtime.expression_audio, "store_pcm_async", archive)
    original_open = MockProvider.open_stream

    async def synthetic_stt(adapter, request, context):
        request = request.model_copy(update={"metadata": {**request.metadata,
            "development_transcript": "这是服务器识别的合成试音字幕。"}})
        return await original_open(adapter, request, context)

    monkeypatch.setattr(MockProvider, "open_stream", synthetic_stt)
    plans = []
    monkeypatch.setattr(channel, "_schedule_planning", lambda cause: plans.append(cause))
    value = SimpleNamespace(store=store, runtime=runtime, channel=channel, managed=managed,
                            provider=provider, scope=scope, calls_before=calls_before,
                            publishers=publishers, plans=plans)
    try:
        yield value
    finally:
        await managed.stop("synthetic_complete")
        await channel.close("synthetic_complete")
        await close_fixed_speech_preparation()


async def finish_trial(h, *, through_channel=False):
    await h.channel.send(ClientSignal(type="evidence.stream.open", idempotency_key="trial_open", payload={
        "content_type": "audio/pcm", "sample_rate_hz": 16000, "channels": 1}))
    assert h.managed._capture_id
    await h.managed._ingress.push(sequence=1, pcm_s16le=b"\x01\x00" * 320)
    signal = ClientSignal(type="evidence.finish", idempotency_key="trial_final",
        causation_id="trial_final", payload={"capture_id": h.managed._capture_id})
    if through_channel:
        return await h.channel.send(signal)
    return await h.managed.dispatch(h.channel, signal)


def current(h):
    return h.runtime.interviews.get_interview(h.channel.interview_id)


@pytest.mark.anyio
@pytest.mark.parametrize("cached", [True, False])
async def test_sqlite_final_confirmation_and_explicit_confirm_with_cache_hit_or_miss(tmp_path, monkeypatch, cached):
    async with harness(tmp_path, monkeypatch, cached=cached) as h:
        await finish_trial(h)
        session = current(h)
        assert session["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
        assert not session["agent_runtime"].get("calibration_retry_required")
        assert session["status"] == "in_progress" and not session["answers"] and not session["turns"]
        assert len(h.provider.calls) == h.calls_before + (0 if cached else 1)
        assert h.scope.cache.stats()["hit" if cached else "miss"] >= 1
        assert any(event.type == "transcript.final" and event.payload.get("ephemeral") for event in h.channel._queue._queue)
        assert session["agent_runtime"]["active_performance_id"]
        if cached:
            output, task = h.channel._speech_output, h.channel._speech_output_task
            await _signal(h, "avatar.performance.ready", output.performance_id, output.output_id)
            await _wait_until(lambda: output.producer_finished)
            assert output.total_samples > 0 and h.publishers[-1].published
            await _signal(h, "avatar.performance.stopped", output.performance_id, output.output_id, reason="drained")
            await asyncio.wait_for(task, 1)
            assert current(h)["agent_runtime"]["floor"] == "candidate"
        await h.channel.send(ClientSignal(type="warmup.confirm", idempotency_key="trial_confirm"))
        session = current(h)
        assert session["agent_runtime"]["calibration_status"] == "completed"
        assert not session["answers"] and not session["turns"] and len(h.plans) == 1


@pytest.mark.anyio
async def test_successful_server_final_is_not_reclassified_when_confirmation_selection_fails(tmp_path, monkeypatch):
    async with harness(tmp_path, monkeypatch) as h:
        async def fail_selection(**kwargs):
            raise sqlite3.OperationalError("synthetic downstream output failure")

        monkeypatch.setattr(h.channel, "_select_act", fail_selection)
        await finish_trial(h)
        session = current(h)
        assert session["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
        assert not session["agent_runtime"].get("calibration_retry_required")
        assert not session["answers"] and not session["turns"] and session["status"] == "in_progress"
        assert h.managed._active_warmup_epoch is None
        assert h.managed._failed_warmup_epoch is None
        assert len(h.provider.calls) == h.calls_before
        assert session["agent_runtime"]["problems"][-1]["code"] == "WARMUP_CONFIRMATION_AUDIO_UNAVAILABLE"
        await h.channel.send(ClientSignal(type="warmup.confirm", idempotency_key="trial_confirm"))
        assert current(h)["agent_runtime"]["calibration_status"] == "completed" and len(h.plans) == 1


@pytest.mark.anyio
async def test_unclassified_failure_after_selection_pauses_without_retrying_successful_stt(tmp_path, monkeypatch):
    async with harness(tmp_path, monkeypatch, cached=True) as h:
        select = h.channel._select_act
        async def failed_after_select(**kwargs):
            await select(**kwargs)
            raise sqlite3.OperationalError("synthetic downstream failure after selection")
        monkeypatch.setattr(h.channel, "_select_act", failed_after_select)
        await finish_trial(h, through_channel=True)
        session = current(h)
        state = session["agent_runtime"]
        assert session["status"] == "paused" and state["floor"] == "none"
        assert state["calibration_status"] == "awaiting_confirmation"
        assert not state.get("calibration_retry_required") and not state.get("active_performance_id")
        assert h.channel._speech_output is None and not h.plans
        problem = state["problems"][-1]
        assert problem["code"] == "WARMUP_CONFIRMATION_OUTPUT_FAILED" and not problem["recoverable"]
        events = list(h.channel._queue._queue)
        assert any(e.type == "problem" and e.payload["code"] == problem["code"] and not e.payload["recoverable"] for e in events)
        assert [e for e in events if e.type == "session.snapshot"][-1].payload["status"] == "paused"
        with pytest.raises(ApiError):
            await h.channel._confirm_warmup(ClientSignal(type="warmup.confirm", idempotency_key="invalid_confirm"))
        assert not current(h)["answers"] and not current(h)["turns"]


@pytest.mark.anyio
async def test_superseded_controller_cannot_restore_optional_output_or_confirm(tmp_path, monkeypatch):
    async with harness(tmp_path, monkeypatch) as h:
        async def replaced(**kwargs):
            grant = h.managed.ownership
            h.managed.supervisor.coordinator.attach_control(interview_id=h.channel.interview_id,
                organization_id=h.channel.organization_id, connection_id="replacement_control",
                local_instance_id=grant.local_instance_id)
            raise sqlite3.OperationalError("synthetic stale output failure")
        monkeypatch.setattr(h.channel, "_select_act", replaced)
        with pytest.raises(ApiError) as error:
            await finish_trial(h)
        assert error.value.code == "EVIDENCE_CONTROL_STALE"
        state = current(h)["agent_runtime"]
        assert state["calibration_status"] == "awaiting_confirmation" and not state.get("calibration_retry_required")
        assert not any(p["code"] == "WARMUP_CONFIRMATION_AUDIO_UNAVAILABLE" for p in state.get("problems", []))
        with pytest.raises(ApiError):
            await h.channel._confirm_warmup(ClientSignal(type="warmup.confirm", idempotency_key="stale_confirm"))
        assert not h.plans and not h.publishers


@pytest.mark.anyio
async def test_zero_pcm_failure_with_unconfirmed_validated_stream_cleanup_cannot_recover(monkeypatch):
    h = _streaming_harness(monkeypatch)
    _mutate(h.runtime, h.channel, _make_warmup)
    h.channel._evidence_session._capture_id = "synthetic_trial_capture"
    try:
        performance = await _select_confirmation(h)
        item = h.outputs[0]
        item.raw.chunks = []
        item.raw.error = ProviderError("provider_timeout", "Synthetic timeout.", retryable=True)
        async def failed_abort():
            raise RuntimeError("synthetic cleanup failed")
        item.raw.abort = failed_abort
        task = h.channel._speech_output_task
        await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
        await asyncio.wait_for(task, 1)
        assert not item.output.stream.cleanup_confirmed
        assert item.output.closed and item.output.cleanup_complete and item.output.total_samples == 0
        assert current(h)["status"] == "paused"
        assert current(h)["agent_runtime"]["floor"] == "none"
        assert current(h)["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
        assert not any(p["code"] == "WARMUP_CONFIRMATION_AUDIO_UNAVAILABLE" for p in current(h)["agent_runtime"]["problems"])
    finally:
        await h.channel.close("synthetic_complete")


@pytest.mark.anyio
async def test_preparation_cannot_claim_closed_when_validated_stream_abort_failed(monkeypatch):
    from test_followup_expression_failure_071 import _transport_harness, _open
    from test_tts_streaming import FixtureStream, managed
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
    raw = FixtureStream()
    async def failed_abort():
        raise RuntimeError("synthetic cleanup failed")
    raw.abort = failed_abort
    stream = managed(raw)
    async def opened(*args, **kwargs):
        return stream
    async def failed_publication():
        raise ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic failure.", status_code=503)
    monkeypatch.setattr(runtime.gateway, "open_tts_stream", opened)
    publisher.open = failed_publication
    with pytest.raises(ApiError) as error:
        await _open(runtime, channel)
    assert not stream.cleanup_confirmed
    assert not getattr(error.value, "speech_preparation_closed", False)
