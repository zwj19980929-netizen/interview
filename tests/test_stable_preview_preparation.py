"""Stable prefixes may prepare decisions, but only actual finals can commit."""

from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.model_gateway.schemas import StableTranscriptPreview

from test_prepared_decision_binding import TEXT, _payload, _setup
from test_prepared_turn_decision import Gateway, _data, _input, _service


def _preview(final, **updates):
    fields = {
        "stream_id": final.stream_id,
        "revision": 1,
        "text": final.text,
        "language": final.language,
        "confidence": final.confidence,
        "segments": final.segments,
        "provider": final.provider,
        "has_unstable_tail": False,
    }
    return StableTranscriptPreview(**{**fields, **updates})


async def _prepare(service, preview):
    return await service.prepare_streaming_decision(
        "iv_binding", "turn_binding", preview,
        snapshot_ref="evidence-checkpoint://synthetic_prefix/1/1300",
    )


@pytest.mark.anyio
async def test_stable_preview_prepares_without_fake_final_or_domain_writes(monkeypatch):
    service, final = _setup()
    preview = _preview(final)
    before = service.get_interview("iv_binding")
    private_prepare = service.conversation._prepare_decision
    inputs = []

    async def observe(utterance, turn, interview):
        inputs.append(utterance.model_dump(mode="json"))
        return await private_prepare(utterance, turn, interview)

    async def never_use_authoritative_entry(*args, **kwargs):
        raise AssertionError("a stable preview is not authoritative utterance evidence")

    monkeypatch.setattr(service.conversation, "_prepare_decision", observe)
    monkeypatch.setattr(service.conversation, "prepare_decision", never_use_authoritative_entry)
    monkeypatch.setattr(service.conversation, "understand", never_use_authoritative_entry)
    prepared = await _prepare(service, preview)

    assert len(inputs) == 1
    assert inputs[0]["is_final"] is False
    assert inputs[0]["authoritative"] is False
    assert inputs[0]["source"] == "server_streaming"
    assert inputs[0]["audio_uri"].startswith("evidence-checkpoint://")
    assert prepared.text == TEXT
    assert service.get_interview("iv_binding") == before
    assert not hasattr(preview, "is_final")


@pytest.mark.anyio
async def test_matching_real_final_can_use_preview_preparation_but_preview_never_commits():
    service, final = _setup()
    preview = _preview(final)
    prepared = await _prepare(service, preview)
    before = service.get_interview("iv_binding")

    with pytest.raises(ApiError) as exc:
        service.assert_prepared_streaming_decision(prepared, preview, "org_default")
    assert exc.value.code == "STREAMING_TRANSCRIPT_INVALID"
    assert service.get_interview("iv_binding") == before

    service.assert_prepared_streaming_decision(prepared, final, "org_default")
    result = await service.submit_streaming_answer(
        "iv_binding", _payload(final), prepared_decision=prepared,
    )
    assert result["accepted"] is True
    current = service.get_interview("iv_binding")
    assert len(current["answers"]) == 1
    utterance = current["turns"][0]["utterances"][0]
    assert utterance["is_final"] is True
    assert utterance["authoritative"] is True
    assert utterance["utterance_id"] != prepared.understanding.utterance_id
    assert utterance["audio_uri"] == _payload(final)["audio_uri"]


@pytest.mark.anyio
async def test_preview_composite_path_keeps_one_inference_and_exact_reference_contract():
    utterance, turn, interview = _input()
    _, final = _setup()
    preview = _preview(
        final, text=utterance.text,
        segments=[final.segments[0].model_copy(update={"text": utterance.text})],
    )
    gateway = Gateway(_data())
    before = deepcopy(interview)
    understanding, decision = await _service(gateway).prepare_preview(
        preview, turn, interview, snapshot_ref="evidence-checkpoint://synthetic/1/1",
    )
    assert len(gateway.requests) == 1
    assert gateway.requests[0].metadata["prompt_version"] == "interview_turn_decision.v7"
    assert understanding.evidence_quotes == [utterance.text]
    assert understanding.claims[0].evidence_quote == utterance.text
    assert decision["selected"] is True
    assert interview == before


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["understand", "prepare_decision"])
@pytest.mark.parametrize("source", ["preview", "partial", "forged_final_flag", "client_source"])
async def test_authoritative_entry_points_are_not_widened_to_preview_or_partial(method, source):
    utterance, turn, interview = _input()
    _, final = _setup()
    if source == "preview":
        value = _preview(final)
    elif source == "partial":
        value = utterance.model_copy(update={"authoritative": False, "is_final": False})
    elif source == "forged_final_flag":
        value = utterance.model_copy(update={"is_final": False})
    else:
        value = utterance.model_copy(update={"source": "human_intervention"})
    gateway = Gateway()
    with pytest.raises(ValueError, match="authoritative final"):
        await getattr(_service(gateway), method)(value, turn, interview)
    assert gateway.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    {"has_unstable_tail": True},
    {"text": " "},
    {"confidence": -1},
    {"confidence": float("nan")},
    {"provider": None},
    {"revision": 0},
])
async def test_invalid_or_unstable_typed_preview_is_rejected_before_inference(mutation, monkeypatch):
    service, final = _setup()
    preview = _preview(final).model_copy(update=mutation)
    before = service.get_interview("iv_binding")

    async def never_invoke(*args, **kwargs):
        raise AssertionError("invalid preview must not invoke a model")

    monkeypatch.setattr(service.gateway, "invoke", never_invoke)
    with pytest.raises(ApiError) as exc:
        await _prepare(service, preview)
    assert exc.value.code == "STREAMING_TRANSCRIPT_INVALID"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_preview_entry_rejects_ordinary_event_or_dictionary_before_model():
    utterance, turn, interview = _input()
    _, final = _setup()
    gateway = Gateway()
    for value in (
        final.model_copy(update={"is_final": False, "type": "transcript.partial"}),
        _preview(final).model_dump(mode="json"),
        utterance,
    ):
        with pytest.raises(ValueError, match="stable transcript preview"):
            await _service(gateway).prepare_preview(
                value, turn, interview, snapshot_ref="evidence-checkpoint://synthetic/1/1",
            )
    assert gateway.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    {"is_final": False},
    {"type": "transcript.partial"},
    {"provider": None},
    {"text": " "},
])
async def test_actual_final_gate_remains_strict_for_preparation_and_commit(mutation):
    service, final = _setup()
    prepared = await _prepare(service, _preview(final))
    invalid = final.model_copy(update=mutation)
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as exc:
        await _prepare(service, invalid)
    assert exc.value.code == "STREAMING_TRANSCRIPT_INVALID"
    with pytest.raises(ApiError) as exc:
        service.assert_prepared_streaming_decision(prepared, invalid, "org_default")
    assert exc.value.code == "STREAMING_TRANSCRIPT_INVALID"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
@pytest.mark.parametrize("mutate", [
    lambda p: p.update(final_transcript=TEXT + "后面还有新的回答。"),
    lambda p: p.update(confidence=0.7),
    lambda p: p.update(language="en-US"),
    lambda p: p.update(segments=[]),
    lambda p: p["segments"][0].update(text="供应商修订了稳定段"),
    lambda p: p["segments"][0].update(confidence=0.5),
    lambda p: p["segments"][0].update(start_ms=20),
    lambda p: p["segments"][0].update(end_ms=1400),
    lambda p: p["provider"].update(provider_id="other_stt"),
    lambda p: p["provider"].update(model="other_model"),
    lambda p: p["provider"].update(request_id="other_request"),
    lambda p: p["provider"].update(latency_ms=8),
])
async def test_changed_tail_or_metadata_invalidates_preview_result_without_domain_writes(mutate):
    service, final = _setup()
    prepared = await _prepare(service, _preview(final))
    payload = _payload(final)
    mutate(payload)
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as exc:
        await service.submit_streaming_answer("iv_binding", payload, prepared_decision=prepared)
    assert exc.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_context_bound_before_preview_inference_and_rechecked_at_final(monkeypatch):
    service, final = _setup()
    original = service.conversation.prepare_preview

    async def change_context_after_inference(*args, **kwargs):
        result = await original(*args, **kwargs)
        with service.persistence.transaction("org_default") as transaction:
            session = transaction.interview_sessions.get("iv_binding")
            session["followup_policy"] = {"max_depth": 0}
            transaction.interview_sessions.update(session, expected_version=session["version"])
        return result

    monkeypatch.setattr(service.conversation, "prepare_preview", change_context_after_inference)
    prepared = await _prepare(service, _preview(final))
    with pytest.raises(ApiError) as exc:
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert exc.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding")["answers"] == []
