import json
import logging

from app.core import speech_diagnostics


def test_budget_exhaustion_is_visible_without_raw_response_or_candidate_text(monkeypatch, caplog):
    logger = logging.getLogger("synthetic.budget_diagnostics")
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        speech_diagnostics.record_turn_control(
            "understanding_retry_budget", interview_id="synthetic", turn_id="turn",
            capture_id="capture", failures=3, exhausted=True, revision=500,
            response_body="private response", exception_body="private exception",
        )
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "turn_control.understanding_retry_budget"
    assert record["failures"] == 3 and record["exhausted"] is True
    assert record["revision"] == 500
    assert "private response" not in caplog.text and "private exception" not in caplog.text


def test_reception_idle_and_followup_diagnostics_keep_only_metadata(monkeypatch, caplog):
    logger = logging.getLogger("synthetic.conversation_diagnostics")
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        for event, fields in [
            ("reception_selected", {"kind": "wait", "revision": 5}),
            ("reception_unavailable", {"error_type": "TimeoutError"}),
            ("idle_check", {"phase": "listening", "reason": "initial"}),
            ("followup_decided", {"selected": False, "reason": "budget", "target_count": 2,
                                   "max_depth": 1, "max_per_root": 1, "max_total": 8,
                                   "min_remaining_seconds": 30, "prompt_version": "synthetic"}),
        ]:
            speech_diagnostics.record_turn_control(event, interview_id="synthetic", turn_id="turn",
                                                   text="private speech", response_body="private response", **fields)
    assert len(caplog.records) == 4
    for record in caplog.records:
        parsed = json.loads(record.message)
        assert "text" not in parsed and "response_body" not in parsed
    assert "private speech" not in caplog.text and "private response" not in caplog.text
