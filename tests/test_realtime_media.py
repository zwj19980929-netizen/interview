from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.provider import reset_store_for_tests


def _create_started_interview(api: TestClient, *, start: bool = True) -> dict:
    question = api.post(
        "/api/v1/questions",
        json={
            "knowledge_base_id": "kb_realtime",
            "title": "实时系统设计",
            "question_text": "请说明实时语音面试中的断线恢复策略。",
            "standard_answer": "保存会话状态、音频序号和最终转写，重连后从最近轮次恢复。",
            "key_points": ["保存会话状态", "音频序号", "从最近轮次恢复"],
            "difficulty": "senior",
            "skills": ["realtime", "system_design"],
        },
    )
    assert question.status_code == 200, question.text

    role = api.post(
        "/api/v1/role-requirements",
        json={
            "title": "实时系统工程师",
            "description": "负责 realtime system design 和线上恢复。",
            "must_have_skills": ["realtime", "system_design"],
            "seniority": "senior",
            "interview_duration_minutes": 20,
        },
    )
    assert role.status_code == 200, role.text

    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role.json()["id"],
            "knowledge_base_ids": ["kb_realtime"],
            "question_count": 1,
        },
    )
    assert plan.status_code == 200, plan.text

    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan.json()["id"],
        json={"expected_version": plan.json()["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text

    interview = api.post(
        "/api/v1/interviews",
        json={
            "plan_id": plan.json()["id"],
            "candidate": {"name": "实时候选人", "email": "realtime@example.com"},
            "settings": {"record_audio": True, "record_video": False, "avatar_id": "avatar_default_cn"},
        },
    )
    assert interview.status_code == 200, interview.text
    if start:
        started = api.post("/api/v1/interviews/%s/start" % interview.json()["id"])
        assert started.status_code == 200, started.text
    return interview.json()


def test_avatar_and_realtime_audio_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)
    interview_id = interview["id"]

    detail = api.get("/api/v1/interviews/%s" % interview_id).json()
    turn_id = detail["current_turn_id"]
    avatar = api.post(
        "/api/v1/interviews/%s/avatar/speak" % interview_id,
        json={"turn_id": turn_id, "language": "zh-CN", "voice": "default"},
    )
    assert avatar.status_code == 200, avatar.text
    assert avatar.json()["mode"] == "browser_speech"
    assert avatar.json()["text"] == detail["turns"][0]["question_spoken_text"]
    assert avatar.json()["provider"]["provider_id"] == "mock"

    websocket_url = "/api/v1/interviews/%s/live?role=candidate&token=%s" % (
        interview_id,
        interview["candidate_session_token"],
    )
    with api.websocket_connect(websocket_url) as websocket:
        assert websocket.receive_json()["type"] == "session.state.changed"
        assert websocket.receive_json()["type"] == "question.selected"

        websocket.send_json({"type": "session.ready", "payload": {"source": "test_candidate"}})
        assert websocket.receive_json()["type"] == "session.participant.ready"
        ready_state = websocket.receive_json()
        assert ready_state["type"] == "session.state.changed"
        assert ready_state["payload"]["status"] == "in_progress"

        websocket.send_json(
            {
                "type": "candidate.media.start",
                "turn_id": turn_id,
                "payload": {"mime_type": "audio/webm;codecs=opus", "timeslice_ms": 400},
            }
        )
        assert websocket.receive_json()["type"] == "media.recording.started"
        websocket.send_bytes(b"mock-webm-opus-audio")
        websocket.send_json({"type": "candidate.media.stop", "turn_id": turn_id, "payload": {}})
        recording_stopped = websocket.receive_json()
        assert recording_stopped["type"] == "media.recording.stopped"
        assert recording_stopped["payload"]["byte_count"] == len(b"mock-webm-opus-audio")

        websocket.send_json(
            {
                "type": "candidate.transcript.final",
                "turn_id": turn_id,
                "payload": {
                    "text": "我会保存会话状态和音频序号，断线重连后从最近轮次恢复。",
                    "confidence": 0.88,
                    "language": "zh-CN",
                    "source": "browser_speech_fallback",
                    "duration_seconds": 12,
                },
            }
        )
        event_types = [websocket.receive_json()["type"] for _ in range(4)]
        assert event_types == [
            "stt.transcript.final",
            "evaluation.started",
            "evaluation.completed",
            "interview.completed",
        ]

    completed = api.get("/api/v1/interviews/%s" % interview_id)
    assert completed.status_code == 200
    assert completed.json()["status"] == "report_ready"
    assert len(completed.json()["answers"]) == 1
    audio_uri = completed.json()["answers"][0]["audio_uri"]
    assert audio_uri == recording_stopped["payload"]["audio_uri"]

    recording = api.get(audio_uri)
    assert recording.status_code == 200
    assert recording.content == b"mock-webm-opus-audio"


def test_candidate_websocket_rejects_invalid_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)

    with api.websocket_connect(
        "/api/v1/interviews/%s/live?role=candidate&token=invalid" % interview["id"]
    ) as websocket:
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "CANDIDATE_SESSION_TOKEN_INVALID"


def test_websocket_ready_and_rest_start_share_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api, start=False)
    interview_id = interview["id"]

    websocket_url = "/api/v1/interviews/%s/live?role=candidate&token=%s" % (
        interview_id,
        interview["candidate_session_token"],
    )
    with api.websocket_connect(websocket_url) as websocket:
        initial = websocket.receive_json()
        assert initial["type"] == "session.state.changed"
        assert initial["payload"]["status"] == "scheduled"
        websocket.send_json({"type": "session.ready", "payload": {"source": "candidate_room"}})
        assert websocket.receive_json()["type"] == "session.participant.ready"
        ready_state = websocket.receive_json()
        assert ready_state["type"] == "session.state.changed"
        assert ready_state["payload"]["status"] == "waiting"
        waiting = api.get("/api/v1/interviews/%s" % interview_id)
        assert waiting.status_code == 200
        assert waiting.json()["status"] == "waiting"
        started = api.post("/api/v1/interviews/%s/start" % interview_id)
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "in_progress"
        rest_state = websocket.receive_json()
        rest_question = websocket.receive_json()
        assert rest_state["type"] == "session.state.changed"
        assert rest_state["payload"]["status"] == "in_progress"
        assert rest_question["type"] == "question.selected"

    events = api.get("/api/v1/interviews/%s/events" % interview_id).json()["items"]
    assert [item["type"] for item in events][-2:] == [
        "interview.participant_ready",
        "interview.started",
    ]


def test_websocket_interviewer_control_uses_lifecycle_and_rejects_candidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api, start=False)
    interview_id = interview["id"]

    candidate_url = "/api/v1/interviews/%s/live?role=candidate&token=%s" % (
        interview_id,
        interview["candidate_session_token"],
    )
    with api.websocket_connect(candidate_url) as websocket:
        assert websocket.receive_json()["type"] == "session.state.changed"
        websocket.send_json({"type": "interviewer.control.start", "payload": {}})
        forbidden = websocket.receive_json()
        assert forbidden["type"] == "error"
        assert forbidden["payload"]["code"] == "INTERVIEWER_CONTROL_FORBIDDEN"

    with api.websocket_connect("/api/v1/interviews/%s/live?role=interviewer" % interview_id) as websocket:
        assert websocket.receive_json()["type"] == "session.state.changed"
        websocket.send_json({"type": "interviewer.control.start", "payload": {"reason": "operator start"}})
        state_changed = websocket.receive_json()
        question_selected = websocket.receive_json()
        assert state_changed["type"] == "session.state.changed"
        assert state_changed["payload"]["status"] == "in_progress"
        assert question_selected["type"] == "question.selected"

    detail = api.get("/api/v1/interviews/%s" % interview_id).json()
    assert detail["status"] == "in_progress"
    assert [item["type"] for item in detail["lifecycle_events"]][-1] == "interview.started"
