"""The frozen scope survives appointment, readiness and follow-up boundaries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ApiError
from app.domain.interview_lifecycle import LifecycleCommand, LifecycleCommandType as Kind
from app.domain.session_assessment_scope import scope_allows_followup
from app.services.appointments import AppointmentService
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.interviews import InterviewService
from app.repositories.memory import InMemoryStore
from tests.test_adaptive_interview import LIFECYCLE, NOW
from tests.test_bank_preparation import prepared_scope
from tests.test_session_assessment_scope_090 import contract_for_pool, planned_pairs
from tests.test_primary_question_budget_089 import session_with, choose
from tests.test_inquiry_unit_assessment import submit_unit


CURRENT = datetime.fromisoformat(NOW.replace('Z', '+00:00'))


def at(seconds):
    return (CURRENT + timedelta(seconds=seconds)).isoformat()


@pytest.mark.anyio
async def test_create_and_patch_cannot_shorten_scope_window_or_leave_partial_writes():
    store, _, assembly, args, _ = await prepared_scope()
    plan = await assembly.prepare(**args)
    service = AppointmentService(store, clock=lambda: CURRENT)
    payload = dict(plan_id=plan['id'], candidate_profile_id=args['candidate_profile_id'],
                   job_position_id=args['job_position_id'], scheduled_start_at=at(0), scheduled_end_at=at(300))
    with pytest.raises(ApiError) as error:
        service.create(payload)
    assert error.value.code == 'ASSESSMENT_APPOINTMENT_WINDOW_TOO_SHORT'
    assert not store.interview_appointments
    appointment = service.create({**payload, 'scheduled_end_at': at(2700)})
    before = deepcopy(store.interview_appointments)
    with pytest.raises(ApiError) as error:
        service.patch(appointment['id'], {'expected_version': appointment['version'], 'scheduled_end_at': at(300)})
    assert error.value.code == 'ASSESSMENT_APPOINTMENT_WINDOW_TOO_SHORT'
    assert store.interview_appointments == before


@pytest.mark.anyio
async def test_late_start_readiness_blocks_before_provider_refresh_or_session_creation():
    from tests.test_appointment_readiness_refresh import fixture, NOW as APPOINTMENT_NOW, iso, update
    service, probe = fixture('registered')
    with service.persistence.transaction('org_default') as transaction:
        plan = transaction.interview_plans.get('plan_fixture')
        plan['assessment_contract'] = contract_for_pool()
        transaction.interview_plans.update(plan, expected_version=plan['version'])
    update(service, scheduled_end_at=iso(APPOINTMENT_NOW + timedelta(minutes=5)))
    # Use the real pure readiness seam instead of the fixture's route-only mock.
    service._readiness = AppointmentService._readiness.__get__(service, AppointmentService)
    with service.persistence.transaction('org_default') as transaction:
        plan = transaction.interview_plans.get('plan_fixture')
        appointment = transaction.interview_appointments.get('appointment_fixture')
        readiness = service._readiness(transaction, plan, appointment=appointment, now=APPOINTMENT_NOW)
    scope_check = next(item for item in readiness['checks'] if item['name'] == 'assessment_time_budget')
    assert scope_check['ready'] is False
    assert readiness['can_start'] is False
    public = service.readiness('fixture-token')
    assert public['entry_blocker'] == {'code': 'ASSESSMENT_START_TIME_INSUFFICIENT',
        'details': {'required_seconds': 1830, 'available_seconds': 300}}
    assert public['can_start'] is False
    with pytest.raises(ApiError) as error:
        await service.start_with_refresh('fixture-token')
    assert error.value.code == 'ASSESSMENT_START_TIME_INSUFFICIENT'
    assert probe.calls == []
    with service.persistence.transaction('org_default') as transaction:
        assert transaction.interview_sessions.list() == []
        assert transaction.interview_appointments.get('appointment_fixture')['status'] == 'registered'


def test_final_start_transaction_rechecks_remaining_time_and_preserves_idempotent_start():
    source = session_with(contract_for_pool())
    source.update(status='scheduled', started_at=None, scheduled_end_at=at(300))
    before = deepcopy(source)
    with pytest.raises(ApiError) as error:
        LIFECYCLE.execute(source, LifecycleCommand(Kind.START), now=NOW)
    assert error.value.code == 'ASSESSMENT_START_TIME_INSUFFICIENT'
    assert source == before
    source['scheduled_end_at'] = at(2700)
    started = LIFECYCLE.execute(source, LifecycleCommand(Kind.START), now=NOW).session
    # An already active interview is never re-opened/reinterpreted by a retry.
    assert LIFECYCLE.execute(started, LifecycleCommand(Kind.START), now=at(2600)).session == started


def test_optional_followup_preserves_the_time_reserved_for_unasked_main_questions():
    contract = contract_for_pool()
    source = session_with(contract)
    candidate, unit = planned_pairs(contract)[0]
    source = submit_unit(choose(source, candidate, unit).session)
    reserved = 9 * 180
    source['scheduled_end_at'] = at(reserved + 30 + 89)
    assert not scope_allows_followup(source, reserved + 89)
    assert not ConversationUnderstandingService._within_time_budget(source, 90, now=CURRENT)
    root = source['turns'][0]
    followup = InterviewService(InMemoryStore())._build_followup_turn(source, root, source['answers'][0], {
        'root_turn_id': root['id'], 'question_text': '你会怎样核实这里的边界？',
        'target_key_points': [root['question_snapshot']['key_points'][0]['text']], 'reason': 'missing_key_points'}, now=NOW)
    with pytest.raises(ApiError, match='follow-up budget'):
        LIFECYCLE.execute(source, LifecycleCommand(Kind.FOLLOWUP_REQUESTED,
            {'root_turn_id': root['id'], 'followup_turn': followup}), now=NOW)
    source['scheduled_end_at'] = at(reserved + 30 + 90)
    assert ConversationUnderstandingService._within_time_budget(source, 90, now=CURRENT)
    permitted = LIFECYCLE.execute(source, LifecycleCommand(Kind.FOLLOWUP_REQUESTED,
        {'root_turn_id': root['id'], 'followup_turn': followup}), now=NOW).session
    assert len(permitted['turns']) == 2
    assert permitted['turns'][-1]['is_followup']


@pytest.mark.anyio
async def test_explicit_readiness_refresh_skips_paid_probes_for_an_infeasible_window():
    from tests.test_appointment_readiness_refresh import fixture
    service, probe = fixture('registered')
    underlying = service._readiness
    def readiness(*args, **kwargs):
        result = underlying(*args, **kwargs)
        result['checks'].append({'name': 'assessment_time_budget', 'ready': False,
                                 'required_seconds': 1830, 'available_seconds': 300})
        return result
    service._readiness = readiness
    result = await service.refresh_readiness('appointment_fixture')
    assert result['can_start'] is False
    assert probe.calls == []
