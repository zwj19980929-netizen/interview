from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.registry import get_provider_catalog, get_provider_manifest, provider_exists
from app.model_gateway.schemas import AvatarSpeakRequest, ChatJSONRequest, ChatMessage, TextEmbeddingRequest
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
        self._validate_manifest_document(payload.get("config", {}), manifest["config_schema"], "config")
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
            "config": payload.get("config", {}),
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
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            provider_config = transaction.provider_configs.get(config_id)
        if not provider_config:
            raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Model provider config does not exist.", status_code=404)
        manifest = get_provider_manifest(provider_config["provider_id"])
        capability = next(
            (item for item in (cap.LLM_CHAT_JSON, cap.EMBEDDING_TEXT, cap.AVATAR_SPEAK) if item in manifest["capabilities"]),
            None,
        )
        if capability is None:
            raise ApiError(
                "MODEL_CAPABILITY_NOT_IMPLEMENTED",
                "Provider has no capability with a unified local test schema.",
                status_code=409,
            )
        model = provider_config.get("config", {}).get("test_model") or {
            cap.LLM_CHAT_JSON: "mock-json" if provider_config["provider_id"] == "mock" else "chat-model-default",
            cap.EMBEDDING_TEXT: "mock-embedding" if provider_config["provider_id"] == "mock" else "embedding-model-default",
            cap.AVATAR_SPEAK: "mock-avatar",
        }[capability]
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
            primary_config = transaction.provider_configs.get(primary_config_id)
            if primary_config is None:
                raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Primary provider config does not exist.", status_code=404)
            self._require_provider_capability(primary_config, payload["capability"])
            for fallback in payload.get("fallbacks", []):
                fallback_config = transaction.provider_configs.get(fallback.get("provider_config_id"))
                if fallback_config is None:
                    raise ApiError("MODEL_PROVIDER_CONFIG_NOT_FOUND", "Fallback provider config does not exist.", status_code=404)
                self._require_provider_capability(fallback_config, payload["capability"])
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
        response = await self.gateway.invoke(route["capability"], request, route=route)
        return response.model_dump()

    def _probe_request(self, capability: str, organization_id: str, purpose: str) -> Any:
        if capability == cap.LLM_CHAT_JSON:
            return ChatJSONRequest(
                organization_id=organization_id,
                purpose=purpose,
                messages=[ChatMessage(role="user", content="ping")],
                metadata={},
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
        raise ApiError(
            "MODEL_CAPABILITY_NOT_IMPLEMENTED",
            "Capability has no unified local test schema.",
            status_code=409,
        )

    def _require_provider_capability(self, provider_config: Dict[str, Any], capability: str) -> None:
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
