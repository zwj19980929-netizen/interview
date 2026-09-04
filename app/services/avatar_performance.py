"""Provider-independent text/phoneme alignment for the local VRM performer."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

from pypinyin import Style, lazy_pinyin

from app.core.ids import new_id
from app.domain.interview_agent import AvatarPerformance, GestureCue, VisemeCue
from app.domain.avatar_asset import REQUIRED_VRM_VISEMES


SUPPORTED_VISEMES = REQUIRED_VRM_VISEMES
_SHAPES = SUPPORTED_VISEMES[1:]
_LATIN = {
    "a": "aa", "e": "E", "i": "ih", "o": "oh", "u": "ou",
    "b": "PP", "p": "PP", "m": "PP", "f": "FF", "v": "FF",
    "t": "DD", "d": "DD", "k": "kk", "g": "kk", "c": "CH",
    "s": "SS", "z": "SS", "n": "nn", "l": "nn", "r": "RR",
}
_PINYIN_INITIALS = (
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w",
)
_INITIAL_VISEME = {
    "b": "PP", "p": "PP", "m": "PP", "f": "FF",
    "d": "DD", "t": "DD", "n": "nn", "l": "nn",
    "g": "kk", "k": "kk", "h": "kk",
    "j": "CH", "q": "CH", "x": "SS",
    "zh": "CH", "ch": "CH", "sh": "SS",
    "r": "RR", "z": "SS", "c": "SS", "s": "SS",
    "y": "ih", "w": "ou",
}
_LATIN_CLUSTERS = {
    "th": "TH", "ph": "FF", "ch": "CH", "sh": "SS", "ng": "nn",
    "ck": "kk", "qu": "kk", "wh": "ou",
}


class AvatarPerformanceComposer:
    """Turns approved speech into clocked visemes without leaking policy into UI."""

    def compose(
        self,
        text: str,
        *,
        turn_id: Optional[str],
        audio_uri: Optional[str] = None,
        audio_duration_ms: Optional[int] = None,
        provider_visemes: Optional[Iterable[dict]] = None,
        gesture: str = "look_at_candidate",
        delivery: str = "cascade",
    ) -> AvatarPerformance:
        normalized = str(text).strip()
        if not normalized:
            raise ValueError("approved avatar speech cannot be empty")
        provider_timed = bool(provider_visemes)
        visemes = self._provider_cues(provider_visemes) if provider_timed else self._align(normalized)
        if audio_duration_ms and not provider_timed:
            visemes = self._scale_to_duration(visemes, int(audio_duration_ms))
        duration = (
            int(audio_duration_ms)
            if audio_duration_ms
            else max(cue.at_ms + cue.duration_ms for cue in visemes)
        )
        gestures: List[GestureCue] = [
            GestureCue(at_ms=0, duration_ms=duration, gesture="breathe", intensity=0.35),
            GestureCue(at_ms=0, duration_ms=duration, gesture=gesture, intensity=0.7),
        ]
        if "？" in normalized or "?" in normalized:
            gestures.append(
                GestureCue(at_ms=max(0, duration - 420), duration_ms=420, gesture="nod", intensity=0.45)
            )
        return AvatarPerformance(
            performance_id=new_id("performance"),
            turn_id=turn_id,
            audio_uri=audio_uri,
            audio_clock_origin_ms=0,
            text=normalized,
            visemes=visemes,
            gestures=gestures,
            alignment_source=(
                "provider_timestamp" if provider_timed else "g2p_estimate"
            ),
            delivery=delivery,
            interruptible=True,
        )

    @staticmethod
    def _provider_cues(values: Iterable[dict]) -> List[VisemeCue]:
        cues = [VisemeCue.model_validate(item) for item in values]
        if not cues:
            raise ValueError("provider viseme timing was empty")
        return cues

    @staticmethod
    def _align(text: str) -> List[VisemeCue]:
        cues: List[VisemeCue] = [VisemeCue(at_ms=0, duration_ms=55, shape="sil", weight=0)]
        cursor = 55
        for token in re.findall(r"[A-Za-z0-9+#._-]+|[\u3400-\u9fff]+|[^\s]", text):
            if token.isspace():
                cursor += 70
                continue
            if re.fullmatch(r"[，。！？、,.!?;；:：]", token):
                cues.append(VisemeCue(at_ms=cursor, duration_ms=90, shape="sil", weight=0))
                cursor += 90
                continue
            shapes = (
                AvatarPerformanceComposer._latin_shapes(token)
                if token.isascii()
                else AvatarPerformanceComposer._mandarin_shapes(token)
            )
            for shape, duration in shapes:
                cues.append(
                    VisemeCue(
                        at_ms=cursor,
                        duration_ms=duration,
                        shape=shape,
                        weight=0.82,
                    )
                )
                cursor += duration
        cues.append(VisemeCue(at_ms=cursor, duration_ms=70, shape="sil", weight=0))
        return cues

    @staticmethod
    def _mandarin_shapes(text: str) -> List[tuple[str, int]]:
        """Convert Mandarin words to initials/finals with phrase-aware pinyin."""

        syllables = lazy_pinyin(
            text,
            style=Style.NORMAL,
            strict=False,
            errors=lambda value: list(value),
        )
        result: List[tuple[str, int]] = []
        for raw in syllables:
            syllable = re.sub(r"[^a-zv]", "", str(raw).casefold().replace("ü", "v"))
            if not syllable:
                continue
            initial = next(
                (item for item in _PINYIN_INITIALS if syllable.startswith(item)),
                "",
            )
            final = syllable[len(initial):] or syllable
            if initial:
                result.append((_INITIAL_VISEME[initial], 50))
            result.append((AvatarPerformanceComposer._vowel_viseme(final), 85))
        return result or [("sil", 90)]

    @staticmethod
    def _latin_shapes(text: str) -> List[tuple[str, int]]:
        value = text.casefold()
        result: List[tuple[str, int]] = []
        index = 0
        while index < len(value):
            pair = value[index:index + 2]
            if pair in _LATIN_CLUSTERS:
                result.append((_LATIN_CLUSTERS[pair], 58))
                index += 2
                continue
            unit = value[index]
            if unit in "aeiouy":
                result.append((AvatarPerformanceComposer._vowel_viseme(unit), 78))
            elif unit in _LATIN:
                result.append((_LATIN[unit], 58))
            elif unit.isdigit():
                # Numbers are spoken by TTS as syllables; alternate a neutral
                # consonant/vowel pair instead of inventing amplitude lipsync.
                result.extend((("DD", 46), ("E", 68)))
            index += 1
        return result or [("sil", 70)]

    @staticmethod
    def _vowel_viseme(final: str) -> str:
        value = final.casefold()
        if "a" in value:
            return "aa"
        if "o" in value:
            return "oh"
        if "e" in value:
            return "E"
        if "i" in value or value.startswith("y"):
            return "ih"
        if "u" in value or "v" in value or value.startswith("w"):
            return "ou"
        return "E"

    @staticmethod
    def _scale_to_duration(
        cues: List[VisemeCue], duration_ms: int
    ) -> List[VisemeCue]:
        target = max(120, duration_ms)
        source = max(cue.at_ms + cue.duration_ms for cue in cues)
        if source <= 0 or source == target:
            return cues
        scale = target / source
        scaled: List[VisemeCue] = []
        for cue in cues:
            at_ms = min(target - 1, max(0, round(cue.at_ms * scale)))
            cue_duration = max(1, round(cue.duration_ms * scale))
            cue_duration = min(cue_duration, target - at_ms)
            scaled.append(
                cue.model_copy(
                    update={"at_ms": at_ms, "duration_ms": cue_duration}
                )
            )
        return scaled
