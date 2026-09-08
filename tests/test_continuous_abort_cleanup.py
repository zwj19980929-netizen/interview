"""Owner cancellation cannot orphan detached recognition transports."""

import asyncio
import gc
import warnings

import pytest

from app.model_gateway.errors import ProviderError
from app.services.continuous_stt import ContinuousSTT
from test_continuous_stt import _RawStream, _validated


def capture_for(raw):
    async def reopen():
        raise AssertionError("Closing must never reopen recognition")

    return ContinuousSTT(_validated(raw), reopen=reopen, record=lambda _: None, bytes_per_second=32000)


@pytest.mark.anyio
async def test_immediate_abort_cancellation_retains_cleanup_and_closes_provider():
    raw = _RawStream("synthetic-cancelled-owner")
    capture = capture_for(raw)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", RuntimeWarning)
        closing = asyncio.create_task(capture.abort())
        # Runs after abort detaches _stream but before its children first run.
        asyncio.get_running_loop().call_soon(closing.cancel)
        with pytest.raises(asyncio.CancelledError):
            await closing
        await capture.abort()
        await asyncio.sleep(0)
        gc.collect()
    assert raw.aborted and capture._abort_task.done()
    assert capture._closed and capture._stream is None
    assert not [warning for warning in recorded if "was never awaited" in str(warning.message)]


@pytest.mark.anyio
async def test_concurrent_abort_callers_join_one_cleanup_despite_repeated_cancellation():
    class HeldAbortStream(_RawStream):
        def __init__(self):
            super().__init__("synthetic-held-abort")
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.calls = 0

        async def abort(self):
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            await super().abort()

    raw = HeldAbortStream()
    capture = capture_for(raw)
    first = asyncio.create_task(capture.abort())
    await raw.entered.wait()
    cleanup = capture._abort_task
    second = asyncio.create_task(capture.abort())
    await asyncio.sleep(0)
    first.cancel()
    second.cancel()
    for caller in (first, second):
        with pytest.raises(asyncio.CancelledError):
            await caller
    assert not cleanup.cancelled() and raw.calls == 1
    raw.release.set()
    await capture.abort()
    assert raw.aborted and capture._abort_task is cleanup and raw.calls == 1


@pytest.mark.anyio
async def test_abort_error_does_not_skip_other_detached_transports_or_lose_original_error():
    class FailingAbortStream(_RawStream):
        async def abort(self):
            await super().abort()
            raise ProviderError("provider_stream_close_failed", "Synthetic cleanup error.", retryable=True)

    failed, finishing, retired = FailingAbortStream("failed"), _RawStream("finishing"), _RawStream("retired")
    capture = capture_for(failed)
    capture._finishing_stream = _validated(finishing)
    capture._retired_stream = _validated(retired)
    for _ in range(2):
        with pytest.raises(ProviderError) as error:
            await capture.abort()
        assert error.value.code == "provider_stream_close_failed"
    assert failed.aborted and finishing.aborted and retired.aborted


@pytest.mark.anyio
async def test_abandoned_abort_has_a_bounded_transport_deadline():
    class StalledAbortStream(_RawStream):
        async def abort(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.aborted = True

    raw = StalledAbortStream("stalled")
    capture = capture_for(raw)
    closing = asyncio.create_task(capture.abort())
    asyncio.get_running_loop().call_soon(closing.cancel)
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(capture.abort(), timeout=2.5)
    assert raw.aborted and capture._abort_task.done()
