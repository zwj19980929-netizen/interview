"""Synthetic ASR hypotheses have bounded, content-free source diagnostics."""
import hashlib
import json
import logging

import pytest

from app.core import speech_diagnostics
from app.model_gateway.errors import ProviderError
from test_stt_stable_preview_providers import open_dashscope, sentence


@pytest.fixture
def diagnostic_rows(monkeypatch):
    rows = []

    class Logger:
        def info(self, value):
            rows.append(json.loads(value))

    monkeypatch.setattr(speech_diagnostics, "_LOG", Logger())
    return rows


def hypotheses(rows):
    return [row for row in rows if row["event"] == "stt.hypothesis_source"]


@pytest.mark.anyio
async def test_first_hypothesis_and_corrected_final_link_without_logging_text(diagnostic_rows):
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=0, text="对")
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=50, end_time=400, text="合成的缓存回答")
        final = dict(sentence_id=1, sentence_end=True, begin_time=50, end_time=500, text="合成的缓存回答。")
        await sentence(socket, **final)
        await sentence(socket, **final)
        assert (await stream.preview()).text == final["text"]
        events = await stream.finish()
        assert next(item for item in events if item.type == "transcript.final").text == final["text"]
        rows = hypotheses(diagnostic_rows)
        assert [row["disposition"] for row in rows] == ["first_nonempty", "replaced"]
        assert rows[0]["text_sha256"] == hashlib.sha256("对".encode()).hexdigest()
        assert rows[1]["first_text_sha256"] == rows[0]["text_sha256"]
        assert rows[1]["previous_text_sha256"] == hashlib.sha256("合成的缓存回答".encode()).hexdigest()
        assert rows[1]["sentence_id"] == 1 and rows[1]["revision"] == 3
        assert rows[1]["sentence_final"] is True
        assert rows[1]["start_ms"] == 50 and rows[1]["end_ms"] == 500
        serialized = json.dumps(diagnostic_rows, ensure_ascii=False)
        assert "合成的缓存回答" not in serialized and '"对"' not in serialized
        assert "synthetic-not-real" not in serialized
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("sentence_final", [False, True])
async def test_empty_replacement_records_retraction_without_inventing_transcript(diagnostic_rows, sentence_final):
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=0, text="对")
        await sentence(socket, sentence_id=1, sentence_end=sentence_final, begin_time=0, end_time=300, text="")
        if sentence_final:
            events = await stream.finish()
            assert [event.type for event in events][-2:] == ["transcript.empty", "stream.closed"]
            assert stream.empty_completion_reconciled
            assert not any(event.type == "transcript.final" for event in events)
        else:
            with pytest.raises(ProviderError, match="no final transcript"):
                await stream.finish()
            assert not stream.empty_completion_reconciled
        rows = hypotheses(diagnostic_rows)
        assert [row["disposition"] for row in rows] == ["first_nonempty", "retracted"]
        assert rows[-1]["text_chars"] == 0 and rows[-1]["revision"] == 2
        assert rows[-1]["sentence_final"] is sentence_final
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_task_finish_keeps_last_unfinalized_hypothesis_and_ignores_empty_start(diagnostic_rows):
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_begin=True, sentence_end=False, begin_time=0, text="")
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=20, end_time=400, text="对")
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=20, end_time=450, text="合成修正")
        events = await stream.finish()
        assert next(item for item in events if item.type == "transcript.final").text == "合成修正"
        rows = hypotheses(diagnostic_rows)
        assert [row["disposition"] for row in rows] == ["first_nonempty", "task_finished"]
        assert rows[-1]["sentence_final"] is False and rows[-1]["revision"] == 2
        assert rows[-1]["text_sha256"] == hashlib.sha256("合成修正".encode()).hexdigest()
    finally:
        await stream.abort()


def test_long_stream_coalesces_interim_updates_and_bounds_sentence_memory(diagnostic_rows):
    tracker = speech_diagnostics.STTHypothesisDiagnostics(interview_id="synthetic", turn_id="turn",
        stream_id="stream", provider_id="dashscope", model="synthetic", max_sentences=8)
    for revision in range(1000):
        tracker.observe(("id", 1), text=f"合成版本{revision}", start_ms=0, end_ms=revision, final=False)
    assert len(hypotheses(diagnostic_rows)) == 1
    tracker.observe(("id", 1), text="终稿", start_ms=0, end_ms=1000, final=True)
    for identity in range(2, 1002):
        tracker.observe(("id", identity), text="长流合成终稿", start_ms=identity * 1000,
                        end_ms=identity * 1000 + 900, final=True)
        assert len(tracker._active) <= 8 and len(tracker._settled) <= 8
    count = len(diagnostic_rows)
    tracker.observe(("id", 1), text="终稿", start_ms=0, end_ms=1000, final=True)
    tracker.finish()
    assert len(diagnostic_rows) == count
    assert "合成版本" not in repr(vars(tracker))


def test_open_sentence_overflow_is_explicit_and_bounded(diagnostic_rows):
    tracker = speech_diagnostics.STTHypothesisDiagnostics(interview_id="synthetic", turn_id="turn",
        stream_id="stream", provider_id="dashscope", model="synthetic", max_sentences=4)
    for identity in range(1, 10):
        tracker.observe(("id", identity), text="合成初稿", start_ms=0, end_ms=None, final=False)
        assert len(tracker._active) <= 4
    assert len([row for row in hypotheses(diagnostic_rows) if row["disposition"] == "tracking_evicted"]) == 5
    tracker.finish()
    tracker.finish()
    assert len(hypotheses(diagnostic_rows)) == 18


@pytest.mark.anyio
async def test_broken_log_sink_cannot_fail_recognition(monkeypatch):
    stream, socket = await open_dashscope()
    try:
        monkeypatch.setattr(speech_diagnostics, "_LOG", logging.getLogger("synthetic.hypothesis"))
        monkeypatch.setattr(speech_diagnostics._LOG, "info", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic sink failed")))
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=0, end_time=200, text="合成初稿")
        # Existing final-source logging is separate; the new optional tracker
        # must not introduce another failure path before that established seam.
        assert stream._reader_error is None
        assert stream._transcript_projection()[0] == "合成初稿"
    finally:
        await stream.abort()
