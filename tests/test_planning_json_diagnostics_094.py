"""Parse failures are observable without weakening the structured contract."""

import json
from copy import deepcopy

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.persistence.provider import persistence_for
from app.providers.openai_compatible.provider import OpenAICompatibleProvider
from app.repositories.sqlite import SQLiteStore
from tests.test_model_invocation import ScriptedAdapter, configured_store, request, route
from test_interviewer_supervisor import decision, source_session, supervisor
from app.services.interviewer_supervisor.tools import InterviewerTools


def _gateway(content, *, finish_reason="stop", backend="memory", tmp_path=None, status=200,
             content_sequence=None):
    store = configured_store()
    store.provider_connections["provider_conn_primary"]["connection_config"] = {
        "base_url": "https://models.example.test/v1",
    }
    if backend == "sqlite":
        memory = store
        store = SQLiteStore(str(tmp_path / "diagnostics.sqlite3"))
        for name in ("provider_connections", "model_configurations"):
            getattr(store, name).update(getattr(memory, name))
            store.save_many(name, list(getattr(memory, name).values()))
        store.save_provider_secret("provider_conn_primary", {"api_key": "never-logged"})
    calls = []

    def handler(http_request):
        calls.append(http_request)
        body = content_sequence[len(calls) - 1] if content_sequence is not None else content
        return httpx.Response(status, json={
            "choices": [{"message": {"content": body}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        })

    transport = httpx.MockTransport(handler)
    adapter = OpenAICompatibleProvider(
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )
    return ModelGateway(store, provider_clients={"openai_compatible": adapter}), store, calls


async def _invoke(gateway):
    return await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route(fallback=False))


@pytest.mark.anyio
@pytest.mark.parametrize("content", [None, "", " \n\t ", False, 42, [], {"private": "unusable"}])
async def test_empty_or_nontext_content_is_classified_before_contract_validation(content):
    gateway, store, calls = _gateway(content)
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    error = caught.value
    assert error.code == "provider_schema_invalid" and error.retryable
    assert error.details["json_failure_kind"] == "empty_content"
    assert len(calls) == 1
    row, = store.model_invocations
    assert row["status"] == "failed" and row["json_failure_kind"] == "empty_content"
    assert row["schema_reason"] is None
    assert row["content_length"] == (len(content) if isinstance(content, str) else 0)
    assert not store.interviews


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("content", ["private incomplete response", '{"score":', "[]", '{} trailing'])
async def test_invalid_object_classification_survives_gateway_and_storage(content, backend, tmp_path):
    gateway, store, calls = _gateway(content, backend=backend, tmp_path=tmp_path)
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    error = caught.value
    assert error.code == "provider_schema_invalid" and error.retryable
    assert error.details["json_failure_kind"] == "invalid_json_object"
    assert len(calls) == 1
    if backend == "sqlite":
        store = SQLiteStore(str(tmp_path / "diagnostics.sqlite3"))
    with persistence_for(store).transaction("org_default", read_only=True) as transaction:
        rows = transaction.model_invocations.list()
    row, = rows
    assert row["status"] == "failed" and row["json_failure_kind"] == "invalid_json_object"
    assert row["content_length"] == len(content)
    assert row["schema_reason"] is None
    assert row["input_tokens"] == 12 and row["output_tokens"] == 6
    serialized = json.dumps(rows)
    for sensitive in ("private incomplete response", "sensitive candidate answer", "never-logged"):
        assert sensitive not in serialized
    assert not store.interviews


@pytest.mark.anyio
@pytest.mark.parametrize("content", ['{}', '{"score":101,"summary":"private response"}',
                                     '{"score":"80","summary":"private response"}'])
async def test_valid_json_contract_rejection_is_not_reclassified_as_parse_failure(content):
    gateway, store, calls = _gateway(content)
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    assert caught.value.code == "provider_schema_invalid"
    assert "json_failure_kind" not in caught.value.details
    assert caught.value.details["schema_reason"] in {"required", "maximum", "type"}
    row, = store.model_invocations
    assert row.get("json_failure_kind") is None
    assert row["schema_reason"] == caught.value.details["schema_reason"]
    assert "private response" not in json.dumps(row)
    assert len(calls) == 1 and not store.interviews


@pytest.mark.anyio
@pytest.mark.parametrize("content", [None, '{"score":'])
async def test_length_truncation_keeps_its_existing_nonretryable_classification(content):
    gateway, store, calls = _gateway(content, finish_reason="length")
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    assert caught.value.code == "provider_output_truncated" and not caught.value.retryable
    assert "json_failure_kind" not in caught.value.details
    assert store.model_invocations[0].get("json_failure_kind") is None
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [(401, "provider_auth_failed"), (400, "provider_bad_request"),
                                       (429, "provider_rate_limited"), (500, "provider_server_error")])
async def test_http_failures_are_not_mislabeled_as_json_failure(status, code):
    gateway, store, calls = _gateway(None, status=status)
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    assert caught.value.code == code
    assert "json_failure_kind" not in caught.value.details
    assert store.model_invocations[0].get("json_failure_kind") is None
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind,length,expected_kind,expected_length", [
    ("empty_content", 0, "empty_content", 0),
    ("invalid_json_object", 10_000_000, "invalid_json_object", 10_000_000),
    ("private candidate response", "private candidate response", None, None),
    (["private candidate response"], -1, None, None),
    ({"private": "candidate response"}, True, None, None),
    (42, 1.5, None, None),
    (None, 10_000_001, None, None),
])
async def test_gateway_audit_whitelists_diagnostic_values(kind, length, expected_kind, expected_length):
    store = configured_store()
    error = ProviderError("provider_schema_invalid", "private exception message", retryable=True, details={
        "json_failure_kind": kind, "content_length": length,
        "raw_content": "private candidate response", "exception": "private exception message",
    })
    gateway = ModelGateway(store, provider_clients={"openai_compatible": ScriptedAdapter([error])})
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    assert caught.value is error
    row, = store.model_invocations
    assert row["json_failure_kind"] == expected_kind
    assert row["content_length"] == expected_length
    for sensitive in ("private candidate response", "private exception message", "never-logged"):
        assert sensitive not in json.dumps(row)


@pytest.mark.anyio
@pytest.mark.parametrize("content", ['{"score":80,"summary":"ok"}',
                                     '<think>reason</think>\n```json\n{"score":80,"summary":"ok"}\n```'])
async def test_accepted_complete_object_and_wrapper_behavior_is_unchanged(content):
    gateway, store, calls = _gateway(content)
    response = await _invoke(gateway)
    assert response.data == {"score": 80, "summary": "ok"}
    row, = store.model_invocations
    assert row["status"] == "success" and row.get("json_failure_kind") is None
    assert len(calls) == 1


def _planning_pipeline(contents):
    gateway, store, calls = _gateway(None, content_sequence=contents)
    # A large route retry policy cannot add hidden adapter attempts to the
    # supervisor's existing per-call execution budget.
    planning_route = route(retry_count=4, timeout_s=30, fallback=False)
    planning_route["purpose"] = "interview_turn_understanding"
    store.model_routes[planning_route["id"]] = planning_route
    return supervisor(gateway, store), store, calls


@pytest.mark.anyio
async def test_real_adapter_gateway_supervisor_recovers_one_invalid_json_call():
    planner, store, calls = _planning_pipeline([
        '{"question_id":"private-invalid-output",', json.dumps(decision("q2")),
    ])
    session = source_session()
    original = deepcopy(session)
    result = await planner.propose_next(session)
    assert result.question_id == "q2" and result.model_calls == 2
    assert result.selection_strategy is None and result.fallback_reason is None
    assert len(calls) == 2 and session == original and not store.interviews
    failed, succeeded = store.model_invocations
    assert failed["json_failure_kind"] == "invalid_json_object" and failed["status"] == "failed"
    assert succeeded["status"] == "success" and succeeded["json_failure_kind"] is None
    assert failed["attempt"] == succeeded["attempt"] == 1
    assert failed["invocation_id"] != succeeded["invocation_id"]
    assert all(row["prompt_version"] == "interviewer_supervisor.v4" for row in store.model_invocations)
    # No malformed output is fed back into a retry prompt or result/audit.
    assert "private-invalid-output" not in calls[1].content.decode()
    assert "private-invalid-output" not in json.dumps(store.model_invocations)
    assert "private-invalid-output" not in repr(result)


@pytest.mark.anyio
async def test_real_adapter_gateway_supervisor_fallback_is_independent_of_two_invalid_outputs():
    planner, store, calls = _planning_pipeline(['{"question_id":"foreign",', None])
    session = source_session()
    original = deepcopy(session)
    result = await planner.propose_next(session)
    assert result.action == "select_question" and result.model_calls == 2
    assert result.selection_strategy == "approved_catalog_fallback.v1"
    assert result.fallback_reason == "provider_schema_invalid"
    InterviewerTools(session).require_selection(result.question_id, result.inquiry_unit_id)
    assert result.question_id in {"q1", "q2", "q3"}
    assert len(calls) == 2 and session == original and not store.interviews
    assert [row["json_failure_kind"] for row in store.model_invocations] == [
        "invalid_json_object", "empty_content",
    ]
    assert all(row["status"] == "failed" and row["attempt"] == 1 for row in store.model_invocations)
    assert "foreign" not in repr(result)


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", [decision(action="delete_session"), decision(extra="private-extra"),
                                    {"action": "select_question"}])
async def test_real_adapter_gateway_supervisor_keeps_central_contract_failure_closed(invalid):
    planner, store, calls = _planning_pipeline([json.dumps(invalid), json.dumps(decision("q2"))])
    session = source_session()
    original = deepcopy(session)
    with pytest.raises(ApiError) as caught:
        await planner.propose_next(session)
    assert caught.value.code == "INTERVIEW_DECISION_UNAVAILABLE"
    assert len(calls) == 1 and session == original and not store.interviews
    failed, = store.model_invocations
    assert failed["status"] == "failed" and failed["error_code"] == "provider_schema_invalid"
    assert failed["json_failure_kind"] is None
    assert failed["schema_reason"] in {"enum", "additional_properties", "required"}
    assert "private-extra" not in json.dumps(failed)


@pytest.mark.anyio
async def test_real_adapter_gateway_supervisor_rejects_parsed_foreign_domain_reference():
    planner, store, calls = _planning_pipeline([json.dumps(decision("foreign")), json.dumps(decision("q2"))])
    session = source_session()
    original = deepcopy(session)
    with pytest.raises(ApiError) as caught:
        await planner.propose_next(session)
    assert caught.value.code == "INTERVIEW_DECISION_UNAVAILABLE"
    assert len(calls) == 1 and session == original and not store.interviews
    row, = store.model_invocations
    # The wire contract accepts a string; the supervisor's approved catalog
    # rejects this reference after gateway validation and cannot fall back.
    assert row["status"] == "success" and row["json_failure_kind"] is None
