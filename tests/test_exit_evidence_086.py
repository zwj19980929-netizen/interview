"""Independent exit through actual Evidence, STT, playback and LiveKit cleanup.

Only synthetic provider outputs, PCM and room transport are supplied. The real
owner lease, gateway schemas, delivered receipt, capture journal and final media
seal remain active. No live candidate records or external providers are used.
"""

import asyncio
import io
import json
import wave
from array import array

import pytest

from app.adapters.private_media import read_managed_audio
from app.domain.interview_agent import ClientSignal
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from test_automatic_turn_integration import _automatic_session, _current
from test_declined_answer_integration import (
    _NEXT_TURN_ID, _TECHNICAL, _add_next_question, _answer_and_confirm_exit,
    _playable_speech,
)
from test_evidence_owner_recovery_integration import INTERVIEW_ID, ORGANIZATION_ID, TURN_ID, _wait_until
from test_spoken_supplement_integration import _audio_aligned_transcripts


EXIT = "我想退出面试了，不好意思。"
YES = "是的，确定退出面试。"


def _assert_retained_exit(store, runtime, text, *, voice_frames, continuation_frames):
    """Assert the replacement for an unfinished answer, including actual PCM."""
    current = _current(runtime)
    assert current["answers"] == [] and not store.evaluations
    turn = current["turns"][0]
    saved = turn["exit_evidence"]
    assert saved["assessment_status"] == "not_evaluated"
    assert saved["reason"] == "candidate_confirmed_exit"
    assert saved["raw_transcript"] == saved["final_transcript"] == text
    assert saved["transcript_source"] == "server_streaming"
    assert saved["media_evidence"]["complete"] is True
    assert turn["candidate_exit_confirmation"]["state"] == "confirmed"
    assert not turn.get("current_understanding")
    assert not [item for item in current["agent_runtime"].get("problems", [])
                if item.get("recoverable") is False]
    seals = [command for command in store.evidence_commands.values() if command["command_type"] == "evidence.seal"]
    assert len(seals) == 1 and seals[0]["status"] == "completed"
    recording = read_managed_audio(persistence_for(store), ORGANIZATION_ID, saved["audio_uri"])
    with wave.open(io.BytesIO(recording), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        samples = array("h", wav.readframes(wav.getnframes()))
    assert samples.count(8192) == voice_frames * 320
    assert samples.count(16384) == continuation_frames * 320
    assert set(samples) <= {0, 8192, 16384}
    return saved


def _control_provider(monkeypatch):
    original = MockProvider.invoke
    calls = []

    async def invoke(provider, capability, request, context):
        version = str(request.metadata.get("prompt_version") or "")
        if version == "conversation_reception.v10":
            calls.append(version)
            supplied = json.loads(request.messages[-1].content)
            data = {"kind": "stop_interview", "confidence": .99,
                    "evidence_id": list(supplied["evidence"])[-1], "reply_text": ""}
        elif version == "candidate_exit_reply.v1":
            calls.append(version)
            supplied = json.loads(request.messages[-1].content)
            assert supplied["new_reply"] == YES
            data = {"intent": "confirm", "confidence": .99,
                    "evidence_quote": request.json_schema["properties"]["evidence_quote"]["enum"][-1]}
        elif version.startswith(("interview_turn_understanding.", "interview_turn_decision.")):
            calls.append(version)
            raise AssertionError("Exit must not need a complete technical answer contract")
        else:
            return await original(provider, capability, request, context)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="mock", model="control_086", request_id="control_086", latency_ms=0))

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls


async def _drain_farewell(channel, runtime):
    await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_closing_performance_id")))
    performance_id = _current(runtime)["agent_runtime"]["completion_closing_performance_id"]
    await channel.send(ClientSignal(type="avatar.performance.stopped",
        idempotency_key="exit_farewell_086_" + performance_id,
        payload={"performance_id": performance_id}))
    await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_emitted_at")))


@pytest.mark.parametrize("body", [EXIT, _TECHNICAL + EXIT], ids=["pure_control", "retained_technical_content"])
def test_confirmed_exit_seals_all_speech_and_closes_livekit_without_answer_events(tmp_path, monkeypatch, body):
    calls = _control_provider(monkeypatch)
    _audio_aligned_transcripts(monkeypatch, [body, YES])

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            projected = []
            original_project = channel._project_evidence_event

            async def project(event, *args, **kwargs):
                projected.append(dict(event))
                return await original_project(event, *args, **kwargs)

            channel._project_evidence_event = project
            endpoint, _ = await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            await _wait_until(lambda: all(command["status"] == "completed" for command in store.evidence_commands.values()))
            current = _current(runtime)
            assert current["answers"] == [] and not store.evaluations
            assert current["current_turn_id"] is None
            assert current["candidate_input_completion_reason"] == "candidate_requested"
            assert current["candidate_exit_confirmed_at"]
            turn = current["turns"][0]
            saved = turn["exit_evidence"]
            assert saved["assessment_status"] == "not_evaluated"
            assert saved["final_transcript"] == saved["raw_transcript"] == body + YES
            assert saved["media_evidence"]["complete"] is True
            assert turn["candidate_exit_confirmation"]["state"] == "confirmed"
            assert not turn.get("current_understanding")
            assert not any(event["type"] == "conversation.act.selected" and event["turn_id"] == _NEXT_TURN_ID
                           for event in current["agent_events"])
            assert calls == ["conversation_reception.v10", "candidate_exit_reply.v1"]
            seal = [command for command in store.evidence_commands.values() if command["command_type"] == "evidence.seal"]
            assert len(seal) == 1 and seal[0]["status"] == "completed"
            assert not managed.chain.is_open and managed._capture_id is None
            recording = read_managed_audio(persistence_for(store), ORGANIZATION_ID, saved["audio_uri"])
            with wave.open(io.BytesIO(recording), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                samples = array("h", wav.readframes(wav.getnframes()))
            assert samples.count(8192) == samples.count(16384) == 5 * 320
            await _drain_farewell(channel, runtime)
            await _wait_until(lambda: managed._stopped)
            assert endpoint._closed
            assert not {"answer.accepted", "evaluation.queued", "utterance.not_accepted"} & {
                event["type"] for event in projected}
            assert any(event.type == "completed" for event in channel._queue._queue)
            assert _current(runtime)["answers"] == []
    asyncio.run(scenario())


def test_exit_commit_rechecks_owner_after_control_preparation(tmp_path, monkeypatch):
    calls = _control_provider(monkeypatch)
    _audio_aligned_transcripts(monkeypatch, [_TECHNICAL + EXIT, YES])

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            original = managed.chain.prepare_exit_decision
            prepared = []

            def prepare_then_lose_owner(final, **kwargs):
                result = original(final, **kwargs)
                prepared.append(result)
                old_owner = managed.ownership
                coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
                assert coordinator.release(old_owner)
                successor = coordinator.claim_owner(interview_id=INTERVIEW_ID,
                    organization_id=ORGANIZATION_ID, local_instance_id="exit_successor_086")
                assert successor.ownership_epoch > old_owner.ownership_epoch
                return result

            managed.chain.prepare_exit_decision = prepare_then_lose_owner
            endpoint, _ = await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: endpoint._closed, timeout=5)
            assert len(prepared) == 1
            current = _current(runtime)
            assert not current.get("candidate_input_completed_at")
            assert not current["turns"][0].get("exit_evidence")
            assert not current["answers"] and not store.evaluations
            assert current["current_turn_id"] == TURN_ID
            assert calls == ["conversation_reception.v10", "candidate_exit_reply.v1"]
            # Teardown must not attempt disconnect repair using the deliberately
            # replaced owner. Abort only this rejected synthetic recognizer.
            await managed.chain.abort()
    asyncio.run(scenario())
