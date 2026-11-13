"""Record bounded parsing facts, not model output, for future failure diagnosis."""
import json

import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from test_model_invocation import ScriptedAdapter, configured_store, request, route
from app.model_gateway.gateway import ModelGateway
from test_planning_json_diagnostics_094 import _gateway, _invoke


@pytest.mark.anyio
@pytest.mark.parametrize('content,reason,offset', [
    ('["private-value"]', 'non_object', None),
    ('{"private":"value",}', 'syntax_error', 19),
    ('{"private":"value"} trailing', 'extra_data', 20),
])
@pytest.mark.parametrize('backend', ['memory', 'sqlite'])
async def test_real_adapter_keeps_safe_failure_position_without_body(content, reason, offset, backend, tmp_path):
    gateway, store, calls = _gateway(content, backend=backend, tmp_path=tmp_path)
    with pytest.raises(ProviderError) as caught:
        await _invoke(gateway)
    assert caught.value.code == 'provider_schema_invalid'
    assert caught.value.details['json_failure_kind'] == 'invalid_json_object'
    row, = store.model_invocations
    assert row['json_decode_reason'] == reason
    assert row['json_error_offset'] == offset
    assert row['requested_response_format'] == 'json_schema'
    assert len(calls) == 1
    assert 'private' not in json.dumps(row)
    assert 'trailing' not in json.dumps(row)


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['json_object', 'prompt'])
async def test_requested_format_is_real_wire_mode(mode):
    gateway, store, calls = _gateway('{"private":')
    store.provider_connections['provider_conn_primary']['connection_config']['structured_output_mode'] = mode
    with pytest.raises(ProviderError):
        await _invoke(gateway)
    row, = store.model_invocations
    assert row['requested_response_format'] == mode
    wire = json.loads(calls[0].content)
    assert wire.get('response_format', {}).get('type', 'prompt') == mode


@pytest.mark.anyio
@pytest.mark.parametrize('reason,offset,mode', [
    ('private response', 'private', 'private'), ([], True, {}), ({}, -1, []),
    ('syntax_error', 10_000_001, None),
])
async def test_arbitrary_diagnostic_values_cannot_leak(reason, offset, mode):
    store = configured_store()
    error = ProviderError('provider_schema_invalid', 'not public', retryable=True, details={
        'json_decode_reason': reason, 'json_error_offset': offset, 'requested_response_format': mode,
    })
    gateway = ModelGateway(store, provider_clients={'openai_compatible': ScriptedAdapter([error])})
    with pytest.raises(ProviderError):
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route(fallback=False))
    row, = store.model_invocations
    assert row['json_decode_reason'] == ('syntax_error' if reason == 'syntax_error' else None)
    assert row['json_error_offset'] is None
    assert row['requested_response_format'] is None
    assert 'private' not in json.dumps(row)
