import asyncio
import time

from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final


def test_supplement_check_has_separate_deadline_and_requires_delivery():
    async def scenario():
        endpoint, _, _, _ = _endpoint()
        spoken = []
        async def speak(kind, guard):
            guard()
            spoken.append(kind)
            return True
        confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint.confirmation = confirmation
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(_final())
        endpoint._interaction_deadline = time.monotonic() - 1
        assert await confirmation._say(endpoint, 'check', afterwards='awaiting_reply')
        assert spoken == ['check']
        assert not confirmation._supplement_question_delivered
        assert confirmation.phase == 'speaking'
        await endpoint.close()
    asyncio.run(scenario())


def test_unplayed_check_retries_check_not_asr_or_clarify():
    async def scenario():
        endpoint, clock, _, _ = _endpoint()
        spoken = []
        async def speak(kind, guard):
            guard()
            spoken.append(kind)
            checkpoint = confirmation.begin_unplayed_speech()
            if len(spoken) == 1:
                confirmation.restore_unplayed_speech(checkpoint)
                raise ApiError('PROVIDER_TIMEOUT', 'synthetic', status_code=503)
            return True
        confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint.confirmation = confirmation
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(_final())
        confirmation._remember_boundary(_final())
        assert not await confirmation._say(endpoint, 'check', afterwards='awaiting_reply')
        assert confirmation.phase == 'retrying_speech'
        assert not confirmation._supplement_question_delivered
        clock.value += 1.1
        await confirmation.step(endpoint, 1.1)
        assert spoken == ['check', 'check']
        assert not endpoint.capture.snapshots
        assert not endpoint.capture.prepares
        assert confirmation.phase == 'speaking'
        assert not confirmation._supplement_question_delivered
        await endpoint.close()
    asyncio.run(scenario())


def test_unknown_output_timeout_is_not_replayed_or_marked_as_question_delivered():
    async def scenario():
        endpoint, _, notices, _ = _endpoint()
        async def speak(kind, guard):
            endpoint.confirmation.playback_selected(endpoint)
            raise asyncio.TimeoutError()
        endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(_final())
        assert not await endpoint.confirmation._say(endpoint, 'check', afterwards='awaiting_reply')
        assert endpoint.confirmation.phase == 'speaking', 'Retain exact-output playback watchdog'
        assert not endpoint.confirmation._supplement_question_delivered
        assert endpoint.preparation_recovery_details['retryable'] is False
        assert 'understanding_result_rejected' in notices
        await endpoint.close()
    asyncio.run(scenario())


def test_same_prompt_failure_does_not_buy_another_attempt_after_vad_revision():
    async def scenario():
        endpoint, clock, notices, _ = _endpoint()
        calls = []
        async def speak(kind, guard):
            guard()
            calls.append(kind)
            checkpoint = endpoint.confirmation.begin_unplayed_speech()
            endpoint.confirmation.restore_unplayed_speech(checkpoint)
            raise asyncio.TimeoutError()
        confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint.confirmation = confirmation
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(_final())
        confirmation._remember_boundary(_final())
        await confirmation._say(endpoint, 'check', afterwards='awaiting_reply')
        clock.value += 1.1
        await confirmation.step(endpoint, 1.1)
        assert calls == ['check', 'check']
        assert 'understanding_retry_exhausted' in notices
        endpoint.speech_started(source='audio')
        endpoint._proposal_revision = endpoint.revision
        await confirmation.step(endpoint, 1.1)
        confirmation._remember_boundary(_final())
        await confirmation._say(endpoint, 'check', afterwards='awaiting_reply')
        assert calls == ['check', 'check']
        await endpoint.close()
    asyncio.run(scenario())


def test_new_nonprompt_output_cannot_reuse_old_prompt_cleanup_checkpoint():
    async def scenario():
        endpoint, _, notices, _ = _endpoint()
        calls = []
        async def speak(kind, guard):
            calls.append(kind)
            checkpoint = confirmation.begin_unplayed_speech()
            if kind != 'check':
                confirmation.restore_unplayed_speech(checkpoint)
                await endpoint.expression_preparation_failed()
                return False
            return True
        confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint.confirmation = confirmation
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(_final())
        confirmation._remember_boundary(_final())
        await confirmation._say(endpoint, 'check', afterwards='awaiting_reply')
        await confirmation.complete_playback(endpoint, reason='barge_in')
        await confirmation._say(endpoint, 'reception_wait', afterwards='listening')
        assert endpoint.preparation_recovery_details['stage'] != 'supplement_prompt'
        assert calls == ['check', 'reception_wait']
        await endpoint.close()
    asyncio.run(scenario())
