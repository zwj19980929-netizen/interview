import { createCandidateAudioCapture } from "./audio-worklet-capture.js";
import { EncryptedAudioRingBuffer } from "./encrypted-audio-ring.js";
import { claimPreparedCandidateMedia, runCandidatePreflight } from "./preflight.js";
import { OrderedAgentEventStream } from "../interviews/agent-event-runtime.js";

const EMPTY_AVATAR = {
  status: "idle",
  performanceId: null,
  viseme: "sil",
  visemeWeight: 0,
  gesture: "idle",
  gestureIntensity: 0,
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
        isAgentSpeaking: () => this.state.avatar.status === "speaking",
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
    let causationId = null;
    if (type === "continue_speaking") {
      window.clearTimeout(this.endpointTimer);
      this.endpointTimer = 0;
      this.patch({ endpoint: { active: false, deadlineAt: null }, phase: "listening" });
    }
    if (type === "finish_answer") {
      window.clearTimeout(this.endpointTimer);
      this.endpointTimer = 0;
      this.evidenceOpen = false;
      this.evidenceReady = false;
      this.evidenceOpenCausationId = null;
      this.acknowledgedWarmupRetryCausationIds.clear();
      this.speechStartSignaled = false;
      this.pendingSpeechStop = false;
      this.evidenceReassertPending = false;
      this.patch({
        endpoint: { active: false, deadlineAt: null },
        evidence: { requested: false, ready: false },
        phase: "understanding",
      });
    }
    if (type === "pause") this.stopPerformance("candidate_pause");
    if (type === "warmup.confirm") {
      this.patch({
        calibration: { ...this.state.calibration, status: "confirming" },
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
          this.socket = null;
          // A reset command written to a WebSocket is not durable until the
          // server reflects it in a snapshot/ack. On disconnect, drop the
          // local intent so a durable retry-required snapshot restores the
          // explicit retry button instead of leaving the UI in limbo.
          this.warmupRetryRequested = false;
          this.warmupRetryCausationId = null;
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
    if (event.type === "floor.changed") {
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
        this.evidenceReady = true;
        this.evidenceReassertPending = false;
      }
      this.patch({
        floor: payload.owner,
        phase: payload.owner === "candidate"
          ? this.evidenceReady ? "listening" : "preparing"
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
      this.patch({
        endpoint: { active: true, deadlineAt: this.clock() + Number(payload.endpoint_countdown_ms || 2500) },
      });
      return;
    }
    if (event.type === "transcript.partial") {
      if (!this.partialObserved && this.evidenceOpenedAt != null) {
        this.partialObserved = true;
        this.observeMetric("partial_first_token_ms", this.clock() - this.evidenceOpenedAt);
      }
      this.appendCaption(payload.text, false);
      return;
    }
    if (event.type === "transcript.final") {
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
      if (event.turn_id && payload.act_type !== "opening") {
        this.patch({
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
      await this.playPerformance(payload, event.turn_id);
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
      this.stopPerformance("human_takeover");
      this.patch({ phase: "paused", floor: payload.status === "active" ? "human" : "none" });
      return;
    }
    if (event.type === "problem") {
      const waiter = this.backfillWaiters.get(event.causation_id);
      if (waiter) {
        this.backfillWaiters.delete(event.causation_id);
        waiter.reject(new Error(payload.message || payload.code || "浏览器音频恢复失败"));
      }
      const retryWarmup = payload.recoverable && payload.action === "retry_warmup";
      const rejectsCurrentEvidenceOpen = Boolean(this.evidenceOpenCausationId)
        && event.causation_id === this.evidenceOpenCausationId;
      if (!payload.recoverable || retryWarmup || rejectsCurrentEvidenceOpen) {
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
      this.patch({
        problem: payload,
        phase: retryWarmup ? "preparing" : payload.recoverable ? this.state.phase : "paused",
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
          floor: "none",
          endpoint: { active: false, deadlineAt: null },
          microphone: { ...this.state.microphone, localDetected: false },
          evidence: { requested: false, ready: false },
        } : {}),
      });
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
      if (!payload.recoverable) this.stopPerformance("fatal_problem");
      return;
    }
    if (event.type === "completed") {
      this.patch({ completion: payload, phase: "completed", floor: "none" });
      await this.shutdownMedia();
    }
  }

  beginEventResync(result) {
    this.eventStream.awaitingSnapshot = true;
    this.stopPerformance("event_stream_resync", false);
    const validation = result.problem || {};
    const isGap = result.reason === "sequence_gap" || result.reason === "duplicate_event_id";
    this.patch({
      connection: { ...this.state.connection, control: "resyncing" },
      problem: {
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
    const calibrationRetryRequired = Boolean(payload.calibration_retry_required);
    const calibrationStatus = payload.calibration_status || this.state.calibration.status;
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
    if (sessionStatus === "paused" || sessionStatus === "completed") {
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
      ...(calibrationRetryRequired ? {
        endpoint: { active: false, deadlineAt: null },
        microphone: { ...this.state.microphone, localDetected: false },
        evidence: { requested: false, ready: false },
        recovery: this.ring?.snapshot?.() || this.state.recovery,
      } : {}),
      ...(["paused", "completed"].includes(sessionStatus) ? {
        endpoint: { active: false, deadlineAt: null },
        microphone: { ...this.state.microphone, localDetected: false },
        evidence: { requested: false, ready: false },
      } : {}),
      phase: sessionStatus === "paused"
        ? "paused"
        : sessionStatus === "completed"
          ? "completed"
          : payload.floor === "candidate" && !this.evidenceReady
            ? "preparing"
            : phaseForFloor(payload.floor, this.state.phase),
    });
    const reassertEvidenceOpen = this.evidenceReassertPending
      && this.evidenceOpen
      && !this.evidenceReady;
    if (
      sessionStatus === "in_progress"
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
      phase: "preparing",
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
    if (this.closed || ["paused", "completed"].includes(this.state.phase) || this.state.completion) {
      return;
    }
    const detectedAt = this.clock();
    const agentWasSpeaking = this.state.avatar.status === "speaking";
    this.localSpeechStartedAt = detectedAt;
    this.patch({
      microphone: { ...this.state.microphone, localDetected: true },
      phase: this.evidenceReady ? "listening" : "preparing",
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
    this.sendSignal("speech.stopped", { detected_by: "audio_worklet_vad" });
    this.speechStartSignaled = false;
    // 是否开始 2.5 秒静音收口只能由服务端权威事件决定。客户端若自行倒计时
    // 并切到“理解中”，会在服务端已暂停或没有收到音频时制造虚假进度。
  }

  signalSpeechStarted() {
    if (this.speechStartSignaled || this.socket?.readyState !== WebSocket.OPEN) return false;
    this.sendSignal("speech.started", { detected_by: "audio_worklet_vad" });
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
      this.sendSignal("speech.stopped", { detected_by: "audio_worklet_vad" });
      this.speechStartSignaled = false;
    }
  }

  appendCaption(text, final) {
    const normalized = String(text || "").trim();
    if (!normalized) return;
    const full = final
      ? [...this.state.captions.full, { text: normalized, final: true, at: new Date().toISOString() }]
      : this.state.captions.full;
    const recent = final
      ? full.slice(-2)
      : [...full.slice(-1), { text: normalized, final: false, at: new Date().toISOString() }].slice(-2);
    this.patch({ captions: { forming: !final, recent, full } });
  }

  async playPerformance(performance, turnId) {
    this.stopPerformance("replaced", false);
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
      if (!this.isActivePlayback(playback) || currentPerformance.startedAt != null) return;
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
      this.finishPerformance(playback);
    };
    const onError = () => {
      if (!this.isActivePlayback(playback)) return;
      this.failClosed(new Error("数字人正式语音无法播放"));
    };
    playback.handlers = { play: onPlay, ended: onEnded, error: onError };
    audio.addEventListener("play", onPlay);
    audio.addEventListener("ended", onEnded, { once: true });
    audio.addEventListener("error", onError, { once: true });
    try {
      await audio.play();
    } catch (error) {
      if (!this.isActivePlayback(playback)) return;
      await this.failClosed(new Error(`数字人正式语音播放被阻止：${error.message || error}`));
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
    const elapsedMs = Math.max(0, audio.currentTime * 1000 - Number(performance.audio_clock_origin_ms || 0));
    const viseme = activeCue(performance.visemes, elapsedMs) || { shape: "sil", weight: 0 };
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
    cancelAnimationFrame(this.performanceFrame);
    this.performanceFrame = 0;
    const audio = playback?.audio || this.audio;
    this.activePlayback = null;
    this.audio = null;
    this.performance = null;
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
    this.patch({ avatar: { ...EMPTY_AVATAR }, phase: this.state.floor === "candidate" ? "listening" : this.state.phase });
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
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 0 || numeric > 300_000) return;
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    try {
      this.sendSignal("telemetry.observe", { metric, value: numeric });
    } catch { /* telemetry must never break the interview */ }
  }

  async failClosed(error) {
    this.stopPerformance("fatal", false);
    this.evidenceOpen = false;
    this.evidenceReady = false;
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
    await this.capture?.close?.();
    this.capture = null;
    await this.media?.close?.();
    this.media = null;
    this.stream?.getTracks?.().forEach((track) => track.stop());
    this.patch({ mediaStream: null, microphone: { ...this.state.microphone, enabled: false, localDetected: false, level: 0 } });
  }

  async close(reason) {
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

async function connectLiveKitMedia({ media, stream, onState }) {
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
  room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
    if (track.kind !== "audio" || !isAuthorizedTakeoverAudioParticipant(participant)) return;
    const element = track.attach();
    element.autoplay = true;
    element.dataset.interviewHumanAudio = "true";
    element.hidden = true;
    document.body.append(element);
    remoteAudioElements.add(element);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => {
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
