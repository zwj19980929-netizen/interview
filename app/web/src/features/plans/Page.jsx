import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty } from "../../core/ui.jsx";
import PlanCreate from "./PlanCreate.jsx";
import { planDate, planIdentity, planRange, planStatusLabels } from "./plan-presentation.js";
import "./plans.css";

function PlanIcon({ type = "document", ...props }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
    {type === "search" ? <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></> : type === "back" ? <path d="m14 6-6 6 6 6M8 12h12" /> : type === "arrow" ? <path d="m9 5 7 7-7 7" /> : type === "plus" ? <path d="M12 5v14M5 12h14" /> : <><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z" /><path d="M14 3v6h6M8 13h8M8 17h5" /></>}
  </svg>;
}

function PlanStatus({ status }) {
  return <span className={`plans-status is-${status || "draft"}`}>{planStatusLabels[status] || "状态待确认"}</span>;
}

export default function PlansPage() {
  const { data, auth, route = {}, navigate, reloadRoute, toast } = useWorkbench();
  const [localPlans, setLocalPlans] = useState({});
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  useEffect(() => {
    if (window.scrollY) window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [route.planAction, route.selectedPlanId]);
  const allowed = auth?.roles?.some((role) => ["admin", "interviewer"].includes(role));
  const updatePlan = useCallback((plan) => setLocalPlans((current) => ({ ...current, [plan.id]: plan })), []);
  const go = (id) => navigate("plans", id);
  const plans = useMemo(() => {
    const combined = new Map((data.plans || []).map((plan) => [plan.id, plan]));
    Object.values(localPlans).forEach((plan) => {
      if (!combined.has(plan.id) || plan.version >= combined.get(plan.id).version) combined.set(plan.id, plan);
    });
    return [...combined.values()].sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
  }, [data.plans, localPlans]);
  const onCreated = (plan) => {
    updatePlan(plan); go(plan.id);
    toast("计划已生成", "已从题库准备好考察内容，可以直接安排面试。");
    reloadRoute("plans").catch(() => toast("计划已保存", "列表刷新失败，重新打开可读取最新结果。", "error"));
  };

  if (!allowed) return <Empty title="需要面试配置权限" copy="管理员或面试官可以查看和启用面试计划。" />;
  if (route.planAction === "create") return <div className="plans-page"><button className="plans-back" onClick={() => go()}><PlanIcon type="back" />返回计划列表</button><PlanCreate onCreated={onCreated} onCancel={() => go()} /></div>;
  if (route.selectedPlanId) return <PlanDetail key={route.selectedPlanId} planId={route.selectedPlanId} onUpdated={updatePlan} onBack={() => go()} onCreate={() => go("new")} />;

  const normalized = query.trim().toLocaleLowerCase();
  const filtered = plans.filter((plan) => {
    const identity = planIdentity(plan, data);
    return (filter === "all" || plan.status === filter) && (!normalized || `${identity.name} ${identity.role} ${identity.requirement}`.toLocaleLowerCase().includes(normalized));
  });
  const filters = [["all", "全部"], ["draft", "待审阅"], ["approved", "已启用"], ["archived", "已归档"]];
  return <div className="plans-page">
    <header className="plans-header">
      <div className="plans-heading"><h1>面试计划</h1><p>为每位候选人准备一场有重点的面试。</p></div>
      <div className="plans-header-actions"><button className="button button-primary" onClick={() => go("new")}><PlanIcon type="plus" />新建计划</button></div>
    </header>
    <section className="plans-list-panel" aria-label="面试计划列表">
      <div className="plans-toolbar">
        <div className="plans-tabs" aria-label="按计划状态筛选">{filters.map(([value, label]) => <button key={value} className={`plans-tab${filter === value ? " is-active" : ""}`} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}<span>{value === "all" ? plans.length : plans.filter((plan) => plan.status === value).length}</span></button>)}</div>
        <label className="plans-search"><PlanIcon type="search" /><input type="search" aria-label="搜索候选人或岗位" placeholder="搜索候选人或岗位" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      </div>
      <p className="plans-result-count">{normalized || filter !== "all" ? `找到 ${filtered.length} 份计划` : `共 ${plans.length} 份计划`}<span>选择一份计划查看考察内容</span></p>
      {filtered.length ? <div className="plans-list">{filtered.map((plan) => {
        const identity = planIdentity(plan, data);
        return <article className="plans-row" key={plan.id}>
          <div className="plans-person"><span className="plans-avatar" aria-hidden="true">{identity.candidate?.name && identity.name !== "已清除候选人" ? Array.from(identity.name)[0] : <PlanIcon />}</span><div className="plans-person-copy"><h2>{identity.name}</h2><p>{identity.role}</p></div></div>
          <div className="plans-row-meta"><span>{identity.duration ? `${identity.duration} 分钟` : "时长未记录"} · {planRange(plan)}</span><small>{planDate(plan.created_at, true)} 创建 <span className="plans-mode">{plan.execution_schema_version === 3 ? "自主面试" : "固定题序"}</span></small></div>
          <div className="plans-row-status"><PlanStatus status={plan.status} /></div>
          <button className="plans-row-action" onClick={() => go(plan.id)} aria-label={`${plan.status === "draft" && plan.execution_schema_version === 3 ? "审阅计划" : "查看计划"}：${identity.name}`}>{plan.status === "draft" && plan.execution_schema_version === 3 ? "审阅计划" : "查看计划"}<PlanIcon type="arrow" /></button>
        </article>;
      })}</div> : <div className="plans-empty"><span className="plans-empty-icon"><PlanIcon width="28" height="28" /></span><h2>{plans.length ? "没有找到匹配的计划" : "还没有面试计划"}</h2><p>{plans.length ? "试试其他候选人、岗位或状态。" : "选好候选人和题库，即可准备面试。"}</p><button className="button button-secondary" onClick={() => plans.length ? (setQuery(""), setFilter("all")) : go("new")}>{plans.length ? "清除筛选" : "创建第一份计划"}</button></div>}
    </section>

  </div>;
}

function PlanDetail({ planId, onUpdated, onBack, onCreate }) {
  const { API, data, request, toast, reloadRoute } = useWorkbench();
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [needsReload, setNeedsReload] = useState(true);
  const busyRef = useRef(false);
  const generation = useRef(0);
  const read = async () => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setProblem(""); setNeedsReload(true);
    const operation = ++generation.current;
    try {
      const value = await request(`${API}/interview-plans/${encodeURIComponent(planId)}`);
      if (operation !== generation.current) return;
      if (value?.id !== planId) throw new Error("计划信息不匹配，请重新加载。");
      setPlan(value); onUpdated(value); setNeedsReload(false);
    } catch (error) {
      if (operation === generation.current) setProblem(error.message || "无法读取这份计划，请重新加载。");
    } finally {
      if (operation === generation.current) { busyRef.current = false; setBusy(false); }
    }
  };
  useEffect(() => { read(); return () => { generation.current += 1; busyRef.current = false; }; }, [planId]);
  const contract = plan?.assessment_contract;
  const questions = contract?.candidate_questions || [];
  const budget = contract?.budget || {};
  const skill = plan?.skill_snapshot || plan?.enterprise_skill_snapshot;
  const selectedSkillId = plan?.skill_id || plan?.enterprise_skill_id;
  const modern = plan?.execution_schema_version === 3;
  const reviewable = modern && contract?.contract_hash && Number.isInteger(plan?.version) && questions.length > 0
    && questions.every((question) => question.inquiry_units?.length && question.inquiry_units.every((unit) => typeof unit.question_text === "string" && unit.question_text.trim()))
    && (!selectedSkillId || skill);
  const approve = async () => {
    if (busyRef.current || needsReload || !reviewable || plan?.status !== "draft") return;
    busyRef.current = true; setBusy(true); setProblem("");
    const operation = generation.current;
    try {
      const result = await request(`${API}/interview-plans/${encodeURIComponent(plan.id)}`, { method: "PATCH", body: { expected_version: plan.version, status: "approved" } });
      if (operation !== generation.current) return;
      if (result?.id !== planId || result?.status !== "approved") throw new Error("启用结果尚未确认，请重新加载。");
      setPlan(result); onUpdated(result);
      toast("计划已启用", "这份计划可以用于创建面试预约。");
      reloadRoute("plans").catch(() => toast("计划已启用", "列表刷新失败，重新打开可读取已保存结果。", "error"));
    } catch (error) {
      if (operation === generation.current) {
        setNeedsReload(true);
        setProblem(error.code === "PERSISTENCE_CONFLICT" || error.status === 409 || error.status_code === 409
          ? "计划或授权状态已变化，请重新加载并审阅最新内容，再确认启用。"
          : "本次启用结果未确认。请重新加载计划后再操作。");
      }
    } finally {
      if (operation === generation.current) { busyRef.current = false; setBusy(false); }
    }
  };
  const identity = plan ? planIdentity(plan, data) : null;
  return <div className="plans-page plans-detail">
    <button className="plans-back" onClick={onBack}><PlanIcon type="back" />返回计划列表</button>
    {!plan ? <section className="plans-section">{problem ? <><p className="plans-callout is-error" role="alert">{problem}</p><button className="button button-secondary" onClick={read} disabled={busy}>重新加载</button></> : <p role="status">正在读取计划…</p>}</section> : <>
      <header className="plans-header plans-detail-heading"><div className="plans-heading"><h1>{identity.name}的面试计划</h1><p>{identity.role} · {planDate(plan.created_at, true)} 创建</p></div><PlanStatus status={plan.status} /></header>
      <div className="plans-summary-grid">
        <div className="plans-summary-item"><span>面试时长</span><strong>{identity.duration ? `${identity.duration} 分钟` : "未记录"}</strong></div>
        <div className="plans-summary-item"><span>考察范围</span><strong>{planRange(plan)}</strong></div>
        <div className="plans-summary-item"><span>访谈方式</span><strong>{modern ? "自主面试" : "固定题序"}</strong></div>
      </div>
      {problem && <div className="plans-callout is-error" role="alert">{problem}<button className="button button-secondary" onClick={read} disabled={busy}>重新加载</button></div>}
      {plan.status === "draft" && modern && <div className="plans-callout"><strong>这份计划还在等待你审阅</strong><p>查看下面的问题和评分依据，确认后即可启用。面试官会根据回答选择话题和追问。</p></div>}
      {plan.status === "approved" && <div className="plans-callout is-ready" role="status"><div><strong>计划已启用，可以安排面试</strong><p>到面试会话页创建预约，选择这位候选人和本计划。</p></div><a className="button button-secondary" href="#interviews">去安排面试 <span aria-hidden="true">→</span></a></div>}
      <div className="plans-detail-layout">
        <section className="plans-section" aria-label="计划考察内容">
          <header><h2>{modern ? "面试问题" : "原定考察安排"}</h2><p>{modern ? "每次只围绕一个重点交流。展开评分依据，可以查看具体考察点。" : "这份计划按创建时的题序进行，保留原有安排。"}</p></header>
          {modern ? questions.map((question, index) => <section className="plans-question-group" key={question.question_id}>
            <div className="plans-question-head"><span className="plans-question-number">{String(index + 1).padStart(2, "0")}</span><div><h3>{question.frozen_question?.title || `考察方向 ${index + 1}`}</h3><p>{question.source_type === "resume_experience" ? "来自简历经历" : "来自岗位题库"} · {question.inquiry_units?.length || 0} 个可选问题</p></div></div>
            <ol className="plans-question-list">{(question.inquiry_units || []).map((unit) => <li className="plans-question-item" key={unit.id}>
              <p>{unit.question_text}</p><div className="plans-question-meta">{unit.competency_ids?.map((item) => <span key={item}>{item}</span>)}</div>
              <details className="plans-disclosure"><summary>评分依据</summary><div><h4>考察点</h4><p>{(unit.assessed_rubric_point_ids || []).map((id) => {
                const point = question.frozen_question?.key_points?.find((item) => (typeof item === "string" ? item : item.id) === id);
                return typeof point === "string" ? point : point?.text || id;
              }).join("；")}</p><h4>参考答案片段</h4><blockquote>{unit.standard_answer_quote}</blockquote></div></details>
            </li>)}</ol>
            {!question.inquiry_units?.length && <p className="plans-callout is-warning">这份草稿缺少具体问题，请重新生成后审阅。</p>}
            <details className="plans-disclosure"><summary>原题与完整评分标准</summary><div><p className="plans-prose">{question.frozen_question?.question_text}</p><h4>参考答案</h4><p className="plans-prose">{question.frozen_question?.standard_answer}</p><Rubric value={question.frozen_question?.rubric} /></div></details>
          </section>) : <LegacyPlan plan={plan} onCreate={onCreate} />}
          {modern && !questions.length && <p className="plans-callout is-warning">这份计划缺少可审阅的问题，请重新生成。</p>}
          {!!plan.assembly_summary?.warnings?.length && <details className="plans-disclosure"><summary>计划准备说明（{plan.assembly_summary.warnings.length}）</summary><ul>{plan.assembly_summary.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
        </section>
        <aside>
          {modern && <section className="plans-section"><header><h2>考察重点</h2><p>能力权重在面试前确定。</p></header><div className="plans-competencies">{(contract?.competencies || []).map((item) => <div className="plans-competency" key={item.id}><strong>{item.id}</strong><span>{Number.isFinite(item.weight) ? `${Math.round(item.weight * 10000) / 100}%` : "未设置"}</span><small>至少 {item.min_evidence_roots} 处回答证据{item.required ? " · 必需" : ""}</small></div>)}</div><details className="plans-disclosure"><summary>话题与追问设置</summary><p>每个话题最多追问 {budget.max_followups_per_root ?? 0} 次，总追问上限 {budget.max_total_followups ?? 0} 次。</p><p>预留 {budget.closing_reserve_seconds ?? 0} 秒结束交流。</p></details></section>}
          <section className="plans-section"><header><h2>本场面试定制</h2><p>使用生成本计划时保存的内容，后续修改不会影响本场面试。</p></header><details className="plans-disclosure"><summary>Skill（可选）<small>{skill ? `已选择 · 第 ${skill.revision} 版` : "未使用"}</small></summary>{skill ? <><p>使用生成计划时保存的 Skill 第 {skill.revision} 版。</p><a href="#skills">管理面试定制 →</a></> : <p>{selectedSkillId ? "所选 Skill 版本尚未保存到本场计划，请重新生成。" : "使用基础面试官，根据本场题库和回答展开交流。"}</p>}</details><details className="plans-disclosure"><summary>企业资料（可选）<small>{plan.company_context ? "已提供" : "未提供"}</small></summary><p className="plans-prose">{plan.company_context || "未提供企业资料，面试官仅依据本场考察材料交流，不编造企业信息。"}</p></details></section>
          <section className="plans-section"><details className="plans-disclosure plans-technical"><summary>版本记录</summary><p>计划版本 {plan.version} · {planDate(plan.updated_at || plan.created_at, true)}</p><p>{plan.preparation_mode === "question_bank" ? "考察内容来自所选题库，已自动完成准备检查。" : `岗位要求版本 ${contract?.role_requirement_version ?? "未记录"}`}</p><p>计划编号 <code>{plan.id}</code></p>{skill && <><p>Skill 第 {skill.revision} 版 <code>{skill.skill_id}</code></p><p>内容标识 <code>{skill.content_hash}</code></p></>}<button className="button button-secondary" disabled={busy} onClick={read}>重新加载</button></details></section>
        </aside>
      </div>
      {modern && plan.status === "draft" && <footer className="plans-approval-bar"><div><strong>确认这份计划的考察内容</strong><p>{!reviewable ? "问题或上下文版本不完整，请重新生成计划。" : needsReload ? "请先重新加载并审阅最新版本。" : "启用后即可用于预约，面试官在确认的范围内自主提问。"}</p></div><div className="plans-detail-actions"><button className="button button-secondary" disabled={busy} onClick={read}>重新加载</button><button className="button button-primary" disabled={busy || needsReload || !reviewable} onClick={approve}>{busy ? "正在提交…" : "确认并启用计划"}</button></div></footer>}
    </>}
  </div>;
}

function LegacyPlan({ plan, onCreate }) {
  const bank = plan.bank_slots || [];
  const experience = plan.experience_question_ids || [];
  const frozenExperiences = plan.experience_question_snapshots || [];
  const difficulties = { junior: "基础", mid: "进阶", senior: "深入", easy: "基础", medium: "进阶", hard: "深入" };
  return <><div className="plans-summary-grid"><div className="plans-summary-item"><span>岗位题库</span><strong>{bank.length} 道题</strong></div><div className="plans-summary-item"><span>简历经历</span><strong>{experience.length} 道题</strong></div></div>
    <p className="plans-prose">岗位题按原定顺序提问，简历题在最后进行。这份计划仅供查看，不会改变原来的题目或评分安排。</p>
    {!!bank.length && <section className="plans-question-group"><h3>岗位考察顺序</h3><ol className="plans-question-list">{bank.map((slot, index) => <li className="plans-question-item" key={slot.id || index}>
      <p>{slot.dimension && slot.dimension !== "general" ? slot.dimension : `岗位问题 ${index + 1}`}</p>
      <div className="plans-question-meta">{slot.expected_minutes && <span>约 {slot.expected_minutes} 分钟</span>}{difficulties[slot.difficulty] && <span>{difficulties[slot.difficulty]}</span>}{slot.allow_followup && <span>可追问</span>}</div>
    </li>)}</ol><p className="form-hint">具体问题按本计划保存的候选题范围选取；这里展示原定考察方向。</p></section>}
    {!!frozenExperiences.length && <section className="plans-question-group"><h3>简历经历问题</h3><ol className="plans-question-list">{frozenExperiences.map((question, index) => <li className="plans-question-item" key={question.id || index}><p>{question.question_text}</p>{question.evaluation_focus && <details className="plans-disclosure"><summary>考察重点</summary><Rubric value={question.evaluation_focus} /></details>}</li>)}</ol></section>}
    <details className="plans-disclosure"><summary>查看题目版本记录</summary><ol>{bank.map((slot, index) => <li key={slot.id || index}>岗位问题 {index + 1} · {slot.candidate_pool?.length || slot.candidate_pool_count || 0} 道候选题<ul>{(slot.candidate_pool || []).map((item) => <li className="plans-technical" key={item.question_id}>题目版本 {item.question_version ?? "未记录"} · <code>{item.question_id}</code></li>)}</ul></li>)}</ol><p className="form-hint">仅展示计划已经保存的信息，不使用当前题库内容替代原定安排。</p></details>
    <div className="plans-callout"><strong>想让面试官自主选择话题？</strong><p>新建一份计划，原来的面试记录会继续保留。</p><button className="button button-secondary" onClick={onCreate}>新建自主面试计划</button></div></>;
}

const rubricLabels = { excellent: "优秀回答", good: "良好回答", average: "一般回答", poor: "不足之处", dimensions: "评分维度", criteria: "判断依据", description: "说明", weight: "权重", max_score: "最高分", name: "名称", points: "要点" };
function Rubric({ value }) {
  if (value === null || value === undefined) return <p>未提供更多评分标准。</p>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  if (Array.isArray(value)) return <ul className="plans-rubric">{value.map((item, index) => <li key={index}><Rubric value={item} /></li>)}</ul>;
  return <dl className="plans-rubric">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{rubricLabels[key] || key}</dt><dd><Rubric value={item} /></dd></div>)}</dl>;
}
