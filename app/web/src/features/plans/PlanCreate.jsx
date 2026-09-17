import { useEffect, useRef, useState } from "react";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";

const belongsTo = (candidate, positionId) => !candidate.job_position_id || candidate.job_position_id === positionId;
const availableCandidates = (data, positionId) => (data.candidates || []).filter((item) =>
  belongsTo(item, positionId) && !["archived", "deleted", "purged"].includes(item.status));
const linkedBanks = (data, position) => (data.knowledgeBases || []).filter((bank) => position &&
  (bank.job_position_id === position.id || position.knowledge_base_ids?.includes(bank.id)) && bank.status !== "archived");
const readyBank = (banks) => banks.find((item) => item.status === "ready")?.id || "";

export default function PlanCreate(props) {
  const { API, auth } = useWorkbench();
  return <PlanCreateForm key={`${API}:${auth?.organization_id || ""}:${auth?.actor_id || ""}`} {...props} />;
}

function PlanCreateForm({ onCreated, onCancel, initialRoleId, initialCandidateId }) {
  const { API, data, request } = useWorkbench();
  const positions = (data.positions || []).filter((item) => !["archived", "deleting"].includes(item.status));
  const startingPosition = positions.find((item) => item.id === (data.roles || []).find((role) => role.id === initialRoleId)?.job_position_id)
    || positions.find((item) => item.id === (data.candidates || []).find((candidate) => candidate.id === initialCandidateId)?.job_position_id)
    || positions[0];
  const [positionId, setPositionId] = useState(startingPosition?.id || "");
  const [candidateId, setCandidateId] = useState(() => {
    const candidates = availableCandidates(data, startingPosition?.id);
    return (candidates.find((item) => item.id === initialCandidateId) || candidates[0])?.id || "";
  });
  const [bankId, setBankId] = useState(() => readyBank(linkedBanks(data, startingPosition)));
  const [duration, setDuration] = useState("45");
  const [customization, setCustomization] = useState(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState({ stage: "reading" });
  const [elapsed, setElapsed] = useState(0);
  const activeRequest = useRef(null);
  const busyRef = useRef(false);
  const mounted = useRef(false);
  const position = positions.find((item) => item.id === positionId);
  const candidates = availableCandidates(data, positionId);
  const candidate = candidates.find((item) => item.id === candidateId);
  const banks = linkedBanks(data, position);
  const bank = banks.find((item) => item.id === bankId);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; activeRequest.current?.abort(); };
  }, []);
  useEffect(() => {
    if (!busy) return;
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);
  useEffect(() => {
    let active = true;
    request(`${API}/interview-customization`).then((result) => {
      const fields = ["company_name", "business_overview", "products_services", "additional_info"];
      if (!Number.isInteger(result?.version) || !result.company_profile
        || fields.some((key) => typeof result.company_profile[key] !== "string")
        || (result.skill != null && typeof result.skill.instructions !== "string")) throw new Error("Invalid customization response");
      if (active) setCustomization({ status: "ready",
        hasSkill: ["active", "approved"].includes(result.skill?.status) && Boolean(result.skill?.instructions.trim()),
        hasCompany: fields.some((key) => result.company_profile[key].trim()),
      });
    }).catch(() => { if (active) setCustomization({ status: "unavailable" }); });
    return () => { active = false; };
  }, [API, request]);

  const changePosition = (id) => {
    const next = positions.find((item) => item.id === id);
    setPositionId(id);
    const nextCandidates = availableCandidates(data, id);
    if (!nextCandidates.some((item) => item.id === candidateId)) setCandidateId(nextCandidates[0]?.id || "");
    const nextBanks = linkedBanks(data, next);
    if (!nextBanks.some((item) => item.id === bankId && item.status === "ready")) setBankId(readyBank(nextBanks));
    setProblem("");
  };
  const submit = async (event) => {
    event.preventDefault();
    if (busyRef.current) return;
    if (!position) { setProblem("请选择招聘岗位。"); return; }
    if (!candidate) { setProblem("请选择本次面试的候选人。"); return; }
    if (!bank || bank.status !== "ready") { setProblem("请选择已准备就绪的岗位题库。"); return; }
    if (!["15", "30", "45", "60", "90"].includes(duration)) { setProblem("请选择面试时长。"); return; }
    busyRef.current = true; setBusy(true); setProblem("");
    setProgress({ stage: "reading" }); setElapsed(0);
    const controller = new AbortController();
    activeRequest.current = controller;
    try {
      const plan = await request(`${API}/interview-plans/prepare`, { method: "POST", timeoutMs: 210000,
        signal: controller.signal, headers: { Accept: "text/event-stream" },
        onProgress: (value) => {
          if (!mounted.current || !["reading", "preparing", "saving"].includes(value?.stage)) return;
          if (value.stage === "preparing" && (!Number.isInteger(value.total_points) || value.total_points < 1
            || !Number.isInteger(value.completed_points) || value.completed_points < 0 || value.completed_points > value.total_points
            || !Number.isInteger(value.reused_points) || value.reused_points < 0 || value.reused_points > value.completed_points)) return;
          setProgress((previous) => ({ ...previous, ...value }));
        }, body: {
        job_position_id: position.id, candidate_profile_id: candidate.id,
        knowledge_base_ids: [bank.id], duration_minutes: Number(duration),
      } });
      if (!plan?.id || plan.status !== "approved") throw new Error("准备结果尚未确认，请到计划列表查看后再操作。");
      if (mounted.current) onCreated(plan);
    } catch (error) {
      if (mounted.current) {
        setProblem(["REQUEST_TIMEOUT", "NETWORK_UNAVAILABLE", "REQUEST_CANCELLED"].includes(error?.code)
          ? "等待已结束，创建结果尚未确认。请先到计划列表查看后再重试，已保留你的选择。"
          : typeof error?.message === "string" && error.message.trim() ? error.message : "暂时无法创建计划，已保留你的选择，请稍后重试。");
        busyRef.current = false; setBusy(false);
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  };

  return <div className="plans-create plans-prepare">
    <header className="plans-create-header">
      <p className="plans-eyebrow">新建面试计划</p>
      <h1>为这次面试创建计划</h1>
      <p>考察内容直接从题库带入，面试官根据交流情况选择问题和追问。</p>
    </header>
    <form className="plans-prepare-layout" onSubmit={submit} noValidate aria-busy={busy}>
      <section className="plans-section plans-prepare-main" aria-labelledby="plans-prepare-heading">
        <h2 id="plans-prepare-heading">这次面试谁</h2>
        {problem && <div className="plans-callout is-warning" role="alert">{problem}</div>}
        {!positions.length && <p className="plans-callout">还没有招聘岗位。<a href="#workflow">先添加岗位和候选人 →</a></p>}
        {position && !candidates.length && <p className="plans-callout">这个岗位还没有候选人。<a href="#workflow">添加候选人 →</a></p>}
        {position && !banks.some((item) => item.status === "ready") && <p className="plans-callout">{banks.length ? "关联题库还在准备中，题目入库和语音就绪后即可使用。" : "这个岗位还没有关联题库。"}<a href={banks.length ? "#questions" : "#workflow"}>{banks.length ? "查看题库进度 →" : "关联已有题库 →"}</a></p>}
        <fieldset className="plans-prepare-fields" disabled={busy}>
          <div className="plans-fields">
            <label className="field"><span>招聘岗位</span><select className="form-select" name="job_position_id" value={positionId} required onChange={(event) => changePosition(event.target.value)}>
              <option value="">选择岗位</option>{positions.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
            </select></label>
            <label className="field"><span>候选人</span><select className="form-select" name="candidate_profile_id" value={candidateId} required onChange={(event) => { setCandidateId(event.target.value); setProblem(""); }}>
              <option value="">选择候选人</option>{candidates.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
            </select></label>
            <label className="field plans-field-full"><span>面试题库</span><select className="form-select" name="knowledge_base_id" value={bankId} required onChange={(event) => { setBankId(event.target.value); setProblem(""); }}>
              <option value="">选择已关联的题库</option>{banks.map((item) => <option value={item.id} key={item.id} disabled={item.status !== "ready"}>{item.name}{item.status !== "ready" ? "（准备中）" : ""}</option>)}
            </select><small className="form-hint">直接使用题目、知识点和评分依据，无需重复填写技能。</small></label>
          </div>
          <details className="plans-disclosure plans-prepare-options"><summary><span>时长与面试定制</span><small>{duration} 分钟 · 按需调整</small></summary>
            <label className="field"><span>面试时长</span><select className="form-select" name="duration_minutes" value={duration} onChange={(event) => setDuration(event.target.value)}>
              {[15, 30, 45, 60, 90].map((value) => <option key={value} value={value}>{value} 分钟</option>)}
            </select></label>
            <p className="form-hint">时间是上限，面试官会根据回答和考察进展收尾。</p>
            <div className="plans-prepare-customization" aria-label="默认面试定制">
              <strong>面试定制</strong>
              <p>{customization?.status === "ready"
                ? [customization.hasSkill ? "使用已保存的 Skill" : "未设置 Skill", customization.hasCompany ? "使用已保存的企业资料" : "未设置企业资料"].join("；")
                : customization?.status === "unavailable" ? "定制状态暂时无法读取，生成时应用已保存定制。" : "正在读取已保存的面试定制…"}</p>
              <p>有就使用，没有也可以面试。<a href="#skills">管理面试定制 →</a></p>
            </div>
          </details>
        </fieldset>
      </section>
      <aside className="plans-prepare-preview" aria-label="本次面试安排">
        <span className="plans-prepare-mark" aria-hidden="true">✦</span>
        <h2>其余交给面试官</h2>
        <p>从真实材料出发，围绕候选人的回答展开交流。</p>
        <dl><div><dt>本场候选人</dt><dd>{candidate?.name || "待选择"}</dd></div><div><dt>考察依据</dt><dd>{bank?.name || "待选择题库"}</dd></div><div><dt>预计时长</dt><dd>{duration} 分钟</dd></div></dl>
        <ul><li>自动准备合适的问题与评分依据</li><li>有可用简历问题，就一起带入</li><li>题量和追问随面试进展调整</li></ul>
        <p className="plans-prepare-next">计划创建后，到「面试会话」安排时间，再通过邀请链接进入面试。</p>
      </aside>
      <footer className="plans-form-actions plans-prepare-actions">
        <button className="button button-secondary" type="button" disabled={busy} onClick={onCancel}>取消</button>
        <div>{busy && <div className="plans-preparation-progress" role="status" aria-live="polite">
          <strong>{progress.stage === "saving" ? "问题已就绪，正在校验并保存计划"
            : progress.stage === "preparing" ? `正在整理问题 · 已完成 ${progress.completed_points} / ${progress.total_points} 个考察点`
            : "正在读取题库和已有简历问题"}</strong>
          {progress.total_points > 0 && <progress aria-label="问题整理进度" max={progress.total_points} value={progress.completed_points} />}
          <small>{progress.reused_points > 0 ? `其中 ${progress.reused_points} 个考察点直接复用。` : "首次整理需要调用模型，后续创建可复用未修改的题目。"}</small>
          <small aria-live="off">已等待 {elapsed} 秒</small>
        </div>}<button className="button button-primary" type="submit" disabled={busy || !position || !candidate || !bank || bank.status !== "ready"}>{busy ? "正在创建计划…" : "创建面试计划"}<span aria-hidden="true"> →</span></button></div>
      </footer>
    </form>
  </div>;
}
