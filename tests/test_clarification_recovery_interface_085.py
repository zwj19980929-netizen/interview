"""Actual ingress snapshots distinguish invalid model results from provider blocks."""

from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal, UnderstandingProblem
from test_answer_preparation_projection import _snapshot
from test_automatic_turn_integration import _automatic_session, _current
from test_evidence_owner_recovery_integration import TURN_ID


@pytest.mark.anyio
@pytest.mark.parametrize("source,reason,retryable", [
    ("schema_or_content", "unresolved_clarification_source_invalid", True),
    ("schema_or_content", "unresolved_answer_contract_invalid", True),
    ("provider", "provider_auth_failed", False),
    ("provider", "provider_billing_required", False),
])
async def test_terminal_understanding_failure_has_safe_scoped_snapshot_and_real_retry_gate(
    tmp_path, monkeypatch, source, reason, retryable,
):
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
        endpoint, capture, chain = managed._answer_endpoint, managed._capture_id, managed.chain
        # Exercise real failure accounting and the ingress notification seam;
        # do not mock the public recovery projection or retry implementation.
        endpoint._use_preparation_budget("合成待处理证据。")
        problem = UnderstandingProblem(
            code="UNDERSTANDING_RESULT_REJECTED" if source == "schema_or_content" else "UNDERSTANDING_PROVIDER_UNAVAILABLE",
            source=source, recoverable=True, action="clarify", retryable=False,
            reason_code=reason, attempts=2 if source == "schema_or_content" else 1,
        )
        endpoint._record_preparation_failure(problem=problem)
        await endpoint._understanding_failed(record_failure=False)
        expected = {"status": "retry_required" if retryable else "blocked", "turn_id": TURN_ID,
                    "capture_id": capture, "stage": "answer_understanding",
                    "reason_code": "invalid_result" if retryable else reason,
                    "attempt": 1, "max_attempts": 2, "retryable": retryable}
        assert _snapshot(runtime, channel)["answer_preparation"] == expected
        assert _current(runtime)["agent_runtime"]["answer_preparation"] == expected
        events = [event for event in channel._queue._queue if getattr(event, "type", None) == "problem"]
        assert events and events[-1].payload["action"] == ("retry_answer_processing" if retryable else "contact_support")
        assert events[-1].payload["capture_id"] == capture and events[-1].turn_id == TURN_ID
        assert events[-1].payload["reason_code"] == expected["reason_code"]
        assert "合成待处理证据" not in str(events[-1].payload)
        if retryable:
            assert reason not in str(events[-1].payload)
        assert endpoint.preparation_blocked
        assert endpoint._terminal_provider_failure is not retryable

        stale = deepcopy(_current(runtime))
        stale["current_turn_id"] = "other_turn"
        assert runtime._snapshot(stale, channel.principal)["answer_preparation"] is None
        signal = ClientSignal(type="retry_answer_processing", idempotency_key="clarification_recovery_085",
                              turn_id=TURN_ID, payload={"capture_id": capture})
        if retryable:
            await managed.dispatch(channel, signal)
            assert not endpoint.preparation_blocked
        else:
            with pytest.raises(ApiError) as caught:
                await managed.dispatch(channel, signal)
            assert caught.value.code == "ANSWER_PROCESSING_RECOVERY_BLOCKED"
            assert endpoint.preparation_blocked
        assert managed.chain is chain and managed._capture_id == capture and chain.is_open
        assert not _current(runtime)["answers"]
