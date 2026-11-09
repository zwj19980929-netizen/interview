"""Real ASR retractions invalidate permission without discarding pure work.

DashScope uses the existing in-memory WebSocket transport; the adapter,
gateway, ContinuousSTT and endpoint are real. Only semantic work is gated.
"""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

import pytest

from app.core.errors import ApiError
from app.services.continuous_stt import ContinuousSTT
from test_answer_endpoint import _endpoint, _until
from test_answer_preparation_cancellation_081 import Capture
from test_exit_confirmation_flow_065 import ask, reply, setup as exit_setup
from test_stt_empty_completion import open_socket
from test_stt_stable_preview_providers import PreviewSocket, sentence


BODY = "使用事务和幂等键保证数据一致性。"
ADDITION = "还需要处理失败重试。"


class ContinuousCapture(Capture):
    supports_stable_preview = True

    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    async def transcript_preview(self):
        return await self.stream.preview()

    async def transcript_snapshot(self, *, resume=True):
        self.snapshots.append(resume)
        return await self.stream.snapshot(resume=resume)

    async def resume_capture(self):
        await self.stream.resume()


@asynccontextmanager
async def recognition(*, socket_factory=PreviewSocket):
    sockets, recorded = [], []

    async def reopen():
        socket = socket_factory()
        sockets.append(socket)
        return await open_socket(socket)

    stream = ContinuousSTT(await reopen(), reopen=reopen, record=recorded.append,
                           bytes_per_second=32000)
    try:
        yield stream, sockets, recorded
    finally:
        await stream.abort()


async def emit_sentence(stream, socket, endpoint, *, voiced=False, **values):
    # Recognition arrives asynchronously. A genuine next audio intake tick
    # drains provider events through the same cumulative-text path as LiveKit.
    await sentence(socket, **values)
    events = await stream.send_audio(b"\x00\x20" * 320 if voiced else bytes(640))
    partials = [event for event in events if event.type == "transcript.partial"]
    assert partials, "The assertion must exercise a provider-produced event"
    for event in partials:
        assert not event.is_final
        endpoint.observe_transcript(event.text)
    return partials[-1]


async def start_analysis(stream, sockets, endpoint, capture, clock, *, speculative,
                         semantic_first=True):
    await stream.send_audio(bytes(32000))
    first_event = await emit_sentence(stream, sockets[-1], endpoint,
        sentence_id=1, sentence_end=True, begin_time=0, end_time=900, text=BODY)
    assert first_event.text == BODY
    initial = await stream.preview()
    assert not initial.has_unstable_tail
    if speculative:
        clock.value += 4
        endpoint.prepare_stable_preview(initial)
    else:
        # Formal preparation keeps listening in a newly opened ASR segment.
        initial = await stream.snapshot(resume=True)
        endpoint.cover_transcript(initial)
    endpoint._proposal_revision = endpoint.revision
    waiter = asyncio.create_task(endpoint._prepare_transcript(initial, semantic_first=semantic_first))
    await asyncio.wait_for(capture.started.wait(), 1)
    await _until(lambda: endpoint._inference_task is not None)
    pure = endpoint.preparation._task if speculative else endpoint._confirmed_preparation
    assert pure is not None
    return initial, waiter, pure


@pytest.mark.anyio
@pytest.mark.parametrize("speculative", [False, True])
async def test_real_sentence_retraction_restores_exact_evidence_and_reuses_owned_analysis(speculative):
    async with recognition() as (stream, sockets, recorded):
        capture = ContinuousCapture(stream)
        endpoint, clock, _, commits = _endpoint(capture=capture)
        initial, waiter, pure = await start_analysis(
            stream, sockets, endpoint, capture, clock, speculative=speculative)
        identity = endpoint._identity(initial)
        deadline, revision = endpoint._interaction_deadline, endpoint.revision
        tail_start = 1000 if speculative else 0
        try:
            partial = await emit_sentence(stream, sockets[-1], endpoint,
                sentence_id=2, sentence_end=False, begin_time=tail_start, text="嗯", voiced=True)
            assert partial.text == BODY + "嗯"
            unstable = await stream.preview()
            assert unstable.has_unstable_tail
            endpoint.prepare_stable_preview(unstable)
            with pytest.raises(ApiError) as stale:
                await waiter
            assert stale.value.code == "TURN_DECISION_STALE"
            assert endpoint.revision > revision and not commits
            assert not pure.done() and not capture.cancelled
            with pytest.raises(ApiError):
                endpoint.assert_current()

            # This is the vendor's keyed, empty sentence-end retraction, not
            # a hand-built final with the desired text or provider metadata.
            await sentence(sockets[-1], sentence_id=2, sentence_end=True,
                           begin_time=tail_start, end_time=tail_start + 200, text="")
            retracted_events = await stream.send_audio(bytes(640))
            for event in retracted_events:
                if event.type == "transcript.partial":
                    endpoint.observe_transcript(event.text)
            stable = await stream.preview()
            assert stable is not None
            assert endpoint._identity(stable) == identity
            if speculative:
                assert not stable.has_unstable_tail
                assert stable.revision > initial.revision
            else:
                # A new segment with speech but no committed words still
                # needs its empty task-completion acknowledgement.
                assert stable.has_unstable_tail
            endpoint.prepare_stable_preview(stable)
            assert not pure.done() and not capture.cancelled

            final = await stream.snapshot(resume=False)
            assert final.type == "transcript.final" and final.is_final
            assert endpoint._identity(final) == identity
            assert len(final.segments) == 1
            assert (final.segments[0].start_ms, final.segments[0].end_ms) == (0, 900)
            endpoint.cover_transcript(final)
            endpoint._proposal_revision = endpoint.revision
            capture.release.set()
            prepared = await endpoint._prepare_transcript(final, semantic_first=True)
            assert prepared is pure.result() and capture.validations
            assert len(capture.prepares) == 1 and not capture.cancelled
            assert endpoint._model_budgets[hashlib.sha256(BODY.encode()).hexdigest()].used == 1
            if deadline is not None:
                assert endpoint._interaction_deadline == deadline
            assert not commits and stream.commit_final() == final
            assert sum(map(len, recorded)) == stream.forwarded_bytes
            assert all(socket.closed for socket in sockets)
        finally:
            await endpoint.close()
            await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("speculative", [False, True])
async def test_real_new_final_invalidates_retained_analysis_before_repreparing(speculative):
    async with recognition() as (stream, sockets, _):
        capture = ContinuousCapture(stream)
        endpoint, clock, _, commits = _endpoint(capture=capture)
        initial, waiter, pure = await start_analysis(
            stream, sockets, endpoint, capture, clock, speculative=speculative)
        tail_start = 1000 if speculative else 0
        try:
            partial = await emit_sentence(stream, sockets[-1], endpoint,
                sentence_id=2, sentence_end=False, begin_time=tail_start, text=ADDITION, voiced=True)
            assert partial.text == BODY + ADDITION
            with pytest.raises(ApiError):
                await waiter
            assert not pure.done() and not capture.cancelled
            await emit_sentence(stream, sockets[-1], endpoint,
                sentence_id=2, sentence_end=True, begin_time=tail_start,
                end_time=tail_start + 600, text=ADDITION)
            final = await stream.snapshot(resume=False)
            assert final.text == BODY + ADDITION and len(final.segments) == 2
            assert endpoint._identity(final) != endpoint._identity(initial)
            endpoint.cover_transcript(final)
            await _until(lambda: pure.done())
            assert pure.cancelled() and capture.cancelled == [BODY]
            assert not endpoint.preparation.matching(final)
            assert endpoint._confirmed_preparation is None and not commits

            capture.release.set()
            endpoint._proposal_revision = endpoint.revision
            prepared = await endpoint._prepare_transcript(final, semantic_first=True)
            assert prepared.final == final and prepared.text == BODY + ADDITION
            assert [item.text for item in capture.prepares] == [BODY, BODY + ADDITION]
            assert not commits
        finally:
            await endpoint.close()
            await asyncio.gather(waiter, pure, return_exceptions=True)


@pytest.mark.anyio
async def test_real_partial_revokes_confirmed_exit_and_retraction_never_restores_permission():
    endpoint, _, _, commits, spoken = exit_setup()
    pending_exit = []

    async def hold_before_exit_commit(final):
        pending_exit.append(final)

    # Observe the real confirmation state immediately before its guarded
    # commit. This pause does not fabricate a confirmed flag or receipt.
    endpoint.capture.prepare_exit_decision = hold_before_exit_commit
    endpoint._propose_exit = hold_before_exit_commit
    try:
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        await reply(endpoint, "是的，确定结束。")
        assert spoken == ["exit_check"] and len(pending_exit) == 1
        confirmed_final = pending_exit[0]
        assert endpoint.confirmation.confirmed
        assert endpoint.confirmation.exit_context(confirmed_final)["decision"] == "confirm"
        async with recognition() as (stream, sockets, _):
            await stream.send_audio(bytes(32000))
            await emit_sentence(stream, sockets[-1], endpoint,
                sentence_id=1, sentence_end=True, begin_time=0, end_time=900,
                text=confirmed_final.text)
            assert endpoint.confirmation.confirmed, "Already covered subtitles are harmless"
            await emit_sentence(stream, sockets[-1], endpoint,
                sentence_id=2, sentence_end=False, begin_time=1000, text="不过", voiced=True)
            assert not endpoint.confirmation.confirmed
            assert endpoint.confirmation.phase == "awaiting_exit_reply"
            assert endpoint.confirmation.exit_context(confirmed_final) is None
            with pytest.raises(ApiError):
                endpoint.assert_current()
            await sentence(sockets[-1], sentence_id=2, sentence_end=True,
                           begin_time=1000, end_time=1200, text="")
            final = await stream.snapshot(resume=False)
            assert final.text == confirmed_final.text
            endpoint.cover_transcript(final)
            assert not endpoint.confirmation.confirmed
            assert endpoint.confirmation.exit_context(final) is None
            assert len(pending_exit) == 1 and not commits
    finally:
        await endpoint.close()


class GatedCompletionSocket(PreviewSocket):
    def __init__(self):
        super().__init__()
        self.finish_requested = asyncio.Event()

    async def send(self, data):
        if isinstance(data, str) and json.loads(data)["header"]["action"] == "finish-task":
            self.sent.append(data)
            self.finish_requested.set()
            return  # The test supplies the actual vendor completion ACK later.
        await super().send(data)


@pytest.mark.anyio
@pytest.mark.parametrize("retracted", [False, True])
async def test_zero_pcm_hypothesis_cannot_commit_old_preparation_without_retraction_and_ack(retracted):
    count = 0

    def socket_factory():
        nonlocal count
        count += 1
        return GatedCompletionSocket() if count == 2 else PreviewSocket()

    async with recognition(socket_factory=socket_factory) as (stream, sockets, recorded):
        capture = ContinuousCapture(stream)
        endpoint, clock, _, commits = _endpoint(capture=capture)
        initial, waiter, pure = await start_analysis(
            stream, sockets, endpoint, capture, clock, speculative=False, semantic_first=False)
        proposal = None
        try:
            # A provider may hallucinate a short word during complete local
            # silence. Preserve this counterexample instead of relying on VAD.
            partial = await emit_sentence(stream, sockets[-1], endpoint,
                sentence_id=2, sentence_end=False, begin_time=0, end_time=200, text="嗯")
            assert partial.text == BODY + "嗯"
            assert all(not any(chunk) for chunk in recorded)
            with pytest.raises(ApiError):
                await waiter
            assert not pure.done() and not capture.cancelled
            capture.release.set()
            await _until(lambda: pure.done())
            original = pure.result()
            if retracted:
                await sentence(sockets[-1], sentence_id=2, sentence_end=True,
                               begin_time=0, end_time=200, text="")
            preview = await stream.preview()
            assert not preview.has_unstable_tail
            assert endpoint._identity(preview) == endpoint._identity(initial)

            endpoint._proposal_revision = endpoint.revision
            proposal = asyncio.create_task(endpoint._propose())
            completion = sockets[-1]
            await asyncio.wait_for(completion.finish_requested.wait(), 1)
            assert not proposal.done() and not commits
            assert len(capture.prepares) == 1 and not capture.cancelled
            assert not stream.paused, "The full evidence cut is still awaiting its ACK"

            completion.push("task-finished")
            await asyncio.wait_for(proposal, 1)
            assert len(commits) == 1
            if retracted:
                assert commits[0] is original and commits[0].text == BODY
                assert len(capture.prepares) == 1
            else:
                assert commits[0] is not original
                assert commits[0].text == BODY + "嗯"
                assert [item.text for item in capture.prepares] == [BODY, BODY + "嗯"]
                assert len(commits[0].final.segments) == 2
        finally:
            if proposal is not None and not proposal.done():
                proposal.cancel()
            await endpoint.close()
            await asyncio.gather(waiter, pure, *([proposal] if proposal else []), return_exceptions=True)
