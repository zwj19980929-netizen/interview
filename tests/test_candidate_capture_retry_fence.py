"""Candidate retry fencing against real transactional repositories.

Only synthetic PCM is stored under pytest's temporary directory. No model,
network, microphone, event-loop primitive, or historical candidate data is used.
"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.file_storage.local import LocalPrivateFileAdapter
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import VersionedDocumentRepository
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.evidence_media import DurableEvidenceMedia


ORGANIZATION_ID = "org_default"
INTERVIEW_ID = "interview_candidate_retry"
TURN_ID = "turn_candidate_retry"
CAPTURE_ID = "capture_candidate_retry"


def _seed_session(store, interview_id=INTERVIEW_ID):
    now = utc_now()
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        transaction.interview_sessions.add({
            "id": interview_id,
            "organization_id": ORGANIZATION_ID,
            "status": "in_progress",
            "current_turn_id": TURN_ID,
            "turns": [{"id": TURN_ID, "status": "asking"}],
            "answers": [],
            "lifecycle_events": [],
            "agent_runtime": {
                "floor": "none",
                "takeover": None,
                "capture_recovery": {
                    "status": "retry_required", "turn_id": TURN_ID,
                    "capture_id": CAPTURE_ID, "capture_revision": 1,
                    "attempt": 3, "max_attempts": 3,
                },
            },
            "created_at": now, "updated_at": now,
        })


def _grant(coordinator, *, interview_id=INTERVIEW_ID, owner="owner_a"):
    return coordinator.attach_control(
        interview_id=interview_id, organization_id=ORGANIZATION_ID,
        connection_id="connection_" + owner, local_instance_id=owner,
    )


def _writer(harness, fence=None):
    return harness.media.open_writer(
        interview_id=INTERVIEW_ID, turn_id=TURN_ID, content_type="audio/pcm",
        sample_rate_hz=16000, channels=1, fence=fence or harness.fence,
    )


@pytest.fixture(params=["memory", "sqlite"])
def retry_harness(request, tmp_path):
    store = (InMemoryStore() if request.param == "memory"
             else SQLiteStore(str(tmp_path / "candidate-retry.sqlite3")))
    _seed_session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=120)
    grant = _grant(coordinator)
    harness = SimpleNamespace(
        store=store, coordinator=coordinator, grant=grant,
        fence=grant.commit_fence(),
        media=DurableEvidenceMedia(
            store, organization_id=ORGANIZATION_ID,
            storage=LocalPrivateFileAdapter(tmp_path / "private"),
            segment_target_bytes=4096,
        ),
    )
    writer = _writer(harness)
    writer.append(b"\x01\x00" * 2048)
    writer.append(b"\x02\x00" * 10)
    assert writer.abort() == 20
    return harness


def _snapshot(harness):
    # Reopen SQLite so assertions also detect writes hidden by a store cache.
    store = (SQLiteStore(str(harness.store.path))
             if isinstance(harness.store, SQLiteStore) else harness.store)
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        return {
            name: sorted(getattr(transaction, name).list(), key=lambda row: row["id"])
            for name in ("interview_sessions", "evidence_media_streams",
                         "evidence_media_segments", "file_objects", "evidence_ownerships")
        }


def _edit(harness, collection, edit):
    with persistence_for(harness.store).transaction(ORGANIZATION_ID) as transaction:
        repository = getattr(transaction, collection)
        value = repository.list()[0]
        edit(value)
        repository.update(value, expected_version=value["version"])


def _retry(harness, **overrides):
    arguments = dict(interview_id=INTERVIEW_ID, turn_id=TURN_ID,
                     capture_id=CAPTURE_ID, fence=harness.fence)
    arguments.update(overrides)
    return harness.media.prepare_candidate_retry(**arguments)


def test_candidate_retry_advances_one_incomplete_capture_and_preserves_segments(retry_harness):
    before = _snapshot(retry_harness)
    assert _retry(retry_harness) is True
    after = _snapshot(retry_harness)
    stream = after["evidence_media_streams"][0]
    assert stream["capture_revision"] == 2
    assert stream["complete"] is False
    assert stream["last_sealed_ordinal"] == 0
    assert stream["last_sealed_frame_sequence"] == 0
    assert stream["sealed_byte_count"] == 0
    assert len(stream["abandoned_captures"]) == 1
    assert stream["abandoned_captures"][0].items() >= {
        "capture_revision": 1, "sealed_segment_count": 1,
        "last_sealed_frame_sequence": 1, "sealed_byte_count": 4096,
        "reason": "candidate_capture_retry",
    }.items()
    for collection in ("evidence_media_segments", "file_objects", "evidence_ownerships"):
        assert after[collection] == before[collection]
    expected_session = deepcopy(before["interview_sessions"][0])
    expected_session["agent_runtime"]["capture_recovery"]["retry_capture_revision"] = 2
    expected_session["version"] += 1
    assert after["interview_sessions"] == [expected_session]


def test_failed_open_and_repeated_retry_do_not_advance_again(retry_harness):
    assert _retry(retry_harness) is True
    prepared = _snapshot(retry_harness)
    for _ in range(3):
        assert _retry(retry_harness) is True
        assert _snapshot(retry_harness) == prepared

    # A provider open can fail after its local writer was opened. Abort the
    # empty writer at that seam, without invoking any STT provider.
    failed_open = _writer(retry_harness)
    assert failed_open.checkpoint.capture_revision == 2
    assert failed_open.abort() == 0
    after_failed_open = _snapshot(retry_harness)
    for _ in range(3):
        assert _retry(retry_harness) is True
        assert _snapshot(retry_harness) == after_failed_open

    replacement = _writer(retry_harness)
    replacement.append(b"\x03\x00" * 2048)
    replacement.seal(complete=True)
    finished = _snapshot(retry_harness)
    assert sorted(row["capture_revision"] for row in finished["evidence_media_segments"]) == [1, 2]
    assert len(finished["evidence_media_streams"][0]["abandoned_captures"]) == 1
    assert finished["interview_sessions"][0]["answers"] == []
    with pytest.raises(ApiError) as error:
        _retry(retry_harness)
    assert error.value.code == "EVIDENCE_MEDIA_ALREADY_COMPLETE"
    assert _snapshot(retry_harness) == finished


def test_concurrent_candidate_retries_advance_once_in_real_transactions(retry_harness):
    second_store = (SQLiteStore(str(retry_harness.store.path))
                    if isinstance(retry_harness.store, SQLiteStore) else retry_harness.store)
    second_media = DurableEvidenceMedia(
        second_store, organization_id=ORGANIZATION_ID,
        storage=retry_harness.media.storage, segment_target_bytes=4096,
    )
    barrier = Barrier(2, timeout=3)

    def retry(media):
        barrier.wait()
        return media.prepare_candidate_retry(
            interview_id=INTERVIEW_ID, turn_id=TURN_ID,
            capture_id=CAPTURE_ID, fence=retry_harness.fence,
        )

    before = _snapshot(retry_harness)
    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [executor.submit(retry, media) for media in (retry_harness.media, second_media)]
        assert [call.result(timeout=5) for call in calls] == [True, True]
    after = _snapshot(retry_harness)
    assert after["evidence_media_streams"][0]["capture_revision"] == 2
    assert len(after["evidence_media_streams"][0]["abandoned_captures"]) == 1
    assert after["interview_sessions"][0]["version"] == before["interview_sessions"][0]["version"] + 1
    assert after["evidence_media_segments"] == before["evidence_media_segments"]


@pytest.mark.parametrize("condition", [
    "completed", "paused", "cancelled", "answered", "takeover",
    "different_current_turn", "recovering", "missing_recovery",
    "different_recovery_turn", "different_recovery_capture",
    "requested_turn", "requested_capture", "requested_interview",
])
def test_candidate_retry_rejects_ineligible_session_without_mutation(retry_harness, condition):
    overrides = {}
    if condition.startswith("requested_"):
        overrides[{"requested_turn": "turn_id", "requested_capture": "capture_id",
                   "requested_interview": "interview_id"}[condition]] = "unrelated_scope"
    else:
        def edit(session):
            state = session["agent_runtime"]
            if condition in {"completed", "paused", "cancelled"}:
                session["status"] = condition
            elif condition == "answered":
                session["answers"] = [{"id": "answer_synthetic", "turn_id": TURN_ID}]
            elif condition == "takeover":
                state["takeover"] = {"status": "active", "actor_id": "interviewer_synthetic"}
            elif condition == "different_current_turn":
                session["current_turn_id"] = "turn_other"
            elif condition == "missing_recovery":
                state.pop("capture_recovery")
            elif condition == "recovering":
                state["capture_recovery"]["status"] = "recovering"
            elif condition == "different_recovery_turn":
                state["capture_recovery"]["turn_id"] = "turn_other"
            elif condition == "different_recovery_capture":
                state["capture_recovery"]["capture_id"] = "capture_other"
        _edit(retry_harness, "interview_sessions", edit)
    before = _snapshot(retry_harness)
    if condition == "requested_interview":
        with pytest.raises(ApiError) as error:
            _retry(retry_harness, **overrides)
        assert error.value.code == "EVIDENCE_OWNER_FENCED"
    else:
        assert _retry(retry_harness, **overrides) is False
    assert _snapshot(retry_harness) == before


@pytest.mark.parametrize("condition,code", [
    ("complete", "EVIDENCE_MEDIA_ALREADY_COMPLETE"),
    ("recovered", "EVIDENCE_MEDIA_ALREADY_COMPLETE"),
    ("revision_changed", "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"),
    ("revision_missing", "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"),
    ("stale_retry_revision", "EVIDENCE_MEDIA_CHECKPOINT_CHANGED"),
])
def test_candidate_retry_requires_incomplete_matching_checkpoint(retry_harness, condition, code):
    if condition == "complete":
        _writer(retry_harness).seal(complete=True)
    elif condition == "recovered":
        _edit(retry_harness, "evidence_media_streams",
              lambda row: row.update(recovered_audio_uri="/private/synthetic-recording"))
    elif condition == "revision_missing":
        _edit(retry_harness, "interview_sessions",
              lambda row: row["agent_runtime"]["capture_recovery"].pop("capture_revision"))
    else:
        if condition == "stale_retry_revision":
            assert _retry(retry_harness) is True
        _edit(retry_harness, "evidence_media_streams",
              lambda row: row.update(capture_revision=row["capture_revision"] + 1))
    before = _snapshot(retry_harness)
    with pytest.raises(ApiError) as error:
        _retry(retry_harness)
    assert error.value.code == code
    assert _snapshot(retry_harness) == before


@pytest.mark.parametrize("collection", ["interview_sessions", "evidence_media_streams"])
def test_candidate_retry_does_not_create_missing_state(retry_harness, collection):
    with persistence_for(retry_harness.store).transaction(ORGANIZATION_ID) as transaction:
        repository = getattr(transaction, collection)
        value = repository.list()[0]
        repository.delete(value["id"], expected_version=value["version"])
    before = _snapshot(retry_harness)
    if collection == "interview_sessions":
        assert _retry(retry_harness) is False
    else:
        with pytest.raises(ApiError) as error:
            _retry(retry_harness)
        assert error.value.code == "EVIDENCE_MEDIA_ALREADY_COMPLETE"
    assert _snapshot(retry_harness) == before


@pytest.mark.parametrize("condition", ["released", "successor", "expired", "wrong_epoch", "wrong_lease", "wrong_owner"])
def test_candidate_retry_rejects_stale_ownership_without_mutation(retry_harness, condition):
    fence = retry_harness.fence
    if condition in {"released", "successor"}:
        assert retry_harness.coordinator.release(retry_harness.grant) is True
        if condition == "successor":
            _grant(retry_harness.coordinator, owner="owner_b")
    elif condition == "expired":
        _edit(retry_harness, "evidence_ownerships",
              lambda row: row.update(lease_expires_at="2000-01-01T00:00:00Z"))
    else:
        changed = {"wrong_epoch": {"ownership_epoch": fence.ownership_epoch + 1},
                   "wrong_lease": {"lease_id": "lease_other"},
                   "wrong_owner": {"owner_instance_id": "owner_other"}}[condition]
        fence = fence.model_copy(update=changed)
    before = _snapshot(retry_harness)
    with pytest.raises(ApiError) as error:
        _retry(retry_harness, fence=fence)
    assert error.value.code == "EVIDENCE_OWNER_FENCED"
    assert _snapshot(retry_harness) == before


def test_candidate_retry_rolls_back_media_advance_if_session_update_conflicts(retry_harness, monkeypatch):
    original = VersionedDocumentRepository.update
    attempted_session_update = []

    def conflict(repository, document, *, expected_version):
        if document["id"] == INTERVIEW_ID:
            attempted_session_update.append(document["agent_runtime"]["capture_recovery"]["retry_capture_revision"])
            raise ConcurrencyConflict("synthetic session conflict after media advance")
        return original(repository, document, expected_version=expected_version)

    before = _snapshot(retry_harness)
    with monkeypatch.context() as scoped:
        scoped.setattr(VersionedDocumentRepository, "update", conflict)
        with pytest.raises(ConcurrencyConflict):
            _retry(retry_harness)
    assert attempted_session_update == [2]
    assert _snapshot(retry_harness) == before
    assert _retry(retry_harness) is True
    assert _snapshot(retry_harness)["evidence_media_streams"][0]["capture_revision"] == 2


def test_candidate_retry_does_not_accept_another_interviews_valid_fence(retry_harness):
    _seed_session(retry_harness.store, interview_id="interview_unrelated")
    unrelated = _grant(retry_harness.coordinator, interview_id="interview_unrelated", owner="owner_unrelated")
    before = _snapshot(retry_harness)
    with pytest.raises(ApiError) as error:
        _retry(retry_harness, fence=unrelated.commit_fence())
    assert error.value.code == "EVIDENCE_OWNER_FENCED"
    assert _snapshot(retry_harness) == before
