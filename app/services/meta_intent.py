"""Bounded bilingual meta-intent detection over authoritative transcripts.

The detector deliberately recognises only direct conversational controls.  It
does not classify arbitrary mentions of control phrases, so quoted examples,
code and third-person product descriptions cannot move the interview floor.
Every match carries an exact source span from the original transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Optional


@dataclass(frozen=True)
class MetaIntentMatch:
    intent: str
    action: str
    evidence: str
    start: int
    end: int


@dataclass(frozen=True)
class _Pattern:
    intent: str
    action: str
    expression: re.Pattern[str]
    ambiguous: bool = False
    pause: bool = False


_ZH_GAP = r"[\s,，、:：]*"
_ZH_DIRECT_BOUNDARY = r"(?<![\u3400-\u9fffA-Za-z0-9_])"
_PATTERNS = (
    _Pattern(
        "request_repeat",
        "repeat",
        re.compile(
            rf"(?:麻烦|劳驾|请|能不能|可以(?:请你)?|麻烦你)?{_ZH_GAP}"
            rf"(?P<evidence>(?:再说|重说|重读){_ZH_GAP}(?:"
            rf"(?:一遍|一下|下)(?:{_ZH_GAP}(?:刚才(?:的)?|这个|上一(?:个|道))?{_ZH_GAP}"
            r"(?:问题|题目|那句|内容))?|"
            rf"(?:刚才(?:的)?|这个|上一(?:个|道))?{_ZH_GAP}(?:问题|题目|那句|内容)))",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "request_repeat",
        "repeat",
        re.compile(
            rf"{_ZH_DIRECT_BOUNDARY}(?:麻烦|劳驾|请|能不能|可以(?:请你)?|麻烦你)?{_ZH_GAP}"
            rf"(?P<evidence>重复{_ZH_GAP}(?:一遍|一下|下|"
            rf"(?:刚才(?:的)?|这个|上一(?:个|道))?{_ZH_GAP}(?:问题|题目|那句|内容)))",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "request_repeat",
        "repeat",
        re.compile(
            rf"{_ZH_DIRECT_BOUNDARY}(?P<evidence>(?:我{_ZH_GAP}(?:刚才{_ZH_GAP})?|刚才{_ZH_GAP})?"
            rf"(?:没|没有){_ZH_GAP}(?:听清|听懂|听明白)"
            rf"(?:{_ZH_GAP}(?:刚才(?:的)?|这个)?{_ZH_GAP}"
            r"(?:问题|题目|那句|内容))?)",
            re.IGNORECASE,
        ),
        ambiguous=True,
    ),
    _Pattern(
        "request_repeat",
        "repeat",
        re.compile(
            r"(?P<evidence>\b(?:could|can|would|will)\s+you\s+(?:please\s+)?"
            r"(?:repeat(?:\s+(?:that|it|the\s+(?:question|last\s+part)))?"
            r"|say\s+(?:that|it)\s+again|read\s+(?:that|the\s+question)\s+again)\b)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "request_repeat",
        "repeat",
        re.compile(
            r"(?P<evidence>\bplease\s+(?:repeat(?:\s+(?:that|it|the\s+question))?"
            r"|say\s+(?:that|it)\s+again)\b)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "request_repeat",
        "repeat",
        re.compile(
            r"(?P<evidence>\bi\s+(?:(?:did\s+not|didn't|could\s+not|couldn't)\s+"
            r"(?:catch|hear|understand)|(?:missed))"
            r"(?:\s+(?:that|it|the\s+(?:question|last\s+part)))?\b)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "not_finished",
        "continue_listening",
        re.compile(
            rf"{_ZH_DIRECT_BOUNDARY}(?P<evidence>(?:我{_ZH_GAP})?(?:还没|没有){_ZH_GAP}"
            rf"(?:说|回答|讲){_ZH_GAP}完|"
            rf"(?:让我|请让我){_ZH_GAP}(?:先{_ZH_GAP})?(?:说|回答|讲){_ZH_GAP}完|"
            rf"我{_ZH_GAP}(?:还要|想再|需要再){_ZH_GAP}(?:补充|说)(?:{_ZH_GAP}(?:一点|一下))?)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "not_finished",
        "continue_listening",
        re.compile(
            r"(?P<evidence>\b(?:i(?:'m|\s+am)\s+not\s+(?:done|finished)"
            r"|i\s+(?:have\s+not|haven't)\s+finished"
            r"|(?:please\s+)?let\s+me\s+finish"
            r"|i\s+(?:still\s+need|need|want)\s+to\s+add\s+(?:something|one\s+more\s+thing)"
            r"|one\s+more\s+thing)\b)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "pause",
        "pause",
        re.compile(
            rf"{_ZH_DIRECT_BOUNDARY}(?P<evidence>(?:请{_ZH_GAP})?(?:先{_ZH_GAP})?暂停(?:{_ZH_GAP}一下)?|"
            rf"(?:请{_ZH_GAP})?(?:等一下|稍等(?:一下)?|等一会儿)|"
            rf"(?:给我|让我){_ZH_GAP}(?:一点时间|想一下|缓一下))",
            re.IGNORECASE,
        ),
        pause=True,
    ),
    _Pattern(
        "pause",
        "pause",
        re.compile(
            r"(?P<evidence>\b(?:(?:can|could)\s+we\s+pause(?:\s+for\s+(?:a\s+)?moment)?"
            r"|pause\s+(?:please|for\s+(?:a\s+)?(?:moment|second))"
            r"|(?:please\s+)?hold\s+on"
            r"|wait\s+(?:a\s+)?(?:moment|second)"
            r"|give\s+me\s+(?:a\s+)?(?:moment|second))\b)",
            re.IGNORECASE,
        ),
        pause=True,
    ),
)

_SENTENCE_BREAK = re.compile(r"[。！？!?；;\n]+")
_LOCAL_BREAK = re.compile(r"[,，。！？!?；;\n]")
_NARRATIVE = re.compile(
    r"(?:例如|比如|举例|假设|场景|用例|用户|候选人|面试者|系统|程序|产品|"
    r"识别|检测|意图|提示语|文案|规则|包含|提到|引用|日志|"
    r"他说|她说|他们说|当.+?时|如果.+?时|"
    r"for\s+example|e\.g\.|such\s+as|scenario|use\s+case|"
    r"(?:the|a)\s+(?:user|candidate|system)|when\s+(?:a|the)\s+user|"
    r"if\s+(?:a|the)\s+user|detect(?:s|ing)?|classif(?:y|ies|ying)|"
    r"contains?|mentions?|quotes?|logs?|"
    r"intent|the\s+system\s+should|(?:he|she|they)\s+said)",
    re.IGNORECASE,
)
_CODE = re.compile(
    r"(?:```|`|==|!=|=>|\{\s*[\"']|[\"']\s*:\s*[\"']|"
    r"\b(?:if|elif|switch|case|function|def|class)\s*[\s(]|"
    r"\b(?:intent|action|event_type|suggested_action)\s*[=:])",
    re.IGNORECASE,
)
_CONDITIONAL_SUFFIX = re.compile(
    r"^(?:时|时候|的情况|的场景|这个意图|this\s+intent\b)", re.IGNORECASE
)
_PAUSE_OBJECT = re.compile(
    r"^(?:the\s+)?(?:worker|queue|task|job|thread|service|system|pipeline|"
    r"任务|队列|线程|服务|系统|流水线|request|intent|state|请求|意图|状态)",
    re.IGNORECASE,
)
_DIRECT_HINT = re.compile(
    r"(?:^|\b)(?:我|刚才|麻烦|劳驾|请|please|i|could|can|would|will|"
    r"let\s+me|hold\s+on|wait|give\s+me)(?:\b|)",
    re.IGNORECASE,
)


class MetaIntentDetector:
    """Classify direct repeat/continue/pause controls through one interface."""

    MAX_SENTENCE_CHARS = 280
    MAX_LOCAL_CHARS = 140

    def detect(self, text: str) -> Optional[MetaIntentMatch]:
        if not text or not text.strip():
            return None
        quoted = tuple(self._quoted_spans(text))
        candidates: list[MetaIntentMatch] = []
        for sentence_start, sentence_end in self._sentences(text):
            sentence = text[sentence_start:sentence_end]
            for pattern in _PATTERNS:
                for matched in pattern.expression.finditer(sentence):
                    group_start, group_end = matched.span("evidence")
                    start = sentence_start + group_start
                    end = sentence_start + group_end
                    if any(start < quote_end and end > quote_start for quote_start, quote_end in quoted):
                        continue
                    if not self._is_direct(
                        text,
                        sentence_start,
                        sentence_end,
                        start,
                        end,
                        pattern,
                    ):
                        continue
                    evidence = text[start:end].strip()
                    if evidence:
                        candidates.append(
                            MetaIntentMatch(
                                intent=pattern.intent,
                                action=pattern.action,
                                evidence=evidence,
                                start=start,
                                end=end,
                            )
                        )
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item.start, item.end - item.start))

    def _is_direct(
        self,
        text: str,
        sentence_start: int,
        sentence_end: int,
        start: int,
        end: int,
        pattern: _Pattern,
    ) -> bool:
        sentence = text[sentence_start:sentence_end]
        local_start = max(
            sentence_start,
            self._last_break(text, sentence_start, start) + 1,
        )
        local_end = min(sentence_end, self._next_break(text, end, sentence_end))
        local = text[local_start:local_end].strip()
        if (
            not local
            or len(sentence) > self.MAX_SENTENCE_CHARS
            or len(local) > self.MAX_LOCAL_CHARS
            or _CODE.search(local)
            or _NARRATIVE.search(local)
        ):
            return False

        prefix = text[local_start:start]
        suffix = text[end:local_end].lstrip()
        previous = text[
            max(sentence_start, self._last_break(text, sentence_start, local_start - 1) + 1):local_start
        ].strip(" \t,，")
        if previous and len(previous) <= 48 and _NARRATIVE.search(previous):
            return False
        if _CONDITIONAL_SUFFIX.search(suffix):
            return False
        if pattern.pause and _PAUSE_OBJECT.search(suffix):
            return False
        if pattern.ambiguous:
            compact_local = re.sub(r"[\s,，、:：]", "", local)
            if not _DIRECT_HINT.search(local) and len(compact_local) > 14:
                return False
            if re.search(r"(?:情况|场景|功能|机制|处理|解决|检测|识别)$", compact_local):
                return False
        # A reporting verb immediately before an otherwise imperative phrase
        # is still a mention ("用户会说请再说一遍"), not a floor command.
        if re.search(r"(?:说|表示|提示|输出|返回|says?|said|prints?|returns?)\s*$", prefix, re.IGNORECASE):
            return False
        return True

    @staticmethod
    def _sentences(text: str) -> Iterable[tuple[int, int]]:
        start = 0
        for match in _SENTENCE_BREAK.finditer(text):
            if match.start() > start:
                yield start, match.start()
            start = match.end()
        if start < len(text):
            yield start, len(text)

    @staticmethod
    def _quoted_spans(text: str) -> Iterable[tuple[int, int]]:
        for expression in (
            re.compile(r'"[^"\n]*"'),
            re.compile(r"“[^”\n]*”"),
            re.compile(r"‘[^’\n]*’"),
            re.compile(r"`[^`\n]*`"),
        ):
            for match in expression.finditer(text):
                yield match.span()

    @staticmethod
    def _last_break(text: str, lower: int, upper: int) -> int:
        result = lower - 1
        for match in _LOCAL_BREAK.finditer(text, lower, max(lower, upper)):
            result = match.start()
        return result

    @staticmethod
    def _next_break(text: str, lower: int, upper: int) -> int:
        match = _LOCAL_BREAK.search(text, lower, upper)
        return match.start() if match else upper
