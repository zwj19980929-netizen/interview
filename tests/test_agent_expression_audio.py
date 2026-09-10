import asyncio

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


@pytest.mark.anyio
async def test_cancelled_tts_import_does_not_create_private_audio_or_ready_file() -> None:
    """A superseded decision must cancel its download before materialization."""

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowImporter:
        max_bytes = 20 * 1024 * 1024

        async def audio(self, uri, content_type):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    store = InMemoryStore()
    _interview(store, "iv_expression_cancelled")
    storage = _Storage()
    service = AgentExpressionAudioService(
        persistence_for(store), storage=storage, importer=SlowImporter()
    )
    task = asyncio.create_task(
        service.import_tts(
            organization_id="org_default",
            interview_id="iv_expression_cancelled",
            turn_id="turn_1",
            audio_uri="https://unused.example.test/approved.wav",
            content_type="audio/wav",
            duration_ms=1000,
            provider_id="test_provider",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert storage.values == {}
    assert storage.deleted == []
    with persistence_for(store).transaction("org_default") as transaction:
        assert transaction.file_objects.list() == []
        session = transaction.interview_sessions.get("iv_expression_cancelled")
        assert session["status"] == "in_progress"
        assert session["agent_events"] == []
        assert session["agent_runtime"] == {}


@pytest.mark.anyio
async def test_complete_tts_pcm_is_private_and_skips_provider_download():
    import io
    import wave
    from app.model_gateway.schemas import TTSSynthesizeRequest
    from test_tts_streaming import FixtureStream, managed
    raw = FixtureStream()
    class Gateway:
        async def open_tts_stream(self, request):
            return managed(raw)
    store, storage = InMemoryStore(), _Storage()
    service = AgentExpressionAudioService(persistence_for(store), storage=storage)
    result = await service.synthesize_complete_audio(Gateway(), TTSSynthesizeRequest(text="合成测试句。"), interview_id="iv_synthetic", turn_id="turn_1")
    assert result["audio_uri"].startswith("agent-expression://") and raw.aborted == 1
    with wave.open(io.BytesIO(next(iter(storage.values.values()))), "rb") as audio:
        assert audio.getframerate() == 24000 and audio.getnchannels() == 1
        assert audio.readframes(100) == b"\x01\x00" * 10
    with persistence_for(store).transaction("org_default") as tx:
        assert tx.file_objects.list()[0]["source_type"] == "tts_complete_pcm"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["missing_final", "bad_final", "transport", "too_large", "cancel", "timeout"])
async def test_incomplete_tts_never_publishes_or_replays_partial_audio(failure):
    from app.model_gateway.schemas import TTSSynthesizeRequest
    from test_tts_streaming import FixtureStream, managed, event
    started = asyncio.Event()
    class Stream(FixtureStream):
        async def events(self):
            yield event("audio.chunk", 2, pcm_s16le=b"\x01\x00" * 10)
            started.set()
            if failure in {"cancel", "timeout"}:
                await asyncio.Event().wait()
            elif failure == "transport":
                raise RuntimeError("synthetic transport failure")
            elif failure == "bad_final":
                yield event("audio.final", 3, total_audio_bytes=22)
            elif failure == "too_large":
                yield event("audio.final", 3, total_audio_bytes=20)
    raw = Stream()
    calls = []
    class Gateway:
        async def open_tts_stream(self, request):
            calls.append(request)
            return managed(raw)
    store, storage = InMemoryStore(), _Storage()
    service = AgentExpressionAudioService(persistence_for(store), storage=storage)
    if failure == "too_large":
        service.importer.max_bytes = 50
    task = asyncio.create_task(service.synthesize_complete_audio(Gateway(), TTSSynthesizeRequest(text="合成测试句。"), interview_id="iv_synthetic", turn_id="turn_1", timeout_s=.03 if failure == "timeout" else 1))
    if failure == "cancel":
        await started.wait()
        task.cancel()
    with pytest.raises((Exception, asyncio.CancelledError)):
        await task
    assert len(calls) == 1 and raw.aborted == 1
    assert storage.values == {}
    with persistence_for(store).transaction("org_default") as tx:
        assert tx.file_objects.list() == []


@pytest.mark.anyio
async def test_only_unsupported_tts_transport_allows_batch_fallback():
    from app.model_gateway.errors import ProviderError
    from app.model_gateway.schemas import TTSSynthesizeRequest
    class Gateway:
        def __init__(self, code): self.code = code
        async def open_tts_stream(self, request):
            raise ProviderError(self.code, "synthetic", retryable=False)
    service = AgentExpressionAudioService(persistence_for(InMemoryStore()), storage=_Storage())
    kwargs = dict(interview_id="iv_synthetic", turn_id="turn_1")
    request = TTSSynthesizeRequest(text="合成测试句。")
    assert await service.synthesize_complete_audio(Gateway("provider_streaming_not_supported"), request, **kwargs) is None
    with pytest.raises(ProviderError):
        await service.synthesize_complete_audio(Gateway("provider_unauthorized"), request, **kwargs)


@pytest.mark.anyio
async def test_complete_pcm_never_turns_mock_audio_into_formal_speech():
    from types import SimpleNamespace
    from app.model_gateway.schemas import TTSSynthesizeRequest
    class Stream:
        ready_event = SimpleNamespace(provider=SimpleNamespace(provider_id="mock"))
        aborted = False
        async def abort(self): self.aborted = True
        def events(self): raise AssertionError("mock audio cannot be consumed")
    stream = Stream()
    class Gateway:
        async def open_tts_stream(self, request): return stream
    storage = _Storage()
    service = AgentExpressionAudioService(persistence_for(InMemoryStore()), storage=storage)
    with pytest.raises(ApiError) as error:
        await service.synthesize_complete_audio(Gateway(), TTSSynthesizeRequest(text="合成测试。"), interview_id="iv_synthetic", turn_id="turn_1")
    assert error.value.code == "AGENT_EXPRESSION_AUDIO_REQUIRED"
    assert stream.aborted and storage.values == {}
