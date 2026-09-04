const EVENT_ENVELOPE_KEYS = Object.freeze([
  "event_id",
  "session_sequence",
  "type",
  "turn_id",
  "causation_id",
  "occurred_at",
  "replayability",
  "payload",
]);

export const STABLE_AGENT_EVENT_TYPES = Object.freeze([
  "session.snapshot",
  "floor.changed",
  "speech.started",
  "speech.stopped",
  "transcript.partial",
  "transcript.final",
  "conversation.act.selected",
  "avatar.performance.started",
  "avatar.performance.cue",
  "avatar.performance.interrupted",
  "avatar.performance.stopped",
  "takeover.changed",
  "problem",
  "completed",
]);

const EVENT_TYPE_SET = new Set(STABLE_AGENT_EVENT_TYPES);
const REPLAYABILITY = new Set(["replayable", "transient"]);
const FLOOR_OWNERS = new Set(["agent", "candidate", "human", "none"]);
const SPEAKERS = new Set(["agent", "candidate", "human"]);
const VISEMES = new Set([
  "sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS",
  "nn", "RR", "aa", "E", "ih", "oh", "ou",
]);
const GESTURES = new Set([
  "idle", "blink", "look_at_candidate", "breathe", "nod",
  "listen", "think", "interrupt", "farewell",
]);
const ALIGNMENT_SOURCES = new Set(["provider_timestamp", "g2p_estimate"]);
const EXPRESSION_DELIVERIES = new Set(["pre_generated", "cascade", "s2s"]);

const BASE_PAYLOAD_KEYS = Object.freeze({
  "floor.changed": ["owner", "reason"],
  "speech.started": [
    "speaker", "continued", "local_detected", "server_audio_received",
    "server_received_at", "acknowledged_audio_sequence", "audio_epoch",
    "duplicate", "media_transport", "ingress_sequence",
    "browser_backfill",
  ],
  "speech.stopped": ["speaker", "endpoint_countdown_ms", "cancellable"],
  "transcript.partial": ["text", "confidence", "server_received", "calibration", "ephemeral"],
  "transcript.final": [
    "text", "confidence", "authoritative", "persisted_audio", "calibration", "ephemeral",
  ],
  "conversation.act.selected": ["act_id", "act_type", "text", "evaluative", "approved"],
  "avatar.performance.started": [
    "performance_id", "turn_id", "audio_uri", "audio_clock_origin_ms",
    "text", "visemes", "gestures", "alignment_source", "delivery", "interruptible",
  ],
  "avatar.performance.cue": [
    "performance_id", "at_ms", "duration_ms", "shape", "weight", "gesture", "intensity",
  ],
  "avatar.performance.interrupted": ["performance_id", "reason", "deadline_ms"],
  "avatar.performance.stopped": ["performance_id", "reason"],
  "takeover.changed": ["status", "expires_at", "version", "ai_resumed"],
  problem: ["code", "message", "recoverable", "action", "calibration"],
  completed: [
    "interview_id", "status", "submitted_at", "recording_retention_notice", "human_review_required",
  ],
});

const ENTERPRISE_PAYLOAD_EXTENSIONS = Object.freeze({
  "conversation.act.selected": ["evidence_refs", "target_capability_points", "actor_id"],
  "takeover.changed": [
    "actor_id", "previous_actor_id", "reason", "lease_id",
    "media_permit_available", "media_participant_identity", "media_permit_generation",
    "media_permit_lease_version", "media_permit_issued_at", "media_permit_expires_at",
  ],
});

const CANDIDATE_SNAPSHOT_KEYS = Object.freeze([
  "interview_id", "status", "phase", "current_turn_id", "floor", "takeover",
  "calibration_status", "calibration_retry_required", "recording",
  "current_question", "completed_answers", "total_primary_questions",
]);

const ENTERPRISE_SNAPSHOT_KEYS = Object.freeze([
  ...CANDIDATE_SNAPSHOT_KEYS,
  "candidate", "active_performance_id", "problems", "media_capture",
]);

// These fields belong to enterprise-only or scoring projections. A candidate
// client treats their presence anywhere in an event payload as a projection
// breach instead of merely ignoring them.
const CANDIDATE_FORBIDDEN_KEYS = new Set([
  "actor_id", "previous_actor_id", "reason_internal", "lease_id",
  "media_permit_available", "media_permit_lease_version", "private_uri",
  "media_participant_identity", "media_permit_generation", "media_permit_issued_at",
  "media_permit_expires_at", "participant_identity", "egress_id", "content_hash", "encryption",
  "evidence_refs", "target_capability_points", "question_snapshot",
  "standard_answer", "scoring_rubric", "expected_answer", "provider_result",
]);

/**
 * Validate one server AgentEvent without coercion.
 *
 * @param {unknown} value
 * @param {{ audience?: "candidate" | "enterprise" }} options
 * @returns {{ ok: true, event: Record<string, any> } | { ok: false, code: string, message: string }}
 */
export function validateAgentEvent(value, { audience = "candidate" } = {}) {
  if (!isPlainObject(value)) return invalid("AGENT_EVENT_ENVELOPE_INVALID", "AgentEvent must be a plain object");
  if (!hasExactKeys(value, EVENT_ENVELOPE_KEYS)) {
    return invalid("AGENT_EVENT_ENVELOPE_INVALID", "AgentEvent envelope fields do not match the stable contract");
  }
  if (!boundedString(value.event_id, 1, 128)) return invalid("AGENT_EVENT_ID_INVALID", "event_id is invalid");
  if (!Number.isSafeInteger(value.session_sequence) || value.session_sequence < 1) {
    return invalid("AGENT_EVENT_SEQUENCE_INVALID", "session_sequence must be a positive safe integer");
  }
  if (!EVENT_TYPE_SET.has(value.type)) return invalid("AGENT_EVENT_TYPE_INVALID", "event type is not stable");
  if (!nullableBoundedString(value.turn_id, 128) || !nullableBoundedString(value.causation_id, 128)) {
    return invalid("AGENT_EVENT_CAUSATION_INVALID", "turn_id or causation_id is invalid");
  }
  if (!boundedString(value.occurred_at, 1, 64) || !Number.isFinite(Date.parse(value.occurred_at))) {
    return invalid("AGENT_EVENT_TIME_INVALID", "occurred_at is invalid");
  }
  if (!REPLAYABILITY.has(value.replayability)) {
    return invalid("AGENT_EVENT_REPLAYABILITY_INVALID", "replayability is invalid");
  }
  if (!isPlainObject(value.payload)) return invalid("AGENT_EVENT_PAYLOAD_INVALID", "payload must be a plain object");
  if (audience !== "candidate" && audience !== "enterprise") {
    return invalid("AGENT_EVENT_AUDIENCE_INVALID", "event audience is invalid");
  }
  if (audience === "candidate" && containsForbiddenKey(value.payload, CANDIDATE_FORBIDDEN_KEYS)) {
    return invalid("AGENT_EVENT_ROLE_PROJECTION_INVALID", "candidate event contains enterprise-only fields");
  }
  const payloadResult = validatePayload(value.type, value.payload, audience);
  if (!payloadResult.ok) return payloadResult;
  return { ok: true, event: value };
}

/**
 * Ordered, bounded AgentEvent replay guard. A non-snapshot gap moves the guard
 * into `awaiting_snapshot`; no later side effect is released until a fresh,
 * higher-sequence session.snapshot establishes a new authoritative cursor.
 */
export class OrderedAgentEventStream {
  constructor({ audience = "candidate", cursor = 0, maxSeenEventIds = 2048 } = {}) {
    if (!Number.isSafeInteger(cursor) || cursor < 0) throw new TypeError("cursor must be a non-negative safe integer");
    if (!Number.isSafeInteger(maxSeenEventIds) || maxSeenEventIds < 32 || maxSeenEventIds > 16_384) {
      throw new TypeError("maxSeenEventIds must be between 32 and 16384");
    }
    this.audience = audience;
    this.cursor = cursor;
    this.maxSeenEventIds = maxSeenEventIds;
    this.seenEventIds = new Set();
    this.seenEventOrder = [];
    this.awaitingSnapshot = false;
  }

  accept(value) {
    const validation = validateAgentEvent(value, { audience: this.audience });
    if (!validation.ok) {
      this.awaitingSnapshot = true;
      return { action: "resync", reason: "invalid", problem: validation };
    }
    const event = validation.event;
    if (this.seenEventIds.has(event.event_id)) {
      if (event.session_sequence > this.cursor) this.awaitingSnapshot = true;
      return {
        action: event.session_sequence > this.cursor ? "resync" : "drop",
        reason: "duplicate_event_id",
        event,
      };
    }
    if (event.session_sequence <= this.cursor) {
      this.remember(event.event_id);
      return { action: "drop", reason: "stale_or_replayed_sequence", event };
    }
    if (event.type === "session.snapshot") {
      const recovered = this.awaitingSnapshot || event.session_sequence !== this.cursor + 1;
      this.cursor = event.session_sequence;
      this.awaitingSnapshot = false;
      this.remember(event.event_id);
      return { action: "apply", reason: recovered ? "snapshot_recovered" : "ordered", event, recovered };
    }
    if (this.awaitingSnapshot) {
      return { action: "drop", reason: "awaiting_snapshot", event };
    }
    if (event.session_sequence !== this.cursor + 1) {
      this.awaitingSnapshot = true;
      return {
        action: "resync",
        reason: "sequence_gap",
        event,
        expectedSequence: this.cursor + 1,
        receivedSequence: event.session_sequence,
      };
    }
    this.cursor = event.session_sequence;
    this.remember(event.event_id);
    return { action: "apply", reason: "ordered", event, recovered: false };
  }

  remember(eventId) {
    if (this.seenEventIds.has(eventId)) return;
    this.seenEventIds.add(eventId);
    this.seenEventOrder.push(eventId);
    while (this.seenEventOrder.length > this.maxSeenEventIds) {
      this.seenEventIds.delete(this.seenEventOrder.shift());
    }
  }
}

function validatePayload(type, payload, audience) {
  if (type === "session.snapshot") return validateSnapshot(payload, audience);
  const allowed = [
    ...(BASE_PAYLOAD_KEYS[type] || []),
    ...(audience === "enterprise" ? ENTERPRISE_PAYLOAD_EXTENSIONS[type] || [] : []),
  ];
  if (!hasOnlyKeys(payload, allowed)) return invalid("AGENT_EVENT_PAYLOAD_INVALID", `${type} payload contains undeclared fields`);

  switch (type) {
    case "floor.changed":
      return FLOOR_OWNERS.has(payload.owner) && optionalString(payload.reason, 256)
        ? valid() : invalidPayload(type);
    case "speech.started":
      return SPEAKERS.has(payload.speaker)
        && optionalBooleanFields(payload, ["continued", "local_detected", "server_audio_received", "duplicate"])
        && optionalNonNegativeIntegers(payload, ["acknowledged_audio_sequence", "audio_epoch", "ingress_sequence"])
        && optionalString(payload.server_received_at, 64)
        && optionalString(payload.media_transport, 64)
        && optionalBrowserBackfill(payload.browser_backfill)
        ? valid() : invalidPayload(type);
    case "speech.stopped":
      return SPEAKERS.has(payload.speaker)
        && optionalNonNegativeIntegers(payload, ["endpoint_countdown_ms"])
        && optionalBooleanFields(payload, ["cancellable"])
        ? valid() : invalidPayload(type);
    case "transcript.partial":
    case "transcript.final":
      return boundedString(payload.text, 0, 16_000)
        && optionalConfidence(payload.confidence)
        && optionalBooleanFields(payload, [
          "server_received", "calibration", "authoritative", "persisted_audio", "ephemeral",
        ])
        ? valid() : invalidPayload(type);
    case "conversation.act.selected":
      return boundedString(payload.act_id, 1, 128)
        && boundedString(payload.act_type, 1, 64)
        && boundedString(payload.text, 1, 1_200)
        && typeof payload.approved === "boolean"
        && optionalBooleanFields(payload, ["evaluative"])
        && optionalString(payload.actor_id, 128)
        && optionalStringArray(payload.evidence_refs, 32, 1_200)
        && optionalStringArray(payload.target_capability_points, 32, 512)
        ? valid() : invalidPayload(type);
    case "avatar.performance.started":
      return validatePerformance(payload) ? valid() : invalidPayload(type);
    case "avatar.performance.cue":
      return boundedString(payload.performance_id, 1, 128)
        && nonNegativeInteger(payload.at_ms)
        && positiveInteger(payload.duration_ms, 60_000)
        && optionalEnum(payload.shape, VISEMES)
        && optionalUnitNumber(payload.weight)
        && optionalEnum(payload.gesture, GESTURES)
        && optionalUnitNumber(payload.intensity)
        ? valid() : invalidPayload(type);
    case "avatar.performance.interrupted":
      return optionalString(payload.performance_id, 128)
        && boundedString(payload.reason, 1, 256)
        && optionalNonNegativeIntegers(payload, ["deadline_ms"])
        ? valid() : invalidPayload(type);
    case "avatar.performance.stopped":
      return optionalString(payload.performance_id, 128)
        && optionalString(payload.reason, 256)
        ? valid() : invalidPayload(type);
    case "takeover.changed":
      return boundedString(payload.status, 1, 32)
        && optionalString(payload.expires_at, 64)
        && optionalNonNegativeIntegers(payload, ["version", "media_permit_generation", "media_permit_lease_version"])
        && optionalBooleanFields(payload, ["ai_resumed", "media_permit_available"])
        && optionalString(payload.actor_id, 128)
        && optionalString(payload.previous_actor_id, 128)
        && optionalString(payload.reason, 512)
        && optionalString(payload.lease_id, 128)
        && optionalString(payload.media_participant_identity, 160)
        && optionalString(payload.media_permit_issued_at, 64)
        && optionalString(payload.media_permit_expires_at, 64)
        ? valid() : invalidPayload(type);
    case "problem":
      return boundedString(payload.code, 1, 128)
        && boundedString(payload.message, 1, 1_000)
        && typeof payload.recoverable === "boolean"
        && optionalString(payload.action, 128)
        && optionalBooleanFields(payload, ["calibration"])
        ? valid() : invalidPayload(type);
    case "completed":
      return boundedString(payload.interview_id, 1, 128)
        && boundedString(payload.status, 1, 64)
        && optionalString(payload.submitted_at, 64)
        && optionalString(payload.recording_retention_notice, 2_000)
        && optionalBooleanFields(payload, ["human_review_required"])
        ? valid() : invalidPayload(type);
    default:
      return invalid("AGENT_EVENT_TYPE_INVALID", "event type is not stable");
  }
}

function validateSnapshot(payload, audience) {
  const allowed = audience === "candidate" ? CANDIDATE_SNAPSHOT_KEYS : ENTERPRISE_SNAPSHOT_KEYS;
  if (!hasOnlyKeys(payload, allowed)) return invalid("AGENT_EVENT_ROLE_PROJECTION_INVALID", "snapshot contains fields outside its audience projection");
  if (!boundedString(payload.interview_id, 1, 128)
    || !boundedString(payload.status, 1, 64)
    || !optionalString(payload.phase, 64)
    || !optionalString(payload.current_turn_id, 128)
    || !FLOOR_OWNERS.has(payload.floor)
    || !boundedString(payload.calibration_status, 1, 64)
    || !optionalBooleanFields(payload, ["calibration_retry_required"])
    || !nonNegativeInteger(payload.completed_answers)
    || !nonNegativeInteger(payload.total_primary_questions)) {
    return invalidPayload("session.snapshot");
  }
  if (!validateRecording(payload.recording)
    || !validateCurrentQuestion(payload.current_question)
    || !validateTakeover(payload.takeover, audience)) {
    return invalidPayload("session.snapshot");
  }
  if (audience === "enterprise") {
    if (!validateCandidate(payload.candidate)
      || !optionalString(payload.active_performance_id, 128)
      || !optionalObjectArray(payload.problems, 100)
      || !validateMediaCapture(payload.media_capture)) {
      return invalidPayload("session.snapshot");
    }
  }
  return valid();
}

function validatePerformance(payload) {
  if (!boundedString(payload.performance_id, 1, 128)
    || !optionalString(payload.turn_id, 128)
    || !boundedString(payload.audio_uri, 1, 2_048)
    || !safeAudioUri(payload.audio_uri)
    || !nonNegativeInteger(payload.audio_clock_origin_ms)
    || !boundedString(payload.text, 1, 1_200)
    || !ALIGNMENT_SOURCES.has(payload.alignment_source)
    || !EXPRESSION_DELIVERIES.has(payload.delivery)
    || !Array.isArray(payload.visemes) || payload.visemes.length < 1 || payload.visemes.length > 10_000
    || !Array.isArray(payload.gestures) || payload.gestures.length > 256
    || typeof payload.interruptible !== "boolean") return false;
  let previousAt = -1;
  for (const cue of payload.visemes) {
    if (!isPlainObject(cue)
      || !hasExactKeys(cue, ["at_ms", "duration_ms", "shape", "weight"])
      || !nonNegativeInteger(cue.at_ms) || cue.at_ms < previousAt
      || !positiveInteger(cue.duration_ms, 10_000)
      || !VISEMES.has(cue.shape) || !unitNumber(cue.weight)) return false;
    previousAt = cue.at_ms;
  }
  previousAt = -1;
  for (const cue of payload.gestures) {
    if (!isPlainObject(cue)
      || !hasExactKeys(cue, ["at_ms", "duration_ms", "gesture", "intensity"])
      || !nonNegativeInteger(cue.at_ms) || cue.at_ms < previousAt
      || !positiveInteger(cue.duration_ms, 60_000)
      || !GESTURES.has(cue.gesture) || !unitNumber(cue.intensity)) return false;
    previousAt = cue.at_ms;
  }
  return true;
}

function validateRecording(value) {
  return isPlainObject(value)
    && hasExactKeys(value, ["audio", "video", "status"])
    && typeof value.audio === "boolean"
    && typeof value.video === "boolean"
    && boundedString(value.status, 1, 64);
}

function validateCurrentQuestion(value) {
  return value === null || (isPlainObject(value)
    && hasExactKeys(value, ["turn_id", "order", "is_followup", "question_text"])
    && boundedString(value.turn_id, 1, 128)
    && nonNegativeInteger(value.order)
    && typeof value.is_followup === "boolean"
    && boundedString(value.question_text, 0, 4_000));
}

function validateTakeover(value, audience) {
  if (value === null) return true;
  if (!isPlainObject(value)) return false;
  const allowed = audience === "candidate"
    ? ["status", "expires_at", "version"]
    : ["status", "expires_at", "version", "actor_id", "reason", "lease_id", "media_permit_available"];
  return hasOnlyKeys(value, allowed)
    && boundedString(value.status, 1, 32)
    && optionalString(value.expires_at, 64)
    && optionalNonNegativeIntegers(value, ["version"])
    && optionalString(value.actor_id, 128)
    && optionalString(value.reason, 512)
    && optionalString(value.lease_id, 128)
    && optionalBooleanFields(value, ["media_permit_available"]);
}

function validateCandidate(value) {
  return value === undefined || value === null || (isPlainObject(value)
    && hasExactKeys(value, ["id", "name"])
    && optionalString(value.id, 128)
    && optionalString(value.name, 256));
}

function validateMediaCapture(value) {
  if (value === undefined || value === null) return true;
  if (!isPlainObject(value)) return false;
  const allowed = [
    "id", "status", "requested_scopes", "consented_scopes", "participant_identity",
    "egress_id", "private_uri", "content_hash", "encryption", "retention_expires_at",
    "started_at", "stopped_at", "failure_code",
  ];
  return hasOnlyKeys(value, allowed)
    && optionalString(value.id, 128)
    && optionalString(value.status, 64)
    && optionalStringArray(value.requested_scopes, 16, 64)
    && optionalStringArray(value.consented_scopes, 16, 64)
    && optionalString(value.participant_identity, 256)
    && optionalString(value.egress_id, 256)
    && optionalString(value.private_uri, 2_048)
    && optionalString(value.content_hash, 256)
    && optionalString(value.encryption, 128)
    && optionalString(value.retention_expires_at, 64)
    && optionalString(value.started_at, 64)
    && optionalString(value.stopped_at, 64)
    && optionalString(value.failure_code, 128);
}

function optionalObjectArray(value, maxLength) {
  if (value === undefined) return true;
  return Array.isArray(value) && value.length <= maxLength && value.every(isPlainObject);
}

function containsForbiddenKey(value, forbidden, depth = 0) {
  if (depth > 12) return true;
  if (Array.isArray(value)) return value.some((item) => containsForbiddenKey(item, forbidden, depth + 1));
  if (!isPlainObject(value)) return false;
  for (const [key, item] of Object.entries(value)) {
    if (forbidden.has(key) || containsForbiddenKey(item, forbidden, depth + 1)) return true;
  }
  return false;
}

function safeAudioUri(value) {
  try {
    const parsed = new URL(value, window.location.origin);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function hasExactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function hasOnlyKeys(value, allowed) {
  const keys = new Set(allowed);
  return Object.keys(value).every((key) => keys.has(key));
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function boundedString(value, minLength, maxLength) {
  return typeof value === "string" && value.length >= minLength && value.length <= maxLength;
}

function nullableBoundedString(value, maxLength) {
  return value === null || boundedString(value, 1, maxLength);
}

function optionalString(value, maxLength) {
  return value === undefined || value === null || boundedString(value, 0, maxLength);
}

function nonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function positiveInteger(value, maximum) {
  return Number.isSafeInteger(value) && value > 0 && value <= maximum;
}

function unitNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function optionalUnitNumber(value) {
  return value === undefined || unitNumber(value);
}

function optionalConfidence(value) {
  return value === undefined || unitNumber(value);
}

function optionalEnum(value, allowed) {
  return value === undefined || allowed.has(value);
}

function optionalBooleanFields(value, fields) {
  return fields.every((field) => value[field] === undefined || typeof value[field] === "boolean");
}

function optionalNonNegativeIntegers(value, fields) {
  return fields.every((field) => value[field] === undefined || value[field] === null || nonNegativeInteger(value[field]));
}

function optionalStringArray(value, maxItems, maxItemLength) {
  return value === undefined || (Array.isArray(value)
    && value.length <= maxItems
    && value.every((item) => boundedString(item, 0, maxItemLength)));
}

function optionalBrowserBackfill(value) {
  if (value === undefined) return true;
  if (!isPlainObject(value) || !hasOnlyKeys(value, [
    "status", "batch_id", "audio_epoch", "ack_through", "duplicate",
  ])) return false;
  return new Set(["ready", "acknowledged", "complete"]).has(value.status)
    && boundedString(value.batch_id, 1, 160)
    && boundedString(value.audio_epoch, 1, 160)
    && nonNegativeInteger(value.ack_through)
    && (value.duplicate === undefined || typeof value.duplicate === "boolean");
}

function valid() {
  return { ok: true };
}

function invalid(code, message) {
  return { ok: false, code, message };
}

function invalidPayload(type) {
  return invalid("AGENT_EVENT_PAYLOAD_INVALID", `${type} payload does not match the stable contract`);
}
