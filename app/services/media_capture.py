"""Consent-bound lifecycle for private LiveKit participant captures."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.file_storage.provider import private_file_storage
from app.file_storage.interface import PrivateFileStorage
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.interviews import InterviewService
from app.services.livekit_room_binding import (
    InterviewRoomBinding,
    InterviewRoomBindingService,
)


_SAFE_KEY_PART = re.compile(r"[^A-Za-z0-9_.-]+")


class InterviewMediaCaptureService:
    """Owns capture state; transports and provider adapters cannot invent it."""

    def __init__(
        self,
        store: Any,
        *,
        persistence: Optional[Persistence] = None,
        media_plane: Optional[LiveKitMediaPlane] = None,
        storage: Optional[PrivateFileStorage] = None,
    ) -> None:
        self.store = store
        self.persistence = persistence or persistence_for(store)
        self.media_plane = media_plane or LiveKitMediaPlane()
        self.storage = storage or private_file_storage()
        self.interviews = InterviewService(store, persistence=self.persistence)

    def get_for_interview(
        self, interview_id: str, organization_id: str = "org_default"
    ) -> Optional[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            capture = next(
                (
                    item
                    for item in transaction.interview_media_captures.list()
                    if item.get("interview_id") == interview_id
                ),
                None,
            )
        return deepcopy(capture) if capture else None

    async def start_for_connection(
        self,
        interview_id: str,
        connection_id: str,
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Start and prove a consent-bound recording before formal questions.

        The durable ``starting`` row is written before the provider call and
        includes its final object key.  A retry first reconciles ListEgress, so
        a process crash after the remote start cannot create an untracked
        recording or cause an unconditional second StartParticipantEgress.
        """

        resume_start = False
        with self.persistence.transaction(organization_id) as transaction:
            ticket = next(
                (
                    item
                    for item in transaction.agent_tickets.list()
                    if item.get("interview_id") == interview_id
                    and item.get("connection_id") == connection_id
                    and item.get("status") == "consumed"
                    and item.get("participant_role") == "candidate"
                ),
                None,
            )
            if ticket is None:
                raise ApiError(
                    "MEDIA_CAPTURE_CONNECTION_INVALID",
                    "Capture requires the consumed candidate media identity.",
                    status_code=403,
                )
            capture = self._capture(transaction, interview_id)
            if capture is None:
                raise ApiError(
                    "MEDIA_CAPTURE_NOT_REQUESTED",
                    "This interview has no consented recording scope.",
                    status_code=409,
                )
            ticket_room = str((ticket.get("media") or {}).get("room_name") or "")
            if (
                not ticket_room
                or capture.get("room_name") != ticket_room
                or capture.get("organization_id") != organization_id
                or capture.get("interview_id") != interview_id
            ):
                raise ApiError(
                    "MEDIA_CAPTURE_ROOM_BINDING_INVALID",
                    "Capture requires the candidate ticket's authenticated room binding.",
                    status_code=409,
                )
            requested = set(capture.get("requested_scopes") or [])
            consented = set(capture.get("consented_scopes") or [])
            if not requested or not requested.issubset(consented):
                raise ApiError(
                    "MEDIA_CAPTURE_CONSENT_SCOPE_INVALID",
                    "Capture scopes exceed the candidate's explicit consent.",
                    status_code=409,
                )
            if capture.get("status") == "recording":
                if capture.get("participant_identity") != ticket.get(
                    "participant_identity"
                ):
                    raise ApiError(
                        "MEDIA_CAPTURE_IDENTITY_CONFLICT",
                        "Capture already belongs to another participant identity.",
                        status_code=409,
                    )
                return deepcopy(capture)
            if capture.get("status") == "stopping":
                raise ApiError(
                    "MEDIA_CAPTURE_STOPPING",
                    "The previous recording is still being finalized.",
                    status_code=409,
                )
            if capture.get("status") == "starting":
                if capture.get("participant_identity") != ticket.get(
                    "participant_identity"
                ):
                    raise ApiError(
                        "MEDIA_CAPTURE_IDENTITY_CONFLICT",
                        "Capture startup belongs to another participant identity.",
                        status_code=409,
                    )
                resume_start = True
            if capture.get("status") in {"completed", "cancelled"}:
                raise ApiError(
                    "MEDIA_CAPTURE_ALREADY_FINAL",
                    "A finalized media capture cannot be restarted.",
                    status_code=409,
                )
            if not resume_start:
                started_at = utc_now()
                capture["status"] = "starting"
                capture["participant_identity"] = ticket["participant_identity"]
                capture["connection_id"] = connection_id
                capture["failure_code"] = None
                capture["egress_id"] = None
                capture["object_key"] = self._object_key(
                    organization_id, interview_id, capture["id"]
                )
                capture["start_attempt_id"] = capture["id"]
                capture["start_requested_at"] = started_at
                capture["start_lease_expires_at"] = self._after_seconds(15)
                capture["updated_at"] = started_at
                capture = transaction.interview_media_captures.update(
                    capture, expected_version=capture["version"]
                )
            elif not capture.get("object_key"):
                capture["object_key"] = self._object_key(
                    organization_id, interview_id, capture["id"]
                )
                capture["updated_at"] = utc_now()
                capture = transaction.interview_media_captures.update(
                    capture, expected_version=capture["version"]
                )

        try:
            storage_policy = await asyncio.to_thread(
                self.storage.verify_recording_protection
            )
        except Exception as exc:
            self._mark_failed(
                capture["id"],
                interview_id,
                "recording_encryption_policy_unverified",
                exc,
                actor_id=actor_id,
                organization_id=organization_id,
            )
            raise ApiError(
                "MEDIA_CAPTURE_ENCRYPTION_UNVERIFIED",
                "无法验证录像存储保护策略，面试已暂停。",
                status_code=503,
            ) from exc
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture["id"])
            if current is None or current.get("status") != "starting":
                raise ApiError(
                    "MEDIA_CAPTURE_START_CONFLICT",
                    "验证录像存储保护策略时，录像状态已发生变化。",
                    status_code=409,
                )
            current["storage_protection_policy"] = storage_policy.descriptor
            current["storage_protection_policy_verified_at"] = utc_now()
            current["encryption_policy"] = storage_policy.encryption
            current["encryption_policy_verified_at"] = (
                utc_now() if storage_policy.encryption else None
            )
            current["updated_at"] = utc_now()
            capture = transaction.interview_media_captures.update(
                current, expected_version=current["version"]
            )

        object_key = str(capture["object_key"])

        if resume_start:
            reconciled = await self._reconcile_starting_capture(
                capture,
                actor_id=actor_id,
                organization_id=organization_id,
            )
            if reconciled.get("status") == "recording":
                return reconciled
            if reconciled.get("egress_id"):
                return await self._await_and_mark_recording(
                    reconciled,
                    actor_id=actor_id,
                    requested=requested,
                    organization_id=organization_id,
                )
            if self._lease_active(reconciled.get("start_lease_expires_at")):
                raise ApiError(
                    "MEDIA_CAPTURE_START_IN_PROGRESS",
                    "Required recording startup is still being reconciled; formal questions remain blocked.",
                    status_code=503,
                )
            # LiveKit StartParticipantEgress has no caller idempotency key. If
            # this process cannot prove whether the previous request reached
            # LiveKit, issuing another start could create two recordings. Keep
            # the interview paused for operator reconciliation instead.
            uncertainty = RuntimeError(
                "previous Egress start has no matching provider receipt"
            )
            self._mark_failed(
                capture["id"],
                interview_id,
                "egress_start_outcome_unknown",
                uncertainty,
                actor_id=actor_id,
                organization_id=organization_id,
            )
            raise ApiError(
                "MEDIA_CAPTURE_START_OUTCOME_UNKNOWN",
                "Recording startup could not be proven; the interview remains paused for operator review.",
                status_code=503,
            )

        try:
            response = await self.media_plane.start_participant_egress(
                room_name=str(capture["room_name"]),
                participant_identity=str(capture["participant_identity"]),
                object_key=object_key,
            )
            egress_id = str(
                response.get("egress_id") or response.get("egressId") or ""
            ).strip()
            if not egress_id:
                raise RuntimeError("LiveKit Egress response omitted egress_id")
        except Exception as exc:
            self._mark_failed(
                capture["id"],
                interview_id,
                "egress_start_failed",
                exc,
                actor_id=actor_id,
                organization_id=organization_id,
            )
            raise ApiError(
                "MEDIA_CAPTURE_START_FAILED",
                "Required private recording could not be started; the interview was paused.",
                status_code=503,
            ) from exc

        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture["id"])
            if current is None or current.get("status") != "starting":
                raise ApiError(
                    "MEDIA_CAPTURE_START_CONFLICT",
                    "Capture state changed after provider startup.",
                    status_code=409,
                )
            current.update(
                {
                    "egress_id": egress_id,
                    "object_key": object_key,
                    "updated_at": utc_now(),
                    "provider_start": self._provider_projection(response),
                }
            )
            capture = transaction.interview_media_captures.update(
                current, expected_version=current["version"]
            )
        return await self._await_and_mark_recording(
            capture,
            actor_id=actor_id,
            requested=requested,
            organization_id=organization_id,
            initial=response,
        )

    async def _reconcile_starting_capture(
        self,
        capture: Dict[str, Any],
        *,
        actor_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """Adopt the one remote Egress matching the durable capture identity."""

        try:
            response = await self.media_plane.list_egress(
                room_name=str(capture.get("room_name") or "")
            )
        except Exception as exc:
            raise ApiError(
                "MEDIA_CAPTURE_RECONCILIATION_FAILED",
                "Recording startup could not be reconciled; formal questions remain blocked.",
                status_code=503,
            ) from exc
        matches = [
            item
            for item in self._egress_items(response)
            if self._provider_capture_matches(capture, item)
        ]
        if len(matches) > 1:
            for item in matches:
                egress_id = self._provider_egress_id(item)
                if egress_id:
                    try:
                        await self.media_plane.stop_egress(egress_id)
                    except Exception:
                        pass
            error = RuntimeError("multiple provider Egress jobs matched one capture")
            self._mark_failed(
                capture["id"],
                capture["interview_id"],
                "egress_reconciliation_ambiguous",
                error,
                actor_id=actor_id,
                organization_id=organization_id,
            )
            raise ApiError(
                "MEDIA_CAPTURE_RECONCILIATION_AMBIGUOUS",
                "Multiple recordings matched one capture and were stopped.",
                status_code=503,
            )
        if not matches:
            return self.get_for_interview(
                capture["interview_id"], organization_id
            ) or deepcopy(capture)
        match = matches[0]
        egress_id = self._provider_egress_id(match)
        if not egress_id:
            raise ApiError(
                "MEDIA_CAPTURE_RECONCILIATION_FAILED",
                "The matched provider recording omitted its identity.",
                status_code=503,
            )
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture["id"])
            if current is None or current.get("status") != "starting":
                raise ApiError(
                    "MEDIA_CAPTURE_START_CONFLICT",
                    "Capture state changed during provider reconciliation.",
                    status_code=409,
                )
            existing = str(current.get("egress_id") or "")
            if existing and existing != egress_id:
                raise ApiError(
                    "MEDIA_CAPTURE_EGRESS_ID_CONFLICT",
                    "Capture is already bound to another provider recording.",
                    status_code=409,
                )
            current["egress_id"] = egress_id
            current["provider_start"] = self._provider_projection(match)
            current["reconciled_at"] = utc_now()
            current["updated_at"] = utc_now()
            return transaction.interview_media_captures.update(
                current, expected_version=current["version"]
            )

    async def _await_and_mark_recording(
        self,
        capture: Dict[str, Any],
        *,
        actor_id: str,
        requested: set,
        organization_id: str,
        initial: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Require provider ACTIVE truth; a returned ``starting`` is not enough."""

        provider = initial or capture.get("provider_start") or {}
        egress_id = str(capture.get("egress_id") or self._provider_egress_id(provider))
        if not egress_id:
            raise ApiError(
                "MEDIA_CAPTURE_EGRESS_ID_MISSING",
                "Recording startup has no provider identity.",
                status_code=503,
            )
        for attempt in range(26):
            persisted = self.get_for_interview(
                capture["interview_id"], organization_id
            )
            if persisted and persisted.get("status") == "recording" and str(
                persisted.get("egress_id") or ""
            ) == egress_id:
                return persisted
            status = self._provider_status(provider)
            if status in {"EGRESS_ACTIVE", "ACTIVE", "1"}:
                with self.persistence.transaction(organization_id) as transaction:
                    current = transaction.interview_media_captures.get(capture["id"])
                    if current is None:
                        raise ApiError(
                            "MEDIA_CAPTURE_NOT_FOUND",
                            "Media capture no longer exists.",
                            status_code=404,
                        )
                    if current.get("status") == "recording":
                        return deepcopy(current)
                    if current.get("status") != "starting" or str(
                        current.get("egress_id") or ""
                    ) != egress_id:
                        raise ApiError(
                            "MEDIA_CAPTURE_START_CONFLICT",
                            "Capture state changed before provider activation.",
                            status_code=409,
                        )
                    current.update(
                        {
                            "status": "recording",
                            "started_at": current.get("started_at") or utc_now(),
                            "start_lease_expires_at": None,
                            "updated_at": utc_now(),
                            "provider_start": self._provider_projection(provider),
                        }
                    )
                    current = transaction.interview_media_captures.update(
                        current, expected_version=current["version"]
                    )
                    self._audit(
                        transaction,
                        current,
                        actor_id,
                        "interview.media_capture.started",
                        {
                            "egress_id": egress_id,
                            "requested_scopes": sorted(requested),
                            "provider_status": status,
                        },
                    )
                    return deepcopy(current)
            if status in {
                "EGRESS_COMPLETE",
                "COMPLETE",
                "3",
                "EGRESS_FAILED",
                "FAILED",
                "4",
                "EGRESS_ABORTED",
                "ABORTED",
                "5",
            }:
                break
            if attempt == 25:
                break
            await asyncio.sleep(0.2)
            listed = await self.media_plane.list_egress(egress_id=egress_id)
            items = self._egress_items(listed)
            provider = items[0] if items else {}
        try:
            await self.media_plane.stop_egress(egress_id)
        except Exception:
            pass
        error = RuntimeError("provider Egress did not reach ACTIVE before deadline")
        self._mark_failed(
            capture["id"],
            capture["interview_id"],
            "egress_activation_unconfirmed",
            error,
            actor_id=actor_id,
            organization_id=organization_id,
        )
        raise ApiError(
            "MEDIA_CAPTURE_NOT_RECORDING",
            "Required recording was not confirmed active; the interview was paused.",
            status_code=503,
        )

    async def stop_for_interview(
        self,
        interview_id: str,
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Optional[Dict[str, Any]]:
        capture = self.get_for_interview(interview_id, organization_id)
        if capture is None:
            return None
        if capture.get("status") in {"completed", "cancelled", "failed"}:
            return capture
        if capture.get("status") == "pending":
            return self._cancel_pending(
                capture, actor_id=actor_id, organization_id=organization_id
            )
        if capture.get("status") == "starting":
            capture = await self._reconcile_starting_capture(
                capture,
                actor_id=actor_id,
                organization_id=organization_id,
            )
        egress_id = str(capture.get("egress_id") or "")
        if not egress_id:
            self._mark_failed(
                capture["id"],
                interview_id,
                "egress_identity_missing",
                RuntimeError("capture has no egress id"),
                actor_id=actor_id,
                organization_id=organization_id,
            )
            return self.get_for_interview(interview_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture["id"])
            if current is None:
                return None
            if current.get("status") == "recording":
                current["status"] = "stopping"
                current["updated_at"] = utc_now()
                transaction.interview_media_captures.update(
                    current, expected_version=current["version"]
                )
        try:
            response = await self.media_plane.stop_egress(egress_id)
        except Exception as exc:
            self._mark_failed(
                capture["id"],
                interview_id,
                "egress_stop_failed",
                exc,
                actor_id=actor_id,
                organization_id=organization_id,
            )
            raise ApiError(
                "MEDIA_CAPTURE_STOP_FAILED",
                "Private recording could not be finalized.",
                status_code=503,
            ) from exc
        return await self.finalize_provider_result(
            capture["id"],
            response,
            actor_id=actor_id,
            organization_id=organization_id,
        )

    async def finalize_provider_result(
        self,
        capture_id: str,
        provider_result: Dict[str, Any],
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            capture = transaction.interview_media_captures.get(capture_id)
        if capture is None:
            raise ApiError(
                "MEDIA_CAPTURE_NOT_FOUND", "Media capture does not exist.", status_code=404
            )
        object_key = str(capture.get("object_key") or "")
        content_hash = None
        byte_count = None
        if object_key:
            try:
                content = await asyncio.to_thread(
                    self.storage.open, object_key
                )
                content_hash = "sha256:%s" % hashlib.sha256(content).hexdigest()
                byte_count = len(content)
            except Exception:
                # Some object stores are eventually consistent. The capture is
                # kept in `hash_pending`, never presented as fully verified.
                pass
        storage_protection = None
        encryption = None
        if content_hash:
            try:
                protection = await asyncio.to_thread(
                    self.storage.verify_recording_protection, object_key
                )
                storage_protection = protection.descriptor
                encryption = protection.encryption
            except Exception as exc:
                self._mark_failed(
                    capture_id,
                    capture["interview_id"],
                    "recording_object_encryption_unverified",
                    exc,
                    actor_id=actor_id,
                    organization_id=organization_id,
                )
                raise ApiError(
                    "MEDIA_CAPTURE_ENCRYPTION_UNVERIFIED",
                    "最终录像未能证明所需的存储保护策略。",
                    status_code=503,
                ) from exc
        status = (
            "completed" if content_hash and storage_protection else "hash_pending"
        )
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture_id)
            if current is None:
                raise ApiError(
                    "MEDIA_CAPTURE_NOT_FOUND", "Media capture does not exist.", status_code=404
                )
            current.update(
                {
                    "status": status,
                    "private_uri": "private-media-capture://%s" % capture_id,
                    "content_hash": content_hash,
                    "byte_count": byte_count,
                    "encryption": encryption,
                    "encryption_verified_at": utc_now() if encryption else None,
                    "storage_protection": storage_protection,
                    "storage_protection_verified_at": (
                        utc_now() if storage_protection else None
                    ),
                    "provider_result": self._provider_projection(provider_result),
                    "stopped_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            current = transaction.interview_media_captures.update(
                current, expected_version=current["version"]
            )
            self._audit(
                transaction,
                current,
                actor_id,
                "interview.media_capture.finalized",
                {"status": status, "content_hash": content_hash},
            )
            return deepcopy(current)

    async def handle_provider_webhook(
        self,
        event: Dict[str, Any],
        *,
        event_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve tenant/interview only from the authenticated provider event.

        The webhook adapter has already verified LiveKit's JWT and body hash.
        This module additionally verifies the room's application signature,
        enforces the persisted capture/room/Egress binding, and records a
        durable body fingerprint so provider retries are idempotent.
        """

        event_name = str(event.get("event") or "")
        if event_name not in {"egress_started", "egress_updated", "egress_ended"}:
            raise ApiError(
                "LIVEKIT_EGRESS_WEBHOOK_INVALID",
                "The callback is not a LiveKit Egress lifecycle event.",
                status_code=422,
            )
        info = event.get("egress_info") or event.get("egressInfo") or {}
        if not isinstance(info, dict):
            raise ApiError(
                "LIVEKIT_EGRESS_WEBHOOK_INVALID",
                "LiveKit Egress webhook facts are invalid.",
                status_code=422,
            )
        egress_id = str(info.get("egress_id") or info.get("egressId") or "")
        if not egress_id:
            raise ApiError(
                "LIVEKIT_EGRESS_WEBHOOK_INVALID",
                "LiveKit Egress webhook omitted its Egress identity.",
                status_code=422,
            )
        room_name = self._provider_room_name(event, info)
        try:
            binding = InterviewRoomBindingService().verify(room_name)
        except ValueError as exc:
            raise ApiError(
                "LIVEKIT_EGRESS_ROOM_BINDING_INVALID",
                "LiveKit Egress webhook room identity is invalid.",
                status_code=403,
            ) from exc
        fingerprint = self._provider_event_fingerprint(event, event_fingerprint)
        organization_id = binding.organization_id
        with self.persistence.transaction(organization_id) as transaction:
            capture = next(
                (
                    item
                    for item in transaction.interview_media_captures.list()
                    if item.get("egress_id") == egress_id
                ),
                None,
            )
            if capture is None:
                candidates = [
                    item
                    for item in transaction.interview_media_captures.list()
                    if item.get("interview_id") == binding.interview_id
                    and item.get("organization_id") == organization_id
                    and item.get("status") == "starting"
                    and not item.get("egress_id")
                    and self._provider_capture_matches(item, info)
                ]
                if len(candidates) == 1:
                    adopted = candidates[0]
                    adopted["egress_id"] = egress_id
                    adopted["provider_start"] = self._provider_projection(info)
                    adopted["reconciled_at"] = utc_now()
                    if self._provider_status(info) in {
                        "EGRESS_ACTIVE",
                        "ACTIVE",
                        "1",
                    }:
                        adopted["status"] = "recording"
                        adopted["started_at"] = adopted.get("started_at") or utc_now()
                        adopted["start_lease_expires_at"] = None
                    adopted["updated_at"] = utc_now()
                    capture = transaction.interview_media_captures.update(
                        adopted, expected_version=adopted["version"]
                    )
        if capture is None:
            raise self._provider_binding_error()
        self._require_provider_binding(capture, binding, room_name, egress_id, info)
        if self._has_provider_receipt(capture, fingerprint):
            return deepcopy(capture)

        status = str(info.get("status") or "").upper()
        error = str(info.get("error") or "")
        if event_name in {"egress_ended", "egress_updated"} and (
            error or status in {"EGRESS_FAILED", "FAILED", "4"}
        ):
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.interview_media_captures.get(capture["id"])
                if current is None:
                    raise self._provider_binding_error()
                self._require_provider_binding(
                    current, binding, room_name, egress_id, info
                )
                if self._has_provider_receipt(current, fingerprint):
                    return deepcopy(current)
                current.update(
                    {
                        "status": "failed",
                        "failure_code": "egress_provider_failed",
                        "failure_type": "LiveKitEgressFailure",
                        "provider_result": self._provider_projection(info),
                        "updated_at": utc_now(),
                    }
                )
                self._append_provider_receipt(
                    current, fingerprint=fingerprint, event_name=event_name
                )
                current = transaction.interview_media_captures.update(
                    current, expected_version=current["version"]
                )
                self._audit(
                    transaction,
                    current,
                    "livekit_webhook",
                    "interview.media_capture.failed",
                    {
                        "failure_code": "egress_provider_failed",
                        "failure_type": "LiveKitEgressFailure",
                        "provider_event": event_name,
                    },
                )
            session = self.interviews.get_interview(binding.interview_id, organization_id)
            if session.get("status") == "in_progress":
                self.interviews.pause_interview(
                    binding.interview_id,
                    reason="required_media_capture_failed",
                    organization_id=organization_id,
                )
            return deepcopy(current)
        if event_name != "egress_ended":
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.interview_media_captures.get(capture["id"])
                if current is None:
                    raise self._provider_binding_error()
                self._require_provider_binding(
                    current, binding, room_name, egress_id, info
                )
                if self._has_provider_receipt(current, fingerprint):
                    return deepcopy(current)
                current["provider_result"] = self._provider_projection(info)
                current["updated_at"] = utc_now()
                self._append_provider_receipt(
                    current, fingerprint=fingerprint, event_name=event_name
                )
                current = transaction.interview_media_captures.update(
                    current, expected_version=current["version"]
                )
                self._audit(
                    transaction,
                    current,
                    "livekit_webhook",
                    "interview.media_capture.provider_webhook.accepted",
                    {"provider_event": event_name},
                )
                return deepcopy(current)

        object_key = str(capture.get("object_key") or "")
        content_hash = None
        byte_count = None
        if object_key:
            try:
                content = await asyncio.to_thread(self.storage.open, object_key)
                content_hash = "sha256:%s" % hashlib.sha256(content).hexdigest()
                byte_count = len(content)
            except Exception:
                pass
        storage_protection = None
        encryption = None
        if content_hash:
            try:
                protection = await asyncio.to_thread(
                    self.storage.verify_recording_protection, object_key
                )
                storage_protection = protection.descriptor
                encryption = protection.encryption
            except Exception as exc:
                self._mark_failed(
                    capture["id"],
                    capture["interview_id"],
                    "recording_object_encryption_unverified",
                    exc,
                    actor_id="livekit_webhook",
                    organization_id=organization_id,
                )
                return self.get_for_interview(
                    capture["interview_id"], organization_id
                ) or deepcopy(capture)
        final_status = (
            "completed" if content_hash and storage_protection else "hash_pending"
        )
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture["id"])
            if current is None:
                raise self._provider_binding_error()
            self._require_provider_binding(current, binding, room_name, egress_id, info)
            if self._has_provider_receipt(current, fingerprint):
                return deepcopy(current)
            current.update(
                {
                    "status": final_status,
                    "private_uri": "private-media-capture://%s" % current["id"],
                    "content_hash": content_hash,
                    "byte_count": byte_count,
                    "encryption": encryption,
                    "encryption_verified_at": utc_now() if encryption else None,
                    "storage_protection": storage_protection,
                    "storage_protection_verified_at": (
                        utc_now() if storage_protection else None
                    ),
                    "provider_result": self._provider_projection(info),
                    "stopped_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            self._append_provider_receipt(
                current, fingerprint=fingerprint, event_name=event_name
            )
            current = transaction.interview_media_captures.update(
                current, expected_version=current["version"]
            )
            self._audit(
                transaction,
                current,
                "livekit_webhook",
                "interview.media_capture.finalized",
                {
                    "status": final_status,
                    "content_hash": content_hash,
                    "provider_event": event_name,
                },
            )
            return deepcopy(current)

    @staticmethod
    def _provider_room_name(event: Dict[str, Any], info: Dict[str, Any]) -> str:
        room = event.get("room") or {}
        room_name = str(
            info.get("room_name")
            or info.get("roomName")
            or (room.get("name") if isinstance(room, dict) else "")
            or (room.get("room_name") if isinstance(room, dict) else "")
            or (room.get("roomName") if isinstance(room, dict) else "")
            or event.get("room_name")
            or event.get("roomName")
            or ""
        ).strip()
        if not room_name:
            raise ApiError(
                "LIVEKIT_EGRESS_WEBHOOK_INVALID",
                "LiveKit Egress webhook omitted its room identity.",
                status_code=422,
            )
        return room_name

    @staticmethod
    def _provider_event_fingerprint(
        event: Dict[str, Any], supplied: Optional[str]
    ) -> str:
        if supplied is not None:
            normalized = str(supplied).lower()
            if not re.fullmatch(r"[a-f0-9]{64}", normalized):
                raise ApiError(
                    "LIVEKIT_EGRESS_WEBHOOK_INVALID",
                    "LiveKit Egress webhook fingerprint is invalid.",
                    status_code=422,
                )
            return normalized
        canonical = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _require_provider_binding(
        cls,
        capture: Dict[str, Any],
        binding: InterviewRoomBinding,
        room_name: str,
        egress_id: str,
        info: Dict[str, Any],
    ) -> None:
        provider_identity = cls._provider_identity(info)
        provider_keys = cls._provider_object_keys(info)
        if (
            capture.get("provider") != "livekit"
            or capture.get("organization_id") != binding.organization_id
            or capture.get("interview_id") != binding.interview_id
            or capture.get("room_name") != room_name
            or capture.get("egress_id") != egress_id
            or (
                provider_identity
                and capture.get("participant_identity") != provider_identity
            )
            or (
                provider_keys
                and str(capture.get("object_key") or "") not in provider_keys
            )
        ):
            raise InterviewMediaCaptureService._provider_binding_error()

    @staticmethod
    def _provider_binding_error() -> ApiError:
        return ApiError(
            "LIVEKIT_EGRESS_CAPTURE_BINDING_INVALID",
            "LiveKit Egress webhook does not match a persisted capture binding.",
            status_code=409,
        )

    @staticmethod
    def _has_provider_receipt(capture: Dict[str, Any], fingerprint: str) -> bool:
        return any(
            hmac.compare_digest(str(item.get("fingerprint") or ""), fingerprint)
            for item in capture.get("provider_webhook_receipts") or []
            if isinstance(item, dict)
        )

    @staticmethod
    def _append_provider_receipt(
        capture: Dict[str, Any], *, fingerprint: str, event_name: str
    ) -> None:
        receipts = [
            deepcopy(item)
            for item in capture.get("provider_webhook_receipts") or []
            if isinstance(item, dict)
        ]
        receipts.append(
            {
                "fingerprint": fingerprint,
                "event": event_name,
                "accepted_at": utc_now(),
            }
        )
        # An Egress emits only a small lifecycle event set. Keeping all body
        # fingerprints preserves replay idempotency even for very late retries.
        capture["provider_webhook_receipts"] = receipts

    def _mark_failed(
        self,
        capture_id: str,
        interview_id: str,
        code: str,
        exc: Exception,
        *,
        actor_id: str,
        organization_id: str,
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            capture = transaction.interview_media_captures.get(capture_id)
            if capture is not None:
                capture.update(
                    {
                        "status": "failed",
                        "failure_code": code,
                        "failure_type": type(exc).__name__,
                        "updated_at": utc_now(),
                    }
                )
                capture = transaction.interview_media_captures.update(
                    capture, expected_version=capture["version"]
                )
                self._audit(
                    transaction,
                    capture,
                    actor_id,
                    "interview.media_capture.failed",
                    {"failure_code": code, "failure_type": type(exc).__name__},
                )
        session = self.interviews.get_interview(interview_id, organization_id)
        if session.get("status") == "in_progress":
            self.interviews.pause_interview(
                interview_id,
                reason="required_media_capture_failed",
                organization_id=organization_id,
            )

    def _cancel_pending(
        self, capture: Dict[str, Any], *, actor_id: str, organization_id: str
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_media_captures.get(capture["id"])
            if current is None:
                return capture
            current.update(
                {"status": "cancelled", "stopped_at": utc_now(), "updated_at": utc_now()}
            )
            current = transaction.interview_media_captures.update(
                current, expected_version=current["version"]
            )
            self._audit(
                transaction,
                current,
                actor_id,
                "interview.media_capture.cancelled",
                {"reason": "interview_ended_before_media_publish"},
            )
            return deepcopy(current)

    @staticmethod
    def _capture(transaction: Any, interview_id: str) -> Optional[Dict[str, Any]]:
        return next(
            (
                item
                for item in transaction.interview_media_captures.list()
                if item.get("interview_id") == interview_id
            ),
            None,
        )

    @staticmethod
    def _object_key(organization_id: str, interview_id: str, capture_id: str) -> str:
        values = [
            _SAFE_KEY_PART.sub("_", value).strip("._")
            for value in (organization_id, interview_id, capture_id)
        ]
        if any(not value for value in values):
            raise ApiError("MEDIA_CAPTURE_KEY_INVALID", "Capture storage key is invalid.")
        return "interview-captures/%s/%s/%s.mp4" % tuple(values)

    @staticmethod
    def _after_seconds(seconds: int) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _lease_active(value: Any) -> bool:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return bool(
            parsed.tzinfo is not None and parsed > datetime.now(timezone.utc)
        )

    @staticmethod
    def _egress_items(value: Dict[str, Any]) -> list:
        items = value.get("items") if isinstance(value, dict) else None
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if isinstance(value, dict) and InterviewMediaCaptureService._provider_egress_id(value):
            return [value]
        return []

    @staticmethod
    def _provider_egress_id(value: Dict[str, Any]) -> str:
        return str(value.get("egress_id") or value.get("egressId") or "").strip()

    @staticmethod
    def _provider_status(value: Dict[str, Any]) -> str:
        return str(value.get("status") or "").strip().upper()

    @staticmethod
    def _provider_participant(value: Dict[str, Any]) -> Dict[str, Any]:
        direct = value.get("participant")
        if isinstance(direct, dict):
            return direct
        request = value.get("request")
        if isinstance(request, dict) and isinstance(request.get("participant"), dict):
            return request["participant"]
        return value

    @classmethod
    def _provider_identity(cls, value: Dict[str, Any]) -> str:
        participant = cls._provider_participant(value)
        return str(
            participant.get("identity")
            or participant.get("participant_identity")
            or participant.get("participantIdentity")
            or value.get("identity")
            or ""
        ).strip()

    @classmethod
    def _provider_object_keys(cls, value: Dict[str, Any]) -> set:
        participant = cls._provider_participant(value)
        outputs = participant.get("file_outputs") or participant.get("fileOutputs") or []
        if not isinstance(outputs, list):
            return set()
        return {
            str(item.get("filepath") or "").strip()
            for item in outputs
            if isinstance(item, dict) and item.get("filepath")
        }

    @classmethod
    def _provider_capture_matches(
        cls, capture: Dict[str, Any], value: Dict[str, Any]
    ) -> bool:
        room_name = str(
            value.get("room_name") or value.get("roomName") or ""
        ).strip()
        if room_name and room_name != str(capture.get("room_name") or ""):
            return False
        identity = cls._provider_identity(value)
        if identity and identity != str(capture.get("participant_identity") or ""):
            return False
        object_keys = cls._provider_object_keys(value)
        expected_key = str(capture.get("object_key") or "")
        if object_keys and expected_key not in object_keys:
            return False
        # At least one provider-side binding fact in addition to the room must
        # be present, so a malformed room-wide listing cannot be adopted.
        return bool(identity or object_keys)

    @staticmethod
    def _provider_projection(value: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: deepcopy(item)
            for key, item in value.items()
            if key
            in {
                "egress_id",
                "egressId",
                "status",
                "started_at",
                "ended_at",
                "updated_at",
                "error",
                "file_results",
                "room_name",
                "roomName",
                "participant",
                "request",
            }
        }

    @staticmethod
    def _audit(
        transaction: Any,
        capture: Dict[str, Any],
        actor_id: str,
        action: str,
        metadata: Dict[str, Any],
    ) -> None:
        transaction.audit_events.add(
            {
                "id": new_id("audit"),
                "organization_id": capture["organization_id"],
                "actor_id": actor_id,
                "action": action,
                "resource_type": "interview_media_capture",
                "resource_id": capture["id"],
                "metadata": deepcopy(metadata),
                "created_at": utc_now(),
            }
        )
