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
