"""Short-lived, one-time tickets for the unified interview agent channel."""

from __future__ import annotations

import hashlib
import secrets
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.auth import Principal
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.interview_agent_release import realtime_agent_release_status
from app.core.time import utc_now
from app.persistence.provider import persistence_for
from app.services.interviews import InterviewService
from app.services.livekit_room_binding import interview_room_name


@dataclass(frozen=True)
class ConsumedAgentTicket:
    interview_id: str
    principal: Principal
    connection_id: str
    participant_identity: str
    media: Dict[str, Any]


class InterviewAgentTicketService:
    """Owns application tickets and least-privilege LiveKit room grants."""

    def __init__(self, store: Any, *, media_plane: Optional[LiveKitMediaPlane] = None) -> None:
        self.store = store
        self.persistence = persistence_for(store)
        self.interviews = InterviewService(store)
        self.media_plane = media_plane or LiveKitMediaPlane()

    def issue_candidate(
        self,
        interview_id: str,
        candidate_session_token: Optional[str],
        *,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self.interviews.validate_candidate_token(
            interview_id, candidate_session_token, organization_id
        )
        session = self.interviews.get_interview(interview_id, organization_id)
        candidate_id = str(session.get("candidate", {}).get("id") or "candidate")
        principal = Principal(
            actor_id="candidate:%s" % candidate_id,
            organization_id=organization_id,
            roles=frozenset({"candidate"}),
            authenticated=True,
        )
        return self._issue(session, principal, participant_role="candidate")

    def issue_enterprise(
        self,
        interview_id: str,
        principal: Principal,
        *,
        organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not principal.roles.intersection({"admin", "interviewer", "reviewer"}):
            raise ApiError(
                "AGENT_TICKET_FORBIDDEN",
                "Enterprise interview access is required.",
                status_code=403,
            )
        tenant = organization_id or principal.organization_id
        session = self.interviews.get_interview(interview_id, tenant)
        role = "human" if principal.roles.intersection({"admin", "interviewer"}) else "observer"
        return self._issue(session, principal, participant_role=role)

    def issue_takeover_media_permit(
        self,
        interview_id: str,
        principal: Principal,
        *,
        lease_id: str,
        expected_version: int,
        organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Exchange one active takeover lease for a microphone-only grant.

        The ordinary enterprise ticket remains receive-only. The caller must
        prove ownership of the current persisted lease on every exchange; the
        returned LiveKit token is scoped to one room, one synthetic participant
        identity and at most thirty seconds.
        """

        if not principal.roles.intersection({"admin", "interviewer"}):
            raise ApiError(
                "TAKEOVER_MEDIA_FORBIDDEN",
                "Only an interviewer with an active takeover lease may publish audio.",
                status_code=403,
            )
        tenant = organization_id or principal.organization_id
        if tenant != principal.organization_id:
            raise ApiError(
                "TAKEOVER_MEDIA_SCOPE_INVALID",
                "Takeover media permit belongs to another organization.",
                status_code=403,
            )
        self.media_plane.require_ready(recording=False)
        permit_id = new_id("takeover_media_permit")
        with self.persistence.transaction(tenant) as transaction:
            now = transaction.database_now()
            session = transaction.interview_sessions.get(interview_id)
            if session is None:
                raise ApiError(
                    "INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404
                )
            lease = deepcopy((session.get("agent_runtime") or {}).get("takeover"))
            if (
                not lease
                or lease.get("actor_id") != principal.actor_id
                or lease.get("lease_id") != lease_id
                or int(lease.get("version", 0)) != int(expected_version)
                or self._parse(str(lease.get("expires_at") or "1970-01-01T00:00:00Z"))
                <= now
            ):
                raise ApiError(
                    "TAKEOVER_LEASE_LOST",
                    "Takeover media permit requires the current active lease.",
                    status_code=409,
                )
            remaining_seconds = int(
                (self._parse(str(lease["expires_at"])) - now).total_seconds()
            )
            if remaining_seconds < 15:
                raise ApiError(
                    "TAKEOVER_LEASE_EXPIRING",
                    "Renew the takeover lease before requesting microphone media.",
                    status_code=409,
                )
            if int(lease.get("media_permit_lease_version") or 0) == int(
                expected_version
            ):
                raise ApiError(
                    "TAKEOVER_MEDIA_PERMIT_ALREADY_ISSUED",
                    "This takeover lease revision has already exchanged its media permit.",
                    status_code=409,
                )
            # A LiveKit JWT is an admission bearer rather than a revocable
            # session handle. Keep its replay window to the provider minimum;
            # the connected participant is separately removed on release/loss.
            ttl_seconds = min(15, remaining_seconds)
            participant_identity = str(
                lease.get("media_participant_identity")
                or "takeover:%s" % lease_id
            )
            room_name = interview_room_name(tenant, interview_id)
            participant_token = self.media_plane.issue_participant_token(
                room_name=room_name,
                identity=participant_identity,
                participant_role="takeover",
                publish_sources=("microphone",),
                can_subscribe=False,
                ttl_seconds=ttl_seconds,
            )
            permit_expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat().replace(
                "+00:00", "Z"
            )
            lease["media_participant_identity"] = participant_identity
            lease["media_permit_generation"] = int(
                lease.get("media_permit_generation", 0)
            ) + 1
            lease["media_permit_lease_version"] = int(expected_version)
            lease["media_permit_issued_at"] = now.isoformat().replace("+00:00", "Z")
            lease["media_permit_expires_at"] = permit_expires_at
            session.setdefault("agent_runtime", {})["takeover"] = lease
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": tenant,
                    "actor_id": principal.actor_id,
                    "action": "interview.takeover.media_permit.issued",
                    "resource_type": "interview",
                    "resource_id": interview_id,
                    "metadata": {
                        "permit_id": permit_id,
                        "lease_id": lease_id,
                        "lease_version": int(expected_version),
                        "permit_generation": lease["media_permit_generation"],
                        "expires_at": permit_expires_at,
                        "publish_sources": ["microphone"],
                    },
                    "created_at": utc_now(),
                }
            )
        return {
            "permit_id": permit_id,
            "lease": {
                "lease_id": lease_id,
                "version": int(expected_version),
                "expires_at": lease["expires_at"],
            },
            "media": {
                "provider": "livekit",
                "status": "ready",
                "url": self.media_plane.url,
                "room_name": room_name,
                "participant_identity": participant_identity,
                "participant_token": participant_token,
                "publish_sources": ["microphone"],
                "can_subscribe": False,
                "expires_at": permit_expires_at,
            },
        }

    def consume(
        self,
        interview_id: str,
        raw_ticket: str,
        *,
        organization_id: str = "org_default",
    ) -> ConsumedAgentTicket:
        digest = self._digest(raw_ticket)
        expired = False
        release_blocked: Optional[Dict[str, Any]] = None
        with self.persistence.transaction(organization_id) as transaction:
            now = transaction.database_now()
            ticket = next(
                (
                    item
                    for item in transaction.agent_tickets.list()
                    if item.get("ticket_hash") == digest
                    and item.get("interview_id") == interview_id
                ),
                None,
            )
            if ticket is None:
                raise ApiError(
                    "AGENT_TICKET_INVALID",
                    "Agent ticket is invalid.",
                    status_code=403,
                )
            if ticket.get("status") != "issued" or ticket.get("consumed_at"):
                raise ApiError(
                    "AGENT_TICKET_ALREADY_USED",
                    "Agent ticket has already been used.",
                    status_code=409,
                )
            if self._parse(ticket["expires_at"]) <= now:
                ticket["status"] = "expired"
                ticket["updated_at"] = now.isoformat().replace("+00:00", "Z")
                transaction.agent_tickets.update(ticket, expected_version=ticket["version"])
                expired = True
            elif ticket.get("participant_role") == "candidate" and self._production():
                release = realtime_agent_release_status(organization_id)
                if not release["ready"]:
                    ticket["status"] = "revoked"
                    ticket["revoked_reason"] = "release_gate_not_ready"
                    ticket["updated_at"] = now.isoformat().replace("+00:00", "Z")
                    transaction.agent_tickets.update(
                        ticket, expected_version=ticket["version"]
                    )
                    release_blocked = release
                else:
                    ticket["status"] = "consumed"
                    ticket["consumed_at"] = now.isoformat().replace("+00:00", "Z")
                    ticket["updated_at"] = ticket["consumed_at"]
                    transaction.agent_tickets.update(
                        ticket, expected_version=ticket["version"]
                    )
            else:
                ticket["status"] = "consumed"
                ticket["consumed_at"] = now.isoformat().replace("+00:00", "Z")
                ticket["updated_at"] = ticket["consumed_at"]
                transaction.agent_tickets.update(
                    ticket, expected_version=ticket["version"]
                )

        if expired:
            raise ApiError(
                "AGENT_TICKET_EXPIRED",
                "Agent ticket has expired.",
                status_code=410,
            )
        if release_blocked is not None:
            raise ApiError(
                "REALTIME_AGENT_RELEASE_NOT_READY",
                "This organization and release no longer pass the production real-time-agent gate.",
                status_code=503,
                details={
                    key: release_blocked[key]
                    for key in (
                        "organization_enabled",
                        "release_scope_configured",
                        "acceptance_report_ready",
                    )
                },
            )

        principal = Principal(
            actor_id=ticket["actor_id"],
            organization_id=ticket["organization_id"],
            roles=frozenset(ticket["roles"]),
            authenticated=True,
        )
        return ConsumedAgentTicket(
            interview_id=interview_id,
            principal=principal,
            connection_id=ticket["connection_id"],
            participant_identity=ticket["participant_identity"],
            media=deepcopy(ticket.get("media") or {}),
        )

    def _issue(
        self,
        session: Dict[str, Any],
        principal: Principal,
        *,
        participant_role: str,
    ) -> Dict[str, Any]:
        settings = session.get("settings") or {}
        video_required = bool(settings.get("record_video"))
        consent_scopes = set(
            session.get("candidate", {}).get("media_consent_scopes") or []
        )
        if participant_role == "candidate":
            if settings.get("record_audio", True) and "audio_recording" not in consent_scopes:
                raise ApiError(
                    "AUDIO_RECORDING_CONSENT_REQUIRED",
                    "Explicit audio recording consent is required.",
                    status_code=409,
                )
            if video_required and "video_recording" not in consent_scopes:
                raise ApiError(
                    "VIDEO_RECORDING_CONSENT_REQUIRED",
                    "Explicit video recording consent is required.",
                    status_code=409,
                )
            if self._production() and "audio_recording" not in consent_scopes:
                raise ApiError(
                    "AUDIO_EVIDENCE_CONSENT_REQUIRED",
                    "Formal server-authoritative STT requires explicit consent to persist answer audio evidence.",
                    status_code=409,
                )

        # Enterprise rooms are observe-only by default. An interviewer may
        # publish microphone media only after acquiring the exclusive takeover
        # lease and exchanging it for a separate short-lived media permit.
        publish_sources = ["microphone"] if participant_role == "candidate" else []
        if participant_role == "candidate" and video_required:
            publish_sources.append("camera")

        connection_id = new_id("connection")
        room_name = interview_room_name(principal.organization_id, session["id"])
        browser_backfill = (
            {
                "enabled": True,
                "protocol": "agent-json-backfill.v1",
                "connection_id": connection_id,
                "audio_epoch": secrets.token_urlsafe(18),
                "retention_ms": 30_000,
                "max_bytes": 2 * 1024 * 1024,
                "max_frame_bytes": 32 * 1024,
            }
            if participant_role == "candidate"
            else None
        )
        participant_identity = "%s:%s" % (participant_role, connection_id)
        if participant_role == "candidate":
            # 候选人刷新页面时控制连接需要换新，但 LiveKit 发布身份必须保持不变。
            # 服务端证据订阅器会把首次发布身份冻结为权威媒体绑定；如果这里每次
            # 都生成新身份，音轨虽然进入房间，却会因身份不匹配而被安全过滤掉。
            frozen_binding = (
                (session.get("agent_runtime") or {}).get("authoritative_media_binding") or {}
            )
            frozen_identity = str(frozen_binding.get("candidate_identity") or "").strip()
            frozen_room = str(frozen_binding.get("room_name") or "").strip()
            if frozen_identity:
                if frozen_room != room_name or not frozen_identity.startswith("candidate:"):
                    raise ApiError(
                        "LIVEKIT_EVIDENCE_BINDING_INVALID",
                        "The frozen authoritative media binding is invalid.",
                        status_code=409,
                    )
                participant_identity = frozen_identity
        livekit_ready = self.media_plane.configuration.media_ready()
        recording_required = participant_role == "candidate" and bool(
            settings.get("record_audio", True) or video_required
        )
        if participant_role == "candidate":
            self.media_plane.require_ready(recording=recording_required)
        else:
            self.media_plane.require_ready(recording=False)
        if (
            participant_role == "candidate"
            and not self.media_plane.configuration.authoritative_audio_ingress_ready()
        ):
            raise ApiError(
                "LIVEKIT_EVIDENCE_INGRESS_NOT_READY",
                "A server-side LiveKit audio subscriber is required for authoritative STT evidence.",
                status_code=503,
            )
        if participant_role == "candidate" and self._production():
            release = realtime_agent_release_status(principal.organization_id)
            if not release["ready"]:
                raise ApiError(
                    "REALTIME_AGENT_RELEASE_NOT_READY",
                    "This organization and release have not passed the production real-time-agent gate.",
                    status_code=503,
                    details={
                        key: release[key]
                        for key in (
                            "organization_enabled",
                            "release_scope_configured",
                            "acceptance_report_ready",
                        )
                    },
                )

        participant_token: Optional[str] = None
        if livekit_ready:
            participant_token = self.media_plane.issue_participant_token(
                room_name=room_name,
                identity=participant_identity,
                participant_role=participant_role,
                publish_sources=publish_sources,
                ttl_seconds=90,
            )
        media = {
            "provider": "livekit",
            "status": "ready" if participant_token else "unavailable",
            "url": self.media_plane.url or None,
            "room_name": room_name,
            "participant_token": participant_token,
            "participant_identity": participant_identity,
            "publish_sources": publish_sources,
            "recording_required": recording_required,
            "video_upstream_allowed": "camera" in publish_sources,
            "evidence_transport": (
                "livekit_server_subscriber"
                if participant_role == "candidate"
                else "observe_only"
            ),
            "recovery": (
                {
                    "server_checkpoint": {
                        "enabled": True,
                        "mode": "durable_media_checkpoint",
                    },
                    "browser_backfill": browser_backfill,
                }
                if participant_role == "candidate"
                else None
            ),
        }
        raw_ticket = "agt.%s" % secrets.token_urlsafe(32)
        with self.persistence.transaction(principal.organization_id) as transaction:
            now = transaction.database_now()
            expires = now + timedelta(seconds=60)
            created_at = now.isoformat().replace("+00:00", "Z")
            ticket = {
                "id": new_id("agent_ticket"),
                "organization_id": principal.organization_id,
                "interview_id": session["id"],
                "connection_id": connection_id,
                "participant_identity": participant_identity,
                "participant_role": participant_role,
                "actor_id": principal.actor_id,
                "roles": sorted(principal.roles),
                "ticket_hash": self._digest(raw_ticket),
                "status": "issued",
                "expires_at": expires.isoformat().replace("+00:00", "Z"),
                "consumed_at": None,
                "media": {
                    key: deepcopy(value)
                    for key, value in media.items()
                    if key != "participant_token"
                },
                "created_at": created_at,
                "updated_at": created_at,
            }
            transaction.agent_tickets.add(ticket)
        return {
            "agent_ticket": raw_ticket,
            "expires_at": ticket["expires_at"],
            "agent_ws_url": "/api/v1/interviews/%s/agent" % session["id"],
            "media": media,
        }

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _production() -> bool:
        import os

        return os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
