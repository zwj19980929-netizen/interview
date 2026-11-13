"""The active supervisor must reach its real mock dispatch contract."""
from types import SimpleNamespace

import pytest

from app.core.prompt.interviewer_supervisor import supervisor_contract, SUPERVISOR_VERSION
from app.core.prompt.validation import validate_structured_response
from app.model_gateway.capabilities import LLM_CHAT_JSON
from app.model_gateway.schemas import ChatJSONRequest
from app.providers.mock.provider import MockProvider


@pytest.mark.anyio
async def test_current_supervisor_dispatch_does_not_fall_through_to_understanding():
    contract = supervisor_contract({"eligible_questions": [{"question_id": "q_synthetic"}]}, [],
        tools=["interview.read_context"], question_ids=["q_synthetic"])
    request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages,
        json_schema=contract.response_schema, metadata={"prompt_version": SUPERVISOR_VERSION})
    response = await MockProvider().invoke(LLM_CHAT_JSON, request, SimpleNamespace(model="synthetic_dispatch"))
    validate_structured_response(response.data, contract.response_schema)
    assert contract.version == "interviewer_supervisor.v4"
    assert response.data["action"] == "select_question" and response.data["question_id"] == "q_synthetic"
    assert "understanding" not in response.data and "intent" not in response.data
