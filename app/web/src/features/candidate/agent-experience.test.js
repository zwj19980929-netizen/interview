import { describe, expect, it, vi } from "vitest";

import {
  createCandidateMetricReporter,
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

describe("spoken supplement confirmation", () => {
  it("keeps the real question visible and restores a scoped voice reply state", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", { ...candidateSnapshot(), floor: "candidate" })) });
    socket.emit("message", { data: JSON.stringify({
      ...correlatedCandidateEvent(2, "floor.changed", { owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_spoken" }, latestSentSignal(socket, "evidence.stream.open")), turn_id: "turn_1",
    }) });
    const question = run.getSnapshot().currentQuestion;
    socket.emit("message", { data: JSON.stringify({ ...candidateEvent(3, "conversation.act.selected", {
      act_id: "act_supplement", act_type: "supplement_check", text: "还有什么需要补充的吗？", approved: true, evaluative: false,
    }), turn_id: "turn_1" }) });
    expect(run.getSnapshot().currentQuestion).toEqual(question);
    socket.emit("message", { data: JSON.stringify({ ...candidateEvent(4, "floor.changed", {
      owner: "candidate", reason: "supplement_awaiting_reply", capture_id: "capture_spoken",
    }), turn_id: "turn_1" }) });
    expect(run.getSnapshot()).toMatchObject({ phase: "awaiting_supplement", evidence: { ready: true } });
    socket.emit("message", { data: JSON.stringify(candidateEvent(5, "session.snapshot", {
      ...candidateSnapshot(), floor: "candidate", current_turn_id: "turn_1",
      supplement_confirmation: { status: "awaiting_reply", turn_id: "turn_1", capture_id: "capture_spoken" },
    })) });
    expect(run.getSnapshot().phase).toBe("awaiting_supplement");
    socket.emit("message", { data: JSON.stringify({ ...candidateEvent(6, "floor.changed", {
      owner: "candidate", reason: "supplement_awaiting_reply", capture_id: "capture_old",
    }), turn_id: "turn_1" }) });
    expect(run.getSnapshot().phase).toBe("awaiting_supplement");
    await run.close();
  });

  it("leaves preparing on an understanding error and clears the warning on a new attempt", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", { ...candidateSnapshot(), floor: "candidate" })) });
    socket.emit("message", { data: JSON.stringify({
      ...correlatedCandidateEvent(2, "floor.changed", { owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_spoken" }, latestSentSignal(socket, "evidence.stream.open")), turn_id: "turn_1",
    }) });
    const preparing = sequence => socket.emit("message", { data: JSON.stringify({ ...candidateEvent(sequence, "floor.changed", {
      owner: "candidate", reason: "answer_preparing", capture_id: "capture_spoken",
    }), turn_id: "turn_1" }) });
    preparing(3);
    socket.emit("message", { data: JSON.stringify({ ...candidateEvent(4, "problem", {
      code: "UNDERSTANDING_UNAVAILABLE", message: "已收到语音，正在重试理解。", recoverable: true,
      action: "continue_listening", capture_id: "capture_spoken",
    }), turn_id: "turn_1" }) });
    expect(run.getSnapshot()).toMatchObject({ phase: "listening", evidence: { ready: true }, problem: { code: "UNDERSTANDING_UNAVAILABLE" } });
    preparing(5);
    expect(run.getSnapshot().problem).toBeNull();
    await run.close();
  });
});

describe("audible supplement playback and honest activity", () => {
  it("recovers browser-blocked approved speech without pausing capture or falsely completing it", async () => {
    const audio = new CandidateFakeAudio({ emitPlay: false });
    audio.play.mockImplementationOnce(() => Promise.reject(new DOMException("synthetic", "NotAllowedError")));
    const { run, socket, request } = await openPlaybackHarness({ audios: [audio] });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
    await flushPlaybackEvents();
    expect(run.getSnapshot().speechPlayback.status).toBe("blocked");
    expect(runtimeProblemCalls(request)).toHaveLength(0);
    expect(socket.sent.filter(x => x.type === "avatar.performance.stopped")).toHaveLength(0);
    expect(latestSentSignal(socket, "avatar.performance.playback").payload.status).toBe("blocked");
    audio.emitPlay = true;
    await run.act({ type: "retry_speech" });
    expect(audio.play).toHaveBeenCalledTimes(2);
    expect(audio.muted).toBe(false);
    expect(audio.volume).toBe(1);
    expect(run.getSnapshot().speechPlayback.status).toBe("playing");
    expect(latestSentSignal(socket, "avatar.performance.playback").payload.status).toBe("playing");
    audio.emit("ended");
    expect(socket.sent.filter(x => x.type === "avatar.performance.stopped")).toHaveLength(1);
    expect(await run.act({ type: "retry_speech" })).toBe(false);
    await run.close();
  });

  it("times out a stalled utterance and ignores its recovery after replacement", async () => {
    const first = new CandidateFakeAudio({ emitPlay: false, playResult: new Promise(() => {}) });
    const second = new CandidateFakeAudio();
    const { run, socket, request } = await openPlaybackHarness({ audios: [first, second] });
    vi.useFakeTimers();
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
      socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
      await vi.advanceTimersByTimeAsync(8001);
      expect(run.getSnapshot().speechPlayback.status).toBe("blocked");
      expect(runtimeProblemCalls(request)).toHaveLength(0);
      socket.emit("message", { data: JSON.stringify(candidateEvent(3, "avatar.performance.started", { ...candidatePerformance(), performance_id: "second" })) });
      await vi.advanceTimersByTimeAsync(0);
      expect(run.getSnapshot().speechPlayback.status).toBe("playing");
      expect(await run.act({ type: "retry_speech" })).toBe(false);
      expect(first.play).toHaveBeenCalledOnce();
    } finally { await run.close(); vi.useRealTimers(); }
  });

  it("does not keep the transcription indicator green on duplicate old text", async () => {
    const { run, socket } = await openPlaybackHarness();
    vi.useFakeTimers();
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", { ...candidateSnapshot(), floor: "candidate" })) });
      const caption = n => socket.emit("message", { data: JSON.stringify({ ...candidateEvent(n, "transcript.partial", { text: "服务端合成字幕", server_received: true }), turn_id: "turn_1" }) });
      caption(2);
      expect(run.getSnapshot().captions.forming).toBe(true);
      await vi.advanceTimersByTimeAsync(1500);
      caption(3);
      await vi.advanceTimersByTimeAsync(501);
      expect(run.getSnapshot().captions.forming).toBe(false);
      expect(run.getSnapshot().captions.recent.at(-1).text).toBe("服务端合成字幕");
      await vi.advanceTimersByTimeAsync(5000);
      expect(run.getSnapshot().phase).not.toBe("completed");
    } finally { await run.close(); vi.useRealTimers(); }
  });
});

describe("captions belong to the current question", () => {
  it("starts a follow-up with empty captions instead of showing the previous answer above its partial", async () => {
    const { run, socket } = await openPlaybackHarness();
    let sequence = 0;
    const emit = (type, payload, turnId = "turn_1") => socket.emit("message", {
      data: JSON.stringify({ ...candidateEvent(++sequence, type, payload), turn_id: turnId }),
    });
    try {
      emit("session.snapshot", candidateSnapshot(), null);
      emit("transcript.final", { text: "上一题关于滚动发布的完整回答。", authoritative: true });
      expect(run.getSnapshot().captions.full).toHaveLength(1);
      emit("conversation.act.selected", {
        act_id: "act_followup", act_type: "followup", text: "镜像如何优化？", approved: true, evaluative: false,
      }, "turn_followup");
      expect(run.getSnapshot().captions).toEqual({ forming: false, recent: [], full: [] });
      emit("transcript.partial", { text: "嗯，我得想一下。", server_received: true }, "turn_followup");
      expect(run.getSnapshot().captions.recent.map(row => row.text)).toEqual(["嗯，我得想一下。"]);
      expect(run.getSnapshot().captions.full).toEqual([]);
      emit("transcript.final", { text: "嗯，我得想一下。可以采用多阶段构建。", authoritative: true }, "turn_followup");
      expect(run.getSnapshot().captions.full.map(row => row.text)).toEqual(["嗯，我得想一下。可以采用多阶段构建。"]);
    } finally { await run.close(); }
  });

  it("isolates snapshot turn changes and ignores old or unscoped transcripts without closing current capture", async () => {
    const { run, socket } = await openPlaybackHarness();
    let sequence = 0;
    const emit = (type, payload, turnId = "turn_followup", causationId = null) => socket.emit("message", {
      data: JSON.stringify({ ...candidateEvent(++sequence, type, payload), turn_id: turnId, causation_id: causationId }),
    });
    try {
      emit("session.snapshot", candidateSnapshot(), null);
      emit("transcript.final", { text: "旧题完整回答。", authoritative: true }, "turn_1");
      emit("session.snapshot", {
        ...candidateSnapshot(), floor: "candidate", current_turn_id: "turn_followup",
        current_question: { ...candidateSnapshot().current_question, turn_id: "turn_followup", is_followup: true },
      }, null);
      expect(run.getSnapshot().captions).toEqual({ forming: false, recent: [], full: [] });
      emit("floor.changed", { owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_followup" },
        "turn_followup", latestSentSignal(socket, "evidence.stream.open").causation_id);
      emit("transcript.partial", { text: "当前追问的独立回答。", server_received: true });
      emit("problem", { code: "UNDERSTANDING_UNAVAILABLE", message: "稍后继续整理", recoverable: true, action: "continue_listening" });
      const current = run.getSnapshot();
      expect(current.evidence.ready).toBe(true);
      for (const turnId of ["turn_1", null]) {
        emit("transcript.partial", { text: "迟到的旧题字幕。", server_received: true }, turnId);
        emit("transcript.final", { text: "迟到的旧题终稿。", authoritative: true }, turnId);
      }
      emit("transcript.final", { text: "迟到的试音终稿。", calibration: true }, null);
      expect(run.getSnapshot()).toEqual(current);
      expect(await run.act({ type: "finish_answer" })).not.toBe(false);
      expect(latestSentSignal(socket, "finish_answer")).toMatchObject({ turn_id: "turn_followup", payload: { capture_id: "capture_followup" } });
    } finally { await run.close(); }
  });

  it("keeps current captions through repeating, supplement confirmation and same-turn snapshots", async () => {
    const { run, socket } = await openPlaybackHarness();
    let sequence = 0;
    const emit = (type, payload, turnId = "turn_1") => socket.emit("message", {
      data: JSON.stringify({ ...candidateEvent(++sequence, type, payload), turn_id: turnId }),
    });
    try {
      emit("session.snapshot", candidateSnapshot(), null);
      emit("transcript.partial", { text: "本题已经说过的部分。", server_received: true });
      const captions = run.getSnapshot().captions;
      for (const actType of ["repeat", "supplement_check", "supplement_continue", "supplement_clarify"]) {
        emit("conversation.act.selected", { act_id: `act_${actType}`, act_type: actType, text: "本题口头提示。", approved: true, evaluative: false });
        expect(run.getSnapshot().captions).toEqual(captions);
      }
      emit("session.snapshot", candidateSnapshot(), null);
      expect(run.getSnapshot().captions).toEqual(captions);
    } finally { await run.close(); }
  });

  it("accepts marked warm-up before a question and uses snapshot turn identity when its question is not projected yet", async () => {
    const { run, socket } = await openPlaybackHarness();
    let sequence = 0;
    const emit = (type, payload, turnId = null) => socket.emit("message", {
      data: JSON.stringify({ ...candidateEvent(++sequence, type, payload), turn_id: turnId }),
    });
    try {
      emit("transcript.partial", { text: "身份尚不明确的正式字幕。", server_received: true }, "turn_1");
      expect(run.getSnapshot().captions.recent).toEqual([]);
      emit("transcript.partial", { text: "这是试音。", server_received: true, calibration: true });
      emit("transcript.final", { text: "这是试音。", confidence: 0.96, calibration: true });
      expect(run.getSnapshot()).toMatchObject({ calibration: { status: "awaiting_confirmation" } });
      expect(run.getSnapshot().captions.full.map(row => row.text)).toEqual(["这是试音。"]);
      emit("session.snapshot", { ...candidateSnapshot(), current_question: null });
      expect(run.getSnapshot().captions.full).toEqual([]);
      emit("transcript.partial", { text: "当前正式题字幕。", server_received: true }, "turn_1");
      expect(run.getSnapshot().captions.recent.map(row => row.text)).toEqual(["当前正式题字幕。"]);
      emit("session.snapshot", candidateSnapshot());
      expect(run.getSnapshot().captions.recent.map(row => row.text)).toEqual(["当前正式题字幕。"]);
    } finally { await run.close(); }
  });
});

describe("candidate metric reporter", () => {
  it("emits at most one maximum viseme drift sample per one-second window", async () => {
    vi.useFakeTimers();
    const emit = vi.fn();
    const reporter = createCandidateMetricReporter({ emit });
    try {
      reporter.observe("avatar_viseme_drift_ms", 8);
      reporter.observe("avatar_viseme_drift_ms", 31);
      reporter.observe("avatar_viseme_drift_ms", 13);

      await vi.advanceTimersByTimeAsync(999);
      expect(emit).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      expect(emit).toHaveBeenCalledOnce();
      expect(emit).toHaveBeenLastCalledWith("avatar_viseme_drift_ms", 31);

      reporter.observe("avatar_viseme_drift_ms", 5);
      reporter.observe("avatar_viseme_drift_ms", 9);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(emit).toHaveBeenCalledTimes(2);
      expect(emit).toHaveBeenLastCalledWith("avatar_viseme_drift_ms", 9);
    } finally {
      reporter.clear();
      vi.useRealTimers();
    }
  });

  it("keeps viseme drift and avatar freeze windows independent", async () => {
    vi.useFakeTimers();
    const emit = vi.fn();
    const reporter = createCandidateMetricReporter({ emit });
    try {
      reporter.observe("avatar_viseme_drift_ms", 4);
      await vi.advanceTimersByTimeAsync(400);
      reporter.observe("avatar_freeze_ms", 140);
      reporter.observe("avatar_viseme_drift_ms", 12);
      reporter.observe("avatar_freeze_ms", 220);

      await vi.advanceTimersByTimeAsync(600);
      expect(emit.mock.calls).toEqual([["avatar_viseme_drift_ms", 12]]);
      await vi.advanceTimersByTimeAsync(400);
      expect(emit.mock.calls).toEqual([
        ["avatar_viseme_drift_ms", 12],
        ["avatar_freeze_ms", 220],
      ]);
    } finally {
      reporter.clear();
      vi.useRealTimers();
    }
  });

  it("sends non-avatar latency metrics immediately", () => {
    const emit = vi.fn();
    const reporter = createCandidateMetricReporter({ emit });

    reporter.observe("partial_first_token_ms", 87);

    expect(emit).toHaveBeenCalledOnce();
    expect(emit).toHaveBeenCalledWith("partial_first_token_ms", 87);
    reporter.clear();
  });

  it("does not queue samples while the control socket is closed", async () => {
    vi.useFakeTimers();
    const emit = vi.fn();
    let open = false;
    const reporter = createCandidateMetricReporter({ emit, isOpen: () => open });
    try {
      reporter.observe("avatar_viseme_drift_ms", 40);
      open = true;
      await vi.advanceTimersByTimeAsync(1_000);
      expect(emit).not.toHaveBeenCalled();

      reporter.observe("avatar_viseme_drift_ms", 50);
      open = false;
      await vi.advanceTimersByTimeAsync(1_000);
      open = true;
      await vi.advanceTimersByTimeAsync(1_000);
      expect(emit).not.toHaveBeenCalled();
    } finally {
      reporter.clear();
      vi.useRealTimers();
    }
  });

  it("clears all pending metric windows when playback or the experience stops", async () => {
    vi.useFakeTimers();
    const emit = vi.fn();
    const reporter = createCandidateMetricReporter({ emit });
    try {
      reporter.observe("avatar_viseme_drift_ms", 19);
      reporter.observe("avatar_freeze_ms", 180);
      expect(vi.getTimerCount()).toBe(2);

      reporter.clear();

      expect(vi.getTimerCount()).toBe(0);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(emit).not.toHaveBeenCalled();
    } finally {
      reporter.clear();
      vi.useRealTimers();
    }
  });
});

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
    const staleFirstPlay = first.listener("playing");
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

  it("holds the current formal audio on error without falsely stopping the interview", async () => {
    const audio = new CandidateFakeAudio();
    const { run, socket, request } = await openPlaybackHarness({ audios: [audio] });
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
    socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
    await flushPlaybackEvents();

    audio.emit("error");
    await flushPlaybackEvents();

    expect(run.getSnapshot()).toMatchObject({
      speechPlayback: { status: "blocked" },
      problem: null,
    });
    expect(runtimeProblemCalls(request)).toHaveLength(0);
    expect(socket.sent.filter(x => x.type === "avatar.performance.stopped")).toHaveLength(0);
    expect(latestSentSignal(socket, "avatar.performance.playback").payload.status).toBe("failed");
    await run.close();
  });

  it.each([true, false])("plays approved streaming TTS in either event order without a ready/play deadlock (%s)", async (trackFirst) => {
    const audio = new CandidateFakeAudio({ emitPlay: false, playResult: new Promise(() => {}) });
    const { run, socket, mediaCallbacks, request, audioFactory } = await openPlaybackHarness({ liveAudio: audio });
    const performance = livePerformance();
    const track = liveTrack(performance);
    const subscribe = () => mediaCallbacks.onRemoteAudioTrack(track,
      { trackSid: track.sid, trackName: performance.live_audio.track_name },
      { identity: performance.live_audio.publisher_identity });
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", { ...candidateSnapshot(), floor: "agent" })) });
      if (trackFirst) subscribe();
      expect(audio.play).not.toHaveBeenCalled();
      socket.emit("message", { data: JSON.stringify({ ...candidateEvent(2, "avatar.performance.started", performance), replayability: "transient" }) });
      if (!trackFirst) subscribe();
      await flushPlaybackEvents();
      expect(audioFactory).not.toHaveBeenCalled();
      expect(audio.play).toHaveBeenCalledOnce();
      expect(socket.sent.filter((item) => item.type === "avatar.performance.ready")).toEqual([
        expect.objectContaining({ payload: { performance_id: performance.performance_id, output_id: performance.live_audio.output_id } }),
      ]);
      expect(run.getSnapshot().avatar.status).toBe("idle");
      audio.emit("play");
      expect(run.getSnapshot().avatar.status).toBe("idle");
      audio.currentTime = 15;
      audio.emit("playing");
      expect(run.getSnapshot().avatar.status).toBe("speaking");
      socket.emit("message", { data: JSON.stringify({ ...candidateEvent(3, "avatar.performance.producer_finished", {
        performance_id: performance.performance_id, output_id: performance.live_audio.output_id,
        total_samples: 24_000, sample_rate_hz: 24_000,
      }), replayability: "transient" }) });
      await flushPlaybackEvents();
      expect(run.getSnapshot().floor).toBe("agent");
      expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toHaveLength(0);
      audio.currentTime = 16;
      await new Promise((resolve) => window.setTimeout(resolve, 60));
      expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toEqual([
        expect.objectContaining({ payload: { performance_id: performance.performance_id, output_id: performance.live_audio.output_id, reason: "drained" } }),
      ]);
      expect(track.detach).toHaveBeenCalledWith(audio);
      expect(runtimeProblemCalls(request)).toHaveLength(0);
    } finally { await run.close(); }
  });

  it.each(["paused", "completed", "replacement"])("stops a live output immediately from a %s snapshot without acknowledging drainage", async (state) => {
    const audio = new CandidateFakeAudio();
    const { run, socket, mediaCallbacks, request } = await openPlaybackHarness({ liveAudio: audio });
    const performance = livePerformance();
    const track = liveTrack(performance);
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
      socket.emit("message", { data: JSON.stringify({ ...candidateEvent(2, "avatar.performance.started", performance), replayability: "transient" }) });
      mediaCallbacks.onRemoteAudioTrack(track, { trackSid: track.sid, trackName: performance.live_audio.track_name },
        { identity: performance.live_audio.publisher_identity });
      const staleEnded = audio.listener("ended");
      const staleError = audio.listener("error");
      socket.emit("message", { data: JSON.stringify(candidateEvent(3, "session.snapshot", {
        ...candidateSnapshot(), status: state === "replacement" ? "in_progress" : state,
        active_performance_id: state === "replacement" ? "new_output" : null,
      })) });
      staleEnded(); staleError();
      expect(audio.pause).toHaveBeenCalledOnce();
      expect(track.detach).toHaveBeenCalledOnce();
      expect(run.getSnapshot().avatar.status).toBe("idle");
      expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toHaveLength(0);
      expect(runtimeProblemCalls(request)).toHaveLength(0);
    } finally { await run.close(); }
  });

  it("does not replay the old live output after a control resync and fresh snapshot", async () => {
    const audio = new CandidateFakeAudio();
    const { run, socket, mediaCallbacks } = await openPlaybackHarness({ liveAudio: audio });
    const performance = livePerformance();
    const track = liveTrack(performance);
    const subscribe = () => mediaCallbacks.onRemoteAudioTrack(track,
      { trackSid: track.sid, trackName: performance.live_audio.track_name },
      { identity: performance.live_audio.publisher_identity });
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
      socket.emit("message", { data: JSON.stringify({ ...candidateEvent(2, "avatar.performance.started", performance), replayability: "transient" }) });
      subscribe();
      socket.emit("message", { data: JSON.stringify(candidateEvent(4, "floor.changed", { owner: "agent" })) });
      expect(audio.pause).toHaveBeenCalledOnce();
      socket.emit("message", { data: JSON.stringify(candidateEvent(5, "session.snapshot", {
        ...candidateSnapshot(), floor: "agent", active_performance_id: performance.performance_id,
      })) });
      subscribe();
      socket.emit("message", { data: JSON.stringify({ ...candidateEvent(6, "avatar.performance.started", performance), replayability: "transient" }) });
      expect(audio.play).toHaveBeenCalledOnce();
      expect(socket.sent.filter((item) => item.type === "avatar.performance.ready")).toHaveLength(1);
      expect(socket.sent.filter((item) => item.type === "avatar.performance.stopped")).toHaveLength(0);
    } finally { await run.close(); }
  });

  it("keeps URL playback idle until playing rather than the play request", async () => {
    const audio = new CandidateFakeAudio({ emitPlay: false });
    const { run, socket } = await openPlaybackHarness({ audios: [audio] });
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())) });
      socket.emit("message", { data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())) });
      await flushPlaybackEvents();
      audio.emit("play");
      expect(run.getSnapshot().avatar.status).toBe("idle");
      audio.emit("playing");
      expect(run.getSnapshot().avatar.status).toBe("speaking");
    } finally { await run.close(); }
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

  it("keeps formal audio open through silence and preparation, and only closes on server commit", async () => {
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
          { owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_formal" },
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
      capture.onSpeechStopped();
      socket.emit("message", { data: JSON.stringify(candidateEvent(3, "speech.stopped", {
        speaker: "candidate", endpoint_countdown_ms: 0, cancellable: true,
      })) });
      await vi.advanceTimersByTimeAsync(10_000);
      expect(run.getSnapshot().endpoint).toEqual({ active: true, deadlineAt: null });
      expect(run.getSnapshot().evidence.ready).toBe(true);
      expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(0);
      await run.act({ type: "finish_answer" });
      const submitted = latestSentSignal(socket, "finish_answer");
      expect(submitted.turn_id).toBe("turn_1");
      expect(submitted.payload).toMatchObject({ endpoint: "explicit", capture_id: "capture_formal" });
      await run.act({ type: "finish_answer" });
      expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(1);
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", evidence: { ready: true } });

      socket.emit("message", { data: JSON.stringify({
        ...candidateEvent(4, "floor.changed", {
          owner: "candidate", reason: "answer_preparing", capture_id: "capture_formal",
        }), turn_id: "turn_1",
      }) });
      socket.emit("message", { data: JSON.stringify(candidateEvent(5, "session.snapshot", { ...candidateSnapshot(), floor: "candidate" })) });
      expect(run.getSnapshot()).toMatchObject({ phase: "answer_preparing", floor: "candidate", evidence: { ready: true } });
      capture.onPcm(new Uint8Array([3, 0, 4, 0]).buffer);
      await vi.runAllTicks();
      expect(append).toHaveBeenCalledTimes(2);
      const speechCount = socket.sent.filter((item) => item.type === "speech.started").length;
      await capture.onSpeechStarted();
      expect(latestSentSignal(socket, "speech.started")).toMatchObject({
        turn_id: "turn_1", payload: { capture_id: "capture_formal" },
      });
      expect(socket.sent.filter((item) => item.type === "speech.started")).toHaveLength(speechCount + 1);
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", evidence: { ready: true } });
      await run.act({ type: "finish_answer" });
      await run.act({ type: "finish_answer" });
      expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(2);
      await run.act({ type: "continue_speaking" });
      expect(latestSentSignal(socket, "continue_speaking").payload.capture_id).toBe("capture_formal");
      socket.emit("message", { data: JSON.stringify(candidateEvent(6, "floor.changed", {
        owner: "none", reason: "answer_processing",
      })) });
      socket.emit("message", { data: JSON.stringify(candidateEvent(7, "session.snapshot", { ...candidateSnapshot(), floor: "candidate" })) });
      await capture.onSpeechStarted();
      capture.onSpeechStopped();
      capture.onPcm(new Uint8Array([5, 0, 6, 0]).buffer);
      await run.act({ type: "finish_answer" });
      await run.act({ type: "continue_speaking" });
      await vi.runAllTicks();
      expect(run.getSnapshot().phase).toBe("understanding");
      expect(run.getSnapshot().evidence.ready).toBe(false);
      expect(append).toHaveBeenCalledTimes(2);
      expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(2);
      expect(socket.sent.filter((item) => item.type === "speech.started")).toHaveLength(speechCount + 1);
      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      socket.emit("message", { data: JSON.stringify({
        ...candidateEvent(8, "floor.changed", {
          owner: "candidate", reason: "answer_preparing", capture_id: "capture_formal",
        }), turn_id: "turn_1",
      }) });
      expect(run.getSnapshot()).toMatchObject({ phase: "understanding", evidence: { ready: false } });
      socket.emit("message", { data: JSON.stringify(candidateEvent(9, "problem", {
        code: "UNDERSTANDING_RESULT_REJECTED", message: "面试已暂停", recoverable: false, action: "pause",
      })) });
      socket.emit("message", { data: JSON.stringify(candidateEvent(10, "problem", {
        code: "INTERVIEW_NOT_IN_PROGRESS", message: "请重试", recoverable: true, action: "retry_or_correct_signal",
      })) });
      await vi.runAllTicks();
      expect(run.getSnapshot().phase).toBe("paused");
      expect(run.getSnapshot().problem.code).toBe("UNDERSTANDING_RESULT_REJECTED");
      await run.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores preparation from another capture and lets new speech cancel automatic preparation", async () => {
    const { run, socket, capture } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(), floor: "candidate",
    })) });
    socket.emit("message", { data: JSON.stringify({
      ...correlatedCandidateEvent(2, "floor.changed", {
        owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_current",
      }, latestSentSignal(socket, "evidence.stream.open")), turn_id: "turn_1",
    }) });
    const floor = (sequence, reason, captureId = "capture_current", turnId = "turn_1") => socket.emit("message", {
      data: JSON.stringify({
        ...candidateEvent(sequence, "floor.changed", { owner: "candidate", reason, capture_id: captureId }),
        turn_id: turnId,
      }),
    });
    floor(3, "answer_preparing", "capture_old");
    floor(4, "answer_preparing", "capture_current", "turn_old");
    expect(run.getSnapshot().phase).toBe("listening");
    floor(5, "answer_preparing");
    expect(run.getSnapshot()).toMatchObject({ phase: "answer_preparing", evidence: { ready: true } });
    await capture.onSpeechStarted();
    expect(run.getSnapshot().phase).toBe("listening");
    expect(latestSentSignal(socket, "speech.started")).toMatchObject({
      turn_id: "turn_1", payload: { capture_id: "capture_current" },
    });
    capture.onSpeechStopped();
    floor(6, "answer_preparing");
    floor(7, "answer_listening");
    expect(run.getSnapshot()).toMatchObject({ phase: "listening", floor: "candidate", evidence: { ready: true } });
    expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(0);
    expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    await capture.onSpeechStarted();
    await run.act({ type: "finish_answer" });
    const beforeHintWithdrawn = socket.sent.filter((item) => item.type === "speech.started").length;
    await capture.onSpeechStarted();
    expect(socket.sent.filter((item) => item.type === "speech.started")).toHaveLength(beforeHintWithdrawn + 1);
    await run.act({ type: "finish_answer" });
    expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(2);
    await run.close();
  });

  it("keeps capture ready and permits retry if sending a finish hint fails", async () => {
    const { run, socket, capture } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(), floor: "candidate",
    })) });
    socket.emit("message", { data: JSON.stringify({
      ...correlatedCandidateEvent(2, "floor.changed", {
        owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_retry_hint",
      }, latestSentSignal(socket, "evidence.stream.open")), turn_id: "turn_1",
    }) });
    const originalSend = socket.send.bind(socket);
    const send = vi.spyOn(socket, "send").mockImplementationOnce(() => { throw new Error("control send failed"); });
    await expect(run.act({ type: "finish_answer" })).rejects.toThrow("control send failed");
    expect(run.getSnapshot()).toMatchObject({ phase: "listening", evidence: { requested: true, ready: true } });
    send.mockImplementation(originalSend);
    await run.act({ type: "finish_answer" });
    expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(1);
    await capture.onSpeechStarted();
    expect(latestSentSignal(socket, "speech.started").payload.capture_id).toBe("capture_retry_hint");
    await run.close();
  });

  it.each([
    ["UNDERSTANDING_UNAVAILABLE", "transcript.final"],
    ["TRANSCRIPT_UNAVAILABLE", "transcript.final"],
    ["ENDPOINT_UNCERTAIN", "transcript.final"],
    ["UNDERSTANDING_UNAVAILABLE", "floor.changed"],
    ["TRANSCRIPT_UNAVAILABLE", "floor.changed"],
    ["ENDPOINT_UNCERTAIN", "floor.changed"],
  ])("clears %s after the same formal answer reaches %s", async (code, completionEvent) => {
    const { run, socket } = await openPlaybackHarness();
    try {
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
        ...candidateSnapshot(), floor: "candidate",
      })) });
      socket.emit("message", { data: JSON.stringify({
        ...correlatedCandidateEvent(2, "floor.changed", {
          owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_recovered_prepare",
        }, latestSentSignal(socket, "evidence.stream.open")), turn_id: "turn_1",
      }) });
      socket.emit("message", { data: JSON.stringify({
        ...candidateEvent(3, "problem", {
          code, message: "暂时无法整理回答", recoverable: true,
          action: "continue_listening",
        }), turn_id: "turn_1",
      }) });
      expect(run.getSnapshot().evidence.ready).toBe(true);
      expect(run.getSnapshot().problem.code).toBe(code);
      socket.emit("message", { data: JSON.stringify({
        ...candidateEvent(4, "floor.changed", {
          owner: "candidate", reason: "answer_preparing", capture_id: "capture_recovered_prepare",
        }), turn_id: "turn_1",
      }) });
      expect(run.getSnapshot().phase).toBe("answer_preparing");
      socket.emit("message", { data: JSON.stringify({
        ...candidateEvent(5, completionEvent, completionEvent === "transcript.final" ? {
          text: "服务端已经确认这段回答。", confidence: 0.98,
          authoritative: true, persisted_audio: true,
        } : {
          owner: "none", reason: "answer_processing", capture_id: "capture_recovered_prepare",
        }), turn_id: "turn_1",
      }) });
      expect(run.getSnapshot().problem).toBeNull();
    } finally {
      await run.close();
    }
  });

  it.each(["turn_old", null])("does not clear an endpoint warning from an unscoped or different-turn final (%s)", async (turnId) => {
    const { run, emit } = await openFormalEndpointHarness();
    try {
      emit("problem", { code: "UNDERSTANDING_UNAVAILABLE", message: "暂时无法整理回答", recoverable: true, action: "continue_listening" });
      emit("transcript.final", { text: "旧轮次回答。", confidence: 0.9, authoritative: true, persisted_audio: true }, turnId);
      expect(run.getSnapshot().problem.code).toBe("UNDERSTANDING_UNAVAILABLE");
    } finally {
      await run.close();
    }
  });

  it.each([
    ["DETECTOR_UNAVAILABLE", true, "continue_listening"],
    ["LIVEKIT_EVIDENCE_TRACK_UNAVAILABLE", true, "reconnect_media"],
    ["UNDERSTANDING_UNAVAILABLE", false, "pause"],
  ])("does not treat answer progress as recovery from %s (recoverable=%s)", async (code, recoverable, action) => {
    const { run, emit } = await openFormalEndpointHarness();
    try {
      emit("problem", { code, message: "仍需处理的问题", recoverable, action });
      emit("floor.changed", { owner: "none", reason: "answer_processing", capture_id: "capture_endpoint" });
      emit("transcript.final", { text: "服务端确认的回答。", confidence: 0.9, authoritative: true, persisted_audio: true });
      expect(run.getSnapshot().problem).toMatchObject({ code, recoverable, action });
    } finally {
      await run.close();
    }
  });

  it("only clears a detector warning for a matching active capture without changing preparation or media gates", async () => {
    const { run, emit, socket } = await openFormalEndpointHarness();
    try {
      emit("problem", { code: "DETECTOR_UNAVAILABLE", message: "自动接话暂不可用", recoverable: true, action: "continue_listening" });
      emit("floor.changed", { owner: "candidate", reason: "answer_preparing", capture_id: "capture_endpoint" });
      const preparing = run.getSnapshot();
      emit("floor.changed", { owner: "candidate", reason: "answer_detector_ready", capture_id: "capture_old" });
      emit("floor.changed", { owner: "candidate", reason: "answer_detector_ready", capture_id: "capture_endpoint" }, "turn_old");
      emit("floor.changed", { owner: "candidate", reason: "answer_detector_ready" });
      emit("floor.changed", { owner: "agent", reason: "answer_detector_ready", capture_id: "capture_endpoint" });
      expect(run.getSnapshot()).toEqual(preparing);
      emit("floor.changed", { owner: "candidate", reason: "answer_detector_ready", capture_id: "capture_endpoint" });
      expect(run.getSnapshot()).toEqual({ ...preparing, problem: null });
      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    } finally {
      await run.close();
    }
  });

  it("does not reopen a committed capture or clear an unrelated warning on detector recovery", async () => {
    const { run, emit, capture } = await openFormalEndpointHarness();
    try {
      emit("problem", { code: "UNDERSTANDING_UNAVAILABLE", message: "暂时无法整理回答", recoverable: true, action: "continue_listening" });
      emit("floor.changed", { owner: "candidate", reason: "answer_detector_ready", capture_id: "capture_endpoint" });
      expect(run.getSnapshot().problem.code).toBe("UNDERSTANDING_UNAVAILABLE");
      emit("problem", { code: "DETECTOR_UNAVAILABLE", message: "自动接话暂不可用", recoverable: true, action: "continue_listening" });
      emit("floor.changed", { owner: "none", reason: "answer_processing", capture_id: "capture_endpoint" });
      const processing = run.getSnapshot();
      emit("floor.changed", { owner: "candidate", reason: "answer_detector_ready", capture_id: "capture_endpoint" });
      await capture.onSpeechStarted();
      expect(run.getSnapshot()).toEqual(processing);
      expect(run.getSnapshot()).toMatchObject({ phase: "understanding", evidence: { ready: false }, problem: { code: "DETECTOR_UNAVAILABLE" } });
    } finally {
      await run.close();
    }
  });

  it("does not close or relabel another capture on scoped processing", async () => {
    const { run, emit, socket } = await openFormalEndpointHarness();
    try {
      emit("problem", { code: "TRANSCRIPT_UNAVAILABLE", message: "暂未得到转写", recoverable: true, action: "continue_listening" });
      const listening = run.getSnapshot();
      emit("floor.changed", { owner: "none", reason: "answer_processing", capture_id: "capture_old" });
      expect(run.getSnapshot()).toEqual(listening);
      emit("floor.changed", { owner: "none", reason: "answer_processing", capture_id: "capture_endpoint" }, "turn_old");
      expect(run.getSnapshot()).toEqual(listening);
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", evidence: { ready: true }, problem: { code: "TRANSCRIPT_UNAVAILABLE" } });
      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    } finally {
      await run.close();
    }
  });

  it("retains warm-up's server-controlled automatic endpoint countdown", async () => {
    vi.useFakeTimers();
    try {
      const opening = openPlaybackHarness();
      await vi.runAllTicks();
      const { run, socket, capture } = await opening;
      socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
        ...candidateSnapshot(), floor: "candidate", calibration_status: "listening",
      })) });
      socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(2, "floor.changed", {
        owner: "candidate", reason: "warmup_stream_open", capture_id: "capture_warmup",
      }, latestSentSignal(socket, "evidence.stream.open"))) });
      await capture.onSpeechStarted();
      capture.onSpeechStopped();
      socket.emit("message", { data: JSON.stringify(candidateEvent(3, "speech.stopped", {
        speaker: "candidate", endpoint_countdown_ms: 2500, cancellable: true,
      })) });
      expect(run.getSnapshot().endpoint).toEqual({ active: true, deadlineAt: Date.now() + 2500 });
      await vi.advanceTimersByTimeAsync(2500);
      expect(socket.sent.filter((item) => item.type === "finish_answer")).toHaveLength(0);
      expect(run.getSnapshot().evidence.ready).toBe(true);
      await capture.onSpeechStarted();
      expect(run.getSnapshot().endpoint).toEqual({ active: false, deadlineAt: null });
      socket.emit("message", { data: JSON.stringify(candidateEvent(4, "transcript.final", {
        text: "这是试音。", confidence: 0.98, authoritative: true, persisted_audio: true, calibration: true,
      })) });
      expect(run.getSnapshot()).toMatchObject({
        calibration: { status: "awaiting_confirmation" }, evidence: { ready: false },
      });
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

  it("binds VAD to the acknowledged capture and discards pre-ready speech stops", async () => {
    const { run, socket, capture } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
      ...candidateSnapshot(), floor: "candidate",
    })) });
    await capture.onSpeechStarted();
    capture.onSpeechStopped();
    const firstOpen = latestSentSignal(socket, "evidence.stream.open");
    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(2, "floor.changed", {
      owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_first",
    }, firstOpen)) });
    expect(socket.sent.filter((item) => item.type === "speech.stopped")).toHaveLength(0);
    await capture.onSpeechStarted();
    capture.onSpeechStopped();
    expect(latestSentSignal(socket, "speech.started")).toMatchObject({
      turn_id: "turn_1", payload: { capture_id: "capture_first" },
    });
    expect(latestSentSignal(socket, "speech.stopped")).toMatchObject({
      turn_id: "turn_1", payload: { capture_id: "capture_first" },
    });
    socket.emit("message", { data: JSON.stringify(candidateEvent(3, "problem", {
      code: "STT_TRANSCRIPT_UNAVAILABLE", message: "未得到有效转写", recoverable: true, action: "continue_listening",
    })) });
    expect(run.getSnapshot()).toMatchObject({ phase: "preparing", evidence: { requested: false, ready: false } });
    socket.emit("message", { data: JSON.stringify(candidateEvent(4, "session.snapshot", {
      ...candidateSnapshot(), floor: "candidate",
    })) });
    const retryOpen = latestSentSignal(socket, "evidence.stream.open");
    expect(retryOpen.causation_id).not.toBe(firstOpen.causation_id);
    socket.emit("message", { data: JSON.stringify(correlatedCandidateEvent(5, "floor.changed", {
      owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_retry",
    }, retryOpen)) });
    expect(run.getSnapshot().problem).toBeNull();
    await capture.onSpeechStarted();
    capture.onSpeechStopped();
    expect(latestSentSignal(socket, "speech.stopped").payload.capture_id).toBe("capture_retry");
    await run.close();
  });

  it("clears warm-up captions before the formal interview starts", async () => {
    const { run, socket } = await openPlaybackHarness();
    socket.emit("message", { data: JSON.stringify(candidateEvent(1, "transcript.final", {
      text: "这是一段只用于试音的完整字幕",
      confidence: 0.96,
      calibration: true,
    })) });
    expect(run.getSnapshot()).toMatchObject({
      calibration: { status: "awaiting_confirmation" },
    });
    expect(run.getSnapshot().captions.full).toHaveLength(1);

    await run.act({ type: "warmup.confirm" });

    expect(socket.sent.filter((item) => item.type === "warmup.confirm")).toHaveLength(1);
    expect(run.getSnapshot().captions).toEqual({ forming: false, recent: [], full: [] });
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

  it("does not carry a pending avatar metric window into a reconnected control socket", async () => {
    vi.useFakeTimers();
    try {
      const first = new CandidateFakeSocket();
      const second = new CandidateFakeSocket();
      const audio = new CandidateFakeAudio();
      const { run } = await openPlaybackHarness({
        audios: [audio],
        sockets: [first, second],
      });
      first.emit("message", {
        data: JSON.stringify(candidateEvent(1, "session.snapshot", candidateSnapshot())),
      });
      first.emit("message", {
        data: JSON.stringify(candidateEvent(2, "avatar.performance.started", candidatePerformance())),
      });
      await vi.runAllTicks();

      expect(run.getSnapshot().avatar).toMatchObject({
        status: "speaking",
        performanceId: "performance_1",
      });
      expect(first.sent.filter((item) => item.type === "telemetry.observe")).toHaveLength(0);

      first.readyState = WebSocket.CLOSED;
      first.emit("close", {});
      await vi.advanceTimersByTimeAsync(400);
      await vi.runAllTicks();
      expect(second.sent.filter((item) => item.type === "session.open")).toHaveLength(1);

      await vi.advanceTimersByTimeAsync(1_000);

      expect(first.sent.filter((item) => item.type === "telemetry.observe")).toHaveLength(0);
      expect(second.sent.filter((item) => item.type === "telemetry.observe")).toHaveLength(0);
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

describe("candidate continuous capture recovery", () => {
  const recovery = (status = "recovering", captureId = "capture_endpoint") => ({
    status, turn_id: "turn_1", capture_id: captureId, attempt: status === "recovering" ? 1 : 3, max_attempts: 3,
  });
  const notice = (retry = false, captureId = "capture_endpoint") => ({
    code: retry ? "CAPTURE_RETRY_REQUIRED" : "CAPTURE_RECOVERING", message: "合成恢复通知",
    recoverable: true, action: retry ? "retry_answer" : "continue_listening", capture_id: captureId,
  });

  it("keeps PCM and VAD active during recovery, without receipt/partial/final clearing its warning", async () => {
    const { run, emit, socket, capture, ring } = await openFormalEndpointHarness();
    try {
      emit("floor.changed", { owner: "candidate", reason: "answer_recovering", capture_id: "capture_endpoint" });
      emit("problem", notice());
      expect(run.getSnapshot()).toMatchObject({ phase: "answer_recovering", floor: "candidate", evidence: { ready: true }, captureRecovery: { status: "recovering" } });
      capture.onPcm(new ArrayBuffer(640));
      await capture.onSpeechStarted();
      capture.onSpeechStopped();
      await flushPlaybackEvents();
      expect(ring.append).toHaveBeenCalledTimes(1);
      expect(latestSentSignal(socket, "speech.started").payload.capture_id).toBe("capture_endpoint");
      expect(latestSentSignal(socket, "speech.stopped").payload.capture_id).toBe("capture_endpoint");
      emit("speech.started", { speaker: "candidate", server_audio_received: true });
      emit("transcript.partial", { text: "合成临时前缀" });
      emit("transcript.final", { text: "合成迟到前缀", authoritative: true });
      emit("problem", { code: "UNDERSTANDING_UNAVAILABLE", message: "旧理解提示", recoverable: true, action: "continue_listening" });
      expect(run.getSnapshot()).toMatchObject({ phase: "answer_recovering", evidence: { ready: true }, captions: { forming: false }, problem: { code: "CAPTURE_RECOVERING" } });
      expect(ring.clear).not.toHaveBeenCalled();
      emit("floor.changed", { owner: "candidate", reason: "answer_listening", capture_id: "capture_endpoint" });
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", captureRecovery: null, problem: null, evidence: { ready: true } });
    } finally { await run.close(); }
  });

  it("stops exhausted input and retries once with exact turn/capture, clearing only on a new correlated ready", async () => {
    const { run, emit, socket, capture, ring } = await openFormalEndpointHarness();
    try {
      emit("problem", notice(true));
      emit("floor.changed", { owner: "none", reason: "answer_retry_required", capture_id: "capture_endpoint" });
      expect(run.getSnapshot()).toMatchObject({ phase: "answer_retry_required", floor: "none", evidence: { ready: false }, captureRecovery: { retryPending: false } });
      const before = socket.sent.length;
      capture.onPcm(new ArrayBuffer(640));
      await capture.onSpeechStarted();
      capture.onSpeechStopped();
      expect(socket.sent).toHaveLength(before);
      expect(ring.append).not.toHaveBeenCalled();
      expect(await run.act({ type: "continue_speaking", turnId: "turn_old" })).toBe(false);
      expect(await run.act({ type: "continue_speaking", payload: { capture_id: "capture_old" } })).toBe(false);
      const attempts = await Promise.all([run.act({ type: "continue_speaking" }), run.act({ type: "continue_speaking" })]);
      expect(attempts[1]).toBe(false);
      const retry = latestSentSignal(socket, "continue_speaking");
      expect(retry).toMatchObject({ turn_id: "turn_1", payload: { capture_id: "capture_endpoint" } });
      expect(socket.sent.filter((item) => item.type === "continue_speaking")).toHaveLength(1);
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(true);
      for (const [captureId, turnId, cause] of [
        ["capture_new", "turn_1", "foreign_cause"], ["capture_endpoint", "turn_1", retry.causation_id], ["capture_new", "turn_old", retry.causation_id],
      ]) emit("floor.changed", { owner: "candidate", reason: "evidence_stream_open", capture_id: captureId }, turnId, cause);
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(true);
      emit("floor.changed", { owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_new" }, "turn_1", retry.causation_id);
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", captureRecovery: null, problem: null, evidence: { ready: true } });
      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
      emit("problem", notice(true));
      emit("floor.changed", { owner: "none", reason: "answer_retry_required", capture_id: "capture_endpoint" });
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", captureRecovery: null });
      await capture.onSpeechStarted();
      expect(latestSentSignal(socket, "speech.started").payload.capture_id).toBe("capture_new");
    } finally { await run.close(); }
  });

  it.each([false, true])("preserves media backfill authorization and rejection while keeping the capture warning (reject=%s)", async (reject) => {
    const { run, emit, socket, capture, ring, mediaCallbacks, media } = await openFormalEndpointHarness();
    try {
      emit("problem", notice());
      const frames = [];
      ring.append.mockImplementation(async (sequence, audio) => frames.push({ sequence, audio, byteLength: audio.byteLength, capturedAt: Date.now() }));
      ring.replayRange = async () => frames;
      ring.acknowledgeThrough = vi.fn();
      const originalSend = socket.send.bind(socket);
      socket.send = (value) => {
        originalSend(value);
        const message = socket.sent.at(-1);
        if (!message.type.startsWith("evidence.recovery.")) return;
        if (reject) {
          emit("problem", { code: "LIVEKIT_EVIDENCE_RECOVERY_REJECTED", message: "合成媒体恢复拒绝", recoverable: true, action: "retry" }, "turn_1", message.causation_id);
          return;
        }
        const status = { "evidence.recovery.begin": "ready", "evidence.recovery.chunk": "acknowledged", "evidence.recovery.complete": "complete" }[message.type];
        emit("speech.started", { speaker: "candidate", server_audio_received: true,
          browser_backfill: { status, batch_id: "backfill_synthetic", audio_epoch: "epoch_synthetic", ack_through: status === "ready" ? 0 : 1 },
        }, "turn_1", message.causation_id);
      };
      mediaCallbacks.onState("reconnecting");
      capture.onPcm(new ArrayBuffer(640));
      await flushPlaybackEvents();
      mediaCallbacks.onState("connected");
      emit("problem", { code: "LIVEKIT_EVIDENCE_TRACK_MUTED", message: "合成媒体恢复授权", recoverable: true, action: "reconnect_media" });
      await flushPlaybackEvents();
      expect(socket.sent.some((item) => item.type === "evidence.recovery.begin")).toBe(true);
      if (reject) {
        expect(run.getSnapshot()).toMatchObject({ phase: "paused", problem: { recoverable: false } });
        expect(socket.sent.some((item) => item.type === "evidence.recovery.complete")).toBe(false);
      } else {
        expect(socket.sent.filter((item) => item.type.startsWith("evidence.recovery.")).map((item) => item.type)).toEqual([
          "evidence.recovery.begin", "evidence.recovery.chunk", "evidence.recovery.complete",
        ]);
        expect(media.setRecoveryMute).toHaveBeenLastCalledWith(false);
        expect(run.getSnapshot()).toMatchObject({ phase: "answer_recovering", problem: { code: "CAPTURE_RECOVERING" }, evidence: { ready: true } });
      }
    } finally { await run.close(); }
  });

  it("restores recovery directly from a fresh control snapshot without reopening or submitting", async () => {
    for (const status of ["recovering", "retry_required"]) {
      const { run, socket, capture, ring } = await openPlaybackHarness();
      try {
        socket.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
          ...candidateSnapshot(), floor: status === "recovering" ? "candidate" : "none", capture_recovery: recovery(status),
        })) });
        expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(0);
        expect(run.getSnapshot()).toMatchObject({ phase: status === "recovering" ? "answer_recovering" : "answer_retry_required",
          captureRecovery: { status, turnId: "turn_1", captureId: "capture_endpoint", maxAttempts: 3 } });
        capture.onPcm(new ArrayBuffer(640));
        await capture.onSpeechStarted();
        await flushPlaybackEvents();
        expect(ring.append).toHaveBeenCalledTimes(status === "recovering" ? 1 : 0);
        if (status === "retry_required") {
          await run.act({ type: "continue_speaking" });
          expect(latestSentSignal(socket, "continue_speaking").payload.capture_id).toBe("capture_endpoint");
        } else {
          socket.emit("message", { data: JSON.stringify(candidateEvent(2, "session.snapshot", {
            ...candidateSnapshot(), floor: "candidate", capture_recovery: null,
          })) });
          expect(run.getSnapshot()).toMatchObject({ phase: "listening", captureRecovery: null, problem: null });
        }
      } finally { await run.close(); }
    }
  });

  it("does not let recovery or late activity overwrite a genuine server pause", async () => {
    const { run, emit, capture } = await openFormalEndpointHarness();
    try {
      emit("speech.started", { speaker: "candidate", server_audio_received: true });
      emit("transcript.partial", { text: "暂停前合成字幕" });
      emit("problem", { code: "EVIDENCE_OWNER_FENCED", message: "真实严重故障", recoverable: false, action: "pause_or_human_takeover" });
      emit("session.snapshot", { ...candidateSnapshot(), status: "paused", floor: "none", capture_recovery: recovery() });
      emit("problem", notice());
      emit("floor.changed", { owner: "candidate", reason: "answer_recovering", capture_id: "capture_endpoint" });
      emit("speech.started", { speaker: "candidate", server_audio_received: true });
      emit("transcript.partial", { text: "迟到合成字幕" });
      emit("transcript.final", { text: "迟到合成尾句", authoritative: true });
      await capture.onSpeechStarted();
      expect(run.getSnapshot()).toMatchObject({ phase: "paused", captureRecovery: null, captions: { forming: false },
        serverAudio: { received: false }, microphone: { localDetected: false }, problem: { code: "EVIDENCE_OWNER_FENCED" } });
    } finally { await run.close(); }
  });

  it("keeps the retry warning through partials, receipts and unrelated or old-scope problems", async () => {
    const { run, emit } = await openFormalEndpointHarness();
    try {
      emit("problem", notice(true));
      const before = run.getSnapshot();
      emit("transcript.partial", { text: "迟到合成前缀" });
      emit("speech.started", { speaker: "candidate", server_audio_received: true });
      emit("problem", notice(false, "capture_old"));
      emit("problem", notice(), "turn_old");
      emit("problem", { code: "STT_TRANSCRIPT_UNAVAILABLE", message: "迟到提示", recoverable: true, action: "continue_listening" });
      emit("floor.changed", { owner: "candidate", reason: "answer_listening", capture_id: "capture_endpoint" });
      expect(run.getSnapshot()).toEqual(before);
    } finally { await run.close(); }
  });

  it("unlocks retry if sending fails, and a changed turn snapshot removes the old retry scope", async () => {
    const { run, emit, socket } = await openFormalEndpointHarness();
    try {
      emit("problem", notice(true));
      const originalSend = socket.send;
      socket.send = () => { throw new Error("synthetic send failure"); };
      await expect(run.act({ type: "continue_speaking" })).rejects.toThrow("synthetic send failure");
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(false);
      socket.send = originalSend;
      emit("session.snapshot", { ...candidateSnapshot(), floor: "agent", current_turn_id: "turn_2", capture_recovery: null,
        current_question: { ...candidateSnapshot().current_question, turn_id: "turn_2" } });
      expect(run.getSnapshot().captureRecovery).toBeNull();
      expect(await run.act({ type: "continue_speaking", turnId: "turn_1" })).toBe(false);
      expect(socket.sent.filter((item) => item.type === "continue_speaking")).toHaveLength(0);
    } finally { await run.close(); }
  });

  it("unlocks an explicitly failed retry only for its matching problem ACK after the durable snapshot", async () => {
    const { run, emit, socket } = await openFormalEndpointHarness();
    try {
      emit("problem", notice(true));
      await run.act({ type: "continue_speaking" });
      const first = latestSentSignal(socket, "continue_speaking");
      const retrySnapshot = { ...candidateSnapshot(), floor: "none", capture_recovery: recovery("retry_required") };
      emit("session.snapshot", retrySnapshot, "turn_1", first.causation_id);
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(true);
      emit("problem", notice(true), "turn_1", first.causation_id);
      expect(run.getSnapshot()).toMatchObject({ phase: "answer_retry_required", evidence: { ready: false },
        captureRecovery: { status: "retry_required", retryPending: false }, problem: { code: "CAPTURE_RETRY_REQUIRED" } });

      await run.act({ type: "continue_speaking" });
      const second = latestSentSignal(socket, "continue_speaking");
      expect(second.causation_id).not.toBe(first.causation_id);
      expect(socket.sent.filter((item) => item.type === "continue_speaking")).toHaveLength(2);
      emit("session.snapshot", retrySnapshot, "turn_1", first.causation_id);
      emit("problem", notice(true), "turn_1", first.causation_id);
      emit("problem", notice(true, "capture_old"), "turn_1", second.causation_id);
      emit("problem", notice(true), "turn_old", second.causation_id);
      emit("problem", notice(), "turn_1", second.causation_id);
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(true);
      expect(await run.act({ type: "continue_speaking" })).toBe(false);

      emit("session.snapshot", retrySnapshot, "turn_1", second.causation_id);
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(true);
      emit("problem", notice(true), "turn_1", second.causation_id);
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(false);
      expect(run.getSnapshot().phase).toBe("answer_retry_required");
      expect(socket.sent.filter((item) => item.type === "evidence.stream.open")).toHaveLength(1);
    } finally { await run.close(); }
  });

  it("restores a retry-required snapshot after disconnect without replaying the unacknowledged retry", async () => {
    vi.useFakeTimers();
    let run;
    try {
      const first = new CandidateFakeSocket(), second = new CandidateFakeSocket();
      ({ run } = await openPlaybackHarness({ sockets: [first, second] }));
      first.emit("message", { data: JSON.stringify(candidateEvent(1, "session.snapshot", {
        ...candidateSnapshot(), floor: "none", capture_recovery: recovery("retry_required"),
      })) });
      await run.act({ type: "continue_speaking" });
      const original = latestSentSignal(first, "continue_speaking");
      first.emit("message", { data: JSON.stringify(candidateEvent(2, "session.snapshot", {
        ...candidateSnapshot(), floor: "none", capture_recovery: recovery("retry_required"),
      })) });
      expect(run.getSnapshot().captureRecovery.retryPending).toBe(true);
      first.readyState = WebSocket.CLOSED;
      first.emit("close", {});
      await vi.advanceTimersByTimeAsync(400);
      await vi.runAllTicks();
      second.emit("message", { data: JSON.stringify(candidateEvent(3, "session.snapshot", {
        ...candidateSnapshot(), floor: "none", capture_recovery: recovery("retry_required"),
      })) });
      expect(run.getSnapshot()).toMatchObject({ phase: "answer_retry_required", captureRecovery: { retryPending: false }, evidence: { ready: false } });
      expect(second.sent.filter((item) => ["continue_speaking", "evidence.stream.open"].includes(item.type))).toHaveLength(0);
      await run.act({ type: "continue_speaking" });
      const retry = latestSentSignal(second, "continue_speaking");
      expect(retry.causation_id).not.toBe(original.causation_id);
      expect(retry.payload.capture_id).toBe("capture_endpoint");
      second.emit("message", { data: JSON.stringify({ ...candidateEvent(4, "floor.changed", {
        owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_reconnected",
      }), turn_id: "turn_1", causation_id: retry.causation_id }) });
      expect(run.getSnapshot()).toMatchObject({ phase: "listening", captureRecovery: null, problem: null, evidence: { ready: true } });
    } finally { await run?.close(); vi.useRealTimers(); }
  });
});

async function openFormalEndpointHarness(options = {}) {
  const harness = await openPlaybackHarness(options);
  let sequence = 0;
  const emit = (type, payload, turnId = "turn_1", causationId = null) => harness.socket.emit("message", {
    data: JSON.stringify({ ...candidateEvent(++sequence, type, payload), turn_id: turnId, causation_id: causationId }),
  });
  emit("session.snapshot", { ...candidateSnapshot(), floor: "candidate" });
  emit("floor.changed", { owner: "candidate", reason: "evidence_stream_open", capture_id: "capture_endpoint" },
    "turn_1", latestSentSignal(harness.socket, "evidence.stream.open").causation_id);
  expect(harness.run.getSnapshot().evidence.ready).toBe(true);
  return { ...harness, emit };
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

function livePerformance() {
  return { ...candidatePerformance(), audio_uri: null, delivery: "streaming_tts",
    live_audio: { output_id: "output_1", publisher_identity: "expression:synthetic",
      track_sid: "TR_synthetic", track_name: "speech_output_1", sample_rate_hz: 24_000, channels: 1 } };
}

function liveTrack(performance) {
  return { sid: performance.live_audio.track_sid, kind: "audio", attach: vi.fn(), detach: vi.fn() };
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
      if (this.emitPlay) { this.emit("play"); this.emit("playing"); }
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

async function openPlaybackHarness({ audios = [], sockets = null, liveAudio = null } = {}) {
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
  let mediaCallbacks;
  const queue = [...audios];
  const audioFactory = vi.fn((url) => {
    const audio = queue.shift() || new CandidateFakeAudio();
    audio.src = url;
    return audio;
  });
  const ring = {
    append: vi.fn(), clear: vi.fn(),
    snapshot: () => ({ retainedFrames: 0, unacknowledgedFrames: 0, encryptedBytes: 0, retentionMs: 30_000 }),
  };
  const media = { publishedSources: ["microphone", "camera"], close: vi.fn(), setRecoveryMute: vi.fn() };
  const experience = createCandidateInterviewExperience({
    request,
    socketFactory: () => sockets?.[socketIndex++] || socket,
    mediaConnector: async (callbacks) => {
      mediaCallbacks = callbacks;
      return media;
    },
    audioCaptureFactory: async (_stream, callbacks) => {
      capture = callbacks;
      return { close: vi.fn() };
    },
    ringFactory: () => ring,
    verifyAvatar: async () => ({ ready: true }),
    audioFactory,
    liveAudioFactory: () => liveAudio || new CandidateFakeAudio(),
  });
  const run = await experience.open({ interviewId: "iv_test", ticket: "candidate-token" });
  return { run, socket, capture, request, audioFactory, mediaCallbacks, media, ring };
}

function runtimeProblemCalls(request) {
  return request.mock.calls.filter(([path]) => path.endsWith("/runtime-problems"));
}

async function flushPlaybackEvents() {
  await Promise.resolve();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
}
