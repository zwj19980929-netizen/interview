import { describe, expect, it, vi } from "vitest";

import {
  createCandidateInterviewExperience,
  isAuthorizedTakeoverAudioParticipant,
  measurePublishedTrackNetwork,
  reportCandidateRuntimeProblem,
} from "./agent-experience.js";
import {
  claimPreparedCandidateMedia,
  peekPreparedCandidateMedia,
  prepareCandidateMedia,
} from "./preflight.js";
import {
  advanceAvatarFpsGate,
  applyInterviewPresentationPose,
  verifyLicensedVrmAsset,
} from "./vrm-avatar.js";

describe("candidate real-time experience contracts", () => {
  it("uses published LiveKit RTCStats instead of the HTTP preflight as the formal network gate", async () => {
    const reports = [0.08, 0.10, 0.09].map((roundTripTime) => new Map([
      ["pair", {
        type: "candidate-pair",
        state: "succeeded",
        nominated: true,
        currentRoundTripTime: roundTripTime,
      }],
    ]));
    const track = {
      getRTCStatsReport: vi.fn(async () => reports.shift()),
    };
    await expect(measurePublishedTrackNetwork(track, {
      attempts: 3,
      intervalMs: 0,
    })).resolves.toEqual({ rttMs: 90, jitterMs: 15 });
    expect(track.getRTCStatsReport).toHaveBeenCalledTimes(3);
    await expect(measurePublishedTrackNetwork({}, {
      attempts: 1,
      intervalMs: 0,
    })).rejects.toThrow("RTCStats");
  });

  it("opens the formal room only after three healthy real-VRM FPS windows", () => {
    let gate = { slowWindows: 0, healthyWindows: 0 };
    gate = advanceAvatarFpsGate(gate, 45);
    expect(gate.ready).toBe(false);
    gate = advanceAvatarFpsGate(gate, 42);
    expect(gate.ready).toBe(false);
    gate = advanceAvatarFpsGate(gate, 38);
    expect(gate.ready).toBe(true);

    gate = { slowWindows: 0, healthyWindows: 0 };
    gate = advanceAvatarFpsGate(gate, 22);
    gate = advanceAvatarFpsGate(gate, 25);
    gate = advanceAvatarFpsGate(gate, 29);
    expect(gate.failed).toBe(true);

    gate = { slowWindows: 0, healthyWindows: 0 };
    gate = advanceAvatarFpsGate(gate, 29.5);
    gate = advanceAvatarFpsGate(gate, 29.5);
    gate = advanceAvatarFpsGate(gate, 29.5);
    expect(gate.ready).toBe(true);

    gate = { slowWindows: 0, healthyWindows: 0 };
    gate = advanceAvatarFpsGate(gate, 29.4);
    gate = advanceAvatarFpsGate(gate, 29.4);
    gate = advanceAvatarFpsGate(gate, 29.4);
    expect(gate.failed).toBe(true);
  });

  it("converts the exported VRM T-pose into a restrained interview pose", () => {
    const bones = Object.fromEntries([
      "leftUpperArm",
      "rightUpperArm",
      "leftLowerArm",
      "rightLowerArm",
    ].map((name) => [name, { rotation: { x: 0, y: 0, z: 0 } }]));
    const humanoid = {
      getNormalizedBoneNode: vi.fn((name) => bones[name] || null),
    };

    expect(applyInterviewPresentationPose(
      humanoid,
      { speaking: true, gestureIntensity: 0.6 },
      420,
    )).toBe(true);
    expect(bones.leftUpperArm.rotation.z).toBeCloseTo(-1.12);
    expect(bones.rightUpperArm.rotation.z).toBeCloseTo(1.12);
    expect(bones.leftLowerArm.rotation.z).toBeCloseTo(0.12);
    expect(bones.rightLowerArm.rotation.z).toBeCloseTo(-0.12);
    expect(bones.leftUpperArm.rotation.x).not.toBe(
      bones.rightUpperArm.rotation.x,
    );
  });

  it("subscribes only to the server-authorized takeover microphone identity", () => {
    expect(isAuthorizedTakeoverAudioParticipant({
      identity: "takeover:lease_1",
      metadata: JSON.stringify({ role: "takeover" }),
    })).toBe(true);
    expect(isAuthorizedTakeoverAudioParticipant({
      identity: "candidate:connection_1",
      metadata: JSON.stringify({ role: "candidate" }),
    })).toBe(false);
    expect(isAuthorizedTakeoverAudioParticipant({
      identity: "takeover:forged",
      metadata: JSON.stringify({ role: "candidate" }),
    })).toBe(false);
    expect(isAuthorizedTakeoverAudioParticipant({
      identity: "human:legacy",
      metadata: JSON.stringify({ role: "takeover" }),
    })).toBe(false);
  });

  it("lets the room preview prepared media without consuming facade ownership", () => {
    const stream = {
      getAudioTracks: () => [{ readyState: "live" }],
      getVideoTracks: () => [{ readyState: "live" }],
      getTracks: () => [],
    };
    prepareCandidateMedia(stream, { speaker_verified: true });
    expect(peekPreparedCandidateMedia().stream).toBe(stream);
    expect(claimPreparedCandidateMedia().stream).toBe(stream);
    expect(peekPreparedCandidateMedia()).toBeNull();
  });

  it("accepts only the server-validated VRM 1.0 contract", async () => {
    const config = {
      ready: true,
      renderer: "three-vrm-local",
      vrm_spec: "1.0",
      asset_url: "/api/v1/public/interviews/iv_test/avatar-model?grant=signed",
      asset_sha256: "a".repeat(64),
      asset_grant_expires_at: Math.floor(Date.now() / 1000) + 60,
      visemes: ["sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "ih", "oh", "ou"],
      minimum_fps: 30,
      amplitude_lipsync_formal: false,
    };
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => config }));
    const verified = await verifyLicensedVrmAsset({
      interviewId: "iv_test",
      candidateSessionToken: "candidate-token",
      fetchImpl,
    });
    expect(verified.asset_url).toBe(config.asset_url);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/public/interviews/iv_test/avatar-config",
      expect.objectContaining({
        headers: { "X-Candidate-Session-Token": "candidate-token" },
      }),
    );
    await expect(verifyLicensedVrmAsset({
      interviewId: "iv_test",
      candidateSessionToken: "candidate-token",
      fetchImpl: async () => ({ ok: true, json: async () => ({ ...config, amplitude_lipsync_formal: true }) }),
    })).rejects.toThrow("VRM 1.0");

    const unavailable = verifyLicensedVrmAsset({
      interviewId: "iv_test",
      candidateSessionToken: "candidate-token",
      fetchImpl: async () => ({
        ok: false,
        status: 503,
        json: async () => ({ error: { code: "LICENSED_VRM_NOT_READY" } }),
      }),
    });
    await expect(unavailable).rejects.toMatchObject({
      code: "LICENSED_VRM_NOT_READY",
      candidateProblemCode: "AVATAR_ASSET_UNAVAILABLE",
      message: expect.stringContaining("授权或完整性"),
    });
  });

  it("reports a narrow candidate runtime problem through the server pause endpoint", async () => {
    const request = vi.fn(async () => ({ status: "paused", accepted: true }));
    await expect(reportCandidateRuntimeProblem({
      apiBase: "/api/v1",
      request,
      interviewId: "iv_test",
      candidateSessionToken: "candidate-token",
      code: "AVATAR_RENDERER_FAILED",
    })).resolves.toMatchObject({ status: "paused" });
    expect(request).toHaveBeenCalledWith(
      "/api/v1/public/interviews/iv_test/runtime-problems",
      {
        method: "POST",
        headers: { "X-Candidate-Session-Token": "candidate-token" },
        body: { code: "AVATAR_RENDERER_FAILED" },
      },
    );
  });

  it("fails the formal room closed instead of silently accepting PCM recovery", async () => {
    const stop = vi.fn();
    const stream = {
      getAudioTracks: () => [{ readyState: "live", stop }],
      getVideoTracks: () => [{ readyState: "live", stop }],
      getTracks: () => [{ stop }],
    };
    prepareCandidateMedia(stream, {
      speaker_verified: true,
      microphone_granted: true,
      camera_granted: true,
    });
    const experience = createCandidateInterviewExperience({
      request: async () => ({
        agent_ticket: "agt.test",
        agent_ws_url: "/api/v1/interviews/iv_test/agent",
        media: {
          status: "unavailable",
          recovery: {
            server_checkpoint: { enabled: true, mode: "durable_media_checkpoint" },
            browser_backfill: { enabled: true, protocol: "agent-json-backfill.v1" },
          },
        },
      }),
      verifyAvatar: async () => ({ ready: true }),
      ringFactory: () => ({
        clear: vi.fn(),
        snapshot: () => ({
          retainedFrames: 0,
          unacknowledgedFrames: 0,
          encryptedBytes: 0,
          retentionMs: 30_000,
        }),
      }),
    });
    await expect(
      experience.open({ interviewId: "iv_test", ticket: "candidate-token" }),
    ).rejects.toThrow("不会自动降级");
    expect(stop).toHaveBeenCalled();
  });

  it("executes an ordered avatar performance once and drops its replay", async () => {
    const socket = new CandidateFakeSocket();
    const stream = {
      getAudioTracks: () => [{ readyState: "live", stop: vi.fn() }],
      getVideoTracks: () => [{ readyState: "live", stop: vi.fn() }],
      getTracks: () => [],
    };
    prepareCandidateMedia(stream, {
      speaker_verified: true,
      microphone_granted: true,
      camera_granted: true,
      webrtc_supported: true,
      audio_worklet_supported: true,
      webgl_supported: true,
      media_recorder_supported: true,
      avatar_fps: 60,
    });
    const audio = {
      currentTime: 0,
      src: "",
      preload: "",
      addEventListener: vi.fn(),
      play: vi.fn(async () => {}),
      pause: vi.fn(),
    };
    const audioFactory = vi.fn(() => audio);
    const experience = createCandidateInterviewExperience({
      request: async () => ({
        agent_ticket: "agt.test",
        agent_ws_url: "/api/v1/interviews/iv_test/agent",
        media: {
          status: "ready",
          url: "wss://livekit.test",
          participant_token: "candidate-media-ticket",
          video_upstream_allowed: true,
          recovery: {
            server_checkpoint: { enabled: true, mode: "durable_media_checkpoint" },
            browser_backfill: { enabled: true, protocol: "agent-json-backfill.v1" },
          },
        },
      }),
      socketFactory: () => socket,
      mediaConnector: async () => ({ publishedSources: ["microphone", "camera"], close: vi.fn() }),
      audioCaptureFactory: async () => ({ close: vi.fn() }),
      ringFactory: () => ({
        append: vi.fn(),
        clear: vi.fn(),
        snapshot: () => ({
          retainedFrames: 0,
          unacknowledgedFrames: 0,
          encryptedBytes: 0,
          retentionMs: 30_000,
        }),
      }),
      verifyAvatar: async () => ({ ready: true }),
      audioFactory,
    });
    const run = await experience.open({ interviewId: "iv_test", ticket: "candidate-token" });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    const started = candidateEvent(2, "avatar.performance.started", candidatePerformance());
    socket.emit("message", { data: JSON.stringify(started) });
    socket.emit("message", { data: JSON.stringify(started) });
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(audioFactory).toHaveBeenCalledOnce();
    expect(audio.play).toHaveBeenCalledOnce();
    expect(run.getSnapshot().problem).toBeNull();
    await run.close();
  });

  it("treats local barge-in cleanup callbacks as stale instead of a formal audio failure", async () => {
    const audio = new CandidateFakeAudio();
    const { run, socket, capture, request } = await openPlaybackHarness({ audios: [audio] });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
    await flushPlaybackEvents();
    expect(run.getSnapshot().avatar).toMatchObject({ status: "speaking", performanceId: "performance_1" });

    const staleError = audio.listener("error");
    const staleEnded = audio.listener("ended");
    await capture.onSpeechStarted();
    expect(socket.sent.filter((item) => item.type === "speech.started")).toHaveLength(1);
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
    expect(run.getSnapshot().phase).toBe("preparing");
    staleError();
    staleEnded();
    await flushPlaybackEvents();

    expect(audio.pause).toHaveBeenCalledOnce();
    expect(run.getSnapshot().avatar.status).toBe("idle");
    expect(run.getSnapshot().problem).toBeNull();
    expect(runtimeProblemCalls(request)).toHaveLength(0);
    expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toHaveLength(0);
    await run.close();
  });

  it("isolates replacement, natural end, and server interrupt callbacks by performance identity", async () => {
    const first = new CandidateFakeAudio();
    const second = new CandidateFakeAudio();
    const third = new CandidateFakeAudio();
    const { run, socket, request } = await openPlaybackHarness({ audios: [first, second, third] });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
    await flushPlaybackEvents();
    const staleFirstPlay = first.listener("play");
    const staleFirstError = first.listener("error");
    const staleFirstEnded = first.listener("ended");

    socket.emit("message", {
      data: JSON.stringify(candidateEvent(3, "avatar.performance.started", {
        ...candidatePerformance(),
        performance_id: "performance_2",
        audio_uri: "/api/v1/media/speech_2",
      })),
    });
    await flushPlaybackEvents();
    staleFirstPlay();
    staleFirstError();
    staleFirstEnded();
    socket.emit("message", {
      data: JSON.stringify(candidateEvent(4, "avatar.performance.interrupted", {
        performance_id: "performance_1",
        reason: "late_server_interrupt",
        deadline_ms: 120,
      })),
    });
    await flushPlaybackEvents();

    expect(second.pause).not.toHaveBeenCalled();
    expect(run.getSnapshot().avatar).toMatchObject({ status: "speaking", performanceId: "performance_2" });
    expect(run.getSnapshot().problem).toBeNull();

    second.emit("ended");
    await flushPlaybackEvents();
    expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toEqual([
      expect.objectContaining({ payload: { performance_id: "performance_2" } }),
    ]);

    socket.emit("message", {
      data: JSON.stringify(candidateEvent(5, "avatar.performance.started", {
        ...candidatePerformance(),
        performance_id: "performance_3",
        audio_uri: "/api/v1/media/speech_3",
      })),
    });
    await flushPlaybackEvents();
    const staleThirdError = third.listener("error");
    const staleThirdEnded = third.listener("ended");
    socket.emit("message", {
      data: JSON.stringify(candidateEvent(6, "avatar.performance.interrupted", {
        performance_id: "performance_3",
        reason: "candidate_barge_in",
        deadline_ms: 120,
      })),
    });
    await flushPlaybackEvents();
    staleThirdError();
    staleThirdEnded();
    await flushPlaybackEvents();

    expect(third.pause).toHaveBeenCalledOnce();
    expect(run.getSnapshot().avatar.status).toBe("idle");
    expect(run.getSnapshot().problem).toBeNull();
    expect(runtimeProblemCalls(request)).toHaveLength(0);
    expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toHaveLength(1);
    await run.close();
  });

  it("ignores a delayed play rejection after that performance has been replaced", async () => {
    let rejectFirstPlay;
    const firstPlay = new Promise((_, reject) => { rejectFirstPlay = reject; });
    const first = new CandidateFakeAudio({ playResult: firstPlay, emitPlay: false });
    const second = new CandidateFakeAudio();
    const { run, socket, request } = await openPlaybackHarness({ audios: [first, second] });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
    socket.emit("message", {
      data: JSON.stringify(candidateEvent(3, "avatar.performance.started", {
        ...candidatePerformance(),
        performance_id: "performance_2",
        audio_uri: "/api/v1/media/speech_2",
      })),
    });
    await flushPlaybackEvents();

    rejectFirstPlay(new Error("stale playback was aborted"));
    await flushPlaybackEvents();

    expect(run.getSnapshot().avatar).toMatchObject({ status: "speaking", performanceId: "performance_2" });
    expect(run.getSnapshot().problem).toBeNull();
    expect(runtimeProblemCalls(request)).toHaveLength(0);
    await run.close();
  });

  it("still fails closed when the current formal audio emits a real error", async () => {
    const audio = new CandidateFakeAudio();
    const { run, socket, request } = await openPlaybackHarness({ audios: [audio] });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
    await flushPlaybackEvents();

    audio.emit("error");
    await flushPlaybackEvents();

    expect(run.getSnapshot()).toMatchObject({
      phase: "paused",
      problem: {
        code: "CANDIDATE_EXPERIENCE_FATAL",
        message: "数字人正式语音无法播放",
        pauseConfirmed: true,
      },
    });
    expect(runtimeProblemCalls(request)).toHaveLength(1);
    expect(runtimeProblemCalls(request)[0][1]).toMatchObject({
      method: "POST",
      body: { code: "CANDIDATE_RUNTIME_FAILED" },
    });
    await run.close();
  });

  it("replays a detected media gap over bounded JSON and delays endpointing", async () => {
    const socket = new CandidateFakeSocket();
    const stream = {
      getAudioTracks: () => [{ readyState: "live", stop: vi.fn() }],
      getVideoTracks: () => [{ readyState: "live", stop: vi.fn() }],
      getTracks: () => [],
    };
    prepareCandidateMedia(stream, {
      speaker_verified: true,
      microphone_granted: true,
      camera_granted: true,
      webrtc_supported: true,
      audio_worklet_supported: true,
      webgl_supported: true,
      media_recorder_supported: true,
      avatar_fps: 60,
    });
    const retained = [];
    const acknowledged = new Set();
    const ring = {
      async append(sequence, audio) {
        retained.push({
          sequence,
          capturedAt: 10_000 + sequence,
          byteLength: audio.byteLength,
          audio,
        });
      },
      replayRange: async (first, last) => retained.filter(
        (item) => item.sequence >= first && item.sequence <= last,
      ),
      acknowledgeThrough(sequence) {
        retained.filter((item) => item.sequence <= sequence).forEach((item) => acknowledged.add(item.sequence));
      },
      clear: vi.fn(),
      snapshot: () => ({
        retainedFrames: retained.length,
        unacknowledgedFrames: retained.filter((item) => !acknowledged.has(item.sequence)).length,
        encryptedBytes: retained.reduce((sum, item) => sum + item.byteLength, 0),
        retentionMs: 30_000,
      }),
    };
    let capture;
    let mediaState;
    let serverSequence = 5;
    const originalSend = socket.send.bind(socket);
    socket.send = (value) => {
      originalSend(value);
      const message = socket.sent.at(-1);
      const respond = (status, extra = {}) => socket.emit("message", {
        data: JSON.stringify({
          ...candidateEvent(++serverSequence, "speech.started", {
            speaker: "candidate",
            server_audio_received: true,
            browser_backfill: { status, audio_epoch: "epoch_test", ...extra },
          }),
          turn_id: "turn_1",
          causation_id: message.causation_id,
        }),
      });
      if (message.type === "evidence.recovery.begin") {
        respond("ready", {
          batch_id: "browser_backfill_test",
          ack_through: message.payload.first_sequence - 1,
        });
      } else if (message.type === "evidence.recovery.chunk") {
        respond("acknowledged", {
          batch_id: message.payload.batch_id,
          ack_through: message.payload.client_sequence,
        });
      } else if (message.type === "evidence.recovery.complete") {
        respond("complete", {
          batch_id: message.payload.batch_id,
          ack_through: 2,
        });
      }
    };
    const experience = createCandidateInterviewExperience({
      request: async () => ({
        agent_ticket: "agt.test",
        agent_ws_url: "/api/v1/interviews/iv_test/agent",
        media: {
          status: "ready",
          url: "wss://livekit.test",
          participant_token: "candidate-media-ticket",
          video_upstream_allowed: false,
          recovery: {
            server_checkpoint: { enabled: true, mode: "durable_media_checkpoint" },
            browser_backfill: {
              enabled: true,
              protocol: "agent-json-backfill.v1",
              connection_id: "connection_source",
              audio_epoch: "epoch_test",
              retention_ms: 30_000,
              max_bytes: 2 * 1024 * 1024,
              max_frame_bytes: 32 * 1024,
            },
          },
        },
      }),
      socketFactory: () => socket,
      mediaConnector: async ({ onState }) => {
        mediaState = onState;
        return {
          publishedSources: ["microphone"],
          setRecoveryMute: vi.fn(),
          close: vi.fn(),
        };
      },
      audioCaptureFactory: async (_stream, callbacks) => {
        capture = callbacks;
        return { close: vi.fn() };
      },
      ringFactory: () => ring,
      verifyAvatar: async () => ({ ready: true }),
      clock: (() => {
        let now = 10_000;
        return () => ++now;
      })(),
    });
    const run = await experience.open({ interviewId: "iv_test", ticket: "candidate-token" });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify({
      ...candidateEvent(3, "floor.changed", { owner: "candidate", reason: "agent_finished" }),
      turn_id: "turn_1",
    }) });
    socket.emit("message", { data: JSON.stringify({
      ...correlatedCandidateEvent(
        4,
        "floor.changed",
        { owner: "candidate", reason: "evidence_stream_open" },
        latestSentSignal(socket, "evidence.stream.open"),
      ),
      turn_id: "turn_1",
    }) });
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    await capture.onSpeechStarted();
    mediaState("reconnecting");
    capture.onPcm(new Uint8Array([1, 0, 2, 0]).buffer);
    capture.onPcm(new Uint8Array([3, 0, 4, 0]).buffer);
    capture.onSpeechStopped();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(socket.sent.filter((item) => item.type === "speech.stopped")).toHaveLength(0);
    mediaState("connected");
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(socket.sent.filter((item) => item.type.startsWith("evidence.recovery."))).toHaveLength(0);
    socket.emit("message", { data: JSON.stringify({
      ...candidateEvent(5, "problem", {
        code: "LIVEKIT_EVIDENCE_TRACK_MUTED",
        message: "候选人媒体轨道暂时断开，系统将在恢复窗口内等待重连。",
        recoverable: true,
        action: "reconnect_media",
      }),
      turn_id: "turn_1",
    }) });
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (socket.sent.some((item) => item.type === "evidence.recovery.complete")) break;
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    }
    expect(run.getSnapshot().problem).toBeNull();
    const recovery = socket.sent.filter((item) => item.type.startsWith("evidence.recovery."));
    expect(recovery.map((item) => item.type)).toEqual([
      "evidence.recovery.begin",
      "evidence.recovery.chunk",
      "evidence.recovery.chunk",
      "evidence.recovery.complete",
    ]);
    expect(recovery[0].payload).toMatchObject({
      source_connection_id: "connection_source",
      audio_epoch: "epoch_test",
      first_sequence: 1,
      last_sequence: 2,
      total_bytes: 8,
    });
    expect([...atob(recovery[1].payload.audio_base64)].map((value) => value.charCodeAt(0))).toEqual([1, 0, 2, 0]);
    expect([...atob(recovery[2].payload.audio_base64)].map((value) => value.charCodeAt(0))).toEqual([3, 0, 4, 0]);
    for (let attempt = 0; attempt < 10; attempt += 1) {
      if (socket.sent.some((item) => item.type === "speech.stopped")) break;
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    }
    expect(socket.sent.filter((item) => item.type === "speech.stopped")).toHaveLength(1);
    expect(run.getSnapshot().recovery.unacknowledgedFrames).toBe(0);
    await run.close();
  });

  it("keeps the authoritative evidence gate open across the 2.5s endpoint race", async () => {
    vi.useFakeTimers();
    try {
      const socket = new CandidateFakeSocket();
      const stream = {
        getAudioTracks: () => [{ readyState: "live", stop: vi.fn() }],
        getVideoTracks: () => [{ readyState: "live", stop: vi.fn() }],
        getTracks: () => [],
      };
      prepareCandidateMedia(stream, {
        speaker_verified: true,
        microphone_granted: true,
        camera_granted: true,
        webrtc_supported: true,
        audio_worklet_supported: true,
        webgl_supported: true,
        media_recorder_supported: true,
        avatar_fps: 60,
      });
      let capture;
      const append = vi.fn(async () => {});
      const experience = createCandidateInterviewExperience({
        request: async () => ({
          agent_ticket: "agt.test",
          agent_ws_url: "/api/v1/interviews/iv_test/agent",
          media: {
            status: "ready",
            url: "wss://livekit.test",
            participant_token: "candidate-media-ticket",
            video_upstream_allowed: false,
            recovery: {
              server_checkpoint: { enabled: true, mode: "durable_media_checkpoint" },
              browser_backfill: { enabled: true, protocol: "agent-json-backfill.v1" },
            },
          },
        }),
        socketFactory: () => socket,
        mediaConnector: async () => ({ publishedSources: ["microphone"], close: vi.fn() }),
        audioCaptureFactory: async (_stream, callbacks) => {
          capture = callbacks;
          return { close: vi.fn() };
        },
        ringFactory: () => ({
          append,
          clear: vi.fn(),
          snapshot: () => ({ retainedFrames: 1, unacknowledgedFrames: 1, encryptedBytes: 4, retentionMs: 30_000 }),
        }),
        verifyAvatar: async () => ({ ready: true }),
      });
      const opening = experience.open({ interviewId: "iv_test", ticket: "candidate-token" });
      await vi.runAllTicks();
      const run = await opening;
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", { ...candidateSnapshot(), floor: "candidate" })) });
      await vi.runAllTicks();
      await capture.onSpeechStarted();
      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      expect(socket.sent.filter((item) => item.type === "speech.started")).toHaveLength(0);
      expect(run.getSnapshot().phase).toBe("preparing");

      socket.emit("message", { data: JSON.stringify({
        ...correlatedCandidateEvent(
          2,
          "floor.changed",
          { owner: "candidate", reason: "evidence_stream_open" },
          latestSentSignal(socket, "evidence.stream.open"),
        ),
        turn_id: "turn_1",
      }) });
      await vi.runAllTicks();
      expect(socket.sent.filter((item) => item.type === "speech.started")).toHaveLength(1);
      expect(run.getSnapshot().phase).toBe("listening");

      capture.onSpeechStopped();
      await vi.advanceTimersByTimeAsync(2475);
      expect(run.getSnapshot().phase).toBe("listening");
      expect(run.getSnapshot().endpoint.active).toBe(false);
      capture.onPcm(new Uint8Array([1, 0, 2, 0]).buffer);
      await capture.onSpeechStarted();
      await vi.runAllTicks();

      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      expect(append).toHaveBeenCalledOnce();
      expect(run.getSnapshot().phase).toBe("listening");
      await run.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("persists stream readiness and reopens warm-up only after an explicit retry", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(),
      floor: "candidate",
      calibration_status: "listening",
    })) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      evidence: { requested: true, ready: false },
    });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      2,
      "floor.changed",
      { owner: "candidate", reason: "warmup_stream_open" },
      latestSentSignal(socket, "evidence.stream.open"),
    )) });
    expect(run.getSnapshot()).toMatchObject({
      phase: "listening",
      calibration: { status: "listening" },
      evidence: { requested: true, ready: true },
    });

    socket.emit("message", { data: JSON.stringify(candidateEvent(3, "floor.changed", {
      owner: "candidate",
      reason: "candidate_continues",
    })) });
    expect(run.getSnapshot()).toMatchObject({
      phase: "listening",
      evidence: { requested: true, ready: true },
    });

    socket.emit("message", { data: JSON.stringify(candidateEvent(4, "problem", {
      code: "WARMUP_STT_PROBLEM",
      message: "试音暂时不可用，请重试。",
      recoverable: true,
      action: "retry_warmup",
      calibration: true,
    })) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      calibration: { status: "retrying" },
      evidence: { requested: false, ready: false },
      problem: { action: "retry_warmup" },
    });

    await run.act({ type: "warmup.retry" });
    expect(socket.sent.filter((item) => item.type === "warmup.retry")).toHaveLength(1);
    expect(run.getSnapshot().problem).toBeNull();
    const retry = latestSentSignal(socket, "warmup.retry");
    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      5,
      "floor.changed",
      { owner: "candidate", reason: "warmup_retry" },
      retry,
    )) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(2);
    expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: false });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      6,
      "floor.changed",
      { owner: "candidate", reason: "warmup_retry" },
      retry,
    )) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(2);
    expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: false });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      7,
      "floor.changed",
      { owner: "candidate", reason: "warmup_stream_open" },
      latestSentSignal(socket, "evidence.stream.open"),
    )) });
    expect(run.getSnapshot()).toMatchObject({
      phase: "listening",
      calibration: { status: "listening" },
      evidence: { requested: true, ready: true },
    });
    await run.close();
  });

  it("opens with a new cause after another control has durably reset warm-up", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(),
      floor: "candidate",
      calibration_status: "retrying",
      calibration_retry_required: true,
    })) });
    const foreignRetry = { causation_id: "candidate:other-control-reset" };

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      2,
      "floor.changed",
      { owner: "candidate", reason: "warmup_retry" },
      foreignRetry,
    )) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      calibration: { status: "retrying", retryRequired: false },
      evidence: { requested: true, ready: false },
    });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      3,
      "floor.changed",
      { owner: "candidate", reason: "warmup_retry" },
      foreignRetry,
    )) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: false });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      4,
      "floor.changed",
      { owner: "candidate", reason: "warmup_stream_open" },
      latestSentSignal(socket, "evidence.stream.open"),
    )) });
    expect(run.getSnapshot()).toMatchObject({
      phase: "listening",
      calibration: { status: "listening", retryRequired: false },
      evidence: { requested: true, ready: true },
    });
    await run.close();
  });

  it("ignores a foreign stream-ready broadcast and claims with a new cause on the next snapshot", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(
      1,
      "session.snapshot",
      candidateSnapshot(),
    )) });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      2,
      "floor.changed",
      { owner: "candidate", reason: "evidence_stream_open" },
      { causation_id: "candidate:another-control-connection" },
    )) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      evidence: { requested: false, ready: false },
    });

    socket.emit("message", { data: JSON.stringify(candidateEvent(3, "session.snapshot", {
      ...candidateSnapshot(),
      floor: "candidate",
    })) });
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      evidence: { requested: true, ready: false },
      problem: null,
    });

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      4,
      "floor.changed",
      { owner: "candidate", reason: "evidence_stream_open" },
      latestSentSignal(socket, "evidence.stream.open"),
    )) });
    expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: true });
    await run.close();
  });

  it("keeps a confirmed open causation valid for an at-least-once ready acknowledgement", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(),
      floor: "candidate",
    })) });
    const open = latestSentSignal(socket, "evidence.stream.open");

    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      2,
      "floor.changed",
      { owner: "candidate", reason: "evidence_stream_open" },
      open,
    )) });
    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      3,
      "floor.changed",
      { owner: "candidate", reason: "evidence_stream_open" },
      open,
    )) });

    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot()).toMatchObject({
      phase: "listening",
      evidence: { requested: true, ready: true },
      problem: null,
    });
    await run.close();
  });

  it("does not auto-open a durable retry-required warm-up after a fresh reload", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(),
      floor: "candidate",
      calibration_status: "retrying",
      calibration_retry_required: true,
    })) });

    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      calibration: { status: "retrying", retryRequired: true },
      evidence: { requested: false, ready: false },
    });
    await run.close();
  });

  it("does not auto-open after a retry-required heartbeat invalidates a ready warm-up", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(),
      floor: "candidate",
      calibration_status: "listening",
    })) });
    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
      2,
      "floor.changed",
      { owner: "candidate", reason: "warmup_stream_open" },
      latestSentSignal(socket, "evidence.stream.open"),
    )) });
    expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: true });

    const retrySnapshot = {
      ...candidateSnapshot(),
      floor: "candidate",
      calibration_status: "retrying",
      calibration_retry_required: true,
    };
    socket.emit("message", { data: JSON.stringify(candidateEvent(3, "session.snapshot", retrySnapshot)) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(4, "session.snapshot", retrySnapshot)) });

    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    expect(run.getSnapshot()).toMatchObject({
      phase: "preparing",
      calibration: { status: "retrying", retryRequired: true },
      evidence: { requested: false, ready: false },
    });
    await run.close();
  });

  it("reasserts an unacknowledged Evidence open once after control reconnect", async () => {
    vi.useFakeTimers();
    try {
      const first = new CandidateFakeSocket();
      const second = new CandidateFakeSocket();
      const { run } = await openPlaybackHarness({ sockets: [first, second] });
      first.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
        ...candidateSnapshot(),
        floor: "candidate",
      })) });
      expect(first.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      const firstOpen = latestSentSignal(first, "evidence.stream.open");
      expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: false });

      first.readyState = WebSocket.CLOSED;
      first.emit("close", {});
      await vi.advanceTimersByTimeAsync(400);
      await vi.runAllTicks();
      expect(second.sent.filter((item) => item.type === "session.open")).toHaveLength(1);

      second.emit("message", { data: JSON.stringify(candidateEvent(2, "session.snapshot", {
        ...candidateSnapshot(),
        floor: "agent",
      })) });
      expect(second.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
      expect(run.getSnapshot()).toMatchObject({
        phase: "responding",
        evidence: { requested: true, ready: false },
      });

      second.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
        3,
        "floor.changed",
        { owner: "candidate", reason: "evidence_stream_open" },
        firstOpen,
      )) });
      expect(second.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
      expect(run.getSnapshot()).toMatchObject({
        phase: "preparing",
        evidence: { requested: true, ready: false },
      });

      second.emit("message", { data: JSON.stringify(candidateEvent(4, "floor.changed", {
        owner: "candidate",
        reason: "agent_finished",
      })) });
      expect(second.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      const reassertedOpen = latestSentSignal(second, "evidence.stream.open");
      expect(reassertedOpen.causation_id).not.toBe(firstOpen.causation_id);

      second.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
        5,
        "floor.changed",
        { owner: "candidate", reason: "evidence_stream_open" },
        reassertedOpen,
      )) });
      expect(run.getSnapshot()).toMatchObject({
        phase: "listening",
        evidence: { requested: true, ready: true },
      });
      expect(run.getSnapshot().problem).toBeNull();
      await run.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reasserts a retry-authorized warm-up open when control disconnects before ready", async () => {
    vi.useFakeTimers();
    try {
      const first = new CandidateFakeSocket();
      const second = new CandidateFakeSocket();
      const { run } = await openPlaybackHarness({ sockets: [first, second] });
      first.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
        ...candidateSnapshot(),
        floor: "candidate",
        calibration_status: "retrying",
        calibration_retry_required: true,
      })) });

      await run.act({ type: "warmup.retry" });
      const retry = latestSentSignal(first, "warmup.retry");
      first.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
        2,
        "floor.changed",
        { owner: "candidate", reason: "warmup_retry" },
        retry,
      )) });
      const firstOpen = latestSentSignal(first, "evidence.stream.open");
      expect(run.getSnapshot().evidence).toEqual({ requested: true, ready: false });

      first.readyState = WebSocket.CLOSED;
      first.emit("close", {});
      await vi.advanceTimersByTimeAsync(400);
      await vi.runAllTicks();
      second.emit("message", { data: JSON.stringify(candidateEvent(3, "session.snapshot", {
        ...candidateSnapshot(),
        floor: "candidate",
        calibration_status: "retrying",
        calibration_retry_required: false,
      })) });

      expect(second.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      const reassertedOpen = latestSentSignal(second, "evidence.stream.open");
      expect(reassertedOpen.causation_id).not.toBe(firstOpen.causation_id);
      expect(run.getSnapshot()).toMatchObject({
        phase: "preparing",
        calibration: { status: "retrying", retryRequired: false },
        evidence: { requested: true, ready: false },
      });

      second.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
        4,
        "floor.changed",
        { owner: "candidate", reason: "warmup_stream_open" },
        firstOpen,
      )) });
      expect(run.getSnapshot().evidence.ready).toBe(false);
      second.emit("message", { data: JSON.stringify(correlatedCandidateEvent(
        5,
        "floor.changed",
        { owner: "candidate", reason: "warmup_stream_open" },
        reassertedOpen,
      )) });
      expect(run.getSnapshot()).toMatchObject({
        phase: "listening",
        calibration: { status: "listening", retryRequired: false },
        evidence: { requested: true, ready: true },
      });
      await run.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("restores explicit warm-up retry when reset is lost before the server receives it", async () => {
    vi.useFakeTimers();
    try {
      const first = new CandidateFakeSocket();
      const second = new CandidateFakeSocket();
      const { run } = await openPlaybackHarness({ sockets: [first, second] });
      const retryRequiredSnapshot = {
        ...candidateSnapshot(),
        floor: "candidate",
        calibration_status: "retrying",
        calibration_retry_required: true,
      };
      first.emit("message", { data: JSON.stringify(candidateEvent(
        1,
        "session.snapshot",
        retryRequiredSnapshot,
      )) });
      await run.act({ type: "warmup.retry" });
      expect(first.sent.filter((item) => item.type === "warmup.retry")).toHaveLength(1);
      expect(run.getSnapshot().calibration.retryRequired).toBe(false);

      // The client wrote the reset to the old socket, but it closed before
      // the server durably applied it or emitted warmup_retry.
      first.readyState = WebSocket.CLOSED;
      first.emit("close", {});
      await vi.advanceTimersByTimeAsync(400);
      await vi.runAllTicks();
      second.emit("message", { data: JSON.stringify(candidateEvent(
        2,
        "session.snapshot",
        retryRequiredSnapshot,
      )) });

      expect(second.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
      expect(second.sent.filter((item) => item.type === "warmup.retry")).toHaveLength(0);
      expect(run.getSnapshot()).toMatchObject({
        phase: "preparing",
        calibration: { status: "retrying", retryRequired: true },
        evidence: { requested: false, ready: false },
      });
      await run.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not emit speech or fake endpoint progress after a paused snapshot", async () => {
    const socket = new CandidateFakeSocket();
    const stream = {
      getAudioTracks: () => [{ readyState: "live", stop: vi.fn() }],
      getVideoTracks: () => [{ readyState: "live", stop: vi.fn() }],
      getTracks: () => [],
    };
    prepareCandidateMedia(stream, {
      speaker_verified: true,
      microphone_granted: true,
      camera_granted: true,
      webrtc_supported: true,
      audio_worklet_supported: true,
      webgl_supported: true,
      media_recorder_supported: true,
      avatar_fps: 60,
    });
    let capture;
    const experience = createCandidateInterviewExperience({
      request: async () => ({
        agent_ticket: "agt.test",
        agent_ws_url: "/api/v1/interviews/iv_test/agent",
        media: {
          status: "ready",
          url: "wss://livekit.test",
          participant_token: "candidate-media-ticket",
          video_upstream_allowed: false,
          recovery: {
            server_checkpoint: { enabled: true, mode: "durable_media_checkpoint" },
            browser_backfill: { enabled: true, protocol: "agent-json-backfill.v1" },
          },
        },
      }),
      socketFactory: () => socket,
      mediaConnector: async () => ({ publishedSources: ["microphone"], close: vi.fn() }),
      audioCaptureFactory: async (_stream, callbacks) => {
        capture = callbacks;
        return { close: vi.fn() };
      },
      ringFactory: () => ({ append: vi.fn(), clear: vi.fn(), snapshot: () => ({}) }),
      verifyAvatar: async () => ({ ready: true }),
    });
    const run = await experience.open({ interviewId: "iv_test", ticket: "candidate-token" });
    socket.emit("message", {
      data: JSON.stringify(candidateEvent(1, "session.snapshot", {
        ...candidateSnapshot(),
        status: "paused",
        floor: "none",
      })),
    });
    await capture.onSpeechStarted();
    capture.onSpeechStopped();

    expect(run.getSnapshot().phase).toBe("paused");
    expect(run.getSnapshot().endpoint).toEqual({ active: false, deadlineAt: null });
    expect(socket.sent.filter((item) => [
      "speech.started",
      "speech.stopped",
      "evidence.stream.open",
    ].includes(item.type))).toHaveLength(0);
    await run.close();
  });
});

class CandidateFakeSocket {
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

function candidateEvent(sequence, type, payload) {
  return {
    event_id: `candidate_event_${sequence}`,
    session_sequence: sequence,
    type,
    turn_id: type === "avatar.performance.started" ? "turn_1" : null,
    causation_id: null,
    occurred_at: "2026-09-01T12:00:00Z",
    replayability: type === "session.snapshot" ? "transient" : "replayable",
    payload,
  };
}

function latestSentSignal(socket, type) {
  const signal = socket.sent.filter((item) => item.type === type).at(-1);
  if (!signal) throw new Error(`Missing sent ${type} signal`);
  return signal;
}

function correlatedCandidateEvent(sequence, type, payload, signal) {
  return {
    ...candidateEvent(sequence, type, payload),
    causation_id: signal.causation_id,
    replayability: "transient",
  };
}

function candidateSnapshot() {
  return {
    interview_id: "iv_test",
    status: "in_progress",
    phase: "questioning",
    current_turn_id: "turn_1",
    floor: "agent",
    takeover: null,
    calibration_status: "completed",
    calibration_retry_required: false,
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

function candidatePerformance() {
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

class CandidateFakeAudio {
  constructor({ playResult = Promise.resolve(), emitPlay = true } = {}) {
    this.currentTime = 0;
    this.src = "";
    this.preload = "";
    this.handlers = new Map();
    this.emitPlay = emitPlay;
    this.playResult = playResult;
    this.play = vi.fn(() => {
      if (this.emitPlay) this.emit("play");
      return this.playResult;
    });
    this.pause = vi.fn();
    this.load = vi.fn();
    this.removeAttribute = vi.fn((name) => {
      if (name === "src") this.src = "";
    });
  }

  addEventListener(type, handler, options = {}) {
    const values = this.handlers.get(type) || [];
    values.push({ handler, once: Boolean(options?.once) });
    this.handlers.set(type, values);
  }

  removeEventListener(type, handler) {
    const values = this.handlers.get(type) || [];
    this.handlers.set(type, values.filter((item) => item.handler !== handler));
  }

  listener(type) {
    return this.handlers.get(type)?.[0]?.handler;
  }

  emit(type) {
    const values = [...(this.handlers.get(type) || [])];
    for (const item of values) {
      if (item.once) this.removeEventListener(type, item.handler);
      item.handler({ type, target: this });
    }
  }
}

async function openPlaybackHarness({ audios = [], sockets = null } = {}) {
  const socket = sockets?.[0] || new CandidateFakeSocket();
  let socketIndex = 0;
  const stream = {
    getAudioTracks: () => [{ readyState: "live", stop: vi.fn() }],
    getVideoTracks: () => [{ readyState: "live", stop: vi.fn() }],
    getTracks: () => [],
  };
  prepareCandidateMedia(stream, {
    speaker_verified: true,
    microphone_granted: true,
    camera_granted: true,
    webrtc_supported: true,
    audio_worklet_supported: true,
    webgl_supported: true,
    media_recorder_supported: true,
    avatar_fps: 60,
  });
  const request = vi.fn(async (path) => {
    if (path.endsWith("/runtime-problems")) {
      return { accepted: true, status: "paused" };
    }
    return {
      agent_ticket: "agt.test",
      agent_ws_url: "/api/v1/interviews/iv_test/agent",
      media: {
        status: "ready",
        url: "wss://livekit.test",
        participant_token: "candidate-media-ticket",
        video_upstream_allowed: true,
        recovery: {
          server_checkpoint: { enabled: true, mode: "durable_media_checkpoint" },
          browser_backfill: { enabled: true, protocol: "agent-json-backfill.v1" },
        },
      },
    };
  });
  let capture;
  const queue = [...audios];
  const audioFactory = vi.fn((url) => {
    const audio = queue.shift() || new CandidateFakeAudio();
    audio.src = url;
    return audio;
  });
  const experience = createCandidateInterviewExperience({
    request,
    socketFactory: () => sockets?.[socketIndex++] || socket,
    mediaConnector: async () => ({ publishedSources: ["microphone", "camera"], close: vi.fn() }),
    audioCaptureFactory: async (_stream, callbacks) => {
      capture = callbacks;
      return { close: vi.fn() };
    },
    ringFactory: () => ({
      append: vi.fn(),
      clear: vi.fn(),
      snapshot: () => ({
        retainedFrames: 0,
        unacknowledgedFrames: 0,
        encryptedBytes: 0,
        retentionMs: 30_000,
      }),
    }),
    verifyAvatar: async () => ({ ready: true }),
    audioFactory,
  });
  const run = await experience.open({ interviewId: "iv_test", ticket: "candidate-token" });
  return { run, socket, capture, request, audioFactory };
}

function runtimeProblemCalls(request) {
  return request.mock.calls.filter(([path]) => path.endsWith("/runtime-problems"));
}

async function flushPlaybackEvents() {
  await Promise.resolve();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
}
