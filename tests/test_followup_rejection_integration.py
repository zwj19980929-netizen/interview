"""A rejected optional probe must not trap an already valid spoken answer."""

import asyncio
from copy import deepcopy

import pytest

from app.providers.mock.provider import MockProvider
from app.core.prompt.contracts import SUPPLEMENT_SPEECH
from test_automatic_turn_integration import (
    _automatic_session, _current, _assert_one_automatic_answer, _feed, _CONTINUATION,
)
from test_declined_answer_integration import (
    _semantic_provider, _add_next_question, _playable_speech, _answer_without_confirmation,
    _stop_current_playback,
    _TECHNICAL, _NEXT_TURN_ID,
)
from test_evidence_owner_recovery_integration import _wait_until
from test_spoken_supplement_integration import _audio_aligned_transcripts


@pytest.mark.parametrize("rejection", ["answer_leakage", "multiple_questions"])
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
                # Wire-valid text still needs the single-question domain gate.
                data["followup"]["question_text"] = "如何恢复？恢复之后怎么验证？"
            response = response.model_copy(update={"data": data})
        return response

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    reply = "没有补充了。"
    _audio_aligned_transcripts(monkeypatch, [_TECHNICAL, reply])

    async def scenario():
        async with _automatic_session(
            tmp_path, monkeypatch, [""], spoken_confirmation=True,
        ) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            endpoint, clock = await _answer_without_confirmation(managed)
            await _wait_until(lambda: endpoint.confirmation.speaking)
            assert spoken == [SUPPLEMENT_SPEECH["check"]] and not _current(runtime)["answers"]
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: endpoint.confirmation._supplement_question_delivered)
            assert not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(
                store, runtime, _TECHNICAL + reply, voice_frames=5, continuation_frames=5,
            )
            current = _current(runtime)
            assert current["current_turn_id"] == _NEXT_TURN_ID
            understanding = current["turns"][0]["current_understanding"]
            assert understanding["intent"] == "answer" and understanding["problem"] is None
            assert understanding["turn_intent"] == "finish_answer" and understanding["followup_allowed"]
            assert understanding["evidence_quotes"] == [_TECHNICAL]
            assert [call[2] for call in calls if call[0] == "understanding"] == [
                "interview_turn_decision.v131", "interview_turn_decision.v132",
            ], "One semantic preparation per distinct final, no rejected-probe retries"
            assert [call for call in calls if call[0] == "supplement"] == [("supplement", reply, "finish")]
            assert spoken.count(SUPPLEMENT_SPEECH["check"]) == 1
            assert not any(turn.get("is_followup") for turn in current["turns"])
            acts = [event["payload"]["act_type"] for event in current["agent_events"]
                    if event["type"] == "conversation.act.selected"]
            assert "clarification" not in acts and "followup" not in acts

    asyncio.run(scenario())
