import asyncio

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.interview_agent import TurnUnderstanding
from app.file_storage.local import LocalPrivateFileAdapter
from app.file_storage.provider import reset_private_file_storage_for_tests
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evidence_coordination import (
    EvidenceOwnershipCoordinator,
    EvidenceOwnershipLost,
)
from app.services.evidence_media import DurableEvidenceMedia
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.streaming_stt import StreamingInterviewSTT
from app.model_gateway.errors import ProviderError


ORGANIZATION_ID = "org_default"
INTERVIEW_ID = "interview_media_repair"
TURN_ID = "turn_media_repair"


def _session(store: InMemoryStore) -> None:
    now = utc_now()
    turn = {
        "id": TURN_ID,
        "interview_id": INTERVIEW_ID,
        "question_id": "question_media_repair",
        "question_snapshot_id": "snapshot_media_repair",
        "question_snapshot": {
            "id": "snapshot_media_repair",
            "question_text": "请说明断线恢复设计。",
            "standard_answer": "使用持久检查点和幂等提交。",
            "key_points": [],
            "rubric": {},
        },
        "order": 1,
        "phase": "position_bank",
        "status": "asking",
        "is_followup": False,
        "root_turn_id": TURN_ID,
        "followup_depth": 0,
        "allow_followup": False,
        "weight": 1.0,
        "question_spoken_text": "请说明断线恢复设计。",
        "utterances": [],
        "current_understanding": None,
        "conversation_acts": [],
        "started_at": now,
        "completed_at": None,
    }
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        transaction.interview_sessions.add(
            {
                "id": INTERVIEW_ID,
                "organization_id": ORGANIZATION_ID,
                "status": "in_progress",
                "phase": "position_bank",
                "current_turn_id": TURN_ID,
                "turn_ids": [TURN_ID],
                "turns": [turn],
                "answers": [],
                "evaluation_revisions": [],
                "report_revisions": [],
                "current_report_id": None,
                "report_id": None,
                "followup_policy": {
                    "max_depth": 2,
                    "max_total": 0,
                    "max_per_root": 0,
                },
                "lifecycle_events": [],
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
            }
        )


def _grant(
    coordinator: EvidenceOwnershipCoordinator,
    *,
    instance: str,
    connection: str,
):
    return coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id=connection,
        local_instance_id=instance,
    )


def _media(store, storage, *, segment_bytes=4096):
    return DurableEvidenceMedia(
        store,
        organization_id=ORGANIZATION_ID,
        storage=storage,
        segment_target_bytes=segment_bytes,
    )


def test_unsealed_suffix_is_never_reported_as_recoverable(tmp_path) -> None:
    store = InMemoryStore()
    _session(store)
    storage = LocalPrivateFileAdapter(tmp_path / "private")
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = _grant(coordinator, instance="owner_a", connection="connection_a")
    writer = _media(store, storage).open_writer(
        interview_id=INTERVIEW_ID,
        turn_id=TURN_ID,
        content_type="audio/pcm",
        sample_rate_hz=48000,
        channels=1,
        fence=old.commit_fence(),
    )
    writer.append(b"not-yet-sealed")

    assert coordinator.release(old) is True
    assert writer.abort() == len(b"not-yet-sealed")
    successor = _grant(
        coordinator, instance="owner_b", connection="connection_b"
    )

    with pytest.raises(ApiError) as error:
        _media(store, storage).recover(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            fence=successor.commit_fence(),
        )
    assert error.value.code == "EVIDENCE_MEDIA_NOT_RECOVERABLE"
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        stream = transaction.evidence_media_streams.list()[0]
        assert stream["sealed_byte_count"] == 0
        assert transaction.evidence_media_segments.list() == []


def test_successor_rejects_contiguous_but_incomplete_sealed_prefix(
    tmp_path,
) -> None:
    store = InMemoryStore()
    _session(store)
    storage = LocalPrivateFileAdapter(tmp_path / "private")
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = _grant(coordinator, instance="owner_a", connection="connection_a")
    media = _media(store, storage)
    writer = media.open_writer(
        interview_id=INTERVIEW_ID,
        turn_id=TURN_ID,
        content_type="audio/pcm",
        sample_rate_hz=48000,
        channels=1,
        fence=old.commit_fence(),
    )
    sealed = b"\x01\x02" * 2048
    unsealed = b"\x03\x04" * 50
    checkpoint = writer.append(sealed)
    writer.append(unsealed)
    assert checkpoint.recoverability == "sealed_prefix"
    assert checkpoint.last_sealed_frame_sequence == 1

    assert coordinator.release(old) is True
    assert writer.abort() == len(unsealed)
    successor = _grant(
        coordinator, instance="owner_b", connection="connection_b"
    )
    with pytest.raises(ApiError) as error:
        _media(store, storage).recover(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            fence=successor.commit_fence(),
        )
    assert error.value.code == "EVIDENCE_MEDIA_INCOMPLETE"

    # A contiguous prefix proves which bytes are durable, not that the
    # candidate intended to finish. It must never be promoted to an answer.
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        stream = transaction.evidence_media_streams.list()[0]
        assert stream["complete"] is False
        assert stream["recovered_audio_uri"] is None
        assert transaction.interview_sessions.get(INTERVIEW_ID)["answers"] == []


def test_old_owner_cannot_seal_after_epoch_changes(tmp_path) -> None:
    store = InMemoryStore()
    _session(store)
    storage = LocalPrivateFileAdapter(tmp_path / "private")
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = _grant(coordinator, instance="owner_a", connection="connection_a")
    writer = _media(store, storage).open_writer(
        interview_id=INTERVIEW_ID,
        turn_id=TURN_ID,
        content_type="audio/pcm",
        sample_rate_hz=48000,
        channels=1,
        fence=old.commit_fence(),
    )
    writer.append(b"old-owner-suffix")
    assert coordinator.release(old) is True
    _grant(coordinator, instance="owner_b", connection="connection_b")

    with pytest.raises(EvidenceOwnershipLost):
        writer.seal()
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        assert transaction.evidence_media_segments.list() == []


def test_recovery_rejects_tampered_sealed_segment(tmp_path) -> None:
    store = InMemoryStore()
    _session(store)
    storage = LocalPrivateFileAdapter(tmp_path / "private")
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = _grant(coordinator, instance="owner_a", connection="connection_a")
    writer = _media(store, storage).open_writer(
        interview_id=INTERVIEW_ID,
        turn_id=TURN_ID,
        content_type="audio/pcm",
        sample_rate_hz=48000,
        channels=1,
        fence=old.commit_fence(),
    )
    writer.append(b"\x00\x00" * 2048)
    writer.seal(complete=True)
    assert coordinator.release(old) is True
    successor = _grant(
        coordinator, instance="owner_b", connection="connection_b"
    )
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        segment = transaction.evidence_media_segments.list()[0]
        file_object = transaction.file_objects.get(segment["file_id"])
    object_path = storage.root / file_object["object_key"]
    object_path.write_bytes(b"tampered")

    with pytest.raises(ApiError) as error:
        _media(store, storage).recover(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            fence=successor.commit_fence(),
        )
    assert error.value.code == "EVIDENCE_MEDIA_CHECKSUM_MISMATCH"


def test_incomplete_sealed_prefix_never_reaches_batch_stt_or_answer(
    tmp_path,
) -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        _session(store)
        storage = LocalPrivateFileAdapter(tmp_path / "private")
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
        old = _grant(
            coordinator, instance="owner_a", connection="connection_a"
        )
        media = _media(store, storage)
        writer = media.open_writer(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            content_type="audio/pcm",
            sample_rate_hz=48000,
            channels=1,
            fence=old.commit_fence(),
        )
        writer.append(b"\x00\x00" * 2048)
        assert coordinator.release(old) is True
        successor = _grant(
            coordinator, instance="owner_b", connection="connection_b"
        )
        stream = StreamingInterviewSTT(
            store,
            INTERVIEW_ID,
            organization_id=ORGANIZATION_ID,
            commit_fence=successor.commit_fence(),
        )
        stream.evidence_media = media

        with pytest.raises(ApiError) as error:
            await stream.recover_persisted(turn_id=TURN_ID)
        assert error.value.code == "EVIDENCE_MEDIA_INCOMPLETE"
        current = stream.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert current["answers"] == []
        assert current["turns"][0]["status"] == "asking"

    asyncio.run(scenario())


def test_owner_loss_batch_repair_creates_one_answer_from_persisted_audio(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "global-private")
        )
        reset_private_file_storage_for_tests()
        store = InMemoryStore()
        _session(store)
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
        old = _grant(
            coordinator, instance="owner_a", connection="connection_a"
        )
        media = DurableEvidenceMedia(
            store,
            organization_id=ORGANIZATION_ID,
            segment_target_bytes=4096,
        )
        writer = media.open_writer(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            content_type="audio/pcm",
            sample_rate_hz=48000,
            channels=1,
            fence=old.commit_fence(),
        )
        writer.append(b"\x00\x00" * 2048)
        checkpoint = writer.seal(complete=True)
        assert checkpoint.recoverability == "complete"

        # Simulate an old owner that persisted transcription.started, then died
        # before Provider final and CandidateAnswer commit.
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            session = transaction.interview_sessions.get(INTERVIEW_ID)
            expected_version = session["version"]
            session["turns"][0]["status"] = "transcribing"
            transaction.interview_sessions.update(
                session, expected_version=expected_version
            )
        assert coordinator.release(old) is True
        successor = _grant(
            coordinator, instance="owner_b", connection="connection_b"
        )
        stream = StreamingInterviewSTT(
            store,
            INTERVIEW_ID,
            organization_id=ORGANIZATION_ID,
            commit_fence=successor.commit_fence(),
            commit_guard=lambda: coordinator.assert_current(
                successor.commit_fence(), ORGANIZATION_ID
            ),
        )

        async def understand(utterance, _turn, _interview):
            return TurnUnderstanding(
                understanding_id="understanding_media_repair",
                revision=1,
                prompt_version="interview_turn_understanding.v1",
                utterance_id=utterance.utterance_id,
                intent="answer",
                answer_summary="候选人说明了持久检查点。",
                claims=[],
                evidence_quotes=["持久检查点"],
                covered_capability_points=[],
                missing_capability_points=[],
                ambiguities=[],
                contradictions=[],
                confidence=0.95,
                suggested_action="next",
                provider={"provider_id": "test", "model": "test"},
                created_at=utc_now(),
            )

        async def no_followup(*_args, **_kwargs):
            return {"selected": False, "reason": "disabled"}

        stream.interviews.conversation.understand = understand
        stream.interviews.conversation.select_followup = no_followup
        events = await stream.recover_persisted(
            turn_id=TURN_ID,
            development_transcript="我使用持久检查点和幂等键恢复任务。",
        )
        assert any(item["type"] == "answer.accepted" for item in events)

        current = stream.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert len(current["answers"]) == 1
        answer = current["answers"][0]
        assert answer["transcript_source"] == "server_batch"
        assert answer["audio_uri"].startswith("private-file://")
        assert answer["media_evidence"] == {
            "mode": "sealed_segments",
            "complete": True,
            "stream_id": answer["media_evidence"]["stream_id"],
            "capture_revision": 1,
            "sealed_segment_count": 1,
            "last_sealed_frame_sequence": 1,
            "ownership_epoch": successor.ownership_epoch,
        }

        # A repeated recovery can reuse the private recording, but the domain
        # lifecycle refuses a second CandidateAnswer for the same turn.
        with pytest.raises(ApiError):
            await stream.recover_persisted(
                turn_id=TURN_ID,
                development_transcript="重复恢复不得创建第二个答案。",
            )
        current = stream.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert len(current["answers"]) == 1
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())


def test_sqlite_checkpoint_survives_store_reopen(tmp_path) -> None:
    database_path = tmp_path / "evidence-media.sqlite3"
    storage = LocalPrivateFileAdapter(tmp_path / "private")
    store = SQLiteStore(str(database_path))
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = _grant(coordinator, instance="owner_a", connection="connection_a")
    writer = _media(store, storage).open_writer(
        interview_id=INTERVIEW_ID,
        turn_id=TURN_ID,
        content_type="audio/pcm",
        sample_rate_hz=16000,
        channels=1,
        fence=old.commit_fence(),
    )
    writer.append(b"\x10\x00" * 2048)
    writer.seal(complete=True)
    assert coordinator.release(old) is True

    reopened = SQLiteStore(str(database_path))
    successor = _grant(
        EvidenceOwnershipCoordinator(reopened, lease_seconds=30),
        instance="owner_b",
        connection="connection_b",
    )
    recovered = _media(reopened, storage).recover(
        interview_id=INTERVIEW_ID,
        turn_id=TURN_ID,
        fence=successor.commit_fence(),
    )

    assert recovered.complete is True
    assert recovered.sample_rate_hz == 16000
    assert recovered.source_pcm_byte_count == 4096


@pytest.fixture(params=["memory", "sqlite"])
def recapture_store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path / "media"))
    reset_private_file_storage_for_tests()
    store = InMemoryStore() if request.param == "memory" else SQLiteStore(str(tmp_path / "recapture.sqlite3"))
    _session(store)

    async def understand(_self, utterance, _turn, _session):
        rejected = utterance.text == "请再说一次"
        return TurnUnderstanding(
            understanding_id="understanding_" + utterance.utterance_id,
            revision=1, prompt_version="interview_turn_understanding.v1",
            utterance_id=utterance.utterance_id, intent="answer",
            answer_summary=utterance.text, claims=[], evidence_quotes=[utterance.text],
            covered_capability_points=[], missing_capability_points=[],
            ambiguities=[], contradictions=[], confidence=0.3 if rejected else 0.95,
            suggested_action="clarify" if rejected else "next",
            provider={"provider_id": "test", "model": "test"}, created_at=utc_now(),
        )

    async def no_followup(*_args, **_kwargs):
        return {"selected": False, "reason": "disabled"}

    monkeypatch.setattr(ConversationUnderstandingService, "understand", understand)
    monkeypatch.setattr(ConversationUnderstandingService, "select_followup", no_followup)
    yield store
    reset_private_file_storage_for_tests()


def _open_writer(media, fence):
    return media.open_writer(
        interview_id=INTERVIEW_ID, turn_id=TURN_ID,
        content_type="audio/pcm", sample_rate_hz=16000, channels=1, fence=fence,
    )


async def _open_stt(store, fence, transcript):
    stt = StreamingInterviewSTT(store, INTERVIEW_ID, commit_fence=fence)
    await stt.open({
        "turn_id": TURN_ID, "content_type": "audio/pcm",
        "sample_rate_hz": 16000, "channels": 1, "development_transcript": transcript,
    })
    return stt


@pytest.mark.parametrize("finish_mode", ["streaming", "disconnect", "persisted"])
def test_clarification_reopens_same_turn_without_mixing_recordings(recapture_store, finish_mode):
    async def scenario():
        store = recapture_store
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
        grant = _grant(coordinator, instance="owner", connection="connection")
        fence = grant.commit_fence()
        first = await _open_stt(store, fence, "请再说一次")
        await first.send_audio(b"\x10\x00" * 2048)
        if finish_mode == "streaming":
            events = await first.finish({})
        elif finish_mode == "disconnect":
            events = await first.recover_disconnect()
        else:
            first.evidence_media_writer.seal(complete=True)
            await first.close(repair_disconnect=False)
            first = StreamingInterviewSTT(store, INTERVIEW_ID, commit_fence=fence)
            events = await first.recover_persisted(turn_id=TURN_ID, development_transcript="请再说一次")
        assert any(event["type"] == "utterance.not_accepted" for event in events)
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            stream = transaction.evidence_media_streams.list()[0]
            session = transaction.interview_sessions.get(INTERVIEW_ID)
            assert session["answers"] == []
            assert session["turns"][0]["status"] == "asking"
            first_utterance = session["turns"][0]["utterances"][0]
            assert stream["capture_revision"] == 2
            assert stream["complete"] is False
            assert stream["sealed_byte_count"] == 0
            assert stream["recovered_audio_uri"] is None
            assert stream["abandoned_captures"][0]["rejected_utterance_id"] == first_utterance["utterance_id"]
            assert stream["abandoned_captures"][0]["sealed_byte_count"] == 4096
            assert len(transaction.evidence_media_segments.list()) == 1

        # A new process-local chain sees the committed revision; no UI reset or
        # in-memory flag is required. Only the second recording forms an answer.
        second = await _open_stt(store, fence, "使用持久检查点和幂等键恢复任务。")
        await second.send_audio(b"\x20\x00" * 2048)
        events = await second.finish({})
        assert any(event["type"] == "answer.accepted" for event in events)
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            session = transaction.interview_sessions.get(INTERVIEW_ID)
            assert len(session["answers"]) == 1
            answer = session["answers"][0]
            assert answer["media_evidence"]["capture_revision"] == 2
            assert answer["audio_uri"] != first_utterance["audio_uri"]
            assert len(session["turns"][0]["utterances"]) == 2
            segments = sorted(transaction.evidence_media_segments.list(), key=lambda item: item["capture_revision"])
            assert [item["capture_revision"] for item in segments] == [1, 2]
            assert [item["first_frame_sequence"] for item in segments] == [1, 1]
            second_file = transaction.file_objects.get(segments[1]["file_id"])
        assert second.evidence_media.storage.open(second_file["object_key"]) == b"\x20\x00" * 2048
        # Neither an accepted answer nor an undecided complete capture may be
        # reopened through the general writer/reset interfaces.
        with pytest.raises(ApiError, match="already complete"):
            _open_writer(second.evidence_media, fence)
        with pytest.raises(ApiError, match="cannot be reset"):
            second.evidence_media.reset_incomplete(
                interview_id=INTERVIEW_ID, turn_id=TURN_ID, fence=fence, reason="retry",
            )

    asyncio.run(scenario())


def test_nonanswer_and_capture_advance_roll_back_together(recapture_store, monkeypatch):
    async def scenario():
        store = recapture_store
        grant = _grant(EvidenceOwnershipCoordinator(store, lease_seconds=30), instance="owner", connection="connection")
        stt = await _open_stt(store, grant.commit_fence(), "请再说一次")
        await stt.send_audio(b"\x10\x00" * 2048)
        advance = DurableEvidenceMedia._advance_capture

        def failing_advance(*args, **kwargs):
            advance(*args, **kwargs)
            raise RuntimeError("injected failure after capture advance")

        monkeypatch.setattr(DurableEvidenceMedia, "_advance_capture", failing_advance)
        with pytest.raises(RuntimeError, match="injected failure"):
            await stt.finish({})
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            stream = transaction.evidence_media_streams.list()[0]
            session = transaction.interview_sessions.get(INTERVIEW_ID)
            assert stream["capture_revision"] == 1
            assert stream["complete"] is True
            assert not stream.get("abandoned_captures")
            assert session["turns"][0]["status"] == "transcribing"
            assert session["turns"][0]["utterances"] == []
            assert not any(event["type"] == "utterance.not_accepted" for event in session["lifecycle_events"])
        await stt.close(repair_disconnect=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("buffered", [False, True])
def test_old_writer_cannot_modify_new_capture_with_same_owner(recapture_store, buffered):
    store = recapture_store
    grant = _grant(EvidenceOwnershipCoordinator(store, lease_seconds=30), instance="owner", connection="connection")
    media = DurableEvidenceMedia(store, segment_target_bytes=4096)
    old = _open_writer(media, grant.commit_fence())
    old.append(b"\x10\x00" * 2048)
    if buffered:
        old.append(b"\x11\x00" * 8)
    media.reset_incomplete(interview_id=INTERVIEW_ID, turn_id=TURN_ID, fence=grant.commit_fence(), reason="retry")
    current = _open_writer(media, grant.commit_fence())
    current.append(b"\x20\x00" * 2048)
    with pytest.raises(ApiError) as error:
        old.seal()
    assert error.value.code == "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"
    assert current.seal().capture_revision == 2
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        assert len(transaction.evidence_media_segments.list()) == 2


def test_recovery_cannot_publish_old_bytes_after_same_sized_recapture(recapture_store, monkeypatch):
    async def scenario():
        store = recapture_store
        grant = _grant(EvidenceOwnershipCoordinator(store, lease_seconds=30), instance="owner", connection="connection")
        fence = grant.commit_fence()
        stt = await _open_stt(store, fence, "请再说一次")
        await stt.send_audio(b"\x10\x00" * 2048)
        stt.evidence_media_writer.seal(complete=True)
        # Simulate a committed rejection and same-sized new capture while a
        # recovery reads the old object outside its database transaction.
        media = stt.evidence_media
        original_open = media.storage.open

        def interleaved_open(key):
            monkeypatch.setattr(media.storage, "open", original_open)
            # Event-loop execution is unnecessary for the simulated storage
            # race: copy the authoritative non-answer transaction's outcome.
            with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
                stream = transaction.evidence_media_streams.list()[0]
                media._advance_capture(transaction, stream, fence, reason="utterance_not_accepted", rejected_utterance_id="rejected")
            writer = _open_writer(media, fence)
            writer.append(b"\x20\x00" * 2048)
            writer.seal()
            return original_open(key)

        monkeypatch.setattr(media.storage, "open", interleaved_open)
        with pytest.raises(ApiError) as error:
            media.recover(interview_id=INTERVIEW_ID, turn_id=TURN_ID, fence=fence)
        assert error.value.code == "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            stream = transaction.evidence_media_streams.list()[0]
            assert stream["capture_revision"] == 2
            assert stream["recovered_audio_uri"] is None
            assert not any(item["purpose"] == "candidate_answer_audio" for item in transaction.file_objects.list())
        await stt.close(repair_disconnect=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("suggested_action", ["next", "clarify"])
def test_late_semantic_result_cannot_commit_into_new_capture(recapture_store, monkeypatch, suggested_action):
    async def scenario():
        store = recapture_store
        grant = _grant(EvidenceOwnershipCoordinator(store, lease_seconds=30), instance="owner", connection="connection")
        fence = grant.commit_fence()
        first = await _open_stt(store, fence, "请再说一次")
        await first.send_audio(b"\x10\x00" * 2048)
        await first.finish({})
        session = first.interviews.get_interview(INTERVIEW_ID)
        utterance = session["turns"][0]["utterances"][0]
        second = await _open_stt(store, fence, "使用持久检查点。")
        await second.send_audio(b"\x20\x00" * 2048)
        second.evidence_media_writer.seal(complete=True)
        old_payload = {
            "turn_id": TURN_ID, "audio_uri": utterance["audio_uri"],
            "final_transcript": "迟到结果", "provider": {"provider_id": "test"},
            "media_evidence": first._media_evidence_payload(complete=True),
        }
        # Even transcription.started must be fenced; otherwise an old final
        # changes the new attempt's status and recording before commit fails.
        with pytest.raises(ApiError) as error:
            await first.interviews.submit_streaming_answer(INTERVIEW_ID, old_payload, evidence_fence=fence)
        assert error.value.code == "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"
        assert first.interviews.get_interview(INTERVIEW_ID) == session

        # Simulate a semantic call that was already awaiting when the other
        # result advanced the capture. Its final transaction must also fail.
        understanding = session["turns"][0]["current_understanding"]

        async def late_understand(_utterance, _turn, _session):
            return TurnUnderstanding(**dict(understanding, suggested_action=suggested_action))

        monkeypatch.setattr(first.interviews.conversation, "understand", late_understand)
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            current = transaction.interview_sessions.get(INTERVIEW_ID)
            current["turns"][0]["status"] = "transcribing"
            transaction.interview_sessions.update(current, expected_version=current["version"])
        before = first.interviews.get_interview(INTERVIEW_ID)
        with pytest.raises(ApiError) as error:
            await first.interviews._accept_authoritative_transcript(
                INTERVIEW_ID, dict(old_payload, transcript_source="server_streaming", stt_provider={"provider_id": "test"}),
                evidence_fence=fence,
            )
        assert error.value.code == "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"
        assert first.interviews.get_interview(INTERVIEW_ID) == before
        await second.close(repair_disconnect=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["stream", "batch", "empty_completion"])
def test_missing_transcript_keeps_audio_and_reopens_without_semantics(recapture_store, monkeypatch, source):
    async def scenario():
        store = recapture_store
        grant = _grant(EvidenceOwnershipCoordinator(store, lease_seconds=30), instance="owner", connection="connection")
        fence = grant.commit_fence()
        stt = await _open_stt(store, fence, "fixture")
        await stt.send_audio(b"\x00\x00" * 2048)

        async def missing(*args, **kwargs):
            raise ProviderError("provider_final_transcript_missing", "No transcript", retryable=True)

        async def forbidden(*args, **kwargs):
            pytest.fail("No transcript must not be sent to semantic understanding or mock repair")

        monkeypatch.setattr(stt.interviews.conversation, "understand", forbidden)
        if source in {"stream", "empty_completion"}:
            async def empty_completion():
                from app.model_gateway.schemas import StreamingSTTEvent, ProviderMeta
                return [StreamingSTTEvent(stream_id=stt.stream.stream_id, sequence=999,
                    type="transcript.empty", is_final=True,
                    provider=ProviderMeta(provider_id="synthetic", model="test", request_id="empty", latency_ms=0))]
            monkeypatch.setattr(stt.stream, "finish", missing if source == "stream" else empty_completion)
            monkeypatch.setattr(stt.interviews.gateway, "invoke", forbidden)
            events = await stt.finish({})
        else:
            monkeypatch.setattr(stt.interviews.gateway, "invoke", missing)
            events = await stt.recover_disconnect()
        assert any(event.get("problem", {}).get("code") == "STT_TRANSCRIPT_UNAVAILABLE" for event in events)
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            session = transaction.interview_sessions.get(INTERVIEW_ID)
            stream = transaction.evidence_media_streams.list()[0]
            assert session["status"] == "in_progress"
            assert session["answers"] == []
            assert session["turns"][0]["utterances"] == []
            assert session["turns"][0]["status"] == "asking"
            assert stream["capture_revision"] == 2
            assert stream["abandoned_captures"][0]["reason"] == "transcript_unavailable"
            assert stream["abandoned_captures"][0]["untranscribed_audio_uri"] == session["turns"][0]["recording"]["audio_uri"]
            assert len(transaction.evidence_media_segments.list()) == 1
        replacement = await _open_stt(store, fence, "使用持久检查点和幂等键恢复任务。")
        await replacement.send_audio(b"\x20\x00" * 2048)
        await replacement.finish({})
        assert len(replacement.interviews.get_interview(INTERVIEW_ID)["answers"]) == 1

    asyncio.run(scenario())


def test_completed_capture_cannot_be_released_without_authoritative_rejection(recapture_store):
    store = recapture_store
    grant = _grant(EvidenceOwnershipCoordinator(store, lease_seconds=30), instance="owner", connection="connection")
    media = DurableEvidenceMedia(store)
    writer = _open_writer(media, grant.commit_fence())
    writer.append(b"\x10\x00" * 2048)
    checkpoint = writer.seal().model_dump(mode="json")
    with pytest.raises(ApiError) as error:
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            media.release_rejected_capture(
                transaction, interview_id=INTERVIEW_ID, turn_id=TURN_ID,
                media_evidence=dict(checkpoint, mode="sealed_segments", complete=True),
                utterance_id="not_a_real_rejection", fence=grant.commit_fence(),
            )
    assert error.value.code == "EVIDENCE_MEDIA_REJECTION_REQUIRED"
