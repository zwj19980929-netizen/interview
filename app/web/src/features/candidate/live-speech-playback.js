/** A single approved LiveKit output. Unknown tracks never attach or autoplay.
 *
 * Producer EOF is not playback completion. Only a progressing media clock
 * reaching the declared sample length may acknowledge a drained output.
 * This is a browser media clock, not a claim of sample-accurate RTP alignment.
 */
export class LiveSpeechPlayback {
  constructor({
    audioFactory = () => document.createElement("audio"),
    now = () => performance.now(),
    setTimer = (fn, ms) => window.setTimeout(fn, ms),
    clearTimer = (id) => window.clearTimeout(id),
    timeoutMs = 10_000,
  } = {}) {
    Object.assign(this, { audioFactory, now, setTimer, clearTimer, timeoutMs });
    this.pending = new Map();
    this.seen = new Set();
    this.active = null;
    this.closed = false;
  }

  hasSeen(performanceId) { return this.seen.has(performanceId); }

  trackSubscribed(track, publication, participant) {
    if (this.closed || track?.kind !== "audio") return;
    const sid = publication?.trackSid || track.sid;
    if (!sid || this.pending.has(sid) || this.active?.track?.sid === sid) return;
    const entry = { track, sid, name: publication?.trackName, identity: participant?.identity, timer: null };
    if (this.matches(this.active, entry)) {
      this.attach(this.active, entry);
      return;
    }
    while (this.pending.size >= 4) this.dropPending(this.pending.keys().next().value);
    entry.timer = this.setTimer(() => this.dropPending(sid), 10_000);
    this.pending.set(sid, entry);
  }

  trackUnsubscribed(track) {
    this.dropPending(track?.sid);
    if (this.active?.track === track) this.fail(this.active, "数字人音轨在播放完成前断开");
  }

  begin(performance, callbacks) {
    if (this.closed || this.hasSeen(performance.performance_id)) return null;
    // A session has a bounded number of outputs; never evict a tombstone and
    // accidentally allow a late/replayed output to become audible again.
    if (this.seen.size >= 512) throw new Error("数字人播放身份数量超限");
    this.cancel();
    this.seen.add(performance.performance_id);
    const state = {
      performance, callbacks, track: null, audio: null, handlers: null,
      origin: null, lastPosition: 0, playing: false, waitingAt: null,
      finalSamples: null, timer: null, lastProgressAt: this.now(),
    };
    this.active = state;
    const entry = this.pending.get(performance.live_audio.track_sid);
    if (this.matches(state, entry)) {
      this.dropPending(entry.sid);
      this.attach(state, entry);
    }
    this.poll(state);
    return Object.freeze({
      positionMs: () => this.position(state),
      isPlaying: () => this.active === state && state.playing,
      cancel: () => { if (this.active === state) this.cancel(); },
    });
  }

  producerFinished(payload) {
    const state = this.active;
    if (!state || payload.performance_id !== state.performance.performance_id
      || payload.output_id !== state.performance.live_audio.output_id) return;
    if (!Number.isSafeInteger(payload.total_samples) || payload.total_samples <= 0
      || payload.sample_rate_hz !== state.performance.live_audio.sample_rate_hz
      || payload.total_samples > payload.sample_rate_hz * 600
      || (state.finalSamples !== null && state.finalSamples !== payload.total_samples)) {
      this.fail(state, "数字人流式音频结束位置无效");
      return;
    }
    state.finalSamples = payload.total_samples;
    this.checkDrained(state);
  }

  matches(state, entry) {
    const binding = state?.performance.live_audio;
    return Boolean(binding && entry && binding.track_sid === entry.sid
      && binding.publisher_identity === entry.identity && binding.track_name === entry.name);
  }

  attach(state, entry) {
    if (this.active !== state || state.audio || !this.matches(state, entry)) return;
    try {
      const audio = this.audioFactory();
      state.audio = audio;
      state.track = entry.track;
      audio.autoplay = false;
      audio.hidden = true;
      audio.playsInline = true;
      const current = () => this.active === state;
      const onPlaying = () => {
        if (!current()) return;
        const time = audio.currentTime;
        if (!Number.isFinite(time) || time < 0) {
          this.fail(state, "浏览器未提供可靠的流式播放时钟");
          return;
        }
        // If a stalled MediaStream timeline advanced without playback, that
        // clock cannot safely certify sample drainage. Do not fabricate ACK.
        if (state.waitingAt !== null && time - state.waitingAt > 0.05) {
          this.fail(state, "浏览器流式时钟在等待音频时仍推进，无法确认播放完成");
          return;
        }
        state.waitingAt = null;
        state.playing = true;
        state.lastProgressAt = this.now();
        if (state.origin === null) {
          state.origin = time;
          state.callbacks.onPlaying();
        }
      };
      const onWaiting = () => {
        if (!current()) return;
        this.position(state);
        state.playing = false;
        state.waitingAt = audio.currentTime;
      };
      const onEnded = () => {
        if (!current() || this.checkDrained(state)) return;
        this.fail(state, "数字人音轨提前结束，未确认完整播放");
      };
      const onError = () => { if (current()) this.fail(state, "数字人流式语音无法播放"); };
      state.handlers = { playing: onPlaying, waiting: onWaiting, stalled: onWaiting,
        pause: onWaiting, ended: onEnded, error: onError };
      for (const [type, handler] of Object.entries(state.handlers)) audio.addEventListener(type, handler);
      entry.track.attach(audio);
      // Asking play precedes ready, but awaiting play would deadlock: the
      // publisher is deliberately waiting for ready before sending any PCM.
      const playing = audio.play();
      if (current()) state.callbacks.onReady();
      Promise.resolve(playing).catch(() => {
        if (current()) this.fail(state, "数字人流式语音播放被浏览器阻止");
      });
    } catch {
      this.fail(state, "数字人流式音轨无法连接");
    }
  }

  position(state) {
    if (this.active !== state || state.origin === null || !state.playing) return state.lastPosition;
    const position = (state.audio.currentTime - state.origin) * 1000;
    if (!Number.isFinite(position) || position + 1 < state.lastPosition || position < 0) {
      this.fail(state, "浏览器流式播放时钟无效或倒退");
      return state.lastPosition;
    }
    if (position > state.lastPosition) state.lastProgressAt = this.now();
    state.lastPosition = position;
    return position;
  }

  checkDrained(state) {
    const position = this.position(state);
    if (this.active !== state || state.origin === null || state.finalSamples === null
      || !state.playing || state.audio.paused === true
      || position * state.performance.live_audio.sample_rate_hz < state.finalSamples * 1000) return false;
    const onFinished = state.callbacks.onFinished;
    this.cancel();
    onFinished();
    return true;
  }

  poll(state) {
    if (this.active !== state || this.checkDrained(state)) return;
    if (this.active !== state) return; // A bad media clock may fail during the check.
    if (this.now() - state.lastProgressAt >= this.timeoutMs) {
      this.fail(state, "数字人流式音频未形成可靠播放进度，请确认会话状态");
      return;
    }
    state.timer = this.setTimer(() => this.poll(state), 50);
  }

  fail(state, message) {
    if (this.active !== state) return;
    const onError = state.callbacks.onError;
    this.cancel();
    onError(new Error(message));
  }

  cancel() {
    const state = this.active;
    if (!state) return;
    this.active = null;
    this.clearTimer(state.timer);
    if (!state.audio) return;
    for (const [type, handler] of Object.entries(state.handlers || {})) {
      state.audio.removeEventListener(type, handler);
    }
    try { state.audio.pause(); } catch { /* invalidated before cleanup */ }
    try { state.track.detach(state.audio); } catch { /* best effort */ }
    try { state.audio.srcObject = null; state.audio.remove?.(); } catch { /* best effort */ }
  }

  dropPending(sid) {
    const entry = this.pending.get(sid);
    if (!entry) return;
    this.pending.delete(sid);
    this.clearTimer(entry.timer);
  }

  reset() {
    this.cancel();
    for (const sid of this.pending.keys()) this.dropPending(sid);
  }

  close() { this.closed = true; this.reset(); }
}
