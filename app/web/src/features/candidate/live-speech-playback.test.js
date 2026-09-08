import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveSpeechPlayback } from "./live-speech-playback.js";

const performanceBinding = (id = "one") => ({
  performance_id: `performance_${id}`,
  live_audio: { output_id: `output_${id}`, publisher_identity: "approved-publisher",
    track_sid: `track_${id}`, track_name: `speech_${id}`, sample_rate_hz: 24_000, channels: 1 },
});

class Audio {
  currentTime = 0;
  paused = false;
  handlers = new Map();
  play = vi.fn(() => Promise.resolve());
  pause = vi.fn(() => this.emit("pause"));
  remove = vi.fn();
  addEventListener(type, fn) { this.handlers.set(type, fn); }
  removeEventListener(type, fn) { if (this.handlers.get(type) === fn) this.handlers.delete(type); }
  emit(type) { this.handlers.get(type)?.(); }
}

function harness(id = "one") {
  const audio = new Audio();
  const player = new LiveSpeechPlayback({ audioFactory: () => audio, now: () => Date.now() });
  const performance = performanceBinding(id);
  const track = { sid: performance.live_audio.track_sid, kind: "audio",
    attach: vi.fn(), detach: vi.fn() };
  const publication = { trackSid: track.sid, trackName: performance.live_audio.track_name };
  const participant = { identity: performance.live_audio.publisher_identity };
  const callbacks = { onReady: vi.fn(), onPlaying: vi.fn(), onFinished: vi.fn(), onError: vi.fn() };
  const subscribe = () => player.trackSubscribed(track, publication, participant);
  const eof = () => player.producerFinished({ performance_id: performance.performance_id,
    output_id: performance.live_audio.output_id, sample_rate_hz: 24_000, total_samples: 24_000 });
  return { player, audio, track, publication, participant, callbacks, performance, subscribe, eof };
}

afterEach(() => vi.useRealTimers());

describe("approved LiveKit playback", () => {
  it.each([true, false])("binds either ordering and sends ready before play resolves (track first %s)", async (trackFirst) => {
    vi.useFakeTimers();
    const h = harness();
    let resolvePlay;
    h.audio.play.mockReturnValue(new Promise((resolve) => { resolvePlay = resolve; }));
    if (trackFirst) h.subscribe();
    h.player.begin(h.performance, h.callbacks);
    if (!trackFirst) h.subscribe();
    expect(h.track.attach).toHaveBeenCalledOnce();
    expect(h.audio.play).toHaveBeenCalledOnce();
    expect(h.callbacks.onReady).toHaveBeenCalledOnce();
    expect(h.callbacks.onPlaying).not.toHaveBeenCalled();
    h.audio.emit("play");
    expect(h.callbacks.onPlaying).not.toHaveBeenCalled();
    h.audio.emit("playing");
    h.audio.emit("playing");
    expect(h.callbacks.onPlaying).toHaveBeenCalledOnce();
    resolvePlay();
    h.player.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["sid", "identity", "name"])("never attaches a mismatched %s", (field) => {
    vi.useFakeTimers();
    const h = harness();
    h.player.begin(h.performance, h.callbacks);
    if (field === "sid") { h.track.sid = "other"; h.publication.trackSid = "other"; }
    if (field === "identity") h.participant.identity = "unapproved";
    if (field === "name") h.publication.trackName = "other";
    h.subscribe();
    expect(h.track.attach).not.toHaveBeenCalled();
    expect(h.callbacks.onReady).not.toHaveBeenCalled();
    h.player.close();
  });

  it("keeps at most four unknown tracks for ten seconds and never plays them", async () => {
    vi.useFakeTimers();
    const h = harness();
    for (let i = 0; i < 6; i += 1) {
      h.player.trackSubscribed({ ...h.track, sid: `unknown${i}` }, { trackSid: `unknown${i}` }, h.participant);
    }
    expect(h.player.pending.size).toBe(4);
    expect(vi.getTimerCount()).toBe(4);
    expect(h.track.attach).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.player.pending.size).toBe(0);
    h.player.close();
  });

  it("does not treat producer EOF or wall time as media drainage", async () => {
    vi.useFakeTimers();
    const h = harness();
    h.subscribe();
    h.player.begin(h.performance, h.callbacks);
    h.eof();
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.audio.currentTime = 35;
    h.audio.emit("playing");
    await vi.advanceTimersByTimeAsync(1_100);
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.audio.currentTime = 35.8;
    await vi.advanceTimersByTimeAsync(50);
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.audio.currentTime = 36;
    await vi.advanceTimersByTimeAsync(50);
    expect(h.callbacks.onFinished).toHaveBeenCalledOnce();
    expect(h.track.detach).toHaveBeenCalledWith(h.audio);
    h.eof();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(h.callbacks.onFinished).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores stale EOF and rejects conflicting EOF positions", () => {
    vi.useFakeTimers();
    const h = harness();
    h.player.begin(h.performance, h.callbacks);
    h.player.producerFinished({ performance_id: "old", output_id: "old" });
    expect(h.callbacks.onError).not.toHaveBeenCalled();
    h.eof();
    h.player.producerFinished({ performance_id: h.performance.performance_id,
      output_id: h.performance.live_audio.output_id, sample_rate_hz: 24_000, total_samples: 48_000 });
    expect(h.callbacks.onError).toHaveBeenCalledOnce();
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.player.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["NaN", "backwards", "waiting", "stuck", "early_end"])("fails explicitly instead of fabricating a stop for an unsafe %s clock", async (mode) => {
    vi.useFakeTimers();
    const h = harness();
    h.subscribe();
    h.player.begin(h.performance, h.callbacks);
    h.audio.currentTime = 30;
    h.audio.emit("playing");
    h.eof();
    if (mode === "NaN") h.audio.currentTime = NaN;
    if (mode === "backwards") h.audio.currentTime = 29;
    if (mode === "waiting") {
      h.audio.emit("waiting");
      h.audio.currentTime = 31.1;
      await vi.advanceTimersByTimeAsync(100);
      expect(h.callbacks.onFinished).not.toHaveBeenCalled();
      h.audio.emit("playing");
    }
    if (mode === "early_end") h.audio.emit("ended");
    await vi.advanceTimersByTimeAsync(mode === "stuck" ? 10_050 : 50);
    expect(h.callbacks.onError).toHaveBeenCalledOnce();
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.player.close();
  });

  it("invalidates before detaching; late callbacks/rejection/track cannot revive an output", async () => {
    vi.useFakeTimers();
    const h = harness();
    let rejectPlay;
    h.audio.play.mockReturnValue(new Promise((_, reject) => { rejectPlay = reject; }));
    h.subscribe();
    h.player.begin(h.performance, h.callbacks);
    const stale = [...h.audio.handlers.values()];
    h.player.cancel();
    for (const callback of stale) callback();
    rejectPlay(new Error("cancelled"));
    await Promise.resolve();
    h.subscribe();
    expect(h.player.begin(h.performance, h.callbacks)).toBeNull();
    expect(h.track.attach).toHaveBeenCalledOnce();
    expect(h.callbacks.onError).not.toHaveBeenCalled();
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.player.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cannot acknowledge a disappeared track and cleans up once", () => {
    vi.useFakeTimers();
    const h = harness();
    h.subscribe();
    h.player.begin(h.performance, h.callbacks);
    h.player.trackUnsubscribed(h.track);
    h.player.trackUnsubscribed(h.track);
    expect(h.callbacks.onError).toHaveBeenCalledOnce();
    expect(h.track.detach).toHaveBeenCalledOnce();
    expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.player.close();
  });

  it("isolates late first-track callbacks and EOF from a replacement output", () => {
    vi.useFakeTimers();
    const first = harness();
    first.subscribe();
    first.player.begin(first.performance, first.callbacks);
    first.audio.emit("playing");
    const oldCallbacks = [...first.audio.handlers.values()];
    const second = harness("two");
    first.player.audioFactory = () => second.audio;
    first.player.trackSubscribed(second.track, second.publication, second.participant);
    first.player.begin(second.performance, second.callbacks);
    second.audio.emit("playing");
    for (const callback of oldCallbacks) callback();
    first.eof();
    expect(first.track.detach).toHaveBeenCalledOnce();
    expect(second.track.detach).not.toHaveBeenCalled();
    expect(second.callbacks.onPlaying).toHaveBeenCalledOnce();
    expect(first.callbacks.onError).not.toHaveBeenCalled();
    expect(second.callbacks.onError).not.toHaveBeenCalled();
    expect(second.callbacks.onFinished).not.toHaveBeenCalled();
    first.player.close();
    second.player.close();
    expect(vi.getTimerCount()).toBe(0);
  });
});
