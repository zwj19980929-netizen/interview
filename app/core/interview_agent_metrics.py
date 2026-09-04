"""PII-free process metrics for the real-time interview control plane."""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Deque, Dict


INTERVIEW_AGENT_METRICS = frozenset(
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
