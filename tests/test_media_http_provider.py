import json

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    AvatarSpeakRequest,
    BatchSTTRequest,
    ProviderContext,
    StreamingAudioConfig,
    StreamingSTTRequest,
)
from app.providers.media_http.provider import MediaHttpProvider


def provider(handler):
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return MediaHttpProvider(client_factory=client_factory)


def context(capability: str, model: str, settings=None) -> ProviderContext:
    return ProviderContext(
        organization_id="org_default",
        invocation_id="inv_media_1",
        route_id="route_media_1",
        provider_connection_id="provider_conn_media",
        model_configuration_id="model_cfg_media",
        model_type="avatar" if capability == cap.AVATAR_SPEAK else "stt",
        capability=capability,
        purpose="candidate_answer_transcription" if capability != cap.AVATAR_SPEAK else "interview_question_delivery",
        model=model,
        timeout_s=5,
        attempt=1,
        fallback_index=0,
        connection_config={"base_url": "https://media.example.com/v1"},
        model_settings=settings or {},
        credentials={"api_key": "media-key"},
    )


@pytest.mark.anyio
async def test_media_http_batch_stt_sends_private_audio_as_multipart_and_parses_segments() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "id": "stt_req_1",
                "text": "候选人回答",
                "language": "zh-CN",
                "confidence": 0.92,
                "segments": [{"text": "候选人回答", "start": 0.1, "end": 1.25, "confidence": 0.91}],
            },
        )

    response = await provider(handler).invoke(
        cap.STT_BATCH,
        BatchSTTRequest(
            purpose="candidate_answer_repair",
            audio_uri="private-file://file_1",
            content_type="audio/webm;codecs=opus",
            audio_bytes=b"real-candidate-audio",
        ),
        context(cap.STT_BATCH, "speech-model"),
    )

    assert seen["url"] == "https://media.example.com/v1/audio/transcriptions"
    assert seen["authorization"] == "Bearer media-key"
    assert seen["content_type"].startswith("multipart/form-data;")
    assert b"real-candidate-audio" in seen["body"]
    assert b"speech-model" in seen["body"]
    assert response.text == "候选人回答"
    assert response.segments[0].start_ms == 100
    assert response.segments[0].end_ms == 1250
    assert response.provider.request_id == "stt_req_1"


@pytest.mark.anyio
async def test_media_http_stream_buffers_audio_and_emits_one_authoritative_final() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"chunk-onechunk-two" in request.content
        return httpx.Response(200, json={"text": "完整回答", "confidence": 0.88})

    adapter = provider(handler)
    request = StreamingSTTRequest(
        interview_id="interview_1",
        turn_id="turn_1",
        audio=StreamingAudioConfig(content_type="audio/webm;codecs=opus"),
    )
    stream = await adapter.open_stream(request, context(cap.STT_STREAMING, "speech-model"))
    await stream.send_audio(b"chunk-one")
    await stream.send_audio(b"chunk-two")
    events = await stream.finish()

    assert [item.type for item in events] == ["transcript.final", "stream.closed"]
    assert events[0].text == "完整回答"
    assert events[0].is_final is True


@pytest.mark.anyio
async def test_media_http_avatar_returns_https_video_delivery() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "speech_id": "speech_1",
                "status": "ready",
                "mode": "video",
                "stream_url": "https://cdn.example.com/avatar/speech_1.mp4",
                "request_id": "avatar_req_1",
            },
        )

    response = await provider(handler).invoke(
        cap.AVATAR_SPEAK,
        AvatarSpeakRequest(text="请介绍最近的项目", avatar_id="avatar_cn"),
        context(cap.AVATAR_SPEAK, "avatar-model"),
    )

    assert seen["payload"]["model"] == "avatar-model"
    assert seen["payload"]["avatar_id"] == "avatar_cn"
    assert response.mode == "video"
    assert response.stream_url == "https://cdn.example.com/avatar/speech_1.mp4"
    assert response.text == "请介绍最近的项目"


@pytest.mark.anyio
async def test_media_http_avatar_rejects_public_insecure_media_urls() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"mode": "video", "stream_url": "http://public.example.com/avatar.mp4"})

    with pytest.raises(ProviderError) as raised:
        await provider(handler).invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(text="test"),
            context(cap.AVATAR_SPEAK, "avatar-model"),
        )
    assert raised.value.code == "provider_media_url_invalid"


@pytest.mark.anyio
async def test_media_http_avatar_rejects_credential_bearing_media_urls() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"mode": "video", "stream_url": "https://user:secret@cdn.example.com/avatar.mp4"},
        )

    with pytest.raises(ProviderError) as raised:
        await provider(handler).invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(text="test"),
            context(cap.AVATAR_SPEAK, "avatar-model"),
        )
    assert raised.value.code == "provider_media_url_invalid"


@pytest.mark.anyio
async def test_media_http_health_probe_requires_ready_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://media.example.com/v1/health"
        return httpx.Response(200, json={"status": "healthy"})

    result = await provider(handler).validate_credentials(
        {"base_url": "https://media.example.com/v1"}, {"api_key": "media-key"}
    )
    assert result["status"] == "valid"


@pytest.mark.anyio
async def test_media_http_rejects_credentials_embedded_in_base_url() -> None:
    adapter = provider(lambda _: httpx.Response(200, json={"status": "healthy"}))

    with pytest.raises(ProviderError) as raised:
        await adapter.validate_credentials(
            {"base_url": "https://user:secret@media.example.com/v1"},
            {"api_key": "media-key"},
        )
    assert raised.value.code == "provider_bad_request"
