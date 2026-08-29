import { useEffect, useState } from "react";
import { createAppointmentAndInvite } from "../../../interviews/appointment.js";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status, formatDate } from "../../core/ui.jsx";

export default function InterviewsPage() {
  const wb = useWorkbench();
  return wb.route.view === "live" ? <LiveInterview /> : <InterviewList />;
}

function InterviewList() {
  const { data, auth, navigate, refresh, openModal, closeModal, request, API, toast } = useWorkbench();
  const canManage = auth?.roles?.some((role) => ["admin", "interviewer"].includes(role));
  const create = () => openModal({ title: "创建面试预约", body: <AppointmentForm data={data} request={request} API={API} onDone={async (result) => { await refresh("appointments"); closeModal(); if (result.inviteError) { openModal({ title: "预约已保留，邀请待重试", body: <RetryInvite appointment={result.appointment} /> }); toast("预约已保留", result.inviteError.message, "error"); } else { openModal({ title: "候选人邀请", body: <InvitationLink invitation={result.invitation} /> }); } }} /> });
  return <><section className="page-header"><div><h1>面试会话</h1><p>{data.interviews.length} 场面试</p></div><div className="page-actions"><button className="button button-secondary" onClick={() => refresh("interviews")}>刷新</button>{canManage && <button className="button button-primary" onClick={create} disabled={!data.plans.some((item) => item.status === "approved")}>创建预约</button>}</div></section>{data.interviews.length ? <div className="interview-list">{data.interviews.map((item) => <article className="interview-card" key={item.id}><div><strong>{item.candidate?.name || item.id}</strong><span>{formatDate(item.created_at)}</span></div><Status value={item.status} /><button className="button button-secondary" onClick={() => navigate("interviews", item.id)}>查看</button></article>)}</div> : <Empty title="暂无面试会话" copy="生成可用计划后创建预约" />}</>;
}

function AppointmentForm({ data, request, API, onDone }) {
  const approved = data.plans.filter((item) => item.status === "approved" && item.candidate_profile_id && item.job_position_id);
  return <ModalForm submitLabel="创建预约并生成邀请" onSubmit={async (form) => { const plan = approved.find((item) => item.id === form.get("plan_id")); const start = new Date(form.get("scheduled_start_at")); const end = new Date(form.get("scheduled_end_at")); if (!plan || start >= end) throw new Error("请选择计划并确保结束时间晚于开始时间"); const result = await createAppointmentAndInvite({ createAppointment: () => request(`${API}/interview-appointments`, { method: "POST", body: { plan_id: plan.id, candidate_profile_id: plan.candidate_profile_id, job_position_id: plan.job_position_id, scheduled_start_at: start.toISOString(), scheduled_end_at: end.toISOString(), settings: { record_audio: true, record_video: false, avatar_id: "avatar_default_cn" } } }), issueInvitation: (appointment) => request(`${API}/interview-appointments/${appointment.id}/invite`, { method: "POST", body: { expires_at: end.toISOString() } }) }); await onDone(result); }}><Field label="可用计划" full><select className="form-select" name="plan_id">{approved.map((plan) => <option key={plan.id} value={plan.id}>{data.candidates.find((item) => item.id === plan.candidate_profile_id)?.name || plan.id}</option>)}</select></Field><Field label="开始"><input className="form-input" name="scheduled_start_at" type="datetime-local" required /></Field><Field label="结束"><input className="form-input" name="scheduled_end_at" type="datetime-local" required /></Field></ModalForm>;
}

function RetryInvite({ appointment }) {
  const { API, request, openModal, toast } = useWorkbench();
  const retry = async () => { try { const invitation = await request(`${API}/interview-appointments/${appointment.id}/invite`, { method: "POST", body: { expires_at: appointment.scheduled_end_at } }); openModal({ title: "候选人邀请", body: <InvitationLink invitation={invitation} /> }); } catch (error) { toast("邀请仍未就绪", error.message, "error"); } };
  return <div><p>预约已经安全保存，不会重复创建。readiness 就绪后可重试。</p><button className="button button-primary" onClick={retry}>重试签发邀请</button></div>;
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
  return <Field label="候选人邀请链接" full><div className="invitation-link-row"><input className="form-input" readOnly value={url} aria-label="候选人邀请链接" /><button className="button button-primary" type="button" onClick={copy}>{copied ? "已复制" : "复制链接"}</button></div></Field>;
}

function LiveInterview() {
  const { API, data, auth, request, setResource, refresh, navigate, toast } = useWorkbench();
  const interview = data.selectedInterview;
  const [socketState, setSocketState] = useState("未连接");
  const [transcript, setTranscript] = useState("");
  const [evaluation, setEvaluation] = useState(null);
  const canManage = auth?.roles?.some((role) => ["admin", "interviewer"].includes(role));
  useEffect(() => {
    if (!interview || !canManage || interview.status !== "in_progress") return undefined;
    let socket; let active = true;
    request(`${API}/auth/websocket-ticket`, { method: "POST", body: { interview_id: interview.id } }).then(({ ticket }) => {
      if (!active) return; const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${location.host}${API}/interviews/${interview.id}/live?ticket=${encodeURIComponent(ticket)}`);
      socket.addEventListener("open", () => { setSocketState("已连接"); socket.send(JSON.stringify({ type: "session.ready", payload: { source: "react_console" } })); });
      socket.addEventListener("message", async ({ data: raw }) => { const event = JSON.parse(raw); if (event.type.startsWith("stt.transcript")) setTranscript(event.payload?.text || ""); if (event.type === "evaluation.completed") setEvaluation(event.payload); if (["session.state.changed", "interview.completed"].includes(event.type)) setResource("selectedInterview", await request(`${API}/interviews/${interview.id}`)); });
      socket.addEventListener("close", () => setSocketState("已断开"));
    }).catch((error) => setSocketState(error.message));
    return () => { active = false; socket?.close(); };
  }, [interview?.id, interview?.status, canManage]);
  if (!interview) return <Empty title="面试不可用" copy="未找到指定会话" />;
  const current = interview.turns?.find((item) => item.id === interview.current_turn_id);
  const command = async (action) => { const result = await request(`${API}/interviews/${interview.id}/${action}`, { method: "POST", body: { reason: `react ${action}` } }); setResource("selectedInterview", result); await refresh("interviews"); toast("状态已更新", result.status); };
  const complete = async () => { const result = await request(`${API}/interviews/${interview.id}/complete`, { method: "POST" }); setResource("selectedInterview", result.interview); setResource("report", result.report); await refresh("interviews"); };
  return <><section className="workspace-header"><div className="workspace-title"><button className="icon-button" onClick={() => navigate("interviews")}>←</button><div><h1>{interview.candidate?.name || "候选人"}</h1><span>{socketState}</span></div></div><div className="workspace-status"><Status value={interview.status} />{canManage && interview.status === "in_progress" && <button className="button button-secondary" onClick={() => command("pause")}>暂停</button>}{canManage && interview.status === "paused" && <button className="button button-primary" onClick={() => command("recover")}>恢复</button>}{canManage && ["scheduled", "waiting", "in_progress", "paused"].includes(interview.status) && <button className="button button-secondary" onClick={() => command("cancel")}>取消</button>}</div></section><div className="workspace-grid"><section><div className="avatar-stage"><img src="/web/assets/digital-interviewer.png" alt="数字人面试官" /></div>{data.report && <Report report={data.report} />}{evaluation && <pre>{JSON.stringify(evaluation, null, 2)}</pre>}</section><aside className="session-panel"><div className="question-panel"><h2>{current?.question_spoken_text || "当前没有待答题目"}</h2>{transcript && <p>{transcript}</p>}{canManage && current && <button className="button button-secondary" onClick={() => command("skip")}>跳过本题</button>}{canManage && !current && interview.status === "in_progress" && <button className="button button-primary" onClick={complete}>完成并生成报告</button>}</div><div className="timeline">{interview.turns?.map((turn) => <div key={turn.id}><Status value={turn.status} /><span>第 {turn.order} 题</span></div>)}</div></aside></div></>;
}

function Report({ report }) { return <section className="report-panel"><h2>面试报告</h2><strong className="score-value">{report.overall_score}</strong><p>{report.recommendation}</p><p>最终决定由企业人员完成。</p></section>; }
