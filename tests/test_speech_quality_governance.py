"""Unknown acoustics, grounded clarification, and recoverable provisional scores."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.core.errors import ApiError
from app.core.prompt.contracts import prompt_contract, clarification_speech
from app.core.prompt.understanding_references import understanding_references, resolve_understanding_references
from app.core.prompt.validation import validate_structured_response
from app.domain.interview_agent import ClarificationTarget
from app.domain.scoring_quality import project_evaluation, project_report
from app.domain.speech_quality import minimum_reported_confidence, transcript_identity, transcript_is_verified
from app.model_gateway.schemas import TranscriptSegment, StreamingSTTEvent
from app.providers.media_http.provider import _confidence
from app.providers.volcengine.provider import _transcript_segments, _segment_confidence
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evaluation import EvaluationService
from app.services.interviews import InterviewService
from app.services.review import EnterpriseReviewService
from app.services.spoken_supplement import SpokenSupplementConfirmation
from app.schemas.api import TranscriptionVerification
from test_answer_endpoint import _endpoint, _final
from test_spoken_supplement import Capture
from test_declined_answer_evaluation import ORG, NOW, ScoringGateway
from test_interview_processing_recovery import pending_session
from test_understanding_safety import _Gateway, _turn, _utterance
from app.services.conversation_understanding import ConversationUnderstandingService


def test_missing_provider_measurement_stays_unknown_and_known_low_values_survive():
    assert TranscriptSegment(text="Redis").confidence is None
    assert _confidence({"segments": [{"avg_logprob": -.01}]}, [TranscriptSegment(text="Redis")]) is None
    segments = _transcript_segments([{"text": "术语"}, {"text": "续句", "words": [{"confidence": .3}]}])
    assert segments[0].confidence is None
    assert _segment_confidence(segments) == .3
    assert minimum_reported_confidence(None, .3, .99) == .3
    assert minimum_reported_confidence(None, None) is None
    final = _final(confidence=.99)
    final.segments[0].confidence = .2
    assert StreamingSTTEvent.model_validate(final.model_dump()).confidence == .2
    for invalid in (float("nan"), float("inf"), -1, 1.1):
        with pytest.raises(ValidationError):
            TranscriptSegment(text="合成", confidence=invalid)


@pytest.mark.anyio
@pytest.mark.parametrize("acoustic,action", [(None, "next"), (.3, "clarify")])
async def test_unknown_does_not_force_endless_clarification_but_measured_low_does(acoustic, action):
    utterance = _utterance("我用幂等键避免重复执行。")
    utterance.stt_confidence = acoustic
    data = {"clarification_target": None, "intent": "answer", "answer_summary": "通过幂等键去重", "claims": [{"claim": "幂等去重", "evidence_id": "E1"}],
            "evidence_ids": ["E1"], "covered_point_ids": ["P1"], "missing_point_ids": [],
            "ambiguities": [], "contradictions": [], "confidence": .9, "suggested_action": "next"}
    service = ConversationUnderstandingService(InMemoryStore(), gateway=_Gateway(data=data))
    result = await service.understand(utterance, _turn(), {"id": "synthetic", "organization_id": ORG})
    assert result.problem is None and result.suggested_action == action
    assert result.confidence == (.9 if acoustic is None else .3)


def test_clarification_focus_is_original_speech_and_cannot_invent_a_term():
    text = "我用 redios 做缓存。"
    refs = understanding_references(text, ["缓存"])
    wire = {"intent": "answer", "answer_summary": "缓存", "claims": [{"claim": "缓存", "evidence_id": "E1"}],
            "evidence_ids": ["E1"], "covered_point_ids": ["P1"], "missing_point_ids": [],
            "ambiguities": ["术语不明确"], "contradictions": [], "confidence": .5, "suggested_action": "clarify",
            "clarification_target": {"evidence_id": "E1", "focus_quote": "redios"}}
    contract = prompt_contract("interview_turn_understanding", {"transcript": text, "capability_points": ["缓存"]})
    validate_structured_response(wire, contract.response_schema)
    resolved = resolve_understanding_references(wire, refs)
    target = ClarificationTarget.model_validate(resolved["clarification_target"])
    assert "redios" in clarification_speech(target.focus_quote)
    wire["clarification_target"]["focus_quote"] = "RabbitMQ"
    with pytest.raises(ValueError):
        resolve_understanding_references(wire, refs)


@pytest.mark.anyio
async def test_targeted_clarification_keeps_capture_and_complete_answer_until_correction():
    capture = Capture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []
    async def speak(kind, guard, **kwargs):
        guard(); spoken.append((kind, kwargs)); return True
    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    original = _final("我先实现幂等，再用 redios。没有补充。", confidence=None)
    capture.current_final = original
    async def prepare(final):
        target = ClarificationTarget(evidence_quote="我先实现幂等，再用 redios。", focus_quote="redios")
        result = capture.prepared(final)
        result.understanding = SimpleNamespace(problem=None, suggested_action="clarify", clarification_target=target)
        return result
    capture.prepare_impl = prepare
    endpoint.confirmation.confirmed = True
    endpoint._proposal_revision = endpoint.revision
    await endpoint._propose(final_snapshot=original)
    assert spoken == [("answer_clarify", {"focus_quote": "redios"})]
    assert capture.is_open and not commits and capture.current_final.text == original.text
    assert endpoint.confirmation.speaking
    assert "answer_listening" not in notices, "Do not transfer the floor while clarification audio is playing"
    endpoint.confirmation.floor_returned(endpoint)
    clock.value += 100
    await endpoint.confirmation.step(endpoint, 100)
    assert len(spoken) == 1 and not commits, "Silence after a clarification cannot accept or endlessly re-ask it"
    corrected = _final(original.text + "我说的是 Redis，作为缓存，R E D I S。没有了。", confidence=None)
    capture.current_final = corrected
    capture.prepare_impl = None
    endpoint.speech_started()
    endpoint.confirmation.confirmed = True
    endpoint._proposal_revision = endpoint.revision
    await endpoint._propose(final_snapshot=corrected)
    assert len(commits) == 1 and commits[0].text.startswith(original.text)
    assert "Redis" in commits[0].text and capture.resumes >= 1
    await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_direct_score_is_available_before_optional_version_bound_correction(backend, tmp_path):
    store = InMemoryStore() if backend == "memory" else SQLiteStore(str(tmp_path / "speech.sqlite3"))
    session, persistence = pending_session(store)
    with persistence.transaction(ORG) as tx:
        current = tx.interview_sessions.get(session["id"])
        current["answers"][0]["stt_confidence"] = .3
        current["answers"][0]["stt_confidence_source"] = "provider"
        tx.interview_sessions.update(current, expected_version=current["version"])
    service = InterviewService(store, persistence=persistence)
    service.evaluation = EvaluationService(store, persistence=persistence, gateway=ScoringGateway())
    service.retry_processing(session["id"], ORG)
    with persistence.transaction(ORG) as tx:
        work = tx.outbox.list()
    for item in work:
        await service.process_outbox_work(item["id"], ORG)
    completed = service.get_interview(session["id"], ORG)
    report = service.reports.get_report(session["id"], ORG)
    assert report["overall_score"] == 85 and report["dimension_scores"]
    assert report["score_status"] == "available"
    assert report["recognition_warning_answer_ids"] == ["answer_0"]
    first = completed["answers"][0]
    old_evaluation = deepcopy(next(item for item in completed["evaluation_revisions"] if item["id"] == first["current_evaluation_id"]))
    assert old_evaluation["score"] == 85 and old_evaluation["recognition_warning"]
    assert old_evaluation["confidence"] == .3 and "low_stt_confidence" in old_evaluation["review_flags"]
    review = EnterpriseReviewService(store, persistence=persistence)
    assert review.complete_review(session["id"], {}, ORG)["ai_decision_used"] is False
    exported = service.reports.export_report(session["id"], export_format="csv", actor_id="reviewer", organization_id=ORG)
    assert "available" in exported["content"] and "recognition_notice" in exported["content"]
    payload = {"expected_evaluation_id": first["current_evaluation_id"], "expected_transcript_revision": 1,
               "audio_reviewed": True, "reason": "已回听并确认术语", "final_transcript": first["final_transcript"] + " 实际术语为Outbox。"}
    with pytest.raises(ApiError):
        service.verify_transcription(session["id"], first["id"], {**payload, "expected_evaluation_id": "stale"}, ORG, actor_id="reviewer")
    with pytest.raises(ApiError):
        service.verify_transcription(session["id"], first["id"], payload, "foreign", actor_id="reviewer")
    queued = service.verify_transcription(session["id"], first["id"], payload, ORG, actor_id="reviewer")
    assert queued["status"] == "queued"
    with pytest.raises(ApiError):
        service.verify_transcription(session["id"], first["id"], payload, ORG, actor_id="reviewer")
    during = service.get_interview(session["id"], ORG)
    assert transcript_is_verified(during["answers"][0])
    assert not transcript_is_verified({**during["answers"][0], "final_transcript": "another revision"})
    assert service.reports.get_report(session["id"], ORG)["overall_score"] is None
    with persistence.transaction(ORG) as tx:
        new_work = [item for item in tx.outbox.list() if item["kind"] == "answer.evaluate" and item["status"] == "pending"]
    assert len(new_work) == 1
    await service.process_outbox_work(new_work[0]["id"], ORG)
    after = service.get_interview(session["id"], ORG)
    assert service.reports.get_report(session["id"], ORG)["overall_score"] == 85
    assert next(item for item in after["evaluation_revisions"] if item["id"] == old_evaluation["id"]) == old_evaluation
    assert len(after["report_revisions"]) == 2
    assert after["answers"][0]["audio_uri"] == first["audio_uri"]
    assert after["answers"][0]["transcription_verification"]["reviewer_id"] == "reviewer"


def test_old_disputed_score_is_projected_without_changing_history_or_dropping_weight():
    evaluation = {"id": "eval", "answer_id": "a", "score": 66, "confidence": .8,
                  "dimension_scores": {"communication": 40}, "review_flags": ["transcription_ambiguity"]}
    report = {"id": "r", "overall_score": 76, "question_evaluations": [{"answer_id": "a", "evaluation_id": "eval", "score": 66}]}
    session = {"answers": [{"id": "a", "stt_confidence": 1.0}], "evaluation_revisions": [evaluation]}
    projected = project_report(report, session)
    assert projected["overall_score"] == 76 and projected["question_evaluations"][0]["score"] == 66
    assert projected["recognition_warning_answer_ids"] == ["a"]
    assert report["overall_score"] == 76 and evaluation["score"] == 66


@pytest.mark.parametrize("patch", [{"reason": " "}, {"audio_reviewed": False}, {"audio_reviewed": 1}, {"final_transcript": " "}, {"score": 100}, {"reviewer_id": "forged"}])
def test_review_api_rejects_invalid_or_forged_verification(patch):
    payload = {"expected_evaluation_id": "evaluation", "expected_transcript_revision": 1,
               "audio_reviewed": True, "reason": "已回听核验"}
    with pytest.raises(ValidationError):
        TranscriptionVerification.model_validate({**payload, **patch})


def test_real_verification_route_enforces_role_and_authenticated_reviewer(monkeypatch):
    from app.main import create_app
    from app.repositories.provider import reset_store_for_tests
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", ORG)
    monkeypatch.setenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", "synthetic-lookup-secret-at-least-32-characters")
    monkeypatch.setenv("INTERVIEWER_WEBSOCKET_TICKET_SECRET", "synthetic-websocket-secret-at-least-32-characters")
    monkeypatch.setenv("INTERVIEWER_API_TOKENS_JSON", json.dumps({
        "review-token": {"actor_id": "trusted_reviewer", "organization_id": ORG, "roles": ["reviewer"]},
        "interviewer-token": {"actor_id": "interviewer", "organization_id": ORG, "roles": ["interviewer"]},
    }))
    store = reset_store_for_tests()
    session, persistence = pending_session(store)
    with persistence.transaction(ORG) as tx:
        current = tx.interview_sessions.get(session["id"])
        for answer in current["answers"]:
            answer.update(evaluation_status="completed", current_evaluation_id="eval_" + answer["id"])
            current["evaluation_revisions"].append({"id": answer["current_evaluation_id"], "answer_id": answer["id"], "revision": 1})
        tx.interview_sessions.update(current, expected_version=current["version"])
    api = TestClient(create_app())
    path = "/api/v1/interviews/%s/answers/answer_0/transcription-verification" % session["id"]
    payload = {"expected_evaluation_id": "eval_answer_0", "expected_transcript_revision": 1,
               "audio_reviewed": True, "reason": "已回听合成录音"}
    assert api.post(path, json=payload).status_code == 401
    assert api.post(path, json=payload, headers={"Authorization": "Bearer interviewer-token"}).status_code == 403
    response = api.post(path, json=payload, headers={"Authorization": "Bearer review-token", "X-Actor-Id": "forged_actor"})
    assert response.status_code == 202, response.text
    with persistence.transaction(ORG) as tx:
        saved = tx.interview_sessions.get(session["id"])
    assert saved["answers"][0]["transcription_verification"]["reviewer_id"] == "trusted_reviewer"


def test_speech_style_protection_and_human_verification_are_in_versioned_scoring_prompt():
    contract = prompt_contract("answer_evaluation", {"question_text": "缓存", "standard_answer": "Redis与TTL",
        "answer_text": "嗯，我用redios，缓存，缓存热点数据，再设TTL。", "recognition_quality": [{"confidence": None, "transcript_verified": True}]})
    assert contract.version == "answer_evaluation.v5"
    assert all(value in contract.messages[0].content for value in ["停顿", "填充词", "口音", "不得作为技术分或表达分", "transcript_verified=true"])
    assert '"confidence": null' in contract.messages[-1].content


def test_current_understanding_wire_requires_explicit_clarification_target():
    contract = prompt_contract('interview_turn_understanding', {'transcript': '这个词没听清。', 'capability_points': []})
    valid = {'intent': 'clarification_request', 'answer_summary': '需要核对原话', 'claims': [],
        'evidence_ids': ['E1'], 'covered_point_ids': [], 'missing_point_ids': [],
        'ambiguities': ['需要回听'], 'contradictions': [], 'confidence': .4, 'suggested_action': 'clarify',
        'clarification_target': {'evidence_id': 'E1', 'focus_quote': '这个词'}}
    validate_structured_response(valid, contract.response_schema)
    with pytest.raises(ValueError):
        validate_structured_response({k:v for k,v in valid.items() if k != 'clarification_target'}, contract.response_schema)


def test_legacy_null_scores_restore_weighted_total_without_changing_stored_revisions():
    evaluations = [
        {'id': 'e1', 'answer_id': 'a1', 'score': None, 'provisional_score': 40,
         'dimension_scores': {}, 'provisional_dimension_scores': {'communication': 80},
         'score_status': 'pending_verification', 'review_flags': ['transcription_ambiguity']},
        {'id': 'e2', 'answer_id': 'a2', 'score': 80, 'dimension_scores': {'communication': 90}, 'review_flags': []},
    ]
    report = {'id': 'report', 'overall_score': None, 'score_status': 'pending_verification',
              'question_evaluations': [{'answer_id': 'a1', 'evaluation_id': 'e1', 'question_snapshot_id': 'q1', 'score': None},
                                       {'answer_id': 'a2', 'evaluation_id': 'e2', 'question_snapshot_id': 'q2', 'score': 80}]}
    session = {'answers': [{'id': 'a1'}, {'id': 'a2'}], 'evaluation_revisions': evaluations,
               'plan_snapshot': {'question_snapshots': [{'question_snapshot_id': 'q1', 'weight': 3, 'dimension': 'technical'},
                                                        {'question_snapshot_id': 'q2', 'weight': 1, 'dimension': 'technical'}]}}
    before = deepcopy(session)
    projected = project_report(report, session)
    assert projected['overall_score'] == 50 and projected['score_status'] == 'available'
    assert projected['dimension_scores'] == [{'dimension': 'technical', 'score': 50}]
    assert projected['recognition_warning_answer_ids'] == ['a1']
    assert project_report(projected, session) == projected
    assert project_evaluation(evaluations[0])['dimension_scores'] == {'communication': 80}
    assert report['overall_score'] is None and session == before


@pytest.mark.parametrize('value', [None, '66', True, float('nan'), -1, 101])
def test_failed_or_invalid_model_output_is_not_fabricated_into_a_score(value):
    result = project_evaluation({'score': value, 'dimension_scores': {}, 'review_flags': ['transcription_ambiguity']})
    assert result['score'] is None and result['score_status'] == 'unavailable'
