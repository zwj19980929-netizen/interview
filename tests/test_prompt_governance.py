import ast
from pathlib import Path

import pytest

from app.core.prompt.contracts import prompt_contract, structured_output_instruction
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.core.prompt.understanding_references import understanding_references


def test_understanding_v2_uses_exact_reference_catalog_and_preserves_long_transcripts():
    transcript = "嗯 FastAPI / Redis，readiness 200。" + "待" * 501 + "\n结束！"
    context = {"transcript": transcript, "capability_points": ["幂等键", "健康检查"]}
    references = understanding_references(transcript, context["capability_points"])
    assert all(text in transcript and len(text) <= 240 for text in references["evidence"].values())
    assert sum(text.count("待") for text in references["evidence"].values()) == 501
    contract = prompt_contract("interview_turn_understanding", context)
    assert contract.version == "interview_turn_understanding.v8"
    assert "evidence_quotes" not in contract.response_schema["properties"]
    assert contract.response_schema["properties"]["evidence_ids"]["items"]["enum"] == list(references["evidence"])
    assert contract.response_schema["properties"]["covered_point_ids"]["items"]["enum"] == ["P1", "P2"]
    assert "互不相交" in contract.messages[0].content


def test_question_generation_prompt_and_response_contract_are_centralized() -> None:
    contract = prompt_contract(
        "question_generation",
        {
            "target_count": 2,
            "position_name": "后端工程师",
            "knowledge_base_name": "Python 题库",
            "positioning": "考察生产问题定位",
            "tags": ["Python", "数据库"],
            "requirements": "包含一道场景题",
        },
    )

    assert contract.version == "question_generation.v2"
    assert "后端工程师" in contract.messages[1].content
    assert "包含一道场景题" in contract.messages[1].content
    assert contract.response_schema["properties"]["questions"]["maxItems"] == 2
    instruction = structured_output_instruction(contract.response_schema)
    assert "JSON Schema" in instruction
    assert "markdown" in instruction


def test_question_generation_blueprint_contracts_fix_slot_count_and_shape() -> None:
    planning = prompt_contract(
        "question_blueprint_planning",
        {
            "target_count": 3,
            "positioning": "生产故障分析",
            "tags": ["数据库"],
            "existing_questions": [{"title": "慢查询", "question": "如何定位慢查询"}],
        },
    )
    generation = prompt_contract(
        "question_blueprint_generation",
        {
            "blueprints": [
                {
                    "slot_id": "slot_01",
                    "topic": "数据库",
                    "scenario": "连接池耗尽",
                    "focus": ["容量判断"],
                    "difficulty": "mid",
                    "question_type": "open_ended",
                }
            ],
            "excluded_questions": [],
        },
    )

    blueprint_items = planning.response_schema["properties"]["blueprints"]
    assert planning.version == "question_blueprint_planning.v1"
    assert blueprint_items["minItems"] == blueprint_items["maxItems"] == 3
    assert "已有题目摘要" in planning.messages[1].content
    assert generation.version == "question_blueprint_generation.v2"
    question_items = generation.response_schema["properties"]["questions"]
    assert question_items["minItems"] == question_items["maxItems"] == 1
    assert "slot_id" in question_items["items"]["required"]
    properties = question_items["items"]["properties"]
    assert properties["title"]["maxLength"] == 80
    assert properties["question_text"]["maxLength"] == 600
    assert properties["standard_answer"]["maxLength"] == 1800
    assert properties["key_points"]["maxItems"] == 6
    assert properties["key_points"]["items"]["properties"]["aliases"]["maxItems"] == 4
    assert properties["skills"]["maxItems"] == 8
    assert "标准答案不超过1800字" in generation.messages[1].content

    with pytest.raises(StructuredResponseValidationError, match="too few items"):
        validate_structured_response({"blueprints": []}, planning.response_schema)


def test_structured_response_rejects_blank_required_content() -> None:
    contract = prompt_contract("question_generation", {"target_count": 1})
    invalid = {
        "questions": [{
            "title": "   ",
            "question_text": "题干",
            "standard_answer": "答案",
            "key_points": [{"text": "关键点", "weight": 1}],
            "skills": ["Python"],
            "difficulty": "mid",
            "type": "open_ended",
        }]
    }

    with pytest.raises(StructuredResponseValidationError, match=r"\$\.questions\[0\]\.title"):
        validate_structured_response(invalid, contract.response_schema)


def test_resume_review_contract_contains_explainable_screening_and_protected_attribute_rule() -> None:
    contract = prompt_contract(
        "resume_review",
        {"position_name": "后端工程师", "role_description": "必须掌握 Python", "sanitized_resume": "Python 项目"},
    )
    screening = contract.response_schema["properties"]["screening"]
    assert contract.version == "resume_review.v6"
    assert screening["properties"]["recommendation"]["enum"] == ["qualified", "unqualified", "manual_review"]
    assert {"matched_requirements", "unmet_requirements"}.issubset(screening["required"])
    assert "受保护属性" in contract.messages[0].content
    assert "0到59分 recommendation=unqualified" in contract.messages[0].content
    assert "75到100分 recommendation=qualified" in contract.messages[0].content
    schema = contract.response_schema["properties"]
    assert schema["summary"]["maxLength"] == 320
    assert schema["project_evidence"]["maxItems"] == 4
    assert schema["skill_evidence"]["maxItems"] == 6
    assert "experience_questions" not in schema
    assert "不生成面试问题" in contract.messages[0].content
    assert "输出必须精炼" in contract.messages[0].content


def test_long_resume_map_reduce_contracts_separate_evidence_from_screening() -> None:
    mapped = prompt_contract(
        "resume_evidence_map",
        {
            "position_name": "后端工程师",
            "role_description": "必须掌握 Python",
            "must_have_skills": ["python"],
            "page_start": 2,
            "page_end": 3,
            "chunk_text": "负责 Python 服务",
        },
    )
    compacted = prompt_contract(
        "resume_evidence_compaction",
        {
            "role_description": "必须掌握 Python",
            "must_have_skills": ["python"],
            "evidence_json": "{}",
        },
    )
    reduced = prompt_contract(
        "resume_review_reduce",
        {
            "position_name": "后端工程师",
            "role_description": "必须掌握 Python",
            "must_have_skills": ["python"],
            "evidence_json": "{}",
        },
    )

    assert mapped.version == "resume_evidence_map.v1"
    assert "不做岗位符合性" in mapped.messages[0].content
    assert "screening" not in mapped.response_schema["properties"]
    assert compacted.version == "resume_evidence_compaction.v1"
    compact_item = compacted.response_schema["properties"]["project_evidence"]["items"]
    assert "source_pages" in compact_item["required"]
    assert reduced.version == "resume_review_reduce.v4"
    assert "screening" in reduced.response_schema["properties"]
    assert "60到74分 recommendation=manual_review" in reduced.messages[0].content
    reduced_evidence = reduced.response_schema["properties"]["project_evidence"]["items"]
    assert "source_pages" in reduced_evidence["required"]


def test_resume_question_generation_requires_explicit_resume_evidence_labels() -> None:
    contract = prompt_contract(
        "resume_experience_question_generation",
        {
            "position_name": "后端工程师",
            "evidence_json": '[{"label":"订单平台","evidence":"负责Python服务性能优化"}]',
        },
    )
    questions = contract.response_schema["properties"]["questions"]
    evidence_refs = questions["items"]["properties"]["evidence_refs"]
    assert contract.version == "resume_experience_question_generation.v1"
    assert questions["minItems"] == 1
    assert questions["maxItems"] == 3
    assert evidence_refs["minItems"] == 1
    assert evidence_refs["uniqueItems"] is True
    assert "禁止引入证据中没有出现的技术" in contract.messages[0].content


@pytest.mark.parametrize(
    "value,error_path",
    [
        ({"message": "not-pong"}, "$.message"),
        ({"message": "pong", "unexpected": True}, "$"),
        ({}, "$"),
    ],
)
def test_json_probe_contract_rejects_wrong_content_shape(value, error_path) -> None:
    schema = prompt_contract("json_probe", {}).response_schema
    with pytest.raises(StructuredResponseValidationError, match=error_path.replace("$", r"\$")):
        validate_structured_response(value, schema)


def test_question_generation_contract_rejects_more_than_requested() -> None:
    contract = prompt_contract("question_generation", {"target_count": 1})
    with pytest.raises(StructuredResponseValidationError, match="too many items"):
        validate_structured_response({"questions": [{}, {}]}, contract.response_schema)


def test_chat_message_prompt_text_cannot_be_added_outside_prompt_package() -> None:
    root = Path(__file__).parents[1] / "app"
    violations = []
    for path in root.rglob("*.py"):
        if "core/prompt" in path.as_posix():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChatMessage":
                violations.append(str(path.relative_to(root.parent)))
    assert violations == [], "Move all ChatMessage Prompt construction to app/core/prompt: %s" % violations
