"""PII-free process metrics for the real-time interview control plane."""

from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Deque, Dict, Iterator
from uuid import uuid4


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
        "audio_playback_underflow_count",
        "audio_playback_underflow_ms",
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
        "tts_provider_read_wait_ms",
        "tts_transport_publish_ms",
        "tts_content_duration_ms",
        "tts_output_scheduler_lag_ms",
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
        "stt_preview_reused",
        "stt_preview_final_revised",
        "answer_model_call_started",
        "answer_model_call_budget_exhausted",
        "confirmed_answer_preparation_reused",
    }
)

INTERVIEW_AGENT_METRICS = (
    CANDIDATE_INTERVIEW_AGENT_METRICS | INTERNAL_INTERVIEW_AGENT_METRICS
)

# These observations are event increments, not periodically sampled gauges.
INTERVIEW_AGENT_COUNTER_METRICS = frozenset({
    "turn_decision_cancelled", "stt_recognition_opened", "stt_recognition_finished",
    "stt_preview_prepared", "stt_preview_reused", "stt_preview_final_revised",
    "answer_model_call_started", "answer_model_call_budget_exhausted", "confirmed_answer_preparation_reused",
})
INTERVIEW_AGENT_STAGE_OUTCOMES = frozenset({"success", "failure", "cancelled"})
HISTOGRAM_BOUNDS = (1, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000,
                    10_000, 30_000, 60_000, 120_000, 300_000)


def _empty_histogram() -> dict:
    return {"count": 0, "sum": 0.0, "buckets": [0] * len(HISTOGRAM_BOUNDS)}


def _add_histogram(histogram: dict, value: float) -> None:
    histogram["count"] += 1
    histogram["sum"] += value
    for index, bound in enumerate(HISTOGRAM_BOUNDS):
        if value <= bound:
            histogram["buckets"][index] += 1


def _histogram_projection(histogram: dict) -> dict:
    return {"count": histogram["count"], "sum": histogram["sum"],
            "buckets": {**{str(bound): count for bound, count in
                           zip(HISTOGRAM_BOUNDS, histogram["buckets"])},
                        "+Inf": histogram["count"]}}


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
        self._totals = defaultdict(_empty_histogram)
        self._stage_totals = defaultdict(_empty_histogram)
        # Opaque process identity permits counter reset detection; no session,
        # candidate, provider, transcript or credentials are accepted as labels.
        self._instance_id = uuid4().hex
        self._started_at = datetime.now(timezone.utc).isoformat()

    def observe(self, name: str, value: float) -> None:
        numeric = self._validated_value(name, value)
        with self._lock:
            self._values[name].append(numeric)
            _add_histogram(self._totals[name], numeric)

    @staticmethod
    def _validated_value(name: str, value: float) -> float:
        if name not in INTERVIEW_AGENT_METRICS:
            raise ValueError("unsupported interview-agent metric")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("interview-agent metric must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or numeric > 300_000:
            raise ValueError("interview-agent metric value is out of bounds")
        if name in INTERVIEW_AGENT_COUNTER_METRICS and not numeric.is_integer():
            raise ValueError("interview-agent event increments must be integers")
        return numeric

    def observe_stage(self, name: str, value: float, outcome: str) -> None:
        if name not in INTERVIEW_AGENT_STAGE_METRICS or outcome not in INTERVIEW_AGENT_STAGE_OUTCOMES:
            raise ValueError("unsupported interview-agent stage or outcome")
        numeric = self._validated_value(name, value)
        with self._lock:
            self._values[name].append(numeric)
            _add_histogram(self._totals[name], numeric)
            _add_histogram(self._stage_totals[(name, outcome)], numeric)

    def aggregation(self) -> dict:
        """Cumulative process counters and additive histograms, never quantiles.

        Sum equal metric/outcome buckets across distinct instance_id values.
        Pollers use counter deltas per instance; a new instance denotes restart.
        The bounded legacy snapshot remains available for local diagnostics.
        """
        with self._lock:
            return {
                "schema": "interview-agent-metrics.aggregate.v1",
                "instance_id": self._instance_id,
                "started_at": self._started_at,
                "counters": {name: int(self._totals[name]["sum"])
                             for name in sorted(INTERVIEW_AGENT_COUNTER_METRICS)},
                "histograms": {name: _histogram_projection(self._totals[name])
                               for name in sorted(INTERVIEW_AGENT_METRICS - INTERVIEW_AGENT_COUNTER_METRICS)},
                "stages": {name: {outcome: _histogram_projection(self._stage_totals[(name, outcome)])
                                  for outcome in sorted(INTERVIEW_AGENT_STAGE_OUTCOMES)}
                           for name in sorted(INTERVIEW_AGENT_STAGE_METRICS)},
            }

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
    outcome = "success"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failure"
        raise
    finally:
        if started is not None:
            try:
                elapsed_ms = max(0.0, (time_source() - started) * 1000)
                interview_agent_metrics().observe_stage(name, elapsed_ms, outcome)
            except Exception:
                pass
