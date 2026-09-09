import { createCandidateAudioCapture } from "./audio-worklet-capture.js";
import { EncryptedAudioRingBuffer } from "./encrypted-audio-ring.js";
import { claimPreparedCandidateMedia, runCandidatePreflight } from "./preflight.js";
import { OrderedAgentEventStream } from "../interviews/agent-event-runtime.js";
import { LiveSpeechPlayback } from "./live-speech-playback.js";

const EMPTY_AVATAR = {
  status: "idle",
  performanceId: null,
  viseme: "sil",
  visemeWeight: 0,
  gesture: "idle",
  gestureIntensity: 0,
};

const WINDOWED_AVATAR_METRICS = new Set([
  "avatar_viseme_drift_ms",
  "avatar_freeze_ms",
]);
const AVATAR_METRIC_WINDOW_MS = 1_000;
const ANSWER_PREPARATION_WARNING_CODES = new Set([
  "UNDERSTANDING_UNAVAILABLE", "UNDERSTANDING_RETRY_EXHAUSTED", "TRANSCRIPT_UNAVAILABLE", "ENDPOINT_UNCERTAIN",
]);
const DETECTOR_WARNING_CODES = new Set(["DETECTOR_UNAVAILABLE"]);
const CAPTURE_RECOVERY_CODES = new Set(["CAPTURE_RECOVERING", "CAPTURE_RETRY_REQUIRED"]);
const CAPTURE_RECOVERY_MESSAGES = {
  recovering: "正在恢复语音识别，音频仍在保留。",
  retry_required: "本题收音未能恢复，请重试本题；不会提交不完整回答。",
};

/**
 * Stable read model exposed by CandidateInterviewExperience.subscribe().
 * Transport envelopes and provider payloads never escape through this view.
 *
 * @typedef {Object} CandidateExperienceView
 * @property {string} phase
 * @property {"agent"|"candidate"|"human"|"none"} floor
 * @property {Object|null} session
 * @property {Object|null} currentQuestion
 * @property {MediaStream|null} mediaStream
 * @property {Object|null} mediaPolicy
 * @property {{control:string, media:string, recoveryAdapter:Object|null}} connection
 * @property {{enabled:boolean, localDetected:boolean, level:number}} microphone
 * @property {{received:boolean, receivedAt:string|null}} serverAudio
 * @property {{requested:boolean, ready:boolean}} evidence
 * @property {Object|null} captureRecovery
 * @property {{forming:boolean, recent:Array, full:Array}} captions
 * @property {{active:boolean, deadlineAt:number|null}} endpoint
 * @property {{status:string, transcript:string, confidence:number|null}} calibration
 * @property {{status:string, performanceId:string|null, viseme:string, visemeWeight:number, gesture:string, gestureIntensity:number}} avatar
 * @property {Object|null} problem
 * @property {Object|null} completion
 * @property {{retainedFrames:number, unacknowledgedFrames:number, encryptedBytes:number, retentionMs:number}} recovery
 */

/** @param {unknown} value @returns {value is CandidateExperienceView} */
export function isCandidateExperienceView(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const required = [
    "phase", "floor", "session", "currentQuestion", "mediaStream", "mediaPolicy",
    "connection", "microphone", "serverAudio", "evidence", "captions", "endpoint",
    "calibration", "avatar", "problem", "completion", "recovery",
  ];
  if (!required.every((key) => Object.hasOwn(value, key))) return false;
  return typeof value.phase === "string"
    && ["agent", "candidate", "human", "none"].includes(value.floor)
    && isViewObject(value.connection)
    && isViewObject(value.microphone)
    && isViewObject(value.serverAudio)
    && isViewObject(value.evidence)
    && isViewObject(value.captions)
    && isViewObject(value.endpoint)
    && isViewObject(value.calibration)
    && isViewObject(value.avatar)
    && isViewObject(value.recovery)
    && Array.isArray(value.captions.recent)
    && Array.isArray(value.captions.full);
}

export function createCandidateInterviewExperience({
  apiBase = "/api/v1",
  request,
  socketFactory = (url) => new WebSocket(url),
  mediaConnector = connectLiveKitMedia,
  audioCaptureFactory = createCandidateAudioCapture,
  ringFactory = () => new EncryptedAudioRingBuffer(),
  verifyAvatar = (context) => import("./vrm-avatar.js").then(
    (module) => module.verifyLicensedVrmAsset(context),
  ),
  audioFactory = (url) => new Audio(url),
  liveAudioFactory = () => new Audio(),
  clock = () => Date.now(),
} = {}) {
  if (typeof request !== "function") throw new TypeError("CandidateInterviewExperience requires a request adapter");
  return {
    async open({ interviewId, ticket }) {
      const run = new CandidateExperienceRun({
        interviewId,
        candidateSessionToken: ticket,
        apiBase,
        request,
        socketFactory,
        mediaConnector,
        audioCaptureFactory,
        ringFactory,
        verifyAvatar,
        audioFactory,
        liveAudioFactory,
        clock,
      });
      await run.open();
      return run.interface();
    },
  };
}

export function reportCandidateRuntimeProblem({
  apiBase = "/api/v1",
  request,
  interviewId,
  candidateSessionToken,
  code = "CANDIDATE_RUNTIME_FAILED",
} = {}) {
  if (typeof request !== "function") {
    return Promise.reject(new TypeError("Candidate runtime problem reporting requires a request adapter"));
  }
  return request(`${apiBase}/public/interviews/${encodeURIComponent(interviewId || "")}/runtime-problems`, {
    method: "POST",
    headers: { "X-Candidate-Session-Token": candidateSessionToken || "" },
    body: { code },
  });
}

export function createCandidateMetricReporter({
  emit,
  isOpen = () => true,
  setTimer = (callback, delay) => window.setTimeout(callback, delay),
  clearTimer = (timer) => window.clearTimeout(timer),
  windowMs = AVATAR_METRIC_WINDOW_MS,
} = {}) {
  if (typeof emit !== "function") throw new TypeError("Candidate metric reporter requires an emit adapter");
  const windows = new Map();

  const send = (metric, value) => {
    if (!isOpen()) return;
    try { emit(metric, value); } catch { /* best-effort telemetry must never affect the interview */ }
  };

  return Object.freeze({
    observe(metric, value) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric) || numeric < 0 || numeric > 300_000 || !isOpen()) return;
      if (!WINDOWED_AVATAR_METRICS.has(metric)) {
        send(metric, numeric);
        return;
      }
      const current = windows.get(metric);
      if (current) {
        current.maximum = Math.max(current.maximum, numeric);
        return;
      }
      const pending = { maximum: numeric, timer: 0 };
      pending.timer = setTimer(() => {
        if (windows.get(metric) !== pending) return;
        windows.delete(metric);
        send(metric, pending.maximum);
      }, windowMs);
      windows.set(metric, pending);
    },
    clear() {
      for (const pending of windows.values()) clearTimer(pending.timer);
      windows.clear();
    },
  });
}

class CandidateExperienceRun {
  constructor(options) {
    Object.assign(this, options);
    this.subscribers = new Set();
    this.socket = null;
    this.media = null;
    this.capture = null;
    this.stream = null;
    this.ring = null;
    this.audio = null;
    this.activePlayback = null;
    this.liveSpeech = new LiveSpeechPlayback({ audioFactory: this.liveAudioFactory });
    this.performanceFrame = 0;
    this.performance = null;
    this.currentAct = null;
    this.closed = false;
    this.intentionalClose = false;
    this.evidenceOpen = false;
    this.evidenceReady = false;
    this.evidenceOpenCausationId = null;
    this.speechStartSignaled = false;
    this.pendingSpeechStop = false;
    this.warmupRetryRequested = false;
    this.warmupRetryCausationId = null;
    this.acknowledgedWarmupRetryCausationIds = new Set();
    this.evidenceReassertPending = false;
    this.sequence = 0;
    this.eventStream = new OrderedAgentEventStream({ audience: "candidate" });
    this.lastSentSequence = 0;
    this.evidenceTurnId = null;
    this.endpointTimer = 0;
    this.answerFinishRequested = false;
    this.answerSubmissionPending = false;
    this.answerPreparation = null;
    this.captureRetryCausationId = null;
    this.endpointProblemScope = null;
    this.heartbeatTimer = 0;
    this.reconnectTimer = 0;
    this.eventResyncTimer = 0;
    this.reconnectAttempt = 0;
    this.encryptQueue = Promise.resolve();
    this.backfillWaiters = new Map();
    this.mediaGap = null;
    this.serverBackfillAuthorized = false;
    this.backfillAuthorizationTimer = 0;
    this.mediaRecoveryRunning = false;
    this.mediaConnectionState = "checking";
    this.localSpeechStartedAt = null;
    this.localSpeechStoppedAt = null;
    this.evidenceOpenedAt = null;
    this.partialObserved = false;
    this.finalReceivedAt = null;
    this.ticketResponse = null;
    this.capabilityReport = null;
    this.metricReporter = createCandidateMetricReporter({
      emit: (metric, value) => this.sendSignal("telemetry.observe", { metric, value }),
      isOpen: () => (
        !this.closed
        && this.socket?.readyState === WebSocket.OPEN
      ),
    });
    this.state = {
      phase: "connecting",
      floor: "none",
      session: null,
      currentQuestion: null,
      mediaStream: null,
      mediaPolicy: null,
      connection: { control: "connecting", media: "checking", recoveryAdapter: null },
      microphone: { enabled: true, localDetected: false, level: 0 },
      serverAudio: { received: false, receivedAt: null },
      evidence: { requested: false, ready: false },
      captureRecovery: null,
      captions: { forming: false, recent: [], full: [] },
      endpoint: { active: false, deadlineAt: null },
      calibration: { status: "pending", transcript: "", confidence: null, retryRequired: false },
      avatar: { ...EMPTY_AVATAR },
      problem: null,
      completion: null,
      recovery: { retainedFrames: 0, unacknowledgedFrames: 0, encryptedBytes: 0, retentionMs: 30_000 },
    };
  }

  interface() {
    return Object.freeze({
      subscribe: (listener) => this.subscribe(listener),
      act: (action) => this.act(action),
      close: () => this.close("candidate_closed"),
      getSnapshot: () => this.snapshot(),
    });
  }

  async open() {
    try {
      if (!this.interviewId || !this.candidateSessionToken) throw new Error("面试会话凭据不完整");
      await this.verifyAvatar({
        apiBase: this.apiBase,
        interviewId: this.interviewId,
        candidateSessionToken: this.candidateSessionToken,
      });
      const prepared = claimPreparedCandidateMedia();
      if (prepared) {
        this.stream = prepared.stream;
        this.capabilityReport = prepared.report;
      } else {
        const result = await runCandidatePreflight({ probeUrl: "/healthz" });
        this.stream = result.stream;
        const remembered = readRememberedPreflight();
        this.capabilityReport = { ...result.report, speaker_verified: Boolean(remembered?.speaker_verified) };
      }
      if (!this.capabilityReport?.speaker_verified) {
        throw new Error("扬声器尚未由候选人在设备预检中确认，正式面试不会绕过该检查");
      }
      this.ring = this.ringFactory();
      this.patch({ mediaStream: this.stream });
      this.ticketResponse = await this.issueTicket();
      this.patch({
        mediaPolicy: this.ticketResponse.media,
        connection: {
          ...this.state.connection,
          recoveryAdapter: this.ticketResponse.media?.recovery || null,
        },
      });
      await this.connectMedia(this.ticketResponse.media);
      if (typeof this.media?.measureNetwork === "function") {
        const rtcNetwork = await this.media.measureNetwork();
        this.capabilityReport = {
          ...this.capabilityReport,
          network_rtt_ms: rtcNetwork.rttMs,
          network_jitter_ms: rtcNetwork.jitterMs,
        };
        if (rtcNetwork.rttMs > 500 || rtcNetwork.jitterMs > 100) {
          throw new Error("LiveKit 实时网络 RTT 或抖动未达到正式面试门槛");
        }
      }
      await this.connectControl(this.ticketResponse);
      if (this.media) {
        this.sendSignal("media.published", {
          provider: "livekit",
          sources: this.media.publishedSources || ["microphone"],
        });
      }
      this.capture = await this.audioCaptureFactory(this.stream, {
        onPcm: (frame) => this.onPcm(frame),
        onLevel: (rms) => this.onLevel(rms),
        onSpeechStarted: (details) => this.onLocalSpeechStarted(details),
        onSpeechStopped: () => this.onLocalSpeechStopped(),
        isAgentSpeaking: () => this.state.floor === "agent" || Boolean(this.activePlayback),
      });
      this.heartbeatTimer = window.setInterval(() => {
        if (this.socket?.readyState === WebSocket.OPEN) this.sendSignal("ping", {});
      }, 20_000);
      return this;
    } catch (error) {
      await this.close("open_failed");
      throw error;
    }
  }

  subscribe(listener) {
    if (typeof listener !== "function") throw new TypeError("subscriber must be a function");
    this.subscribers.add(listener);
    listener(this.snapshot());
    return () => this.subscribers.delete(listener);
  }

  async act({ type, idempotencyKey, turnId, payload = {} } = {}) {
    if (!type) throw new Error("候选人动作缺少 type");
    if (type === "retry_speech") return this.retrySpeech();
    let causationId = null;
    let warmupConfirmRollback = null;
    let finishRollback = null;
    if (type === "continue_speaking") {
      const recovery = this.state.captureRecovery;
      if (recovery?.status === "retry_required") {
        if (this.captureRetryCausationId || this.isSafetyStopped()
          || recovery.turnId !== this.state.currentQuestion?.turn_id
          || (turnId && turnId !== recovery.turnId)
          || (payload.capture_id && payload.capture_id !== recovery.captureId)) return false;
        turnId = recovery.turnId;
        payload = { ...payload, capture_id: recovery.captureId };
        causationId = uniqueId("candidate");
        this.captureRetryCausationId = causationId;
        this.patch({ captureRecovery: { ...recovery, retryPending: true } });
      } else {
        if (!this.evidenceReady || this.answerSubmissionPending || this.isSafetyStopped()) return false;
        payload = { ...payload, capture_id: this.evidenceCaptureId };
        this.answerFinishRequested = false;
        window.clearTimeout(this.endpointTimer);
        this.endpointTimer = 0;
        this.patch({ endpoint: { active: false, deadlineAt: null }, phase: recovery?.status === "recovering" ? "answer_recovering" : "listening" });
      }
    }
    if (type === "finish_answer") {
      if (
        !this.evidenceReady || this.answerSubmissionPending || this.answerFinishRequested
        || !["listening", "answer_preparing", "awaiting_supplement"].includes(this.state.phase)
      ) return false;
      payload = { ...payload, endpoint: "explicit", capture_id: this.evidenceCaptureId };
      turnId = this.evidenceTurnId;
      finishRollback = {
        phase: this.state.phase,
        endpoint: { ...this.state.endpoint },
      };
      // This is an optional completion hint, not permission to drop audio.
      // The server alone closes the gate with answer_processing; new speech
      // can withdraw this hint while speculative preparation is in flight.
      this.answerFinishRequested = true;
      window.clearTimeout(this.endpointTimer);
      this.endpointTimer = 0;
      this.patch({ endpoint: { active: false, deadlineAt: null } });
    }
    if (type === "pause") this.stopPerformance("candidate_pause");
    if (type === "warmup.confirm") {
      warmupConfirmRollback = {
        calibration: { ...this.state.calibration },
        captions: {
          forming: this.state.captions.forming,
          recent: [...this.state.captions.recent],
          full: [...this.state.captions.full],
        },
        phase: this.state.phase,
      };
      this.patch({
        calibration: { ...this.state.calibration, status: "confirming" },
        captions: { forming: false, recent: [], full: [] },
        phase: "understanding",
      });
    }
    if (type === "warmup.retry") {
      this.ring?.clear();
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.speechStartSignaled = false;
      this.pendingSpeechStop = false;
      this.warmupRetryRequested = true;
      causationId = uniqueId("candidate");
      this.warmupRetryCausationId = causationId;
      this.evidenceReassertPending = false;
      this.evidenceTurnId = null;
      this.patch({
        calibration: { status: "retrying", transcript: "", confidence: null, retryRequired: false },
        evidence: { requested: false, ready: false },
        captions: { forming: false, recent: [], full: [] },
        endpoint: { active: false, deadlineAt: null },
        problem: null,
        phase: "preparing",
      });
    }
    try {
      return this.sendSignal(type, payload, {
        idempotencyKey,
        turnId: turnId || this.state.currentQuestion?.turn_id,
        causationId,
      });
    } catch (error) {
      if (type === "continue_speaking" && causationId === this.captureRetryCausationId) {
        this.captureRetryCausationId = null;
        if (this.state.captureRecovery) this.patch({ captureRecovery: { ...this.state.captureRecovery, retryPending: false } });
      }
      if (finishRollback) {
        this.answerFinishRequested = false;
        this.patch(finishRollback);
      }
      if (type === "warmup.confirm" && warmupConfirmRollback) {
        this.patch(warmupConfirmRollback);
      }
      if (type === "warmup.retry") {
        this.warmupRetryRequested = false;
        this.warmupRetryCausationId = null;
        this.patch({
          calibration: { ...this.state.calibration, retryRequired: true },
        });
      }
      throw error;
    }
  }

  async issueTicket() {
    return this.request(`${this.apiBase}/public/interviews/${encodeURIComponent(this.interviewId)}/agent-ticket`, {
      method: "POST",
      headers: { "X-Candidate-Session-Token": this.candidateSessionToken },
    });
  }

  async connectMedia(media) {
    if (media?.status !== "ready") {
      throw new Error("WebRTC 媒体面不可用，正式面试已暂停并等待人工接管；不会自动降级为问卷或 PCM 模式");
    }
    try {
      this.media = await this.mediaConnector({
        media,
        stream: this.stream,
        onState: (value) => this.onMediaState(value),
        onRemoteAudioTrack: (track, publication, participant) => this.liveSpeech.trackSubscribed(track, publication, participant),
        onRemoteAudioTrackRemoved: (track) => this.liveSpeech.trackUnsubscribed(track),
      });
      this.mediaConnectionState = "connected";
      this.patch({ connection: { ...this.state.connection, media: "connected" } });
    } catch (error) {
      throw new Error(`WebRTC 媒体面连接失败：${error.message || error}`);
    }
  }

  onMediaState(value) {
    this.mediaConnectionState = value;
    this.patch({ connection: { ...this.state.connection, media: value } });
    if (this.closed || this.intentionalClose || this.state.completion) return;
    if (["reconnecting", "disconnected"].includes(value)) {
      if (this.performance?.delivery === "streaming_tts") this.stopPerformance("media_reconnecting", false);
      this.liveSpeech.reset();
      this.media?.setRecoveryMute?.(true);
      if (!this.mediaGap && this.evidenceOpen) {
        const capability = this.ticketResponse?.media?.recovery?.browser_backfill;
        if (capability?.enabled) {
          this.serverBackfillAuthorized = false;
          window.clearTimeout(this.backfillAuthorizationTimer);
          this.backfillAuthorizationTimer = 0;
          this.mediaGap = {
            sourceConnectionId: capability.connection_id,
            audioEpoch: capability.audio_epoch,
            firstSequence: this.lastSentSequence + 1,
            startedAt: this.clock(),
          };
        }
      }
      return;
    }
    if (value === "connected" && this.mediaGap) {
      if (this.serverBackfillAuthorized) {
        this.recoverMediaGap().catch((error) => this.failClosed(error));
      } else {
        this.armBackfillAuthorizationDeadline();
      }
    }
  }

  armBackfillAuthorizationDeadline() {
    if (!this.mediaGap || this.backfillAuthorizationTimer) return;
    const remainingMs = Math.max(
      1,
      Math.min(30_000, this.mediaGap.startedAt + 30_000 - this.clock()),
    );
    this.backfillAuthorizationTimer = window.setTimeout(() => {
      this.backfillAuthorizationTimer = 0;
      if (this.mediaGap && !this.serverBackfillAuthorized) {
        this.failClosed(new Error("服务端未确认媒体缺口，浏览器不会擅自回放音频；面试已暂停并等待人工处理"));
      }
    }, remainingMs);
  }

  async recoverMediaGap() {
    if (this.mediaRecoveryRunning || !this.mediaGap) return;
    if (!this.serverBackfillAuthorized) {
      this.armBackfillAuthorizationDeadline();
      return;
    }
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this.mediaRecoveryRunning = true;
    const gap = this.mediaGap;
    try {
      await this.encryptQueue;
      const lastSequence = this.lastSentSequence;
      if (lastSequence < gap.firstSequence) {
        this.mediaGap = null;
        this.serverBackfillAuthorized = false;
        window.clearTimeout(this.backfillAuthorizationTimer);
        this.backfillAuthorizationTimer = 0;
        await this.media?.setRecoveryMute?.(false);
        this.mediaRecoveryRunning = false;
        if (this.pendingSpeechStop) {
          this.pendingSpeechStop = false;
          this.onLocalSpeechStopped();
        }
        return;
      }
      const frames = await this.ring.replayRange(gap.firstSequence, lastSequence);
      if (
        !frames.length
        || frames[0].sequence !== gap.firstSequence
        || frames.at(-1).sequence !== lastSequence
      ) {
        throw new Error("30 秒加密音频恢复缓冲不完整，面试已暂停并等待人工处理");
      }
      const totalBytes = frames.reduce((sum, frame) => sum + frame.byteLength, 0);
      const capturedFromMs = Number(frames[0].capturedAt || gap.startedAt);
      const capturedToMs = Number(frames.at(-1).capturedAt || this.clock());
      if (capturedToMs - capturedFromMs > 30_000 || totalBytes > 2 * 1024 * 1024) {
        throw new Error("媒体断流超过 30 秒恢复上限，面试已暂停并等待人工处理");
      }
      const scope = {
        source_connection_id: gap.sourceConnectionId,
        audio_epoch: gap.audioEpoch,
      };
      const ready = await this.sendAndWaitBackfill(
        "evidence.recovery.begin",
        {
          ...scope,
          first_sequence: gap.firstSequence,
          last_sequence: lastSequence,
          total_bytes: totalBytes,
          captured_from_ms: capturedFromMs,
          captured_to_ms: capturedToMs,
        },
        "ready",
      );
      const batchId = ready.batch_id;
      for (const frame of frames) {
        const acknowledged = await this.sendAndWaitBackfill(
          "evidence.recovery.chunk",
          {
            ...scope,
            batch_id: batchId,
            client_sequence: frame.sequence,
            audio_base64: arrayBufferToBase64(frame.audio),
          },
          "acknowledged",
        );
        this.ring.acknowledgeThrough(Number(acknowledged.ack_through || 0));
        this.patch({ recovery: this.ring.snapshot() });
      }
      await this.sendAndWaitBackfill(
        "evidence.recovery.complete",
        { ...scope, batch_id: batchId },
        "complete",
      );
      this.mediaGap = null;
      this.serverBackfillAuthorized = false;
      window.clearTimeout(this.backfillAuthorizationTimer);
      this.backfillAuthorizationTimer = 0;
      await this.media?.setRecoveryMute?.(false);
      this.mediaRecoveryRunning = false;
      if (
        this.state.problem?.recoverable
        && this.state.problem?.action === "reconnect_media"
        && String(this.state.problem?.code || "").startsWith("LIVEKIT_EVIDENCE_")
      ) this.patch({ problem: null });
      if (this.pendingSpeechStop) {
        this.pendingSpeechStop = false;
        this.onLocalSpeechStopped();
      }
    } finally {
      this.mediaRecoveryRunning = false;
    }
  }

  sendAndWaitBackfill(type, payload, expectedStatus) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("浏览器音频恢复需要已连接的控制通道"));
    }
    const causationId = uniqueId("browser-backfill");
    const idempotencyKey = uniqueId(type);
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        this.backfillWaiters.delete(causationId);
        reject(new Error(`浏览器音频恢复等待 ${expectedStatus} 超时`));
      }, 15_000);
      this.backfillWaiters.set(causationId, {
        expectedStatus,
        resolve: (value) => {
          window.clearTimeout(timeout);
          resolve(value);
        },
        reject: (error) => {
          window.clearTimeout(timeout);
          reject(error);
        },
      });
      this.socket.send(JSON.stringify({
        type,
        idempotency_key: idempotencyKey,
        turn_id: this.evidenceTurnId,
        causation_id: causationId,
        payload,
      }));
    });
  }

  connectControl(ticketResponse) {
    return new Promise((resolve, reject) => {
      const socket = this.socketFactory(agentWebSocketUrl(ticketResponse.agent_ws_url, ticketResponse.agent_ticket));
      this.socket = socket;
      socket.binaryType = "arraybuffer";
      let opened = false;
      const timeout = window.setTimeout(() => {
        if (!opened) reject(new Error("实时面试控制通道连接超时"));
      }, 10_000);
      socket.addEventListener("open", () => {
        opened = true;
        window.clearTimeout(timeout);
        this.reconnectAttempt = 0;
        this.patch({ connection: { ...this.state.connection, control: "connected" } });
        socket.send(JSON.stringify({
          type: "session.open",
          payload: {
            recovery_cursor: this.sequence,
            capabilities: capabilityProjection(this.capabilityReport),
          },
        }));
        this.sendSignal("client.ready", { source: "candidate_experience_v1" });
        if (this.mediaGap && this.mediaConnectionState === "connected") {
          if (this.serverBackfillAuthorized) {
            this.recoverMediaGap().catch((error) => this.failClosed(error));
          } else {
            this.armBackfillAuthorizationDeadline();
          }
        }
        resolve();
      }, { once: true });
      socket.addEventListener("message", ({ data }) => this.onAgentEvent(data));
      socket.addEventListener("error", () => {
        if (!opened) {
          window.clearTimeout(timeout);
          reject(new Error("实时面试控制通道连接失败"));
        }
      });
      socket.addEventListener("close", () => {
        window.clearTimeout(timeout);
        if (this.socket === socket) {
          if (this.performance?.delivery === "streaming_tts") this.stopPerformance("control_reconnecting", false);
          this.liveSpeech.reset();
          this.metricReporter.clear();
          this.socket = null;
          // A reset command written to a WebSocket is not durable until the
          // server reflects it in a snapshot/ack. On disconnect, drop the
          // local intent so a durable retry-required snapshot restores the
          // explicit retry button instead of leaving the UI in limbo.
          this.warmupRetryRequested = false;
          this.warmupRetryCausationId = null;
          this.captureRetryCausationId = null;
          if (this.state.captureRecovery) this.patch({ captureRecovery: { ...this.state.captureRecovery, retryPending: false } });
          if (
            !this.intentionalClose
            && !this.closed
            && this.evidenceOpen
            && !this.evidenceReady
          ) {
            this.evidenceOpenCausationId = null;
            this.evidenceReassertPending = true;
          }
        }
        if (!this.intentionalClose && !this.closed && !this.state.completion) this.scheduleReconnect();
      });
    });
  }

  async onAgentEvent(raw) {
    let event;
    try { event = typeof raw === "string" ? JSON.parse(raw) : JSON.parse(await raw.text()); }
    catch {
      this.beginEventResync({
        reason: "invalid_json",
        problem: { code: "AGENT_EVENT_ENVELOPE_INVALID", message: "实时事件不是合法 JSON" },
      });
      return;
    }
    const ordered = this.eventStream.accept(event);
    this.sequence = this.eventStream.cursor;
    if (ordered.action === "resync") {
      this.beginEventResync(ordered);
      return;
    }
    if (ordered.action !== "apply") return;
    event = ordered.event;
    const payload = event.payload || {};
    if (event.type === "session.snapshot") {
      this.finishEventResync(ordered.recovered);
      this.applySnapshot(payload);
      return;
    }
    if (this.isSafetyStopped() && ["floor.changed", "speech.started", "speech.stopped", "transcript.partial", "transcript.final"].includes(event.type)) return;
    if (event.type === "floor.changed") {
      if (["answer_recovering", "answer_retry_required"].includes(payload.reason)) {
        if (this.matchesCapture(event)) this.applyCaptureRecovery({
          status: payload.reason === "answer_recovering" ? "recovering" : "retry_required",
          turnId: event.turn_id, captureId: payload.capture_id,
        });
        return;
      }
      const recovery = this.state.captureRecovery;
      if (recovery?.status === "retry_required") {
        const ready = payload.reason === "evidence_stream_open" && payload.owner === "candidate"
          && this.captureRetryCausationId && event.causation_id === this.captureRetryCausationId
          && event.turn_id === recovery.turnId && event.turn_id === this.state.currentQuestion?.turn_id
          && payload.capture_id && payload.capture_id !== recovery.captureId;
        if (!ready) return;
        this.evidenceCaptureId = payload.capture_id;
        this.evidenceTurnId = event.turn_id;
        this.evidenceOpen = true;
        this.evidenceReady = true;
        this.evidenceReassertPending = false;
        this.evidenceOpenCausationId = null;
        this.answerSubmissionPending = false;
        this.answerFinishRequested = false;
        this.speechStartSignaled = false;
        this.pendingSpeechStop = false;
        this.ring?.clear();
        this.clearCaptureRecovery();
        this.patch({ floor: "candidate", phase: "listening", evidence: { requested: true, ready: true },
          serverAudio: { received: false, receivedAt: null }, captions: { forming: false, recent: [], full: [] } });
        this.flushPendingSpeechState();
        return;
      }
      if (payload.reason === "answer_processing") {
        if (recovery) return;
        if (payload.capture_id && (
          payload.capture_id !== this.evidenceCaptureId || event.turn_id !== this.evidenceTurnId
        )) return;
        this.clearEndpointWarning(event, ANSWER_PREPARATION_WARNING_CODES);
        this.answerPreparation = null;
        this.answerFinishRequested = false;
        this.answerSubmissionPending = true;
        this.evidenceOpen = false;
        this.evidenceReady = false;
        this.evidenceOpenCausationId = null;
        this.speechStartSignaled = false;
        this.pendingSpeechStop = false;
        this.patch({ floor: "none", phase: "understanding", evidence: { requested: false, ready: false }, endpoint: { active: false, deadlineAt: null } });
        return;
      }
      if (["answer_preparing", "answer_listening", "answer_detector_ready", "supplement_awaiting_reply"].includes(payload.reason)) {
        // Preparation never acknowledges a new capture. A late result from
        // another turn/capture must not reopen or relabel the active stream.
        if (
          payload.owner !== "candidate" || !this.evidenceReady || this.answerSubmissionPending
          || this.state.calibration.status !== "completed"
          || !this.evidenceCaptureId || payload.capture_id !== this.evidenceCaptureId
          || event.turn_id !== this.evidenceTurnId
          || ["paused", "completed"].includes(this.state.phase)
        ) return;
        if (payload.reason === "answer_detector_ready") {
          // A detector recovery is not an acknowledgement of a new capture
          // and must not alter an in-flight preparation or the media gate.
          this.clearEndpointWarning(event, DETECTOR_WARNING_CODES);
          return;
        }
        this.answerPreparation = payload.reason === "answer_preparing"
          ? { status: "preparing", turn_id: event.turn_id, capture_id: payload.capture_id } : null;
        if (payload.reason === "answer_listening") {
          this.answerFinishRequested = false;
          this.clearCaptureRecovery();
        } else if (recovery) return;
        if (["answer_preparing", "supplement_awaiting_reply"].includes(payload.reason)) {
          this.clearEndpointWarning(event, ANSWER_PREPARATION_WARNING_CODES);
        }
        this.patch({
          floor: "candidate",
          phase: payload.reason === "answer_preparing" ? "answer_preparing"
            : payload.reason === "supplement_awaiting_reply" ? "awaiting_supplement" : "listening",
          endpoint: { active: false, deadlineAt: null },
          evidence: { requested: this.evidenceOpen, ready: this.evidenceReady },
        });
        return;
      }
      if (payload.owner === "agent" || payload.owner === "candidate") this.answerSubmissionPending = false;
      if (payload.owner !== "candidate" || payload.reason === "barge_in") this.answerPreparation = null;
      if (payload.owner === "agent") this.answerFinishRequested = false;
      const acknowledgesEvidenceReady = payload.owner === "candidate"
        && ["warmup_stream_open", "evidence_stream_open"].includes(payload.reason);
      const invalidatesEvidenceReady = payload.reason === "warmup_retry";
      const acknowledgesCurrentOpen = acknowledgesEvidenceReady
        && Boolean(this.evidenceOpenCausationId)
        && event.causation_id === this.evidenceOpenCausationId;
      const acknowledgesCurrentRetry = invalidatesEvidenceReady
        && this.warmupRetryRequested
        && Boolean(this.warmupRetryCausationId)
        && event.causation_id === this.warmupRetryCausationId;
      const repeatsAcknowledgedRetry = invalidatesEvidenceReady
        && Boolean(event.causation_id)
        && this.acknowledgedWarmupRetryCausationIds.has(event.causation_id);
      if (repeatsAcknowledgedRetry) return;
      const calibrationStatus = acknowledgesEvidenceReady && !acknowledgesCurrentOpen
        ? this.state.calibration.status
        : calibrationForFloorReason(payload.reason, this.state.calibration.status);
      if (invalidatesEvidenceReady) {
        if (event.causation_id) {
          this.acknowledgedWarmupRetryCausationIds.add(event.causation_id);
          if (this.acknowledgedWarmupRetryCausationIds.size > 8) {
            const oldest = this.acknowledgedWarmupRetryCausationIds.values().next().value;
            this.acknowledgedWarmupRetryCausationIds.delete(oldest);
          }
        }
        window.clearTimeout(this.endpointTimer);
        this.endpointTimer = 0;
        this.ring?.clear();
        this.evidenceOpen = false;
        this.evidenceReady = false;
        this.evidenceOpenCausationId = null;
        this.speechStartSignaled = false;
        this.pendingSpeechStop = false;
        this.warmupRetryRequested = false;
        this.warmupRetryCausationId = null;
        this.evidenceReassertPending = false;
        this.evidenceTurnId = null;
      } else if (acknowledgesCurrentOpen) {
        if (this.evidenceCaptureId !== payload.capture_id) {
          this.answerFinishRequested = false;
          this.speechStartSignaled = false;
          this.pendingSpeechStop = false;
        }
        this.evidenceCaptureId = payload.capture_id;
        this.evidenceReady = true;
        this.evidenceReassertPending = false;
      }
      this.patch({
        floor: payload.owner,
        phase: recovery?.status === "recovering" ? "answer_recovering" : payload.owner === "candidate"
          ? this.isPreparingAnswer() ? "answer_preparing" : this.evidenceReady ? "listening" : "preparing"
          : phaseForFloor(payload.owner, this.state.phase),
        calibration: {
          ...this.state.calibration,
          status: calibrationStatus,
          retryRequired: invalidatesEvidenceReady
            ? false
            : acknowledgesCurrentOpen
              ? false
              : this.state.calibration.retryRequired,
        },
        evidence: {
          requested: this.evidenceOpen,
          ready: this.evidenceReady,
        },
        ...(acknowledgesCurrentOpen && this.state.problem?.code === "STT_TRANSCRIPT_UNAVAILABLE"
          ? { problem: null } : {}),
        ...(invalidatesEvidenceReady ? {
          microphone: { ...this.state.microphone, localDetected: false },
          endpoint: { active: false, deadlineAt: null },
          recovery: this.ring?.snapshot?.() || this.state.recovery,
        } : {}),
      });
      if (
        payload.owner === "candidate"
        && !["awaiting_confirmation", "confirming"].includes(calibrationStatus)
      ) {
        if (acknowledgesEvidenceReady) {
          if (acknowledgesCurrentOpen) this.flushPendingSpeechState();
        } else if (invalidatesEvidenceReady) {
          await this.ensureEvidenceOpen();
        } else {
          const reassertEvidenceOpen = this.evidenceReassertPending
            && this.evidenceOpen
            && !this.evidenceReady;
          this.evidenceReassertPending = false;
          await this.ensureEvidenceOpen({ force: reassertEvidenceOpen });
        }
      }
      return;
    }
    if (event.type === "speech.started") {
      if (payload.browser_backfill) {
        const recovery = payload.browser_backfill;
        const waiter = this.backfillWaiters.get(event.causation_id);
        if (waiter && waiter.expectedStatus === recovery.status) {
          this.backfillWaiters.delete(event.causation_id);
          waiter.resolve(recovery);
        }
        if (Number(recovery.ack_through || 0) > 0) {
          this.ring?.acknowledgeThrough?.(Number(recovery.ack_through));
          this.patch({ recovery: this.ring?.snapshot?.() || this.state.recovery });
        }
      }
      if (this.state.captureRecovery?.status === "retry_required") return;
      if (payload.speaker === "candidate" && payload.server_audio_received) {
        if (this.localSpeechStartedAt != null) {
          this.observeMetric("server_audio_confirmation_ms", this.clock() - this.localSpeechStartedAt);
          this.localSpeechStartedAt = null;
        }
        this.patch({ serverAudio: { received: true, receivedAt: payload.server_received_at || new Date().toISOString() } });
      }
      return;
    }
    if (event.type === "speech.stopped" && payload.speaker === "candidate") {
      if (this.answerSubmissionPending || this.state.captureRecovery || ["paused", "completed"].includes(this.state.phase)) return;
      const countdownMs = Number(payload.endpoint_countdown_ms ?? 2500);
      this.patch({
        endpoint: { active: true, deadlineAt: countdownMs > 0 ? this.clock() + countdownMs : null },
      });
      return;
    }
    if (event.type === "transcript.partial") {
      if (!this.matchesTranscriptTurn(event)) return;
      if (this.state.captureRecovery?.status === "retry_required" || (this.state.captureRecovery && event.turn_id !== this.state.captureRecovery.turnId)) return;
      if (!this.partialObserved && this.evidenceOpenedAt != null) {
        this.partialObserved = true;
        this.observeMetric("partial_first_token_ms", this.clock() - this.evidenceOpenedAt);
      }
      this.appendCaption(payload.text, false);
      return;
    }
    if (event.type === "transcript.final") {
      if (!this.matchesTranscriptTurn(event)) return;
      // During repair this may be a late provider prefix, not an answer commit.
      // Recovery can only be cleared by its scoped ready/listening or snapshot.
      if (this.state.captureRecovery) return;
      this.answerPreparation = null;
      if (!payload.calibration) this.clearEndpointWarning(event, ANSWER_PREPARATION_WARNING_CODES);
      this.answerFinishRequested = false;
      this.answerSubmissionPending = false;
      if (this.localSpeechStoppedAt != null) {
        this.observeMetric("stop_to_final_ms", this.clock() - this.localSpeechStoppedAt);
        this.localSpeechStoppedAt = null;
      }
      this.finalReceivedAt = this.clock();
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.acknowledgedWarmupRetryCausationIds.clear();
      this.speechStartSignaled = false;
      this.pendingSpeechStop = false;
      this.evidenceReassertPending = false;
      this.evidenceTurnId = null;
      this.appendCaption(payload.text, true);
      if (payload.calibration) {
        this.ring?.clear();
        this.patch({
          calibration: {
            status: "awaiting_confirmation",
            transcript: String(payload.text || ""),
            confidence: Number(payload.confidence || 0),
            retryRequired: false,
          },
          recovery: this.ring?.snapshot?.() || this.state.recovery,
          evidence: { requested: false, ready: false },
          phase: "understanding",
          endpoint: { active: false, deadlineAt: null },
        });
      } else {
        this.ring?.clear();
        this.patch({
          recovery: this.ring?.snapshot?.() || this.state.recovery,
          evidence: { requested: false, ready: false },
          phase: "understanding",
          endpoint: { active: false, deadlineAt: null },
        });
      }
      return;
    }
    if (event.type === "conversation.act.selected") {
      this.currentAct = { ...payload, turnId: event.turn_id };
      if (event.turn_id && ["question", "repeat", "followup"].includes(payload.act_type)) {
        const turnChanged = event.turn_id !== this.currentTranscriptTurnId();
        if (turnChanged) {
          window.clearTimeout(this.captionFreshnessTimer);
          this.answerPreparation = null;
        }
        this.patch({
          ...(turnChanged ? { captions: { forming: false, recent: [], full: [] } } : {}),
          ...(turnChanged && this.state.phase === "answer_preparing" ? {
            phase: this.state.floor === "candidate" ? "preparing" : phaseForFloor(this.state.floor, this.state.phase),
          } : {}),
          currentQuestion: {
            ...(this.state.currentQuestion || {}),
            turn_id: event.turn_id,
            question_text: payload.text,
            is_followup: payload.act_type === "followup",
          },
        });
      }
      return;
    }
    if (event.type === "avatar.performance.started") {
      if (payload.delivery === "streaming_tts" && event.replayability !== "transient") {
        this.beginEventResync({ reason: "invalid", problem: { code: "AGENT_EVENT_PAYLOAD_INVALID", message: "流式语音不能从旧事件重放" } });
        return;
      }
      await this.playPerformance(payload, event.turn_id);
      return;
    }
    if (event.type === "avatar.performance.producer_finished") {
      this.liveSpeech.producerFinished(payload);
      return;
    }
    if (event.type === "avatar.performance.interrupted") {
      this.stopPerformance(
        payload.reason || "interrupted",
        false,
        payload.performance_id || null,
      );
      return;
    }
    if (event.type === "takeover.changed") {
      this.clearCaptureRecovery();
      this.stopPerformance("human_takeover");
      this.patch({ phase: "paused", floor: payload.status === "active" ? "human" : "none" });
      return;
    }
    if (event.type === "problem") {
      // A queued VAD error must never replace the actionable safety pause.
      if (this.state.problem?.recoverable === false && payload.recoverable) return;
      if (CAPTURE_RECOVERY_CODES.has(payload.code)) {
        if (!this.isSafetyStopped() && this.matchesCapture(event)) {
          // An unchanged snapshot is not an ACK. Only the scoped failure for
          // this retry unlocks it; a previous attempt cannot unlock a new one.
          if (payload.code === "CAPTURE_RETRY_REQUIRED" && payload.action === "retry_answer"
            && this.captureRetryCausationId && event.causation_id === this.captureRetryCausationId) {
            this.captureRetryCausationId = null;
          }
          this.applyCaptureRecovery({
            status: payload.code === "CAPTURE_RECOVERING" ? "recovering" : "retry_required",
            turnId: event.turn_id, captureId: payload.capture_id,
          });
        }
        return;
      }
      // Retaining a capture warning must not swallow transport acknowledgements:
      // media backfill has its own authorization and failure lifecycle.
      const waiter = this.backfillWaiters.get(event.causation_id);
      if (waiter) {
        this.backfillWaiters.delete(event.causation_id);
        waiter.reject(new Error(payload.message || payload.code || "浏览器音频恢复失败"));
      }
      if (
        this.mediaGap
        && payload.recoverable
        && payload.action === "reconnect_media"
        && String(payload.code || "").startsWith("LIVEKIT_EVIDENCE_")
      ) {
        this.serverBackfillAuthorized = true;
        window.clearTimeout(this.backfillAuthorizationTimer);
        this.backfillAuthorizationTimer = 0;
        if (this.mediaConnectionState === "connected") {
          this.recoverMediaGap().catch((error) => this.failClosed(error));
        }
      }
      if (this.state.captureRecovery && payload.recoverable) {
        if (event.causation_id === this.captureRetryCausationId) {
          this.captureRetryCausationId = null;
          this.patch({ captureRecovery: { ...this.state.captureRecovery, retryPending: false } });
        }
        return;
      }
      this.endpointProblemScope = payload.recoverable === true
        && payload.action === "continue_listening"
        && (ANSWER_PREPARATION_WARNING_CODES.has(payload.code) || DETECTOR_WARNING_CODES.has(payload.code))
        ? { turnId: event.turn_id, captureId: payload.capture_id || this.evidenceCaptureId }
        : null;
      this.answerFinishRequested = false;
      this.answerSubmissionPending = false;
      const retryWarmup = payload.recoverable && payload.action === "retry_warmup";
      const retryTranscript = payload.recoverable && payload.code === "STT_TRANSCRIPT_UNAVAILABLE";
      const rejectsCurrentEvidenceOpen = Boolean(this.evidenceOpenCausationId)
        && event.causation_id === this.evidenceOpenCausationId;
      if (!payload.recoverable || retryWarmup || retryTranscript || rejectsCurrentEvidenceOpen) {
        if (!payload.recoverable) this.captureRetryCausationId = null;
        window.clearTimeout(this.endpointTimer);
        this.endpointTimer = 0;
        this.evidenceOpen = false;
        this.evidenceReady = false;
        this.evidenceOpenCausationId = null;
        if (retryWarmup || !payload.recoverable) this.acknowledgedWarmupRetryCausationIds.clear();
        this.speechStartSignaled = false;
        this.pendingSpeechStop = false;
        this.warmupRetryRequested = false;
        this.warmupRetryCausationId = null;
        this.evidenceReassertPending = false;
        this.evidenceTurnId = null;
        if (retryWarmup) this.ring?.clear();
      }
      if (this.endpointProblemScope || !payload.recoverable) this.answerPreparation = null;
      this.patch({
        problem: payload,
        phase: retryWarmup || retryTranscript ? "preparing" : this.endpointProblemScope
          ? "listening" : payload.recoverable ? this.state.phase : "paused",
        ...(retryTranscript ? {
          evidence: { requested: false, ready: false },
          endpoint: { active: false, deadlineAt: null },
          captions: { ...this.state.captions, forming: false },
        } : {}),
        ...(retryWarmup ? {
          calibration: { ...this.state.calibration, status: "retrying", retryRequired: true },
          evidence: { requested: false, ready: false },
          endpoint: { active: false, deadlineAt: null },
          microphone: { ...this.state.microphone, localDetected: false },
          recovery: this.ring?.snapshot?.() || this.state.recovery,
        } : {}),
        ...(rejectsCurrentEvidenceOpen && payload.recoverable && !retryWarmup ? {
          evidence: { requested: false, ready: false },
        } : {}),
        ...(!payload.recoverable ? {
          captureRecovery: null,
          floor: "none",
          endpoint: { active: false, deadlineAt: null },
          microphone: { ...this.state.microphone, localDetected: false },
          evidence: { requested: false, ready: false },
        } : {}),
      });
      if (!payload.recoverable) this.stopPerformance("fatal_problem");
      return;
    }
    if (event.type === "completed") {
      this.captureRetryCausationId = null;
      this.patch({ completion: payload, phase: "completed", floor: "none", captureRecovery: null });
      await this.shutdownMedia();
    }
  }

  isSafetyStopped() {
    return this.closed || Boolean(this.state.completion) || this.state.problem?.recoverable === false
      || ["paused", "completed"].includes(this.state.phase)
      || ["paused", "completed", "cancelled", "expired", "report_ready"].includes(this.state.session?.status);
  }

  matchesCapture(event) {
    return this.state.calibration.status === "completed" && event.turn_id === this.state.currentQuestion?.turn_id
      && event.turn_id === this.evidenceTurnId && Boolean(this.evidenceCaptureId)
      && event.payload.capture_id === this.evidenceCaptureId;
  }

  currentTranscriptTurnId() {
    return this.state.currentQuestion?.turn_id || this.state.session?.current_turn_id || null;
  }

  isPreparingAnswer(turnId = this.currentTranscriptTurnId()) {
    const preparation = this.answerPreparation;
    return preparation?.status === "preparing" && preparation.turn_id === turnId
      && (!this.evidenceCaptureId || preparation.capture_id === this.evidenceCaptureId)
      && (!this.evidenceReady || this.evidenceTurnId === turnId);
  }

  matchesTranscriptTurn(event) {
    if (event.payload?.calibration) {
      return event.turn_id == null && this.state.calibration.status !== "completed";
    }
    // A delayed final must not close the next question's microphone gate.
    // Before a question is known, only explicitly marked warm-up text belongs
    // to this view; an unscoped formal transcript cannot establish identity.
    const currentTurnId = this.currentTranscriptTurnId();
    return Boolean(currentTurnId && event.turn_id === currentTurnId);
  }

  applyCaptureRecovery(value) {
    const previous = this.state.captureRecovery;
    if (previous?.status === "retry_required" && value.status === "recovering" && previous.captureId === value.captureId) return;
    const recovery = { ...(previous?.captureId === value.captureId ? previous : {}), ...value,
      retryPending: Boolean(this.captureRetryCausationId) };
    const retry = value.status === "retry_required";
    this.answerPreparation = null;
    this.answerFinishRequested = false;
    this.answerSubmissionPending = false;
    this.endpointProblemScope = null;
    window.clearTimeout(this.endpointTimer);
    this.endpointTimer = 0;
    if (retry) {
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.evidenceReassertPending = false;
      this.speechStartSignaled = false;
      this.pendingSpeechStop = false;
    }
    this.patch({ captureRecovery: recovery, phase: retry ? "answer_retry_required" : "answer_recovering",
      floor: retry ? "none" : "candidate", endpoint: { active: false, deadlineAt: null },
      evidence: { requested: this.evidenceOpen, ready: this.evidenceReady },
      problem: { code: retry ? "CAPTURE_RETRY_REQUIRED" : "CAPTURE_RECOVERING", recoverable: true,
        action: retry ? "retry_answer" : "continue_listening", message: CAPTURE_RECOVERY_MESSAGES[value.status] },
      captions: { ...this.state.captions, forming: false },
      ...(retry ? { serverAudio: { received: false, receivedAt: null }, microphone: { ...this.state.microphone, localDetected: false } } : {}),
    });
  }

  clearCaptureRecovery() {
    this.captureRetryCausationId = null;
    this.patch({ captureRecovery: null, ...(CAPTURE_RECOVERY_CODES.has(this.state.problem?.code) ? { problem: null } : {}) });
  }

  clearEndpointWarning(event, allowedCodes) {
    const problem = this.state.problem;
    const scope = this.endpointProblemScope;
    if (
      problem?.recoverable !== true || problem.action !== "continue_listening"
      || !allowedCodes.has(problem.code) || !scope?.turnId || !scope.captureId
      || scope.turnId !== this.evidenceTurnId || event.turn_id !== scope.turnId
      || scope.captureId !== this.evidenceCaptureId
      || (event.payload?.capture_id && event.payload.capture_id !== scope.captureId)
    ) return;
    this.endpointProblemScope = null;
    this.patch({ problem: null });
  }

  beginEventResync(result) {
    this.eventStream.awaitingSnapshot = true;
    this.stopPerformance("event_stream_resync", false);
    this.liveSpeech.reset();
    const validation = result.problem || {};
    const isGap = result.reason === "sequence_gap" || result.reason === "duplicate_event_id";
    this.patch({
      connection: { ...this.state.connection, control: "resyncing" },
      problem: this.state.problem?.recoverable === false ? this.state.problem : {
        code: validation.code || (isGap ? "AGENT_EVENT_SEQUENCE_GAP" : "AGENT_EVENT_INVALID"),
        message: validation.message || "实时事件顺序不连续，正在从服务端权威快照恢复。",
        recoverable: true,
        action: "resync_from_snapshot",
      },
    });
    if (this.eventResyncTimer) return;
    this.eventResyncTimer = window.setTimeout(() => {
      this.eventResyncTimer = 0;
      const socket = this.socket;
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close(4001, "agent_event_resync_required");
      } else if (!this.intentionalClose && !this.closed) {
        this.scheduleReconnect();
      }
    }, 1500);
  }

  finishEventResync(recovered) {
    window.clearTimeout(this.eventResyncTimer);
    this.eventResyncTimer = 0;
    const problem = this.state.problem;
    const localOrderingProblem = String(problem?.code || "").startsWith("AGENT_EVENT_");
    if (recovered || this.state.connection.control === "resyncing") {
      this.patch({
        connection: { ...this.state.connection, control: "connected" },
        problem: localOrderingProblem ? null : problem,
      });
    }
  }

  applySnapshot(payload) {
    const sessionStatus = payload.status;
    const nextTranscriptTurnId = payload.current_question?.turn_id || payload.current_turn_id || null;
    this.answerPreparation = sessionStatus === "in_progress" && payload.floor === "candidate"
      && !payload.capture_recovery && payload.answer_preparation?.turn_id === nextTranscriptTurnId
      ? payload.answer_preparation : null;
    const transcriptTurnChanged = nextTranscriptTurnId !== this.currentTranscriptTurnId();
    const oldRecovery = this.state.captureRecovery;
    const captureRecovery = sessionStatus === "in_progress" && this.state.problem?.recoverable !== false
      ? payload.capture_recovery : null;
    if (oldRecovery && captureRecovery && oldRecovery.captureId !== captureRecovery.capture_id) this.captureRetryCausationId = null;
    const recoveryTurnChanged = oldRecovery && oldRecovery.turnId !== payload.current_turn_id;
    if (recoveryTurnChanged) {
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceTurnId = null;
      this.evidenceCaptureId = null;
      this.evidenceOpenCausationId = null;
    }
    if (!captureRecovery && oldRecovery && (Object.hasOwn(payload, "capture_recovery") || recoveryTurnChanged || sessionStatus !== "in_progress")) {
      this.clearCaptureRecovery();
    }
    if (["paused", "completed", "cancelled", "expired"].includes(sessionStatus)
      || (Object.hasOwn(payload, "active_performance_id") && this.performance
        && payload.active_performance_id !== this.performance.performance_id)) {
      this.stopPerformance("authoritative_snapshot", false);
      this.liveSpeech.reset();
    }
    const calibrationRetryRequired = Boolean(payload.calibration_retry_required);
    const calibrationStatus = payload.calibration_status || this.state.calibration.status;
    const warmupJustCompleted = calibrationStatus === "completed"
      && this.state.calibration.status !== "completed";
    if (transcriptTurnChanged || warmupJustCompleted) window.clearTimeout(this.captionFreshnessTimer);
    const retryAckPending = this.warmupRetryRequested && Boolean(this.warmupRetryCausationId);
    if (calibrationStatus === "retrying" && !calibrationRetryRequired && retryAckPending) {
      this.acknowledgedWarmupRetryCausationIds.add(this.warmupRetryCausationId);
      this.warmupRetryRequested = false;
      this.warmupRetryCausationId = null;
    }
    if (calibrationRetryRequired) {
      window.clearTimeout(this.endpointTimer);
      this.endpointTimer = 0;
      this.ring?.clear();
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.acknowledgedWarmupRetryCausationIds.clear();
      this.speechStartSignaled = false;
      this.pendingSpeechStop = false;
      this.evidenceReassertPending = false;
      this.evidenceTurnId = null;
      if (!retryAckPending) {
        this.warmupRetryRequested = false;
        this.warmupRetryCausationId = null;
      }
    }
    if (["paused", "completed", "cancelled", "expired", "report_ready"].includes(sessionStatus)) {
      this.captureRetryCausationId = null;
      this.answerFinishRequested = false;
      window.clearTimeout(this.endpointTimer);
      this.endpointTimer = 0;
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.acknowledgedWarmupRetryCausationIds.clear();
      this.speechStartSignaled = false;
      this.pendingSpeechStop = false;
      this.warmupRetryRequested = false;
      this.warmupRetryCausationId = null;
      this.evidenceReassertPending = false;
    }
    this.patch({
      session: payload,
      floor: payload.floor || this.state.floor,
      currentQuestion: payload.current_question || null,
      mediaPolicy: { ...(this.state.mediaPolicy || {}), recording: payload.recording },
      calibration: {
        ...this.state.calibration,
        status: calibrationStatus,
        retryRequired: calibrationRetryRequired && !retryAckPending,
      },
      ...(transcriptTurnChanged || warmupJustCompleted ? {
        captions: { forming: false, recent: [], full: [] },
      } : {}),
      ...(calibrationRetryRequired ? {
        endpoint: { active: false, deadlineAt: null },
        microphone: { ...this.state.microphone, localDetected: false },
        evidence: { requested: false, ready: false },
        recovery: this.ring?.snapshot?.() || this.state.recovery,
      } : {}),
      ...(["paused", "completed", "cancelled", "expired", "report_ready"].includes(sessionStatus) ? {
        captureRecovery: null,
        endpoint: { active: false, deadlineAt: null },
        microphone: { ...this.state.microphone, localDetected: false },
        evidence: { requested: false, ready: false },
      } : {}),
      phase: sessionStatus === "paused"
        ? "paused"
        : sessionStatus === "completed"
          ? "completed"
          : payload.floor === "candidate" && this.isPreparingAnswer(nextTranscriptTurnId)
            ? "answer_preparing"
          : this.answerSubmissionPending
            ? "understanding"
          : payload.floor === "candidate" && !this.evidenceReady
            ? "preparing"
          : payload.floor === "candidate" && payload.supplement_confirmation?.status === "awaiting_reply"
            && payload.supplement_confirmation?.turn_id === payload.current_turn_id
            && payload.supplement_confirmation?.capture_id === this.evidenceCaptureId
            ? "awaiting_supplement"
            : phaseForFloor(payload.floor, this.state.phase),
    });
    if (captureRecovery && calibrationStatus === "completed") {
      this.evidenceTurnId = captureRecovery.turn_id;
      this.evidenceCaptureId = captureRecovery.capture_id;
      if (captureRecovery.status === "recovering") {
        this.evidenceOpen = true;
        this.evidenceReady = true;
        this.evidenceReassertPending = false;
      }
      this.applyCaptureRecovery({ status: captureRecovery.status, turnId: captureRecovery.turn_id,
        captureId: captureRecovery.capture_id, attempt: captureRecovery.attempt, maxAttempts: captureRecovery.max_attempts });
      return;
    }
    const reassertEvidenceOpen = this.evidenceReassertPending
      && this.evidenceOpen
      && !this.evidenceReady;
    if (
      sessionStatus === "in_progress"
      && this.state.captureRecovery?.status !== "retry_required"
      && !this.answerSubmissionPending
      && payload.floor === "candidate"
      && (!this.evidenceOpen || reassertEvidenceOpen)
      && !calibrationRetryRequired
      && !["awaiting_confirmation", "confirming"].includes(
        calibrationStatus,
      )
    ) {
      this.evidenceReassertPending = false;
      this.ensureEvidenceOpen({ force: reassertEvidenceOpen }).catch((error) => this.failClosed(error));
    }
    if (sessionStatus === "completed") this.shutdownMedia();
  }

  async ensureEvidenceOpen({ force = false } = {}) {
    const turnId = force && this.evidenceTurnId && this.evidenceTurnId !== "__warmup__"
      ? this.evidenceTurnId
      : this.state.currentQuestion?.turn_id;
    const calibrationStatus = this.state.calibration.status;
    const warmup = calibrationStatus !== "completed";
    const reasserting = force && this.evidenceOpen && !this.evidenceReady;
    if (
      (this.evidenceOpen && !reasserting)
      || this.state.captureRecovery?.status === "retry_required"
      || this.state.phase === "paused"
      || (!warmup && !turnId)
      || (calibrationStatus === "retrying"
        && (this.state.calibration.retryRequired || this.warmupRetryRequested)
        && !reasserting)
      || (warmup && !["opening", "listening", "retrying"].includes(calibrationStatus))
    ) return;
    this.evidenceOpen = true;
    this.evidenceReady = false;
    this.evidenceTurnId = warmup ? "__warmup__" : turnId;
    this.evidenceOpenedAt = this.clock();
    this.partialObserved = false;
    this.patch({
      serverAudio: { received: false, receivedAt: null },
      evidence: { requested: true, ready: false },
      phase: this.isPreparingAnswer() ? "answer_preparing" : "preparing",
    });
    try {
      const causationId = uniqueId("candidate");
      this.evidenceOpenCausationId = causationId;
      this.sendSignal("evidence.stream.open", {
        content_type: "audio/pcm",
        sample_rate_hz: 16_000,
        channels: 1,
        language: "zh-CN",
        enable_partial: true,
      }, { turnId: warmup ? null : turnId, causationId });
    } catch (error) {
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.evidenceReassertPending = false;
      this.patch({
        evidence: { requested: false, ready: false },
        ...(calibrationStatus === "retrying" ? {
          calibration: { ...this.state.calibration, retryRequired: true },
        } : {}),
      });
      throw error;
    }
  }

  onPcm(frame) {
    if (!this.evidenceOpen) {
      // A confirmed barge-in can be signalled before the server has switched
      // the floor and acknowledged Evidence open. Preserve its matching stop
      // until that acknowledgement arrives instead of leaving speech open.
      this.pendingSpeechStop = this.speechStartSignaled;
      return;
    }
    const sequence = ++this.lastSentSequence;
    const transferable = frame.slice(0);
    this.encryptQueue = this.encryptQueue.then(async () => {
      await this.ring.append(sequence, transferable);
      this.patch({ recovery: this.ring.snapshot() });
    }).catch((error) => this.patch({ problem: { code: "AUDIO_RECOVERY_ENCRYPTION_FAILED", message: error.message, recoverable: false }, phase: "paused" }));
  }

  onLevel(rms) {
    this.patch({ microphone: { ...this.state.microphone, level: Math.min(1, rms / 0.12) } }, false);
  }

  async onLocalSpeechStarted(details = {}) {
    if (this.state.captureRecovery?.status === "retry_required") return;
    if (this.answerSubmissionPending || (this.state.phase === "understanding" && this.state.calibration.status === "completed")) return;
    if (this.closed || ["paused", "completed"].includes(this.state.phase) || this.state.completion) {
      return;
    }
    const cancelsPreparation = this.answerFinishRequested || this.state.phase === "answer_preparing";
    this.answerFinishRequested = false;
    // A click can happen while VAD is already active. Still send one fresh,
    // scoped start when withdrawing that hint instead of deduplicating it.
    if (cancelsPreparation) this.speechStartSignaled = false;
    const detectedAt = this.clock();
    const agentWasSpeaking = this.state.floor === "agent" || Boolean(this.activePlayback);
    this.localSpeechStartedAt = detectedAt;
    this.patch({
      microphone: { ...this.state.microphone, localDetected: true },
      phase: this.state.captureRecovery?.status === "recovering" ? "answer_recovering" : this.evidenceReady ? "listening" : "preparing",
    });
    if (Number.isFinite(Number(details.feedbackLatencyMs))) {
      this.observeMetric("local_microphone_feedback_ms", Number(details.feedbackLatencyMs));
    }
    window.clearTimeout(this.endpointTimer);
    this.endpointTimer = 0;
    this.patch({ endpoint: { active: false, deadlineAt: null } });
    if (agentWasSpeaking) {
      this.stopPerformance("barge_in_local");
      this.observeMetric("barge_in_mute_ms", this.clock() - detectedAt);
    }
    // Only a high-confidence agent-speaking barge-in bypasses the ready gate.
    // Ordinary candidate-floor VAD can request Evidence, but speech signalling
    // waits for the correlated server acknowledgement.
    const mayOpenEvidence = this.state.floor === "candidate"
      && !this.state.calibration.retryRequired
      && !["awaiting_confirmation", "confirming"].includes(this.state.calibration.status);
    if (agentWasSpeaking || this.evidenceReady) {
      this.signalSpeechStarted();
    }
    if (!agentWasSpeaking && mayOpenEvidence) {
      await this.ensureEvidenceOpen();
    }
  }

  onLocalSpeechStopped() {
    if (this.state.captureRecovery?.status === "retry_required") return;
    if (this.answerSubmissionPending) return;
    if (this.closed || ["paused", "completed"].includes(this.state.phase) || this.state.completion) {
      return;
    }
    this.localSpeechStoppedAt = this.clock();
    this.patch({ microphone: { ...this.state.microphone, localDetected: false } });
    if (!this.evidenceOpen) return;
    if (!this.evidenceReady) {
      this.pendingSpeechStop = this.speechStartSignaled;
      return;
    }
    if (!this.speechStartSignaled) return;
    if (this.mediaGap || this.mediaRecoveryRunning) {
      this.pendingSpeechStop = true;
      this.patch({
        phase: "connecting",
        endpoint: { active: false, deadlineAt: null },
      });
      if (this.mediaConnectionState === "connected") {
        if (this.serverBackfillAuthorized) {
          this.recoverMediaGap().catch((error) => this.failClosed(error));
        } else {
          this.armBackfillAuthorizationDeadline();
        }
      }
      return;
    }
    this.sendCaptureSignal("speech.stopped");
    this.speechStartSignaled = false;
    // 是否开始 2.5 秒静音收口只能由服务端权威事件决定。客户端若自行倒计时
    // 并切到“理解中”，会在服务端已暂停或没有收到音频时制造虚假进度。
  }

  signalSpeechStarted() {
    if (this.speechStartSignaled || this.socket?.readyState !== WebSocket.OPEN) return false;
    this.sendCaptureSignal("speech.started");
    this.speechStartSignaled = true;
    this.pendingSpeechStop = false;
    return true;
  }

  flushPendingSpeechState() {
    if (!this.evidenceReady) return;
    const speaking = Boolean(this.capture?.speaking ?? this.state.microphone.localDetected);
    if (speaking) {
      this.signalSpeechStarted();
      return;
    }
    if (this.speechStartSignaled && this.pendingSpeechStop) {
      this.pendingSpeechStop = false;
      this.sendCaptureSignal("speech.stopped");
      this.speechStartSignaled = false;
    }
  }

  appendCaption(text, final) {
    const normalized = String(text || "").trim();
    if (!normalized) return;
    const previous = this.state.captions.recent.at(-1);
    if (!final && previous?.text === normalized) return;
    window.clearTimeout(this.captionFreshnessTimer);
    const full = final
      ? [...this.state.captions.full, { text: normalized, final: true, at: new Date().toISOString() }]
      : this.state.captions.full;
    const recent = final
      ? full.slice(-2)
      : [...full.slice(-1), { text: normalized, final: false, at: new Date().toISOString() }].slice(-2);
    this.patch({ captions: { forming: !final, recent, full } });
    if (!final) this.captionFreshnessTimer = window.setTimeout(() => {
      if (!this.closed) this.patch({ captions: { ...this.state.captions, forming: false } });
    }, 2000);
  }

  sendCaptureSignal(type) {
    return this.sendSignal(type, {
      detected_by: "audio_worklet_vad",
      ...(this.evidenceReady ? { capture_id: this.evidenceCaptureId } : {}),
    }, {
      turnId: this.evidenceReady
        ? this.evidenceTurnId === "__warmup__" ? null : this.evidenceTurnId
        : this.state.currentQuestion?.turn_id,
    });
  }

  async playPerformance(performance, turnId) {
    if (performance?.delivery === "streaming_tts" && this.liveSpeech.hasSeen(performance.performance_id)) return;
    this.stopPerformance("replaced", false);
    if (performance?.delivery === "streaming_tts") {
      this.playLivePerformance(performance, turnId);
      return;
    }
    if (!performance?.audio_uri || !Array.isArray(performance.visemes) || !performance.visemes.length) {
      await this.failClosed(new Error("数字人表达缺少正式音频或 viseme 时间轴"));
      return;
    }
    const currentPerformance = { ...performance, turnId, startedAt: null, lastCue: "" };
    const audio = this.audioFactory(performance.audio_uri);
    const playback = {
      audio,
      performance: currentPerformance,
      handlers: null,
    };
    this.activePlayback = playback;
    this.performance = currentPerformance;
    this.audio = audio;
    audio.preload = "auto";
    const onPlay = () => {
      if (!this.isActivePlayback(playback)) return;
      window.clearTimeout(playback.watchdog);
      playback.watchdog = 0;
      this.patch({ speechPlayback: { status: "playing", message: "面试官正在说话" } });
      this.reportPlayback(playback, "playing");
      if (currentPerformance.startedAt != null) {
        cancelAnimationFrame(this.performanceFrame);
        this.tickPerformance(playback);
        return;
      }
      currentPerformance.startedAt = this.clock();
      currentPerformance.lastTickAt = currentPerformance.startedAt;
      if (this.finalReceivedAt != null) {
        this.observeMetric(
          performance.delivery === "s2s"
            ? "final_to_first_audio_realtime_ms"
            : "final_to_first_audio_cascade_ms",
          currentPerformance.startedAt - this.finalReceivedAt,
        );
        this.finalReceivedAt = null;
      }
      this.patch({ phase: "responding", avatar: { ...EMPTY_AVATAR, status: "speaking", performanceId: performance.performance_id } });
      this.tickPerformance(playback);
    };
    const onEnded = () => {
      if (!this.isActivePlayback(playback)) return;
      if (currentPerformance.startedAt == null) {
        this.blockPlayback(playback, "failed");
        return;
      }
      this.finishPerformance(playback);
    };
    const onError = () => {
      if (!this.isActivePlayback(playback)) return;
      this.blockPlayback(playback, "failed");
    };
    const onWaiting = () => {
      if (!this.isActivePlayback(playback)) return;
      this.reportPlayback(playback, "buffering");
      this.patch({ speechPlayback: { status: "loading", message: "正在缓冲面试官语音…" } });
      this.armPlaybackWatchdog(playback);
    };
    playback.handlers = { playing: onPlay, ended: onEnded, error: onError, waiting: onWaiting, stalled: onWaiting };
    for (const [type, handler] of Object.entries(playback.handlers)) audio.addEventListener(type, handler);
    await this.startPlayback(playback);
  }

  reportPlayback(playback, status) {
    if (!this.isActivePlayback(playback) || playback.reportedStatus === status) return;
    playback.reportedStatus = status;
    this.sendSignal("avatar.performance.playback", { performance_id: playback.performance.performance_id, status },
      { turnId: playback.performance.turnId });
  }

  armPlaybackWatchdog(playback) {
    if (playback.watchdog) return;
    playback.watchdog = window.setTimeout(() => {
      playback.watchdog = 0;
      if (this.isActivePlayback(playback)) this.blockPlayback(playback, "failed");
    }, 8000);
  }

  blockPlayback(playback, status) {
    if (!this.isActivePlayback(playback)) return;
    window.clearTimeout(playback.watchdog);
    playback.watchdog = 0;
    playback.audio.pause();
    cancelAnimationFrame(this.performanceFrame);
    this.performanceFrame = 0;
    this.reportPlayback(playback, status);
    this.patch({ speechPlayback: { status: "blocked", message: status === "blocked"
      ? "浏览器阻止了语音播放，请点击播放这句话。"
      : "这句话的语音尚未播放完成，可以重新播放；本题回答仍保留。" },
      avatar: { ...EMPTY_AVATAR } });
  }

  async retrySpeech() {
    const playback = this.activePlayback;
    if (!playback?.audio || this.isSafetyStopped() || this.state.speechPlayback?.status !== "blocked") return false;
    // Reuse only this still-active approved utterance. A replacement, pause or
    // server timeout invalidates the handle; no old question can be replayed.
    if (playback.audio.error) playback.audio.load?.();
    await this.startPlayback(playback);
    return true;
  }

  async startPlayback(playback) {
    if (!this.isActivePlayback(playback)) return;
    const audio = playback.audio;
    audio.muted = false;
    audio.volume = 1;
    this.patch({ speechPlayback: { status: "loading", message: "正在播放面试官语音…" } });
    this.armPlaybackWatchdog(playback);
    try {
      await audio.play();
    } catch (error) {
      if (!this.isActivePlayback(playback)) return;
      this.blockPlayback(playback, error?.name === "NotAllowedError" ? "blocked" : "failed");
    }
  }

  playLivePerformance(performance, turnId) {
    const currentPerformance = { ...performance, turnId, startedAt: null, lastCue: "" };
    const playback = { audio: null, performance: currentPerformance, live: null };
    this.activePlayback = playback;
    this.performance = currentPerformance;
    this.audio = null;
    const identity = { performance_id: performance.performance_id, output_id: performance.live_audio.output_id };
    try {
      playback.live = this.liveSpeech.begin(performance, {
        onReady: () => {
          if (this.isActivePlayback(playback)) this.sendSignal("avatar.performance.ready", identity, { turnId });
        },
        onPlaying: () => {
          if (!this.isActivePlayback(playback) || currentPerformance.startedAt !== null) return;
          currentPerformance.startedAt = this.clock();
          currentPerformance.lastTickAt = currentPerformance.startedAt;
          if (this.finalReceivedAt !== null && this.finalReceivedAt !== undefined) {
            this.observeMetric("final_to_first_audio_cascade_ms", currentPerformance.startedAt - this.finalReceivedAt);
            this.finalReceivedAt = null;
          }
          this.patch({ phase: "responding", avatar: { ...EMPTY_AVATAR, status: "speaking", performanceId: performance.performance_id } });
          this.tickPerformance(playback);
        },
        onFinished: () => {
          if (!this.isActivePlayback(playback)) return;
          this.stopPerformance("completed", false, performance.performance_id);
          this.sendSignal("avatar.performance.stopped", { ...identity, reason: "drained" }, { turnId });
        },
        onError: (error) => {
          if (this.isActivePlayback(playback)) this.failClosed(error);
        },
      });
    } catch (error) {
      if (this.isActivePlayback(playback)) this.failClosed(error);
    }
  }

  isActivePlayback(playback) {
    return Boolean(
      playback
      && this.activePlayback === playback
      && this.audio === playback.audio
      && this.performance === playback.performance,
    );
  }

  tickPerformance(playback) {
    if (!this.isActivePlayback(playback)) return;
    const { audio, performance } = playback;
    const tickAt = this.clock();
    if (
      performance.lastTickAt != null
      && tickAt - performance.lastTickAt >= 100
    ) {
      this.observeMetric("avatar_freeze_ms", tickAt - performance.lastTickAt);
    }
    performance.lastTickAt = tickAt;
    const elapsedMs = performance.delivery === "streaming_tts"
      ? playback.live?.positionMs() || 0
      : Math.max(0, audio.currentTime * 1000 - Number(performance.audio_clock_origin_ms || 0));
    if (!this.isActivePlayback(playback)) return;
    const waitingForLiveAudio = performance.delivery === "streaming_tts" && !playback.live?.isPlaying();
    const viseme = (!waitingForLiveAudio && activeCue(performance.visemes, elapsedMs)) || { shape: "sil", weight: 0 };
    const gesture = activeCue(performance.gestures || [], elapsedMs) || { gesture: "breathe", intensity: 0.3 };
    const cueKey = `${viseme.shape}:${viseme.weight}:${gesture.gesture}:${gesture.intensity}`;
    if (cueKey !== performance.lastCue) {
      performance.lastCue = cueKey;
      if (Number.isFinite(Number(viseme.at_ms))) {
        this.observeMetric(
          "avatar_viseme_drift_ms",
          Math.abs(elapsedMs - Number(viseme.at_ms)),
        );
      }
      this.patch({
        avatar: {
          status: "speaking",
          performanceId: performance.performance_id,
          viseme: viseme.shape,
          visemeWeight: viseme.weight,
          gesture: gesture.gesture,
          gestureIntensity: gesture.intensity,
        },
      });
    }
    this.performanceFrame = requestAnimationFrame(() => this.tickPerformance(playback));
  }

  finishPerformance(playback) {
    if (!this.isActivePlayback(playback)) return;
    const { performance } = playback;
    this.stopPerformance("completed", false, performance.performance_id);
    this.sendSignal("avatar.performance.stopped", { performance_id: performance.performance_id }, { turnId: performance.turnId });
  }

  stopPerformance(reason, notify = false, expectedPerformanceId = null) {
    const playback = this.activePlayback;
    const performance = playback?.performance || this.performance;
    if (
      expectedPerformanceId
      && performance?.performance_id !== expectedPerformanceId
    ) return false;
    this.metricReporter.clear();
    window.clearTimeout(playback?.watchdog);
    cancelAnimationFrame(this.performanceFrame);
    this.performanceFrame = 0;
    const audio = playback?.audio || this.audio;
    this.activePlayback = null;
    this.audio = null;
    this.performance = null;
    playback?.live?.cancel();
    this.liveSpeech.cancel();
    if (audio) {
      const handlers = playback?.handlers || {};
      for (const [type, handler] of Object.entries(handlers)) {
        audio.removeEventListener?.(type, handler);
      }
      try { audio.pause(); } catch { /* best-effort release after playback invalidation */ }
      try {
        if (typeof audio.removeAttribute === "function") audio.removeAttribute("src");
        else audio.src = "";
        audio.load?.();
      } catch { /* cleanup errors from an invalidated element are not playback failures */ }
    }
    this.patch({ speechPlayback: null, avatar: { ...EMPTY_AVATAR }, phase: this.state.floor === "candidate" ? "listening" : this.state.phase });
    if (notify && performance) {
      this.sendSignal("avatar.performance.stopped", { performance_id: performance.performance_id, reason }, { turnId: performance.turnId });
    }
    return Boolean(performance);
  }

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.patch({ connection: { ...this.state.connection, control: "recovering" }, phase: "connecting" });
    const delay = Math.min(5000, 400 * (2 ** this.reconnectAttempt));
    this.reconnectAttempt += 1;
    this.reconnectTimer = window.setTimeout(async () => {
      this.reconnectTimer = 0;
      try {
        const ticket = await this.issueTicket();
        await this.connectControl(ticket);
      } catch (error) {
        if (this.reconnectAttempt >= 6) await this.failClosed(new Error(`实时会话恢复失败：${error.message || error}`));
        else this.scheduleReconnect();
      }
    }, delay);
  }

  sendSignal(type, payload = {}, { idempotencyKey, turnId, causationId } = {}) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) throw new Error("实时面试控制通道尚未连接");
    const message = {
      type,
      idempotency_key: idempotencyKey || uniqueId(type),
      turn_id: turnId || null,
      causation_id: causationId || uniqueId("candidate"),
      payload,
    };
    this.socket.send(JSON.stringify(message));
    return message.idempotency_key;
  }

  observeMetric(metric, value) {
    this.metricReporter.observe(metric, value);
  }

  async failClosed(error) {
    this.stopPerformance("fatal", false);
    this.evidenceOpen = false;
    this.evidenceReady = false;
    this.answerFinishRequested = false;
    this.evidenceOpenCausationId = null;
    this.acknowledgedWarmupRetryCausationIds.clear();
    this.speechStartSignaled = false;
    this.pendingSpeechStop = false;
    this.warmupRetryRequested = false;
    this.warmupRetryCausationId = null;
    this.evidenceReassertPending = false;
    try { if (this.socket?.readyState === WebSocket.OPEN) this.sendSignal("pause", { reason: "candidate_runtime_fatal" }); } catch { /* channel may already be unavailable */ }
    let pauseConfirmed = false;
    try {
      const result = await reportCandidateRuntimeProblem({
        apiBase: this.apiBase,
        request: this.request,
        interviewId: this.interviewId,
        candidateSessionToken: this.candidateSessionToken,
        code: "CANDIDATE_RUNTIME_FAILED",
      });
      pauseConfirmed = result?.status === "paused";
    } catch { /* UI distinguishes an unconfirmed server pause below */ }
    this.patch({
      phase: "paused",
      evidence: { requested: false, ready: false },
      problem: {
        code: "CANDIDATE_EXPERIENCE_FATAL",
        message: error.message || String(error),
        recoverable: false,
        action: "pause_or_human_takeover",
        pauseConfirmed,
      },
    });
  }

  async shutdownMedia() {
    this.liveSpeech.close();
    await this.capture?.close?.();
    this.capture = null;
    await this.media?.close?.();
    this.media = null;
    this.stream?.getTracks?.().forEach((track) => track.stop());
    this.patch({ mediaStream: null, microphone: { ...this.state.microphone, enabled: false, localDetected: false, level: 0 } });
  }

  async close(reason) {
    window.clearTimeout(this.captionFreshnessTimer);
    if (this.closed) return;
    this.closed = true;
    this.intentionalClose = true;
    window.clearInterval(this.heartbeatTimer);
    window.clearTimeout(this.endpointTimer);
    window.clearTimeout(this.reconnectTimer);
    window.clearTimeout(this.eventResyncTimer);
    window.clearTimeout(this.backfillAuthorizationTimer);
    this.backfillAuthorizationTimer = 0;
    this.stopPerformance(reason, false);
    for (const waiter of this.backfillWaiters.values()) {
      waiter.reject(new Error("实时面试已关闭"));
    }
    this.backfillWaiters.clear();
    this.mediaGap = null;
    this.serverBackfillAuthorized = false;
    this.evidenceOpen = false;
    this.evidenceReady = false;
    this.evidenceOpenCausationId = null;
    this.acknowledgedWarmupRetryCausationIds.clear();
    this.speechStartSignaled = false;
    this.pendingSpeechStop = false;
    this.warmupRetryRequested = false;
    this.warmupRetryCausationId = null;
    this.evidenceReassertPending = false;
    await this.shutdownMedia();
    this.ring?.clear();
    if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close(1000, reason);
    this.socket = null;
    this.subscribers.clear();
  }

  patch(changes, notify = true) {
    this.state = { ...this.state, ...changes };
    if (["paused", "completed", "answer_retry_required"].includes(this.state.phase)) {
      this.state = { ...this.state,
        captions: { ...this.state.captions, forming: false },
        serverAudio: { received: false, receivedAt: null },
        microphone: { ...this.state.microphone, localDetected: false },
      };
    } else if (this.state.captureRecovery?.status === "recovering") {
      this.state.captions = { ...this.state.captions, forming: false };
    }
    if (!notify) return;
    const value = this.snapshot();
    for (const subscriber of this.subscribers) subscriber(value);
  }

  snapshot() {
    /** @type {CandidateExperienceView} */
    const view = {
      ...this.state,
      connection: { ...this.state.connection },
      microphone: { ...this.state.microphone },
      serverAudio: { ...this.state.serverAudio },
      evidence: { ...this.state.evidence },
      captureRecovery: this.state.captureRecovery ? { ...this.state.captureRecovery } : null,
      captions: {
        forming: this.state.captions.forming,
        recent: [...this.state.captions.recent],
        full: [...this.state.captions.full],
      },
      endpoint: { ...this.state.endpoint },
      calibration: { ...this.state.calibration },
      avatar: { ...this.state.avatar },
      recovery: { ...this.state.recovery },
    };
    if (!isCandidateExperienceView(view)) throw new Error("CandidateExperienceView contract violated");
    return view;
  }
}

export async function connectLiveKitMedia({ media, stream, onState, onRemoteAudioTrack, onRemoteAudioTrackRemoved }) {
  const { LocalAudioTrack, LocalVideoTrack, Room, RoomEvent, Track } = await import("livekit-client");
  const room = new Room({ adaptiveStream: true, dynacast: true, disconnectOnPageLeave: true });
  const remoteAudioElements = new Set();
  let publishedAudio = null;
  let recoveryMuted = false;
  room.on(RoomEvent.Reconnecting, () => {
    recoveryMuted = true;
    publishedAudio?.track?.mute?.();
    onState("reconnecting");
  });
  room.on(RoomEvent.Reconnected, () => onState("connected"));
  room.on(RoomEvent.Disconnected, () => onState("disconnected"));
  room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
    if (track.kind !== "audio") return;
    if (!isAuthorizedTakeoverAudioParticipant(participant)) {
      onRemoteAudioTrack?.(track, publication, participant);
      return;
    }
    const element = track.attach();
    element.autoplay = true;
    element.dataset.interviewHumanAudio = "true";
    element.hidden = true;
    document.body.append(element);
    remoteAudioElements.add(element);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => {
    onRemoteAudioTrackRemoved?.(track);
    for (const element of track.detach()) {
      remoteAudioElements.delete(element);
      element.remove();
    }
  });
  await room.connect(media.url, media.participant_token, { autoSubscribe: true });
  const audioTrack = stream.getAudioTracks()[0];
  publishedAudio = await room.localParticipant.publishTrack(new LocalAudioTrack(audioTrack), { source: Track.Source.Microphone });
  const publishedSources = ["microphone"];
  if (media.video_upstream_allowed) {
    const videoTrack = stream.getVideoTracks()[0];
    if (!videoTrack) {
      await room.disconnect();
      throw new Error("录像场次缺少摄像头轨道");
    }
    await room.localParticipant.publishTrack(new LocalVideoTrack(videoTrack), { source: Track.Source.Camera });
    publishedSources.push("camera");
  }
  return {
    room,
    publishedSources,
    async measureNetwork() {
      return measurePublishedTrackNetwork(publishedAudio?.track);
    },
    async setRecoveryMute(muted) {
      recoveryMuted = Boolean(muted);
      if (recoveryMuted) publishedAudio?.track?.mute?.();
      else publishedAudio?.track?.unmute?.();
    },
    async close() {
      for (const element of remoteAudioElements) element.remove();
      remoteAudioElements.clear();
      await room.disconnect();
    },
  };
}

export async function measurePublishedTrackNetwork(track, { attempts = 5, intervalMs = 250 } = {}) {
  if (!track?.getRTCStatsReport) {
    throw new Error("浏览器未提供 LiveKit RTCStats，正式面试已暂停");
  }
  const roundTrips = [];
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const report = await track.getRTCStatsReport();
    const sample = [];
    report?.forEach?.((item) => {
      if (item.type === "candidate-pair" && item.state === "succeeded" && item.nominated) {
        const seconds = Number(item.currentRoundTripTime);
        if (Number.isFinite(seconds) && seconds >= 0) sample.push(seconds * 1000);
      }
      if (item.type === "remote-inbound-rtp") {
        const seconds = Number(item.roundTripTime);
        if (Number.isFinite(seconds) && seconds >= 0) sample.push(seconds * 1000);
      }
    });
    if (sample.length) {
      roundTrips.push(sample.reduce((sum, value) => sum + value, 0) / sample.length);
    }
    if (roundTrips.length >= 3) break;
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
  if (!roundTrips.length) {
    throw new Error("LiveKit RTCStats 尚未形成有效 RTT，正式面试已暂停");
  }
  const recent = roundTrips.slice(-5);
  const rttMs = recent.reduce((sum, value) => sum + value, 0) / recent.length;
  const jitterMs = recent.slice(1).reduce(
    (sum, value, index) => sum + Math.abs(value - recent[index]),
    0,
  ) / Math.max(1, recent.length - 1);
  return {
    rttMs: Math.round(rttMs * 10) / 10,
    jitterMs: Math.round(jitterMs * 10) / 10,
  };
}

export function isAuthorizedTakeoverAudioParticipant(participant) {
  if (!String(participant?.identity || "").startsWith("takeover:")) return false;
  try {
    const metadata = JSON.parse(String(participant?.metadata || ""));
    return metadata?.role === "takeover";
  } catch {
    return false;
  }
}

function capabilityProjection(report) {
  return {
    webrtc: Boolean(report?.webrtc_supported),
    audio_worklet: Boolean(report?.audio_worklet_supported),
    webgl: Boolean(report?.webgl_supported),
    camera: Boolean(report?.camera_granted),
    microphone: Boolean(report?.microphone_granted),
    speaker: Boolean(report?.speaker_verified),
    avatar_fps: Number(report?.avatar_fps || 0),
    media_recorder: Boolean(report?.media_recorder_supported),
    browser: navigator.userAgent.slice(0, 160),
  };
}

function agentWebSocketUrl(path, ticket) {
  const base = new URL(path, window.location.origin);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  base.searchParams.set("ticket", ticket);
  return base.toString();
}

function phaseForFloor(floor, current) {
  if (floor === "agent") return "responding";
  if (floor === "candidate") return "listening";
  if (floor === "human") return "paused";
  return ["connecting", "completed", "paused"].includes(current) ? current : "understanding";
}

function activeCue(cues, elapsedMs) {
  for (let index = cues.length - 1; index >= 0; index -= 1) {
    const cue = cues[index];
    if (elapsedMs >= cue.at_ms && elapsedMs < cue.at_ms + cue.duration_ms) return cue;
  }
  return null;
}

function uniqueId(prefix) {
  return `${prefix}:${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function arrayBufferToBase64(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = "";
  const stride = 8192;
  for (let offset = 0; offset < bytes.length; offset += stride) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + stride));
  }
  return btoa(binary);
}

function calibrationForFloorReason(reason, current) {
  if (reason === "warmup_listening") return "listening";
  if (reason === "warmup_retry") return "retrying";
  if (reason === "warmup_stream_open") return "listening";
  if (reason === "barge_in" && ["pending", "opening"].includes(current)) return "listening";
  if (reason === "warmup_awaiting_confirmation") return "awaiting_confirmation";
  if (reason === "agent_finished" && current === "confirming") return "completed";
  return current;
}

function readRememberedPreflight() {
  try { return JSON.parse(sessionStorage.getItem("candidate-preflight-report") || "null"); }
  catch { return null; }
}

function isViewObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function rememberPreflightReport(report) {
  sessionStorage.setItem("candidate-preflight-report", JSON.stringify(report));
}
