import { useEffect, useMemo, useRef, useState } from "react";

import { requestWithLatestVersion, semanticIdentity } from "../../../core/concurrency.js";
import { createAppointmentAndInvite } from "../../../interviews/appointment.js";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status, formatDate } from "../../core/ui.jsx";
import { createEnterpriseInterviewMonitor } from "./agent-monitor.js";
import InterviewReview, { useInterviewReview } from "./Review.jsx";

export default function InterviewsPage() {
  const wb = useWorkbench();
  return wb.route.view === "live" ? <LiveInterview /> : <InterviewList />;
}

const INTERVIEW_STATUS_FILTERS = [
  { value: "all", label: "全部状态" },
  { value: "active", label: "正在面试" },
  { value: "scheduled", label: "待开始" },
  { value: "paused", label: "已暂停" },
  { value: "processing", label: "评分 / 报告处理中" },
  { value: "report_ready", label: "报告就绪" },
  { value: "expired", label: "已超时结束" },
  { value: "cancelled", label: "已取消" },
  { value: "failed", label: "处理失败" },
];

function interviewDisplayStatus(item) {
  if (item.status === "cancelled" && item.termination_reason === "appointment_window_expired") return "expired";
  if (item.status === "in_progress" && item.candidate_input_completed_at) return "scoring_pending";
  return item.status;
}

function interviewMatchesStatus(status, filter) {
  if (filter === "all") return true;
  if (filter === "active") return status === "in_progress";
  if (filter === "scheduled") return ["scheduled", "waiting"].includes(status);
  if (filter === "processing") return ["scoring_pending", "evaluating", "completed", "report_generating"].includes(status);
  if (filter === "failed") return ["processing_failed", "report_failed", "failed"].includes(status);
  return status === filter;
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
  const [candidateQuery, setCandidateQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const normalizedQuery = candidateQuery.trim().toLocaleLowerCase("zh-CN");
  const filteredInterviews = useMemo(() => data.interviews.filter((item) => {
    const candidateName = String(item.candidate?.name || item.id).toLocaleLowerCase("zh-CN");
    return candidateName.includes(normalizedQuery)
      && interviewMatchesStatus(interviewDisplayStatus(item), statusFilter);
  }), [data.interviews, normalizedQuery, statusFilter]);
  const hasFilters = Boolean(normalizedQuery || statusFilter !== "all");
  const removeIdentity = semanticIdentity(["id", "status", "candidate_input_completed_at", "current_report_id", "list_removed_at"]);
  const remove = (item) => {
    const candidateName = item.candidate?.name || item.id;
    const path = `${API}/interviews/${encodeURIComponent(item.id)}`;
    openModal({
      title: `移除 ${candidateName}`,
      body: <ModalForm
        submitLabel="确认移除"
        submitVariant="danger"
        onSubmit={async () => {
          await requestWithLatestVersion({
            request,
            resourcePath: path,
            snapshot: item,
            identity: removeIdentity,
            changedMessage: "这场面试的状态或报告已经变化，请刷新后重新确认移除",
            perform: (latest) => request(`${path}?expected_version=${latest.version}`, { method: "DELETE" }),
          });
          await refresh("interviews");
          closeModal();
          toast("已从面试列表移除", "候选人资料、回答、报告、录音和审计记录仍然保留");
        }}
      >
        <div className="delete-warning field-full">
          <strong>从面试会话列表移除“{candidateName}”？</strong>
          <p>这里只隐藏当前这场面试，不会删除候选人资料、回答、报告、录音或审计记录。</p>
        </div>
      </ModalForm>,
    });
  };
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
      <div><h1>面试会话</h1><p>{hasFilters ? `显示 ${filteredInterviews.length} / 共 ${data.interviews.length} 场面试` : `${data.interviews.length} 场面试`}</p></div>
      <div className="page-actions">
        <button className="button button-secondary" onClick={() => refresh("interviews")}>刷新</button>
        {canManage && <button
          className="button button-primary"
          onClick={create}
          disabled={!data.plans.some((item) => item.status === "approved")}
        >创建预约</button>}
      </div>
    </section>
    {data.interviews.length ? <>
      <section className="interview-list-toolbar" aria-label="筛选面试会话">
        <label className="interview-search-field">
          <span className="interview-search-icon" aria-hidden="true">
            <svg viewBox="0 0 20 20" focusable="false"><circle cx="8.5" cy="8.5" r="5.25" /><path d="m12.5 12.5 4 4" /></svg>
          </span>
          <input
            className="form-input"
            type="search"
            aria-label="搜索候选人姓名"
            placeholder="搜索候选人姓名"
            value={candidateQuery}
            onChange={(event) => setCandidateQuery(event.target.value)}
          />
        </label>
        <select
          className="form-select interview-status-filter"
          aria-label="按面试状态筛选"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
        >
          {INTERVIEW_STATUS_FILTERS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        {hasFilters && <button
          className="button button-ghost button-small interview-filter-reset"
          type="button"
          onClick={() => { setCandidateQuery(""); setStatusFilter("all"); }}
        >清除筛选</button>}
        <span className="interview-filter-summary" aria-live="polite">{filteredInterviews.length} 条结果</span>
      </section>
      {filteredInterviews.length ? <section className="interview-list-panel" aria-label="面试会话列表">
        <div className="interview-list-head" aria-hidden="true">
          <span>候选人</span><span>创建时间</span><span>状态</span><span>操作</span>
        </div>
        <div className="interview-list">
          {filteredInterviews.map((item) => {
            const candidateName = item.candidate?.name || item.id;
            const removable = ["cancelled", "report_ready"].includes(item.status);
            const status = interviewDisplayStatus(item);
            return <article className="interview-row" key={item.id}>
              <div className="candidate-cell">
                <span className="candidate-avatar" aria-hidden="true">{candidateInitial(candidateName)}</span>
                <span className="candidate-copy">
                  <strong>{candidateName}</strong>
                  <small>会话 {interviewReference(item.id)}</small>
                </span>
              </div>
              <time className="interview-time" dateTime={item.created_at || undefined}>{formatDate(item.created_at)}</time>
              <Status value={status} />
              <div className="interview-actions">
                <button className="button button-ghost button-small interview-view-action" onClick={() => navigate("interviews", item.id)}>
                  查看详情 <span aria-hidden="true">→</span>
                </button>
                {canManage && <span className="interview-secondary-action">
                  {removable
                    ? <InterviewMoreMenu candidateName={candidateName} onRemove={() => remove(item)} />
                    : item.status === "in_progress" && !item.candidate_input_completed_at
                      ? <span className="interview-action-state">正在面试中</span>
                      : null}
                </span>}
              </div>
            </article>;
          })}
        </div>
      </section> : <Empty title="没有匹配的面试" copy="换个候选人姓名或状态试试，也可以清除筛选条件" />}
    </> : <Empty title="暂无面试会话" copy="生成可用计划后创建预约" />}
  </>;
}

function candidateInitial(name) {
  return Array.from(String(name || "候").trim())[0] || "候";
}

function interviewReference(id) {
  const value = String(id || "").replace(/^iv_/, "");
  return value ? value.slice(0, 8).toUpperCase() : "-";
}

function InterviewMoreMenu({ candidateName, onRemove }) {
  const close = (event) => event.currentTarget.closest("details")?.removeAttribute("open");
  return <details
    className="interview-action-menu"
    onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.removeAttribute("open"); }}
    onKeyDown={(event) => {
      if (event.key === "Escape") {
        event.currentTarget.removeAttribute("open");
        event.currentTarget.querySelector("summary")?.focus();
      }
    }}
  >
    <summary className="interview-action-trigger" role="button" aria-haspopup="menu" aria-label={`${candidateName}的更多操作`}>•••</summary>
    <div className="interview-action-popover" role="menu">
      <button type="button" role="menuitem" onClick={(event) => { close(event); onRemove(); }}>移出列表</button>
    </div>
  </details>;
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
  const { review, error: reviewError, reload: reloadReview } = useInterviewReview(interview?.id);
  const submitted = Boolean(interview?.candidate_input_completed_at || review?.processing?.submitted_at);
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
    if (!interview?.id || submitted || !["in_progress", "paused"].includes(interview.status)) return undefined;
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
  }, [auth?.actor_id, canManage, interview?.id, interview?.status, submitted, monitor]);

  useEffect(() => {
    if (candidateMediaRef.current && monitorState.candidateStream) {
      candidateMediaRef.current.srcObject = monitorState.candidateStream;
    }
  }, [monitorState.candidateStream]);

  if (!interview) return <Empty title="面试不可用" copy="未找到指定会话" />;

  const current = interview.turns?.find((item) => item.id === interview.current_turn_id);
  const persistedStatus = review?.status || monitorState.session?.status || interview.status;
  const effectiveStatus = persistedStatus === "cancelled" && interview.termination_reason === "appointment_window_expired"
    ? "expired"
    : persistedStatus;
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
            {submitted ? "回答已提交 · 后台评分与音视频回放" : `控制 ${monitorState.connection.control} · 媒体 ${monitorState.connection.media} · 发言权 ${monitorState.floor}`}
          </span>
        </div>
      </div>
      <div className="workspace-status">
        <Status value={submitted && effectiveStatus === "in_progress" ? (review?.processing?.failed ? "processing_failed" : "scoring_pending") : effectiveStatus} />
        {canManage && !submitted && effectiveStatus === "in_progress" && !activeTakeover && <button
          className="button button-secondary"
          onClick={() => command("pause")}
        >暂停</button>}
        {canManage && !submitted && effectiveStatus === "paused" && !activeTakeover && <button
          className="button button-primary"
          onClick={() => command("recover")}
        >人工确认后恢复 AI</button>}
        {canManage && !submitted && ["scheduled", "waiting", "in_progress", "paused"].includes(effectiveStatus) && <button
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

    {!submitted && !["report_ready", "completed", "report_failed", "report_generating", "cancelled", "expired"].includes(effectiveStatus) && <div className="workspace-grid enterprise-live-grid">
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
          <span><small>私有存储</small><strong>{capture?.status === "completed" && capture?.content_hash ? "完整性校验通过" : "尚未完成"}</strong></span>
        </div>

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
    </div>}
    <InterviewReview key={interview.id} interviewId={interview.id} review={review} error={reviewError} reload={reloadReview} canManage={auth?.roles?.some((role) => ["admin", "reviewer"].includes(role))} />
  </>;
}
