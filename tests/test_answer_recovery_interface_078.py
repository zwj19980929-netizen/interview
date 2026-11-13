"""Retained-answer recovery is independent of renewed candidate speaking."""
import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.persistence.provider import persistence_for
from app.services.answer_preparation_recovery import public_preparation_recovery
from test_automatic_turn_integration import _automatic_session, _current
from test_answer_preparation_projection import _snapshot
from test_evidence_owner_recovery_integration import ORGANIZATION_ID, TURN_ID

DETAILS = {"stage": "answer_understanding", "reason_code": "provider_timeout",
           "attempt": 2, "max_attempts": 2, "retryable": True}


def test_answer_recovery_command_preserves_confirmation_and_reuses_current_capture(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            endpoint, capture, chain = managed._answer_endpoint, managed._capture_id, managed.chain
            retry = Mock()
            continue_speaking = Mock()
            monkeypatch.setattr(endpoint, "retry_answer_processing", retry)
            monkeypatch.setattr(endpoint, "continue_speaking", continue_speaking)
            monkeypatch.setattr(type(endpoint), "preparation_recovery_details", property(lambda self: DETAILS))
            await endpoint._notify("understanding_retry_exhausted")
            signal = ClientSignal(type="retry_answer_processing", idempotency_key="recovery_078", turn_id=TURN_ID,
                                  payload={"capture_id": capture})
            await channel.send(signal)
            await channel.send(signal)
            retry.assert_called_once_with()
            continue_speaking.assert_not_called()
            assert managed.chain is chain and managed._capture_id == capture and chain.is_open
            assert not _current(runtime)["answers"]
            commands = [item for item in store.evidence_commands.values() if item["command_type"] == "evidence.retry_processing"]
            assert len(commands) == 1 and commands[0]["payload"] == {"capture_id": capture}
            await endpoint._notify("understanding_retrying")
            await managed.dispatch(channel, signal.model_copy(update={"idempotency_key": "merge_recovery_078"}))
            retry.assert_called_once_with()
    asyncio.run(scenario())


def test_processing_retry_cannot_silently_accept_an_inactive_endpoint(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            endpoint = managed._answer_endpoint
            await endpoint._notify("understanding_retry_exhausted")
            monkeypatch.setattr(endpoint, "retry_answer_processing", Mock(return_value=False))
            with pytest.raises(ApiError) as rejected:
                await managed.dispatch(channel, ClientSignal(type="retry_answer_processing", idempotency_key="inactive_recovery_078",
                    turn_id=TURN_ID, payload={"capture_id": managed._capture_id}))
            assert rejected.value.code == "ANSWER_PROCESSING_RECOVERY_UNAVAILABLE"
            assert not _current(runtime)["answers"]
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["turn", "capture", "missing_capture", "paused", "completed", "answered", "blocked", "takeover"])
def test_answer_recovery_rejects_stale_or_inactive_scope(tmp_path, monkeypatch, mutation):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            endpoint = managed._answer_endpoint
            retry = Mock()
            monkeypatch.setattr(endpoint, "retry_answer_processing", retry)
            await endpoint._notify("understanding_retry_exhausted")
            turn, capture = TURN_ID, managed._capture_id
            if mutation == "turn": turn = "old_turn"
            elif mutation == "capture": capture = "old_capture"
            elif mutation == "missing_capture": capture = None
            else:
                with persistence_for(runtime.store).transaction(ORGANIZATION_ID) as tx:
                    current = tx.interview_sessions.get(managed.interview_id)
                    if mutation in {"paused", "completed"}: current["status"] = mutation
                    elif mutation == "answered": current["answers"].append({"turn_id": TURN_ID})
                    elif mutation == "blocked": current["agent_runtime"]["answer_preparation"]["status"] = "blocked"
                    elif mutation == "takeover": current["agent_runtime"]["takeover"] = {"status": "active"}
                    tx.interview_sessions.update(current, expected_version=current["version"])
            with pytest.raises(ApiError):
                await managed.dispatch(channel, ClientSignal(type="retry_answer_processing", idempotency_key=f"invalid_{mutation}_078",
                    turn_id=turn, payload={"capture_id": capture} if capture else {}))
            retry.assert_not_called()
    asyncio.run(scenario())


def test_preparation_diagnostics_are_safe_scoped_and_nonretryable_stays_blocked(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            details = {**DETAILS, "reason_code": "provider_billing_required", "retryable": False, "raw_response": "private"}
            monkeypatch.setattr(type(managed._answer_endpoint), "preparation_recovery_details", property(lambda self: details))
            await managed._answer_endpoint._notify("understanding_result_rejected")
            expected = {"status": "blocked", "turn_id": TURN_ID, "capture_id": managed._capture_id,
                        **{k: v for k, v in details.items() if k != "raw_response"}}
            assert _snapshot(runtime, channel)["answer_preparation"] == expected
            problem = [event for event in channel._queue._queue if getattr(event, "type", None) == "problem"][-1]
            assert problem.payload["action"] == "contact_support" and "raw_response" not in problem.payload
            assert problem.payload["stage"] == "answer_understanding"
            assert problem.payload["reason_code"] == "provider_billing_required"
            stale = deepcopy(_current(runtime))
            stale["current_turn_id"] = "another_turn"
            assert runtime._snapshot(stale, channel.principal)["answer_preparation"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [{"attempt": True}, {"attempt": 3}, {"reason_code": "private text"},
    {"retryable": "false"}, {"stage": "private stage"}, {"max_attempts": 20}])
def test_recovery_diagnostics_reject_unbounded_or_private_values(change):
    assert public_preparation_recovery({**DETAILS, **change}) == {}
