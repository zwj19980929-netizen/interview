export async function createPcm16kStream(mediaStream, onChunk) {
  const tracks = mediaStream?.getAudioTracks?.() || [];
  if (!tracks.length) throw new Error("麦克风音轨不可用");
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) throw new Error("当前浏览器不支持实时 PCM 音频处理");
  const context = new AudioContext();
  await context.resume();
  const source = context.createMediaStreamSource(new MediaStream(tracks));
  const processor = context.createScriptProcessor(4096, 1, 1);
  const silence = context.createGain();
  silence.gain.value = 0;
  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    const output = resampleTo16k(input, context.sampleRate);
    if (output.byteLength) onChunk(output.buffer);
  };
  source.connect(processor);
  processor.connect(silence);
  silence.connect(context.destination);
  return {
    stop() {
      processor.onaudioprocess = null;
      source.disconnect();
      processor.disconnect();
      silence.disconnect();
      return context.close();
    },
  };
}

export function createPcmPlaybackQueue({ onStart = () => {}, onEnd = () => {} } = {}) {
  let context = null;
  let nextStartAt = 0;
  let stopped = false;
  const sources = new Set();

  async function ensureContext() {
    if (context) return context;
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) throw new Error("当前浏览器不支持实时 PCM 播放");
    context = new AudioContext();
    await context.resume();
    return context;
  }

  async function enqueue(audioBase64, sampleRateHz = 24000) {
    if (stopped || !audioBase64) return;
    const activeContext = await ensureContext();
    const binary = window.atob(audioBase64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const sampleCount = Math.floor(bytes.byteLength / 2);
    if (!sampleCount) return;
    const buffer = activeContext.createBuffer(1, sampleCount, sampleRateHz);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < sampleCount; index += 1) channel[index] = view.getInt16(index * 2, true) / 32768;
    const source = activeContext.createBufferSource();
    source.buffer = buffer;
    source.connect(activeContext.destination);
    const startAt = Math.max(activeContext.currentTime + 0.015, nextStartAt);
    nextStartAt = startAt + buffer.duration;
    sources.add(source);
    source.addEventListener("ended", () => {
      sources.delete(source);
      source.disconnect();
      if (!sources.size) onEnd();
    }, { once: true });
    onStart();
    source.start(startAt);
  }

  async function stop() {
    stopped = true;
    for (const source of sources) {
      try { source.stop(); } catch { /* source may already be complete */ }
      source.disconnect();
    }
    sources.clear();
    nextStartAt = 0;
    if (context) await context.close();
    context = null;
    onEnd();
  }

  return { enqueue, stop };
}

function resampleTo16k(input, sourceRate) {
  if (!input.length) return new Int16Array();
  const ratio = sourceRate / 16000;
  const length = Math.max(1, Math.floor(input.length / ratio));
  const output = new Int16Array(length);
  for (let index = 0; index < length; index += 1) {
    const sourceIndex = index * ratio;
    const left = Math.floor(sourceIndex);
    const right = Math.min(left + 1, input.length - 1);
    const fraction = sourceIndex - left;
    const sample = Math.max(-1, Math.min(1, input[left] * (1 - fraction) + input[right] * fraction));
    output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output;
}
