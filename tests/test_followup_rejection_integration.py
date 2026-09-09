"""A rejected optional probe must not trap an already valid spoken answer."""

import asyncio
from copy import deepcopy

import pytest

from app.providers.mock.provider import MockProvider
from test_automatic_turn_integration import _automatic_session, _current, _assert_one_automatic_answer
from test_declined_answer_integration import (
    _semantic_provider, _add_next_question, _playable_speech, _answer_and_confirm,
    _TECHNICAL, _NEXT_TURN_ID,
)
from test_evidence_owner_recovery_integration import _wait_until


@pytest.mark.parametrize("rejection", ["answer_leakage", "covered_target"])
def test_invalid_optional_probe_advances_valid_answer_without_repeating_inference(
    tmp_path, monkeypatch, rejection,
):
    calls = _semantic_provider(monkeypatch, technical=True, probe=True)
    original = MockProvider.invoke

    async def invoke(provider, capability, request, context):
        response = await original(provider, capability, request, context)
        if request.metadata.get("prompt_version", "").startswith("interview_turn_decision."):
            data = deepcopy(response.data)
            if rejection == "answer_leakage":
                data["followup"]["leaks_answer"] = True
            else:
                # P1 is a valid wire reference but already covered: the
                # follow-up domain gate must reject targeting it again.
                data["followup"]["target_point_ids"] = ["P1"]
            response = response.model_copy(update={"data": data})
        return response

    monkeypatch.setattr(MockProvider, "invoke", invoke)

    async def scenario():
        reply = "没有补充了。"
        async with _automatic_session(
            tmp_path, monkeypatch, [_TECHNICAL, reply, ""], spoken_confirmation=True,
        ) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            await _answer_and_confirm(runtime, channel, managed)
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(
                store, runtime, _TECHNICAL + reply, voice_frames=5, continuation_frames=5,
            )
            current = _current(runtime)
            assert current["current_turn_id"] == _NEXT_TURN_ID
            understanding = current["turns"][0]["current_understanding"]
            assert understanding["intent"] == "answer" and understanding["problem"] is None
            assert understanding["evidence_quotes"] == [_TECHNICAL]
            assert len([call for call in calls if call[0] == "understanding"]) == 1
            assert not any(turn.get("is_followup") for turn in current["turns"])
            acts = [event["payload"]["act_type"] for event in current["agent_events"]
                    if event["type"] == "conversation.act.selected"]
            assert "clarification" not in acts and "followup" not in acts

    asyncio.run(scenario())
