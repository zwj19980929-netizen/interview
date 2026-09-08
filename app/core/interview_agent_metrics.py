"""PII-free process metrics for the real-time interview control plane."""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from time import perf_counter
from typing import Callable, Deque, Dict, Iterator


CANDIDATE_INTERVIEW_AGENT_METRICS = frozenset(
    {
        "local_microphone_feedback_ms",
        "server_audio_confirmation_ms",
        "partial_first_token_ms",
        "stop_to_final_ms",
        "barge_in_mute_ms",
        "final_to_first_audio_realtime_ms",
        "final_to_first_audio_cascade_ms",
        "avatar_fps",
        "avatar_viseme_drift_ms",
        "avatar_freeze_ms",
    }
)

INTERVIEW_AGENT_STAGE_METRICS = frozenset(
    {
        "stt_snapshot_ms",
        "stt_final_ms",
        "understanding_prepare_ms",
        "decision_commit_ms",
        "turn_detector_ms",
        "tts_synthesis_ms",
        "tts_asset_import_ms",
        "tts_stream_open_ms",
        "tts_first_pcm_ms",
        "tts_playback_ready_ms",
        "tts_remote_drain_ms",
    }
)

INTERNAL_INTERVIEW_AGENT_METRICS = INTERVIEW_AGENT_STAGE_METRICS | frozenset(
    {
        "evidence_owner_renew_scheduler_lag_ms",
        "evidence_owner_renew_db_latency_ms",
        "evidence_owner_renew_success",
        "turn_decision_cancelled",
        "stt_recognition_opened",
        "stt_recognition_finished",
        "stt_preview_prepared",
        "stt_preview_final_revised",
    }
)

INTERVIEW_AGENT_METRICS = (
    CANDIDATE_INTERVIEW_AGENT_METRICS | INTERNAL_INTERVIEW_AGENT_METRICS
)


class InterviewAgentMetrics:
    """Bounded histograms with a fixed label-free vocabulary.

    Interview/candidate/provider identifiers are deliberately not accepted, so
    telemetry cannot turn into a second store of sensitive interview facts.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self._values: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._capacity)
        )
        self._lock = threading.Lock()

    def observe(self, name: str, value: float) -> None:
        if name not in INTERVIEW_AGENT_METRICS:
            raise ValueError("unsupported interview-agent metric")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or numeric > 300_000:
            raise ValueError("interview-agent metric value is out of bounds")
        with self._lock:
            self._values[name].append(numeric)

    def snapshot(self) -> Dict[str, Dict[str, float | int | None]]:
        with self._lock:
            copied = {name: list(values) for name, values in self._values.items()}
        result: Dict[str, Dict[str, float | int | None]] = {}
        for name in sorted(INTERVIEW_AGENT_METRICS):
            values = sorted(copied.get(name, []))
            result[name] = {
                "count": len(values),
                "p50": self._percentile(values, 0.50),
                "p95": self._percentile(values, 0.95),
                "max": values[-1] if values else None,
            }
        return result

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float | None:
        if not values:
            return None
        index = max(0, math.ceil(len(values) * quantile) - 1)
        return round(values[index], 3)


_METRICS = InterviewAgentMetrics()


def interview_agent_metrics() -> InterviewAgentMetrics:
    return _METRICS


def observe_interview_agent_metric(name: str, value: float) -> None:
    """Best-effort internal observations can never change interview outcomes."""

    try:
        interview_agent_metrics().observe(name, value)
    except Exception:
        # No dynamic labels, exception messages, provider text or identifiers.
        # Telemetry failure must not mask the original failure or cancellation.
        pass


@contextmanager
def measure_interview_agent_stage(
    name: str,
    *,
    time_source: Callable[[], float] = perf_counter,
) -> Iterator[None]:
    """Measure one awaited stage, including failed/cancelled attempts.

    This synchronous context manager intentionally spans ``await`` without
    scheduling another task. It only records a bounded, fixed-vocabulary
    duration; it cannot prolong a critical path or turn an error into success.
    Counter names and client-supplied names are not valid stage durations.
    """

    started = None
    if name in INTERVIEW_AGENT_STAGE_METRICS:
        try:
            started = time_source()
        except Exception:
            pass
    try:
        yield
    finally:
        if started is not None:
            try:
                elapsed_ms = max(0.0, (time_source() - started) * 1000)
                observe_interview_agent_metric(name, elapsed_ms)
            except Exception:
                pass
