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
