"""Recovery speech may overlap analysis, never answer acceptance."""
import asyncio

import pytest

from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final


def test_recovery_starts_pure_preparation_before_notice_selection(monkeypatch):
    async def scenario():
        endpoint, _, _, commits = _endpoint()
        final = _final()
        calls = []
        selected, release = asyncio.Event(), asyncio.Event()

        def start(transcript):
            calls.append(endpoint._identity(transcript))
            return True

        async def speak(kind, guard):
            guard()
            assert kind == "reception_unavailable"
            selected.set()
            await release.wait()
            return True

        monkeypatch.setattr(endpoint, "start_recovery_preparation", start, raising=False)
        endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint._proposal_revision = endpoint.revision
        task = asyncio.create_task(endpoint.confirmation._recover_interaction(endpoint, final))
        try:
            await asyncio.wait_for(selected.wait(), 1)
            assert calls == [endpoint._identity(final)], "Pure recovery must run while the notice is being prepared"
            assert not commits
        finally:
            release.set()
            await task
            await endpoint.close()
    asyncio.run(scenario())


def test_notice_ack_rechecks_current_final_without_another_silence_window(monkeypatch):
    async def scenario():
        endpoint, clock, _, commits = _endpoint()
        reviews = []

        async def speak(kind, guard):
            guard()
            return True

        async def semantic(endpoint, final, **kwargs):
            endpoint.assert_current()
            reviews.append(endpoint._identity(final))
            return True

        monkeypatch.setattr(endpoint, "start_recovery_preparation", lambda final: True, raising=False)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        monkeypatch.setattr(confirmation, "_semantic_turn", semantic)
        endpoint._proposal_revision = endpoint.revision
        final = endpoint.capture.current_final
        await confirmation._recover_interaction(endpoint, final)
        assert confirmation.speaking
        await confirmation.step(endpoint, quiet=20)
        assert not reviews and not endpoint.capture.snapshots and not commits
        clock.value += 3
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        assert endpoint.clock() - endpoint._last_voice == 0
        await confirmation.step(endpoint, quiet=0)
        assert reviews == [endpoint._identity(final)]
        assert endpoint.capture.snapshots == [False], "Only a new authoritative cut can reconcile a background result"
        assert not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["new_voice", "new_capture", "closed", "blocked", "interrupted_playback"])
def test_recovery_review_does_not_survive_changed_input_or_scope(monkeypatch, change):
    async def scenario():
        endpoint, _, _, commits = _endpoint()
        reviews = []

        async def speak(kind, guard):
            guard()
            return True

        async def semantic(*args, **kwargs):
            reviews.append(True)
            return True

        monkeypatch.setattr(endpoint, "start_recovery_preparation", lambda final: True, raising=False)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        monkeypatch.setattr(confirmation, "_semantic_turn", semantic)
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(endpoint.capture.current_final)
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        await confirmation.complete_playback(endpoint,
            reason="candidate_barge_in" if change == "interrupted_playback" else "agent_finished")
        if change == "new_voice":
            endpoint.speech_started(source="audio")
        elif change == "new_capture":
            endpoint.capture = _Capture()
        elif change == "closed":
            endpoint._closed = True
        elif change == "blocked":
            endpoint._record_preparation_failure()
            endpoint._record_preparation_failure()
        await confirmation.step(endpoint, quiet=0)
        assert not reviews and not endpoint.capture.snapshots and not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("waiting", ["supplement", "exit"])
def test_recovery_marker_cannot_skip_a_real_confirmation_exchange(monkeypatch, waiting):
    async def scenario():
        endpoint, _, _, commits = _endpoint()
        async def speak(kind, guard):
            guard()
            return True
        monkeypatch.setattr(endpoint, "start_recovery_preparation", lambda final: True, raising=False)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint._proposal_revision = endpoint.revision
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        confirmation.phase = "awaiting_reply" if waiting == "supplement" else "awaiting_exit_reply"
        confirmation.reply_seen = False
        confirmation._exit_waiting = waiting == "exit"
        await confirmation.step(endpoint, quiet=0)
        assert confirmation.phase == ("awaiting_reply" if waiting == "supplement" else "awaiting_exit_reply")
        assert not endpoint.capture.snapshots and not endpoint.capture.prepares and not commits
        await endpoint.close()
    asyncio.run(scenario())


def test_recovery_review_uses_new_final_and_current_guard_not_notice_snapshot(monkeypatch):
    async def scenario():
        endpoint, _, _, commits = _endpoint()
        reviewed = []
        async def speak(kind, guard):
            guard()
            return True
        async def semantic(endpoint, final, **kwargs):
            endpoint.assert_current()
            reviewed.append(endpoint._identity(final))
            return True
        monkeypatch.setattr(endpoint, "start_recovery_preparation", lambda final: True, raising=False)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        monkeypatch.setattr(confirmation, "_semantic_turn", semantic)
        endpoint._proposal_revision = endpoint.revision
        old = endpoint.capture.current_final
        await confirmation._recover_interaction(endpoint, old)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        corrected = _final("Synthetic corrected authoritative answer")
        corrected.provider.request_id = "revalidated-request"
        endpoint.capture.current_final = corrected
        await confirmation.step(endpoint, quiet=0)
        assert reviewed == [endpoint._identity(corrected)]
        assert reviewed != [endpoint._identity(old)] and not commits
        await endpoint.close()
    asyncio.run(scenario())


def test_absent_final_after_recovery_notice_never_reuses_notice_evidence(monkeypatch):
    async def scenario():
        endpoint, _, notices, commits = _endpoint()
        async def speak(kind, guard):
            guard()
            return True
        monkeypatch.setattr(endpoint, "start_recovery_preparation", lambda final: True, raising=False)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint._proposal_revision = endpoint.revision
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.current_final = None
        await confirmation.step(endpoint, quiet=0)
        assert "transcript_unavailable" in notices
        assert not endpoint.capture.prepares and not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("selected", [False, True])
def test_failed_recovery_notice_cannot_grant_immediate_review_without_playback_ack(monkeypatch, selected):
    async def scenario():
        endpoint, _, _, commits = _endpoint()
        spoken, starts, reviews = [], [], []

        def start(final):
            starts.append(endpoint._identity(final))
            return True

        async def speak(kind, guard):
            guard()
            spoken.append(kind)
            if selected:
                endpoint.confirmation.playback_selected(endpoint)
            raise OSError("Synthetic output delivery failure")

        async def semantic(*args, **kwargs):
            reviews.append(True)
            return True

        monkeypatch.setattr(endpoint, "start_recovery_preparation", start)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        monkeypatch.setattr(confirmation, "_semantic_turn", semantic)
        endpoint._proposal_revision = endpoint.revision
        final = endpoint.capture.current_final
        try:
            await confirmation._recover_interaction(endpoint, final)
            assert confirmation.phase == "listening"
            assert confirmation._recovery_review is None
            await confirmation.step(endpoint, quiet=0)
            assert not reviews and not endpoint.capture.snapshots and not commits
            await confirmation._recover_interaction(endpoint, final)
            assert len(starts) == 2  # Re-entering may reuse the existing pure work.
            assert spoken == ["reception_unavailable"]  # Never replay the failed notice.
            assert confirmation._recovery_review is None
            await confirmation.step(endpoint, quiet=0)
            assert not reviews and not endpoint.capture.snapshots and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_real_preparation_overlaps_notice_and_reuses_after_delivery_without_skipping_supplement():
    from app.services.conversation_delivery import update_delivery
    from test_preparation_context_081 import select_notice
    from test_prepared_decision_binding import _setup

    async def scenario():
        service, final = _setup()
        capture = _Capture()
        capture.current_final = final
        bound, release = asyncio.Event(), asyncio.Event()
        validations, spoken = [], []

        async def prepare(final, **kwargs):
            capture.prepares.append(final.model_copy(deep=True))
            prepared = await service.prepare_streaming_decision(
                "iv_binding", "turn_binding", final,
                snapshot_ref="evidence-checkpoint://synthetic/1/1", **kwargs,
            )
            bound.set()
            await release.wait()
            return prepared

        def validate(prepared, final, **kwargs):
            service.assert_streaming_preparation_current(prepared, final, "org_default", **kwargs)
            validations.append(endpoint._identity(final))

        async def speak(kind, guard):
            guard()
            spoken.append(kind)
            if kind == "reception_unavailable":
                # Bind the model result before selecting its concurrent status
                # notice so the real conversation-memory fence is exercised.
                await asyncio.wait_for(bound.wait(), 1)
                select_notice(service, status="selected")
                return True
            assert kind == "check"
            return {"act_id": "synthetic_supplement_check"}

        capture.prepare_decision = prepare
        capture.assert_preparation_current = validate
        endpoint, clock, _, commits = _endpoint(capture=capture)
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(final)
        endpoint._begin_interaction(final.text)
        endpoint._use_preparation_budget(final.text)
        endpoint._record_preparation_failure()
        deadline = endpoint._interaction_deadline
        try:
            await asyncio.wait_for(confirmation._recover_interaction(endpoint, final), 2)
            assert confirmation.speaking and len(capture.prepares) == 1
            task = endpoint._confirmed_preparation
            assert task is not None and not task.done()
            release.set()
            await asyncio.wait_for(asyncio.shield(task), 1)
            await confirmation.step(endpoint, quiet=20)
            assert spoken == ["reception_unavailable"] and not commits
            assert not capture.snapshots and not service.get_interview("iv_binding")["answers"]

            # The actual delivery facts remain persisted; only this versioned
            # status notice is harmless to the model-result context binding.
            with service.persistence.transaction("org_default") as tx:
                session = tx.interview_sessions.get("iv_binding")
                update_delivery(session, event_id="event_recovery_notice", performance_id="performance_notice",
                                status="delivered", updated_at="2026-10-26T09:15:45Z")
                tx.interview_sessions.update(session, expected_version=session["version"])
            clock.value += 3
            await confirmation.complete_playback(endpoint, reason="agent_finished")
            assert endpoint.clock() - endpoint._last_voice == 0
            await asyncio.wait_for(confirmation.step(endpoint, quiet=0), 1)
            assert len(capture.prepares) == 1 and validations
            assert capture.snapshots == [False, False]
            assert spoken == ["reception_unavailable", "check"]
            assert confirmation.speaking and confirmation._after_speech == "awaiting_reply"
            assert not confirmation.confirmed and not commits
            assert not service.get_interview("iv_binding")["answers"]
            assert endpoint._interaction_deadline == deadline
        finally:
            release.set()
            await endpoint.close()
    asyncio.run(scenario())
