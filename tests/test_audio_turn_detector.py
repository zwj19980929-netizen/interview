import asyncio
import base64
import io
import json
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from app.adapters.audio_turn_detector import LocalAudioTurnDetector


_READY = {"status": "ready", "model": "turn-detector-v1-mini", "runtime_version": "0.2.7"}


class _FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, value):
        request = json.loads(value)
        self.process.requests.append(request)
        response = self.process.response(request)
        if response is not None:
            self.process.stdout.feed_data(json.dumps(response).encode() + b"\n")

    async def drain(self):
        pass


class _FakeProcess:
    def __init__(self, response=None, *, ready=_READY):
        self.returncode = None
        self.requests = []
        self.killed = False
        self.stdout = asyncio.StreamReader()
        if ready is not None:
            self.stdout.feed_data(json.dumps(ready).encode() + b"\n")
        self.stdin = _FakeInput(self)
        self.response = response or (
            lambda request: {"id": request["id"], "status": "ready", "probability": 0.8}
        )

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self):
        return self.returncode


def test_detector_runs_offline_worker_and_sends_only_bounded_pcm_tail():
    async def scenario():
        processes, calls = [], []

        async def factory(*args, **kwargs):
            calls.append((args, kwargs))
            process = _FakeProcess()
            processes.append(process)
            return process

        detector = LocalAudioTurnDetector("/private/runtime/python", process_factory=factory)
        pcm = b"\x01\x00" * 32_000 + b"\x02\x00" * 19_200
        result = await detector.predict(pcm, language="zh-CN")
        assert result.status == "ready" and result.probability == 0.8
        assert result.reason_code is None and result.latency_ms >= 0
        assert base64.b64decode(processes[0].requests[0]["pcm"]) == pcm[-38_400:]
        assert set(processes[0].requests[0]) == {"id", "pcm"}
        assert calls[0][0][0:2] == ("/private/runtime/python", "-I")
        assert calls[0][1]["stderr"] == asyncio.subprocess.DEVNULL
        assert set(calls[0][1]["env"]) == {"PATH", "LANG"}  # No parent credentials are inherited.
        await detector.predict(b"\x00\x00" * 4000, language="zh_Hans")
        assert len(processes) == 1
        assert [request["id"] for request in processes[0].requests] == [1, 2]
        await detector.close()
        assert processes[0].killed
        assert (await detector.predict(pcm)).reason_code == "closed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("pcm", "rate", "language", "status", "reason"),
    [
        (b"", 16_000, "zh", "unavailable", "invalid_audio"),
        (b"x", 16_000, "zh", "unavailable", "invalid_audio"),
        (b"xx", 48_000, "zh", "unsupported", "sample_rate_unsupported"),
        (b"xx", 16_000, "unknown", "unsupported", "language_unsupported"),
    ],
)
def test_invalid_or_unsupported_audio_never_starts_model(pcm, rate, language, status, reason):
    async def scenario():
        async def factory(*_args, **_kwargs):
            raise AssertionError("Must not start model")

        detector = LocalAudioTurnDetector("/private/runtime/python", process_factory=factory)
        result = await detector.predict(pcm, rate, language)
        assert (result.status, result.reason_code, result.probability) == (status, reason, None)

    asyncio.run(scenario())


def test_missing_runtime_never_becomes_turn_complete():
    async def scenario():
        detector = LocalAudioTurnDetector("")
        assert (await detector.predict(b"\0\0")).reason_code == "runtime_not_configured"

        async def factory(*_args, **_kwargs):
            raise FileNotFoundError("Do not expose this private runtime path")

        detector = LocalAudioTurnDetector("/not/installed", process_factory=factory)
        result = await detector.predict(b"\0\0")
        assert result.probability is None and result.reason_code == "runtime_unavailable"
        assert "/not/installed" not in repr(result)

    asyncio.run(scenario())


@pytest.mark.parametrize("probability", [None, True, "0.9", -0.1, 1.1, float("nan"), float("inf")])
def test_invalid_worker_probabilities_fail_unknown(probability):
    async def scenario():
        process = _FakeProcess(lambda req: {"id": req["id"], "status": "ready", "probability": probability})

        async def factory(*_args, **_kwargs):
            return process

        detector = LocalAudioTurnDetector("python", process_factory=factory)
        result = await detector.predict(b"\0\0")
        assert result.probability is None and result.status == "unavailable"
        assert process.killed

    asyncio.run(scenario())


def test_timeout_reaps_worker_and_a_new_request_can_recover_without_queue():
    async def scenario():
        processes = []

        async def factory(*_args, **_kwargs):
            process = _FakeProcess((lambda _: None) if not processes else None)
            processes.append(process)
            return process

        detector = LocalAudioTurnDetector("python", timeout_seconds=0.01, process_factory=factory)
        first = asyncio.create_task(detector.predict(b"\0\0"))
        while not processes or not processes[0].requests:
            await asyncio.sleep(0)
        concurrent = await detector.predict(b"\1\0")
        assert concurrent.reason_code == "busy" and concurrent.probability is None
        result = await first
        assert result.reason_code == "timeout" and result.probability is None
        assert processes[0].killed and len(processes[0].requests) == 1
        assert (await detector.predict(b"\1\0")).status == "ready"
        assert len(processes) == 2
        await detector.close()

    asyncio.run(scenario())


def test_cancellation_reaps_worker_so_old_prediction_cannot_be_reused():
    async def scenario():
        process = _FakeProcess(lambda _: None)

        async def factory(*_args, **_kwargs):
            return process

        detector = LocalAudioTurnDetector("python", process_factory=factory)
        task = asyncio.create_task(detector.predict(b"\0\0"))
        while not process.requests:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed
        await detector.close()

    asyncio.run(scenario())


def test_close_during_startup_cannot_leak_a_late_created_worker():
    async def scenario():
        gate = asyncio.Event()
        started = asyncio.Event()
        process = _FakeProcess()

        async def factory(*_args, **_kwargs):
            started.set()
            await gate.wait()
            return process

        detector = LocalAudioTurnDetector("python", process_factory=factory)
        task = asyncio.create_task(detector.predict(b"\0\0"))
        await started.wait()
        await detector.close()
        gate.set()
        result = await task
        assert result.reason_code == "closed" and result.probability is None
        assert process.killed and not process.requests

    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    {"id": True, "status": "ready", "probability": 0.9},
    {"id": 999, "status": "ready", "probability": 0.9},
    {"id": 1, "status": "ready", "probability": 0.9, "extra": "private"},
    {"id": 1, "status": "unavailable", "probability": None},
])
def test_stale_or_malformed_worker_response_is_never_a_prediction(response):
    async def scenario():
        process = _FakeProcess(lambda _: response)

        async def factory(*_args, **_kwargs):
            return process

        detector = LocalAudioTurnDetector("python", process_factory=factory)
        result = await detector.predict(b"\0\0")
        assert result.status == "unavailable" and result.probability is None
        assert process.killed

    asyncio.run(scenario())


@pytest.mark.parametrize("ready", [None, {**_READY, "runtime_version": "0.1.0"}])
def test_startup_failure_returns_unknown_and_reaps_worker(ready):
    async def scenario():
        process = _FakeProcess(ready=ready)

        async def factory(*_args, **_kwargs):
            return process

        detector = LocalAudioTurnDetector("python", startup_timeout_seconds=0.05, process_factory=factory)
        result = await detector.predict(b"\0\0")
        assert result.probability is None and result.status == "unavailable"
        assert process.killed

    asyncio.run(scenario())


def _worker():
    path = Path(__file__).resolve().parents[1] / "scripts" / "audio_turn_detector_worker.py"
    spec = spec_from_file_location("audio_turn_worker_test", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_protocol_is_model_injected_and_never_echoes_audio_or_exceptions():
    worker = _worker()
    pcm = b"\1\0" * 19_200
    source = io.BytesIO(json.dumps({"id": 7, "pcm": base64.b64encode(pcm).decode()}).encode() + b"\n")
    output = io.BytesIO()
    calls = []

    def predict(data):
        calls.append(data)
        raise RuntimeError("candidate-data-that-must-not-be-logged")

    worker.serve(predict, source, output)
    assert calls == [pcm]
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert lines == [_READY, {"id": 7, "status": "unavailable", "probability": None}]
    assert b"candidate-data" not in output.getvalue()


@pytest.mark.parametrize("payload", [
    {"id": 1, "pcm": "***"},
    {"id": True, "pcm": "AAA="},
    {"id": 1, "pcm": "AA=="},
    {"id": 1, "pcm": ""},
    {"id": 1, "pcm": "AAA=", "extra": "unsafe"},
    {"id": 1, "pcm": base64.b64encode(b"\0" * 38_402).decode()},
])
def test_worker_rejects_invalid_requests_before_model(payload):
    worker = _worker()

    def predict(_):
        raise AssertionError("Invalid request must not reach model")

    output = io.BytesIO()
    worker.serve(predict, io.BytesIO(json.dumps(payload).encode() + b"\n"), output)
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [_READY]


def test_worker_valid_probability_is_not_changed_to_a_turn_decision():
    worker = _worker()
    output = io.BytesIO()
    worker.serve(lambda _: 0.355, io.BytesIO(b'{"id":1,"pcm":"AAA="}\n'), output)
    assert json.loads(output.getvalue().splitlines()[1]) == {
        "id": 1, "status": "ready", "probability": 0.355,
    }


@pytest.mark.skipif(
    not os.environ.get("INTERVIEWER_TEST_TURN_DETECTOR_PYTHON"),
    reason="Explicit isolated native turn-detector runtime required",
)
def test_native_worker_local_synthetic_audio_smoke():
    """Proves local runtime/protocol only, never Chinese turn-taking accuracy."""

    async def scenario():
        detector = LocalAudioTurnDetector(os.environ["INTERVIEWER_TEST_TURN_DETECTOR_PYTHON"])
        try:
            for pcm in (bytes(38_400), b"\x10\x00\xf0\xff" * 9600):
                result = await detector.predict(pcm)
                assert result.status == "ready", result
                assert result.probability is not None and 0 <= result.probability <= 1
        finally:
            await detector.close()

    asyncio.run(scenario())
