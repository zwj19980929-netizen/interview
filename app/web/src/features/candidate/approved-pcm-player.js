/** Count source samples rendered by Web Audio, never MediaStream wall time. */
export class ApprovedPcmQueue {
  constructor(capacity = 48_000, bufferSamples = 5_760) {
    this.samples = new Float32Array(capacity);
    this.read = 0; this.size = 0; this.received = 0; this.consumed = 0;
    this.final = null;
    this.bufferSamples = Math.min(bufferSamples, capacity);
    this.buffering = true;
    this.waitingSamples = 0;
    this.underflowCount = 0; this.underflowSamples = 0;
  }
  append(samples) {
    if (!(samples instanceof Float32Array) || !samples.length
      || this.final !== null || this.size + samples.length > this.samples.length) throw new Error("PCM queue overflow or closed");
    for (let i = 0; i < samples.length; i += 1) this.samples[(this.read + this.size + i) % this.samples.length] = samples[i];
    this.size += samples.length; this.received += samples.length;
  }
  finish(total) {
    if (!Number.isSafeInteger(total) || total <= 0 || total !== this.received
      || (this.final !== null && this.final !== total)) throw new Error("PCM final mismatch");
    this.final = total;
  }
  render(output) {
    output.fill(0);
    // A data channel has no native audio jitter buffer. Build a small reserve
    // before starting, and rebuild it after starvation instead of emitting a
    // fragment of speech and silence for every arriving 20 ms packet.
    if (this.buffering && (this.size >= this.bufferSamples || this.final !== null)) this.buffering = false;
    if (this.buffering) {
      if (this.consumed > 0) this.waitingSamples += output.length;
      return this.progress(0);
    }
    const count = Math.min(output.length, this.size);
    if (count > 0 && this.waitingSamples > 0) {
      this.underflowCount += 1;
      this.underflowSamples += this.waitingSamples;
      this.waitingSamples = 0;
    }
    for (let i = 0; i < count; i += 1) output[i] = this.samples[(this.read + i) % this.samples.length];
    this.read = (this.read + count) % this.samples.length;
    this.size -= count; this.consumed += count;
    if (count < output.length && this.final === null) {
      this.buffering = true;
      if (this.consumed > 0) this.waitingSamples += output.length - count;
    }
    return this.progress(count);
  }
  progress(count) {
    return { count, consumed: this.consumed,
      drained: this.final !== null && this.consumed === this.final,
      buffering: this.buffering, bufferedSamples: this.size,
      underflowCount: this.underflowCount, underflowSamples: this.underflowSamples };
  }
}

// Bundlers may rename or anonymize the class. Bind the serialized expression
// explicitly so the worklet has the same stable name in development and builds.
export const APPROVED_PCM_WORKLET = `const ApprovedPcmQueue = ${ApprovedPcmQueue.toString()};
class ApprovedPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super(); this.queue = new ApprovedPcmQueue(); this.closed = false;
    this.lastRenderEnd = 0; this.firstRenderStart = null;
    this.lastReportFrame = -Infinity; this.lastBuffering = null;
    this.port.onmessage = ({data}) => {
      if (this.closed) return;
      try {
        if (data.type === "pcm") this.queue.append(data.samples);
        else if (data.type === "final") this.queue.finish(data.total);
        else if (data.type === "close") { this.closed = true; this.queue = null; }
      } catch { this.closed = true; this.port.postMessage({type:"error"}); }
    };
  }
  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output || this.closed) { output?.fill(0); return !this.closed; }
    const result = this.queue.render(output);
    const firstRender = result.count > 0 && this.firstRenderStart === null;
    if (result.count) {
      if (firstRender) this.firstRenderStart = currentFrame / sampleRate;
      this.lastRenderEnd = (currentFrame + result.count) / sampleRate;
    }
    if (firstRender || result.drained || this.lastBuffering !== result.buffering
      || currentFrame - this.lastReportFrame >= sampleRate * 0.032) {
      this.port.postMessage({type:"progress", consumed:result.consumed, received:this.queue.received,
        renderedUntil:this.lastRenderEnd, firstRenderStart:this.firstRenderStart,
        producing:result.count > 0, drained:result.drained,
        buffering:result.buffering, bufferedSamples:result.bufferedSamples,
        underflowCount:result.underflowCount, underflowSamples:result.underflowSamples});
      this.lastReportFrame = currentFrame; this.lastBuffering = result.buffering;
    }
    if (result.drained) this.closed = true;
    return !this.closed;
  }
}
registerProcessor("approved-pcm-player", ApprovedPcmProcessor);`;

export async function createApprovedPcmPlayer({ onProgress, onError, signal,
  AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext,
  AudioWorkletNodeClass = globalThis.AudioWorkletNode,
} = {}) {
  if (!AudioContextClass || !AudioWorkletNodeClass) throw new Error("当前浏览器不支持流式语音播放");
  const context = new AudioContextClass({ sampleRate: 24_000, latencyHint: "interactive" });
  let node, closed = false;
  const close = () => {
    if (closed) return;
    closed = true; signal?.removeEventListener("abort", close);
    if (node) { node.port.onmessage = null; node.port.postMessage({ type: "close" }); node.disconnect(); }
    void context.close().catch(() => {});
  };
  signal?.addEventListener("abort", close, { once: true });
  try {
    if (signal?.aborted) throw new Error("播放已停止");
    if (context.sampleRate !== 24_000 || !context.audioWorklet || !context.getOutputTimestamp) {
      throw new Error("浏览器未提供可靠的音频消费时钟");
    }
    const moduleUrl = URL.createObjectURL(new Blob([APPROVED_PCM_WORKLET], { type: "text/javascript" }));
    try { await context.audioWorklet.addModule(moduleUrl); }
    finally { URL.revokeObjectURL(moduleUrl); }
    if (closed) throw new Error("播放已停止");
    node = new AudioWorkletNodeClass(context, "approved-pcm-player", {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
    });
    node.port.onmessage = ({ data }) => {
      if (closed) return;
      if (data?.type === "error") onError(new Error("流式语音缓冲区或结束位置无效"));
      else if (data?.type === "progress") onProgress(data);
    };
    node.connect(context.destination);
    await context.resume();
    if (context.state !== "running") throw new Error("请允许浏览器播放面试官语音");
    return {
      append(pcm) {
        if (closed) return;
        const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
        const samples = new Float32Array(pcm.byteLength / 2);
        for (let i = 0; i < samples.length; i += 1) samples[i] = view.getInt16(i * 2, true) / 32768;
        node.port.postMessage({ type: "pcm", samples }, [samples.buffer]);
      },
      finish(total) { if (!closed) node.port.postMessage({ type: "final", total }); },
      outputTime() { return context.state === "running" ? context.getOutputTimestamp().contextTime : null; },
      close,
    };
  } catch (error) { close(); throw error; }
}
