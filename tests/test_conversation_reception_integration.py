"""Real owner/STT/act/TTS/playback path with only synthetic provider inputs."""
import asyncio
import json

import pytest

from app.core.prompt.conversation_reception import RECEPTION_SPEECH
from app.domain.interview_agent import ClientSignal
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider
from test_answer_endpoint import _Clock
from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _CONTINUATION
from test_evidence_owner_recovery_integration import ORGANIZATION_ID, TURN_ID, _wait_until
from test_spoken_supplement_integration import _synthetic_tts, _finish_playback


def reception_provider(monkeypatch, kind, *, release=None):
    original = MockProvider.invoke
    calls = []

    async def invoke(provider, capability, request, context):
        if request.metadata.get("prompt_version") != "conversation_reception.v1":
            return await original(provider, capability, request, context)
        value = json.loads(request.messages[-1].content)
        calls.append(value)
        if release:
            await release.wait()
        return ChatJSONResponse(data={"kind": kind, "confidence": .98,
            "evidence_id": next(iter(value["evidence"]))}, usage=Usage(), provider=ProviderMeta(
                provider_id="mock", model="synthetic_reception", request_id="reception_test", latency_ms=0))
    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls


def acts(runtime):
    return [event["payload"] for event in _current(runtime)["agent_events"]
            if event["type"] == "conversation.act.selected"]


@pytest.mark.parametrize("kind,text", [
    ("wait", "稍等。没有。嗯。"), ("presence", "你能听到我说话吗？"),
    ("transcript_correction", "这里的字幕不是我说的。"), ("repeat", "请再说一遍题目。"),
])
def test_request_is_audible_before_answer_review_and_continues_same_capture(tmp_path, monkeypatch, kind, text):
    calls = reception_provider(monkeypatch, kind)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [text, "我继续说。", ""],
                stable_previews=True, spoken_confirmation=True) as (store, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            clock = _Clock()
            endpoint = managed._answer_endpoint
            endpoint.clock = clock
            capture_id = managed._capture_id
            before = _current(runtime)
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            await _wait_until(lambda: bool(spoken), timeout=5)
            current = _current(runtime)
            assert len(acts(runtime)) == 1
            assert acts(runtime)[0]["act_type"] == "conversation_acknowledgement"
            assert spoken == [acts(runtime)[0]["text"]]
            if kind == "repeat":
                assert before["turns"][0]["question_snapshot"]["question_text"] in spoken[0]
            else:
                assert spoken == [RECEPTION_SPEECH[kind]]
            assert current["current_turn_id"] == TURN_ID and current["turn_ids"] == before["turn_ids"]
            assert not current["answers"] and not current["agent_runtime"].get("answer_preparation")
            assert managed._capture_id == capture_id
            assert not [c for c in store.evidence_commands.values() if c["command_type"] == "evidence.seal"]
            assert [v["prompt_version"] for v in store.model_invocations if v["purpose"] == "interview_turn_understanding"] == ["conversation_reception.v1"]
            await _finish_playback(channel, runtime, managed)
            assert endpoint.confirmation.phase == "listening" and managed.chain.is_open
            clock.value += 30
            await asyncio.sleep(.2)
            assert len(spoken) == len(calls) == 1 and not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1.1
            try:
                await _wait_until(lambda: len(spoken) == 2, timeout=5)
            except AssertionError:
                raise AssertionError(str({"calls": calls, "phase": endpoint.confirmation.phase,
                    "revision": endpoint.revision, "blocked": endpoint._blocked_revision,
                    "retry": endpoint._retry_at, "last_voice": endpoint._last_voice,
                    "clock": clock.value, "preview": await managed.chain.transcript_preview(),
                    "events": acts(runtime)})) from None
            assert calls[-1]["new_speech"] == "我继续说。"
            assert calls[-1]["preceding_text"] == text
            assert managed._capture_id == capture_id and not _current(runtime)["answers"]
    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", [False, True])
def test_pause_acknowledgement_waits_for_playback_and_barge_in_revokes_pause(tmp_path, monkeypatch, interrupt):
    reception_provider(monkeypatch, "pause")

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["我们先暂停面试。", "不用暂停了。", ""],
                stable_previews=True, spoken_confirmation=True) as (_, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")), timeout=5)
            assert spoken == [RECEPTION_SPEECH["pause"]]
            assert _current(runtime)["status"] == "in_progress" and managed.chain.is_open
            if interrupt:
                await _feed(managed._ingress, _CONTINUATION, 5)
                await channel.send(ClientSignal(type="speech.started", turn_id=TURN_ID,
                    idempotency_key="revoke_pause", payload={"capture_id": managed._capture_id}))
                await asyncio.sleep(.15)
                assert endpoint.confirmation.phase == "listening"
                assert _current(runtime)["status"] == "in_progress" and managed.chain.is_open
            else:
                await _finish_playback(channel, runtime, managed)
                await _wait_until(lambda: _current(runtime)["status"] == "paused")
            assert not _current(runtime)["answers"]
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["voice", "owner"])
def test_late_request_cannot_speak_over_new_input_or_new_owner(tmp_path, monkeypatch, change):
    async def scenario():
        release = asyncio.Event()
        calls = reception_provider(monkeypatch, "wait", release=release)
        async with _automatic_session(tmp_path, monkeypatch, ["稍等。", ""],
                stable_previews=True, spoken_confirmation=True) as (store, runtime, _, managed):
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            await _wait_until(lambda: bool(calls))
            if change == "voice":
                await _feed(managed._ingress, _CONTINUATION, 5)
            else:
                with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
                    owner = tx.evidence_ownerships.list()[0]
                    owner["ownership_epoch"] += 1
                    owner["owner_instance_id"] = "successor"
                    tx.evidence_ownerships.update(owner, expected_version=owner["version"])
            release.set()
            await asyncio.sleep(.2)
            assert not spoken and not acts(runtime) and not _current(runtime)["answers"]
            assert not _current(runtime)["agent_runtime"].get("answer_preparation")
    asyncio.run(scenario())


def test_barge_in_while_tts_is_preparing_prevents_late_playback(tmp_path, monkeypatch):
    reception_provider(monkeypatch, "wait")
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["稍等。", "不用等了，我继续。", ""],
                stable_previews=True, spoken_confirmation=True) as (_, runtime, channel, managed):
            _synthetic_tts(runtime)
            original = runtime.gateway.invoke
            entered, release = asyncio.Event(), asyncio.Event()
            async def delayed(capability, request, **kwargs):
                if capability == "tts.synthesize":
                    entered.set()
                    await release.wait()
                return await original(capability, request, **kwargs)
            runtime.gateway.invoke = delayed
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            await asyncio.wait_for(entered.wait(), 5)
            await _feed(managed._ingress, _CONTINUATION, 5)
            await channel.send(ClientSignal(type="speech.started", turn_id=TURN_ID,
                idempotency_key="reception_tts_barge", payload={"capture_id": managed._capture_id}))
            release.set()
            await asyncio.sleep(.2)
            assert endpoint.confirmation.phase == "listening" and managed.chain.is_open
            assert not _current(runtime)["agent_runtime"].get("active_performance_id")
            assert not _current(runtime)["answers"]
    asyncio.run(scenario())


def test_pause_commit_rechecks_owner_inside_the_lifecycle_transaction(tmp_path, monkeypatch):
    from app.core.errors import ApiError
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [],
                stable_previews=True, spoken_confirmation=True) as (store, runtime, _, managed):
            fence = managed.ownership.commit_fence()
            with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
                owner = tx.evidence_ownerships.list()[0]
                owner["ownership_epoch"] += 1
                owner["owner_instance_id"] = "successor"
                tx.evidence_ownerships.update(owner, expected_version=owner["version"])
            with pytest.raises(ApiError) as rejected:
                runtime.interviews.pause_interview(_current(runtime)["id"],
                    organization_id=ORGANIZATION_ID, evidence_fence=fence)
            assert rejected.value.code == "EVIDENCE_OWNER_FENCED"
            assert _current(runtime)["status"] == "in_progress"
            await managed.chain.abort()
    asyncio.run(scenario())
