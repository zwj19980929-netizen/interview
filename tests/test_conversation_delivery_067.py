from copy import deepcopy
import pytest

from app.core.errors import ApiError
from app.core.prompt.conversation_reception import reception_context, reception_contract
from app.services.conversation_delivery import record_selection, selected_events, update_delivery
from app.services.dialogue_history import recent_turn_dialogue
from app.services.content_clarification import validated_content_clarification
from app.services.candidate_exit_confirmation import validated_exit_context


def durable(exchange):
    session, turn, text, context = exchange()
    session["turns"] = [turn]
    event = session["agent_events"][0]
    event.update(event_id="selected", session_sequence=7, occurred_at="2026-09-28T12:00:00Z")
    record_selection(session, event)
    session["agent_events"] = []
    return session, turn, text, context


@pytest.mark.parametrize("kind", ["content", "exit"])
def test_trimmed_replay_does_not_destroy_confirmation_selection(kind):
    from test_content_clarification_066 import exchange as content
    from test_exit_confirmation_commit_065 import exchange as exit_exchange
    session, turn, text, context = durable(content if kind == "content" else exit_exchange)
    validate = validated_content_clarification if kind == "content" else validated_exit_context
    assert validate(session, turn, text, context) == context
    # Durable selection does not replace source-bound new consent.
    with pytest.raises(ApiError):
        validate(session, turn, text + "等一下，我继续。", context)
    turn["conversation_acts"][0]["selection_receipts"][0]["text_sha256"] = "0" * 64
    with pytest.raises(ApiError):
        validate(session, turn, text, context)


def test_delivery_is_bound_to_performance_and_terminal_interruption_is_not_completion():
    from test_content_clarification_066 import exchange
    session, turn, _, _ = durable(exchange)
    def status(value, performance="p1"):
        return update_delivery(session, event_id="selected", performance_id=performance,
                               status=value, updated_at="now")
    assert status("selected")
    assert status("playing")
    assert not status("delivered", "old_performance")
    assert status("interrupted")
    assert not status("delivered")
    history = recent_turn_dialogue(session, turn)
    assert history[0]["delivery_status"] == "interrupted"
    assert selected_events(session, turn)[0]["delivery"]["performance_id"] == "p1"


def test_unselected_approval_cannot_manufacture_history_or_confirmation():
    from test_content_clarification_066 import exchange
    session, turn, text, context = exchange()
    session["agent_events"] = []
    assert selected_events(session, turn) == []
    with pytest.raises(ApiError):
        validated_content_clarification(session, turn, text, context)


def test_reception_prompt_projects_only_safe_delivery_status():
    source = {"text": "刚才没听清。", "history": [
        {"role": "interviewer", "text": "解释", "delivery_status": "interrupted", "performance_id": "private"},
        {"role": "interviewer", "text": "解释2", "delivery_status": "forged"}]}
    before = deepcopy(source)
    history = reception_context(source)["history"]
    assert [row["delivery_status"] for row in history] == ["interrupted", "unknown"]
    assert not any("performance_id" in row for row in history)
    contract = reception_contract(source)
    assert contract.version == "conversation_reception.v10"
    assert "只有delivered" in contract.messages[0].content
    assert source == before


@pytest.mark.anyio
async def test_runtime_only_records_delivered_after_current_remote_drain(monkeypatch):
    from test_streaming_expression_lifecycle import _streaming_harness, _select, _signal, _session, _wait_until
    h = _streaming_harness(monkeypatch)
    def status():
        session = _session(h)
        return selected_events(session, session["turns"][0])[-1]["delivery"]["status"]
    try:
        performance = await _select(h)
        item = h.outputs[0]
        assert status() == "selected"
        await _signal(h, "avatar.performance.stopped", performance.performance_id, item.output.output_id, "drained")
        assert status() == "selected"
        await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
        await _wait_until(lambda: bool(item.publisher.published))
        await _wait_until(lambda: item.output.producer_finished)
        await _signal(h, "avatar.performance.stopped", performance.performance_id, item.output.output_id, "drained")
        assert status() == "delivered"
    finally:
        await h.channel.close("test_finished")


@pytest.mark.parametrize("recovered_status", ["playing", "delivered"])
@pytest.mark.parametrize("active", ["exact", "cleared", "other_performance", "other_selection"])
def test_failed_delivery_recovery_requires_the_exact_still_active_player(recovered_status, active):
    from test_content_clarification_066 import exchange
    session, turn, _, _ = durable(exchange)
    assert update_delivery(session, event_id="selected", performance_id="p1", status="failed", updated_at="failed")
    session["agent_runtime"] = {
        "active_performance_id": None if active == "cleared" else "p2" if active == "other_performance" else "p1",
        "active_expression_act_event_id": "other" if active == "other_selection" else "selected",
    }
    before = deepcopy(turn)
    assert update_delivery(session, event_id="selected", performance_id="p1",
                           status=recovered_status, updated_at="retried") is (active == "exact")
    if active != "exact":
        assert turn == before


@pytest.mark.anyio
@pytest.mark.parametrize("playing_ack", [True, False])
async def test_runtime_browser_retry_can_complete_the_same_failed_approved_audio(playing_ack):
    from app.domain.interview_agent import ClientSignal
    from test_expression_stale_fence import _harness, _select, _expression
    runtime, channel, _, _ = _harness()
    channel._evidence_session.assert_controller = lambda _: None
    async def expression(*args):
        return _expression()
    runtime._expression_audio = expression
    performance = await _select(channel)
    async def report(kind, key, **payload):
        await channel.send(ClientSignal(type=kind, idempotency_key=key, turn_id="turn_1",
            payload={"performance_id": performance.performance_id, **payload}))
    await report("avatar.performance.playback", "failed", status="failed")
    session = runtime.interviews.get_interview(channel.interview_id)
    assert selected_events(session, session["turns"][0])[-1]["delivery"]["status"] == "failed"
    if playing_ack:
        await report("avatar.performance.playback", "retry_playing", status="playing")
    await report("avatar.performance.stopped", "retry_completed")
    session = runtime.interviews.get_interview(channel.interview_id)
    assert selected_events(session, session["turns"][0])[-1]["delivery"]["status"] == "delivered"
    assert session["agent_runtime"]["floor"] == "candidate"
    assert session["agent_runtime"]["active_performance_id"] is None
    # The now stale player cannot overwrite its delivered fact.
    await report("avatar.performance.playback", "late_failed", status="failed")
    later = runtime.interviews.get_interview(channel.interview_id)
    assert selected_events(later, later["turns"][0])[-1]["delivery"] == selected_events(session, session["turns"][0])[-1]["delivery"]


@pytest.mark.anyio
async def test_superseded_controller_cannot_complete_pre_generated_playback():
    from app.domain.interview_agent import ClientSignal
    from app.services.evidence_coordination import assert_current_evidence_control
    from test_expression_stale_fence import _harness, _select, _expression
    runtime, channel, coordinator, grant = _harness()
    def assert_controller(_):
        with runtime.persistence.transaction(channel.organization_id) as tx:
            assert_current_evidence_control(tx, grant)
    async def detach(_):
        pass
    channel._evidence_session.assert_controller = assert_controller
    channel._evidence_session.detach = detach
    async def expression(*args):
        return _expression()
    runtime._expression_audio = expression
    performance = await _select(channel)
    coordinator.attach_control(interview_id=channel.interview_id, organization_id=channel.organization_id,
        connection_id="replacement_control", local_instance_id=grant.local_instance_id)
    with pytest.raises(ApiError) as error:
        assert_controller(channel)
    assert error.value.code == "EVIDENCE_CONTROL_STALE"
    before = runtime.interviews.get_interview(channel.interview_id)
    await channel.send(ClientSignal(type="avatar.performance.stopped", idempotency_key="stale_controller_stop",
        turn_id="turn_1", payload={"performance_id": performance.performance_id}))
    if channel._termination_task is not None:
        await channel._termination_task
    session = runtime.interviews.get_interview(channel.interview_id)
    assert session["agent_runtime"]["active_performance_id"] == performance.performance_id
    assert session["agent_runtime"]["floor"] == "agent"
    assert selected_events(session, session["turns"][0]) == selected_events(before, before["turns"][0])
