import { describe, expect, it } from "vitest";

import { OrderedAgentEventStream, validateAgentEvent } from "./agent-event-runtime.js";

describe("ordered AgentEvent runtime", () => {
  it("accepts unknown transcript confidence without converting it into certainty or a protocol failure", () => {
    const stream = new OrderedAgentEventStream({ audience: "candidate" });
    const received = event(1, "transcript.final", { text: "合成术语回答。", confidence: null, authoritative: true, persisted_audio: true });
    expect(stream.accept(received).action).toBe("apply");
    expect(received.payload.confidence).toBeNull();
  });
  it("deduplicates and drops replayed avatar performances", () => {
    const stream = new OrderedAgentEventStream({ audience: "candidate" });
    const started = event(1, "avatar.performance.started", performance());

    expect(stream.accept(started).action).toBe("apply");
    expect(stream.accept(started)).toMatchObject({ action: "drop", reason: "duplicate_event_id" });
    expect(stream.accept({ ...started, event_id: "event_replayed_with_new_id" })).toMatchObject({
      action: "drop",
      reason: "stale_or_replayed_sequence",
    });
    expect(stream.cursor).toBe(1);
  });

  it("does not release an out-of-order effect and recovers only from a fresh snapshot", () => {
    const stream = new OrderedAgentEventStream({ audience: "candidate" });

    expect(stream.accept(event(2, "avatar.performance.started", performance()))).toMatchObject({
      action: "resync",
      reason: "sequence_gap",
      expectedSequence: 1,
      receivedSequence: 2,
    });
    expect(stream.accept(event(3, "conversation.act.selected", act()))).toMatchObject({
      action: "drop",
      reason: "awaiting_snapshot",
    });
    const recovered = stream.accept(event(4, "session.snapshot", candidateSnapshot()));
    expect(recovered).toMatchObject({ action: "apply", reason: "snapshot_recovered", recovered: true });
    expect(stream.cursor).toBe(4);
    expect(stream.accept(event(5, "floor.changed", { owner: "candidate", reason: "agent_finished" })).action).toBe("apply");
  });

  it("rejects malformed and candidate role-leaking payloads without advancing the cursor", () => {
    const candidate = new OrderedAgentEventStream({ audience: "candidate" });
    const leaked = event(1, "conversation.act.selected", {
      ...act(),
      target_capability_points: ["secret scoring point"],
    });
    expect(candidate.accept(leaked)).toMatchObject({
      action: "resync",
      reason: "invalid",
      problem: { code: "AGENT_EVENT_ROLE_PROJECTION_INVALID" },
    });
    expect(candidate.cursor).toBe(0);

    const invalidEnvelope = { ...event(1, "floor.changed", { owner: "candidate" }), organization_id: "org_secret" };
    expect(validateAgentEvent(invalidEnvelope, { audience: "candidate" })).toMatchObject({
      ok: false,
      code: "AGENT_EVENT_ENVELOPE_INVALID",
    });

    const enterprise = new OrderedAgentEventStream({ audience: "enterprise" });
    expect(enterprise.accept(leaked).action).toBe("apply");
  });

  it("bounds remembered event ids", () => {
    const stream = new OrderedAgentEventStream({ audience: "candidate", maxSeenEventIds: 32 });
    for (let sequence = 1; sequence <= 40; sequence += 1) {
      expect(stream.accept(event(sequence, "floor.changed", { owner: "candidate", reason: "test" })).action).toBe("apply");
    }
    expect(stream.seenEventIds.size).toBe(32);
    expect(stream.seenEventIds.has("event_1")).toBe(false);
    expect(stream.seenEventIds.has("event_40")).toBe(true);
  });

  it("accepts the bounded browser backfill acknowledgement projection", () => {
    const stream = new OrderedAgentEventStream({ audience: "candidate" });
    expect(stream.accept(event(1, "speech.started", {
      speaker: "candidate",
      server_audio_received: true,
      media_transport: "browser_backfill",
      browser_backfill: {
        status: "acknowledged",
        batch_id: "browser_backfill_abc",
        audio_epoch: "epoch_abc",
        ack_through: 17,
        duplicate: false,
      },
    })).action).toBe("apply");
  });

  it("requires the server-declared viseme alignment provenance", () => {
    expect(validateAgentEvent(event(1, "avatar.performance.started", performance()), {
      audience: "candidate",
    }).ok).toBe(true);
    expect(validateAgentEvent(event(1, "avatar.performance.started", {
      ...performance(),
      alignment_source: "amplitude_estimate",
    }), { audience: "candidate" })).toMatchObject({
      ok: false,
      code: "AGENT_EVENT_PAYLOAD_INVALID",
    });
  });

  it("accepts only a complete LiveKit binding with no URI for streaming TTS", () => {
    const live = {
      ...performance(), audio_uri: null, delivery: "streaming_tts",
      live_audio: { output_id: "output_1", publisher_identity: "expression:synthetic",
        track_sid: "TR_one", track_name: "speech_output_1", sample_rate_hz: 24_000, channels: 1 },
    };
    expect(validateAgentEvent(event(1, "avatar.performance.started", live)).ok).toBe(true);
    for (const change of [
      { audio_uri: "https://provider.example/private.wav" },
      { live_audio: null },
      { live_audio: { ...live.live_audio, participant_token: "secret" } },
      { live_audio: { ...live.live_audio, output_id: "" } },
      { live_audio: { ...live.live_audio, sample_rate_hz: 16_000 } },
      { live_audio: { ...live.live_audio, channels: 2 } },
      { delivery: "cascade", audio_uri: "/managed.wav" },
    ]) {
      expect(validateAgentEvent(event(1, "avatar.performance.started", { ...live, ...change })).ok).toBe(false);
    }
  });

  it("validates producer EOF without allowing client text, PCM, URLs, or coercion", () => {
    const finished = { performance_id: "performance_1", output_id: "output_1", total_samples: 48_000, sample_rate_hz: 24_000 };
    expect(validateAgentEvent(event(2, "avatar.performance.producer_finished", finished)).ok).toBe(true);
    for (const change of [
      { total_samples: "48000" }, { total_samples: 0 }, { total_samples: -1 },
      { total_samples: 14_400_001 }, { sample_rate_hz: 48_000 }, { output_id: "" },
      { audio_base64: "AA==" }, { provider_url: "https://provider.example" },
    ]) expect(validateAgentEvent(event(2, "avatar.performance.producer_finished", { ...finished, ...change })).ok).toBe(false);
  });

  it("accepts optional current performance identity in a candidate snapshot", () => {
    for (const id of [undefined, null, "performance_current"]) {
      expect(validateAgentEvent(event(1, "session.snapshot", { ...candidateSnapshot(), active_performance_id: id })).ok).toBe(true);
    }
    expect(validateAgentEvent(event(1, "session.snapshot", { ...candidateSnapshot(), active_performance_id: 7 })).ok).toBe(false);
  });

  it("strictly validates the scoped capture recovery snapshot without internal diagnostics", () => {
    const recovery = { status: "recovering", turn_id: "turn_1", capture_id: "capture_1", attempt: 1, max_attempts: 3 };
    const snapshot = { ...candidateSnapshot(), current_turn_id: "turn_1" };
    for (const capture_recovery of [undefined, null, recovery, { ...recovery, status: "retry_required", attempt: 3 }]) {
      expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, capture_recovery })).ok).toBe(true);
    }
    for (const change of [
      { stage: "provider_snapshot" }, { cause_code: "provider_timeout" }, { attempt: "1" },
      { attempt: -1 }, { attempt: 4 }, { max_attempts: 4 }, { capture_id: "" }, { capture_id: "bad scope" },
      { turn_id: "turn_old" }, { status: "auto_resume" },
    ]) expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, capture_recovery: { ...recovery, ...change } })).ok).toBe(false);
    expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, current_question: null, capture_recovery: recovery })).ok).toBe(false);
  });

  it("validates only safe fields in the current turn's supplement confirmation", () => {
    const snapshot = { ...candidateSnapshot(), current_turn_id: "turn_1" };
    const confirmation = { status: "awaiting_reply", turn_id: "turn_1", capture_id: "capture_1" };
    expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, supplement_confirmation: confirmation })).ok).toBe(true);
    for (const extra of [{ boundary: "private transcript" }, { turn_id: "turn_old" }, { status: "finished" }, { capture_id: "" }]) {
      expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, supplement_confirmation: { ...confirmation, ...extra } })).ok).toBe(false);
    }
  });

  it("accepts only scoped candidate preparation facts without granting a capture or exposing internal work", () => {
    const snapshot = { ...candidateSnapshot(), floor: "candidate", current_turn_id: "turn_1" };
    const preparation = { status: "preparing", turn_id: "turn_1", capture_id: "capture_1" };
    for (const answer_preparation of [undefined, null, preparation]) {
      expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, answer_preparation })).ok).toBe(true);
    }
    for (const change of [{ status: "done" }, { turn_id: "turn_old" }, { capture_id: "" },
      { capture_id: "bad scope" }, { ready: true }, { transcript: "private" }, { deadline_ms: 0 }]) {
      expect(validateAgentEvent(event(1, "session.snapshot", {
        ...snapshot, answer_preparation: { ...preparation, ...change },
      })).ok).toBe(false);
    }
    for (const change of [{ status: "paused" }, { floor: "agent" }, { calibration_status: "listening" },
      { capture_recovery: { status: "recovering", turn_id: "turn_1", capture_id: "capture_1", attempt: 1, max_attempts: 3 } }]) {
      expect(validateAgentEvent(event(1, "session.snapshot", { ...snapshot, ...change, answer_preparation: preparation })).ok).toBe(false);
    }
  });

  it("requires capture and turn scope with the correct floor/action for recovery events", () => {
    const recoveryEvents = [
      ["floor.changed", { owner: "candidate", reason: "answer_recovering", capture_id: "capture_1" }],
      ["floor.changed", { owner: "none", reason: "answer_retry_required", capture_id: "capture_1" }],
      ["problem", { code: "CAPTURE_RECOVERING", message: "正在恢复", recoverable: true, action: "continue_listening", capture_id: "capture_1" }],
      ["problem", { code: "CAPTURE_RETRY_REQUIRED", message: "请重试本题", recoverable: true, action: "retry_answer", capture_id: "capture_1" }],
    ];
    for (const [type, payload] of recoveryEvents) {
      const item = { ...event(1, type, payload), turn_id: "turn_1" };
      expect(validateAgentEvent(item).ok).toBe(true);
      expect(validateAgentEvent({ ...item, turn_id: null }).ok).toBe(false);
      expect(validateAgentEvent({ ...item, payload: { ...payload, capture_id: undefined } }).ok).toBe(false);
      expect(validateAgentEvent({ ...item, payload: { ...payload, cause_type: "ProviderError" } }).ok).toBe(false);
      expect(validateAgentEvent({ ...item, payload: { ...payload, ...(type === "problem" ? { action: "pause_or_human_takeover" } : { owner: "human" }) } }).ok).toBe(false);
    }
  });
});

function event(sequence, type, payload) {
  return {
    event_id: `event_${sequence}`,
    session_sequence: sequence,
    type,
    turn_id: type.startsWith("avatar") || type.startsWith("conversation") ? "turn_1" : null,
    causation_id: null,
    occurred_at: "2026-09-01T12:00:00Z",
    replayability: type === "session.snapshot" ? "transient" : "replayable",
    payload,
  };
}

function act() {
  return {
    act_id: "act_1",
    act_type: "question",
    text: "请介绍你的方案。",
    evaluative: false,
    approved: true,
  };
}

function performance() {
  return {
    performance_id: "performance_1",
    turn_id: "turn_1",
    audio_uri: "/api/v1/media/speech_1",
    audio_clock_origin_ms: 0,
    text: "请介绍你的方案。",
    visemes: [{ at_ms: 0, duration_ms: 120, shape: "aa", weight: 0.8 }],
    gestures: [{ at_ms: 0, duration_ms: 500, gesture: "look_at_candidate", intensity: 0.6 }],
    alignment_source: "g2p_estimate",
    delivery: "cascade",
    interruptible: true,
  };
}

function candidateSnapshot() {
  return {
    interview_id: "iv_1",
    status: "in_progress",
    phase: "questioning",
    current_turn_id: "turn_1",
    floor: "candidate",
    takeover: null,
    calibration_status: "completed",
    recording: { audio: true, video: true, status: "recording" },
    current_question: {
      turn_id: "turn_1",
      order: 1,
      is_followup: false,
      question_text: "请介绍你的方案。",
    },
    completed_answers: 0,
    total_primary_questions: 6,
  };
}
