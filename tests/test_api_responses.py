import ast
from pathlib import Path

from fastapi.testclient import TestClient

from app.transport.http.fields import list_of, marshal, nested, raw, string
from app.transport.http.responses import collection_response, error_response
from app.main import create_app
from app.repositories.provider import reset_store_for_tests


def test_marshal_projects_only_declared_fields_and_supports_nested_sources() -> None:
    source = {
        "id": 42,
        "secret": "must-not-leak",
        "profile": {"display_name": "张三", "private_phone": "13800000000"},
        "records": [{"id": "one", "internal": True}, {"id": "two"}],
    }
    fields = {
        "id": string(),
        "name": string(source="profile.display_name"),
        "items": list_of(nested({"id": string()}), source="records"),
        "optional": raw(default=None),
    }

    assert marshal(source, fields) == {
        "id": "42",
        "name": "张三",
        "items": [{"id": "one"}, {"id": "two"}],
        "optional": None,
    }


def test_response_factories_keep_compatible_resource_and_collection_shapes() -> None:
    collection = collection_response([{"id": "one"}])
    failure = error_response("EXAMPLE", "Example failure.", status_code=409)

    assert collection == {"items": [{"id": "one"}], "next_cursor": None}
    assert failure.status_code == 409
    assert failure.body == b'{"error":{"code":"EXAMPLE","message":"Example failure.","details":{}}}'


def test_request_validation_errors_use_the_same_error_contract_without_echoing_input() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())

    response = api.post(
        "/api/v1/job-positions",
        json={"code": "", "name": "", "unexpected_secret": "do-not-echo"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert response.json()["error"]["details"]["fields"]
    assert "do-not-echo" not in response.text


def test_all_collection_routes_expose_a_cursor_slot() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())

    response = api.get("/api/v1/admin/model-providers/catalog")

    assert response.status_code == 200
    assert response.json()["items"]
    assert response.json()["next_cursor"] is None


def test_api_package_contains_only_router_declarations_and_assembly() -> None:
    api_root = Path(__file__).resolve().parents[1] / "app" / "api"
    router_names = {path.stem for path in (api_root / "routers").glob("*.py")}

    assert router_names == {
        "__init__",
        "admin",
        "catalog",
        "interview_customization",
        "interview_skills",
        "interviews",
        "plans",
        "realtime",
        "system",
        "talent",
    }
    assert len((api_root / "routes.py").read_text(encoding="utf-8").splitlines()) < 30
    assert not (api_root / "fields").exists()
    assert not (api_root / "dependencies.py").exists()
    assert not (api_root / "realtime.py").exists()
    assert not (api_root / "responses.py").exists()


def test_skill_routes_delegate_delivery_and_recovery_policy_to_the_service() -> None:
    path = Path(__file__).resolve().parents[1] / "app" / "api" / "routers" / "interview_skills.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    for function in (node for node in module.body if isinstance(node, ast.AsyncFunctionDef)):
        assert function.decorator_list, "Async Skill operations belong in the service; only route handlers live here."
        assert not any(isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.Try, ast.If))
                       for node in ast.walk(function)), "Skill routes must not own lifecycle or delivery recovery policy."
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.persistence")
                   for node in ast.walk(module))
