from copy import deepcopy
import hashlib
from io import BytesIO
import wave
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.forms import apply_form_defaults, validate_form_values
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.errors import ProviderError
from app.model_gateway.registry import (
    get_model_definition,
    get_provider_catalog,
    get_provider_manifest,
    provider_exists,
)
from app.model_gateway.schemas import (
    AvatarSpeakRequest,
    BatchSTTRequest,
    ChatJSONRequest,
    ChatTextRequest,
    StreamingSTTRequest,
    TextEmbeddingRequest,
    TTSSynthesizeRequest,
)
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


UNIFIED_PARAMETER_FORMS: Dict[str, Dict[str, Any]] = {
    cap.MODEL_TYPE_LLM: {
        "schema_version": "1",
        "fields": [
            {"name": "temperature", "label": "Temperature", "control": "number", "default": 0.1, "min": 0, "max": 2},
            {"name": "max_output_tokens", "label": "最大输出 Token", "control": "number", "default": 1200, "min": 1, "max": 128000},
        ],
    },
    cap.MODEL_TYPE_TTS: {
        "schema_version": "1",
        "fields": [
            {"name": "speaking_rate", "label": "默认语速", "control": "number", "default": 1.0, "min": 0.5, "max": 2.0},
        ],
    },
}


class ModelAdminService:
    """Owns provider connections, configured models, validation, and model routes."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self.gateway = ModelGateway(store, persistence=self.persistence)

    def catalog(self) -> List[Dict[str, Any]]:
        return get_provider_catalog()

    def create_provider_connection(
        self, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        if not provider_exists(payload["provider_id"]):
            raise ApiError("PROVIDER_NOT_FOUND", "Provider plugin is not installed.", status_code=404)
        manifest = get_provider_manifest(payload["provider_id"])
        connection_config = validate_form_values(
            manifest["connection_form"],
            apply_form_defaults(manifest["connection_form"], payload.get("connection_config")),
            path="connection_config",
        )
        credentials = validate_form_values(
            manifest["credential_form"],
            apply_form_defaults(manifest["credential_form"], payload.get("credentials")),
            path="credentials",
        )
        connection_id = new_id("provider_conn")
        now = utc_now()
        item = {
            "id": connection_id,
            "organization_id": organization_id,
            "provider_id": payload["provider_id"],
            "display_name": payload["display_name"],
            "enabled": payload.get("enabled", True),
            "connection_config": connection_config,
            "credential_ref": "secret://provider-connections/%s" % connection_id,
            "credential_status": "valid" if payload["provider_id"] == "mock" else "untested",
            "last_validation": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_connections.add(item)
            transaction.provider_secrets.replace(connection_id, credentials)
        return item

    def list_provider_connections(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.provider_connections.list()

    def get_provider_connection(
        self, connection_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        return self._connection(connection_id, organization_id)

    def patch_provider_connection(
        self,
        connection_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_connections.get(connection_id)
            if not item:
                raise ApiError("PROVIDER_CONNECTION_NOT_FOUND", "Provider connection does not exist.", status_code=404)
            if item["version"] != expected_version:
                raise ConcurrencyConflict(
                    "ProviderConnection %s expected version %s, found %s"
                    % (connection_id, expected_version, item["version"])
                )
            manifest = get_provider_manifest(item["provider_id"])
            if payload.get("connection_config") is not None:
                item["connection_config"] = validate_form_values(
                    manifest["connection_form"],
                    apply_form_defaults(manifest["connection_form"], payload["connection_config"]),
                    path="connection_config",
                )
            if payload.get("credentials") is not None:
                credentials = validate_form_values(
                    manifest["credential_form"], payload["credentials"], path="credentials", partial=True
                )
                current_credentials = transaction.provider_secrets.get(connection_id)
                merged_credentials = {**current_credentials, **credentials}
                validate_form_values(manifest["credential_form"], merged_credentials, path="credentials")
                transaction.provider_secrets.replace(connection_id, merged_credentials)
                item["credential_status"] = "untested"
                item["last_validation"] = None
            for field in ("display_name", "enabled"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            item["updated_at"] = utc_now()
            return transaction.provider_connections.update(item, expected_version=expected_version)

    def delete_provider_connection(
        self,
        connection_id: str,
        expected_version: int,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            connection = transaction.provider_connections.get(connection_id)
            if connection is None:
                raise ApiError(
                    "PROVIDER_CONNECTION_NOT_FOUND",
                    "Provider connection does not exist.",
                    status_code=404,
                )
            transaction.provider_connections.delete(connection_id, expected_version=expected_version)
            models = [
                item
                for item in transaction.model_configurations.list()
                if item.get("provider_connection_id") == connection_id
            ]
            model_ids = {item["id"] for item in models}
            referenced_by = [
                item["id"]
                for item in transaction.knowledge_bases.list()
                if (item.get("speech_profile") or {}).get("model_configuration_id") in model_ids
            ]
            if referenced_by:
                raise ApiError(
                    "MODEL_CONFIGURATION_IN_USE",
                    "Provider contains TTS models still used by knowledge bases.",
                    status_code=409,
                    details={"knowledge_base_ids": sorted(referenced_by)},
                )
            deleted_route_ids = self._delete_model_dependencies(transaction, model_ids, organization_id)
            for model in models:
                transaction.model_configurations.delete(model["id"], expected_version=model["version"])
            transaction.provider_secrets.delete(connection_id)
        return {
            "id": connection_id,
            "deleted": True,
            "deleted_model_configuration_ids": sorted(model_ids),
            "deleted_model_route_ids": deleted_route_ids,
        }

    async def validate_provider_connection(
        self, connection_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_connections.get(connection_id)
            if item is None:
                raise ApiError("PROVIDER_CONNECTION_NOT_FOUND", "Provider connection does not exist.", status_code=404)
            manifest = get_provider_manifest(item["provider_id"])
            validate_form_values(manifest["connection_form"], item["connection_config"], path="connection_config")
            credentials = transaction.provider_secrets.get(connection_id)
            validate_form_values(manifest["credential_form"], credentials, path="credentials")
        adapter = self.gateway.providers.adapter(item["provider_id"], manifest["capabilities"][0])
        validator = getattr(adapter, "validate_credentials", None)
        if not callable(validator):
            result = {
                "status": "model_required",
                "message": "This provider requires a concrete model test to validate credentials.",
            }
        else:
            try:
                result = await validator(item["connection_config"], credentials, timeout_s=10)
            except Exception as exc:
                self._record_connection_health(connection_id, organization_id, "invalid", str(exc))
                raise
        status = str(result.get("status") or "valid")
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_connections.get(connection_id)
            if item is None:
                raise ApiError("PROVIDER_CONNECTION_NOT_FOUND", "Provider connection does not exist.", status_code=404)
            item["credential_status"] = status
            item["last_validation"] = {
                "status": status,
                "checked_at": utc_now(),
                "message": str(result.get("message") or "Provider credentials are valid."),
            }
            item["updated_at"] = utc_now()
            return transaction.provider_connections.update(item, expected_version=item["version"])

    def model_catalog(
        self,
        connection_id: str,
        model_type: Optional[str] = None,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        connection = self._connection(connection_id, organization_id)
        manifest = get_provider_manifest(connection["provider_id"])
        if model_type is not None and model_type not in manifest["model_types"]:
            raise ApiError("MODEL_TYPE_UNAVAILABLE", "Provider does not support this model type.", status_code=404)
        types = {
            key: deepcopy(value)
            for key, value in manifest["model_types"].items()
            if model_type is None or key == model_type
        }
        models = []
        for model in manifest["models"]:
            if model_type is not None and model["model_type"] != model_type:
                continue
            definition = get_model_definition(
                connection["provider_id"], model["model_type"], model["model_id"]
            )
            models.append({**deepcopy(model), "configuration_form": definition["configuration_form"]})
        return {
            "provider_connection_id": connection_id,
            "provider_id": connection["provider_id"],
            "model_types": types,
            "models": models,
            "parameter_forms": {
                key: deepcopy(UNIFIED_PARAMETER_FORMS.get(key, {"schema_version": "1", "fields": []}))
                for key in types
            },
        }

    def create_model_configuration(
        self, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        connection = self._connection(payload["provider_connection_id"], organization_id)
        if not connection.get("enabled", True):
            raise ApiError("PROVIDER_CONNECTION_DISABLED", "Provider connection is disabled.", status_code=409)
        definition = self._model_definition(
            connection["provider_id"], payload["model_type"], payload["provider_model_id"]
        )
        settings = validate_form_values(
            definition["configuration_form"],
            apply_form_defaults(definition["configuration_form"], payload.get("settings")),
            path="settings",
        )
        parameter_form = UNIFIED_PARAMETER_FORMS.get(payload["model_type"], {"fields": []})
        default_parameters = validate_form_values(
            parameter_form,
            apply_form_defaults(parameter_form, payload.get("default_parameters")),
            path="default_parameters",
        )
        now = utc_now()
        item = {
            "id": new_id("model_cfg"),
            "organization_id": organization_id,
            "provider_connection_id": connection["id"],
            "provider_id": connection["provider_id"],
            "model_type": payload["model_type"],
            "provider_model_id": payload["provider_model_id"],
            "display_name": payload["display_name"],
            "supported_capabilities": list(definition["capabilities"]),
            "settings": settings,
            "default_parameters": default_parameters,
            "definition_version": get_provider_manifest(connection["provider_id"])["version"],
            "enabled": payload.get("enabled", True),
            "status": "ready" if connection["provider_id"] == "mock" else "untested",
            "last_validation": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.model_configurations.add(item)

    def list_model_configurations(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.model_configurations.list()

    def get_model_configuration(
        self, configuration_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        return self._model_configuration(configuration_id, organization_id)

    def voice_catalog(
        self, configuration_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        """Normalize a configured TTS model's selectable voices behind one interface."""
        model = self._model_configuration(configuration_id, organization_id)
        if cap.TTS_SYNTHESIZE not in model.get("supported_capabilities", []):
            raise ApiError(
                "MODEL_CONFIGURATION_CAPABILITY_MISMATCH",
                "Model configuration does not support TTS synthesis.",
                status_code=409,
            )
        settings = model.get("settings") or {}
        default_voice = str(settings.get("default_voice") or "voice_default_cn").strip()
        voice_map = settings.get("voice_map") or {}
        voices: List[Dict[str, Any]] = []
        seen = set()

        def append(voice_id: str, label: str, *, default: bool = False) -> None:
            value = str(voice_id or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            voices.append(
                {
                    "voice_profile_id": value,
                    "label": str(label or value),
                    "languages": [],
                    "default": default,
                }
            )

        append(default_voice, default_voice, default=True)
        for profile_id, provider_voice in sorted(dict(voice_map).items()):
            append(str(profile_id), "%s · %s" % (profile_id, provider_voice))
        return {
            "model_configuration_id": configuration_id,
            "model_configuration_version": model["version"],
            "items": voices,
        }

    def patch_model_configuration(
        self, configuration_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.model_configurations.get(configuration_id)
            if item is None:
                raise ApiError("MODEL_CONFIGURATION_NOT_FOUND", "Model configuration does not exist.", status_code=404)
            if item["version"] != expected_version:
                raise ConcurrencyConflict(
                    "ModelConfiguration %s expected version %s, found %s"
                    % (configuration_id, expected_version, item["version"])
                )
            definition = self._model_definition(item["provider_id"], item["model_type"], item["provider_model_id"])
            if payload.get("settings") is not None:
                item["settings"] = validate_form_values(
                    definition["configuration_form"],
                    apply_form_defaults(definition["configuration_form"], payload["settings"]),
                    path="settings",
                )
                item["status"] = "untested"
            if payload.get("default_parameters") is not None:
                parameter_form = UNIFIED_PARAMETER_FORMS.get(item["model_type"], {"fields": []})
                item["default_parameters"] = validate_form_values(
                    parameter_form,
                    apply_form_defaults(parameter_form, payload["default_parameters"]),
                    path="default_parameters",
                )
                item["status"] = "untested"
            for field in ("display_name", "enabled"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            item["updated_at"] = utc_now()
            return transaction.model_configurations.update(item, expected_version=expected_version)

    def delete_model_configuration(
        self,
        configuration_id: str,
        expected_version: int,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            model = transaction.model_configurations.get(configuration_id)
            if model is None:
                raise ApiError(
                    "MODEL_CONFIGURATION_NOT_FOUND",
                    "Model configuration does not exist.",
                    status_code=404,
                )
            referenced_by = [
                item["id"]
                for item in transaction.knowledge_bases.list()
                if (item.get("speech_profile") or {}).get("model_configuration_id") == configuration_id
            ]
            if referenced_by:
                raise ApiError(
                    "MODEL_CONFIGURATION_IN_USE",
                    "TTS model configuration is still used by knowledge bases.",
                    status_code=409,
                    details={"knowledge_base_ids": sorted(referenced_by)},
                )
            transaction.model_configurations.delete(configuration_id, expected_version=expected_version)
            deleted_route_ids = self._delete_model_dependencies(
                transaction, {configuration_id}, organization_id
            )
        return {
            "id": configuration_id,
            "deleted": True,
            "deleted_model_route_ids": deleted_route_ids,
        }

    async def test_model_configuration(
        self,
        configuration_id: str,
        probe: Optional[Dict[str, Any]] = None,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        model = self._model_configuration(configuration_id, organization_id)
        capabilities = model["supported_capabilities"]
        capability = (probe or {}).get("capability") or capabilities[0]
        if capability not in capabilities:
            raise ApiError("MODEL_CAPABILITY_MISMATCH", "Configured model does not support this capability.", status_code=409)
        route = {
            "id": "model_configuration_test",
            "organization_id": organization_id,
            "capability": capability,
            "purpose": "model_configuration_test",
            "primary": {"model_configuration_id": configuration_id, "timeout_s": 10},
            "fallbacks": [],
            "policy": {
                "retry_count": 2,
                "retry_backoff_ms": 250,
                "circuit_failure_threshold": 0,
            },
            "enabled": True,
        }
        try:
            request = self._probe_request(capability, organization_id, "model_configuration_test")
            if capability == cap.STT_STREAMING:
                stream = await self.gateway.open_stream(request, route=route)
                events = list(stream.ready_events)
                events.extend(await stream.send_audio(_probe_wav()))
                events.extend(await stream.finish())
                result = {"stream_id": stream.stream_id, "events": [item.model_dump() for item in events]}
            else:
                result = (await self.gateway.invoke(capability, request, route=route)).model_dump()
        except Exception as exc:
            self._record_model_health(configuration_id, organization_id, "failed", str(exc))
            raise
        self._record_model_health(configuration_id, organization_id, "ready", None)
        return result

    def create_route(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        if payload["capability"] not in cap.ALL_CAPABILITIES:
            raise ApiError("MODEL_CAPABILITY_INVALID", "Capability is not supported.", status_code=400)
        self._validate_route_payload(payload)
        now = utc_now()
        item = {
            "id": new_id("route"),
            "organization_id": organization_id,
            "capability": payload["capability"],
            "purpose": payload.get("purpose", "default"),
            "primary": payload["primary"],
            "fallbacks": payload.get("fallbacks", []),
            "policy": payload.get("policy", {}),
            "enabled": payload.get("enabled", True),
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            duplicate = next(
                (
                    route for route in transaction.model_routes.list()
                    if route.get("capability") == item["capability"] and route.get("purpose") == item["purpose"]
                ),
                None,
            )
            if duplicate is not None:
                raise ApiError("MODEL_ROUTE_CONFLICT", "A model route already exists for this capability and purpose.", status_code=409)
            for target in [payload["primary"], *payload.get("fallbacks", [])]:
                model = transaction.model_configurations.get(target["model_configuration_id"])
                self._require_model_capability(model, payload["capability"])
            return transaction.model_routes.add(item)

    def list_routes(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.model_routes.list()

    async def test_route(self, route_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            route = transaction.model_routes.get(route_id)
        if not route:
            raise ApiError("MODEL_ROUTE_NOT_FOUND", "Model route does not exist.", status_code=404)
        request = self._probe_request(route["capability"], organization_id, route["purpose"])
        try:
            if route["capability"] == cap.STT_STREAMING:
                stream = await self.gateway.open_stream(request, route=route)
                events = list(stream.ready_events)
                events.extend(await stream.send_audio(b"connection-test-audio"))
                events.extend(await stream.finish())
                result = {"stream_id": stream.stream_id, "events": [item.model_dump() for item in events]}
            else:
                response = await self.gateway.invoke(route["capability"], request, route=route)
                result = response.model_dump()
        except Exception as exc:
            self._record_route_health(route_id, organization_id, status="failed", error=str(exc))
            raise
        self._record_route_health(route_id, organization_id, status="healthy", error=None)
        return result

    def _connection(self, connection_id: str, organization_id: str) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_connections.get(connection_id)
        if item is None:
            raise ApiError("PROVIDER_CONNECTION_NOT_FOUND", "Provider connection does not exist.", status_code=404)
        return item

    def _model_definition(self, provider_id: str, model_type: str, model_id: str) -> Dict[str, Any]:
        try:
            return get_model_definition(provider_id, model_type, model_id)
        except ProviderError as exc:
            status_code = 404 if exc.code == "provider_model_type_missing" else 409
            code = {
                "provider_model_unavailable": "MODEL_PROVIDER_MODEL_UNAVAILABLE",
                "provider_model_type_missing": "MODEL_TYPE_UNAVAILABLE",
            }.get(exc.code, exc.code.upper())
            raise ApiError(code, exc.message, status_code=status_code) from exc

    def _model_configuration(self, configuration_id: str, organization_id: str) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.model_configurations.get(configuration_id)
        if item is None:
            raise ApiError("MODEL_CONFIGURATION_NOT_FOUND", "Model configuration does not exist.", status_code=404)
        return item

    def _delete_model_dependencies(
        self,
        transaction: Any,
        model_configuration_ids: set[str],
        organization_id: str,
    ) -> List[str]:
        if not model_configuration_ids:
            return []
        routes = [
            route
            for route in transaction.model_routes.list()
            if any(
                target.get("model_configuration_id") in model_configuration_ids
                for target in [route.get("primary") or {}, *(route.get("fallbacks") or [])]
            )
        ]
        for route in routes:
            transaction.model_routes.delete(route["id"], expected_version=route["version"])
        circuit_ids = {
            "circuit_%s"
            % hashlib.sha256(
                ("%s:%s:%s" % (organization_id, model_id, capability)).encode("utf-8")
            ).hexdigest()
            for model_id in model_configuration_ids
            for capability in cap.ALL_CAPABILITIES
        }
        for state in transaction.model_circuit_states.list():
            if state["id"] in circuit_ids:
                transaction.model_circuit_states.delete(state["id"], expected_version=state["version"])
        return sorted(route["id"] for route in routes)

    def _record_model_health(self, configuration_id: str, organization_id: str, status: str, error: Optional[str]) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            model = transaction.model_configurations.get(configuration_id)
            if model is None:
                return
            model["status"] = status
            model["last_validation"] = {"status": status, "checked_at": utc_now(), "error": error[:500] if error else None}
            model["updated_at"] = utc_now()
            transaction.model_configurations.update(model, expected_version=model["version"])
            connection = transaction.provider_connections.get(model["provider_connection_id"])
            if connection is not None:
                connection["credential_status"] = "valid" if status == "ready" else "invalid"
                connection["last_validation"] = {"status": connection["credential_status"], "checked_at": utc_now(), "error": error[:500] if error else None}
                connection["updated_at"] = utc_now()
                transaction.provider_connections.update(connection, expected_version=connection["version"])

    def _record_connection_health(
        self, connection_id: str, organization_id: str, status: str, error: Optional[str]
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            connection = transaction.provider_connections.get(connection_id)
            if connection is None:
                return
            connection["credential_status"] = status
            connection["last_validation"] = {
                "status": status,
                "checked_at": utc_now(),
                "error": error[:500] if error else None,
            }
            connection["updated_at"] = utc_now()
            transaction.provider_connections.update(connection, expected_version=connection["version"])

    def _record_route_health(self, route_id: str, organization_id: str, *, status: str, error: Optional[str]) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            route = transaction.model_routes.get(route_id)
            if route is None:
                return
            route["last_health"] = {"status": status, "checked_at": utc_now(), "error": error[:500] if error else None}
            route["updated_at"] = utc_now()
            transaction.model_routes.update(route, expected_version=route["version"])

    def _probe_request(self, capability: str, organization_id: str, purpose: str) -> Any:
        if capability == cap.LLM_CHAT_JSON:
            contract = prompt_contract("json_probe", {})
            return ChatJSONRequest(
                organization_id=organization_id,
                purpose=purpose,
                messages=contract.messages,
                json_schema=contract.response_schema,
                metadata={"prompt_version": contract.version},
            )
        if capability == cap.LLM_CHAT_TEXT:
            contract = prompt_contract("text_probe", {})
            return ChatTextRequest(
                organization_id=organization_id,
                purpose=purpose,
                messages=contract.messages,
                metadata={"prompt_version": contract.version},
            )
        if capability == cap.EMBEDDING_TEXT:
            return TextEmbeddingRequest(organization_id=organization_id, purpose=purpose, texts=["ping"])
        if capability == cap.AVATAR_SPEAK:
            return AvatarSpeakRequest(organization_id=organization_id, purpose=purpose, text="连接测试")
        if capability == cap.TTS_SYNTHESIZE:
            return TTSSynthesizeRequest(organization_id=organization_id, purpose=purpose, text="连接测试")
        if capability == cap.STT_BATCH:
            return BatchSTTRequest(
                organization_id=organization_id,
                purpose=purpose,
                audio_uri="probe-audio://connection-test.wav",
                content_type="audio/wav",
                metadata={"development_transcript": "连接测试"},
                audio_bytes=_probe_wav(),
            )
        if capability == cap.STT_STREAMING:
            return StreamingSTTRequest(organization_id=organization_id, interview_id="connection_test", turn_id="connection_test_turn", purpose=purpose, metadata={"development_transcript": "连接测试", "confidence": 1.0})
        raise ApiError("MODEL_CAPABILITY_NOT_IMPLEMENTED", "Capability has no unified local test schema.", status_code=409)
    def _require_model_capability(self, model: Optional[Dict[str, Any]], capability: str) -> None:
        if model is None:
            raise ApiError("MODEL_CONFIGURATION_NOT_FOUND", "Configured model does not exist.", status_code=404)
        if not model.get("enabled", True):
            raise ApiError("MODEL_CONFIGURATION_DISABLED", "Configured model is disabled.", status_code=409)
        if model.get("status") != "ready":
            raise ApiError("MODEL_CONFIGURATION_NOT_READY", "Configured model must pass validation before routing.", status_code=409)
        if capability not in model.get("supported_capabilities", []):
            raise ApiError("MODEL_CAPABILITY_MISMATCH", "Configured model does not support the route capability.", status_code=409)

    def _validate_route_payload(self, payload: Dict[str, Any]) -> None:
        for target in [payload.get("primary") or {}, *payload.get("fallbacks", [])]:
            if not target.get("model_configuration_id"):
                raise ApiError("MODEL_ROUTE_INVALID", "Every model route target requires model_configuration_id.", status_code=400)
            try:
                timeout_s = float(target.get("timeout_s", 20))
            except (TypeError, ValueError) as exc:
                raise ApiError("MODEL_ROUTE_INVALID", "Route timeout_s must be numeric.", status_code=400) from exc
            if timeout_s <= 0:
                raise ApiError("MODEL_ROUTE_INVALID", "Route timeout_s must be positive.", status_code=400)
            target["timeout_s"] = timeout_s
        policy = payload.get("policy") or {}
        retry_count = int(policy.get("retry_count", 0))
        if retry_count < 0 or retry_count > 3:
            raise ApiError("MODEL_ROUTE_INVALID", "retry_count must be between 0 and 3.", status_code=400)
        fallback_on = policy.get("fallback_on")
        if fallback_on is not None and (not isinstance(fallback_on, list) or not all(isinstance(item, str) for item in fallback_on)):
            raise ApiError("MODEL_ROUTE_INVALID", "fallback_on must be a list of error codes.", status_code=400)


def _probe_wav() -> bytes:
    """Small valid silent WAV used only for provider contract/authorization probes."""
    target = BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    return target.getvalue()
