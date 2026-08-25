from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.registry import get_provider_catalog, get_provider_manifest, provider_exists
from app.model_gateway.schemas import (
    AvatarSpeakRequest,
    BatchSTTRequest,
    ChatJSONRequest,
    ChatMessage,
    ChatTextRequest,
    TextEmbeddingRequest,
    StreamingSTTRequest,
    TTSSynthesizeRequest,
)
from app.model_gateway.gateway import ModelGateway
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class ModelAdminService:
    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self.gateway = ModelGateway(store, persistence=self.persistence)

    def catalog(self) -> List[Dict[str, Any]]:
        return get_provider_catalog()

    def create_provider_config(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        if not provider_exists(payload["provider_id"]):
            raise ApiError("PROVIDER_NOT_FOUND", "Provider plugin is not installed.", status_code=404)
        manifest = get_provider_manifest(payload["provider_id"])
        config = self._with_manifest_defaults(manifest, payload.get("config", {}))
        self._validate_manifest_document(config, manifest["config_schema"], "config")
        self._validate_manifest_document(
            payload.get("credentials", {}),
            manifest["credential_schema"],
            "credentials",
        )
        config_id = new_id("mpc")
        now = utc_now()
        item = {
            "id": config_id,
            "organization_id": organization_id,
            "provider_id": payload["provider_id"],
            "display_name": payload["display_name"],
            "enabled": payload.get("enabled", True),
            "config": config,
            "credential_ref": "secret://model-providers/%s" % config_id,
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_configs.add(item)
            transaction.provider_secrets.replace(config_id, payload.get("credentials", {}))
            return item

    def list_provider_configs(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.provider_configs.list()

    def patch_provider_config(
        self,
        config_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.provider_configs.get(config_id)
            if not item:
                raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Model provider config does not exist.", status_code=404)
            if item["version"] != expected_version:
                raise ConcurrencyConflict(
                    "ModelProviderConfig %s expected version %s, found %s"
                    % (config_id, expected_version, item["version"])
                )
            manifest = get_provider_manifest(item["provider_id"])
            if payload.get("config") is not None:
                payload["config"] = self._with_manifest_defaults(manifest, payload["config"])
                self._validate_manifest_document(payload["config"], manifest["config_schema"], "config")
            if payload.get("credentials") is not None:
                self._validate_manifest_document(
                    payload["credentials"],
                    manifest["credential_schema"],
                    "credentials",
                )
            for field in ("display_name", "enabled", "config"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            if payload.get("credentials") is not None:
                item["credential_ref"] = "secret://model-providers/%s" % config_id
                transaction.provider_secrets.replace(config_id, payload.get("credentials", {}))
            item["updated_at"] = utc_now()
            return transaction.provider_configs.update(item, expected_version=expected_version)

    async def test_provider_config(
        self,
        config_id: str,
        probe: Optional[Dict[str, Any]] = None,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            provider_config = transaction.provider_configs.get(config_id)
        if not provider_config:
            raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Model provider config does not exist.", status_code=404)
        manifest = get_provider_manifest(provider_config["provider_id"])
        probe = probe or {}
        probe_capabilities = (
            cap.LLM_CHAT_JSON,
            cap.LLM_CHAT_TEXT,
            cap.EMBEDDING_TEXT,
            cap.TTS_SYNTHESIZE,
            cap.STT_BATCH,
            cap.AVATAR_SPEAK,
        )
        requested_capability = probe.get("capability")
        if requested_capability is not None and requested_capability not in probe_capabilities:
            raise ApiError(
                "MODEL_CAPABILITY_NOT_IMPLEMENTED",
                "Capability has no unified local test schema.",
                status_code=409,
                details={"capability": requested_capability},
            )
        capability = requested_capability or next(
            (
                item
                for item in probe_capabilities
                if item in manifest["capabilities"]
            ),
            None,
        )
        if capability is None:
            raise ApiError(
                "MODEL_CAPABILITY_NOT_IMPLEMENTED",
                "Provider has no capability with a unified local test schema.",
                status_code=409,
            )
        model = self._test_model(
            provider_config,
            manifest,
            capability,
            requested_model=probe.get("model"),
        ) or {
            cap.LLM_CHAT_JSON: "mock-json" if provider_config["provider_id"] == "mock" else "chat-model-default",
            cap.LLM_CHAT_TEXT: "mock-text" if provider_config["provider_id"] == "mock" else "chat-model-default",
            cap.EMBEDDING_TEXT: "mock-embedding" if provider_config["provider_id"] == "mock" else "embedding-model-default",
            cap.TTS_SYNTHESIZE: "mock-tts",
            cap.STT_BATCH: "mock-stt",
            cap.AVATAR_SPEAK: "mock-avatar",
        }[capability]
        self._require_provider_capability(provider_config, capability, model)
        request = self._probe_request(capability, organization_id, "provider_test")
        response = await self.gateway.invoke(
            capability,
            request,
            route={
                "id": "provider_config_test",
                "organization_id": organization_id,
                "capability": capability,
                "purpose": "provider_test",
                "primary": {
                    "provider_config_id": config_id,
                    "model": model,
                    "timeout_s": int(provider_config.get("config", {}).get("timeout_s", 10)),
                },
                "fallbacks": [],
                "policy": {"retry_count": 0},
                "enabled": True,
            },
        )
        return response.model_dump()

    def create_route(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        if payload["capability"] not in cap.ALL_CAPABILITIES:
            raise ApiError("MODEL_CAPABILITY_INVALID", "Capability is not supported.", status_code=400)
        self._validate_route_payload(payload)
        primary_config_id = payload["primary"].get("provider_config_id")
        route_id = new_id("route")
        now = utc_now()
        item = {
            "id": route_id,
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
                    route
                    for route in transaction.model_routes.list()
                    if route.get("capability") == item["capability"]
                    and route.get("purpose") == item["purpose"]
                ),
                None,
            )
            if duplicate is not None:
                raise ApiError(
                    "MODEL_ROUTE_CONFLICT",
                    "A model route already exists for this capability and purpose.",
                    status_code=409,
                    details={"route_id": duplicate["id"]},
                )
            primary_config = transaction.provider_configs.get(primary_config_id)
            if primary_config is None:
                raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Primary provider config does not exist.", status_code=404)
            self._require_provider_capability(
                primary_config,
                payload["capability"],
                payload["primary"]["model"],
            )
            for fallback in payload.get("fallbacks", []):
                fallback_config = transaction.provider_configs.get(fallback.get("provider_config_id"))
                if fallback_config is None:
                    raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Fallback provider config does not exist.", status_code=404)
                self._require_provider_capability(
                    fallback_config,
                    payload["capability"],
                    fallback["model"],
                )
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

    def _record_route_health(
        self, route_id: str, organization_id: str, *, status: str, error: Optional[str]
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            route = transaction.model_routes.get(route_id)
            if route is None:
                return
            route["last_health"] = {
                "status": status,
                "checked_at": utc_now(),
                "error": error[:500] if error else None,
            }
            route["updated_at"] = utc_now()
            transaction.model_routes.update(route, expected_version=route["version"])

    def _probe_request(self, capability: str, organization_id: str, purpose: str) -> Any:
        if capability == cap.LLM_CHAT_JSON:
            return ChatJSONRequest(
                organization_id=organization_id,
                purpose=purpose,
                messages=[ChatMessage(role="user", content="ping")],
                metadata={},
            )
        if capability == cap.LLM_CHAT_TEXT:
            return ChatTextRequest(
                organization_id=organization_id,
                purpose=purpose,
                messages=[ChatMessage(role="user", content="ping")],
            )
        if capability == cap.EMBEDDING_TEXT:
            return TextEmbeddingRequest(
                organization_id=organization_id,
                purpose=purpose,
                texts=["ping"],
            )
        if capability == cap.AVATAR_SPEAK:
            return AvatarSpeakRequest(
                organization_id=organization_id,
                purpose=purpose,
                text="连接测试",
            )
        if capability == cap.TTS_SYNTHESIZE:
            return TTSSynthesizeRequest(
                organization_id=organization_id,
                purpose=purpose,
                text="连接测试",
            )
        if capability == cap.STT_BATCH:
            return BatchSTTRequest(
                organization_id=organization_id,
                purpose=purpose,
                audio_uri="mock-media://connection-test.webm",
                metadata={"development_transcript": "连接测试"},
            )
        if capability == cap.STT_STREAMING:
            return StreamingSTTRequest(
                organization_id=organization_id,
                interview_id="connection_test",
                turn_id="connection_test_turn",
                purpose=purpose,
                metadata={"development_transcript": "连接测试", "confidence": 1.0},
            )
        raise ApiError(
            "MODEL_CAPABILITY_NOT_IMPLEMENTED",
            "Capability has no unified local test schema.",
            status_code=409,
        )

    def _require_provider_capability(
        self,
        provider_config: Dict[str, Any],
        capability: str,
        model: str,
    ) -> None:
        manifest = get_provider_manifest(provider_config["provider_id"])
        if not manifest.get("implemented", manifest["provider_id"] == "mock"):
            raise ApiError(
                "MODEL_PROVIDER_NOT_IMPLEMENTED",
                "Configured provider does not have a runtime adapter yet.",
                status_code=409,
            )
        if capability not in manifest["capabilities"]:
            raise ApiError(
                "MODEL_PROVIDER_CAPABILITY_MISSING",
                "Configured provider does not declare the route capability.",
                status_code=409,
            )
        if manifest.get("model_selection") == "predefined":
            declared = any(
                item.get("model_id") == model and capability in item.get("capabilities", [])
                for item in manifest.get("models", [])
            )
            if not declared:
                raise ApiError(
                    "MODEL_PROVIDER_MODEL_UNAVAILABLE",
                    "Configured provider does not declare this model for the route capability.",
                    status_code=409,
                    details={
                        "provider_id": manifest["provider_id"],
                        "model": model,
                        "capability": capability,
                    },
                )

    def _with_manifest_defaults(self, manifest: Dict[str, Any], config: Any) -> Dict[str, Any]:
        if not isinstance(config, dict):
            return config
        return {**(manifest.get("defaults") or {}), **config}

    def _default_model(self, manifest: Dict[str, Any], capability: str) -> Optional[str]:
        candidates = [
            item
            for item in manifest.get("models", [])
            if capability in item.get("capabilities", [])
        ]
        selected = next((item for item in candidates if item.get("default") is True), None)
        if selected is None and candidates:
            selected = candidates[0]
        return selected.get("model_id") if selected else None

    def _test_model(
        self,
        provider_config: Dict[str, Any],
        manifest: Dict[str, Any],
        capability: str,
        *,
        requested_model: Optional[str],
    ) -> Optional[str]:
        if requested_model:
            return str(requested_model)
        config = provider_config.get("config") or {}
        test_models = config.get("test_models") or {}
        if isinstance(test_models, dict) and test_models.get(capability):
            return str(test_models[capability])
        legacy_model = str(config.get("test_model") or "").strip()
        if legacy_model:
            catalog = manifest.get("models") or []
            if not catalog or any(
                item.get("model_id") == legacy_model
                and capability in item.get("capabilities", [])
                for item in catalog
            ):
                return legacy_model
        return self._default_model(manifest, capability)

    def _validate_manifest_document(self, value: Any, schema: Dict[str, Any], path: str) -> None:
        expected_type = schema.get("type")
        if expected_type == "object" and not isinstance(value, dict):
            raise ApiError(
                "MODEL_PROVIDER_CONFIG_INVALID",
                "%s must be an object." % path,
                status_code=400,
            )
        if not isinstance(value, dict):
            return
        missing = [
            key
            for key in schema.get("required", [])
            if key not in value or value[key] is None or value[key] == ""
        ]
        if missing:
            raise ApiError(
                "MODEL_PROVIDER_CONFIG_INVALID",
                "%s is missing required fields." % path,
                status_code=400,
                details={"fields": sorted(missing)},
            )
        properties = schema.get("properties") or {}
        type_map = {
            "string": str,
            "object": dict,
            "array": list,
            "boolean": bool,
            "integer": int,
            "number": (int, float),
        }
        for key, item in value.items():
            item_schema = properties.get(key) or {}
            python_type = type_map.get(item_schema.get("type"))
            if python_type is not None and (not isinstance(item, python_type) or isinstance(item, bool) and item_schema.get("type") in {"integer", "number"}):
                raise ApiError(
                    "MODEL_PROVIDER_CONFIG_INVALID",
                    "%s.%s has the wrong type." % (path, key),
                    status_code=400,
                )

    def _validate_route_payload(self, payload: Dict[str, Any]) -> None:
        targets = [payload.get("primary") or {}] + list(payload.get("fallbacks") or [])
        for target in targets:
            if not target.get("provider_config_id") or not target.get("model"):
                raise ApiError(
                    "MODEL_ROUTE_INVALID",
                    "Every model route target requires provider_config_id and model.",
                    status_code=400,
                )
            try:
                timeout_s = float(target.get("timeout_s", 20))
            except (TypeError, ValueError) as exc:
                raise ApiError("MODEL_ROUTE_INVALID", "Route timeout_s must be numeric.", status_code=400) from exc
            if timeout_s <= 0:
                raise ApiError("MODEL_ROUTE_INVALID", "Route timeout_s must be positive.", status_code=400)
            target["timeout_s"] = timeout_s
        policy = payload.get("policy") or {}
        try:
            retry_count = int(policy.get("retry_count", 0))
        except (TypeError, ValueError) as exc:
            raise ApiError("MODEL_ROUTE_INVALID", "retry_count must be an integer.", status_code=400) from exc
        if retry_count < 0 or retry_count > 3:
            raise ApiError("MODEL_ROUTE_INVALID", "retry_count must be between 0 and 3.", status_code=400)
        fallback_on = policy.get("fallback_on")
        if fallback_on is not None and (
            not isinstance(fallback_on, list) or not all(isinstance(item, str) for item in fallback_on)
        ):
            raise ApiError("MODEL_ROUTE_INVALID", "fallback_on must be a list of error codes.", status_code=400)
