#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_DIR}/infra/local-media/docker-compose.yml"
ACTION="${1:-up}"

# 浏览器与 Egress 容器都要能连接 LiveKit 的 RTC 地址，因此优先使用本机局域网地址。
detect_node_ip() {
  local default_interface=""
  local detected_ip=""
  if command -v route >/dev/null 2>&1; then
    default_interface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
  fi
  if [[ -n "${default_interface}" ]] && command -v ipconfig >/dev/null 2>&1; then
    detected_ip="$(ipconfig getifaddr "${default_interface}" 2>/dev/null || true)"
  fi
  if [[ -z "${detected_ip}" ]] && command -v hostname >/dev/null 2>&1; then
    detected_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  printf '%s' "${detected_ip:-127.0.0.1}"
}

case "${ACTION}" in
  up)
    mkdir -p "${PROJECT_DIR}/data/private-files"
    export INTERVIEWER_LOCAL_LIVEKIT_NODE_IP="${INTERVIEWER_LOCAL_LIVEKIT_NODE_IP:-$(detect_node_ip)}"
    docker compose -f "${COMPOSE_FILE}" up -d
    echo "本地 LiveKit/Egress 已启动，RTC 地址：${INTERVIEWER_LOCAL_LIVEKIT_NODE_IP}"
    echo "启动后端时只需增加：INTERVIEWER_LOCAL_MEDIA=true"
    ;;
  down)
    docker compose -f "${COMPOSE_FILE}" down
    ;;
  status)
    docker compose -f "${COMPOSE_FILE}" ps
    ;;
  logs)
    docker compose -f "${COMPOSE_FILE}" logs --tail=200 livekit egress
    ;;
  doctor)
    if ! command -v python3 >/dev/null 2>&1; then
      echo "LOCAL_MEDIA_DOCTOR_UNAVAILABLE：只读诊断需要 Python 3；未修改任何服务。" >&2
      exit 1
    fi
    python3 "${SCRIPT_DIR}/local-media-doctor.py" "${COMPOSE_FILE}"
    ;;
  *)
    echo "用法：$0 [up|down|status|logs|doctor]" >&2
    exit 2
    ;;
esac
