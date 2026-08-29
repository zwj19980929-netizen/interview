import { useEffect, useState } from "react";

import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status, formatDate } from "../../core/ui.jsx";

const screeningLabels = { qualified: "符合", unqualified: "不符合", manual_review: "待人工复核", processing: "处理中", failed: "处理失败" };
const screeningScorePolicyText = "匹配分标准：0–59 分不符合，60–74 分待人工复核，75–100 分符合；人工复核可覆盖 AI 建议。";
const splitSkills = (value) => String(value || "").split(/[,，、\n]/).map((item) => item.trim()).filter(Boolean);

export default function WorkflowPage() {
  const { API, data, request, reloadRoute, openModal, closeModal, toast, navigate } = useWorkbench();
  const hasProcessingCandidate = data.candidates.some((candidate) => candidate.screening?.effective_outcome === "processing");
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
  const editCandidate = (candidate) => openModal({ title: `编辑 ${candidate.name}`, body: <ModalForm submitLabel="保存修改" onSubmit={async (form) => { const body = { expected_version: candidate.version, name: form.get("name"), external_ref: form.get("external_ref") || null }; if (String(form.get("email") || "").trim()) body.email = form.get("email"); if (String(form.get("phone") || "").trim()) body.phone = form.get("phone"); await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}`, { method: "PATCH", body }); await finish("候选人资料已更新"); }}><Field label="姓名"><input className="form-input" name="name" defaultValue={candidate.name} required /></Field><Field label="外部编号"><input className="form-input" name="external_ref" defaultValue={candidate.external_ref || ""} /></Field><Field label="新邮箱" hint={`留空则保持 ${candidate.email || "现有邮箱"}`}><input className="form-input" name="email" type="email" /></Field><Field label="新手机号" hint={`留空则保持 ${candidate.phone || "现有手机号"}`}><input className="form-input" name="phone" /></Field></ModalForm> });
  const deleteCandidate = (candidate) => openModal({ title: `删除 ${candidate.name}`, body: <ModalForm submitLabel="确认删除" submitVariant="danger" onSubmit={async () => { await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}?expected_version=${candidate.version}`, { method: "DELETE" }); await finish("候选人已从当前列表归档；审计与历史面试快照仍保留"); }}><p className="form-intro field-full">删除后将从候选人列表移除。历史面试与审计记录不会被破坏。</p></ModalForm> });
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

  return <>
    <section className="page-header"><div><h1>岗位到复核的业务闭环</h1><p>岗位题库、企业简历、岗位初筛、计划和预约</p></div><div className="page-actions"><button className="button button-secondary" onClick={candidateForm} disabled={!data.positions.length}>录入候选人</button><button className="button button-primary" onClick={positionForm}>新建岗位</button></div></section>
    <section className="panel"><div className="section-title-row"><div><h2>岗位与题库</h2><p>岗位要求用于简历初筛；题目和读题音色由关联题库统一维护</p></div></div>{data.positions.length ? <div className="question-grid">{data.positions.map((position) => { const assigned = data.knowledgeBases.filter((knowledgeBase) => assignedTo(position, knowledgeBase)); const positionRoles = data.roles.filter((role) => role.job_position_id === position.id); return <article className="question-card" key={position.id}><div className="question-card-top"><h3>{position.name}</h3><Status value={position.status} /></div><p>{position.description || "暂未填写岗位说明"}</p><div className="tag-list">{positionRoles.length ? <span className="tag">岗位要求 {positionRoles.length} 个版本</span> : <span className="tag">尚无岗位要求</span>}{assigned.map((knowledgeBase) => <span className="tag" key={knowledgeBase.id}>{knowledgeBase.name} · {voiceLabel(knowledgeBase)}</span>)}</div><div className="position-card-actions"><button className="button button-secondary button-small" onClick={() => roleForm(position)}>{positionRoles.length ? "新增要求版本" : "添加岗位要求"}</button><button className="button button-secondary button-small" onClick={() => kbForm(position)}>{assigned.length ? "管理题库" : "关联题库"}</button><PositionActions position={position} onEdit={editPosition} onDelete={deletePosition} /></div></article>; })}</div> : <Empty title="尚无岗位" copy="点击“新建岗位”同时建立首版岗位要求" />}</section>
    <CandidateTable candidates={data.candidates} positions={data.positions} showCandidate={showCandidate} editCandidate={editCandidate} resumeForm={resumeForm} deleteCandidate={deleteCandidate} />
    <section className="panel"><div className="section-title-row"><div><h2>预约</h2><p>计划审批后生成候选人邀请</p></div></div>{data.appointments.length ? <div className="resource-list">{data.appointments.map((item) => <article className="list-card" key={item.id}><strong>{data.candidates.find((candidate) => candidate.id === item.candidate_profile_id)?.name || "候选人"}</strong><span>{formatDate(item.scheduled_start_at)}</span><Status value={item.status} /></article>)}</div> : <Empty title="尚无预约" copy="批准面试计划后创建预约" />}</section>
  </>;
}

function PositionActions({ position, onEdit, onDelete }) {
  const closeMenu = (event) => event.currentTarget.closest("details")?.removeAttribute("open");
  return <details className="position-action-menu" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.removeAttribute("open"); }} onKeyDown={(event) => { if (event.key === "Escape") { event.currentTarget.removeAttribute("open"); event.currentTarget.querySelector("summary")?.focus(); } }}><summary className="position-action-trigger" role="button" aria-haspopup="menu" aria-label={`${position.name}的更多操作`}>•••</summary><div className="position-action-popover" role="menu"><button type="button" role="menuitem" onClick={(event) => { closeMenu(event); onEdit(position); }}>编辑岗位</button><button type="button" role="menuitem" className="is-danger" onClick={(event) => { closeMenu(event); onDelete(position); }}>删除岗位</button></div></details>;
}

function CandidateTable({ candidates, positions, showCandidate, editCandidate, resumeForm, deleteCandidate }) {
  return <section className="panel"><div className="section-title-row"><div><h2>候选人</h2><p>候选人明确归属于应聘岗位；上传简历后完成初筛与人工复核</p><small className="muted">{screeningScorePolicyText}</small></div></div>{candidates.length ? <div className="data-table-wrap"><table className="data-table"><thead><tr><th>姓名</th><th>应聘岗位</th><th>邮箱</th><th>手机</th><th>是否符合岗位要求</th><th></th></tr></thead><tbody>{candidates.map((item) => { const purged = item.name === "[retention_purged]"; const screening = item.screening; const positionName = positions.find((position) => position.id === item.job_position_id)?.name || screening?.job_position_name || "待关联"; const screeningDetail = screening?.effective_outcome === "processing" ? " · 后台处理中" : screening?.effective_outcome === "failed" ? " · 请查看错误" : screening?.human_review_status === "reviewed" ? " · 已复核" : " · 待复核"; return <tr key={item.id}><td><strong>{purged ? "已清除候选人" : item.name}</strong>{purged && <small className="cell-subtitle">仅保留审计占位</small>}</td><td>{positionName}</td><td>{item.email || "—"}</td><td>{item.phone || "—"}</td><td>{screening ? <button className={`screening-link screening-${screening.effective_outcome}`} onClick={() => showCandidate(item)}><span>{screeningLabels[screening.effective_outcome] || "待初筛"}</span><small>{screening.job_position_name}{screeningDetail}</small></button> : <button className="screening-link" onClick={() => showCandidate(item)}><span>待初筛</span><small>请上传简历并选择岗位</small></button>}</td><td><div className="table-actions"><button className="button button-secondary button-small" onClick={() => showCandidate(item)} disabled={purged}>查看</button><button className="button button-secondary button-small" onClick={() => editCandidate(item)} disabled={purged}>编辑</button><button className="button button-secondary button-small" onClick={() => resumeForm(item)} disabled={purged}>上传简历</button><button className="button button-danger button-small" onClick={() => deleteCandidate(item)} disabled={purged}>删除</button></div></td></tr>; })}</tbody></table></div> : <Empty title="尚无候选人" copy="录入候选人后上传简历并完成岗位初筛" />}</section>;
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
  const review = async (decision) => { if (!screening) return; const note = document.querySelector('[name="screening_review_note"]')?.value || ""; await request(`${API}/resume-reviews/${encodeURIComponent(screening.review_id)}/screening-review`, { method: "PATCH", body: { expected_version: screening.review_version, decision, note } }); await onReviewed(decision === "qualified" ? "复核为符合岗位要求，已取消自动清理" : "复核为不符合岗位要求，将按 7 天规则清理"); };
  const retryScreening = async () => {
    if (!screening?.review_id) return;
    setRetryingScreening(true);
    try {
      await request(`${API}/resume-reviews/${encodeURIComponent(screening.review_id)}/retry`, { method: "POST", body: { expected_version: screening.review_version, reason: "interviewer_requested_retry" } });
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
      const updated = await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/resumes/${encodeURIComponent(resume.id)}`, { method: "PATCH", body: { expected_version: resume.version, display_name: displayName } });
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
      await request(`${API}/candidate-profiles/${encodeURIComponent(candidate.id)}/resumes/${encodeURIComponent(resume.id)}?expected_version=${resume.version}`, { method: "DELETE" });
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
