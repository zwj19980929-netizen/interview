"""Read-only check for stale LiveKit RTC node IP after a local network change.

Only exact-container command metadata is read; config files, environment,
tokens, room APIs and restart/delete operations are deliberately out of scope.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from typing import Optional


def _read(command: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    # Never echo raw Docker output/errors: a command may contain credentials.
    return result.stdout.strip() if result.returncode == 0 else None


def _ip(value: object, *, automatic: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if address.is_unspecified or address.is_multicast or (automatic and address.is_loopback):
        return None
    return str(address)


def _detected_ip() -> Optional[str]:
    route = _read(["route", "-n", "get", "default"])
    if route is not None:
        interface = next((line.split(":", 1)[1].strip() for line in route.splitlines()
                          if line.strip().startswith("interface:")), "")
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", interface):
            address = _ip(_read(["ipconfig", "getifaddr", interface]), automatic=True)
            if address is not None:
                return address
    # Linux fallback mirrors the existing up command's detection order.
    addresses = _read(["hostname", "-I"])
    if addresses is not None:
        for value in addresses.split():
            address = _ip(value, automatic=True)
            if address is not None:
                return address
    return None


def _configured_node_ip(raw: str) -> Optional[str]:
    try:
        command = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return None
    values = []
    for index, part in enumerate(command):
        if part == "--node-ip":
            if index + 1 >= len(command):
                return None
            values.append(command[index + 1])
        elif part.startswith("--node-ip="):
            values.append(part[len("--node-ip="):])
    # Ambiguous flags must not silently select a value different from LiveKit.
    return _ip(values[0]) if len(values) == 1 else None


def _failed(code: str, message: str) -> int:
    print("%s：%s 未修改任何服务。" % (code, message), file=sys.stderr)
    print("确认没有进行中的面试后，可手动运行：bash scripts/local-media.sh up", file=sys.stderr)
    return 1


def main(compose_file: str) -> int:
    override = os.environ.get("INTERVIEWER_LOCAL_LIVEKIT_NODE_IP", "").strip()
    expected = _ip(override) if override else _detected_ip()
    if expected is None:
        return _failed(
            "LOCAL_MEDIA_DOCTOR_DETECTION_FAILED",
            "显式 RTC IP 无效。" if override else "无法检测本机非回环 IP，请检查网络或显式设置 INTERVIEWER_LOCAL_LIVEKIT_NODE_IP。",
        )
    containers = _read(["docker", "compose", "-f", compose_file, "ps", "-q", "livekit"])
    if containers is None:
        return _failed("LOCAL_MEDIA_DOCTOR_DOCKER_UNAVAILABLE", "无法只读查询本地 LiveKit 容器，请检查 Docker 状态。")
    if not containers:
        return _failed("LOCAL_MEDIA_DOCTOR_NOT_RUNNING", "本地 LiveKit 未启动。")
    container_ids = containers.splitlines()
    if len(container_ids) != 1 or not re.fullmatch(r"[0-9a-fA-F]{12,64}", container_ids[0]):
        return _failed("LOCAL_MEDIA_DOCTOR_CONTAINER_AMBIGUOUS", "无法确定唯一的本地 LiveKit 容器。")
    raw_command = _read(["docker", "inspect", "--format", "{{json .Config.Cmd}}", container_ids[0]])
    if raw_command is None:
        return _failed("LOCAL_MEDIA_DOCTOR_INSPECT_FAILED", "无法读取当前 LiveKit 的 RTC 地址。")
    actual = _configured_node_ip(raw_command)
    if actual is None:
        return _failed("LOCAL_MEDIA_DOCTOR_NODE_IP_INVALID", "当前 LiveKit 命令缺少唯一有效的 --node-ip。")
    if actual != expected:
        return _failed(
            "LOCAL_MEDIA_DOCTOR_IP_DRIFT",
            "LiveKit node-ip=%s，但%s=%s；旧 RTC 地址可能导致 ICE 无响应。"
            % (actual, "显式配置 IP" if override else "本机当前 IP", expected),
        )
    print("LOCAL_MEDIA_DOCTOR_OK：LiveKit node-ip=%s，与%s一致。" % (actual, "显式配置" if override else "本机当前 IP"))
    print("本检查只核对 RTC 地址：信令健康不等于浏览器媒体健康，仍需验证浏览器 WebRTC/ICE 实际收发。")
    print("需要更新地址时，确认没有进行中的面试后，手动运行：bash scripts/local-media.sh up")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("请通过 bash scripts/local-media.sh doctor 运行只读诊断。", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
