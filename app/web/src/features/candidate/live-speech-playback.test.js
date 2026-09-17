import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveSpeechPlayback } from "./live-speech-playback.js";
import { ApprovedPcmQueue } from "./approved-pcm-player.js";

export function pcmPacket(sequence, start, count = 480) {
  const data = new Uint8Array(16 + count * 2); const view = new DataView(data.buffer);
  view.setUint32(0, 0x49415331); view.setUint32(4, sequence); view.setUint32(8, start); view.setUint32(12, 24_000);
  return data;
}
function harness(id = "one", factory) {
  const performance = { performance_id: `performance_${id}`, live_audio: {
    output_id: `output_${id}`, publisher_identity: "expression:server", track_sid: `track_${id}`,
    track_name: `speech_${id}`, sample_rate_hz: 24_000, channels: 1 } };
  const participant = { identity: performance.live_audio.publisher_identity, metadata: '{"role":"approved_expression"}' };
  const track = { sid: performance.live_audio.track_sid, kind: "audio", attach: vi.fn(), detach: vi.fn() };
  const callbacks = { onReady: vi.fn(), onPlaying: vi.fn(), onFinished: vi.fn(), onError: vi.fn() };
  const sink = { append: vi.fn(), finish: vi.fn(), close: vi.fn(), outputTime: () => sink.time, time: 0 };
  let consumer;
  const player = new LiveSpeechPlayback({ now: () => Date.now(), pcmPlayerFactory: factory || (async (options) => { consumer = options; return sink; }) });
  const subscribe = () => player.trackSubscribed(track, { trackSid: track.sid, trackName: performance.live_audio.track_name }, participant);
  const data = (seq = 1, start = 0, count = 480, who = participant) => player.dataReceived(pcmPacket(seq, start, count), who, `interviewer.approved-pcm.${performance.live_audio.output_id}`);
  const eof = (total = 480) => player.producerFinished({ performance_id: performance.performance_id, output_id: performance.live_audio.output_id,
    total_samples: total, sample_rate_hz: 24_000 });
  const progress = (consumed, drained = false, renderedUntil = 1) => consumer.onProgress({ consumed, drained, renderedUntil, firstRenderStart: 0.5, producing: consumed > 0 });
  return { player, sink, track, participant, callbacks, performance, subscribe, data, eof, progress };
}
afterEach(() => vi.useRealTimers());
describe("counted approved PCM playback", () => {
  it.each([true, false])("waits for exact track and worklet before ready, never double-plays RTC (%s)", async (trackFirst) => {
    vi.useFakeTimers(); const h = harness();
    if (trackFirst) h.subscribe(); h.player.begin(h.performance, h.callbacks); if (!trackFirst) h.subscribe();
    expect(h.callbacks.onReady).not.toHaveBeenCalled(); await Promise.resolve();
    expect(h.callbacks.onReady).toHaveBeenCalledOnce(); expect(h.track.attach).not.toHaveBeenCalled();
    h.data(); expect(h.sink.append).toHaveBeenCalledOnce(); expect(h.callbacks.onPlaying).not.toHaveBeenCalled();
    h.player.close(); expect(vi.getTimerCount()).toBe(0);
  });
  it("does not use elapsed media time, EOF, receipt or render-ahead as device playout", async () => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); const playback = h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    h.eof(960); h.data(); h.progress(480); h.sink.time = 0.75;
    await vi.advanceTimersByTimeAsync(3000);
    expect(playback.positionMs()).toBe(20); expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.data(2, 480); expect(h.sink.finish).toHaveBeenCalledWith(960);
    h.progress(960, true, 4); expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    h.sink.time = 4; await vi.advanceTimersByTimeAsync(25);
    expect(h.callbacks.onFinished).toHaveBeenCalledOnce(); expect(h.sink.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });
  it.each(["wrong sequence", "wrong offset", "format", "oversize", "eof mismatch"])("fails closed on %s without fallback or drainage", async (mode) => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    if (mode === "wrong sequence") h.data(2);
    if (mode === "wrong offset") h.data(1, 1);
    if (mode === "oversize") h.data(1, 0, 481);
    if (mode === "format") { const packet = pcmPacket(1, 0); packet[0] = 0; h.player.dataReceived(packet, h.participant, "interviewer.approved-pcm.output_one"); }
    if (mode === "eof mismatch") { h.data(); h.eof(1); }
    expect(h.callbacks.onError).toHaveBeenCalledOnce(); expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    expect(h.sink.close).toHaveBeenCalledOnce(); h.player.close();
  });
  it("ignores unapproved participants, old output data and late callbacks after replacement", async () => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    h.data(1, 0, 480, { identity: "expression:other", metadata: h.participant.metadata });
    h.data(1, 0, 480, { ...h.participant, metadata: '{"role":"candidate"}' });
    h.player.dataReceived(pcmPacket(1, 0), h.participant, "interviewer.approved-pcm.old");
    expect(h.sink.append).not.toHaveBeenCalled(); h.player.cancel(); h.data(); h.eof(); h.progress(480, true);
    expect(h.callbacks.onFinished).not.toHaveBeenCalled(); expect(h.callbacks.onError).not.toHaveBeenCalled();
    expect(h.player.begin(h.performance, h.callbacks)).toBeNull(); h.player.close();
  });
  it("bounds missing data, suspended output and the PCM queue instead of assuming completion", async () => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    h.sink.time = null; h.data(); h.eof(); h.progress(480, true);
    await vi.advanceTimersByTimeAsync(10_025);
    expect(h.callbacks.onError).toHaveBeenCalledOnce(); expect(h.callbacks.onFinished).not.toHaveBeenCalled(); h.player.close();
    const second = harness("two"); second.subscribe(); second.player.begin(second.performance, second.callbacks); await Promise.resolve();
    for (let i = 0; i < 101; i += 1) second.data(i + 1, i * 480);
    expect(second.callbacks.onError).toHaveBeenCalledOnce(); second.player.close();
  });
  it("cancels a worklet that resolves after replacement before sending ready", async () => {
    vi.useFakeTimers(); let resolve; let signal;
    const h = harness("one", (options) => { signal = options.signal; return new Promise(r => { resolve = r; }); });
    h.subscribe(); h.player.begin(h.performance, h.callbacks); h.player.cancel();
    expect(signal.aborted).toBe(true); resolve(h.sink); await Promise.resolve();
    expect(h.sink.close).toHaveBeenCalledOnce(); expect(h.callbacks.onReady).not.toHaveBeenCalled(); h.player.close();
  });
  it("rejects vanished tracks and conflicting final counts", async () => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    h.eof(); h.eof(960); expect(h.callbacks.onError).toHaveBeenCalledOnce(); h.player.close();
    const second = harness("two"); second.subscribe(); second.player.begin(second.performance, second.callbacks); await Promise.resolve();
    second.player.trackUnsubscribed(second.track);
    expect(second.callbacks.onError).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(350);
    expect(second.callbacks.onError).toHaveBeenCalledOnce();
    expect(second.callbacks.onError.mock.calls[0][0].candidateProblemCode).toBe("AUDIO_TRACK_LOST"); second.player.close();
  });

  it.each(["control first", "track first"])("accepts an authorized cancellation in either transport order: %s", async order => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    h.data(); h.progress(480);
    if (order === "control first") h.player.cancel();
    h.player.trackUnsubscribed(h.track);
    expect(h.sink.close).toHaveBeenCalledOnce();
    if (order === "track first") {
      // The detached output neither consumes late frames nor acknowledges EOF.
      h.data(2, 480); h.eof(960); h.progress(960, true);
      await vi.advanceTimersByTimeAsync(349); h.player.cancel();
    }
    await vi.advanceTimersByTimeAsync(1000);
    expect(h.sink.append).toHaveBeenCalledOnce(); expect(h.callbacks.onError).not.toHaveBeenCalled();
    expect(h.callbacks.onFinished).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0); h.player.close();
  });

  it("keeps the track-loss deadline when late PCM, EOF, progress or repeated unsubscription arrives", async () => {
    vi.useFakeTimers(); const h = harness(); h.subscribe(); h.player.begin(h.performance, h.callbacks); await Promise.resolve();
    h.data(); h.progress(480); h.player.trackUnsubscribed(h.track);
    await vi.advanceTimersByTimeAsync(300);
    h.player.trackUnsubscribed(h.track); h.data(2, 480); h.eof(960); h.progress(960, true);
    await vi.advanceTimersByTimeAsync(50);
    expect(h.callbacks.onError).toHaveBeenCalledOnce(); expect(h.callbacks.onFinished).not.toHaveBeenCalled();
    expect(h.sink.close).toHaveBeenCalledOnce(); h.player.close();
  });
});

describe("worklet source sample queue", () => {
  it("plays the first chunk before final and resumes after arbitrary underflow without phantom samples", () => {
    const q = new ApprovedPcmQueue(8, 3); const output = new Float32Array(4);
    q.append(new Float32Array([1, 2, 3])); expect(q.render(output)).toMatchObject({count:3, consumed:3, drained:false});
    expect([...output]).toEqual([1, 2, 3, 0]);
    for (let i = 0; i < 100; i += 1) expect(q.render(output)).toMatchObject({count:0, consumed:3, drained:false});
    q.append(new Float32Array([4, 5])); q.finish(5);
    expect(q.render(output)).toMatchObject({count:2, consumed:5, drained:true}); expect([...output]).toEqual([4, 5, 0, 0]);
  });
  it("preserves authored silence, rejects oversize and validates final source size", () => {
    const q = new ApprovedPcmQueue(4); q.append(new Float32Array([0, 0, 0, 0]));
    expect(() => q.append(new Float32Array([1]))).toThrow(); expect(() => q.finish(3)).toThrow();
    q.finish(4); expect(() => q.append(new Float32Array([1]))).toThrow();
    expect(q.render(new Float32Array(4))).toMatchObject({count:4, consumed:4, drained:true});
  });
});
