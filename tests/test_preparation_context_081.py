"""Retaining analysis does not grant permission to act on stale evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ApiError
from app.core.prompt.conversation_reception import RECEPTION_SPEECH, RECEPTION_SPEECH_VERSION
from app.services.conversation_delivery import record_selection, update_delivery
from test_prepared_decision_binding import _setup
from test_stable_preview_preparation import _preview


async def prepare(service, transcript):
    return await service.prepare_streaming_decision(
        "iv_binding", "turn_binding", transcript, semantic_first=True,
        snapshot_ref="evidence-checkpoint://synthetic/1/1",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("preview", [False, True])
async def test_matching_unconfirmed_analysis_can_be_reused_without_completion_permission(preview):
    service, final = _setup()
    transcript = _preview(final) if preview else final
    prepared = await prepare(service, transcript)
    before = service.get_interview("iv_binding")
    service.assert_streaming_preparation_current(prepared, transcript, "org_default", semantic_first=True)
    service.assert_streaming_preparation_current(prepared, final, "org_default", semantic_first=True)
    with pytest.raises(ApiError) as rejected:
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert rejected.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"confidence": .1}, {"language": "en-US"}, {"segments": []},
    {"text": "新增了候选人的补充内容。"},
])
async def test_changed_server_evidence_cannot_reuse_analysis(change):
    service, final = _setup()
    prepared = await prepare(service, final)
    with pytest.raises(ApiError) as rejected:
        service.assert_streaming_preparation_current(
            prepared, final.model_copy(update=change), "org_default", semantic_first=True,
        )
    assert rejected.value.code == "TURN_DECISION_STALE"


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    lambda s: s["turns"][0].update(question_spoken_text="另一个问题"),
    lambda s: s.update(followup_policy={"max_depth": 0}),
    lambda s: s.update(decision_revision=9),
    lambda s: s.setdefault("agent_runtime", {}).update(takeover={"status": "active"}),
])
async def test_current_context_is_checked_before_reusing_even_noncommitting_analysis(change):
    service, final = _setup()
    prepared = await prepare(service, final)
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        change(session)
        tx.interview_sessions.update(session, expected_version=session["version"])
    with pytest.raises(ApiError) as rejected:
        service.assert_streaming_preparation_current(prepared, final, "org_default", semantic_first=True)
    assert rejected.value.code == "TURN_DECISION_STALE"


@pytest.mark.anyio
@pytest.mark.parametrize("kwargs", [{}, {"semantic_first": True, "completion_confirmed": True}])
async def test_preparation_mode_is_part_of_reuse_identity(kwargs):
    service, final = _setup()
    prepared = await prepare(service, final)
    with pytest.raises(ApiError) as rejected:
        service.assert_streaming_preparation_current(prepared, final, "org_default", **kwargs)
    assert rejected.value.code == "TURN_DECISION_STALE"


def select_notice(service, *, recovery=True, version=RECEPTION_SPEECH_VERSION, status="selected",
                  act_type="conversation_acknowledgement", act_id="recovery_notice", linked=False):
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        text = RECEPTION_SPEECH["unavailable"] if recovery else "合成的另一个问题"
        session["turns"][0]["conversation_acts"].append({
            "act_id": act_id, "turn_id": "turn_binding",
            "act_type": act_type, "text": text, "prompt_version": version,
        })
        sequence = len(session.get("agent_events", [])) + 1
        event = {"type": "conversation.act.selected", "event_id": "event_" + act_id, "turn_id": "turn_binding",
                 "session_sequence": sequence, "occurred_at": "2026-10-26T09:15:36Z",
                 "payload": {"act_id": act_id, "text": text, "approved": True}}
        session.setdefault("agent_events", []).append(event)
        record_selection(session, event)
        update_delivery(session, event_id=event["event_id"], performance_id="performance_notice",
                        status=status, updated_at="2026-10-26T09:15:45Z")
        if linked:
            session["turns"][0].setdefault("reception_exchanges", []).append({
                "act_id": act_id, "selected_event_id": event["event_id"], "session_sequence": sequence,
                "request_text": "合成候选人请求", "exchange_id": "exchange_notice",
            })
        tx.interview_sessions.update(session, expected_version=session["version"])


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["selected", "playing", "delivered", "interrupted", "failed"])
async def test_heartbeat_and_recovery_notice_do_not_change_reasoning_context(status):
    service, final = _setup()
    prepared = await prepare(service, final)
    service.record_heartbeat("iv_binding", participant="candidate")
    select_notice(service, status=status)
    service.assert_streaming_preparation_current(prepared, final, "org_default", semantic_first=True)


@pytest.mark.anyio
@pytest.mark.parametrize("options", [
    {"recovery": False}, {"version": "unknown_speech_version"}, {"act_type": "supplement_check"}, {"linked": True},
])
async def test_other_selected_speech_is_still_a_change_of_context(options):
    service, final = _setup()
    prepared = await prepare(service, final)
    select_notice(service, **options)
    with pytest.raises(ApiError) as rejected:
        service.assert_streaming_preparation_current(prepared, final, "org_default", semantic_first=True)
    assert rejected.value.code == "TURN_DECISION_STALE"


@pytest.mark.anyio
async def test_status_notice_does_not_evict_real_dialogue_from_fingerprint_window():
    service, final = _setup()
    for index in range(4):
        select_notice(service, recovery=False, act_id="question_%s" % index)
    prepared = await prepare(service, final)
    select_notice(service, status="delivered")
    service.assert_streaming_preparation_current(prepared, final, "org_default", semantic_first=True)


@pytest.mark.anyio
async def test_completed_followup_cannot_outlive_its_domain_time_budget():
    service, final = _setup()
    prepared = replace(await prepare(service, final), followup={"selected": True})
    service.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=21)
    with pytest.raises(ApiError) as rejected:
        service.assert_streaming_preparation_current(prepared, final, "org_default", semantic_first=True)
    assert rejected.value.code == "TURN_DECISION_STALE"


@pytest.mark.anyio
async def test_unstable_preview_cannot_read_completed_analysis():
    service, final = _setup()
    prepared = await prepare(service, final)
    with pytest.raises(ApiError) as rejected:
        service.assert_streaming_preparation_current(
            prepared, _preview(final, has_unstable_tail=True), "org_default", semantic_first=True,
        )
    assert rejected.value.code == "STREAMING_TRANSCRIPT_INVALID"
