"""Licensed local VRM asset contract shared by readiness and delivery."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any, Dict


REQUIRED_VRM_VISEMES = (
    "sil",
    "PP",
    "FF",
    "TH",
    "DD",
    "kk",
    "CH",
    "SS",
    "nn",
    "RR",
    "aa",
    "E",
    "ih",
    "oh",
    "ou",
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VRM_ASSET_PATH = _PROJECT_ROOT / "model" / "interviewer.vrm"
DEFAULT_VRM_LICENSE_MANIFEST = (
    _PROJECT_ROOT / "model" / "interviewer-license.json"
)


def inspect_licensed_vrm() -> Dict[str, Any]:
    asset_path = _configured_path(
        "INTERVIEWER_VRM_ASSET_PATH", DEFAULT_VRM_ASSET_PATH
    )
    manifest_path = _configured_path(
        "INTERVIEWER_VRM_LICENSE_MANIFEST", DEFAULT_VRM_LICENSE_MANIFEST
    )
    result: Dict[str, Any] = {
        "ready": False,
        "asset_path": asset_path,
        "manifest_path": manifest_path,
        "asset_sha256": None,
        "reason": "asset_missing",
    }
    if asset_path.suffix.lower() != ".vrm" or not asset_path.is_file():
        return result
    if not manifest_path.is_file():
        result["reason"] = "license_manifest_missing"
        return result
    try:
        asset_content = asset_path.read_bytes()
        asset_hash = hashlib.sha256(asset_content).hexdigest()
        binary = _inspect_vrm_binary(asset_content)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, struct.error, json.JSONDecodeError):
        result["reason"] = "asset_or_manifest_invalid"
        return result
    if not isinstance(manifest, dict):
        result["reason"] = "license_manifest_invalid"
        return result
    spec = str(manifest.get("vrm_spec") or manifest.get("vrm_version") or "")
    declared_hash = str(manifest.get("asset_sha256") or "").removeprefix(
        "sha256:"
    )
    rights_holder = str(manifest.get("rights_holder") or "").strip()
    embedded = binary["license_meta"]
    manifest_authors = manifest.get("authors") or []
    if not isinstance(manifest_authors, list):
        manifest_authors = []
    license_matches_binary = bool(
        manifest.get("schema") == "interviewer-vrm-license.v1"
        and str(manifest.get("asset") or "") == asset_path.name
        and str(manifest.get("name") or "") == embedded["name"]
        and str(manifest.get("version") or "") == embedded["version"]
        and [str(author).strip() for author in manifest_authors]
        == embedded["authors"]
        and str(manifest.get("copyright") or "")
        == embedded["copyright"]
        and rights_holder
        and rights_holder in embedded["authors"]
        and str(manifest.get("commercial_use_scope") or "")
        == embedded["commercial_usage"]
        and embedded["commercial_usage"] in {"personalProfit", "corporation"}
        and str(manifest.get("avatar_permission") or "")
        == embedded["avatar_permission"]
        and manifest.get("credit_required")
        is (embedded["credit_notation"] == "required")
        and manifest.get("modification_allowed")
        is (embedded["modification"] != "prohibited")
    )
    binary_ready = bool(
        binary["vrm_spec"] == "1.0"
        and binary["required_visemes"]
        and binary["blink"]
        and binary["look_at"]
        and binary["humanoid"]
    )
    license_ready = bool(
        manifest.get("commercial_use") is True
        and manifest.get("likeness_use_authorized") is True
        and str(manifest.get("license_id") or "").strip()
        and rights_holder
        and spec == "1.0"
        and declared_hash == asset_hash
        and license_matches_binary
    )
    ready = binary_ready and license_ready
    reason = "ready"
    if not binary_ready:
        reason = "vrm_binary_contract_mismatch"
    elif not license_ready:
        reason = "license_or_hash_mismatch"
    result.update(
        {
            "ready": ready,
            "asset_sha256": asset_hash,
            "vrm_spec": spec,
            "commercial_use": manifest.get("commercial_use") is True,
            "license_id": str(manifest.get("license_id") or ""),
            "rights_holder": rights_holder,
            "likeness_use_authorized": manifest.get("likeness_use_authorized")
            is True,
            "commercial_use_scope": str(
                manifest.get("commercial_use_scope") or ""
            ),
            "binary_contract": binary,
            "reason": reason,
        }
    )
    return result


def _configured_path(environment_name: str, default: Path) -> Path:
    configured = os.getenv(environment_name, "").strip()
    if not configured:
        return default
    path = Path(configured).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _inspect_vrm_binary(content: bytes) -> Dict[str, Any]:
    """Read only the bounded GLB JSON chunk needed for the VRM contract."""

    if len(content) < 20:
        raise ValueError("VRM GLB is truncated")
    magic, glb_version, declared_length = struct.unpack_from("<4sII", content, 0)
    if magic != b"glTF" or glb_version != 2 or declared_length != len(content):
        raise ValueError("VRM must be a complete glTF 2.0 binary")
    json_length, json_type = struct.unpack_from("<II", content, 12)
    if json_type != 0x4E4F534A or json_length <= 0 or 20 + json_length > len(content):
        raise ValueError("VRM GLB has no valid JSON chunk")
    document = json.loads(
        content[20 : 20 + json_length].rstrip(b" \x00").decode("utf-8")
    )
    if not isinstance(document, dict):
        raise ValueError("VRM GLB JSON root is invalid")
    vrm = (document.get("extensions") or {}).get("VRMC_vrm") or {}
    if not isinstance(vrm, dict):
        raise ValueError("VRMC_vrm extension is missing")
    expressions = vrm.get("expressions") or {}
    custom = expressions.get("custom") or {}
    preset = expressions.get("preset") or {}
    custom_names = set(custom) if isinstance(custom, dict) else set()
    preset_names = set(preset) if isinstance(preset, dict) else set()
    humanoid = vrm.get("humanoid") or {}
    human_bones = humanoid.get("humanBones") or {}
    required_bones = {"hips", "spine", "head"}
    meta = vrm.get("meta") or {}
    authors = meta.get("authors") or []
    return {
        "vrm_spec": str(vrm.get("specVersion") or ""),
        # VRM 1.0 defines the vowel mouth shapes as presets. Exporters may
        # still place project-specific consonants in custom expressions, so
        # the formal contract must validate the union instead of requiring
        # every viseme to be custom.
        "required_visemes": set(REQUIRED_VRM_VISEMES).issubset(
            custom_names | preset_names
        ),
        "blink": isinstance(preset, dict) and isinstance(preset.get("blink"), dict),
        "look_at": isinstance(vrm.get("lookAt"), dict),
        "humanoid": isinstance(human_bones, dict)
        and required_bones.issubset(set(human_bones)),
        "license_meta": {
            "name": str(meta.get("name") or ""),
            "version": str(meta.get("version") or ""),
            "authors": [
                str(author).strip()
                for author in authors
                if str(author).strip()
            ]
            if isinstance(authors, list)
            else [],
            "copyright": str(meta.get("copyrightInformation") or ""),
            "avatar_permission": str(meta.get("avatarPermission") or ""),
            "commercial_usage": str(meta.get("commercialUsage") or ""),
            "credit_notation": str(meta.get("creditNotation") or ""),
            "modification": str(meta.get("modification") or ""),
        },
    }
