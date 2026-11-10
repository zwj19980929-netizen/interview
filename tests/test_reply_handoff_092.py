"""PCM after delivered playback survives the old-prefix ASR handoff."""
import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.livekit_audio_ingress import LiveKitIngressAudioFrame, LiveKitAudioIngressBackpressureError
from app.core.errors import ApiError
from app.services.livekit_evidence_ingress import ManagedLiveKitEvidenceSession
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final


def frame(number):
    return LiveKitIngressAudioFrame(number, "track", 16000, 1, 320,
                                   "2026-11-10T00:00:00Z", number.to_bytes(2, "little") * 320)


class HandoffCapture(_Capture):
    kind = "formal"
    turn_id = "turn_handoff"
    recovery_required = False

    def __init__(self):
        super().__init__()
        self.current_final = _final("合成答案正文。")
        self.snapshot_entered = asyncio.Event()
        self.release_snapshot = asyncio.Event()
        self.resume_entered = asyncio.Event()
        self.release_resume = asyncio.Event()
        self.send_entered = asyncio.Event()
        self.release_send = asyncio.Event()
        self.audio = []
        self.block_send = False

    async def transcript_snapshot(self, *, resume=True):
        self.snapshot_entered.set()
        await self.release_snapshot.wait()
        return await super().transcript_snapshot(resume=resume)

    async def resume_capture(self):
        self.resume_entered.set()
        await self.release_resume.wait()
        await super().resume_capture()

    async def send_audio(self, pcm):
        self.audio.append(pcm)
        self.send_entered.set()
        if self.block_send:
            await self.release_send.wait()
        return None

    def revoke_audio_input(self):
        self.is_open = False


def setup():
    capture = HandoffCapture()
    endpoint, _, notices, commits = _endpoint(capture=capture)
    confirmation = SpokenSupplementConfirmation(speak=lambda *args: None)
    endpoint.confirmation = confirmation
    confirmation.phase = "speaking"
    confirmation._after_speech = "awaiting_reply"
    confirmation._supplement_delivery_pending = True
    managed = object.__new__(ManagedLiveKitEvidenceSession)
    managed.chain, managed._answer_endpoint = capture, endpoint
    managed._capture_id = "capture_handoff"
    managed._stopped = False
    managed._partial_projection_failure = None
    managed._supplement_preroll = []
    managed.ownership = None
    return managed, endpoint, capture, notices, commits


@pytest.mark.anyio
async def test_post_delivery_audio_keeps_prefix_and_fifo_through_finish_resume_and_flush():
    managed, endpoint, capture, _, commits = setup()
    await managed._receive_audio(frame(9))  # Interviewer echo before delivery.
    capture.block_send = True
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    try:
        await capture.snapshot_entered.wait()
        await managed._receive_audio(frame(1))
        capture.release_snapshot.set()
        await capture.resume_entered.wait()
        await managed._receive_audio(frame(2))
        capture.release_resume.set()
        await asyncio.wait_for(capture.send_entered.wait(), .2)
        await managed._receive_audio(frame(3))
        capture.release_send.set()
        await returned
        await managed._receive_audio(frame(4))
        assert capture.audio == [frame(n).pcm_s16le for n in (1, 2, 3, 4)]
        assert endpoint.confirmation.boundary == "合成答案正文。"
        assert endpoint.confirmation._supplement_question_delivered
        assert not endpoint.confirmation.confirmed and not commits
        assert managed._supplement_preroll == []
    finally:
        returned.cancel()
        await asyncio.gather(returned, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["capture", "chain", "endpoint", "turn", "closed", "stopped"])
async def test_stale_handoff_never_replays_into_a_replacement(change):
    managed, endpoint, capture, _, _ = setup()
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    try:
        await capture.snapshot_entered.wait()
        await managed._receive_audio(frame(1))
        if change == "capture":
            managed._capture_id = "replacement"
        elif change == "chain":
            managed.chain = HandoffCapture()
        elif change == "endpoint":
            managed._answer_endpoint = SimpleNamespace()
        elif change == "turn":
            capture.turn_id = "replacement"
        elif change == "closed":
            capture.is_open = False
        else:
            managed._stopped = True
        capture.release_snapshot.set()
        capture.release_resume.set()
        await returned
        assert capture.audio == []
        assert not getattr(managed, "_reply_audio_handoff", None)
    finally:
        returned.cancel()
        await asyncio.gather(returned, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
async def test_cancelled_live_handoff_preserves_pcm_but_revokes_delivery_permission():
    managed, endpoint, capture, _, _ = setup()
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    await capture.snapshot_entered.wait()
    await managed._receive_audio(frame(1))
    capture.release_resume.set()
    returned.cancel()
    await asyncio.gather(returned, return_exceptions=True)
    try:
        assert capture.audio == [frame(1).pcm_s16le]
        assert not getattr(managed, "_reply_audio_handoff", None)
        assert not endpoint.confirmation._supplement_question_delivered
        await managed._receive_audio(frame(2))
        assert capture.audio == [frame(1).pcm_s16le, frame(2).pcm_s16le]
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_handoff_overflow_fails_closed_instead_of_trimming_first_syllables(monkeypatch):
    monkeypatch.setattr("app.services.livekit_evidence_ingress._REPLY_HANDOFF_MAX_BYTES", 640)
    managed, endpoint, capture, _, _ = setup()
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    try:
        await capture.snapshot_entered.wait()
        await managed._receive_audio(frame(1))
        with pytest.raises(LiveKitAudioIngressBackpressureError):
            await managed._receive_audio(frame(2))
        capture.release_snapshot.set()
        capture.release_resume.set()
        await returned
        assert not endpoint.confirmation._supplement_question_delivered
        assert capture.audio == []
        assert managed._reply_audio_handoff is None
    finally:
        returned.cancel()
        await asyncio.gather(returned, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
async def test_scoped_client_vad_during_boundary_wait_is_applied_after_the_cut():
    from app.domain.interview_agent import ClientSignal
    managed, endpoint, capture, _, _ = setup()
    managed.interview_id, managed.organization_id = "interview", "org_default"
    managed._endpoint_task = None
    emitted, floors = [], []

    async def emit(*args, **kwargs):
        emitted.append(args)

    async def set_floor(*args):
        floors.append(args)

    source = SimpleNamespace(runtime=SimpleNamespace(interviews=SimpleNamespace(
        get_interview=lambda *args: {"current_turn_id": capture.turn_id,
                                    "agent_runtime": {"floor": "agent"}})),
        _emit=emit, _set_floor=set_floor)
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    try:
        await capture.snapshot_entered.wait()
        revision = endpoint.revision
        await managed._apply_speech_started(source, ClientSignal(type="speech.started",
            idempotency_key="handoff_voice", turn_id=capture.turn_id,
            payload={"capture_id": managed._capture_id}))
        assert endpoint.revision == revision
        await managed._receive_audio(frame(1))
        capture.release_snapshot.set()
        capture.release_resume.set()
        await returned
        assert endpoint.revision > revision
        assert endpoint.confirmation._supplement_question_delivered
        assert endpoint.confirmation.boundary == "合成答案正文。"
        assert capture.audio == [frame(1).pcm_s16le]
        assert not floors and emitted[0][0] == "speech.started"
    finally:
        returned.cancel()
        await asyncio.gather(returned, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["controller", "owner", "ended"])
async def test_database_fence_change_rejects_delivery_even_when_local_grant_is_stale(change):
    from app.persistence.provider import persistence_for
    from app.repositories.memory import InMemoryStore
    from app.services.evidence_coordination import EvidenceOwnershipCoordinator
    from test_evidence_ownership import _session, INTERVIEW_ID, ORGANIZATION_ID
    store = InMemoryStore()
    session = _session(store)
    coordinator = EvidenceOwnershipCoordinator(store)
    managed, endpoint, capture, _, _ = setup()
    managed.persistence = persistence_for(store)
    managed.interview_id, managed.organization_id = INTERVIEW_ID, ORGANIZATION_ID
    capture.turn_id = session["current_turn_id"]
    managed.ownership = coordinator.attach_control(interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID, connection_id="original", local_instance_id="instance")
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    try:
        await capture.snapshot_entered.wait()
        await managed._receive_audio(frame(1))
        if change == "controller":
            coordinator.attach_control(interview_id=INTERVIEW_ID, organization_id=ORGANIZATION_ID,
                                       connection_id="replacement", local_instance_id="instance")
        else:
            with managed.persistence.transaction(ORGANIZATION_ID) as transaction:
                repository = transaction.evidence_ownerships if change == "owner" else transaction.interview_sessions
                item = repository.get(managed.ownership.ownership_id if change == "owner" else INTERVIEW_ID)
                item["ownership_epoch" if change == "owner" else "status"] = 2 if change == "owner" else "completed"
                repository.update(item, expected_version=item["version"])
        capture.release_snapshot.set()
        capture.release_resume.set()
        with pytest.raises(ApiError):
            await returned
        assert not endpoint.confirmation._supplement_question_delivered
        assert capture.audio == []
        assert managed._reply_audio_handoff is None
    finally:
        returned.cancel()
        await asyncio.gather(returned, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
async def test_barge_in_retains_only_existing_preroll_once_without_delivery_permission():
    managed, endpoint, capture, _, commits = setup()
    try:
        await managed._receive_audio(frame(1))
        await managed._receive_audio(frame(2))
        await managed.confirmation_floor_returned("barge_in")
        await managed.confirmation_floor_returned("barge_in")
        assert capture.audio == [frame(1).pcm_s16le, frame(2).pcm_s16le]
        assert not endpoint.confirmation._supplement_question_delivered
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_pre_delivery_sink_backlog_cannot_become_new_reply_evidence():
    managed, endpoint, capture, _, _ = setup()
    managed._ingress = SimpleNamespace(accepted_audio_sequence=2)
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    try:
        await capture.snapshot_entered.wait()
        await managed._receive_audio(frame(1))
        await managed._receive_audio(frame(2))
        await managed._receive_audio(frame(3))
        capture.release_snapshot.set()
        capture.release_resume.set()
        await returned
        assert capture.audio == [frame(3).pcm_s16le]
        assert endpoint.confirmation.boundary == "合成答案正文。"
    finally:
        returned.cancel()
        await asyncio.gather(returned, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
async def test_playback_watermark_outlives_a_fast_empty_handoff():
    managed, endpoint, capture, _, _ = setup()
    managed._ingress = SimpleNamespace(accepted_audio_sequence=2)
    capture.release_snapshot.set()
    capture.release_resume.set()
    try:
        await managed.confirmation_floor_returned("agent_finished")
        assert managed._reply_audio_handoff is None
        await managed._receive_audio(frame(1))
        await managed._receive_audio(frame(2))
        assert capture.audio == []
        await managed._receive_audio(frame(3))
        assert capture.audio == [frame(3).pcm_s16le]
        assert managed._reply_audio_watermark is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_cancel_after_delivery_metadata_keeps_pcm_without_confirming_it():
    managed, endpoint, capture, _, commits = setup()
    entered = asyncio.Event()

    async def blocked_recovery():
        entered.set()
        await asyncio.Event().wait()

    endpoint._recovery_stage = "supplement_prompt"
    endpoint._understanding_succeeded = blocked_recovery
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    await capture.snapshot_entered.wait()
    await managed._receive_audio(frame(1))
    capture.release_snapshot.set()
    capture.release_resume.set()
    await entered.wait()
    returned.cancel()
    await asyncio.gather(returned, return_exceptions=True)
    try:
        await managed._receive_audio(frame(2))
        assert capture.audio == [frame(1).pcm_s16le, frame(2).pcm_s16le]
        assert not endpoint.confirmation._supplement_question_delivered
        assert not endpoint.confirmation.confirmed and not commits
        assert managed._reply_audio_handoff is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_cancel_before_prefix_cut_keeps_pcm_and_leaves_speaking_gate():
    managed, endpoint, capture, _, _ = setup()
    handoff = managed.begin_confirmation_handoff()
    try:
        await managed._receive_audio(frame(1))
        await managed.cancel_confirmation_handoff(handoff)
        await managed._receive_audio(frame(2))
        assert capture.audio == [frame(1).pcm_s16le, frame(2).pcm_s16le]
        assert not endpoint.confirmation.speaking
        assert not endpoint.confirmation._supplement_question_delivered
        assert managed._reply_audio_handoff is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_cancel_during_flush_records_inflight_frame_once_and_retains_its_tail():
    managed, endpoint, capture, _, _ = setup()
    capture.block_send = True
    returned = asyncio.create_task(managed.confirmation_floor_returned("agent_finished"))
    await capture.snapshot_entered.wait()
    await managed._receive_audio(frame(1))
    await managed._receive_audio(frame(2))
    capture.release_snapshot.set()
    capture.release_resume.set()
    await capture.send_entered.wait()
    returned.cancel()
    capture.release_send.set()
    await asyncio.gather(returned, return_exceptions=True)
    try:
        assert capture.audio == [frame(1).pcm_s16le, frame(2).pcm_s16le]
        assert not endpoint.confirmation._supplement_question_delivered
        assert managed._reply_audio_handoff is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_cancel_cleanup_failure_revokes_input_and_reports_gap():
    managed, endpoint, capture, _, _ = setup()
    failures = []

    async def failed_send(pcm):
        raise OSError("synthetic capture failure")

    async def fatal(code, exc):
        failures.append(code)

    handoff = managed.begin_confirmation_handoff()
    await managed._receive_audio(frame(1))
    capture.send_audio = failed_send
    managed._fatal_ingress_problem = fatal
    try:
        with pytest.raises(OSError):
            await managed.cancel_confirmation_handoff(handoff)
        assert not capture.is_open
        assert not endpoint.confirmation._supplement_question_delivered
        assert failures == ["LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"]
        assert managed._reply_audio_handoff is None
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_projection", [False, True])
async def test_real_adapter_queue_and_channel_receipt_keep_only_fresh_pcm(
    tmp_path, monkeypatch, cancel_projection,
):
    from app.adapters.livekit_audio_ingress import LiveKitCandidateAudioIngress, LiveKitAudioIngressBinding
    from app.domain.interview_agent import ClientSignal
    from test_answer_endpoint import _Clock, _until
    from test_automatic_turn_integration import _automatic_session, _current, _VOICE
    from test_evidence_owner_recovery_integration import TURN_ID
    from test_livekit_audio_ingress import _FakeFrame
    from test_spoken_supplement_integration import _audio_aligned_transcripts, _synthetic_tts

    body, reply = "合成答案正文。", "没有需要补充的了。"
    _audio_aligned_transcripts(monkeypatch, [body, reply])
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
        _synthetic_tts(runtime)
        endpoint = managed._answer_endpoint
        endpoint.clock = _Clock()
        await managed._ingress.push(_VOICE)
        endpoint._proposal_revision = endpoint.revision
        await endpoint.listening_snapshot()
        await endpoint.confirmation._say(endpoint, "check", afterwards="awaiting_reply")
        performance = _current(runtime)["agent_runtime"]["active_performance_id"]
        assert endpoint.confirmation.speaking
        initial_bytes = managed.chain._stt.stream._received_bytes
        old_ingress = managed._ingress
        queue = asyncio.Queue()
        release_sink, emit_entered, release_emit = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await queue.get()
                if item is None:
                    raise StopAsyncIteration
                return SimpleNamespace(frame=_FakeFrame(item))

            async def aclose(self):
                pass

        async def sink(received):
            if received.sequence == 1:
                await release_sink.wait()
            await managed._receive_audio(received)

        ingress = LiveKitCandidateAudioIngress(managed.media_plane,
            LiveKitAudioIngressBinding("synthetic", "candidate", "subscriber"),
            on_audio_frame=sink, audio_stream_factory=lambda _: Stream())
        ingress._track_sid = "track"
        managed._ingress = ingress
        consumer = asyncio.create_task(ingress._consume_track(object(), "track", None))
        original_emit = channel._emit

        async def slow_emit(event_type, *args, **kwargs):
            if event_type == "avatar.performance.stopped":
                emit_entered.set()
                await release_emit.wait()
            return await original_emit(event_type, *args, **kwargs)

        channel._emit = slow_emit
        stopped = None
        try:
            await queue.put(frame(1).pcm_s16le)
            await queue.put(frame(2).pcm_s16le)
            await _until(lambda: ingress.accepted_audio_sequence == 2)
            stopped = asyncio.create_task(channel.send(ClientSignal(type="avatar.performance.stopped",
                turn_id=TURN_ID, idempotency_key="handoff_slow_emit", payload={"performance_id": performance})))
            await asyncio.wait_for(emit_entered.wait(), 2)
            assert managed._reply_audio_handoff.audio_watermark == 2
            release_sink.set()
            await _until(lambda: ingress._sink_state.delivered_sequence == 2)
            await queue.put(frame(3).pcm_s16le)
            await _until(lambda: ingress._sink_state.delivered_sequence == 3)
            assert managed.chain._stt.stream._received_bytes == initial_bytes
            if cancel_projection:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
            else:
                release_emit.set()
                await stopped
            assert managed.chain._stt.stream._received_bytes == initial_bytes + 640
            assert not endpoint.confirmation.speaking
            assert endpoint.confirmation._supplement_question_delivered is (not cancel_projection)
            assert not endpoint.confirmation.confirmed
            assert managed._reply_audio_handoff is None
            endpoint._proposal_revision = endpoint.revision
            final = await endpoint.listening_snapshot()
            assert final.text == body + reply
            assert not _current(runtime)["answers"]
        finally:
            release_sink.set()
            release_emit.set()
            if stopped is not None:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            managed._ingress = old_ingress
            channel._emit = original_emit
