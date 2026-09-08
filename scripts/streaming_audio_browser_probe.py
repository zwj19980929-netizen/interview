"""Loopback-only synthetic browser playback probe; no database or recording.

Run from the repository root (uses only the existing local LiveKit service):
  INTERVIEWER_RUNTIME_ENV=development INTERVIEWER_LOCAL_MEDIA=true \
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m scripts.streaming_audio_browser_probe
Then run Vite on 127.0.0.1:5178 and open /web/streaming-audio-probe.html.
Every connection owns a fresh random room, removed when the probe finishes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.core.access_log import install_sensitive_access_log_filter

install_sensitive_access_log_filter()

from app.adapters.livekit_audio_output import LiveKitApprovedAudioPublisher
from app.adapters.livekit_media import LiveKitMediaPlane, prepare_local_rtc_environment
from app.domain.interview_agent import AvatarPerformance, LiveSpeechBinding, VisemeCue


_ORIGIN = "http://127.0.0.1:5178"
_RATE = 24_000
_TOTAL = _RATE * 2
_MODES = {"normal": 0.0, "gap600": 0.6, "gap3000": 3.0}
_LOG = logging.getLogger("streaming_audio_probe")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_busy = False


class _ProbeOnlyLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Native error details may contain room URLs or short-lived JWTs.
        # This isolated diagnostic process prints only our numeric events.
        return record.name == _LOG.name


def _local_plane() -> LiveKitMediaPlane:
    if os.getenv("INTERVIEWER_RUNTIME_ENV", "").strip().lower() != "development":
        raise RuntimeError("Probe requires an explicit development environment")
    if os.getenv("INTERVIEWER_LOCAL_MEDIA", "").strip().lower() not in {"true", "1", "yes", "on"}:
        raise RuntimeError("Probe requires explicitly enabled local media")
    prepare_local_rtc_environment()
    plane = LiveKitMediaPlane()
    parsed = urlsplit(plane.url)
    if (parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("Probe refuses non-loopback LiveKit endpoints")
    plane.require_ready(recording=False)
    return plane


async def _room_admin(plane: LiveKitMediaPlane, room: str, *, delete: bool) -> None:
    # Probe-local cleanup only: the random target never comes from a browser.
    # The admin grant stays in this process; the browser is receive-only.
    now = int(time.time())
    token = plane._jwt({"iss": plane.configuration.api_key, "sub": "synthetic-browser-probe",
                        "nbf": now - 5, "exp": now + 30, "video": {"roomCreate": True, "room": room}})
    parsed = urlsplit(plane.url)
    base = "%s://%s" % ("https" if parsed.scheme == "wss" else "http", parsed.netloc)
    method = "DeleteRoom" if delete else "CreateRoom"
    payload = {"room": room} if delete else {"name": room, "empty_timeout": 10, "max_participants": 2}
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.post("%s/twirp/livekit.RoomService/%s" % (base, method),
                                     json=payload, headers={"Authorization": "Bearer %s" % token})
        response.raise_for_status()


def _frame(index: int) -> bytes:
    # 20ms PCM16. Two low-volume tones make the resumed second half audible;
    # no microphone, TTS provider, stored file or candidate content is involved.
    frequency = 330 if index < 50 else 550
    first = index * 480
    return b"".join(int(900 * math.sin(2 * math.pi * frequency * (first + sample) / _RATE))
                    .to_bytes(2, "little", signed=True) for sample in range(480))


def _event(kind: str, started: float, **numbers: Any) -> dict:
    event = {"type": kind, "server_ms": round((time.monotonic() - started) * 1000, 1)}
    for key, value in numbers.items():
        if type(value) in {int, float} and math.isfinite(value):
            event[key] = value
    _LOG.info(json.dumps(event, separators=(",", ":")))
    return event


async def _receive(websocket: WebSocket, expected: str, *, started: float,
                   performance_id: str = "", output_id: str = "") -> dict:
    async def wait() -> dict:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                raise ValueError("Invalid probe message")
            if data.get("type") == "observation":
                if data.get("event") not in {"playing", "eof", "drained", "failed"}:
                    raise ValueError("Invalid observation")
                _event("client_" + data["event"], started,
                       client_ms=data.get("elapsed_ms"), current_time=data.get("current_time"))
                continue
            allowed = {"type", "mode"} if expected == "start" else {"type"}
            if expected in {"ready", "drained"}:
                allowed |= {"performance_id", "output_id"}
            if set(data) - allowed or data.get("type") != expected:
                raise ValueError("Unexpected probe message")
            if expected in {"ready", "drained"} and (
                data.get("performance_id") != performance_id or data.get("output_id") != output_id
            ):
                raise ValueError("Stale probe acknowledgement")
            return data
    return await asyncio.wait_for(wait(), timeout=15)


@app.get("/healthz")
async def healthz() -> dict:
    return {"probe": "synthetic_audio_only", "busy": _busy}


@app.websocket("/probe")
async def probe(websocket: WebSocket) -> None:
    global _busy
    if (websocket.headers.get("origin") != _ORIGIN or websocket.client is None
            or websocket.client.host not in {"127.0.0.1", "::1"}):
        await websocket.close(code=1008)
        return
    try:
        plane = _local_plane()
    except Exception:
        await websocket.close(code=1008)
        return
    if _busy:
        await websocket.close(code=1013)
        return
    _busy = True
    started = time.monotonic()
    room = "synthetic-browser-probe-" + uuid.uuid4().hex
    performance_id = "performance_probe_" + uuid.uuid4().hex
    output_id = "output_probe_" + uuid.uuid4().hex
    reader = None
    publisher = None
    created = False
    alive = True
    drained = False
    producer_done = False

    def assert_current() -> None:
        if not alive:
            raise RuntimeError("Probe output cancelled")

    async def wait_drained() -> None:
        nonlocal alive
        try:
            await _receive(websocket, "drained", started=started, performance_id=performance_id, output_id=output_id)
            if not producer_done:
                raise ValueError("Premature probe acknowledgement")
        except BaseException:
            alive = False
            raise

    try:
        await websocket.accept()
        request = await _receive(websocket, "start", started=started)
        if request.get("mode") not in _MODES:
            raise ValueError("Unsupported synthetic mode")
        gap = _MODES[request["mode"]]
        await _room_admin(plane, room, delete=False)
        created = True
        token = plane.issue_participant_token(room_name=room,
            identity="synthetic-listener:" + uuid.uuid4().hex, participant_role="synthetic_probe",
            publish_sources=[], can_subscribe=True, ttl_seconds=90)
        # Credentials are transported only to the local browser, never logged.
        await websocket.send_json({"type": "room", "url": plane.url, "participant_token": token})
        await _receive(websocket, "connected", started=started)
        _event("client_connected", started)
        publisher = LiveKitApprovedAudioPublisher(plane, room_name=room, performance_id=performance_id,
                                                 assert_current=assert_current, sample_rate_hz=_RATE)
        publication = await publisher.open()
        performance = AvatarPerformance(performance_id=performance_id, turn_id=None, audio_uri=None,
            audio_clock_origin_ms=0, text="本机低音量合成音频测试", delivery="streaming_tts",
            alignment_source="g2p_estimate", visemes=[VisemeCue(at_ms=0, duration_ms=2000, shape="sil", weight=0)],
            live_audio=LiveSpeechBinding(output_id=output_id, publisher_identity=publication.participant_identity,
                track_sid=publication.track_sid, track_name=publication.track_name, sample_rate_hz=_RATE, channels=1))
        await websocket.send_json({"type": "started", "performance": performance.model_dump(mode="json")})
        _event("started", started)
        await _receive(websocket, "ready", started=started, performance_id=performance_id, output_id=output_id)
        _event("ready", started)
        reader = asyncio.create_task(wait_drained())
        for index in range(100):
            if index == 50 and gap:
                await websocket.send_json(_event("gap_started", started, samples=publisher.total_samples, gap_ms=gap * 1000))
                await asyncio.sleep(gap)
                await websocket.send_json(_event("gap_finished", started, samples=publisher.total_samples))
            await publisher.publish(_frame(index))
            await asyncio.sleep(0.02)
        await publisher.finish()
        if publisher.total_samples != _TOTAL:
            raise RuntimeError("Synthetic sample count differs from the declared probe")
        producer_done = True
        eof = {"performance_id": performance_id, "output_id": output_id,
               "total_samples": publisher.total_samples, "sample_rate_hz": _RATE}
        await websocket.send_json({**_event("producer_finished", started, samples=publisher.total_samples), "payload": eof})
        await asyncio.wait_for(reader, timeout=12)
        drained = True
        _event("drained", started, samples=publisher.total_samples)
        await publisher.aclose()
        await websocket.send_json(_event("complete", started, samples=publisher.total_samples))
    except (WebSocketDisconnect, asyncio.CancelledError):
        _event("disconnected", started)
    except Exception:
        _event("probe_failed", started)
        try:
            await websocket.send_json(_event("probe_failed", started))
        except Exception:
            pass
    finally:
        alive = False
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if publisher is not None and not publisher.closed:
            try:
                await (publisher.aclose() if drained else publisher.abort())
            except Exception:
                _event("publisher_cleanup_failed", started)
        if created:
            try:
                await _room_admin(plane, room, delete=True)
                _event("room_deleted", started)
            except Exception:
                # The room also has a ten-second empty timeout as a fallback.
                _event("room_cleanup_failed", started)
        try:
            await websocket.close()
        except Exception:
            pass
        _busy = False


def main() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_ProbeOnlyLogs())
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    install_sensitive_access_log_filter()
    _local_plane()  # Fail before binding if environment or target is not local.
    uvicorn.run(app, host="127.0.0.1", port=5179, log_config=None, access_log=False,
                ws_max_size=8192, ws_ping_interval=10, ws_ping_timeout=10)


if __name__ == "__main__":
    main()
