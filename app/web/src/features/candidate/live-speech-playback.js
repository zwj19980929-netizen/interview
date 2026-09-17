import { createApprovedPcmPlayer } from "./approved-pcm-player.js";

/** Exact approved publisher binding plus a counted, bounded PCM consumer.
 * RTC stays subscribed for recording but never attaches to another audio sink.
 * Underflow produces silence without advancing the source-sample clock.
 */
export class LiveSpeechPlayback {
  constructor({ pcmPlayerFactory = createApprovedPcmPlayer, now = () => performance.now(),
    setTimer = (fn, ms) => window.setTimeout(fn, ms), clearTimer = (id) => window.clearTimeout(id),
    timeoutMs = 10_000, interruptionGraceMs = 350 } = {}) {
    Object.assign(this, { pcmPlayerFactory, now, setTimer, clearTimer, timeoutMs, interruptionGraceMs });
    this.pending = new Map(); this.seen = new Set(); this.active = null; this.closed = false;
  }
  hasSeen(id) { return this.seen.has(id); }
  trackSubscribed(track, publication, participant) {
    if (this.closed || track?.kind !== "audio") return;
    const sid = publication?.trackSid || track.sid;
    if (!sid || this.pending.has(sid) || this.active?.track?.sid === sid) return;
    const entry = { track, sid, name: publication?.trackName, identity: participant?.identity, timer: null };
    if (this.matches(this.active, entry)) { this.attach(this.active, entry); return; }
    while (this.pending.size >= 4) this.dropPending(this.pending.keys().next().value);
    entry.timer = this.setTimer(() => this.dropPending(sid), 10_000);
    this.pending.set(sid, entry);
  }
  trackUnsubscribed(track) {
    this.dropPending(track?.sid);
    const state = this.active;
    if (!state || state.track !== track || state.detached) return;
    // Media unpublication and the authoritative interrupt use separate
    // transports. Silence immediately, then allow the bounded control race to
    // resolve. A lost track never counts as a completed utterance.
    state.detached = true; state.playing = false;
    this.clearTimer(state.timer); state.abort.abort(); state.player?.close(); state.player = null;
    state.timer = this.setTimer(() => this.fail(state, "面试官音轨在播放完成前断开", "AUDIO_TRACK_LOST"), this.interruptionGraceMs);
  }
  begin(performance, callbacks) {
    if (this.closed || this.hasSeen(performance.performance_id)) return null;
    if (this.seen.size >= 512) throw new Error("面试官播放身份数量超限");
    this.cancel(); this.seen.add(performance.performance_id);
    const state = { performance, callbacks, track: null, player: null, opening: false,
      ready: false, detached: false, received: 0, consumed: 0, sequence: 0, final: null, finalSent: false,
      progress: null, playing: false, firstPlayed: false, timer: null, abort: new AbortController(), lastProgressAt: this.now() };
    this.active = state;
    const entry = this.pending.get(performance.live_audio.track_sid);
    if (this.matches(state, entry)) { this.dropPending(entry.sid); this.attach(state, entry); }
    this.poll(state);
    return Object.freeze({ positionMs: () => state.consumed / 24,
      isPlaying: () => this.active === state && state.playing,
      cancel: () => { if (this.active === state) this.cancel(); } });
  }
  matches(state, entry) {
    const binding = state?.performance.live_audio;
    return Boolean(binding && entry && binding.track_sid === entry.sid
      && binding.publisher_identity === entry.identity && binding.track_name === entry.name);
  }
  async attach(state, entry) {
    if (this.active !== state || state.opening || !this.matches(state, entry)) return;
    state.opening = true; state.track = entry.track;
    try {
      const player = await this.pcmPlayerFactory({
        signal: state.abort.signal,
        onProgress: (value) => this.progress(state, value),
        onError: () => { if (!state.detached) this.fail(state, "面试官流式语音播放失败", "AUDIO_PLAYBACK_FAILED"); },
      });
      if (this.active !== state || state.detached) { player.close(); return; }
      state.player = player; state.ready = true; state.lastProgressAt = this.now();
      state.callbacks.onReady();
    } catch { if (!state.detached) this.fail(state, "浏览器无法开始流式语音播放，请检查音频权限", "AUDIO_OUTPUT_UNAVAILABLE"); }
  }
  dataReceived(payload, participant, topic) {
    const state = this.active;
    let role;
    try { role = JSON.parse(participant?.metadata || "{}").role; } catch { return; }
    if (!state || state.detached || role !== "approved_expression" || participant?.identity !== state.performance.live_audio.publisher_identity
      || topic !== `interviewer.approved-pcm.${state.performance.live_audio.output_id}`) return;
    if (!state.ready || !(payload instanceof Uint8Array) || payload.length < 18 || payload.length > 976
      || payload.length % 2 || state.finalSent) { this.fail(state, "面试官音频分片无效", "AUDIO_STREAM_INVALID"); return; }
    const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
    const count = (payload.length - 16) / 2;
    if (view.getUint32(0) !== 0x49415331 || view.getUint32(4) !== state.sequence + 1
      || view.getUint32(8) !== state.received || view.getUint32(12) !== 24_000
      || state.received + count > 24_000 * 300 || state.received + count - state.consumed > 48_000
      || (state.final !== null && state.received + count > state.final)) {
      this.fail(state, "面试官音频分片缺失、乱序或超过缓冲限制", "AUDIO_STREAM_INVALID"); return;
    }
    state.sequence += 1; state.received += count;
    try { state.player.append(payload.subarray(16)); this.finishInput(state); }
    catch { this.fail(state, "面试官音频播放通道已关闭", "AUDIO_PLAYBACK_FAILED"); }
  }
  producerFinished(payload) {
    const state = this.active;
    if (!state || state.detached || payload.performance_id !== state.performance.performance_id
      || payload.output_id !== state.performance.live_audio.output_id) return;
    if (!Number.isSafeInteger(payload.total_samples) || payload.total_samples <= 0
      || payload.sample_rate_hz !== 24_000 || payload.total_samples > 24_000 * 300
      || payload.total_samples < state.received || (state.final !== null && state.final !== payload.total_samples)) {
      this.fail(state, "面试官流式语音结束位置无效", "AUDIO_STREAM_INVALID"); return;
    }
    state.final = payload.total_samples; this.finishInput(state);
  }
  finishInput(state) {
    if (state.ready && !state.finalSent && state.final !== null && state.received === state.final) {
      state.finalSent = true;
      try { state.player.finish(state.final); }
      catch { this.fail(state, "面试官音频结束位置无法确认", "AUDIO_STREAM_INVALID"); }
    }
  }
  progress(state, value) {
    if (this.active !== state || state.detached) return;
    if (!Number.isSafeInteger(value.consumed) || value.consumed < state.consumed || value.consumed > state.received
      || !Number.isFinite(value.renderedUntil) || value.renderedUntil < 0) {
      this.fail(state, "面试官音频消费位置无效", "AUDIO_STREAM_INVALID"); return;
    }
    if (value.consumed > state.consumed) state.lastProgressAt = this.now();
    state.consumed = value.consumed; state.progress = value; state.playing = value.producing && !value.buffering;
    state.callbacks.onQuality?.({ buffering: Boolean(value.buffering), bufferedSamples: value.bufferedSamples,
      underflowCount: value.underflowCount, underflowSamples: value.underflowSamples });
    this.checkDrained(state);
  }
  checkDrained(state) {
    const progress = state.progress;
    if (this.active !== state || state.detached || !progress || !state.player) return false;
    let outputTime;
    try { outputTime = state.player.outputTime(); }
    catch { this.fail(state, "浏览器音频输出时钟不可用", "AUDIO_OUTPUT_UNAVAILABLE"); return false; }
    if (!Number.isFinite(outputTime)) return false;
    if (!state.firstPlayed && progress.consumed > 0 && outputTime > progress.firstRenderStart) {
      state.firstPlayed = true; state.callbacks.onPlaying();
    }
    if (this.active !== state) return false;
    if (!state.firstPlayed || !state.finalSent || !progress.drained || state.consumed !== state.final
      || outputTime < progress.renderedUntil) return false;
    const onFinished = state.callbacks.onFinished;
    this.cancel(); onFinished(); return true;
  }
  poll(state) {
    if (this.active !== state || state.detached || this.checkDrained(state)) return;
    if (this.active !== state) return;
    if (this.now() - state.lastProgressAt >= this.timeoutMs) { this.fail(state, "面试官语音暂时没有播放进度，请确认会话状态", "AUDIO_PLAYBACK_TIMEOUT"); return; }
    if (this.now() - state.lastProgressAt >= 100) state.playing = false;
    state.timer = this.setTimer(() => this.poll(state), 25);
  }
  fail(state, message, code) {
    if (this.active !== state) return;
    const onError = state.callbacks.onError; this.cancel();
    const error = new Error(message); error.candidateProblemCode = code;
    onError(error);
  }
  cancel() {
    const state = this.active;
    if (!state) return;
    this.active = null; this.clearTimer(state.timer); state.abort.abort(); state.player?.close();
  }
  dropPending(sid) {
    const entry = this.pending.get(sid); if (!entry) return;
    this.pending.delete(sid); this.clearTimer(entry.timer);
  }
  reset() { this.cancel(); for (const sid of this.pending.keys()) this.dropPending(sid); }
  close() { this.closed = true; this.reset(); }
}
