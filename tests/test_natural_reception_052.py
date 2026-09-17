"""Synthetic, candidate-facing contract coverage; no private interview replay."""
import asyncio
import json
from copy import deepcopy

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.conversation_reception import RECEPTION_VERSION, validate_reception_response
from app.core.prompt.validation import StructuredResponseValidationError
from app.model_gateway.errors import ProviderError
from test_prepared_turn_decision import Gateway, _service


@pytest.mark.anyio
@pytest.mark.parametrize('speech,kind,reply', [
    ('我有点紧张，脑子突然空白了，容我理一下。', 'wait', '没关系，我们慢慢来。你先整理一下，想好了再说。'),
    ('我想先画出整体的思路再说细节，可以这样讲吗？', 'interview_dialogue', '可以，你按自己顺手的方式讲，先说整体思路就好。'),
    ('我能不能先用一个自己做过的例子解释？', 'interview_dialogue', '可以，结合你的实际经历来讲就好。'),
    ('如果讲到一半想起前面有遗漏，可以回来补吗？', 'interview_dialogue', '可以，随时告诉我你想补充哪一段，我会继续听。'),
    ('刚才的问题是想让我谈设计取舍还是贴完整代码？', 'question_clarification', '这道题是在问设计取舍，你可以先说你会怎么选择。'),
    ('咱们先聊聊昨晚的球赛怎么样？', 'interview_dialogue', '球赛可以留到之后聊，我们先回到这次面试。你可以继续刚才的思路。'),
    ('我会把可重试任务加入队列，消费者按幂等键去重。', 'other', ''),
    ('你们公司主要做什么，团队有多少人？', 'other', ''),
    ('不用等了，我们继续下一题。', 'other', ''),
    ('刚才没听到题目，麻烦重新说一遍。', 'repeat', ''),
])
async def test_single_call_returns_natural_reply_without_answer_or_lifecycle_authority(speech, kind, reply):
    gateway = Gateway({'kind': kind, 'confidence': .96, 'evidence_id': 'E1', 'reply_text': reply})
    result = await _service(gateway).classify_reception(speech, 'org_default', question='请谈一下方案的设计取舍。')
    assert result['kind'] == kind
    assert result['reply_text'] == reply
    assert result['evidence_quote'] in speech
    assert set(result) == {'kind', 'confidence', 'evidence_quote', 'reply_text'}
    assert len(gateway.requests) == 1
    assert gateway.requests[0].metadata == {'prompt_version': RECEPTION_VERSION}
    assert gateway.requests[0].execution_budget.max_provider_retries == 0


@pytest.mark.anyio
async def test_context_projection_includes_only_bounded_candidate_visible_values():
    gateway = Gateway({'kind': 'interview_dialogue', 'confidence': .94, 'evidence_id': 'E1', 'reply_text': '可以先讲思路。'})
    history = [
        {'role': 'candidate', 'text': '我讲一下项目。', 'standard_answer': 'PRIVATE_RUBRIC'},
        {'role': 'interviewer', 'text': '请继续。', 'key_points': ['PRIVATE_POINTS']},
        {'role': 'system', 'text': 'HIDDEN_SYSTEM'},
        {'role': 'candidate', 'text': '长' * 500},
    ]
    original = deepcopy(history)
    facts = {'duration_minutes': 45, 'remaining_seconds': 800, 'completed_topics': 2,
             'standard_answer': 'PRIVATE_ANSWER', 'company': 'UNTRUSTED_COMPANY_FACT'}
    await _service(gateway).classify_reception('可以先讲思路吗？', 'org_default',
        question='题' * 2400, preceding_text='前' * 1100, history=history,
        process_facts=facts, skill_instructions='简洁温和，允许思考。')
    payload = json.loads(gateway.requests[0].messages[-1].content)
    assert payload['process_facts'] == {'duration_minutes': 45, 'remaining_seconds': 800, 'completed_topics': 2}
    assert payload['optional_skill'] == '简洁温和，允许思考。'
    assert len(payload['question']) == 2000 and len(payload['preceding_text']) == 1000
    assert len(payload['history']) == 3 and len(payload['history'][-1]['text']) == 400
    assert history == original
    serialized = json.dumps(payload)
    assert all(sentinel not in serialized for sentinel in ['PRIVATE_RUBRIC', 'PRIVATE_POINTS', 'PRIVATE_ANSWER', 'HIDDEN_SYSTEM', 'UNTRUSTED_COMPANY_FACT'])


def test_contract_defaults_do_not_invent_company_skill_or_process_facts():
    payload = json.loads(prompt_contract('conversation_reception', {'text': '请问还能讲多久？'}).messages[-1].content)
    assert payload['process_facts'] == {} and payload['optional_skill'] == '' and payload['history'] == []
    payload = json.loads(prompt_contract('conversation_reception', {
        'text': '你好', 'process_facts': {'duration_minutes': True, 'remaining_seconds': -3, 'completed_topics': '5'},
        'standard_answer': 'DO_NOT_SEND', 'company_context': 'DO_NOT_SEND',
    }).messages[-1].content)
    assert payload['process_facts'] == {} and 'DO_NOT_SEND' not in str(payload)


@pytest.mark.anyio
@pytest.mark.parametrize('changes,reason', [
    ({'reply_text': ''}, 'reception_reply_missing'),
    ({'reply_text': '   '}, 'reception_reply_missing'),
    ({'reply_text': '字' * 181}, 'max_length'),
    ({'reply_text': None}, 'type'),
    ({'reply_text': '好的\n我们继续'}, 'reception_reply_not_plain_speech'),
    ({'reply_text': '<script>继续</script>'}, 'reception_reply_not_plain_speech'),
    ({'reply_text': '打开 https://example.test'}, 'reception_reply_not_plain_speech'),
    ({'kind': 'other'}, 'reception_silent_kind_has_reply'),
    ({'kind': 'repeat'}, 'reception_silent_kind_has_reply'),
    ({'kind': 'next_question'}, 'enum'),
    ({'evidence_id': 'E999'}, 'enum'),
    ({'confidence': float('nan')}, 'reception_confidence_invalid'),
    ({'submit_answer': True}, 'additional_properties'),
])
async def test_invalid_free_reply_never_reaches_the_speech_layer(changes, reason):
    data = {'kind': 'interview_dialogue', 'confidence': .95, 'evidence_id': 'E1', 'reply_text': '可以，先说你的思路。', **changes}
    gateway = Gateway(data)
    with pytest.raises(StructuredResponseValidationError) as rejected:
        await _service(gateway).classify_reception('我能先说思路吗？', 'org_default')
    assert rejected.value.reason_code == reason
    assert len(gateway.requests) == 1


@pytest.mark.anyio
async def test_provider_failure_is_bounded_and_left_to_runtime_recovery_not_a_fabricated_reply():
    gateway = Gateway(error=ProviderError('provider_unavailable', 'synthetic unavailable', retryable=True))
    with pytest.raises(ProviderError):
        await _service(gateway).classify_reception('我有点紧张。', 'org_default')
    assert len(gateway.requests) == 1


@pytest.mark.anyio
async def test_cancelling_a_pending_reply_propagates_and_cannot_return_late_speech():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class SlowGateway:
        async def invoke(self, capability, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
    task = asyncio.create_task(_service(SlowGateway()).classify_reception('等我整理一下。', 'org_default'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
