"""Synthetic owner/STT/playback integration for cursor and live role memory."""
import asyncio
import sqlite3
import pytest
from app.domain.interview_agent import ClientSignal
from app.persistence.errors import PersistenceError
from app.persistence.interface import InterviewSessionRepository

from test_answer_endpoint import _Clock
from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _CONTINUATION
from test_conversation_reception_integration import reception_provider
from test_evidence_owner_recovery_integration import _wait_until
from test_spoken_supplement_integration import _synthetic_tts, _finish_playback


def test_real_selection_records_live_candidate_request_and_noise_does_not_replay_it(tmp_path, monkeypatch):
    request = "请解释这里幂等这个词的意思。"
    reply = "题目里的幂等指同一操作重复执行，结果保持一致。你可以继续回答原来的问题。"
    calls = reception_provider(monkeypatch, "question_clarification", reply_text=reply)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [request, "请重新说一次题目。", ""],
                stable_previews=True, spoken_confirmation=True) as (_, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            await _wait_until(lambda: bool(_current(runtime)["turns"][0].get("reception_exchanges")), timeout=5)
            saved = _current(runtime)
            entry = saved["turns"][0]["reception_exchanges"][0]
            assert entry["request_text"] == request and not saved["turns"][0].get("utterances")
            assert not saved["answers"]
            assert entry["selected_event_id"] in {event["event_id"] for event in saved["agent_events"]}
            await _finish_playback(channel, runtime, managed)
            confirmation = endpoint.confirmation
            await managed._apply_open(channel, ClientSignal(type="evidence.open", idempotency_key="refresh_current_capture",
                turn_id=saved["current_turn_id"], payload={}))
            assert managed._answer_endpoint is endpoint and endpoint.confirmation is confirmation
            for _ in range(6):
                endpoint.speech_started(source="audio")
                clock.value += 6
                await asyncio.sleep(.12)
            assert spoken == [reply] and len(calls) == 1
            assert managed.chain.is_open and not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1.1
            await _wait_until(lambda: len(_current(runtime)["turns"][0].get("reception_exchanges", [])) == 2, timeout=5)
            history = calls[-1]["history"]
            assert [(item["role"], item["text"]) for item in history[-2:]] == [("candidate", request), ("interviewer", reply)]
            assert history[-2]["exchange_id"] == entry["exchange_id"]
            assert not _current(runtime)["answers"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [PersistenceError, sqlite3.OperationalError])
def test_optional_history_write_failure_keeps_selected_speech_and_handled_cursor(tmp_path, monkeypatch, caplog, failure):
    calls = reception_provider(monkeypatch, "question_clarification", reply_text="这里指题目中所说的概念。")
    original = InterviewSessionRepository.update
    failures = []
    def update(repository, document, **kwargs):
        if any(turn.get("reception_exchanges") for turn in document.get("turns", [])):
            failures.append(True)
            raise failure("PRIVATE_FAKE_DB_DETAIL")
        return original(repository, document, **kwargs)
    monkeypatch.setattr(InterviewSessionRepository, "update", update)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["请解释这个概念。", ""],
                stable_previews=True, spoken_confirmation=True) as (_, runtime, channel, managed):
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            # The speech child clears its optional receipt before _infer wakes
            # the endpoint parent to advance the handled cursor. Observe the
            # completed reception operation, not that intermediate handoff.
            await _wait_until(lambda: failures and managed.chain._pending_reception_exchange is None
                              and endpoint._inference_task is None, timeout=5)
            assert endpoint.confirmation.speaking, "An optional write cannot reopen microphone echo intake"
            assert endpoint.confirmation._handled_final is not None
            assert not endpoint._prepare_failures and not _current(runtime)["answers"]
            assert not _current(runtime)["turns"][0].get("reception_exchanges")
            await _finish_playback(channel, runtime, managed)
            for _ in range(3):
                endpoint.speech_started(source="audio")
                clock.value += 6
                await asyncio.sleep(.12)
            assert len(spoken) == len(calls) == len(failures) == 1
            assert managed.chain.is_open

    asyncio.run(scenario())
    assert "PRIVATE_FAKE_DB_DETAIL" not in caplog.text
