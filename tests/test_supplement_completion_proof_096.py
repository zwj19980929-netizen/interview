"""A semantic aside can preserve a real confirmation, never invent one."""
from dataclasses import replace
import asyncio
import copy
import gc
import weakref
from types import SimpleNamespace

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.supplement_reply import validate_supplement_reply
from app.core.prompt.validation import StructuredResponseValidationError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from test_answer_reuse_078 import _append, _delivered
from test_off_topic_semantics_068 import off_topic_response
from test_preparation_fences_081 import _chain
from test_prepared_turn_decision import Gateway


def _wire(intent="finish", effect="unchanged", evidence="E1"):
    return {"intent": intent, "answer_effect": effect, "confidence": .95,
            "evidence_id": evidence, "reply_text": ""}


async def prepared_harness(tmp_path, monkeypatch, *, storage="sqlite"):
    harness = _chain(tmp_path, monkeypatch)
    if storage == "memory":
        from app.repositories.memory import InMemoryStore
        from app.services.evidence_coordination import EvidenceOwnershipCoordinator
        from app.services.interview_evidence import InterviewEvidenceChain
        from app.services.streaming_stt import StreamingInterviewSTT
        store = InMemoryStore()
        chain = InterviewEvidenceChain(store, "iv_binding")
        with chain.persistence.transaction("org_default") as tx:
            tx.interview_sessions.add(harness.stream.interviews.get_interview("iv_binding"))
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
        owner = coordinator.attach_control(interview_id="iv_binding", organization_id="org_default",
            connection_id="control_memory", local_instance_id="instance_memory")
        def guard():
            coordinator.assert_current(owner.commit_fence(), "org_default")
            coordinator.assert_control_current(owner)
        stream = StreamingInterviewSTT(store, "iv_binding", commit_fence=owner.commit_fence(), commit_guard=guard)
        stream.turn_id = "turn_binding"
        stream.last_media_checkpoint = dict(harness.stream.last_media_checkpoint)
        chain._stt = stream
        harness = SimpleNamespace(store=store, chain=chain, stream=stream, final=harness.final,
                                  coordinator=coordinator, owner=owner)
    service, base = harness.stream.interviews, harness.final
    service.conversation.gateway = Gateway(off_topic_response(base.text, ["幂等键"],
        technical=True, relevant=True)["understanding"])
    harness.prepared = await harness.chain.prepare_decision(base, semantic_first=True)
    _delivered(service)
    class SmallGateway:
        def __init__(self):
            self.requests, self.data = [], _wire()
        async def invoke(self, capability, request):
            self.requests.append(request)
            return ChatJSONResponse(data=self.data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    harness.small = service.conversation.gateway = SmallGateway()
    harness.stream.stream = SimpleNamespace(_final=base)
    return harness


async def classified(harness, reply, *, boundary=None, prior=None):
    boundary = boundary or harness.final
    final = _append(boundary, reply)
    harness.stream.stream._final = final
    decision = await harness.chain.classify_supplement_reply(reply, final=final,
        boundary_final=boundary, prior_confirmation_proof=prior)
    return final, decision


def compose(harness, final, decision, *, boundary=None, reply=""):
    return harness.chain.confirm_prepared_decision(harness.prepared, harness.final,
        boundary or harness.final, final, reply_text=reply,
        decision=decision, question_delivered=True)


def test_current_contract_explicitly_separates_answer_effect_from_completion():
    contract = prompt_contract("supplement_reply", {"reply": "我已经说完了。太好笑了。"})
    assert contract.version == "supplement_reply.v11"
    assert "answer_effect" in contract.response_schema["required"]
    result = validate_supplement_reply(_wire(), contract.response_schema,
                                      reply="我已经说完了。太好笑了。")
    assert result["answer_effect"] == "unchanged"
    assert result["evidence_quote"] == "我已经说完了。"


def test_prior_confirmation_aside_rule_has_priority_without_overriding_real_requests():
    contract = prompt_contract("supplement_reply", {"reply": "窗外好像下雨了。", "prior_confirmation": True})
    assert contract.version == "supplement_reply.v11"
    instructions = contract.messages[0].content
    assert "必须使用confirmation_unchanged" in instructions
    assert "主动请求另聊无关话题仍用off_topic" in instructions
    assert "真实请求优先" in instructions


@pytest.mark.parametrize("effect", ["unchanged", "uncertain", "updated"])
def test_unresolved_transcription_dispute_cannot_claim_the_answer_is_unchanged(effect):
    reply = "我没有补充，但刚才字幕里的队列名称不是我说的。"
    contract = prompt_contract("supplement_reply", {"reply": reply})
    response = {**_wire("transcript_correction", effect),
                "reply_text": "刚才这段可能没识别准确，可以按你的原意说明。"}
    if effect == "unchanged":
        with pytest.raises(StructuredResponseValidationError) as rejected:
            validate_supplement_reply(response, contract.response_schema, reply=reply)
        assert rejected.value.reason_code == "supplement_answer_effect_conflict"
        assert rejected.value.schema_path == "$.answer_effect"
    else:
        assert validate_supplement_reply(response, contract.response_schema, reply=reply)["answer_effect"] == effect


@pytest.mark.parametrize("effect", [None, "invented", True])
def test_answer_effect_is_required_and_strict(effect):
    wire = _wire(effect=effect)
    if effect is None:
        del wire["answer_effect"]
    contract = prompt_contract("supplement_reply", {"reply": "我已经说完了。"})
    with pytest.raises(StructuredResponseValidationError):
        validate_supplement_reply(wire, contract.response_schema, reply="我已经说完了。")


@pytest.mark.anyio
async def test_mixed_finish_receipt_keeps_all_audio_words_and_reuses_technical_result(tmp_path, monkeypatch):
    harness = _chain(tmp_path, monkeypatch)
    service, base = harness.stream.interviews, harness.final
    service.conversation.gateway = Gateway(off_topic_response(base.text, ["幂等键"],
        technical=True, relevant=True)["understanding"])
    prepared = await harness.chain.prepare_decision(base, semantic_first=True)
    assert prepared.understanding.problem is None
    _delivered(service)
    final = _append(base, "我已经说完了。太好笑了。")
    harness.stream.stream = SimpleNamespace(_final=final)
    calls = []
    class SmallGateway:
        async def invoke(self, capability, request):
            calls.append(request)
            return ChatJSONResponse(data=_wire(), usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    service.conversation.gateway = SmallGateway()
    decision = await harness.chain.classify_supplement_reply("我已经说完了。太好笑了。",
        final=final, boundary_final=base)
    assert decision.get("completion_proof") is not None
    completed = harness.chain.confirm_prepared_decision(prepared, base, base, final,
        reply_text="我已经说完了。太好笑了。", decision=decision, question_delivered=True)
    assert completed is not None and completed.completion_confirmed
    assert completed.understanding.claims == prepared.understanding.claims
    assert completed.understanding.completion_basis.evidence_quotes == ["我已经说完了。"]
    assert completed.text == final.text and len(calls) == 1
    service.assert_prepared_streaming_decision(completed, final, "org_default")


@pytest.mark.anyio
async def test_plain_finish_dictionary_cannot_authorize_mixed_tail_reuse():
    from test_supplement_commit_gate_073 import _ordinary
    service, base, prepared = await _ordinary()
    _delivered(service)
    reply = "我已经说完了。太好笑了。"
    final = _append(base, reply)
    assert service.confirm_prepared_streaming_decision(prepared, base, base, final,
        reply_text=reply, decision={"intent": "finish", "confidence": .95,
            "answer_effect": "unchanged", "completion_proof": {}}, question_delivered=True) is None


@pytest.mark.anyio
@pytest.mark.parametrize("effect", ["updated", "uncertain", "invented", None, True])
async def test_explicit_answer_effect_cannot_fall_back_to_legacy_pure_phrase_reuse(effect):
    from test_supplement_commit_gate_073 import _ordinary
    service, base, prepared = await _ordinary()
    _delivered(service)
    reply = "我已经说完了。"
    assert service.confirm_prepared_streaming_decision(prepared, base, base, _append(base, reply),
        reply_text=reply, decision={"intent": "finish", "confidence": .95, "answer_effect": effect},
        question_delivered=True) is None


@pytest.mark.anyio
@pytest.mark.parametrize("reply", ["我已经说完了，太好笑了吧", "这就是我的全部思路，今天真有意思", "我已经说完了。太好笑了。"])
async def test_semantically_confirmed_mixed_and_natural_finish_never_requires_phrase_dictionary(tmp_path, monkeypatch, reply):
    harness = await prepared_harness(tmp_path, monkeypatch)
    final, decision = await classified(harness, reply)
    completed = compose(harness, final, decision)
    assert completed is not None and completed.text == final.text
    assert completed.understanding.claims == harness.prepared.understanding.claims
    assert completed.understanding.completion_basis.evidence_quotes == [decision["evidence_quote"]]
    from app.services.prepared_answer_completion import pure_completion_reply
    assert not pure_completion_reply(reply), "The old pure control eligibility remains narrow"


@pytest.mark.anyio
async def test_following_aside_uses_same_root_answer_and_original_real_finish(tmp_path, monkeypatch):
    harness = await prepared_harness(tmp_path, monkeypatch)
    first, decision = await classified(harness, "我已经说完了。太好笑了。")
    first_prepared = compose(harness, first, decision)
    assert first_prepared is not None
    # A provider request rollover inside the same authoritative capture does
    # not invalidate the first confirmation fact.
    harness.stream.stream._stream = object()
    harness.small.data = _wire("confirmation_unchanged")
    current, next_decision = await classified(harness, "窗外的鸟真有趣。", boundary=first,
                                            prior=decision["completion_proof"])
    assert next_decision.get("completion_proof") is not None
    prepared = compose(harness, current, next_decision, boundary=first)
    assert prepared is not None and prepared.completion_receipt.source is harness.prepared
    assert prepared.understanding.claims == harness.prepared.understanding.claims
    assert prepared.understanding.completion_basis.evidence_quotes == ["我已经说完了。"]
    harness.stream.interviews.assert_prepared_streaming_decision(prepared, current, "org_default")
    # A composed receipt itself is never a new technical source.
    assert harness.chain.confirm_prepared_decision(first_prepared, first, first, current,
        reply_text="窗外的鸟真有趣。", decision=next_decision, question_delivered=True) is None


@pytest.mark.anyio
@pytest.mark.parametrize("intent,effect", [("finish", "updated"), ("finish", "uncertain"),
    ("supplement", "updated"), ("off_topic", "unchanged"), ("stop_interview", "unchanged"),
    ("finish_topic", "unchanged"), ("continue", "updated")])
async def test_current_changes_and_other_controls_never_issue_unchanged_answer_proof(tmp_path, monkeypatch, intent, effect):
    harness = await prepared_harness(tmp_path, monkeypatch)
    harness.small.data = _wire(intent, effect)
    final, decision = await classified(harness, "补充、改口或其它真实控制的合成原文。")
    assert "completion_proof" not in decision
    assert compose(harness, final, decision, reply="补充、改口或其它真实控制的合成原文。") is None


@pytest.mark.anyio
@pytest.mark.parametrize("pretend", [True, {}, "proof", "fresh_object", "copy", "replace"])
async def test_unissued_or_copied_tokens_never_preserve_confirmation(tmp_path, monkeypatch, pretend):
    from app.services.supplement_completion_proof import SupplementCompletionProof
    harness = await prepared_harness(tmp_path, monkeypatch)
    first, decision = await classified(harness, "我已经说完了。太好笑了。")
    proof = decision["completion_proof"]
    if pretend == "fresh_object": pretend = SupplementCompletionProof()
    elif pretend == "copy": pretend = copy.copy(proof)
    elif pretend == "replace": pretend = replace(proof)
    harness.small.data = _wire("confirmation_unchanged")
    with pytest.raises(StructuredResponseValidationError):
        await classified(harness, "窗外的鸟真有趣。", boundary=first, prior=pretend)


@pytest.mark.anyio
async def test_bare_prior_confirmation_boolean_cannot_create_prior_authority(tmp_path, monkeypatch):
    harness = await prepared_harness(tmp_path, monkeypatch)
    harness.small.data = _wire("confirmation_unchanged")
    final = _append(harness.final, "窗外的鸟真有趣。")
    harness.stream.stream._final = final
    with pytest.raises(StructuredResponseValidationError):
        await harness.chain.classify_supplement_reply("窗外的鸟真有趣。", final=final,
            boundary_final=harness.final, prior_confirmation=True,
            preceding_text="我已经说完了。")


@pytest.mark.anyio
async def test_proof_registry_does_not_retain_dropped_capture_tokens(tmp_path, monkeypatch):
    from app.services.supplement_completion_proof import _ISSUED
    harness = await prepared_harness(tmp_path, monkeypatch)
    _, decision = await classified(harness, "我已经说完了。太好笑了。")
    proof = decision.pop("completion_proof")
    reference, count = weakref.ref(proof), len(_ISSUED)
    del proof
    gc.collect()
    assert reference() is None and len(_ISSUED) < count


@pytest.mark.anyio
async def test_failed_tail_ledger_and_registry_do_not_keep_a_closed_chain_alive(tmp_path, monkeypatch):
    harness = await prepared_harness(tmp_path, monkeypatch)
    _, decision = await classified(harness, "我已经说完了。太好笑了。")
    proof = decision["completion_proof"]
    harness.chain._supplement_reply_failure = (proof,)
    proof_reference, chain_reference = weakref.ref(proof), weakref.ref(harness.chain)
    del proof, decision, harness
    # Let completed inference tasks release their inherited correction context.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    assert chain_reference() is None and proof_reference() is None


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["text", "quality", "timing", "provider", "language", "question", "policy", "performance", "capture", "generation", "owner", "control"])
async def test_semantic_receipt_rechecks_every_source_and_context_at_preflight(tmp_path, monkeypatch, change):
    from app.core.errors import ApiError
    harness = await prepared_harness(tmp_path, monkeypatch)
    final, decision = await classified(harness, "我已经说完了。太好笑了。")
    prepared = compose(harness, final, decision)
    assert prepared is not None
    if change == "text": final = _append(final, "我还要补充。")
    elif change == "quality": final.confidence = .2
    elif change == "timing": final.segments[0].end_ms -= 1
    elif change == "provider": final.provider.model = "changed"
    elif change == "language": final.language = "en-US"
    elif change == "capture": harness.chain._stt = None
    elif change == "generation": harness.stream._capture_generation += 1
    elif change == "owner": harness.coordinator.release(harness.owner)
    elif change == "control":
        harness.coordinator.attach_control(interview_id="iv_binding", organization_id="org_default",
            connection_id="new_control", local_instance_id="instance_original")
    else:
        with harness.chain.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            if change == "question": session["turns"][0]["question_spoken_text"] = "新的问题。"
            elif change == "policy": session["followup_policy"] = {"max_depth": 0}
            else: session["turns"][0]["conversation_acts"][-1]["selection_receipts"][-1]["delivery"]["performance_id"] = "changed"
            tx.interview_sessions.update(session, expected_version=session["version"])
    with pytest.raises(ApiError):
        harness.stream.interviews.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert not harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("storage", ["memory", "sqlite"])
@pytest.mark.parametrize("aside", [False, True])
async def test_mixed_finish_commits_once_with_full_transcript_and_no_second_understanding(tmp_path, monkeypatch, storage, aside):
    from test_prepared_decision_binding import _payload
    harness = await prepared_harness(tmp_path, monkeypatch, storage=storage)
    final, decision = await classified(harness, "我已经说完了。太好笑了。")
    if aside:
        harness.small.data = _wire("confirmation_unchanged")
        final, decision = await classified(harness, "窗外的鸟真有趣。", boundary=final,
                                            prior=decision["completion_proof"])
    prepared = compose(harness, final, decision)
    result = await harness.stream.interviews.submit_streaming_answer("iv_binding", _payload(final),
        prepared_decision=prepared)
    assert result["accepted"] and result["answer"]["final_transcript"] == final.text
    assert result["answer"]["transcript_segments"] == _payload(final)["segments"]
    assert len(harness.small.requests) == (2 if aside else 1)
    assert len(harness.stream.interviews.get_interview("iv_binding")["answers"]) == 1
    if storage == "sqlite":
        from app.repositories.sqlite import SQLiteStore
        from app.services.interviews import InterviewService
        reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3")))
        assert reopened.get_interview("iv_binding")["answers"][0]["final_transcript"] == final.text


@pytest.mark.anyio
@pytest.mark.parametrize("change,expected", [(None, "supplement_reply.v12"),
    ("context", "supplement_reply.v11"), ("source", "supplement_reply.v11"),
    ("syntax", "supplement_reply.v11"), ("terminal", "supplement_reply.v11"),
    ("no_failure", "supplement_reply.v11")])
async def test_format_correction_binds_the_actual_failure_source_and_context(tmp_path, monkeypatch, change, expected):
    from app.model_gateway.errors import ProviderError
    harness = await prepared_harness(tmp_path, monkeypatch)
    reply = "我已经说完了。太好笑了。"
    final = _append(harness.final, reply).model_copy(deep=True)
    harness.stream.stream._final = final
    requests = []
    class GatewayWithOneFailure:
        async def invoke(self, capability, request):
            requests.append(request)
            if len(requests) == 1 and change != "no_failure":
                raise ProviderError("provider_schema_invalid", "sensitive-rejected-body-sentinel",
                    retryable=change != "terminal", details={"json_failure_kind": "invalid_json_object",
                        "json_decode_reason": "syntax_error" if change == "syntax" else "non_object"})
            return ChatJSONResponse(data=_wire(), usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    harness.stream.interviews.conversation.gateway = GatewayWithOneFailure()
    if change != "no_failure":
        with pytest.raises(ProviderError):
            await harness.chain.classify_supplement_reply(reply, final=final, boundary_final=harness.final)
    if change == "context":
        with harness.chain.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            session["turns"][0]["question_spoken_text"] = "新的正式问题。"
            tx.interview_sessions.update(session, expected_version=session["version"])
    elif change == "source":
        final.confidence = .85
    result = await harness.chain.classify_supplement_reply(reply, final=final,
        boundary_final=harness.final, correction_reason="invalid_json_object")
    assert result["intent"] == "finish"
    assert requests[-1].metadata["prompt_version"] == expected
    assert result["reply_prompt_version"] == expected
    assert len(requests) == (1 if change == "no_failure" else 2)
    assert all("sensitive-rejected-body-sentinel" not in str(request.messages) for request in requests)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["updated", "uncertain", "low_confidence", "same_words_changed_quality",
                                    "overlap", "missing_segments", "provider_changed", "rewritten_prefix"])
async def test_proof_creation_cannot_hide_technical_or_source_uncertainty(tmp_path, monkeypatch, change):
    harness = await prepared_harness(tmp_path, monkeypatch)
    reply = "我已经说完了。太好笑了。"
    final = _append(harness.final, reply).model_copy(deep=True)
    if change in {"updated", "uncertain"}: harness.small.data["answer_effect"] = change
    elif change == "low_confidence": harness.small.data["confidence"] = .5
    elif change == "same_words_changed_quality": final.segments[0].confidence = .7
    elif change == "overlap": final.segments[-1].start_ms = 1
    elif change == "missing_segments": final.segments = []
    elif change == "provider_changed": final.provider.model = "different"
    else:
        final.text = "改写了原技术回答。" + reply
        final.segments[0].text = "改写了原技术回答。"
    harness.stream.stream._final = final
    result = await harness.chain.classify_supplement_reply(reply, final=final, boundary_final=harness.final)
    assert "completion_proof" not in result
    assert compose(harness, final, result, reply=reply) is None
