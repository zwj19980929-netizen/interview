import { OrderedAgentEventStream } from "./agent-event-runtime.js";

export function createEnterpriseInterviewMonitor({
  apiBase = "/api/v1",
  request,
  socketFactory = (url) => new WebSocket(url),
} = {}) {
  if (typeof request !== "function") throw new TypeError("enterprise monitor requires request");
  return {
    async open({ interviewId, actorId, canTakeover }) {
      const monitor = new EnterpriseMonitorRun({
        apiBase,
        request,
        socketFactory,
        interviewId,
        actorId,
        canTakeover: Boolean(canTakeover),
      });
      await monitor.open();
      return monitor.interface();
    },
  };
}

class EnterpriseMonitorRun {
  constructor(options) {
    Object.assign(this, options);
    this.listeners = new Set();
    this.socket = null;
    this.room = null;
    this.takeoverRoom = null;
    this.livekit = null;
    this.candidateStream = null;
    this.humanMedia = null;
    this.humanTrack = null;
    this.closed = false;
    this.intentionalClose = false;
    this.sequence = 0;
    this.eventStream = new OrderedAgentEventStream({ audience: "enterprise" });
    this.reconnectAttempt = 0;
    this.reconnectTimer = 0;
    this.eventResyncTimer = 0;
    this.renewTimer = 0;
    this.pendingTakeover = false;
    this.permitRenewalPending = false;
    this.state = {
      connection: { control: "connecting", media: "connecting" },
      session: null,
      floor: "none",
      takeover: null,
      candidateStream: null,
      captions: [],
      acts: [],
      problem: null,
    };
  }

  interface() {
    return Object.freeze({
      subscribe: (listener) => this.subscribe(listener),
      acquireTakeover: (reason) => this.acquireTakeover(reason),
      enableTakeoverAudio: () => this.enableTakeoverAudio(),
      releaseTakeover: () => this.releaseTakeover(),
      recordHumanSpeech: (transcript) => this.recordHumanSpeech(transcript),
      close: () => this.close(),
      getSnapshot: () => this.snapshot(),
    });
  }

  async open() {
    try {
      const ticket = await this.issueTicket();
      await this.connectMedia(ticket.media);
      await this.connectControl(ticket);
    } catch (error) {
      await this.close();
      throw error;
    }
  }

  subscribe(listener) {
    this.listeners.add(listener);
    listener(this.snapshot());
    return () => this.listeners.delete(listener);
  }

  async issueTicket() {
    return this.request(`${this.apiBase}/interviews/${encodeURIComponent(this.interviewId)}/agent-ticket`, {
      method: "POST",
    });
  }

  async connectMedia(media) {
    if (media?.status !== "ready" || !media.url || !media.participant_token) {
      throw new Error("LiveKit 监看媒体面未就绪，不能伪装成实时监看");
    }
    this.livekit = await import("livekit-client");
    const { Room, RoomEvent } = this.livekit;
    const room = new Room({ adaptiveStream: true, dynacast: true, disconnectOnPageLeave: true });
    this.room = room;
    this.candidateStream = new MediaStream();
    room.on(RoomEvent.Reconnecting, () => this.patch({ connection: { ...this.state.connection, media: "reconnecting" } }));
    room.on(RoomEvent.Reconnected, () => this.patch({ connection: { ...this.state.connection, media: "connected" } }));
    room.on(RoomEvent.Disconnected, () => this.patch({ connection: { ...this.state.connection, media: "disconnected" } }));
    room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
      if (!String(participant?.identity || "").startsWith("candidate:")) return;
      const mediaTrack = track.mediaStreamTrack;
      if (!mediaTrack || this.candidateStream.getTracks().some((item) => item.id === mediaTrack.id)) return;
      this.candidateStream.addTrack(mediaTrack);
      this.patch({ candidateStream: this.candidateStream });
    });
    room.on(RoomEvent.TrackUnsubscribed, (track) => {
      const mediaTrack = track.mediaStreamTrack;
      if (mediaTrack && this.candidateStream.getTracks().some((item) => item.id === mediaTrack.id)) {
        this.candidateStream.removeTrack(mediaTrack);
        this.patch({ candidateStream: this.candidateStream });
      }
    });
    await room.connect(media.url, media.participant_token, { autoSubscribe: true });
    this.patch({
      candidateStream: this.candidateStream,
      connection: { ...this.state.connection, media: "connected" },
    });
  }

  connectControl(ticket) {
    return new Promise((resolve, reject) => {
      const socket = this.socketFactory(agentSocketUrl(ticket.agent_ws_url, ticket.agent_ticket));
      this.socket = socket;
      let opened = false;
      const timeout = window.setTimeout(() => {
        if (!opened) reject(new Error("企业监看控制通道连接超时"));
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
            capabilities: {
              webrtc: true,
              audio_worklet: false,
              webgl: false,
              camera: false,
              microphone: this.canTakeover,
              speaker: true,
              avatar_fps: 0,
              media_recorder: false,
              browser: navigator.userAgent.slice(0, 160),
            },
          },
        }));
        this.send("client.ready", { source: "enterprise_agent_monitor_v1" });
        resolve();
      }, { once: true });
      socket.addEventListener("message", ({ data }) => this.onEvent(data));
      socket.addEventListener("error", () => {
        if (!opened) reject(new Error("企业监看控制通道连接失败"));
      });
      socket.addEventListener("close", () => {
        window.clearTimeout(timeout);
        if (this.socket === socket) this.socket = null;
        if (!this.intentionalClose && !this.closed) this.scheduleReconnect();
      });
    });
  }

  onEvent(raw) {
    let event;
    try { event = JSON.parse(raw); }
    catch {
      this.beginEventResync({
        reason: "invalid_json",
        problem: { code: "AGENT_EVENT_ENVELOPE_INVALID", message: "监看事件不是合法 JSON" },
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
      this.patch({
        session: payload,
        floor: payload.floor || this.state.floor,
        takeover: payload.takeover || null,
      });
      this.syncLease();
      return;
    }
    if (event.type === "floor.changed") this.patch({ floor: payload.owner || "none" });
    if (event.type === "transcript.partial" || event.type === "transcript.final") {
      const captions = event.type === "transcript.final"
        ? [...this.state.captions, { text: payload.text, final: true, turnId: event.turn_id }].slice(-100)
        : [...this.state.captions.filter((item) => item.final), { text: payload.text, final: false, turnId: event.turn_id }].slice(-100);
      this.patch({ captions });
    }
    if (event.type === "conversation.act.selected") {
      this.patch({ acts: [...this.state.acts, { ...payload, turnId: event.turn_id }].slice(-50) });
    }
    if (event.type === "takeover.changed") {
      this.patch({
        takeover: payload,
        session: {
          ...(this.state.session || {}),
          status: payload.status === "active" || payload.ai_resumed === false
            ? "paused"
            : this.state.session?.status,
        },
      });
      this.syncLease();
    }
    if (event.type === "problem") {
      if (this.pendingTakeover && String(payload.code || "").startsWith("TAKEOVER_")) {
        this.pendingTakeover = false;
        this.stopHumanAudio();
      }
      this.patch({ problem: payload });
    }
    if (event.type === "completed") this.patch({ session: { ...(this.state.session || {}), status: "completed" } });
  }

  beginEventResync(result) {
    this.eventStream.awaitingSnapshot = true;
    this.stopHumanAudio();
    const validation = result.problem || {};
    const isGap = result.reason === "sequence_gap" || result.reason === "duplicate_event_id";
    this.patch({
      connection: { ...this.state.connection, control: "resyncing" },
      problem: {
        code: validation.code || (isGap ? "AGENT_EVENT_SEQUENCE_GAP" : "AGENT_EVENT_INVALID"),
        message: validation.message || "监看事件顺序不连续，正在请求服务端权威快照。",
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

  async acquireTakeover(reason) {
    if (!this.canTakeover) throw new Error("当前角色只有监看权限");
    const normalized = String(reason || "").trim();
    if (!normalized) throw new Error("人工接管必须填写原因");
    this.pendingTakeover = true;
    this.send("takeover.acquire", { reason: normalized });
  }

  async enableTakeoverAudio() {
    if (!this.ownsLease()) throw new Error("当前没有属于你的接管 lease");
    const lease = this.state.takeover;
    const permit = await this.request(
      `${this.apiBase}/interviews/${encodeURIComponent(this.interviewId)}/takeover/media-permit`,
      {
        method: "POST",
        body: { lease_id: lease.lease_id, expected_version: lease.version },
      },
    );
    await this.prepareHumanAudio();
    await this.connectTakeoverMedia(permit.media);
    await this.publishHumanAudio();
  }

  async connectTakeoverMedia(media) {
    if (media?.status !== "ready" || !media.url || !media.participant_token) {
      throw new Error("人工接管麦克风 permit 未就绪");
    }
    this.stopTakeoverRoom();
    const { Room, RoomEvent } = this.livekit;
    const room = new Room({ adaptiveStream: false, dynacast: false, disconnectOnPageLeave: true });
    room.on(RoomEvent.Disconnected, () => {
      if (this.takeoverRoom === room) {
        this.takeoverRoom = null;
        this.humanTrack = null;
      }
    });
    await room.connect(media.url, media.participant_token, { autoSubscribe: false });
    this.takeoverRoom = room;
  }

  async prepareHumanAudio() {
    if (this.humanMedia) return;
    this.humanMedia = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
  }

  async publishHumanAudio() {
    if (this.humanTrack || !this.humanMedia || !this.takeoverRoom) return;
    const audioTrack = this.humanMedia.getAudioTracks()[0];
    if (!audioTrack) throw new Error("人工接管麦克风不可用");
    this.humanTrack = new this.livekit.LocalAudioTrack(audioTrack);
    await this.takeoverRoom.localParticipant.publishTrack(this.humanTrack, {
      source: this.livekit.Track.Source.Microphone,
    });
  }

  async releaseTakeover() {
    const lease = this.state.takeover;
    if (!this.ownsLease() || !lease?.lease_id) throw new Error("接管 lease 已丢失");
    this.stopHumanAudio();
    this.send("takeover.release", {
      lease_id: lease.lease_id,
      expected_version: lease.version,
    });
  }

  recordHumanSpeech(transcript) {
    if (!this.ownsLease()) throw new Error("只有接管者能登记人工问话");
    this.send("human.speech", { transcript: String(transcript || "").trim() });
  }

  ownsLease() {
    return this.state.takeover?.status === "active"
      && this.state.takeover?.actor_id === this.actorId;
  }

  syncLease() {
    window.clearInterval(this.renewTimer);
    this.renewTimer = 0;
    if (this.ownsLease()) {
      const lease = this.state.takeover;
      const permitAvailable = lease.media_permit_available !== false
        && Number(lease.media_permit_lease_version || 0) !== Number(lease.version || 0);
      if (!this.takeoverRoom && permitAvailable) {
        this.pendingTakeover = false;
        this.permitRenewalPending = false;
        this.enableTakeoverAudio().catch((error) => this.patch({
          problem: { code: "TAKEOVER_AUDIO_FAILED", message: error.message, recoverable: true },
        }));
      } else if (!this.takeoverRoom && !this.permitRenewalPending) {
        this.permitRenewalPending = true;
        this.send("takeover.renew", {
          lease_id: lease.lease_id,
          expected_version: lease.version,
        });
      }
      this.renewTimer = window.setInterval(() => {
        const lease = this.state.takeover;
        if (!this.ownsLease() || !lease?.lease_id) return;
        this.send("takeover.renew", {
          lease_id: lease.lease_id,
          expected_version: lease.version,
        });
      }, 30_000);
    } else {
      this.pendingTakeover = false;
      this.permitRenewalPending = false;
      this.stopHumanAudio();
    }
  }

  send(type, payload) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) throw new Error("监看控制通道未连接");
    this.socket.send(JSON.stringify({
      type,
      payload,
      idempotency_key: uniqueId(type),
      turn_id: this.state.session?.current_turn_id || null,
      causation_id: uniqueId("enterprise"),
    }));
  }

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.patch({ connection: { ...this.state.connection, control: "recovering" } });
    const delay = Math.min(5000, 400 * (2 ** this.reconnectAttempt));
    this.reconnectAttempt += 1;
    this.reconnectTimer = window.setTimeout(async () => {
      this.reconnectTimer = 0;
      try {
        await this.connectControl(await this.issueTicket());
      } catch (error) {
        if (this.reconnectAttempt >= 6) {
          this.patch({ problem: { code: "MONITOR_RECONNECT_FAILED", message: error.message, recoverable: false } });
        } else {
          this.scheduleReconnect();
        }
      }
    }, delay);
  }

  stopHumanAudio() {
    if (this.humanTrack && this.takeoverRoom) {
      this.takeoverRoom.localParticipant.unpublishTrack(this.humanTrack, true);
    }
    this.humanTrack = null;
    this.humanMedia?.getTracks().forEach((track) => track.stop());
    this.humanMedia = null;
    this.stopTakeoverRoom();
  }

  stopTakeoverRoom() {
    const room = this.takeoverRoom;
    this.takeoverRoom = null;
    if (room) void room.disconnect();
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    this.intentionalClose = true;
    window.clearInterval(this.renewTimer);
    window.clearTimeout(this.reconnectTimer);
    window.clearTimeout(this.eventResyncTimer);
    this.stopHumanAudio();
    if (this.socket?.readyState < WebSocket.CLOSING) this.socket.close(1000, "monitor_closed");
    this.stopTakeoverRoom();
    await this.room?.disconnect();
    this.listeners.clear();
  }

  patch(changes) {
    this.state = { ...this.state, ...changes };
    const value = this.snapshot();
    for (const listener of this.listeners) listener(value);
  }

  snapshot() {
    return {
      ...this.state,
      connection: { ...this.state.connection },
      captions: [...this.state.captions],
      acts: [...this.state.acts],
    };
  }
}

function agentSocketUrl(path, ticket) {
  const url = new URL(path, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("ticket", ticket);
  return url.toString();
}

function uniqueId(prefix) {
  return `${prefix}:${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
}
