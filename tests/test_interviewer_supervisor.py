"""Enterprise interviewer cognition and real runtime commit boundaries."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from app.core.auth import Principal
from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.interview_agent import ClientCapabilities, ClientSignal, OpenAgentSession
from app.model_gateway.schemas import ChatJSONResponse, Usage, ProviderMeta
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService
from app.services.interview_agent import InterviewAgentRuntime, AgentChannel
from app.services.interviewer_supervisor import InterviewerSupervisor
from app.services.interviewer_supervisor.context import context_fingerprint, conversation_memory
from app.services.interviewer_supervisor.tools import InterviewerTools
from app.services.interview_skills import InterviewSkillService
from test_adaptive_interview import make_session, select, submit
from test_interview_skills import approved, snapshot


def source_session(store=None):
    session = make_session()
    session.update(started_at=utc_now(), updated_at=utc_now(), candidate={"id": "candidate", "name": "候选人"},
        candidate_id="candidate", settings={"record_audio": False, "record_video": False},
        agent_runtime={"floor": "none", "calibration_status": "awaiting_confirmation", "processed_signal_keys": [], "last_sequence": 0}, agent_events=[])
    if store is not None:
        with persistence_for(store).transaction("org_default") as tx:
            return tx.interview_sessions.add(session)
    return session


def decision(question="q1", **changes):
    return {"action": "select_question", "question_id": question, "inquiry_unit_id": None, "tool_name": None, "argument": None,
            "reason_code": "coverage_gap", "transition_key": "continue", **changes}


class Gateway:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def invoke(self, capability, request):
        self.calls.append(request)
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return ChatJSONResponse(data=result, usage=Usage(),
            provider=ProviderMeta(provider_id="synthetic", model="test", request_id="test", latency_ms=0))


def supervisor(gateway, store=None):
    return InterviewerSupervisor(gateway=gateway, conversation=None, persistence=persistence_for(store or InMemoryStore()))


@pytest.mark.anyio
async def test_supervisor_reads_real_tool_result_then_selects_without_mutation():
    session = source_session()
    original = deepcopy(session)
    gateway = Gateway(decision(None, action="use_tool", tool_name="questions.read", argument="q2", reason_code="inspect_context"), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert result.question_id == "q2" and result.model_calls == 2
    assert result.tools_used == ("questions.read",)
    observed = json.loads(gateway.calls[1].messages[-1].content)["tool_observations"]
    assert observed[0]["result"]["question"]["question_text"] == "说明database的边界。"
    serialized = json.dumps([request.model_dump() for request in gateway.calls], ensure_ascii=False)
    assert "standard_answer" not in serialized and "说明边界与权衡。" not in serialized
    assert all(request.execution_budget.max_provider_retries == 0 for request in gateway.calls)
    assert session == original


@pytest.mark.anyio
async def test_specialist_has_no_side_effects_and_shares_three_call_budget():
    gateway = Gateway(decision(None, action="use_tool", tool_name="specialists.consult", reason_code="inspect_context"),
        {"question_ids": ["q2"], "evidence_gap": "尚未了解数据库经验。"}, decision("q2"))
    result = await supervisor(gateway).propose_next(source_session())
    assert result.model_calls == 3
    assert [request.metadata["prompt_version"] for request in gateway.calls] == [
        "interviewer_supervisor.v2", "interviewer_evidence_expert.v1", "interviewer_supervisor.v2"]
    assert gateway.calls[-1].json_schema["properties"]["tool_name"]["enum"] == [None]


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [decision("foreign"), decision(extra="forbidden"), decision(question_id=""),
    decision(action="delete_session"), decision(tool_name="questions.read"), decision(transition_key="reveal_answer"),
    decision(None, action="use_tool", tool_name="http.fetch", argument="https://private", reason_code="inspect_context"),
    decision(None, action="finish_interview", reason_code="candidate_requested_stop")])
async def test_invalid_model_output_never_leaves_cognitive_boundary(payload):
    with pytest.raises(ApiError):
        await supervisor(Gateway(payload)).propose_next(source_session())


def test_tools_honor_skill_permissions_and_frozen_question_scope():
    session = source_session()
    session["plan_snapshot"]["enterprise_skill_snapshot"] = {"allowed_tools": ["questions.search"]}
    tools = InterviewerTools(session)
    assert len(tools.execute("questions.search", "database")["items"]) == 1
    with pytest.raises(ApiError):
        tools.execute("questions.read", "q1")
    tools = InterviewerTools(source_session())
    with pytest.raises(ApiError):
        tools.execute("questions.read", "foreign")
    with pytest.raises(ApiError):
        tools.execute("resume.read_evidence", "q1")
    with pytest.raises(ApiError):
        tools.execute("company.read_reference", "foreign")


def test_followup_refusal_closes_source_for_tools_and_conversation_agenda():
    session = source_session()
    session["plan_snapshot"]["assessment_contract"]["candidate_questions"][0]["inquiry_units"] = [
        {"id": "unit_1", "question_text": "说说你怎样处理异常？", "competency_ids": ["python"]},
        {"id": "unit_2", "question_text": "说说你怎样释放资源？", "competency_ids": ["python"]},
    ]
    session["turns"] = [
        {"id": "root", "question_id": "q1", "inquiry_unit_id": "unit_1", "current_understanding": {}},
        {"id": "followup", "question_id": "generated_followup", "root_turn_id": "root", "is_followup": True,
         "current_understanding": {"turn_intent": "decline_topic", "followup_allowed": False}},
    ]
    tools = InterviewerTools(session)
    assert "q1" not in tools.candidates
    assert "q2" in tools.candidates
    assert conversation_memory(session)["agenda"][0]["topic_closed"] is True


@pytest.mark.anyio
async def test_service_selects_exactly_one_root_and_rejects_changed_session():
    store = InMemoryStore()
    session = source_session(store)
    service = InterviewService(store)
    service.supervisor = supervisor(Gateway(decision()), store)
    selected = await service.advance_adaptive_interview(session["id"])
    assert len(selected["turns"]) == 1 and selected["decision_revision"] == 1
    again = await service.advance_adaptive_interview(session["id"])
    assert len(again["turns"]) == 1
    assert selected["agent_runtime"]["last_planning"]["prompt_version"] == "interviewer_supervisor.v2"

    store = InMemoryStore()
    session = source_session(store)
    service = InterviewService(store)
    proposal = await supervisor(Gateway(decision()), store).propose_next(session)
    async def pause_while_planning(source):
        service.pause_interview(session["id"])
        return proposal
    service.supervisor.propose_next = pause_while_planning
    with pytest.raises(ApiError) as error:
        await service.advance_adaptive_interview(session["id"])
    assert error.value.code == "AGENT_DECISION_STALE"
    assert service.get_interview(session["id"])["turns"] == []


@pytest.mark.anyio
async def test_skill_revoke_during_inference_prevents_commit_and_expression():
    store = InMemoryStore()
    skills = InterviewSkillService(store)
    skill = approved(skills, "org_default")
    session = source_session()
    session["plan_snapshot"]["enterprise_skill_snapshot"] = snapshot(skills, skill, "org_default")
    with persistence_for(store).transaction("org_default") as tx:
        session = tx.interview_sessions.add(session)
    service = InterviewService(store)
    proposal = await supervisor(Gateway(decision()), store).propose_next(session)
    async def revoke(source):
        skills.revoke(skill["id"], expected_version=skill["version"], actor_id="reviewer", reason="Superseded policy", organization_id="org_default")
        return proposal
    service.supervisor.propose_next = revoke
    with pytest.raises(ApiError) as error:
        await service.advance_adaptive_interview(session["id"])
    assert error.value.code == "SKILL_AUTHORIZATION_REVOKED"
    current = service.get_interview(session["id"])
    assert current["status"] == "paused" and not current["turns"]
    runtime = InterviewAgentRuntime(store)
    with pytest.raises(ApiError):
        runtime._approve_conversation_act(session["id"], act_type="question", text="过期问题", turn_id=None,
            evidence_quotes=[], approved_by="interview_agent_runtime", organization_id="org_default")


def channel_for(runtime, session):
    return AgentChannel(runtime, OpenAgentSession(interview_id=session["id"], connection_id="connection",
        principal=Principal(actor_id="candidate", organization_id="org_default", roles=frozenset({"candidate"}), authenticated=True),
        capabilities=ClientCapabilities(webrtc=True, audio_worklet=True, webgl=True, camera=False, microphone=True, speaker=True, avatar_fps=60)))


@pytest.mark.anyio
async def test_real_warmup_entry_starts_adaptive_interviewer_and_safe_candidate_snapshot():
    store = InMemoryStore()
    session = source_session(store)
    runtime = InterviewAgentRuntime(store)
    runtime.interviews.supervisor = supervisor(Gateway(decision()), store)
    channel = channel_for(runtime, session)
    channel._select_turn = AsyncMock()
    await channel.send(ClientSignal(type="warmup.confirm", idempotency_key="warmup"))
    assert channel._planning_task is not None
    await channel._planning_task
    channel._select_turn.assert_awaited_once()
    current = runtime.interviews.get_interview(session["id"])
    view = runtime._snapshot(current, channel.principal)
    assert view["execution_schema_version"] == 3 and view["total_primary_questions"] is None
    assert view["current_question"]["question_text"] == "说明python的边界。"
    assert "assessment_contract" not in view and "enterprise_skill_snapshot" not in view
    await channel.close("test_done")


@pytest.mark.anyio
async def test_planning_failure_is_recoverable_and_retry_does_not_hold_pause_lock():
    store = InMemoryStore()
    session = source_session(store)
    runtime = InterviewAgentRuntime(store)
    runtime.interviews.supervisor = supervisor(Gateway(RuntimeError("raw sensitive response"), decision()), store)
    channel = channel_for(runtime, session)
    channel._select_turn = AsyncMock()
    await channel.send(ClientSignal(type="warmup.confirm", idempotency_key="warmup"))
    await channel._planning_task
    current = runtime.interviews.get_interview(session["id"])
    assert current["agent_runtime"]["planning_problem"]["action"] == "retry_planning"
    assert "raw sensitive" not in json.dumps(current["agent_events"])
    await channel.send(ClientSignal(type="planning.retry", idempotency_key="retry"))
    await channel._planning_task
    assert runtime.interviews.get_interview(session["id"])["agent_runtime"]["planning_problem"] is None
    channel._select_turn.assert_awaited_once()
    await channel.close("test_done")

    store = InMemoryStore()
    session = source_session(store)
    runtime = InterviewAgentRuntime(store)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def slow(source):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    runtime.interviews.supervisor.propose_next = slow
    channel = channel_for(runtime, session)
    await channel.send(ClientSignal(type="warmup.confirm", idempotency_key="warmup"))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(channel.send(ClientSignal(type="pause", idempotency_key="pause")), .5)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert runtime.interviews.get_interview(session["id"])["status"] == "paused"
    assert not runtime.interviews.get_interview(session["id"])["turns"]
    await channel.close("test_done")


def test_next_context_ignores_background_scoring_but_tracks_new_evidence():
    session = submit(select(make_session(), "q1").session).session
    initial = context_fingerprint(session)
    session["turns"][0]["status"] = "evaluated"
    assert context_fingerprint(session) == initial
    session["turns"][0]["current_understanding"] = {"turn_intent": "stop_interview"}
    assert context_fingerprint(session) != initial


def test_committed_candidate_stop_intent_ends_input_without_selecting_new_question():
    store = InMemoryStore()
    session = submit(select(make_session(), "q1").session).session
    session["turns"][0]["current_understanding"] = {"turn_intent": "stop_interview"}
    with persistence_for(store).transaction("org_default") as tx:
        tx.interview_sessions.add(session)
    service = InterviewService(store)
    stopped = service.finish_on_candidate_intent(session["id"])
    assert stopped["candidate_input_completion_reason"] == "candidate_requested"
    assert len(stopped["answers"]) == 1 and len(stopped["turns"]) == 1
