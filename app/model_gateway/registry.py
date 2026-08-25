import json
import importlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError


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
    _validate_manifest(manifest, path)
    manifest.setdefault("models", [])
    manifest.setdefault("config_schema", {"type": "object", "properties": {}})
    manifest.setdefault("credential_schema", {"type": "object", "properties": {}})
    manifest.setdefault("implemented", manifest["provider_id"] == "mock")
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

    for model in manifest.get("models", []):
        model_capabilities = model.get("capabilities", [])
        unsupported_model_caps = sorted(set(model_capabilities).difference(capabilities))
        if unsupported_model_caps:
            raise ProviderManifestError(
                "Provider manifest %s model %s declares capabilities not owned by provider: %s"
                % (path, model.get("model_id", "<unknown>"), ", ".join(unsupported_model_caps))
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
