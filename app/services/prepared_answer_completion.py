"""Conservative evidence checks for reusing a prepared technical answer."""
import re


def pure_completion_reply(text):
    """Recognize only closed control phrases after semantic finish classification.

    This is a reuse eligibility check, never an intent classifier or permission
    to complete. Unknown wording retains the normal full-understanding path.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 100:
        return False
    compact = re.sub(r"[\s，。！？、,.!?；;：:]", "", text).casefold()
    prefix = r"(?:(?:嗯|呃|啊|哦|好|好的|那|我的|我|这边|已经|现在))*"
    finish = r"(?:没有(?:什么|其他|其它|更多)?(?:要|需要)?补充(?:的)?(?:了)?|没(?:有)?补充(?:了)?|没有了|没了|没有|(?:回答|介绍|说|讲)(?:已经)?完(?:毕)?(?:了)?|就这些(?:了)?|就这样(?:了)?)"
    return re.fullmatch(r"(?:" + prefix + finish + r"(?:谢谢)?){1,4}", compact) is not None


def unchanged_answer_prefix(base, current, reply):
    """Keep original source segments, timing and confidence on a new final.

    Continuous capture rotates provider requests. A new request ID is expected,
    while the adapter/model/language and exact old segments must remain stable.
    Missing segment alignment is a miss; it cannot erase quality or old words.
    """
    before, after = str(base["final_transcript"]).strip(), str(current["final_transcript"]).strip()
    if (not before or not after.startswith(before) or after[len(before):].strip() != reply
            or not pure_completion_reply(reply) or base.get("language") != current.get("language")
            or base.get("transcript_source") != "server_streaming"
            or current.get("transcript_source") != "server_streaming"):
        return False
    old_provider, provider = base.get("stt_provider") or {}, current.get("stt_provider") or {}
    if any(not old_provider.get(key) or old_provider.get(key) != provider.get(key)
           for key in ("provider_id", "model")):
        return False
    old_segments, segments = base.get("transcript_segments") or [], current.get("transcript_segments") or []
    if (not old_segments or len(segments) <= len(old_segments) or segments[:len(old_segments)] != old_segments
            or "".join(item["text"] for item in old_segments).strip() != before
            or "".join(item["text"] for item in segments).strip() != after):
        return False
    old_confidence, confidence = base.get("stt_confidence"), current.get("stt_confidence")
    if old_confidence is not None and (confidence is None or confidence > old_confidence):
        return False
    if confidence is not None and confidence < .75:
        return False
    end_ms = old_segments[-1]["end_ms"]
    for segment in segments[len(old_segments):]:
        if (segment["start_ms"] < end_ms or segment["end_ms"] < segment["start_ms"]
                or (segment.get("confidence") is not None and segment["confidence"] < .75)):
            return False
        end_ms = segment["end_ms"]
    return True
