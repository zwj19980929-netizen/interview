import asyncio
from copy import deepcopy
from typing import Optional

from app.domain.interview_lifecycle import (
    InterviewSessionLifecycle,
    LifecycleCommand,
    LifecycleCommandType,
)
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService
from app.services.interview_evidence import InterviewEvidenceChain


NOW = "2026-08-31T12:00:00Z"


def test_closed_evidence_gate_drops_frames_without_waiting_for_provider_open_lock() -> None:
    async def scenario() -> None:
        chain = InterviewEvidenceChain(InMemoryStore(), "iv_opening_stream")
        await chain._lock.acquire()
        try:
            result = await asyncio.wait_for(
                chain.send_audio(b"\x00\x00" * 320), timeout=0.05
            )
        finally:
            chain._lock.release()
        assert result is None

    asyncio.run(scenario())


def test_evidence_finish_closes_gate_before_waiting_for_provider_final() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowWarmup:
            async def finish(self):
                started.set()
                await release.wait()
                return [
                    {
                        "type": "transcript.final",
                        "text": "试音完成。",
                        "confidence": 0.9,
                    }
                ]

        chain = InterviewEvidenceChain(InMemoryStore(), "iv_finishing_stream")
        chain._warmup = SlowWarmup()
        finishing = asyncio.create_task(chain.finish({}))
        await started.wait()

        assert chain.is_open is False
        assert await asyncio.wait_for(
            chain.send_audio(b"\x00\x00" * 320), timeout=0.05
        ) is None
        release.set()
        result = await finishing
        assert result.final["text"] == "试音完成。"

    asyncio.run(scenario())


def _snapshot(snapshot_id: str, question_id: str) -> dict:
    return {
        "id": snapshot_id,
        "source_question_id": question_id,
        "source_question_version": 1,
        "source_type": "position_bank",
        "question_text": "请说明可靠任务处理。",
        "spoken_text": "请说明可靠任务处理。",
        "standard_answer": "使用幂等键、Outbox 和可观测重试。",
        "key_points": [
            {"id": "kp_idempotency", "text": "幂等键", "weight": 0.5},
            {"id": "kp_outbox", "text": "Outbox", "weight": 0.5},
        ],
        "rubric": {"semantic_correctness": 1.0},
        "difficulty": "senior",
    }


def _turn(
    turn_id: str,
    order: int,
    *,
    status: str,
    is_followup: bool = False,
    parent_turn_id: Optional[str] = None,
    root_turn_id: Optional[str] = None,
    depth: int = 0,
) -> dict:
    snapshot = _snapshot("snap_%s" % turn_id, "q_%s" % turn_id)
    return {
        "id": turn_id,
        "interview_id": "iv_evidence",
        "turn_blueprint_id": "bp_%s" % turn_id,
        "question_id": snapshot["source_question_id"],
        "question_snapshot_id": snapshot["id"],
        "question_snapshot": snapshot,
        "order": order,
        "phase": "position_bank",
        "is_followup": is_followup,
        "parent_turn_id": parent_turn_id,
        "root_turn_id": root_turn_id or turn_id,
        "followup_depth": depth,
        "followup_reason": "missing_capability_evidence" if is_followup else None,
        "target_key_points": ["Outbox"] if is_followup else [],
        "allow_followup": depth < 2,
        "weight": 0.0 if is_followup else 1.0,
        "expected_minutes": 3,
        "status": status,
        "question_spoken_text": snapshot["question_text"],
        "started_at": NOW,
        "completed_at": NOW if status == "completed" else None,
        "utterances": [],
        "current_understanding": None,
        "conversation_acts": [],
    }


def _answer(answer_id: str, turn_id: str, transcript: str, *, status: str) -> dict:
    return {
        "id": answer_id,
        "organization_id": "org_default",
        "interview_id": "iv_evidence",
        "turn_id": turn_id,
        "question_id": "q_%s" % turn_id,
        "question_snapshot_id": "snap_%s" % turn_id,
        "utterance_id": "utterance_%s" % answer_id,
        "root_turn_id": "turn_root",
        "raw_transcript": transcript,
        "final_transcript": transcript,
        "audio_uri": "private-test://%s.webm" % answer_id,
        "stt_confidence": 0.95,
        "transcript_source": "server_streaming",
        "evaluation_status": status,
        "current_evaluation_id": None,
        "evaluation_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _evaluation(evaluation_id: str, answer_id: str, snapshot_id: str, score: int) -> dict:
    return {
        "id": evaluation_id,
        "organization_id": "org_default",
        "interview_id": "iv_evidence",
        "answer_id": answer_id,
        "question_id": "q_turn_root",
        "question_snapshot_id": snapshot_id,
        "score": score,
        "confidence": 0.9,
        "dimension_scores": [{"dimension": "technical", "score": score}],
        "covered_key_points": [{"key_point_id": "kp_outbox", "evidence": "Outbox"}],
        "missing_key_points": [],
        "incorrect_claims": [],
        "evidence": ["Outbox"],
        "review_flags": [],
        "feedback": "有服务端证据。",
        "suggested_followup": None,
        "model_info": {"provider_id": "test", "model": "test"},
        "created_by": "system",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _base_session() -> dict:
    root = _turn("turn_root", 1, status="asking")
    second = _turn("turn_second", 2, status="pending")
    return {
        "id": "iv_evidence",
        "organization_id": "org_default",
        "status": "in_progress",
        "phase": "position_bank",
        "current_turn_id": root["id"],
        "turn_ids": [root["id"], second["id"]],
        "turns": [root, second],
        "answers": [],
        "evaluation_revisions": [],
        "report_revisions": [],
        "current_report_id": None,
        "report_id": None,
        "followup_policy": {"max_depth": 2, "max_total": 2, "max_per_root": 2},
        "plan_snapshot": {
            "role_requirement": {"description": "后端可靠性"},
            "question_snapshots": [
                {
                    "question_snapshot_id": root["question_snapshot_id"],
                    "weight": 1.0,
                    "dimension": "technical",
                },
                {
                    "question_snapshot_id": second["question_snapshot_id"],
                    "weight": 0.0,
                    "dimension": "technical",
                },
            ],
        },
        "lifecycle_events": [],
        "last_activity_at": NOW,
        "started_at": NOW,
        "completed_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _apply(lifecycle: InterviewSessionLifecycle, session: dict, command_type, payload: dict):
    return lifecycle.execute(
        session,
        LifecycleCommand(command_type, payload),
        now=NOW,
    )


def test_depth_two_followups_queue_one_root_evidence_revision() -> None:
    lifecycle = InterviewSessionLifecycle()
    session = _base_session()
    root_answer = _answer("ans_root", "turn_root", "我会使用幂等键。", status="pending")

    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.ANSWER_SUBMITTED,
        {"answer": root_answer, "hold_for_followup": True},
    )
    session = decision.session
    followup_one = _turn(
        "turn_followup_one",
        2,
        status="pending",
        is_followup=True,
        parent_turn_id="turn_root",
        root_turn_id="turn_root",
        depth=1,
    )
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.FOLLOWUP_REQUESTED,
        {
            "root_turn_id": "turn_root",
            "answer_id": "ans_root",
            "followup_turn": followup_one,
        },
    )
    session = decision.session
    root_eval = _evaluation("eval_root_1", "ans_root", "snap_turn_root", 60)
    root_eval["evidence_answer_ids"] = ["ans_root"]
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.EVALUATION_SUCCEEDED,
        {"answer_id": "ans_root", "evaluation": root_eval, "revision": 1},
    )
    session = decision.session

    followup_one_answer = _answer(
        "ans_followup_one", "turn_followup_one", "Outbox 用于可靠发布事件。", status="pending"
    )
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.ANSWER_SUBMITTED,
        {"answer": followup_one_answer, "hold_for_followup": True},
    )
    session = decision.session
    followup_two = _turn(
        "turn_followup_two",
        3,
        status="pending",
        is_followup=True,
        parent_turn_id="turn_followup_one",
        root_turn_id="turn_root",
        depth=2,
    )
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.FOLLOWUP_REQUESTED,
        {
            "root_turn_id": "turn_root",
            "answer_id": "ans_followup_one",
            "followup_turn": followup_two,
        },
    )
    session = decision.session
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.EVALUATION_SUCCEEDED,
        {
            "answer_id": "ans_followup_one",
            "evaluation": _evaluation(
                "eval_followup_one", "ans_followup_one", "snap_turn_followup_one", 70
            ),
            "revision": 1,
        },
    )
    session = decision.session

    followup_two_answer = _answer(
        "ans_followup_two", "turn_followup_two", "重试消费也由同一个幂等键保护。", status="pending"
    )
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.ANSWER_SUBMITTED,
        {"answer": followup_two_answer, "hold_for_followup": False},
    )
    session = decision.session
    decision = _apply(
        lifecycle,
        session,
        LifecycleCommandType.EVALUATION_SUCCEEDED,
        {
            "answer_id": "ans_followup_two",
            "evaluation": _evaluation(
                "eval_followup_two", "ans_followup_two", "snap_turn_followup_two", 80
            ),
            "revision": 1,
        },
    )

    assert [turn["followup_depth"] for turn in decision.session["turns"] if turn["is_followup"]] == [1, 2]
    assert all(turn["weight"] == 0.0 for turn in decision.session["turns"] if turn["is_followup"])
    assert len(decision.effects) == 1
    assert decision.effects[0]["type"] == "evaluation.requested"
    assert decision.effects[0]["payload"] == {
        "answer_id": "ans_root",
        "revision": 2,
        "trigger_reason": "followup_evidence_merged",
        "evidence_answer_ids": ["ans_root", "ans_followup_one", "ans_followup_two"],
    }
    stored_root = next(item for item in decision.session["answers"] if item["id"] == "ans_root")
    assert stored_root["evaluation_status"] == "pending"
    assert stored_root["current_evaluation_id"] == "eval_root_1"


def test_service_scores_merged_authoritative_evidence_as_root_revision_and_report_item() -> None:
    store = InMemoryStore()
    persistence = persistence_for(store)
    service = InterviewService(store, persistence=persistence)
    session = _base_session()
    session["turns"] = [
        _turn("turn_root", 1, status="completed"),
        _turn(
            "turn_followup_one",
            2,
            status="completed",
            is_followup=True,
            parent_turn_id="turn_root",
            root_turn_id="turn_root",
            depth=1,
        ),
        _turn(
            "turn_followup_two",
            3,
            status="evaluating",
            is_followup=True,
            parent_turn_id="turn_followup_one",
            root_turn_id="turn_root",
            depth=2,
        ),
        _turn("turn_second", 4, status="asking"),
    ]
    session["turn_ids"] = [item["id"] for item in session["turns"]]
    session["current_turn_id"] = "turn_second"
    root_answer = _answer("ans_root", "turn_root", "我会使用幂等键。", status="completed")
    root_answer["current_evaluation_id"] = "eval_root_1"
    root_answer["evaluation_id"] = "eval_root_1"
    root_answer["current_evidence_answer_ids"] = ["ans_root"]
    first_answer = _answer(
        "ans_followup_one", "turn_followup_one", "Outbox 保证可靠发布。", status="completed"
    )
    first_answer["current_evaluation_id"] = "eval_followup_one"
    first_answer["evaluation_id"] = "eval_followup_one"
    final_answer = _answer(
        "ans_followup_two", "turn_followup_two", "消费重试继续复用幂等键。", status="pending"
    )
    session["answers"] = [root_answer, first_answer, final_answer]
    root_eval = _evaluation("eval_root_1", "ans_root", "snap_turn_root", 50)
    root_eval.update(
        {
            "revision": 1,
            "supersedes_evaluation_id": None,
            "trigger_reason": "initial_scoring",
            "evidence_answer_ids": ["ans_root"],
        }
    )
    first_eval = _evaluation(
        "eval_followup_one", "ans_followup_one", "snap_turn_followup_one", 60
    )
    first_eval.update(
        {"revision": 1, "supersedes_evaluation_id": None, "trigger_reason": "initial_scoring"}
    )
    session["evaluation_revisions"] = [root_eval, first_eval]
    with persistence.transaction("org_default") as transaction:
        transaction.interview_sessions.add(session)

    final_eval = _evaluation(
        "eval_followup_two", "ans_followup_two", "snap_turn_followup_two", 70
    )
    _, work_items = service._apply_command(
        "iv_evidence",
        LifecycleCommand(
            LifecycleCommandType.EVALUATION_SUCCEEDED,
            {"answer_id": "ans_followup_two", "evaluation": final_eval, "revision": 1},
        ),
        "org_default",
    )
    assert len(work_items) == 1
    assert work_items[0]["payload"]["evidence_answer_ids"] == [
        "ans_root",
        "ans_followup_one",
        "ans_followup_two",
    ]

    captured = {}

    async def evaluate_merged(answer, question_snapshot, role_requirement):
        captured["answer"] = deepcopy(answer)
        captured["snapshot"] = deepcopy(question_snapshot)
        return _evaluation("eval_root_2", "ans_root", "snap_turn_root", 92)

    service.evaluation.evaluate_answer = evaluate_merged
    persisted = asyncio.run(
        service._process_evaluation_work(work_items[0]["id"], "org_default")
    )

    assert captured["answer"]["id"] == "ans_root"
    assert captured["answer"]["transcript_source"] == "server_authoritative_evidence_group"
    assert captured["answer"]["final_transcript"] == (
        "【主回答】\n我会使用幂等键。\n\n"
        "【追问1回答】\nOutbox 保证可靠发布。\n\n"
        "【追问2回答】\n消费重试继续复用幂等键。"
    )
    assert captured["snapshot"]["id"] == "snap_turn_root"
    assert persisted["revision"] == 2
    assert persisted["supersedes_evaluation_id"] == "eval_root_1"
    assert persisted["trigger_reason"] == "followup_evidence_merged"
    assert persisted["evidence_mode"] == "root_with_followups"
    assert persisted["evidence_answer_ids"] == [
        "ans_root",
        "ans_followup_one",
        "ans_followup_two",
    ]

    updated = service.get_interview("iv_evidence")
    updated_root = next(item for item in updated["answers"] if item["id"] == "ans_root")
    assert updated_root["current_evaluation_id"] == "eval_root_2"
    assert updated_root["evaluation_status"] == "completed"
    report = service.reports.build_report(updated, trigger_reason="test")
    assert report["overall_score"] == 92
    assert len(report["question_evaluations"]) == 1
    assert report["question_evaluations"][0]["answer_id"] == "ans_root"
    assert report["question_evaluations"][0]["evidence_answer_ids"] == [
        "ans_root",
        "ans_followup_one",
        "ans_followup_two",
    ]
