"""Optional preferences and company context stay independent across real seams."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.errors import ApiError
from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.schemas.api import InterviewPlanGenerateRequest
from app.services.interviewer_supervisor import InterviewerSupervisor
from app.services.interviewer_supervisor.context import context_fingerprint
from app.services.interviewer_supervisor.tools import InterviewerTools
from app.services.interview_skills import InterviewSkillService
from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyRequest
from test_enterprise_interviewer_flow import COMPANY_CONTEXT, PERSONAL_SKILL
from test_interviewer_supervisor import Gateway, decision, source_session
from test_plan_assembly import add_question, create_scope


async def assembly_fixture():
    store = InMemoryStore()
    catalog, position, bank, role, candidate = create_scope(store, "独立上下文", ["python"], 15)
    await add_question(catalog, bank["id"], title="任务边界", skill="python", difficulty="mid", key_points=["重试"])
    request = PlanAssemblyRequest(role_requirement_id=role["id"], job_position_id=position["id"],
        candidate_profile_id=candidate["id"], knowledge_base_ids=(bank["id"],), question_count=1,
        execution_schema_version=3, approve=True,
        adaptive_policy={"min_root_questions": 1, "max_root_questions": 1, "min_evidence_units_per_competency": 1})
    return store, request


@pytest.mark.anyio
@pytest.mark.parametrize("has_skill,has_company", [(False, False), (True, False), (False, True), (True, True)])
async def test_real_plan_context_is_optional_frozen_and_read_on_demand(monkeypatch, has_skill, has_company):
    store, request = await assembly_fixture()
    skill_service = InterviewSkillService(store)
    if has_skill:
        skill = skill_service.create({"package": deepcopy(PERSONAL_SKILL)}, actor_id="author", organization_id="org_default")
        assert skill["status"] == "active"
        request = replace(request, skill_id=skill["id"])
    else:
        def unexpected(*args, **kwargs):
            pytest.fail("Missing optional Skill must bypass every Skill authorization/load call")
        for method in ("freeze_snapshot", "verify_current_authorization", "load_compiled"):
            monkeypatch.setattr(InterviewSkillService, method, unexpected)
    if has_company:
        request = replace(request, company_context="\n" + COMPANY_CONTEXT + "  ")
    plan = await InterviewPlanAssembly(store).assemble(request)
    assert plan["status"] == "approved"
    inquiry_audit = [item for item in store.model_invocations if item["purpose"] == "question_generation"]
    assert inquiry_audit and all(item["prompt_version"] == "interview_inquiry_units.v2" for item in inquiry_audit)
    assert plan.get("company_context") == (COMPANY_CONTEXT if has_company else None)
    assert bool(plan.get("enterprise_skill_snapshot")) is has_skill
    assert plan.get("enterprise_skill_id") == (skill["id"] if has_skill else None)
    session = source_session()
    session["plan_snapshot"] = deepcopy(plan)
    question = plan["assessment_contract"]["candidate_questions"][0]
    selection = decision(question["question_id"], inquiry_unit_id=question["inquiry_units"][0]["id"])
    outputs = ([decision(None, action="use_tool", tool_name="company.read_reference",
                         argument="company:context", reason_code="inspect_context")] if has_company else []) + [selection]
    gateway = Gateway(*outputs)
    prepared = SimpleNamespace(prepare_decision=AsyncMock(return_value=("understanding", "followup")))
    supervisor = InterviewerSupervisor(gateway=gateway, conversation=prepared, persistence=persistence_for(store))
    result = await supervisor.propose_next(session)
    assert result.question_id == question["question_id"]
    initial = json.loads(gateway.calls[0].messages[-1].content)
    context = initial["context"]
    assert context["company_context"] == ({"available": True, "reference_id": "company:context"} if has_company else {"available": False})
    assert COMPANY_CONTEXT not in json.dumps(initial, ensure_ascii=False)
    assert bool(context["skill"].get("instructions")) is has_skill
    assert "enterprise_skill" not in context
    if has_skill:
        assert PERSONAL_SKILL["instructions"] in context["skill"]["instructions"]
    exposed = gateway.calls[0].json_schema["properties"]["tool_name"]["enum"]
    assert ("company.read_reference" in exposed) is has_company
    if has_company:
        observations = json.loads(gateway.calls[1].messages[-1].content)["tool_observations"]
        assert observations == [{"tool_name": "company.read_reference", "result": {
            "status": "ok", "reference": {"id": "company:context", "title": "本场提供的企业资料", "content": COMPANY_CONTEXT}}}]
        assert result.model_calls == 2
    else:
        assert initial["tool_observations"] == [] and result.model_calls == 1
        with pytest.raises(ApiError) as error:
            InterviewerTools(session, supervisor.load_skill(session)).execute("company.read_reference", "company:context")
        assert error.value.code == "INTERVIEW_TOOL_FORBIDDEN"
    before = deepcopy(session)
    assert await supervisor.prepare_turn("utterance", {}, session) == ("understanding", "followup")
    enriched = prepared.prepare_decision.await_args.args[2]["_interviewer_context"]
    assert enriched["company_context"] == context["company_context"]
    assert enriched["skill"] == context["skill"]
    assert COMPANY_CONTEXT not in json.dumps(enriched, ensure_ascii=False)
    assert session == before


@pytest.mark.anyio
async def test_personal_resource_is_readable_without_implying_company_information():
    store, request = await assembly_fixture()
    service = InterviewSkillService(store)
    resource = {"id": "practice", "title": "我的练习记录", "content": "我希望多练习描述工程权衡。"}
    skill = service.create({"package": {**PERSONAL_SKILL, "resources": [resource]}}, actor_id="author", organization_id="org_default")
    plan = await InterviewPlanAssembly(store).assemble(replace(request, skill_id=skill["id"]))
    session = source_session()
    session["plan_snapshot"] = plan
    supervisor = InterviewerSupervisor(gateway=None, conversation=None, persistence=persistence_for(store))
    tools = InterviewerTools(session, supervisor.load_skill(session))
    assert supervisor._company_context(session) == {"available": False}
    assert tools.execute("company.read_reference", "practice")["reference"] == resource
    with pytest.raises(ApiError) as error:
        tools.execute("company.read_reference", "company:context")
    assert error.value.code == "INTERVIEW_TOOL_REFERENCE_INVALID"


def request_body(**changes):
    return {"role_requirement_id": "role", "job_position_id": "position", "candidate_profile_id": "candidate",
            "knowledge_base_ids": ["bank"], **changes}


@pytest.mark.parametrize("company", [None, "", " \n\t "])
def test_api_normalizes_empty_company_and_optional_skill(company):
    request = InterviewPlanGenerateRequest(**request_body(company_context=company))
    assert request.skill_id is None and request.enterprise_skill_id is None and request.company_context is None


def test_api_accepts_identical_skill_alias_and_trims_company():
    request = InterviewPlanGenerateRequest(**request_body(skill_id="same", enterprise_skill_id="same", company_context="  company\n"))
    assert request.skill_id == request.enterprise_skill_id == "same"
    assert request.company_context == "company"


@pytest.mark.parametrize("changes", [{"skill_id": "one", "enterprise_skill_id": "two"}, {"company_context": "x" * 12001}])
def test_http_rejects_ambiguous_skill_or_oversized_company_before_business_access(changes):
    with pytest.raises(ValidationError):
        InterviewPlanGenerateRequest(**request_body(**changes))
    response = TestClient(create_app()).post("/api/v1/interview-plans/generate", json=request_body(**changes))
    assert response.status_code == 422, response.text


@pytest.mark.anyio
async def test_internal_plan_request_rejects_alias_conflict_and_oversized_company():
    store, request = await assembly_fixture()
    for changes in ({"skill_id": "one", "enterprise_skill_id": "two"}, {"company_context": "x" * 12001}):
        with pytest.raises(ApiError) as error:
            await InterviewPlanAssembly(store).assemble(replace(request, **changes))
        assert error.value.status_code == 422


@pytest.mark.anyio
async def test_internal_plan_request_accepts_same_alias_and_blank_company():
    store, request = await assembly_fixture()
    skill = InterviewSkillService(store).create({"package": deepcopy(PERSONAL_SKILL)}, actor_id="author", organization_id="org_default")
    plan = await InterviewPlanAssembly(store).assemble(replace(request, skill_id=skill["id"], enterprise_skill_id=skill["id"], company_context=" \n "))
    assert plan["enterprise_skill_snapshot"]["skill_id"] == skill["id"]
    assert plan.get("company_context") is None


def test_company_changes_invalidate_proposal_context_fingerprint():
    session = source_session()
    original = context_fingerprint(session)
    session["plan_snapshot"]["company_context"] = COMPANY_CONTEXT
    supplied = context_fingerprint(session)
    assert supplied != original
    session["plan_snapshot"]["company_context"] += "另一个不同背景。"
    assert context_fingerprint(session) != supplied
