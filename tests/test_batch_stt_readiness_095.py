"""Real recovery readiness uses known speech, never candidate audio or silent fixtures."""

import hashlib
import json
import wave
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
import pytest

from app.domain.appointment_admission import AppointmentAdmission
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.providers.dashscope.provider import DashScopeProvider
from app.repositories.memory import InMemoryStore
from app.services.model_admin import ModelAdminService


PHRASE = "连接测试，语音识别正常。"


def service_with_batch_response(text, status=200):
    store = InMemoryStore()
    service = ModelAdminService(store)
    connection = service.create_provider_connection({
        "provider_id": "dashscope", "display_name": "synthetic test",
        "credentials": {"api_key": "synthetic-test-key"}, "connection_config": {},
    })
    model = service.create_model_configuration({
        "provider_connection_id": connection["id"], "model_type": "stt",
        "provider_model_id": "qwen3-asr-flash", "display_name": "synthetic batch",
    })
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, json={
            "id": "synthetic-asr-request", "choices": [{"message": {"content": text}}],
        })
    transport = httpx.MockTransport(handler)
    adapter = DashScopeProvider(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))
    service.gateway = ModelGateway(store, provider_clients={"dashscope": adapter})
    return service, store, model, calls


def test_batch_probe_is_bounded_non_silent_known_speech():
    service = ModelAdminService(InMemoryStore())
    request = service._probe_request(cap.STT_BATCH, "org_default", "model_configuration_test")
    with wave.open(BytesIO(request.audio_bytes)) as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        assert source.getframerate() == 16000
        assert 1 < source.getnframes() / source.getframerate() < 5
        assert any(source.readframes(source.getnframes()))
    assert len(request.audio_bytes) < 200_000
    assert request.metadata["development_transcript"] == PHRASE


@pytest.mark.parametrize("defect", ["missing", "corrupt", "symlink", "oversized"])
def test_probe_asset_must_match_versioned_hash(monkeypatch, tmp_path, defect):
    from app.core.prompt import model_probes
    original = model_probes.batch_stt_probe_audio()
    path = tmp_path / "probe.wav"
    if defect == "corrupt":
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    elif defect == "oversized":
        path.write_bytes(original + b"extra")
    elif defect == "symlink":
        actual = tmp_path / "actual.wav"
        actual.write_bytes(original)
        path.symlink_to(actual)
    monkeypatch.setattr(model_probes, "BATCH_STT_PROBE_PATH", path)
    with pytest.raises(ProviderError) as caught:
        model_probes.batch_stt_probe_audio()
    assert caught.value.code == "provider_probe_failed" and caught.value.retryable is False


def test_audio_asset_contract_pins_exact_version_and_digest():
    from app.core.prompt import model_probes
    audio = model_probes.batch_stt_probe_audio()
    assert model_probes.BATCH_STT_PROBE_VERSION == "batch_stt_known_speech.v1"
    assert len(audio) == 86366
    assert hashlib.sha256(audio).hexdigest() == "326d0aad88aab144a81701c3f30b10a49795bee4e655e96ab583f105a31f3a5a"


@pytest.mark.anyio
async def test_real_batch_route_mismatch_cannot_become_healthy():
    service, _, model, calls = service_with_batch_response(PHRASE)
    await service.test_model_configuration(model["id"], {"capability": cap.STT_BATCH})
    route = service.create_route({"capability": cap.STT_BATCH, "purpose": "candidate_answer_repair",
        "primary": {"model_configuration_id": model["id"], "timeout_s": 10}})
    # Same provider configuration, but a subsequently unusable recognition result.
    class WrongRecognition:
        async def invoke(self, *args, **kwargs):
            from app.model_gateway.schemas import BatchSTTResponse, ProviderMeta
            return BatchSTTResponse(text="unrelated speech", provider=ProviderMeta(
                provider_id="dashscope", model="qwen3-asr-flash", request_id="test", latency_ms=1))
    service.gateway = WrongRecognition()
    with pytest.raises(ProviderError):
        await service.test_route(route["id"])
    assert service.list_routes()[0]["readiness"]["status"] == "failed"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_matching_mock_fallback_cannot_certify_real_speech_route():
    service, _, model, _ = service_with_batch_response(PHRASE)
    class MockRecognition:
        async def invoke(self, *args, **kwargs):
            from app.model_gateway.schemas import BatchSTTResponse, ProviderMeta
            return BatchSTTResponse(text=PHRASE, provider=ProviderMeta(
                provider_id="mock", model="mock-stt", request_id="test", latency_ms=1))
    service.gateway = MockRecognition()
    with pytest.raises(ProviderError) as caught:
        await service.test_model_configuration(model["id"], {"capability": cap.STT_BATCH})
    assert caught.value.code == "provider_probe_failed" and not caught.value.retryable
    assert service.get_model_configuration(model["id"])["status"] == "failed"


@pytest.mark.anyio
async def test_failed_batch_model_probe_has_exactly_one_paid_provider_attempt():
    service, _, model, calls = service_with_batch_response("", status=500)
    with pytest.raises(ProviderError):
        await service.test_model_configuration(model["id"], {"capability": cap.STT_BATCH})
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("text", [PHRASE, "连接测试语音识别正常", " 连接测试，语音识别正常！ "])
async def test_real_batch_model_and_route_require_matching_speech_without_returning_text(text):
    service, store, model, calls = service_with_batch_response(text)
    result = await service.test_model_configuration(model["id"], {"capability": cap.STT_BATCH})
    assert result["probe_mode"] == "known_speech" and result["transcript_matched"] is True
    assert "text" not in result and "audio_uri" not in result
    assert service.get_model_configuration(model["id"])["status"] == "ready"
    route = service.create_route({"capability": cap.STT_BATCH, "purpose": "candidate_answer_repair",
        "primary": {"model_configuration_id": model["id"], "timeout_s": 10}})
    assert (await service.test_route(route["id"]))["transcript_matched"] is True
    assert service.list_routes()[0]["readiness"]["ready"] is True
    assert len(calls) == 2
    assert all(call["model"] == "qwen3-asr-flash" for call in calls)
    assert all(call["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,") for call in calls)
    assert PHRASE not in json.dumps(store.model_invocations, ensure_ascii=False)
    assert not store.interviews


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["", "unrelated transcription", "连接测试语音识别不正常"])
async def test_empty_or_mismatched_real_speech_cannot_ready_model(text):
    service, _, model, _ = service_with_batch_response(text)
    with pytest.raises(ProviderError):
        await service.test_model_configuration(model["id"], {"capability": cap.STT_BATCH})
    persisted = service.get_model_configuration(model["id"])
    assert persisted["status"] == "failed"
    assert not text or text not in json.dumps(persisted.get("last_validation"), ensure_ascii=False)


@pytest.mark.anyio
async def test_explicit_mock_batch_probe_stays_available_without_faking_production_ready():
    service = ModelAdminService(InMemoryStore())
    connection = next(item for item in service.list_provider_connections() if item["provider_id"] == "mock")
    model = service.create_model_configuration({"provider_connection_id": connection["id"],
        "model_type": "stt", "provider_model_id": "mock-stt", "display_name": "mock fixture"})
    result = await service.test_model_configuration(model["id"], {"capability": cap.STT_BATCH})
    assert result["transcript_matched"] is True
    route = service.create_route({"capability": cap.STT_BATCH, "purpose": "candidate_answer_repair",
        "primary": {"model_configuration_id": model["id"], "timeout_s": 10}})
    await service.test_route(route["id"])
    assert service.list_routes()[0]["readiness"]["reason_code"] == "NON_PRODUCTION_PROVIDER"


def admission_batch_check(*, stream_provider=None, batch_state=None, batch_provider="dashscope"):
    store = InMemoryStore()
    service = ModelAdminService(store)
    now = datetime.now(timezone.utc)
    with service.persistence.transaction("org_default") as transaction:
        for capability, purpose, provider, health in (
            (cap.STT_STREAMING, "candidate_answer_transcription", stream_provider, "healthy"),
            (cap.STT_BATCH, "candidate_answer_repair", batch_provider if batch_state else None, batch_state),
        ):
            if provider is None:
                continue
            suffix = capability.rsplit(".", 1)[-1]
            connection = transaction.provider_connections.add({"id": "test_connection_" + suffix,
                "organization_id": "org_default", "provider_id": provider, "enabled": True, "configuration_revision": 1})
            model = transaction.model_configurations.add({"id": "test_model_" + suffix,
                "organization_id": "org_default",
                "provider_id": provider, "provider_connection_id": connection["id"],
                "enabled": True, "status": "ready", "supported_capabilities": [capability]})
            transaction.model_routes.add({"id": "test_route_" + suffix, "capability": capability,
                "organization_id": "org_default",
                "purpose": purpose, "primary": {"model_configuration_id": model["id"]},
                "policy": {"readiness_ttl_seconds": 60}, "last_health": {
                    "status": "healthy" if health == "expired" else health,
                    "checked_at": (now - timedelta(seconds=120 if health == "expired" else 0)).isoformat(),
                    "reason_code": "provider_timeout" if health == "failed" else "ROUTE_HEALTH_FRESH"}})
        result = AppointmentAdmission().plan_readiness(transaction, {}, now=now)
    return next(check for check in result["checks"] if check["name"] == "stt_batch")


@pytest.mark.parametrize("batch_state", [None, "failed", "expired"])
def test_real_streaming_requires_fresh_real_batch_recovery_even_in_development(monkeypatch, batch_state):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    check = admission_batch_check(stream_provider="dashscope", batch_state=batch_state)
    assert check["ready"] is False
    assert check["production_ready"] is False


def test_real_streaming_cannot_use_explicit_mock_repair(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    assert not admission_batch_check(stream_provider="dashscope", batch_state="healthy", batch_provider="mock")["ready"]


def test_real_streaming_with_healthy_real_batch_is_ready(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    check = admission_batch_check(stream_provider="dashscope", batch_state="healthy")
    assert check["ready"] is True and check["production_ready"] is True


@pytest.mark.parametrize("explicit_mock", [False, True])
def test_pure_mock_development_retains_explicit_fixture_path(monkeypatch, explicit_mock):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    assert admission_batch_check(stream_provider="mock" if explicit_mock else None,
        batch_state="healthy" if explicit_mock else None, batch_provider="mock")["ready"] is True


def test_explicit_real_batch_failure_is_not_hidden_without_streaming(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    assert admission_batch_check(batch_state="failed")["ready"] is False
