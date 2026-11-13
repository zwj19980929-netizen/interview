"""The candidate HTTP boundary must preserve answers without exposing review data."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.core.rate_limit import public_rate_limiter
from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import reset_store_for_tests
from app.services.interviews import InterviewService


@pytest.fixture
def candidate_history_api(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    monkeypatch.setenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "synthetic-candidate-history-signing-key-056")
    # Keep production admission/authentication active without contacting Redis.
    class SyntheticLimiterRedis:
        async def incr(self, key):
            return 1

        async def expire(self, key, window):
            return True

    monkeypatch.setenv("INTERVIEWER_REDIS_URL", "redis://synthetic-only")
    monkeypatch.setattr(public_rate_limiter, "_redis_client", lambda url: SyntheticLimiterRedis())
    store = reset_store_for_tests()
    session = {
        "id": "iv_synthetic_history", "organization_id": "org_default", "version": 1,
        "created_at": "2026-09-19T00:00:00Z", "status": "in_progress",
        "current_turn_id": "child", "candidate": {"name": "合成候选人", "email": "PRIVATE_EMAIL"},
        "internal_prompt": "PRIVATE_PROMPT", "settings": {"provider_token": "PRIVATE_TOKEN"},
        "turns": [
            {"id": "parent", "order": 1, "status": "completed", "question_spoken_text": "如何发布？",
             "standard_answer": "PRIVATE_REFERENCE", "scoring_criteria": "PRIVATE_CRITERIA"},
            {"id": "child", "order": 2, "status": "evaluating", "question_spoken_text": "怎样回滚？",
             "is_followup": True, "parent_turn_id": "parent", "root_turn_id": "parent", "followup_depth": 1},
            {"id": "future", "order": 3, "status": "pending", "question_spoken_text": "PRIVATE_FUTURE_QUESTION"},
        ],
        "answers": [
            {"id": "parent_answer", "turn_id": "parent", "evaluation_status": "completed",
             "final_transcript": "主回答：滚动发布。", "audio_uri": "PRIVATE_AUDIO", "evaluation": "PRIVATE_SCORE"},
            {"id": "child_answer", "turn_id": "child", "evaluation_status": "pending",
             "final_transcript": "子回答：恢复上一版本。", "segments": [{"text": "PRIVATE_PROVIDER_SEGMENT"}]},
        ],
    }
    other = deepcopy(session)
    other["id"] = "iv_synthetic_other"
    other["answers"][0]["final_transcript"] = "PRIVATE_OTHER_SESSION_ANSWER"
    foreign = deepcopy(other)
    foreign.update(id="iv_synthetic_foreign", organization_id="other_org")
    persistence = persistence_for(store)
    for value in (session, other, foreign):
        with persistence.transaction(value["organization_id"]) as transaction:
            transaction.interview_sessions.add(value)
    service = InterviewService(store)
    # Do not enter the application lifespan or start background/media workers.
    api = TestClient(create_app())
    yield api, session, service._candidate_session_token(session), other, service._candidate_session_token(other), foreign, service._candidate_session_token(foreign)
    api.close()


def test_final_http_projection_keeps_separate_authoritative_answers_and_hides_private_data(candidate_history_api):
    api, session, token, *_ = candidate_history_api
    response = api.get(f"/api/v1/public/interviews/{session['id']}", headers={"X-Candidate-Session-Token": token})

    assert response.status_code == 200
    payload = response.json()
    assert payload["answers"] == [
        {"id": "parent_answer", "turn_id": "parent", "evaluation_status": "completed", "final_transcript": "主回答：滚动发布。"},
        {"id": "child_answer", "turn_id": "child", "evaluation_status": "pending", "final_transcript": "子回答：恢复上一版本。"},
    ]
    child = payload["turns"][1]
    assert child["question_spoken_text"] == "怎样回滚？"
    assert (child["parent_turn_id"], child["root_turn_id"], child["followup_depth"]) == ("parent", "parent", 1)
    assert not payload["turns"][2].get("question_spoken_text")
    assert payload["candidate"] == {"name": "合成候选人"}
    assert "PRIVATE_" not in response.text
    assert token not in response.text


@pytest.mark.parametrize("token_source, expected_status", [("missing", 422), ("invalid", 403), ("other_session", 403)])
def test_candidate_history_route_rejects_missing_invalid_and_cross_session_tokens(candidate_history_api, token_source, expected_status):
    api, session, _, _, other_token, *_ = candidate_history_api
    token = {"missing": None, "invalid": "invalid-synthetic-token", "other_session": other_token}[token_source]
    headers = {"X-Candidate-Session-Token": token} if token else {}
    response = api.get(f"/api/v1/public/interviews/{session['id']}", headers=headers)

    assert response.status_code == expected_status
    assert "answers" not in response.json()
    assert "滚动发布" not in response.text and "恢复上一版本" not in response.text
    assert "PRIVATE_" not in response.text
    assert other_token not in response.text


def test_candidate_history_route_keeps_deployment_tenant_isolation(candidate_history_api):
    api, _, _, _, _, foreign, foreign_token = candidate_history_api
    response = api.get(f"/api/v1/public/interviews/{foreign['id']}", headers={"X-Candidate-Session-Token": foreign_token})

    assert response.status_code == 404
    assert "answers" not in response.json()
    assert "PRIVATE_" not in response.text
