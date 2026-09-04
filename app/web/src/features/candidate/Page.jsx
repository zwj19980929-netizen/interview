import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, Status } from "../../core/ui.jsx";
import {
  createCandidateInterviewExperience,
  rememberPreflightReport,
  reportCandidateRuntimeProblem,
} from "./agent-experience.js";
import {
  peekPreparedCandidateMedia,
  prepareCandidateMedia,
  recordPreparedAvatarFps,
  runCandidatePreflight,
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
  const { route } = useWorkbench();
  return route.view === "invite" ? <InvitationPage /> : <CandidateRoom />;
}

function InvitationPage() {
  const { API, data, request, route, toast } = useWorkbench();
  const invitation = data.invitation;
  const [busy, setBusy] = useState(false);
  const [entering, setEntering] = useState(false);
  const [confirmed, setConfirmed] = useState(invitation?.status === "registered");
  const [now, setNow] = useState(Date.now());
  const [preflight, setPreflight] = useState(null);
  const [speakerPlayed, setSpeakerPlayed] = useState(false);
  const previewRef = useRef(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (invitation?.status === "registered") setConfirmed(true);
  }, [invitation?.status]);
  useEffect(() => {
    if (previewRef.current && preflight?.stream) {
      previewRef.current.srcObject = preflight.stream;
    }
  }, [preflight]);

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

  const confirm = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const form = new FormData(event.currentTarget);
      await request(`${API}/public/interview-invitations/${encodeURIComponent(token)}/intake`, {
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
      setConfirmed(true);
      toast("预约确认成功", "系统正在准备本次面试语音，并已安排面试前 30 分钟邮件提醒");
    } catch (error) {
      toast("暂时无法确认预约", error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const beginPreflight = async () => {
    setEntering(true);
    try {
      const result = await runCandidatePreflight({ probeUrl: "/healthz" });
      setPreflight(result);
    } catch (error) {
      toast("设备预检未通过", error.message, "error");
    } finally {
      setEntering(false);
    }
  };

  const playSpeakerTest = async () => {
    try {
      await verifySpeaker();
      setSpeakerPlayed(true);
    } catch (error) {
      toast("扬声器检测失败", error.message, "error");
    }
  };

  const finishPreflightAndEnter = async () => {
    if (!preflight?.stream || !speakerPlayed) return;
    setEntering(true);
    try {
      const report = { ...preflight.report, speaker_verified: true };
      prepareCandidateMedia(preflight.stream, report);
      rememberPreflightReport(report);
      const readiness = await request(
        `${API}/public/interview-invitations/${encodeURIComponent(token)}/readiness`,
        { method: "POST", body: report },
      );
      if (!readiness.can_start) {
        throw new Error("摄像头、扬声器、网络、WebRTC、AudioWorklet、WebGL 或模型服务尚未达到正式面试门槛");
      }
      const result = await request(
        `${API}/public/interview-invitations/${encodeURIComponent(token)}/start`,
        { method: "POST" },
      );
      location.href = result.candidate_join_url;
    } catch (error) {
      toast("暂时无法进入面试", error.message, "error");
      setEntering(false);
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
      {confirmed ? <div className="appointment-confirmed">
        <Status value="预约已确认" />
        <h2>身份核验通过</h2>
        <p>我们会在面试开始前 30 分钟向您的登记邮箱发送提醒。到预约时间后，请先完成一次完整设备预检。</p>
        {!preflight ? <button
          className="button button-primary"
          type="button"
          disabled={!canEnter || entering}
          onClick={beginPreflight}
        >
          {entering ? "正在检查设备…" : canEnter ? "检查设备并进入面试" : "尚未到面试时间"}
        </button> : <section className="candidate-preflight" aria-label="设备预检">
          <div className="candidate-preflight-preview">
            <video ref={previewRef} autoPlay muted playsInline aria-label="摄像头真实自拍预览" />
            <span>仅本机预览{requiredScopes.has("video_recording") ? " · 同意后将加密录像" : " · 本场不上传视频"}</span>
          </div>
          <div className="candidate-preflight-checks">
            <h3>设备预检</h3>
            <DeviceCheck label="麦克风与摄像头" ready={preflight.report.microphone_granted && preflight.report.camera_granted} />
            <DeviceCheck label="WebRTC 与 AudioWorklet" ready={preflight.report.webrtc_supported && preflight.report.audio_worklet_supported} />
            <DeviceCheck label="基础 WebGL 渲染探测" ready={preflight.report.webgl_supported && Math.round(preflight.report.avatar_fps) >= 30} detail={`${Math.round(preflight.report.avatar_fps)} FPS`} />
            <DeviceCheck label="入场服务 RTT / 抖动" ready={preflight.report.network_rtt_ms <= 500 && preflight.report.network_jitter_ms <= 100} detail={`${preflight.report.network_rtt_ms} / ${preflight.report.network_jitter_ms} ms`} />
            <p className="form-hint">正式入场时还会用真实 VRM 模型和 LiveKit RTCStats 再做一次失败关闭校验。</p>
            <button className="button button-secondary" type="button" onClick={playSpeakerTest}>
              {speakerPlayed ? "重新播放测试音" : "播放扬声器测试音"}
            </button>
            <button className="button button-primary" type="button" disabled={!speakerPlayed || entering} onClick={finishPreflightAndEnter}>
              {entering ? "正在建立安全会话…" : "我听到了测试音，进入面试"}
            </button>
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
            {invitation.consent?.audio_recording_notice || "我同意录制答题音频用于权威转写、评分与人工复核"}
          </label>}
          {requiredScopes.has("video_recording") && <label className="candidate-consent-check">
            <input type="checkbox" name="video_recording" required />
            {invitation.consent?.video_recording_notice || "我同意加密录制候选人视频轨用于人工复核"}
          </label>}
          <button className="button button-primary" type="submit" disabled={busy}>
            {busy ? "正在核验…" : "核验身份并确认预约"}
          </button>
        </form>
      </>}
    </section>
  </PublicShell>;
}

function CandidateRoom() {
  const { API, data, request, route } = useWorkbench();
  const interview = data.selectedInterview;
  const token = data.candidateToken || route.candidateToken;
  const [experience, setExperience] = useState(EMPTY_EXPERIENCE);
  const [avatarReady, setAvatarReady] = useState(false);
  const [expandedTranscript, setExpandedTranscript] = useState(false);
  const [startingProblem, setStartingProblem] = useState(null);
  const [previewStream] = useState(() => peekPreparedCandidateMedia()?.stream || null);
  const runRef = useRef(null);
  const videoRef = useRef(null);

  const candidateInterview = useMemo(
    () => createCandidateInterviewExperience({ apiBase: API, request }),
    [API, request],
  );
  const lastAvatarMetricAt = useRef(0);
  const avatarLoaded = useCallback((details) => {
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
      setStartingProblem({
        code: error?.code || problemCode,
        message: error.message || String(error),
        recoverable: false,
        pausePending: false,
        pauseConfirmed,
      });
      return { pauseConfirmed };
    } catch {
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
    candidateInterview.open({ interviewId: interview.id, ticket: token })
      .then((run) => {
        if (!active) {
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
    if (!run) return;
    run.act({
      type,
      payload,
      idempotencyKey: uniqueActionKey(type),
      turnId: experience.currentQuestion?.turn_id,
    }).catch((error) => setStartingProblem({
      code: "CANDIDATE_ACTION_FAILED",
      message: error.message || String(error),
      recoverable: true,
    }));
  }, [experience.currentQuestion?.turn_id]);

  const problem = experience.problem || startingProblem;
  const session = experience.session || {};
  const completion = experience.completion;
  const calibration = experience.calibration || EMPTY_EXPERIENCE.calibration;
  const currentQuestion = experience.currentQuestion;
  const totalQuestions = Number(session.total_primary_questions || interview?.turns?.filter((item) => !item.is_followup).length || 0);
  const answered = Number(session.completed_answers || interview?.answers?.length || 0);
  const pauseConfirmed = problem?.pauseConfirmed === true || session.status === "paused";

  if (!interview || !token) {
    return <div className="candidate-room"><PublicHeader /><div className="candidate-loading" role="status">正在读取经授权的面试会话…</div></div>;
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
        <p>谢谢你的参与。系统已生成不可重复提交的回执，企业人员将进行最终审核。</p>
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
        <span>{interview.record_video ? "音视频加密录制" : interview.record_audio !== false ? "仅音频加密录制" : "本场不录制"}</span>
      </div>
    </PublicHeader>
    <main>
      <div className="candidate-stage-grid">
        <Suspense fallback={<div className="candidate-avatar-stage"><div className="vrm-avatar-gate" role="status">正在加载 3D 渲染模块…</div></div>}>
          <VrmAvatar
            apiBase={API}
            interviewId={interview.id}
            candidateSessionToken={token}
            avatar={experience.avatar}
            onReady={avatarLoaded}
            onFps={avatarFpsObserved}
            onFatalProblem={fatalAvatarProblem}
          />
        </Suspense>
        <aside className="candidate-device-panel" aria-label="候选人设备状态">
          <div className="candidate-camera">
            <video ref={videoRef} autoPlay muted playsInline aria-label="候选人真实自拍预览" />
            {!experience.mediaStream && !previewStream && <div className="device-permission" role="status">
              <strong>正在连接已预检的摄像头</strong>
              <span>没有真实预览时不会开始正式问答</span>
            </div>}
            <span className="camera-label">本机自拍预览</span>
          </div>
          <div className="candidate-signal-list">
            <SignalState
              label="本机检测到声音"
              active={experience.microphone.localDetected}
              detail={experience.microphone.localDetected ? "麦克风正在拾音" : "等待你开口"}
            >
              <span className="microphone-level" aria-hidden="true"><i style={{ width: `${Math.round(experience.microphone.level * 100)}%` }} /></span>
            </SignalState>
            <SignalState
              label="服务器收到音频"
              active={experience.serverAudio.received}
              detail={experience.serverAudio.received ? "服务器已确认当前音频帧" : "尚未收到正式音频"}
            />
            <SignalState
              label="正在形成实时字幕"
              active={experience.captions.forming}
              detail={experience.captions.forming ? "服务端正在转写" : "等待有效语音"}
            />
          </div>
          <p className="privacy-note">
            摄像头与麦克风不用于情绪、眼神、人格、诚信或能力推断。人工接管会被审计并明确显示。
          </p>
        </aside>
      </div>

      <section className="candidate-question-band">
        {problem && <div className={`candidate-problem${problem.recoverable ? "" : " is-fatal"}`} role="alert">
          <strong>{problem.recoverable
            ? "实时链路需要注意"
            : problem.pausePending
              ? "正在安全暂停面试"
              : pauseConfirmed
                ? "面试已在服务器暂停，未退回问卷模式"
                : "实时链路不可用，服务器暂停尚未确认"}</strong>
          <span>{problem.message}</span>
          {!problem.recoverable && <small>{pauseConfirmed
            ? "请等待企业面试官监看或接管。"
            : "请停止作答并联系企业面试官确认会话状态。"}</small>}
        </div>}

        {!experience.session ? <CandidateSessionGate problem={problem} /> : calibration.status !== "completed" ? <WarmupPanel calibration={calibration} experience={experience} act={act} /> : <>
          <div className="candidate-question-head">
            <div>
              <p className="eyebrow">{currentQuestion?.is_followup ? "基于你刚才回答的追问" : `正式问题 ${currentQuestion?.order || answered + 1}`}</p>
              <h1>{currentQuestion?.question_text || "面试官正在组织下一句话…"}</h1>
            </div>
            <div className="candidate-progress" aria-label={`已完成 ${answered}，共 ${totalQuestions} 个主问题`}>
              {answered}/{totalQuestions || "—"}
            </div>
          </div>
          <ConversationState phase={experience.phase} endpoint={experience.endpoint} />
          <CaptionPanel
            captions={experience.captions}
            expanded={expandedTranscript}
            onToggle={() => setExpandedTranscript((value) => !value)}
          />
          <div className="candidate-answer-actions">
            <div className="recording-actions">
              <button className="button button-secondary" type="button" onClick={() => act("request_repeat")}>
                请再说一遍
              </button>
              <button className="button button-secondary" type="button" onClick={() => act("continue_speaking")} disabled={!experience.endpoint.active}>
                继续补充
              </button>
            </div>
            <button className="button button-danger" type="button" onClick={() => act("pause")}>暂停面试</button>
          </div>
        </>}
      </section>
    </main>
  </div>;
}

function CandidateSessionGate({ problem }) {
  return <div className="candidate-warmup" role="status">
    <p className="eyebrow">实时安全会话</p>
    <h1>{problem?.recoverable === false ? "会话尚未建立" : "正在建立实时安全会话"}</h1>
    <p>{problem?.recoverable === false
      ? "请按上方状态等待人工处理，不会提前进入自我介绍试音。"
      : "正在核对 WebRTC、服务端录制轨、控制通道和 3D 表达能力。"}</p>
    {!problem && <span className="spinner" aria-hidden="true" />}
  </div>;
}

export function WarmupPanel({ calibration, experience, act }) {
  const awaiting = calibration.status === "awaiting_confirmation";
  const retryable = calibration.status === "retrying"
    && (calibration.retryRequired === true || (
      experience.problem?.recoverable === true
      && experience.problem?.action === "retry_warmup"
    ));
  return <div className="candidate-warmup">
    <p className="eyebrow">不评分试音</p>
    <h1>{awaiting ? "请确认系统是否正确听懂了你" : "请用普通话做一句简短自我介绍，也可以夹带英文技术词"}</h1>
    <p>试音音频仅用于当场校准，确认后删除，不进入答案、评分或报告。</p>
    <ConversationState phase={experience.phase} endpoint={experience.endpoint} />
    <CaptionPanel captions={experience.captions} expanded />
    {(awaiting || retryable) && <div className="candidate-warmup-actions">
      {awaiting && <button className="button button-primary" type="button" onClick={() => act("warmup.confirm")}>字幕正确，开始正式面试</button>}
      <button className="button button-secondary" type="button" onClick={() => act("warmup.retry")}>
        {retryable ? "重新试音" : "听写不对，重新试音"}
      </button>
    </div>}
  </div>;
}

function CaptionPanel({ captions, expanded, onToggle }) {
  const rows = expanded ? captions.full : captions.recent;
  return <div className="candidate-transcript-wrap" aria-live="polite">
    <div className="transcript-toolbar">
      <span>{captions.forming ? "服务端实时字幕形成中" : "最近两行服务端字幕"}</span>
      {onToggle && <button className="button button-ghost button-small" type="button" onClick={onToggle}>
        {expanded ? "收起完整转写" : "展开完整转写"}
      </button>}
    </div>
    <div className={`candidate-live-captions${expanded ? " is-expanded" : ""}`}>
      {rows.length ? rows.map((row, index) => <p key={`${row.at || index}:${row.text}`} className={row.final ? "is-final" : "is-partial"}>{row.text}</p>) : <p className="is-empty">你开口后，服务端识别到的内容会在这里出现。</p>}
    </div>
  </div>;
}

function ConversationState({ phase, endpoint }) {
  return <div className="candidate-waiting-row" role="status" aria-live="polite">
    <span>
      <strong>{phaseText(phase)}</strong>
      <small>{endpoint.active ? "检测到连续静音；若 2.5 秒内没有继续说话，服务端将收口当前回答并判断是否追问。继续说话会取消本次收口，不会直接跳到下一题。" : phaseDetail(phase)}</small>
    </span>
    <span className={`conversation-phase is-${phase}`} aria-hidden="true" />
  </div>;
}

function SignalState({ label, active, detail, children }) {
  return <div className={`candidate-signal-state${active ? " is-active" : ""}`} role="status" aria-live="polite">
    <span className="signal-state-dot" />
    <span><strong>{label}</strong><small>{detail}</small>{children}</span>
  </div>;
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
    connecting: "正在建立实时安全会话",
    preparing: "正在准备语音识别",
    listening: "正在听你说",
    understanding: "回答已识别，正在判断下一步",
    responding: "面试官正在回应，可随时开口打断",
    paused: "面试已暂停",
    completed: "面试已完成",
  }[phase] || "正在同步面试状态");
}

function phaseDetail(phase) {
  return ({
    connecting: "正在核对 WebRTC、控制通道、录制轨和 3D 表达能力。",
    preparing: "服务端正在打开权威语音识别流；就绪前不会误报正在听或提交本地 VAD 信号。",
    listening: "无需点击开始；系统会自动识别你的发言。",
    understanding: "可能生成追问，也可能进入下一道主问题；结果以服务端状态为准。",
    responding: "检测到你开口后，数字人会在 200ms 目标内停止发言。",
    paused: "系统不会退回静态图片和问卷，请等待人工处理。",
  }[phase] || "所有状态均来自统一 InterviewAgentRuntime。");
}

function connectionText(connection) {
  if (connection.control === "connected" && ["connected", "recovery"].includes(connection.media)) {
    return connection.media === "recovery" ? "音频恢复通道" : "实时通道已连接";
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
