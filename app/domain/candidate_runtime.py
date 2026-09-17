"""Public runtime diagnostics contain fixed codes, never raw browser errors."""

from typing import Literal


CandidateRuntimeProblemCode = Literal[
    "AVATAR_ASSET_UNAVAILABLE", "AVATAR_MODEL_LOAD_FAILED", "AVATAR_RENDERER_FAILED",
    "CANDIDATE_RUNTIME_FAILED", "AUDIO_TRACK_LOST", "AUDIO_STREAM_INVALID",
    "AUDIO_PLAYBACK_FAILED", "AUDIO_PLAYBACK_TIMEOUT", "AUDIO_OUTPUT_UNAVAILABLE",
]

CANDIDATE_RUNTIME_PROBLEM_REASONS = {
    "AVATAR_ASSET_UNAVAILABLE": "candidate_avatar_asset_unavailable",
    "AVATAR_MODEL_LOAD_FAILED": "candidate_avatar_model_load_failed",
    "AVATAR_RENDERER_FAILED": "candidate_avatar_renderer_failed",
    "CANDIDATE_RUNTIME_FAILED": "candidate_runtime_failed",
    "AUDIO_TRACK_LOST": "candidate_audio_track_lost",
    "AUDIO_STREAM_INVALID": "candidate_audio_stream_invalid",
    "AUDIO_PLAYBACK_FAILED": "candidate_audio_playback_failed",
    "AUDIO_PLAYBACK_TIMEOUT": "candidate_audio_playback_timeout",
    "AUDIO_OUTPUT_UNAVAILABLE": "candidate_audio_output_unavailable",
}


def runtime_pause_epoch(session: dict) -> str:
    """Event identity distinguishes two pauses within the same clock second."""
    return next((event["id"] for event in reversed(session.get("lifecycle_events", []))
                 if event.get("type") in {"interview.paused", "interview.timed_out"}),
                (session.get("interruption") or {}).get("occurred_at", "legacy_paused"))
