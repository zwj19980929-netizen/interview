import asyncio
from types import SimpleNamespace

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import StructuredResponseValidationError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_semantic_turn_endpoint import SemanticCapture
from test_stable_preview_preparation import _preview
from test_prepared_turn_decision import Gateway, _input, _service
from test_semantic_turn_policy import semantic_response


def test_idle_check_is_versioned_speech_without_submission_or_new_assessment():
    from app.core.prompt.conversation_reception import RECEPTION_SPEECH, RECEPTION_SPEECH_VERSION
    assert RECEPTION_SPEECH_VERSION == "conversation_reception_speech.v3"
    assert RECEPTION_SPEECH["idle_check"] == "我还在听。需要一点时间，或希望我再解释一下题目，都可以告诉我。"


@pytest.mark.anyio
async def test_reception_uses_small_grounded_schema_and_no_answer_preparation():
    gateway = Gateway({'kind': 'wait', 'confidence': .95, 'evidence_id': 'E1', 'reply_text': '没关系，慢慢想，我在听。'})
    result = await _service(gateway).classify_reception('稍等。', 'org_default', question='请介绍一个项目。')
    assert result == {'kind': 'wait', 'confidence': .95, 'evidence_quote': '稍等。', 'reply_text': '没关系，慢慢想，我在听。'}
    request = gateway.requests[0]
    assert request.max_output_tokens == 320
    assert request.execution_budget.timeout_s == 4
    assert request.execution_budget.max_provider_retries == 0
    assert request.metadata == {'prompt_version': 'conversation_reception.v6'}
    assert 'standard_answer' not in str(request.messages)


@pytest.mark.anyio
@pytest.mark.parametrize('data', [
    {'kind': 'submit_answer', 'confidence': .95, 'evidence_id': 'E1'},
    {'kind': 'wait', 'confidence': True, 'evidence_id': 'E1'},
    {'kind': 'wait', 'confidence': 1.01, 'evidence_id': 'E1'},
    {'kind': 'wait', 'confidence': .9, 'evidence_id': 'E2'},
    {'kind': 'wait', 'confidence': .9, 'evidence_id': 'E1', 'speech': 'unapproved'},
    {'kind': 'wait', 'evidence_id': 'E1'},
])
async def test_reception_rejects_invalid_model_results_before_routing(data):
    with pytest.raises(StructuredResponseValidationError):
        await _service(Gateway({'reply_text': '我在听。', **data})).classify_reception('稍等。', 'org_default')


class ReceptionCapture(SemanticCapture):
    supports_stable_preview = True

    def __init__(self, kind='wait'):
        super().__init__()
        self.current_final = _final('我继续回答。' if kind == 'continue' else '稍等。')
        self.kind = kind
        self.requests = []
        self.release = None
        self.entered = asyncio.Event()
        self.preview_override = None

    async def transcript_preview(self):
        return self.preview_override or _preview(self.current_final)

    async def classify_reception(self, text, **kwargs):
        self.requests.append((text, kwargs))
        self.entered.set()
        if self.release:
            await self.release.wait()
        return {'kind': self.kind, 'confidence': .95, 'evidence_quote': text}

    async def prepare_decision(self, final, **kwargs):
        if self.kind not in {'wait', 'continue'}:
            return await super().prepare_decision(final, **kwargs)
        self.flags.append((kwargs.get('semantic_first', False), kwargs.get('completion_confirmed', False)))
        self.prepares.append(final)
        utterance, turn, interview = _input()
        utterance = utterance.model_copy(update={'text': final.text})
        interview['_semantic_first'] = True
        data = semantic_response(content='none', intent='thinking', technical=False)
        if self.kind == 'continue':
            data['understanding']['turn_intent'] = 'answering'
        class SemanticGateway(Gateway):
            async def invoke(self, capability, request):
                self.responses.append(data['understanding'] if request.metadata['prompt_version'].startswith('interview_turn_understanding.') else data)
                return await super().invoke(capability, request)
        understanding, followup = await _service(SemanticGateway()).prepare_decision(utterance, turn, interview)
        assert understanding.problem is None, 'The full semantic fixture must remain a valid response'
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
                               understanding=understanding, followup=followup)


def setup(kind='wait'):
    capture = ReceptionCapture(kind)
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []
    async def speak(kind, guard):
        guard()
        endpoint.confirmation.playback_selected(endpoint)
        spoken.append(kind)
        if kind == 'pause':
            capture.is_open = False
        return True
    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    endpoint.start()
    clock.value = 1.1
    return endpoint, clock, notices, commits, spoken


@pytest.mark.parametrize('kind', ['wait', 'continue', 'presence', 'repeat', 'question_clarification',
    'transcript_correction', 'audio_problem', 'unclear'])
def test_social_requests_speak_after_current_final_before_full_answer_analysis(kind):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup(kind)
        initial_text = endpoint.capture.current_final.text
        try:
            await _until(lambda: spoken)
            assert spoken == ['reception_' + kind]
            assert endpoint.capture.snapshots == [False], 'Preview alone cannot speak'
            assert len(endpoint.capture.prepares) <= 1 and not commits, 'Parallel preparation never authorizes submission'
            assert len(endpoint.capture.requests) == 1, 'Reuse the validated result for identical final words'
            endpoint.confirmation.floor_returned(endpoint)
            clock.value = 60
            expected = ['reception_' + kind]
            if kind != 'wait':
                await _until(lambda: len(spoken) == 2)
                expected.append('reception_idle_check')
                assert endpoint.confirmation.speaking
                endpoint.confirmation.floor_returned(endpoint)
            clock.value = 600
            await asyncio.sleep(.15)
            assert spoken == expected and not commits, 'Ordinary silence gets one check; explicit waiting gets none'
            assert endpoint.capture.snapshots == [False], 'Long waiting must not rotate the next STT stream'
            assert len(endpoint.capture.requests) == 1, 'The idle check must not repeatedly classify handled words'
            assert endpoint.capture.is_open
            assert 'answer_preparing' not in notices
            if kind in {'wait', 'continue'}:
                assert len(endpoint.capture.prepares) == 1, 'Reuse the original full review after fast reception'
                assert endpoint.confirmation._reception_review is None
                assert endpoint.confirmation._idle_check_allowed is (kind == 'continue')
                assert not endpoint._prepare_failures

            endpoint.capture.current_final = _final(initial_text + '你还在吗？')
            endpoint.capture.kind = 'presence'
            endpoint.observe_transcript(endpoint.capture.current_final.text)
            clock.value += 1.1
            await _until(lambda: len(spoken) == len(expected) + 1)
            assert spoken == expected + ['reception_presence'] and not commits
            assert endpoint.capture.requests[-1][0] == '你还在吗？'
            assert endpoint.capture.requests[-1][1]['preceding_text'] == initial_text
            assert endpoint.capture.is_open
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_ordinary_answer_preview_does_not_seal_audio_or_interrupt_candidate():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup('other')
        try:
            await _until(lambda: endpoint.capture.requests)
            await asyncio.sleep(.15)
            assert endpoint.capture.snapshots == [] and spoken == [] and not commits
            assert len(endpoint.capture.requests) == 1
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['voice', 'transcript', 'close'])
def test_new_input_revokes_pending_reception_and_clears_preparing(change):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        endpoint.capture.release = asyncio.Event()
        try:
            await asyncio.wait_for(endpoint.capture.entered.wait(), 1)
            if change == 'voice':
                endpoint.speech_started()
            elif change == 'transcript':
                endpoint.observe_transcript('不用等了。')
            else:
                await endpoint.close()
            endpoint.capture.release.set()
            await asyncio.sleep(.15)
            assert not spoken and not commits
            if change != 'close':
                assert notices[-1] == 'answer_listening'
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_changed_final_cannot_reuse_preview_reception():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        endpoint.capture.preview_override = _preview(_final('稍等。'))
        endpoint.capture.current_final = _final('不用等了，我继续回答。')
        calls = 0
        async def classify(text, **kwargs):
            nonlocal calls
            calls += 1
            return {'kind': 'wait' if calls == 1 else 'other', 'confidence': .95, 'evidence_quote': text}
        endpoint.capture.classify_reception = classify
        try:
            await _until(lambda: calls == 2)
            assert not spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_new_request_after_wait_is_classified_from_new_words_with_context():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        try:
            await _until(lambda: spoken)
            endpoint.confirmation.floor_returned(endpoint)
            endpoint.capture.current_final = _final('稍等。你还在吗？')
            endpoint.capture.kind = 'presence'
            endpoint.speech_started()
            clock.value += 1.1
            await _until(lambda: len(spoken) == 2)
            assert spoken == ['reception_wait', 'reception_presence']
            assert endpoint.capture.requests[-1][0] == '你还在吗？'
            assert endpoint.capture.requests[-1][1]['preceding_text'] == '稍等。'
            assert not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('interrupt', [False, True])
def test_pause_is_acknowledged_before_pausing_and_can_be_revoked(interrupt):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup('pause')
        try:
            await _until(lambda: spoken)
            assert spoken == ['reception_pause'] and endpoint.capture.is_open
            endpoint.confirmation.floor_returned(endpoint)
            if interrupt:
                endpoint.speech_started()
                await asyncio.sleep(.15)
                assert endpoint.capture.is_open and spoken == ['reception_pause']
            else:
                await _until(lambda: spoken[-1] == 'pause')
                assert not endpoint.capture.is_open
            assert not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['unavailable', 'exception'])
def test_failed_pause_speech_does_not_silently_pause_interview(failure):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup('pause')
        async def fail(kind, guard):
            guard()
            spoken.append(kind)
            if failure == 'exception':
                raise RuntimeError('Synthetic speech failure')
            return False
        endpoint.confirmation.speak = fail
        try:
            await _until(lambda: spoken)
            await asyncio.sleep(.15)
            assert spoken == ['reception_pause'] and endpoint.capture.is_open
            assert endpoint.confirmation.phase == 'listening' and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())
