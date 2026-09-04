import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.file_storage.interface import StoredFile
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.agent_expression_audio import AgentExpressionAudioService


class _Storage:
    backend_name = "test_private"

    def __init__(self, *, encryption_error: Exception = None) -> None:
        self.values = {}
        self.deleted = []
        self.encryption_error = encryption_error

    def store(self, *, organization_id, object_id, content, content_type, checksum):
        key = "%s/%s.wav" % (organization_id, object_id)
        self.values[key] = bytes(content)
        return StoredFile(
            storage_backend=self.backend_name,
            object_key=key,
            byte_count=len(content),
            checksum=checksum,
            content_type=content_type,
        )

    def issue_read_access(self, object_key, *, expires_seconds=300):
        assert object_key in self.values
        return "signed-expression-token"

    def verify_encryption(self, object_key=None):
        if self.encryption_error:
            raise self.encryption_error
        return "test_kms"

    def delete(self, object_key):
        self.deleted.append(object_key)
        self.values.pop(object_key, None)


def _interview(store: InMemoryStore, interview_id: str) -> None:
    now = utc_now()
    with persistence_for(store).transaction("org_default") as transaction:
        transaction.interview_sessions.add(
            {
                "id": interview_id,
                "organization_id": "org_default",
                "status": "in_progress",
                "turns": [],
                "answers": [],
                "agent_events": [],
                "agent_runtime": {},
                "created_at": now,
                "updated_at": now,
            }
        )


def test_s2s_pcm_is_private_scoped_and_signed_only_at_projection() -> None:
    store = InMemoryStore()
    _interview(store, "iv_expression_1")
    _interview(store, "iv_expression_2")
    storage = _Storage()
    service = AgentExpressionAudioService(
        persistence_for(store), storage=storage
    )

    stored = service.store_pcm(
        organization_id="org_default",
        interview_id="iv_expression_1",
        turn_id="turn_1",
        pcm_s16le=b"\x00\x00" * 2_400,
        sample_rate_hz=24_000,
        channels=1,
    )
    assert stored["audio_uri"].startswith("agent-expression://file_")
    assert stored["duration_ms"] == 100
    assert "signed" not in stored["audio_uri"]

    grant = service.issue_access(
        stored["audio_uri"],
        organization_id="org_default",
        interview_id="iv_expression_1",
        actor_id="candidate:candidate_1",
    )
    assert grant == "/api/v1/private-files/signed-expression-token"
    with pytest.raises(ApiError) as wrong_interview:
        service.issue_access(
            stored["audio_uri"],
            organization_id="org_default",
            interview_id="iv_expression_2",
            actor_id="candidate:candidate_2",
        )
    assert wrong_interview.value.code == "AGENT_EXPRESSION_AUDIO_SCOPE_INVALID"


def test_production_expression_store_deletes_object_if_encryption_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    store = InMemoryStore()
    _interview(store, "iv_expression_encryption")
    storage = _Storage(encryption_error=RuntimeError("missing SSE"))
    service = AgentExpressionAudioService(
        persistence_for(store), storage=storage
    )

    with pytest.raises(RuntimeError, match="missing SSE"):
        service.store_pcm(
            organization_id="org_default",
            interview_id="iv_expression_encryption",
            turn_id="turn_1",
            pcm_s16le=b"\x00\x00" * 100,
            sample_rate_hz=24_000,
            channels=1,
        )
    assert len(storage.deleted) == 1
    with persistence_for(store).transaction("org_default") as transaction:
        assert transaction.file_objects.list() == []
