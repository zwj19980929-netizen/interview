import asyncio

import pytest

from app.core.time import utc_now
from app.domain.interview_agent import ConversationUtterance
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.interviews import InterviewService
from app.services.meta_intent import MetaIntentDetector


@pytest.mark.parametrize(
    ("transcript", "intent", "action", "evidence"),
    [
        ("请把刚才的问题再说一遍。", "request_repeat", "repeat", "再说一遍"),
        (
            "Sorry, I didn't catch the last part, 可以再说一下吗？",
            "request_repeat",
            "repeat",
            "I didn't catch the last part",
        ),
        (
            "Wait，我还没回答完，let me finish.",
            "not_finished",
            "continue_listening",
            "我还没回答完",
        ),
        (
            "I'm not finished, 我还想补充一点。",
            "not_finished",
            "continue_listening",
            "I'm not finished",
        ),
        ("Could we pause for a moment?", "pause", "pause", "Could we pause for a moment"),
        ("稍等一下，我想整理一下。", "pause", "pause", "稍等一下"),
        ("Please 再说一遍这个问题。", "request_repeat", "repeat", "再说一遍这个问题"),
    ],
)
def test_bounded_bilingual_meta_intent_corpus(
    transcript: str, intent: str, action: str, evidence: str
) -> None:
    result = MetaIntentDetector().detect(transcript)

    assert result is not None
    assert result.intent == intent
    assert result.action == action
    assert result.evidence == evidence
    assert transcript[result.start : result.end] == evidence


@pytest.mark.parametrize(
    "transcript",
    [
        "用户没听清时系统应自动重读。",
        "如果候选人没听清，系统应该重读问题。",
        "我的处理逻辑是：用户说“请再说一遍”时进入 repeat 状态。",
        'if intent == "pause": return WAIT',
        "I would pause the worker before draining the queue.",
        'The system should detect "I didn\'t catch that" as repeat.',
        "这个机制用来处理没听清的情况。",
        "比如，用户会说没听清，产品此时重读。",
        "日志内容是 `suggested_action=pause`，并不是暂停请求。",
        "回答得长时不能因为包含 wait a second 就改变发言权。",
        "隔离级别用于平衡脏读、不可重复读和幻读。",
        "幂等键用于保证请求不能重复提交。",
        "接口设计里要支持重读策略和暂停机制。",
    ],
)
def test_meta_intent_adversarial_mentions_do_not_move_floor(transcript: str) -> None:
    assert MetaIntentDetector().detect(transcript) is None


class _Gateway:
    def __init__(self, *, data=None, error: Exception = None) -> None:
        self.data = data
        self.error = error

    async def invoke(self, _capability, _request):
        if self.error is not None:
            raise self.error
        return ChatJSONResponse(
            data=self.data,
            usage=Usage(),
            provider=ProviderMeta(
                provider_id="unsafe-test-provider",
                model="test-model",
                request_id="request_1",
                latency_ms=1,
            ),
        )


def _turn() -> dict:
    return {
        "id": "turn_1",
        "question_spoken_text": "请说明你的幂等恢复设计。",
        "question_snapshot": {
            "question_text": "请说明你的幂等恢复设计。",
            "key_points": [{"text": "幂等键"}],
        },
        "current_understanding": None,
    }


def _utterance(text: str) -> ConversationUtterance:
    return ConversationUtterance(
        utterance_id="utterance_1",
        revision=1,
        speaker="candidate",
        text=text,
        is_final=True,
        authoritative=True,
        audio_uri="private-file://audio_1",
        stt_confidence=0.95,
        source="server_streaming",
        created_at=utc_now(),
    )


def test_retryable_provider_failure_returns_only_approved_clarification() -> None:
    service = ConversationUnderstandingService(
        InMemoryStore(),
        gateway=_Gateway(
            error=ProviderError(
                "provider_unavailable", "sensitive provider detail", retryable=True
            )
        ),
    )

    result = asyncio.run(
        service.understand(
            _utterance("我使用幂等键记录提交。"),
            _turn(),
            {"id": "interview_1", "organization_id": "org_default"},
        )
    )

    assert result.intent == "clarification_request"
    assert result.suggested_action == "clarify"
    assert result.claims == []
    assert result.evidence_quotes == []
    assert result.problem is not None
    assert result.problem.code == "UNDERSTANDING_PROVIDER_UNAVAILABLE"
    assert result.problem.recoverable is True
    assert "sensitive provider detail" not in result.model_dump_json()


def test_invalid_schema_or_non_verbatim_evidence_pauses_fail_closed() -> None:
    transcript = "我使用幂等键记录提交。"
    invalid = {"clarification_target": None,
        "intent": "answer",
        "answer_summary": transcript,
        "claims": [],
        "evidence_quotes": ["模型编造的证据"],
        "covered_capability_points": ["幂等键"],
        "missing_capability_points": [],
        "ambiguities": [],
        "contradictions": [],
        "confidence": 0.95,
        "suggested_action": "next",
    }
    service = ConversationUnderstandingService(
        InMemoryStore(), gateway=_Gateway(data=invalid)
    )

    result = asyncio.run(
        service.understand(
            _utterance(transcript),
            _turn(),
            {"id": "interview_1", "organization_id": "org_default"},
        )
    )

    assert result.suggested_action == "pause"
    assert result.claims == []
    assert result.evidence_quotes == []
    assert result.problem is not None
    assert result.problem.code == "UNDERSTANDING_RESULT_REJECTED"
    assert result.problem.recoverable is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(claims=[], evidence_quotes=[]),
        lambda value: value.update(
            covered_capability_points=[], missing_capability_points=[]
        ),
        lambda value: value.update(
            covered_capability_points=["幂等键"],
            missing_capability_points=["幂等键"],
        ),
        lambda value: value["claims"][0].update(evidence_quote="未声明的原文片段"),
    ],
)
def test_answer_requires_nonempty_verbatim_evidence_and_complete_capability_partition(
    mutate,
) -> None:
    transcript = "我使用幂等键记录提交。"
    value = {"clarification_target": None,
        "intent": "answer",
        "answer_summary": transcript,
        "claims": [{"claim": "使用幂等键", "evidence_quote": transcript}],
        "evidence_quotes": [transcript],
        "covered_capability_points": ["幂等键"],
        "missing_capability_points": [],
        "ambiguities": [],
        "contradictions": [],
        "confidence": 0.95,
        "suggested_action": "next",
    }
    mutate(value)
    service = ConversationUnderstandingService(
        InMemoryStore(), gateway=_Gateway(data=value)
    )

    result = asyncio.run(
        service.understand(
            _utterance(transcript),
            _turn(),
            {"id": "interview_1", "organization_id": "org_default"},
        )
    )

    assert result.suggested_action == "pause"
    assert result.problem is not None
    assert result.problem.code == "UNDERSTANDING_RESULT_REJECTED"


def _session(store: InMemoryStore) -> dict:
    now = utc_now()
    turn = {
        **_turn(),
        "interview_id": "interview_understanding_safety",
        "question_id": "question_1",
        "question_snapshot_id": "snapshot_1",
        "order": 1,
        "phase": "position_bank",
        "status": "asking",
        "is_followup": False,
        "root_turn_id": "turn_1",
        "followup_depth": 0,
        "allow_followup": False,
        "weight": 1.0,
        "utterances": [],
        "conversation_acts": [],
        "started_at": now,
        "completed_at": None,
    }
    session = {
        "id": "interview_understanding_safety",
        "organization_id": "org_default",
        "status": "in_progress",
        "phase": "position_bank",
        "current_turn_id": "turn_1",
        "turn_ids": ["turn_1"],
        "turns": [turn],
        "answers": [],
        "evaluation_revisions": [],
        "report_revisions": [],
        "lifecycle_events": [],
        "created_at": now,
        "updated_at": now,
        "last_activity_at": now,
    }
    with persistence_for(store).transaction("org_default") as transaction:
        return transaction.interview_sessions.add(session)


def test_understanding_contract_failure_never_creates_candidate_answer() -> None:
    store = InMemoryStore()
    session = _session(store)
    service = InterviewService(store)
    service.conversation.gateway = _Gateway(data={"intent": "answer"})

    result = asyncio.run(
        service.submit_streaming_answer(
            session["id"],
            {
                "turn_id": "turn_1",
                "audio_uri": "private-file://audio_1",
                "content_type": "audio/pcm",
                "final_transcript": "我使用幂等键记录提交。",
                "confidence": 0.95,
                "language": "zh-CN",
                "duration_seconds": 3,
                "provider": {"provider_id": "stt-test", "model": "test"},
                "segments": [],
            },
        )
    )

    current = service.get_interview(session["id"])
    assert result["accepted"] is False
    assert result["answer"] is None
    assert result["evaluation_work_id"] is None
    assert result["understanding_problem"]["code"] == "UNDERSTANDING_RESULT_REJECTED"
    assert current["answers"] == []
    assert current["status"] == "paused"
    rejected = next(
        event
        for event in current["lifecycle_events"]
        if event["type"] == "utterance.not_accepted"
    )
    assert rejected["payload"]["problem"]["action"] == "pause"
