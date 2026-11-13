"""Realistic generation volume, exact source references and failure containment."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.inquiry_units import answer_excerpts, inquiry_units_contract, validate_inquiry_units
from app.model_gateway.errors import ProviderError
from tests.test_bank_preparation import prepared_scope
from tests.test_plan_coverage_diagnostics import UnitGateway
from tests.test_inquiry_unit_assessment import SOURCE, PROPOSAL


def synthetic_questions():
    # Ten sources / 51 points, matching the observed failing bank's size.
    topics = [
        ('幂等任务', ['业务唯一键', '重复请求返回', '并发状态检查', '副作用与事务边界']),
        ('缓存一致性', ['读取回源', '缓存失效', '过期时间', '热点保护']),
        ('数据库索引', ['联合索引顺序', '查询执行计划', '写入成本', '慢查询定位']),
        ('接口鉴权', ['访问身份验证', '资源归属检查', '凭据过期', '最小访问权限']),
        ('异步队列', ['任务持久化', '消费确认', '临时失败重试', '死信处理', '队列积压监控']),
        ('文件上传', ['大小上限', '文件类型验证', '分片重传', '存储访问隔离', '过期对象清理']),
        ('服务可观测性', ['请求关联标识', '结构化日志', '错误比率', '延迟分位数', '依赖追踪', '告警阈值', '敏感字段脱敏']),
        ('资源控制', ['连接池上限', '有界工作队列', '请求超时', '并发限额', '内存水位', '背压信号', '资源释放']),
        ('发布与恢复', ['兼容数据变更', '灰度验证', '就绪检查', '优雅停止', '回滚路径', '故障演练']),
        ('事件通知', ['事件持久化', '投递重试', '接收方去重', '签名校验', '失败记录']),
    ]
    result = []
    for index, (title, points) in enumerate(topics):
        result.append({'title': title, 'question_text': f'在一个有多个调用方的后端系统中设计{title}方案，说明正常与失败路径下的行为。请围绕' + '、'.join(points) + '逐项讨论实现、边界和验证方式，并解释发生局部失败后如何保证服务可恢复。',
            'standard_answer': ''.join(f'关于{point}，应明确该机制所负责的边界和正常行为，定义失败时的处理路径并记录可追踪的结果，通过针对正常、重复和异常输入的验证确认行为符合预期；不要依赖其他组件永不失败的假设，应在该机制适用范围内说明具体取舍。' for point in points),
            'key_points': points, 'skills': ['python'], 'difficulty': 'mid', 'rubric': {'semantic_correctness': 1.0}})
    return result


def candidates_with_counts(counts):
    return [{'frozen_question': {**deepcopy(SOURCE), 'id': f'source_{index}',
                'key_points': [{'id': f'p{point}', 'text': f'边界{point}'} for point in range(count)]},
             'competency_ids': ['python']} for index, count in enumerate(counts)]


@pytest.mark.anyio
async def test_large_sources_are_partitioned_by_points_and_all_units_merged_in_source_order():
    _, _, assembly, _, _ = await prepared_scope()
    gateway = UnitGateway()
    assembly.catalog.gateway = gateway
    counts = [4, 4, 4, 4, 5, 5, 7, 7, 6, 5, 3, 3, 3, 20]
    candidates = candidates_with_counts(counts)
    actual = await assembly._prepare_inquiry_units(candidates, 'org_default')
    assert len(gateway.calls) == 19  # 80 points; each batch belongs to one source.
    for request in gateway.calls:
        sources = json.loads(request.messages[-1].content)['questions']
        assert len(sources) == 1
        assert sum(len(q['key_points']) for q in sources) <= 6
        assert len({q['id'] for q in sources}) == len(sources)
        assert request.max_output_tokens == 3500
        assert request.execution_budget.max_provider_retries == 0
    for candidate, count in zip(candidates, counts):
        units = actual[candidate['frozen_question']['id']]
        assert [u['assessed_rubric_point_ids'][0] for u in units] == [f'p{i}' for i in range(count)]
        assert len({u['id'] for u in units}) == count
        assert all(u['standard_answer_quote'] in SOURCE['standard_answer'] for u in units)
        assert all(u['prompt_version'] == 'interview_inquiry_units.v2' for u in units)


@pytest.mark.parametrize('answer', ['', '单行没有句号', '行一。\n 行二!?\r\n\n尾部', 'x' * 1201 + '。', '1.25; 版本2.0\n'])
def test_answer_excerpts_preserve_every_original_character(answer):
    excerpts = answer_excerpts(answer)
    assert ''.join(e['text'] for e in excerpts) == answer
    assert all(0 < len(e['text']) <= 400 for e in excerpts)


def test_model_selects_reference_range_without_repeating_or_rewriting_approved_answers():
    contract = inquiry_units_contract([SOURCE])
    assert contract.version == 'interview_inquiry_units.v2'
    request = json.loads(contract.messages[-1].content)['questions'][0]
    assert 'standard_answer' not in request
    assert ''.join(e['text'] for e in request['answer_excerpts']) == SOURCE['standard_answer']
    data = deepcopy(PROPOSAL)
    data['questions'][0]['units'][0]['answer_excerpt_end'] = 'a2'
    unit = validate_inquiry_units(data, [SOURCE])[SOURCE['id']][0]
    assert unit['standard_answer_quote'] == '使用幂等键避免重复执行。对暂时故障采用指数退避重试。'
    assert 'answer_excerpt_start' not in unit and 'answer_excerpt_end' not in unit


@pytest.mark.parametrize('start,end', [('a2', 'a1'), ('foreign', 'a1'), ('a1', None), (True, 'a1')])
def test_invalid_reference_ranges_are_rejected(start, end):
    data = deepcopy(PROPOSAL)
    data['questions'][0]['units'][0].update(answer_excerpt_start=start, answer_excerpt_end=end)
    with pytest.raises(ValueError):
        validate_inquiry_units(data, [SOURCE])


def test_reference_cannot_borrow_a_range_from_another_source_or_exceed_quote_limit():
    other = {**deepcopy(SOURCE), 'id': 'other', 'standard_answer': 'x' * 2400}
    row = deepcopy(PROPOSAL['questions'][0]); row['question_id'] = 'other'
    data = deepcopy(PROPOSAL); data['questions'].append(row)
    data['questions'][0]['units'][0]['answer_excerpt_end'] = 'a6'
    with pytest.raises(ValueError, match='own source'):
        validate_inquiry_units(data, [SOURCE, other])
    data['questions'][0]['units'][0]['answer_excerpt_end'] = 'a1'
    data['questions'][1]['units'][0]['answer_excerpt_end'] = 'a6'
    with pytest.raises(ValueError, match='exact approved'):
        validate_inquiry_units(data, [SOURCE, other])


@pytest.mark.anyio
@pytest.mark.parametrize('failure,code', [
    (ProviderError('provider_timeout', 'private provider response'), 'INQUIRY_UNIT_GENERATION_TIMEOUT'),
    (ProviderError('provider_schema_invalid', 'private provider response'), 'INQUIRY_UNIT_RESPONSE_INVALID'),
    (ValueError('private question content'), 'INQUIRY_UNIT_RESPONSE_INVALID'),
    (ProviderError('provider_auth_failed', 'private credential'), 'INQUIRY_UNIT_PROVIDER_UNAVAILABLE'),
    (RuntimeError('private internal state'), 'INQUIRY_UNIT_GENERATION_FAILED'),
])
async def test_failure_cause_is_preserved_without_leaking_private_details_or_publishing(failure, code, caplog):
    store, _, assembly, args, _ = await prepared_scope()
    class FailedGateway:
        async def invoke(self, *args, **kwargs):
            raise failure
    assembly.catalog.gateway = FailedGateway()
    with pytest.raises(ApiError) as error:
        await assembly.prepare(**args)
    assert error.value.code == code and error.value.status_code == 503
    assert 'private' not in str(error.value) + caplog.text
    assert 'inquiry_preparation_failed' in caplog.text
    assert not store.interview_plans and not store.interviews and not store.interview_appointments


@pytest.mark.anyio
@pytest.mark.parametrize('failure_mode', ['batch_failure', 'total_timeout', 'caller_cancel'])
async def test_pending_model_work_is_cancelled_and_no_task_is_orphaned(monkeypatch, failure_mode):
    _, _, assembly, _, _ = await prepared_scope()
    started, stopped = [], []
    ready = asyncio.Event()
    class BlockingGateway:
        async def invoke(self, capability, request):
            started.append(len(started))
            number = started[-1]
            if len(started) == 3:
                ready.set()
            try:
                await ready.wait()
                if failure_mode == 'batch_failure' and number == 0:
                    raise ProviderError('provider_timeout', 'private timeout')
                await asyncio.Event().wait()
            finally:
                stopped.append(number)
    assembly.catalog.gateway = BlockingGateway()
    if failure_mode == 'total_timeout':
        monkeypatch.setattr('app.services.plan_assembly.INQUIRY_TOTAL_TIMEOUT_SECONDS', .02)
    task = asyncio.create_task(assembly._prepare_inquiry_units(candidates_with_counts([20, 20]), 'org_default'))
    await asyncio.wait_for(ready.wait(), 1)
    if failure_mode == 'caller_cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ApiError) as error:
            await task
        assert error.value.code == 'INQUIRY_UNIT_GENERATION_TIMEOUT'
    assert len(started) == len(stopped)


@pytest.mark.anyio
@pytest.mark.parametrize('persistent_failure', [False, True])
async def test_only_invalid_batch_is_repaired_once_and_successful_batch_is_not_regenerated(persistent_failure):
    from app.core.prompt.inquiry_units import mock_inquiry_units_result
    from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
    _, _, assembly, _, _ = await prepared_scope()
    calls = {}
    class RepairGateway:
        async def invoke(self, capability, request):
            source = json.loads(request.messages[-1].content)['questions'][0]['id']
            calls.setdefault(source, []).append(request)
            data = mock_inquiry_units_result(request)
            if source == 'source_0' and (len(calls[source]) == 1 or persistent_failure):
                data['questions'][0]['units'][0]['question_text'] = '你如何处理重复任务？你如何恢复失败？'
            return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(provider_id='synthetic', model='synthetic', request_id='fixture', latency_ms=0))
    assembly.catalog.gateway = RepairGateway()
    if persistent_failure:
        with pytest.raises(ApiError) as error:
            await assembly._prepare_inquiry_units(candidates_with_counts([6, 6]), 'org_default')
        assert error.value.code == 'INQUIRY_UNIT_RESPONSE_INVALID'
    else:
        units = await assembly._prepare_inquiry_units(candidates_with_counts([6, 6]), 'org_default')
        assert sum(len(group) for group in units.values()) == 12
    assert len(calls['source_0']) == 2
    assert len(calls['source_1']) == 1
    assert '上一次本批结果未通过校验' in calls['source_0'][1].messages[0].content
