"""A fresh supplement reply uses one confirmed analysis behind real SQLite fences."""

from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.sqlite import SQLiteStore
from app.services.conversation_delivery import record_selection, update_delivery
from app.services.interviews import InterviewService
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint
from test_answer_reuse_078 import _append, _delivered
from test_off_topic_semantics_068 import off_topic_response
from test_preparation_fences_081 import _chain
from test_prepared_decision_binding import _payload


_EXPLANATION_REQUEST = "请说明这道题希望我解释哪部分。"
_NEW_REPLY = "我再补充用唯一约束解决并发重复请求。现在我的回答完毕了。"


def _delivery(service, act_id, status="delivered"):
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        update_delivery(session, event_id="event_" + act_id, performance_id="performance_" + act_id,
                        status=status, updated_at=utc_now())
        tx.interview_sessions.update(session, expected_version=session["version"])


def _explanation(service):
    """Persist a selected explanation and source-linked request, not just text."""
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        act = {"act_id": "explanation_082", "turn_id": turn["id"],
               "act_type": "question_clarification", "prompt_version": "synthetic_explanation.v1",
               "text": "请解释重复请求到来时如何保持结果一致。"}
        turn["conversation_acts"].append(act)
        sequence = len(session.get("agent_events", [])) + 1
        event = {"event_id": "event_explanation_082", "turn_id": turn["id"],
                 "session_sequence": sequence, "occurred_at": utc_now(), "type": "conversation.act.selected",
                 "payload": {"act_id": act["act_id"], "text": act["text"], "approved": True}}
        session.setdefault("agent_events", []).append(event)
        record_selection(session, event)
        turn.setdefault("reception_exchanges", []).append({
            "act_id": act["act_id"], "selected_event_id": event["event_id"],
            "session_sequence": sequence, "request_text": _EXPLANATION_REQUEST,
            "exchange_id": "exchange_explanation_082", "kind": "question_clarification",
        })
        tx.interview_sessions.update(session, expected_version=session["version"])


class _Gateway:
    def __init__(self, harness, timeline, loss=None):
        self.harness, self.timeline, self.loss = harness, timeline, loss
        self.requests = []

    async def invoke(self, capability, request):
        self.requests.append(request)
        version = request.metadata["prompt_version"]
        if version in {"supplement_reply.v5", "supplement_reply.v6", "supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10"}:
            self.timeline.append("classify_reply")
            data = {"intent": "finish", "confidence": .96,
                    "evidence_id": request.json_schema["properties"]["evidence_id"]["enum"][-1]}
            if version in {"supplement_reply.v6", "supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10"}:
                data["reply_text"] = ""
            if version in {"supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10"}:
                data["answer_effect"] = "updated"
        else:
            confirmed = version == "interview_turn_understanding.v121"
            self.timeline.append("confirmed_prepare" if confirmed else "unconfirmed_prepare")
            data = off_topic_response(request.metadata["transcript"], ["幂等键"],
                                      technical=True, relevant=True)["understanding"]
            if confirmed and self.loss == "capture":
                self.harness.chain._stt = None
            elif confirmed and self.loss == "owner":
                assert self.harness.coordinator.release(self.harness.owner)
        return ChatJSONResponse(data=deepcopy(data), usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="supplement_082", request_id="synthetic_082", latency_ms=1))


class _CaptureFacade(_Capture):
    """Synthetic audio cuts with the production evidence preparation/owner seam."""

    def __init__(self, harness):
        super().__init__()
        self.harness = harness
        self.current_final = harness.final
        self.confirmation_misses = []
        self.flags = []

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final.model_copy(deep=True))
        self.flags.append(dict(kwargs))
        return await self.harness.chain.prepare_decision(final, **kwargs)

    def assert_preparation_current(self, prepared, final, **kwargs):
        return self.harness.chain.assert_preparation_current(prepared, final, **kwargs)

    async def classify_supplement_reply(self, reply, **kwargs):
        return await self.harness.chain.classify_supplement_reply(reply, **kwargs)

    def confirm_prepared_decision(self, *args, **kwargs):
        result = self.harness.chain.confirm_prepared_decision(*args, **kwargs)
        self.confirmation_misses.append(result is None)
        return result


async def _scenario(tmp_path, monkeypatch, *, delivered=True, loss=None):
    harness = _chain(tmp_path, monkeypatch)
    service = harness.stream.interviews
    timeline, spoken, committed = [], [], []
    gateway = service.conversation.gateway = _Gateway(harness, timeline, loss)
    capture = _CaptureFacade(harness)

    async def commit(prepared, guard):
        guard()
        harness.chain.assert_preparation_current(prepared, capture.current_final,
                                                semantic_first=True, completion_confirmed=True)
        service.assert_prepared_streaming_decision(prepared, capture.current_final, "org_default")
        result = await service.submit_streaming_answer("iv_binding", _payload(capture.current_final),
            evidence_fence=harness.owner.commit_fence(), prepared_decision=prepared)
        assert result["accepted"]
        committed.append(result)
        timeline.append("commit")

    endpoint, clock, _, _ = _endpoint(capture=capture, commit_impl=commit)

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        if kind == "check":
            _delivered(service, status="selected", act_id="supplement_082_%s" % len(spoken))
        elif kind == "reception_question_clarification":
            _explanation(service)
        return True

    confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    try:
        await confirmation.step(endpoint, quiet=5)
        assert spoken == ["check"] and not committed
        initial = confirmation._prepared_answer
        assert initial is not None and not initial.completion_confirmed
        if delivered:
            _delivery(service, "supplement_082_1")
            await confirmation.complete_playback(endpoint, reason="agent_finished")
        else:
            await confirmation.complete_playback(endpoint, reason="candidate_barge_in")
        assert confirmation._supplement_question_delivered is delivered

        request_final = _append(harness.final, _EXPLANATION_REQUEST)
        capture.current_final = request_final
        endpoint.cover_transcript(request_final)
        confirmation._remember_boundary(request_final)
        confirmation._remember_handled(request_final)
        endpoint._proposal_revision = endpoint.revision
        await confirmation._say(endpoint, "reception_question_clarification", afterwards="listening")
        _delivery(service, "explanation_082")
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        snapshot = service.get_interview("iv_binding")
        assert service._prepared_context_fingerprint(snapshot, snapshot["turns"][0]) != initial.context_fingerprint

        final = _append(request_final, _NEW_REPLY)
        capture.current_final = final
        endpoint.observe_transcript(final.text)
        clock.value += 5
        # A technical addition and the real explanation both forbid the old
        # answer receipt from replacing a new confirmed full preparation.
        assert harness.chain.confirm_prepared_decision(initial, harness.final, request_final, final,
            reply_text=_NEW_REPLY, decision={"intent": "finish", "confidence": .96},
            question_delivered=delivered) is None
        try:
            await confirmation.step(endpoint, quiet=5)
        except ApiError as exc:
            assert loss is not None
            assert exc.code in {"TURN_DECISION_STALE", "EVIDENCE_OWNER_FENCED"}
        return harness, final, capture, gateway, timeline, spoken, committed
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_delivered_reply_context_change_runs_one_confirmed_preparation_and_commits_once(tmp_path, monkeypatch):
    harness, final, capture, gateway, timeline, _, committed = await _scenario(tmp_path, monkeypatch)
    assert timeline == ["unconfirmed_prepare", "classify_reply", "confirmed_prepare", "commit"]
    assert capture.flags == [{"semantic_first": True}, {"completion_confirmed": True, "semantic_first": True}]
    assert capture.confirmation_misses == [True]
    assert len(gateway.requests) == 3 and len(committed) == 1
    assert capture.snapshots == [False] * 5  # Including a new cut after confirmed reasoning and before commit.
    # Reopen the physical temporary SQLite file; the endpoint's receipt must
    # agree with the durable answer, transcript segments and owner transaction.
    reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))).get_interview("iv_binding")
    assert len(reopened["answers"]) == 1
    assert reopened["answers"][0]["final_transcript"] == final.text
    assert reopened["answers"][0]["transcript_segments"] == _payload(final)["segments"]
    assert len(reopened["turns"][0]["utterances"]) == 1


@pytest.mark.anyio
async def test_undelivered_supplement_question_cannot_confirm_or_commit_new_words(tmp_path, monkeypatch):
    harness, _, _, _, timeline, _, committed = await _scenario(tmp_path, monkeypatch, delivered=False)
    assert "classify_reply" not in timeline and "confirmed_prepare" not in timeline
    assert not committed and not harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("loss", ["capture", "owner"])
async def test_confirmed_analysis_cannot_commit_after_source_capture_or_owner_is_lost(tmp_path, monkeypatch, loss):
    harness, _, _, _, timeline, _, committed = await _scenario(tmp_path, monkeypatch, loss=loss)
    assert timeline[:3] == ["unconfirmed_prepare", "classify_reply", "confirmed_prepare"]
    assert "commit" not in timeline and not committed
    assert not harness.stream.interviews.get_interview("iv_binding")["answers"]
