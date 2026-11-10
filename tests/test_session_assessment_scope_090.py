"""A planned scope fits its question/time budget before any answer exists."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.core.errors import ApiError
from app.domain.adaptive_interview import (
    freeze_assessment_contract, validate_assessment_contract, coverage_summary,
)
from app.repositories.memory import InMemoryStore
from app.services.reports import ReportService
from app.services.interviewer_supervisor.tools import InterviewerTools
from tests.test_primary_question_budget_089 import synthetic_pool, session_with, choose
from tests.test_inquiry_unit_assessment import submit_unit
from tests.test_adaptive_interview import scored, end


def contract_for_pool(**overrides):
    args = dict(role={"id": None, "version": None}, candidates=synthetic_pool(),
        dimension_weights={"c%d" % index: 1 for index in range(41)}, question_count=10,
        duration_minutes=45, assessment_basis={"kind": "question_bank", "job_position_id": "position",
        "position_version": 1, "knowledge_base_ids": ["bank"]})
    return freeze_assessment_contract(**{**args, **overrides})


def planned_pairs(contract):
    approved = {(candidate['question_id'], unit['id']): (candidate, unit)
        for candidate in contract['candidate_questions'] for unit in candidate['inquiry_units']}
    return [approved[(item['question_id'], item['inquiry_unit_id'])]
        for item in contract['assessment_scope']['selected_units']]


def rehash(contract):
    contract['contract_hash'] = 'sha256:' + hashlib.sha256(json.dumps(
        {key: value for key, value in contract.items() if key != 'contract_hash'},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def test_41_competencies_56_units_have_an_achievable_frozen_ten_topic_score():
    contract = contract_for_pool()
    assert contract['schema_version'] == 'interview_assessment.v2'
    scope = contract['assessment_scope']
    assert len(scope['selected_units']) == 10
    assert 1 <= scope['source_question_counts']['resume_experience'] <= 3
    assert sum(scope['source_question_counts'].values()) == 10
    assert scope['excluded_competency_ids']
    assert set(scope['included_competency_ids']).isdisjoint(scope['excluded_competency_ids'])
    assert len(scope['available_competency_ids']) == 41
    assert scope['estimated_required_seconds'] + contract['budget']['closing_reserve_seconds'] <= 45 * 60
    assert all(item['min_evidence_roots'] == 2 for item in contract['competencies'])
    original = deepcopy(contract)
    session = session_with(contract)
    for candidate, unit in planned_pairs(contract):
        session = scored(submit_unit(choose(session, candidate, unit).session), 80).session
    assert coverage_summary(session)['sufficient']
    session = end(session).session
    report = ReportService(InMemoryStore()).build_report(session, trigger_reason='test')
    assert report['overall_score'] == 80
    assert report['coverage_status'] == 'sufficient'
    assert report['score_scope'] == 'session_assessment'
    assert report['assessment_scope'] == scope
    assert session['plan_snapshot']['assessment_contract'] == original


def test_missing_planned_evidence_never_reweights_the_remaining_results():
    contract = contract_for_pool()
    session = session_with(contract)
    candidate, unit = planned_pairs(contract)[0]
    session = scored(submit_unit(choose(session, candidate, unit).session), 95).session
    session = end(session, 'candidate_requested').session
    report = ReportService(InMemoryStore()).build_report(session, trigger_reason='test')
    assert report['overall_score'] is None
    assert report['coverage_status'] == 'insufficient_evidence'
    assert report['assessment_scope'] == contract['assessment_scope']
    assert [item['weight'] for item in report['dimension_scores']] == [item['weight'] for item in contract['competencies']]


def test_unplanned_unit_cannot_enter_via_supervisor_or_direct_lifecycle():
    contract = contract_for_pool()
    session = session_with(contract)
    planned = {(c['question_id'], u['id']) for c, u in planned_pairs(contract)}
    foreign = next((c, u) for c in contract['candidate_questions'] for u in c['inquiry_units']
                   if (c['question_id'], u['id']) not in planned)
    tools = InterviewerTools(session)
    assert tools.catalog_page()['total'] == len(planned)
    with pytest.raises(ValueError):
        tools.require_selection(foreign[0]['question_id'], foreign[1]['id'])
    with pytest.raises(ApiError) as error:
        choose(session, *foreign)
    assert error.value.code == 'ADAPTIVE_INQUIRY_UNIT_NOT_APPROVED'


@pytest.mark.parametrize('change', ['duplicate_unit', 'unknown_field', 'missing_evidence', 'time', 'quota'])
def test_rehashed_infeasible_or_malformed_scope_is_rejected(change):
    contract = contract_for_pool()
    scope = contract['assessment_scope']
    if change == 'duplicate_unit':
        scope['selected_units'][-1] = scope['selected_units'][0]
    elif change == 'unknown_field':
        scope['unreviewed_rule'] = 'anything'
    elif change == 'missing_evidence':
        contract['competencies'][0]['min_evidence_roots'] = 10
    elif change == 'time':
        scope['estimated_required_seconds'] = 1
    else:
        scope['source_question_counts']['resume_experience'] = 10
    rehash(contract)
    with pytest.raises(ApiError):
        validate_assessment_contract(contract)


def test_scope_is_deterministic_and_does_not_mutate_pool():
    pool = synthetic_pool()
    before = deepcopy(pool)
    a = contract_for_pool(candidates=pool)
    b = contract_for_pool(candidates=list(reversed(pool)))
    assert a['assessment_scope'] == b['assessment_scope']
    assert a['competencies'] == b['competencies']
    assert pool == before


@pytest.mark.parametrize('change', ['extra_contract', 'extra_competency', 'boolean_count', 'string_weight', 'nan_weight', 'empty_scope'])
def test_new_contract_schema_rejects_invalid_values_before_domain_use(change):
    contract = contract_for_pool()
    if change == 'extra_contract':
        contract['unreviewed_rule'] = True
    elif change == 'extra_competency':
        contract['competencies'][0]['optional_override'] = True
    elif change == 'boolean_count':
        contract['budget']['max_root_questions'] = True
    elif change == 'string_weight':
        contract['competencies'][0]['weight'] = '1'
    elif change == 'nan_weight':
        contract['competencies'][0]['weight'] = float('nan')
    else:
        contract['assessment_scope']['selected_units'] = []
    # The strict seam rejects malformed data even before a valid content hash
    # exists; a storage/import caller always receives the structured error.
    with pytest.raises(ApiError) as error:
        validate_assessment_contract(contract)
    assert error.value.code == 'ASSESSMENT_CONTRACT_INVALID'


def test_unachievable_time_budget_is_rejected_before_an_interview_can_start():
    pool = synthetic_pool()
    for candidate in pool:
        candidate['expected_minutes'] = 1000
    with pytest.raises(ApiError) as error:
        contract_for_pool(candidates=pool, duration_minutes=10)
    assert error.value.code == 'ASSESSMENT_COVERAGE_BUDGET_INVALID'


def test_short_interview_freezes_fewer_questions_without_claiming_full_pool_coverage():
    from app.domain.adaptive_interview import _hash
    pool = synthetic_pool()
    candidate = pool[0]
    candidate['competency_ids'].append('one_available_evidence')
    unit = candidate['inquiry_units'][0]
    unit['competency_ids'].append('one_available_evidence')
    unit['content_hash'] = _hash({key: value for key, value in unit.items() if key != 'content_hash'})
    weights = {name: 1 for row in pool for item in row['inquiry_units'] for name in item['competency_ids']}
    contract = contract_for_pool(candidates=pool, dimension_weights=weights, duration_minutes=5)
    assert len(contract['assessment_scope']['selected_units']) == 1
    assert contract['budget']['max_root_questions'] == 1
    assert contract['assessment_scope']['excluded_competency_ids']
    assert contract['assessment_scope']['estimated_required_seconds'] <= 270
    assert contract['assessment_scope']['included_competency_ids'] == ['one_available_evidence']


def test_short_budget_cannot_reduce_two_required_evidence_units_to_one():
    with pytest.raises(ApiError) as error:
        contract_for_pool(duration_minutes=5)
    assert error.value.code == 'ASSESSMENT_COVERAGE_BUDGET_INVALID'


def test_approved_resume_competency_receives_frozen_slots_with_independent_evidence():
    from app.domain.adaptive_interview import _hash
    pool = synthetic_pool()
    for candidate in pool:
        if candidate['source_type'] == 'resume_experience':
            candidate['competency_ids'] = ['resume_experience']
            for unit in candidate['inquiry_units']:
                unit['competency_ids'] = ['resume_experience']
                unit['content_hash'] = _hash({key: value for key, value in unit.items() if key != 'content_hash'})
    weights = {name: 1 for candidate in pool for unit in candidate['inquiry_units'] for name in unit['competency_ids']}
    contract = contract_for_pool(candidates=pool, dimension_weights=weights)
    scope = contract['assessment_scope']
    assert scope['source_question_counts']['resume_experience'] == 2
    assert 'resume_experience' in scope['included_competency_ids']
    resume = next(item for item in contract['competencies'] if item['id'] == 'resume_experience')
    assert resume['min_evidence_roots'] == 2
    assert sum(scope['source_question_counts'].values()) <= 10


@pytest.mark.anyio
async def test_real_prepare_path_freezes_two_evidence_and_a_combined_achievable_scope():
    from tests.test_resume_tail import setup_plan
    from tests.test_plan_assembly import add_question
    from tests.test_plan_coverage_diagnostics import UnitGateway
    store, catalog, assembly, request, _, _ = await setup_plan()
    with assembly.persistence.transaction('org_default') as transaction:
        for item in transaction.experience_questions.list():
            item['key_points'] = [{'id': 'project_evidence', 'text': '协程', 'weight': 1}]
            transaction.experience_questions.update(item, expected_version=item['version'])
    for index in range(8):
        await add_question(catalog, request.knowledge_base_ids[0], title='边界方案 %s' % index,
            skill='python', difficulty='mid', key_points=['边界%s' % index, '验证%s' % index, '异常%s' % index])
    assembly.catalog.gateway = UnitGateway()
    plan = await assembly.prepare(job_position_id=request.job_position_id,
        candidate_profile_id=request.candidate_profile_id, knowledge_base_ids=request.knowledge_base_ids)
    contract = plan['assessment_contract']
    assert plan['status'] == 'approved'
    assert plan['adaptive_policy']['min_evidence_units_per_competency'] == 2
    assert 8 <= len(contract['assessment_scope']['selected_units']) <= 10
    assert contract['assessment_scope']['source_question_counts']['resume_experience'] == 2
    assert all(item['min_evidence_roots'] == 2 for item in contract['competencies'])
    session = session_with(contract)
    for candidate, unit in planned_pairs(contract):
        session = scored(submit_unit(choose(session, candidate, unit).session), 85).session
    report = ReportService(InMemoryStore()).build_report(end(session).session, trigger_reason='test')
    assert report['overall_score'] == 85


@pytest.mark.parametrize('minimum,expected_resume', [(3, 3), (4, None)])
def test_custom_evidence_minimum_never_silently_removes_approved_resume_scope(minimum, expected_resume):
    from app.domain.adaptive_interview import _hash
    pool = synthetic_pool()
    for candidate in pool:
        if candidate['source_type'] == 'resume_experience':
            candidate['competency_ids'] = ['resume_experience']
            for unit in candidate['inquiry_units']:
                unit['competency_ids'] = ['resume_experience']
                unit['content_hash'] = _hash({key: value for key, value in unit.items() if key != 'content_hash'})
    weights = {name: 1 for candidate in pool for unit in candidate['inquiry_units'] for name in unit['competency_ids']}
    if expected_resume is None:
        with pytest.raises(ApiError) as error:
            contract_for_pool(candidates=pool, dimension_weights=weights,
                              policy={'min_evidence_units_per_competency': minimum})
        assert error.value.code == 'ASSESSMENT_COVERAGE_BUDGET_INVALID'
    else:
        contract = contract_for_pool(candidates=pool, dimension_weights=weights,
                                    policy={'min_evidence_units_per_competency': minimum})
        assert contract['assessment_scope']['source_question_counts']['resume_experience'] == expected_resume
        assert 'resume_experience' in contract['assessment_scope']['included_competency_ids']
