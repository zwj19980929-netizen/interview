"""Source coverage diagnostics precede paid adaptation and preserve score scope."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import plans as plan_routes
from app.core.errors import ApiError, api_error_handler
from app.core.prompt.inquiry_units import mock_inquiry_units_result
from app.core.prompt.validation import validate_structured_response
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyPolicy, PlanAssemblyRequest
from tests.test_plan_assembly import create_scope, add_question


class UnitGateway:
    """Only the test's joint knowledge points map to both approved competencies."""
    def __init__(self, narrow=False):
        self.calls = []
        self.narrow = narrow

    async def invoke(self, capability, request):
        self.calls.append(request)
        assert request.metadata["prompt_version"] == "interview_inquiry_units.v2"
        data = mock_inquiry_units_result(request)
        sources = {item["id"]: item for item in json.loads(request.messages[-1].content)["questions"]}
        for row in data["questions"]:
            mapping = sources[row["question_id"]]["competency_ids"]
            for unit in row["units"]:
                unit["competency_ids"] = mapping[:1] if self.narrow else mapping
        validate_structured_response(data, request.json_schema)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="synthetic", request_id="fixture", latency_ms=0))


async def scope():
    store = InMemoryStore()
    catalog, position, bank, role, candidate = create_scope(store, "覆盖诊断", ["python", "flask", "redis"], 45)
    for skill in ("python", "redis"):
        await add_question(catalog, bank["id"], title=f"{skill} 的处理边界", skill=skill, difficulty="mid",
                           key_points=[f"{skill} 的调用边界", f"{skill} 的故障处理"])
    assembly = InterviewPlanAssembly(store)
    assembly.catalog.gateway = UnitGateway()
    request = PlanAssemblyRequest(role_requirement_id=role["id"], job_position_id=position["id"],
        candidate_profile_id=candidate["id"], knowledge_base_ids=(bank["id"],), question_count=6,
        execution_schema_version=3, policy=PlanAssemblyPolicy(coverage=("python", "系统设计")),
        adaptive_policy={"min_root_questions": 3, "max_root_questions": 6, "min_evidence_units_per_competency": 2})
    return store, catalog, assembly, request, bank


async def add_missing_sources(catalog, bank_id):
    # Both points in each source deliberately concern the joint boundary,
    # so a unit may legitimately evidence both of its approved source skills.
    for skills, points in [(["python", "flask"], ["Python Flask 请求上下文边界", "Python Flask 请求异常处理"]),
                          (["redis", "系统设计"], ["Redis 缓存的系统设计边界", "Redis 故障时的系统设计取舍"])]:
        question = await catalog.create_question(bank_id, {"knowledge_base_id": bank_id,
            "title": points[0], "question_text": "请解释%s的方案。" % "、".join(points),
            "standard_answer": "需要说明%s。" % "、".join(points), "key_points": points,
            "difficulty": "mid", "skills": skills, "rubric": {"semantic_correctness": 1.0}})
        await catalog.process_speech_work(question["job_id"])


@pytest.mark.anyio
@pytest.mark.parametrize("coverage,missing,requested", [
    (("python", "系统设计"), ["flask", "系统设计"], ["系统设计"]),
    (("python",), ["flask"], []),
    ((" FLASK ", "系统设计"), ["flask", "系统设计"], ["flask", "系统设计"]),
])
async def test_missing_sources_report_role_and_requested_gaps_without_model_or_plan_writes(coverage, missing, requested):
    store, _, assembly, request, _ = await scope()
    before_questions, before_roles = deepcopy(store.questions), deepcopy(store.role_requirements)
    with pytest.raises(ApiError) as raised:
        await assembly.assemble(replace(request, policy=PlanAssemblyPolicy(coverage=coverage)))
    error = raised.value
    assert error.code == "ASSESSMENT_SOURCE_COVERAGE_MISSING"
    assert error.status_code == 422
    assert error.details == {"missing_competency_ids": missing, "missing_role_competency_ids": ["flask"],
                             "missing_requested_competency_ids": requested}
    assert "当前可用题池" in error.message
    assert all(name in error.message for name in missing)
    assert assembly.catalog.gateway.calls == []
    assert store.interview_plans == {}
    assert store.questions == before_questions
    assert store.role_requirements == before_roles


@pytest.mark.anyio
async def test_adding_both_real_source_mappings_allows_the_same_request_to_generate_a_valid_draft():
    store, catalog, assembly, request, bank = await scope()
    with pytest.raises(ApiError, match="当前可用题池"):
        await assembly.assemble(request)
    await add_missing_sources(catalog, bank["id"])
    plan = await assembly.assemble(request)
    assert plan["status"] == "draft"
    assert plan["execution_schema_version"] == 3
    assert assembly.catalog.gateway.calls
    assert set(item["id"] for item in plan["assessment_contract"]["competencies"]) == {"python", "flask", "redis", "系统设计"}
    assert all(item["min_evidence_roots"] == 2 for item in plan["assessment_contract"]["competencies"])
    assert plan["assessment_contract"]["budget"]["max_root_questions"] == 6
    assert list(store.interview_plans) == [plan["id"]]


@pytest.mark.anyio
async def test_sources_in_an_unselected_bank_do_not_mask_the_scoped_pool_gap():
    store, catalog, assembly, request, _ = await scope()
    other_bank = catalog.create_knowledge_base(request.job_position_id, {"name": "未选中的题库"})
    await add_missing_sources(catalog, other_bank["id"])
    with pytest.raises(ApiError) as raised:
        await assembly.assemble(request)
    assert raised.value.details["missing_competency_ids"] == ["flask", "系统设计"]
    assert "当前可用题池" in raised.value.message
    assert assembly.catalog.gateway.calls == []
    assert store.interview_plans == {}


@pytest.mark.anyio
async def test_source_preflight_does_not_weaken_final_unit_coverage_validation():
    store, catalog, assembly, request, bank = await scope()
    await add_missing_sources(catalog, bank["id"])
    assembly.catalog.gateway = UnitGateway(narrow=True)
    with pytest.raises(ApiError) as raised:
        await assembly.assemble(request)
    assert raised.value.code == "INQUIRY_COMPETENCY_COVERAGE_MISSING"
    assert raised.value.details["missing_competency_ids"] == ["flask"]
    assert assembly.catalog.gateway.calls
    assert store.interview_plans == {}


@pytest.mark.anyio
async def test_legacy_v2_keeps_its_existing_partial_coverage_behavior():
    store, _, assembly, request, _ = await scope()
    legacy = await assembly.assemble(replace(request, execution_schema_version=2, adaptive_policy=None))
    assert legacy["execution_schema_version"] == 2
    assert {"flask", "系统设计"}.issubset(legacy["assembly_summary"]["uncovered_dimensions"])
    assert "assessment_contract" not in legacy
    assert assembly.catalog.gateway.calls == []
    assert list(store.interview_plans) == [legacy["id"]]


@pytest.mark.anyio
async def test_generate_http_returns_422_structured_coverage_diagnostics_and_retries_do_not_write(monkeypatch):
    store, _, assembly, request, _ = await scope()
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(plan_routes.router)
    monkeypatch.setattr(plan_routes, "services", lambda: {"plan_assembly": assembly})
    payload = {"role_requirement_id": request.role_requirement_id, "job_position_id": request.job_position_id,
        "candidate_profile_id": request.candidate_profile_id, "knowledge_base_ids": list(request.knowledge_base_ids),
        "question_count": 6, "execution_schema_version": 3, "adaptive_policy": request.adaptive_policy,
        "strategy": {"coverage": ["python", "系统设计"]}, "approve": False, "skill_id": None, "company_context": ""}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        for _ in range(2):
            result = await api.post("/api/v1/interview-plans/generate", json=payload)
            assert result.status_code == 422
            assert result.json()["error"]["code"] == "ASSESSMENT_SOURCE_COVERAGE_MISSING"
            assert result.json()["error"]["details"] == {"missing_competency_ids": ["flask", "系统设计"],
                "missing_role_competency_ids": ["flask"], "missing_requested_competency_ids": ["系统设计"]}
            assert "flask、系统设计" in result.json()["error"]["message"]
    assert assembly.catalog.gateway.calls == []
    assert store.interview_plans == {}
