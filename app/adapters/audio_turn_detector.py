"""Bounded, local-only audio end-of-turn inference.

Only a short PCM tail crosses this seam. Predictions are advisory: a caller
must recheck its capture/input revision before acting and must never interpret
an unavailable result as an end of turn. The Python 3.10+ native runtime lives
in one private subprocess; the API remains compatible with Python 3.9.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional


_MODEL = "turn-detector-v1-mini"
_RUNTIME_VERSION = "0.2.7"
_MAX_PCM_BYTES = 19_200 * 2  # The native model consumes the last 1.2s at 16kHz.
_SUPPORTED_LANGUAGES = frozenset(
    {"en", "ar", "de", "es", "fr", "hi", "id", "it", "ja", "ko", "nl", "pt", "tr", "zh"}
)
_WORKER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audio_turn_detector_worker.py"


@dataclass(frozen=True)
class AudioTurnPrediction:
    status: Literal["ready", "unsupported", "unavailable"]
    probability: Optional[float]
    latency_ms: float
    reason_code: Optional[str] = None


class LocalAudioTurnDetector:
    """One local worker, at most one in-flight request, and no pending queue.

    ``python_executable`` must name an explicitly provisioned local interpreter
    containing livekit-local-inference==0.2.7 and numpy. Nothing is downloaded
    at runtime. A busy/failed/slow worker returns an unknown prediction rather
    than blocking candidate audio. ``close`` cancels active work and reaps the
    worker. Input must already be authorized, mono PCM signed 16-bit LE.
    """

    def __init__(
        self,
        python_executable: str,
        *,
        timeout_seconds: float = 0.8,
        startup_timeout_seconds: float = 8.0,
        process_factory: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0.01 <= timeout_seconds <= 5:
            raise ValueError("Audio turn prediction timeout must be between 0.01 and 5 seconds")
        if not math.isfinite(startup_timeout_seconds) or not 0.05 <= startup_timeout_seconds <= 30:
            raise ValueError("Audio turn worker startup timeout must be between 0.05 and 30 seconds")
        self._python_executable = str(python_executable or "")
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process: Any = None
        self._busy = False
        self._closed = False
        self._request_id = 0

    async def predict(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16_000,
        language: str = "zh-CN",
    ) -> AudioTurnPrediction:
        started = time.monotonic()

        def result(status: str, reason: Optional[str] = None, probability: Optional[float] = None):
            return AudioTurnPrediction(status, probability, (time.monotonic() - started) * 1000, reason)

        if self._closed:
            return result("unavailable", "closed")
        if sample_rate_hz != 16_000:
            return result("unsupported", "sample_rate_unsupported")
        language_code = str(language or "").replace("_", "-").split("-", 1)[0].lower()
        if language_code not in _SUPPORTED_LANGUAGES:
            return result("unsupported", "language_unsupported")
        if not isinstance(pcm_s16le, bytes) or not pcm_s16le or len(pcm_s16le) % 2:
            return result("unavailable", "invalid_audio")
        if self._busy:
            return result("unavailable", "busy")
        if not self._python_executable:
            return result("unavailable", "runtime_not_configured")

        self._busy = True
        try:
            if self._process is None or self._process.returncode is not None:
                await self._stop_worker()
                await asyncio.wait_for(self._start_worker(), self._startup_timeout_seconds)
            if self._closed:
                await self._stop_worker()
                return result("unavailable", "closed")
            self._request_id += 1
            payload = {
                "id": self._request_id,
                "pcm": base64.b64encode(pcm_s16le[-_MAX_PCM_BYTES:]).decode("ascii"),
            }
            response = await asyncio.wait_for(self._exchange(payload), self._timeout_seconds)
            if self._closed:
                return result("unavailable", "closed")
            if (
                set(response) != {"id", "status", "probability"}
                or type(response.get("id")) is not int
                or response.get("id") != self._request_id
            ):
                raise ValueError("Invalid local turn prediction envelope")
            if response.get("status") != "ready":
                await self._stop_worker()
                return result("unavailable", "inference_failed")
            probability = response.get("probability")
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
            ):
                raise ValueError("Invalid local turn probability")
            return result("ready", probability=float(probability))
        except asyncio.CancelledError:
            # Never leave a cancelled request's response for a newer request.
            await self._stop_worker()
            raise
        except asyncio.TimeoutError:
            await self._stop_worker()
            return result("unavailable", "timeout")
        except (FileNotFoundError, PermissionError):
            await self._stop_worker()
            return result("unavailable", "runtime_unavailable")
        except Exception:
            # Exception bodies may contain native paths or input: do not log them.
            await self._stop_worker()
            return result("unavailable", "worker_failed")
        finally:
            self._busy = False

    async def close(self) -> None:
        self._closed = True
        await self._stop_worker()

    async def _start_worker(self) -> None:
        self._process = await self._process_factory(
            self._python_executable,
            "-I",
            str(_WORKER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            limit=8192,
        )
        response = await self._read_response()
        if response != {"status": "ready", "model": _MODEL, "runtime_version": _RUNTIME_VERSION}:
            raise ValueError("Audio turn runtime failed to initialize")

    async def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Audio turn worker is not running")
        process.stdin.write(json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n")
        await process.stdin.drain()
        return await self._read_response()

    async def _read_response(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("Audio turn worker is not running")
        line = await process.stdout.readline()
        if not line or len(line) > 4096:
            raise ValueError("Invalid audio turn worker response")
        response = json.loads(line)
        if not isinstance(response, dict):
            raise ValueError("Invalid audio turn worker response")
        return response

    async def _stop_worker(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 0.5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
