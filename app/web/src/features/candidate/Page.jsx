import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { candidateNotice, candidateEntryMessage, candidateEntryFailure } from "./presentation.js";

import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, Status } from "../../core/ui.jsx";
import {
  createCandidateInterviewExperience,
  rememberPreflightReport,
  reportCandidateRuntimeProblem,
} from "./agent-experience.js";
import {
  peekPreparedCandidateMedia,
  discardPreparedCandidateMedia,
  prepareCandidateMedia,
  recordPreparedAvatarFps,
  runCandidatePreflight,
  runCandidateNetworkCheck,
  verifySpeaker,
} from "./preflight.js";

const VrmAvatar = lazy(() => import("./VrmAvatar.jsx").then((module) => ({
  default: module.VrmAvatar,
})));

const EMPTY_EXPERIENCE = {
  phase: "connecting",
  floor: "none",
  session: null,
  currentQuestion: null,
  mediaStream: null,
  mediaPolicy: null,
  connection: { control: "connecting", media: "checking", recoveryAdapter: null },
  microphone: { enabled: true, localDetected: false, level: 0 },
  serverAudio: { received: false, receivedAt: null },
  evidence: { requested: false, ready: false },
  captureRecovery: null,
  captions: { forming: false, recent: [], full: [] },
  endpoint: { active: false, deadlineAt: null },
  calibration: { status: "pending", transcript: "", confidence: null, retryRequired: false },
  avatar: {
    status: "idle",
    performanceId: null,
    viseme: "sil",
    visemeWeight: 0,
    gesture: "idle",
    gestureIntensity: 0,
  },
  problem: null,
  completion: null,
  recovery: {
    retainedFrames: 0,
    unacknowledgedFrames: 0,
    encryptedBytes: 0,
    retentionMs: 30_000,
  },
};

export default function CandidateFeaturePage() {
  const { route, data, fatalError } = useWorkbench();
  if (route.view === "invite") {
    if (fatalError || !route.invitationToken || data.invitation?.token !== route.invitationToken) {
      return <PublicShell><div className="candidate-loading" role={fatalError ? "alert" : "status"}>
        {fatalError ? "暂时无法读取本次邀请，请重新打开邀请链接；若仍不可用，请联系面试安排人。" : "正在读取本次邀请…"}
      </div></PublicShell>;
    }
    return <InvitationPage key={route.invitationToken} />;
  }
  const binding = data.candidateSession;
  if (!binding || binding.interview?.id !== route.selectedInterviewId || binding.token !== route.candidateToken) {
    return <div className="candidate-room"><PublicHeader /><div className="candidate-loading" role={fatalError ? "alert" : "status"}>
      {fatalError ? "暂时无法进入面试，请重新打开邀请链接；若仍不可用，请联系面试安排人。" : "正在进入面试…"}
    </div></div>;
  }
  return <CandidateRoom key={`${binding.interview.id}:${binding.token}`} interview={binding.interview} token={binding.token} />;
}

function InvitationPage() {
  const { API, data, request, route, toast } = useWorkbench();
  const invitation = data.invitation;
  const [busy, setBusy] = useState(false);
  const [entering, setEntering] = useState(false);
  const enteringRef = useRef(false);
  const [entryStage, setEntryStage] = useState(null);
  const [entryFailure, setEntryFailure] = useState(null);
  const [confirmed, setConfirmed] = useState(invitation?.status === "registered");
  const [now, setNow] = useState(Date.now());
  const [preflight, setPreflight] = useState(null);
  const [speakerPlayed, setSpeakerPlayed] = useState(false);
  const speakerPlayedRef = useRef(false);
  const [networkState, setNetworkState] = useState({ status: "idle" });
  const networkBusyRef = useRef(false);
  const activeRef = useRef(true);
  const operationRef = useRef(0);
  const probeControllerRef = useRef(null);
  const previewRef = useRef(null);
  const mediaHandedOff = useRef(false);
  const entryFailureRef = useRef(null);

  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
      operationRef.current += 1;
      probeControllerRef.current?.abort();
      if (!mediaHandedOff.current) discardPreparedCandidateMedia();
    };
  }, []);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    setConfirmed(invitation?.status === "registered");
  }, [invitation?.status]);
  useEffect(() => {
    if (previewRef.current && preflight?.stream) {
      previewRef.current.srcObject = preflight.stream;
    }
  }, [preflight]);
  useEffect(() => {
    if (!entryFailure) return;
    entryFailureRef.current?.focus({ preventScroll: true });
    entryFailureRef.current?.scrollIntoView?.({ block: "center" });
  }, [entryFailure]);

  if (!invitation) {
    return <PublicShell><Empty title="邀请链接不可用" copy="链接可能已过期、已使用或被撤销" /></PublicShell>;
  }

  const token = invitation.token || route.invitationToken || location.hash.split("/")[1];
  const startAt = new Date(invitation.scheduled_start_at);
  const endAt = new Date(invitation.scheduled_end_at);
  const canEnter = now >= startAt.getTime() && now <= endAt.getTime();
  const requiredScopes = new Set(
    invitation.consent?.required_scopes
    || (invitation.consent?.recording_required ? ["audio_recording"] : []),
  );
  const isCurrentOperation = (operation) => activeRef.current && operationRef.current === operation;
  const networkReady = networkState.status === "checked" && networkReportReady(preflight?.report);
  const networkChecking = networkState.status === "checking";
  const browserReady = preflight?.report.browser_supported && preflight?.report.webrtc_supported
    && preflight?.report.audio_worklet_supported && preflight?.report.media_recorder_supported
    && preflight?.report.audio_content_type?.startsWith("audio/")
    && (!requiredScopes.has("video_recording") || preflight?.report.video_content_type?.startsWith("video/"));
  const displayReady = preflight?.report.webgl_supported && preflight?.report.avatar_fps >= 30;
  const devicesReady = browserReady && displayReady && preflight?.report.microphone_granted && preflight?.report.camera_granted;

  const confirm = async (event) => {
    event.preventDefault();
    if (enteringRef.current) return;
    enteringRef.current = true;
    const operation = ++operationRef.current;
    setBusy(true);
    try {
      const form = new FormData(event.currentTarget);
      const receipt = await request(`${API}/public/interview-invitations/${encodeURIComponent(token)}/intake`, {
        method: "POST",
        body: {
          name: form.get("name"),
          email: form.get("email"),
          phone: form.get("phone"),
          consent: {
            accepted: form.get("privacy_accepted") === "on",
            version: invitation.consent?.version || "v1",
            audio_recording: form.get("audio_recording") === "on",
            video_recording: form.get("video_recording") === "on",
          },
        },
      });
      if (!isCurrentOperation(operation)) return;
      if (!invitation.appointment_id || receipt?.appointment_id !== invitation.appointment_id
        || receipt.matched !== true || receipt.status !== "registered") {
        throw new Error("Invalid registration receipt");
      }
      setConfirmed(true);
      setEntryFailure(null);
      toast("预约确认成功", "系统正在准备本次面试语音，并已安排面试前 30 分钟邮件提醒");
    } catch (error) {
      if (!isCurrentOperation(operation)) return;
      toast("暂时无法确认预约", "请检查填写的信息，稍后再试。", "error");
    } finally {
      if (isCurrentOperation(operation)) {
        setBusy(false);
        enteringRef.current = false;
      }
    }
  };

  const beginPreflight = async () => {
    if (enteringRef.current || networkBusyRef.current) return;
    enteringRef.current = true;
    const operation = ++operationRef.current;
    const controller = new AbortController();
    probeControllerRef.current = controller;
    setEntering(true);
    setEntryFailure(null);
    discardPreparedCandidateMedia();
    setPreflight(null);
    speakerPlayedRef.current = false;
    setSpeakerPlayed(false);
    setNetworkState({ status: "idle" });
    try {
      const result = await runCandidatePreflight({ probeUrl: "/healthz", signal: controller.signal });
      if (!isCurrentOperation(operation)) return;
      setPreflight(result);
      setNetworkState({ status: "checked" });
    } catch (error) {
      if (!isCurrentOperation(operation)) return;
      const networkMessage = networkCheckErrorMessage(error?.code);
      toast(networkMessage ? "网络检测未通过" : "请检查设备", networkMessage || candidateEntryMessage(error), "error");
    } finally {
      if (isCurrentOperation(operation)) {
        enteringRef.current = false;
        setEntering(false);
      }
    }
  };

  const recheckNetwork = async () => {
    if (!preflight?.stream || enteringRef.current || networkBusyRef.current) return;
    networkBusyRef.current = true;
    const operation = ++operationRef.current;
    const controller = new AbortController();
    probeControllerRef.current = controller;
    const invalidReport = { ...preflight.report, network_rtt_ms: null, network_jitter_ms: null };
    setPreflight({ ...preflight, report: invalidReport });
    prepareCandidateMedia(preflight.stream, invalidReport);
    setNetworkState({ status: "checking" });
    try {
      const network = await runCandidateNetworkCheck({ probeUrl: "/healthz", signal: controller.signal });
      if (!isCurrentOperation(operation)) return;
      const report = {
        ...invalidReport,
        network_rtt_ms: network.rttMs,
        network_jitter_ms: network.jitterMs,
        speaker_verified: speakerPlayedRef.current,
      };
      prepareCandidateMedia(preflight.stream, report);
      setPreflight({ ...preflight, report });
      setNetworkState({ status: "checked" });
      if (networkReportReady(report)) setEntryFailure((failure) => failure?.invalidateNetwork ? null : failure);
    } catch (error) {
      if (!isCurrentOperation(operation)) return;
      setNetworkState({ status: "failed", reason: error?.code === "NETWORK_CHECK_TIMEOUT" ? "timeout" : "unavailable" });
    } finally {
      if (isCurrentOperation(operation)) networkBusyRef.current = false;
    }
  };

  const playSpeakerTest = async () => {
    try {
      await verifySpeaker();
      if (!activeRef.current) return;
      speakerPlayedRef.current = true;
      setSpeakerPlayed(true);
    } catch (error) {
      if (!activeRef.current) return;
      toast("扬声器检测失败", candidateEntryMessage(error), "error");
    }
  };

  const finishPreflightAndEnter = async () => {
    if (!confirmed || !preflight?.stream || !speakerPlayed || !networkReady || !devicesReady || enteringRef.current || networkBusyRef.current) return;
    enteringRef.current = true;
    const operation = ++operationRef.current;
    setEntering(true);
    setEntryStage("model_services");
    setEntryFailure(null);
    try {
      const report = { ...preflight.report, speaker_verified: true };
      prepareCandidateMedia(preflight.stream, report);
      rememberPreflightReport(report);
      const readiness = await request(
        `${API}/public/interview-invitations/${encodeURIComponent(token)}/readiness`,
        { method: "POST", body: report, timeoutMs: 40000 },
      );
      if (!isCurrentOperation(operation)) return;
      if (readiness?.can_start !== true) {
        throw Object.assign(new Error("Admission blocked"), { code: readiness?.entry_blocker?.code });
      }
      setEntryStage("starting");
      const result = await request(
        `${API}/public/interview-invitations/${encodeURIComponent(token)}/start`,
        { method: "POST", timeoutMs: 40000 },
      );
      if (!isCurrentOperation(operation)) return;
      mediaHandedOff.current = true;
      location.href = result.candidate_join_url;
    } catch (error) {
      if (!isCurrentOperation(operation)) return;
      const failure = candidateEntryFailure(error);
      setEntryFailure(failure);
      if (failure.registrationRequired) {
        setConfirmed(false);
        discardPreparedCandidateMedia();
        setPreflight(null);
        speakerPlayedRef.current = false;
        setSpeakerPlayed(false);
        setNetworkState({ status: "idle" });
      }
      if (failure.invalidateNetwork) {
        const invalidReport = { ...preflight.report, network_rtt_ms: null, network_jitter_ms: null };
        setPreflight({ ...preflight, report: invalidReport });
        prepareCandidateMedia(preflight.stream, invalidReport);
        rememberPreflightReport(invalidReport);
        setNetworkState({ status: "failed", reason: error.code === "REQUEST_TIMEOUT" ? "entry_timeout" : "unavailable" });
      }
      enteringRef.current = false;
      setEntering(false);
      setEntryStage(null);
    }
  };

  return <PublicShell>
    <section className="panel candidate-invitation">
      <p className="eyebrow">候选人面试预约</p>
      <h1>{invitation.position_name}</h1>
      <div className="appointment-time-card">
        <span>预约时间</span>
        <strong>{formatAppointmentTime(startAt)} — {formatAppointmentTime(endAt)}</strong>
      </div>
      {entryFailure && <div className="candidate-entry-failure" role="alert" tabIndex={-1} ref={entryFailureRef}>
        <strong>{entryFailure.title}</strong>
        <p>{entryFailure.detail}</p>
        {entryFailure.recheckDevices && <button className="button button-secondary" type="button" disabled={entering || networkChecking} onClick={beginPreflight}>重新检查设备</button>}
      </div>}
      {confirmed ? <div className="appointment-confirmed">
        <Status value="预约已确认" />
        <h2>身份核验通过</h2>
        <p>我们会在面试开始前 30 分钟向您的登记邮箱发送提醒。到预约时间后，检查设备即可进入面试。</p>
        {!preflight ? <button
          className="button button-primary"
          type="button"
          disabled={!canEnter || entering}
          onClick={beginPreflight}
        >
          {entering ? "正在检查设备…" : canEnter ? "检查设备并进入面试" : "尚未到面试时间"}
        </button> : <section className="candidate-preflight" aria-label="设备检查">
          <div className="candidate-preflight-preview">
            <video ref={previewRef} autoPlay muted playsInline aria-label="摄像头真实自拍预览" />
            <span>仅本机预览{requiredScopes.has("video_recording") ? " · 同意后将加密录像" : " · 本场不上传视频"}</span>
          </div>
          <div className="candidate-preflight-checks">
            <h3>设备检查</h3>
            <DeviceCheck label="麦克风与摄像头" ready={preflight.report.microphone_granted && preflight.report.camera_granted} />
            <DeviceCheck label="浏览器支持" ready={browserReady} />
            <DeviceCheck label="画面显示" ready={displayReady} />
            <DeviceCheck label="服务连接" ready={networkReady} detail={networkCheckDetail(preflight.report, networkState)} />
            <button className="button button-secondary" type="button" disabled={entering || networkChecking} onClick={recheckNetwork}>
              {networkChecking ? "正在检测网络…" : "重新检测网络"}
            </button>
            <p className="form-hint">请播放测试音，确认能听清后进入面试。</p>
            <button className="button button-secondary" type="button" disabled={entering || networkChecking} onClick={playSpeakerTest}>
              {speakerPlayed ? "重新播放测试音" : "播放扬声器测试音"}
            </button>
            <button className="button button-primary" type="button" disabled={!speakerPlayed || !networkReady || !devicesReady || entering || networkChecking} onClick={finishPreflightAndEnter}>
              {entering ? entryStage === "model_services" ? "正在准备面试…" : "正在进入面试…" : "我听到了测试音，进入面试"}
            </button>
            {entering && <p className="form-hint" role="status">{entryStage === "model_services"
              ? "正在准备，请稍等片刻。"
              : "正在进入面试，请稍候。"}</p>}
          </div>
        </section>}
      </div> : <>
        <div className="candidate-consent-copy">
          <p>{invitation.consent?.privacy_notice}</p>
          {invitation.consent?.takeover_notice && <p>{invitation.consent.takeover_notice}</p>}
          {invitation.consent?.inference_notice && <p>{invitation.consent.inference_notice}</p>}
        </div>
        <p className="form-hint">核验通过后只确认预约，不会启动摄像头、麦克风或面试。</p>
        <form onSubmit={confirm}>
          <div className="form-grid">
            <Field label="姓名" full><input className="form-input" name="name" required /></Field>
            <Field label="邮箱"><input className="form-input" name="email" type="email" required /></Field>
            <Field label="手机号"><input className="form-input" name="phone" required /></Field>
          </div>
          <label className="candidate-consent-check">
            <input type="checkbox" name="privacy_accepted" required /> 我已阅读并同意隐私说明
          </label>
          {requiredScopes.has("audio_recording") && <label className="candidate-consent-check">
            <input type="checkbox" name="audio_recording" required />
            {invitation.consent?.audio_recording_notice || "我同意录音，用于回答记录、评分和面试官复核"}
          </label>}
          {requiredScopes.has("video_recording") && <label className="candidate-consent-check">
            <input type="checkbox" name="video_recording" required />
            {invitation.consent?.video_recording_notice || "我同意录制视频，供面试官复核"}
          </label>}
          <button className="button button-primary" type="submit" disabled={busy}>
            {busy ? "正在核验…" : "核验身份并确认预约"}
          </button>
        </form>
      </>}
    </section>
  </PublicShell>;
}

function CandidateRoom({ interview, token }) {
  const { API, request } = useWorkbench();
  const [experience, setExperience] = useState(EMPTY_EXPERIENCE);
  const [avatarReady, setAvatarReady] = useState(false);
  const [expandedTranscript, setExpandedTranscript] = useState(false);
  const [startingProblem, setStartingProblem] = useState(null);
  const [previewStream] = useState(() => peekPreparedCandidateMedia()?.stream || null);
  const runRef = useRef(null);
  const openingController = useRef(null);
  const runtimeBlocked = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const videoRef = useRef(null);

  const candidateInterview = useMemo(
    () => createCandidateInterviewExperience({ apiBase: API, request }),
    [API, request],
  );
  const lastAvatarMetricAt = useRef(0);
  const avatarLoaded = useCallback((details) => {
    if (!mounted.current || runtimeBlocked.current) return;
    recordPreparedAvatarFps(details?.fps);
    setAvatarReady(true);
  }, []);
  const avatarFpsObserved = useCallback((fps) => {
    const now = Date.now();
    if (now - lastAvatarMetricAt.current < 10_000) return;
    lastAvatarMetricAt.current = now;
    runRef.current?.act({
      type: "telemetry.observe",
      idempotencyKey: uniqueActionKey("avatar_fps"),
      payload: { metric: "avatar_fps", value: Number(fps) },
    }).catch(() => {});
  }, []);
  const pauseForFatalProblem = useCallback(async (error, fallbackCode = "CANDIDATE_RUNTIME_FAILED") => {
    if (!mounted.current) return { pauseConfirmed: false };
    runtimeBlocked.current = true;
    openingController.current?.abort();
    const problemCode = error?.candidateProblemCode || fallbackCode;
    setStartingProblem({
      code: error?.code || problemCode,
      message: error.message || String(error),
      recoverable: false,
      pausePending: true,
      pauseConfirmed: false,
    });
    runRef.current?.act({
      type: "pause",
      idempotencyKey: uniqueActionKey("avatar_fatal"),
      payload: { reason: "avatar_renderer_fatal" },
    }).catch(() => {});
    try {
      const result = await reportCandidateRuntimeProblem({
        apiBase: API,
        request,
        interviewId: interview?.id,
        candidateSessionToken: token,
        code: problemCode,
      });
      const pauseConfirmed = result?.status === "paused";
      if (!mounted.current) return { pauseConfirmed };
      setStartingProblem({
        code: error?.code || problemCode,
        message: error.message || String(error),
        recoverable: false,
        pausePending: false,
        pauseConfirmed,
      });
      return { pauseConfirmed };
    } catch {
      if (!mounted.current) return { pauseConfirmed: false };
      setStartingProblem({
        code: error?.code || problemCode,
        message: error.message || String(error),
        recoverable: false,
        pausePending: false,
        pauseConfirmed: false,
      });
      return { pauseConfirmed: false };
    }
  }, [API, interview?.id, request, token]);

  const fatalAvatarProblem = useCallback(
    (error) => pauseForFatalProblem(error, "AVATAR_RENDERER_FAILED"),
    [pauseForFatalProblem],
  );

  useEffect(() => {
    if (!interview?.id || !token || !avatarReady) return undefined;
    let active = true;
    let unsubscribe = () => {};
    const controller = new AbortController();
    openingController.current = controller;
    candidateInterview.open({ interviewId: interview.id, ticket: token, signal: controller.signal })
      .then((run) => {
        if (!active || runtimeBlocked.current) {
          run.close();
          return;
        }
        runRef.current = run;
        unsubscribe = run.subscribe(setExperience);
      })
      .catch((error) => {
        if (active) pauseForFatalProblem(error, "CANDIDATE_RUNTIME_FAILED");
      });
    return () => {
      active = false;
      controller.abort();
      if (openingController.current === controller) openingController.current = null;
      unsubscribe();
      const run = runRef.current;
      runRef.current = null;
      run?.close();
    };
  }, [avatarReady, candidateInterview, interview?.id, pauseForFatalProblem, token]);

  useEffect(() => {
    const stream = experience.mediaStream || previewStream;
    if (videoRef.current && stream) {
      videoRef.current.srcObject = stream;
    }
  }, [experience.mediaStream, previewStream]);

  const act = useCallback((type, payload = {}) => {
    const run = runRef.current;
    if (!run || (runtimeBlocked.current && type !== "pause")) return;
    return run.act({
      type,
      payload,
      idempotencyKey: uniqueActionKey(type),
      turnId: experience.currentQuestion?.turn_id,
    }).catch((error) => {
      if (mounted.current && !runtimeBlocked.current) setStartingProblem({
        code: "CANDIDATE_ACTION_FAILED",
        message: error.message || String(error),
        recoverable: true,
      });
    });
  }, [experience.currentQuestion?.turn_id]);

  const problem = experience.problem || startingProblem;
  const session = experience.session || {};
  const completion = experience.completion;
  const calibration = experience.calibration || EMPTY_EXPERIENCE.calibration;
  const currentQuestion = experience.currentQuestion;
  const adaptive = session.execution_schema_version === 3;
  const planning = experience.phase === "planning";
  const totalQuestions = adaptive ? null : Number(session.total_primary_questions ?? interview?.turns?.filter((item) => !item.is_followup).length ?? 0);
  const answered = Number(session.completed_answers ?? interview?.answers?.length ?? 0);
  const pauseConfirmed = problem?.pauseConfirmed === true || session.status === "paused";
  const notice = candidateNotice(problem, pauseConfirmed);
  const visibleExperience = problem?.recoverable === false ? {
    ...experience, phase: "paused", floor: "none",
    avatar: { ...experience.avatar, status: "idle", viseme: "sil", visemeWeight: 0, gesture: "idle" },
  } : experience;

  if (!interview || !token) {
    return <div className="candidate-room"><PublicHeader /><div className="candidate-loading" role="status">正在进入面试…</div></div>;
  }
  if (completion || ["completed", "report_generating", "report_ready"].includes(interview.status)) {
    const receipt = completion || {
      interview_id: interview.id,
      submitted_at: interview.updated_at,
      recording_retention_notice: "录音录像按邀请页同意范围和企业保留策略保存。",
      human_review_required: true,
    };
    return <div className="candidate-room">
      <PublicHeader />
      <main className="candidate-completion">
        <span className="completion-icon" aria-hidden="true">✓</span>
        <h1>面试已提交</h1>
        <p>谢谢你的参与，回答已提交。面试结果将由企业审核。</p>
        <div className="candidate-receipt">
          <span><small>面试编号</small><strong>{receipt.interview_id}</strong></span>
          <span><small>提交时间</small><strong>{formatDateTime(receipt.submitted_at)}</strong></span>
        </div>
        <p>{receipt.recording_retention_notice}</p>
        <p>自动评分仅提供辅助证据，不会自动决定录用或淘汰。</p>
      </main>
    </div>;
  }

  return <div className="candidate-room">
    <PublicHeader>
      <div className="candidate-header-meta">
        <span className={`connection-dot is-${experience.connection.control}`} />
        <span>{connectionText(experience.connection)}</span>
        <span>{interview.record_video ? "本场录音录像" : interview.record_audio !== false ? "本场录音" : "本场不录制"}</span>
      </div>
    </PublicHeader>
    <main>
      <div className="candidate-stage-grid">
        <Suspense fallback={<div className="candidate-avatar-stage"><div className="vrm-avatar-gate" role="status">面试官正在准备…</div></div>}>
          <VrmAvatar
            apiBase={API}
            interviewId={interview.id}
            candidateSessionToken={token}
            avatar={visibleExperience.avatar}
            onReady={avatarLoaded}
            onFps={avatarFpsObserved}
            onFatalProblem={fatalAvatarProblem}
          />
        </Suspense>
        <aside className="candidate-device-panel" aria-label="候选人设备状态">
          <div className="candidate-camera">
            <video ref={videoRef} autoPlay muted playsInline aria-label="候选人真实自拍预览" />
            {!experience.mediaStream && !previewStream && <div className="device-permission" role="status">
              <strong>正在连接摄像头</strong>
              <span>请确认能看到自己的画面</span>
            </div>}
            <span className="camera-label">我的画面</span>
          </div>
          <CandidateSignalList experience={experience} blocked={problem?.recoverable === false} />
          <p className="privacy-note">
            录音录像仅用于本场面试记录与复核。真人面试官加入时会明确提示。
          </p>
        </aside>
      </div>

      <section className="candidate-question-band">
        {notice && <div className={notice.urgent ? "candidate-problem is-fatal" : "candidate-waiting-row"} role={notice.urgent ? "alert" : "status"}>
          <span><strong>{notice.title}</strong><small>{notice.detail}</small></span>
          {notice.retry && <button className="button button-secondary" type="button" disabled={experience.planningRetryPending}
            onClick={() => act(notice.action || "continue_speaking")}>{experience.planningRetryPending ? "正在重试…" : "重试"}</button>}
        </div>}

        <SpeechPlaybackState playback={problem?.recoverable === false ? null : experience.speechPlayback} act={act} />
        {!experience.session ? <CandidateSessionGate problem={problem} /> : calibration.status !== "completed" ? <WarmupPanel calibration={calibration} experience={visibleExperience} act={act} /> : <>
          <div className="candidate-question-head">
            <div>
              <p className="eyebrow">{planning ? "稍作停顿" : currentQuestion?.is_followup ? "基于你刚才回答的追问" : adaptive ? `交流话题 ${currentQuestion?.order || answered + 1}` : `正式问题 ${currentQuestion?.order || answered + 1}`}</p>
              <h1>{planning ? "面试官正在准备下一话题…" : currentQuestion?.question_text || "面试官正在组织下一句话…"}</h1>
            </div>
            <CandidateProgress answered={answered} totalQuestions={totalQuestions} adaptive={adaptive} />
          </div>
          <ConversationState phase={visibleExperience.phase} endpoint={experience.endpoint} formal
            speechDetected={experience.microphone.localDetected || experience.captions.forming} />
          <CaptionPanel
            captions={experience.captions}
            expanded={expandedTranscript}
            onToggle={() => setExpandedTranscript((value) => !value)}
          />
          <div className="candidate-answer-actions">
            {experience.captureRecovery?.status === "retry_required" && experience.phase !== "paused"
              ? <AnswerRecoveryAction recovery={experience.captureRecovery} act={act} disabled={problem?.recoverable === false} />
              : <div className="recording-actions">
              <button className="button button-secondary" type="button" onClick={() => act("finish_answer")} disabled={!experience.evidence?.ready || !["listening", "answer_preparing", "awaiting_supplement"].includes(visibleExperience.phase)}>
                提前结束回答
              </button>
              <button className="button button-secondary" type="button" onClick={() => act("request_repeat")} disabled={planning || visibleExperience.phase === "understanding" || visibleExperience.phase === "paused" || Boolean(experience.captureRecovery)}>
                请再说一遍
              </button>
              <button className="button button-secondary" type="button" onClick={() => act("continue_speaking")} disabled={problem?.recoverable === false || (!experience.endpoint.active && !["answer_preparing", "awaiting_supplement"].includes(experience.phase)) || !experience.evidence?.ready}>
                继续补充
              </button>
            </div>}
            <button className="button button-danger" type="button" onClick={() => act("pause")}>暂停面试</button>
          </div>
        </>}
      </section>
    </main>
  </div>;
}

function CandidateSessionGate({ problem }) {
  return <div className="candidate-warmup" role="status">
    <p className="eyebrow">面试准备</p>
    <h1>{problem?.recoverable === false ? "暂时无法进入面试" : "正在进入面试"}</h1>
    <p>{problem?.recoverable === false
      ? "请按上方提示操作。"
      : "马上就好，请稍等片刻。"}</p>
    {!problem && <span className="spinner" aria-hidden="true" />}
  </div>;
}

export function WarmupPanel({ calibration, experience, act }) {
  const blocked = experience.phase === "paused" || experience.session?.status === "paused" || experience.problem?.recoverable === false;
  const awaiting = calibration.status === "awaiting_confirmation";
  const retryable = calibration.status === "retrying"
    && (calibration.retryRequired === true || (
      experience.problem?.recoverable === true
      && experience.problem?.action === "retry_warmup"
    ));
  return <div className="candidate-warmup">
    <p className="eyebrow">不评分试音</p>
    <h1>{awaiting ? "请确认系统是否正确听懂了你" : "请用普通话做一句简短自我介绍，也可以夹带英文技术词"}</h1>
    <p>这段试音不计分，确认后会删除。</p>
    <ConversationState phase={experience.phase} endpoint={experience.endpoint} />
    <CaptionPanel captions={experience.captions} expanded />
    {(awaiting || retryable) && <div className="candidate-warmup-actions">
      {awaiting && <button className="button button-primary" type="button" disabled={blocked} onClick={() => act("warmup.confirm")}>字幕正确，开始正式面试</button>}
      <button className="button button-secondary" type="button" disabled={blocked} onClick={() => act("warmup.retry")}>
        {retryable ? "重新试音" : "听写不对，重新试音"}
      </button>
    </div>}
  </div>;
}

function CaptionPanel({ captions, expanded, onToggle }) {
  const rows = expanded ? captions.full : captions.recent;
  return <div className="candidate-transcript-wrap" aria-live="polite">
    <div className="transcript-toolbar">
      <span>{captions.forming
        ? "字幕更新中"
        : expanded ? "完整回答" : "你的回答"}</span>
      {onToggle && <button className="button button-ghost button-small" type="button" onClick={onToggle}>
        {expanded ? "收起完整转写" : "展开完整转写"}
      </button>}
    </div>
    <div className={`candidate-live-captions${expanded ? " is-expanded" : ""}`}>
      {rows.length ? rows.map((row, index) => <p key={`${row.at || index}:${row.text}`} className={row.final ? "is-final" : "is-partial"}>{row.text}</p>) : <p className="is-empty">你说的话会显示在这里。</p>}
    </div>
  </div>;
}

export function SpeechPlaybackState({ playback, act }) {
  if (!playback) return null;
  return <div className="candidate-waiting-row" role="status" aria-live="polite">
    <span><strong>{playback.status === "blocked" ? "点击下方按钮，继续听面试官提问" : "面试官正在准备，请稍等"}</strong></span>
    {playback.status === "blocked" && <button type="button" className="button button-primary"
      onClick={() => act("retry_speech")}>播放这句话</button>}
  </div>;
}

export function CandidateProgress({ answered, totalQuestions, adaptive = false }) {
  return <div className={`candidate-progress${adaptive ? " is-adaptive" : ""}`}
    aria-label={adaptive ? `已完成 ${answered} 个话题` : `已完成 ${answered}，共 ${totalQuestions} 个主问题`}>
    {adaptive ? `已完成 ${answered} 个话题` : `${answered}/${totalQuestions || "—"}`}
  </div>;
}

export function ConversationState({ phase, endpoint, formal = false, speechDetected }) {
  return <div className="candidate-waiting-row" role="status" aria-live="polite">
    <span>
      <strong>{phase === "listening" && speechDetected === false ? "正在聆听，等待你开口" : phaseText(phase)}</strong>
      <small>{["listening", "preparing"].includes(phase) && endpoint.active ? endpoint.deadlineAt == null
        ? "自然表达即可。需要思考、想换个话题或已经讲完，都可以直接告诉面试官。"
        : "说完后稍等片刻，就能查看试音字幕。"
        : phaseDetail(phase, formal)}</small>
    </span>
    <span className={`conversation-phase is-${phase}`} aria-hidden="true" />
  </div>;
}

export function AnswerRecoveryAction({ recovery, act, disabled = false }) {
  const pending = useRef(false);
  const [sending, setSending] = useState(false);
  const retry = async () => {
    if (disabled || pending.current || recovery.retryPending) return;
    pending.current = true;
    setSending(true);
    try { await act("continue_speaking"); }
    finally { pending.current = false; setSending(false); }
  };
  return <div className="recording-actions">
    <button className="button button-primary" type="button" disabled={disabled || sending || recovery.retryPending} onClick={retry}>
      {sending || recovery.retryPending ? "正在重试…" : "重试本题"}
    </button>
  </div>;
}

export function CandidateSignalList({ experience, blocked = false }) {
  const stopped = blocked || ["paused", "completed", "answer_retry_required", "understanding", "planning"].includes(experience.phase);
  const recovering = experience.captureRecovery?.status === "recovering" && !stopped;
  const local = !stopped && experience.microphone.localDetected;
  const stoppedDetail = blocked ? "收音已停止，请查看页面提示"
    : experience.phase === "paused" ? "面试已暂停"
    : experience.phase === "completed" ? "面试已结束"
    : experience.phase === "answer_retry_required" ? "本题收音已停止，请重试本题"
    : experience.calibration?.status === "awaiting_confirmation" ? "试音已收好，请确认下方字幕"
    : experience.calibration?.status === "confirming" ? "正在进入正式面试"
    : experience.phase === "planning" ? "面试官正在准备下一话题"
    : "这段回答已收好，面试官正在整理";
  return <div className="candidate-signal-list">
    <SignalState label={recovering ? "连接有些慢，正在恢复" : "麦克风"} active={local && !recovering}
      detail={stopped ? stoppedDetail : recovering ? "请稍等，已收到的回答会保留" : local ? "正在收到你的声音" : "等待你开口"}>
      <span className="microphone-level" aria-hidden="true"><i style={{ width: `${stopped ? 0 : Math.round(experience.microphone.level * 100)}%` }} /></span>
    </SignalState>
  </div>;
}

function SignalState({ label, active, detail, children }) {
  return <div className={`candidate-signal-state${active ? " is-active" : ""}`} role="status" aria-live="polite">
    <span className="signal-state-dot" />
    <span><strong>{label}</strong><small>{detail}</small>{children}</span>
  </div>;
}

function networkReportReady(report) {
  return Number.isFinite(report?.network_rtt_ms) && report.network_rtt_ms >= 0 && report.network_rtt_ms <= 500
    && Number.isFinite(report?.network_jitter_ms) && report.network_jitter_ms >= 0 && report.network_jitter_ms <= 100;
}

function networkCheckErrorMessage(code) {
  if (code === "NETWORK_CHECK_TIMEOUT") return "连接检测超时，请确认面试服务可访问后重新检测。";
  if (code === "NETWORK_CHECK_FAILED") return "未能连接面试服务，请检查连接后重新检测。";
  if (code === "NETWORK_CHECK_UNAVAILABLE") return "当前无法执行网络检测，请重新打开页面后再试。";
  return null;
}

function networkCheckDetail(report, state) {
  if (state.status === "checking") return "正在检测本机到面试服务的连接，请稍候。";
  if (state.status === "failed" && state.reason === "entry_timeout") return "入场请求超时，之前的检测结果已失效，请重新检测。";
  if (state.status === "failed") return state.reason === "timeout"
    ? "连接检测超时，请确认面试服务可访问后重新检测。"
    : "未能连接面试服务，请检查连接后重新检测。";
  if (!Number.isFinite(report?.network_rtt_ms) || !Number.isFinite(report?.network_jitter_ms)) {
    return "尚未取得有效检测结果，请重新检测。";
  }
  const metrics = `响应 ${report.network_rtt_ms} ms · 波动 ${report.network_jitter_ms} ms`;
  if (networkReportReady(report)) return `${metrics}。页面服务连接检测通过，入场条件将在下一步核验。`;
  const reasons = [];
  if (report.network_rtt_ms > 500) reasons.push("服务响应较慢（需 ≤ 500 ms）");
  if (report.network_jitter_ms > 100) reasons.push("响应波动较大（需 ≤ 100 ms）");
  return `${metrics}。${reasons.join("；") || "检测结果无效"}，请重新检测。`;
}

function DeviceCheck({ label, ready, detail }) {
  return <div className={`preflight-check${ready ? " is-ready" : " is-failed"}`}>
    <span aria-hidden="true">{ready ? "✓" : "!"}</span>
    <strong>{label}</strong>
    {detail && <small>{detail}</small>}
  </div>;
}

function PublicShell({ children }) {
  return <div className="candidate-room"><PublicHeader />{children}</div>;
}

function PublicHeader({ children }) {
  return <header className="candidate-header">
    <span className="candidate-brand">
      <span className="brand-mark">I</span>
      <span><strong>Interviewer</strong><small>实时智能面试</small></span>
    </span>
    {children}
  </header>;
}

function phaseText(phase) {
  return ({
    connecting: "正在进入面试",
    preparing: "准备听你回答",
    listening: "正在听你说",
    awaiting_supplement: "还有什么需要补充的吗？",
    answer_preparing: "正在整理你的回答",
    answer_recovering: "连接有些慢，正在恢复",
    answer_retry_required: "这段回答需要重试",
    understanding: "正在整理你的回答",
    planning: "正在准备下一话题",
    responding: "面试官正在回应，可随时开口打断",
    paused: "面试已暂停",
    completed: "面试已完成",
  }[phase] || "请稍等片刻");
}

function phaseDetail(phase, formal) {
  return ({
    connecting: "请稍等片刻。",
    preparing: "准备好后就可以开口了。",
    listening: formal ? "自然表达即可。需要思考、想换个话题或已经讲完，都可以直接告诉面试官。" : "正在识别试音，停止说话后将自动形成试音字幕。",
    awaiting_supplement: "还有想说的可以直接补充，已经讲完也请告诉我；没听清可以让我再问一遍。",
    answer_preparing: "不需要重复确认。若想补充，仍可直接开口，面试官会重新整理后再回应。",
    answer_recovering: "请稍等，已收到的回答会保留。",
    answer_retry_required: "点击“重试本题”后，再回答一次。",
    understanding: "这段回答已收好，请等面试官回应后再说。",
    planning: "这段回答已保留，请稍等面试官继续。",
    responding: "如果想补充，可以直接开口。",
    paused: "请联系面试安排人协助恢复。",
  }[phase] || "请稍等片刻。");
}

function connectionText(connection) {
  if (connection.control === "connected" && ["connected", "recovery"].includes(connection.media)) {
    return connection.media === "recovery" ? "正在恢复连接" : "已连接";
  }
  if (connection.control === "recovering" || connection.media === "reconnecting") return "正在恢复连接";
  return "正在连接";
}

function uniqueActionKey(type) {
  return `${type}:${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
}

function formatAppointmentTime(value) {
  return Number.isNaN(value.getTime())
    ? "待确认"
    : value.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
}

function formatDateTime(value) {
  if (!value) return "已提交";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "已提交" : date.toLocaleString("zh-CN");
}
