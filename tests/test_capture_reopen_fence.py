"""Held provider handshakes cannot resurrect paused or superseded captures."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.model_gateway.errors import ProviderError
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.streaming_stt import StreamingInterviewSTT

from test_continuous_stt import _RawStream, _FaultStream, _validated


_INTERVIEW = "interview_reopen_fence"
_TURN = "turn_reopen_fence"
_PAYLOAD = {"turn_id": _TURN, "content_type": "audio/pcm", "sample_rate_hz": 16000,
            "channels": 1, "_continuous_capture": True}


class _Recording:
    def __init__(self):
        self.aborted = False
        self.chunks = []

    def append(self, chunk):
        assert not self.aborted
        self.chunks.append(chunk)

    def abort(self):
        self.aborted = True

    def seal(self, *, complete):
        return SimpleNamespace(model_dump=lambda **_: {"complete": complete})


def _harness():
    store = InMemoryStore()
    persistence = persistence_for(store)
    now = utc_now()
    with persistence.transaction("org_default") as tx:
        tx.interview_sessions.add({
            "id": _INTERVIEW, "organization_id": "org_default", "candidate_id": "synthetic_candidate",
            "status": "in_progress", "current_turn_id": _TURN,
            "turns": [{"id": _TURN, "status": "asking"}], "answers": [], "settings": {},
            "agent_runtime": {"floor": "candidate"}, "created_at": now, "updated_at": now,
        })
    owner = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    grant = owner.attach_control(interview_id=_INTERVIEW, organization_id="org_default",
                                 connection_id="synthetic_connection", local_instance_id="synthetic_owner")
    fence = grant.commit_fence()
    capture = StreamingInterviewSTT(store, _INTERVIEW, commit_fence=fence,
                                    commit_guard=lambda: owner.assert_current(fence, "org_default"))
    recordings, writers = [], []

    def create(items):
        item = _Recording()
        items.append(item)
        return item

    capture.media = SimpleNamespace(start_recording=lambda *_, **__: create(recordings))
    capture.evidence_media = SimpleNamespace(open_writer=lambda **_: create(writers))
    return capture, persistence, owner, grant, recordings, writers


def _mutate(persistence, owner, grant, change):
    if change == "owner_lost":
        assert owner.release(grant)
        return
    with persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get(_INTERVIEW)
        if change == "paused":
            session["status"] = "paused"
        elif change == "turn_changed":
            session["current_turn_id"] = "next_turn"
        elif change == "answered":
            session["answers"] = [{"id": "synthetic_answer", "turn_id": _TURN}]
        elif change == "takeover":
            session["agent_runtime"]["takeover"] = {"status": "active"}
        elif change == "human_floor":
            session["agent_runtime"]["floor"] = "human"
        elif change == "processing_floor":
            session["agent_runtime"]["floor"] = "none"
        tx.interview_sessions.update(session, expected_version=session["version"])


_CHANGES = ["paused", "turn_changed", "answered", "takeover", "human_floor", "owner_lost"]


@pytest.mark.parametrize("change", _CHANGES)
def test_initial_held_continuous_open_discards_provider_and_recorders_after_fence_change(change):
    async def scenario():
        capture, persistence, owner, grant, recordings, writers = _harness()
        entered, release = asyncio.Event(), asyncio.Event()
        raw = _RawStream("held_initial")

        async def opening(_request):
            entered.set()
            await release.wait()
            return _validated(raw)

        capture.gateway = SimpleNamespace(open_stream=opening)
        task = asyncio.create_task(capture.open(_PAYLOAD))
        await entered.wait()
        _mutate(persistence, owner, grant, change)
        release.set()
        with pytest.raises(ApiError) as raised:
            await task
        assert raised.value.code in {"EVIDENCE_OWNER_FENCED", "INTERVIEW_NOT_IN_PROGRESS", "INTERVIEW_TURN_NOT_ACTIVE"}
        assert raw.aborted and not raw.chunks
        assert recordings[0].aborted and writers[0].aborted
        assert capture.stream is None and capture.recording is None and capture.evidence_media_writer is None

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["resume", "recover"])
@pytest.mark.parametrize("change", _CHANGES)
def test_held_reopen_rejects_superseded_capture_without_replaying_pcm(operation, change):
    async def scenario():
        capture, persistence, owner, grant, recordings, writers = _harness()
        first = (_RawStream("first") if operation == "resume" else
                 _FaultStream("first", send_error=ProviderError("provider_timeout", "Synthetic timeout.", retryable=True)))
        late = _RawStream("late")
        entered, release = asyncio.Event(), asyncio.Event()
        opens = []

        async def opening(_request):
            opens.append(True)
            if len(opens) == 1:
                return _validated(first)
            entered.set()
            await release.wait()
            return _validated(late)

        capture.gateway = SimpleNamespace(open_stream=opening)
        await capture.open(_PAYLOAD)
        await capture.send_audio(b"\x00\x20" * 320)
        if operation == "resume":
            await capture.transcript_snapshot(resume=False)
        task = asyncio.create_task(capture.resume_capture() if operation == "resume" else capture.recover_capture())
        await entered.wait()
        _mutate(persistence, owner, grant, change)
        release.set()
        with pytest.raises(ApiError) as raised:
            await task
        assert raised.value.code in {"EVIDENCE_OWNER_FENCED", "INTERVIEW_NOT_IN_PROGRESS", "INTERVIEW_TURN_NOT_ACTIVE"}
        assert late.aborted and not late.chunks
        assert recordings[0].aborted and writers[0].aborted
        assert len(recordings) == len(writers) == 1
        assert capture.stream is None and capture.recording is None and capture.evidence_media_writer is None

    asyncio.run(scenario())


def test_cancelled_initial_handshake_releases_recorders_and_propagates_cancellation():
    async def scenario():
        capture, _, _, _, recordings, writers = _harness()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def opening(_request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        capture.gateway = SimpleNamespace(open_stream=opening)
        task = asyncio.create_task(capture.open(_PAYLOAD))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and recordings[0].aborted and writers[0].aborted
        assert capture.stream is None and capture.recording is None and capture.evidence_media_writer is None

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_close_releases_both_recorders_even_if_provider_abort_fails_or_is_cancelled(failure):
    async def scenario():
        capture, _, _, _, recordings, writers = _harness()
        entered = asyncio.Event()

        class FailedAbort(_RawStream):
            async def abort(self):
                entered.set()
                if failure == "exception":
                    raise ProviderError("provider_stream_close_failed", "Synthetic abort failure.")
                await asyncio.Future()

        raw = FailedAbort("cannot_close")

        async def opening(_request):
            return _validated(raw)

        capture.gateway = SimpleNamespace(open_stream=opening)
        await capture.open(_PAYLOAD)
        task = asyncio.create_task(capture.close())
        await entered.wait()
        if failure == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ProviderError):
            await task
        assert recordings[0].aborted and writers[0].aborted
        assert capture.stream is None and capture.recording is None and capture.evidence_media_writer is None

    asyncio.run(scenario())


def test_stale_initial_open_cannot_close_a_new_generation_on_same_object():
    async def scenario():
        capture, _, _, _, recordings, writers = _harness()
        entered, release = asyncio.Event(), asyncio.Event()
        late, current = _RawStream("late"), _RawStream("current")
        opens = []

        async def opening(_request):
            opens.append(True)
            if len(opens) == 1:
                entered.set()
                await release.wait()
                return _validated(late)
            return _validated(current)

        capture.gateway = SimpleNamespace(open_stream=opening)
        old_task = asyncio.create_task(capture.open(_PAYLOAD))
        await entered.wait()
        await capture.close()
        await capture.open(_PAYLOAD)
        release.set()
        with pytest.raises(ApiError):
            await old_task
        assert late.aborted and not current.aborted
        assert recordings[0].aborted and writers[0].aborted
        assert not recordings[1].aborted and not writers[1].aborted
        await capture.send_audio(b"\x00\x20")
        assert current.chunks == [b"\x00\x20"]
        await capture.close()

    asyncio.run(scenario())


def test_reopen_allows_nonhuman_none_floor_and_audio_frames_do_not_read_session(monkeypatch):
    async def scenario():
        capture, persistence, owner, grant, _, _ = _harness()
        streams = iter([_RawStream("first"), _RawStream("next")])

        async def opening(_request):
            return _validated(next(streams))

        capture.gateway = SimpleNamespace(open_stream=opening)
        await capture.open(_PAYLOAD)
        await capture.send_audio(b"\x00\x20")
        await capture.transcript_snapshot(resume=False)
        _mutate(persistence, owner, grant, "processing_floor")
        await capture.resume_capture()

        def forbidden_read(*_args, **_kwargs):
            raise AssertionError("PCM input must not perform a session DB read per frame")

        monkeypatch.setattr(capture.interviews, "get_interview", forbidden_read)
        for _ in range(30):
            await capture.send_audio(b"\x00\x20")
        await capture.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["paused", "owner_lost"])
def test_optional_dialogue_handshake_cannot_publish_ready_after_capture_fence_changes(change):
    async def scenario():
        capture, persistence, owner, grant, recordings, writers = _harness()
        with persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get(_INTERVIEW)
            session["settings"]["speech_dialogue_mode"] = "s2s"
            tx.interview_sessions.update(session, expected_version=session["version"])
        raw = _RawStream("stt")
        dialogue = _RawStream("dialogue")
        entered, release = asyncio.Event(), asyncio.Event()

        async def opening(_request):
            return _validated(raw)

        async def dialogue_opening(_request):
            entered.set()
            await release.wait()
            return dialogue

        capture.gateway = SimpleNamespace(open_stream=opening, open_speech_dialogue=dialogue_opening)
        task = asyncio.create_task(capture.open(_PAYLOAD))
        await entered.wait()
        _mutate(persistence, owner, grant, change)
        release.set()
        with pytest.raises(ApiError):
            await task
        assert raw.aborted and dialogue.aborted
        assert recordings[0].aborted and writers[0].aborted
        assert capture.stream is None and capture.dialogue is None

    asyncio.run(scenario())


@pytest.mark.parametrize("continuous", [False, True])
def test_close_after_answered_or_paused_never_applies_open_only_domain_guard(continuous):
    async def scenario():
        capture, persistence, owner, grant, recordings, writers = _harness()
        raw = _RawStream("stt")

        async def opening(_request):
            return _validated(raw)

        capture.gateway = SimpleNamespace(open_stream=opening)
        await capture.open({**_PAYLOAD, "_continuous_capture": continuous})
        _mutate(persistence, owner, grant, "answered")
        _mutate(persistence, owner, grant, "paused")
        await capture.close()
        assert raw.aborted and recordings[0].aborted and writers[0].aborted

    asyncio.run(scenario())
