import asyncio
import json
import os

import httpx

from app.adapters.livekit_media import (
    LiveKitConfiguration,
    LiveKitMediaPlane,
    prepare_local_rtc_environment,
)
from app.file_storage.local import LocalPrivateFileAdapter
from app.file_storage.provider import (
    private_file_storage,
    reset_private_file_storage_for_tests,
)


def test_one_local_media_switch_supplies_the_complete_development_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    # 即使遗留配置还写着 OSS，本地总开关也应形成完整、自洽的本地选择。
    monkeypatch.setenv("INTERVIEWER_FILE_STORAGE_BACKEND", "aliyun_oss")
    for name in (
        "INTERVIEWER_LIVEKIT_URL",
        "INTERVIEWER_LIVEKIT_API_KEY",
        "INTERVIEWER_LIVEKIT_API_SECRET",
        "INTERVIEWER_LIVEKIT_EGRESS_URL",
        "INTERVIEWER_LIVEKIT_INGRESS_ENABLED",
        "INTERVIEWER_LIVEKIT_INGRESS_MODE",
        "INTERVIEWER_OSS_ENDPOINT",
        "INTERVIEWER_OSS_BUCKET",
        "INTERVIEWER_OSS_ACCESS_KEY_ID",
        "INTERVIEWER_OSS_ACCESS_KEY_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    configuration = LiveKitConfiguration.from_environment()

    assert configuration.url == "ws://127.0.0.1:7880"
    assert configuration.egress_url == "http://127.0.0.1:7880"
    assert configuration.recording_storage_backend == "local"
    assert configuration.authoritative_audio_ingress_ready() is True
    assert configuration.recording_ready() is True
    reset_private_file_storage_for_tests()
    try:
        assert isinstance(private_file_storage(), LocalPrivateFileAdapter)
    finally:
        reset_private_file_storage_for_tests()


def test_local_media_switch_is_rejected_outside_development(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    monkeypatch.setenv("INTERVIEWER_FILE_STORAGE_BACKEND", "local")

    configuration = LiveKitConfiguration.from_environment()

    assert configuration.media_ready() is False
    assert configuration.recording_ready() is False
    assert "local_media_forbidden_in_production" in (
        configuration.recording_readiness_issues()
    )


def test_local_rtc_removes_only_http_and_socks_proxy_variables(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8118")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:8118")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:8118")
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:8118")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8118")

    removed = prepare_local_rtc_environment()

    assert set(removed) == {"HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"}
    assert "HTTP_PROXY" not in os.environ
    assert "ALL_PROXY" not in os.environ
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:8118"


def test_local_egress_uses_container_path_and_returns_logical_object_key() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "egress_id": "egress_local",
                "participant": {
                    "file_outputs": [
                        {
                            "filepath": (
                                "/recordings/interview-captures/org/iv/capture.mp4"
                            )
                        }
                    ]
                },
            },
        )

    configuration = LiveKitConfiguration(
        url="ws://127.0.0.1:7880",
        api_key="interviewer-local-dev",
        api_secret="interviewer-local-dev-secret-change-me",
        egress_url="http://127.0.0.1:7880",
        storage_endpoint="",
        storage_bucket="",
        storage_region="auto",
        storage_access_key="",
        storage_secret="",
        runtime_environment="development",
        local_media_enabled=True,
        recording_storage_backend="local",
        private_file_storage_backend="local",
        authoritative_ingress_enabled=True,
        authoritative_ingress_mode="database_fenced",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = LiveKitMediaPlane(configuration, client=client)

    result = asyncio.run(
        plane.start_participant_egress(
            room_name="room_local",
            participant_identity="candidate:local",
            object_key="interview-captures/org/iv/capture.mp4",
        )
    )
    asyncio.run(client.aclose())

    output = seen["payload"]["file_outputs"][0]
    assert seen["url"].endswith("/twirp/livekit.Egress/StartParticipantEgress")
    assert output["filepath"] == (
        "/recordings/interview-captures/org/iv/capture.mp4"
    )
    assert "s3" not in output
    assert result["participant"]["file_outputs"][0]["filepath"] == (
        "interview-captures/org/iv/capture.mp4"
    )


def test_local_recording_protection_is_development_only_and_not_encryption(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    storage = LocalPrivateFileAdapter(tmp_path)
    object_key = "interview-captures/org/iv/capture.mp4"
    path = tmp_path / object_key
    path.parent.mkdir(parents=True)
    path.write_bytes(b"local recording")

    protection = storage.verify_recording_protection(object_key)

    assert protection.descriptor == "local_private_development"
    assert protection.encryption is None
    assert protection.development_only is True
