"""Live exchanges have real candidate roles without becoming scored answers."""
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.dialogue_history import recent_turn_dialogue
from app.services.interview_evidence import InterviewEvidenceChain
from test_answer_endpoint import _final

pytestmark = pytest.mark.anyio

@pytest.fixture(params=["memory", "sqlite"])
async def bundle(request, tmp_path):
    store = InMemoryStore() if request.param == "memory" else SQLiteStore(str(tmp_path / "reception.sqlite3"))
    persistence = persistence_for(store)
    with persistence.transaction("org_default") as tx:
        tx.interview_sessions.add({"id": "iv_synthetic", "organization_id": "org_default",
            "status": "in_progress", "current_turn_id": "turn_synthetic", "turns": [{
                "id": "turn_synthetic", "question_spoken_text": "正式问题", "conversation_acts": [], "utterances": []}],
            "agent_runtime": {}, "agent_events": [], "answers": []})
    chain = InterviewEvidenceChain(store, "iv_synthetic", persistence=persistence)
    chain._stt = SimpleNamespace(turn_id="turn_synthetic", _assert_commit_allowed=lambda: None)
    return chain, persistence


def snapshot(persistence):
    with persistence.transaction("org_default", read_only=True) as tx:
        return tx.interview_sessions.get("iv_synthetic")


def select(persistence, index, *, act_id=None):
    act_id = act_id or "act_" + str(index)
    with persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_synthetic")
        turn = session["turns"][0]
        text = "解释" + act_id
        if not any(act["act_id"] == act_id for act in turn["conversation_acts"]):
            turn["conversation_acts"].append({"act_id": act_id, "turn_id": turn["id"], "text": text})
        session["agent_events"].append({"event_id": "event_" + str(index), "type": "conversation.act.selected",
            "turn_id": turn["id"], "session_sequence": index, "occurred_at": str(index).zfill(3),
            "payload": {"act_id": act_id, "approved": True, "text": text}})
        session["agent_runtime"].update(active_performance_id="performance_" + str(index),
            active_expression_act_event_id="event_" + str(index))
        tx.interview_sessions.update(session, expected_version=session["version"])


def record(chain, persistence, index, *, act_id=None):
    text = "候选人的概念请求" + str(index)
    token = chain.stage_reception_exchange(_final(text), text, "question_clarification")
    select(persistence, index, act_id=act_id)
    assert chain.record_reception_exchange(performance_id="performance_" + str(index), guard=lambda: None)
    chain.discard_reception_exchange(token)


async def test_six_live_exchanges_survive_refresh_in_actual_selection_order(bundle):
    chain, persistence = bundle
    for index in range(1, 8):
        record(chain, persistence, index, act_id="reused_a" if index % 2 else "reused_b")
    saved = snapshot(persistence)
    turn = saved["turns"][0]
    assert len(turn["reception_exchanges"]) == 6
    assert not turn["utterances"] and not saved["answers"]
    # A fresh projection uses only persisted source IDs, not process memory.
    history = recent_turn_dialogue(saved, turn, max_acts=6)
    assert [item["speaker"] for item in history] == ["candidate", "interviewer"] * 6
    assert [item["text"] for item in history[::2]] == ["候选人的概念请求" + str(i) for i in range(2, 8)]
    assert all(item.get("exchange_id") and not item.get("utterance_id") for item in history[::2])
    assert history[-1]["act_id"] == "reused_a"


@pytest.mark.parametrize("mutation", ["paused", "turn", "capture", "guard", "performance", "unselected"])
async def test_stale_exchange_cannot_be_attached_to_a_new_or_unselected_response(bundle, mutation):
    chain, persistence = bundle
    token = chain.stage_reception_exchange(_final("请解释"), "请解释", "question_clarification")
    if mutation != "unselected":
        select(persistence, 1)
    if mutation in {"paused", "turn"}:
        with persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_synthetic")
            session["status" if mutation == "paused" else "current_turn_id"] = "paused" if mutation == "paused" else "new_turn"
            tx.interview_sessions.update(session, expected_version=session["version"])
    if mutation == "capture":
        chain._stt = SimpleNamespace(turn_id="turn_synthetic", _assert_commit_allowed=lambda: None)
    def guard():
        if mutation == "guard":
            raise ApiError("TURN_DECISION_STALE", "New input", status_code=409)
    assert not chain.record_reception_exchange(
        performance_id="other" if mutation == "performance" else "performance_1", guard=guard)
    chain.discard_reception_exchange(token)
    assert not snapshot(persistence)["turns"][0].get("reception_exchanges")


async def test_abandoned_stage_and_duplicate_record_do_not_create_candidate_facts(bundle):
    chain, persistence = bundle
    token = chain.stage_reception_exchange(_final("请解释"), "请解释", "question_clarification")
    chain.discard_reception_exchange(token)
    select(persistence, 1)
    assert not chain.record_reception_exchange(performance_id="performance_1", guard=lambda: None)
    record(chain, persistence, 2)
    assert not chain.record_reception_exchange(performance_id="performance_2", guard=lambda: None)
    assert len(snapshot(persistence)["turns"][0]["reception_exchanges"]) == 1


async def test_durable_owner_fence_is_rechecked_even_when_process_guard_allows(bundle):
    from app.services.evidence_coordination import EvidenceOwnershipCoordinator
    chain, persistence = bundle
    coordinator = EvidenceOwnershipCoordinator(chain.store, persistence=persistence)
    grant = coordinator.attach_control(interview_id=chain.interview_id, organization_id="org_default",
        connection_id="controller", local_instance_id="instance")
    chain.commit_fence = grant.commit_fence().model_copy(update={"ownership_epoch": grant.ownership_epoch + 1})
    chain.stage_reception_exchange(_final("请解释"), "请解释", "question_clarification")
    select(persistence, 1)
    assert not chain.record_reception_exchange(performance_id="performance_1", guard=lambda: None)
    assert not snapshot(persistence)["turns"][0].get("reception_exchanges")


async def test_live_memory_rejects_preview_and_cannot_create_unselected_candidate_history(bundle):
    from test_stable_preview_preparation import _preview
    chain, persistence = bundle
    with pytest.raises(ValueError):
        chain.stage_reception_exchange(_preview(_final("请解释")), "请解释", "question_clarification")
    record(chain, persistence, 1)
    saved = snapshot(persistence)
    saved["turns"][0]["reception_exchanges"][0]["selected_event_id"] = "absent_event"
    history = recent_turn_dialogue(saved, saved["turns"][0])
    assert [item["speaker"] for item in history] == ["interviewer"]


async def test_optional_stage_read_failure_discards_pending_receipt_without_weakening_guard(bundle, monkeypatch, caplog):
    import sqlite3
    from contextlib import contextmanager
    chain, _ = bundle
    @contextmanager
    def failed_read(*args, **kwargs):
        raise sqlite3.OperationalError("PRIVATE_STAGE_DETAIL")
        yield
    monkeypatch.setattr(chain.persistence, "transaction", failed_read)
    assert chain.stage_reception_exchange(_final("请解释"), "请解释", "question_clarification") is None
    assert chain._pending_reception_exchange is None
    assert not chain.record_reception_exchange(performance_id="absent", guard=lambda: None)
    assert "PRIVATE_STAGE_DETAIL" not in caplog.text
    def revoked():
        raise ApiError("EVIDENCE_OWNER_FENCED", "Owner changed", status_code=409)
    chain._stt._assert_commit_allowed = revoked
    with pytest.raises(ApiError):
        chain.stage_reception_exchange(_final("请解释"), "请解释", "question_clarification")


async def test_cancel_is_not_swallowed_by_optional_recording(bundle):
    import asyncio
    chain, persistence = bundle
    chain.stage_reception_exchange(_final("请解释"), "请解释", "question_clarification")
    select(persistence, 1)
    def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        chain.record_reception_exchange(performance_id="performance_1", guard=cancelled)
    assert chain._pending_reception_exchange is None


@pytest.mark.anyio
@pytest.mark.parametrize("future_turns", [False, True])
async def test_reception_sees_live_candidate_roles_and_distinct_current_root_clarification(bundle, future_turns):
    chain, persistence = bundle
    for index in range(1, 7):
        record(chain, persistence, index)
    with persistence.transaction("org_default") as tx:
        saved = tx.interview_sessions.get(chain.interview_id)
        turn = saved["turns"][0]
        turn.update(root_turn_id="root", parent_turn_id="parent", dialogue_focus={"act_id": "act_6"})
        turn["conversation_acts"][-1]["act_type"] = "clarification"
        saved["turns"].insert(0, {"id": "root", "question_spoken_text": "原始主问题"})
        if future_turns:
            for index in range(2):
                identifier = "future_" + str(index)
                text = "FUTURE_DO_NOT_LEAK_" + str(index)
                saved["turns"].append({"id": identifier, "question_spoken_text": text,
                    "conversation_acts": [{"act_id": identifier, "turn_id": identifier, "text": text}]})
                saved["agent_events"].append({"event_id": identifier, "type": "conversation.act.selected",
                    "turn_id": identifier, "session_sequence": 20 + index, "occurred_at": "999",
                    "payload": {"act_id": identifier, "approved": True, "text": text}})
        tx.interview_sessions.update(saved, expected_version=saved["version"])
    calls = []
    async def classify(text, org, **kwargs):
        calls.append(kwargs)
        return {"kind": "other", "confidence": .5}
    chain._stt.interviews = SimpleNamespace(get_interview=lambda *args: snapshot(persistence),
        supervisor=SimpleNamespace(load_skill=lambda session: {}),
        conversation=SimpleNamespace(classify_reception=classify))
    await chain.classify_reception("我继续答", phase="listening", preceding_text="之前的请求")
    context = calls[0]
    assert context["current_question"] == "正式问题" and context["root_question"] == "原始主问题"
    assert context["last_clarification"] == "解释act_6"
    assert context["current_turn_id"] == "turn_synthetic" and context["parent_turn_id"] == "parent"
    assert [item["role"] for item in context["history"]] == ["candidate", "interviewer"] * 6
    assert context["history"][0]["exchange_id"]
    assert "FUTURE_DO_NOT_LEAK" not in str(context)
