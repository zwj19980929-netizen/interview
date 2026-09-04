from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.evidence_coordination import (
    EvidenceCommandOutcome,
    EvidenceCommandSubmission,
)
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evidence_command_journal import EvidenceCommandJournal
from app.services.evidence_coordination import (
    EvidenceOwnershipCoordinator,
    EvidenceOwnershipLost,
)


INTERVIEW_ID = "interview_evidence_commands"
ORGANIZATION_ID = "org_default"


def _session(store: InMemoryStore) -> None:
    now = utc_now()
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        transaction.interview_sessions.add(
            {
                "id": INTERVIEW_ID,
                "organization_id": ORGANIZATION_ID,
                "status": "in_progress",
                "phase": "position_bank",
                "current_turn_id": "turn_1",
                "turns": [],
                "answers": [],
                "lifecycle_events": [],
                "created_at": now,
                "updated_at": now,
            }
        )


def _grant(store: InMemoryStore, *, instance_id: str = "instance_a"):
    return EvidenceOwnershipCoordinator(store, lease_seconds=30).attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_1",
        local_instance_id=instance_id,
    )


def _submission(
    key: str = "open_command_1",
) -> EvidenceCommandSubmission:
    return EvidenceCommandSubmission(
        command_type="evidence.open",
        idempotency_key=key,
        turn_id="turn_1",
        causation_id="cause_1",
        payload={
            "content_type": "audio/pcm",
            "sample_rate_hz": 16_000,
            "channels": 1,
            "language": "zh-CN",
            "enable_partial": True,
        },
    )


def test_concurrent_same_key_submission_is_one_privacy_minimized_command() -> None:
    store = InMemoryStore()
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda _index: journal.submit(grant, _submission()), range(2))
        )

    assert receipts[0].command_id == receipts[1].command_id
    assert sorted(item.disposition for item in receipts) == [
        "accepted",
        "duplicate",
    ]
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        commands = transaction.evidence_commands.list()
    assert len(commands) == 1
    serialized = json.dumps(commands[0], ensure_ascii=False)
    assert "open_command_1" not in serialized
    assert "idempotency_hash" in commands[0]
    assert commands[0]["payload"] == {
        "content_type": "audio/pcm",
        "sample_rate_hz": 16_000,
        "channels": 1,
        "language": "zh-CN",
        "enable_partial": True,
    }


def test_payload_allow_list_blocks_audio_transcript_token_and_conflicting_retry() -> None:
    store = InMemoryStore()
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)

    for field, value in (
        ("audio", b"private-pcm"),
        ("transcript", "private transcript"),
        ("token", "secret bearer"),
        ("participant_identity", "candidate:private"),
    ):
        with pytest.raises(ApiError) as forbidden:
            journal.submit(
                grant,
                EvidenceCommandSubmission(
                    command_type="evidence.open",
                    idempotency_key="forbidden_%s" % field,
                    payload={field: value},
                ),
            )
        assert forbidden.value.code == "EVIDENCE_COMMAND_PAYLOAD_FORBIDDEN"

    accepted = journal.submit(grant, _submission("one_logical_command"))
    with pytest.raises(ApiError) as conflict:
        journal.submit(
            grant,
            EvidenceCommandSubmission(
                command_type="evidence.seal",
                idempotency_key="one_logical_command",
                turn_id="turn_1",
                payload={"endpoint": "explicit"},
            ),
        )
    assert conflict.value.code == "EVIDENCE_COMMAND_IDEMPOTENCY_CONFLICT"
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        assert [item["id"] for item in transaction.evidence_commands.list()] == [
            accepted.command_id
        ]


def test_backfill_command_persists_only_private_object_reference() -> None:
    store = InMemoryStore()
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)
    payload = {
        "batch_id": "browser_backfill_abc",
        "file_id": "backfill_frame_abc",
        "checksum": "sha256:" + "a" * 64,
        "byte_count": 4096,
        "audio_epoch": "epoch_abc",
        "client_sequence": 9,
    }
    receipt = journal.submit(
        grant,
        EvidenceCommandSubmission(
            command_type="evidence.backfill",
            idempotency_key="backfill.abc.9",
            turn_id="turn_1",
            payload=payload,
        ),
    )
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        command = transaction.evidence_commands.get(receipt.command_id)
    assert command["payload"] == payload
    assert "audio_base64" not in json.dumps(command)
    assert "cHJpdmF0ZQ==" not in json.dumps(command)
    with pytest.raises(ApiError) as forbidden:
        journal.submit(
            grant,
            EvidenceCommandSubmission(
                command_type="evidence.backfill",
                idempotency_key="backfill.raw.10",
                turn_id="turn_1",
                payload={**payload, "audio_base64": "cHJpdmF0ZQ=="},
            ),
        )
    assert forbidden.value.code == "EVIDENCE_COMMAND_PAYLOAD_FORBIDDEN"


def test_claim_complete_and_same_key_retry_replay_one_safe_result() -> None:
    store = InMemoryStore()
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)
    receipt = journal.submit(
        grant,
        EvidenceCommandSubmission(
            command_type="evidence.seal",
            idempotency_key="seal_once",
            turn_id="turn_1",
            payload={"endpoint": "semantic_timeout"},
        ),
    )

    claimed = journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None
    assert claimed.command_id == receipt.command_id
    assert claimed.attempt_count == 1
    completed = journal.complete(
        grant.commit_fence(),
        ORGANIZATION_ID,
        claimed.command_id,
        claimed.claim_id,
        EvidenceCommandOutcome(
            disposition="applied",
            effective_turn_id="turn_1",
            event_cursor=42,
        ),
    )
    assert completed.status == "completed"
    assert completed.outcome is not None
    assert completed.outcome.event_cursor == 42

    duplicate = journal.submit(
        grant,
        EvidenceCommandSubmission(
            command_type="evidence.seal",
            idempotency_key="seal_once",
            turn_id="turn_1",
            payload={"endpoint": "semantic_timeout"},
        ),
    )
    assert duplicate.disposition == "duplicate"
    assert duplicate.status == "completed"
    assert duplicate.outcome == completed.outcome
    assert journal.claim_next(grant.commit_fence(), ORGANIZATION_ID) is None


def test_new_control_generation_rejects_unclaimed_old_command() -> None:
    store = InMemoryStore()
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_old",
        local_instance_id="instance_a",
    )
    journal = EvidenceCommandJournal(store)
    submitted = journal.submit(old, _submission("old_controller_command"))
    replacement = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_new",
        local_instance_id="instance_a",
    )

    with pytest.raises(ApiError) as stale:
        journal.submit(old, _submission("old_controller_late_command"))
    assert stale.value.code == "EVIDENCE_CONTROL_STALE"
    assert journal.claim_next(replacement.commit_fence(), ORGANIZATION_ID) is None
    rejected = journal.get_receipt(replacement, submitted.command_id)
    assert rejected.status == "rejected"
    assert rejected.outcome is not None
    assert rejected.outcome.error_code == "EVIDENCE_CONTROL_STALE"


def test_same_key_retry_rebinds_unknown_running_command_to_current_control() -> None:
    store = InMemoryStore()
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_old",
        local_instance_id="instance_a",
    )
    journal = EvidenceCommandJournal(store, claim_seconds=30)
    submitted = journal.submit(old, _submission("unknown_during_reconnect"))
    claimed = journal.claim_next(old.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None
    replacement = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_new",
        local_instance_id="instance_a",
    )

    rebound = journal.submit(
        replacement, _submission("unknown_during_reconnect")
    )
    assert rebound.command_id == submitted.command_id
    assert rebound.status == "running"
    journal.complete(
        old.commit_fence(),
        ORGANIZATION_ID,
        claimed.command_id,
        claimed.claim_id,
        EvidenceCommandOutcome(disposition="applied", event_cursor=9),
    )
    replayed = journal.get_receipt(replacement, submitted.command_id)
    assert replayed.status == "completed"
    assert replayed.outcome is not None
    assert replayed.outcome.event_cursor == 9
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        command = transaction.evidence_commands.get(submitted.command_id)
    assert command["control_generation"] == replacement.control_generation
    assert command["control_rebind_count"] == 1


def test_same_key_retry_can_revive_only_a_stale_control_rejection() -> None:
    store = InMemoryStore()
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_old",
        local_instance_id="instance_a",
    )
    journal = EvidenceCommandJournal(store)
    submitted = journal.submit(old, _submission("rebind_stale_rejection"))
    replacement = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_new",
        local_instance_id="instance_a",
    )
    assert journal.claim_next(replacement.commit_fence(), ORGANIZATION_ID) is None
    assert journal.get_receipt(replacement, submitted.command_id).status == "rejected"

    rebound = journal.submit(
        replacement, _submission("rebind_stale_rejection")
    )
    assert rebound.status == "pending"
    claimed = journal.claim_next(replacement.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None
    assert claimed.command_id == submitted.command_id


def test_owner_epoch_change_reclaims_expired_claim_without_duplicate_result() -> None:
    store = InMemoryStore()
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_1",
        local_instance_id="instance_a",
    )
    journal = EvidenceCommandJournal(store, claim_seconds=30)
    submitted = journal.submit(
        old,
        EvidenceCommandSubmission(
            command_type="speech.started",
            idempotency_key="owner_failover_command",
            turn_id="turn_1",
        ),
    )
    first_claim = journal.claim_next(old.commit_fence(), ORGANIZATION_ID)
    assert first_claim is not None

    assert coordinator.release(old) is True
    successor = coordinator.claim_owner(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        local_instance_id="instance_b",
    )
    assert successor.control_generation == old.control_generation
    assert successor.ownership_epoch == old.ownership_epoch + 1
    with pytest.raises(EvidenceOwnershipLost):
        journal.complete(
            old.commit_fence(),
            ORGANIZATION_ID,
            first_claim.command_id,
            first_claim.claim_id,
            EvidenceCommandOutcome(disposition="applied"),
        )

    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        command = transaction.evidence_commands.get(submitted.command_id)
        command["claim_expires_at"] = "2000-01-01T00:00:00Z"
        transaction.evidence_commands.update(
            command, expected_version=command["version"]
        )

    recovered = journal.claim_next(successor.commit_fence(), ORGANIZATION_ID)
    assert recovered is not None
    assert recovered.command_id == first_claim.command_id
    assert recovered.claim_id != first_claim.claim_id
    assert recovered.attempt_count == 2
    assert recovered.ownership_epoch == successor.ownership_epoch
    completed = journal.complete(
        successor.commit_fence(),
        ORGANIZATION_ID,
        recovered.command_id,
        recovered.claim_id,
        EvidenceCommandOutcome(disposition="applied", effective_turn_id="turn_1"),
    )
    assert completed.status == "completed"
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        commands = transaction.evidence_commands.list()
    assert len(commands) == 1
    assert commands[0]["attempt_count"] == 2


def test_retryable_failure_releases_claim_for_at_least_once_redelivery() -> None:
    store = InMemoryStore()
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)
    journal.submit(
        grant,
        EvidenceCommandSubmission(
            command_type="evidence.continue",
            idempotency_key="retryable_command",
            turn_id="turn_1",
        ),
    )
    first = journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert first is not None
    pending = journal.fail(
        grant.commit_fence(),
        ORGANIZATION_ID,
        first.command_id,
        first.claim_id,
        error_code="EVIDENCE_OWNER_TEMPORARY_FAILURE",
        retryable=True,
        retry_after_seconds=0,
    )
    assert pending.status == "pending"
    second = journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert second is not None
    assert second.command_id == first.command_id
    assert second.attempt_count == 2


def test_sqlite_journal_round_trip_uses_the_same_contract(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "evidence-command-journal.sqlite3"))
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)
    submitted = journal.submit(
        grant,
        EvidenceCommandSubmission(
            command_type="evidence.reset",
            idempotency_key="sqlite_reset_once",
        ),
    )
    claimed = journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None
    journal.complete(
        grant.commit_fence(),
        ORGANIZATION_ID,
        claimed.command_id,
        claimed.claim_id,
        EvidenceCommandOutcome(disposition="applied", event_cursor=7),
    )

    reopened = SQLiteStore(str(store.path))
    reopened_grant = EvidenceOwnershipCoordinator(
        reopened, lease_seconds=30
    ).attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_2",
        local_instance_id="instance_a",
    )
    replayed = EvidenceCommandJournal(reopened).get_receipt(
        reopened_grant, submitted.command_id
    )
    assert replayed.status == "completed"
    assert replayed.outcome is not None
    assert replayed.outcome.event_cursor == 7
