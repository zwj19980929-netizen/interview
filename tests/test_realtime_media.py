from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.provider import reset_store_for_tests


RETIRED_HTTP_PATHS = {
    "/api/v1/interviews/{interview_id}/audio-answers",
    "/api/v1/public/interviews/{interview_id}/audio-answers",
    "/api/v1/interviews/{interview_id}/avatar/speak",
    "/api/v1/public/interviews/{interview_id}/avatar/speak",
    "/api/v1/public/interviews/{interview_id}/avatar/session/close",
    "/api/v1/public/agent/avatar-config",
    "/api/v1/public/agent/avatar-model",
}

RETIRED_WEBSOCKET_PATHS = {
    "/api/v1/interviews/{interview_id}/live",
    "/api/v1/interviews/{interview_id}/stt-stream",
}


def test_formal_candidate_cutover_removes_all_compatibility_routes() -> None:
    reset_store_for_tests()
    app = create_app()
    route_paths = {route.path for route in app.routes}

    assert "/api/v1/interviews/{interview_id}/agent" in route_paths
    assert "/api/v1/public/interviews/{interview_id}/avatar-config" in route_paths
    assert "/api/v1/public/interviews/{interview_id}/avatar-model" in route_paths
    assert "/api/v1/public/interviews/{interview_id}/runtime-problems" in route_paths
    assert route_paths.isdisjoint(RETIRED_HTTP_PATHS)
    assert route_paths.isdisjoint(RETIRED_WEBSOCKET_PATHS)

    openapi_paths = set(app.openapi()["paths"])
    assert openapi_paths.isdisjoint(RETIRED_HTTP_PATHS)


def test_retired_http_entry_points_fail_instead_of_imitating_agent_mode() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    for path in (
        "/api/v1/interviews/iv_retired/audio-answers",
        "/api/v1/public/interviews/iv_retired/audio-answers",
        "/api/v1/interviews/iv_retired/avatar/speak",
        "/api/v1/public/interviews/iv_retired/avatar/speak",
        "/api/v1/public/interviews/iv_retired/avatar/session/close",
    ):
        response = api.post(path, json={})
        assert response.status_code == 404, (path, response.text)
    assert api.get("/api/v1/public/agent/avatar-config").status_code == 404
    assert api.get("/api/v1/public/agent/avatar-model").status_code == 404


def test_scoped_avatar_routes_require_candidate_or_asset_grant() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    missing_candidate = api.get(
        "/api/v1/public/interviews/iv_private/avatar-config"
    )
    assert missing_candidate.status_code == 422
    missing_grant = api.get(
        "/api/v1/public/interviews/iv_private/avatar-model"
    )
    assert missing_grant.status_code == 422
