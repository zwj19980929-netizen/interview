let preparedMedia = null;

const AUDIO_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];

const VIDEO_TYPES = [
  "video/webm;codecs=vp9,opus",
  "video/webm;codecs=vp8,opus",
  "video/webm",
  "video/mp4",
];

export async function runCandidatePreflight({
  probeUrl,
  mediaDevices = navigator.mediaDevices,
  fetchImpl = globalThis.fetch,
  now = () => performance.now(),
  frame = (callback) => requestAnimationFrame(callback),
  durationMs = 650,
  signal,
} = {}) {
  assertNotAborted(signal);
  if (!mediaDevices?.getUserMedia) throw new Error("当前浏览器无法访问麦克风和摄像头");
  const stream = await mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
  });
  if (signal?.aborted) {
    stream.getTracks().forEach((track) => track.stop());
    assertNotAborted(signal);
  }
  if (!stream.getAudioTracks().length || !stream.getVideoTracks().length) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error("设备预检必须同时取得麦克风和摄像头；摄像头只会按同意范围决定是否上行");
  }

  try {
    // WebGL context creation can block the main thread. Finish that work before
    // starting the HTTP clocks so it cannot inflate the first network sample.
    const avatarFps = await measureWebglFps({ now, frame, durationMs });
    assertNotAborted(signal);
    const network = await runCandidateNetworkCheck({ probeUrl, fetchImpl, now, signal });
    assertNotAborted(signal);
    const report = {
      browser_supported: Boolean(globalThis.MediaRecorder && globalThis.RTCPeerConnection),
      microphone_granted: true,
      camera_granted: true,
      speaker_verified: false,
      webrtc_supported: Boolean(globalThis.RTCPeerConnection),
      audio_worklet_supported: supportsAudioWorklet(),
      webgl_supported: avatarFps > 0,
      media_recorder_supported: Boolean(globalThis.MediaRecorder),
      network_rtt_ms: network.rttMs,
      network_jitter_ms: network.jitterMs,
      avatar_fps: avatarFps,
      audio_content_type: supportedAudioMimeType(),
      video_content_type: supportedVideoMimeType(),
    };
    prepareCandidateMedia(stream, report);
    return { stream, report };
  } catch (error) {
    stream.getTracks().forEach((track) => track.stop());
    throw error;
  }
}

export async function verifySpeaker({ AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext } = {}) {
  if (!AudioContextClass) throw new Error("当前浏览器无法执行扬声器检测");
  const context = new AudioContextClass();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.frequency.value = 660;
  gain.gain.setValueAtTime(0.0001, context.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.03);
  gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.28);
  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start();
  oscillator.stop(context.currentTime + 0.3);
  await new Promise((resolve) => oscillator.addEventListener("ended", resolve, { once: true }));
  oscillator.disconnect();
  gain.disconnect();
  await context.close();
}

export function prepareCandidateMedia(stream, report) {
  if (preparedMedia?.stream && preparedMedia.stream !== stream) {
    preparedMedia.stream.getTracks().forEach((track) => track.stop());
  }
  preparedMedia = { stream, report: { ...report }, preparedAt: Date.now() };
}

export function claimPreparedCandidateMedia(maxAgeMs = 5 * 60 * 1000) {
  const value = preparedMedia;
  preparedMedia = null;
  if (!value) return null;
  const live = value.stream?.getAudioTracks?.().some((track) => track.readyState === "live")
    && value.stream?.getVideoTracks?.().some((track) => track.readyState === "live");
  if (!live || Date.now() - value.preparedAt > maxAgeMs) {
    value.stream?.getTracks?.().forEach((track) => track.stop());
    return null;
  }
  return value;
}

export function peekPreparedCandidateMedia(maxAgeMs = 5 * 60 * 1000) {
  if (!preparedMedia) return null;
  const live = preparedMedia.stream?.getAudioTracks?.().some((track) => track.readyState === "live")
    && preparedMedia.stream?.getVideoTracks?.().some((track) => track.readyState === "live");
  if (!live || Date.now() - preparedMedia.preparedAt > maxAgeMs) return null;
  return { stream: preparedMedia.stream, report: { ...preparedMedia.report } };
}

export function discardPreparedCandidateMedia() {
  preparedMedia?.stream?.getTracks?.().forEach((track) => track.stop());
  preparedMedia = null;
}

export function recordPreparedAvatarFps(value) {
  const fps = Number(value);
  if (!preparedMedia || !Number.isFinite(fps) || fps < 0) return false;
  preparedMedia.report = {
    ...preparedMedia.report,
    avatar_fps: Math.round(fps * 10) / 10,
    webgl_supported: fps > 0,
  };
  return true;
}

export function supportedAudioMimeType() {
  return AUDIO_TYPES.find((value) => globalThis.MediaRecorder?.isTypeSupported?.(value)) || "audio/webm";
}

export function supportedVideoMimeType() {
  return VIDEO_TYPES.find((value) => globalThis.MediaRecorder?.isTypeSupported?.(value)) || "video/webm";
}

export function supportsAudioWorklet() {
  const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
  return Boolean(Context && globalThis.AudioWorkletNode && "audioWorklet" in Context.prototype);
}

export async function runCandidateNetworkCheck({
  probeUrl,
  fetchImpl = globalThis.fetch,
  now = () => performance.now(),
  timeoutMs = 3000,
  signal,
} = {}) {
  if (!probeUrl || typeof fetchImpl !== "function") throw networkError("NETWORK_CHECK_UNAVAILABLE");
  const timeout = Number.isFinite(timeoutMs) ? Math.min(10_000, Math.max(1, timeoutMs)) : 3000;
  const samples = [];
  for (let index = 0; index < 4; index += 1) {
    assertNotAborted(signal);
    const started = now();
    const separator = probeUrl.includes("?") ? "&" : "?";
    const response = await fetchNetworkSample(`${probeUrl}${separator}preflight_probe=${index}`, {
      fetchImpl, timeout, signal,
    });
    assertNotAborted(signal);
    if (!response?.ok) throw networkError("NETWORK_CHECK_FAILED");
    samples.push(Math.max(0, now() - started));
  }
  const rttMs = samples.reduce((sum, value) => sum + value, 0) / samples.length;
  const jitterMs = samples.slice(1).reduce((sum, value, index) => sum + Math.abs(value - samples[index]), 0) / Math.max(1, samples.length - 1);
  return { rttMs: roundMetric(rttMs), jitterMs: roundMetric(jitterMs) };
}

async function fetchNetworkSample(url, { fetchImpl, timeout, signal }) {
  const controller = new AbortController();
  let timer;
  let onAbort;
  try {
    return await Promise.race([
      new Promise((resolve, reject) => {
        onAbort = () => {
          reject(networkError("NETWORK_CHECK_ABORTED"));
          controller.abort();
        };
        signal?.addEventListener("abort", onAbort, { once: true });
        timer = setTimeout(() => {
          reject(networkError("NETWORK_CHECK_TIMEOUT"));
          controller.abort();
        }, timeout);
        if (signal?.aborted) onAbort();
      }),
      Promise.resolve().then(() => {
        assertNotAborted(signal);
        return fetchImpl(url, { cache: "no-store", credentials: "same-origin", signal: controller.signal });
      }),
    ]);
  } catch (error) {
    if (["NETWORK_CHECK_ABORTED", "NETWORK_CHECK_TIMEOUT"].includes(error?.code)) throw error;
    throw networkError("NETWORK_CHECK_FAILED");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

function assertNotAborted(signal) {
  if (signal?.aborted) throw networkError("NETWORK_CHECK_ABORTED");
}

function networkError(code) {
  const error = new Error("网络检测未完成，请重新检测。");
  error.code = code;
  return error;
}

async function measureWebglFps({ now, frame, durationMs }) {
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl2", { antialias: false }) || canvas.getContext("webgl", { antialias: false });
  if (!gl) return 0;
  const started = now();
  let count = 0;
  await new Promise((resolve) => {
    const draw = (timestamp) => {
      gl.clearColor((count % 10) / 10, 0.12, 0.08, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      count += 1;
      if (timestamp - started >= durationMs) resolve();
      else frame(draw);
    };
    frame(draw);
  });
  const elapsed = Math.max(1, now() - started);
  gl.getExtension("WEBGL_lose_context")?.loseContext();
  return roundMetric((count * 1000) / elapsed);
}

function roundMetric(value) {
  return Math.round(value * 10) / 10;
}
