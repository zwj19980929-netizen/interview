import asyncio
from datetime import datetime, timezone
import hashlib
import json
import struct
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.avatar_asset import REQUIRED_VRM_VISEMES, inspect_licensed_vrm
from app.domain.appointment_admission import AppointmentAdmission
from app.model_gateway import capabilities as cap
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.avatar_asset_access import AvatarAssetAccessService
from app.services.interviews import InterviewService
from app.services.livekit_room_binding import (
    InterviewRoomBindingService,
    interview_room_name,
)
from app.services.media_capture import InterviewMediaCaptureService


def _session(
    store: InMemoryStore,
    organization_id: str,
    interview_id: str,
    *,
    candidate_id: str,
    status: str = "paused",
) -> dict:
    now = utc_now()
    with persistence_for(store).transaction(organization_id) as transaction:
        return transaction.interview_sessions.add(
            {
                "id": interview_id,
                "organization_id": organization_id,
                "candidate_id": candidate_id,
                "candidate": {"id": candidate_id, "name": "候选人"},
                "settings": {"record_audio": True, "record_video": False},
                "status": status,
                "phase": "position_bank",
                "turns": [],
                "answers": [],
                "agent_runtime": {"floor": "none", "last_sequence": 0},
                "agent_events": [],
                "created_at": now,
                "updated_at": now,
            }
        )


def _minimal_vrm_binary() -> bytes:
    vowel_presets = {"aa", "ih", "oh", "ou"}
    document = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["VRMC_vrm"],
        "extensions": {
            "VRMC_vrm": {
                "specVersion": "1.0",
                "humanoid": {
                    "humanBones": {
                        "hips": {"node": 0},
                        "spine": {"node": 0},
                        "head": {"node": 0},
                    }
                },
                "expressions": {
                    "preset": {
                        "blink": {},
                        **{name: {} for name in vowel_presets},
                    },
                    "custom": {
                        name: {}
                        for name in REQUIRED_VRM_VISEMES
                        if name not in vowel_presets
                    },
                },
                "lookAt": {"type": "expression"},
                "meta": {
                    "name": "面试官",
                    "version": "1.0",
                    "authors": ["张文君"],
                    "copyrightInformation": "© 2026 张文君",
                    "avatarPermission": "onlySeparatelyLicensedPerson",
                    "commercialUsage": "personalProfit",
                    "creditNotation": "required",
                    "modification": "prohibited",
                },
            }
        },
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    total = 12 + 8 + len(encoded)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
    )


def _capture(
    store: InMemoryStore,
    organization_id: str,
    interview_id: str,
    *,
    capture_id: str,
    egress_id: str,
    participant_identity: str,
) -> dict:
    now = utc_now()
    with persistence_for(store).transaction(organization_id) as transaction:
        return transaction.interview_media_captures.add(
            {
                "id": capture_id,
                "organization_id": organization_id,
                "interview_id": interview_id,
                "candidate_id": "candidate_%s" % organization_id,
                "provider": "livekit",
                "room_name": interview_room_name(organization_id, interview_id),
                "participant_identity": participant_identity,
                "connection_id": "connection_%s" % organization_id,
                "requested_scopes": ["audio_recording"],
                "consented_scopes": ["audio_recording"],
                "status": "recording",
                "egress_id": egress_id,
                "object_key": None,
                "private_uri": None,
                "content_hash": None,
                "byte_count": None,
                "provider_webhook_receipts": [],
                "encryption": "private_object_storage_sse",
                "retention_expires_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )


def _provider_event(room_name: str, egress_id: str, identity: str) -> dict:
    return {
        "event": "egress_updated",
        "egress_info": {
            "egress_id": egress_id,
            "room_name": room_name,
            "identity": identity,
            "status": "EGRESS_ACTIVE",
        },
    }


def test_authenticated_room_binding_detects_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "r" * 48)
    service = InterviewRoomBindingService()
    binding = service.issue("tenant_甲", "interview_1")
    assert service.verify(binding.room_name) == binding
    with pytest.raises(ValueError, match="signature"):
        service.verify(binding.room_name[:-1] + ("A" if binding.room_name[-1] != "A" else "B"))


def test_livekit_webhook_routes_by_signed_room_not_process_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "r" * 48)
    store = InMemoryStore()
    _session(store, "tenant_alpha", "interview_alpha", candidate_id="candidate_alpha")
    _session(store, "tenant_beta", "interview_beta", candidate_id="candidate_beta")
    alpha = _capture(
        store,
        "tenant_alpha",
        "interview_alpha",
        capture_id="capture_alpha",
        egress_id="egress_shared",
        participant_identity="candidate:alpha",
    )
    beta = _capture(
        store,
        "tenant_beta",
        "interview_beta",
        capture_id="capture_beta",
        egress_id="egress_shared",
        participant_identity="candidate:beta",
    )
    event = _provider_event(
        alpha["room_name"], "egress_shared", "candidate:alpha"
    )
    accepted = asyncio.run(
        InterviewMediaCaptureService(store).handle_provider_webhook(event)
    )
    assert accepted["organization_id"] == "tenant_alpha"
    assert len(accepted["provider_webhook_receipts"]) == 1
    with persistence_for(store).transaction("tenant_beta") as transaction:
        untouched = transaction.interview_media_captures.get(beta["id"])
    assert untouched["provider_webhook_receipts"] == []


def test_livekit_webhook_rejects_wrong_room_for_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "r" * 48)
    store = InMemoryStore()
    _session(store, "tenant_alpha", "interview_alpha", candidate_id="candidate_alpha")
    _capture(
        store,
        "tenant_alpha",
        "interview_alpha",
        capture_id="capture_alpha",
        egress_id="egress_alpha",
        participant_identity="candidate:alpha",
    )
    wrong_room = interview_room_name("tenant_alpha", "interview_other")
    with pytest.raises(ApiError) as mismatch:
        asyncio.run(
            InterviewMediaCaptureService(store).handle_provider_webhook(
                _provider_event(wrong_room, "egress_alpha", "candidate:alpha")
            )
        )
    assert mismatch.value.code == "LIVEKIT_EGRESS_CAPTURE_BINDING_INVALID"


def test_livekit_webhook_body_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "r" * 48)
    store = InMemoryStore()
    _session(store, "tenant_alpha", "interview_alpha", candidate_id="candidate_alpha")
    capture = _capture(
        store,
        "tenant_alpha",
        "interview_alpha",
        capture_id="capture_alpha",
        egress_id="egress_alpha",
        participant_identity="candidate:alpha",
    )
    event = _provider_event(
        capture["room_name"], "egress_alpha", "candidate:alpha"
    )
    fingerprint = hashlib.sha256(
        json.dumps(event, sort_keys=True).encode("utf-8")
    ).hexdigest()
    service = InterviewMediaCaptureService(store)
    first = asyncio.run(
        service.handle_provider_webhook(event, event_fingerprint=fingerprint)
    )
    replay = asyncio.run(
        service.handle_provider_webhook(event, event_fingerprint=fingerprint)
    )
    assert replay["version"] == first["version"]
    assert replay["provider_webhook_receipts"] == first["provider_webhook_receipts"]
    with persistence_for(store).transaction("tenant_alpha") as transaction:
        audits = [
            item
            for item in transaction.audit_events.list()
            if item.get("action")
            == "interview.media_capture.provider_webhook.accepted"
        ]
    assert len(audits) == 1


def test_avatar_asset_requires_candidate_session_and_short_lived_grant(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "c" * 48)
    monkeypatch.setenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "m" * 48)
    asset_path = tmp_path / "interviewer.vrm"
    asset_path.write_bytes(_minimal_vrm_binary())
    digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "interviewer-license.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "interviewer-vrm-license.v1",
                "asset": "interviewer.vrm",
                "vrm_spec": "1.0",
                "asset_sha256": digest,
                "name": "面试官",
                "version": "1.0",
                "authors": ["张文君"],
                "copyright": "© 2026 张文君",
                "commercial_use": True,
                "license_id": "license-contract-1",
                "rights_holder": "张文君",
                "likeness_use_authorized": True,
                "commercial_use_scope": "personalProfit",
                "avatar_permission": "onlySeparatelyLicensedPerson",
                "credit_required": True,
                "modification_allowed": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("INTERVIEWER_VRM_ASSET_PATH", str(asset_path))
    monkeypatch.setenv("INTERVIEWER_VRM_LICENSE_MANIFEST", str(manifest_path))
    assert inspect_licensed_vrm()["ready"] is True

    store = InMemoryStore()
    session = _session(
        store, "org_default", "interview_avatar", candidate_id="candidate_avatar"
    )
    interviews = InterviewService(store)
    candidate_token = interviews.candidate_join_url(session).split("token=", 1)[1]
    service = AvatarAssetAccessService(store, clock=lambda: 1_000)
    with pytest.raises(ApiError) as missing_candidate:
        service.issue_config(session["id"], "invalid-token")
    assert missing_candidate.value.code == "CANDIDATE_SESSION_TOKEN_INVALID"

    config = service.issue_config(session["id"], candidate_token)
    assert config["ready"] is True
    assert config["asset_grant_expires_at"] == 1_060
    grant = parse_qs(urlparse(config["asset_url"]).query)["grant"][0]
    authorized = service.authorize(session["id"], grant)
    assert authorized.asset_path == asset_path
    assert authorized.candidate_id == "candidate_avatar"

    with pytest.raises(ApiError) as wrong_interview:
        service.authorize("interview_other", grant)
    assert wrong_interview.value.code == "AVATAR_ASSET_GRANT_SCOPE_INVALID"
    with pytest.raises(ApiError) as expired:
        AvatarAssetAccessService(store, clock=lambda: 1_061).authorize(
            session["id"], grant
        )
    assert expired.value.code == "AVATAR_ASSET_GRANT_EXPIRED"

    asset_path.write_bytes(b"commercial-asset-was-replaced")
    with pytest.raises(ApiError) as replaced:
        service.authorize(session["id"], grant)
    assert replaced.value.code == "LICENSED_VRM_NOT_READY"


def test_candidate_runtime_problem_persists_real_pause_without_leaking_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "c" * 48)
    store = InMemoryStore()
    session = _session(
        store,
        "org_default",
        "interview_runtime_problem",
        candidate_id="candidate_runtime_problem",
        status="in_progress",
    )
    service = InterviewService(store)
    token = service.candidate_join_url(session).split("token=", 1)[1]

    result = service.report_candidate_runtime_problem(
        session["id"], token, "AVATAR_ASSET_UNAVAILABLE"
    )

    assert result == {
        "interview_id": session["id"],
        "accepted": True,
        "status": "paused",
        "problem_code": "AVATAR_ASSET_UNAVAILABLE",
        "action": "await_human_takeover",
        "paused_at": result["paused_at"],
    }
    assert result["paused_at"]
    assert "candidate" not in result
    paused = service.get_interview(session["id"])
    assert paused["status"] == "paused"
    assert paused["interruption"]["reason"] == "candidate_avatar_asset_unavailable"

    # Re-reporting is idempotent at the lifecycle boundary.
    replay = service.report_candidate_runtime_problem(
        session["id"], token, "AVATAR_ASSET_UNAVAILABLE"
    )
    assert replay["status"] == "paused"
    with pytest.raises(ApiError) as invalid:
        service.report_candidate_runtime_problem(
            session["id"], "wrong-token", "AVATAR_ASSET_UNAVAILABLE"
        )
    assert invalid.value.code == "CANDIDATE_SESSION_TOKEN_INVALID"


def test_development_admission_does_not_bypass_a_missing_local_vrm(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setenv(
        "INTERVIEWER_VRM_ASSET_PATH", str(tmp_path / "missing.vrm")
    )
    monkeypatch.setenv(
        "INTERVIEWER_VRM_LICENSE_MANIFEST", str(tmp_path / "missing.json")
    )
    store = InMemoryStore()
    with persistence_for(store).transaction("org_default") as transaction:
        local = AppointmentAdmission().plan_readiness(
            transaction,
            {"bank_slots": []},
            appointment={"settings": {"avatar_mode": "local"}},
            now=datetime.now(timezone.utc),
        )
        cloud = AppointmentAdmission().plan_readiness(
            transaction,
            {"bank_slots": []},
            appointment={"settings": {"avatar_mode": "cloud"}},
            now=datetime.now(timezone.utc),
        )

    local_check = next(
        item for item in local["checks"] if item["name"] == "licensed_local_vrm"
    )
    cloud_check = next(
        item for item in cloud["checks"] if item["name"] == "licensed_local_vrm"
    )
    assert local_check["ready"] is False
    assert local_check["mode"] == "asset_required"
    assert cloud_check["ready"] is True
    assert cloud_check["mode"] == "not_required_for_cloud_avatar"


def test_development_admission_fails_closed_for_an_explicit_unhealthy_tts_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    store = InMemoryStore()
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    with persistence_for(store).transaction("org_default") as transaction:
        connection = transaction.provider_connections.add(
            {
                "id": "provider_conn_realtime_tts",
                "organization_id": "org_default",
                "provider_id": "dashscope",
                "display_name": "百炼实时语音",
                "enabled": True,
                "connection_config": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        model = transaction.model_configurations.add(
            {
                "id": "model_cfg_realtime_tts",
                "organization_id": "org_default",
                "provider_connection_id": connection["id"],
                "provider_id": "dashscope",
                "model_type": "tts",
                "provider_model_id": "qwen3-tts-flash",
                "display_name": "面试官实时语音",
                "supported_capabilities": [cap.TTS_SYNTHESIZE],
                "settings": {},
                "default_parameters": {},
                "enabled": True,
                "status": "ready",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        route = transaction.model_routes.add(
            {
                "id": "route_realtime_tts",
                "organization_id": "org_default",
                "capability": cap.TTS_SYNTHESIZE,
                "purpose": "interview_agent_expression",
                "primary": {"model_configuration_id": model["id"], "timeout_s": 30},
                "fallbacks": [],
                "policy": {"readiness_ttl_seconds": 60},
                "enabled": True,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        unhealthy = AppointmentAdmission().plan_readiness(
            transaction, {"bank_slots": []}, now=now
        )
        stored = transaction.model_routes.get(route["id"])
        stored["last_health"] = {"status": "healthy", "checked_at": timestamp}
        transaction.model_routes.update(stored, expected_version=stored["version"])
        healthy = AppointmentAdmission().plan_readiness(
            transaction, {"bank_slots": []}, now=now
        )

    unhealthy_check = next(
        item for item in unhealthy["checks"] if item["name"] == "agent_expression_tts"
    )
    healthy_check = next(
        item for item in healthy["checks"] if item["name"] == "agent_expression_tts"
    )
    assert unhealthy_check["ready"] is False
    assert unhealthy_check["mode"] == "configured_route_unhealthy"
    assert healthy_check["ready"] is True
    assert healthy_check["mode"] == "configured_route"
