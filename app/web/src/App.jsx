import { useState } from "react";

import { ModalLayer, ToastLayer } from "./core/Overlays.jsx";
import { WorkbenchProvider, useWorkbench } from "./core/WorkbenchProvider.jsx";
import CandidateFeaturePage from "./features/candidate/Page.jsx";
import InterviewsPage from "./features/interviews/Page.jsx";
import ModelsPage from "./features/models/Page.jsx";
import OverviewPage from "./features/overview/Page.jsx";
import PlansPage from "./features/plans/Page.jsx";
import QuestionsPage from "./features/questions/Page.jsx";
import WorkflowPage from "./features/workflow/Page.jsx";
import { workspaceFeatures } from "./features/registry.js";

const pages = { overview: OverviewPage, questions: QuestionsPage, workflow: WorkflowPage, plans: PlansPage, interviews: InterviewsPage, live: InterviewsPage, models: ModelsPage };

export function Shell({ children }) {
  const { auth, route, logout, navigate } = useWorkbench();
  const [mobile, setMobile] = useState(false);
  const visible = workspaceFeatures.filter((feature) => feature.roles.some((role) => auth?.roles?.includes(role)));
  return <div className="app-shell"><button className={`mobile-backdrop${mobile ? " is-visible" : ""}`} onClick={() => setMobile(false)} aria-label="关闭导航" /><aside className={`sidebar${mobile ? " is-open" : ""}`} aria-label="主导航"><a className="brand" href="#overview"><span className="brand-mark">I</span><span><strong>Interviewer</strong><small>智能面试工作台</small></span></a><nav className="nav-list">{visible.map((feature) => <button className={`nav-item${(route.view === "live" ? "interviews" : route.view) === feature.id ? " is-active" : ""}`} type="button" data-view={feature.id} key={feature.id} onClick={() => { navigate(feature.id); setMobile(false); }}><span>{feature.label}</span></button>)}</nav><div className="sidebar-footer"><div className="storage-status"><span className="status-indicator" /><span><strong>React 工作台</strong><small>API 同源连接</small></span></div><div className="account-row"><span className="account-avatar">{auth?.roles?.includes("admin") ? "管" : auth?.roles?.includes("reviewer") ? "复" : "面"}</span><span><strong>{auth?.actor_id}</strong><small>{auth?.organization_id}</small></span><button className="icon-button" onClick={logout} aria-label="退出">退出</button></div></div></aside><main className="main-shell"><header className="topbar"><button className="icon-button mobile-menu" onClick={() => setMobile(true)}>☰</button><div><span className="topbar-kicker">工作空间</span><strong>{workspaceFeatures.find((item) => item.id === (route.view === "live" ? "interviews" : route.view))?.label}</strong></div><span className="api-status"><span />API 在线</span></header><div className="content" aria-live="polite">{children}</div></main></div>;
}

function Login() {
  const { login, toast } = useWorkbench(); const [busy, setBusy] = useState(false);
  const submit = async (event) => { event.preventDefault(); setBusy(true); try { await login(new FormData(event.currentTarget).get("access_token")); } catch (error) { toast("认证失败", error.message, "error"); setBusy(false); } };
  return <main className="auth-gate"><section className="panel auth-panel"><h1>连接企业工作台</h1><p>请输入后台 Bearer token，凭据仅保存在 sessionStorage。</p><form onSubmit={submit}><label className="field"><span>Bearer token</span><input className="form-input" name="access_token" type="password" required /></label><button className="button button-primary" disabled={busy}>{busy ? "验证中…" : "进入工作台"}</button></form></section></main>;
}

function WorkbenchApp() {
  const { route, authRequired, loading, fatalError } = useWorkbench();
  if (["candidate", "invite"].includes(route.view)) return <CandidateFeaturePage />;
  if (authRequired) return <Login />;
  const Page = pages[route.view] || OverviewPage;
  return <Shell>{loading ? <div className="loading-state"><span className="spinner" />正在读取工作区数据</div> : fatalError ? <div className="empty-state"><strong>工作区加载失败</strong><span>{fatalError}</span></div> : <Page />}</Shell>;
}

export default function App() {
  return <WorkbenchProvider><WorkbenchApp /><ModalLayer /><ToastLayer /></WorkbenchProvider>;
}
