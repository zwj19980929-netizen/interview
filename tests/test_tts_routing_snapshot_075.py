"""Same-voice cache identity is invalidated by current configuration facts."""
import json
import traceback
import warnings
from copy import deepcopy

import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import TTSSynthesizeRequest
from test_tts_streaming import store_and_route


def setup():
    store, route = store_and_route()
    route["purpose"] = "interview_agent_expression"
    route["fallbacks"] = [{"model_configuration_id": "other_voice"}]
    store.model_routes[route["id"]] = route
    return store, ModelGateway(store), TTSSynthesizeRequest(text="synthetic fixed phrase", purpose=route["purpose"])


@pytest.mark.parametrize("change", ["voice", "defaults", "endpoint", "route", "credentials", "credential_version", "model", "connection"])
def test_current_voice_configuration_and_credentials_invalidate_identity(change):
    store, gateway, request = setup()
    before = gateway.tts_routing_snapshot(request)
    if change == "voice":
        store.model_configurations["config_test"]["settings"] = {"default_voice": "new_voice"}
    elif change == "defaults":
        store.model_configurations["config_test"]["default_parameters"] = {"speaking_rate": 1.1}
    elif change == "endpoint":
        store.provider_connections["connection_test"]["connection_config"]["base_url"] = "https://synthetic.invalid/v1"
    elif change == "route":
        store.model_routes["route_test"]["version"] = 2
    elif change == "credentials":
        store.save_provider_secret("connection_test", {"api_key": "rotated-synthetic-secret"})
    elif change == "credential_version":
        store.provider_connections["connection_test"]["version"] = 2
    elif change == "model":
        store.model_configurations["config_test"]["provider_model_id"] = "other-real-model"
    else:
        store.provider_connections["new_connection"] = {**deepcopy(store.provider_connections["connection_test"]), "id": "new_connection"}
        store.save_provider_secret("new_connection", {"api_key": "synthetic-secret"})
        store.model_configurations["config_test"]["provider_connection_id"] = "new_connection"
    after = gateway.tts_routing_snapshot(request)
    assert before["identity"] != after["identity"]
    assert after["route"]["fallbacks"] == [] and after["route"]["policy"]["fallback_on"] == []
    assert len(store.model_routes["route_test"]["fallbacks"]) == 1
    assert "synthetic-secret" not in json.dumps(after["identity"])
    assert "connection_config" not in after and "credentials" not in after
    if change == "defaults":
        assert after["request"].speaking_rate == 1.1


@pytest.mark.parametrize("change", ["model", "connection", "route"])
def test_cached_identity_cannot_bypass_configuration_disable(change):
    store, gateway, request = setup()
    gateway.tts_routing_snapshot(request)
    collection, key = {"model": (store.model_configurations, "config_test"),
                       "connection": (store.provider_connections, "connection_test"),
                       "route": (store.model_routes, "route_test")}[change]
    collection[key]["enabled"] = False
    with pytest.raises(ProviderError):
        gateway.tts_routing_snapshot(request)
    assert not store.model_invocations


def test_explicit_voice_parameters_and_org_are_part_of_request_not_provider_defaults():
    store, gateway, request = setup()
    store.model_configurations["config_test"]["default_parameters"] = {"speaking_rate": 1.1, "voice_profile_id": "voice_changed"}
    explicit = request.model_copy(update={"voice_profile_id": "voice_original", "speaking_rate": 1.0})
    snapshot = gateway.tts_routing_snapshot(explicit)
    assert snapshot["request"].voice_profile_id == "voice_original"
    assert snapshot["request"].speaking_rate == 1.0
    with pytest.raises(ProviderError):
        gateway.tts_routing_snapshot(request.model_copy(update={"organization_id": "org_other"}))


def test_model_defaults_cannot_change_tenant_purpose_text_or_metadata_and_effective_values_are_frozen():
    store, gateway, request = setup()
    store.model_configurations["config_test"]["default_parameters"] = {
        "organization_id": "org_other", "purpose": "other_purpose", "text": "unapproved replacement",
        "metadata": {"private": "must-not-be-added"}, "voice_profile_id": "voice_current", "speaking_rate": 1.2,
    }
    snapshot = gateway.tts_routing_snapshot(request)
    effective = snapshot["request"]
    assert (effective.organization_id, effective.purpose, effective.text, effective.metadata) == (
        request.organization_id, request.purpose, request.text, request.metadata)
    assert (effective.voice_profile_id, effective.speaking_rate) == ("voice_current", 1.2)
    assert gateway._apply_model_defaults(effective, {"organization_id": "org_other", "voice_profile_id": "late_voice"}) == effective


@pytest.mark.parametrize("defaults", [{"speaking_rate": 5}, {"speaking_rate": "private-invalid-value"}, {"voice_profile_id": None}])
def test_invalid_speech_defaults_fail_with_safe_structured_error(defaults):
    store, gateway, request = setup()
    store.model_configurations["config_test"]["default_parameters"] = defaults
    with warnings.catch_warnings(record=True) as recorded:
        with pytest.raises(ProviderError) as error:
            gateway.tts_routing_snapshot(request)
    assert error.value.code == "provider_bad_request" and not error.value.retryable
    assert "private-invalid-value" not in str(error.value) + json.dumps(error.value.details)
    assert "private-invalid-value" not in "".join(traceback.format_exception(type(error.value), error.value, error.value.__traceback__))
    assert not recorded


@pytest.mark.parametrize("change", ["disabled", "missing"])
def test_disabled_or_missing_exact_route_does_not_enable_a_different_default_voice(change):
    store, gateway, request = setup()
    store.model_routes["default_real"] = {**deepcopy(store.model_routes["route_test"]), "id": "default_real", "purpose": "default"}
    if change == "disabled":
        store.model_routes["route_test"]["enabled"] = False
    else:
        del store.model_routes["route_test"]
    with pytest.raises(ProviderError):
        gateway.tts_routing_snapshot(request)
    assert not store.model_invocations


@pytest.mark.parametrize("change", ["model_org", "connection_org", "provider", "model_type", "invalid_credentials", "model_unready", "model_capability"])
def test_invalid_referenced_configuration_cannot_authorize_cached_pcm(change):
    store, gateway, request = setup()
    gateway.tts_routing_snapshot(request)
    model = store.model_configurations["config_test"]
    connection = store.provider_connections["connection_test"]
    if change == "model_org": model["organization_id"] = "org_other"
    elif change == "connection_org": connection["organization_id"] = "org_other"
    elif change == "provider": connection["provider_id"] = "different_provider"
    elif change == "model_type": model["model_type"] = "llm"
    elif change == "invalid_credentials": connection["credential_status"] = "invalid"
    elif change == "model_unready": model["status"] = "failed"
    else: model["supported_capabilities"] = []
    with pytest.raises(ProviderError):
        gateway.tts_routing_snapshot(request)
    assert not store.model_invocations


def test_snapshot_is_detached_primary_only_and_does_not_expose_secrets():
    store, gateway, request = setup()
    store.provider_connections["connection_test"]["connection_config"]["private_setting"] = "synthetic-private-setting"
    before = deepcopy(store.model_routes["route_test"])
    snapshot = gateway.tts_routing_snapshot(request)
    public = {**snapshot, "request": snapshot["request"].model_dump()}
    assert all(value not in json.dumps(public) for value in ("synthetic-secret", "synthetic-private-setting"))
    assert snapshot["route"]["primary"] == before["primary"]
    assert snapshot["route"]["fallbacks"] == [] and snapshot["route"]["policy"]["fallback_on"] == []
    snapshot["route"]["primary"]["model_configuration_id"] = "other_config"
    assert store.model_routes["route_test"] == before


def test_distinct_organizations_with_equivalent_provider_settings_have_distinct_identity():
    store, gateway, request = setup()
    first = gateway.tts_routing_snapshot(request)
    store.provider_connections["other_connection"] = {
        **deepcopy(store.provider_connections["connection_test"]), "id": "other_connection", "organization_id": "org_other"}
    store.model_configurations["other_model"] = {**deepcopy(store.model_configurations["config_test"]),
        "id": "other_model", "organization_id": "org_other", "provider_connection_id": "other_connection"}
    store.save_provider_secret("other_connection", {"api_key": "synthetic-secret"})
    store.model_routes["other_route"] = {**deepcopy(store.model_routes["route_test"]),
        "id": "other_route", "organization_id": "org_other", "primary": {"model_configuration_id": "other_model", "timeout_s": 1}}
    second = gateway.tts_routing_snapshot(request.model_copy(update={"organization_id": "org_other"}))
    assert first["identity"] != second["identity"]
    assert second["identity"]["organization_id"] == second["request"].organization_id == "org_other"
