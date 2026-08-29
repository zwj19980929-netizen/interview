import struct

import pytest

from app.core.errors import ApiError
from app.services.resume_ingestion import ClamdMalwareScanner, clamd_ping


class FakeClamdConnection:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def recv(self, _: int) -> bytes:
        response, self.response = self.response, b""
        return response


def test_clamd_scanner_uses_framed_instream_and_accepts_clean_content(monkeypatch) -> None:
    connection = FakeClamdConnection(b"stream: OK\0")
    monkeypatch.setattr(
        "app.services.resume_ingestion.socket.create_connection",
        lambda *_args, **_kwargs: connection,
    )

    ClamdMalwareScanner("clamd.internal").scan(b"candidate-pdf")

    assert connection.sent.startswith(b"zINSTREAM\0")
    assert struct.pack("!I", len(b"candidate-pdf")) + b"candidate-pdf" in connection.sent
    assert connection.sent.endswith(struct.pack("!I", 0))


def test_clamd_scanner_maps_found_to_malware_error(monkeypatch) -> None:
    connection = FakeClamdConnection(b"stream: Eicar-Signature FOUND\0")
    monkeypatch.setattr(
        "app.services.resume_ingestion.socket.create_connection",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(ApiError) as raised:
        ClamdMalwareScanner("clamd.internal").scan(b"eicar")
    assert raised.value.code == "FILE_MALWARE_DETECTED"


def test_clamd_ping_uses_nul_framing(monkeypatch) -> None:
    connection = FakeClamdConnection(b"PONG\0")
    monkeypatch.setattr(
        "app.services.resume_ingestion.socket.create_connection",
        lambda *_args, **_kwargs: connection,
    )

    assert clamd_ping("clamd.internal") is True
    assert connection.sent == b"zPING\0"
