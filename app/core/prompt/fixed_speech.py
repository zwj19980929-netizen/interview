"""Versioned, exact-match speech that contains no candidate information.

Only these complete texts may enter the fixed PCM cache. Dynamic questions,
explanations and candidate-derived text always use the ordinary speech path.
"""

from dataclasses import dataclass
from typing import Optional

from app.core.prompt.candidate_exit import (
    EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION,
)
from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION


WARMUP_CONFIRMATION_SPEECH_VERSION = "warmup_confirmation.v1"
WARMUP_CONFIRMATION_SPEECH = "我已经形成了试音字幕。请确认是否听写正确；不正确可以立即重试。"


@dataclass(frozen=True)
class FixedSpeech:
    id: str
    version: str
    text: str


FIXED_SPEECH_CATALOG = (
    FixedSpeech("warmup_confirmation", WARMUP_CONFIRMATION_SPEECH_VERSION, WARMUP_CONFIRMATION_SPEECH),
    FixedSpeech("supplement.check", SUPPLEMENT_SPEECH_VERSION, SUPPLEMENT_SPEECH["check"]),
    FixedSpeech("candidate_exit_confirmation", EXIT_CONFIRMATION_SPEECH_VERSION, EXIT_CONFIRMATION_SPEECH),
    FixedSpeech("supplement.continue", SUPPLEMENT_SPEECH_VERSION, SUPPLEMENT_SPEECH["continue"]),
    FixedSpeech("supplement.clarify", SUPPLEMENT_SPEECH_VERSION, SUPPLEMENT_SPEECH["clarify"]),
)


def fixed_speech_for_text(text: str) -> Optional[FixedSpeech]:
    # Do not normalize, interpolate or classify candidate content into a key.
    return next((item for item in FIXED_SPEECH_CATALOG if item.text == text), None)
