import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createAvatarDeliveryRuntime } from "../../../candidate/avatar-runtime.js";
import { createCandidateInterviewRuntime, createLocalRecordingBackup } from "../../../candidate/runtime.js";
import { createPcm16kStream, createPcmPlaybackQueue } from "../../../candidate/pcm-stream.js";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, Status } from "../../core/ui.jsx";

export default function CandidateFeaturePage() {
  const { route } = useWorkbench();
  return route.view === "invite" ? <InvitationPage /> : <CandidateRoom />;
}

function InvitationPage() {
  const { API, data, request, toast } = useWorkbench(); const [busy, setBusy] = useState(false); const [entering, setEntering] = useState(false); const invitation = data.invitation; const [confirmed, setConfirmed] = useState(invitation?.status === "registered"); const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 30000); return () => window.clearInterval(timer); }, []);
  useEffect(() => { if (invitation?.status === "registered") setConfirmed(true); }, [invitation?.status]);
  if (!invitation) return <PublicShell><Empty title="邀请链接不可用" copy="链接可能已过期、已使用或被撤销" /></PublicShell>;
  const token = invitation.token || location.hash.split("/")[1]; const startAt = new Date(invitation.scheduled_start_at); const endAt = new Date(invitation.scheduled_end_at); const canEnter = now >= startAt.getTime() && now <= endAt.getTime();
  const confirm = async (event) => { event.preventDefault(); setBusy(true); try { const form = new FormData(event.currentTarget); await request(`${API}/public/interview-invitations/${encodeURIComponent(token)}/intake`, { method: "POST", body: { name: form.get("name"), email: form.get("email"), phone: form.get("phone"), consent: { accepted: form.get("privacy_accepted") === "on", version: invitation.consent?.version || "v1", recording_accepted: form.get("recording_accepted") === "on" } } }); setConfirmed(true); toast("预约确认成功", "系统正在准备本次面试语音，并已安排面试前 30 分钟邮件提醒"); } catch (error) { toast("暂时无法确认预约", error.message, "error"); } finally { setBusy(false); } };
  const enterInterview = async () => { setEntering(true); try { let granted = false; let stream; try { stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false }); granted = stream.getAudioTracks().length > 0; } finally { stream?.getTracks().forEach((track) => track.stop()); } const readiness = await request(`${API}/public/interview-invitations/${encodeURIComponent(token)}/readiness`, { method: "POST", body: { browser_supported: Boolean(navigator.mediaDevices && window.MediaRecorder), microphone_granted: granted, audio_content_type: supportedMimeType() } }); if (!readiness.can_start) throw new Error("麦克风或预约运行条件尚未就绪"); const result = await request(`${API}/public/interview-invitations/${encodeURIComponent(token)}/start`, { method: "POST" }); location.href = result.candidate_join_url; } catch (error) { toast("暂时无法进入面试", error.message, "error"); setEntering(false); } };
  return <PublicShell><section className="panel candidate-invitation" style={{ maxWidth: 760, margin: "48px auto", padding: 28 }}><p className="eyebrow">候选人面试预约</p><h1>{invitation.position_name}</h1><div className="appointment-time-card"><span>预约时间</span><strong>{formatAppointmentTime(startAt)} — {formatAppointmentTime(endAt)}</strong></div>{confirmed ? <div className="appointment-confirmed"><Status value="预约已确认" tone="ready" /><h2>身份核验通过</h2><p>我们会在面试开始前 30 分钟向您的登记邮箱发送提醒。请保留当前邀请链接，到预约时间后再进入面试。</p><button className="button button-primary" type="button" disabled={!canEnter || entering} onClick={enterInterview}>{entering ? "正在检查设备…" : canEnter ? "检查设备并进入面试" : "尚未到面试时间"}</button></div> : <><p>{invitation.consent?.privacy_notice}</p><p className="form-hint">核验通过后即确认预约，不会立即启动面试。</p><form onSubmit={confirm}><div className="form-grid"><Field label="姓名" full><input className="form-input" name="name" required /></Field><Field label="邮箱"><input className="form-input" name="email" type="email" required /></Field><Field label="手机号"><input className="form-input" name="phone" required /></Field></div><label><input type="checkbox" name="privacy_accepted" required /> 我已阅读并同意隐私说明</label>{invitation.consent?.recording_required && <label style={{ display: "block", marginTop: 12 }}><input type="checkbox" name="recording_accepted" required /> 我同意录制答题音频用于转写、评分与复核</label>}<button className="button button-primary" type="submit" disabled={busy} style={{ marginTop: 24 }}>{busy ? "正在核验…" : "核验身份并确认预约"}</button></form></>}</section></PublicShell>;
}

function CandidateRoom() {
  const { API, data, route, request, setResource, toast } = useWorkbench();
  const interview = data.selectedInterview;
  const token = data.candidateToken || route.candidateToken;
  const current = interview?.turns?.find((item) => item.id === interview.current_turn_id);
  const wantsVideo = Boolean(interview?.record_video ?? interview?.settings?.record_video ?? false);
  const [stream, setStream] = useState(null);
  const [devices, setDevices] = useState({ audio: [], video: [] });
  const [selectedAudio, setSelectedAudio] = useState("");
  const [selectedVideo, setSelectedVideo] = useState("");
  const [avatarMedia, setAvatarMedia] = useState(null);
  const [avatarSpeaking, setAvatarSpeaking] = useState(false);
  const [phase, setPhase] = useState("idle");
  const [storedAudioUri, setStoredAudioUri] = useState(null);
  const [mimeType, setMimeType] = useState("audio/webm");
  const [transcript, setTranscript] = useState("");
  const [interim, setInterim] = useState("");
  const [duration, setDuration] = useState(0);
  const [audioEnabled, setAudioEnabled] = useState(true);
  const [videoEnabled, setVideoEnabled] = useState(wantsVideo);
  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const socketRef = useRef(null);
  const sttSocketRef = useRef(null);
  const pcmRef = useRef(null);
  const timerRef = useRef(null);
  const recognitionRef = useRef(null);
  const startedAtRef = useRef(0);
  const phaseRef = useRef("idle");
  const interviewRef = useRef(interview);
  const currentRef = useRef(current);
  const pendingSubmissionRef = useRef(null);
  const streamModeRef = useRef("none");
  const streamBackupRef = useRef(null);
  const submittingRef = useRef(false);
  const spokenTurnRef = useRef(null);
  const s2sTurnRef = useRef(null);
  const s2sAudioReceivedRef = useRef(false);
  interviewRef.current = interview;
  currentRef.current = current;

  const changePhase = useCallback((next) => { phaseRef.current = next; setPhase(next); }, []);
  const candidateRequest = useCallback((suffix = "", options = {}) => request(`${API}/public/interviews/${encodeURIComponent(interviewRef.current?.id || route.selectedInterviewId)}${suffix}`, { ...options, headers: { ...(options.headers || {}), "X-Candidate-Session-Token": token || "" } }), [API, request, route.selectedInterviewId, token]);
  const refreshInterview = useCallback(async () => {
    const updated = await candidateRequest();
    interviewRef.current = updated;
    setResource("selectedInterview", updated);
    return updated;
  }, [candidateRequest, setResource]);
  const runtime = useMemo(() => createCandidateInterviewRuntime({
    onStateChange: (state) => { changePhase(state.phase); if (state.mimeType) setMimeType(state.mimeType); },
    onRecoveryNeeded: (message) => toast("录音等待恢复", message, "error"),
  }), [changePhase, toast]);
  const backupRuntime = useMemo(() => createLocalRecordingBackup(), []);
  const avatarRuntime = useMemo(() => createAvatarDeliveryRuntime({
    onMediaChange: setAvatarMedia,
    onSpeakingChange: setAvatarSpeaking,
    closeSession: (media) => closeAvatarSession(media, API, interviewRef.current?.id || route.selectedInterviewId, token),
  }), [API, route.selectedInterviewId, token]);
  const pcmPlayback = useMemo(() => createPcmPlaybackQueue({
    onStart: () => setAvatarSpeaking(true),
    onEnd: () => setAvatarSpeaking(false),
  }), []);

  const resetAnswerUi = useCallback(() => {
    clearInterval(timerRef.current);
    recognitionRef.current?.stop?.();
    recognitionRef.current = null;
    pendingSubmissionRef.current = null;
    streamBackupRef.current = null;
    streamModeRef.current = "none";
    submittingRef.current = false;
    const socket = sttSocketRef.current;
    sttSocketRef.current = null;
    if (socket?.readyState === WebSocket.OPEN) socket.close();
    pcmRef.current?.stop?.();
    pcmRef.current = null;
    setStoredAudioUri(null);
    setTranscript("");
    setInterim("");
    setDuration(0);
    runtime.reset();
    backupRuntime.reset();
  }, [backupRuntime, runtime]);

  const submitStoredAnswer = useCallback(async (audioUri, contentType, metadata = pendingSubmissionRef.current) => {
    if (!audioUri || !metadata?.turnId || submittingRef.current) return;
    submittingRef.current = true;
    setStoredAudioUri(audioUri);
    changePhase("processing");
    try {
      await candidateRequest("/audio-answers", {
        method: "POST",
        body: {
          turn_id: metadata.turnId,
          audio_uri: audioUri,
          content_type: contentType || metadata.mimeType || "audio/webm",
          language: "zh-CN",
          duration_seconds: metadata.duration,
          ...(["localhost", "127.0.0.1"].includes(location.hostname) && metadata.transcript ? { development_transcript: metadata.transcript, development_confidence: .85 } : {}),
        },
      });
      resetAnswerUi();
      await refreshInterview();
      toast("回答已接收", "追问或下一题已同步，评分正在后台进行");
    } catch (error) {
      submittingRef.current = false;
      changePhase("submit_failed");
      toast("回答提交失败", `${error.message}；完整录音仍保留，可重试提交`, "error");
    }
  }, [candidateRequest, changePhase, refreshInterview, resetAnswerUi, toast]);

  const handleDialogueMedia = useCallback(async (event) => {
    const payload = event.payload || {};
    if (event.type === "output.audio.delta") {
      s2sAudioReceivedRef.current = true;
      await pcmPlayback.enqueue(event.audio_base64, event.sample_rate_hz || 24000);
    } else if (event.type === "dialogue.error") {
      if (!s2sAudioReceivedRef.current && s2sTurnRef.current) spokenTurnRef.current = null;
      toast("实时语音已降级", "本轮将使用普通数字人语音播报", "error");
    } else if (event.type.endsWith("audio.delta") && (payload.delivery || payload.audio_uri)) {
      await avatarRuntime.play(payload.delivery || { mode: "audio", audio_uri: payload.audio_uri, avatar_mode: interviewRef.current?.avatar_mode || "local" });
    }
    if (event.type.endsWith("speech.interrupted") || event.type === "interview.paused") await avatarRuntime.stop();
  }, [avatarRuntime, pcmPlayback, toast]);

  const ensureSocket = useCallback(async () => {
    const existing = socketRef.current;
    if (existing?.readyState === WebSocket.OPEN) return existing;
    if (existing?.readyState === WebSocket.CONNECTING) {
      return new Promise((resolve, reject) => {
        existing.addEventListener("open", () => resolve(existing), { once: true });
        existing.addEventListener("error", () => reject(new Error("实时会话连接失败")), { once: true });
      });
    }
    return new Promise((resolve, reject) => {
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const active = interviewRef.current;
      const socket = new WebSocket(`${protocol}//${location.host}${API}/interviews/${encodeURIComponent(active.id)}/live?role=candidate&token=${encodeURIComponent(token)}`);
      socketRef.current = socket;
      socket.addEventListener("open", () => {
        socket.send(JSON.stringify({ type: "session.ready", payload: { source: "react_candidate" } }));
        resolve(socket);
      }, { once: true });
      socket.addEventListener("message", async ({ data: raw }) => {
        const event = JSON.parse(raw);
        if (event.type === "media.recording.stopped") {
          runtime.acknowledgeMediaStored();
          const uri = event.payload.audio_uri;
          const type = event.payload.mime_type || "audio/webm";
          setStoredAudioUri(uri);
          setMimeType(type);
          await submitStoredAnswer(uri, type);
          return;
        }
        if (event.type === "stt.transcript.partial") setInterim(event.payload?.text || "");
        if (event.type === "stt.stream.error" && event.payload?.error_code === "stream_disconnected_batch_repaired") {
          resetAnswerUi();
          await refreshInterview();
          toast("回答已恢复", "网络中断前的录音已由服务端修复并进入后台评分");
        }
        if (event.type === "error") toast("实时会话错误", event.payload?.message || event.payload?.code, "error");
        await handleDialogueMedia(event);
        if (event.type === "session.state.changed") {
          const status = event.payload?.status;
          if (["paused", "cancelled"].includes(status)) changePhase(status);
          else if (status === "in_progress" && ["paused", "cancelled"].includes(phaseRef.current)) changePhase("idle");
        }
        if (
          ["session.state.changed", "question.selected", "evaluation.completed", "interview.completed"].includes(event.type)
          || event.type.startsWith("followup.")
          || event.type.startsWith("transcription.")
          || event.type.startsWith("evaluation.")
        ) await refreshInterview();
      });
      socket.addEventListener("error", () => reject(new Error("实时会话连接失败")), { once: true });
      socket.addEventListener("close", () => {
        if (socketRef.current === socket) socketRef.current = null;
        if (["recording", "stopping", "processing"].includes(phaseRef.current)) toast("实时连接中断", "完整录音保存在本地，连接恢复后可继续提交", "error");
      });
    });
  }, [API, changePhase, handleDialogueMedia, refreshInterview, resetAnswerUi, runtime, submitStoredAnswer, toast, token]);

  const uploadStreamBackup = useCallback(async () => {
    const metadata = pendingSubmissionRef.current;
    const recording = streamBackupRef.current || await backupRuntime.stop();
    streamBackupRef.current = recording;
    if (!recording?.size) throw new Error("实时识别中断且本地录音为空");
    streamModeRef.current = "batch_upload";
    await runtime.submitRecording({ recording, candidateSocket: await ensureSocket(), activeTurnId: metadata.turnId });
  }, [backupRuntime, ensureSocket, runtime]);

  const downgradeStreaming = useCallback(async (message, waitForServerRepair = false) => {
    if (streamModeRef.current !== "streaming") return;
    streamModeRef.current = "backup";
    await pcmRef.current?.stop?.();
    pcmRef.current = null;
    const socket = sttSocketRef.current;
    sttSocketRef.current = null;
    if (socket?.readyState === WebSocket.OPEN) socket.close();
    toast("实时转写已降级", `${message}；完整录音仍在本机保存，将在结束后自动提交批量转写`, "error");
    if (phaseRef.current !== "recording") {
      if (waitForServerRepair) {
        await new Promise((resolve) => window.setTimeout(resolve, 800));
        try {
          const updated = await refreshInterview();
          const turnId = pendingSubmissionRef.current?.turnId;
          const repaired = updated.current_turn_id !== turnId || updated.answers?.some((answer) => answer.turn_id === turnId);
          if (repaired) {
            resetAnswerUi();
            toast("回答已恢复", "服务端已保存完整音频并进入后台评分");
            return;
          }
        } catch { /* local backup remains available */ }
      }
      await uploadStreamBackup();
    }
  }, [refreshInterview, resetAnswerUi, toast, uploadStreamBackup]);

  const openSttStream = useCallback((turnId) => new Promise((resolve, reject) => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}${API}/interviews/${encodeURIComponent(interviewRef.current.id)}/stt-stream?token=${encodeURIComponent(token)}`);
    socket.binaryType = "arraybuffer";
    sttSocketRef.current = socket;
    let ready = false;
    socket.addEventListener("open", () => socket.send(JSON.stringify({ type: "stream.open", payload: { turn_id: turnId, content_type: "audio/pcm", sample_rate_hz: 16000, channels: 1, language: "zh-CN", enable_partial: true } })));
    socket.addEventListener("message", async ({ data: raw }) => {
      const event = JSON.parse(raw);
      if (event.type === "stream.ready") { ready = true; resolve(socket); }
      else if (event.type === "transcript.partial") setInterim(event.text || "");
      else if (event.type === "transcript.final") { setTranscript(event.text || ""); setInterim(""); }
      else if (event.type === "followup.selected" && event.delivery === "s2s") {
        s2sTurnRef.current = event.payload?.turn_id || null;
        s2sAudioReceivedRef.current = false;
        if (s2sTurnRef.current) spokenTurnRef.current = s2sTurnRef.current;
      } else if (event.type === "output.audio.delta" || event.type === "dialogue.error") {
        await handleDialogueMedia(event);
      } else if (event.type === "answer.accepted") {
        resetAnswerUi();
        await refreshInterview();
        toast("回答已接收", "追问或下一题已同步，评分正在后台进行");
      } else if (event.type === "interview.completed") await refreshInterview();
      else if (event.type === "stream.error") {
        const error = new Error(event.message || event.error_code || "实时转写失败");
        if (!ready) reject(error);
        else if (event.error_code !== "stream_failed_batch_repaired") await downgradeStreaming(error.message);
      }
    });
    socket.addEventListener("error", () => { if (!ready) reject(new Error("实时转写通道连接失败")); else downgradeStreaming("实时转写通道连接失败"); }, { once: true });
    socket.addEventListener("close", () => {
      if (sttSocketRef.current === socket) sttSocketRef.current = null;
      if (ready && streamModeRef.current === "streaming" && !["idle", "completed"].includes(phaseRef.current)) downgradeStreaming("实时转写通道已断开", true);
    });
  }), [API, downgradeStreaming, handleDialogueMedia, refreshInterview, resetAnswerUi, toast, token]);

  useEffect(() => { if (videoRef.current) videoRef.current.srcObject = stream; }, [stream]);
  useEffect(() => {
    if (!interview?.id || !token) return undefined;
    let heartbeat;
    let active = true;
    ensureSocket().then((socket) => {
      if (!active) return;
      heartbeat = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping", payload: {} }));
      }, 20000);
    }).catch((error) => toast("实时会话连接失败", error.message, "error"));
    return () => { active = false; clearInterval(heartbeat); };
  }, [ensureSocket, interview?.id, toast, token]);

  const speak = useCallback(async (turnId = currentRef.current?.id) => {
    if (!turnId || phaseRef.current !== "idle") return;
    try {
      const response = await candidateRequest("/avatar/speak", { method: "POST", body: { turn_id: turnId, language: "zh-CN", voice: "default" } });
      if (currentRef.current?.id !== turnId) return;
      await avatarRuntime.play(response);
    } catch (error) { toast("数字人读题失败", error.message, "error"); }
  }, [avatarRuntime, candidateRequest, toast]);

  useEffect(() => {
    if (!current?.id || current.status !== "asking" || interview?.status !== "in_progress" || spokenTurnRef.current === current.id) return;
    spokenTurnRef.current = current.id;
    speak(current.id);
  }, [current?.id, current?.status, interview?.status, speak]);

  useEffect(() => () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    socketRef.current?.close();
    sttSocketRef.current?.close();
    pcmRef.current?.stop?.();
    runtime.reset();
    backupRuntime.reset();
    clearInterval(timerRef.current);
    recognitionRef.current?.stop?.();
    avatarRuntime.stop();
    pcmPlayback.stop();
  }, [avatarRuntime, backupRuntime, pcmPlayback, runtime]);

  if (!interview) return <PublicShell><Empty title="面试链接不可用" copy="请联系面试官重新发送链接" /></PublicShell>;
  const completed = interview.turns?.filter((item) => item.status === "completed").length || 0;
  const finished = ["completed", "report_generating", "report_ready"].includes(interview.status);
  const cancelled = interview.status === "cancelled";
  const interrupted = interview.status === "paused";

  const openMedia = async (audioId = "", videoId = "", allowVideo = wantsVideo) => {
    const constraints = {
      audio: { ...(audioId ? { deviceId: { exact: audioId } } : {}), echoCancellation: true, noiseSuppression: true },
      video: allowVideo ? { ...(videoId ? { deviceId: { exact: videoId } } : {}), width: { ideal: 1280 }, height: { ideal: 720 } } : false,
    };
    let next;
    try { next = await navigator.mediaDevices.getUserMedia(constraints); }
    catch (error) {
      if (!allowVideo) throw error;
      next = await navigator.mediaDevices.getUserMedia({ ...constraints, video: false });
      toast("摄像头不可用", "已切换为仅麦克风模式，仍可继续面试", "error");
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = next;
    setStream(next);
    setAudioEnabled(true);
    setVideoEnabled(next.getVideoTracks().length > 0);
    return next;
  };
  const enableMedia = async () => {
    try {
      const next = await openMedia();
      const all = await navigator.mediaDevices.enumerateDevices();
      const audio = all.filter((item) => item.kind === "audioinput");
      const video = wantsVideo ? all.filter((item) => item.kind === "videoinput") : [];
      setDevices({ audio, video });
      setSelectedAudio(next.getAudioTracks()[0]?.getSettings?.().deviceId || audio[0]?.deviceId || "");
      setSelectedVideo(next.getVideoTracks()[0]?.getSettings?.().deviceId || video[0]?.deviceId || "");
      await ensureSocket();
      toast("设备已就绪", next.getVideoTracks().length ? "麦克风和摄像头检查通过" : "麦克风检查通过，本场无需摄像头");
    } catch (error) { toast("无法启用设备", error.message, "error"); }
  };
  const switchDevice = async (kind, deviceId) => {
    if (phaseRef.current !== "idle") return;
    const audioId = kind === "audio" ? deviceId : selectedAudio;
    const videoId = kind === "video" ? deviceId : selectedVideo;
    try {
      await openMedia(audioId, videoId, wantsVideo);
      if (kind === "audio") setSelectedAudio(deviceId); else setSelectedVideo(deviceId);
      toast("设备已切换", kind === "audio" ? "麦克风已更新" : "摄像头已更新");
    } catch (error) { toast("设备切换失败", error.message, "error"); }
  };
  const toggleAudio = () => { if (phaseRef.current !== "idle") return; const next = !audioEnabled; stream?.getAudioTracks().forEach((track) => { track.enabled = next; }); setAudioEnabled(next); };
  const toggleVideo = () => { if (phaseRef.current !== "idle") return; const next = !videoEnabled; stream?.getVideoTracks().forEach((track) => { track.enabled = next; }); setVideoEnabled(next); };

  const startRecording = async () => {
    const activeTurn = currentRef.current;
    if (!stream || !audioEnabled || !activeTurn || activeTurn.status !== "asking" || phaseRef.current !== "idle") return;
    changePhase("connecting");
    setStoredAudioUri(null); setTranscript(""); setInterim(""); setDuration(0);
    await avatarRuntime.stop();
    startedAtRef.current = Date.now();
    pendingSubmissionRef.current = { turnId: activeTurn.id, duration: 1, transcript: "", mimeType: "audio/webm" };
    try {
      const sttSocket = await openSttStream(activeTurn.id);
      backupRuntime.start(stream);
      streamModeRef.current = "streaming";
      try {
        pcmRef.current = await createPcm16kStream(stream, (chunk) => { if (sttSocket.readyState === WebSocket.OPEN) sttSocket.send(chunk); });
      } catch (error) {
        streamModeRef.current = "none";
        sttSocket.close();
        await backupRuntime.stop(); backupRuntime.reset();
        throw error;
      }
      changePhase("recording");
    } catch (streamError) {
      try {
        const socket = await ensureSocket();
        runtime.start({ mediaStream: stream, candidateSocket: socket, activeTurnId: activeTurn.id });
        streamModeRef.current = "batch";
        startRecognition(setTranscript, setInterim, recognitionRef);
        toast("实时转写已降级", "当前使用完整录音，结束后会自动提交批量转写");
      } catch (error) {
        changePhase("idle");
        toast("无法开始录音", `${streamError.message}；${error.message}`, "error");
        return;
      }
    }
    timerRef.current = setInterval(() => setDuration(Math.floor((Date.now() - startedAtRef.current) / 1000)), 1000);
  };
  const stopRecording = async () => {
    if (phaseRef.current !== "recording") return;
    changePhase("stopping");
    clearInterval(timerRef.current);
    recognitionRef.current?.stop?.();
    const elapsed = Math.max(1, Math.floor((Date.now() - startedAtRef.current) / 1000));
    setDuration(elapsed);
    pendingSubmissionRef.current = { turnId: currentRef.current?.id, duration: elapsed, transcript: `${transcript} ${interim}`.trim(), mimeType };
    if (streamModeRef.current === "streaming") {
      await pcmRef.current?.stop?.(); pcmRef.current = null;
      streamBackupRef.current = await backupRuntime.stop();
      changePhase("processing");
      const socket = sttSocketRef.current;
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "stream.finish", payload: { duration_seconds: elapsed } }));
      else await uploadStreamBackup();
    } else if (streamModeRef.current === "backup") {
      streamBackupRef.current = await backupRuntime.stop();
      await uploadStreamBackup();
    } else runtime.stop();
  };
  const recover = async () => { try { await runtime.recover(await ensureSocket()); } catch (error) { toast("恢复失败", error.message, "error"); } };
  const retrySubmit = async () => submitStoredAnswer(storedAudioUri, mimeType);
  const answerDisabled = !stream || !audioEnabled || !current || current.status !== "asking" || phase !== "idle" || avatarSpeaking || interrupted;

  return <PublicShell><div className="candidate-room">{cancelled ? <section className="candidate-completion"><h1>面试已取消</h1><p>本场面试已由企业面试官结束，如有疑问请联系招聘人员。</p></section> : finished ? <section className="candidate-completion"><h1>面试已完成</h1><p>{interview.status === "report_generating" ? "回答已经提交，系统正在生成报告。" : "回答已经提交，报告将由企业人员审核。"}</p></section> : <><section className="candidate-stage-grid"><AvatarStage media={avatarMedia} mode={interview.avatar_mode} speaking={avatarSpeaking} onReplay={() => speak()} onEnded={() => avatarRuntime.stop()} disabled={!current || phase !== "idle" || interrupted} /><aside className="candidate-device-panel"><div className={`candidate-camera${wantsVideo ? "" : " is-audio-only"}`}>{wantsVideo ? <video ref={videoRef} autoPlay muted playsInline /> : <div className="candidate-audio-only"><strong>仅麦克风面试</strong><span>本场不采集摄像头画面</span></div>}{!stream && <button className="button button-primary" onClick={enableMedia}>检查设备</button>}</div>{stream && <><div className="device-controls"><button className="device-toggle" onClick={toggleAudio} disabled={phase !== "idle"}>{audioEnabled ? "麦克风开" : "麦克风关"}</button>{wantsVideo && stream.getVideoTracks().length > 0 && <button className="device-toggle" onClick={toggleVideo} disabled={phase !== "idle"}>{videoEnabled ? "摄像头开" : "摄像头关"}</button>}</div><Field label="麦克风"><select className="form-select" value={selectedAudio} disabled={phase !== "idle"} onChange={(event) => switchDevice("audio", event.target.value)}>{devices.audio.map((item, index) => <option key={item.deviceId} value={item.deviceId}>{item.label || `麦克风 ${index + 1}`}</option>)}</select></Field>{wantsVideo && devices.video.length > 0 && <Field label="摄像头"><select className="form-select" value={selectedVideo} disabled={phase !== "idle"} onChange={(event) => switchDevice("video", event.target.value)}>{devices.video.map((item, index) => <option key={item.deviceId} value={item.deviceId}>{item.label || `摄像头 ${index + 1}`}</option>)}</select></Field>}</>}</aside></section>{interrupted && <div className="candidate-session-notice" role="status"><strong>面试已暂停</strong><span>请等待企业面试官恢复，本地录音不会主动丢弃。</span></div>}<section className="candidate-question-band"><div className="candidate-question-head"><div><span>{current?.is_followup ? `第 ${current.order} 题 · 追问` : `第 ${current?.order || "-"} / ${interview.turns?.length} 题`}</span><h1>{current?.question_spoken_text || "等待下一题"}</h1>{current?.is_followup && <small>请针对上一题补充说明</small>}</div><span>{completed}/{interview.turns?.length}</span></div><textarea className="form-textarea" value={`${transcript}${interim ? ` ${interim}` : ""}`} readOnly placeholder="服务端实时转写将在这里显示" /><div className="candidate-processing-status" role="status">{candidatePhaseText(phase, duration)}</div><div className="candidate-answer-actions"><button className="button button-record" onClick={startRecording} disabled={answerDisabled}>开始回答</button><button className="button button-secondary" onClick={stopRecording} disabled={phase !== "recording"}>结束并自动提交</button>{phase === "recoverable" && <button className="button button-secondary" onClick={recover}>恢复提交录音</button>}{phase === "submit_failed" && <button className="button button-primary" onClick={retrySubmit}>重试提交</button>}</div></section></>}</div></PublicShell>;
}

function AvatarStage({ media, mode, speaking, onReplay, onEnded, disabled }) {
  const selectedMode = media?.avatar_mode || mode || "cloud";
  const label = selectedMode === "local" ? "自研数字人" : "云数字人";
  const detail = media?.fallback_reason === "cloud_unavailable" ? "云服务不可用，已切换本地播放" : selectedMode === "local" ? "本地渲染 · 冻结语音" : "实时视频 · 云端驱动";
  return <div className={`candidate-avatar-stage${speaking ? " is-speaking" : ""}`}>{media?.mode === "webrtc" ? <iframe title="实时数智人" allow="autoplay; fullscreen" src={`/web/webrtc-player.html?url=${encodeURIComponent(media.stream_url)}`} /> : media?.mode === "video" ? <video src={media.stream_url} autoPlay playsInline onEnded={onEnded} /> : <img src="/web/assets/digital-interviewer.png" alt="数字人面试官" />}<span className="avatar-speaking-indicator" aria-hidden="true"><span /><span /><span /><span /></span><div className="candidate-stage-caption"><span><strong>{label}</strong><small>{detail}</small></span><button className="button button-secondary" type="button" onClick={onReplay} disabled={disabled}>重新朗读</button></div></div>;
}

function PublicShell({ children }) { return <div className="candidate-room"><header className="candidate-header"><span className="candidate-brand"><strong>Interviewer</strong><small>智能面试</small></span></header>{children}</div>; }
function candidatePhaseText(phase, duration) { return ({ idle: "准备就绪", connecting: "正在连接实时语音服务…", recording: `正在回答 · ${duration}s`, stopping: "正在结束录音…", processing: "回答已接收，正在生成追问并后台评分…", awaiting_server: "正在保存完整录音…", replaying: "正在恢复上传录音…", recoverable: "录音已保存在本机，等待恢复提交", submit_failed: "提交失败，完整录音仍可重试", paused: "面试已暂停", cancelled: "面试已取消" }[phase] || "正在处理…"); }
function supportedMimeType() { return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find((value) => window.MediaRecorder?.isTypeSupported?.(value)) || "audio/webm"; }
function formatAppointmentTime(value) { return Number.isNaN(value.getTime()) ? "待确认" : value.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
function startRecognition(setTranscript, setInterim, ref) { const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition; if (!Recognition) return; const instance = new Recognition(); instance.lang = "zh-CN"; instance.continuous = true; instance.interimResults = true; instance.addEventListener("result", (event) => { let final = ""; let partial = ""; for (let index = event.resultIndex; index < event.results.length; index += 1) { if (event.results[index].isFinal) final += event.results[index][0].transcript; else partial += event.results[index][0].transcript; } if (final) setTranscript((value) => `${value} ${final}`.trim()); setInterim(partial); }); ref.current = instance; instance.start(); }
function closeAvatarSession(media, API, interviewId, token) { if (!media?.session_id || !interviewId) return Promise.resolve(); return fetch(`${API}/public/interviews/${encodeURIComponent(interviewId)}/avatar/session/close`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Candidate-Session-Token": token || "" }, body: JSON.stringify({ session_id: media.session_id }) }).catch(() => {}); }
