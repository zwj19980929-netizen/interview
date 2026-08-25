import pytest
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.main import create_app
from app.repositories.memory import InMemoryStore
from app.repositories.provider import reset_store_for_tests
from app.services.plan_assembly import (
    InterviewPlanAssembly,
    PlanAssemblyPolicy,
    PlanAssemblyRequest,
)
from app.services.questions import QuestionService
from app.services.roles import RoleRequirementService


async def add_question(
    questions: QuestionService,
    *,
    title: str,
    skill: str,
    difficulty: str,
    key_points: list,
) -> dict:
    return await questions.create_question(
        {
            "knowledge_base_id": "kb_plan",
            "title": title,
            "question_text": "%s：请解释方案与权衡。" % title,
            "standard_answer": "需要覆盖 %s，并解释边界与替代方案。" % "、".join(key_points),
            "key_points": key_points,
            "difficulty": difficulty,
            "skills": [skill],
        }
    )


@pytest.mark.anyio
async def test_plan_assembly_balances_coverage_deduplication_curve_weights_and_time() -> None:
    store = InMemoryStore()
    roles = RoleRequirementService(store)
    questions = QuestionService(store)
    role = roles.create_role_requirement(
        {
            "title": "高级后端工程师",
            "description": "负责 Python、数据库和系统设计。",
            "must_have_skills": ["python", "database", "system_design"],
            "nice_to_have_skills": [],
            "seniority": "senior",
            "interview_duration_minutes": 40,
        }
    )
    await add_question(
        questions,
        title="Python 并发基础",
        skill="python",
        difficulty="mid",
        key_points=["线程模型", "并发边界"],
    )
    await add_question(
        questions,
        title="Python 并发进阶",
        skill="python",
        difficulty="senior",
        key_points=["线程模型", "并发边界"],
    )
    await add_question(
        questions,
        title="数据库索引",
        skill="database",
        difficulty="mid",
        key_points=["索引选择", "查询计划"],
    )
    await add_question(
        questions,
        title="系统容量设计",
        skill="system_design",
        difficulty="senior",
        key_points=["容量估算", "故障降级"],
    )

    plan = await InterviewPlanAssembly(store).assemble(
        PlanAssemblyRequest(
            role_requirement_id=role["id"],
            knowledge_base_ids=("kb_plan",),
            question_count=3,
            policy=PlanAssemblyPolicy(
                coverage=("python", "database", "system_design"),
                max_same_skill_questions=1,
                deduplication_threshold=0.6,
            ),
        )
    )

    assert {item["dimension"] for item in plan["items"]} == {
        "python",
        "database",
        "system_design",
    }
    assert len({item["question_id"] for item in plan["items"]}) == 3
    assert plan["items"][0]["dimension"] in {"python", "database"}
    assert plan["items"][0]["selection_reason"].startswith("覆盖 ")
    assert "难度曲线:" in plan["items"][0]["selection_reason"]
    assert round(sum(item["weight"] for item in plan["items"]), 4) == 1.0
    assert sum(item["expected_minutes"] for item in plan["items"]) == 40
    assert plan["assembly_summary"]["uncovered_dimensions"] == []
    assert plan["assembly_summary"]["selected_question_count"] == 3
    assert plan["assembly_policy"]["max_same_skill_questions"] == 1


@pytest.mark.anyio
async def test_plan_assembly_exposes_uncovered_dimensions_and_short_candidate_pool() -> None:
    store = InMemoryStore()
    roles = RoleRequirementService(store)
    questions = QuestionService(store)
    role = roles.create_role_requirement(
        {
            "title": "Python 工程师",
            "description": "负责 Python 服务。",
            "must_have_skills": ["python"],
            "nice_to_have_skills": [],
            "seniority": "mid",
            "interview_duration_minutes": 20,
        }
    )
    await add_question(
        questions,
        title="Python 运行时",
        skill="python",
        difficulty="mid",
        key_points=["解释器", "运行时"],
    )

    plan = await InterviewPlanAssembly(store).assemble(
        PlanAssemblyRequest(
            role_requirement_id=role["id"],
            knowledge_base_ids=("kb_plan",),
            question_count=2,
            policy=PlanAssemblyPolicy(coverage=("python", "security")),
        )
    )

    assert len(plan["items"]) == 1
    assert "security" in plan["assembly_summary"]["uncovered_dimensions"]
    assert plan["assembly_summary"]["warnings"]
    assert plan["estimated_minutes"] == 20

    with pytest.raises(ApiError) as exc_info:
        await InterviewPlanAssembly(store).assemble(
            PlanAssemblyRequest(
                role_requirement_id=role["id"],
                knowledge_base_ids=("kb_plan",),
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
