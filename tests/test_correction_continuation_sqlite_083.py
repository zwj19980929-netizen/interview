"""Bounded correction survives preview/final orchestration, never evidence fences."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.core.errors import ApiError
from app.repositories.sqlite import SQLiteStore
from app.services.interviews import InterviewService
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint
from test_answer_reuse_078 import _append
from test_off_topic_semantics_068 import off_topic_response
from test_preparation_fences_081 import _chain
from test_prepared_decision_binding import _payload
from test_stable_preview_preparation import _preview
from test_supplement_reply_sqlite_082 import _CaptureFacade


_REASON = "completion_has_unresolved_ambiguity"


class _CorrectionGateway:
    def __init__(self, *, reject_again=False):
        self.requests = []
        self.corrected = []
        self.reject_again = reject_again

    async def invoke(self, capability, request):
        self.requests.append(request)
        corrected = any(_REASON in message.content for message in request.messages)
        self.corrected.append(corrected)
        data = off_topic_response(request.metadata["transcript"], ["幂等键"],
                                  intent="finish_topic", technical=True, relevant=True)
        if not corrected or self.reject_again:
            data["understanding"]["ambiguities"] = ["合成证据存在两种不同语义，尚未确定其含义。"]
        if "understanding" not in request.json_schema["properties"]:
            data = data["understanding"]
        return ChatJSONResponse(data=deepcopy(data), usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="correction_083", request_id="synthetic_correction_083", latency_ms=1))


class _PreviewCapture(_CaptureFacade):
    supports_stable_preview = True

    async def transcript_preview(self):
        return _preview(self.current_final)


async def _started(tmp_path, monkeypatch, *, composite=False, reject_again=False, explicit_topic=True):
    harness = _chain(tmp_path, monkeypatch)
    if explicit_topic:
        harness.final = _append(harness.final, "我的回答已结束，请继续下一题。")
    service = harness.stream.interviews
    if composite:
        with service.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            session["turns"][0]["allow_followup"] = True
            session["followup_policy"] = {"max_total": 1, "max_per_root": 1, "max_depth": 1}
            tx.interview_sessions.update(session, expected_version=session["version"])
    gateway = service.conversation.gateway = _CorrectionGateway(reject_again=reject_again)
    capture = _PreviewCapture(harness)
    submitted, spoken = [], []

    async def commit(prepared, guard):
        guard()
        harness.chain.assert_preparation_current(prepared, capture.current_final, semantic_first=True)
        service.assert_prepared_streaming_decision(prepared, capture.current_final, "org_default")
        result = await service.submit_streaming_answer("iv_binding", _payload(capture.current_final),
            evidence_fence=harness.owner.commit_fence(), prepared_decision=prepared)
        assert result["accepted"]
        submitted.append(result)

    endpoint, clock, notices, _ = _endpoint(capture=capture, commit_impl=commit)

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        return False

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    clock.value = 3.1
    endpoint._proposal_revision = endpoint.revision
    endpoint.cover_transcript(capture.current_final)
    endpoint.prepare_stable_preview(_preview(capture.current_final))
    task = endpoint.preparation._task
    assert task is not None
    failed = await asyncio.wait_for(asyncio.shield(task), 1)
    assert failed.understanding.problem is not None
    assert len(gateway.requests) == 1 and gateway.corrected == [False]
    assert not service.get_interview("iv_binding")["answers"]
    return SimpleNamespace(**vars(harness), service=service, capture=capture, gateway=gateway,
                           endpoint=endpoint, clock=clock, submitted=submitted, spoken=spoken, notices=notices)


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_failed_preview_continues_with_reason_and_two_calls_commit_one_valid_answer(tmp_path, monkeypatch, composite):
    h = await _started(tmp_path, monkeypatch, composite=composite)
    try:
        await asyncio.wait_for(h.endpoint.confirmation.step(h.endpoint, quiet=5), 1)
        assert h.gateway.corrected == [False, True]
        assert len(h.gateway.requests) == 2 and len(h.submitted) == 1
        assert not h.endpoint.confirmation.confirmed, "Only the explicit next-topic evidence grants this boundary"
        assert not h.spoken
        assert h.capture.snapshots == [False, False]
        assert h.submitted[0]["understanding"]["turn_intent"] == "finish_topic"
        reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))).get_interview("iv_binding")
        assert len(reopened["answers"]) == 1
        assert reopened["answers"][0]["final_transcript"] == h.final.text
        assert reopened["answers"][0]["transcript_segments"] == _payload(h.final)["segments"]
        assert len(reopened["turns"][0]["utterances"]) == 1
    finally:
        await h.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_second_invalid_result_stops_at_two_calls_without_durable_answer(tmp_path, monkeypatch, composite):
    h = await _started(tmp_path, monkeypatch, composite=composite, reject_again=True)
    try:
        await asyncio.wait_for(h.endpoint.confirmation.step(h.endpoint, quiet=5), 1)
        assert h.gateway.corrected == [False, True]
        assert len(h.gateway.requests) == 2
        assert h.endpoint.preparation_blocked
        assert not h.submitted and not h.service.get_interview("iv_binding")["answers"]
        assert not h.service.get_interview("iv_binding")["turns"][0]["utterances"]
    finally:
        await h.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["provider", "confidence", "question", "policy", "mode"])
async def test_unchanged_words_do_not_carry_old_correction_across_evidence_context_or_mode(tmp_path, monkeypatch, change):
    h = await _started(tmp_path, monkeypatch, explicit_topic=change != "mode")
    try:
        if change == "provider":
            h.capture.current_final = h.final.model_copy(update={
                "provider": h.final.provider.model_copy(update={"request_id": "different_source_083"})})
        elif change == "confidence":
            h.capture.current_final = h.final.model_copy(update={"confidence": .8})
        elif change in {"question", "policy"}:
            with h.service.persistence.transaction("org_default") as tx:
                session = tx.interview_sessions.get("iv_binding")
                if change == "question":
                    session["turns"][0]["question_spoken_text"] = "请解释另一个合成问题。"
                else:
                    session["followup_policy"] = {"max_depth": 0}
                tx.interview_sessions.update(session, expected_version=session["version"])
        if change == "mode":
            # Changing semantic mode is a new analysis contract, never a
            # fabricated completion confirmation. This test does not submit.
            await h.endpoint._call_prepare_decision(h.final, semantic_first=False)
        else:
            await asyncio.wait_for(h.endpoint.confirmation.step(h.endpoint, quiet=5), 1)
        assert h.gateway.corrected == [False, False]
        assert len(h.gateway.requests) == 2 and not h.submitted
        assert not h.service.get_interview("iv_binding")["answers"]
    finally:
        await h.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("loss", ["capture", "owner", "control"])
async def test_lost_capture_or_owner_rejects_correction_before_another_provider_call(tmp_path, monkeypatch, loss):
    h = await _started(tmp_path, monkeypatch)
    try:
        if loss == "capture":
            h.chain._stt = None
        elif loss == "owner":
            assert h.coordinator.release(h.owner)
        else:
            h.coordinator.attach_control(interview_id="iv_binding", organization_id="org_default",
                connection_id="control_replaced_083", local_instance_id="instance_original")
        try:
            await asyncio.wait_for(h.endpoint.confirmation.step(h.endpoint, quiet=5), 1)
        except ApiError as exc:
            assert exc.code in {"AGENT_EVIDENCE_NOT_OPEN", "EVIDENCE_OWNER_FENCED", "EVIDENCE_CONTROL_STALE"}
        assert len(h.gateway.requests) == 1 and not h.submitted
        assert not h.service.get_interview("iv_binding")["answers"]
        assert not h.service.get_interview("iv_binding")["turns"][0]["utterances"]
    finally:
        await h.endpoint.close()
