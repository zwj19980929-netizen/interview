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
from app.services.streaming_stt import StreamingInterviewSTT


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
