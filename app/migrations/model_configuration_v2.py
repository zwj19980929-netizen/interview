"""One-way migration from ModelProviderConfig documents to v2 model resources."""

import argparse
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from app.model_gateway.capabilities import CAPABILITY_MODEL_TYPES
from app.model_gateway.forms import apply_form_defaults
from app.model_gateway.registry import get_model_definition, get_provider_manifest


Document = Dict[str, Any]


def migrate_documents(
    provider_configs: Iterable[Document], routes: Iterable[Document]
) -> Tuple[List[Document], List[Document], List[Document]]:
    """Return provider connections, model configurations, and rewritten routes."""
    configs = {item["id"]: deepcopy(item) for item in provider_configs}
    connections = [_connection_from_legacy(item) for item in configs.values()]
    models: Dict[Tuple[str, str, str], Document] = {}
    rewritten_routes: List[Document] = []

    for legacy in configs.values():
        configured = legacy.get("config", {})
        for capability, model_id in (configured.get("test_models") or {}).items():
            if capability in CAPABILITY_MODEL_TYPES and isinstance(model_id, str) and model_id:
                _ensure_model(legacy, CAPABILITY_MODEL_TYPES[capability], model_id, models)
        legacy_test_model = configured.get("test_model")
        if isinstance(legacy_test_model, str) and legacy_test_model:
            declared = next(
                (item for item in get_provider_manifest(legacy["provider_id"])["models"] if item["model_id"] == legacy_test_model),
                None,
            )
            if declared:
                _ensure_model(legacy, declared["model_type"], legacy_test_model, models)

    for route in routes:
        migrated = deepcopy(route)
        migrated["primary"] = _target_from_legacy(route["primary"], route["capability"], configs, models)
        migrated["fallbacks"] = [
            _target_from_legacy(target, route["capability"], configs, models)
            for target in route.get("fallbacks", [])
        ]
        rewritten_routes.append(migrated)

    return connections, list(models.values()), rewritten_routes


def migrate_sqlite(path: str, *, dry_run: bool = False) -> Dict[str, int]:
    """Migrate a stopped local SQLite database inside one transaction."""
    database = Path(path)
    with sqlite3.connect(str(database)) as connection:
        connection.row_factory = sqlite3.Row
        old_configs = _read_collection(connection, "provider_configs")
        routes = _read_collection(connection, "model_routes")
        connections, models, rewritten_routes = migrate_documents(old_configs, routes)
        for collection, items in (
            ("provider_connections", connections),
            ("model_configurations", models),
            ("model_routes", rewritten_routes),
        ):
            for item in items:
                connection.execute(
                    "INSERT OR REPLACE INTO documents(collection, id, data, updated_at) VALUES (?, ?, ?, ?)",
                    (collection, item["id"], json.dumps(item, ensure_ascii=False), item.get("updated_at")),
                )
        connection.execute("DELETE FROM documents WHERE collection = 'provider_configs'")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(provider_secrets)")}
        if "provider_config_id" in columns and "provider_connection_id" not in columns:
            connection.execute("ALTER TABLE provider_secrets RENAME COLUMN provider_config_id TO provider_connection_id")
        if dry_run:
            connection.rollback()
    return {"provider_connections": len(connections), "model_configurations": len(models), "model_routes": len(rewritten_routes)}


def _read_collection(connection: sqlite3.Connection, collection: str) -> List[Document]:
    return [json.loads(row["data"]) for row in connection.execute("SELECT data FROM documents WHERE collection = ?", (collection,))]


def _connection_from_legacy(item: Document) -> Document:
    manifest = get_provider_manifest(item["provider_id"])
    config = item.get("config", {})
    connection_fields = {field["name"] for field in manifest["connection_form"]["fields"]}
    return {
        "id": item["id"],
        "organization_id": item["organization_id"],
        "provider_id": item["provider_id"],
        "display_name": item["display_name"],
        "enabled": item.get("enabled", True),
        "connection_config": apply_form_defaults(
            manifest["connection_form"], {key: value for key, value in config.items() if key in connection_fields}
        ),
        "credential_ref": item.get("credential_ref", "secret://provider-connections/%s" % item["id"]),
        "credential_status": "valid" if item["provider_id"] == "mock" else "untested",
        "last_validation": None,
        "version": item.get("version", 1),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _target_from_legacy(
    target: Document,
    capability: str,
    configs: Dict[str, Document],
    models: Dict[Tuple[str, str, str], Document],
) -> Document:
    connection_id = target["provider_config_id"]
    legacy = configs[connection_id]
    model_type = CAPABILITY_MODEL_TYPES[capability]
    provider_model_id = target["model"]
    key = (connection_id, model_type, provider_model_id)
    _ensure_model(legacy, model_type, provider_model_id, models)
    return {
        "model_configuration_id": models[key]["id"],
        "timeout_s": target.get("timeout_s", 30),
        **({"pricing": target["pricing"]} if "pricing" in target else {}),
    }


def _ensure_model(
    legacy: Document,
    model_type: str,
    provider_model_id: str,
    models: Dict[Tuple[str, str, str], Document],
) -> None:
    key = (legacy["id"], model_type, provider_model_id)
    if key in models:
        return
    definition = get_model_definition(legacy["provider_id"], model_type, provider_model_id)
    config = legacy.get("config", {})
    setting_names = {field["name"] for field in definition["configuration_form"]["fields"]}
    digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:20]
    models[key] = {
        "id": "model_cfg_%s" % digest,
        "organization_id": legacy["organization_id"],
        "provider_connection_id": legacy["id"],
        "provider_id": legacy["provider_id"],
        "model_type": model_type,
        "provider_model_id": provider_model_id,
        "display_name": "%s · %s" % (legacy["display_name"], provider_model_id),
        "supported_capabilities": list(definition["capabilities"]),
        "settings": apply_form_defaults(
            definition["configuration_form"],
            {key: value for key, value in config.items() if key in setting_names},
        ),
        "default_parameters": {},
        "definition_version": get_provider_manifest(legacy["provider_id"])["version"],
        "enabled": legacy.get("enabled", True),
        "status": "ready" if legacy["provider_id"] == "mock" else "untested",
        "last_validation": None,
        "version": 1,
        "created_at": legacy.get("created_at"),
        "updated_at": legacy.get("updated_at"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate a local SQLite database to model configuration v2.")
    parser.add_argument("sqlite_path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate_sqlite(args.sqlite_path, dry_run=args.dry_run), ensure_ascii=False))


if __name__ == "__main__":
    main()
