import json
import importlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.forms import (
    FormSchemaError,
    merge_form_schemas,
    schema_from_json_object,
    validate_form_schema,
)


PROVIDERS_DIR = Path(__file__).resolve().parents[1] / "providers"
PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
REQUIRED_FIELDS = {"provider_id", "display_name", "version", "capabilities", "entrypoint"}


class ProviderManifestError(ValueError):
    pass


def _manifest_paths(providers_dir: Optional[Path] = None) -> List[Path]:
    root = providers_dir or PROVIDERS_DIR
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*/provider.json") if path.is_file())


def load_provider_manifest(path: Path) -> Dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderManifestError("Invalid provider manifest JSON: %s" % path) from exc
    manifest = _normalize_manifest(manifest)
    _validate_manifest(manifest, path)
    return manifest


def _normalize_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    manifest = dict(manifest)
    manifest.setdefault("schema_version", "1")
    manifest.setdefault("models", [])
    manifest.setdefault("model_selection", "customizable")
    manifest.setdefault("defaults", {})
    manifest.setdefault("implemented", manifest.get("provider_id") == "mock")
    manifest.setdefault(
        "connection_form",
        schema_from_json_object(manifest.get("config_schema") or {"type": "object", "properties": {}}),
    )
    manifest.setdefault(
        "credential_form",
        schema_from_json_object(
            manifest.get("credential_schema") or {"type": "object", "properties": {}},
            secret=True,
        ),
    )
    model_types = manifest.get("model_types")
    if not isinstance(model_types, dict):
        model_types = {}
        for capability in manifest.get("capabilities", []):
            model_type = cap.CAPABILITY_MODEL_TYPES.get(capability)
            if not model_type:
                continue
            item = model_types.setdefault(
                model_type,
                {
                    "label": _model_type_label(model_type),
                    "selection_mode": manifest["model_selection"],
                    "capabilities": [],
                    "configuration_form": {"schema_version": "legacy", "fields": []},
                },
            )
            item["capabilities"].append(capability)
        manifest["model_types"] = model_types
    normalized_models = []
    for model in manifest["models"]:
        item = dict(model)
        item.setdefault("label", item.get("model_id", ""))
        item.setdefault("model_type", _model_type_for_capabilities(item.get("capabilities", [])))
        item.setdefault("configuration_form", {"schema_version": manifest["schema_version"], "fields": []})
        item.setdefault("properties", {})
        normalized_models.append(item)
    manifest["models"] = normalized_models
    return manifest


def _validate_manifest(manifest: Dict[str, Any], path: Path) -> None:
    missing = sorted(REQUIRED_FIELDS.difference(manifest.keys()))
    if missing:
        raise ProviderManifestError("Provider manifest %s missing fields: %s" % (path, ", ".join(missing)))

    provider_id = manifest["provider_id"]
    if not isinstance(provider_id, str) or not PROVIDER_ID_PATTERN.match(provider_id):
        raise ProviderManifestError("Provider manifest %s has invalid provider_id." % path)
    if path.parent.name != provider_id:
        raise ProviderManifestError("Provider manifest %s provider_id must match directory name." % path)

    capabilities = manifest["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise ProviderManifestError("Provider manifest %s must declare at least one capability." % path)
    unsupported = sorted(set(capabilities).difference(cap.ALL_CAPABILITIES))
    if unsupported:
        raise ProviderManifestError("Provider manifest %s has unsupported capabilities: %s" % (path, ", ".join(unsupported)))

    model_selection = manifest.get("model_selection")
    if model_selection not in {"predefined", "customizable"}:
        raise ProviderManifestError("Provider manifest %s has invalid model_selection." % path)
    defaults = manifest.get("defaults")
    if not isinstance(defaults, dict):
        raise ProviderManifestError("Provider manifest %s defaults must be an object." % path)
    models = manifest.get("models")
    if not isinstance(models, list):
        raise ProviderManifestError("Provider manifest %s models must be a list." % path)
    try:
        manifest["connection_form"] = validate_form_schema(
            manifest["connection_form"], path="%s.connection_form" % provider_id
        )
        manifest["credential_form"] = validate_form_schema(
            manifest["credential_form"], path="%s.credential_form" % provider_id
        )
    except FormSchemaError as exc:
        raise ProviderManifestError("Provider manifest %s has an invalid form: %s" % (path, exc)) from exc

    model_types = manifest.get("model_types")
    if not isinstance(model_types, dict) or not model_types:
        raise ProviderManifestError("Provider manifest %s must declare model_types." % path)
    declared_capabilities = set()
    for model_type, definition in model_types.items():
        if model_type not in cap.MODEL_TYPE_CAPABILITIES or not isinstance(definition, dict):
            raise ProviderManifestError("Provider manifest %s has invalid model type %s." % (path, model_type))
        selection_mode = definition.get("selection_mode", model_selection)
        if selection_mode not in {"predefined", "customizable"}:
            raise ProviderManifestError("Provider manifest %s model type %s has invalid selection mode." % (path, model_type))
        definition["selection_mode"] = selection_mode
        type_capabilities = definition.get("capabilities")
        if not isinstance(type_capabilities, list) or not type_capabilities:
            raise ProviderManifestError("Provider manifest %s model type %s has no capabilities." % (path, model_type))
        if set(type_capabilities).difference(cap.MODEL_TYPE_CAPABILITIES[model_type]):
            raise ProviderManifestError("Provider manifest %s model type %s owns invalid capabilities." % (path, model_type))
        declared_capabilities.update(type_capabilities)
        try:
            definition["configuration_form"] = validate_form_schema(
                definition.get("configuration_form") or {"fields": []},
                path="%s.model_types.%s.configuration_form" % (provider_id, model_type),
            )
        except FormSchemaError as exc:
            raise ProviderManifestError("Provider manifest %s has an invalid model form: %s" % (path, exc)) from exc
    if declared_capabilities != set(capabilities):
        raise ProviderManifestError("Provider manifest %s model types do not cover provider capabilities." % path)

    seen_model_ids = set()
    default_capabilities = set()
    for model in models:
        if not isinstance(model, dict):
            raise ProviderManifestError("Provider manifest %s model entries must be objects." % path)
        model_id = model.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ProviderManifestError("Provider manifest %s has a model without model_id." % path)
        if model_id in seen_model_ids:
            raise ProviderManifestError("Provider manifest %s has duplicate model %s." % (path, model_id))
        seen_model_ids.add(model_id)
        model_capabilities = model.get("capabilities", [])
        if not isinstance(model_capabilities, list) or not model_capabilities:
            raise ProviderManifestError("Provider manifest %s model %s must declare capabilities." % (path, model_id))
        unsupported_model_caps = sorted(set(model_capabilities).difference(capabilities))
        if unsupported_model_caps:
            raise ProviderManifestError(
                "Provider manifest %s model %s declares capabilities not owned by provider: %s"
                % (path, model_id, ", ".join(unsupported_model_caps))
            )
        model_type = model.get("model_type")
        if model_type not in model_types or set(model_capabilities).difference(model_types[model_type]["capabilities"]):
            raise ProviderManifestError(
                "Provider manifest %s model %s does not match model type %s."
                % (path, model_id, model_type)
            )
        try:
            model["configuration_form"] = validate_form_schema(
                model.get("configuration_form") or {"fields": []},
                path="%s.models.%s.configuration_form" % (provider_id, model_id),
            )
        except FormSchemaError as exc:
            raise ProviderManifestError("Provider manifest %s has an invalid model form: %s" % (path, exc)) from exc
        if model.get("default") is True:
            duplicate_defaults = default_capabilities.intersection(model_capabilities)
            if duplicate_defaults:
                raise ProviderManifestError(
                    "Provider manifest %s has multiple default models for: %s"
                    % (path, ", ".join(sorted(duplicate_defaults)))
                )
            default_capabilities.update(model_capabilities)

    for model_type, definition in model_types.items():
        if definition["selection_mode"] == "predefined" and not any(
            model["model_type"] == model_type for model in models
        ):
            raise ProviderManifestError(
                "Provider manifest %s predefined model type %s has no models." % (path, model_type)
            )


def load_provider_catalog(providers_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    providers = [load_provider_manifest(path) for path in _manifest_paths(providers_dir)]
    providers.sort(key=lambda item: (item["provider_id"] != "mock", item["provider_id"]))
    return providers


def get_provider_catalog() -> List[Dict[str, Any]]:
    return load_provider_catalog()


def provider_exists(provider_id: str) -> bool:
    return any(provider["provider_id"] == provider_id for provider in get_provider_catalog())


def get_provider_manifest(provider_id: str) -> Dict[str, Any]:
    manifest = next((item for item in get_provider_catalog() if item["provider_id"] == provider_id), None)
    if manifest is None:
        raise ProviderError(
            "provider_not_installed",
            "Provider %s is not installed." % provider_id,
            retryable=False,
        )
    return manifest


def get_model_definition(provider_id: str, model_type: str, model_id: str) -> Dict[str, Any]:
    manifest = get_provider_manifest(provider_id)
    type_definition = manifest["model_types"].get(model_type)
    if type_definition is None:
        raise ProviderError(
            "provider_model_type_missing",
            "Provider %s does not support model type %s." % (provider_id, model_type),
            retryable=False,
        )
    declared = next(
        (
            model
            for model in manifest["models"]
            if model["model_type"] == model_type and model["model_id"] == model_id
        ),
        None,
    )
    if declared is None and type_definition["selection_mode"] == "predefined":
        raise ProviderError(
            "provider_model_unavailable",
            "Provider %s does not declare model %s for %s." % (provider_id, model_id, model_type),
            retryable=False,
        )
    model = dict(
        declared
        or {
            "model_id": model_id,
            "label": model_id,
            "model_type": model_type,
            "capabilities": list(type_definition["capabilities"]),
            "configuration_form": {"fields": []},
            "properties": {},
            "custom": True,
        }
    )
    model["configuration_form"] = merge_form_schemas(
        type_definition.get("configuration_form") or {"fields": []},
        model.get("configuration_form") or {"fields": []},
    )
    return model


def _model_type_for_capabilities(capabilities: List[str]) -> Optional[str]:
    types = {cap.CAPABILITY_MODEL_TYPES.get(capability) for capability in capabilities}
    types.discard(None)
    return next(iter(types)) if len(types) == 1 else None


def _model_type_label(model_type: str) -> str:
    return {
        cap.MODEL_TYPE_LLM: "大语言模型",
        cap.MODEL_TYPE_EMBEDDING: "Embedding",
        cap.MODEL_TYPE_STT: "语音识别",
        cap.MODEL_TYPE_TTS: "语音合成",
        cap.MODEL_TYPE_AVATAR: "数字人",
        cap.MODEL_TYPE_REALTIME_SPEECH: "实时语音对话（S2S / STS）",
        cap.MODEL_TYPE_MODERATION: "内容审核",
    }.get(model_type, model_type)


class ProviderRegistry:
    """Loads provider adapters from the manifest entrypoint at the provider seam."""

    def __init__(self, adapters: Optional[Dict[str, Any]] = None) -> None:
        self._adapters = dict(adapters or {})
        self._manifests = {item["provider_id"]: item for item in get_provider_catalog()}

    def manifest(self, provider_id: str) -> Dict[str, Any]:
        manifest = self._manifests.get(provider_id)
        if manifest is None:
            raise ProviderError(
                "provider_not_installed",
                "Provider %s is not installed." % provider_id,
                retryable=False,
            )
        return manifest

    def adapter(self, provider_id: str, capability: str) -> Any:
        manifest = self.manifest(provider_id)
        if capability not in manifest["capabilities"]:
            raise ProviderError(
                "provider_capability_missing",
                "Provider %s does not declare capability %s." % (provider_id, capability),
                retryable=False,
            )
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            adapter = self._load_entrypoint(manifest)
            self._adapters[provider_id] = adapter
        if getattr(adapter, "provider_id", None) != provider_id or not callable(getattr(adapter, "invoke", None)):
            raise ProviderError(
                "provider_entrypoint_invalid",
                "Provider %s entrypoint does not implement the provider adapter interface." % provider_id,
                retryable=False,
            )
        return adapter

    def _load_entrypoint(self, manifest: Dict[str, Any]) -> Any:
        provider_id = manifest["provider_id"]
        if not manifest.get("implemented", provider_id == "mock"):
            raise ProviderError(
                "provider_not_implemented",
                "Provider %s is installed but its runtime adapter is not implemented." % provider_id,
                retryable=False,
            )
        entrypoint = str(manifest.get("entrypoint") or "")
        module_name, separator, attribute_name = entrypoint.partition(":")
        if not separator or not module_name or not attribute_name:
            raise ProviderError(
                "provider_entrypoint_invalid",
                "Provider %s has an invalid entrypoint." % provider_id,
                retryable=False,
            )
        try:
            module = importlib.import_module(module_name)
            adapter_type = getattr(module, attribute_name)
            return adapter_type()
        except (ImportError, AttributeError, TypeError) as exc:
            raise ProviderError(
                "provider_entrypoint_invalid",
                "Provider %s entrypoint could not be loaded." % provider_id,
                retryable=False,
            ) from exc
