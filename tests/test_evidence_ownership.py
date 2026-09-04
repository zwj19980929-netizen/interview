import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.interview_agent import TurnUnderstanding
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evidence_coordination import (
    EvidenceOwnershipCoordinator,
    EvidenceOwnershipLost,
)
from app.services.interviews import InterviewService


INTERVIEW_ID = "interview_evidence_ownership"
ORGANIZATION_ID = "org_default"


def _session(store: InMemoryStore) -> dict:
    now = utc_now()
    turn = {
        "id": "turn_1",
        "interview_id": INTERVIEW_ID,
        "question_id": "question_1",
        "question_snapshot_id": "snapshot_1",
        "question_snapshot": {
            "id": "snapshot_1",
            "question_text": "请说明幂等提交。",
            "key_points": [],
        },
        "order": 1,
        "phase": "position_bank",
        "status": "asking",
        "is_followup": False,
        "root_turn_id": "turn_1",
        "followup_depth": 0,
        "allow_followup": False,
        "weight": 1.0,
        "question_spoken_text": "请说明幂等提交。",
        "utterances": [],
        "current_understanding": None,
        "conversation_acts": [],
        "started_at": now,
        "completed_at": None,
    }
    item = {
        "id": INTERVIEW_ID,
        "organization_id": ORGANIZATION_ID,
        "status": "in_progress",
        "phase": "position_bank",
        "current_turn_id": turn["id"],
        "turn_ids": [turn["id"]],
        "turns": [turn],
        "answers": [],
        "evaluation_revisions": [],
        "report_revisions": [],
        "current_report_id": None,
        "report_id": None,
        "followup_policy": {"max_depth": 2, "max_total": 1, "max_per_root": 2},
        "lifecycle_events": [],
        "created_at": now,
        "updated_at": now,
        "last_activity_at": now,
    }
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        return transaction.interview_sessions.add(item)


def test_database_clock_is_timezone_aware_and_owner_claim_is_single_winner() -> None:
    store = InMemoryStore()
    _session(store)
    persistence = persistence_for(store)
    with persistence.transaction(ORGANIZATION_ID) as transaction:
        assert transaction.database_now().tzinfo == timezone.utc

    first = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    second = EvidenceOwnershipCoordinator(store, lease_seconds=30)

    def claim(instance_id: str):
        coordinator = first if instance_id == "instance_a" else second
        return coordinator.attach_control(
            interview_id=INTERVIEW_ID,
            organization_id=ORGANIZATION_ID,
            connection_id="connection_%s" % instance_id,
            local_instance_id=instance_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        grants = list(pool.map(claim, ["instance_a", "instance_b"]))

    assert {item.owner_instance_id for item in grants} in (
        {"instance_a"},
        {"instance_b"},
    )
    assert sorted(item.disposition for item in grants) == [
        "local_owner",
        "remote_owner",
    ]
    assert {item.ownership_epoch for item in grants} == {1}


def test_control_generation_fences_old_connection_and_old_detach_is_harmless() -> None:
    store = InMemoryStore()
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    first = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_1",
        local_instance_id="instance_a",
    )
    replacement = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_2",
        local_instance_id="instance_a",
    )

    assert replacement.ownership_epoch == first.ownership_epoch
    assert replacement.lease_id == first.lease_id
    assert replacement.control_generation == first.control_generation + 1
    with pytest.raises(ApiError) as stale:
        coordinator.assert_control_current(first)
    assert stale.value.code == "EVIDENCE_CONTROL_STALE"
    assert coordinator.detach_control(first, grace_seconds=30) is False
    coordinator.assert_control_current(replacement)
    assert coordinator.detach_control(replacement, grace_seconds=30) is True


def test_new_epoch_fences_old_owner_and_old_release_cannot_clobber_successor() -> None:
    store = InMemoryStore()
    _session(store)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    old = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_old",
        local_instance_id="instance_a",
    )
    assert coordinator.release(old) is True
    successor = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_new",
        local_instance_id="instance_b",
    )

    assert successor.ownership_epoch == old.ownership_epoch + 1
    with pytest.raises(EvidenceOwnershipLost):
        coordinator.assert_current(old.commit_fence(), ORGANIZATION_ID)
    assert coordinator.release(old) is False
    coordinator.assert_current(successor.commit_fence(), ORGANIZATION_ID)


def test_delayed_old_stt_final_cannot_create_candidate_answer() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        _session(store)
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
        old = coordinator.attach_control(
            interview_id=INTERVIEW_ID,
            organization_id=ORGANIZATION_ID,
            connection_id="connection_old",
            local_instance_id="instance_a",
        )
        service = InterviewService(store)

        async def understand(utterance, _turn, _interview):
            # Model work began under epoch 1. Before its result returns, owner B
            # claims epoch 2; the subsequent domain transaction must reject it.
            assert coordinator.release(old) is True
            coordinator.attach_control(
                interview_id=INTERVIEW_ID,
                organization_id=ORGANIZATION_ID,
                connection_id="connection_new",
                local_instance_id="instance_b",
            )
            return TurnUnderstanding(
                understanding_id="understanding_1",
                revision=1,
                prompt_version="interview_turn_understanding.v1",
                utterance_id=utterance.utterance_id,
                intent="answer",
                answer_summary="候选人说明了幂等提交。",
                claims=[],
                evidence_quotes=["幂等提交"],
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

        service.conversation.understand = understand
        service.conversation.select_followup = no_followup

        with pytest.raises(EvidenceOwnershipLost):
            await service.submit_streaming_answer(
                INTERVIEW_ID,
                {
                    "turn_id": "turn_1",
                    "audio_uri": "private-file://recording_1",
                    "content_type": "audio/pcm",
                    "final_transcript": "我会使用幂等提交。",
                    "confidence": 0.95,
                    "language": "zh-CN",
                    "duration_seconds": 3,
                    "provider": {"provider_id": "test", "model": "test"},
                    "segments": [],
                },
                ORGANIZATION_ID,
                evidence_fence=old.commit_fence(),
            )

        current = service.get_interview(INTERVIEW_ID, ORGANIZATION_ID)
        assert current["answers"] == []
        assert current["turns"][0]["status"] == "transcribing"

    asyncio.run(scenario())
