"""Reuse must be content-scoped; transport progress cannot stand in for a save."""
import asyncio
from copy import deepcopy
import hashlib
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api.routers import plans as plan_routes
from app.core.errors import ApiError
from app.domain.adaptive_interview import validate_assessment_contract
from app.transport.http.progress import progress_response
from tests.test_bank_preparation import prepared_scope


def rehash(value, field):
    value[field] = "sha256:" + hashlib.sha256(json.dumps({k: v for k, v in value.items() if k != field},
        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


@pytest.mark.anyio
async def test_identical_preparation_reuses_all_sources_without_a_model_and_keeps_old_plan_immutable():
    store, _, assembly, args, _ = await prepared_scope()
    previous = await assembly.prepare(**args)
    snapshot = deepcopy(previous)
    before = len(assembly.catalog.gateway.calls)
    progress = []
    plan = await assembly.prepare(**args, on_progress=progress.append)
    assert len(assembly.catalog.gateway.calls) == before
    assert plan['id'] != previous['id']
    assert previous == snapshot
    assert len(store.interview_plans) == 2
    assert plan['inquiry_unit_snapshots'] == previous['inquiry_unit_snapshots']
    validate_assessment_contract(plan['assessment_contract'])
    assert progress == [{'stage': 'reading'}, {'stage': 'preparing', 'completed_points': 4,
        'total_points': 4, 'reused_points': 4}, {'stage': 'saving'}]


@pytest.mark.anyio
async def test_only_changed_source_is_generated_and_progress_starts_with_reuse():
    store, _, assembly, args, _ = await prepared_scope()
    await assembly.prepare(**args)
    before = len(assembly.catalog.gateway.calls)
    next(iter(store.questions.values()))['version'] += 1
    progress = []
    plan = await assembly.prepare(**args, on_progress=progress.append)
    assert len(assembly.catalog.gateway.calls) == before + 1
    assert [(p['completed_points'], p['reused_points']) for p in progress if p['stage'] == 'preparing'] == [(2, 2), (4, 2)]
    assert plan['status'] == 'approved'


@pytest.mark.anyio
@pytest.mark.parametrize('reason', ['draft', 'other_org', 'tampered_contract', 'old_prompt', 'changed_content'])
async def test_ineligible_frozen_sources_never_reuse(reason):
    store, _, assembly, args, _ = await prepared_scope()
    previous = await assembly.prepare(**args)
    cached = store.interview_plans[previous['id']]
    if reason == 'draft':
        cached['status'] = 'draft'
    elif reason == 'other_org':
        cached['organization_id'] = 'other_org'
    elif reason == 'tampered_contract':
        cached['assessment_contract']['candidate_questions'][0]['inquiry_units'][0]['question_text'] = '未经批准的题目？'
    elif reason == 'changed_content':
        for question in store.questions.values():
            question['standard_answer'] += '已修订的参考内容。'
    else:
        for question in cached['assessment_contract']['candidate_questions']:
            for unit in question['inquiry_units']:
                unit['prompt_version'] = 'interview_inquiry_units.v1'
                rehash(unit, 'content_hash')
        rehash(cached['assessment_contract'], 'contract_hash')
        validate_assessment_contract(cached['assessment_contract'])
    before = len(assembly.catalog.gateway.calls)
    await assembly.prepare(**args)
    assert len(assembly.catalog.gateway.calls) == before + 2


@pytest.mark.anyio
async def test_reuse_requires_original_generation_scope_and_resume_owner():
    store, _, assembly, args, _ = await prepared_scope()
    plan = await assembly.prepare(**args)
    candidate = plan['assessment_contract']['candidate_questions'][0]
    source = deepcopy(plan)
    source['assessment_contract']['candidate_questions'] = [deepcopy(candidate)]
    source['assessment_contract']['candidate_questions'][0]['competency_ids'].append('new_scope')
    assert assembly._reusable_inquiry_units(source, 'org_default') == {}
    # A valid synthetic frozen resume source with a different owner must miss.
    cached = store.interview_plans[plan['id']]
    for row in cached['assessment_contract']['candidate_questions']:
        row['source_type'] = 'resume_experience'
    rehash(cached['assessment_contract'], 'contract_hash')
    source = deepcopy(cached)
    source['candidate_profile_id'] = 'another_candidate'
    assert assembly._reusable_inquiry_units(source, 'org_default') == {}
    source = deepcopy(cached)
    source['resume_review_id'] = 'another_review'
    assert assembly._reusable_inquiry_units(source, 'org_default') == {}


@pytest.mark.anyio
async def test_http_stream_finishes_only_with_a_saved_plan_and_plain_json_still_works(monkeypatch):
    store, _, assembly, args, _ = await prepared_scope()
    app = FastAPI()
    app.include_router(plan_routes.router)
    monkeypatch.setattr(plan_routes, 'services', lambda: {'plan_assembly': assembly})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
        response = await api.post('/api/v1/interview-plans/prepare', json=args, headers={'Accept': 'text/event-stream'})
        assert response.headers['content-type'].startswith('text/event-stream')
        assert response.headers['x-accel-buffering'] == 'no'
        events = [frame.split('\ndata: ', 1) for frame in response.text.strip().split('\n\n')]
        assert events[0][0] == 'event: progress'
        assert events[-1][0] == 'event: complete'
        plan = json.loads(events[-1][1])
        assert store.interview_plans[plan['id']]['status'] == 'approved'
        plain = await api.post('/api/v1/interview-plans/prepare', json=args)
        assert plain.headers['content-type'] == 'application/json'
        assert plain.json()['status'] == 'approved'


@pytest.mark.anyio
@pytest.mark.parametrize('known', [True, False])
async def test_stream_errors_are_terminal_and_do_not_leak_unexpected_messages(known):
    async def work(notify):
        notify({'stage': 'reading'})
        await asyncio.sleep(0)
        if known:
            raise ApiError('INQUIRY_UNIT_GENERATION_TIMEOUT', '模型整理问题超时，计划尚未创建。', status_code=503)
        raise RuntimeError('secret response and credentials')
    response = progress_response(work)
    content = b''.join([chunk async for chunk in response.body_iterator]).decode()
    assert 'event: error' in content
    assert 'event: complete' not in content
    assert 'secret' not in content
    assert ('INQUIRY_UNIT_GENERATION_TIMEOUT' if known else 'PLAN_PREPARATION_FAILED') in content


@pytest.mark.anyio
async def test_disconnect_cancels_inflight_work_without_a_partial_plan():
    store, _, assembly, args, _ = await prepared_scope()
    started, cancelled = asyncio.Event(), asyncio.Event()
    class BlockingGateway:
        async def invoke(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
    assembly.catalog.gateway = BlockingGateway()
    response = progress_response(lambda notify: assembly.prepare(**args, on_progress=notify))
    first = await response.body_iterator.__anext__()
    assert b'event: progress' in first
    await asyncio.wait_for(started.wait(), 1)
    await response.body_iterator.aclose()
    await asyncio.wait_for(cancelled.wait(), 1)
    assert store.interview_plans == {}


@pytest.mark.anyio
async def test_stream_keeps_authenticated_organization_when_body_runs_after_route_context(monkeypatch):
    from types import SimpleNamespace
    from app.transport.service_locator import _OrganizationBoundService
    import app.transport.service_locator as locator
    class Service:
        async def prepare(self, *, organization_id='org_default', on_progress=None):
            await asyncio.sleep(0)
            return {'organization_id': organization_id}
    monkeypatch.setattr(locator, 'current_principal', lambda: SimpleNamespace(organization_id='authorized_tenant'))
    service = _OrganizationBoundService(Service())
    response = progress_response(lambda notify: service.prepare(on_progress=notify))
    monkeypatch.setattr(locator, 'current_principal', lambda: SimpleNamespace(organization_id='wrong_tenant'))
    content = b''.join([chunk async for chunk in response.body_iterator]).decode()
    assert 'authorized_tenant' in content
    assert 'wrong_tenant' not in content
