export function createCandidateInterviewRuntime({
  onStateChange = () => {},
  onRecoveryNeeded = () => {},
  maxBufferedBytes = 50 * 1024 * 1024,
  stopAckTimeoutMs = 15000,
} = {}) {
  let recorder = null;
  let turnId = null;
  let socket = null;
  let chunks = [];
  let bufferedBytes = 0;
  let mimeType = "audio/webm";
  let stopTimer = null;
  let phase = "idle";

  function transition(next, details = {}) {
    phase = next;
    onStateChange({ phase, turnId, mimeType, bufferedBytes, ...details });
  }

  function sendJson(target, message) {
    if (target?.readyState !== WebSocket.OPEN) return false;
    target.send(JSON.stringify(message));
    return true;
  }

  function sendStart(target) {
    return sendJson(target, {
      type: "candidate.media.start",
      turn_id: turnId,
      payload: { mime_type: mimeType, source: "microphone", timeslice_ms: 400 },
    });
  }

  function waitForStopAcknowledgement() {
    if (stopTimer) window.clearTimeout(stopTimer);
    stopTimer = window.setTimeout(() => {
      stopTimer = null;
      transition("recoverable", { reason: "media_stop_ack_timeout" });
      onRecoveryNeeded("录音已保存在本机，但服务端未确认接收。请重新连接并恢复提交。");
    }, stopAckTimeoutMs);
  }

  function start({ mediaStream, candidateSocket, activeTurnId }) {
    if (phase !== "idle") throw new Error("已有录音正在处理");
    const audioTracks = mediaStream?.getAudioTracks?.() || [];
    if (!audioTracks.length) throw new Error("麦克风音轨不可用");
    if (!candidateSocket || candidateSocket.readyState !== WebSocket.OPEN) throw new Error("实时通道尚未连接");
    const audioStream = new MediaStream(audioTracks);
    const supported = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    mimeType = supported.find((value) => MediaRecorder.isTypeSupported(value)) || "";
    recorder = mimeType ? new MediaRecorder(audioStream, { mimeType }) : new MediaRecorder(audioStream);
    mimeType = recorder.mimeType || mimeType || "audio/webm";
    turnId = activeTurnId;
    socket = candidateSocket;
    chunks = [];
    bufferedBytes = 0;
    if (!sendStart(socket)) throw new Error("无法启动服务端录音");
    recorder.addEventListener("dataavailable", (event) => {
      if (!event.data.size) return;
      bufferedBytes += event.data.size;
      if (bufferedBytes > maxBufferedBytes) {
        try { recorder.stop(); } catch { /* recorder may already be stopping */ }
        transition("recoverable", { reason: "local_buffer_limit" });
        onRecoveryNeeded("录音过长，已停止并保留本地音频，请恢复连接后提交。");
        return;
      }
      chunks.push(event.data);
      if (socket?.readyState === WebSocket.OPEN) socket.send(event.data);
    });
    recorder.addEventListener("stop", () => {
      recorder = null;
      if (sendJson(socket, { type: "candidate.media.stop", turn_id: turnId, payload: {} })) {
        transition("awaiting_server");
        waitForStopAcknowledgement();
      } else {
        transition("recoverable", { reason: "socket_closed_before_stop" });
        onRecoveryNeeded("网络已断开，完整录音仍保存在本机。重新连接后即可恢复提交。");
      }
    }, { once: true });
    recorder.start(400);
    transition("recording");
    return { mimeType };
  }

  function stop() {
    if (phase !== "recording" || !recorder) return;
    transition("stopping");
    if (recorder.state !== "inactive") recorder.stop();
  }

  async function recover(candidateSocket) {
    if (phase !== "recoverable" || !chunks.length) throw new Error("没有可恢复的本地录音");
    if (!candidateSocket || candidateSocket.readyState !== WebSocket.OPEN) throw new Error("实时通道尚未恢复");
    socket = candidateSocket;
    if (!sendStart(socket)) throw new Error("无法重新启动服务端录音");
    transition("replaying");
    for (const chunk of chunks) socket.send(await chunk.arrayBuffer());
    if (!sendJson(socket, { type: "candidate.media.stop", turn_id: turnId, payload: { recovered: true } })) {
      transition("recoverable", { reason: "socket_closed_during_replay" });
      throw new Error("恢复发送期间连接再次中断");
    }
    transition("awaiting_server");
    waitForStopAcknowledgement();
  }

  async function submitRecording({ recording, candidateSocket, activeTurnId }) {
    if (!recording || typeof recording.arrayBuffer !== "function") throw new Error("没有可提交的完整录音");
    if (!["idle", "recoverable"].includes(phase)) throw new Error("已有录音正在处理");
    if (!candidateSocket || candidateSocket.readyState !== WebSocket.OPEN) throw new Error("实时通道尚未恢复");
    turnId = activeTurnId;
    socket = candidateSocket;
    mimeType = recording.type || "audio/webm";
    chunks = [recording];
    bufferedBytes = Number(recording.size || 0);
    if (bufferedBytes > maxBufferedBytes) throw new Error("完整录音超过可恢复大小上限");
    if (!sendStart(socket)) throw new Error("无法启动服务端录音");
    transition("replaying");
    try {
      socket.send(await recording.arrayBuffer());
      if (!sendJson(socket, { type: "candidate.media.stop", turn_id: turnId, payload: { recovered: true } })) {
        throw new Error("恢复发送期间连接再次中断");
      }
      transition("awaiting_server");
      waitForStopAcknowledgement();
    } catch (error) {
      transition("recoverable", { reason: "socket_closed_during_replay" });
      throw error;
    }
  }

  function acknowledgeMediaStored() {
    if (stopTimer) window.clearTimeout(stopTimer);
    stopTimer = null;
    transition("stored");
  }

  function reset() {
    if (stopTimer) window.clearTimeout(stopTimer);
    stopTimer = null;
    if (recorder?.state && recorder.state !== "inactive") {
      try { recorder.stop(); } catch { /* recorder may already be stopping */ }
    }
    recorder = null;
    turnId = null;
    socket = null;
    chunks = [];
    bufferedBytes = 0;
    mimeType = "audio/webm";
    transition("idle");
  }

  return {
    start,
    stop,
    recover,
    submitRecording,
    acknowledgeMediaStored,
    reset,
    getSnapshot: () => ({ phase, turnId, mimeType, bufferedBytes, hasBufferedAudio: chunks.length > 0 }),
  };
}

export function createLocalRecordingBackup({ maxBufferedBytes = 50 * 1024 * 1024 } = {}) {
  let recorder = null;
  let chunks = [];
  let bufferedBytes = 0;
  let mimeType = "audio/webm";
  let completion = null;

  function start(mediaStream) {
    if (recorder) throw new Error("本地录音备份已经启动");
    const audioTracks = mediaStream?.getAudioTracks?.() || [];
    if (!audioTracks.length) throw new Error("麦克风音轨不可用");
    const audioStream = new MediaStream(audioTracks);
    const supported = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    const selected = supported.find((value) => MediaRecorder.isTypeSupported(value)) || "";
    recorder = selected ? new MediaRecorder(audioStream, { mimeType: selected }) : new MediaRecorder(audioStream);
    mimeType = recorder.mimeType || selected || "audio/webm";
    chunks = [];
    bufferedBytes = 0;
    completion = new Promise((resolve, reject) => {
      recorder.addEventListener("dataavailable", (event) => {
        if (!event.data?.size) return;
        bufferedBytes += event.data.size;
        if (bufferedBytes > maxBufferedBytes) {
          reject(new Error("本地录音备份超过大小上限"));
          try { recorder.stop(); } catch { /* recorder may already be stopping */ }
          return;
        }
        chunks.push(event.data);
      });
      recorder.addEventListener("error", () => reject(new Error("本地录音备份失败")), { once: true });
      recorder.addEventListener("stop", () => {
        const result = new Blob(chunks, { type: mimeType });
        recorder = null;
        resolve(result);
      }, { once: true });
    });
    recorder.start(400);
  }

  async function stop() {
    if (!recorder) return completion;
    if (recorder.state !== "inactive") recorder.stop();
    return completion;
  }

  function reset() {
    if (recorder?.state && recorder.state !== "inactive") {
      try { recorder.stop(); } catch { /* recorder may already be stopping */ }
    }
    recorder = null;
    chunks = [];
    bufferedBytes = 0;
    completion = null;
    mimeType = "audio/webm";
  }

  return {
    start,
    stop,
    reset,
    getSnapshot: () => ({ active: Boolean(recorder), mimeType, bufferedBytes }),
  };
}
