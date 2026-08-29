import json

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import AvatarSpeakRequest, ProviderContext
from app.providers.tencent_cloud_avatar.provider import TencentCloudAvatarProvider


def context() -> ProviderContext:
    return ProviderContext(
        organization_id="org_default",
        invocation_id="invocation_avatar_test",
        route_id="route_avatar",
        provider_connection_id="provider_conn_avatar",
        model_configuration_id="model_cfg_avatar",
        model_type="avatar",
        capability=cap.AVATAR_SPEAK,
        purpose="interview_question_delivery",
        model="tencent-cloud-avatar-webrtc",
        timeout_s=5,
        attempt=1,
        fallback_index=0,
        connection_config={"base_url": "https://gw.tvs.qq.com", "asset_virtualman_key": "asset_1", "ready_poll_interval_s": 0.1},
        credentials={"app_key": "app-key", "access_token": "access-token"},
    )


@pytest.mark.anyio
async def test_tencent_avatar_creates_drives_and_closes_webrtc_session() -> None:
    seen = []
    sockets = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen.append((request.url.path, dict(request.url.params), payload))
        assert "access-token" not in str(request.url)
        assert request.url.params.get("signature")
        if request.url.path.endswith("createsessionbyasset"):
            return httpx.Response(200, json={"Header": {"Code": 0}, "Payload": {"SessionId": "session_1", "SessionStatus": 3}})
        if request.url.path.endswith("statsession"):
            return httpx.Response(200, json={"Header": {"Code": 0}, "Payload": {"SessionStatus": 1, "PlayStreamAddr": "webrtc://liveplay.ivh.qq.com/live/session_1"}})
        return httpx.Response(200, json={"Header": {"Code": 0}, "Payload": {}})

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    class FakeSocket:
        def __init__(self, url):
            self.url = url
            self.sent = []
            self.closed = False

        async def send(self, value):
            command = json.loads(value)
            self.sent.append(command)
            self.response = json.dumps({
                "Payload": {
                    "Type": 3,
                    "ReqId": command["Payload"]["ReqId"],
                    "SessionId": command["Payload"]["SessionId"],
                    "SpeakStatus": "TextStart",
                    "ErrorCode": 0,
                }
            })

        async def recv(self):
            return self.response

        async def close(self):
            self.closed = True

    async def websocket_connect(url, **kwargs):
        socket = FakeSocket(url)
        sockets.append(socket)
        return socket

    provider = TencentCloudAvatarProvider(
        client_factory=client_factory,
        websocket_connect=websocket_connect,
    )
    response = await provider.invoke(
        cap.AVATAR_SPEAK,
        AvatarSpeakRequest(text="请介绍你负责过的系统", metadata={"interview_id": "iv_1"}),
        context(),
    )

    assert response.mode == "webrtc"
    assert response.session_id == "session_1"
    assert response.stream_url.startswith("webrtc://")
    assert [item[0].rsplit("/", 1)[-1] for item in seen] == [
        "createsessionbyasset", "statsession", "startsession"
    ]
    assert sockets[0].url.startswith("wss://gw.tvs.qq.com/v2/ws/ivh/")
    assert "requestid=session_1" in sockets[0].url
    assert sockets[0].sent[0]["Payload"]["Data"]["ChatCommand"] == "NotUseChat"
    assert len(sockets[0].sent[0]["Payload"]["ReqId"]) == 32
    assert sockets[0].closed is True

    closed = await provider.invoke(
        cap.AVATAR_SPEAK,
        AvatarSpeakRequest(text="close", operation="close", session_id="session_1"),
        context(),
    )
    assert closed.status == "closed"
    assert seen[-1][0].endswith("closesession")


@pytest.mark.anyio
async def test_tencent_avatar_closes_billed_session_when_wss_drive_fails() -> None:
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("createsessionbyasset"):
            return httpx.Response(
                200,
                json={
                    "Header": {"Code": 0},
                    "Payload": {
                        "SessionId": "session_failed_drive",
                        "SessionStatus": 1,
                        "PlayStreamAddr": "webrtc://liveplay.ivh.qq.com/live/failed",
                    },
                },
            )
        if request.url.path.endswith("statsession"):
            return httpx.Response(
                200,
                json={"Header": {"Code": 0}, "Payload": {"SessionStatus": 1}},
            )
        return httpx.Response(200, json={"Header": {"Code": 0}, "Payload": {}})

    class RejectingSocket:
        closed = False

        async def send(self, value):
            self.request_id = json.loads(value)["Payload"]["ReqId"]

        async def recv(self):
            return json.dumps({
                "Payload": {
                    "Type": 9,
                    "ReqId": self.request_id,
                    "SessionId": "session_failed_drive",
                    "SpeakStatus": "Error",
                    "ErrorCode": 23001,
                }
            })

        async def close(self):
            self.closed = True

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    socket = RejectingSocket()

    async def websocket_connect(url, **kwargs):
        return socket

    provider = TencentCloudAvatarProvider(
        client_factory=client_factory,
        websocket_connect=websocket_connect,
    )
    with pytest.raises(ProviderError, match="rejected the text drive"):
        await provider.invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(text="连接失败时必须释放并发"),
            context(),
        )

    assert socket.closed is True
    assert seen_paths[-1].endswith("closesession")
