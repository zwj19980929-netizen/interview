import os

import pytest

from app.core.errors import ApiError
from app.services.resume_ingestion import ClamdMalwareScanner, clamd_ping


CLAMD_HOST = os.getenv("INTERVIEWER_TEST_CLAMD_HOST", "").strip()
CLAMD_PORT = int(os.getenv("INTERVIEWER_TEST_CLAMD_PORT", "3310"))


@pytest.mark.skipif(not CLAMD_HOST, reason="INTERVIEWER_TEST_CLAMD_HOST is not configured")
def test_real_clamd_accepts_clean_content_and_rejects_eicar() -> None:
    scanner = ClamdMalwareScanner(CLAMD_HOST, CLAMD_PORT)
    assert clamd_ping(CLAMD_HOST, CLAMD_PORT) is True
    scanner.scan(b"clean candidate resume content")

    eicar = (
        b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!"
        b"$H+H*"
    )
    with pytest.raises(ApiError) as raised:
        scanner.scan(eicar)
    assert raised.value.code == "FILE_MALWARE_DETECTED"
