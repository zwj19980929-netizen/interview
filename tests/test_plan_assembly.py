import pytest
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.main import create_app
from app.repositories.memory import InMemoryStore
from app.repositories.provider import reset_store_for_tests
from app.services.catalog import CatalogService
from app.services.plan_assembly import (
    InterviewPlanAssembly,
    PlanAssemblyPolicy,
    PlanAssemblyRequest,
)
from app.services.roles import RoleRequirementService
from app.services.talent import TalentService


async def add_question(
    catalog: CatalogService,
    knowledge_base_id: str,
    *,
    title: str,
    skill: str,
    difficulty: str,
    key_points: list,
) -> dict:
    question = await catalog.create_question(
        knowledge_base_id,
        {
            "knowledge_base_id": knowledge_base_id,
            "title": title,
            "question_text": "%s：请解释方案与权衡。" % title,
            "standard_answer": "需要覆盖 %s，并解释边界与替代方案。" % "、".join(key_points),
            "key_points": key_points,
            "difficulty": difficulty,
            "skills": [skill],
            "rubric": {"semantic_correctness": 1.0},
        }
    )
    await catalog.process_speech_work(question["job_id"])
    return catalog.get_question(question["id"])


def create_scope(store: InMemoryStore, title: str, skills: list, duration: int) -> tuple:
    catalog = CatalogService(store)
    position = catalog.create_position({"code": title.lower().replace(" ", "_"), "name": title})
    knowledge_base = catalog.create_knowledge_base(position["id"], {"name": "%s 题库" % title})
    role = RoleRequirementService(store).create_role_requirement(
        {
            "job_position_id": position["id"],
            "title": title,
            "description": "负责 %s。" % "、".join(skills),
            "must_have_skills": skills,
            "nice_to_have_skills": [],
            "seniority": "senior",
            "interview_duration_minutes": duration,
        }
    )
    candidate = TalentService(store).create_candidate(
        {"name": "候选人", "email": "candidate@example.com", "phone": "13800138000"}
    )
    return catalog, position, knowledge_base, role, candidate


@pytest.mark.anyio
async def test_plan_assembly_balances_coverage_deduplication_curve_weights_and_time() -> None:
    store = InMemoryStore()
    catalog, position, knowledge_base, role, candidate = create_scope(
        store,
        "高级后端工程师",
        ["python", "database", "system_design"],
        40,
    )
    await add_question(
        catalog,
        knowledge_base["id"],
        title="Python 并发基础",
        skill="python",
        difficulty="mid",
        key_points=["线程模型", "并发边界"],
    )
    await add_question(
        catalog,
        knowledge_base["id"],
        title="Python 并发进阶",
        skill="python",
        difficulty="senior",
        key_points=["线程模型", "并发边界"],
    )
    await add_question(
        catalog,
        knowledge_base["id"],
        title="数据库索引",
        skill="database",
        difficulty="mid",
        key_points=["索引选择", "查询计划"],
    )
    await add_question(
        catalog,
        knowledge_base["id"],
        title="系统容量设计",
        skill="system_design",
        difficulty="senior",
        key_points=["容量估算", "故障降级"],
    )

    plan = await InterviewPlanAssembly(store).assemble(
        PlanAssemblyRequest(
            role_requirement_id=role["id"],
            job_position_id=position["id"],
            candidate_profile_id=candidate["id"],
            knowledge_base_ids=(knowledge_base["id"],),
            question_count=3,
            policy=PlanAssemblyPolicy(
                coverage=("python", "database", "system_design"),
                max_same_skill_questions=1,
                deduplication_threshold=0.6,
            ),
            approve=True,
        )
    )

    slots = plan["bank_slots"]
    assert "items" not in plan
    assert {item["dimension"] for item in slots} == {
        "python",
        "database",
        "system_design",
    }
    assert len({item["display_question_id"] for item in slots}) == 3
    assert slots[0]["dimension"] in {"python", "database"}
    assert slots[0]["selection_reason"].startswith("覆盖 ")
    assert "难度曲线:" in slots[0]["selection_reason"]
    assert round(sum(item["weight"] for item in slots), 4) == 1.0
    assert sum(item["expected_minutes"] for item in slots) == 40
    assert plan["assembly_summary"]["uncovered_dimensions"] == []
    assert plan["assembly_summary"]["selected_question_count"] == 3
    assert plan["assembly_policy"]["max_same_skill_questions"] == 1
    assert plan["status"] == "approved"
    assert plan["approved_at"]


@pytest.mark.anyio
async def test_plan_assembly_exposes_uncovered_dimensions_and_short_candidate_pool() -> None:
    store = InMemoryStore()
    catalog, position, knowledge_base, role, candidate = create_scope(
        store,
        "Python 工程师",
        ["python"],
        20,
    )
    await add_question(
        catalog,
        knowledge_base["id"],
        title="Python 运行时",
        skill="python",
        difficulty="mid",
        key_points=["解释器", "运行时"],
    )

    plan = await InterviewPlanAssembly(store).assemble(
        PlanAssemblyRequest(
            role_requirement_id=role["id"],
            job_position_id=position["id"],
            candidate_profile_id=candidate["id"],
            knowledge_base_ids=(knowledge_base["id"],),
            question_count=2,
            policy=PlanAssemblyPolicy(coverage=("python", "security")),
        )
    )

    assert len(plan["bank_slots"]) == 1
    assert "security" in plan["assembly_summary"]["uncovered_dimensions"]
    assert plan["assembly_summary"]["warnings"]
    assert plan["estimated_minutes"] == 20

    with pytest.raises(ApiError) as exc_info:
        await InterviewPlanAssembly(store).assemble(
            PlanAssemblyRequest(
                role_requirement_id=role["id"],
                job_position_id=position["id"],
                candidate_profile_id=candidate["id"],
                knowledge_base_ids=(knowledge_base["id"],),
                question_count=21,
            )
        )
    assert exc_info.value.code == "INTERVIEW_PLAN_POLICY_INVALID"


def test_plan_request_and_strategy_are_validated_before_assembly() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())

    invalid_count = api.post(
        "/api/v1/interview-plans/generate",
        json={"role_requirement_id": "role_missing", "question_count": 0},
    )
    invalid_strategy = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": "role_missing",
            "question_count": 2,
            "strategy": {"max_same_skill_questions": 0},
        },
    )

    assert invalid_count.status_code == 422
    assert invalid_strategy.status_code == 422


def test_role_profile_uses_declared_priorities_without_a_fixed_skill_catalog() -> None:
    role = RoleRequirementService(InMemoryStore()).create_role_requirement(
        {
            "title": "平台工程师",
            "description": "负责 Rust、Kafka 和可观测性平台。",
            "must_have_skills": ["Rust", "Kafka"],
            "nice_to_have_skills": ["Observability"],
            "seniority": "senior",
            "interview_duration_minutes": 45,
        }
    )

    weights = role["parsed_profile"]["skill_weights"]
    assert set(weights) == {"rust", "kafka", "observability"}
    assert weights["rust"] == weights["kafka"] > weights["observability"]
    assert round(sum(weights.values()), 4) == 1.0
