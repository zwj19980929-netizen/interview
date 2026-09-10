import asyncio
import base64
import hashlib
import hmac
import json
from copy import deepcopy
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import httpx
from pydantic import ValidationError

from app.adapters.livekit_media import LiveKitConfiguration, LiveKitMediaPlane
from app.core.auth import Principal
from app.core.errors import ApiError
from app.core.interview_agent_metrics import (
    INTERNAL_INTERVIEW_AGENT_METRICS,
    InterviewAgentMetrics,
)
from app.core.time import utc_now
from app.core.prompt.understanding_references import understanding_references
from app.file_storage.local import LocalPrivateFileAdapter
from app.domain.interview_agent import (
    AgentEvent,
    ApprovedConversationAct,
    AvatarPerformance,
    ClientCapabilities,
    ClientSignal,
    ConversationUtterance,
    GestureCue,
    OpenAgentSession,
    Replayability,
    STABLE_AGENT_EVENT_TYPES,
    VisemeCue,
)
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ChatJSONResponse,
    ProviderMeta,
    TTSSynthesizeResponse,
    Usage,
)
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.agent_ticket import InterviewAgentTicketService
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.interview_agent import (
    AgentChannel,
    InterviewAgentRuntime,
    _AGENT_CHANNEL_HUB,
    _AGENT_QUEUE_CAPACITY,
    receive_remote_agent_event,
)
from app.services.media_capture import InterviewMediaCaptureService
from app.services.livekit_room_binding import interview_room_name


def _ready_livekit(
    *, recording: bool = True, authoritative_ingress: bool = True
) -> LiveKitMediaPlane:
    return LiveKitMediaPlane(
        LiveKitConfiguration(
            url="wss://livekit.internal.example",
            api_key="test-key",
            api_secret="secret-at-least-sixteen-characters",
            egress_url="https://livekit.internal.example",
            storage_endpoint="https://oss.internal.example" if recording else "",
            storage_bucket="private-interviews" if recording else "",
            storage_region="test-region",
            storage_access_key="access-key" if recording else "",
            storage_secret="storage-secret" if recording else "",
            storage_sse_algorithm="AES256" if recording else "",
            authoritative_ingress_enabled=authoritative_ingress,
            authoritative_ingress_mode=(
                "database_fenced" if authoritative_ingress else "disabled"
            ),
        )
    )


def _decode_jwt_payload(token: str) -> dict:
    encoded = token.split(".")[1]
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))


def _encode_segment(value: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _signed_webhook_token(
    media: LiveKitMediaPlane,
    body: bytes,
    *,
    claims: dict,
) -> str:
    header = _encode_segment({"alg": "HS256", "typ": "JWT"})
    payload = _encode_segment(
        {
            "iss": media.configuration.api_key,
            "nbf": int(time.time()) - 5,
            "exp": int(time.time()) + 60,
            "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
            **claims,
        }
    )
    signing_input = "%s.%s" % (header, payload)
    signature = base64.urlsafe_b64encode(
        hmac.new(
            media.configuration.api_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii").rstrip("=")
    return "%s.%s" % (signing_input, signature)


def _session(
    store: InMemoryStore,
    *,
    interview_id: str = "interview_agent_contract",
    organization_id: str = "org_default",
    record_audio: bool = True,
    record_video: bool = False,
    scopes=None,
) -> dict:
    item = {
        "id": interview_id,
        "organization_id": organization_id,
        "candidate_id": "candidate_1",
        "candidate": {
            "id": "candidate_1",
            "name": "候选人",
            "media_consent_scopes": list(
                scopes
                if scopes is not None
                else (["audio_recording"] if record_audio else [])
            ),
        },
        "settings": {
            "record_audio": record_audio,
            "record_video": record_video,
        },
        "status": "paused",
        "phase": "position_bank",
        "turns": [],
        "answers": [],
        "agent_runtime": {
            "floor": "none",
            "last_sequence": 0,
            "processed_signal_keys": [],
            "takeover": None,
        },
        "agent_events": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    with persistence_for(store).transaction(organization_id) as transaction:
        return transaction.interview_sessions.add(item)


def _runtime_session(store: InMemoryStore) -> dict:
    now = utc_now()
    item = {
        "id": "interview_warmup_contract",
        "organization_id": "org_default",
        "candidate_id": "candidate_1",
        "candidate": {"id": "candidate_1", "name": "候选人"},
        "settings": {"record_audio": False, "record_video": False},
        "status": "in_progress",
        "phase": "position_bank",
        "current_turn_id": "turn_1",
        "turns": [
            {
                "id": "turn_1",
                "order": 1,
                "status": "asking",
                "phase": "position_bank",
                "is_followup": False,
                "question_spoken_text": "请说明你的断线恢复设计。",
            }
        ],
        "answers": [],
        "evaluation_revisions": [],
        "report_revisions": [],
        "lifecycle_events": [],
        "agent_runtime": {
            "floor": "candidate",
            "floor_reason": "warmup_listening",
            "last_sequence": 0,
            "processed_signal_keys": [],
            "active_performance_id": None,
            "takeover": None,
            "calibration_status": "listening",
            "calibration_updated_at": now,
        },
        "agent_events": [],
        "created_at": now,
        "updated_at": now,
    }
    with persistence_for(store).transaction("org_default") as transaction:
        return transaction.interview_sessions.add(item)


def _recording_capture(store: InMemoryStore, session: dict) -> dict:
    now = utc_now()
    with persistence_for(store).transaction("org_default") as transaction:
        return transaction.interview_media_captures.add(
            {
                "id": "media_capture_contract",
                "organization_id": "org_default",
                "interview_id": session["id"],
                "candidate_id": session["candidate_id"],
                "provider": "livekit",
                "room_name": interview_room_name(
                    session["organization_id"], session["id"]
                ),
                "participant_identity": None,
                "connection_id": None,
                "requested_scopes": ["audio_recording"],
                "consented_scopes": ["audio_recording"],
                "status": "pending",
                "egress_id": None,
                "object_key": None,
                "private_uri": None,
                "content_hash": None,
                "byte_count": None,
                "encryption": None,
                "encryption_verified_at": None,
                "encryption_policy": None,
                "encryption_policy_verified_at": None,
                "storage_protection": None,
                "storage_protection_verified_at": None,
                "storage_protection_policy": None,
                "storage_protection_policy_verified_at": None,
                "retention_expires_at": None,
                "started_at": None,
                "stopped_at": None,
                "failure_code": None,
                "created_at": now,
                "updated_at": now,
            }
        )


class _EphemeralWarmupStream:
    def __init__(self, *args, **kwargs) -> None:
        self.transcript = "这是只用于试音的临时字幕"

    async def open(self, payload: dict) -> list:
        self.transcript = str(payload.get("development_transcript") or self.transcript)
        return [{"type": "stream.ready"}]

    async def send_audio(self, chunk: bytes) -> list:
        return [
            {
                "type": "transcript.partial",
                "text": self.transcript[:6],
                "confidence": 0.9,
            }
        ]

    async def finish(self) -> list:
        return [
            {
                "type": "transcript.final",
                "text": self.transcript,
                "confidence": 0.98,
            }
        ]

    async def abort(self) -> None:
        return None


class _CaptureMediaPlane:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.start_calls = []
        self.stop_calls = []
        self.egresses = []

    async def start_participant_egress(self, **payload) -> dict:
        self.start_calls.append(payload)
        if self.fail_start:
            raise RuntimeError("egress unavailable")
        value = {
            "egress_id": "egress_contract",
            "status": "EGRESS_ACTIVE",
            "room_name": payload["room_name"],
            "participant": {
                "identity": payload["participant_identity"],
                "file_outputs": [{"filepath": payload["object_key"]}],
            },
        }
        self.egresses = [value]
        return value

    async def list_egress(self, **filters) -> dict:
        items = list(self.egresses)
        if filters.get("egress_id"):
            items = [
                item
                for item in items
                if item["egress_id"] == filters["egress_id"]
            ]
        if filters.get("room_name"):
            items = [
                item
                for item in items
                if item["room_name"] == filters["room_name"]
            ]
        return {"items": items}

    async def stop_egress(self, egress_id: str) -> dict:
        self.stop_calls.append(egress_id)
        return {
            "egress_id": egress_id,
            "status": "EGRESS_COMPLETE",
            "file_results": [],
        }


class _CaptureObjectStorage:
    def __init__(self, expected_key: str = None, content: bytes = b"capture") -> None:
        self.expected_key = expected_key
        self.content = content

    def open(self, object_key: str) -> bytes:
        if self.expected_key is not None:
            assert object_key == self.expected_key
        return self.content

    def verify_encryption(self, object_key: str = None) -> str:
        if object_key is not None and self.expected_key is not None:
            assert object_key == self.expected_key
        return "aliyun_oss_aes256"

    def verify_recording_protection(self, object_key: str = None):
        from app.file_storage.interface import RecordingProtection

        encryption = self.verify_encryption(object_key)
        return RecordingProtection(
            descriptor=encryption,
            encryption=encryption,
            development_only=False,
        )


class _UnencryptedCaptureStorage(_CaptureObjectStorage):
    def verify_encryption(self, object_key: str = None) -> str:
        raise RuntimeError("bucket default encryption is disabled")


def _candidate_token(service: InterviewAgentTicketService, session: dict) -> str:
    return service.interviews.candidate_join_url(session).split("token=", 1)[1]


def _utterance(text: str, *, confidence: float = 0.9) -> ConversationUtterance:
    return ConversationUtterance(
        utterance_id="utterance_1",
        revision=1,
        speaker="candidate",
        text=text,
        is_final=True,
        authoritative=True,
        audio_uri="private-file://audio_1",
        stt_confidence=confidence,
        source="server_streaming",
        created_at=utc_now(),
    )


def _turn(*, depth: int = 0, allow_followup: bool = True) -> dict:
    return {
        "id": "turn_root" if depth == 0 else "turn_followup_%s" % depth,
        "root_turn_id": "turn_root",
        "followup_depth": depth,
        "is_followup": depth > 0,
        "allow_followup": allow_followup,
        "question_spoken_text": "请说明你的断线恢复设计。",
        "question_snapshot": {
            "difficulty": "senior",
            "standard_answer": "使用幂等键和音频序号完成断线恢复，并验证最终转写。",
            "key_points": [
                {"text": "幂等键"},
                {"text": "音频序号"},
                {"text": "恢复验证"},
            ],
            "followup_probes": [],
        },
        "current_understanding": None,
    }


def _interview(turn: dict, *, seconds_remaining: int = 600) -> dict:
    end = datetime.now(timezone.utc) + timedelta(seconds=seconds_remaining)
    return {
        "id": "interview_understanding",
        "organization_id": "org_default",
        "turns": [turn],
        "scheduled_end_at": end.isoformat().replace("+00:00", "Z"),
        "followup_policy": {
            "min_answer_chars": 4,
            "max_answer_chars": 4000,
        },
    }


class _Gateway:
    def __init__(self, responses=None, *, error: Exception = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.requests = []
        self.last_response = None

    async def invoke(self, capability, request):
        self.requests.append((capability, request))
        if self.error is not None:
            raise self.error
        if self.responses:
            self.last_response = self.responses.pop(0)
        data = deepcopy(self.last_response)
        if request.purpose == "interview_turn_understanding" and "evidence_quotes" in data:
            # This fixture's domain-style expectations are encoded into the
            # wire protocol; unknown references remain invalid, never repaired.
            references = understanding_references(request.metadata["transcript"], request.metadata["capability_points"])
            evidence_ids = {text: key for key, text in references["evidence"].items()}
            point_ids = {text: key for key, text in references["capabilities"].items()}
            data["evidence_ids"] = [evidence_ids.get(text, text) for text in data.pop("evidence_quotes")]
            for claim in data["claims"]:
                text = claim.pop("evidence_quote")
                claim["evidence_id"] = evidence_ids.get(text, text)
            for state in ("covered", "missing"):
                data[state + "_point_ids"] = [point_ids.get(text, text) for text in data.pop(state + "_capability_points")]
        return ChatJSONResponse(
            data=data,
            usage=Usage(),
            provider=ProviderMeta(
                provider_id="test",
                model="test-model",
                request_id="request_1",
                latency_ms=1,
            ),
        )


def _understanding_response(transcript: str) -> dict:
    return {"clarification_target": None,
        "intent": "answer",
        "answer_summary": transcript,
        "claims": [{"claim": "设计了恢复", "evidence_quote": transcript}],
        "evidence_quotes": [transcript],
        "covered_capability_points": ["音频序号"],
        "missing_capability_points": ["幂等键", "恢复验证"],
        "ambiguities": [],
        "contradictions": [],
        "confidence": 0.9,
        "suggested_action": "followup",
    }


def _followup_response(transcript: str, **overrides) -> dict:
    value = {
        "selected": True,
        "question_text": "你会如何验证恢复后没有生成重复答案？",
        "evidence_quote": transcript,
        "target_capability_points": ["幂等键"],
        "rationale": "补充缺失证据",
        "difficulty": "senior",
        "sensitive_attribute_inference": False,
        "leaks_answer": False,
    }
    value.update(overrides)
    return value


def test_agent_contracts_reject_invalid_binary_and_non_authoritative_evidence() -> None:
    with pytest.raises(ValidationError):
        ClientSignal(
            type="speech.started",
            idempotency_key="signal_1",
            audio=b"not-allowed",
        )
    with pytest.raises(ValidationError):
        ClientSignal(type="evidence.audio.chunk", idempotency_key="signal_2")
    with pytest.raises(ValidationError):
        ClientSignal(
            type="evidence.audio.chunk",
            idempotency_key="signal_oversized",
            audio=b"x" * (256 * 1024 + 1),
        )
    with pytest.raises(ValidationError):
        ConversationUtterance(
            utterance_id="utterance_invalid",
            revision=1,
            speaker="candidate",
            text="没有录音证据",
            is_final=True,
            authoritative=True,
            audio_uri=None,
            stt_confidence=0.9,
            source="server_streaming",
            created_at=utc_now(),
        )


def test_candidate_telemetry_is_ephemeral_non_idempotent_and_fair_on_sqlite(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(str(tmp_path / "telemetry.sqlite3"))
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)
    metrics = InterviewAgentMetrics()
    monkeypatch.setattr(
        "app.services.interview_agent.interview_agent_metrics", lambda: metrics
    )

    def opened(*, roles: frozenset[str], actor_id: str) -> OpenAgentSession:
        return OpenAgentSession(
            interview_id=session["id"],
            principal=Principal(
                actor_id=actor_id,
                organization_id="org_default",
                roles=roles,
                authenticated=True,
            ),
            connection_id="connection_telemetry_%s" % actor_id,
            capabilities=ClientCapabilities(
                webrtc=True,
                audio_worklet=True,
                webgl=True,
                camera=True,
                microphone=True,
                speaker=True,
                avatar_fps=60,
                media_recorder=True,
                browser="Chrome test",
            ),
        )

    def domain_fingerprint() -> dict:
        current = runtime.interviews.get_interview(session["id"], "org_default")
        state = current.get("agent_runtime") or {}
        return {
            "version": current["version"],
            "updated_at": current["updated_at"],
            "processed_signal_keys": list(state.get("processed_signal_keys", [])),
            "last_sequence": state.get("last_sequence"),
            "events": list(current.get("agent_events", [])),
            "problems": list(state.get("problems", [])),
        }

    async def scenario() -> None:
        channel = AgentChannel(
            runtime,
            opened(
                roles=frozenset({"candidate"}),
                actor_id="candidate:candidate_1",
            ),
        )
        shared_sample = ClientSignal(
            type="telemetry.observe",
            idempotency_key="same_process_sample_key",
            payload={"metric": "avatar_viseme_drift_ms", "value": 12.5},
        )
        before = domain_fingerprint()

        marker_ran = False

        async def mark_scheduler_progress() -> None:
            nonlocal marker_ran
            marker_ran = True

        await channel._lock.acquire()
        marker = asyncio.create_task(mark_scheduler_progress())
        try:
            # Process telemetry must not queue behind a domain signal, while
            # its explicit yield lets ownership/media tasks make progress.
            await asyncio.wait_for(channel.send(shared_sample), timeout=1)
            assert marker_ran is True
        finally:
            channel._lock.release()
        await marker

        for _ in range(511):
            await channel.send(shared_sample)
        assert metrics.snapshot()["avatar_viseme_drift_ms"]["count"] == 512

        invalid_samples = [
            {},
            {"metric": "not_registered", "value": 1},
            {"metric": "evidence_owner_renew_success", "value": 1},
            {"metric": "avatar_viseme_drift_ms"},
            {"metric": "avatar_viseme_drift_ms", "value": True},
            {"metric": "avatar_viseme_drift_ms", "value": -1},
            {"metric": "avatar_viseme_drift_ms", "value": 300_001},
            {"metric": "avatar_viseme_drift_ms", "value": float("inf")},
            {"metric": "avatar_viseme_drift_ms", "value": "not-a-number"},
        ]
        for index, payload in enumerate(invalid_samples):
            await channel.send(
                ClientSignal(
                    type="telemetry.observe",
                    idempotency_key="invalid_process_sample_%s" % index,
                    payload=payload,
                )
            )
        assert metrics.snapshot()["avatar_viseme_drift_ms"]["count"] == 512
        assert all(
            metrics.snapshot()[name]["count"] == 0
            for name in INTERNAL_INTERVIEW_AGENT_METRICS
        )

        reviewer = AgentChannel(
            runtime,
            opened(roles=frozenset({"interviewer"}), actor_id="interviewer_1"),
        )
        with pytest.raises(ApiError) as forbidden:
            await reviewer.send(shared_sample)
        assert forbidden.value.code == "CANDIDATE_SIGNAL_FORBIDDEN"

        # Even hundreds of duplicate samples, invalid input, and a forbidden
        # role leave the SQLite document byte-for-byte stable at domain level.
        assert domain_fingerprint() == before

    asyncio.run(scenario())


def test_non_telemetry_signal_idempotency_is_unchanged() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = AgentChannel(
            runtime,
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_ping_idempotency",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            ),
        )
        signal = ClientSignal(type="ping", idempotency_key="ping_once")
        await channel.send(signal)
        after_first = runtime.interviews.get_interview(session["id"])
        await channel.send(signal)
        after_duplicate = runtime.interviews.get_interview(session["id"])

        assert after_duplicate == after_first
        assert (
            after_duplicate["agent_runtime"]["processed_signal_keys"].count(
                "ping_once"
            )
            == 1
        )

    asyncio.run(scenario())


def test_avatar_performance_requires_ordered_visemes_and_stable_event_types() -> None:
    with pytest.raises(ValidationError):
        AvatarPerformance(
            performance_id="performance_1",
            audio_uri="private-file://speech_1",
            audio_clock_origin_ms=0,
            text="测试",
            visemes=[
                VisemeCue(at_ms=100, duration_ms=80, shape="aa", weight=1),
                VisemeCue(at_ms=20, duration_ms=80, shape="sil", weight=1),
            ],
            gestures=[GestureCue(at_ms=0, duration_ms=200, gesture="nod")],
            alignment_source="provider_timestamp",
        )
    assert "session.snapshot" in STABLE_AGENT_EVENT_TYPES
    assert "takeover.changed" in STABLE_AGENT_EVENT_TYPES
    event = AgentEvent(
        event_id="event_1",
        session_sequence=1,
        type="problem",
        occurred_at=utc_now(),
        replayability=Replayability.REPLAYABLE,
        payload={"code": "TEST"},
    )
    assert event.model_dump(mode="json")["replayability"] == "replayable"


def test_livekit_observer_token_is_subscribe_only() -> None:
    media = _ready_livekit()
    observer = media.issue_participant_token(
        room_name="interview-1",
        identity="observer:1",
        participant_role="observer",
        publish_sources=(),
    )
    grant = _decode_jwt_payload(observer)["video"]
    assert grant["canPublish"] is False
    assert grant["canPublishData"] is False
    assert grant["canPublishSources"] == []
    with pytest.raises(ValueError):
        media.issue_participant_token(
            room_name="interview-1",
            identity="bad:1",
            participant_role="candidate",
            publish_sources=("screen_share",),
        )


def test_livekit_webhook_malformed_signature_and_claim_types_are_controlled() -> None:
    media = _ready_livekit()
    body = b'{"event":"egress_ended"}'

    with pytest.raises(ApiError) as malformed_signature:
        media.verify_webhook(body, "Bearer header.payload.\u7b7e\u540d")
    assert malformed_signature.value.status_code == 401
    assert malformed_signature.value.code == "LIVEKIT_WEBHOOK_UNAUTHORIZED"

    token = _signed_webhook_token(media, body, claims={"exp": {"invalid": True}})
    with pytest.raises(ApiError) as invalid_claims:
        media.verify_webhook(body, "Bearer %s" % token)
    assert invalid_claims.value.status_code == 401
    assert invalid_claims.value.code == "LIVEKIT_WEBHOOK_UNAUTHORIZED"


def test_livekit_room_admin_removes_revoked_takeover_participant() -> None:
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sid": "participant_removed"})

    base = _ready_livekit()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    media = LiveKitMediaPlane(base.configuration, client=client)

    async def scenario() -> None:
        result = await media.remove_participant(
            room_name="interview-1",
            participant_identity="takeover:lease_1",
        )
        assert result["sid"] == "participant_removed"
        request = requests[-1]
        assert request.url.path == "/twirp/livekit.RoomService/RemoveParticipant"
        assert json.loads(request.content) == {
            "room": "interview-1",
            "identity": "takeover:lease_1",
        }
        token = request.headers["Authorization"].split(" ", 1)[1]
        grant = _decode_jwt_payload(token)["video"]
        assert grant == {"roomAdmin": True, "room": "interview-1"}
        await client.aclose()

    asyncio.run(scenario())


def test_candidate_agent_ticket_is_one_time_and_stores_no_bearer_secret() -> None:
    store = InMemoryStore()
    session = _session(store)
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = service.issue_candidate(session["id"], _candidate_token(service, session))
    assert issued["media"]["publish_sources"] == ["microphone"]
    assert issued["media"]["video_upstream_allowed"] is False
    assert issued["media"]["participant_token"]

    stored = next(iter(store.agent_tickets.values()))
    assert issued["agent_ticket"] not in json.dumps(stored)
    assert "participant_token" not in stored["media"]

    consumed = service.consume(session["id"], issued["agent_ticket"])
    assert consumed.principal.roles == frozenset({"candidate"})
    assert consumed.media["video_upstream_allowed"] is False
    with pytest.raises(ApiError) as reused:
        service.consume(session["id"], issued["agent_ticket"])
    assert reused.value.code == "AGENT_TICKET_ALREADY_USED"


def test_candidate_reconnect_reuses_frozen_authoritative_media_identity() -> None:
    store = InMemoryStore()
    session = _session(store)
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    first = service.issue_candidate(session["id"], _candidate_token(service, session))
    first_identity = first["media"]["participant_identity"]
    first_connection_id = first["media"]["recovery"]["browser_backfill"][
        "connection_id"
    ]

    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["agent_runtime"]["authoritative_media_binding"] = {
            "room_name": interview_room_name("org_default", session["id"]),
            "candidate_identity": first_identity,
            "connection_id": first_connection_id,
            "frozen_at": utc_now(),
        }
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )

    reconnected = service.issue_candidate(
        session["id"], _candidate_token(service, session)
    )
    reconnected_connection_id = reconnected["media"]["recovery"][
        "browser_backfill"
    ]["connection_id"]
    assert reconnected_connection_id != first_connection_id
    assert reconnected["media"]["participant_identity"] == first_identity
    claims = _decode_jwt_payload(reconnected["media"]["participant_token"])
    assert claims["sub"] == first_identity
    assert claims["video"]["roomJoin"] is True


def test_expired_agent_ticket_status_is_committed() -> None:
    store = InMemoryStore()
    session = _session(store)
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = service.issue_candidate(session["id"], _candidate_token(service, session))
    persistence = persistence_for(store)
    with persistence.transaction("org_default") as transaction:
        ticket = transaction.agent_tickets.list()[0]
        ticket["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        transaction.agent_tickets.update(ticket, expected_version=ticket["version"])

    with pytest.raises(ApiError) as expired:
        service.consume(session["id"], issued["agent_ticket"])
    assert expired.value.code == "AGENT_TICKET_EXPIRED"
    assert next(iter(store.agent_tickets.values()))["status"] == "expired"


def test_agent_ticket_expiry_uses_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.persistence.memory import _MemoryTransactionBackend

    database_time = [datetime(2035, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(
        _MemoryTransactionBackend,
        "database_now",
        lambda self: database_time[0],
    )
    store = InMemoryStore()
    session = _session(store)
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())

    issued = service.issue_candidate(
        session["id"], _candidate_token(service, session)
    )
    stored = next(iter(store.agent_tickets.values()))
    assert datetime.fromisoformat(stored["expires_at"].replace("Z", "+00:00")) == (
        database_time[0] + timedelta(seconds=60)
    )

    database_time[0] += timedelta(seconds=61)
    with pytest.raises(ApiError) as expired:
        service.consume(session["id"], issued["agent_ticket"])
    assert expired.value.code == "AGENT_TICKET_EXPIRED"


def test_video_ticket_requires_explicit_video_consent() -> None:
    store = InMemoryStore()
    session = _session(
        store,
        record_video=True,
        scopes=["audio_recording"],
    )
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    with pytest.raises(ApiError) as denied:
        service.issue_candidate(session["id"], _candidate_token(service, session))
    assert denied.value.code == "VIDEO_RECORDING_CONSENT_REQUIRED"


def test_candidate_ticket_grant_contains_only_consented_media_sources() -> None:
    store = InMemoryStore()
    session = _session(
        store,
        record_video=True,
        scopes=["audio_recording", "video_recording"],
    )
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = service.issue_candidate(session["id"], _candidate_token(service, session))
    assert issued["media"]["publish_sources"] == ["microphone", "camera"]
    assert issued["media"]["video_upstream_allowed"] is True
    grant = _decode_jwt_payload(issued["media"]["participant_token"])["video"]
    assert grant["canPublishSources"] == ["microphone", "camera"]
    assert "screen_share" not in grant["canPublishSources"]


def test_production_audio_recording_ticket_requires_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv(
        "INTERVIEWER_CANDIDATE_TOKEN_SECRET",
        "production-candidate-token-secret-with-32-chars",
    )
    store = InMemoryStore()
    session = _session(store, record_audio=True, record_video=False)
    service = InterviewAgentTicketService(
        store,
        media_plane=_ready_livekit(recording=False),
    )
    with pytest.raises(ApiError) as unavailable:
        service.issue_candidate(session["id"], _candidate_token(service, session))
    assert unavailable.value.code == "LIVEKIT_RECORDING_NOT_READY"


def test_production_ticket_fails_closed_without_livekit_authoritative_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv(
        "INTERVIEWER_CANDIDATE_TOKEN_SECRET",
        "production-candidate-token-secret-with-32-chars",
    )
    store = InMemoryStore()
    session = _session(store, record_audio=True, record_video=False)
    service = InterviewAgentTicketService(
        store, media_plane=_ready_livekit(authoritative_ingress=False)
    )
    with pytest.raises(ApiError) as unavailable:
        service.issue_candidate(session["id"], _candidate_token(service, session))
    assert unavailable.value.code == "LIVEKIT_EVIDENCE_INGRESS_NOT_READY"


def test_production_ticket_selects_server_livekit_evidence_only_after_full_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv(
        "INTERVIEWER_CANDIDATE_TOKEN_SECRET",
        "production-candidate-token-secret-with-32-chars",
    )
    monkeypatch.setattr(
        "app.services.agent_ticket.realtime_agent_release_status",
        lambda organization_id: {
            "ready": True,
            "organization_enabled": True,
            "release_scope_configured": True,
            "acceptance_report_ready": True,
        },
    )
    store = InMemoryStore()
    session = _session(store, record_audio=True, record_video=False)
    service = InterviewAgentTicketService(
        store,
        media_plane=_ready_livekit(authoritative_ingress=True),
    )
    issued = service.issue_candidate(
        session["id"], _candidate_token(service, session)
    )
    assert issued["media"]["evidence_transport"] == "livekit_server_subscriber"
    assert issued["media"]["recovery"]["server_checkpoint"] == {
        "enabled": True,
        "mode": "durable_media_checkpoint",
    }
    browser_backfill = issued["media"]["recovery"]["browser_backfill"]
    assert browser_backfill["protocol"] == "agent-json-backfill.v1"
    assert browser_backfill["retention_ms"] == 30_000
    assert browser_backfill["max_bytes"] == 2 * 1024 * 1024
    assert browser_backfill["connection_id"]
    assert browser_backfill["audio_epoch"]
    consumed = service.consume(session["id"], issued["agent_ticket"])
    assert consumed.media["evidence_transport"] == "livekit_server_subscriber"


def test_production_candidate_ticket_rechecks_release_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv(
        "INTERVIEWER_CANDIDATE_TOKEN_SECRET",
        "production-candidate-token-secret-with-32-chars",
    )
    release = {
        "ready": True,
        "organization_enabled": True,
        "release_scope_configured": True,
        "acceptance_report_ready": True,
    }
    monkeypatch.setattr(
        "app.services.agent_ticket.realtime_agent_release_status",
        lambda organization_id: dict(release),
    )
    store = InMemoryStore()
    session = _session(store, record_audio=True, record_video=False)
    service = InterviewAgentTicketService(
        store,
        media_plane=_ready_livekit(authoritative_ingress=True),
    )

    issued = service.issue_candidate(
        session["id"], _candidate_token(service, session)
    )
    release.update(ready=False, acceptance_report_ready=False)
    with pytest.raises(ApiError) as unavailable:
        service.consume(session["id"], issued["agent_ticket"])

    assert unavailable.value.code == "REALTIME_AGENT_RELEASE_NOT_READY"
    stored = next(iter(store.agent_tickets.values()))
    assert stored["status"] == "revoked"
    assert stored["revoked_reason"] == "release_gate_not_ready"


def test_production_agent_rejects_legacy_no_audio_consent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv(
        "INTERVIEWER_CANDIDATE_TOKEN_SECRET",
        "production-candidate-token-secret-with-32-chars",
    )
    store = InMemoryStore()
    session = _session(
        store,
        record_audio=False,
        record_video=False,
        scopes=[],
    )
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    with pytest.raises(ApiError) as denied:
        service.issue_candidate(session["id"], _candidate_token(service, session))
    assert denied.value.code == "AUDIO_EVIDENCE_CONSENT_REQUIRED"


def test_reviewer_agent_ticket_has_subscribe_only_media_projection() -> None:
    store = InMemoryStore()
    session = _session(store)
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = service.issue_enterprise(
        session["id"],
        Principal(
            actor_id="reviewer_1",
            organization_id="org_default",
            roles=frozenset({"reviewer"}),
            authenticated=True,
        ),
    )
    assert issued["media"]["publish_sources"] == []
    grant = _decode_jwt_payload(issued["media"]["participant_token"])["video"]
    assert grant["canPublish"] is False
    assert grant["canSubscribe"] is True


def test_interviewer_base_ticket_is_subscribe_only_and_takeover_permit_is_mic_only() -> None:
    store = InMemoryStore()
    session = _session(store)
    media = _ready_livekit()
    service = InterviewAgentTicketService(store, media_plane=media)
    interviewer = Principal(
        actor_id="interviewer_1",
        organization_id="org_default",
        roles=frozenset({"interviewer"}),
        authenticated=True,
    )
    base = service.issue_enterprise(session["id"], interviewer)
    base_grant = _decode_jwt_payload(base["media"]["participant_token"])["video"]
    assert base["media"]["publish_sources"] == []
    assert base_grant["canPublish"] is False
    assert base_grant["canPublishData"] is False

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat().replace(
        "+00:00", "Z"
    )
    with persistence_for(store).transaction("org_default") as transaction:
        value = transaction.interview_sessions.get(session["id"])
        value["agent_runtime"]["takeover"] = {
            "lease_id": "takeover_permit_1",
            "actor_id": "interviewer_1",
            "reason": "人工澄清",
            "expires_at": expires_at,
            "version": 3,
        }
        transaction.interview_sessions.update(value, expected_version=value["version"])

    permit = service.issue_takeover_media_permit(
        session["id"],
        interviewer,
        lease_id="takeover_permit_1",
        expected_version=3,
    )
    grant = _decode_jwt_payload(permit["media"]["participant_token"])
    assert grant["video"] == {
        "roomJoin": True,
        "room": interview_room_name("org_default", session["id"]),
        "canPublish": True,
        "canSubscribe": False,
        "canPublishData": False,
        "canPublishSources": ["microphone"],
    }
    assert grant["sub"] == "takeover:takeover_permit_1"
    assert 15 <= grant["exp"] - int(time.time()) <= 30
    persisted = service.interviews.get_interview(session["id"])
    lease = persisted["agent_runtime"]["takeover"]
    assert lease["media_permit_generation"] == 1
    assert lease["media_permit_lease_version"] == 3
    assert lease["media_participant_identity"] == grant["sub"]
    with persistence_for(store).transaction("org_default") as transaction:
        audits = [
            item
            for item in transaction.audit_events.list()
            if item.get("action") == "interview.takeover.media_permit.issued"
        ]
    assert audits[-1]["actor_id"] == "interviewer_1"
    assert "participant_token" not in json.dumps(persisted)
    assert "participant_token" not in json.dumps(audits)
    with pytest.raises(ApiError) as repeated:
        service.issue_takeover_media_permit(
            session["id"],
            interviewer,
            lease_id="takeover_permit_1",
            expected_version=3,
        )
    assert repeated.value.code == "TAKEOVER_MEDIA_PERMIT_ALREADY_ISSUED"


def test_takeover_media_permit_requires_same_actor_current_version_and_active_lease() -> None:
    store = InMemoryStore()
    session = _session(store)
    service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    owner = Principal(
        actor_id="interviewer_owner",
        organization_id="org_default",
        roles=frozenset({"interviewer"}),
        authenticated=True,
    )
    other = Principal(
        actor_id="interviewer_other",
        organization_id="org_default",
        roles=frozenset({"interviewer"}),
        authenticated=True,
    )
    with persistence_for(store).transaction("org_default") as transaction:
        value = transaction.interview_sessions.get(session["id"])
        value["agent_runtime"]["takeover"] = {
            "lease_id": "takeover_security",
            "actor_id": owner.actor_id,
            "reason": "人工接管",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=60)
            ).isoformat().replace("+00:00", "Z"),
            "version": 2,
        }
        transaction.interview_sessions.update(value, expected_version=value["version"])

    for principal, version in ((other, 2), (owner, 1)):
        with pytest.raises(ApiError) as denied:
            service.issue_takeover_media_permit(
                session["id"],
                principal,
                lease_id="takeover_security",
                expected_version=version,
            )
        assert denied.value.code == "TAKEOVER_LEASE_LOST"

    with persistence_for(store).transaction("org_default") as transaction:
        value = transaction.interview_sessions.get(session["id"])
        value["agent_runtime"]["takeover"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        transaction.interview_sessions.update(value, expected_version=value["version"])
    with pytest.raises(ApiError) as expired:
        service.issue_takeover_media_permit(
            session["id"],
            owner,
            lease_id="takeover_security",
            expected_version=2,
        )
    assert expired.value.code == "TAKEOVER_LEASE_LOST"


def test_takeover_acquire_uses_one_authoritative_transaction_across_runtimes() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    first = InterviewAgentRuntime(store)
    second = InterviewAgentRuntime(store)
    lease, replaced = first._acquire_takeover_lease(
        session["id"],
        actor_id="interviewer_first",
        reason="首次接管",
        organization_id="org_default",
    )
    assert replaced is None
    with pytest.raises(ApiError) as held:
        second._acquire_takeover_lease(
            session["id"],
            actor_id="interviewer_second",
            reason="并发接管",
            organization_id="org_default",
        )
    assert held.value.code == "TAKEOVER_LEASE_HELD"
    current = first.interviews.get_interview(session["id"])
    assert current["agent_runtime"]["takeover"]["lease_id"] == lease.lease_id
    with persistence_for(store).transaction("org_default") as transaction:
        acquired = [
            item
            for item in transaction.audit_events.list()
            if item.get("action") == "interview.takeover.changed"
            and item.get("metadata", {}).get("transition") == "acquired"
        ]
    assert len(acquired) == 1
    assert acquired[0]["actor_id"] == "interviewer_first"


def test_takeover_lease_expiry_uses_database_clock_not_process_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.persistence.memory import _MemoryTransactionBackend

    store = InMemoryStore()
    session = _runtime_session(store)
    database_time = [datetime(2035, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(
        _MemoryTransactionBackend,
        "database_now",
        lambda self: database_time[0],
    )
    runtime = InterviewAgentRuntime(store)

    lease, _ = runtime._acquire_takeover_lease(
        session["id"],
        actor_id="interviewer_db_clock",
        reason="验证数据库时钟",
        organization_id="org_default",
    )
    assert datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00")) == (
        database_time[0] + timedelta(seconds=60)
    )

    database_time[0] += timedelta(seconds=61)
    expired = runtime._expire_takeover_if_due(session["id"], "org_default")
    assert expired["lease_id"] == lease.lease_id
    assert runtime.interviews.get_interview(session["id"])["agent_runtime"][
        "takeover"
    ] is None


def test_warmup_is_ephemeral_and_question_is_selected_only_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["settings"] = {"record_audio": True, "record_video": False}
        current["candidate"]["media_consent_scopes"] = ["audio_recording"]
        current["updated_at"] = utc_now()
        session = transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
        transaction.agent_tickets.add(
            {
                "id": "ticket_warmup_recording_boundary",
                "organization_id": "org_default",
                "interview_id": session["id"],
                "connection_id": "connection_warmup",
                    "participant_identity": "candidate:connection_warmup",
                    "participant_role": "candidate",
                    "media": {
                        "room_name": interview_room_name(
                            "org_default", session["id"]
                        )
                    },
                    "status": "consumed",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
    _recording_capture(store, session)
    monkeypatch.setattr(
        "app.services.interview_agent.WarmupCalibrationStream",
        _EphemeralWarmupStream,
    )
    runtime = InterviewAgentRuntime(store)
    capture_plane = _CaptureMediaPlane()
    runtime.media_captures.media_plane = capture_plane
    runtime.media_captures.storage = _CaptureObjectStorage()

    async def expression_audio(*args, **kwargs) -> dict:
        return {
            "audio_uri": "private-file://approved-expression-audio",
            "duration_ms": 1200,
            "visemes": [],
        }

    monkeypatch.setattr(runtime, "_expression_audio", expression_audio)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_warmup",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        await channel.send(
            ClientSignal(type="media.published", idempotency_key="media_ready")
        )
        assert capture_plane.start_calls == []
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="warmup_open",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "development_transcript": "这是只用于试音的临时字幕",
                },
            )
        )
        await channel.send(
            ClientSignal(
                type="evidence.audio.chunk",
                idempotency_key="warmup_audio_1",
                audio=b"ephemeral-pcm",
            )
        )
        await channel.send(
            ClientSignal(type="evidence.finish", idempotency_key="warmup_finish")
        )

        before_confirmation = runtime.interviews.get_interview(session["id"])
        assert before_confirmation["answers"] == []
        assert before_confirmation["turns"][0]["status"] == "asking"
        assert before_confirmation["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
        acts = [
            item["payload"]["act_type"]
            for item in before_confirmation["agent_events"]
            if item["type"] == "conversation.act.selected"
        ]
        assert acts == ["warmup_confirmation"]
        assert "这是只用于试音的临时字幕" not in json.dumps(
            before_confirmation, ensure_ascii=False
        )
        assert capture_plane.start_calls == []
        assert runtime.media_captures.get_for_interview(session["id"])["status"] == "pending"

        await channel.send(
            ClientSignal(type="warmup.confirm", idempotency_key="warmup_confirm")
        )
        confirmed = runtime.interviews.get_interview(session["id"])
        assert confirmed["answers"] == []
        assert confirmed["agent_runtime"]["calibration_status"] == "completed"
        assert confirmed["agent_runtime"]["calibration_retry_required"] is False
        assert confirmed["agent_runtime"]["calibration_media_deleted_at"]
        assert len(capture_plane.start_calls) == 1
        assert runtime.media_captures.get_for_interview(session["id"])["status"] == "recording"
        question_acts = [
            item
            for item in confirmed["agent_events"]
            if item["type"] == "conversation.act.selected"
            and item["payload"]["act_type"] == "question"
        ]
        assert len(question_acts) == 1
        assert question_acts[0]["turn_id"] == "turn_1"
        persisted_question_acts = confirmed["turns"][0]["conversation_acts"]
        assert len(persisted_question_acts) == 1
        assert persisted_question_acts[0]["act_id"] == question_acts[0]["payload"]["act_id"]
        assert persisted_question_acts[0]["act_type"] == "question"
        assert persisted_question_acts[0]["text"] == "请说明你的断线恢复设计。"
        assert persisted_question_acts[0]["evaluative"] is False
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_expression_reuses_frozen_controlled_followup_act_without_duplication() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    frozen = {
        "act_id": "conversation_act_frozen",
        "act_type": "followup",
        "text": "你会如何验证恢复后不会生成重复答案？",
        "turn_id": None,
        "root_turn_id": "turn_1",
        "evidence_quotes": ["我会补传断线期间的音频帧"],
        "target_capability_points": ["恢复验证"],
        "followup_depth": 1,
        "evaluative": False,
        "approved_by": "controlled_followup.v1",
        "prompt_version": "controlled_followup.v1",
        "created_at": utc_now(),
    }
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        turn = current["turns"][0]
        turn["is_followup"] = True
        turn["followup_depth"] = 1
        turn["root_turn_id"] = "turn_1"
        turn["target_key_points"] = ["恢复验证"]
        turn["conversation_acts"] = [frozen]
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )

    runtime = InterviewAgentRuntime(store)
    approved = runtime._approve_conversation_act(
        session["id"],
        act_type="followup",
        text=frozen["text"],
        turn_id="turn_1",
        evidence_quotes=frozen["evidence_quotes"],
        approved_by="controlled_followup.v1",
        organization_id="org_default",
    )
    stored = runtime.interviews.get_interview(session["id"])["turns"][0]
    assert approved.act_id == frozen["act_id"]
    assert approved.turn_id == "turn_1"
    assert stored["conversation_acts"] == [approved.model_dump(mode="json")]


def test_expression_rejects_followup_that_was_not_frozen_by_decision_gate() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)

    with pytest.raises(ApiError) as error:
        runtime._approve_conversation_act(
            session["id"],
            act_type="followup",
            text="请说明如何避免重复答案？",
            turn_id="turn_1",
            evidence_quotes=["我会记录序号"],
            approved_by="controlled_followup.v1",
            organization_id="org_default",
        )

    assert error.value.code == "FOLLOWUP_ACT_NOT_FROZEN"
    assert runtime.interviews.get_interview(session["id"])["turns"][0].get(
        "conversation_acts", []
    ) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_quotes": []},
        {"target_capability_points": []},
        {"followup_depth": 0},
        {"root_turn_id": None},
        {"evaluative": True},
        {"text": "你的回答得很好，我再确认一个细节。"},
    ],
)
def test_approved_followup_contract_rejects_unbound_or_evaluative_act(
    overrides: dict,
) -> None:
    value = {
        "act_id": "conversation_act_1",
        "act_type": "followup",
        "text": "你提到了幂等键，请说明如何验证它。",
        "turn_id": "turn_followup_1",
        "root_turn_id": "turn_1",
        "evidence_quotes": ["我会使用幂等键"],
        "target_capability_points": ["幂等性"],
        "followup_depth": 1,
        "evaluative": False,
        "approved_by": "controlled_followup_gate",
        "prompt_version": "controlled_followup.v1",
        "created_at": utc_now(),
    }
    value.update(overrides)

    with pytest.raises(ValidationError):
        ApprovedConversationAct.model_validate(value)


def test_candidate_barge_in_revokes_agent_floor_and_active_performance() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["agent_runtime"].update(
            {
                "calibration_status": "completed",
                "floor": "agent",
                "active_performance_id": "performance_active",
            }
        )
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_barge_in",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        await channel.send(
            ClientSignal(
                type="speech.started",
                idempotency_key="barge_in_once",
                turn_id="turn_1",
            )
        )
        value = runtime.interviews.get_interview(session["id"])
        assert value["agent_runtime"]["floor"] == "candidate"
        assert value["agent_runtime"]["active_performance_id"] is None
        interruptions = [
            event
            for event in list(channel._queue._queue)
            if getattr(event, "type", None) == "avatar.performance.interrupted"
        ]
        assert interruptions[-1].payload == {
            "reason": "barge_in",
            "deadline_ms": 200,
            "performance_id": "performance_active",
        }
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_stale_performance_stop_cannot_end_replacement_performance() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["agent_runtime"].update(
            {
                "calibration_status": "completed",
                "floor": "agent",
                "floor_reason": "question_playback",
                "active_performance_id": "performance_current",
            }
        )
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_stale_performance_stop",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        before = runtime.interviews.get_interview(session["id"])
        stopped_count = len(
            [
                event
                for event in before["agent_events"]
                if event["type"] == "avatar.performance.stopped"
            ]
        )

        await channel.send(
            ClientSignal(
                type="avatar.performance.stopped",
                idempotency_key="stale_performance_stopped",
                payload={"performance_id": "performance_replaced"},
            )
        )
        stale = runtime.interviews.get_interview(session["id"])
        assert stale["agent_runtime"]["active_performance_id"] == "performance_current"
        assert stale["agent_runtime"]["floor"] == "agent"
        assert stale["agent_runtime"]["floor_reason"] == "question_playback"
        assert len(
            [
                event
                for event in stale["agent_events"]
                if event["type"] == "avatar.performance.stopped"
            ]
        ) == stopped_count

        await channel.send(
            ClientSignal(
                type="avatar.performance.stopped",
                idempotency_key="current_performance_stopped",
                payload={"performance_id": "performance_current"},
            )
        )
        matched = runtime.interviews.get_interview(session["id"])
        assert matched["agent_runtime"]["active_performance_id"] is None
        assert matched["agent_runtime"]["floor"] == "candidate"
        assert matched["agent_runtime"]["floor_reason"] == "agent_finished"
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_completed_interview_plays_farewell_before_single_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["status"] = "completed"
        current["completed_at"] = utc_now()
        current["agent_runtime"]["calibration_status"] = "completed"
        current["updated_at"] = utc_now()
        session = transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    runtime = InterviewAgentRuntime(store)

    async def expression_audio(*args, **kwargs) -> dict:
        return {
            "audio_uri": "private-file://farewell-audio",
            "duration_ms": 600,
            "visemes": [],
        }

    monkeypatch.setattr(runtime, "_expression_audio", expression_audio)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_farewell",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        await channel._after_formal_evidence_finished(
            "turn_1",
            ClientSignal(
                type="evidence.finish",
                idempotency_key="formal_finish_farewell",
            ),
            conversation_action_selected=False,
            stop_evidence_session=False,
        )
        pending = runtime.interviews.get_interview(session["id"])
        closing_id = pending["agent_runtime"]["completion_closing_performance_id"]
        assert closing_id
        assert not pending["agent_runtime"].get("completion_emitted_at")
        assert [
            item for item in pending["agent_events"] if item["type"] == "completed"
        ] == []
        closing_acts = [
            item
            for item in pending["agent_events"]
            if item["type"] == "conversation.act.selected"
            and item["payload"]["act_type"] == "closing"
        ]
        assert len(closing_acts) == 1
        assert "企业人员" in closing_acts[0]["payload"]["text"]

        await channel.send(
            ClientSignal(
                type="avatar.performance.stopped",
                idempotency_key="farewell_stopped_once",
                payload={"performance_id": closing_id},
            )
        )
        completed = runtime.interviews.get_interview(session["id"])
        assert completed["agent_runtime"]["completion_emitted_at"]
        assert completed["agent_runtime"]["floor"] == "none"
        assert len(
            [item for item in completed["agent_events"] if item["type"] == "completed"]
        ) == 1

        await channel.send(
            ClientSignal(
                type="avatar.performance.stopped",
                idempotency_key="farewell_stopped_duplicate",
                payload={"performance_id": closing_id},
            )
        )
        deduped = runtime.interviews.get_interview(session["id"])
        assert deduped["agent_runtime"]["floor"] == "none"
        assert len(
            [item for item in deduped["agent_events"] if item["type"] == "completed"]
        ) == 1
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_last_answer_closes_candidate_experience_before_scoring_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    submitted_at = utc_now()
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["current_turn_id"] = None
        current["turns"][0]["status"] = "evaluating"
        current["candidate_input_completed_at"] = submitted_at
        current["answers"] = [
            {
                "id": "answer_pending_worker",
                "turn_id": "turn_1",
                "evaluation_status": "pending",
            }
        ]
        current["agent_runtime"]["calibration_status"] = "completed"
        session = transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )

    runtime = InterviewAgentRuntime(store)

    async def expression_audio(*args, **kwargs) -> dict:
        return {
            "audio_uri": "private-file://farewell-before-scoring",
            "duration_ms": 400,
            "visemes": [],
        }

    monkeypatch.setattr(runtime, "_expression_audio", expression_audio)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_submit_before_scoring",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        await channel._after_formal_evidence_finished(
            "turn_1",
            ClientSignal(
                type="evidence.finish",
                idempotency_key="last_answer_finish",
            ),
            conversation_action_selected=False,
            stop_evidence_session=False,
        )
        closing = runtime.interviews.get_interview(session["id"])
        closing_id = closing["agent_runtime"]["completion_closing_performance_id"]
        assert closing["status"] == "in_progress"
        assert closing_id

        await channel.send(
            ClientSignal(
                type="avatar.performance.stopped",
                idempotency_key="last_farewell_stopped",
                payload={"performance_id": closing_id},
            )
        )
        final = runtime.interviews.get_interview(session["id"])
        event = next(item for item in final["agent_events"] if item["type"] == "completed")
        assert event["payload"]["status"] == "submitted_for_human_review"
        assert event["payload"]["submitted_at"] == submitted_at
        assert final["answers"][0]["evaluation_status"] == "pending"
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_human_takeover_is_exclusive_audited_unscored_and_stays_paused() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    media = _ready_livekit()
    media.remove_participant = AsyncMock(return_value={})
    runtime = InterviewAgentRuntime(store, media_plane=media)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="interviewer_1",
                    organization_id="org_default",
                    roles=frozenset({"interviewer"}),
                    authenticated=True,
                ),
                connection_id="connection_takeover",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=False,
                    webgl=False,
                    camera=False,
                    microphone=True,
                    speaker=True,
                    avatar_fps=0,
                    media_recorder=False,
                    browser="Chrome test",
                ),
            )
        )
        await channel.send(
            ClientSignal(
                type="takeover.acquire",
                idempotency_key="takeover_acquire_once",
                payload={"reason": "候选人要求人工解释题意"},
            )
        )
        acquired = runtime.interviews.get_interview(session["id"])
        lease = acquired["agent_runtime"]["takeover"]
        assert acquired["status"] == "paused"
        assert acquired["agent_runtime"]["floor"] == "human"
        assert lease["actor_id"] == "interviewer_1"
        InterviewAgentTicketService(
            store, media_plane=media
        ).issue_takeover_media_permit(
            session["id"],
            channel.principal,
            lease_id=lease["lease_id"],
            expected_version=lease["version"],
        )
        lease = runtime.interviews.get_interview(session["id"])["agent_runtime"][
            "takeover"
        ]

        await channel.send(
            ClientSignal(
                type="human.speech",
                idempotency_key="human_speech_once",
                turn_id="turn_1",
                payload={"transcript": "我先澄清一下这道题的范围。"},
            )
        )
        intervened = runtime.interviews.get_interview(session["id"])
        acts = intervened["turns"][0]["conversation_acts"]
        assert acts[-1]["act_type"] == "unscored_intervention"
        assert acts[-1]["approved_by"] == "interviewer_1"
        assert acts[-1]["evaluative"] is False

        await channel.send(
            ClientSignal(
                type="takeover.release",
                idempotency_key="takeover_release_once",
                payload={
                    "lease_id": lease["lease_id"],
                    "expected_version": lease["version"],
                },
            )
        )
        released = runtime.interviews.get_interview(session["id"])
        assert released["status"] == "paused"
        assert released["agent_runtime"]["floor"] == "none"
        assert released["agent_runtime"]["takeover"] is None
        with persistence_for(store).transaction("org_default") as transaction:
            takeover_audits = [
                item
                for item in transaction.audit_events.list()
                if item.get("action") == "interview.takeover.changed"
            ]
        assert takeover_audits[-1]["metadata"]["transition"] == "released"
        assert takeover_audits[-1]["actor_id"] == "interviewer_1"
        media.remove_participant.assert_awaited_once_with(
            room_name=interview_room_name("org_default", session["id"]),
            participant_identity=lease["media_participant_identity"],
        )
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_takeover_watchdog_expires_without_client_signal_and_revokes_media() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(milliseconds=80)
    ).isoformat().replace("+00:00", "Z")
    with persistence_for(store).transaction("org_default") as transaction:
        value = transaction.interview_sessions.get(session["id"])
        value["status"] = "paused"
        value["agent_runtime"]["floor"] = "human"
        value["agent_runtime"]["takeover"] = {
            "lease_id": "takeover_watchdog",
            "actor_id": "interviewer_1",
            "reason": "人工澄清",
            "expires_at": expires_at,
            "version": 4,
            "media_participant_identity": "takeover:takeover_watchdog",
            "media_permit_generation": 1,
            "media_permit_issued_at": utc_now(),
            "media_permit_expires_at": expires_at,
        }
        transaction.interview_sessions.update(value, expected_version=value["version"])
    media = _ready_livekit()
    media.remove_participant = AsyncMock(return_value={})
    runtime = InterviewAgentRuntime(store, media_plane=media)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="interviewer_1",
                    organization_id="org_default",
                    roles=frozenset({"interviewer"}),
                    authenticated=True,
                ),
                connection_id="connection_takeover_watchdog",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=False,
                    webgl=False,
                    camera=False,
                    microphone=True,
                    speaker=True,
                    avatar_fps=0,
                    media_recorder=False,
                    browser="Chrome test",
                ),
            )
        )
        await asyncio.sleep(0.16)
        current = runtime.interviews.get_interview(session["id"])
        assert current["status"] == "paused"
        assert current["agent_runtime"]["floor"] == "none"
        assert current["agent_runtime"]["takeover"] is None
        media.remove_participant.assert_awaited_once_with(
            room_name=interview_room_name("org_default", session["id"]),
            participant_identity="takeover:takeover_watchdog",
        )
        lost = [
            event
            for event in list(channel._queue._queue)
            if getattr(event, "type", None) == "takeover.changed"
            and event.payload.get("status") == "lost"
        ]
        assert lost[-1].payload["ai_resumed"] is False
        with persistence_for(store).transaction("org_default") as transaction:
            audits = transaction.audit_events.list()
        assert any(
            item.get("action") == "interview.takeover.media_participant.revoked"
            and item.get("metadata", {}).get("outcome") == "removed"
            for item in audits
        )
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_persisted_takeover_sweeper_recovers_after_process_restart_once() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        value = transaction.interview_sessions.get(session["id"])
        value["status"] = "paused"
        value["agent_runtime"]["floor"] = "human"
        value["agent_runtime"]["takeover"] = {
            "lease_id": "takeover_restart",
            "actor_id": "interviewer_restart",
            "reason": "进程重启恢复",
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z"),
            "version": 7,
            "media_participant_identity": "takeover:takeover_restart",
            "media_permit_generation": 1,
            "media_permit_lease_version": 7,
            "media_permit_issued_at": utc_now(),
            "media_permit_expires_at": utc_now(),
        }
        transaction.interview_sessions.update(value, expected_version=value["version"])
    media = _ready_livekit()
    media.remove_participant = AsyncMock(return_value={})
    runtime = InterviewAgentRuntime(store, media_plane=media)

    async def scenario() -> None:
        assert await runtime.sweep_expired_takeovers() == 1
        assert await InterviewAgentRuntime(
            store, media_plane=media
        ).sweep_expired_takeovers() == 0
        current = runtime.interviews.get_interview(session["id"])
        assert current["status"] == "paused"
        assert current["agent_runtime"]["takeover"] is None
        assert current["agent_runtime"]["floor"] == "none"
        assert [
            event["type"] for event in current["agent_events"][-2:]
        ] == ["floor.changed", "takeover.changed"]
        media.remove_participant.assert_awaited_once()

    asyncio.run(scenario())


def test_agent_hub_fans_out_takeover_with_role_safe_replay_and_snapshot() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    candidate_runtime = InterviewAgentRuntime(store)
    enterprise_runtime = InterviewAgentRuntime(store)

    candidate_principal = Principal(
        actor_id="candidate:candidate_1",
        organization_id="org_default",
        roles=frozenset({"candidate"}),
        authenticated=True,
    )
    interviewer_principal = Principal(
        actor_id="interviewer_1",
        organization_id="org_default",
        roles=frozenset({"interviewer"}),
        authenticated=True,
    )

    async def scenario() -> None:
        candidate = await candidate_runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=candidate_principal,
                connection_id="connection_candidate_projection",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        enterprise = await enterprise_runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=interviewer_principal,
                connection_id="connection_enterprise_projection",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=False,
                    webgl=False,
                    camera=False,
                    microphone=True,
                    speaker=True,
                    avatar_fps=0,
                    media_recorder=False,
                    browser="Chrome test",
                ),
            )
        )
        try:
            while not candidate._queue.empty():
                candidate._queue.get_nowait()
            while not enterprise._queue.empty():
                enterprise._queue.get_nowait()

            await enterprise.send(
                ClientSignal(
                    type="takeover.acquire",
                    idempotency_key="role_projection_takeover_once",
                    payload={"reason": "需要人工解释题意"},
                )
            )

            candidate_takeover = [
                event
                for event in list(candidate._queue._queue)
                if getattr(event, "type", None) == "takeover.changed"
            ][-1]
            enterprise_takeover = [
                event
                for event in list(enterprise._queue._queue)
                if getattr(event, "type", None) == "takeover.changed"
            ][-1]

            assert candidate_takeover.payload["status"] == "active"
            assert set(candidate_takeover.payload).isdisjoint(
                {"actor_id", "reason", "lease_id"}
            )
            assert enterprise_takeover.payload["actor_id"] == "interviewer_1"
            assert enterprise_takeover.payload["reason"] == "需要人工解释题意"
            assert enterprise_takeover.payload["lease_id"]

            stored = enterprise_runtime.interviews.get_interview(session["id"])
            persisted_takeover = [
                event
                for event in stored["agent_events"]
                if event["type"] == "takeover.changed"
            ][-1]
            assert set(persisted_takeover["payload"]).isdisjoint(
                {"actor_id", "reason", "lease_id"}
            )
            assert not any(
                event["type"] == "session.snapshot"
                for event in stored["agent_events"]
            )

            recovered_candidate = await candidate_runtime.open(
                OpenAgentSession(
                    interview_id=session["id"],
                    principal=candidate_principal,
                    connection_id="connection_candidate_recovered",
                    recovery_cursor=0,
                    capabilities=ClientCapabilities(
                        webrtc=True,
                        audio_worklet=True,
                        webgl=True,
                        camera=True,
                        microphone=True,
                        speaker=True,
                        avatar_fps=60,
                        media_recorder=True,
                        browser="Chrome test",
                    ),
                )
            )
            try:
                snapshot = [
                    event
                    for event in list(recovered_candidate._queue._queue)
                    if getattr(event, "type", None) == "session.snapshot"
                ][-1]
                assert snapshot.payload["interview_id"] == session["id"]
                assert set(snapshot.payload).isdisjoint(
                    {"candidate", "media_capture", "problems", "active_output_id"}
                )
                assert snapshot.payload["active_performance_id"] is None
                assert set(snapshot.payload["takeover"]).isdisjoint(
                    {"actor_id", "reason", "lease_id"}
                )
                replayed_takeover = [
                    event
                    for event in list(recovered_candidate._queue._queue)
                    if getattr(event, "type", None) == "takeover.changed"
                ][-1]
                assert set(replayed_takeover.payload).isdisjoint(
                    {"actor_id", "reason", "lease_id"}
                )
            finally:
                await recovered_candidate.close("test_complete")
        finally:
            await candidate.close("test_complete")
            await enterprise.close("test_complete")

    asyncio.run(scenario())


def test_reviewer_projection_hides_operator_and_private_capture_fields() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    capture = _recording_capture(store, session)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_media_captures.get(capture["id"])
        current.update(
            {
                "status": "recording",
                "participant_identity": "candidate-private-identity",
                "egress_id": "egress_private",
                "private_uri": "private-file://capture-secret",
                "content_hash": "a" * 64,
                "encryption": "private_object_storage_sse",
            }
        )
        transaction.interview_media_captures.update(
            current, expected_version=current["version"]
        )

    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        operator = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="interviewer_1",
                    organization_id="org_default",
                    roles=frozenset({"interviewer"}),
                    authenticated=True,
                ),
                connection_id="connection_operator_projection",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=False,
                    webgl=False,
                    camera=False,
                    microphone=True,
                    speaker=True,
                    avatar_fps=0,
                    media_recorder=False,
                    browser="Chrome test",
                ),
            )
        )
        await operator.send(
            ClientSignal(
                type="takeover.acquire",
                idempotency_key="reviewer_projection_takeover",
                payload={"reason": "内部接管原因"},
            )
        )
        reviewer = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="reviewer_1",
                    organization_id="org_default",
                    roles=frozenset({"reviewer"}),
                    authenticated=True,
                ),
                connection_id="connection_reviewer_projection",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=False,
                    webgl=False,
                    camera=False,
                    microphone=False,
                    speaker=True,
                    avatar_fps=0,
                    media_recorder=False,
                    browser="Chrome test",
                ),
            )
        )
        try:
            snapshot = [
                event
                for event in list(reviewer._queue._queue)
                if getattr(event, "type", None) == "session.snapshot"
            ][-1]
            assert snapshot.payload["candidate"]["id"] == "candidate_1"
            assert set(snapshot.payload["takeover"]).isdisjoint(
                {"actor_id", "reason", "lease_id"}
            )
            assert snapshot.payload["media_capture"]["status"] == "recording"
            assert set(snapshot.payload["media_capture"]).isdisjoint(
                {
                    "participant_identity",
                    "egress_id",
                    "private_uri",
                    "content_hash",
                    "encryption",
                }
            )

            while not reviewer._queue.empty():
                reviewer._queue.get_nowait()
            await reviewer.send(
                ClientSignal(
                    type="takeover.acquire",
                    idempotency_key="reviewer_forbidden_takeover",
                    payload={"reason": "reviewer must not acquire"},
                )
            )
            forbidden = [
                event
                for event in list(reviewer._queue._queue)
                if getattr(event, "type", None) == "problem"
            ][-1]
            assert forbidden.payload["code"] == "HUMAN_TAKEOVER_FORBIDDEN"
            assert "reviewer must not acquire" not in forbidden.payload["message"]
            current = runtime.interviews.get_interview(session["id"])
            assert current["agent_runtime"]["takeover"]["actor_id"] == "interviewer_1"
        finally:
            await reviewer.close("test_complete")
            await operator.close("test_complete")

    asyncio.run(scenario())


def test_legacy_snapshot_is_never_replayed_and_cursor_contract_is_explicit() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["agent_runtime"]["last_sequence"] = 7
        current["agent_runtime"]["replay_history_floor"] = 4
        current["agent_events"] = [
            AgentEvent(
                event_id="legacy_privileged_snapshot",
                session_sequence=5,
                type="session.snapshot",
                occurred_at=utc_now(),
                replayability=Replayability.REPLAYABLE,
                payload={
                    "media_capture": {"private_uri": "private-file://must-not-leak"},
                    "takeover": {"lease_id": "lease_must_not_leak"},
                },
            ).model_dump(mode="json"),
            AgentEvent(
                event_id="replay_floor_after_gap",
                session_sequence=6,
                type="floor.changed",
                occurred_at=utc_now(),
                replayability=Replayability.REPLAYABLE,
                payload={"owner": "candidate", "reason": "candidate_continues"},
            ).model_dump(mode="json"),
        ]
        current["updated_at"] = utc_now()
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    runtime = InterviewAgentRuntime(store)
    principal = Principal(
        actor_id="candidate:candidate_1",
        organization_id="org_default",
        roles=frozenset({"candidate"}),
        authenticated=True,
    )
    capabilities = ClientCapabilities(
        webrtc=True,
        audio_worklet=True,
        webgl=True,
        camera=True,
        microphone=True,
        speaker=True,
        avatar_fps=60,
        media_recorder=True,
        browser="Chrome test",
    )

    async def scenario() -> None:
        with pytest.raises(ApiError) as invalid:
            await runtime.open(
                OpenAgentSession(
                    interview_id=session["id"],
                    principal=principal,
                    connection_id="connection_cursor_ahead",
                    recovery_cursor=8,
                    capabilities=capabilities,
                )
            )
        assert invalid.value.code == "AGENT_RECOVERY_CURSOR_INVALID"

        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=principal,
                connection_id="connection_recovery_gap",
                recovery_cursor=1,
                capabilities=capabilities,
            )
        )
        try:
            queued = list(channel._queue._queue)
            snapshots = [event for event in queued if event.type == "session.snapshot"]
            assert len(snapshots) == 1
            assert "media_capture" not in snapshots[0].payload
            assert "lease_id" not in (snapshots[0].payload.get("takeover") or {})
            gap = [
                event
                for event in queued
                if event.type == "problem"
                and event.payload.get("code") == "AGENT_RECOVERY_GAP"
            ][-1]
            assert gap.payload["action"] == "resync_from_snapshot"
            assert [event.type for event in queued][-1] == "session.snapshot"
        finally:
            await channel.close("test_complete")

    asyncio.run(scenario())


def test_agent_channel_close_always_unregisters_and_terminates_event_stream() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)

    class FailingEvidenceStream:
        async def close(self, *, repair_disconnect: bool) -> list:
            assert repair_disconnect is True
            raise RuntimeError("injected evidence close failure")

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_close_failure",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        while not channel._queue.empty():
            channel._queue.get_nowait()
        channel._stt = FailingEvidenceStream()
        hub_key = _AGENT_CHANNEL_HUB._key(channel)
        assert channel in _AGENT_CHANNEL_HUB._channels[hub_key]

        with pytest.raises(RuntimeError, match="injected evidence close failure"):
            await channel.close("failure_injection")

        assert channel not in _AGENT_CHANNEL_HUB._channels.get(hub_key, set())
        event_stream = channel.events()
        with pytest.raises(StopAsyncIteration):
            await event_stream.__anext__()

    asyncio.run(scenario())


def test_agent_channel_close_cancellation_cannot_skip_transport_cleanup() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_cancelled_close",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        while not channel._queue.empty():
            channel._queue.get_nowait()

        endpoint_release = asyncio.Event()

        async def endpoint_that_needs_cancellation() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await endpoint_release.wait()

        endpoint = asyncio.create_task(endpoint_that_needs_cancellation())
        await asyncio.sleep(0)
        channel._endpoint_task = endpoint
        hub_key = _AGENT_CHANNEL_HUB._key(channel)
        closing = asyncio.create_task(channel.close("server_shutdown"))
        await asyncio.sleep(0)
        closing.cancel()
        endpoint_release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert channel not in _AGENT_CHANNEL_HUB._channels.get(hub_key, set())
        event_stream = channel.events()
        with pytest.raises(StopAsyncIteration):
            await event_stream.__anext__()
        await asyncio.gather(endpoint, return_exceptions=True)

    asyncio.run(scenario())


def test_event_queue_is_bounded_coalesces_transients_and_preserves_replayables() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_bounded_queue",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        try:
            while not channel._queue.empty():
                channel._queue.get_nowait()
            replay_ids = []
            for sequence in range(1, 5):
                event_id = "replay_preserved_%s" % sequence
                replay_ids.append(event_id)
                channel._enqueue_event(
                    AgentEvent(
                        event_id=event_id,
                        session_sequence=sequence,
                        type="floor.changed",
                        occurred_at=utc_now(),
                        replayability=Replayability.REPLAYABLE,
                        payload={"owner": "candidate", "reason": "test"},
                    )
                )
            for sequence in range(5, 10_005):
                channel._enqueue_event(
                    AgentEvent(
                        event_id="partial_%s" % sequence,
                        session_sequence=sequence,
                        type="transcript.partial",
                        occurred_at=utc_now(),
                        replayability=Replayability.TRANSIENT,
                        payload={"text": "partial %s" % sequence},
                    )
                )
            newest_replay = AgentEvent(
                event_id="newest_replay",
                session_sequence=10_005,
                type="takeover.changed",
                occurred_at=utc_now(),
                replayability=Replayability.REPLAYABLE,
                payload={"status": "active"},
            )
            channel._enqueue_event(newest_replay)

            queued = list(channel._queue._queue)
            assert channel._queue.qsize() <= _AGENT_QUEUE_CAPACITY
            assert channel._closed is False
            queued_ids = {event.event_id for event in queued}
            assert set(replay_ids).issubset(queued_ids)
            assert newest_replay.event_id in queued_ids
            partials = [event for event in queued if event.type == "transcript.partial"]
            assert partials[-1].payload["text"] == "partial 10004"
        finally:
            await channel.close("test_complete")

    asyncio.run(scenario())


def test_one_recipient_projection_failure_does_not_block_healthy_channels() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    source_runtime = InterviewAgentRuntime(store)
    healthy_runtime = InterviewAgentRuntime(store)
    bad_runtime = InterviewAgentRuntime(store)

    def candidate_open(runtime: InterviewAgentRuntime, connection_id: str):
        return runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id=connection_id,
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )

    async def scenario() -> None:
        source = await candidate_open(source_runtime, "connection_projection_source")
        healthy = await candidate_open(healthy_runtime, "connection_projection_healthy")
        bad = await candidate_open(bad_runtime, "connection_projection_bad")
        try:
            for channel in (source, healthy, bad):
                while not channel._queue.empty():
                    channel._queue.get_nowait()

            def reject_projection(*args, **kwargs):
                raise RuntimeError("injected projection failure")

            bad.runtime._project_event_for_principal = reject_projection
            await source.send(
                ClientSignal(
                    type="speech.started",
                    idempotency_key="projection_failure_speech",
                    turn_id="turn_1",
                )
            )
            await asyncio.sleep(0)

            assert any(
                event.type == "speech.started"
                for event in list(source._queue._queue)
            )
            assert any(
                event.type == "speech.started"
                for event in list(healthy._queue._queue)
            )
            assert bad._closed is True
            bad_stream = bad.events()
            with pytest.raises(StopAsyncIteration):
                await bad_stream.__anext__()
        finally:
            await source.close("test_complete")
            await healthy.close("test_complete")
            await bad.close("test_complete")

    asyncio.run(scenario())


def test_cross_instance_agent_envelope_is_org_scoped_and_re_sanitized() -> None:
    remote_store = InMemoryStore()
    session = _runtime_session(remote_store)
    remote_runtime = InterviewAgentRuntime(remote_store)

    async def scenario() -> None:
        channel = await remote_runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="interviewer_remote",
                    organization_id="org_default",
                    roles=frozenset({"interviewer"}),
                    authenticated=True,
                ),
                connection_id="connection_cross_instance_remote",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=False,
                    webgl=False,
                    camera=False,
                    microphone=True,
                    speaker=True,
                    avatar_fps=0,
                    media_recorder=False,
                    browser="Chrome test",
                ),
            )
        )
        try:
            while not channel._queue.empty():
                channel._queue.get_nowait()
            raw_event = AgentEvent(
                event_id="remote_takeover_event",
                session_sequence=10,
                type="takeover.changed",
                occurred_at=utc_now(),
                replayability=Replayability.REPLAYABLE,
                payload={
                    "status": "active",
                    "expires_at": utc_now(),
                    "version": 1,
                    "actor_id": "must_not_cross_redis",
                    "reason": "must_not_cross_redis",
                    "lease_id": "must_not_cross_redis",
                },
            )
            handled = await receive_remote_agent_event(
                session["id"],
                {
                    "transport": "interview_agent.v1",
                    "organization_id": "org_default",
                    "event": raw_event.model_dump(mode="json"),
                },
            )
            assert handled is True
            delivered = list(channel._queue._queue)[-1]
            assert delivered.payload["status"] == "active"
            assert set(delivered.payload).isdisjoint(
                {"actor_id", "reason", "lease_id"}
            )

            before = channel._queue.qsize()
            assert (
                await receive_remote_agent_event(
                    session["id"],
                    {
                        "transport": "interview_agent.v1",
                        "organization_id": "another_org",
                        "event": raw_event.model_dump(mode="json"),
                    },
                )
                is True
            )
            assert channel._queue.qsize() == before
            assert (
                await receive_remote_agent_event(
                    session["id"], {"type": "legacy.event"}
                )
                is False
            )
        finally:
            await channel.close("test_complete")

    asyncio.run(scenario())


def test_required_media_capture_start_failure_pauses_interview() -> None:
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_failure")
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["status"] = "in_progress"
        current["updated_at"] = utc_now()
        session = transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane(fail_start=True)
    service = InterviewMediaCaptureService(
        store, media_plane=plane, storage=_CaptureObjectStorage()
    )

    with pytest.raises(ApiError) as failed:
        asyncio.run(
            service.start_for_connection(
                session["id"],
                consumed.connection_id,
                actor_id=consumed.principal.actor_id,
            )
        )
    assert failed.value.code == "MEDIA_CAPTURE_START_FAILED"
    capture = service.get_for_interview(session["id"])
    assert capture["status"] == "failed"
    assert capture["failure_code"] == "egress_start_failed"
    paused = service.interviews.get_interview(session["id"])
    assert paused["status"] == "paused"
    assert paused["interruption"]["reason"] == "required_media_capture_failed"
    assert len(plane.start_calls) == 1


def test_required_capture_fails_before_egress_when_bucket_encryption_is_unverified() -> None:
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_no_encryption")
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["status"] = "in_progress"
        session = transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane()
    service = InterviewMediaCaptureService(
        store, media_plane=plane, storage=_UnencryptedCaptureStorage()
    )

    with pytest.raises(ApiError) as failed:
        asyncio.run(
            service.start_for_connection(
                session["id"],
                consumed.connection_id,
                actor_id=consumed.principal.actor_id,
            )
        )
    assert failed.value.code == "MEDIA_CAPTURE_ENCRYPTION_UNVERIFIED"
    assert plane.start_calls == []
    capture = service.get_for_interview(session["id"])
    assert capture["status"] == "failed"
    assert capture["encryption"] is None
    assert capture["failure_code"] == "recording_encryption_policy_unverified"
    assert service.interviews.get_interview(session["id"])["status"] == "paused"


def test_start_reconciles_crash_window_without_creating_duplicate_egress() -> None:
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_crash_recovery")
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane()
    object_key = InterviewMediaCaptureService._object_key(
        "org_default", session["id"], "media_capture_contract"
    )
    plane.egresses = [
        {
            "egress_id": "egress_recovered",
            "status": "EGRESS_ACTIVE",
            "room_name": interview_room_name("org_default", session["id"]),
            "participant": {
                "identity": consumed.participant_identity,
                "file_outputs": [{"filepath": object_key}],
            },
        }
    ]
    with persistence_for(store).transaction("org_default") as transaction:
        capture = transaction.interview_media_captures.get("media_capture_contract")
        capture.update(
            {
                "status": "starting",
                "participant_identity": consumed.participant_identity,
                "connection_id": consumed.connection_id,
                "object_key": object_key,
                "start_requested_at": "2026-01-01T00:00:00Z",
                "start_lease_expires_at": "2026-01-01T00:00:15Z",
            }
        )
        transaction.interview_media_captures.update(
            capture, expected_version=capture["version"]
        )
    service = InterviewMediaCaptureService(
        store, media_plane=plane, storage=_CaptureObjectStorage()
    )

    recovered = asyncio.run(
        service.start_for_connection(
            session["id"],
            consumed.connection_id,
            actor_id=consumed.principal.actor_id,
        )
    )
    assert recovered["status"] == "recording"
    assert recovered["egress_id"] == "egress_recovered"
    assert recovered["reconciled_at"]
    assert plane.start_calls == []


def test_unknown_crash_window_fails_closed_instead_of_starting_second_egress() -> None:
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_unknown_start")
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["status"] = "in_progress"
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane()
    object_key = InterviewMediaCaptureService._object_key(
        "org_default", session["id"], "media_capture_contract"
    )
    with persistence_for(store).transaction("org_default") as transaction:
        capture = transaction.interview_media_captures.get("media_capture_contract")
        capture.update(
            {
                "status": "starting",
                "participant_identity": consumed.participant_identity,
                "connection_id": consumed.connection_id,
                "object_key": object_key,
                "start_requested_at": "2026-01-01T00:00:00Z",
                "start_lease_expires_at": "2026-01-01T00:00:15Z",
            }
        )
        transaction.interview_media_captures.update(
            capture, expected_version=capture["version"]
        )
    service = InterviewMediaCaptureService(
        store, media_plane=plane, storage=_CaptureObjectStorage()
    )

    with pytest.raises(ApiError) as failed:
        asyncio.run(
            service.start_for_connection(
                session["id"],
                consumed.connection_id,
                actor_id=consumed.principal.actor_id,
            )
        )
    assert failed.value.code == "MEDIA_CAPTURE_START_OUTCOME_UNKNOWN"
    assert plane.start_calls == []
    capture = service.get_for_interview(session["id"])
    assert capture["status"] == "failed"
    assert capture["failure_code"] == "egress_start_outcome_unknown"
    assert service.interviews.get_interview(session["id"])["status"] == "paused"


def test_media_capture_uses_consumed_identity_and_finalizes_private_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_success")
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane()
    storage = _CaptureObjectStorage()
    service = InterviewMediaCaptureService(store, media_plane=plane, storage=storage)

    started = asyncio.run(
        service.start_for_connection(
            session["id"],
            consumed.connection_id,
            actor_id=consumed.principal.actor_id,
        )
    )
    assert started["status"] == "recording"
    assert started["participant_identity"] == consumed.participant_identity
    assert plane.start_calls[0]["participant_identity"] == consumed.participant_identity

    content = b"encrypted-private-capture"
    storage.expected_key = started["object_key"]
    storage.content = content
    completed = asyncio.run(
        service.stop_for_interview(
            session["id"], actor_id=consumed.principal.actor_id
        )
    )
    assert completed["status"] == "completed"
    assert completed["private_uri"].startswith("private-media-capture://")
    assert completed["content_hash"] == "sha256:%s" % hashlib.sha256(content).hexdigest()
    assert completed["byte_count"] == len(content)
    assert completed["encryption"] == "aliyun_oss_aes256"
    assert completed["encryption_verified_at"]
    assert plane.stop_calls == ["egress_contract"]


def test_media_capture_finalizes_to_local_private_directory_in_development(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_local")
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane()
    storage = LocalPrivateFileAdapter(tmp_path)
    service = InterviewMediaCaptureService(store, media_plane=plane, storage=storage)

    started = asyncio.run(
        service.start_for_connection(
            session["id"],
            consumed.connection_id,
            actor_id=consumed.principal.actor_id,
        )
    )
    output = tmp_path / started["object_key"]
    output.parent.mkdir(parents=True)
    output.write_bytes(b"local-private-capture")

    completed = asyncio.run(
        service.stop_for_interview(
            session["id"], actor_id=consumed.principal.actor_id
        )
    )

    assert completed["status"] == "completed"
    assert completed["storage_protection"] == "local_private_development"
    assert completed["storage_protection_verified_at"]
    assert completed["encryption"] is None
    assert completed["encryption_verified_at"] is None


def test_finalized_capture_without_object_encryption_never_becomes_completed() -> None:
    store = InMemoryStore()
    session = _session(store, interview_id="interview_capture_object_unencrypted")
    _recording_capture(store, session)
    ticket_service = InterviewAgentTicketService(store, media_plane=_ready_livekit())
    issued = ticket_service.issue_candidate(
        session["id"], _candidate_token(ticket_service, session)
    )
    consumed = ticket_service.consume(session["id"], issued["agent_ticket"])
    plane = _CaptureMediaPlane()
    storage = _CaptureObjectStorage()
    service = InterviewMediaCaptureService(store, media_plane=plane, storage=storage)
    started = asyncio.run(
        service.start_for_connection(
            session["id"],
            consumed.connection_id,
            actor_id=consumed.principal.actor_id,
        )
    )
    storage.expected_key = started["object_key"]

    def reject_object_encryption(object_key=None):
        raise RuntimeError("stored object omitted SSE metadata")

    storage.verify_encryption = reject_object_encryption

    with pytest.raises(ApiError) as failed:
        asyncio.run(
            service.stop_for_interview(
                session["id"], actor_id=consumed.principal.actor_id
            )
        )
    assert failed.value.code == "MEDIA_CAPTURE_ENCRYPTION_UNVERIFIED"
    capture = service.get_for_interview(session["id"])
    assert capture["status"] == "failed"
    assert capture.get("private_uri") is None
    assert capture["failure_code"] == "recording_object_encryption_unverified"


def test_meta_intent_survives_low_stt_confidence_and_keeps_exact_evidence() -> None:
    store = InMemoryStore()
    service = ConversationUnderstandingService(store, gateway=_Gateway())
    utterance = _utterance("麻烦请，再说 一遍。", confidence=0.2)
    result = asyncio.run(service.understand(utterance, _turn(), _interview(_turn())))
    assert result.intent == "request_repeat"
    assert result.suggested_action == "repeat"
    assert result.evidence_quotes == ["再说 一遍"]
    assert result.evidence_quotes[0] in utterance.text


def test_understanding_rejects_non_verbatim_evidence_without_creating_semantics() -> None:
    transcript = "我会保存音频序号并在恢复后检查结果。"
    response = _understanding_response(transcript)
    response["evidence_quotes"] = ["模型编造的证据"]
    response["claims"] = []
    service = ConversationUnderstandingService(
        InMemoryStore(), gateway=_Gateway([response])
    )
    result = asyncio.run(
        service.understand(
            _utterance(transcript),
            _turn(),
            _interview(_turn()),
        )
    )
    assert result.intent == "clarification_request"
    assert result.suggested_action == "pause"
    assert result.evidence_quotes == []
    assert result.claims == []
    assert result.problem is not None
    assert result.problem.code == "UNDERSTANDING_RESULT_REJECTED"


def test_understanding_reference_contract_retries_partition_without_rewriting_evidence(caplog) -> None:
    transcript = "嗯，Fast API 检查 Redis 依赖，readiness 返回 200。然后等待 10 秒，再退出旧 Pod。"
    first = {"clarification_target": None,
        "intent": "answer", "answer_summary": "说明了就绪检查和滚动更新",
        "claims": [{"claim": "先检查依赖", "evidence_id": "E1"}],
        "evidence_ids": ["E1"], "covered_point_ids": ["P1"],
        "missing_point_ids": ["P2"], "ambiguities": [], "contradictions": [],
        "confidence": 0.9, "suggested_action": "accept",
    }
    second = {**first, "missing_point_ids": ["P2", "P3"]}
    gateway = _Gateway([first, second])
    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=gateway).understand(
        _utterance(transcript), _turn(), _interview(_turn()),
    ))
    assert result.problem is None
    assert result.prompt_version == "interview_turn_understanding.v8"
    assert result.evidence_quotes == ["嗯，Fast API 检查 Redis 依赖，readiness 返回 200。"]
    assert result.claims[0].evidence_quote == result.evidence_quotes[0]
    assert len(gateway.requests) == 2
    assert "point_partition_invalid" in gateway.requests[1][1].messages[-1].content
    assert "point_partition_invalid" in caplog.text
    assert transcript not in caplog.text and "Redis" not in caplog.text


@pytest.mark.parametrize("overrides,reason", [
    ({"evidence_ids": ["E999"]}, "wire_schema_invalid"),
    ({"evidence_ids": ["E1", "E1"]}, "wire_schema_invalid"),
    ({"covered_point_ids": ["P999"]}, "wire_schema_invalid"),
    ({"missing_point_ids": ["P1", "P2", "P3"]}, "point_partition_invalid"),
    ({"evidence_ids": ["E2"]}, "claim_evidence_undeclared"),
    ({"claims": []}, "answer_evidence_empty"),
    ({"answer_summary": ""}, "answer_evidence_empty"),
    ({"unexpected": "private_response_marker"}, "wire_schema_invalid"),
])
def test_reference_contract_invalid_twice_never_creates_semantics(overrides, reason, caplog) -> None:
    transcript = "我检查依赖。然后验证就绪。"
    data = {"clarification_target": None,
        "intent": "answer", "answer_summary": "说明检查流程",
        "claims": [{"claim": "检查依赖", "evidence_id": "E1"}],
        "evidence_ids": ["E1"], "covered_point_ids": ["P1"],
        "missing_point_ids": ["P2", "P3"], "ambiguities": [], "contradictions": [],
        "confidence": 0.9, "suggested_action": "accept", **overrides,
    }
    gateway = _Gateway([data, data])
    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=gateway).understand(
        _utterance(transcript), _turn(), _interview(_turn()),
    ))
    assert result.suggested_action == "pause"
    assert result.problem.reason_code == reason
    assert result.problem.attempts == 2
    assert len(gateway.requests) == 2
    assert result.claims == [] and result.evidence_quotes == []
    assert "private_response_marker" not in caplog.text
    assert transcript not in caplog.text


def test_agent_projects_understanding_failure_as_problem_and_pauses() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_understanding_failure",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        while not channel._queue.empty():
            channel._queue.get_nowait()

        await channel._project_evidence_event(
            {
                "type": "utterance.not_accepted",
                "turn_id": "turn_1",
                "intent": "clarification_request",
                "suggested_action": "pause",
                "problem": {
                    "code": "UNDERSTANDING_RESULT_REJECTED",
                    "source": "schema_or_content",
                    "recoverable": False,
                    "action": "pause",
                    "retryable": False,
                },
            },
            "understanding_failure_1",
        )

        current = runtime.interviews.get_interview(session["id"])
        assert current["status"] == "paused"
        assert current["answers"] == []
        assert current["agent_runtime"]["floor"] == "none"
        assert current["agent_runtime"]["problems"][-1]["code"] == (
            "UNDERSTANDING_RESULT_REJECTED"
        )
        events = list(channel._queue._queue)
        problem = next(item for item in events if item.type == "problem")
        assert problem.payload["action"] == "pause"
        assert problem.payload["recoverable"] is False
        assert not any(
            item.type == "conversation.act.selected" for item in events
        )
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_agent_plays_materialized_s2s_followup_without_invoking_cascade_tts() -> None:
    store = InMemoryStore()
    session = _runtime_session(store)
    with persistence_for(store).transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(session["id"])
        current["agent_runtime"]["calibration_status"] = "completed"
        current["turns"][0].update(
            {
                "is_followup": True,
                "followup_depth": 1,
                "root_turn_id": "turn_1",
                "target_key_points": ["幂等性"],
                "conversation_acts": [
                    ApprovedConversationAct(
                        act_id="conversation_act_s2s_frozen",
                        act_type="followup",
                        text="请说明如何避免重复答案？",
                        turn_id="turn_1",
                        root_turn_id="turn_1",
                        evidence_quotes=["我会使用幂等键"],
                        target_capability_points=["幂等性"],
                        followup_depth=1,
                        evaluative=False,
                        approved_by="controlled_followup_gate",
                        prompt_version="controlled_followup.v1",
                        created_at=utc_now(),
                    ).model_dump(mode="json")
                ],
            }
        )
        current["updated_at"] = utc_now()
        transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )
    runtime = InterviewAgentRuntime(store)

    async def cascade_must_not_run(*args, **kwargs):
        raise AssertionError("approved S2S audio must not be discarded")

    runtime._expression_audio = cascade_must_not_run

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_s2s_expression",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        while not channel._queue.empty():
            channel._queue.get_nowait()
        await channel._project_evidence_event(
            {
                "type": "followup.selected",
                "payload": {
                    "turn_id": "turn_1",
                    "question_text": "请说明如何避免重复答案？",
                },
                "delivery": "s2s",
                "expression": {
                    "audio_uri": "/media/approved-s2s.wav",
                    "duration_ms": 800,
                    "visemes": [],
                    "delivery": "s2s",
                },
            },
            "s2s_followup_1",
        )
        events = list(channel._queue._queue)
        performance = next(
            item for item in events if item.type == "avatar.performance.started"
        )
        assert performance.payload["delivery"] == "s2s"
        assert performance.payload["audio_uri"] == "/media/approved-s2s.wav"
        assert performance.payload["alignment_source"] == "g2p_estimate"
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_root_question_falls_back_from_mock_avatar_to_managed_tts(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_BUFFERED_TTS_ENABLED", "false")
    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)
    runtime.avatar.speak = AsyncMock(
        return_value={"mode": "browser_speech", "text": "开发占位朗读"}
    )
    runtime.gateway.invoke = AsyncMock(
        return_value=TTSSynthesizeResponse(
            audio_uri="https://provider.example/question.wav",
            content_type="audio/wav",
            duration_ms=900,
            content_hash="sha256:test",
            visemes=[],
            provider=ProviderMeta(
                provider_id="dashscope",
                model="qwen3-tts-flash",
                request_id="tts_request_1",
                latency_ms=1,
            ),
        )
    )
    runtime.expression_audio.import_tts = AsyncMock(
        return_value={
            "audio_uri": "agent-expression://file_question",
            "duration_ms": 900,
        }
    )

    result = asyncio.run(
        runtime._expression_audio(
            session["id"],
            "turn_1",
            "请说明你的断线恢复设计。",
            "candidate:candidate_1",
            "org_default",
        )
    )

    assert result == {
        "audio_uri": "agent-expression://file_question",
        "duration_ms": 900,
        "visemes": [],
        "delivery": "cascade",
    }
    runtime.gateway.invoke.assert_awaited_once()
    runtime.expression_audio.import_tts.assert_awaited_once()


def test_paused_candidate_reconnect_is_read_only_and_does_not_replay_opening() -> None:
    store = InMemoryStore()
    session = _session(store)
    runtime = InterviewAgentRuntime(store)

    async def scenario() -> None:
        channel = await runtime.open(
            OpenAgentSession(
                interview_id=session["id"],
                principal=Principal(
                    actor_id="candidate:candidate_1",
                    organization_id="org_default",
                    roles=frozenset({"candidate"}),
                    authenticated=True,
                ),
                connection_id="connection_paused_reconnect",
                capabilities=ClientCapabilities(
                    webrtc=True,
                    audio_worklet=True,
                    webgl=True,
                    camera=True,
                    microphone=True,
                    speaker=True,
                    avatar_fps=60,
                    media_recorder=True,
                    browser="Chrome test",
                ),
            )
        )
        while not channel._queue.empty():
            channel._queue.get_nowait()
        await channel.send(
            ClientSignal(
                type="client.ready",
                idempotency_key="paused_client_ready",
                payload={"source": "test"},
            )
        )

        events = list(channel._queue._queue)
        assert any(item.type == "session.snapshot" for item in events)
        assert not any(item.type == "conversation.act.selected" for item in events)
        current = runtime.interviews.get_interview(session["id"])
        assert current["status"] == "paused"
        assert current["agent_runtime"].get("calibration_status") is None
        await channel.close("test_complete")

    asyncio.run(scenario())


def test_controlled_followup_binds_frozen_target_and_verbatim_evidence() -> None:
    transcript = "我会记录音频序号，重连时从最后确认的位置继续。"
    gateway = _Gateway(
        [_understanding_response(transcript), _followup_response(transcript)]
    )
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    turn = _turn()
    interview = _interview(turn)
    utterance = _utterance(transcript)
    understanding = asyncio.run(service.understand(utterance, turn, interview))
    decision = asyncio.run(
        service.select_followup(interview, turn, utterance, understanding)
    )
    assert decision["selected"] is True
    assert decision["followup_depth"] == 1
    assert decision["evidence_quotes"] == [transcript]
    assert decision["target_key_points"] == ["幂等键"]
    assert decision["conversation_act"]["approved_by"] == "controlled_followup_gate"
    assert decision["conversation_act"]["evaluative"] is False


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda interview, turn, understanding: turn.update(followup_depth=2), "depth_budget_exhausted"),
        (
            lambda interview, turn, understanding: interview.update(
                scheduled_end_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=89)
                ).isoformat().replace("+00:00", "Z")
            ),
            "time_budget_exhausted",
        ),
        (
            lambda interview, turn, understanding: understanding.missing_capability_points.__setitem__(
                slice(None), ["年龄"]
            ),
            "sensitive_capability_target",
        ),
        (
            lambda interview, turn, understanding: understanding.evidence_quotes.__setitem__(
                slice(None), []
            ),
            "model_evidence_unavailable",
        ),
    ],
)
def test_followup_hard_gates_stop_unsafe_or_over_budget_probe(mutate, reason: str) -> None:
    transcript = "我会记录音频序号，重连时从最后确认的位置继续。"
    service = ConversationUnderstandingService(
        InMemoryStore(), gateway=_Gateway([_understanding_response(transcript)])
    )
    turn = _turn()
    interview = _interview(turn)
    utterance = _utterance(transcript)
    understanding = asyncio.run(service.understand(utterance, turn, interview))
    mutate(interview, turn, understanding)
    decision = asyncio.run(
        service.select_followup(interview, turn, utterance, understanding)
    )
    assert decision["selected"] is False
    assert decision["reason"] == reason


def test_provider_failure_uses_evidence_bound_safe_deterministic_probe() -> None:
    transcript = "我会记录音频序号，重连时从最后确认的位置继续。"
    service = ConversationUnderstandingService(
        InMemoryStore(),
        gateway=_Gateway(
            error=ProviderError("provider_unavailable", "offline", retryable=True)
        ),
    )
    turn = _turn()
    interview = _interview(turn)
    understanding_gateway = _Gateway([_understanding_response(transcript)])
    understanding = asyncio.run(
        ConversationUnderstandingService(
            InMemoryStore(), gateway=understanding_gateway
        ).understand(_utterance(transcript), turn, interview)
    )
    decision = asyncio.run(
        service.select_followup(interview, turn, _utterance(transcript), understanding)
    )
    assert decision["selected"] is True
    assert decision["probe_source"] == "deterministic_template"
    assert decision["evidence_quotes"] == [transcript]
    assert decision["conversation_act"]["approved_by"] == "deterministic_followup_gate"
    assert "年龄" not in decision["question_text"]


def test_unsafe_model_followup_is_rejected_before_expression() -> None:
    transcript = "我会记录音频序号，重连时从最后确认的位置继续。"
    unsafe = _followup_response(
        transcript,
        question_text="你多大年龄？",
        sensitive_attribute_inference=True,
    )
    gateway = _Gateway([_understanding_response(transcript), unsafe])
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    turn = _turn()
    interview = _interview(turn)
    utterance = _utterance(transcript)
    understanding = asyncio.run(service.understand(utterance, turn, interview))
    decision = asyncio.run(
        service.select_followup(interview, turn, utterance, understanding)
    )
    assert decision["selected"] is True
    assert decision["probe_source"] == "deterministic_template"
    assert "年龄" not in decision["question_text"]
