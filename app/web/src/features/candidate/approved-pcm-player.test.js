import { describe, expect, it, vi } from "vitest";
import vm from "node:vm";
import { execFileSync } from "node:child_process";
import { fileURLToPath, URL as NodeURL } from "node:url";
import { APPROVED_PCM_WORKLET, createApprovedPcmPlayer } from "./approved-pcm-player.js";

function worklet(source = APPROVED_PCM_WORKLET) {
  let Processor;
  const messages = [];
  const scope = { Float32Array, sampleRate: 24_000, currentFrame: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (value) => messages.push(value) }; } },
    registerProcessor: (_name, implementation) => { Processor = implementation; },
  };
  vm.runInNewContext(source, scope);
  const processor = new Processor();
  return { processor, scope, messages,
    send: (data) => processor.port.onmessage({ data }),
    render: () => { const output = new Float32Array(128); const alive = processor.process([], [[output]]); scope.currentFrame += 128; return { output, alive }; },
  };
}

function pacedPlayback({ interval = 20, jitter = () => 0, gapAt = -1, gapMs = 0 } = {}) {
  const h = worklet();
  const arrivals = [];
  for (let index = 0; index < 200; index += 1) {
    arrivals.push(Math.max(arrivals.at(-1) ?? 0,
      index * interval + jitter(index) + (index >= gapAt && gapAt >= 0 ? gapMs : 0)));
  }
  let sent = 0, rendered = 0, insertedSilence = 0, gaps = 0, inGap = false, firstSoundMs = null;
  for (let quantum = 0; quantum < 2000; quantum += 1) {
    while (sent < arrivals.length && arrivals[sent] <= h.scope.currentFrame / 24) {
      h.send({ type: "pcm", samples: new Float32Array(480).fill(0.25) });
      sent += 1;
      if (sent === arrivals.length) h.send({ type: "final", total: 96_000 });
    }
    const frame = h.scope.currentFrame;
    const result = h.render();
    for (const sample of result.output) {
      if (sample) {
        firstSoundMs ??= frame / 24;
        rendered += 1; inGap = false;
      } else if (rendered > 0 && rendered < 96_000) {
        insertedSilence += 1;
        if (!inGap) { inGap = true; gaps += 1; }
      }
    }
    if (!result.alive) return { firstSoundMs, rendered, insertedSilence, gaps, messages: h.messages };
  }
  throw new Error("Synthetic playout did not drain within its bounded budget");
}

describe("actual PCM worklet source", () => {
  it("absorbs ordered packet jitter without inserting silence into a continuous sentence", () => {
    const result = pacedPlayback({ jitter: (index) => (index * 37) % 81 });
    expect(result.rendered).toBe(96_000);
    expect(result.firstSoundMs).toBeGreaterThanOrEqual(180);
    expect(result.firstSoundMs).toBeLessThan(300);
    expect(result.gaps).toBe(0);
    expect(result.insertedSilence).toBe(0);
    expect(result.messages.at(-1)).toMatchObject({ underflowCount: 0, underflowSamples: 0, drained: true });
  });

  it("absorbs one millisecond of per-frame overhead instead of chopping every packet", () => {
    const result = pacedPlayback({ interval: 21 });
    expect(result.rendered).toBe(96_000);
    expect(result.gaps).toBe(0);
    expect(result.insertedSilence).toBe(0);
  });

  it("re-buffers a slow source into bounded uninterrupted stretches and reports actual missing samples", () => {
    const result = pacedPlayback({ interval: 24 });
    expect(result.rendered).toBe(96_000);
    expect(result.gaps).toBeGreaterThan(0);
    expect(result.gaps).toBeLessThanOrEqual(5);
    expect(result.messages.at(-1)).toMatchObject({ underflowCount: result.gaps,
      underflowSamples: result.insertedSilence, consumed: 96_000, drained: true });
  });

  it("limits worklet-to-main-thread updates while reporting first sound and drain immediately", () => {
    const result = pacedPlayback();
    expect(result.messages.length).toBeLessThan(150);
    const firstPlaying = result.messages.find(value => value.producing);
    expect(firstPlaying.consumed).toBe(128);
    expect(result.messages.at(-1)).toMatchObject({ consumed: 96_000, drained: true });
  });
  it("constructs and plays the actual worklet after production minification", () => {
    // Build outside the DOM emulation realm, just like Vite's build process.
    const bundled = execFileSync(process.execPath, ["--input-type=module", "--eval", `
      import { build } from "esbuild";
      const result = await build({ entryPoints: [process.argv[1]], bundle: true,
        minify: true, write: false, format: "cjs", platform: "browser" });
      process.stdout.write(result.outputFiles[0].text);
    `, fileURLToPath(new NodeURL("./approved-pcm-player.js", import.meta.url))], {
      cwd: fileURLToPath(new NodeURL("../../../", import.meta.url)), encoding: "utf8",
    });
    const moduleScope = { module: { exports: {} }, exports: {} };
    vm.runInNewContext(bundled, moduleScope);
    const h = worklet(moduleScope.module.exports.APPROVED_PCM_WORKLET);
    h.send({ type: "pcm", samples: new Float32Array(32).fill(0.5) });
    h.send({ type: "final", total: 32 });
    const result = h.render();
    expect([...result.output.slice(0, 32)]).toEqual(Array(32).fill(0.5));
    expect(result.alive).toBe(false);
    expect(h.messages.at(-1)).toMatchObject({ consumed: 32, received: 32, drained: true });
  });

  it("buffers initial packets, freezes source time in a three-second gap and drains a short final tail", () => {
    const h = worklet(); h.send({ type: "pcm", samples: new Float32Array(240).fill(0.25) });
    expect(h.render().output.every(value => value === 0)).toBe(true);
    h.send({ type: "pcm", samples: new Float32Array(5520).fill(0.25) });
    expect(h.render().output[0]).toBe(0.25);
    for (let i = 0; i < 44; i += 1) h.render();
    for (let i = 0; i < 563; i += 1) expect(h.render().output.every(value => value === 0)).toBe(true);
    expect(h.messages.at(-1).consumed).toBe(5760);
    h.send({ type: "pcm", samples: new Float32Array(139).fill(-0.25) });
    h.send({ type: "final", total: 5899 });
    const resumed = h.scope.currentFrame;
    expect(h.render().alive).toBe(true); const last = h.render();
    expect(last.alive).toBe(false); expect([...last.output.slice(0, 11)]).toEqual(Array(11).fill(-0.25));
    expect(h.messages.at(-1)).toMatchObject({ consumed: 5899, received: 5899, drained: true,
      renderedUntil: (resumed + 139) / 24000, firstRenderStart: 128 / 24000, underflowCount: 1 });
  });

  it("does not count normal trailing silence while awaiting producer EOF as an audio dropout", () => {
    const h = worklet(); h.send({ type: "pcm", samples: new Float32Array(5760).fill(0.25) });
    for (let i = 0; i < 45 + 60; i += 1) h.render();
    h.send({ type: "final", total: 5760 });
    expect(h.render().alive).toBe(false);
    expect(h.messages.at(-1)).toMatchObject({ consumed: 5760, underflowCount: 0,
      underflowSamples: 0, drained: true, renderedUntil: 0.24 });
  });
  it("discards all queued samples on cancellation and rejects a false final", () => {
    const h = worklet(); h.send({ type: "pcm", samples: new Float32Array(240).fill(1) });
    h.send({ type: "close" }); expect(h.render()).toMatchObject({ alive: false });
    expect(h.messages).toEqual([]);
    const invalid = worklet(); invalid.send({ type: "final", total: 123 });
    expect(invalid.messages).toEqual([{ type: "error" }]); expect(invalid.render().alive).toBe(false);
  });
});

describe("PCM audio context lifecycle", () => {
  it("closes a context immediately if cancelled during module loading, without creating a node", async () => {
    let release; let context;
    const close = vi.fn(async () => {}); const controller = new AbortController();
    class Context {
      sampleRate = 24000; state = "running"; close = close;
      getOutputTimestamp = () => ({ contextTime: 0 });
      audioWorklet = { addModule: () => new Promise(resolve => { release = resolve; }) };
      constructor() { context = this; }
    }
    const oldCreate = URL.createObjectURL, oldRevoke = URL.revokeObjectURL;
    const create = URL.createObjectURL = vi.fn(() => "blob:test");
    const revoke = URL.revokeObjectURL = vi.fn();
    try {
      const pending = createApprovedPcmPlayer({ AudioContextClass: Context, AudioWorkletNodeClass: class {}, signal: controller.signal });
      controller.abort(); expect(context.close).toHaveBeenCalledOnce(); release();
      await expect(pending).rejects.toThrow("播放已停止"); expect(close).toHaveBeenCalledOnce(); expect(revoke).toHaveBeenCalledWith("blob:test");
    } finally { URL.createObjectURL = oldCreate; URL.revokeObjectURL = oldRevoke; }
  });
});
