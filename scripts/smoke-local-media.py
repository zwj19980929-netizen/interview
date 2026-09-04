"""本地 LiveKit Participant Egress 冒烟：发布合成音视频并验证本机 MP4。"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from pathlib import Path

# 允许从项目根目录直接执行本脚本，不要求用户再配置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.livekit_media import (
    LiveKitMediaPlane,
    prepare_local_rtc_environment,
)

# 与正式后端一致：本地 ws:// RTC 绕过 HTTP/SOCKS 代理，HTTPS 代理保持不变。
prepare_local_rtc_environment()

from livekit import rtc

from app.file_storage.local import LocalPrivateFileAdapter


async def _wait_for_egress(
    plane: LiveKitMediaPlane,
    egress_id: str,
    wanted: set[str],
    *,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest = {}
    while time.monotonic() < deadline:
        response = await plane.list_egress(egress_id=egress_id)
        items = response.get("items") or response.get("egress_items") or []
        latest = next(
            (
                item
                for item in items
                if str(item.get("egress_id") or item.get("egressId") or "")
                == egress_id
            ),
            latest,
        )
        status = str(latest.get("status") or "").upper()
        if status in wanted:
            return latest
        if status in {"EGRESS_FAILED", "FAILED", "4"}:
            raise RuntimeError("本地 Egress 失败：%s" % latest.get("error", status))
        await asyncio.sleep(0.5)
    raise TimeoutError("等待本地 Egress 状态超时：%s" % latest)


async def _publish_media(audio: rtc.AudioSource, video: rtc.VideoSource) -> None:
    sample_rate = 48_000
    samples_per_frame = 960
    width, height = 640, 360
    video_frame = rtc.VideoFrame(
        width,
        height,
        rtc.VideoBufferType.RGB24,
        bytes((38, 112, 88)) * width * height,
    )
    for frame_index in range(250):
        samples = bytearray()
        for sample_index in range(samples_per_frame):
            position = frame_index * samples_per_frame + sample_index
            value = int(4_000 * math.sin(2 * math.pi * 440 * position / sample_rate))
            samples.extend(value.to_bytes(2, "little", signed=True))
        await audio.capture_frame(
            rtc.AudioFrame(samples, sample_rate, 1, samples_per_frame)
        )
        if frame_index % 5 == 0:
            video.capture_frame(video_frame)
        await asyncio.sleep(0.02)


async def main() -> None:
    if os.getenv("INTERVIEWER_LOCAL_MEDIA", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("请先设置 INTERVIEWER_LOCAL_MEDIA=true")

    plane = LiveKitMediaPlane()
    plane.require_ready(recording=True)
    suffix = str(int(time.time()))
    room_name = "local-smoke-%s" % suffix
    identity = "candidate:local-smoke-%s" % suffix
    object_key = "interview-captures/local-smoke/%s/capture.mp4" % suffix
    room = rtc.Room()
    audio = rtc.AudioSource(48_000, 1)
    video = rtc.VideoSource(640, 360)
    try:
        token = plane.issue_participant_token(
            room_name=room_name,
            identity=identity,
            participant_role="candidate",
            publish_sources=("microphone", "camera"),
            ttl_seconds=90,
        )
        await room.connect(plane.url, token)
        audio_track = rtc.LocalAudioTrack.create_audio_track("microphone", audio)
        video_track = rtc.LocalVideoTrack.create_video_track("camera", video)
        await room.local_participant.publish_track(
            audio_track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        await room.local_participant.publish_track(
            video_track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
        )
        started = await plane.start_participant_egress(
            room_name=room_name,
            participant_identity=identity,
            object_key=object_key,
        )
        egress_id = str(started.get("egress_id") or started.get("egressId") or "")
        if not egress_id:
            raise RuntimeError("Egress 启动响应缺少 egress_id")
        await _wait_for_egress(
            plane,
            egress_id,
            {"EGRESS_ACTIVE", "ACTIVE", "1"},
            timeout_seconds=45,
        )
        await _publish_media(audio, video)
        await plane.stop_egress(egress_id)
        await _wait_for_egress(
            plane,
            egress_id,
            {"EGRESS_COMPLETE", "COMPLETE", "3"},
            timeout_seconds=45,
        )
    finally:
        await room.disconnect()

    storage = LocalPrivateFileAdapter()
    protection = storage.verify_recording_protection(object_key)
    path = Path("data/private-files") / object_key
    byte_count = path.stat().st_size
    if byte_count <= 0:
        raise RuntimeError("本地录像文件为空")
    print(
        "本地媒体冒烟通过：%s，%d 字节，保护=%s"
        % (path, byte_count, protection.descriptor)
    )


if __name__ == "__main__":
    asyncio.run(main())
