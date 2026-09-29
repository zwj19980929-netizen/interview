"""Repeated requests are located by one authoritative audio occurrence."""
from copy import deepcopy
import json

import pytest

from app.domain.speech_quality import control_evidence_confidence, transcript_confidence_segments
from app.model_gateway.schemas import TranscriptSegment
from app.services.conversation_input_context import (
    capture_source_fingerprint, conversation_input_context, reception_source_boundary,
)
from test_answer_endpoint import _final
from test_conversation_input_context_064 import _session
from test_reception_history_062 import bundle, select, snapshot


CAPTURE = {"stream_id": "synthetic-audio", "capture_revision": 7}
SNAPSHOT = "evidence-checkpoint://synthetic-audio/7/32"


def _spans(*parts):
    text = "".join(parts)
    segments = [{"text": part, "start_ms": i * 1000, "end_ms": (i + 1) * 1000,
                 "confidence": .95} for i, part in enumerate(parts)]
    return transcript_confidence_segments(text, segments)


def _source_session(request="FluxDB 是什么？", *, prior=""):
    session, turn = _session([(request, "question_clarification", "它是虚构数据库。")])
    text = prior + request
    turn["reception_exchanges"][0]["source_boundary"] = reception_source_boundary(
        text, request, segments=_spans(*([prior] if prior else []), request), source_capture=CAPTURE)
    return session, turn, text


def test_identical_new_question_is_fresh_even_when_an_old_occurrence_has_a_receipt():
    request, new = "FluxDB 是什么？", "我刚才没听清。"
    session, turn, old = _source_session(request)
    text = old + new + request
    before = deepcopy(session)
    result = conversation_input_context(session, turn, text,
        segments=_spans(old, new, request), source_capture=SNAPSHOT)
    assert result["current_speech"] == new + request
    assert result["current_speech_start"] == len(old)
    assert len(result["responded_exchanges"]) == 1
    assert result["responded_exchanges"][0]["request_start"] == 0
    assert session == before and text == old + new + request
    # Capture identity is stable across checkpoint seals, not across captures.
    assert capture_source_fingerprint(CAPTURE) == capture_source_fingerprint(SNAPSHOT)
    assert capture_source_fingerprint(SNAPSHOT.replace("/32", "/33")) == capture_source_fingerprint(CAPTURE)


def test_coarse_overlapping_source_times_preserve_current_scope_without_acoustic_uplift():
    request, wait, ending = "FluxDB 是什么？", "让我想一下。", "我讲完了。"
    session, turn = _session([(request, "question_clarification", "它是虚构数据库。"),
                             (wait, "wait", "好的。")])
    text = request + wait + ending
    raw = [{"text": part, "start_ms": i * 100, "end_ms": i * 100 + 200,
            "confidence": .3 if i == 0 else .99} for i, part in enumerate((request, wait, ending))]
    for index, (prefix, current) in enumerate(((request, request), (request + wait, wait))):
        turn["reception_exchanges"][index]["source_boundary"] = reception_source_boundary(
            prefix, current, segments=transcript_confidence_segments(prefix, raw[:index + 1]), source_capture=CAPTURE)
    spans = transcript_confidence_segments(text, raw)
    assert [(span["start_ms"], span["end_ms"]) for span in spans] == [(0, 200), (100, 300), (200, 400)]
    result = conversation_input_context(session, turn, text, segments=spans, source_capture=SNAPSHOT)
    assert result["current_speech"] == ending and len(result["responded_exchanges"]) == 2
    assert control_evidence_confidence(text, spans, [(len(request + wait), len(text))]) is None


def test_duplicated_audio_interval_cannot_prove_a_different_source_occurrence():
    first, second = "FluxDB 是什么？", "请重读题目。"
    spans = transcript_confidence_segments(first + second, [
        {"text": first, "start_ms": 0, "end_ms": 200, "confidence": .9},
        {"text": second, "start_ms": 0, "end_ms": 200, "confidence": .9}])
    assert reception_source_boundary(first + second, second, segments=spans, source_capture=CAPTURE) is None
    assert control_evidence_confidence(first + second, spans, [(len(first), len(first + second))]) is None


def test_a_later_receipt_locates_its_own_repeated_request_and_preserves_technical_prefix():
    old, answer, request = "请重读题目。", "我会保存确认位置。", "请重读题目。"
    session, turn = _session([(request, "repeat", "正式问题。")])
    receipt_text = old + answer + request
    proof = reception_source_boundary(receipt_text, request,
        segments=_spans(old, answer, request), source_capture=CAPTURE)
    turn["reception_exchanges"][0]["source_boundary"] = proof
    text = receipt_text + "我讲完了。"
    result = conversation_input_context(session, turn, text,
        segments=_spans(old, answer, request, "我讲完了。"), source_capture=SNAPSHOT)
    assert result["current_speech"] == "我讲完了。"
    assert result["responded_exchanges"][0]["request_start"] == len(old + answer)
    assert answer in text and answer not in json.dumps(proof, ensure_ascii=False)


@pytest.mark.parametrize("mutation", [
    "other_stream", "other_capture", "missing_capture", "rewrite_prefix", "rewrite_request",
    "audio_changed", "segment_changed", "missing_segments", "merged_segments", "zero_duration",
    "malformed_proof", "missing_proof", "extra_field", "prefix_hash", "offset_bool",
])
def test_unverifiable_source_keeps_the_full_original_without_unique_text_fallback(mutation):
    prior, request, new = "我会保存状态。", "FluxDB 是什么？", "我讲完了。"
    session, turn, old = _source_session(request, prior=prior)
    text, capture = old + new, deepcopy(CAPTURE)
    spans = _spans(prior, request, new)
    proof = turn["reception_exchanges"][0]["source_boundary"]
    if mutation == "other_stream": capture["stream_id"] = "other"
    elif mutation == "other_capture": capture["capture_revision"] += 1
    elif mutation == "missing_capture": capture = None
    elif mutation == "rewrite_prefix": text = text.replace("状态", "快照"); spans = _spans("我会保存快照。", request, new)
    elif mutation == "rewrite_request": text = text.replace("FluxDB", "FlexDB"); spans = _spans(prior, "FlexDB 是什么？", new)
    elif mutation == "audio_changed": spans[1]["start_ms"] += 1
    elif mutation == "segment_changed": proof["segments"][0]["text_sha256"] = "0" * 64
    elif mutation == "missing_segments": spans = []
    elif mutation == "merged_segments": spans = _spans(prior + request, new)
    elif mutation == "zero_duration": spans[0]["end_ms"] = 0
    elif mutation == "malformed_proof": turn["reception_exchanges"][0]["source_boundary"] = "not-proof"
    elif mutation == "missing_proof": turn["reception_exchanges"][0]["source_boundary"] = None
    elif mutation == "extra_field": proof["invented_authority"] = True
    elif mutation == "prefix_hash": proof["prefix_sha256"] = "0" * 64
    elif mutation == "offset_bool": proof["request_start"] = True
    result = conversation_input_context(session, turn, text, segments=spans, source_capture=capture)
    assert result["current_speech"] == text and result["current_speech_start"] == 0
    assert result["responded_exchanges"] == []


@pytest.mark.anyio
async def test_server_receipt_persists_source_boundary_without_creating_answer_evidence(bundle):
    chain, persistence = bundle
    old, new = "FluxDB 是什么？", "我会保存确认位置。"
    chain._stt.last_media_checkpoint = CAPTURE
    chain.stage_reception_exchange(_final(old), old, "question_clarification")
    select(persistence, 1)
    assert chain.record_reception_exchange(performance_id="performance_1", guard=lambda: None)
    saved = snapshot(persistence)
    turn = saved["turns"][0]
    proof = turn["reception_exchanges"][0]["source_boundary"]
    assert proof["capture_fingerprint"] == capture_source_fingerprint(CAPTURE)
    assert "FluxDB" not in json.dumps(proof) and not saved["answers"] and not turn["utterances"]
    assert conversation_input_context(saved, turn, old + new, segments=_spans(old, new),
        source_capture=SNAPSHOT)["current_speech"] == new


@pytest.mark.anyio
async def test_capture_reopened_on_same_object_cannot_record_old_request(bundle):
    chain, persistence = bundle
    chain._stt.last_media_checkpoint = deepcopy(CAPTURE)
    chain.stage_reception_exchange(_final("请重读。"), "请重读。", "repeat")
    select(persistence, 1)
    chain._stt.last_media_checkpoint["capture_revision"] += 1
    assert not chain.record_reception_exchange(performance_id="performance_1", guard=lambda: None)
    assert not snapshot(persistence)["turns"][0].get("reception_exchanges")


@pytest.mark.anyio
async def test_new_receipt_with_no_source_proof_cannot_silently_use_legacy_text_cursor(bundle):
    chain, persistence = bundle
    text = "请重读。"
    chain.stage_reception_exchange(_final(text), text, "repeat")
    select(persistence, 1)
    assert chain.record_reception_exchange(performance_id="performance_1", guard=lambda: None)
    saved = snapshot(persistence)
    turn = saved["turns"][0]
    assert "source_boundary" in turn["reception_exchanges"][0]
    assert conversation_input_context(saved, turn, text)["current_speech"] == text


def test_prepared_context_is_invalidated_when_source_cursor_changes():
    from app.services.interviews import InterviewService
    session, turn, _ = _source_session()
    previous = InterviewService._prepared_context_fingerprint(session, turn)
    turn["reception_exchanges"][0]["source_boundary"]["segments"][0]["start_ms"] += 1
    assert InterviewService._prepared_context_fingerprint(session, turn) != previous


@pytest.mark.anyio
async def test_supervisor_projects_authoritative_and_preview_scopes_from_same_source():
    from types import SimpleNamespace
    from app.services.interviewer_supervisor.service import InterviewerSupervisor
    from test_prepared_turn_decision import _input
    from test_stable_preview_preparation import _preview
    request, new = "FluxDB 是什么？", "我讲完了。"
    session, turn, _ = _source_session(request)
    text = request + new
    utterance, _, _ = _input()
    utterance = utterance.model_copy(update={"text": text, "audio_uri": SNAPSHOT, "stt_segments": _spans(request, new)})
    received = []
    async def capture(value, turn, session, **kwargs):
        received.append(session["_interviewer_context"]["input_context"])
    supervisor = InterviewerSupervisor(gateway=None, persistence=None,
        conversation=SimpleNamespace(prepare_decision=capture, prepare_preview=capture))
    await supervisor.prepare_turn(utterance, turn, session)
    final = _final(text).model_copy(update={"segments": [
        TranscriptSegment(text=part, start_ms=i * 1000, end_ms=(i + 1) * 1000, confidence=.95)
        for i, part in enumerate((request, new))]})
    await supervisor.prepare_preview(_preview(final), turn, session, snapshot_ref=SNAPSHOT)
    assert len(received) == 2 and received[0] == received[1]
    assert received[0]["current_speech"] == new
