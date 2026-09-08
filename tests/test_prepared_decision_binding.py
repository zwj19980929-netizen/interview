from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.model_gateway.schemas import ProviderMeta, StreamingSTTEvent, TranscriptSegment
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService


TEXT = "我使用幂等键记录已经完成的请求。"


def _setup():
    store = InMemoryStore()
    now = utc_now()
    turn = {
        "id": "turn_binding", "interview_id": "iv_binding",
        "question_id": "question_binding", "question_snapshot_id": "snapshot_binding",
        "question_spoken_text": "请解释如何防止重复请求。",
        "question_snapshot": {
            "question_text": "请解释如何防止重复请求。",
            "standard_answer": "使用请求标识记录处理状态。",
            "key_points": [{"text": "幂等键"}], "difficulty": "mid", "rubric": {},
        },
        "order": 1, "phase": "position_bank", "status": "asking",
        "is_followup": False, "root_turn_id": "turn_binding", "followup_depth": 0,
        "allow_followup": False, "weight": 1.0, "utterances": [],
        "conversation_acts": [], "started_at": now, "completed_at": None,
    }
    session = {
        "id": "iv_binding", "organization_id": "org_default", "status": "in_progress",
        "phase": "position_bank", "current_turn_id": "turn_binding", "turn_ids": ["turn_binding"],
        "turns": [turn], "answers": [], "evaluation_revisions": [], "report_revisions": [],
        "lifecycle_events": [], "created_at": now, "updated_at": now,
        "last_activity_at": now, "started_at": now, "settings": {"language": "zh-CN"},
        "scheduled_end_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
    }
    with persistence_for(store).transaction("org_default") as transaction:
        transaction.interview_sessions.add(session)
    final = StreamingSTTEvent(
        stream_id="stream_binding", sequence=9, type="transcript.final", text=TEXT,
        language="zh-CN", confidence=0.95, is_final=True,
        segments=[TranscriptSegment(text=TEXT, confidence=0.9, start_ms=0, end_ms=1300)],
        provider=ProviderMeta(provider_id="synthetic_stt", model="synthetic_model", request_id="request_final", latency_ms=7),
    )
    return InterviewService(store), final


def _payload(final):
    return {
        "turn_id": "turn_binding", "audio_uri": "private-file://synthetic_recording",
        "content_type": "audio/wav", "final_transcript": final.text,
        "confidence": final.confidence, "language": final.language,
        "segments": [item.model_dump(mode="json") for item in final.segments],
        "provider": final.provider.model_dump(mode="json"),
    }


async def _prepared(service, final):
    return await service.prepare_streaming_decision(
        "iv_binding", "turn_binding", final, snapshot_ref="private-file://synthetic_prefix",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mutate", [
    lambda p: p.update(confidence=0.1),
    lambda p: p.update(language="en-US"),
    lambda p: p.update(segments=[]),
    lambda p: p["segments"][0].update(text="不同的分段证据"),
    lambda p: p["segments"][0].update(confidence=0.1),
    lambda p: p["segments"][0].update(start_ms=20),
    lambda p: p["segments"][0].update(end_ms=1400),
    lambda p: p["provider"].update(provider_id="different_stt"),
    lambda p: p["provider"].update(model="different_model"),
    lambda p: p["provider"].update(request_id="different_final"),
    lambda p: p["provider"].update(latency_ms=8),
])
async def test_equal_text_cannot_reuse_decision_for_changed_stt_evidence(mutate):
    service, final = _setup()
    prepared = await _prepared(service, final)
    payload = _payload(final)
    mutate(payload)
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as exc:
        await service.submit_streaming_answer("iv_binding", payload, prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
@pytest.mark.parametrize("mutate", [
    lambda s: s["turns"][0].update(question_spoken_text="新的题目"),
    lambda s: s["turns"][0]["question_snapshot"].update(standard_answer="新的标准答案"),
    lambda s: s["turns"][0]["question_snapshot"].update(difficulty="expert"),
    lambda s: s["turns"][0]["question_snapshot"].update(key_points=[{"text": "新的能力点"}]),
    lambda s: s["turns"][0].update(allow_followup=True),
    lambda s: s["turns"][0].update(followup_depth=1),
    lambda s: s["turns"][0].update(current_understanding={"revision": 2, "understanding_id": "newer"}),
    lambda s: s.update(followup_policy={"max_per_root": 0}),
    lambda s: s.update(scheduled_end_at="2000-01-01T00:00:00Z"),
    lambda s: s["turns"].append({"id": "new_probe", "is_followup": True, "root_turn_id": "turn_binding"}),
])
async def test_changed_question_or_decision_budget_invalidates_prepared_result(mutate):
    service, final = _setup()
    prepared = await _prepared(service, final)
    with service.persistence.transaction("org_default") as transaction:
        current = transaction.interview_sessions.get("iv_binding")
        mutate(current)
        transaction.interview_sessions.update(current, expected_version=current["version"])
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as exc:
        await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_heartbeat_and_text_edge_whitespace_do_not_invalidate_valid_preparation():
    service, final = _setup()
    final = final.model_copy(update={"text": "  " + TEXT + "\n"})
    prepared = await _prepared(service, final)
    assert prepared.text == TEXT
    service.record_heartbeat("iv_binding", participant="candidate")
    payload = _payload(final)
    payload["final_transcript"] = TEXT
    result = await service.submit_streaming_answer("iv_binding", payload, prepared_decision=prepared)
    assert result["accepted"] is True
    assert result["answer"]["stt_confidence"] == 0.95
    assert result["answer"]["transcript_segments"] == payload["segments"]


@pytest.mark.anyio
async def test_authoritative_accept_entry_independently_rejects_changed_confidence():
    service, final = _setup()
    prepared = await _prepared(service, final)
    payload = _payload(final)
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as exc:
        await service._accept_authoritative_transcript("iv_binding", {
            "turn_id": "turn_binding", "audio_uri": payload["audio_uri"],
            "final_transcript": TEXT, "stt_confidence": 0.2,
            "language": payload["language"], "stt_provider": payload["provider"],
            "transcript_segments": payload["segments"], "transcript_source": "server_streaming",
        }, prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_context_is_bound_before_model_await_not_after_it_returns():
    service, final = _setup()
    original = service.conversation.prepare_decision

    async def changed_during_inference(utterance, turn, interview):
        result = await original(utterance, turn, interview)
        with service.persistence.transaction("org_default") as transaction:
            current = transaction.interview_sessions.get("iv_binding")
            current["followup_policy"] = {"max_depth": 0}
            transaction.interview_sessions.update(current, expected_version=current["version"])
        return result

    service.conversation.prepare_decision = changed_during_inference
    prepared = await _prepared(service, final)
    with pytest.raises(ApiError) as exc:
        await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"


@pytest.mark.anyio
async def test_selected_probe_expiring_time_budget_cannot_reuse_unchanged_anchor():
    service, final = _setup()
    prepared = await _prepared(service, final)
    # The test exercises the commit time gate without generating or playing a
    # probe; even an otherwise matching prepared proposal must be rejected.
    prepared = replace(prepared, followup={"selected": True})
    service.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=21)
    with pytest.raises(ApiError) as exc:
        await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"


@pytest.mark.anyio
@pytest.mark.parametrize("text", [TEXT, "请再说一遍。"])
async def test_stale_context_is_rechecked_inside_final_effect_transaction(monkeypatch, text):
    service, final = _setup()
    final = final.model_copy(update={
        "text": text,
        "segments": [final.segments[0].model_copy(update={"text": text})],
    })
    prepared = await _prepared(service, final)
    original_assert = service._assert_prepared_decision
    checks = 0

    def change_after_preflight(prepared, session, turn, payload):
        nonlocal checks
        checks += 1
        original_assert(prepared, session, turn, payload)
        if checks == 2:
            with service.persistence.transaction("org_default") as transaction:
                current = transaction.interview_sessions.get("iv_binding")
                current["followup_policy"] = {"max_depth": 0}
                transaction.interview_sessions.update(current, expected_version=current["version"])

    monkeypatch.setattr(service, "_assert_prepared_decision", change_after_preflight)
    with pytest.raises(ApiError) as exc:
        await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"
    current = service.get_interview("iv_binding")
    assert current["answers"] == []
    assert current["turns"][0]["utterances"] == []
    assert all(e["type"] != "answer.submitted" for e in current["lifecycle_events"])
