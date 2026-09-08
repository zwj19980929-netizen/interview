"""Synthetic-only route health projection, lease and cancellation contracts."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.readiness import _route_ready
from app.domain.appointment_admission import AppointmentAdmission
from app.domain.model_route_readiness import configuration_fingerprint, project_route_readiness, timestamp
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.persistence.errors import ConcurrencyConflict
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.model_admin import ModelAdminService
from app.services.model_route_readiness import ModelRouteReadinessRefresher, route_facts, route_readiness


NOW = datetime(2026, 9, 7, 7, 8, tzinfo=timezone.utc)


def fixture(count=1, organization_id="org_default", capability=cap.LLM_CHAT_JSON,
            purpose="interview_turn_understanding", store=None):
    store = store or InMemoryStore()
    service = ModelAdminService(store)
    with service.persistence.transaction(organization_id) as tx:
        connection = tx.provider_connections.add({
            "id": "connection_fixture", "organization_id": organization_id, "provider_id": "dashscope",
            "configuration_revision": 1, "enabled": True,
            "credential_ref": "secret-reference-not-a-key", "connection_config": {},
        })
        model = tx.model_configurations.add({
            "id": "model_fixture", "organization_id": organization_id, "provider_id": "dashscope",
            "provider_connection_id": connection["id"], "configuration_revision": 1,
            "model_type": "llm", "provider_model_id": "fixture-model", "enabled": True,
            "status": "ready", "supported_capabilities": [capability], "settings": {}, "default_parameters": {},
        })
        for index in range(count):
            tx.model_routes.add({
                "id": f"route_{index}", "organization_id": organization_id, "capability": capability,
                "purpose": purpose if index == 0 else f"fixture_{index}", "enabled": True,
                "primary": {"model_configuration_id": model["id"]}, "fallbacks": [],
                "policy": {"readiness_ttl_seconds": 60},
            })
    return service, store


def update(service, collection="model_routes", item_id="route_0", organization_id="org_default", **fields):
    with service.persistence.transaction(organization_id) as tx:
        repository = getattr(tx, collection)
        item = repository.get(item_id)
        item.update(fields)
        repository.update(item, expected_version=item["version"])


def state(service, organization_id="org_default", now=None):
    with service.persistence.transaction(organization_id) as tx:
        return route_readiness(tx, tx.model_routes.get("route_0"), now)


def stored(service):
    with service.persistence.transaction("org_default") as tx:
        return tx.model_routes.get("route_0")


def refresher(service, probe, **kwargs):
    result = ModelRouteReadinessRefresher(service.persistence, probe, **kwargs)
    service._route_readiness_refresher = result
    return result


@pytest.mark.parametrize("health,expected", [
    (None, "untested"),
    ({"status": "healthy", "checked_at": timestamp(NOW - timedelta(seconds=59))}, "healthy"),
    ({"status": "healthy", "checked_at": timestamp(NOW - timedelta(seconds=61))}, "expired"),
    ({"status": "healthy", "checked_at": "2026-09-07T07:07:30"}, "untested"),
    ({"status": "healthy", "checked_at": "not-a-time"}, "untested"),
    ({"status": "healthy", "checked_at": timestamp(NOW + timedelta(seconds=1))}, "expired"),
    ({"status": "failed", "checked_at": timestamp(NOW), "reason_code": "provider_output_truncated"}, "failed"),
])
def test_projection_distinguishes_health_states_and_legacy_ttl(health, expected):
    service, _ = fixture()
    if health:
        update(service, last_health=health)
    result = state(service, now=NOW)
    assert result["status"] == expected
    assert result["route_id"] == "route_0"
    assert result["ready"] is (expected == "healthy")


def test_projection_failure_cooldown_is_not_expiry_and_sanitizes_legacy_error():
    service, _ = fixture()
    update(service, last_health={"status": "failed", "checked_at": timestamp(NOW),
                                "reason_code": "https://secret-key:password@bad.example", "error": "secret"})
    result = state(service, now=NOW + timedelta(seconds=29))
    assert result["status"] == "failed" and not result["can_refresh"]
    assert result["reason_code"] == "provider_probe_failed"
    assert result["retry_at"] == timestamp(NOW + timedelta(seconds=30))
    assert state(service, now=NOW + timedelta(seconds=30))["can_refresh"]
    assert "secret" not in json.dumps(result)


def test_configuration_fingerprint_ignores_health_version_but_binds_semantics():
    service, _ = fixture()
    with service.persistence.transaction("org_default") as tx:
        route = tx.model_routes.get("route_0")
        model, connection, _, original = route_facts(tx, route)
    route.update(version=999, last_health={"status": "healthy"}, updated_at="tomorrow")
    model.update(version=88, last_validation={"status": "ready"})
    connection.update(version=77, credential_status="valid")
    assert configuration_fingerprint(route, [(model, connection)]) == original
    for target, field, value in ((route, "policy", {"readiness_ttl_seconds": 3600}),
                                 (model, "configuration_revision", 2),
                                 (connection, "configuration_revision", 2)):
        before = deepcopy(target)
        target[field] = value
        assert configuration_fingerprint(route, [(model, connection)]) != original
        target.clear()
        target.update(before)


def test_bound_health_expires_when_configuration_changes():
    service, _ = fixture()
    with service.persistence.transaction("org_default") as tx:
        fingerprint = route_facts(tx, tx.model_routes.get("route_0"))[3]
    update(service, last_health={"status": "healthy", "checked_at": timestamp(NOW), "configuration_fingerprint": fingerprint})
    update(service, "provider_connections", "connection_fixture", configuration_revision=2)
    result = state(service, now=NOW)
    assert result["status"] == "expired"
    assert result["reason_code"] == "ROUTE_CONFIGURATION_CHANGED"


@pytest.mark.parametrize("collection,item_id,fields,reason", [
    ("model_routes", "route_0", {"enabled": False}, "ROUTE_DISABLED"),
    ("model_configurations", "model_fixture", {"enabled": False}, "MODEL_CONFIGURATION_DISABLED"),
    ("model_configurations", "model_fixture", {"status": "untested"}, "MODEL_NOT_READY"),
    ("provider_connections", "connection_fixture", {"enabled": False}, "PROVIDER_CONNECTION_DISABLED"),
])
def test_invalid_configuration_never_probes(collection, item_id, fields, reason):
    service, _ = fixture()
    update(service, collection, item_id, **fields)
    async def probe(*args):
        pytest.fail("Invalid configuration must never invoke the provider")
    refresher(service, probe)
    result = asyncio.run(service.refresh_routes(["route_0"]))["items"][0]["readiness"]
    assert result["status"] == "configuration_invalid"
    assert result["reason_code"] == reason


def test_fresh_evidence_skips_and_get_list_is_read_only_and_redacted():
    service, _ = fixture()
    update(service, last_health={"status": "healthy", "checked_at": timestamp(datetime.now(timezone.utc)),
                                "error": "raw-sensitive-message"})
    async def probe(*args):
        pytest.fail("Fresh route must not create paid work")
    refresher(service, probe)
    before = stored(service)
    result = asyncio.run(service.refresh_routes(["route_0"]))
    assert result["items"][0]["readiness"]["ready"]
    assert service.list_routes()[0]["readiness"]["status"] == "healthy"
    assert "raw-sensitive" not in json.dumps(service.list_routes())
    assert stored(service) == before


def test_success_uses_database_time_binds_configuration_and_returns_raw_manual_response():
    service, _ = fixture()
    async def probe(route, organization_id):
        assert organization_id == "org_default"
        assert route["health_probe"]["token"]
        return {"output": {"ok": True}, "meta": {"fixture": True}}
    refresher(service, probe)
    result = asyncio.run(service.test_route("route_0"))
    assert result == {"output": {"ok": True}, "meta": {"fixture": True}}
    item = stored(service)
    assert item["last_health"]["status"] == "healthy"
    assert item["last_health"]["configuration_fingerprint"]
    assert "health_probe" not in item
    assert state(service)["ready"]


def test_failures_have_safe_codes_cooldown_and_explicit_manual_retry():
    service, _ = fixture()
    calls = []
    async def probe(*args):
        calls.append(1)
        raise ProviderError("provider_auth_failed", "api_key=DO_NOT_STORE", retryable=False)
    refresher(service, probe)
    asyncio.run(service.refresh_routes(["route_0"]))
    asyncio.run(service.refresh_routes(["route_0"]))
    assert calls == [1]
    with pytest.raises(ProviderError) as failure:
        asyncio.run(service.test_route("route_0"))
    assert failure.value.code == "provider_auth_failed"
    assert calls == [1, 1]
    assert "DO_NOT_STORE" not in json.dumps(stored(service))
    assert "DO_NOT_STORE" not in failure.value.message


def test_separate_instances_join_same_durable_probe_and_wait_for_result():
    service, store = fixture()
    second = ModelAdminService(store, persistence=service.persistence)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def probe(*args):
            calls.append(1)
            entered.set()
            await release.wait()
            return {"ok": True}
        refresher(service, probe)
        refresher(second, probe)
        first = asyncio.create_task(service.refresh_routes(["route_0", "route_0"]))
        await entered.wait()
        waiting = asyncio.create_task(second.refresh_routes(["route_0"]))
        await asyncio.sleep(.01)
        assert state(service)["status"] == "checking"
        assert not waiting.done()
        release.set()
        results = await asyncio.gather(first, waiting)
        assert len(calls) == 1
        assert all(item["items"][0]["readiness"]["ready"] for item in results)
    asyncio.run(scenario())


def test_independent_sqlite_connections_join_durable_lease_without_network_transaction(tmp_path):
    database = tmp_path / "route-readiness-fixture.sqlite3"
    service, _ = fixture(store=SQLiteStore(str(database)))
    second = ModelAdminService(SQLiteStore(str(database)))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def probe(*args):
            calls.append(1)
            # A second connection can commit while the first provider is held:
            # no transaction lock is retained around network work.
            update(second, "model_configurations", "model_fixture", last_validation={"status": "ready"})
            entered.set()
            await release.wait()
            return {"ok": True}
        refresher(service, probe)
        refresher(second, probe)
        first = asyncio.create_task(service.refresh_routes(["route_0"]))
        await entered.wait()
        waiting = asyncio.create_task(second.refresh_routes(["route_0"]))
        await asyncio.sleep(.01)
        assert state(second)["status"] == "checking"
        release.set()
        results = await asyncio.gather(first, waiting)
        assert len(calls) == 1
        assert all(item["items"][0]["readiness"]["ready"] for item in results)
    asyncio.run(scenario())


def test_fallback_configuration_is_also_bound_to_probe_snapshot():
    service, _ = fixture()
    with service.persistence.transaction("org_default") as tx:
        original = tx.model_configurations.get("model_fixture")
        tx.model_configurations.add({**original, "id": "model_fallback"})
    update(service, fallbacks=[{"model_configuration_id": "model_fallback"}])
    async def probe(*args):
        update(service, "model_configurations", "model_fallback", configuration_revision=2)
        return {"ok": True}
    refresher(service, probe)
    result = asyncio.run(service.refresh_routes(["route_0"]))
    assert not result["items"][0]["readiness"]["ready"]
    assert "last_health" not in stored(service)


def test_repeated_health_cas_conflict_fails_closed_without_failing_batch():
    service, _ = fixture()
    async def probe(*args):
        return {"ok": True}
    refresh = refresher(service, probe)
    calls = []
    def conflict(*args):
        calls.append(1)
        raise ConcurrencyConflict("synthetic competing writer")
    refresh._complete_once = conflict
    result = asyncio.run(service.refresh_routes(["route_0"]))
    assert len(calls) == 3
    assert result["items"][0]["readiness"]["status"] == "checking"
    assert "last_health" not in stored(service)


@pytest.mark.parametrize("mutation", ["route_policy", "connection_revision", "model_revision", "disabled", "deleted", "superseded"])
def test_late_probe_never_writes_health_for_changed_or_removed_configuration(mutation):
    service, _ = fixture()
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(*args):
            entered.set()
            await release.wait()
            return {"ok": True}
        refresher(service, probe)
        task = asyncio.create_task(service.refresh_routes(["route_0"]))
        await entered.wait()
        if mutation == "route_policy":
            update(service, policy={"readiness_ttl_seconds": 120})
        elif mutation == "connection_revision":
            update(service, "provider_connections", "connection_fixture", configuration_revision=2)
        elif mutation == "model_revision":
            update(service, "model_configurations", "model_fixture", configuration_revision=2)
        elif mutation == "disabled":
            update(service, enabled=False)
        elif mutation == "superseded":
            current = stored(service)["health_probe"]
            update(service, health_probe={**current, "token": "newer-probe-token"})
        else:
            with service.persistence.transaction("org_default") as tx:
                tx.model_routes.delete("route_0", expected_version=tx.model_routes.get("route_0")["version"])
        release.set()
        result = await task
        assert not result["items"][0]["readiness"]["ready"]
        item = stored(service)
        assert not item or (item.get("last_health") or {}).get("status") != "healthy"
        if mutation == "superseded":
            assert item["health_probe"]["token"] == "newer-probe-token"
    asyncio.run(scenario())


def test_generic_version_change_during_probe_is_not_configuration_change():
    service, _ = fixture()
    async def probe(*args):
        update(service, updated_at="metadata-only")
        update(service, "model_configurations", "model_fixture", last_validation={"status": "ready"})
        return {"ok": True}
    refresh = refresher(service, probe)
    original = refresh._complete_once
    count = []
    def compete(*args):
        count.append(1)
        if len(count) == 1:
            raise ConcurrencyConflict("model_routes", "route_0")
        return original(*args)
    refresh._complete_once = compete
    assert asyncio.run(service.refresh_routes(["route_0"]))["items"][0]["readiness"]["ready"]
    assert len(count) == 2


def test_expired_lease_recovered_but_live_foreign_lease_wait_is_bounded():
    service, _ = fixture()
    async def probe(*args):
        return {"ok": True}
    refresh = refresher(service, probe, batch_timeout=.04)
    with service.persistence.transaction("org_default") as tx:
        fingerprint = route_facts(tx, tx.model_routes.get("route_0"))[3]
    update(service, health_probe={"token": "orphan", "configuration_fingerprint": fingerprint,
                                   "expires_at": timestamp(datetime.now(timezone.utc) + timedelta(seconds=60))})
    result = asyncio.run(service.refresh_routes(["route_0"]))
    assert result["items"][0]["readiness"]["status"] == "checking"
    update(service, health_probe={"token": "expired", "configuration_fingerprint": fingerprint,
                                   "expires_at": timestamp(NOW - timedelta(days=30))})
    assert asyncio.run(service.refresh_routes(["route_0"]))["items"][0]["readiness"]["ready"]


def test_concurrency_and_batch_budget_never_start_all_routes_at_once():
    service, _ = fixture(count=8)
    async def scenario():
        active, maximum, cancelled = 0, 0, 0
        async def probe(*args):
            nonlocal active, maximum, cancelled
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1
        refresher(service, probe, probe_timeout=.04, batch_timeout=.07)
        started = asyncio.get_running_loop().time()
        result = await service.refresh_routes([f"route_{i}" for i in range(8)])
        assert asyncio.get_running_loop().time() - started < .3
        assert maximum == 3 and active == 0 and cancelled >= 3
        assert not any(item["readiness"]["ready"] for item in result["items"])
    asyncio.run(scenario())


def test_cancellation_and_late_provider_success_never_write_healthy():
    service, _ = fixture()
    async def scenario():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def probe(*args):
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return {"late": "success"}
        refresher(service, probe)
        task = asyncio.create_task(service.refresh_routes(["route_0"]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert state(service)["status"] == "failed"
        assert state(service)["reason_code"] == "provider_probe_cancelled"
        release.set()
        await asyncio.sleep(.01)
        assert not state(service)["ready"]
    asyncio.run(scenario())


def test_foreign_tenant_route_is_indistinguishable_from_missing():
    service, _ = fixture(organization_id="org_foreign")
    async def probe(*args):
        pytest.fail("Foreign route must never be probed")
    refresher(service, probe)
    result = asyncio.run(service.refresh_routes(["route_0"]))
    assert result["items"][0]["readiness"]["route_id"] is None
    assert result["items"][0]["readiness"]["reason_code"] == "ROUTE_MISSING"
    with pytest.raises(ApiError) as failure:
        asyncio.run(service.test_route("route_0"))
    assert failure.value.status_code == 404


def test_admission_and_deployment_reuse_same_projection_with_explicit_disabled_route(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    service, _ = fixture()
    update(service, enabled=False)
    with service.persistence.transaction("org_default") as tx:
        admission = AppointmentAdmission().plan_readiness(tx, {"bank_slots": []}, now=NOW)
        assert not _route_ready(tx, cap.LLM_CHAT_JSON, "interview_turn_understanding")
    check = next(item for item in admission["checks"] if item["name"] == "turn_understanding")
    assert check["ready"] is False
    assert check["mode"] == "configured_route_configuration_invalid"
    assert check["route_readiness"]["route_id"] == "route_0"
    absent = next(item for item in admission["checks"] if item["name"] == "warmup_stt_streaming")
    assert absent["route_readiness"]["route_id"] is None


def test_stream_probe_cleanup_is_bounded_and_never_marks_healthy():
    service, _ = fixture(capability=cap.STT_STREAMING, purpose="candidate_answer_transcription")
    async def scenario():
        aborted = asyncio.Event()
        class Stream:
            ready_events = []
            stream_id = "fixture-stream"
            async def abort(self):
                try:
                    await asyncio.sleep(20)
                finally:
                    aborted.set()
        async def open_stream(*args, **kwargs):
            return Stream()
        service.gateway = SimpleNamespace(open_stream=open_stream)
        service._route_readiness_refresher.probe_timeout = 3
        started = asyncio.get_running_loop().time()
        result = await service.refresh_routes(["route_0"])
        assert asyncio.get_running_loop().time() - started < 2.5
        await asyncio.sleep(0)
        assert aborted.is_set()
        assert result["items"][0]["readiness"]["status"] == "failed"
        assert result["items"][0]["readiness"]["reason_code"] == "provider_probe_cleanup_failed"
    asyncio.run(scenario())


def test_stream_probe_accepts_successful_six_hundred_ms_cleanup():
    service, _ = fixture(capability=cap.STT_STREAMING, purpose="candidate_answer_transcription")
    async def scenario():
        closed = asyncio.Event()
        class Stream:
            ready_events = []
            stream_id = "fixture-stream"
            async def abort(self):
                await asyncio.sleep(.6)
                closed.set()
        async def open_stream(*args, **kwargs):
            return Stream()
        service.gateway = SimpleNamespace(open_stream=open_stream)
        result = await service.refresh_routes(["route_0"])
        assert closed.is_set()
        assert result["items"][0]["readiness"]["ready"]
    asyncio.run(scenario())


def test_stream_probe_cleanup_exception_has_its_own_safe_error_category():
    service, _ = fixture(capability=cap.STT_STREAMING, purpose="candidate_answer_transcription")
    class Stream:
        ready_events = []
        stream_id = "fixture-stream"
        async def abort(self):
            raise RuntimeError("socket cleanup failed with api_key=DO_NOT_STORE")
    async def open_stream(*args, **kwargs):
        return Stream()
    service.gateway = SimpleNamespace(open_stream=open_stream)
    result = asyncio.run(service.refresh_routes(["route_0"]))
    assert result["items"][0]["readiness"]["reason_code"] == "provider_probe_cleanup_failed"
    assert not result["items"][0]["readiness"]["ready"]
    assert "DO_NOT_STORE" not in json.dumps(stored(service))


def test_stream_cleanup_does_not_extend_overall_probe_deadline():
    service, _ = fixture(capability=cap.STT_STREAMING, purpose="candidate_answer_transcription")
    async def scenario():
        cancelled = asyncio.Event()
        class Stream:
            ready_events = []
            stream_id = "fixture-stream"
            async def abort(self):
                try:
                    await asyncio.sleep(.6)
                finally:
                    cancelled.set()
        async def open_stream(*args, **kwargs):
            return Stream()
        service.gateway = SimpleNamespace(open_stream=open_stream)
        service._route_readiness_refresher.probe_timeout = .05
        started = asyncio.get_running_loop().time()
        result = await service.refresh_routes(["route_0"])
        assert asyncio.get_running_loop().time() - started < .25
        assert cancelled.is_set()
        assert not result["items"][0]["readiness"]["ready"]
        assert result["items"][0]["readiness"]["reason_code"] == "provider_timeout"
    asyncio.run(scenario())


def put_fixture_model_in_fallback(service):
    with service.persistence.transaction("org_default") as tx:
        connection = tx.provider_connections.get("connection_fixture")
        tx.provider_connections.add({**connection, "id": "connection_other"})
        model = tx.model_configurations.get("model_fixture")
        tx.model_configurations.add({**model, "id": "model_other", "provider_connection_id": "connection_other"})
    update(service, primary={"model_configuration_id": "model_other"},
           fallbacks=[{"model_configuration_id": "model_fixture"}])


@pytest.mark.parametrize("payload", [
    {"credentials": {"api_key": "synthetic-key-never-sent"}},
    {"connection_config": {"base_url": "https://fixture.invalid/compatible-mode/v1"}},
])
@pytest.mark.parametrize("fallback_only", [False, True])
def test_connection_patch_binds_legacy_health_before_credentials_or_config_change(payload, fallback_only):
    service, _ = fixture()
    if fallback_only:
        put_fixture_model_in_fallback(service)
    checked = timestamp(datetime.now(timezone.utc))
    update(service, last_health={"status": "healthy", "checked_at": checked})
    before = state(service)
    assert before["ready"]
    with service.persistence.transaction("org_default") as tx:
        fingerprint = route_facts(tx, tx.model_routes.get("route_0"))[3]
        version = tx.provider_connections.get("connection_fixture")["version"]
    service.patch_provider_connection("connection_fixture", {"expected_version": version, **payload})
    health = stored(service)["last_health"]
    assert health["configuration_fingerprint"] == fingerprint
    assert health["checked_at"] == checked
    assert state(service)["expires_at"] == before["expires_at"]
    assert state(service)["status"] == "expired"
    assert state(service)["reason_code"] == "ROUTE_CONFIGURATION_CHANGED"
    # Credential/model validation alone cannot validate the old route evidence.
    service._record_model_health("model_fixture", "org_default", "ready", None)
    assert not state(service)["ready"]


@pytest.mark.parametrize("fallback_only", [False, True])
def test_model_patch_then_model_ready_still_requires_fresh_route_probe(fallback_only):
    service, _ = fixture()
    update(service, "model_configurations", "model_fixture", provider_model_id="qwen-plus")
    if fallback_only:
        put_fixture_model_in_fallback(service)
    checked = timestamp(datetime.now(timezone.utc))
    update(service, last_health={"status": "healthy", "checked_at": checked})
    with service.persistence.transaction("org_default") as tx:
        fingerprint = route_facts(tx, tx.model_routes.get("route_0"))[3]
        version = tx.model_configurations.get("model_fixture")["version"]
    service.patch_model_configuration("model_fixture", {
        "expected_version": version, "default_parameters": {"temperature": .2},
    })
    assert stored(service)["last_health"]["configuration_fingerprint"] == fingerprint
    assert stored(service)["last_health"]["checked_at"] == checked
    service._record_model_health("model_fixture", "org_default", "ready", None)
    assert state(service)["status"] == "expired"
    async def probe(*args):
        return {"ok": True}
    refresher(service, probe)
    assert asyncio.run(service.refresh_routes(["route_0"]))["items"][0]["readiness"]["ready"]


def test_pure_health_version_updates_keep_unmodified_legacy_route_evidence():
    service, _ = fixture()
    update(service, last_health={"status": "healthy", "checked_at": timestamp(datetime.now(timezone.utc))})
    before = stored(service)
    service._record_model_health("model_fixture", "org_default", "ready", None)
    service._record_connection_health("connection_fixture", "org_default", "valid", None)
    assert stored(service) == before
    assert state(service)["ready"]


def test_config_patch_does_not_rewrite_already_bound_health_or_unrelated_routes():
    service, _ = fixture(count=2)
    put_fixture_model_in_fallback(service)
    update(service, item_id="route_1", primary={"model_configuration_id": "model_other"},
           last_health={"status": "healthy", "checked_at": timestamp(datetime.now(timezone.utc))})
    with service.persistence.transaction("org_default") as tx:
        fingerprint = route_facts(tx, tx.model_routes.get("route_0"))[3]
        other_before = tx.model_routes.get("route_1")
        version = tx.provider_connections.get("connection_fixture")["version"]
    update(service, last_health={"status": "healthy", "checked_at": timestamp(datetime.now(timezone.utc)),
                                "configuration_fingerprint": fingerprint})
    before = stored(service)
    service.patch_provider_connection("connection_fixture", {
        "expected_version": version, "credentials": {"api_key": "synthetic-key-never-sent"},
    })
    assert stored(service) == before
    with service.persistence.transaction("org_default") as tx:
        assert tx.model_routes.get("route_1") == other_before
    assert state(service)["status"] == "expired"
