import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.main import create_app
from app.repositories.memory import InMemoryStore
from app.repositories.provider import reset_store_for_tests
from app.services.appointments import AppointmentService


NOW = datetime(2026, 9, 7, 7, 8, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class Probe:
    def __init__(self):
        self.calls = []
        self.healthy = False
        self.succeed = True
        self.after = None

    async def refresh_routes(self, route_ids, organization_id="org_default"):
        self.calls.append((route_ids, organization_id))
        await asyncio.sleep(0)
        self.healthy = self.succeed
        if self.after:
            self.after()


def fixture(status="scheduled", organization_id="org_default"):
    store, probe = InMemoryStore(), Probe()
    service = AppointmentService(store, clock=lambda: NOW, model_admin=probe)
    with service.persistence.transaction(organization_id) as tx:
        tx.interview_plans.add({"id": "plan_fixture", "status": "approved", "organization_id": organization_id})
        tx.interview_appointments.add({
            "id": "appointment_fixture", "plan_id": "plan_fixture", "status": status, "organization_id": organization_id,
            "scheduled_start_at": iso(NOW - timedelta(minutes=1)),
            "scheduled_end_at": iso(NOW + timedelta(hours=1)),
            "invitation_token_hash": service._token_hash("fixture-token"),
            "invitation_expires_at": iso(NOW + timedelta(hours=1)),
            "settings": {"record_audio": True}, "admission_policy": {},
            "device_readiness": {"ready": True, "expires_at": iso(NOW + timedelta(minutes=5))},
        })
        tx.candidate_intakes.add({
            "id": "intake_fixture", "appointment_id": "appointment_fixture", "organization_id": organization_id,
            "consent_evidence_status": "verified", "privacy_accepted": True,
            "media_consent_scopes": ["audio_recording"],
        })

    def readiness(*args, **kwargs):
        return {
            "can_invite": probe.healthy, "can_start": probe.healthy,
            "expires_at": iso(NOW + timedelta(seconds=60)),
            "checks": [
                {"name": name, "ready": probe.healthy,
                 "route_readiness": {"route_id": "route_fixture", "status": "healthy" if probe.healthy else "expired"}}
                for name in ("turn_understanding", "controlled_followup")
            ] + [{"name": "candidate_pools", "ready": True}],
        }
    service._readiness = readiness
    return service, probe


def update(service, **fields):
    with service.persistence.transaction("org_default") as tx:
        item = tx.interview_appointments.get("appointment_fixture")
        item.update(fields)
        tx.interview_appointments.update(item, expected_version=item["version"])


def test_expired_invite_refreshes_deduplicated_routes_then_uses_final_gate():
    service, probe = fixture()
    result = asyncio.run(service.invite_with_refresh("appointment_fixture", {"expires_at": iso(NOW + timedelta(hours=1))}))
    assert probe.calls == [(["route_fixture"], "org_default")]
    assert result["appointment"]["status"] == "invited"
    assert result["invitation_token"]


def test_explicit_refresh_is_non_inviting_and_does_not_rewrite_appointment():
    service, probe = fixture()
    before = service.get("appointment_fixture")
    result = asyncio.run(service.refresh_readiness("appointment_fixture"))
    assert result["can_invite"] is True
    assert service.get("appointment_fixture") == before
    assert len(probe.calls) == 1


def test_fresh_health_reuses_without_provider_work():
    service, probe = fixture()
    probe.healthy = True
    asyncio.run(service.refresh_readiness("appointment_fixture"))
    assert probe.calls == []


@pytest.mark.parametrize("case,code", [
    ("expired", "INVITATION_EXPIRY_INVALID"),
    ("missing", "INTERVIEW_APPOINTMENT_NOT_FOUND"),
    ("cancelled", "APPOINTMENT_NOT_INVITABLE"),
    ("registered", "APPOINTMENT_NOT_INVITABLE"),
    ("cross_tenant", "INTERVIEW_APPOINTMENT_NOT_FOUND"),
])
def test_invalid_invite_does_not_probe(case, code):
    service, probe = fixture(case if case in {"cancelled", "registered"} else "scheduled")
    expires = NOW - timedelta(seconds=1) if case == "expired" else NOW + timedelta(hours=1)
    with pytest.raises(ApiError) as error:
        asyncio.run(service.invite_with_refresh(
            "missing" if case == "missing" else "appointment_fixture",
            {"expires_at": iso(expires)}, "org_other" if case == "cross_tenant" else "org_default"))
    assert error.value.code == code
    assert probe.calls == []


def test_failed_probe_cannot_issue_invite_or_mutate_appointment():
    service, probe = fixture()
    probe.succeed = False
    before = service.get("appointment_fixture")
    with pytest.raises(ApiError, match="readiness") as error:
        asyncio.run(service.invite_with_refresh("appointment_fixture", {"expires_at": iso(NOW + timedelta(hours=1))}))
    assert error.value.code == "APPOINTMENT_NOT_READY"
    assert service.get("appointment_fixture") == before


def test_cancellation_while_probe_waits_is_rechecked_before_issuing():
    service, probe = fixture()
    probe.after = lambda: update(service, status="cancelled")
    with pytest.raises(ApiError) as error:
        asyncio.run(service.invite_with_refresh("appointment_fixture", {"expires_at": iso(NOW + timedelta(hours=1))}))
    assert error.value.code == "APPOINTMENT_NOT_INVITABLE"
    assert service.get("appointment_fixture")["status"] == "cancelled"


def test_expiry_while_probe_waits_is_rechecked_before_issuing():
    service, probe = fixture()
    probe.after = lambda: setattr(service, "clock", lambda: NOW + timedelta(hours=2))
    with pytest.raises(ApiError) as error:
        asyncio.run(service.invite_with_refresh("appointment_fixture", {"expires_at": iso(NOW + timedelta(hours=1))}))
    assert error.value.code == "INVITATION_EXPIRY_INVALID"
    assert service.get("appointment_fixture")["status"] == "scheduled"


def test_start_refresh_requires_registered_consented_in_window_ready_device():
    service, probe = fixture("registered")
    service.start = lambda token, organization_id: {"health_at_start": probe.healthy}
    result = asyncio.run(service.start_with_refresh("fixture-token"))
    assert result == {"health_at_start": True}
    assert len(probe.calls) == 1


@pytest.mark.parametrize("case,code", [
    ("bad_token", "INVITATION_INVALID"),
    ("invited", "INVITATION_UNAVAILABLE"),
    ("expired_token", "INVITATION_EXPIRED"),
    ("no_consent", "CONSENT_REQUIRED"),
    ("early", "APPOINTMENT_TOO_EARLY"),
    ("late", "APPOINTMENT_WINDOW_CLOSED"),
    ("no_device", "APPOINTMENT_DEVICE_NOT_READY"),
])
def test_invalid_candidate_start_never_triggers_paid_probe(case, code):
    service, probe = fixture("invited" if case == "invited" else "registered")
    if case == "expired_token":
        update(service, invitation_expires_at=iso(NOW - timedelta(seconds=1)))
    if case == "early":
        update(service, scheduled_start_at=iso(NOW + timedelta(minutes=5)))
    if case == "late":
        update(service, scheduled_end_at=iso(NOW - timedelta(minutes=1)))
    if case == "no_device":
        update(service, device_readiness=None)
    if case == "no_consent":
        with service.persistence.transaction("org_default") as tx:
            item = tx.candidate_intakes.get("intake_fixture")
            item["privacy_accepted"] = False
            tx.candidate_intakes.update(item, expected_version=item["version"])
    with pytest.raises(ApiError) as error:
        asyncio.run(service.start_with_refresh("invalid" if case == "bad_token" else "fixture-token"))
    assert error.value.code == code
    assert probe.calls == []


def test_readiness_refresh_keeps_original_device_expiry_and_invitation_state():
    service, probe = fixture("registered")
    before = service.get("appointment_fixture")
    result = asyncio.run(service.readiness_with_refresh("fixture-token"))
    assert result["can_start"] is True
    assert result["device_readiness"] == before["device_readiness"]
    assert service.get("appointment_fixture") == before


def test_pre_window_readiness_is_read_only_for_model_probes():
    service, probe = fixture("registered")
    update(service, scheduled_start_at=iso(NOW + timedelta(minutes=5)))
    asyncio.run(service.readiness_with_refresh("fixture-token"))
    assert probe.calls == []


def test_consumed_start_returns_existing_without_refresh():
    service, probe = fixture("consumed")
    service.start = lambda token, organization_id: {"id": "existing-session"}
    assert asyncio.run(service.start_with_refresh("fixture-token")) == {"id": "existing-session"}
    assert probe.calls == []


@pytest.mark.parametrize("during_probe", [False, True])
def test_archived_plan_cannot_be_invited_even_after_successful_probe(during_probe):
    service, probe = fixture()
    def archive():
        with service.persistence.transaction("org_default") as tx:
            plan = tx.interview_plans.get("plan_fixture")
            plan["status"] = "archived"
            tx.interview_plans.update(plan, expected_version=plan["version"])
    if during_probe:
        probe.after = archive
    else:
        archive()
    with pytest.raises(ApiError) as error:
        asyncio.run(service.invite_with_refresh("appointment_fixture", {"expires_at": iso(NOW + timedelta(hours=1))}))
    assert error.value.code == "INTERVIEW_PLAN_NOT_APPROVED"
    assert len(probe.calls) == int(during_probe)
    assert service.get("appointment_fixture")["status"] == "scheduled"


@pytest.mark.parametrize("case", ["invited", "early", "device_expired"])
def test_healthy_models_do_not_make_ineligible_candidate_start_ready(case):
    service, probe = fixture("invited" if case == "invited" else "registered")
    probe.healthy = True
    if case == "early":
        update(service, scheduled_start_at=iso(NOW + timedelta(minutes=5)))
    if case == "device_expired":
        update(service, device_readiness={"ready": True, "expires_at": iso(NOW - timedelta(seconds=1))})
    result = asyncio.run(service.readiness_with_refresh("fixture-token"))
    assert result["can_start"] is False
    assert probe.calls == []


@pytest.mark.parametrize("case", ["device_expired", "consent_revoked"])
def test_candidate_eligibility_is_recomputed_after_network_wait(case):
    service, probe = fixture("registered")
    def change():
        if case == "device_expired":
            update(service, device_readiness={"ready": True, "expires_at": iso(NOW - timedelta(seconds=1))})
        else:
            with service.persistence.transaction("org_default") as tx:
                intake = tx.candidate_intakes.get("intake_fixture")
                intake["privacy_accepted"] = False
                tx.candidate_intakes.update(intake, expected_version=intake["version"])
    probe.after = change
    result = asyncio.run(service.readiness_with_refresh("fixture-token"))
    assert result["can_start"] is False
    assert len(probe.calls) == 1


def test_start_consumed_by_another_request_during_probe_replays_existing():
    service, probe = fixture("registered")
    probe.after = lambda: update(service, status="consumed")
    service.start = lambda token, organization_id: {"id": "existing-session"}
    assert asyncio.run(service.start_with_refresh("fixture-token")) == {"id": "existing-session"}
    assert len(probe.calls) == 1


def test_http_commands_await_async_refresh_and_bind_deployment_tenant(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_refresh")
    reset_store_for_tests()
    seen = []
    async def refresh(self, appointment_id, organization_id="org_default"):
        seen.append((appointment_id, organization_id))
        return {"can_invite": True}
    monkeypatch.setattr(AppointmentService, "refresh_readiness", refresh)
    api = TestClient(create_app())
    response = api.post("/api/v1/interview-appointments/example/readiness/refresh")
    assert response.status_code == 200
    assert seen == [("example", "org_refresh")]
    api.get("/api/v1/interview-appointments/example")
    assert len(seen) == 1


def test_refresh_endpoint_has_enterprise_rbac_and_no_public_alias(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    monkeypatch.setenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", "lookup-secret-for-readiness-refresh-tests")
    monkeypatch.setenv("INTERVIEWER_API_TOKENS_JSON", json.dumps({
        key: {"actor_id": key, "organization_id": org, "roles": [role]}
        for key, org, role in [("review", "org_default", "reviewer"), ("interview", "org_default", "interviewer"),
                               ("admin", "org_default", "admin"), ("other", "org_other", "admin")]
    }))
    reset_store_for_tests()
    api = TestClient(create_app())
    path = "/api/v1/interview-appointments/missing/readiness/refresh"
    assert api.post(path).status_code == 401
    for key in ("review", "other"):
        assert api.post(path, headers={"Authorization": "Bearer " + key}).status_code == 403
    for key in ("admin", "interview"):
        assert api.post(path, headers={"Authorization": "Bearer " + key}).status_code == 404
