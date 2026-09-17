"""Behavior of the concise, bank-based preparation boundary."""
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import plans as plan_routes
from app.core.errors import ApiError, api_error_handler
from app.domain.adaptive_interview import validate_assessment_contract
from app.persistence.errors import ConcurrencyConflict
from tests.test_plan_coverage_diagnostics import scope, UnitGateway


async def prepared_scope():
    store, catalog, assembly, request, bank = await scope()
    args = dict(job_position_id=request.job_position_id, candidate_profile_id=request.candidate_profile_id,
                knowledge_base_ids=[bank['id']])
    return store, catalog, assembly, args, request


@pytest.mark.anyio
async def test_bank_preparation_ignores_old_flask_requirement_and_is_immediately_usable():
    store, _, assembly, args, _ = await prepared_scope()
    before_roles = deepcopy(store.role_requirements)
    plan = await assembly.prepare(**args)
    assert plan['status'] == 'approved'
    assert plan['approval_source'] == 'automatic_preparation'
    assert plan['role_requirement_id'] is None
    assert store.role_requirements == before_roles
    assert list(store.interview_plans) == [plan['id']]
    assert {row['id'] for row in plan['assessment_contract']['competencies']} == {'python', 'redis'}
    assert plan['assembly_summary']['warnings'] == []
    assert 'enterprise_skill_snapshot' not in plan and 'company_context' not in plan
    validate_assessment_contract(plan['assessment_contract'])
    assert plan['assessment_contract']['budget']['max_root_questions'] == 4


@pytest.mark.anyio
async def test_no_role_or_resume_review_is_required_and_short_interview_uses_available_points():
    store, _, assembly, args, _ = await prepared_scope()
    store.role_requirements.clear()
    plan = await assembly.prepare(**args, duration_minutes=5)
    assert plan['estimated_minutes'] == 5
    assert plan['resume_review_id'] is None
    assert plan['assessment_contract']['budget']['min_root_questions'] == 1
    assert plan['assessment_contract']['budget']['max_root_questions'] == 1
    assert store.role_requirements == {}


@pytest.mark.anyio
async def test_http_prepare_needs_only_position_candidate_and_bank(monkeypatch):
    store, _, assembly, args, _ = await prepared_scope()
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(plan_routes.router)
    monkeypatch.setattr(plan_routes, 'services', lambda: {'plan_assembly': assembly})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
        response = await api.post('/api/v1/interview-plans/prepare', json=args)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'approved'
        for patch in ({'question_count': 6}, {'duration_minutes': True}, {'duration_minutes': 121},
                      {'knowledge_base_ids': args['knowledge_base_ids'] * 2}, {'knowledge_base_ids': []}):
            result = await api.post('/api/v1/interview-plans/prepare', json={**args, **patch})
            assert result.status_code == 422
    assert len(store.interview_plans) == 1


@pytest.mark.anyio
async def test_cross_tenant_missing_scope_and_unready_sources_never_call_model_or_write():
    store, _, assembly, args, _ = await prepared_scope()
    with pytest.raises(ApiError):
        await assembly.prepare(**args, organization_id='other_org')
    with pytest.raises(ApiError):
        await assembly.prepare(**{**args, 'candidate_profile_id': 'absent'})
    bank = store.knowledge_bases[args['knowledge_base_ids'][0]]
    bank['status'] = 'draft'
    with pytest.raises(ApiError):
        await assembly.prepare(**args)
    assert not assembly.catalog.gateway.calls
    assert not store.interview_plans


@pytest.mark.anyio
@pytest.mark.parametrize('changed', ['position', 'question', 'candidate'])
async def test_changes_during_generation_cannot_publish_an_approved_plan(changed):
    store, _, assembly, args, _ = await prepared_scope()
    base = assembly.catalog.gateway
    class ChangingGateway:
        async def invoke(self, capability, request):
            result = await base.invoke(capability, request)
            if changed == 'position':
                store.job_positions[args['job_position_id']]['version'] += 1
            elif changed == 'question':
                next(iter(store.questions.values()))['version'] += 1
            else:
                store.candidate_profiles[args['candidate_profile_id']]['job_position_id'] = 'other'
            return result
    assembly.catalog.gateway = ChangingGateway()
    with pytest.raises(ConcurrencyConflict):
        await assembly.prepare(**args)
    assert not store.interview_plans


@pytest.mark.anyio
@pytest.mark.parametrize('has_skill,has_company', [(False, False), (True, False), (False, True), (True, True)])
async def test_preparation_uses_saved_optional_modules_independently(has_skill, has_company):
    from app.services.interview_customization import InterviewCustomizationService
    store, _, assembly, args, _ = await prepared_scope()
    InterviewCustomizationService(store).update({'expected_version': 0,
        'skill_instructions': '请自然交流，一次只问一个问题。' if has_skill else '',
        'company_profile': {'business_overview': '为物流公司提供调度软件。'} if has_company else {},
    }, actor_id='author')
    plan = await assembly.prepare(**args)
    assert bool(plan.get('enterprise_skill_snapshot')) is has_skill
    assert bool(plan.get('company_context')) is has_company
    assert plan['status'] == 'approved'


@pytest.mark.anyio
async def test_shared_bank_is_usable_only_after_explicit_position_assignment():
    from app.services.talent import TalentService
    store, catalog, assembly, args, _ = await prepared_scope()
    position = catalog.create_position({'code': 'another', 'name': '另一个招聘岗位'})
    candidate = TalentService(store).create_candidate({'name': '另一位候选人', 'email': 'other@example.com', 'phone': '13800138001', 'job_position_id': position['id']})
    args.update(job_position_id=position['id'], candidate_profile_id=candidate['id'])
    with pytest.raises(ApiError) as error:
        await assembly.prepare(**args)
    assert error.value.code == 'KNOWLEDGE_BASE_POSITION_MISMATCH'
    store.job_positions[position['id']]['knowledge_base_ids'] = args['knowledge_base_ids']
    plan = await assembly.prepare(**args)
    assert plan['status'] == 'approved'
    assert plan['assessment_basis']['job_position_id'] == position['id']


@pytest.mark.anyio
async def test_failed_or_invalid_generation_does_not_publish_a_plan():
    store, _, assembly, args, _ = await prepared_scope()
    class FailedGateway:
        async def invoke(self, *args, **kwargs):
            raise RuntimeError('synthetic provider failure')
    assembly.catalog.gateway = FailedGateway()
    with pytest.raises(ApiError):
        await assembly.prepare(**args)
    assert store.interview_plans == {}
