"""Synthetic storage, provider and media faults; no external service or real PCM."""
import asyncio
import threading
from time import monotonic
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.interaction_budget import interaction_deadline
from app.model_gateway.dialogue import OptionalSpeechDialogueInput
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.model_gateway.tts_streaming import ValidatedTTSStream
from app.persistence.provider import persistence_for
from app.providers.dashscope.provider import DashScopeProvider
from app.repositories.memory import InMemoryStore
from app.services.agent_expression_audio import AgentExpressionAudioService
from app.services.streaming_stt import StreamingInterviewSTT
from app.services.answer_endpoint import AnswerEndpoint
from app.services import interview_agent
from test_agent_expression_audio import _Storage
from test_approved_speech_output import _RawTTS, _Publisher, _harness as output_harness
from test_streaming_expression_lifecycle import _harness as runtime_harness, _mutate
from test_streaming_expression_lifecycle import _streaming_harness, _select, _signal, _session
from test_tts_streaming import store_and_route
from test_reception_cursor_062 import setup as reception_endpoint
from test_answer_endpoint import _final


def audio():
    return dict(organization_id="org_default", interview_id="iv_synthetic", turn_id="turn_synthetic",
                pcm_s16le=b"\1\0" * 48, sample_rate_hz=24000, channels=1)


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(wait(), 2)


class SlowStorage(_Storage):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def store(self, **kwargs):
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError("synthetic storage was not released")
        return super().store(**kwargs)


@pytest.mark.anyio
async def test_archive_does_not_block_loop_or_publish_before_persistence():
    storage = SlowStorage()
    service = AgentExpressionAudioService(persistence_for(InMemoryStore()), storage=storage)
    h = output_harness()
    h.output._archive = lambda **pcm: service.store_pcm_async(**{**audio(), **pcm})
    await h.output.open()
    h.output.acknowledge_ready(h.output.performance_id, h.output.output_id)
    task = asyncio.create_task(h.output.run(h.on_finished))
    await until(storage.entered.is_set)
    assert not task.done() and not h.notifications and h.publisher.finish_calls == 0
    # This coroutine continues while the blocking storage thread is held.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.002)
        ticks += 1
    assert ticks == 5
    storage.release.set()
    await h.producer_notified.wait()
    assert storage.values
    h.output.acknowledge_drained(h.output.performance_id, h.output.output_id, "drained")
    await task


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_archive_timeout_and_cancellation_collect_late_asset(cancel):
    store, storage = InMemoryStore(), SlowStorage()
    service = AgentExpressionAudioService(persistence_for(store), storage=storage)
    task = asyncio.create_task(service.store_pcm_async(timeout_s=.02 if not cancel else 1, **audio()))
    await until(storage.entered.is_set)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
        await task
    storage.release.set()
    await until(lambda: bool(storage.deleted))
    assert not storage.values
    with service.persistence.transaction("org_default", read_only=True) as transaction:
        assert transaction.file_objects.list() == []


@pytest.mark.anyio
async def test_archive_capacity_is_bounded_while_threads_are_blocked():
    storage = SlowStorage()
    service = AgentExpressionAudioService(persistence_for(InMemoryStore()), storage=storage)
    tasks = [asyncio.create_task(service.store_pcm_async(**audio())) for _ in range(4)]
    await until(storage.entered.is_set)
    await asyncio.sleep(.01)
    try:
        with pytest.raises(ApiError) as error:
            await service.store_pcm_async(**audio())
        assert error.value.code == "AGENT_AUDIO_ARCHIVE_BUSY"
    finally:
        storage.release.set()
        await asyncio.gather(*tasks)


@pytest.mark.anyio
async def test_gateway_open_retries_share_one_first_audio_budget():
    store, route = store_and_route()
    # Isolate the shared deadline from the separately tested circuit threshold.
    route["policy"] = {"retry_count": 3, "circuit_failure_threshold": 10}
    calls = []
    class Adapter(DashScopeProvider):
        async def open_tts_stream(self, request, context):
            calls.append(context.timeout_s)
            await asyncio.Event().wait()
    gateway = ModelGateway(store, {"dashscope": Adapter()})
    start = monotonic()
    with pytest.raises(ProviderError) as error:
        await gateway.open_tts_stream(TTSSynthesizeRequest(text="synthetic approved"), route=route,
                                      first_audio_deadline=start + .12)
    assert error.value.code == "provider_timeout"
    assert len(calls) == 4 and 0 < calls[0] <= .03
    assert all(0 < timeout <= .12 for timeout in calls) and sum(calls) <= .12
    assert monotonic() - start < .2


@pytest.mark.anyio
async def test_first_read_uses_remaining_open_budget_without_replaying():
    store, route = store_and_route()
    raw = _RawTTS([b"\1\0" * 48], provider_id="dashscope")
    raw.read_gate = asyncio.Event()
    calls = []
    class Adapter(DashScopeProvider):
        async def open_tts_stream(self, request, context):
            calls.append(1)
            await asyncio.sleep(.02)
            # Real gateway binds this exact model identity.
            raw.meta = raw.meta.model_copy(update={"model": "qwen3-tts-flash"})
            raw.ready_event = raw.event(1, "stream.ready")
            return raw
    gateway = ModelGateway(store, {"dashscope": Adapter()})
    started = monotonic()
    stream = await gateway.open_tts_stream(TTSSynthesizeRequest(text="synthetic approved"), route=route,
                                           first_audio_deadline=started + .05)
    with pytest.raises(ProviderError):
        _ = [event async for event in stream.events()]
    assert monotonic() - started < .15 and calls == [1] and raw.aborted


@pytest.mark.anyio
async def test_first_audio_deadline_does_not_cut_started_speech():
    raw = _RawTTS([b"\1\0" * 48, b"\2\0" * 48])
    async def events():
        yield raw.event(2, "audio.chunk", pcm_s16le=raw.chunks[0])
        await asyncio.sleep(.04)
        yield raw.event(3, "audio.chunk", pcm_s16le=raw.chunks[1])
        yield raw.event(4, "audio.final", total_audio_bytes=192)
    raw.events = events
    stream = ValidatedTTSStream(raw, provider_id=raw.meta.provider_id, model=raw.meta.model,
                               read_timeout_s=.2, first_audio_deadline=monotonic() + .02)
    assert [event.type async for event in stream.events()] == ["audio.chunk", "audio.chunk", "audio.final"]


@pytest.mark.anyio
async def test_client_ready_wait_shares_first_pcm_deadline():
    h = output_harness(first_audio_deadline=monotonic() + .02)
    await h.output.open()
    with pytest.raises(ApiError) as error:
        await h.output.run(h.on_finished)
    assert error.value.code == "AGENT_AUDIO_FIRST_PCM_TIMEOUT"
    assert not h.publisher.published and not h.notifications


@pytest.mark.anyio
async def test_long_first_provider_chunk_survives_deadline_after_first_frame():
    h = output_harness(first_audio_deadline=monotonic() + .02)
    first_frame, remainder = asyncio.Event(), asyncio.Event()
    async def publish(chunk):
        h.publisher.total_samples = 1
        first_frame.set()
        await remainder.wait()
        h.publisher.published.append(chunk)
    h.publisher.publish = publish
    await h.output.open()
    h.output.acknowledge_ready(h.output.performance_id, h.output.output_id)
    task = asyncio.create_task(h.output.run(h.on_finished))
    await first_frame.wait()
    await asyncio.sleep(.06)
    assert not task.done()
    remainder.set()
    await h.producer_notified.wait()
    h.output.acknowledge_drained(h.output.performance_id, h.output.output_id, "drained")
    await task


@pytest.mark.anyio
async def test_runtime_prepares_transports_in_parallel_with_inherited_budget(monkeypatch):
    runtime, channel, _, _ = runtime_harness()
    _mutate(runtime, channel, lambda session: session["turns"][0].update(is_followup=True))
    raw, publisher = _RawTTS([b"\1\0" * 48]), _Publisher()
    tts_entered, publisher_entered = asyncio.Event(), asyncio.Event()
    async def provider(request, *, first_audio_deadline):
        assert 0 < first_audio_deadline - monotonic() <= .2
        tts_entered.set()
        await publisher_entered.wait()
        return ValidatedTTSStream(raw, provider_id=raw.meta.provider_id, model=raw.meta.model, read_timeout_s=1)
    async def open_publisher():
        publisher_entered.set()
        await tts_entered.wait()
        publisher.opened += 1
        return publisher.publication
    publisher.open = open_publisher
    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", lambda *args, **kwargs: publisher)
    with interaction_deadline(monotonic() + .2):
        output = await asyncio.wait_for(runtime._open_streamed_expression(
            channel.interview_id, "turn_1", "synthetic approved", channel.organization_id,
            performance_id="performance_synthetic", assert_current=lambda: None), .3)
    assert publisher.opened == 1 and not publisher.published and not raw.started.is_set()
    await output.open()
    assert publisher.opened == 1
    await output.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("failing_side", ["tts", "publisher", "cancel"])
async def test_parallel_prepare_failure_reaps_both_transports(monkeypatch, failing_side):
    runtime, channel, _, _ = runtime_harness()
    _mutate(runtime, channel, lambda session: session["turns"][0].update(is_followup=True))
    raw, publisher = _RawTTS([b"\1\0" * 48]), _Publisher()
    tts_entered, publisher_entered = asyncio.Event(), asyncio.Event()
    stopped = []
    async def provider(request, **kwargs):
        tts_entered.set()
        await publisher_entered.wait()
        if failing_side == "tts":
            raise ProviderError("provider_streaming_not_supported", "synthetic", retryable=False)
        if failing_side == "publisher":
            return ValidatedTTSStream(raw, provider_id=raw.meta.provider_id, model=raw.meta.model, read_timeout_s=1)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("tts")
    async def open_publisher():
        publisher_entered.set()
        await tts_entered.wait()
        if failing_side == "publisher":
            await asyncio.sleep(0)
            raise RuntimeError("synthetic media failure")
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("publisher")
    publisher.open = open_publisher
    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", lambda *args, **kwargs: publisher)
    task = asyncio.create_task(runtime._open_streamed_expression(
        channel.interview_id, "turn_1", "synthetic approved", channel.organization_id,
        performance_id="performance_synthetic", assert_current=lambda: None))
    await tts_entered.wait()
    await publisher_entered.wait()
    if failing_side == "cancel":
        task.cancel()
    if failing_side == "tts":
        assert await task is None
        assert stopped == ["publisher"]
    else:
        with pytest.raises(asyncio.CancelledError if failing_side == "cancel" else RuntimeError):
            await task
        if failing_side == "publisher":
            assert raw.aborted == 1
        else:
            assert set(stopped) == {"tts", "publisher"}
    assert publisher.abort_calls == 1 and not publisher.published


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["timeout", "cancel", "fresh_speech", "superseded"])
async def test_unplayed_preparation_restores_only_its_current_capture(monkeypatch, reason):
    runtime, channel, _, _ = runtime_harness()
    notices = []
    async def notify(value):
        notices.append(value)
    async def prepare(*args, **kwargs):
        raise AssertionError("This test must not infer another answer")
    capture = SimpleNamespace(is_open=True, prepare_decision=prepare)
    endpoint = AnswerEndpoint(detector=None, capture=capture, commit=prepare, notify=notify)
    endpoint._begin_interaction("synthetic current answer")
    endpoint._use_preparation_budget("synthetic current answer")
    managed = channel._evidence_session
    managed._answer_endpoint, managed._capture_id = endpoint, "capture_synthetic"
    entered, release = asyncio.Event(), asyncio.Event()
    original_revision = endpoint.revision
    def guard():
        if endpoint.revision != original_revision:
            raise ApiError("TURN_DECISION_STALE", "synthetic new speech", status_code=409)
    async def preparation(*args, **kwargs):
        entered.set()
        await release.wait()
        raise asyncio.TimeoutError()
    monkeypatch.setattr(runtime, "_expression_audio", preparation)
    async def select():
        with interaction_deadline(monotonic() + .02 if reason == "cancel" else monotonic() + 1):
            return await channel._select_act(act_type="clarification", text="synthetic clarification",
                turn_id="turn_1", causation_id="synthetic", evidence_refs=[], gesture="listen", approval_guard=guard)
    task = asyncio.create_task(select())
    await entered.wait()
    if reason == "timeout":
        release.set()
        assert await task is None
    else:
        if reason == "cancel":
            await asyncio.sleep(.03)
        elif reason == "fresh_speech":
            endpoint.revision += 1
        else:
            _mutate(runtime, channel, lambda session: session["agent_runtime"].update(active_performance_id="new_performance"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    session = runtime.interviews.get_interview(channel.interview_id)
    state = session["agent_runtime"]
    assert session["status"] == "in_progress" and capture.is_open and not session["answers"]
    if reason == "superseded":
        assert state["floor"] == "agent" and state["active_performance_id"] == "new_performance"
        assert not notices
    else:
        assert state["floor"] == "candidate" and state["active_performance_id"] is None
        delivery = session["turns"][0]["conversation_acts"][-1]["selection_receipts"][-1]["delivery"]
        assert delivery["status"] == ("interrupted" if reason == "fresh_speech" else "failed")
        assert endpoint.preparation_blocked is (reason != "fresh_speech")
        assert notices == ([] if reason == "fresh_speech" else ["understanding_retry_exhausted"])
        if reason != "fresh_speech":
            endpoint.continue_speaking()
            assert not endpoint.preparation_blocked


def attach_endpoint(h):
    notices = []
    async def notify(value):
        notices.append(value)
    async def unexpected(*args, **kwargs):
        raise AssertionError("This test must preserve, not infer or submit, the capture")
    capture = SimpleNamespace(is_open=True, prepare_decision=unexpected)
    endpoint = AnswerEndpoint(detector=None, capture=capture, commit=unexpected, notify=notify)
    endpoint._begin_interaction("synthetic retained answer")
    endpoint._use_preparation_budget("synthetic retained answer")
    h.channel._evidence_session._answer_endpoint = endpoint
    h.channel._evidence_session._capture_id = "capture_synthetic"
    return endpoint, notices


@pytest.mark.anyio
@pytest.mark.parametrize("timeout_stage", ["client_ready", "first_read"])
async def test_selected_zero_pcm_timeout_preserves_capture_and_rejects_late_ack(monkeypatch, timeout_stage):
    h = _streaming_harness(monkeypatch, gate_before_first=True)
    endpoint, notices = attach_endpoint(h)
    performance = await _select(h)
    o = h.outputs[0]
    o.output._first_audio_deadline = monotonic() + .02
    task = h.channel._speech_output_task
    if timeout_stage == "first_read":
        await _signal(h, "avatar.performance.ready", performance.performance_id, o.output.output_id)
        await o.raw.started.wait()
    await task
    state = _session(h)["agent_runtime"]
    assert _session(h)["status"] == "in_progress" and endpoint.capture.is_open and not _session(h)["answers"]
    assert state["floor"] == "candidate" and state["active_performance_id"] is None and state["active_output_id"] is None
    assert o.output.closed and o.publisher.abort_calls and not o.publisher.published and not o.archives
    assert notices == ["understanding_retry_exhausted"] and endpoint.preparation_blocked
    delivery = _session(h)["turns"][0]["conversation_acts"][-1]["selection_receipts"][-1]["delivery"]
    assert delivery["status"] == "failed" and delivery["performance_id"] == performance.performance_id
    # Provider release and delayed browser callbacks cannot finish or replay it.
    o.raw.read_gate.set()
    await _signal(h, "avatar.performance.ready", performance.performance_id, o.output.output_id)
    await _signal(h, "avatar.performance.stopped", performance.performance_id, o.output.output_id, "drained")
    assert _session(h)["agent_runtime"]["floor"] == "candidate" and not o.publisher.published
    assert not any(e["type"] in {"avatar.performance.producer_finished", "avatar.performance.stopped"} for e in h.emitted)
    assert len(h.outputs) == 1
    endpoint.continue_speaking()
    assert not endpoint.preparation_blocked


@pytest.mark.anyio
async def test_zero_sample_counter_after_attempted_pcm_keeps_conservative_failure(monkeypatch):
    h = _streaming_harness(monkeypatch)
    endpoint, notices = attach_endpoint(h)
    performance = await _select(h)
    o = h.outputs[0]
    async def partly_delivered(chunk):
        o.publisher.pcm_delivery_started = True
        raise ApiError("LIVEKIT_AUDIO_OUTPUT_TIMEOUT", "one sink may already have received PCM", status_code=503)
    o.publisher.publish = partly_delivered
    task = h.channel._speech_output_task
    await _signal(h, "avatar.performance.ready", performance.performance_id, o.output.output_id)
    await task
    assert o.output.total_samples == 0 and getattr(o.publisher, "total_samples", 0) == 0
    assert _session(h)["status"] == "paused" and not endpoint.preparation_blocked and not notices
    assert len(h.outputs) == 1 and not o.archives


@pytest.mark.anyio
@pytest.mark.parametrize("resume", ["explicit_retry", "new_speech"])
async def test_unheard_reception_rolls_back_only_its_cursor_and_can_speak_again(monkeypatch, resume):
    h = _streaming_harness(monkeypatch)
    endpoint, clock, _, commits, _ = reception_endpoint()
    managed = h.channel._evidence_session
    managed._answer_endpoint, managed._capture_id = endpoint, "capture_synthetic"
    confirmation = endpoint.confirmation
    previous = _final("前一个已经听到解释的请求。")
    confirmation._remember_handled(previous)
    confirmation._reception_seen_text = previous.text
    confirmation._reception_seen_final = previous
    endpoint.capture.current_final = _final(previous.text + "这里说的幂等是什么意思？")
    async def speak(kind, guard, **kwargs):
        performance = await h.channel._select_act(act_type="conversation_acknowledgement", text="合成解释。",
            turn_id="turn_1", causation_id=None, evidence_refs=[], gesture="listen", approval_guard=guard,
            on_playback_selected=lambda: confirmation.playback_selected(endpoint))
        return performance is not None
    confirmation.speak = speak
    try:
        await confirmation._listen(endpoint)
        assert confirmation.speaking and confirmation._already_handled(endpoint.capture.current_final)
        first = h.outputs[0]
        first.output._ready_timeout = .01
        await h.channel._speech_output_task
        assert confirmation.phase == "listening" and endpoint.preparation_blocked
        assert confirmation._handled_final.text == confirmation._reception_seen_text == previous.text
        assert not confirmation._already_handled(endpoint.capture.current_final)
        if resume == "explicit_retry":
            endpoint.continue_speaking()
        else:
            endpoint.capture.current_final = _final(endpoint.capture.current_final.text + "请再解释一下。")
            endpoint.observe_transcript(endpoint.capture.current_final.text)
        clock.value += 6
        await confirmation.step(endpoint, 6)
        assert len(h.outputs) == 2 and confirmation.speaking
        assert not commits and endpoint.capture.is_open
        assert first.output.closed and not first.publisher.published
    finally:
        await h.channel._cancel_speech_output()
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [True, False])
async def test_cancel_after_selection_before_output_task_restores_only_unheard_capture(monkeypatch, expired):
    h = _streaming_harness(monkeypatch)
    endpoint, notices = attach_endpoint(h)
    emit = h.channel._emit
    entered = asyncio.Event()
    async def held_started(kind, *args, **kwargs):
        event = await emit(kind, *args, **kwargs)
        if kind == "avatar.performance.started":
            entered.set()
            await asyncio.Event().wait()
        return event
    h.channel._emit = held_started
    async def select():
        with interaction_deadline(monotonic() + (.01 if expired else 1)):
            await _select(h)
    task = asyncio.create_task(select())
    await entered.wait()
    if expired:
        await asyncio.sleep(.02)
    else:
        endpoint.revision += 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    o = h.outputs[0]
    assert _session(h)["status"] == "in_progress" and _session(h)["agent_runtime"]["floor"] == "candidate"
    assert _session(h)["agent_runtime"]["active_performance_id"] is None
    assert h.channel._speech_output_task is None and not o.publisher.published and o.output.closed
    assert endpoint.preparation_blocked is expired
    assert notices == (["understanding_retry_exhausted"] if expired else [])
    delivery = _session(h)["turns"][0]["conversation_acts"][-1]["selection_receipts"][-1]["delivery"]
    assert delivery["status"] == ("failed" if expired else "interrupted")


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["exit", "content", "late_selection_result"])
async def test_unheard_question_grants_no_reply_permission_even_when_selection_returns_late(monkeypatch, kind):
    h = _streaming_harness(monkeypatch)
    endpoint, _, _, commits, _ = reception_endpoint()
    managed = h.channel._evidence_session
    managed._answer_endpoint, managed._capture_id = endpoint, "capture_synthetic"
    confirmation = endpoint.confirmation
    async def speak(speech_kind, guard, **kwargs):
        performance = await h.channel._select_act(act_type="clarification", text="合成确认问题。",
            turn_id="turn_1", causation_id=None, evidence_refs=[], gesture="listen", approval_guard=guard,
            on_playback_selected=lambda: confirmation.playback_selected(endpoint))
        h.outputs[-1].output._ready_timeout = .01
        if kind == "late_selection_result":
            await h.channel._speech_output_task
        return {"act_id": _session(h)["turns"][0]["conversation_acts"][-1]["act_id"]} if performance else None
    confirmation.speak = speak
    try:
        if kind == "exit":
            await confirmation._ask_exit(endpoint, endpoint.capture.current_final)
        elif kind == "content":
            await confirmation.clarify_answer(endpoint, SimpleNamespace(clarification_target=SimpleNamespace(
                evidence_quote=endpoint.capture.current_final.text, focus_quote="幂等")), endpoint.capture.current_final)
        else:
            await confirmation._listen(endpoint)
        if h.channel._speech_output_task:
            await h.channel._speech_output_task
        assert confirmation.phase == "listening" and not confirmation.confirmed
        assert not confirmation._exit_waiting and not confirmation._exit_question_delivered
        assert not confirmation._content_delivery_pending and not confirmation._content_question_delivered
        assert confirmation._handled_final is None and confirmation._reception_seen_text == ""
        assert not commits and endpoint.capture.is_open and endpoint.preparation_blocked
        assert not h.outputs[0].publisher.published
    finally:
        await h.channel._cancel_speech_output()
        await endpoint.close()


@pytest.mark.anyio
async def test_timed_out_output_does_not_restore_replacement_performance(monkeypatch):
    h = _streaming_harness(monkeypatch, gate_before_first=True)
    endpoint, notices = attach_endpoint(h)
    performance = await _select(h)
    o = h.outputs[0]
    entered, release = asyncio.Event(), asyncio.Event()
    abort = o.publisher.abort
    async def delayed_abort():
        entered.set()
        await release.wait()
        await abort()
    o.publisher.abort = delayed_abort
    o.output._first_audio_deadline = monotonic() + .01
    task = h.channel._speech_output_task
    await entered.wait()
    _mutate(h.runtime, h.channel, lambda session: session["agent_runtime"].update(
        active_performance_id="performance_replacement", active_output_id="speech_output_replacement"))
    release.set()
    await task
    assert _session(h)["status"] == "in_progress"
    state = _session(h)["agent_runtime"]
    assert state["floor"] == "agent" and state["active_performance_id"] == "performance_replacement"
    assert not notices and not endpoint.preparation_blocked
    await _signal(h, "avatar.performance.stopped", performance.performance_id, o.output.output_id, "drained")
    assert _session(h)["agent_runtime"]["active_performance_id"] == "performance_replacement"


class Dialogue:
    stream_id = "synthetic_dialogue"
    ready_events = []
    def __init__(self, block=False):
        self.block = block
        self.chunks = []
        self.aborted = 0
        self.committed = False
    async def send_audio(self, chunk):
        if self.block:
            await asyncio.Event().wait()
        self.chunks.append(chunk)
        return []
    async def abort(self):
        self.aborted += 1
    async def commit(self, command, on_event=None):
        self.committed = True
        return []


@pytest.mark.anyio
async def test_hung_optional_input_does_not_delay_authoritative_stt_or_recording():
    authoritative, recorded = [], []
    async def send(chunk):
        authoritative.append(chunk)
        return []
    stt = StreamingInterviewSTT(InMemoryStore(), "iv_synthetic")
    stt.stream = SimpleNamespace(send_audio=send)
    stt.recording = SimpleNamespace(append=recorded.append)
    optional = Dialogue(block=True)
    stt.dialogue = OptionalSpeechDialogueInput(optional, max_pending_bytes=4096, send_timeout=.02)
    for chunk in (b"\1\0" * 32, b"\2\0" * 32):
        await asyncio.wait_for(stt.send_audio(chunk), .01)
    await until(lambda: optional.aborted == 1)
    events = await asyncio.wait_for(stt.send_audio(b"\3\0" * 32), .01)
    assert authoritative == recorded and len(recorded) == 3
    assert stt.dialogue is None and any(event.get("type") == "dialogue.error" for event in events)
    await asyncio.gather(*stt._dialogue_cleanup_tasks)


@pytest.mark.anyio
async def test_optional_input_overflow_fails_only_that_expression():
    optional = Dialogue(block=True)
    buffered = OptionalSpeechDialogueInput(optional, max_pending_bytes=4)
    await buffered.send_audio(b"1234")
    with pytest.raises(ProviderError) as error:
        await buffered.send_audio(b"56")
    assert error.value.code == "provider_dialogue_input_overflow"
    await buffered.abort()
    with pytest.raises(ProviderError):
        await buffered.commit(None)
    assert not optional.committed


@pytest.mark.anyio
async def test_optional_commit_drains_ordered_input_and_abort_is_idempotent():
    optional = Dialogue()
    buffered = OptionalSpeechDialogueInput(optional, max_pending_bytes=32)
    await buffered.send_audio(b"12")
    await buffered.send_audio(b"34")
    await buffered.commit(None)
    assert optional.chunks == [b"12", b"34"] and optional.committed
    await buffered.abort()
    await buffered.abort()
    assert optional.aborted == 1


@pytest.mark.anyio
async def test_late_optional_provider_completion_cannot_reach_commit_or_events():
    release, entered = asyncio.Event(), asyncio.Event()
    class Late(Dialogue):
        async def send_audio(self, chunk):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return [SimpleNamespace(type="output.audio.delta")]
    provider = Late()
    buffered = OptionalSpeechDialogueInput(provider, max_pending_bytes=32, send_timeout=.01)
    await buffered.send_audio(b"12")
    await entered.wait()
    await until(lambda: provider.aborted == 1)
    release.set()
    await asyncio.sleep(.01)
    assert buffered._events == []
    with pytest.raises(ProviderError):
        await buffered.commit(None)
    assert not provider.committed
