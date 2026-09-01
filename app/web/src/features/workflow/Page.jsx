import { useEffect, useState } from "react";

import { requestWithLatestVersion, semanticIdentity } from "../../../core/concurrency.js";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status, formatDate } from "../../core/ui.jsx";

const screeningLabels = { qualified: "符合", unqualified: "不符合", manual_review: "待人工复核", processing: "处理中", failed: "处理失败" };
const screeningScorePolicyText = "匹配分标准：0–59 分不符合，60–74 分待人工复核，75–100 分符合；人工复核可覆盖 AI 建议。";
const splitSkills = (value) => String(value || "").split(/[,，、\n]/).map((item) => item.trim()).filter(Boolean);
const candidateIdentity = semanticIdentity(["name", "external_ref", "job_position_id", "email", "phone"]);
const resumeIdentity = semanticIdentity(["id", "candidate_profile_id", "file_name", "source_hash"]);
const reviewCommandIdentity = semanticIdentity(["id", "status", "resume_document_id", "job_position_id", "role_requirement_id", "input_hash"]);

export default function WorkflowPage() {
  const { API, data, request, reloadRoute, openModal, closeModal, toast, navigate } = useWorkbench();
  const hasProcessingCandidate = data.candidates.some((candidate) => (
    candidate.screening?.effective_outcome === "processing"
    || ["queued", "processing"].includes(candidate.screening?.question_generation_status)
  ));
  useEffect(() => {
    if (!hasProcessingCandidate) return undefined;
    const timer = window.setInterval(() => { reloadRoute("candidates"); }, 2500);
    return () => window.clearInterval(timer);
  }, [hasProcessingCandidate, reloadRoute]);
  const finish = async (message) => { await reloadRoute("positions", "knowledgeBases", "questions", "candidates", "appointments", "roles"); closeModal(); toast("操作完成", message); };
  const positionForm = () => openModal({ title: "新建岗位与岗位要求", body: <ModalForm submitLabel="创建岗位" onSubmit={async (form) => { const item = await request(`${API}/job-positions`, { method: "POST", body: { code: form.get("code"), name: form.get("name"), description: form.get("description"), initial_requirement: { title: form.get("requirement_title"), description: form.get("requirement_description"), must_have_skills: splitSkills(form.get("must_have_skills")), nice_to_have_skills: splitSkills(form.get("nice_to_have_skills")), seniority: form.get("seniority"), interview_duration_minutes: Number(form.get("interview_duration_minutes")) } } }); await finish(`${item.name} 和首版岗位要求已创建，可直接用于简历初筛`); }}><p className="form-intro field-full">岗位要求会和岗位一起创建，后续上传简历时可直接选择它进行初筛。</p><Field label="岗位编码"><input className="form-input" name="code" required /></Field><Field label="岗位名称"><input className="form-input" name="name" required /></Field><Field label="岗位说明" full><textarea className="form-textarea" name="description" placeholder="介绍团队、业务方向和岗位定位" /></Field><Field label="要求版本名称"><input className="form-input" name="requirement_title" defaultValue="首版岗位要求" required /></Field><Field label="目标级别"><select className="form-select" name="seniority" defaultValue="mid"><option value="junior">初级</option><option value="mid">中级</option><option value="senior">高级</option><option value="expert">专家</option></select></Field><Field label="岗位职责与要求" full><textarea className="form-textarea" name="requirement_description" placeholder="填写工作职责、经验要求、技术能力和业务场景" required /></Field><Field label="必备技能" hint="用逗号分隔"><input className="form-input" name="must_have_skills" placeholder="Python，FastAPI，PostgreSQL" required /></Field><Field label="加分技能" hint="用逗号分隔"><input className="form-input" name="nice_to_have_skills" placeholder="Redis，Kubernetes" /></Field><Field label="建议面试时长（分钟）"><input className="form-input" name="interview_duration_minutes" type="number" min="5" max="480" defaultValue="45" required /></Field></ModalForm> });
  const roleForm = (position) => openModal({ title: `为 ${position.name} 新增岗位要求`, body: <ModalForm submitLabel="保存岗位要求" onSubmit={async (form) => { await request(`${API}/job-positions/${encodeURIComponent(position.id)}/role-requirements`, { method: "POST", body: { title: form.get("requirement_title"), description: form.get("requirement_description"), must_have_skills: splitSkills(form.get("must_have_skills")), nice_to_have_skills: splitSkills(form.get("nice_to_have_skills")), seniority: form.get("seniority"), interview_duration_minutes: Number(form.get("interview_duration_minutes")) } }); await finish(`${position.name} 的岗位要求已保存，可用于简历初筛`); }}><p className="form-intro field-full">为已有岗位补充首版要求，或新增一个招聘批次的要求版本。</p><Field label="要求版本名称"><input className="form-input" name="requirement_title" defaultValue={`${position.name} 岗位要求`} required /></Field><Field label="目标级别"><select className="form-select" name="seniority" defaultValue="mid"><option value="junior">初级</option><option value="mid">中级</option><option value="senior">高级</option><option value="expert">专家</option></select></Field><Field label="岗位职责与要求" full><textarea className="form-textarea" name="requirement_description" placeholder="填写工作职责、经验要求、技术能力和业务场景" required /></Field><Field label="必备技能" hint="用逗号分隔"><input className="form-input" name="must_have_skills" placeholder="Python，FastAPI，PostgreSQL" required /></Field><Field label="加分技能" hint="用逗号分隔"><input className="form-input" name="nice_to_have_skills" placeholder="Redis，Kubernetes" /></Field><Field label="建议面试时长（分钟）"><input className="form-input" name="interview_duration_minutes" type="number" min="5" max="480" defaultValue="45" required /></Field></ModalForm> });
  const assignedTo = (position, knowledgeBase) => knowledgeBase.job_position_id === position.id || position.knowledge_base_ids?.includes(knowledgeBase.id);
  const voiceLabel = (knowledgeBase) => knowledgeBase.speech_profile?.voice_profile_id || knowledgeBase.voice_profile_id || "待配置音色";
  const kbForm = (position) => {
    const assigned = data.knowledgeBases.filter((knowledgeBase) => assignedTo(position, knowledgeBase));
    const available = data.knowledgeBases.filter((knowledgeBase) => !assignedTo(position, knowledgeBase));
    openModal({ title: `管理 ${position.name} 的题库`, body: <ModalForm submitLabel="确认关联" submitDisabled={!available.length} onSubmit={async (form) => { const knowledgeBaseId = form.get("knowledge_base_id"); if (!knowledgeBaseId) throw new Error("请选择题库"); await request(`${API}/job-positions/${encodeURIComponent(position.id)}/knowledge-base-assignments`, { method: "POST", body: { knowledge_base_id: knowledgeBaseId, expected_position_version: position.version } }); await finish("题库已关联，题目和读题音色均沿用题库配置"); }}><p className="form-intro field-full">一个题库可以关联多个岗位；同一岗位不会重复关联同一题库。关联后直接使用该题库已有题目、语音模型和音色。</p>{assigned.length ? <div className="field-full"><strong>已关联题库</strong><div className="tag-list">{assigned.map((knowledgeBase) => <span className="tag" key={knowledgeBase.id}>{knowledgeBase.name} · {voiceLabel(knowledgeBase)}</span>)}</div></div> : <p className="field-full">当前岗位尚未关联题库。</p>}{available.length ? <Field label="继续关联组织题库" full><select className="form-select" name="knowledge_base_id" required defaultValue=""><option value="" disabled>请选择题库</option>{available.map((knowledgeBase) => <option key={knowledgeBase.id} value={knowledgeBase.id}>{knowledgeBase.name} · 音色 {voiceLabel(knowledgeBase)}</option>)}</select></Field> : <div className="form-intro field-full"><p>{data.knowledgeBases.length ? "组织内现有题库均已关联到这个岗位，没有可重复添加的题库。" : "组织内还没有题库，请先创建题库。"}</p><button type="button" className="button button-secondary button-small" onClick={() => { closeModal(); navigate("questions"); }}>前往题库管理</button></div>}</ModalForm> });
  };
  const candidateForm = () => openModal({ title: "录入候选人", body: <ModalForm onSubmit={async (form) => { const item = await request(`${API}/candidate-profiles`, { method: "POST", body: { job_position_id: form.get("job_position_id"), name: form.get("name"), email: form.get("email"), phone: form.get("phone"), external_ref: form.get("external_ref") || null } }); await finish(`${item.name} 已录入`); }}><Field label="应聘岗位" full><select className="form-select" name="job_position_id" required defaultValue=""><option value="" disabled>请选择岗位</option>{data.positions.map((position) => <option key={position.id} value={position.id}>{position.name}</option>)}</select></Field><Field label="姓名"><input className="form-input" name="name" required /></Field><Field label="外部编号"><input className="form-input" name="external_ref" /></Field><Field label="邮箱"><input className="form-input" name="email" type="email" required /></Field><Field label="手机号"><input className="form-input" name="phone" required /></Field></ModalForm> });
  const editPosition = (position) => openModal({ title: `编辑 ${position.name}`, body: <ModalForm submitLabel="保存修改" onSubmit={async (form) => { await request(`${API}/job-positions/${encodeURIComponent(position.id)}`, { method: "PATCH", body: { expected_version: position.version, name: form.get("name"), description: form.get("description") } }); await finish("岗位已更新"); }}><Field label="岗位名称"><input className="form-input" name="name" defaultValue={position.name} required /></Field><Field label="岗位说明" full><textarea className="form-textarea" name="description" defaultValue={position.description || ""} /></Field></ModalForm> });
  const deletePosition = async (position) => {
    const impact = await request(`${API}/job-positions/${encodeURIComponent(position.id)}/deletion-impact`);
    openModal({ title: `删除岗位 ${position.name}`, body: <ModalForm submitLabel="永久删除岗位及候选人" submitVariant="danger" onSubmit={async (form) => { const confirmation = String(form.get("confirmation") || "").trim(); if (confirmation !== position.name) throw new Error(`请输入完整岗位名称“${position.name}”`); await request(`${API}/job-positions/${encodeURIComponent(position.id)}`, { method: "DELETE", body: { expected_version: impact.position_version, confirmation } }); await finish("岗位及其私有招聘数据已按规则处理"); }}><div className="delete-warning field-full"><strong>这是不可撤销的敏感数据清除操作</strong><p>将清除其下 {impact.candidate_count} 位候选人的联系方式、简历、录音、转写和评分数据；归档 {impact.role_requirement_count} 条岗位要求、{impact.plan_count} 份计划并取消 {impact.appointment_count} 个预约。共享题库不会删除。</p></div><Field label={`请输入完整岗位名称“${position.name}”确认`} full><input className="form-input" name="confirmation" required autoComplete="off" /></Field></ModalForm> });
  };
  const editCandidate = (candidate) => { const path = `${API}/candidate-profiles/${encodeURIComponent(candidate.id)}`; openModal({ title: `编辑 ${candidate.name}`, body: <ModalForm submitLabel="保存修改" onSubmit={async (form) => { const changes = { name: form.get("name"), external_ref: form.get("external_ref") || null }; if (String(form.get("email") || "").trim()) changes.email = form.get("email"); if (String(form.get("phone") || "").trim()) changes.phone = form.get("phone"); await requestWithLatestVersion({ request, resourcePath: path, snapshot: candidate, identity: candidateIdentity, changedMessage: "候选人资料已被修改，请刷新后重新确认", perform: (latest) => request(path, { method: "PATCH", body: { expected_version: latest.version, ...changes } }) }); await finish("候选人资料已更新"); }}><Field label="姓名"><input className="form-input" name="name" defaultValue={candidate.name} required /></Field><Field label="外部编号"><input className="form-input" name="external_ref" defaultValue={candidate.external_ref || ""} /></Field><Field label="新邮箱" hint={`留空则保持 ${candidate.email || "现有邮箱"}`}><input className="form-input" name="email" type="email" /></Field><Field label="新手机号" hint={`留空则保持 ${candidate.phone || "现有手机号"}`}><input className="form-input" name="phone" /></Field></ModalForm> }); };
  const deleteCandidate = (candidate) => { const path = `${API}/candidate-profiles/${encodeURIComponent(candidate.id)}`; openModal({ title: `删除 ${candidate.name}`, body: <ModalForm submitLabel="确认删除" submitVariant="danger" onSubmit={async () => { await requestWithLatestVersion({ request, resourcePath: path, snapshot: candidate, identity: candidateIdentity, changedMessage: "候选人资料已发生变化，请重新确认删除", perform: (latest) => request(`${path}?expected_version=${latest.version}`, { method: "DELETE" }) }); await finish("候选人已从当前列表归档；审计与历史面试快照仍保留"); }}><p className="form-intro field-full">删除后将从候选人列表移除。历史面试与审计记录不会被破坏。</p></ModalForm> }); };
  const resumeForm = (candidate) => openModal({ title: `上传 ${candidate.name} 的简历并初筛`, body: <ResumeForm candidate={candidate} data={data} request={request} API={API} onDone={finish} /> });
  const showCandidate = async (candidate) => {
    const resumes = await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/resumes`);
    openModal({
      title: `${candidate.name} · 初筛与简历`,
      body: <CandidateDetail
        candidate={candidate}
        resumes={resumes.items || []}
        request={request}
        API={API}
        toast={toast}
        onReviewed={finish}
        onResumeChanged={async (message) => { await reloadRoute("candidates"); toast("操作完成", message); }}
        onResumeDeleted={finish}
      />,
    });
  };
  const showResumeQuestions = async (candidate) => {
    const screening = candidate.screening;
    if (!screening?.review_id || screening.effective_outcome !== "qualified") {
      toast("暂无简历问答", "只有初筛结论为符合，或人工复核为符合后，才会生成简历问答", "error");
      return;
    }
    const [questions, review] = await Promise.all([
      request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/experience-questions`),
      request(`${API}/resume-reviews/${encodeURIComponent(screening.review_id)}`),
    ]);
    openModal({
      title: `${candidate.name} · 简历问答`,
      body: <CandidateQuestionBank candidate={candidate} screening={screening} review={review} initialQuestions={questions.items || []} request={request} API={API} toast={toast} />,
    });
  };

  return <>
    <section className="page-header"><div><h1>岗位到复核的业务闭环</h1><p>岗位题库、企业简历、岗位初筛、计划和预约</p></div><div className="page-actions"><button className="button button-secondary" onClick={candidateForm} disabled={!data.positions.length}>录入候选人</button><button className="button button-primary" onClick={positionForm}>新建岗位</button></div></section>
    <section className="panel"><div className="section-title-row"><div><h2>岗位与题库</h2><p>岗位要求用于简历初筛；题目和读题音色由关联题库统一维护</p></div></div>{data.positions.length ? <div className="question-grid">{data.positions.map((position) => { const assigned = data.knowledgeBases.filter((knowledgeBase) => assignedTo(position, knowledgeBase)); const positionRoles = data.roles.filter((role) => role.job_position_id === position.id); return <article className="question-card" key={position.id}><div className="question-card-top"><h3>{position.name}</h3><Status value={position.status} /></div><p>{position.description || "暂未填写岗位说明"}</p><div className="tag-list">{positionRoles.length ? <span className="tag">岗位要求 {positionRoles.length} 个版本</span> : <span className="tag">尚无岗位要求</span>}{assigned.map((knowledgeBase) => <span className="tag" key={knowledgeBase.id}>{knowledgeBase.name} · {voiceLabel(knowledgeBase)}</span>)}</div><div className="position-card-actions"><button className="button button-secondary button-small" onClick={() => roleForm(position)}>{positionRoles.length ? "新增要求版本" : "添加岗位要求"}</button><button className="button button-secondary button-small" onClick={() => kbForm(position)}>{assigned.length ? "管理题库" : "关联题库"}</button><PositionActions position={position} onEdit={editPosition} onDelete={deletePosition} /></div></article>; })}</div> : <Empty title="尚无岗位" copy="点击“新建岗位”同时建立首版岗位要求" />}</section>
    <CandidateTable candidates={data.candidates} positions={data.positions} showCandidate={showCandidate} showResumeQuestions={showResumeQuestions} editCandidate={editCandidate} resumeForm={resumeForm} deleteCandidate={deleteCandidate} />
    <section className="panel"><div className="section-title-row"><div><h2>预约</h2><p>计划审批后生成候选人邀请</p></div></div>{data.appointments.length ? <div className="resource-list">{data.appointments.map((item) => <article className="list-card" key={item.id}><strong>{data.candidates.find((candidate) => candidate.id === item.candidate_profile_id)?.name || "候选人"}</strong><span>{formatDate(item.scheduled_start_at)}</span><Status value={item.status} /></article>)}</div> : <Empty title="尚无预约" copy="批准面试计划后创建预约" />}</section>
  </>;
}

function PositionActions({ position, onEdit, onDelete }) {
  const closeMenu = (event) => event.currentTarget.closest("details")?.removeAttribute("open");
  return <details className="position-action-menu" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.removeAttribute("open"); }} onKeyDown={(event) => { if (event.key === "Escape") { event.currentTarget.removeAttribute("open"); event.currentTarget.querySelector("summary")?.focus(); } }}><summary className="position-action-trigger" role="button" aria-haspopup="menu" aria-label={`${position.name}的更多操作`}>•••</summary><div className="position-action-popover" role="menu"><button type="button" role="menuitem" onClick={(event) => { closeMenu(event); onEdit(position); }}>编辑岗位</button><button type="button" role="menuitem" className="is-danger" onClick={(event) => { closeMenu(event); onDelete(position); }}>删除岗位</button></div></details>;
}

function CandidateTable({ candidates, positions, showCandidate, showResumeQuestions, editCandidate, resumeForm, deleteCandidate }) {
  return <section className="panel"><div className="section-title-row"><div><h2>候选人</h2><p>候选人明确归属于应聘岗位；上传简历后完成初筛与人工复核</p><small className="muted">{screeningScorePolicyText}</small></div></div>{candidates.length ? <div className="data-table-wrap"><table className="data-table"><thead><tr><th>姓名</th><th>应聘岗位</th><th>邮箱</th><th>手机</th><th>是否符合岗位要求</th><th></th></tr></thead><tbody>{candidates.map((item) => {
    const purged = item.name === "[retention_purged]";
    const screening = item.screening;
    const questionEligible = screening?.effective_outcome === "qualified";
    const positionName = positions.find((position) => position.id === item.job_position_id)?.name || screening?.job_position_name || "待关联";
    const screeningDetail = screening?.effective_outcome === "processing" ? " · 后台处理中" : screening?.effective_outcome === "failed" ? " · 请查看错误" : screening?.human_review_status === "reviewed" ? " · 已复核" : " · 待复核";
    return <tr key={item.id}><td><strong>{purged ? "已清除候选人" : item.name}</strong>{purged && <small className="cell-subtitle">仅保留审计占位</small>}</td><td>{positionName}</td><td>{item.email || "—"}</td><td>{item.phone || "—"}</td><td>{screening ? <button className={`screening-link screening-${screening.effective_outcome}`} onClick={() => showCandidate(item)}><span>{screeningLabels[screening.effective_outcome] || "待初筛"}</span><small>{screening.job_position_name}{screeningDetail}</small></button> : <button className="screening-link" onClick={() => showCandidate(item)}><span>待初筛</span><small>请上传简历并选择岗位</small></button>}</td><td><div className="table-actions"><button className="button button-secondary button-small" onClick={() => showCandidate(item)} disabled={purged}>查看初筛</button><button className="button button-secondary button-small" onClick={() => showResumeQuestions(item)} disabled={purged || !questionEligible} title={questionEligible ? "查看与简历证据绑定的问题" : "复核为符合后才生成简历问答"}>简历问答{screening?.question_generation_status === "queued" || screening?.question_generation_status === "processing" ? "生成中…" : ""}</button><button className="button button-secondary button-small" onClick={() => resumeForm(item)} disabled={purged}>上传简历</button><CandidateActions candidate={item} onEdit={editCandidate} onDelete={deleteCandidate} disabled={purged} /></div></td></tr>;
  })}</tbody></table></div> : <Empty title="尚无候选人" copy="录入候选人后上传简历并完成岗位初筛" />}</section>;
}

function CandidateActions({ candidate, onEdit, onDelete, disabled }) {
  const closeMenu = (event) => event.currentTarget.closest("details")?.removeAttribute("open");
  return <details className="position-action-menu" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.removeAttribute("open"); }}><summary className="position-action-trigger" role="button" aria-haspopup="menu" aria-label={`${candidate.name}的更多操作`} aria-disabled={disabled}>•••</summary><div className="position-action-popover" role="menu"><button type="button" role="menuitem" disabled={disabled} onClick={(event) => { closeMenu(event); onEdit(candidate); }}>编辑候选人</button><button type="button" role="menuitem" className="is-danger" disabled={disabled} onClick={(event) => { closeMenu(event); onDelete(candidate); }}>删除候选人</button></div></details>;
}

function CandidateDetail({ candidate, resumes, request, API, toast, onReviewed, onResumeChanged, onResumeDeleted }) {
  const screening = candidate.screening;
  const reviewPending = screening && ["processing", "failed"].includes(screening.effective_outcome);
  const [retryingScreening, setRetryingScreening] = useState(false);
  const [resumeItems, setResumeItems] = useState(resumes);
  const [editingResumeId, setEditingResumeId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busyResumeId, setBusyResumeId] = useState("");
  const openResume = async (resume) => { const grant = await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/resumes/${encodeURIComponent(resume.id)}/content-url`, { method: "POST" }); window.open(grant.url, "_blank", "noopener,noreferrer"); };
  const review = async (decision) => { if (!screening) return; const note = document.querySelector('[name="screening_review_note"]')?.value || ""; const path = `${API}/resume-reviews/${encodeURIComponent(screening.review_id)}`; const snapshot = await request(path); await requestWithLatestVersion({ request, resourcePath: path, snapshot, identity: reviewCommandIdentity, changedMessage: "初筛结论已发生变化，请刷新后重新复核", perform: (latest) => request(`${path}/screening-review`, { method: "PATCH", body: { expected_version: latest.version, decision, note } }) }); await onReviewed(decision === "qualified" ? "复核为符合岗位要求，已取消自动清理" : "复核为不符合岗位要求，将按 7 天规则清理"); };
  const retryScreening = async () => {
    if (!screening?.review_id) return;
    setRetryingScreening(true);
    try {
      const path = `${API}/resume-reviews/${encodeURIComponent(screening.review_id)}`;
      const snapshot = await request(path);
      const idempotencyKey = globalThis.crypto?.randomUUID?.() || `${Date.now()}`;
      await requestWithLatestVersion({ request, resourcePath: path, snapshot, identity: reviewCommandIdentity, changedMessage: "初筛任务状态已发生变化，请刷新后重新确认", perform: (latest) => request(`${path}/retry`, { method: "POST", idempotencyKey, body: { expected_version: latest.version, reason: "interviewer_requested_retry" } }) });
      await onReviewed("失败的简历初筛已重新排队，Worker 会从头生成完整结论");
    } catch (error) {
      toast("重试失败", error.message, "error");
    } finally {
      setRetryingScreening(false);
    }
  };
  const startRename = (resume) => { setEditingResumeId(resume.id); setDisplayName(resume.file_name || "候选人简历.pdf"); };
  const renameResume = async (resume) => {
    setBusyResumeId(resume.id);
    try {
      const path = `${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/resumes/${encodeURIComponent(resume.id)}`;
      const updated = await requestWithLatestVersion({ request, resourcePath: path, snapshot: resume, identity: resumeIdentity, changedMessage: "简历内容或名称已发生变化，请刷新后重新确认", perform: (latest) => request(path, { method: "PATCH", body: { expected_version: latest.version, display_name: displayName } }) });
      setResumeItems((items) => items.map((item) => item.id === updated.id ? updated : item));
      setEditingResumeId("");
      await onResumeChanged("简历名称已更新；PDF 内容和历史版本没有变化");
    } catch (error) {
      toast("更新失败", error.message, "error");
    } finally {
      setBusyResumeId("");
    }
  };
  const deleteResume = async (resume) => {
    if (!window.confirm(`确认删除简历“${resume.file_name || "候选人简历.pdf"}”？待处理任务和私有文件会一并清理。`)) return;
    setBusyResumeId(resume.id);
    try {
      const path = `${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/resumes/${encodeURIComponent(resume.id)}`;
      await requestWithLatestVersion({ request, resourcePath: path, snapshot: resume, identity: resumeIdentity, changedMessage: "简历内容或名称已发生变化，请重新确认删除", perform: (latest) => request(`${path}?expected_version=${latest.version}`, { method: "DELETE" }) });
      setResumeItems((items) => items.filter((item) => item.id !== resume.id));
      await onResumeDeleted("简历、待处理任务和私有文件已删除；面试历史引用的简历不会允许删除");
    } catch (error) {
      toast("删除失败", error.message, "error");
    } finally {
      setBusyResumeId("");
    }
  };
  return <div className="candidate-detail">
    <div className="candidate-detail-summary"><div><small>候选人</small><strong>{candidate.name}</strong></div><div><small>岗位初筛</small><strong>{screening ? screeningLabels[screening.effective_outcome] : "待初筛"}</strong></div>{screening && <div><small>目标岗位</small><strong>{screening.job_position_name}</strong></div>}</div>
    {screening ? reviewPending ? <section className="screening-explanation"><div className="section-title-row"><div><h3>{screening.summary}</h3><p>{screening.effective_outcome === "failed" ? screeningFailureText(screening.error) : progressText(screening)}</p>{screening.effective_outcome === "failed" && screening.error?.code && <small className="muted">错误码：{screening.error.code}</small>}</div><Status value={screening.effective_outcome} /></div><p>处理完成前不会生成符合或淘汰结论，也不会启动 7 天自动清理。</p>{screening.effective_outcome === "failed" && screening.review_id && <div className="table-actions"><button className="button button-primary" onClick={retryScreening} disabled={retryingScreening}>{retryingScreening ? "重新排队中…" : "重新初筛"}</button></div>}</section> : <><section className="screening-explanation"><div className="section-title-row"><div><h3>{screening.summary}</h3><p>AI 建议：{screeningLabels[screening.ai_recommendation]} · 匹配分 {screening.score}</p><small className="muted">{screeningScorePolicyText}</small></div><Status value={screening.effective_outcome} /></div><div className="screening-reasons"><ReasonList title="入选依据" items={screening.matched_requirements} empty="暂无明确匹配证据。" /><ReasonList title="淘汰/待补证据" items={screening.unmet_requirements} empty="未发现岗位硬性缺口。" /></div>{candidate.retention_reason === "screening_unqualified" && <p className="retention-warning">当前结论为不符合，系统将在 {formatDate(candidate.retention_expires_at)} 后自动清理；复核为符合会取消清理。</p>}</section><section><h3>人工复核</h3><textarea className="form-textarea" name="screening_review_note" placeholder="填写复核说明（可选）" defaultValue={screening.human_review_note || ""} /><div className="table-actions"><button className="button button-primary" onClick={() => review("qualified")}>复核为符合</button><button className="button button-danger" onClick={() => review("unqualified")}>复核为不符合</button></div></section></> : <Empty title="尚无岗位初筛" copy="上传 PDF 简历时选择岗位要求，系统会生成可解释初筛结果" />}
    <section><div className="section-title-row"><div><h3>简历</h3><p>修改只更新显示名称；替换 PDF 请重新上传，系统会保存为新版本。</p></div></div>{resumeItems.length ? <div className="resource-list">{resumeItems.map((resume) => <article className="list-card resume-list-card" key={resume.id}><div className="resume-list-copy">{editingResumeId === resume.id ? <input className="form-input" aria-label="简历名称" value={displayName} onChange={(event) => setDisplayName(event.target.value)} disabled={busyResumeId === resume.id} /> : <strong>{resume.file_name || "候选人简历.pdf"}</strong>}<small>版本 {resume.resume_version || 1} · {resume.status === "ready" ? "安全扫描与解析已完成" : `状态：${resume.status}`} · {formatDate(resume.created_at)}</small></div><div className="table-actions">{editingResumeId === resume.id ? <><button className="button button-primary button-small" onClick={() => renameResume(resume)} disabled={busyResumeId === resume.id || !displayName.trim()}>保存名称</button><button className="button button-secondary button-small" onClick={() => setEditingResumeId("")} disabled={busyResumeId === resume.id}>取消</button></> : <><button className="button button-secondary button-small" onClick={() => openResume(resume)} disabled={resume.status !== "ready" || busyResumeId === resume.id}>查看简历</button><button className="button button-secondary button-small" onClick={() => startRename(resume)} disabled={busyResumeId === resume.id}>修改名称</button><button className="button button-danger button-small" onClick={() => deleteResume(resume)} disabled={busyResumeId === resume.id}>删除简历</button></>}</div></article>)}</div> : <Empty title="尚无简历" copy="请先上传 PDF 简历" />}</section>
  </div>;
}

function CandidateQuestionBank({ candidate, screening, review, initialQuestions, request, API, toast }) {
  const [questions, setQuestions] = useState(initialQuestions);
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState("");
  const [busyId, setBusyId] = useState("");
  const evidenceOptions = [...(review?.project_evidence || []).map((item) => ({ ...item, evidence_type: "项目经历" })), ...(review?.skill_evidence || []).map((item) => ({ ...item, evidence_type: "技能经历" }))]
    .filter((item, index, items) => item.label && item.evidence && items.findIndex((candidate) => candidate.label === item.label) === index);
  const activeReviewId = screening?.review_id && screening.effective_outcome === "qualified" ? screening.review_id : "";
  const generationStatus = review?.question_generation_status || screening?.question_generation_status;
  const replace = (updated) => setQuestions((items) => items.map((item) => item.id === updated.id ? updated : item));
  const create = async (payload) => {
    setBusyId("create");
    try {
      const created = await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/experience-questions`, { method: "POST", body: { resume_review_id: activeReviewId, ...payload } });
      setQuestions((items) => [created, ...items]);
      setCreating(false);
      toast("简历问题已创建", "题目已绑定所选简历证据；批准后可进入新计划，候选人确认预约时再生成语音");
    } catch (error) { toast("创建失败", error.message, "error"); } finally { setBusyId(""); }
  };
  const update = async (question, changes, successMessage) => {
    setBusyId(question.id);
    try {
      const updated = await request(`${API}/experience-questions/${encodeURIComponent(question.id)}`, { method: "PATCH", body: { expected_version: question.version, ...changes } });
      replace(updated);
      setEditingId("");
      toast("简历问答已更新", successMessage);
    } catch (error) { toast("更新失败", error.message, "error"); } finally { setBusyId(""); }
  };
  const archive = async (question) => {
    if (!window.confirm(`确认删除简历问题“${question.question_text}”？历史计划与面试快照不会受影响。`)) return;
    setBusyId(question.id);
    try {
      await request(`${API}/experience-questions/${encodeURIComponent(question.id)}?expected_version=${question.version}`, { method: "DELETE" });
      setQuestions((items) => items.filter((item) => item.id !== question.id));
      toast("简历问题已归档", "后续计划不再选用，历史面试仍可追溯");
    } catch (error) { toast("删除失败", error.message, "error"); } finally { setBusyId(""); }
  };
  return <section className="candidate-question-bank">
    <div className="section-title-row"><div><h3>简历问答</h3><p>每道问题都必须点名并绑定简历中真实出现的项目或技能证据；批准后可进入计划，候选人确认预约时按题库冻结音色生成语音。</p></div><button className="button button-primary button-small" onClick={() => setCreating(true)} disabled={!activeReviewId || !evidenceOptions.length || creating}>新建简历问题</button></div>
    {["queued", "processing"].includes(generationStatus) && <p className="inline-warning">正在根据这份简历的项目与技能证据生成问题，页面会自动刷新候选人状态。</p>}
    {generationStatus === "failed" && <p className="inline-warning">自动生成失败。可在初筛页再次复核为符合来重新排队，或先人工新建与简历证据绑定的问题。</p>}
    {!activeReviewId && <p className="inline-warning">只有最终初筛结论为符合时，才能查看或维护简历问答。</p>}
    {activeReviewId && !evidenceOptions.length && <p className="inline-warning">这份简历没有可追溯的项目或技能证据，不能创建无依据的问题。</p>}
    {creating && <ExperienceQuestionForm evidenceOptions={evidenceOptions} title="人工新增简历问题" submitLabel={busyId === "create" ? "创建中…" : "保存草稿"} disabled={busyId === "create"} onCancel={() => setCreating(false)} onSubmit={create} />}
    {questions.length ? <div className="candidate-question-list">{questions.map((question) => <article className="list-card candidate-question-card" key={question.id}>
      {editingId === question.id ? <ExperienceQuestionForm evidenceOptions={evidenceOptions} current={question} title="编辑简历问题" submitLabel={busyId === question.id ? "保存中…" : "保存修改"} disabled={busyId === question.id} onCancel={() => setEditingId("")} onSubmit={(payload) => update(question, payload, "题干、简历依据与评分依据已保存")} /> : <>
        <div className="candidate-question-copy"><div className="candidate-question-meta"><span className="tag">{question.source_type === "manual" ? "人工创建" : "AI 简历生成"}</span><Status value={question.status} /><Status value={question.speech_status} /></div><strong>{question.question_text}</strong><small className="candidate-question-evidence">简历依据：{question.evidence_refs?.map((item) => typeof item === "string" ? item : item.label).filter(Boolean).join(" · ") || "无（已被规则拦截）"}</small><small>评分关键点：{question.key_points?.map((item) => item.text).join(" · ") || "待补充"}</small></div>
        <div className="table-actions">{question.status !== "approved" && <button className="button button-primary button-small" onClick={() => update(question, { status: "approved" }, "题目已批准，可用于新计划；读题语音将在候选人确认预约后生成")} disabled={busyId === question.id}>批准</button>}{question.status !== "rejected" && <button className="button button-secondary button-small" onClick={() => update(question, { status: "rejected" }, "题目已拒绝，不会进入计划")} disabled={busyId === question.id}>拒绝</button>}<QuestionActions question={question} disabled={busyId === question.id} onEdit={() => setEditingId(question.id)} onDelete={() => archive(question)} /></div>
      </>}
    </article>)}</div> : !creating && <Empty title="还没有简历问题" copy={["queued", "processing"].includes(generationStatus) ? "AI 正在根据简历证据生成，请稍后查看" : "可点击“新建简历问题”人工添加，每题必须选择并写明简历依据"} />}
  </section>;
}

function QuestionActions({ question, disabled, onEdit, onDelete }) {
  const closeMenu = (event) => event.currentTarget.closest("details")?.removeAttribute("open");
  return <details className="position-action-menu" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.removeAttribute("open"); }}><summary className="position-action-trigger" role="button" aria-haspopup="menu" aria-label={`${question.question_text}的更多操作`} aria-disabled={disabled}>•••</summary><div className="position-action-popover" role="menu"><button type="button" role="menuitem" disabled={disabled} onClick={(event) => { closeMenu(event); onEdit(); }}>编辑</button><button type="button" role="menuitem" className="is-danger" disabled={disabled} onClick={(event) => { closeMenu(event); onDelete(); }}>删除</button></div></details>;
}

function ExperienceQuestionForm({ current = null, evidenceOptions, title, submitLabel, disabled, onCancel, onSubmit }) {
  const submit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const keyPoints = String(form.get("key_points") || "").split(/\n/).map((item) => item.trim()).filter(Boolean);
    if (!keyPoints.length) throw new Error("至少填写一个评分关键点");
    await onSubmit({ question_text: form.get("question_text"), standard_answer: form.get("standard_answer"), key_points: keyPoints, evidence_refs: [form.get("evidence_ref")] });
  };
  const currentEvidenceLabel = current?.evidence_refs?.[0] && (typeof current.evidence_refs[0] === "string" ? current.evidence_refs[0] : current.evidence_refs[0].label);
  return <form className="candidate-question-form" onSubmit={submit}><strong>{title}</strong><Field label="简历依据" full hint="问题中必须明确写出这个项目或技能名称"><select className="form-select" name="evidence_ref" defaultValue={currentEvidenceLabel || evidenceOptions[0]?.label || ""} required><option value="" disabled>请选择简历中真实出现的经历</option>{evidenceOptions.map((item) => <option key={item.label} value={item.label}>{item.evidence_type} · {item.label} — {item.evidence}</option>)}</select></Field><Field label="问题" full><textarea className="form-textarea" name="question_text" defaultValue={current?.question_text || ""} placeholder="例如：你在“循道大模型开发平台”中为什么选择 Redis？具体如何落地？" required /></Field><Field label="评分参考" full><textarea className="form-textarea" name="standard_answer" defaultValue={current?.standard_answer || ""} placeholder="只描述这段简历经历中期待核实的背景、本人职责、方案、权衡和结果" required /></Field><Field label="评分关键点（每行一个）" full><textarea className="form-textarea" name="key_points" defaultValue={current?.key_points?.map((item) => item.text).join("\n") || ""} placeholder={"本人职责\n简历描述的实现细节\n方案权衡与量化结果"} required /></Field><div className="table-actions field-full"><button className="button button-primary" type="submit" disabled={disabled}>{submitLabel}</button><button className="button button-secondary" type="button" onClick={onCancel} disabled={disabled}>取消</button></div></form>;
}

function ReasonList({ title, items, empty }) {
  return <div><h4>{title}</h4>{items.length ? <ul>{items.map((item, index) => <li key={`${item.requirement}-${index}`}><strong>{item.requirement}</strong><span>{item.evidence || item.reason}</span></li>)}</ul> : <p>{empty}</p>}</div>;
}

function ResumeForm({ candidate, data, request, API, onDone }) {
  const candidateRoles = data.roles.filter((role) => !candidate.job_position_id || role.job_position_id === candidate.job_position_id);
  const submit = async (form) => {
    const file = form.get("file"); const url = String(form.get("source_url") || "").trim();
    if ((!file?.size && !url) || (file?.size && url)) throw new Error("本地 PDF 和 URL 必须且只能选择一种");
    const role = data.roles.find((item) => item.id === form.get("role_requirement_id"));
    if (!role) throw new Error("请选择用于初筛的岗位要求");
    const idempotencyKey = crypto.randomUUID(); let queued;
    if (file?.size) { const upload = new FormData(); upload.set("file", file, file.name); upload.set("display_name", file.name); upload.set("job_position_id", role.job_position_id); upload.set("role_requirement_id", role.id); queued = await request(`${API}/candidate-profiles/${candidate.id}/resumes`, { method: "POST", body: upload, idempotencyKey }); }
    else queued = await request(`${API}/candidate-profiles/${candidate.id}/resumes/import-url`, { method: "POST", body: { url, display_name: url.split("/").pop() || "resume.pdf", job_position_id: role.job_position_id, role_requirement_id: role.id }, idempotencyKey });
    if (!queued.ingestion_job_id) throw new Error("后台处理任务创建失败");
    await onDone("简历已进入后台处理；完成解析和 LLM 证据聚合后，候选人列表会显示初筛结论");
  };
  return <ModalForm onSubmit={submit} submitLabel="保存并后台初筛" submitDisabled={!candidateRoles.length}><p className="form-intro field-full">提交后可以立即关闭此窗口。短简历单次评估；长简历按页分块提取证据，再统一生成符合性、入选依据与缺口，结果必须人工复核。</p><Field label="本地 PDF" full><input className="form-input" name="file" type="file" accept="application/pdf,.pdf" /></Field><Field label="PDF URL" full><input className="form-input" name="source_url" type="url" /></Field><Field label="初筛岗位要求" full hint={!candidateRoles.length ? "请先为候选人的应聘岗位建立岗位要求" : null}><select className="form-select" name="role_requirement_id" required defaultValue=""><option value="" disabled>请选择岗位要求</option>{candidateRoles.map((role) => <option value={role.id} key={role.id}>{role.title}</option>)}</select></Field></ModalForm>;
}

function progressText(screening) {
  const progress = screening.processing_progress;
  if (screening.processing_stage === "ingesting") return "正在进行 PDF 安全扫描和逐页文本解析";
  if (screening.processing_stage === "extracting_evidence" && progress?.total_chunks) return `正在分块提取证据：${progress.completed_chunks}/${progress.total_chunks}`;
  if (screening.processing_stage === "aggregating_review") return "全部分块已完成，正在聚合岗位匹配结论";
  return "后台任务已排队，请稍后刷新查看进度";
}

function screeningFailureText(error) {
  if (error?.code === "provider_circuit_open") return "模型服务连续失败后已进入保护状态。请稍后点击“重新初筛”；若仍失败，请检查模型超时和备用路由。";
  if (error?.code === "provider_timeout") return "模型处理超过当前时限，没有生成初筛结论。请调整模型超时后重新初筛。";
  if (error?.code === "provider_auth_failed") return "模型凭据验证失败，请管理员更新连接后重新初筛。";
  if (error?.code === "provider_rate_limited") return "模型服务正在限流或额度不足，请稍后重新初筛。";
  return error?.message || "后台任务未能完成，请检查模型服务后重新初筛。";
}
