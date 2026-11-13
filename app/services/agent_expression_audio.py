"""Private, replay-safe audio assets for approved agent expression.

Durable AgentEvents retain an opaque ``agent-expression://`` reference.  A
short-lived read grant is minted only while projecting an event to an
authenticated participant, so reconnecting clients never receive an expired
URL from replay history.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from app.adapters.local_media import pcm_wav_header
from app.domain.appointment_speech import turn_speech_asset_id
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.persistence.interface import Persistence
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.services.private_assets import PrivateAssetImporter


_REFERENCE_PREFIX = "agent-expression://"
_ALLOWED_PURPOSES = frozenset({"agent_expression_audio", "question_speech"})
_ARCHIVE_SLOTS = threading.BoundedSemaphore(4)
_ARCHIVE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="expression_archive")
_LOG = logging.getLogger(__name__)


def expression_audio_reference(file_id: str) -> str:
    value = str(file_id or "").strip()
    if not value:
        raise ValueError("expression audio file id cannot be empty")
    return "%s%s" % (_REFERENCE_PREFIX, value)


class AgentExpressionAudioService:
    """Owns provider audio ingestion, private storage and scoped read grants."""

    def __init__(
        self,
        persistence: Persistence,
        *,
        storage: Optional[PrivateFileStorage] = None,
        importer: Optional[PrivateAssetImporter] = None,
    ) -> None:
        self.persistence = persistence
        self.storage = storage or private_file_storage()
        self.importer = importer or PrivateAssetImporter(
            max_bytes=int(
                os.getenv("INTERVIEWER_AGENT_EXPRESSION_MAX_BYTES", "20971520")
            )
        )
        self.production = (
            os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
            == "production"
        )

    async def synthesize_complete_audio(
        self, gateway: Any, request: TTSSynthesizeRequest, *, interview_id: str,
        turn_id: Optional[str], timeout_s: float = 20,
    ) -> Optional[Dict[str, Any]]:
        """Collect validated PCM before publishing a complete private WAV.

        This avoids a second provider-URL download without exposing a live
        audio clock to the browser. Only an unsupported transport may fall
        back to batch; cancellation or partial audio must never be replayed.
        """
        open_stream = getattr(gateway, "open_tts_stream", None)
        if not callable(open_stream):
            return None

        async def collect():
            try:
                stream = await open_stream(request)
            except ProviderError as exc:
                if exc.code == "provider_streaming_not_supported":
                    return None
                raise
            pcm = bytearray()
            completed = False
            try:
                if stream.ready_event.provider.provider_id == "mock":
                    raise ApiError("AGENT_EXPRESSION_AUDIO_REQUIRED", "Development mock speech is not playable formal interview audio.", status_code=503)
                async for event in stream.events():
                    if event.type == "audio.chunk":
                        if len(pcm) + len(event.pcm_s16le) + 44 > self.importer.max_bytes:
                            raise ApiError("AGENT_EXPRESSION_AUDIO_SIZE_INVALID", "Complete speech exceeds the private asset limit.", status_code=502)
                        pcm.extend(event.pcm_s16le)
                    elif event.type == "audio.final":
                        completed = bool(pcm) and event.total_audio_bytes == len(pcm)
                if not completed:
                    raise ApiError("AGENT_EXPRESSION_AUDIO_INVALID", "Complete speech requires a validated final event.", status_code=502)
                return bytes(pcm), stream.sample_rate_hz, stream.channels
            finally:
                await stream.abort()

        result = await asyncio.wait_for(collect(), timeout=timeout_s)
        if result is None:
            return None
        pcm, sample_rate_hz, channels = result
        return await self.store_pcm_async(
            organization_id=request.organization_id, interview_id=interview_id, turn_id=turn_id,
            pcm_s16le=pcm, sample_rate_hz=sample_rate_hz, channels=channels,
            source_type="tts_complete_pcm",
        )

    async def store_pcm_async(self, *, timeout_s: float = 10.0, **audio: Any) -> Dict[str, Any]:
        """Bound blocking storage without blocking capture or losing late assets.

        Cancelling a Python waiter cannot stop a storage thread. Keep that task
        owned, and delete any late successful asset rather than publishing an
        archive reference after its output has already failed or been revoked.
        """
        return await self._store_async(self.store_pcm, timeout_s=timeout_s, **audio)

    async def _store_async(self, operation, *, timeout_s: float = 10.0, **payload: Any) -> Dict[str, Any]:
        if not _ARCHIVE_SLOTS.acquire(blocking=False):
            raise ApiError("AGENT_AUDIO_ARCHIVE_BUSY", "Speech archive capacity is busy.", status_code=503)
        state = {"cancelled": False, "result": None}
        lock = threading.Lock()

        def discard(result):
            try:
                self._discard_unpublished(result["audio_uri"], payload["organization_id"])
            except Exception as exc:
                _LOG.warning("Expression archive cleanup failed: error_type=%s", type(exc).__name__)
            finally:
                _ARCHIVE_SLOTS.release()

        def store():
            try:
                result = operation(**payload)
            except BaseException:
                _ARCHIVE_SLOTS.release()
                raise
            with lock:
                state["result"] = result
                cancelled = state["cancelled"]
            if cancelled:
                discard(result)
            return result

        # Cleanup lives in the same bounded executor, including after the
        # event loop shuts down. Cancellation cannot abandon a storage thread.
        task = asyncio.wrap_future(_ARCHIVE_EXECUTOR.submit(store))
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=max(0.01, timeout_s))
        except BaseException:
            with lock:
                state["cancelled"] = True
                late = state.pop("result", None)
            if late is not None:
                _ARCHIVE_EXECUTOR.submit(discard, late)
            raise
        _ARCHIVE_SLOTS.release()
        return result

    def _discard_unpublished(self, audio_uri: str, organization_id: str) -> None:
        file_id = audio_uri.removeprefix(_REFERENCE_PREFIX)
        with self.persistence.transaction(organization_id) as transaction:
            asset = transaction.file_objects.get(file_id)
            if asset is None:
                return
            self.storage.delete(asset["object_key"])
            transaction.file_objects.delete(file_id, expected_version=asset["version"])

    def store_pcm(
        self,
        *,
        organization_id: str,
        interview_id: str,
        turn_id: Optional[str],
        pcm_s16le: bytes,
        sample_rate_hz: int,
        channels: int,
        source_type: str = "realtime_speech_output",
    ) -> Dict[str, Any]:
        if not pcm_s16le or len(pcm_s16le) % 2:
            raise ApiError(
                "AGENT_EXPRESSION_AUDIO_INVALID",
                "Realtime speech output must contain complete PCM16 samples.",
                status_code=502,
            )
        header = pcm_wav_header(
            len(pcm_s16le),
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        content = header + pcm_s16le
        duration_ms = max(
            1,
            round(
                len(pcm_s16le)
                * 1000
                / max(1, sample_rate_hz * channels * 2)
            ),
        )
        return self._store(
            organization_id=organization_id,
            interview_id=interview_id,
            turn_id=turn_id,
            content=content,
            content_type="audio/wav",
            duration_ms=duration_ms,
            source_type=source_type,
        )

    async def import_tts(
        self,
        *,
        organization_id: str,
        interview_id: str,
        turn_id: Optional[str],
        audio_uri: str,
        content_type: str,
        duration_ms: Optional[int],
        provider_id: str,
    ) -> Dict[str, Any]:
        if provider_id == "mock":
            raise ApiError(
                "AGENT_EXPRESSION_AUDIO_REQUIRED",
                "Development mock speech is not playable formal interview audio.",
                status_code=503,
            )
        if str(audio_uri).startswith(_REFERENCE_PREFIX):
            return {
                "audio_uri": str(audio_uri),
                "duration_ms": duration_ms,
            }
        content, _ = await self.importer.audio(str(audio_uri), content_type)
        return await self._store_async(self._store,
            organization_id=organization_id,
            interview_id=interview_id,
            turn_id=turn_id,
            content=content,
            content_type=content_type,
            duration_ms=duration_ms,
            source_type="tts_provider_copy",
        )

    def issue_access(
        self,
        audio_uri: str,
        *,
        organization_id: str,
        interview_id: str,
        actor_id: str,
        expires_seconds: int = 300,
    ) -> str:
        """Exchange an opaque durable reference for one scoped read grant."""

        value = str(audio_uri or "")
        if not value.startswith(_REFERENCE_PREFIX):
            if self.production and value.startswith("/media/"):
                raise ApiError(
                    "AGENT_EXPRESSION_AUDIO_UNMANAGED",
                    "Production Agent audio cannot use local public media paths.",
                    status_code=503,
                )
            return value
        file_id = value.removeprefix(_REFERENCE_PREFIX)
        with self.persistence.transaction(organization_id) as transaction:
            file_object = transaction.file_objects.get(file_id)
            if (
                file_object is None
                or file_object.get("status") != "ready"
                or file_object.get("purpose") not in _ALLOWED_PURPOSES
                or not file_object.get("object_key")
            ):
                raise ApiError(
                    "AGENT_EXPRESSION_AUDIO_NOT_FOUND",
                    "Approved Agent expression audio is unavailable.",
                    status_code=404,
                )
            if file_object.get("purpose") == "agent_expression_audio":
                if str(file_object.get("interview_id") or "") != interview_id:
                    raise ApiError(
                        "AGENT_EXPRESSION_AUDIO_SCOPE_INVALID",
                        "Agent expression audio belongs to another interview.",
                        status_code=403,
                    )
            elif not self._question_audio_belongs_to_interview(
                transaction, interview_id, file_id
            ):
                raise ApiError(
                    "AGENT_EXPRESSION_AUDIO_SCOPE_INVALID",
                    "Question speech audio is not frozen into this interview.",
                    status_code=403,
                )
            grant = self.storage.issue_read_access(
                str(file_object["object_key"]),
                expires_seconds=expires_seconds,
            )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "interview.agent_expression.access_granted",
                    "resource_type": "interview",
                    "resource_id": interview_id,
                    "metadata": {
                        "file_object_id": file_id,
                        "expires_seconds": expires_seconds,
                    },
                    "created_at": utc_now(),
                }
            )
        return (
            grant
            if grant.startswith(("http://", "https://"))
            else "/api/v1/private-files/%s" % grant
        )

    def _store(
        self,
        *,
        organization_id: str,
        interview_id: str,
        turn_id: Optional[str],
        content: bytes,
        content_type: str,
        duration_ms: Optional[int],
        source_type: str,
    ) -> Dict[str, Any]:
        if not content or len(content) > self.importer.max_bytes:
            raise ApiError(
                "AGENT_EXPRESSION_AUDIO_SIZE_INVALID",
                "Agent expression audio is empty or exceeds the size limit.",
                status_code=502,
            )
        checksum = "sha256:%s" % hashlib.sha256(content).hexdigest()
        file_id = new_id("file")
        stored = self.storage.store(
            organization_id=organization_id,
            object_id=file_id,
            content=content,
            content_type=content_type,
            checksum=checksum,
        )
        try:
            encryption = None
            if self.production:
                encryption = self.storage.verify_encryption(stored.object_key)
            now = utc_now()
            with self.persistence.transaction(organization_id) as transaction:
                transaction.file_objects.add(
                    {
                        "id": file_id,
                        "organization_id": organization_id,
                        "purpose": "agent_expression_audio",
                        "status": "ready",
                        "storage_backend": stored.storage_backend,
                        "object_key": stored.object_key,
                        "content_type": stored.content_type,
                        "checksum": stored.checksum,
                        "byte_count": stored.byte_count,
                        "scan_status": "not_applicable",
                        "source_type": source_type,
                        "interview_id": interview_id,
                        "turn_id": turn_id,
                        "duration_ms": duration_ms,
                        "encryption": encryption,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        except Exception:
            self.storage.delete(stored.object_key)
            raise
        return {
            "audio_uri": expression_audio_reference(file_id),
            "duration_ms": duration_ms,
            "content_type": stored.content_type,
            "content_hash": stored.checksum,
        }

    @staticmethod
    def _question_audio_belongs_to_interview(
        transaction: Any, interview_id: str, file_id: str
    ) -> bool:
        session = transaction.interview_sessions.get(interview_id)
        if session is None:
            return False
        asset_ids = {
            str(turn_speech_asset_id(turn))
            for turn in session.get("turns", [])
            if turn_speech_asset_id(turn)
        }
        for asset_id in asset_ids:
            asset = transaction.question_speech_assets.get(asset_id)
            if asset and str(asset.get("file_object_id") or "") == file_id:
                return True
        return False
