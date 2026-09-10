"""Resume selection, deferred speech, and the actual appointment-to-turn seam."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ApiError
from app.domain.appointment_speech import turn_speech_asset_id
from app.domain.interview_lifecycle import LifecycleCommand, LifecycleCommandType
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.appointments import AppointmentService
from app.services.agent_expression_audio import AgentExpressionAudioService
from app.services.interviews import InterviewService
from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyRequest, PlanAssemblyPolicy
from test_plan_assembly import create_scope, add_question


async def setup_plan():
    store = InMemoryStore()
    catalog, position, kb, role, candidate = create_scope(store, '简历尾题测试', ['python'], 30)
    await add_question(catalog, kb['id'], title='Python 并发', skill='python', difficulty='mid', key_points=['协程'])
    persistence = persistence_for(store)
    with persistence.transaction('org_default') as tx:
        for review_id, created, candidate_id, position_id, status in [
            ('review_old', '2026-01-01', candidate['id'], position['id'], 'ready_for_review'),
            ('review_current', '2026-02-01', candidate['id'], position['id'], 'ready_for_review'),
            ('review_foreign_candidate', '2026-03-01', 'another_candidate', position['id'], 'ready_for_review'),
            ('review_foreign_position', '2026-03-01', candidate['id'], 'another_position', 'ready_for_review'),
            ('review_deleted', '2026-04-01', candidate['id'], position['id'], 'deleted'),
        ]:
            tx.resume_reviews.add({'id': review_id, 'organization_id': 'org_default', 'candidate_profile_id': candidate_id,
                'job_position_id': position_id, 'status': status, 'screening_score': 90, 'created_at': created})
        for i in range(5):
            tx.experience_questions.add({'id': f'experience_{i}', 'organization_id': 'org_default', 'resume_review_id': 'review_current',
                'order': i, 'status': 'draft' if i == 0 else 'approved',
                'question_text': f'在项目{i}中如何使用Python？', 'standard_answer': '解释实际职责与并发实现',
                'key_points': [{'text': '协程', 'weight': 1}], 'rubric': {'semantic_correctness': 1},
                'evidence_refs': [{'label': f'项目{i}', 'evidence': '合成简历证据'}],
                'speech_status': 'deferred', 'speech_asset_id': None})
    request = PlanAssemblyRequest(role_requirement_id=role['id'], job_position_id=position['id'],
        candidate_profile_id=candidate['id'], knowledge_base_ids=(kb['id'],), question_count=1,
        policy=PlanAssemblyPolicy(allow_followups=False), approve=True)
    assembly = InterviewPlanAssembly(store)
    plan = await assembly.assemble(request)
    return store, catalog, assembly, request, plan, candidate


@pytest.mark.anyio
async def test_default_selects_three_grounded_approved_questions_from_matching_latest_review():
    store, _, assembly, request, plan, _ = await setup_plan()
    assert plan['resume_review_id'] == 'review_current'
    assert plan['experience_question_ids'] == ['experience_1', 'experience_2', 'experience_3']
    assert plan['assembly_summary']['experience_question_count'] == 3
    assert sum(x['expected_minutes'] for x in [*plan['bank_slots'], *plan['experience_question_snapshots']]) == 30
    assert round(sum(x['weight'] for x in [*plan['bank_slots'], *plan['experience_question_snapshots']]), 4) == 1
    with assembly.persistence.transaction('org_default') as tx:
        _, turns = assembly.materialize_execution(tx, plan, 'seed')
        assert [x['source_type'] for x in turns] == ['position_bank'] + ['resume_experience'] * 3
        assert not [w for w in tx.outbox.list() if w.get('payload', {}).get('owner_type') == 'experience_question']
    explicit = await assembly.assemble(replace(request, resume_review_id='review_old'))
    assert explicit['resume_review_id'] == 'review_old' and not explicit['experience_question_ids']
    assert any('不足2道' in warning for warning in explicit['assembly_summary']['warnings'])
    with pytest.raises(ApiError) as error:
        await assembly.assemble(replace(request, resume_review_id='review_foreign_candidate'))
    assert error.value.code == 'RESUME_REVIEW_SCOPE_MISMATCH'
    with assembly.persistence.transaction('another_org') as tx:
        assert tx.resume_reviews.get('review_current') is None


async def start_with_pending_speech(monkeypatch):
    monkeypatch.setenv('INTERVIEWER_LOCAL_MEDIA', 'true')
    store, catalog, _, _, plan, candidate = await setup_plan()
    appointments = AppointmentService(store)
    now = datetime.now(timezone.utc)
    appointment = appointments.create({'plan_id': plan['id'], 'candidate_profile_id': candidate['id'],
        'job_position_id': plan['job_position_id'], 'scheduled_start_at': (now - timedelta(minutes=1)).isoformat(),
        'scheduled_end_at': (now + timedelta(hours=1)).isoformat(), 'settings': {'record_audio': True}})
    assert appointment['speech_preparation']['status'] == 'not_requested'
    invitation = appointments.invite(appointment['id'], {'expires_at': (now + timedelta(hours=1)).isoformat()})
    token = invitation['invitation_token']
    intake = {'name': '候选人', 'email': 'candidate@example.com', 'phone': '13800138000',
        'consent': {'accepted': True, 'version': 'v1', 'audio_recording': True}}
    appointments.intake(token, intake)
    appointments.intake(token, intake)
    current = appointments.get(appointment['id'])
    assert current['speech_preparation']['total'] == 3
    assert current['speech_preparation']['status'] == 'queued'
    with appointments.persistence.transaction('org_default') as tx:
        works = [w for w in tx.outbox.list() if w.get('payload', {}).get('appointment_id') == appointment['id']
                 and w['kind'] == 'question.speech.generate']
    assert len(works) == 3
    readiness = appointments.readiness(token, {**{k: True for k in (
        'browser_supported', 'microphone_granted', 'camera_granted', 'speaker_verified', 'webrtc_supported',
        'audio_worklet_supported', 'webgl_supported', 'media_recorder_supported')},
        'audio_content_type': 'audio/webm', 'network_rtt_ms': 10, 'network_jitter_ms': 1, 'avatar_fps': 60})
    assert readiness['can_start'], readiness
    started = appointments.start(token)
    service = appointments.interviews
    session = service.get_interview(started['interview_id'])
    assert session['turns'][0]['status'] == 'asking'
    assert all(t['status'] == 'pending' and not turn_speech_asset_id(t) for t in session['turns'][1:])
    return catalog, appointments, service, session, works


def skip_current(service, session):
    return service._apply_command(session['id'], LifecycleCommand(LifecycleCommandType.SKIP_CURRENT_TURN,
        {'turn_id': session['current_turn_id'], 'reason': 'synthetic_test_transition'}), 'org_default')[0]


@pytest.mark.anyio
async def test_late_speech_is_bound_at_tail_and_missing_speech_skipped_without_resurrection(monkeypatch):
    catalog, appointments, service, session, works = await start_with_pending_speech(monkeypatch)
    frozen = deepcopy(session['plan_snapshot'])
    # Voice generation finishes while the candidate is on the position question.
    for work in works[:2]:
        await catalog.process_speech_work(work['id'])
    session = skip_current(service, session)
    assert session['current_turn_id'] == session['turns'][1]['id']
    assert session['phase'] == 'resume_experience'
    asset_id = turn_speech_asset_id(session['turns'][1])
    assert asset_id
    with service.persistence.transaction('org_default') as tx:
        asset = tx.question_speech_assets.get(asset_id)
        asset['file_object_id'] = 'synthetic_resume_audio'
        tx.question_speech_assets.update(asset, expected_version=asset['version'])
        assert not AgentExpressionAudioService._question_audio_belongs_to_interview(tx, session['id'], 'foreign_audio')
        assert AgentExpressionAudioService._question_audio_belongs_to_interview(tx, session['id'], asset['file_object_id'])
    session = skip_current(service, session)
    assert session['current_turn_id'] == session['turns'][2]['id']
    session = skip_current(service, session)
    assert session['turns'][3]['status'] == 'skipped'
    assert session['turns'][3]['skip_reason'] == 'resume_speech_not_ready'
    assert not session['answers']
    assert session['plan_snapshot'] == frozen
    skipped_events = [e for e in session['lifecycle_events'] if e['type'] == 'turn.skipped'
                      and e['payload'].get('reason') == 'resume_speech_not_ready']
    assert len(skipped_events) == 1
    await catalog.process_speech_work(works[2]['id'])
    with service.persistence.transaction('org_default') as tx:
        service._refresh_deferred_speech(tx, session)
    assert session['turns'][3]['status'] == 'skipped'
    assert turn_speech_asset_id(session['turns'][3]) is None


@pytest.mark.anyio
@pytest.mark.parametrize('mismatch', ['version', 'voice', 'owner', 'cancelled', 'production'])
async def test_tail_rejects_stale_foreign_or_nonproduction_assets(monkeypatch, mismatch):
    catalog, appointments, service, session, works = await start_with_pending_speech(monkeypatch)
    await catalog.process_speech_work(works[0]['id'])
    with service.persistence.transaction('org_default') as tx:
        appointment = tx.interview_appointments.get(session['appointment_id'])
        item = appointment['speech_preparation']['items'][0]
        asset = tx.question_speech_assets.get(item['asset_id'])
        if mismatch == 'version': asset['source_version'] += 1
        if mismatch == 'voice': asset['speech_profile_fingerprint'] = 'different_voice'
        if mismatch == 'owner': asset['owner_id'] = 'foreign_question'
        if mismatch == 'production':
            asset['production_ready'] = False
            monkeypatch.setenv('INTERVIEWER_RUNTIME_ENV', 'production')
        if mismatch == 'cancelled':
            appointment['status'] = 'cancelled'
            tx.interview_appointments.update(appointment, expected_version=appointment['version'])
        tx.question_speech_assets.update(asset, expected_version=asset['version'])
    session = skip_current(service, session)
    assert all(t.get('skip_reason') == 'resume_speech_not_ready' for t in session['turns'][1:])
    assert not session['current_turn_id'] and not session['answers']


@pytest.mark.anyio
async def test_report_scores_actual_answer_only_when_all_resume_speech_is_unfinished(monkeypatch):
    _, _, service, session, _ = await start_with_pending_speech(monkeypatch)
    first = session['turns'][0]
    result = await service.submit_audio_answer(session['id'], {
        'turn_id': first['id'], 'audio_uri': f"/media/{session['id']}/{first['id']}.webm",
        'development_transcript': '通过Python协程实现异步并发处理。', 'duration_seconds': 10,
    })
    with service.persistence.transaction('org_default') as tx:
        work = next(w for w in tx.outbox.list() if w['kind'] == 'answer.evaluate')
    await service.process_outbox_work(work['id'])
    session = service.get_interview(session['id'])
    assert len(session['answers']) == 1
    assert all(t.get('skip_reason') == 'resume_speech_not_ready' for t in session['turns'][1:])
    with service.persistence.transaction('org_default') as tx:
        report_work = next(w for w in tx.outbox.list() if w['kind'] == 'interview.report.generate')
    if report_work['status'] != 'completed':
        await service.process_outbox_work(report_work['id'])
    session = service.get_interview(session['id'])
    report = session['report_revisions'][-1]
    assert report['overall_score'] == session['evaluation_revisions'][-1]['score']
    assert len(report['question_evaluations']) == 1
    assert len(report['skipped_questions']) == 3
    assert all(x['counts_toward_score'] is False for x in report['skipped_questions'])
