import json

import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.registry import (
    ProviderManifestError,
    ProviderRegistry,
    load_provider_catalog,
    load_provider_manifest,
    provider_exists,
)
from app.providers.mock.provider import MockProvider
from app.providers.openai_compatible.provider import OpenAICompatibleProvider


def test_provider_catalog_loads_provider_json_manifests() -> None:
    catalog = load_provider_catalog()
    provider_ids = {provider["provider_id"] for provider in catalog}

    assert "mock" in provider_ids
    assert "openai_compatible" in provider_ids
    assert "azure_speech" in provider_ids
    assert "tencent_cloud_speech" in provider_ids
    assert provider_exists("mock")
    assert not provider_exists("missing_provider")

    for provider in catalog:
        assert provider["capabilities"]
        assert set(provider["capabilities"]).issubset(cap.ALL_CAPABILITIES)
        assert "manifest_path" not in provider


def test_provider_manifest_validation_rejects_bad_capability(tmp_path) -> None:
    provider_dir = tmp_path / "bad_provider"
    provider_dir.mkdir()
    manifest_path = provider_dir / "provider.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider_id": "bad_provider",
                "display_name": "Bad Provider",
                "version": "0.1.0",
                "capabilities": ["not.real"],
                "entrypoint": "app.providers.bad_provider.provider:BadProvider",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProviderManifestError):
        load_provider_manifest(manifest_path)


def test_provider_registry_loads_runtime_adapters_from_manifest_entrypoints() -> None:
    registry = ProviderRegistry()

    assert isinstance(registry.adapter("mock", cap.LLM_CHAT_JSON), MockProvider)
    assert isinstance(registry.adapter("openai_compatible", cap.EMBEDDING_TEXT), OpenAICompatibleProvider)
