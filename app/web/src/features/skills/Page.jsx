import { useEffect, useRef, useState } from "react";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty } from "../../core/ui.jsx";
import "./skills.css";

const COMPANY_FIELDS = [
  ["company_name", "企业名称", "例如：星河科技"],
  ["business_overview", "企业介绍", "介绍企业的业务方向、服务对象或团队情况"],
  ["products_services", "产品与服务", "介绍主要产品、服务和应用场景"],
  ["additional_info", "其他补充", "其他可以向候选人介绍的信息"],
];
const emptyCompany = () => Object.fromEntries(COMPANY_FIELDS.map(([key]) => [key, ""]));
const companyLength = (value) => COMPANY_FIELDS.reduce((total, [key]) => total + value[key].length, 0);
const companyContextLength = (value) => [
  ["company_name", "公司名称"], ["business_overview", "业务介绍"], ["products_services", "产品与服务"], ["additional_info", "其他资料"],
].filter(([key]) => value[key].trim()).map(([key, label]) => `${label}：\n${value[key].trim()}`).join("\n\n").length;
const companyBudgetLength = (value) => Math.max(companyLength(value), companyContextLength(value));
const companyHasContent = (value) => COMPANY_FIELDS.some(([key]) => value[key].trim());
const companyEqual = (first, second) => COMPANY_FIELDS.every(([key]) => first[key] === second[key]);
const savedSkill = (value) => value?.skill?.instructions || "";
const usableSkill = (value) => ["active", "approved"].includes(value?.skill?.status) && Boolean(savedSkill(value).trim());
const readCustomization = (value) => {
  if (!Number.isInteger(value?.version) || value.version < 0 || !value.company_profile
    || COMPANY_FIELDS.some(([key]) => typeof value.company_profile[key] !== "string")
    || (value.skill != null && typeof value.skill.instructions !== "string")) throw new Error("Invalid customization response");
  return { ...value, company_profile: Object.fromEntries(COMPANY_FIELDS.map(([key]) => [key, value.company_profile[key]])) };
};
const changedStatus = (changed, configured) => changed ? "未保存" : configured ? "已设置" : "未设置";
const validationCopy = (error) => {
  const fields = error?.details?.fields;
  return Array.isArray(fields) ? [...new Set(fields.map((item) => item?.message)
    .filter((message) => typeof message === "string" && message.trim()).map((message) => message.replace(/^Value error,\s*/, "")))].slice(0, 3).join("；") : "";
};

export default function SkillsPage() {
  const { auth, API } = useWorkbench();
  const permitted = auth?.roles?.some((role) => ["admin", "interviewer"].includes(role));
  if (!permitted) return <Empty title="需要面试配置权限" copy="管理员或面试官可以编辑面试定制。" />;
  return <CustomizationEditor key={`${API}:${auth?.organization_id || ""}:${auth?.actor_id || ""}`} />;
}

function CustomizationEditor() {
  const { API, request } = useWorkbench();
  const [saved, setSaved] = useState(null);
  const [instructions, setInstructions] = useState("");
  const [company, setCompany] = useState(emptyCompany);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [loadProblem, setLoadProblem] = useState("");
  const [problems, setProblems] = useState({ skill: "", company: "" });
  const [messages, setMessages] = useState({ skill: "", company: "" });
  const [needsReload, setNeedsReload] = useState(false);
  const [latestVisible, setLatestVisible] = useState(false);
  const [confirmUpdate, setConfirmUpdate] = useState({ skill: false, company: false });
  const [refreshNote, setRefreshNote] = useState("");
  const generation = useRef(0);
  const busyRef = useRef(false);
  const initialized = useRef(false);
  const skillSection = useRef(null);
  const companySection = useRef(null);
  const base = `${API}/interview-customization`;
  const skillChanged = Boolean(saved && instructions !== savedSkill(saved));
  const companyChanged = Boolean(saved && !companyEqual(company, saved.company_profile));

  const applyRead = (result) => {
    setSaved(result);
    if (!initialized.current) {
      setInstructions(savedSkill(result)); setCompany(result.company_profile); initialized.current = true;
    } else {
      setLatestVisible(true);
      setConfirmUpdate({ skill: true, company: true });
      setRefreshNote("已读取最新保存内容，当前输入保持不变。请对照下方内容；选择“用当前内容更新”会替换对应部分。");
    }
  };
  useEffect(() => {
    const token = ++generation.current;
    busyRef.current = false; setBusy(null); setLoading(true); setLoadProblem("");
    request(base).then((value) => {
      const result = readCustomization(value);
      if (token === generation.current) applyRead(result);
    }).catch(() => {
      if (token === generation.current) setLoadProblem("面试定制暂时无法读取，请重试。");
    }).finally(() => { if (token === generation.current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [base, request]);

  const reload = async () => {
    if (busyRef.current) return;
    busyRef.current = true; setLoading(true); setLoadProblem("");
    const token = ++generation.current;
    try {
      const result = readCustomization(await request(base));
      if (token !== generation.current) return;
      applyRead(result); setNeedsReload(false); setProblems({ skill: "", company: "" });
    } catch {
      if (token === generation.current) setLoadProblem("最新内容读取失败，你的输入仍保留，请稍后重试。");
    } finally {
      if (token === generation.current) { busyRef.current = false; setLoading(false); }
    }
  };
  const save = async (module, event) => {
    event.preventDefault();
    if (busyRef.current || loading || !saved || needsReload) return;
    const invalid = module === "skill" && instructions.length > 6000 ? "Skill 正文最多 6000 字。"
      : module === "company" && companyBudgetLength(company) > 12000 ? "企业资料连同栏目标题不能超过 12000 字。" : "";
    setProblems((current) => ({ ...current, [module]: invalid }));
    if (invalid) return;
    busyRef.current = true; setBusy(module); setRefreshNote("");
    setMessages((current) => ({ ...current, [module]: "" }));
    const token = ++generation.current;
    const body = { expected_version: saved.version,
      ...(module === "skill" ? { skill_instructions: instructions } : { company_profile: { ...company } }),
    };
    try {
      const result = readCustomization(await request(base, { method: "PATCH", body }));
      if (token !== generation.current) return;
      setSaved(result);
      setConfirmUpdate((current) => ({ ...current, [module]: false }));
      if (module === "skill") setInstructions(savedSkill(result));
      else setCompany(result.company_profile);
      setMessages((current) => ({ ...current, [module]: module === "skill"
        ? savedSkill(result).trim() ? "Skill 已保存，新面试会默认使用。" : "已保存，新面试不使用 Skill 定制。"
        : companyHasContent(result.company_profile) ? "企业资料已保存，新面试会默认使用。" : "已保存，新面试不使用企业资料。" }));
    } catch (error) {
      if (token !== generation.current) return;
      const conflict = error?.status === 409 || error?.status_code === 409 || error?.code === "PERSISTENCE_CONFLICT";
      const uncertain = !error?.status || ["REQUEST_TIMEOUT", "NETWORK_UNAVAILABLE"].includes(error?.code);
      setNeedsReload(conflict || uncertain);
      setProblems((current) => ({ ...current, [module]: conflict
        ? "保存内容已发生变化，你的输入仍保留。请先读取最新版本，再决定是否保存。"
        : uncertain ? "保存结果尚未确认，你的输入仍保留。请先读取最新版本确认结果。"
          : error?.status === 422 && validationCopy(error) ? validationCopy(error)
            : "保存未完成，你的输入仍保留。请检查内容或稍后重试。" }));
    } finally {
      if (token === generation.current) { busyRef.current = false; setBusy(null); }
    }
  };
  const editSkill = (value) => {
    setInstructions(value); setProblems((current) => ({ ...current, skill: "" })); setMessages((current) => ({ ...current, skill: "" }));
  };
  const editCompany = (key, value) => {
    setCompany((current) => ({ ...current, [key]: value }));
    setProblems((current) => ({ ...current, company: "" })); setMessages((current) => ({ ...current, company: "" }));
  };
  const disabled = loading || Boolean(busy) || !saved || needsReload;

  return <div className="customization-page">
    <header className="customization-heading"><p className="customization-eyebrow">让面试官更了解你的需要</p><h1>面试定制</h1>
      <p>保存后，新面试默认使用；未填写则不使用。</p></header>
    <div className="customization-intro">两部分可以分别保存。更新用于之后新生成的面试计划，已有计划和进行中的面试保留原有内容。</div>
    <nav className="customization-section-nav" aria-label="跳转到定制模块">
      <button className="button button-secondary" type="button" onClick={() => { skillSection.current?.querySelector("h2")?.focus({ preventScroll: true }); skillSection.current?.scrollIntoView?.({ block: "start", behavior: "auto" }); }}>Skill 定制</button>
      <button className="button button-secondary" type="button" onClick={() => { companySection.current?.querySelector("h2")?.focus({ preventScroll: true }); companySection.current?.scrollIntoView?.({ block: "start", behavior: "auto" }); }}>企业资料</button>
    </nav>
    {loading && <p className="customization-notice" role="status">正在读取面试定制…</p>}
    {loadProblem && <div className="customization-notice is-warning" role="alert"><p>{loadProblem}</p><button className="button button-secondary" disabled={loading || Boolean(busy)} onClick={reload}>重新读取</button></div>}
    {needsReload && !loadProblem && <div className="customization-notice is-warning"><p>读取最新内容不会覆盖当前输入。请查看最新内容后，再保存需要更新的部分。</p><button className="button button-secondary" disabled={loading || Boolean(busy)} onClick={reload}>读取最新版本</button></div>}
    {refreshNote && <p className="customization-notice" role="status">{refreshNote}</p>}
    <div className="customization-grid">
      <section className="customization-card" ref={skillSection} aria-labelledby="customization-skill-title">
        <header className="customization-card-heading"><div><h2 id="customization-skill-title" tabIndex={-1}>Skill 定制</h2><p>按你的方式交流、提问和追问。</p></div>
          {saved && <span className={`customization-state${skillChanged ? " is-dirty" : usableSkill(saved) ? " is-set" : ""}`}>{!skillChanged && savedSkill(saved).trim() && !usableSkill(saved) ? "已停用" : changedStatus(skillChanged, usableSkill(saved))}</span>}
        </header>
        {savedSkill(saved).trim() && !usableSkill(saved) && <p className="customization-company-hint">当前 Skill 已停用，新面试暂不使用；保存后会重新启用。</p>}
        <form onSubmit={(event) => save("skill", event)} noValidate aria-label="Skill 定制" aria-busy={busy === "skill"}>
          <label className="customization-field"><span>Skill 正文</span><textarea className="form-textarea customization-skill-editor" name="skill_instructions"
            value={instructions} onChange={(event) => editSkill(event.target.value)} disabled={!saved || busy === "skill"}
            maxLength={6000} rows={19} placeholder="在这里写下你的面试方法与偏好，也可以直接粘贴 Markdown。" />
            <small>自由编写，无固定模板。留空并保存后不使用 Skill 定制。</small></label>
          <div className="customization-counter">{instructions.length} / 6000 字</div>
          {problems.skill && <p className="customization-feedback is-error" role="alert">{problems.skill}</p>}
          {messages.skill && <p className="customization-feedback" role="status">{messages.skill}</p>}
          {latestVisible && saved && <details className="customization-latest" open={skillChanged}><summary>最新已保存的 Skill</summary><pre className="skill-prose">{savedSkill(saved) || "未填写"}</pre></details>}
          <footer className="customization-actions"><span>{skillChanged ? "当前修改尚未保存" : "仅保存 Skill 定制"}</span><button className="button button-primary" disabled={disabled}>{busy === "skill" ? "保存中…" : confirmUpdate.skill && skillChanged ? "用当前内容更新 Skill" : "保存 Skill"}</button></footer>
        </form>
      </section>
      <section className="customization-card" ref={companySection} aria-labelledby="customization-company-title">
        <header className="customization-card-heading"><div><h2 id="customization-company-title" tabIndex={-1}>企业资料</h2><p>让面试官能介绍企业，并回应相关提问。</p></div>
          {saved && <span className={`customization-state${companyChanged ? " is-dirty" : companyHasContent(saved.company_profile) ? " is-set" : ""}`}>{changedStatus(companyChanged, companyHasContent(saved.company_profile))}</span>}
        </header>
        <p className="customization-company-hint">只填写可以向候选人介绍的信息。各项均可留空。</p>
        <form onSubmit={(event) => save("company", event)} noValidate aria-label="企业资料" aria-busy={busy === "company"}>
          <div className="customization-company-fields">{COMPANY_FIELDS.map(([key, label, placeholder]) => <label className="customization-field" key={key}><span>{label}</span>
            {key === "company_name" ? <input className="form-input" name={key} value={company[key]} onChange={(event) => editCompany(key, event.target.value)} disabled={!saved || busy === "company"} maxLength={12000} placeholder={placeholder} />
              : <textarea className="form-textarea" name={key} value={company[key]} onChange={(event) => editCompany(key, event.target.value)} disabled={!saved || busy === "company"} maxLength={12000} rows={key === "business_overview" ? 4 : 3} placeholder={placeholder} />}
          </label>)}</div>
          <div className="customization-counter">资料总长度 {companyBudgetLength(company)} / 12000 字</div>
          {problems.company && <p className="customization-feedback is-error" role="alert">{problems.company}</p>}
          {messages.company && <p className="customization-feedback" role="status">{messages.company}</p>}
          {latestVisible && saved && <details className="customization-latest" open={companyChanged}><summary>最新已保存的企业资料</summary><dl>{COMPANY_FIELDS.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{saved.company_profile[key] || "未填写"}</dd></div>)}</dl></details>}
          <footer className="customization-actions"><span>{companyChanged ? "当前修改尚未保存" : "仅保存企业资料"}</span><button className="button button-primary" disabled={disabled}>{busy === "company" ? "保存中…" : confirmUpdate.company && companyChanged ? "用当前内容更新企业资料" : "保存企业资料"}</button></footer>
        </form>
      </section>
    </div>
  </div>;
}
