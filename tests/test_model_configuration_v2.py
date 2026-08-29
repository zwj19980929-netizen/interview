import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.main import create_app
from app.migrations.model_configuration_v2 import migrate_documents, migrate_sqlite
from app.model_gateway.forms import validate_form_values
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.registry import get_provider_manifest
from app.model_gateway.schemas import AvatarSpeakResponse, ChatJSONResponse, ProviderMeta, Usage
from app.repositories.provider import get_store, reset_store_for_tests
from app.services.model_admin import ModelAdminService


def client() -> TestClient:
    reset_store_for_tests()
    return TestClient(create_app())


def test_manifest_exposes_backend_owned_connection_and_model_forms() -> None:
    manifest = get_provider_manifest("openai_compatible")

    assert {field["name"] for field in manifest["credential_form"]["fields"]} == {"api_key"}
    assert {field["name"] for field in manifest["connection_form"]["fields"]} >= {"base_url"}
    assert set(manifest["model_types"]) >= {"llm", "embedding", "tts"}
    assert {field["name"] for field in manifest["model_types"]["tts"]["configuration_form"]["fields"]} >= {
        "default_voice"
    }


def test_dynamic_form_validation_rejects_unknown_and_invalid_values() -> None:
    schema = {
        "fields": [
            {"name": "mode", "control": "select", "required": True, "options": [{"label": "A", "value": "a"}]},
            {"name": "rate", "control": "number", "min": 0.5, "max": 2},
        ]
    }

    with pytest.raises(ApiError, match="unknown fields"):
        validate_form_values(schema, {"mode": "a", "vendor_typo": True}, path="settings")
    with pytest.raises(ApiError, match="allowed option"):
        validate_form_values(schema, {"mode": "b"}, path="settings")
    with pytest.raises(ApiError, match="exceeds"):
        validate_form_values(schema, {"mode": "a", "rate": 3}, path="settings")


def test_connection_model_and_route_are_separate_resources() -> None:
    api = client()
    connection = api.post(
        "/api/v1/admin/model-provider-connections",
        json={"provider_id": "mock", "display_name": "Local provider"},
    )
    assert connection.status_code == 200, connection.text

    catalog = api.get(
        "/api/v1/admin/model-provider-connections/%s/model-catalog" % connection.json()["id"]
    )
    assert catalog.status_code == 200
    assert set(catalog.json()["model_types"]) >= {"llm", "embedding", "tts"}

    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": connection.json()["id"],
            "model_type": "llm",
            "provider_model_id": "mock-json",
            "display_name": "Evaluation JSON",
            "default_parameters": {"temperature": 0.25, "max_output_tokens": 800},
        },
    )
    assert model.status_code == 200, model.text
    assert model.json()["provider_connection_id"] == connection.json()["id"]
    assert model.json()["status"] == "ready"

    route = api.post(
        "/api/v1/admin/model-routes",
        json={
            "capability": "llm.chat_json",
            "purpose": "answer_evaluation",
            "primary": {"model_configuration_id": model.json()["id"], "timeout_s": 5},
        },
    )
    assert route.status_code == 200, route.text
    assert route.json()["primary"] == {
        "model_configuration_id": model.json()["id"],
        "timeout_s": 5.0,
        "pricing": {},
    }
    assert "provider_config_id" not in str(route.json())


def test_model_and_provider_crud_apply_scoped_cascades() -> None:
    api = client()
    connection = api.post(
        "/api/v1/admin/model-provider-connections",
        json={"provider_id": "mock", "display_name": "CRUD provider"},
    ).json()
    json_model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": connection["id"],
            "model_type": "llm",
            "provider_model_id": "mock-json",
            "display_name": "JSON model",
        },
    ).json()
    text_model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": connection["id"],
            "model_type": "llm",
            "provider_model_id": "mock-text",
            "display_name": "Text model",
        },
    ).json()
    json_route = api.post(
        "/api/v1/admin/model-routes",
        json={
            "capability": "llm.chat_json",
            "purpose": "answer_evaluation",
            "primary": {"model_configuration_id": json_model["id"]},
        },
    ).json()

    assert api.get(f"/api/v1/admin/model-provider-connections/{connection['id']}").json()["id"] == connection["id"]
    assert api.get(f"/api/v1/admin/model-configurations/{json_model['id']}").json()["id"] == json_model["id"]

    stale = api.delete(
        f"/api/v1/admin/model-configurations/{json_model['id']}?expected_version={json_model['version'] + 1}"
    )
    assert stale.status_code == 409
    assert api.get(f"/api/v1/admin/model-configurations/{json_model['id']}").status_code == 200

    deleted_model = api.delete(
        f"/api/v1/admin/model-configurations/{json_model['id']}?expected_version={json_model['version']}"
    )
    assert deleted_model.status_code == 200, deleted_model.text
    assert deleted_model.json()["deleted_model_route_ids"] == [json_route["id"]]
    remaining_models = api.get("/api/v1/admin/model-configurations").json()["items"]
    assert [item["id"] for item in remaining_models] == [text_model["id"]]
    assert api.get(f"/api/v1/admin/model-provider-connections/{connection['id']}").status_code == 200
    assert api.get("/api/v1/admin/model-routes").json()["items"] == []

    text_route = api.post(
        "/api/v1/admin/model-routes",
        json={
            "capability": "llm.chat_text",
            "purpose": "candidate_message",
            "primary": {"model_configuration_id": text_model["id"]},
        },
    ).json()
    deleted_provider = api.delete(
        f"/api/v1/admin/model-provider-connections/{connection['id']}?expected_version={connection['version']}"
    )
    assert deleted_provider.status_code == 200, deleted_provider.text
    assert deleted_provider.json()["deleted_model_configuration_ids"] == [text_model["id"]]
    assert deleted_provider.json()["deleted_model_route_ids"] == [text_route["id"]]
    remaining_connections = api.get("/api/v1/admin/model-provider-connections").json()["items"]
    assert connection["id"] not in {item["id"] for item in remaining_connections}
    assert api.get("/api/v1/admin/model-configurations").json()["items"] == []
    assert api.get("/api/v1/admin/model-routes").json()["items"] == []
    assert api.get(f"/api/v1/admin/model-provider-connections/{connection['id']}").status_code == 404
    assert connection["id"] not in get_store().provider_secrets


def test_provider_delete_is_tenant_scoped() -> None:
    store = reset_store_for_tests()
    service = ModelAdminService(store)
    connection = service.create_provider_connection(
        {"provider_id": "mock", "display_name": "Tenant A"}, organization_id="org_a"
    )

    with pytest.raises(ApiError, match="does not exist"):
        service.delete_provider_connection(connection["id"], connection["version"], organization_id="org_b")

    assert service.get_provider_connection(connection["id"], organization_id="org_a")["id"] == connection["id"]


@pytest.mark.anyio
async def test_untested_model_can_run_probe_and_receives_saved_defaults() -> None:
    store = reset_store_for_tests()
    service = ModelAdminService(store)
    connection = service.create_provider_connection(
        {
            "provider_id": "openai_compatible",
            "display_name": "Probe gateway",
            "connection_config": {"base_url": "https://models.example.com/v1"},
            "credentials": {"api_key": "test-key"},
        }
    )
    model = service.create_model_configuration(
        {
            "provider_connection_id": connection["id"],
            "model_type": "llm",
            "provider_model_id": "chat-model",
            "display_name": "Probe model",
            "settings": {"structured_output_mode": "json_object"},
            "default_parameters": {"temperature": 0.7, "max_output_tokens": 321},
        }
    )
    seen = {}

    class Adapter:
        provider_id = "openai_compatible"

        async def invoke(self, capability, request, context):
            seen["calls"] = seen.get("calls", 0) + 1
            if seen["calls"] < 3:
                raise ProviderError("provider_rate_limited", "retry", retryable=True)
            seen["temperature"] = request.temperature
            seen["max_output_tokens"] = request.max_output_tokens
            seen["json_schema"] = request.json_schema
            seen["connection_config"] = context.connection_config
            seen["model_settings"] = context.model_settings
            return ChatJSONResponse(
                data={"message": "pong"},
                usage=Usage(),
                provider=ProviderMeta(provider_id="openai_compatible", model=context.model, request_id="probe", latency_ms=1),
            )

    service.gateway = ModelGateway(store, provider_clients={"openai_compatible": Adapter()})
    result = await service.test_model_configuration(model["id"])

    assert result["provider"]["model"] == "chat-model"
    assert seen == {
        "calls": 3,
        "temperature": 0.7,
        "max_output_tokens": 321,
        "json_schema": {
            "type": "object",
            "required": ["message"],
                "properties": {"message": {"type": "string", "enum": ["pong"]}},
            "additionalProperties": False,
        },
        "connection_config": {"base_url": "https://models.example.com/v1", "use_environment_proxy": False},
        "model_settings": {"structured_output_mode": "json_object"},
    }
    assert service.list_model_configurations()[0]["status"] == "ready"


@pytest.mark.anyio
async def test_avatar_model_probe_closes_created_vendor_session() -> None:
    store = reset_store_for_tests()
    service = ModelAdminService(store)
    connection = service.create_provider_connection(
        {
            "provider_id": "tencent_cloud_avatar",
            "display_name": "Avatar probe",
            "connection_config": {
                "base_url": "https://gw.tvs.qq.com",
                "asset_virtualman_key": "asset_test",
            },
            "credentials": {"app_key": "app-key", "access_token": "access-token"},
        }
    )
    model = service.create_model_configuration(
        {
            "provider_connection_id": connection["id"],
            "model_type": "avatar",
            "provider_model_id": "tencent-cloud-avatar-webrtc",
            "display_name": "Tencent avatar",
        }
    )
    operations = []

    class Adapter:
        provider_id = "tencent_cloud_avatar"

        async def invoke(self, capability, request, context):
            operations.append((request.operation, request.session_id, context.fallback_index))
            return AvatarSpeakResponse(
                speech_id="speech_probe",
                status="closed" if request.operation == "close" else "ready",
                mode="webrtc",
                text=request.text,
                stream_url=None if request.operation == "close" else "webrtc://example.test/live",
                session_id=request.session_id or "session_probe",
                player_kind="tencent_web_player",
                provider=ProviderMeta(
                    provider_id=self.provider_id,
                    model=context.model,
                    request_id="probe",
                    latency_ms=1,
                ),
            )

    service.gateway = ModelGateway(store, provider_clients={"tencent_cloud_avatar": Adapter()})
    result = await service.test_model_configuration(model["id"])

    assert result["session_id"] == "session_probe"
    assert operations == [("speak", None, 0), ("close", "session_probe", 0)]
    assert service.list_model_configurations()[0]["status"] == "ready"


@pytest.mark.anyio
async def test_provider_connection_validation_crosses_provider_adapter_seam() -> None:
    store = reset_store_for_tests()
    service = ModelAdminService(store)
    connection = service.create_provider_connection(
        {
            "provider_id": "deepseek",
            "display_name": "Credential preflight",
            "connection_config": {},
            "credentials": {"api_key": "test-key"},
        }
    )
    seen = {}

    class Adapter:
        provider_id = "deepseek"

        async def invoke(self, capability, request, context):
            raise AssertionError("credential validation must not invoke a configured model")

        async def validate_credentials(self, config, credentials, *, timeout_s):
            seen.update(config=config, credentials=credentials, timeout_s=timeout_s)
            return {"status": "valid", "message": "Remote credentials accepted."}

    service.gateway = ModelGateway(store, provider_clients={"deepseek": Adapter()})
    result = await service.validate_provider_connection(connection["id"])

    assert result["credential_status"] == "valid"
    assert result["last_validation"]["message"] == "Remote credentials accepted."
    assert seen == {
        "config": {"base_url": "https://api.deepseek.com", "use_environment_proxy": False},
        "credentials": {"api_key": "test-key"},
        "timeout_s": 10,
    }


def test_legacy_documents_migrate_without_dual_route_representation() -> None:
    legacy = {
        "id": "mpc_legacy",
        "organization_id": "org_default",
        "provider_id": "openai_compatible",
        "display_name": "Legacy gateway",
        "enabled": True,
        "config": {"base_url": "https://models.example.com/v1", "structured_output_mode": "json_object"},
        "credential_ref": "secret://model-providers/mpc_legacy",
        "version": 3,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    old_route = {
        "id": "route_legacy",
        "organization_id": "org_default",
        "capability": "llm.chat_json",
        "purpose": "answer_evaluation",
        "primary": {"provider_config_id": "mpc_legacy", "model": "chat-model", "timeout_s": 12},
        "fallbacks": [],
        "policy": {},
        "enabled": True,
    }

    connections, models, routes = migrate_documents([legacy], [old_route])

    assert connections[0]["connection_config"] == {
        "base_url": "https://models.example.com/v1",
        "use_environment_proxy": False,
    }
    assert models[0]["settings"] == {"structured_output_mode": "json_object"}
    assert routes[0]["primary"]["model_configuration_id"] == models[0]["id"]
    assert "provider_config_id" not in routes[0]["primary"]
    assert "model" not in routes[0]["primary"]


def test_sqlite_migration_supports_dry_run_then_one_way_upgrade(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE documents(collection TEXT, id TEXT, data TEXT, updated_at TEXT, PRIMARY KEY(collection, id))")
        connection.execute("CREATE TABLE provider_secrets(provider_config_id TEXT PRIMARY KEY, data TEXT, updated_at TEXT)")
        legacy = {
            "id": "mpc_mock", "organization_id": "org_default", "provider_id": "mock",
            "display_name": "Legacy mock", "enabled": True, "config": {}, "version": 1,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }
        route = {
            "id": "route_old", "organization_id": "org_default", "capability": "llm.chat_json",
            "purpose": "answer_evaluation", "primary": {"provider_config_id": "mpc_mock", "model": "mock-json"},
            "fallbacks": [], "policy": {}, "enabled": True,
        }
        connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("provider_configs", legacy["id"], json.dumps(legacy), legacy["updated_at"]))
        connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("model_routes", route["id"], json.dumps(route), legacy["updated_at"]))

    assert migrate_sqlite(str(path), dry_run=True)["model_configurations"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM documents WHERE collection = 'provider_configs'").fetchone()[0] == 1

    migrate_sqlite(str(path))
    with sqlite3.connect(path) as connection:
        collections = {row[0] for row in connection.execute("SELECT DISTINCT collection FROM documents")}
        secret_columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_secrets)")}
    assert "provider_configs" not in collections
    assert {"provider_connections", "model_configurations", "model_routes"}.issubset(collections)
    assert "provider_connection_id" in secret_columns


def test_admin_console_uses_schema_renderer_and_no_legacy_endpoint() -> None:
    script = Path("app/web/src/features/models/Page.jsx").read_text(encoding="utf-8")

    assert "SchemaFields" in script
    assert "connection_form" in script
    assert "model_types" in script
    assert "model-provider-configs" not in script
    assert "provider_config_id" not in script
