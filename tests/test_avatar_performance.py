import pytest
from pydantic import ValidationError

from app.model_gateway.schemas import ProviderMeta, TTSSynthesizeResponse
from app.services.avatar_performance import AvatarPerformanceComposer


def test_mandarin_g2p_uses_phrase_pronunciation_and_technical_english() -> None:
    performance = AvatarPerformanceComposer().compose(
        "重庆 API 幂等键",
        turn_id="turn_1",
        audio_uri="private-file://speech_1",
    )
    shapes = [cue.shape for cue in performance.visemes]

    # “重庆” is phrase-resolved as chong-qing rather than a codepoint-derived
    # mouth shape; mixed English and Mandarin share the same 15-viseme clock.
    assert shapes[:5] == ["sil", "CH", "oh", "CH", "ih"]
    assert "PP" in shapes  # mi
    assert "DD" in shapes  # deng
    assert "aa" in shapes  # jian final
    assert [cue.at_ms for cue in performance.visemes] == sorted(
        cue.at_ms for cue in performance.visemes
    )


def test_local_g2p_cues_scale_to_the_authoritative_audio_clock() -> None:
    performance = AvatarPerformanceComposer().compose(
        "请介绍你的幂等设计",
        turn_id="turn_1",
        audio_uri="private-file://speech_1",
        audio_duration_ms=2_400,
    )

    end_ms = max(cue.at_ms + cue.duration_ms for cue in performance.visemes)
    assert abs(end_ms - 2_400) <= 1
    assert all(cue.duration_ms > 0 for cue in performance.visemes)
    assert all(cue.at_ms + cue.duration_ms <= 2_400 for cue in performance.visemes)
    assert all(gesture.duration_ms == 2_400 for gesture in performance.gestures[:2])
    assert performance.alignment_source == "g2p_estimate"


def test_provider_viseme_timestamps_win_over_local_alignment() -> None:
    performance = AvatarPerformanceComposer().compose(
        "动态追问",
        turn_id="turn_2",
        audio_uri="private-file://speech_2",
        audio_duration_ms=900,
        provider_visemes=[
            {"at_ms": 0, "duration_ms": 80, "shape": "sil", "weight": 0},
            {"at_ms": 80, "duration_ms": 120, "shape": "CH", "weight": 0.8},
            {"at_ms": 200, "duration_ms": 120, "shape": "ou", "weight": 0.8},
        ],
    )

    assert [cue.at_ms for cue in performance.visemes] == [0, 80, 200]
    assert performance.gestures[0].duration_ms == 900
    assert performance.alignment_source == "provider_timestamp"


def test_tts_provider_visemes_are_typed_monotonic_and_within_audio_clock() -> None:
    provider = ProviderMeta(
        provider_id="tts_test", model="voice", request_id="request_1", latency_ms=1
    )
    with pytest.raises(ValidationError):
        TTSSynthesizeResponse(
            audio_uri="private-file://speech",
            content_type="audio/wav",
            duration_ms=300,
            content_hash="sha256:test",
            visemes=[
                {"at_ms": 240, "duration_ms": 80, "shape": "aa", "weight": 1}
            ],
            provider=provider,
        )
    with pytest.raises(ValidationError):
        TTSSynthesizeResponse(
            audio_uri="private-file://speech",
            content_type="audio/wav",
            duration_ms=500,
            content_hash="sha256:test",
            visemes=[
                {"at_ms": 200, "duration_ms": 40, "shape": "aa", "weight": 1},
                {"at_ms": 100, "duration_ms": 40, "shape": "E", "weight": 1},
            ],
            provider=provider,
        )
