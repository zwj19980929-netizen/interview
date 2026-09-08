"""Post-commit projection faults must not replay STT or strand presentation.

Use the existing synthetic automatic-turn fixture: ownership, the real Evidence
chain, gateway/STT validation, recorded PCM and the prepared command journal all
remain real. No candidate history, external model or room network is involved.
"""

import asyncio
import logging

import pytest

from app.domain.interview_agent import ClientSignal
from app.model_gateway import capabilities as cap
from app.services.interview_evidence import EvidenceFinishResult

from test_automatic_turn_integration import (
    _PREFIX, _VOICE, _automatic_session, _current, _feed, _silence_until,
    _assert_one_automatic_answer,
)
from test_evidence_owner_recovery_integration import TURN_ID, _wait_until


def stt_invocations(store):
    return [(item["invocation_id"], item["capability"], item["status"])
            for item in store.model_invocations
            if item["capability"] in {cap.STT_STREAMING, cap.STT_BATCH}]


def completed_seal(store):
    seals = [item for item in store.evidence_commands.values() if item["command_type"] == "evidence.seal"]
    return len(seals) == 1 and seals[0]["status"] == "completed"


@pytest.mark.parametrize("site", ["floor", "deactivate", "event"])
@pytest.mark.parametrize("fault", ["exception", "slow"])
def test_committed_answer_survives_projection_fault_and_continues_expression(
    tmp_path, monkeypatch, site, fault, caplog,
):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            hit, cancelled, after_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            selected_acts, after_calls = [], []
            original_emit = channel._emit
            original_deactivate = managed._deactivate_partial_projection
            original_project = channel._project_evidence_event
            original_after = channel._after_formal_evidence_finished

            async def fail_once():
                hit.set()
                assert len(_current(runtime)["answers"]) == 1, "Only inject after the real answer transaction"
                if fault == "exception":
                    raise RuntimeError("SYNTHETIC_PRIVATE_POST_COMMIT_BODY")
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            async def emit(kind, payload, **kwargs):
                if (site == "floor" and not hit.is_set() and kind == "floor.changed"
                        and payload.get("reason") == "answer_processing"
                        and _current(runtime)["answers"]):
                    await fail_once()
                return await original_emit(kind, payload, **kwargs)

            async def deactivate(kind, turn_id):
                if site == "deactivate" and not hit.is_set() and _current(runtime)["answers"]:
                    await fail_once()
                return await original_deactivate(kind, turn_id)

            async def project(raw, causation_id):
                if (site == "event" and not hit.is_set() and raw.get("type") == "transcript.final"
                        and _current(runtime)["answers"]):
                    await fail_once()
                return await original_project(raw, causation_id)

            async def after(turn_id, signal, **kwargs):
                after_calls.append(turn_id)
                after_entered.set()
                return await original_after(turn_id, signal, **kwargs)

            async def select_act(**kwargs):
                # Preserve the real after-formal state/fence checks; only the
                # synthetic approved speech output itself has no external I/O.
                selected_acts.append(kwargs["act_type"])
                return None

            async def stop_recording(*_args, **_kwargs):
                return None

            monkeypatch.setattr(channel, "_emit", emit)
            monkeypatch.setattr(managed, "_deactivate_partial_projection", deactivate)
            monkeypatch.setattr(channel, "_project_evidence_event", project)
            monkeypatch.setattr(channel, "_after_formal_evidence_finished", after)
            monkeypatch.setattr(channel, "_select_act", select_act)
            monkeypatch.setattr(runtime.media_captures, "stop_for_interview", stop_recording)
            caplog.set_level(logging.WARNING, logger="app.services.livekit_evidence_ingress")

            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1)
            invocations_after_commit = stt_invocations(store)
            await asyncio.wait_for(hit.wait(), timeout=2)
            await _wait_until(lambda: completed_seal(store), timeout=3)
            await asyncio.wait_for(after_entered.wait(), timeout=3)
            await _wait_until(lambda: selected_acts, timeout=2)
            await _wait_until(lambda: managed._presentation_task is not None and managed._presentation_task.done(), timeout=2)

            _assert_one_automatic_answer(store, runtime, _PREFIX, voice_frames=10)
            assert after_calls == [TURN_ID] and selected_acts == ["closing"]
            assert stt_invocations(store) == invocations_after_commit
            assert not any(item[1] == cap.STT_BATCH for item in stt_invocations(store))
            if fault == "slow":
                assert cancelled.is_set(), "Slow UI work must have its own bounded cancellation"
            assert "SYNTHETIC_PRIVATE_POST_COMMIT_BODY" not in caplog.text

    asyncio.run(scenario())


def test_post_commit_presentation_failure_is_observed_without_replay(tmp_path, monkeypatch, caplog):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            after_calls = []

            async def failed_after(turn_id, _signal, **_kwargs):
                assert len(_current(runtime)["answers"]) == 1
                after_calls.append(turn_id)
                raise RuntimeError("SYNTHETIC_PRIVATE_PRESENTATION_BODY")

            monkeypatch.setattr(channel, "_after_formal_evidence_finished", failed_after)
            caplog.set_level(logging.WARNING, logger="app.services.livekit_evidence_ingress")
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1)
            invocations_after_commit = stt_invocations(store)
            await _wait_until(lambda: completed_seal(store))
            await _wait_until(lambda: "Committed answer presentation failed" in caplog.text)
            assert after_calls == [TURN_ID]
            assert "error_type=RuntimeError" in caplog.text
            assert "SYNTHETIC_PRIVATE_PRESENTATION_BODY" not in caplog.text
            assert stt_invocations(store) == invocations_after_commit
            _assert_one_automatic_answer(store, runtime, _PREFIX, voice_frames=10)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["followup.selected", "utterance.not_accepted"])
def test_approved_expression_event_is_not_cancelled_by_one_second_ui_deadline(
    tmp_path, monkeypatch, kind,
):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, []) as (_store, _runtime, channel, managed):
            completed, cancelled = asyncio.Event(), asyncio.Event()
            acts, after_calls = [], []

            async def slow_approved_expression(**kwargs):
                acts.append(kwargs["act_type"])
                try:
                    # A normal complete TTS synthesis can exceed the transient
                    # UI deadline; expression has its own lifecycle/budget.
                    await asyncio.sleep(1.1)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                completed.set()

            async def after(_turn_id, _signal, **kwargs):
                after_calls.append(kwargs)

            monkeypatch.setattr(channel, "_select_act", slow_approved_expression)
            monkeypatch.setattr(channel, "_after_formal_evidence_finished", after)
            event = ({
                "type": kind, "payload": {"turn_id": "synthetic_followup", "question_text": "合成批准追问。"},
            } if kind == "followup.selected" else {
                "type": kind, "turn_id": TURN_ID, "suggested_action": "clarify",
            })
            result = EvidenceFinishResult("formal", TURN_ID, [event], None, {"accepted": kind == "followup.selected"})
            await managed._present_formal_result(
                channel, ClientSignal(type="evidence.finish", idempotency_key="synthetic_expression", turn_id=TURN_ID), result,
            )
            assert completed.is_set() and not cancelled.is_set()
            assert acts == ["followup" if kind == "followup.selected" else "clarification"]
            assert len(after_calls) == 1 and after_calls[0]["conversation_action_selected"] is True

    asyncio.run(scenario())
