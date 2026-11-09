"""Retained analysis stays behind real capture, SQLite and owner fences."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.repositories.sqlite import SQLiteStore
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.interview_evidence import InterviewEvidenceChain
from app.services.streaming_stt import StreamingInterviewSTT
from test_prepared_decision_binding import _setup


def _chain(tmp_path, monkeypatch):
    seed, final = _setup()
    store = SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))
    connect = store._connect

    def connection():
        result = connect()
        result.execute("PRAGMA busy_timeout = 30")
        return result

    monkeypatch.setattr(store, "_connect", connection)
    chain = InterviewEvidenceChain(store, "iv_binding")
    with chain.persistence.transaction("org_default") as transaction:
        transaction.interview_sessions.add(seed.get_interview("iv_binding"))
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    owner = coordinator.attach_control(interview_id="iv_binding", organization_id="org_default",
        connection_id="control_original", local_instance_id="instance_original")

    def guard():
        coordinator.assert_current(owner.commit_fence(), "org_default")
        coordinator.assert_control_current(owner)

    stream = StreamingInterviewSTT(store, "iv_binding", commit_fence=owner.commit_fence(),
                                   commit_guard=guard)
    stream.turn_id = "turn_binding"
    # A synthetic already-sealed prefix exercises the real preparation seam
    # without creating a microphone, recording, or external provider session.
    stream.last_media_checkpoint = {"stream_id": final.stream_id, "capture_revision": 1,
                                    "last_sealed_frame_sequence": 1}
    chain._stt = stream
    return SimpleNamespace(store=store, chain=chain, stream=stream, final=final,
                           coordinator=coordinator, owner=owner)


def _session(harness):
    return harness.stream.interviews.get_interview("iv_binding")


async def _prepare(harness):
    return await harness.chain.prepare_decision(harness.final, semantic_first=True)


@pytest.mark.anyio
async def test_sqlite_cache_validation_runs_external_guard_before_read_scope_and_grants_no_completion(tmp_path, monkeypatch):
    harness = _chain(tmp_path, monkeypatch)
    prepared = await _prepare(harness)
    before = _session(harness)
    original_guard = harness.stream.commit_guard
    renewed = []

    def write_guard():
        original_guard()
        # This real SQLite write would fail if cache validation kept its
        # session read transaction open across the external control guard.
        renewed.append(harness.coordinator.renew(harness.owner))

    harness.stream.commit_guard = write_guard
    harness.chain.assert_preparation_current(prepared, harness.final, semantic_first=True)
    assert len(renewed) == 1
    assert not prepared.completion_confirmed
    with pytest.raises(ApiError) as error:
        harness.stream.interviews.assert_prepared_streaming_decision(prepared, harness.final, "org_default")
    assert error.value.code == "TURN_DECISION_STALE"
    assert _session(harness) == before
    assert harness.chain.is_open and harness.chain._stt is harness.stream


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["replacement", "revoked", "closed", "unbound", "other_interview"])
async def test_same_words_never_reuse_a_preparation_from_another_capture(tmp_path, monkeypatch, change):
    harness = _chain(tmp_path, monkeypatch)
    prepared = await _prepare(harness)
    before = _session(harness)
    if change == "replacement":
        harness.chain._stt = StreamingInterviewSTT(harness.store, "iv_binding")
        harness.chain._stt.turn_id = "turn_binding"
    elif change == "revoked":
        harness.chain.revoke_audio_input()
    elif change == "closed":
        harness.chain._stt = None
    elif change == "unbound":
        prepared = replace(prepared, capture_binding=None)
    else:
        prepared = replace(prepared, interview_id="iv_other")
    with pytest.raises(ApiError) as error:
        harness.chain.assert_preparation_current(prepared, harness.final, semantic_first=True)
    assert error.value.code == "TURN_DECISION_STALE"
    assert _session(harness) == before


@pytest.mark.anyio
@pytest.mark.parametrize("change,code", [("control", "EVIDENCE_CONTROL_STALE"), ("owner", "EVIDENCE_OWNER_FENCED")])
async def test_retained_success_cannot_hide_durable_control_or_owner_loss(tmp_path, monkeypatch, change, code):
    harness = _chain(tmp_path, monkeypatch)
    prepared = await _prepare(harness)
    if change == "owner":
        assert harness.coordinator.release(harness.owner)
    harness.coordinator.attach_control(interview_id="iv_binding", organization_id="org_default",
        connection_id="control_replacement", local_instance_id="instance_replacement" if change == "owner" else "instance_original")
    before = _session(harness)
    with pytest.raises(ApiError) as error:
        harness.chain.assert_preparation_current(prepared, harness.final, semantic_first=True)
    assert error.value.code == code
    assert _session(harness) == before


@pytest.mark.anyio
async def test_control_check_failure_is_propagated_without_turning_it_into_cache_miss(tmp_path, monkeypatch):
    harness = _chain(tmp_path, monkeypatch)
    prepared = await _prepare(harness)
    failure = RuntimeError("synthetic unavailable owner check")

    def unavailable():
        raise failure

    harness.stream.commit_guard = unavailable
    with pytest.raises(RuntimeError) as caught:
        harness.chain.assert_preparation_current(prepared, harness.final, semantic_first=True)
    assert caught.value is failure
    assert not _session(harness)["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["question", "policy", "paused", "takeover"])
async def test_context_changed_while_analysis_runs_is_rejected_before_cached_result_is_used(tmp_path, monkeypatch, change):
    harness = _chain(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = harness.stream.interviews.supervisor.prepare_turn

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(harness.stream.interviews.supervisor, "prepare_turn", delayed)
    task = asyncio.create_task(_prepare(harness))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with harness.chain.persistence.transaction("org_default") as transaction:
            current = transaction.interview_sessions.get("iv_binding")
            if change == "question":
                current["turns"][0]["question_spoken_text"] = "另一道问题。"
            elif change == "policy":
                current["followup_policy"] = {"max_depth": 0}
            elif change == "paused":
                current["status"] = "paused"
            else:
                current["agent_runtime"] = {"floor": "human", "takeover": {"status": "active"}}
            transaction.interview_sessions.update(current, expected_version=current["version"])
        release.set()
        prepared = await asyncio.wait_for(task, 1)
        before = _session(harness)
        with pytest.raises(ApiError):
            harness.chain.assert_preparation_current(prepared, harness.final, semantic_first=True)
        assert _session(harness) == before
        assert not before["answers"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["capture", "owner"])
async def test_analysis_finishing_after_capture_or_owner_replacement_never_returns_reusable_work(tmp_path, monkeypatch, change):
    harness = _chain(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = harness.stream.interviews.supervisor.prepare_turn

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(harness.stream.interviews.supervisor, "prepare_turn", delayed)
    task = asyncio.create_task(_prepare(harness))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "capture":
            harness.chain._stt = StreamingInterviewSTT(harness.store, "iv_binding")
            harness.chain._stt.turn_id = "turn_binding"
        else:
            assert harness.coordinator.release(harness.owner)
        before = _session(harness)
        release.set()
        with pytest.raises(ApiError) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.code == ("TURN_DECISION_STALE" if change == "capture" else "EVIDENCE_OWNER_FENCED")
        assert _session(harness) == before
        assert not before["answers"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
