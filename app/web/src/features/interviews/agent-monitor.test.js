import { beforeEach, describe, expect, it, vi } from "vitest";

const rooms = [];

class FakeRoom {
  constructor() {
    this.handlers = new Map();
    this.connect = vi.fn(async () => {});
    this.disconnect = vi.fn(async () => {});
    this.localParticipant = {
      publishTrack: vi.fn(async () => {}),
      unpublishTrack: vi.fn(),
    };
    rooms.push(this);
  }

  on(event, handler) {
    this.handlers.set(event, handler);
    return this;
  }
}

class FakeLocalAudioTrack {
  constructor(track) {
    this.mediaStreamTrack = track;
  }
}

vi.mock("livekit-client", () => ({
  Room: FakeRoom,
  RoomEvent: {
    Reconnecting: "reconnecting",
    Reconnected: "reconnected",
    Disconnected: "disconnected",
    TrackSubscribed: "trackSubscribed",
    TrackUnsubscribed: "trackUnsubscribed",
  },
  LocalAudioTrack: FakeLocalAudioTrack,
  Track: { Source: { Microphone: "microphone" } },
}));

import { createEnterpriseInterviewMonitor } from "./agent-monitor.js";

class FakeSocket {
  constructor() {
    this.readyState = WebSocket.CONNECTING;
    this.handlers = new Map();
    this.sent = [];
  }

  addEventListener(type, handler) {
    const values = this.handlers.get(type) || [];
    values.push(handler);
    this.handlers.set(type, values);
    if (type === "open") {
      queueMicrotask(() => {
        this.readyState = WebSocket.OPEN;
        handler({});
      });
    }
  }

  send(value) {
    this.sent.push(JSON.parse(value));
  }

  close() {
    this.readyState = WebSocket.CLOSED;
  }

  emit(type, value) {
    for (const handler of this.handlers.get(type) || []) handler(value);
  }
}

describe("enterprise takeover media contracts", () => {
  beforeEach(() => {
    rooms.length = 0;
    globalThis.MediaStream = class {
      constructor() { this.tracks = []; }
      addTrack(track) { this.tracks.push(track); }
      removeTrack(track) { this.tracks = this.tracks.filter((item) => item !== track); }
      getTracks() { return [...this.tracks]; }
    };
  });

  it("keeps the base room subscribe-only and exchanges an active lease for microphone media", async () => {
    const socket = new FakeSocket();
    const audioTrack = { stop: vi.fn() };
    const getUserMedia = vi.fn(async () => ({
      getAudioTracks: () => [audioTrack],
      getTracks: () => [audioTrack],
    }));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const request = vi.fn(async (url, options) => {
      if (url.endsWith("/agent-ticket")) {
        return {
          agent_ticket: "agent-ticket",
          agent_ws_url: "/api/v1/interviews/iv_1/agent",
          media: {
            status: "ready",
            url: "wss://livekit.test",
            participant_token: "observe-only-token",
          },
        };
      }
      if (url.endsWith("/takeover/media-permit")) {
        expect(options.body).toEqual({ lease_id: "lease_1", expected_version: 1 });
        return {
          media: {
            status: "ready",
            url: "wss://livekit.test",
            participant_token: "microphone-only-token",
          },
        };
      }
      throw new Error(`unexpected request ${url}`);
    });
    const monitor = createEnterpriseInterviewMonitor({
      request,
      socketFactory: () => socket,
    });
    const run = await monitor.open({
      interviewId: "iv_1",
      actorId: "interviewer_1",
      canTakeover: true,
    });

    expect(rooms).toHaveLength(1);
    expect(rooms[0].connect).toHaveBeenCalledWith(
      "wss://livekit.test",
      "observe-only-token",
      { autoSubscribe: true },
    );
    socket.emit("message", {
      data: JSON.stringify(agentEvent(1, "session.snapshot", enterpriseSnapshot())),
    });
    await run.acquireTakeover("候选人需要人工澄清");
    expect(getUserMedia).not.toHaveBeenCalled();

    socket.emit("message", {
      data: JSON.stringify(agentEvent(2, "takeover.changed", {
          status: "active",
          actor_id: "interviewer_1",
          lease_id: "lease_1",
          version: 1,
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          reason: "候选人需要人工澄清",
          media_permit_available: true,
        })),
    });
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(getUserMedia).toHaveBeenCalledOnce();
    expect(rooms).toHaveLength(2);
    expect(rooms[1].connect).toHaveBeenCalledWith(
      "wss://livekit.test",
      "microphone-only-token",
      { autoSubscribe: false },
    );
    expect(rooms[1].localParticipant.publishTrack).toHaveBeenCalledOnce();

    socket.emit("message", {
      data: JSON.stringify(agentEvent(3, "takeover.changed", { status: "lost", ai_resumed: false })),
    });
    expect(audioTrack.stop).toHaveBeenCalled();
    expect(rooms[1].disconnect).toHaveBeenCalled();
    await run.close();
  });
});

function agentEvent(sequence, type, payload) {
  return {
    event_id: `monitor_event_${sequence}`,
    session_sequence: sequence,
    type,
    turn_id: null,
    causation_id: null,
    occurred_at: "2026-09-01T12:00:00Z",
    replayability: type === "session.snapshot" ? "transient" : "replayable",
    payload,
  };
}

function enterpriseSnapshot() {
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
    candidate: { id: "candidate_1", name: "候选人" },
    active_performance_id: null,
    problems: [],
    media_capture: null,
  };
}
