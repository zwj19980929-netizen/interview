import { useEffect, useMemo, useRef, useState } from "react";

import { createAppointmentAndInvite } from "../../../interviews/appointment.js";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status, formatDate } from "../../core/ui.jsx";
import { createEnterpriseInterviewMonitor } from "./agent-monitor.js";

export default function InterviewsPage() {
  const wb = useWorkbench();
  return wb.route.view === "live" ? <LiveInterview /> : <InterviewList />;
}

function InterviewList() {
  const {
    data,
    auth,
    navigate,
    refresh,
    openModal,
    closeModal,
    request,
    API,
    toast,
  } = useWorkbench();
  const canManage = auth?.roles?.some((role) => ["admin", "interviewer"].includes(role));
  const create = () => openModal({
    title: "创建面试预约",
    body: <AppointmentForm
      data={data}
      request={request}
      API={API}
      onDone={async (result) => {
        await refresh("appointments");
        closeModal();
        if (result.inviteError) {
          openModal({
            title: "预约已保留，邀请待重试",
            body: <RetryInvite appointment={result.appointment} />,
          });
          toast("预约已保留", result.inviteError.message, "error");
        } else {
          openModal({
            title: "候选人邀请",
            body: <InvitationLink invitation={result.invitation} />,
          });
        }
      }}
    />,
  });
  return <>
    <section className="page-header">
      <div><h1>面试会话</h1><p>{data.interviews.length} 场面试</p></div>
      <div className="page-actions">
        <button className="button button-secondary" onClick={() => refresh("interviews")}>刷新</button>
        {canManage && <button
          className="button button-primary"
          onClick={create}
          disabled={!data.plans.some((item) => item.status === "approved")}
        >创建预约</button>}
      </div>
    </section>
    {data.interviews.length ? <div className="interview-list">
      {data.interviews.map((item) => <article className="interview-card" key={item.id}>
        <div><strong>{item.candidate?.name || item.id}</strong><span>{formatDate(item.created_at)}</span></div>
        <Status value={item.status} />
        <button className="button button-secondary" onClick={() => navigate("interviews", item.id)}>查看</button>
      </article>)}
    </div> : <Empty title="暂无面试会话" copy="生成可用计划后创建预约" />}
  </>;
}

function AppointmentForm({ data, request, API, onDone }) {
  const submitting = useRef(false);
  const savedAppointment = useRef(null);
  const completedResult = useRef(null);
  const [phase, setPhase] = useState(null);
  const approved = data.plans.filter(
    (item) => item.status === "approved" && item.candidate_profile_id && item.job_position_id,
  );
  return <ModalForm
    submitLabel="创建预约并生成邀请"
    onSubmit={async (form) => {
      if (submitting.current) return;
      const plan = approved.find((item) => item.id === form.get("plan_id"));
      const start = new Date(form.get("scheduled_start_at"));
      const end = new Date(form.get("scheduled_end_at"));
      if (!plan || start >= end) throw new Error("请选择计划并确保结束时间晚于开始时间");
      submitting.current = true;
      try {
        const result = completedResult.current || await createAppointmentAndInvite({
          createAppointment: async () => {
            if (savedAppointment.current) return savedAppointment.current;
            setPhase("saving");
            const appointment = await request(`${API}/interview-appointments`, {
              method: "POST",
              body: {
                plan_id: plan.id,
                candidate_profile_id: plan.candidate_profile_id,
                job_position_id: plan.job_position_id,
                scheduled_start_at: start.toISOString(),
                scheduled_end_at: end.toISOString(),
                settings: {
                  record_audio: true,
                  record_video: form.get("record_video") === "on",
                  avatar_mode: form.get("avatar_mode"),
                  speech_dialogue_mode: form.get("speech_dialogue_mode"),
                  avatar_id: "avatar_default_cn",
                },
              },
            });
            savedAppointment.current = appointment;
            return appointment;
          },
          issueInvitation: (appointment) => {
            setPhase("checking");
            return request(
              `${API}/interview-appointments/${appointment.id}/invite`,
              { method: "POST", body: { expires_at: appointment.scheduled_end_at || end.toISOString() }, timeoutMs: 40000 },
            );
          },
        });
        if (result.invitation) completedResult.current = result;
        await onDone(result);
      } finally {
        submitting.current = false;
        setPhase(null);
      }
    }}
  >
    {phase && <p className="form-hint field-full" role="status">{phase === "checking"
      ? "预约已保存，正在检查模型服务并生成邀请，可能需要约 30 秒，请勿重复提交。"
      : "正在保存预约…"}</p>}
    {savedAppointment.current && !phase && <p className="form-hint field-full">预约已保存，重试不会重复创建预约。</p>}
    <Field label="可用计划" full>
      <select className="form-select" name="plan_id">
        {approved.map((plan) => <option key={plan.id} value={plan.id}>
          {data.candidates.find((item) => item.id === plan.candidate_profile_id)?.name || plan.id}
        </option>)}
      </select>
    </Field>
    <Field label="数字人方案" full>
      <select className="form-select" name="avatar_mode" defaultValue="local">
        <option value="local">自研实时 3D 数字人（正式路径）</option>
        <option value="cloud">云数字人 Provider（需单独验收）</option>
      </select>
      <small className="form-hint">
        正式路径必须通过 VRM 授权、15 viseme、TTS 与 WebGL 门禁；任何表达链路故障都会暂停，不会退回静态图片。
      </small>
    </Field>
    <Field label="录制范围" full>
      <label className="candidate-consent-check">
        <input type="checkbox" name="record_video" />
        在音频录制之外，加密录制候选人视频轨（邀请页将单独征得同意）
      </label>
    </Field>
    <Field label="语音追问链路" full>
      <select className="form-select" name="speech_dialogue_mode" defaultValue="cascade">
        <option value="cascade">级联模式（稳定：STT + LLM + TTS）</option>
        <option value="s2s">实时 S2S（低延迟，需配置路由）</option>
      </select>
      <small className="form-hint">
        两种模式共用受控追问与异步评分；S2S 只能表达已批准动作，不作为评分真相源。
      </small>
    </Field>
    <Field label="开始">
      <input className="form-input" name="scheduled_start_at" type="datetime-local" required />
    </Field>
    <Field label="结束">
      <input className="form-input" name="scheduled_end_at" type="datetime-local" required />
    </Field>
  </ModalForm>;
}

function RetryInvite({ appointment }) {
  const { API, request, openModal, toast } = useWorkbench();
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const retry = async () => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    try {
      const invitation = await request(
        `${API}/interview-appointments/${appointment.id}/invite`,
        { method: "POST", body: { expires_at: appointment.scheduled_end_at }, timeoutMs: 40000 },
      );
      openModal({ title: "候选人邀请", body: <InvitationLink invitation={invitation} /> });
    } catch (error) {
      toast("邀请仍未就绪", error.message, "error");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };
  return <div>
    <p>预约已经安全保存，不会重复创建。readiness 就绪后可重试。</p>
    {busy && <p role="status">正在检查模型服务并签发邀请，可能需要约 30 秒，请勿重复点击。</p>}
    <button className="button button-primary" disabled={busy} onClick={retry}>{busy ? "正在检查模型服务…" : "重试签发邀请"}</button>
  </div>;
}

function InvitationLink({ invitation }) {
  const url = `${window.location.origin}${invitation.join_url}`;
  const { toast } = useWorkbench();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      toast("已复制邀请链接", "可直接发送给候选人");
    } catch {
      toast("复制失败", "请允许浏览器访问剪贴板后重试", "error");
    }
  };
  return <Field label="候选人邀请链接" full>
    <div className="invitation-link-row">
      <input className="form-input" readOnly value={url} aria-label="候选人邀请链接" />
      <button className="button button-primary" type="button" onClick={copy}>
        {copied ? "已复制" : "复制链接"}
      </button>
    </div>
  </Field>;
}

const EMPTY_MONITOR = {
  connection: { control: "connecting", media: "connecting" },
  session: null,
  floor: "none",
  takeover: null,
  candidateStream: null,
  captions: [],
  acts: [],
  problem: null,
};

function LiveInterview() {
  const {
    API,
    data,
    auth,
    request,
    setResource,
    refresh,
    navigate,
    toast,
  } = useWorkbench();
  const interview = data.selectedInterview;
  const canManage = auth?.roles?.some((role) => ["admin", "interviewer"].includes(role));
  const [monitorState, setMonitorState] = useState(EMPTY_MONITOR);
  const [takeoverReason, setTakeoverReason] = useState("");
  const [humanTranscript, setHumanTranscript] = useState("");
  const [monitorError, setMonitorError] = useState("");
  const monitorRef = useRef(null);
  const candidateMediaRef = useRef(null);
  const monitor = useMemo(
    () => createEnterpriseInterviewMonitor({ apiBase: API, request }),
    [API, request],
  );

  useEffect(() => {
    if (!interview?.id || !["in_progress", "paused"].includes(interview.status)) return undefined;
    let active = true;
    let unsubscribe = () => {};
    monitor.open({
      interviewId: interview.id,
      actorId: auth?.actor_id,
      canTakeover: canManage,
    }).then((run) => {
      if (!active) {
        run.close();
        return;
      }
      monitorRef.current = run;
      unsubscribe = run.subscribe(setMonitorState);
    }).catch((error) => {
      if (active) setMonitorError(error.message || String(error));
    });
    return () => {
      active = false;
      unsubscribe();
      const run = monitorRef.current;
      monitorRef.current = null;
      run?.close();
    };
  }, [auth?.actor_id, canManage, interview?.id, interview?.status, monitor]);

  useEffect(() => {
    if (candidateMediaRef.current && monitorState.candidateStream) {
      candidateMediaRef.current.srcObject = monitorState.candidateStream;
    }
  }, [monitorState.candidateStream]);

  if (!interview) return <Empty title="面试不可用" copy="未找到指定会话" />;

  const current = interview.turns?.find((item) => item.id === interview.current_turn_id);
  const effectiveStatus = monitorState.session?.status || interview.status;
  const activeTakeover = monitorState.takeover?.status === "active";
  const ownsTakeover = activeTakeover && monitorState.takeover?.actor_id === auth?.actor_id;
  const latestCaption = [...monitorState.captions].reverse().find((item) => item.text);
  const capture = monitorState.session?.media_capture;
  const hasVideo = Boolean(monitorState.candidateStream?.getVideoTracks?.().length);
  const hasAudio = Boolean(monitorState.candidateStream?.getAudioTracks?.().length);

  const command = async (action) => {
    const result = await request(`${API}/interviews/${interview.id}/${action}`, {
      method: "POST",
      body: { reason: `enterprise_console:${action}` },
    });
    setResource("selectedInterview", result);
    await refresh("interviews");
    toast("状态已更新", result.status);
  };
  const complete = async () => {
    const result = await request(`${API}/interviews/${interview.id}/complete`, {
      method: "POST",
    });
    setResource("selectedInterview", result.interview);
    setResource("report", result.report);
    await refresh("interviews");
  };
  const acquire = async () => {
    try {
      await monitorRef.current?.acquireTakeover(takeoverReason);
    } catch (error) {
      toast("无法接管", error.message, "error");
    }
  };
  const release = async () => {
    try {
      await monitorRef.current?.releaseTakeover();
    } catch (error) {
      toast("无法释放接管", error.message, "error");
    }
  };
  const registerHumanSpeech = () => {
    try {
      monitorRef.current?.recordHumanSpeech(humanTranscript);
      setHumanTranscript("");
    } catch (error) {
      toast("人工问话未登记", error.message, "error");
    }
  };

  return <>
    <section className="workspace-header">
      <div className="workspace-title">
        <button className="icon-button" onClick={() => navigate("interviews")}>←</button>
        <div>
          <h1>{interview.candidate?.name || "候选人"}</h1>
          <span>
            控制 {monitorState.connection.control} · 媒体 {monitorState.connection.media} · 发言权 {monitorState.floor}
          </span>
        </div>
      </div>
      <div className="workspace-status">
        <Status value={effectiveStatus} />
        {canManage && effectiveStatus === "in_progress" && !activeTakeover && <button
          className="button button-secondary"
          onClick={() => command("pause")}
        >暂停</button>}
        {canManage && effectiveStatus === "paused" && !activeTakeover && <button
          className="button button-primary"
          onClick={() => command("recover")}
        >人工确认后恢复 AI</button>}
        {canManage && ["scheduled", "waiting", "in_progress", "paused"].includes(effectiveStatus) && <button
          className="button button-secondary"
          onClick={() => command("cancel")}
        >取消</button>}
      </div>
    </section>

    {(monitorError || monitorState.problem) && <div className="enterprise-monitor-alert" role="alert">
      <strong>实时监看链路告警</strong>
      <span>{monitorError || monitorState.problem?.message}</span>
      <small>系统不会用静态图片伪装成实时监看；请暂停或接管。</small>
    </div>}

    <div className="workspace-grid enterprise-live-grid">
      <section>
        <div className="enterprise-candidate-feed">
          <video ref={candidateMediaRef} autoPlay playsInline aria-label="候选人 LiveKit 实时音视频轨" />
          {!hasVideo && <div className="enterprise-feed-empty">
            <strong>{hasAudio ? "候选人音频轨已连接" : "等待候选人媒体轨"}</strong>
            <span>{interview.settings?.record_video ? "录像场次缺少视频时必须暂停处理" : "本场未获视频录制同意，不会上传候选人视频"}</span>
          </div>}
          <div className="enterprise-feed-meta">
            <span>{hasAudio ? "候选人音频在线" : "候选人音频未到达"}</span>
            <button className="button button-small button-secondary" type="button" onClick={() => candidateMediaRef.current?.play()}>
              启用监看声音
            </button>
          </div>
        </div>
        <div className="enterprise-capture-strip">
          <span><small>录像状态</small><strong>{capture?.status || monitorState.session?.recording?.status || "等待媒体发布"}</strong></span>
          <span><small>授权范围</small><strong>{(capture?.consented_scopes || []).join("、") || (interview.record_video ? "音频、视频" : "音频")}</strong></span>
          <span><small>私有存储</small><strong>{capture?.private_uri ? "已校验 hash" : "尚未完成"}</strong></span>
        </div>
        {data.report && <Report report={data.report} />}
      </section>

      <aside className="session-panel enterprise-agent-panel">
        <div className="question-panel">
          <p className="eyebrow">{current?.is_followup ? "受控追问" : "当前问题"}</p>
          <h2>{current?.question_spoken_text || monitorState.session?.current_question?.question_text || "当前没有待答题目"}</h2>
          <div className="enterprise-live-caption" aria-live="polite">
            <small>服务端权威转写</small>
            <p>{latestCaption?.text || "候选人开口后，实时字幕会显示在这里。"}</p>
          </div>
          {canManage && current && !activeTakeover && <button
            className="button button-secondary"
            onClick={() => command("skip")}
          >跳过本题</button>}
          {canManage && !current && effectiveStatus === "in_progress" && <button
            className="button button-primary"
            onClick={complete}
          >完成并生成报告</button>}
        </div>

        <section className="enterprise-takeover-panel">
          <div>
            <h3>审计式人工接管</h3>
            <p>接管会立即中断 AI。lease 为 60 秒并自动续租；丢失或释放后保持暂停，绝不自动恢复 AI。</p>
          </div>
          {!canManage && <Status value="只读监看" />}
          {canManage && !activeTakeover && <>
            <textarea
              className="form-textarea form-textarea-compact"
              value={takeoverReason}
              onChange={(event) => setTakeoverReason(event.target.value)}
              placeholder="填写接管原因（必填并进入审计）"
            />
            <button className="button button-danger" type="button" disabled={!takeoverReason.trim()} onClick={acquire}>
              接管并启用麦克风
            </button>
          </>}
          {activeTakeover && <div className="takeover-lease">
            <strong>{ownsTakeover ? "你正在接管" : "其他企业人员正在接管"}</strong>
            <small>原因：{monitorState.takeover.reason || "已记录"} · 到期：{formatDate(monitorState.takeover.expires_at)}</small>
          </div>}
          {ownsTakeover && <>
            <textarea
              className="form-textarea form-textarea-compact"
              value={humanTranscript}
              onChange={(event) => setHumanTranscript(event.target.value)}
              placeholder="登记刚才的人工问话；该内容标记为 unscored_intervention"
            />
            <div className="enterprise-takeover-actions">
              <button className="button button-secondary" type="button" disabled={!humanTranscript.trim()} onClick={registerHumanSpeech}>
                登记人工问话
              </button>
              <button className="button button-danger" type="button" onClick={release}>
                释放接管并保持暂停
              </button>
            </div>
          </>}
        </section>

        <div className="timeline">
          {interview.turns?.map((turn) => <div key={turn.id}>
            <Status value={turn.status} />
            <span>第 {turn.order} 题{turn.is_followup ? ` · 追问深度 ${turn.followup_depth}` : ""}</span>
          </div>)}
        </div>
      </aside>
    </div>
  </>;
}

function Report({ report }) {
  return <section className="report-panel">
    <h2>面试报告</h2>
    <strong className="score-value">{report.overall_score}</strong>
    <p>{report.recommendation}</p>
    <p>最终决定由企业人员完成。</p>
  </section>;
}
