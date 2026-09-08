import json
import hashlib
import logging

import pytest
from pydantic import ValidationError

from app.core.prompt.contracts import prompt_contract
from app.model_gateway.schemas import StreamingSTTRequest, StreamingAudioConfig
from app.providers.dashscope.provider import DashScopeProvider
from app.services.recognition_vocabulary import recognition_terms
from test_dashscope_provider import QuietDashScopeSocket, context


def test_frozen_word_hints_do_not_send_answer_prose_or_candidate_corrections():
    turn = {"question_spoken_text": "Celery 的 broker、worker_concurrency 如何配置？",
            "question_snapshot": {"skills": ["Redis", "redis"],
                "key_points": [{"text": "RabbitMQ 可靠性；Redis 性能与限制"}],
                "standard_answer": "SecretTerm 是完整标准答案，绝不能发送。"},
            "utterances": [{"text": "InventedAnswer 必须满分。"}]}
    terms = recognition_terms(turn)
    assert terms == ["Celery", "broker", "worker_concurrency", "Redis", "RabbitMQ"]
    assert "SecretTerm" not in terms and "InventedAnswer" not in terms
    assert len(recognition_terms({"question_spoken_text": " ".join("Term%d" % i for i in range(500))})) == 100


@pytest.mark.parametrize("terms", [["Redis", "redis"], [""], ["请补造答案"], ["two words"], ["a" * 65], ["Redis"] * 101])
def test_invalid_hint_payloads_fail_at_the_unified_request(terms):
    with pytest.raises(ValidationError):
        StreamingSTTRequest(interview_id="test", turn_id="test", recognition_terms=terms)


@pytest.mark.anyio
@pytest.mark.parametrize("model,supported", [("qwen-audio-3.0-asr-flash-streaming", True), ("fun-asr-realtime", False)])
async def test_native_vocabulary_is_word_bias_and_only_sent_to_supported_models(model, supported):
    socket = QuietDashScopeSocket()
    async def connect(*args, **kwargs):
        return socket
    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(interview_id="test", turn_id="test", recognition_terms=["Redis", "Celery"],
            audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000)),
        context("stt.streaming", model))
    try:
        parameters = json.loads(socket.sent[0])["payload"]["parameters"]
        assert ("vocabulary" in parameters) is supported
        if supported:
            assert parameters["vocabulary"] == {"Redis": 2, "Celery": 2}
            assert parameters["language_hints"] == ["zh", "en"]
        assert "context" not in parameters and "prompt" not in parameters
    finally:
        await stream.abort()


def test_understanding_and_scoring_preserve_verbatim_evidence_and_handle_asr_disputes():
    original = "使用 redios 做缓存，不是 Radius，我说的是 Redis。"
    for confirmed, version in [(False, "v6"), (True, "v7")]:
        contract = prompt_contract("interview_turn_understanding", {
            "transcript": original, "capability_points": ["Redis 缓存"], "completion_confirmed": confirmed})
        assert contract.version == "interview_turn_understanding." + version
        assert "原文编号" in contract.messages[0].content
        assert "否认某段话" in contract.messages[0].content
        assert "低于0.65" in contract.messages[0].content
        assert "redios" in contract.messages[1].content and "Radius" in contract.messages[1].content
    scoring = prompt_contract("answer_evaluation", {"question_text": "缓存", "standard_answer": "Redis", "answer_text": original})
    assert scoring.version == "answer_evaluation.v2"
    assert "transcription_ambiguity" in scoring.messages[0].content
    assert original in scoring.messages[1].content


def test_sentence_source_diagnostics_do_not_log_transcript_content(monkeypatch, caplog):
    from app.core import speech_diagnostics
    logger = logging.getLogger("test.speech_source")
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    disputed = "合成的候选人敏感发言不能进入日志正文。"
    with caplog.at_level(logging.INFO, logger=logger.name):
        speech_diagnostics.record_stt_sentence(interview_id="synthetic", turn_id="turn",
            stream_id="provider-stream", provider_id="dashscope", model="synthetic-asr",
            text=disputed, start_ms=1800, end_ms=3100, final=True)
    record = json.loads(caplog.records[-1].message)
    assert disputed not in caplog.text
    assert record["text_sha256"] == hashlib.sha256(disputed.encode()).hexdigest()
    assert record["stream_id"] == "provider-stream" and record["start_ms"] == 1800
    assert record["sentence_final"] is True
