import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, Iterable, Optional


OUTPUT_FIELDS = (
    "model_configuration_id",
    "model_configuration_version",
    "voice_profile_id",
    "language",
    "audio_format",
    "speaking_rate",
)


def speech_profile_output(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {field: deepcopy(profile.get(field)) for field in OUTPUT_FIELDS}


def speech_profile_fingerprint(profile: Dict[str, Any]) -> str:
    fingerprint_payload: Dict[str, Any] = {
        "output": speech_profile_output(profile),
    }
    if profile.get("knowledge_base_revisions") is not None:
        fingerprint_payload["knowledge_base_revisions"] = {
            str(key): int(value)
            for key, value in sorted(profile["knowledge_base_revisions"].items())
        }
    elif profile.get("revision") is not None:
        fingerprint_payload["revision"] = int(profile["revision"])
    payload = json.dumps(
        fingerprint_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:%s" % hashlib.sha256(payload).hexdigest()


def freeze_interview_speech_profile(
    knowledge_bases: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    items = list(knowledge_bases)
    profiles = [item.get("speech_profile") for item in items]
    if not items or any(profile is None for profile in profiles):
        return None
    outputs = [speech_profile_output(profile) for profile in profiles if profile is not None]
    if any(output != outputs[0] for output in outputs[1:]):
        return None
    frozen = {
        **outputs[0],
        "source": "knowledge_base_selection",
        "knowledge_base_revisions": {
            item["id"]: int((item.get("speech_profile") or {}).get("revision", 0))
            for item in items
        },
    }
    frozen["fingerprint"] = speech_profile_fingerprint(frozen)
    return frozen


def speech_asset_matches_profile(asset: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    return (
        (
            not profile.get("fingerprint")
            or asset.get("speech_profile_fingerprint") == profile.get("fingerprint")
        )
        and asset.get("model_configuration_id") == profile.get("model_configuration_id")
        and asset.get("model_configuration_version")
        == profile.get("model_configuration_version")
        and asset.get("voice_profile_id") == profile.get("voice_profile_id")
        and asset.get("language") == profile.get("language")
        and (asset.get("audio_format") or asset.get("content_type"))
        == profile.get("audio_format")
        and float(asset.get("speaking_rate", 1.0))
        == float(profile.get("speaking_rate", 1.0))
    )
