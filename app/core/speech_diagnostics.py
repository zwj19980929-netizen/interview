"""Source/timing diagnostics without transcript content or credentials."""
import hashlib
import json
import logging
from collections import OrderedDict

from app.core.time import utc_now
from app.core.model_diagnostics import SUPPLEMENT_DIAGNOSTIC_SCHEMA_PATHS, safe_model_diagnostic_fields

_LOG = logging.getLogger("interviewer.speech_evidence")


def configure_speech_diagnostics() -> None:
    if not _LOG.handlers:
        _LOG.addHandler(logging.StreamHandler())
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False


def record_stt_audio_origin(stream, *, start_byte: int, bytes_per_second: int) -> None:
    request = stream.request
    _LOG.info(json.dumps({"event": "stt.audio_origin", "at": utc_now(),
        "interview_id": request.interview_id, "turn_id": request.turn_id,
        "stream_id": stream.stream_id, "start_byte": start_byte,
        "bytes_per_second": bytes_per_second}, ensure_ascii=True))


def record_stt_sentence(*, interview_id: str, turn_id: str, stream_id: str,
                        provider_id: str, model: str, text: str,
                        start_ms: int, end_ms: int, final: bool) -> None:
    _LOG.info(json.dumps({
        "event": "stt.sentence_source", "at": utc_now(),
        "interview_id": interview_id, "turn_id": turn_id, "stream_id": stream_id,
        "provider_id": provider_id, "model": model,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_chars": len(text), "start_ms": start_ms, "end_ms": end_ms,
        "sentence_final": final,
    }, ensure_ascii=True))


class STTHypothesisDiagnostics:
    """Keep first/last provider hypothesis fingerprints, never transcript text.

    Interim revisions are coalesced without timers or network work. Identity
    eviction is explicit; no diagnostic limit may change recognition results.
    The adapter already rejects regressed unseen IDs and duplicate finals.
    """

    def __init__(self, *, interview_id, turn_id, stream_id, provider_id, model, max_sentences=64):
        self._source = {"interview_id": interview_id, "turn_id": turn_id, "stream_id": stream_id,
                        "provider_id": provider_id, "model": model}
        self._limit = max(1, min(64, int(max_sentences)))
        self._active = OrderedDict()
        self._settled = OrderedDict()
        self._retired = {"id": -1, "time": -1}
        self._finished = False

    def observe(self, key, *, text, start_ms, end_ms, final):
        # Missing identity cannot safely correlate two hypotheses. Existing
        # final-source diagnostics remain available for those legacy results.
        if (self._finished or not isinstance(key, tuple) or len(key) != 2
                or key[0] not in self._retired or type(key[1]) is not int or key[1] < 0
                or not isinstance(text, str) or type(final) is not bool):
            return
        if key in self._settled or (key not in self._active and key[1] <= self._retired[key[0]]):
            return
        current = {"text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "text_chars": len(text),
                   "start_ms": start_ms if type(start_ms) is int and start_ms >= 0 else None,
                   "end_ms": end_ms if type(end_ms) is int and end_ms >= 0 else None}
        state = self._active.get(key)
        if state is None:
            if not text:
                if final:
                    self._settle(key)
                return
            if len(self._active) >= self._limit:
                old_key, old = self._active.popitem(last=False)
                self._emit(old_key, old, final=False, disposition="tracking_evicted")
                self._settle(old_key)
            state = {"first_text_sha256": current["text_sha256"], "latest": current,
                     "previous_text_sha256": None, "revision": 1}
            self._active[key] = state
            if not final:
                self._emit(key, state, final=False, disposition="first_nonempty")
        elif current != state["latest"]:
            state["previous_text_sha256"] = state["latest"]["text_sha256"]
            state["latest"] = current
            state["revision"] += 1
        if final:
            disposition = ("retracted" if not text else "finalized"
                           if current["text_sha256"] == state["first_text_sha256"] else "replaced")
            self._emit(key, state, final=True, disposition=disposition)
            self._active.pop(key, None)
            self._settle(key)

    def finish(self, *, disposition="task_finished"):
        if self._finished or disposition not in {"task_finished", "aborted"}:
            return
        self._finished = True
        for key, state in self._active.items():
            outcome = "retracted" if disposition == "task_finished" and not state["latest"]["text_chars"] else disposition
            self._emit(key, state, final=False, disposition=outcome)
        self._active.clear()

    def _settle(self, key):
        self._settled[key] = None
        while len(self._settled) > self._limit:
            retired, _ = self._settled.popitem(last=False)
            self._retired[retired[0]] = max(self._retired[retired[0]], retired[1])

    def _emit(self, key, state, *, final, disposition):
        payload = {"event": "stt.hypothesis_source", "at": utc_now(), **self._source,
                   "sentence_id": key[1] if key[0] == "id" else None,
                   "sentence_time_key_ms": key[1] if key[0] == "time" else None,
                   "revision": state["revision"], "sentence_final": final,
                   "disposition": disposition, "first_text_sha256": state["first_text_sha256"],
                   "previous_text_sha256": state["previous_text_sha256"], **state["latest"]}
        try:
            _LOG.info(json.dumps(payload, ensure_ascii=True))
        except Exception:
            # Optional diagnostics cannot stop an otherwise healthy recognizer.
            pass


def record_turn_control(event: str, *, interview_id: str, turn_id: str,
                        capture_id: str = None, **details) -> None:
    """Explicit safe fields; arbitrary exception/response bodies never escape."""
    if event not in {"input_invalidated", "supplement_decided", "proposal_rejected", "audio_playback",
                     "understanding_retry_budget", "reception_selected", "reception_unavailable", "recovery_speech_unavailable",
                     "idle_check", "followup_decided", "handled_final_suppressed", "prepared_answer_reused",
                     "supplement_classification_failed"}:
        return
    if event == "supplement_classification_failed":
        # This previously discarded event carries only typed failure facts.
        # Do not reuse the broader speech trace's text/cause-code passthrough.
        safe = safe_model_diagnostic_fields({key: details.get(key) for key in (
            "revision", "diagnostic_scope_id", "parent_scope_id", "interaction_remaining_ms",
            "failure_reason", "failure_source", "schema_reason", "error_type", "validation_stage",
            "prompt_version", "retryable",
        )}, strict_failure_reasons=True)
        path = details.get("schema_path")
        if isinstance(path, str) and path in SUPPLEMENT_DIAGNOSTIC_SCHEMA_PATHS:
            safe["schema_path"] = path
        try:
            _LOG.info(json.dumps({"event": "turn_control." + event, "at": utc_now(),
                "interview_id": interview_id, "turn_id": turn_id, "capture_id": capture_id,
                **safe}, ensure_ascii=True))
        except Exception:
            pass
        return
    safe = {key: details[key] for key in (
        "revision", "phase", "source", "stage", "cause_code", "intent", "confidence",
        "performance_id", "status", "failures", "exhausted",
        "kind", "reason", "selected", "target_count", "error_type",
        "prompt_version", "max_depth", "max_per_root", "max_total", "min_remaining_seconds",
        "source_understanding_id", "current_understanding_id", "question_act_id",
    ) if key in details}
    safe.update(safe_model_diagnostic_fields({key: details.get(key) for key in (
        "diagnostic_scope_id", "parent_scope_id", "interaction_remaining_ms", "retryable",
    )}))
    if details.get("reason_code") in {
        "timeout", "unavailable", "invalid_result", "retry_exhausted", "non_retryable",
        "call_budget_exhausted", "provider_timeout", "provider_auth_failed", "provider_billing_required",
        "provider_rate_limited", "provider_circuit_open", "provider_unavailable",
    }:
        safe["reason_code"] = details["reason_code"]
    if isinstance(details.get("text"), str):
        safe.update(text_sha256=hashlib.sha256(details["text"].encode()).hexdigest(),
                    text_chars=len(details["text"]))
    try:
        _LOG.info(json.dumps({"event": "turn_control." + event, "at": utc_now(),
            "interview_id": interview_id, "turn_id": turn_id, "capture_id": capture_id,
            **safe}, ensure_ascii=True))
    except Exception:
        # Diagnostic delivery is optional; a sink failure cannot change the
        # answer's retry budget, cancellation or submission permissions.
        pass
