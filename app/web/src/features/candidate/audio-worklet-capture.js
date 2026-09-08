const WORKLET_SOURCE = `
class CandidatePcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.offset = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || !input.length) return true;
    let squareSum = 0;
    for (let index = 0; index < input.length; index += 1) {
      const value = input[index];
      squareSum += value * value;
      this.buffer[this.offset++] = value;
      if (this.offset === this.buffer.length) {
        const frame = this.buffer;
        this.port.postMessage({ type: "pcm", samples: frame, sampleRate }, [frame.buffer]);
        this.buffer = new Float32Array(2048);
        this.offset = 0;
      }
    }
    this.port.postMessage({
      type: "level",
      rms: Math.sqrt(squareSum / input.length),
      audioTime: currentTime,
      sampleCount: input.length,
      sampleRate,
    });
    return true;
  }
}
registerProcessor("candidate-pcm-processor", CandidatePcmProcessor);
`;

export async function createCandidateAudioCapture(mediaStream, {
  onPcm = () => {},
  onLevel = () => {},
  onSpeechStarted = () => {},
  onSpeechStopped = () => {},
  isAgentSpeaking = () => false,
  speechThreshold = 0.006,
  agentSpeechThreshold = 0.05,
  speechStartMs = 120,
  agentSpeechStartMs = 160,
  speechStopMs = 800,
  AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext,
  AudioWorkletNodeClass = globalThis.AudioWorkletNode,
} = {}) {
  const tracks = mediaStream?.getAudioTracks?.() || [];
  if (!tracks.length) throw new Error("麦克风音轨不可用");
  if (!AudioContextClass || !AudioWorkletNodeClass) {
    throw new Error("当前浏览器不支持 AudioWorklet，正式面试已暂停");
  }
  const context = new AudioContextClass({ latencyHint: "interactive" });
  if (!context.audioWorklet) {
    await context.close();
    throw new Error("当前浏览器未开放 AudioWorklet，正式面试已暂停");
  }
  const moduleUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "text/javascript" }));
  try {
    await context.audioWorklet.addModule(moduleUrl);
  } finally {
    URL.revokeObjectURL(moduleUrl);
  }
  await context.resume();
  const audioClockOriginMs = performance.now() - context.currentTime * 1000;
  const source = context.createMediaStreamSource(new MediaStream(tracks));
  const node = new AudioWorkletNodeClass(context, "candidate-pcm-processor", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
  });
  const silence = context.createGain();
  silence.gain.value = 0;
  const vad = createSpeechActivityDetector({
    isAgentSpeaking,
    speechThreshold,
    agentSpeechThreshold,
    speechStartMs,
    agentSpeechStartMs,
    speechStopMs,
    onSpeechStarted,
    onSpeechStopped,
  });
  let stopped = false;
  node.port.onmessage = ({ data }) => {
    if (stopped) return;
    if (data?.type === "pcm") {
      const pcm = resamplePcm16(data.samples, Number(data.sampleRate || context.sampleRate), 16_000);
      if (pcm.byteLength) onPcm(pcm.buffer);
      return;
    }
    if (data?.type !== "level") return;
    const observedAt = performance.now();
    const audioTime = Number(data.audioTime);
    const detectedAt = Number.isFinite(audioTime)
      ? audioClockOriginMs + audioTime * 1000
      : observedAt;
    const feedbackLatencyMs = Math.max(0, observedAt - detectedAt);
    const rms = Math.max(0, Number(data.rms || 0));
    onLevel(rms);
    const sampleCount = Math.max(0, Number(data.sampleCount || 0));
    const observedSampleRate = Math.max(0, Number(data.sampleRate || context.sampleRate));
    const durationMs = sampleCount && observedSampleRate
      ? sampleCount * 1000 / observedSampleRate
      : 0;
    vad.observe({ rms, durationMs, detectedAt, observedAt, feedbackLatencyMs });
  };
  source.connect(node);
  node.connect(silence);
  silence.connect(context.destination);

  return {
    get sampleRate() { return 16_000; },
    get speaking() { return vad.speaking; },
    async close() {
      if (stopped) return;
      stopped = true;
      node.port.onmessage = null;
      source.disconnect();
      node.disconnect();
      silence.disconnect();
      await context.close();
    },
  };
}

/**
 * Time-based local speech gate. AudioWorklet render quanta vary by browser and
 * sample rate, so frame counts are not a stable duration. While the Agent is
 * speaking, require a stronger and longer signal to suppress loudspeaker echo
 * without removing the bounded barge-in path.
 */
export function createSpeechActivityDetector({
  isAgentSpeaking = () => false,
  speechThreshold = 0.006,
  agentSpeechThreshold = 0.05,
  speechStartMs = 120,
  agentSpeechStartMs = 160,
  speechStopMs = 800,
  onSpeechStarted = () => {},
  onSpeechStopped = () => {},
} = {}) {
  let speaking = false;
  let aboveMs = 0;
  let belowMs = 0;

  return {
    get speaking() { return speaking; },
    observe(details = {}) {
      const rms = Math.max(0, Number(details.rms || 0));
      const durationMs = Math.max(0, Math.min(100, Number(details.durationMs || 0)));
      const agentSpeaking = !speaking && Boolean(isAgentSpeaking());
      const threshold = agentSpeaking
        ? Math.max(speechThreshold, agentSpeechThreshold)
        : speechThreshold;
      const requiredStartMs = agentSpeaking
        ? Math.min(200, Math.max(speechStartMs, agentSpeechStartMs))
        : speechStartMs;
      if (rms >= threshold) {
        aboveMs += durationMs;
        belowMs = 0;
        if (!speaking && aboveMs >= requiredStartMs) {
          speaking = true;
          aboveMs = 0;
          onSpeechStarted({ ...details, rms, agentSpeaking });
        }
        return;
      }
      aboveMs = 0;
      if (!speaking) return;
      belowMs += durationMs;
      if (belowMs >= speechStopMs) {
        speaking = false;
        belowMs = 0;
        onSpeechStopped({ ...details, rms });
      }
    },
  };
}

export function resamplePcm16(samples, sourceRate, targetRate) {
  const input = samples instanceof Float32Array ? samples : new Float32Array(samples || []);
  if (!input.length || !sourceRate || !targetRate) return new Int16Array();
  const ratio = sourceRate / targetRate;
  const length = Math.max(1, Math.floor(input.length / ratio));
  const output = new Int16Array(length);
  for (let index = 0; index < length; index += 1) {
    const position = index * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, input.length - 1);
    const fraction = position - left;
    const sample = Math.max(-1, Math.min(1, input[left] * (1 - fraction) + input[right] * fraction));
    output[index] = sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff);
  }
  return output;
}
