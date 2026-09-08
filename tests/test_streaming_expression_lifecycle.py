"""Agent integration around real ApprovedSpeechOutput and fake PCM transport."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.model_gateway.tts_streaming import ValidatedTTSStream
from app.services import interview_agent
from app.services.approved_speech_output import ApprovedSpeechOutput
from app.services.evidence_coordination import assert_current_evidence_control
from test_approved_speech_output import _Publisher, _RawTTS
from test_expression_stale_fence import _harness, _mutate


def _streaming_harness(monkeypatch, *, gate_before_first=False, gate_eof=False):
    runtime, channel, coordinator, grant = _harness()
    monkeypatch.setattr(interview_agent, "ManagedLiveKitEvidenceSession", SimpleNamespace)
    managed = channel._evidence_session
    managed.assert_controller = lambda _channel: None

    async def detach(_channel):
        managed._stopped = True

    managed.detach = detach
    async def pause_capture():
        managed.capture_paused = True

    managed.pause_capture = pause_capture
    outputs = []

    async def open_streamed(_interview_id, _turn_id, _text, _org, *, performance_id, assert_current):
        raw = _RawTTS([b"\1\0" * 480, b"\2\0" * 480])
        if gate_before_first:
            raw.read_gate = asyncio.Event()
        if gate_eof:
            raw.eof_gate = asyncio.Event()
        publisher = _Publisher()
        publisher.publication = replace(
            publisher.publication, performance_id=performance_id,
            participant_identity="expression:" + performance_id,
            track_sid="TR_" + str(len(outputs) + 1), track_name="approved-expression:" + performance_id,
        )
        stream = ValidatedTTSStream(raw, provider_id="synthetic_tts", model=raw.meta.model, read_timeout_s=1)
        archives = []

        def archive(**audio):
            archives.append(audio)
            return {"audio_uri": "private-file://synthetic-approved-archive"}

        output = ApprovedSpeechOutput(
            performance_id=performance_id, stream=stream, publisher=publisher,
            assert_current=assert_current, archive=archive, ready_timeout=0.5,
            drain_timeout=0.5, total_timeout=2,
        )
        outputs.append(SimpleNamespace(output=output, raw=raw, publisher=publisher, archives=archives))
        return output

    monkeypatch.setattr(runtime, "_open_streamed_expression", open_streamed)
    return SimpleNamespace(runtime=runtime, channel=channel, coordinator=coordinator, grant=grant, outputs=outputs)


async def _select(h, turn_id="turn_1", text="请说明测试方案。"):
    async with h.channel._lock:
        return await h.channel._select_act(
            act_type="question", text=text, turn_id=turn_id,
            causation_id="synthetic-select", evidence_refs=[], gesture="look_at_candidate",
        )


def _session(h):
    return h.runtime.interviews.get_interview(h.channel.interview_id)


async def _signal(h, kind, performance_id, output_id=None, reason=None):
    payload = {"performance_id": performance_id}
    if output_id is not None:
        payload["output_id"] = output_id
    if reason is not None:
        payload["reason"] = reason
    h.signal_sequence = getattr(h, "signal_sequence", 0) + 1
    await h.channel.send(ClientSignal(
        type=kind, idempotency_key="synthetic_signal_%s" % h.signal_sequence,
        turn_id=_session(h).get("current_turn_id"), payload=payload,
    ))


async def _wait_until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("Synthetic integration condition did not settle")


async def _replacement_channel(h):
    # Exercise the real control initialization, with only native media attach
    # replaced by a fresh fenced owner fixture; no room or network is created.
    h.runtime.evidence_ingress = SimpleNamespace(enabled=False)
    opened = h.channel.opened.model_copy(update={
        "connection_id": "replacement_control", "recovery_cursor": 0,
        "capabilities": h.channel.opened.capabilities.model_copy(update={"media_recorder": True}),
    })
    replacement = interview_agent.AgentChannel(h.runtime, opened)
    await replacement.initialize()
    grant = h.coordinator.attach_control(
        interview_id=replacement.interview_id, organization_id=replacement.organization_id,
        connection_id=opened.connection_id, local_instance_id=h.grant.local_instance_id,
    )
    managed = SimpleNamespace(ownership=grant, _stopped=False)

    def assert_controller(_channel):
        with h.runtime.persistence.transaction(replacement.organization_id) as transaction:
            assert_current_evidence_control(transaction, grant)

    async def detach(_channel):
        managed._stopped = True

    managed.assert_controller = assert_controller
    managed.detach = detach
    replacement._evidence_session = managed
    return SimpleNamespace(**{**vars(h), "channel": replacement, "grant": grant, "signal_sequence": 1000})


async def _client_ready(h):
    await h.channel.send(ClientSignal(type="client.ready", idempotency_key="replacement_client_ready"))


@pytest.mark.parametrize("enabled", [None, "false", "FALSE", "0", ""])
def test_streaming_tts_is_closed_by_default_or_explicitly_without_provider_or_publisher_calls(monkeypatch, enabled):
    async def scenario():
        runtime, channel, _coordinator, _grant = _harness()
        # Dynamic follow-ups would use streaming when explicitly enabled;
        # a primary question would mask a broken default by returning early.
        _mutate(runtime, channel, lambda session: session["turns"][0].update(is_followup=True))
        if enabled is None:
            monkeypatch.delenv("INTERVIEWER_STREAMING_TTS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", enabled)
        calls = []

        async def provider(*_args, **_kwargs):
            calls.append("provider")
            raise AssertionError("Disabled experimental streaming must never contact a provider")

        def publisher(*_args, **_kwargs):
            calls.append("publisher")
            raise AssertionError("Disabled experimental streaming must never create a media publisher")

        monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
        monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", publisher)
        output = await runtime._open_streamed_expression(
            channel.interview_id, "turn_1", "请说明测试方案。", channel.organization_id,
            performance_id="performance_synthetic", assert_current=lambda: None,
        )
        assert output is None and calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("owner_current", [True, False])
def test_explicit_streaming_opt_in_retains_owner_fence_and_ready_before_pcm(monkeypatch, owner_current):
    async def scenario():
        runtime, channel, _coordinator, _grant = _harness()
        _mutate(runtime, channel, lambda session: session["turns"][0].update(is_followup=True))
        monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
        raw = _RawTTS([b"\1\0" * 480])
        stream = ValidatedTTSStream(raw, provider_id="synthetic_tts", model=raw.meta.model, read_timeout_s=1)
        publisher = _Publisher()
        requests, publications = [], []

        async def provider(request):
            requests.append(request)
            return stream

        def make_publisher(_plane, **kwargs):
            publications.append(kwargs)
            return publisher

        def fence():
            if not owner_current:
                raise ApiError("EXPRESSION_OWNER_STALE", "Synthetic owner replaced.", status_code=409)

        monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
        monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", make_publisher)
        if not owner_current:
            with pytest.raises(ApiError) as exc:
                await runtime._open_streamed_expression(
                    channel.interview_id, "turn_1", "请说明测试方案。", channel.organization_id,
                    performance_id="performance_synthetic", assert_current=fence,
                )
            assert exc.value.code == "EXPRESSION_OWNER_STALE"
            assert requests == [] and publications == []
            return
        output = await runtime._open_streamed_expression(
            channel.interview_id, "turn_1", "请说明测试方案。", channel.organization_id,
            performance_id="performance_synthetic", assert_current=fence,
        )
        try:
            assert isinstance(output, ApprovedSpeechOutput)
            assert len(requests) == 1 and requests[0].metadata["approved"] is True
            assert requests[0].purpose == "interview_agent_expression"
            assert len(publications) == 1 and publications[0]["assert_current"] is fence
            assert publications[0]["performance_id"] == "performance_synthetic"
            assert publisher.opened == 0 and publisher.published == [] and not raw.started.is_set()
        finally:
            await output.abort()

    asyncio.run(scenario())


def test_selection_returns_without_waiting_for_ready_and_only_remote_drain_releases_floor(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_eof=True)
        try:
            performance = await asyncio.wait_for(_select(h), timeout=0.5)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            assert performance.delivery == "streaming_tts"
            assert performance.live_audio.output_id == item.output.output_id
            assert not h.channel._lock.locked() and item.publisher.published == []
            assert _session(h)["agent_runtime"]["active_output_id"] == item.output.output_id
            await asyncio.wait_for(_signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id), timeout=0.5)
            await item.raw.eof_reached.wait()
            assert item.publisher.published
            assert not item.output.producer_finished and item.archives == []
            before = _session(h)["agent_runtime"]
            await _signal(h, "avatar.performance.stopped", performance.performance_id, item.output.output_id, "drained")
            assert _session(h)["agent_runtime"]["floor"] == "agent"
            assert _session(h)["agent_runtime"]["active_performance_id"] == before["active_performance_id"]
            item.raw.eof_gate.set()
            await _wait_until(lambda: item.output.producer_finished)
            assert _session(h)["agent_runtime"]["floor"] == "agent"
            assert item.publisher.close_calls == 0 and not task.done()
            for wrong_output, reason in [(None, "completed"), ("old_output", "drained"), (item.output.output_id, "error")]:
                await _signal(h, "avatar.performance.stopped", performance.performance_id, wrong_output, reason)
                assert _session(h)["agent_runtime"]["active_output_id"] == item.output.output_id
            await _signal(h, "avatar.performance.stopped", performance.performance_id, item.output.output_id, "drained")
            await asyncio.wait_for(task, timeout=0.5)
            current = _session(h)
            assert current["status"] == "in_progress" and current["agent_runtime"]["floor"] == "candidate"
            assert current["agent_runtime"]["active_performance_id"] is None
            assert current["agent_runtime"].get("active_output_id") is None
            assert item.publisher.close_calls == 1 and h.channel._speech_output is None
            assert h.channel._speech_output_task is None
        finally:
            await h.channel.close("test_finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("change_turn", [False, True])
def test_replaced_output_cancels_old_task_and_old_ready_or_stop_cannot_change_new_performance(monkeypatch, change_turn):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        try:
            first = await _select(h)
            old = h.outputs[0]
            old_task = h.channel._speech_output_task
            await _signal(h, "avatar.performance.ready", first.performance_id, old.output.output_id)
            await old.raw.chunk_reached.wait()
            turn_id = "turn_2" if change_turn else "turn_1"
            if change_turn:
                def next_turn(session):
                    session["current_turn_id"] = turn_id
                    session["turns"].append({"id": turn_id, "status": "asking", "order": 2,
                                             "question_spoken_text": "请介绍下一项方案。", "is_followup": False})
                _mutate(h.runtime, h.channel, next_turn)
            second = await _select(h, turn_id, "请介绍下一项方案。")
            new = h.outputs[1]
            assert old_task.done() and old.raw.read_cancelled and old.publisher.abort_calls >= 1
            assert old.archives == [] and h.channel._speech_output is new.output
            expected = _session(h)["agent_runtime"]
            await _signal(h, "avatar.performance.ready", first.performance_id, old.output.output_id)
            await _signal(h, "avatar.performance.stopped", first.performance_id, old.output.output_id, "drained")
            await _signal(h, "avatar.performance.stopped", second.performance_id, old.output.output_id, "drained")
            after = _session(h)["agent_runtime"]
            assert after["floor"] == expected["floor"] == "agent"
            assert after["active_performance_id"] == second.performance_id
            assert after["active_output_id"] == new.output.output_id
            assert new.publisher.published == [] and not new.raw.started.is_set()
            assert _session(h)["status"] == "in_progress"
        finally:
            await h.channel.close("test_finished")

    asyncio.run(scenario())


def test_owner_loss_while_provider_read_is_stalled_aborts_without_new_pcm_or_late_pause(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        try:
            performance = await _select(h)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await item.raw.chunk_reached.wait()
            assert h.coordinator.release(h.grant)
            h.coordinator.attach_control(
                interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
                connection_id="new_controller", local_instance_id="new_instance",
            )
            before = _session(h)
            await asyncio.wait_for(task, timeout=0.5)
            item.raw.read_gate.set()
            assert item.publisher.published == [] and item.archives == []
            assert item.raw.read_cancelled and item.publisher.abort_calls >= 1
            after = _session(h)
            assert after["status"] == before["status"] == "in_progress"
            assert after["agent_runtime"] == before["agent_runtime"]
            assert after["agent_events"] == before["agent_events"]
            assert h.channel._speech_output is None and h.channel._speech_output_task is None
        finally:
            await h.channel.close("test_finished")

    asyncio.run(scenario())


def test_owner_loss_after_first_chunk_stops_remaining_pcm_without_a_false_complete_archive(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch)
        try:
            performance = await _select(h)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            publish = item.publisher.publish

            async def publish_then_replace_owner(chunk):
                await publish(chunk)
                if len(item.publisher.published) == 1:
                    assert h.coordinator.release(h.grant)
                    h.coordinator.attach_control(
                        interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
                        connection_id="new_controller", local_instance_id="new_instance",
                    )

            item.publisher.publish = publish_then_replace_owner
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await asyncio.wait_for(task, timeout=0.5)
            assert item.publisher.published == [b"\1\0" * 480]
            assert item.archives == [] and not item.output.producer_finished
            assert item.publisher.abort_calls >= 1 and item.raw.aborted >= 1
            assert _session(h)["status"] == "in_progress"
            assert not any(event["type"] == "problem" for event in _session(h)["agent_events"])
        finally:
            await h.channel.close("test_finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["pause", "close"])
def test_pause_or_control_close_cancels_output_without_any_late_expression_failure(monkeypatch, ending):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        try:
            performance = await _select(h)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await item.raw.chunk_reached.wait()
            if ending == "pause":
                await h.channel.send(ClientSignal(type="pause", idempotency_key="synthetic_pause", turn_id="turn_1"))
                assert _session(h)["status"] == "paused"
                assert _session(h)["agent_runtime"]["floor"] == "none"
                assert _session(h)["agent_runtime"]["active_performance_id"] is None
                assert h.channel._evidence_session.capture_paused
            else:
                await h.channel.close("synthetic_control_close")
            assert task.done() and item.raw.read_cancelled
            item.raw.read_gate.set()
            await asyncio.sleep(0)
            assert h.channel._speech_output is None and h.channel._speech_output_task is None
            assert item.publisher.published == [] and item.archives == [] and item.publisher.abort_calls >= 1
            assert not any(item["type"] == "problem" and item.get("payload", {}).get("action") == "pause"
                           for item in _session(h)["agent_events"])
        finally:
            await h.channel.close("test_finished")

    asyncio.run(scenario())


def test_wrong_ready_scope_never_opens_pcm_then_exact_ready_is_still_accepted(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_eof=True)
        try:
            performance = await _select(h)
            item = h.outputs[0]
            for perf_id, output_id in [("old_performance", item.output.output_id), (performance.performance_id, "old_output")]:
                await _signal(h, "avatar.performance.ready", perf_id, output_id)
            await asyncio.sleep(0.01)
            assert item.publisher.published == [] and not item.raw.started.is_set()
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await item.raw.eof_reached.wait()
            assert item.publisher.published
        finally:
            await h.channel.close("test_finished")

    asyncio.run(scenario())


def test_control_reconnect_cannot_inherit_an_unreplayable_aborted_output_as_active(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        replacement = None
        try:
            performance = await _select(h)
            item = h.outputs[0]
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await item.raw.chunk_reached.wait()
            await h.channel.close("synthetic_control_connection_lost")
            assert item.publisher.abort_calls >= 1
            assert h.channel._speech_output_task is None
            # Opening another control channel must not recreate a native room
            # in this isolated regression; only its replay/snapshot is tested.
            h.runtime.evidence_ingress = SimpleNamespace(enabled=False)
            opened = h.channel.opened.model_copy(update={
                "connection_id": "replacement_control", "recovery_cursor": 0,
                "capabilities": h.channel.opened.capabilities.model_copy(update={"media_recorder": True}),
            })
            replacement = interview_agent.AgentChannel(h.runtime, opened)
            await replacement.initialize()
            assert replacement._speech_output is None
            assert not any(event.type == "avatar.performance.started" for event in replacement._queue._queue)
            current = _session(h)
            assert current["agent_runtime"].get("active_output_id") is None, (
                "The publisher was aborted and its transient binding cannot replay; keeping its active output wedges reconnect"
            )
        finally:
            await h.channel.close("test_finished")
            if replacement is not None:
                await replacement.close("test_finished")

    asyncio.run(scenario())


def test_reconnect_ready_reissues_approved_text_on_a_new_track_without_replaying_old_pcm(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        replacement = None
        try:
            performance = await _select(h)
            old = h.outputs[0]
            await _signal(h, "avatar.performance.ready", performance.performance_id, old.output.output_id)
            await old.raw.chunk_reached.wait()
            await h.channel.close("synthetic_control_connection_lost")
            assert old.publisher.abort_calls >= 1 and old.raw.read_cancelled
            old.raw.read_gate.set()
            replacement = await _replacement_channel(h)
            await asyncio.wait_for(_client_ready(replacement), timeout=0.5)
            assert len(h.outputs) == 2
            new = h.outputs[1]
            assert new.output.performance_id != old.output.performance_id
            assert new.output.output_id != old.output.output_id
            assert new.publisher.publication.track_sid != old.publisher.publication.track_sid
            assert new.publisher.publication.participant_identity != old.publisher.publication.participant_identity
            assert new.publisher.published == [] and old.publisher.published == []
            acts = [event for event in _session(h)["agent_events"] if event["type"] == "conversation.act.selected"]
            assert len(acts) == 2 and acts[0]["payload"]["text"] == acts[1]["payload"]["text"]
            started = [event for event in replacement.channel._queue._queue if event.type == "avatar.performance.started"]
            assert len(started) == 1
            assert started[0].payload["live_audio"]["track_sid"] == new.publisher.publication.track_sid
            assert started[0].payload["performance_id"] == new.output.performance_id
            for kind in ["avatar.performance.ready", "avatar.performance.stopped"]:
                await _signal(replacement, kind, old.output.performance_id, old.output.output_id, "drained")
            assert _session(h)["agent_runtime"]["active_output_id"] == new.output.output_id
            assert new.publisher.published == [] and not new.raw.started.is_set()
            await _signal(replacement, "avatar.performance.ready", new.output.performance_id, new.output.output_id)
            await new.raw.chunk_reached.wait()
            new.raw.read_gate.set()
            await _wait_until(lambda: new.output.producer_finished)
            task = replacement.channel._speech_output_task
            assert old.publisher.published == [] and old.archives == []
            assert new.publisher.published == [b"\1\0" * 480, b"\2\0" * 480]
            await _signal(replacement, "avatar.performance.stopped", new.output.performance_id, new.output.output_id, "drained")
            await asyncio.wait_for(task, timeout=0.5)
            assert _session(h)["agent_runtime"]["floor"] == "candidate"
            assert _session(h)["agent_runtime"].get("expression_replay_act_event_id") is None
        finally:
            await h.channel.close("test_finished")
            if replacement is not None:
                await replacement.channel.close("test_finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("superseding_state", ["new_turn", "new_act", "paused"])
def test_reconnect_discards_old_approved_replay_after_turn_act_or_session_changes(monkeypatch, superseding_state):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        replacement = None
        try:
            await _select(h)
            await h.channel.close("synthetic_control_connection_lost")

            def supersede(session):
                if superseding_state == "new_turn":
                    session["current_turn_id"] = "turn_2"
                    session["turns"].append({"id": "turn_2", "status": "asking", "order": 2,
                                             "question_spoken_text": "这是新问题。", "is_followup": False})
                elif superseding_state == "new_act":
                    old = next(event for event in reversed(session["agent_events"])
                               if event["type"] == "conversation.act.selected")
                    session["agent_events"].append({**old, "event_id": "newer_approved_event"})
                else:
                    session["status"] = "paused"
                    session["agent_runtime"]["floor"] = "none"

            _mutate(h.runtime, h.channel, supersede)
            replacement = await _replacement_channel(h)
            await _client_ready(replacement)
            assert len(h.outputs) == 1
            assert replacement.channel._speech_output is None and replacement.channel._speech_output_task is None
            assert not any(event.type == "avatar.performance.started" for event in replacement.channel._queue._queue)
            current = _session(h)
            assert current["agent_runtime"].get("active_output_id") is None
            assert current["agent_runtime"].get("active_performance_id") is None
            if superseding_state == "paused":
                assert current["status"] == "paused"
            else:
                assert current["agent_runtime"].get("expression_replay_act_event_id") is None
                assert current["status"] == "in_progress"
            assert not any(event["type"] == "problem" for event in current["agent_events"])
        finally:
            await h.channel.close("test_finished")
            if replacement is not None:
                await replacement.channel.close("test_finished")

    asyncio.run(scenario())


def test_close_replay_mark_failure_still_cancels_pcm_tasks_and_orphan_recovers_on_ready(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        replacement = None
        try:
            performance = await _select(h)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            managed = h.channel._evidence_session
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await item.raw.chunk_reached.wait()

            def mark_unavailable(*_args, **_kwargs):
                raise ApiError("SYNTHETIC_STORE_UNAVAILABLE", "Synthetic close failure.", status_code=503)

            with monkeypatch.context() as scoped:
                scoped.setattr(h.runtime, "_mark_streamed_expression_for_replay", mark_unavailable)
                with pytest.raises(ApiError) as exc:
                    await h.channel.close("synthetic_database_temporarily_unavailable")
            assert exc.value.code == "SYNTHETIC_STORE_UNAVAILABLE"
            assert task.done() and item.raw.read_cancelled and managed._stopped
            assert h.channel._speech_output is None and h.channel._speech_output_task is None
            assert h.channel._evidence_session is None and h.channel._queue.qsize() == 1
            assert item.publisher.abort_calls >= 1 and item.archives == []
            assert _session(h)["agent_runtime"]["active_output_id"] == item.output.output_id
            replacement = await _replacement_channel(h)
            await _client_ready(replacement)
            assert len(h.outputs) == 2
            assert _session(h)["agent_runtime"]["active_output_id"] == h.outputs[1].output.output_id
            assert h.outputs[1].output.output_id != item.output.output_id
            assert _session(h)["status"] == "in_progress"
        finally:
            await h.channel.close("test_finished")
            if replacement is not None:
                await replacement.channel.close("test_finished")

    asyncio.run(scenario())


def test_late_old_control_close_cannot_clear_a_new_recovery_performance(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        replacement = None
        try:
            performance = await _select(h)
            old = h.outputs[0]
            await _signal(h, "avatar.performance.ready", performance.performance_id, old.output.output_id)
            await old.raw.chunk_reached.wait()
            # A new controller can arrive before the old close callback. Its
            # recovery makes a new output; any delayed cleanup owns old IDs only.
            replacement = await _replacement_channel(h)
            await _client_ready(replacement)
            assert len(h.outputs) == 2
            new = h.outputs[1]
            h.runtime._mark_streamed_expression_for_replay(
                h.channel.interview_id, h.channel.organization_id,
                performance_id=old.output.performance_id, output_id=old.output.output_id,
            )
            await h.channel.close("superseded_control_close_arrived_late")
            state = _session(h)["agent_runtime"]
            assert state["active_output_id"] == new.output.output_id
            assert state["active_performance_id"] == new.output.performance_id
            assert state["floor"] == "agent" and state.get("expression_replay_act_event_id") is None
            assert replacement.channel._speech_output is new.output
            assert new.publisher.abort_calls == 0 and not replacement.channel._speech_output_task.done()
            assert old.publisher.abort_calls >= 1 and old.archives == []
        finally:
            await h.channel.close("test_finished")
            if replacement is not None:
                await replacement.channel.close("test_finished")

    asyncio.run(scenario())
