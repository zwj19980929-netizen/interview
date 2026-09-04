import { describe, expect, it } from "vitest";

import { OrderedAgentEventStream, validateAgentEvent } from "./agent-event-runtime.js";

describe("ordered AgentEvent runtime", () => {
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
