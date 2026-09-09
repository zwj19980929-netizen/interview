"""Source/timing diagnostics without transcript content or credentials."""
import hashlib
import json
import logging

from app.core.time import utc_now

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


def record_turn_control(event: str, *, interview_id: str, turn_id: str,
                        capture_id: str = None, **details) -> None:
    """Explicit safe fields; arbitrary exception/response bodies never escape."""
    if event not in {"input_invalidated", "supplement_decided", "proposal_rejected", "audio_playback",
                     "understanding_retry_budget"}:
        return
    safe = {key: details[key] for key in (
        "revision", "phase", "source", "stage", "cause_code", "intent", "confidence",
        "performance_id", "status", "failures", "exhausted",
    ) if key in details}
    if isinstance(details.get("text"), str):
        safe.update(text_sha256=hashlib.sha256(details["text"].encode()).hexdigest(),
                    text_chars=len(details["text"]))
    _LOG.info(json.dumps({"event": "turn_control." + event, "at": utc_now(),
        "interview_id": interview_id, "turn_id": turn_id, "capture_id": capture_id,
        **safe}, ensure_ascii=True))
