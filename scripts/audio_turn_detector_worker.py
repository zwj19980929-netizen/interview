"""Private stdio worker for local audio turn detection (Python >= 3.10).

Provision independently; do not install into or upgrade the API interpreter:

    python3.12 -m venv .runtime/turn-detector
    .runtime/turn-detector/bin/python -m pip install \
        livekit-local-inference==0.2.7 numpy==2.2.6

The wheel bundles the native model. Code is Apache-2.0; model weights are
subject to LicenseRef-LiveKit-Model. This worker never imports the app, accesses
candidate storage, downloads weights, or sends audio over a network.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import sys
from importlib.metadata import version
from typing import BinaryIO, Callable


_RUNTIME_VERSION = "0.2.7"
_MAX_PCM_BYTES = 19_200 * 2
_MAX_REQUEST_BYTES = 60_000


def _write(output: BinaryIO, response: dict) -> None:
    output.write(json.dumps(response, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n")
    output.flush()


def serve(predict: Callable[[bytes], float], source: BinaryIO, output: BinaryIO) -> None:
    """Strict bounded protocol, independently testable with a fake predictor."""

    _write(output, {"status": "ready", "model": "turn-detector-v1-mini", "runtime_version": _RUNTIME_VERSION})
    while True:
        line = source.readline(_MAX_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > _MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            return
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "pcm"}:
                return
            request_id = request["id"]
            if type(request_id) is not int or request_id < 1 or not isinstance(request["pcm"], str):
                return
            pcm = base64.b64decode(request["pcm"], validate=True)
            if not pcm or len(pcm) % 2 or len(pcm) > _MAX_PCM_BYTES:
                return
        except (ValueError, TypeError, UnicodeError):
            return
        try:
            # Keep library diagnostics out of the stdout protocol. The parent
            # discards stderr, so neither PCM nor exception bodies enter logs.
            with contextlib.redirect_stdout(sys.stderr):
                probability = predict(pcm)
            if isinstance(probability, bool):
                raise ValueError("Invalid model probability")
            probability = float(probability)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("Invalid model probability")
            _write(output, {"id": request_id, "status": "ready", "probability": probability})
        except Exception:
            _write(output, {"id": request_id, "status": "unavailable", "probability": None})


def main() -> int:
    try:
        if version("livekit-local-inference") != _RUNTIME_VERSION:
            return 2
        with contextlib.redirect_stdout(sys.stderr):
            import numpy as np
            from livekit.local_inference import EOT

            model = EOT()

        def predict(pcm: bytes) -> float:
            samples = np.ascontiguousarray(np.frombuffer(pcm, dtype="<i2"), dtype=np.int16)
            return float(model.predict(samples))

        try:
            serve(predict, sys.stdin.buffer, sys.stdout.buffer)
        finally:
            close = getattr(model, "close", None)
            if close is not None:
                with contextlib.redirect_stdout(sys.stderr):
                    close()
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
