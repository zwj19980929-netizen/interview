const API = "/api/v1";

const state = {
  view: "overview",
  questions: [],
  positions: [],
  knowledgeBases: [],
  candidates: [],
  appointments: [],
  roles: [],
  plans: [],
  interviews: [],
  catalog: [],
  providerConnections: [],
  modelConfigurations: [],
  routes: [],
  questionResults: null,
  selectedInterview: null,
  activeEvaluation: null,
  report: null,
  socket: null,
  candidateToken: null,
  invitationToken: null,
  publicInvitation: null,
  candidateMediaStream: null,
  candidateRecorder: null,
  candidateRecognition: null,
  candidateDevices: { audio: [], video: [] },
  candidateAudioEnabled: true,
  candidateVideoEnabled: true,
  candidateRecording: false,
  candidateRecordingPending: false,
  candidateAudioUri: null,
  candidateAudioMimeType: "audio/webm;codecs=opus",
  candidateTranscript: "",
  candidateInterimTranscript: "",
  candidateRecordingStartedAt: null,
  candidateRecordingDuration: 0,
  candidateTimer: null,
  candidateLastEvaluation: null,
  avatarSpeech: null,
  liveTranscript: "",
};

const viewTitles = {
  overview: "总览",
  questions: "题库",
  workflow: "招聘流程",
  plans: "面试计划",
  interviews: "面试会话",
  live: "实时面试",
  models: "模型服务",
  candidate: "候选人面试",
  invite: "面试邀请",
};

const statusLabels = {
  active: "可用",
  indexed: "已索引",
  pending: "处理中",
  processing: "安全处理中",
  queued: "等待处理",
  retry_scheduled: "等待重试",
  dead_letter: "待人工重放",
  failed: "处理失败",
  draft: "草稿",
  approved: "已确认",
  scheduled: "待开始",
  waiting: "等待开始",
  in_progress: "进行中",
  paused: "已暂停",
  cancelled: "已取消",
  completed: "已完成",
  report_generating: "报告生成中",
  report_ready: "报告已生成",
  asking: "正在提问",
  answering: "正在作答",
  evaluating: "评分中",
  skipped: "已跳过",
  strong_advance: "强烈推荐推进",
  advance: "建议推进",
  hold: "暂缓决定",
  reject: "不建议推进",
  manual_review: "人工复核",
  strong_match: "高度匹配",
  match: "匹配",
  partial_match: "部分匹配",
  insufficient_evidence: "证据不足",
  ready: "已就绪",
  building: "构建中",
  invited: "已邀请",
  registered: "已登记",
  consumed: "已使用",
  transcribing: "服务端转写中",
};

const difficultyLabels = {
  junior: "初级",
  mid: "中级",
  senior: "高级",
  expert: "专家",
};

const recommendationLabels = {
  strong_advance: "强烈推荐推进",
  advance: "建议推进",
  hold: "暂缓决定",
  reject: "不建议推进",
  manual_review: "人工复核",
  strong_match: "高度匹配",
  match: "匹配",
  partial_match: "部分匹配",
  insufficient_evidence: "证据不足",
};

const appContent = document.querySelector("#app-content");
const modalRoot = document.querySelector("#modal-root");
const toastRegion = document.querySelector("#toast-region");
const sidebar = document.querySelector("#sidebar");
const mobileBackdrop = document.querySelector("#mobile-backdrop");
const quickAction = document.querySelector("#quick-action");

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindShell();
  syncRouteFromHash();
  try {
    await loadRouteData();
    render();
  } catch (error) {
    renderFatalError(error);
  }
}

function bindShell() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.view));
  });

  document.querySelector("#mobile-menu").addEventListener("click", openMobileNav);
  mobileBackdrop.addEventListener("click", closeMobileNav);
  quickAction.addEventListener("click", runQuickAction);
  window.addEventListener("hashchange", async () => {
    syncRouteFromHash();
    await loadRouteData();
    render();
  });

  appContent.addEventListener("click", handleContentAction);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
}

async function loadRouteData() {
  if (state.view === "invite") {
    stopCandidateMedia();
    await hydrateInvitationRoute();
    return;
  }
  if (state.view === "candidate") {
    await hydrateCandidateRoute();
    return;
  }
  stopCandidateMedia();
  if (!state.catalog.length) await loadWorkspace();
  await hydrateRoute();
}

async function loadWorkspace() {
  const [roles, plans, interviews, catalog, providerConnections, modelConfigurations, routes, positions, candidates, appointments] = await Promise.all([
    api(`${API}/role-requirements`),
    api(`${API}/interview-plans`),
    api(`${API}/interviews`),
    api(`${API}/admin/model-providers/catalog`),
    api(`${API}/admin/model-provider-connections`),
    api(`${API}/admin/model-configurations`),
    api(`${API}/admin/model-routes`),
    api(`${API}/job-positions`),
    api(`${API}/candidate-profiles`),
    api(`${API}/interview-appointments`),
  ]);
  state.roles = newestFirst(roles.items);
  state.plans = newestFirst(plans.items);
  state.interviews = newestFirst(interviews.items);
  state.catalog = catalog.items;
  state.providerConnections = newestFirst(providerConnections.items);
  state.modelConfigurations = newestFirst(modelConfigurations.items);
  state.routes = newestFirst(routes.items);
  state.positions = newestFirst(positions.items);
  state.candidates = newestFirst(candidates.items);
  state.appointments = newestFirst(appointments.items);
  const knowledgeBaseGroups = await Promise.all(
    state.positions.map((item) => api(`${API}/job-positions/${encodeURIComponent(item.id)}/knowledge-bases`))
  );
  state.knowledgeBases = newestFirst(knowledgeBaseGroups.flatMap((item) => item.items || []));
  const questionGroups = await Promise.all(
    state.knowledgeBases.map((item) => api(`${API}/knowledge-bases/${encodeURIComponent(item.id)}/questions`))
  );
  state.questions = newestFirst(questionGroups.flatMap((item) => item.items || []));
}

async function refreshCollection(name) {
  if (name === "questions") {
    const groups = await Promise.all(
      state.knowledgeBases.map((item) => api(`${API}/knowledge-bases/${encodeURIComponent(item.id)}/questions`))
    );
    state.questions = newestFirst(groups.flatMap((item) => item.items || []));
    return;
  }
  const endpoints = {
    positions: `${API}/job-positions`,
    candidates: `${API}/candidate-profiles`,
    appointments: `${API}/interview-appointments`,
    roles: `${API}/role-requirements`,
    plans: `${API}/interview-plans`,
    interviews: `${API}/interviews`,
    providerConnections: `${API}/admin/model-provider-connections`,
    modelConfigurations: `${API}/admin/model-configurations`,
    routes: `${API}/admin/model-routes`,
  };
  const data = await api(endpoints[name]);
  state[name] = newestFirst(data.items);
}

function newestFirst(items) {
  return [...(items || [])].sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
}

function syncRouteFromHash() {
  const route = window.location.hash.replace(/^#\/?/, "") || "overview";
  const [path, query = ""] = route.split("?", 2);
  const [view, id] = path.split("/");
  if (view === "interviews" && id) {
    state.view = "live";
    state.selectedInterviewId = id;
  } else if (view === "candidate" && id) {
    state.view = "candidate";
    state.selectedInterviewId = id;
    const suppliedToken = new URLSearchParams(query).get("token");
    const storageKey = `candidate-session:${id}`;
    if (suppliedToken) {
      state.candidateToken = suppliedToken;
      window.sessionStorage.setItem(storageKey, suppliedToken);
      window.history.replaceState(null, "", `#candidate/${id}`);
    } else {
      state.candidateToken = window.sessionStorage.getItem(storageKey);
    }
  } else if (view === "invite" && id) {
    state.view = "invite";
    state.invitationToken = id;
    state.selectedInterviewId = null;
  } else if (["overview", "questions", "workflow", "plans", "interviews", "models"].includes(view)) {
    state.view = view;
    state.selectedInterviewId = null;
  } else {
    state.view = "overview";
    state.selectedInterviewId = null;
  }
}

async function hydrateInvitationRoute() {
  disconnectSocket();
  state.publicInvitation = null;
  if (!state.invitationToken) return;
  try {
    state.publicInvitation = await api(`${API}/public/interview-invitations/${encodeURIComponent(state.invitationToken)}`);
  } catch (error) {
    toast("邀请链接不可用", error.message, "error");
  }
}

async function hydrateCandidateRoute() {
  disconnectSocket();
  if (!state.selectedInterviewId) return;
  try {
    state.selectedInterview = await candidateApi();
    state.report = null;
  } catch (error) {
    state.selectedInterview = null;
    toast("无法进入面试", error.message, "error");
  }
}

function candidateSessionApi(suffix = "") {
  return `${API}/public/interviews/${encodeURIComponent(state.selectedInterviewId)}${suffix}`;
}

function candidateApi(suffix = "", options = {}) {
  return api(candidateSessionApi(suffix), {
    ...options,
    headers: { ...(options.headers || {}), "X-Candidate-Session-Token": state.candidateToken || "" },
  });
}

async function hydrateRoute() {
  disconnectSocket();
  state.selectedInterview = null;
  state.report = null;
  state.activeEvaluation = null;
  if (state.view !== "live" || !state.selectedInterviewId) return;
  try {
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterviewId)}`);
    if (state.selectedInterview.status === "report_ready") {
      state.report = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterviewId)}/report`);
    }
    if (state.selectedInterview.status === "in_progress") connectSocket(state.selectedInterview.id);
  } catch (error) {
    toast("无法打开面试", error.message, "error");
    state.view = "interviews";
    window.history.replaceState(null, "", "#interviews");
  }
}

function navigate(view, id = null) {
  closeMobileNav();
  const hash = id ? `${view}/${id}` : view;
  if (window.location.hash === `#${hash}`) {
    syncRouteFromHash();
    hydrateRoute().then(render);
    return;
  }
  window.location.hash = hash;
}

function render() {
  updateShell();
  const renderers = {
    overview: renderOverview,
    questions: renderQuestions,
    workflow: renderWorkflow,
    plans: renderPlans,
    interviews: renderInterviews,
    live: renderLiveInterview,
    models: renderModels,
    candidate: renderCandidateRoom,
    invite: renderInvitation,
  };
  appContent.innerHTML = renderers[state.view]();
  bindViewEvents();
  iconize();
}

function updateShell() {
  document.body.classList.toggle("candidate-mode", ["candidate", "invite"].includes(state.view));
  document.querySelector("#topbar-title").textContent = viewTitles[state.view];
  document.querySelectorAll("[data-view]").forEach((button) => {
    const target = state.view === "live" ? "interviews" : state.view;
    button.classList.toggle("is-active", button.dataset.view === target);
  });

  const actions = {
    overview: ["新建题目", "plus"],
    questions: ["新建题目", "plus"],
    workflow: ["新建岗位", "briefcase-business"],
    plans: ["新建岗位", "briefcase-business"],
    interviews: ["创建预约", "calendar-plus"],
    live: ["返回会话", "arrow-left"],
    models: ["添加配置", "plug-zap"],
    candidate: ["候选人面试", "video"],
    invite: ["邀请登记", "shield-check"],
  };
  const [label, icon] = actions[state.view];
  quickAction.innerHTML = `<i data-lucide="${icon}" aria-hidden="true"></i><span>${label}</span>`;
}

function renderOverview() {
  const completed = state.interviews.filter((item) => item.status === "report_ready").length;
  const inProgress = state.interviews.filter((item) => item.status === "in_progress").length;
  const implementedProviders = state.catalog.filter((item) => item.implemented).length;
  const recentQuestions = state.questions.slice(0, 5);
  const recentInterviews = state.interviews.slice(0, 4);

  return `
    <section class="page-header">
      <div>
        <h1>面试运营台</h1>
        <p>${formatLongDate(new Date())} · ${inProgress ? `${inProgress} 场面试正在进行` : "当前没有进行中的面试"}</p>
      </div>
      <div class="page-actions">
        <button class="button button-secondary" type="button" data-action="create-role">
          <i data-lucide="briefcase-business" aria-hidden="true"></i>新建岗位
        </button>
        <button class="button button-primary" type="button" data-action="create-interview">
          <i data-lucide="calendar-plus" aria-hidden="true"></i>创建预约
        </button>
      </div>
    </section>

    <section class="metric-grid" aria-label="工作区指标">
      ${metricCard("题库题目", state.questions.length, `${state.questions.filter((item) => item.index_status === "indexed").length} 道已完成索引`, "library-big")}
      ${metricCard("面试计划", state.plans.length, `${state.roles.length} 个岗位要求`, "clipboard-list")}
      ${metricCard("面试会话", state.interviews.length, `${completed} 份报告已生成`, "video")}
      ${metricCard("模型插件", state.catalog.length, `${implementedProviders} 个已实现调用`, "cpu")}
    </section>

    <section class="section-block">
      <div class="section-title-row">
        <div><h2>业务闭环</h2><p>当前工作区资源状态</p></div>
      </div>
      <div class="workflow-band">
        ${workflowStep(1, "题库", state.questions.length ? `${state.questions.length} 道可用题目` : "尚无题目")}
        ${workflowStep(2, "岗位与计划", state.plans.length ? `${state.plans.length} 份面试计划` : "尚无计划")}
        ${workflowStep(3, "面试会话", state.interviews.length ? `${state.interviews.length} 场面试` : "尚无面试")}
        ${workflowStep(4, "评分报告", completed ? `${completed} 份报告` : "尚无报告")}
      </div>
    </section>

    <div class="two-column section-block">
      <section>
        <div class="section-title-row">
          <div><h2>最近题目</h2><p>按创建时间排序</p></div>
          <button class="button button-secondary button-small" type="button" data-action="go-questions">查看题库<i data-lucide="arrow-right" aria-hidden="true"></i></button>
        </div>
        ${recentQuestions.length ? questionTable(recentQuestions, false) : emptyState("library-big", "题库为空", "尚未创建面试题目")}
      </section>
      <section>
        <div class="section-title-row">
          <div><h2>最近会话</h2><p>候选人与面试状态</p></div>
          <button class="icon-button" type="button" title="刷新会话" aria-label="刷新会话" data-action="refresh-interviews"><i data-lucide="refresh-cw" aria-hidden="true"></i></button>
        </div>
        ${recentInterviews.length ? recentInterviewList(recentInterviews) : emptyState("video", "暂无面试", "当前没有候选人会话")}
      </section>
    </div>
  `;
}

function renderWorkflow() {
  const positionCards = state.positions.map((position) => {
    const bases = state.knowledgeBases.filter((item) => item.job_position_id === position.id);
    return `<article class="question-card"><div class="question-card-top"><div><span class="question-eyebrow">岗位 · ${escapeHtml(position.code)}</span><h3>${escapeHtml(position.name)}</h3></div><span class="status-badge status-${escapeHtml(position.status)}">${statusLabel(position.status)}</span></div><p class="question-copy">${escapeHtml(position.description || "尚未填写岗位说明")}</p><div class="tag-list">${bases.map((item) => `<span class="tag">${escapeHtml(item.name)} · ${statusLabel(item.status)}</span>`).join("") || '<span class="tag">尚无岗位题库</span>'}</div><div class="card-actions"><button class="button button-secondary button-small" type="button" data-action="create-knowledge-base" data-id="${escapeHtml(position.id)}"><i data-lucide="library-big" aria-hidden="true"></i>添加题库</button></div></article>`;
  }).join("");
  const candidateRows = state.candidates.map((candidate) => `<tr><td><span class="cell-title">${escapeHtml(candidate.name)}</span></td><td>${escapeHtml(candidate.email)}</td><td>${escapeHtml(candidate.phone)}</td><td><button class="button button-secondary button-small" type="button" data-action="upload-resume" data-id="${escapeHtml(candidate.id)}"><i data-lucide="file-up" aria-hidden="true"></i>上传简历</button></td></tr>`).join("");
  const appointmentRows = state.appointments.map((item) => {
    const position = state.positions.find((candidate) => candidate.id === item.job_position_id);
    const candidate = state.candidates.find((value) => value.id === item.candidate_profile_id);
    return `<tr><td><span class="cell-title">${escapeHtml(candidate?.name || item.candidate_profile_id)}</span></td><td>${escapeHtml(position?.name || item.job_position_id)}</td><td><span class="status-badge status-${escapeHtml(item.status)}">${statusLabel(item.status)}</span></td><td>${formatDate(item.scheduled_start_at)}</td></tr>`;
  }).join("");
  return `
    <section class="page-header"><div><h1>岗位到复核的业务闭环</h1><p>岗位题库、简历证据、预约匹配、随机抽题、服务端 STT 与人工复核</p></div><div class="page-actions"><button class="button button-secondary" type="button" data-action="create-candidate-profile"><i data-lucide="user-plus"></i>录入候选人</button><button class="button button-primary" type="button" data-action="create-position"><i data-lucide="briefcase-business"></i>新建岗位</button></div></section>
    <section class="workflow-band section-block">
      ${workflowStep(1, "岗位", `${state.positions.length} 个岗位`)}
      ${workflowStep(2, "岗位题库", `${state.knowledgeBases.length} 个题库`)}
      ${workflowStep(3, "简历库", `${state.candidates.length} 位候选人`)}
      ${workflowStep(4, "AI 审阅", "证据问题需人工批准")}
      ${workflowStep(5, "计划与预约", `${state.appointments.length} 个预约`)}
      ${workflowStep(6, "语音面试", "服务端转写后逐题评分")}
      ${workflowStep(7, "企业复核", "人员作最终决定")}
    </section>
    <section class="panel"><div class="section-title-row"><div><h2>岗位与岗位题库</h2><p>题库只归属一个岗位；语音全部 ready 后才可进入计划</p></div></div>${positionCards ? `<div class="question-grid">${positionCards}</div>` : emptyState("briefcase-business", "尚无岗位", "先建立稳定岗位，再为岗位添加一个或多个题库")}</section>
    <section class="panel"><div class="section-title-row"><div><h2>企业简历库</h2><p>候选人联系方式用于预约强匹配，AI 只提取岗位相关证据</p></div></div>${candidateRows ? `<div class="table-wrap"><table><thead><tr><th>候选人</th><th>邮箱</th><th>手机</th><th>操作</th></tr></thead><tbody>${candidateRows}</tbody></table></div>` : emptyState("users", "尚无候选人", "录入姓名、邮箱和手机号后上传简历")}</section>
    <section class="panel"><div class="section-title-row"><div><h2>预约状态</h2><p>邀请 token 一次性使用；姓名加邮箱或手机号匹配后 self-start</p></div></div>${appointmentRows ? `<div class="table-wrap"><table><thead><tr><th>候选人</th><th>岗位</th><th>状态</th><th>预约时间</th></tr></thead><tbody>${appointmentRows}</tbody></table></div>` : emptyState("calendar-clock", "尚无预约", "批准候选人专属计划后创建预约")}</section>`;
}

function renderQuestions() {
  const items = state.questionResults === null ? state.questions : state.questionResults;
  const resultLabel = state.questionResults === null ? `${state.questions.length} 道题目` : `${items.length} 条搜索结果`;
  const scopeOptions = state.knowledgeBases.map((knowledgeBase) => {
    const position = state.positions.find((item) => item.id === knowledgeBase.job_position_id);
    return `<option value="${escapeHtml(knowledgeBase.id)}">${escapeHtml(position?.name || "岗位")} · ${escapeHtml(knowledgeBase.name)}</option>`;
  }).join("");
  return `
    <section class="page-header">
      <div><h1>题库</h1><p>标准答案、关键点与检索索引 · ${resultLabel}</p></div>
      <div class="page-actions">
        ${state.questionResults !== null ? `<button class="button button-secondary" type="button" data-action="clear-search"><i data-lucide="rotate-ccw" aria-hidden="true"></i>清除搜索</button>` : ""}
        <button class="button button-primary" type="button" data-action="create-question"><i data-lucide="plus" aria-hidden="true"></i>新建题目</button>
      </div>
    </section>

    <form class="search-toolbar" id="question-search-form">
      <div class="input-with-icon">
        <i data-lucide="search" aria-hidden="true"></i>
        <input class="form-input" id="question-search-query" name="query" placeholder="搜索题目、技能或岗位要求" required />
      </div>
      <select class="form-select" name="difficulty" aria-label="难度筛选">
        <option value="">全部难度</option>
        <option value="junior">初级</option>
        <option value="mid">中级</option>
        <option value="senior">高级</option>
        <option value="expert">专家</option>
      </select>
      <select class="form-select" name="knowledge_base_id" aria-label="岗位题库范围" required>
        <option value="">选择岗位题库</option>
        ${scopeOptions}
      </select>
      <button class="button button-secondary" type="submit" ${scopeOptions ? "" : "disabled"}><i data-lucide="search" aria-hidden="true"></i>检索</button>
    </form>

    ${items.length ? questionTable(items, state.questionResults !== null) : emptyState("search-x", state.questionResults === null ? "题库为空" : "没有匹配结果", state.questionResults === null ? "尚未创建面试题目" : "请调整检索条件")}
  `;
}

function renderPlans() {
  return `
    <section class="page-header">
      <div><h1>面试计划</h1><p>岗位画像与题目组合 · ${state.plans.length} 份计划</p></div>
      <div class="page-actions">
        <button class="button button-secondary" type="button" data-action="create-role"><i data-lucide="briefcase-business" aria-hidden="true"></i>新建岗位</button>
        <button class="button button-primary" type="button" data-action="generate-plan" ${state.roles.length && state.questions.length ? "" : "disabled"}><i data-lucide="wand-sparkles" aria-hidden="true"></i>生成计划</button>
      </div>
    </section>

    <div class="two-column">
      <section>
        <div class="section-title-row"><div><h2>计划列表</h2><p>展开查看题目和选择原因</p></div></div>
        ${state.plans.length ? `<div class="plan-list">${state.plans.map(planDetails).join("")}</div>` : emptyState("clipboard-list", "暂无面试计划", "当前没有已生成的计划")}
      </section>
      <section>
        <div class="section-title-row"><div><h2>岗位要求</h2><p>${state.roles.length} 个岗位画像</p></div></div>
        ${state.roles.length ? roleList(state.roles) : emptyState("briefcase-business", "暂无岗位要求", "当前没有岗位画像")}
      </section>
    </div>
  `;
}

function renderInterviews() {
  return `
    <section class="page-header">
      <div><h1>面试会话</h1><p>候选人、轮次与报告状态 · ${state.interviews.length} 场面试</p></div>
      <div class="page-actions">
        <button class="button button-secondary" type="button" data-action="refresh-interviews"><i data-lucide="refresh-cw" aria-hidden="true"></i>刷新</button>
        <button class="button button-primary" type="button" data-action="create-interview" ${state.plans.length ? "" : "disabled"}><i data-lucide="calendar-plus" aria-hidden="true"></i>创建预约</button>
      </div>
    </section>

    ${state.interviews.length ? `<div class="interview-list">${state.interviews.map(interviewRow).join("")}</div>` : emptyState("video", "暂无面试会话", "当前没有已安排的面试")}
  `;
}

function renderLiveInterview() {
  const interview = state.selectedInterview;
  if (!interview) return emptyState("video-off", "面试不可用", "未找到指定面试会话");

  const currentTurn = interview.turns.find((item) => item.id === interview.current_turn_id);
  const completedTurns = interview.turns.filter((item) => item.status === "completed").length;
  const progress = interview.turns.length ? Math.round((completedTurns / interview.turns.length) * 100) : 0;
  const canAnswer = interview.status === "in_progress" && currentTurn;
  const canComplete = interview.status === "in_progress" && !currentTurn && completedTurns > 0;

  return `
    <section class="workspace-header">
      <div class="workspace-title">
        <button class="icon-button" type="button" title="返回会话列表" aria-label="返回会话列表" data-action="go-interviews"><i data-lucide="arrow-left" aria-hidden="true"></i></button>
        <div><h1>${escapeHtml(interview.candidate?.name || "候选人")}</h1><span class="muted">${escapeHtml(interview.candidate?.email || "未填写邮箱")}</span></div>
      </div>
      <div class="workspace-status"><span class="status-badge status-${escapeHtml(interview.status)}">${statusLabel(interview.status)}</span><span id="socket-status">${interview.status === "in_progress" ? "正在连接实时通道" : "会话未连接"}</span>${interview.status === "in_progress" ? `<button class="button button-secondary button-small" type="button" data-action="pause-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="pause" aria-hidden="true"></i>暂停</button>` : ""}${interview.status === "paused" ? `<button class="button button-primary button-small" type="button" data-action="recover-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="rotate-ccw" aria-hidden="true"></i>恢复</button>` : ""}${["scheduled", "waiting", "in_progress", "paused"].includes(interview.status) ? `<button class="button button-secondary button-small" type="button" data-action="cancel-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="x-circle" aria-hidden="true"></i>取消</button>` : ""}</div>
    </section>

    <div class="workspace-grid">
      <section>
        <div class="avatar-stage">
          <img src="/web/assets/digital-interviewer.png" alt="数字人面试官" />
          <div class="avatar-overlay">
            <div><strong>数字人面试官</strong><small>${currentTurn ? "正在进行第 " + currentTurn.order + " 题" : "等待会话指令"}</small></div>
            <span class="live-pulse" aria-hidden="true"></span>
          </div>
        </div>

        ${state.report ? renderReport(state.report) : state.activeEvaluation ? renderEvaluation(state.activeEvaluation, currentTurn) : ""}
      </section>

      <aside class="session-panel">
        <div class="session-progress">
          <div class="meta-row"><span>面试进度</span><span>${completedTurns}/${interview.turns.length}</span></div>
          <div class="progress-track"><div class="progress-value" style="width:${progress}%"></div></div>
        </div>
        <div class="question-panel">
          ${renderQuestionPanel(interview, currentTurn, canAnswer, canComplete)}
        </div>
        <div class="section-block" style="margin: 0 16px 16px; padding-top: 16px;">
          <div class="section-title-row"><div><h2>面试轮次</h2></div></div>
          ${renderTimeline(interview.turns)}
        </div>
      </aside>
    </div>
  `;
}

function renderInvitation() {
  const invitation = state.publicInvitation;
  if (!invitation) {
    return `<div class="candidate-room">${emptyState("link-2-off", "邀请链接不可用", "链接可能已过期、已使用或被撤销，请联系面试官重新发送")}</div>`;
  }
  const consent = invitation.consent || {};
  return `
    <div class="candidate-room">
      <header class="candidate-header">
        <a class="candidate-brand" href="#invite/${escapeHtml(state.invitationToken || "")}" aria-label="Interviewer 面试邀请">
          <span class="brand-mark"><i data-lucide="messages-square" aria-hidden="true"></i></span>
          <span><strong>Interviewer</strong><small>候选人登记</small></span>
        </a>
        <div class="candidate-header-meta"><span class="status-badge status-${escapeHtml(invitation.status)}">${statusLabel(invitation.status)}</span></div>
      </header>
      <main style="width:min(760px, calc(100% - 32px)); margin:48px auto;">
        <section class="panel" style="padding:28px;">
          <div class="section-title-row"><div><span class="question-eyebrow">面试邀请</span><h1 style="margin:8px 0;">${escapeHtml(invitation.position_name)}</h1><p>${formatDate(invitation.scheduled_start_at)} 至 ${formatDate(invitation.scheduled_end_at)}</p></div></div>
          <form id="invitation-intake-form">
            <div class="form-grid">
              ${field("姓名", `<input class="form-input" name="name" autocomplete="name" required />`, true)}
              ${field("邮箱", `<input class="form-input" name="email" type="email" autocomplete="email" required />`)}
              ${field("手机号", `<input class="form-input" name="phone" type="tel" autocomplete="tel" required />`)}
            </div>
            <div class="privacy-note" style="margin-top:20px;"><i data-lucide="shield-check" aria-hidden="true"></i><span>${escapeHtml(consent.privacy_notice || "身份信息仅用于本次面试登记与核验。")}</span></div>
            <label style="display:flex; gap:10px; align-items:flex-start; margin-top:18px;"><input type="checkbox" name="privacy_accepted" required /><span>我已阅读并同意隐私说明（版本 ${escapeHtml(consent.version || "v1")}）</span></label>
            ${consent.recording_required ? `<div class="privacy-note" style="margin-top:14px;"><i data-lucide="mic" aria-hidden="true"></i><span>${escapeHtml(consent.recording_notice || "面试需要录制答题音频。")}</span></div><label style="display:flex; gap:10px; align-items:flex-start; margin-top:14px;"><input type="checkbox" name="recording_accepted" required /><span>我同意录制答题音频并用于服务端转写、评分与授权复核</span></label>` : ""}
            <button class="button button-primary" type="submit" style="margin-top:24px;"><i data-lucide="mic-2" aria-hidden="true"></i>核验身份、检查麦克风并进入面试</button>
          </form>
        </section>
      </main>
    </div>`;
}

function renderCandidateRoom() {
  const interview = state.selectedInterview;
  if (!interview) {
    return `<div class="candidate-room">${emptyState("video-off", "面试链接不可用", "请联系面试官重新发送候选人链接")}</div>`;
  }
  const currentTurn = interview.turns?.find((item) => item.id === interview.current_turn_id);
  const completedTurns = interview.turns?.filter((item) => item.status === "completed").length || 0;
  const mediaReady = Boolean(state.candidateMediaStream);
  const interviewComplete = interview.status === "report_ready" || interview.status === "completed";

  return `
    <div class="candidate-room">
      <header class="candidate-header">
        <a class="candidate-brand" href="#candidate/${escapeHtml(interview.id)}" aria-label="Interviewer 候选人面试">
          <span class="brand-mark"><i data-lucide="messages-square" aria-hidden="true"></i></span>
          <span><strong>Interviewer</strong><small>候选人面试</small></span>
        </a>
        <div class="candidate-header-meta">
          <span class="status-badge status-${escapeHtml(interview.status)}">${statusLabel(interview.status)}</span>
          <span>${escapeHtml(interview.candidate?.name || "候选人")}</span>
        </div>
      </header>

      ${interviewComplete ? renderCandidateCompletion(interview) : `
        <section class="candidate-stage-grid">
          <div class="candidate-avatar-stage" id="candidate-avatar-stage">
            <img src="/web/assets/digital-interviewer.png" alt="数字人面试官" />
            <div class="avatar-speaking-indicator" id="avatar-speaking-indicator">
              <span></span><span></span><span></span><span></span><span></span>
            </div>
            <div class="candidate-stage-caption">
              <div><strong>数字人面试官</strong><small id="avatar-state-label">${currentTurn ? "题目已准备" : "等待面试开始"}</small></div>
              <button class="icon-button icon-button-inverse" type="button" title="重新朗读题目" aria-label="重新朗读题目" data-action="candidate-speak" ${currentTurn ? "" : "disabled"}><i data-lucide="volume-2" aria-hidden="true"></i></button>
            </div>
          </div>

          <aside class="candidate-device-panel">
            <div class="candidate-camera">
              <video id="candidate-camera" autoplay muted playsinline></video>
              ${mediaReady ? "" : `<div class="device-permission"><i data-lucide="shield-check" aria-hidden="true"></i><strong>设备权限</strong><span>摄像头和麦克风仅在授权后启用</span><button class="button button-primary" type="button" data-action="candidate-enable-media"><i data-lucide="camera" aria-hidden="true"></i>检查设备</button></div>`}
              <span class="camera-label"><i data-lucide="user-round" aria-hidden="true"></i>${escapeHtml(interview.candidate?.name || "候选人")}</span>
            </div>
            <div class="device-controls">
              <button class="device-toggle ${state.candidateAudioEnabled ? "is-on" : ""}" type="button" data-action="candidate-toggle-audio" ${mediaReady ? "" : "disabled"}><i data-lucide="${state.candidateAudioEnabled ? "mic" : "mic-off"}" aria-hidden="true"></i><span>麦克风</span></button>
              <button class="device-toggle ${state.candidateVideoEnabled ? "is-on" : ""}" type="button" data-action="candidate-toggle-video" ${mediaReady ? "" : "disabled"}><i data-lucide="${state.candidateVideoEnabled ? "video" : "video-off"}" aria-hidden="true"></i><span>摄像头</span></button>
            </div>
            ${mediaReady ? renderDeviceSelectors() : ""}
            <div class="privacy-note"><i data-lucide="lock-keyhole" aria-hidden="true"></i><span>仅录制答题音频；摄像头画面当前只在本机预览</span></div>
          </aside>
        </section>

        <section class="candidate-question-band">
          <div class="candidate-question-head">
            <div>
              <span class="question-eyebrow">${currentTurn ? `第 ${currentTurn.order} / ${interview.turns.length} 题` : "面试准备"}</span>
              <h1>${escapeHtml(currentTurn?.question_spoken_text || "设备就绪后进入面试")}</h1>
            </div>
            <span class="candidate-progress">${completedTurns}/${interview.turns.length}</span>
          </div>

          ${renderCandidateAnswerControls(currentTurn, mediaReady)}
        </section>
      `}
    </div>
  `;
}

function renderDeviceSelectors() {
  const audioOptions = state.candidateDevices.audio.map((device, index) => `<option value="${escapeHtml(device.deviceId)}">${escapeHtml(device.label || `麦克风 ${index + 1}`)}</option>`).join("");
  const videoOptions = state.candidateDevices.video.map((device, index) => `<option value="${escapeHtml(device.deviceId)}">${escapeHtml(device.label || `摄像头 ${index + 1}`)}</option>`).join("");
  return `<div class="device-selectors"><label><span>麦克风设备</span><select class="form-select" id="candidate-audio-device">${audioOptions}</select></label><label><span>摄像头设备</span><select class="form-select" id="candidate-video-device">${videoOptions}</select></label></div>`;
}

function renderCandidateAnswerControls(currentTurn, mediaReady) {
  if (!currentTurn) return `<div class="candidate-waiting-row"><span><strong>正在结束面试</strong><small>请保持页面打开</small></span></div>`;
  const transcript = escapeHtml(state.candidateTranscript);
  const recognitionAvailable = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
  return `
    <div class="candidate-transcript-wrap">
      <div class="transcript-toolbar">
        <span><i data-lucide="captions" aria-hidden="true"></i>实时转写</span>
        <span id="recording-timer">${state.candidateRecording ? "录音中 00:00" : state.candidateAudioUri ? "录音已保存" : recognitionAvailable ? "浏览器可显示辅助转写" : "提交后由服务端转写"}</span>
      </div>
      <textarea class="form-textarea candidate-transcript" id="candidate-transcript" placeholder="浏览器支持时在这里显示辅助转写；最终答案以服务端 STT 为准" readonly>${transcript}</textarea>
      <div class="interim-transcript" id="candidate-interim-transcript">${escapeHtml(state.candidateInterimTranscript)}</div>
    </div>
    <div class="candidate-answer-actions">
      <div class="recording-actions">
        <button class="button button-record" type="button" data-action="candidate-record" ${mediaReady && !state.candidateRecording ? "" : "disabled"}><i data-lucide="circle" aria-hidden="true"></i>开始回答</button>
        <button class="button button-secondary" type="button" data-action="candidate-stop-recording" ${state.candidateRecording ? "" : "disabled"}><i data-lucide="square" aria-hidden="true"></i>结束录音</button>
      </div>
      <button class="button button-primary" type="button" data-action="candidate-submit-answer" ${!state.candidateRecording && !state.candidateRecordingPending && state.candidateAudioUri ? "" : "disabled"}><i data-lucide="send" aria-hidden="true"></i>提交录音</button>
    </div>
    ${state.candidateLastEvaluation ? `<div class="candidate-score-strip"><span><i data-lucide="circle-check" aria-hidden="true"></i>上一题已完成评分</span><strong>${state.candidateLastEvaluation.score} 分</strong></div>` : ""}
  `;
}

function renderCandidateCompletion(interview) {
  return `<section class="candidate-completion"><span class="completion-icon"><i data-lucide="badge-check" aria-hidden="true"></i></span><h1>面试已完成</h1><p>你的回答已经提交，面试报告将由面试官审核。</p><div class="candidate-completion-meta"><span><strong>${interview.turns?.length || 0}</strong><small>问答轮次</small></span><span><strong>${interview.answers?.length || 0}</strong><small>已提交回答</small></span></div></section>`;
}

function renderModels() {
  const readyModels = state.modelConfigurations.filter((item) => item.enabled && item.status === "ready");
  return `
    <section class="page-header">
      <div><h1>模型服务</h1><p>先连接模型厂商，再按模型类型配置具体模型</p></div>
      <div class="page-actions">
        <button class="button button-secondary" type="button" data-action="create-route" ${readyModels.length ? "" : "disabled"}><i data-lucide="route" aria-hidden="true"></i>添加路由</button>
        <button class="button button-secondary" type="button" data-action="create-model" ${state.providerConnections.length ? "" : "disabled"}><i data-lucide="brain-circuit" aria-hidden="true"></i>添加模型</button>
        <button class="button button-primary" type="button" data-action="create-provider"><i data-lucide="plug-zap" aria-hidden="true"></i>添加厂商连接</button>
      </div>
    </section>

    <section>
      <div class="section-title-row"><div><h2>已安装插件</h2><p>${state.catalog.length} 个 Provider · ${state.catalog.filter((item) => item.implemented).length} 个可调用</p></div></div>
      <div class="provider-grid">${state.catalog.map(providerCard).join("")}</div>
    </section>

    <section class="section-block">
      <div class="section-title-row"><div><h2>厂商连接</h2><p>API Key 只写入密钥库，页面仅显示凭证状态</p></div></div>
      ${state.providerConnections.length ? providerConnectionTable() : emptyState("plug", "暂无厂商连接", "先连接一个已安装的 Provider 插件")}
    </section>

    <section class="section-block">
      <div class="section-title-row"><div><h2>模型配置</h2><p>LLM、Embedding、TTS 等模型分别配置</p></div></div>
      ${state.modelConfigurations.length ? modelConfigurationTable() : emptyState("brain-circuit", "暂无模型配置", "在厂商连接下添加一个具体模型")}
    </section>

    <section class="section-block">
      <div class="section-title-row"><div><h2>能力路由</h2><p>${state.routes.length} 条路由规则</p></div></div>
      ${state.routes.length ? routeTable() : emptyState("route", "暂无能力路由", "业务调用将使用默认 Mock Provider")}
    </section>
  `;
}

function metricCard(label, value, note, icon) {
  return `<article class="metric-card"><div class="metric-top"><span>${label}</span><span class="metric-icon"><i data-lucide="${icon}" aria-hidden="true"></i></span></div><strong class="metric-value">${value}</strong><span class="metric-note">${note}</span></article>`;
}

function workflowStep(index, label, value) {
  return `<div class="workflow-step"><span class="step-index">${index}</span><small>${label}</small><strong>${value}</strong></div>`;
}

function questionTable(items, isSearch) {
  return `
    <div class="data-table-wrap">
      <table class="data-table">
        <thead><tr><th>题目</th><th>技能</th><th>难度</th><th>${isSearch ? "匹配度" : "索引"}</th><th class="text-right">操作</th></tr></thead>
        <tbody>
          ${items.map((item) => {
            const question = isSearch ? state.questions.find((candidate) => candidate.id === item.question_id) || item : item;
            const id = question.id || item.question_id;
            return `<tr>
              <td><span class="cell-title">${escapeHtml(question.title)}</span><span class="cell-subtitle">${escapeHtml(question.question_text || (item.match_reasons || []).join(" · ") || question.knowledge_base_id || "")}</span></td>
              <td><div class="tag-list">${(question.skills || []).slice(0, 3).map(tag).join("") || `<span class="muted">通用</span>`}</div></td>
              <td>${escapeHtml(difficultyLabels[question.difficulty] || question.difficulty || "-")}</td>
              <td>${isSearch ? `<strong>${Math.round((item.score || 0) * 100)}%</strong>` : `<span class="status-badge">${statusLabel(question.index_status)}</span>`}</td>
              <td><div class="table-actions"><button class="icon-button" type="button" title="查看题目" aria-label="查看题目" data-action="view-question" data-id="${escapeHtml(id)}"><i data-lucide="eye" aria-hidden="true"></i></button></div></td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>`;
}

function recentInterviewList(items) {
  return `<div class="list-stack">${items.map((item) => `<button class="list-row" type="button" data-action="open-interview" data-id="${escapeHtml(item.id)}"><span class="list-primary"><span class="candidate-avatar">${escapeHtml(initial(item.candidate?.name))}</span><span><strong>${escapeHtml(item.candidate?.name || "候选人")}</strong><small>${formatDate(item.created_at)}</small></span></span><span class="status-badge status-${escapeHtml(item.status)}">${statusLabel(item.status)}</span></button>`).join("")}</div>`;
}

function roleList(items) {
  return `<div class="list-stack">${items.map((item) => `<div class="list-row"><span class="list-primary"><span class="list-icon"><i data-lucide="briefcase-business" aria-hidden="true"></i></span><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(difficultyLabels[item.seniority] || item.seniority)} · ${item.interview_duration_minutes} 分钟</small></span></span><span class="tag">${Object.keys(item.parsed_profile?.skill_weights || {}).length} 项技能</span></div>`).join("")}</div>`;
}

function planSlots(plan) {
  return plan.bank_slots || [];
}

function planDetails(plan) {
  const role = state.roles.find((item) => item.id === plan.role_requirement_id);
  const slots = planSlots(plan);
  const assembly = plan.assembly_summary || {};
  const coverage = (assembly.coverage || []).map((item) => `<span class="tag">${escapeHtml(item.dimension)} ${item.selected_count}/${item.target_count}</span>`).join("");
  const warnings = (assembly.warnings || []).map((item) => `<div class="plan-warning"><i data-lucide="triangle-alert" aria-hidden="true"></i><span>${escapeHtml(item)}</span></div>`).join("");
  return `<details class="plan-item">
    <summary>
      <span class="plan-summary-title"><strong>${escapeHtml(role?.title || "面试计划")}</strong><small>${formatDate(plan.created_at)} · ${slots.length} 道岗位题</small></span>
      <span class="muted">${plan.estimated_minutes} 分钟</span>
      <span class="status-badge status-${escapeHtml(plan.status)}">${statusLabel(plan.status)}</span>
      <i class="plan-chevron" data-lucide="chevron-right" aria-hidden="true"></i>
    </summary>
    <div class="plan-questions">
      ${(coverage || warnings) ? `<div class="plan-assembly-summary"><div><strong>装配覆盖</strong><span class="muted">候选 ${assembly.candidate_count || 0} · 已选 ${assembly.selected_question_count || slots.length}/${assembly.requested_question_count || slots.length}</span></div>${coverage ? `<div class="plan-coverage">${coverage}</div>` : ""}${warnings}</div>` : ""}
      ${slots.map((item) => {
        const question = state.questions.find((candidate) => candidate.id === item.display_question_id);
        return `<div class="plan-question"><span class="question-order">${item.order}</span><span><strong>${escapeHtml(question?.title || item.dimension || "题目")}</strong><span class="cell-subtitle">${escapeHtml(item.selection_reason || "已选入计划")}</span></span><span class="muted">${item.expected_minutes} 分钟</span></div>`;
      }).join("")}
      ${plan.status === "draft" ? `<div class="table-actions"><button class="button button-primary button-small" type="button" data-action="approve-plan" data-id="${escapeHtml(plan.id)}"><i data-lucide="badge-check" aria-hidden="true"></i>审批计划</button></div>` : ""}
    </div>
  </details>`;
}

function interviewRow(interview) {
  const plan = state.plans.find((item) => item.id === interview.plan_id);
  const role = state.roles.find((item) => item.id === plan?.role_requirement_id);
  const completed = (interview.turns || []).filter((item) => item.status === "completed").length;
  return `<article class="interview-row">
    <div class="candidate-cell"><span class="candidate-avatar">${escapeHtml(initial(interview.candidate?.name))}</span><span><strong>${escapeHtml(interview.candidate?.name || "候选人")}</strong><small>${escapeHtml(interview.candidate?.email || "未填写邮箱")}</small></span></div>
    <div class="interview-plan-cell"><span class="cell-title">${escapeHtml(role?.title || "面试计划")}</span><span class="cell-subtitle">${completed}/${(interview.turns || []).length} 题</span></div>
    <span class="status-badge status-${escapeHtml(interview.status)}">${statusLabel(interview.status)}</span>
    <span class="muted">${formatDate(interview.created_at)}</span>
    <div class="table-actions">
      <button class="button button-secondary button-small" type="button" data-action="open-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="external-link" aria-hidden="true"></i>${interview.status === "report_ready" ? "报告" : "进入"}</button>
    </div>
  </article>`;
}

function providerCard(provider) {
  const descriptions = {
    mock: "本地确定性模型，用于开发、检索和评分闭环。",
    openai_compatible: "兼容 Chat Completions 与 Embeddings 协议。",
    deepseek: "DeepSeek 官方 Chat API，复用统一 OpenAI-compatible 运行时。",
    zhipuai: "智谱 GLM Chat 与 GLM-TTS，支持 JSON Object 结构化输出和 WAV/PCM 语音。",
    dashscope: "阿里云百炼千问 LLM、Embedding，以及 Qwen3-TTS / CosyVoice。",
  };
  const modelSummary = (provider.models || []).length ? ` · ${(provider.models || []).length} 个模型候选` : "";
  return `<article class="provider-card"><div class="provider-card-head"><div><h3>${escapeHtml(provider.display_name)}</h3><span class="mono">${escapeHtml(provider.provider_id)}</span></div><span class="status-badge ${provider.implemented ? "" : "status-draft"}">${provider.implemented ? "可调用" : "待实现"}</span></div><p>${escapeHtml(descriptions[provider.provider_id] || "Provider 插件清单已安装，真实调用尚未接入。")}<span class="muted">${escapeHtml(modelSummary)}</span></p><div class="provider-capabilities">${(provider.capabilities || []).slice(0, 4).map(tag).join("")}${provider.capabilities.length > 4 ? `<span class="tag">+${provider.capabilities.length - 4}</span>` : ""}</div></article>`;
}

function providerConnectionTable() {
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>连接名称</th><th>Provider</th><th>状态</th><th>凭证</th><th class="text-right">操作</th></tr></thead><tbody>${state.providerConnections.map((connection) => `<tr><td><span class="cell-title">${escapeHtml(connection.display_name)}</span><span class="cell-subtitle">${formatDate(connection.updated_at)}</span></td><td class="mono">${escapeHtml(connection.provider_id)}</td><td><span class="status-badge ${connection.enabled ? "" : "status-draft"}">${connection.enabled ? "已启用" : "已停用"}</span></td><td>${escapeHtml(connection.credential_status || "missing")}</td><td><div class="table-actions"><button class="button button-secondary button-small" type="button" data-action="edit-provider" data-id="${escapeHtml(connection.id)}"><i data-lucide="settings-2" aria-hidden="true"></i>编辑</button><button class="button button-secondary button-small" type="button" data-action="validate-provider" data-id="${escapeHtml(connection.id)}"><i data-lucide="shield-check" aria-hidden="true"></i>校验</button></div></td></tr>`).join("")}</tbody></table></div>`;
}

function modelConfigurationTable() {
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>模型名称</th><th>类型</th><th>厂商模型 ID</th><th>状态</th><th class="text-right">操作</th></tr></thead><tbody>${state.modelConfigurations.map((model) => `<tr><td><span class="cell-title">${escapeHtml(model.display_name)}</span><span class="cell-subtitle">${escapeHtml(model.provider_connection_id)}</span></td><td>${escapeHtml(model.model_type)}</td><td class="mono">${escapeHtml(model.provider_model_id)}</td><td><span class="status-badge status-${escapeHtml(model.status)}">${statusLabel(model.status)}</span></td><td><div class="table-actions"><button class="button button-secondary button-small" type="button" data-action="edit-model" data-id="${escapeHtml(model.id)}"><i data-lucide="settings-2" aria-hidden="true"></i>编辑</button><button class="button button-secondary button-small" type="button" data-action="test-model" data-id="${escapeHtml(model.id)}"><i data-lucide="flask-conical" aria-hidden="true"></i>测试</button></div></td></tr>`).join("")}</tbody></table></div>`;
}

function routeTable() {
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>用途</th><th>能力</th><th>主配置</th><th>模型</th><th class="text-right">操作</th></tr></thead><tbody>${state.routes.map((route) => {
    const model = state.modelConfigurations.find((item) => item.id === route.primary?.model_configuration_id);
    const connection = state.providerConnections.find((item) => item.id === model?.provider_connection_id);
    return `<tr><td><span class="cell-title">${escapeHtml(route.purpose)}</span></td><td class="mono">${escapeHtml(route.capability)}</td><td>${escapeHtml(connection?.display_name || "-")}</td><td class="mono">${escapeHtml(model?.display_name || route.primary?.model_configuration_id || "-")}</td><td><div class="table-actions"><button class="button button-secondary button-small" type="button" data-action="test-route" data-id="${escapeHtml(route.id)}"><i data-lucide="flask-conical" aria-hidden="true"></i>测试</button></div></td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function renderQuestionPanel(interview, currentTurn, canAnswer, canComplete) {
  if (interview.status === "report_ready" && state.report) {
    return `<span class="question-eyebrow">面试报告</span><h2>${recommendationLabels[state.report.recommendation] || "报告已生成"}</h2><div class="score-value">${state.report.overall_score}<small> / 100</small></div><p class="evaluation-copy">${escapeHtml(state.report.strengths?.[0] || state.report.risks?.[0] || "评分数据已汇总。")}</p>`;
  }
  if (interview.status === "paused") {
    return `<span class="question-eyebrow">面试已暂停</span><h2>当前轮次已安全保留，可从同一题恢复。</h2><button class="button button-primary" type="button" data-action="recover-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="rotate-ccw" aria-hidden="true"></i>恢复面试</button>`;
  }
  if (interview.status === "cancelled") {
    return `<span class="question-eyebrow">面试已取消</span><h2>本场会话已结束，不再接受新的回答。</h2>`;
  }
  if (canComplete) {
    return `<span class="question-eyebrow">全部题目已完成</span><h2>本场面试已完成 ${interview.turns.length} 个问答轮次。</h2><button class="button button-primary" type="button" data-action="complete-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="file-check-2" aria-hidden="true"></i>结束并生成报告</button>`;
  }
  if (!currentTurn) {
    return `<span class="question-eyebrow">会话结束</span><h2>当前没有待回答题目。</h2>`;
  }
  return `<span class="question-eyebrow">第 ${currentTurn.order} 题</span><h2>${escapeHtml(currentTurn.question_spoken_text)}</h2><div class="evaluation-copy">候选人回答必须从候选人房间录音，并由服务端 STT 形成权威转写。面试官可查看实时状态或跳过本题，不能代填最终答案。</div><div class="session-action-row"><small>服务端语音闭环</small><div class="table-actions"><button class="button button-secondary" type="button" data-action="skip-interview-turn" data-id="${escapeHtml(interview.id)}" ${canAnswer ? "" : "disabled"}><i data-lucide="skip-forward" aria-hidden="true"></i>跳过本题</button></div></div>`;
}

function renderEvaluation(evaluation) {
  const interview = state.selectedInterview;
  const answer = interview?.answers?.find((item) => item.id === evaluation.answer_id);
  const turn = interview?.turns?.find((item) => item.id === answer?.turn_id);
  const question = turn?.question_snapshot || state.questions.find((item) => item.id === answer?.question_id);
  const keyPoints = new Map((question?.key_points || []).map((item) => [item.id, item.text]));
  return `<section class="evaluation-panel"><div class="section-title-row"><div><h2>本题评分</h2><p>${escapeHtml(evaluation.model_info?.provider_id || "mock")} · 置信度 ${Math.round((evaluation.confidence || 0) * 100)}%</p></div><div class="score-value">${evaluation.score}<small> / 100</small></div></div><p class="evaluation-copy">${escapeHtml(evaluation.feedback || "评分完成")}</p>${keyPointList("已覆盖关键点", evaluation.covered_key_points, keyPoints, false)}${keyPointList("缺失关键点", evaluation.missing_key_points, keyPoints, true)}</section>`;
}

function keyPointList(title, items, keyPoints, missing) {
  if (!items?.length) return "";
  return `<div class="keypoint-group"><strong>${title}</strong><ul class="keypoint-list ${missing ? "missing" : ""}">${items.map((item) => `<li><i data-lucide="${missing ? "circle-x" : "circle-check"}" aria-hidden="true"></i><span>${escapeHtml(keyPoints.get(item.key_point_id) || item.reason || "关键点评估项")}</span></li>`).join("")}</ul></div>`;
}

function renderReport(report) {
  return `<section class="report-panel"><div class="section-title-row"><div><h2>面试报告</h2><p>${formatDate(report.generated_at)} · ${report.question_evaluations?.length || 0} 道题已评分</p></div><div class="score-value">${report.overall_score}<small> / 100</small></div></div><div class="score-line"><span class="status-badge status-${escapeHtml(report.recommendation)}">${recommendationLabels[report.recommendation] || report.recommendation}</span></div>${report.strengths?.length ? `<div class="keypoint-group"><strong>优势</strong><ul class="keypoint-list">${report.strengths.map((item) => `<li><i data-lucide="circle-check" aria-hidden="true"></i><span>${escapeHtml(item)}</span></li>`).join("")}</ul></div>` : ""}${report.risks?.length ? `<div class="keypoint-group"><strong>风险</strong><ul class="keypoint-list missing">${report.risks.map((item) => `<li><i data-lucide="triangle-alert" aria-hidden="true"></i><span>${escapeHtml(item)}</span></li>`).join("")}</ul></div>` : ""}</section>`;
}

function renderTimeline(turns) {
  return `<div class="timeline">${turns.map((turn) => `<div class="timeline-item ${turn.status === "completed" ? "is-complete" : ""}"><div class="timeline-rail"><span class="timeline-dot"><i data-lucide="${turn.status === "completed" ? "check" : turn.status === "asking" ? "message-circle" : "circle"}" aria-hidden="true"></i></span></div><div class="timeline-content"><strong>第 ${turn.order} 题</strong><small>${statusLabel(turn.status)}</small></div></div>`).join("")}</div>`;
}

function emptyState(icon, title, copy) {
  return `<div class="empty-state"><div><i data-lucide="${icon}" aria-hidden="true"></i><strong>${title}</strong><span>${copy}</span></div></div>`;
}

function tag(value) {
  return `<span class="tag">${escapeHtml(value)}</span>`;
}

function bindViewEvents() {
  const searchForm = document.querySelector("#question-search-form");
  if (searchForm) searchForm.addEventListener("submit", searchQuestions);
  const invitationForm = document.querySelector("#invitation-intake-form");
  if (invitationForm) invitationForm.addEventListener("submit", submitInvitation);
  const audioDevice = document.querySelector("#candidate-audio-device");
  const videoDevice = document.querySelector("#candidate-video-device");
  if (audioDevice) audioDevice.addEventListener("change", switchCandidateDevices);
  if (videoDevice) videoDevice.addEventListener("change", switchCandidateDevices);
  attachCandidateStream();
}

async function handleContentAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const { action, id } = button.dataset;
  const actions = {
    "create-question": openQuestionModal,
    "create-position": openPositionModal,
    "create-knowledge-base": () => openKnowledgeBaseModal(id),
    "create-candidate-profile": openCandidateProfileModal,
    "upload-resume": () => openResumeModal(id),
    "create-role": openRoleModal,
    "generate-plan": openPlanModal,
    "approve-plan": () => approvePlan(id, button),
    "create-interview": openAppointmentModal,
    "create-provider": openProviderModal,
    "create-model": () => openModelModal(),
    "create-route": openRouteModal,
    "edit-provider": () => openProviderEditModal(id),
    "edit-model": () => openModelModal(id),
    "go-questions": () => navigate("questions"),
    "go-interviews": () => navigate("interviews"),
    "clear-search": () => { state.questionResults = null; render(); },
    "view-question": () => openQuestionDetails(id),
    "open-interview": () => navigate("interviews", id),
    "pause-interview": () => controlInterview(id, "pause", "interviewer paused", button),
    "recover-interview": () => controlInterview(id, "recover", "interviewer resumed", button),
    "skip-interview-turn": () => controlInterview(id, "skip", "interviewer skipped turn", button),
    "cancel-interview": () => controlInterview(id, "cancel", "interviewer cancelled", button),
    "complete-interview": () => completeInterview(id, button),
    "refresh-interviews": refreshInterviews,
    "validate-provider": () => validateProviderConnection(id, button),
    "test-model": () => testModelConfiguration(id, button),
    "test-route": () => testRoute(id, button),
    "candidate-enable-media": enableCandidateMedia,
    "candidate-toggle-audio": toggleCandidateAudio,
    "candidate-toggle-video": toggleCandidateVideo,
    "candidate-speak": speakCandidateQuestion,
    "candidate-record": startCandidateRecording,
    "candidate-stop-recording": stopCandidateRecording,
    "candidate-submit-answer": submitCandidateAnswer,
  };
  if (actions[action]) await actions[action]();
}

function runQuickAction() {
  const actions = {
    overview: openQuestionModal,
    questions: openQuestionModal,
    workflow: openPositionModal,
    plans: openRoleModal,
    interviews: openAppointmentModal,
    live: () => navigate("interviews"),
    models: openProviderModal,
  };
  actions[state.view]();
}

async function searchQuestions(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  setBusy(submit, true, "检索中");
  try {
    const data = new FormData(form);
    const difficulty = data.get("difficulty");
    const knowledgeBaseId = data.get("knowledge_base_id");
    const knowledgeBase = state.knowledgeBases.find((item) => item.id === knowledgeBaseId);
    if (!knowledgeBase) throw new Error("请选择有效的岗位题库范围");
    const result = await api(`${API}/questions/search`, {
      method: "POST",
      body: {
        job_position_id: knowledgeBase.job_position_id,
        knowledge_base_ids: [knowledgeBase.id],
        query: data.get("query"),
        filters: difficulty ? { difficulty: [difficulty] } : {},
        limit: 50,
        include_answer: true,
      },
    });
    state.questionResults = result.items;
    render();
  } catch (error) {
    toast("检索失败", error.message, "error");
    setBusy(submit, false);
  }
}

async function submitInvitation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  const token = state.invitationToken;
  const notice = state.publicInvitation?.consent || {};
  setBusy(submit, true, "正在核验");
  try {
    await api(`${API}/public/interview-invitations/${encodeURIComponent(token)}/intake`, {
      method: "POST",
      body: {
        name: data.get("name"),
        email: data.get("email"),
        phone: data.get("phone"),
        consent: {
          accepted: data.get("privacy_accepted") === "on",
          version: notice.version || "v1",
          recording_accepted: data.get("recording_accepted") === "on",
        },
      },
    });
    const browserSupported = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
    let microphoneGranted = false;
    if (browserSupported) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        microphoneGranted = stream.getAudioTracks().length > 0;
        stream.getTracks().forEach((track) => track.stop());
      } catch {
        microphoneGranted = false;
      }
    }
    const mimeTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    const audioContentType = mimeTypes.find((value) => window.MediaRecorder?.isTypeSupported?.(value)) || "audio/webm";
    const readiness = await api(`${API}/public/interview-invitations/${encodeURIComponent(token)}/readiness`, {
      method: "POST",
      body: { browser_supported: browserSupported, microphone_granted: microphoneGranted, audio_content_type: audioContentType },
    });
    if (!readiness.can_start) throw new Error("麦克风或面试运行条件尚未就绪，请检查权限和预约时间后重试");
    const result = await api(`${API}/public/interview-invitations/${encodeURIComponent(token)}/start`, { method: "POST" });
    window.location.href = result.candidate_join_url;
  } catch (error) {
    setBusy(submit, false);
    toast("暂时无法进入面试", error.message, "error");
  }
}

async function refreshWorkflow() {
  const [positions, candidates, appointments] = await Promise.all([
    api(`${API}/job-positions`),
    api(`${API}/candidate-profiles`),
    api(`${API}/interview-appointments`),
  ]);
  state.positions = newestFirst(positions.items);
  state.candidates = newestFirst(candidates.items);
  state.appointments = newestFirst(appointments.items);
  const groups = await Promise.all(
    state.positions.map((item) => api(`${API}/job-positions/${encodeURIComponent(item.id)}/knowledge-bases`))
  );
  state.knowledgeBases = newestFirst(groups.flatMap((item) => item.items || []));
}

function openPositionModal() {
  openModal("新建岗位", `<form id="position-create-form"><div class="form-grid">
    ${field("岗位编码", `<input class="form-input" name="code" placeholder="backend_engineer" required />`)}
    ${field("岗位名称", `<input class="form-input" name="name" placeholder="后端工程师" required />`)}
    ${field("岗位说明", `<textarea class="form-textarea" name="description"></textarea>`, true)}
  </div></form>`, { submitLabel: "创建岗位", submitIcon: "briefcase-business", onSubmit: createPosition, formId: "position-create-form" });
}

async function createPosition(form, submit) {
  const data = new FormData(form);
  setBusy(submit, true, "创建中");
  const item = await api(`${API}/job-positions`, { method: "POST", body: { code: data.get("code"), name: data.get("name"), description: data.get("description") } });
  await refreshWorkflow();
  closeModal();
  render();
  toast("岗位已创建", `${item.name} 可以添加多个岗位题库`);
}

function openKnowledgeBaseModal(positionId) {
  const position = state.positions.find((item) => item.id === positionId);
  if (!position) return;
  openModal(`为 ${position.name} 添加题库`, `<form id="knowledge-base-form"><div class="form-grid">
    <input type="hidden" name="position_id" value="${escapeHtml(positionId)}" />
    ${field("题库名称", `<input class="form-input" name="name" required />`, true)}
    ${field("说明", `<textarea class="form-textarea" name="description"></textarea>`, true)}
    ${field("语言", `<select class="form-select" name="language"><option value="zh-CN">中文</option><option value="en-US">English</option></select>`)}
    ${field("音色配置", `<input class="form-input" name="voice_profile_id" value="voice_default_cn" required />`)}
  </div></form>`, { submitLabel: "创建题库", submitIcon: "library-big", onSubmit: createKnowledgeBase, formId: "knowledge-base-form" });
}

async function createKnowledgeBase(form, submit) {
  const data = new FormData(form);
  setBusy(submit, true, "创建中");
  const item = await api(`${API}/job-positions/${encodeURIComponent(data.get("position_id"))}/knowledge-bases`, { method: "POST", body: { name: data.get("name"), description: data.get("description"), language: data.get("language"), voice_profile_id: data.get("voice_profile_id") } });
  await refreshWorkflow();
  closeModal();
  render();
  toast("岗位题库已创建", `${item.name} 等待录入题目和生成语音`);
}

function openCandidateProfileModal() {
  openModal("录入候选人", `<form id="candidate-profile-form"><div class="form-grid">
    ${field("姓名", `<input class="form-input" name="name" required />`)}
    ${field("企业外部编号", `<input class="form-input" name="external_ref" />`)}
    ${field("邮箱", `<input class="form-input" name="email" type="email" required />`)}
    ${field("手机号", `<input class="form-input" name="phone" required />`)}
  </div></form>`, { submitLabel: "保存候选人", submitIcon: "user-plus", onSubmit: createCandidateProfile, formId: "candidate-profile-form" });
}

async function createCandidateProfile(form, submit) {
  const data = new FormData(form);
  setBusy(submit, true, "保存中");
  const item = await api(`${API}/candidate-profiles`, { method: "POST", body: { name: data.get("name"), email: data.get("email"), phone: data.get("phone"), external_ref: data.get("external_ref") || null } });
  await refreshWorkflow();
  closeModal();
  render();
  toast("候选人已录入", `${item.name} 可继续上传简历`);
}

function openResumeModal(candidateId) {
  const candidate = state.candidates.find((item) => item.id === candidateId);
  if (!candidate) return;
  const roles = state.roles.filter((item) => item.job_position_id);
  openModal(`上传 ${candidate.name} 的简历`, `<form id="resume-upload-form"><div class="form-grid">
    <input type="hidden" name="candidate_id" value="${escapeHtml(candidateId)}" />
    ${field("本地 PDF", `<input class="form-input" name="file" type="file" accept="application/pdf,.pdf" />`, true, "选择本地 PDF，或在下方填写一个公开 HTTPS PDF 地址")}
    ${field("PDF URL", `<input class="form-input" name="source_url" type="url" placeholder="https://example.com/resume.pdf" />`, true, "系统会在隔离区下载、校验、扫描并解析；不接受内网地址或带凭据的 URL")}
    ${field("面向岗位要求（可选，保存后立即 AI 审阅）", `<select class="form-select" name="role_requirement_id"><option value="">仅保存到简历库</option>${roles.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("")}</select>`, true)}
    <div class="field field-full"><span id="resume-ingestion-status" class="field-hint" role="status">文件仅保存在私有存储中，下载使用短期签名地址并记录访问审计。</span></div>
  </div></form>`, { submitLabel: "保存并处理", submitIcon: "file-up", onSubmit: uploadResume, formId: "resume-upload-form" });
}

async function uploadResume(form, submit) {
  const data = new FormData(form);
  const candidateId = data.get("candidate_id");
  const file = data.get("file");
  const sourceUrl = String(data.get("source_url") || "").trim();
  if ((!file || !file.size) && !sourceUrl) throw new Error("请选择本地 PDF，或填写 PDF URL");
  if (file && file.size && sourceUrl) throw new Error("本地 PDF 和 PDF URL 只能选择一种");
  setBusy(submit, true, "正在安全摄取");
  const idempotencyKey = globalThis.crypto?.randomUUID?.() || `resume-${Date.now()}`;
  let queued;
  if (file && file.size) {
    const upload = new FormData();
    upload.set("file", file, file.name);
    upload.set("display_name", file.name);
    queued = await api(`${API}/candidate-profiles/${encodeURIComponent(candidateId)}/resumes`, {
      method: "POST",
      body: upload,
      headers: { "Idempotency-Key": idempotencyKey },
    });
  } else {
    queued = await api(`${API}/candidate-profiles/${encodeURIComponent(candidateId)}/resumes/import-url`, {
      method: "POST",
      body: { url: sourceUrl, display_name: sourceUrl.split("/").pop() || "resume.pdf" },
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }
  const job = await waitForResumeIngestion(queued.ingestion_job_id);
  const resume = job.resume_document;
  if (resume.status !== "ready") {
    closeModal();
    toast("简历已进入处理队列", "后台 worker 完成扫描和解析后即可发起岗位审阅");
    return;
  }
  const roleId = data.get("role_requirement_id");
  let message = "简历已保存到企业简历库";
  if (roleId) {
    const role = state.roles.find((item) => item.id === roleId);
    await api(`${API}/candidate-profiles/${encodeURIComponent(candidateId)}/resume-reviews`, { method: "POST", body: { resume_document_id: resume.id, job_position_id: role.job_position_id, role_requirement_id: role.id } });
    message = "AI 审阅已完成，经历问题等待人工批准";
  }
  closeModal();
  render();
  toast("简历处理完成", message);
}

async function waitForResumeIngestion(jobId) {
  const status = document.querySelector("#resume-ingestion-status");
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const job = await api(`${API}/file-ingestion-jobs/${encodeURIComponent(jobId)}`);
    const resumeStatus = job.resume_document?.status || job.status;
    if (status) status.textContent = `摄取状态：${statusLabel(resumeStatus)}（${attempt + 1}/30）`;
    if (resumeStatus === "ready") return job;
    if (resumeStatus === "failed" || job.status === "dead_letter") {
      throw new Error(job.resume_document?.processing_error?.message || job.last_error || "简历摄取失败");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  return api(`${API}/file-ingestion-jobs/${encodeURIComponent(jobId)}`);
}

function openQuestionModal() {
  if (!state.knowledgeBases.length) {
    toast("暂时无法创建", "请先创建岗位题库", "error");
    return;
  }
  const knowledgeBaseControl = `<select class="form-select" name="knowledge_base_id" required>${state.knowledgeBases.map((item) => { const position = state.positions.find((value) => value.id === item.job_position_id); return `<option value="${escapeHtml(item.id)}">${escapeHtml(position?.name || "岗位")} · ${escapeHtml(item.name)}</option>`; }).join("")}</select>`;
  openModal("新建题目", `
    <form id="question-create-form">
      <div class="form-grid">
        ${field("题目标题", `<input class="form-input" name="title" required maxlength="120" />`, true)}
        ${field("岗位题库", knowledgeBaseControl)}
        ${field("实际题干", `<textarea class="form-textarea" name="question_text" required></textarea>`, true)}
        ${field("标准答案", `<textarea class="form-textarea" name="standard_answer" required></textarea>`, true)}
        ${field("关键点", `<textarea class="form-textarea" name="key_points" required placeholder="每行一个关键点"></textarea>`, true, "至少填写一个关键点")}
        ${field("技能标签", `<input class="form-input" name="skills" placeholder="python, concurrency" />`, true)}
        ${field("难度", `<select class="form-select" name="difficulty"><option value="junior">初级</option><option value="mid" selected>中级</option><option value="senior">高级</option><option value="expert">专家</option></select>`)}
        ${field("题型", `<select class="form-select" name="type"><option value="open_ended">开放问答</option><option value="coding_discussion">代码讨论</option><option value="scenario">场景题</option><option value="behavioral">行为题</option></select>`)}
      </div>
    </form>`, {
      submitLabel: "保存并索引",
      submitIcon: "save",
      onSubmit: createQuestion,
      formId: "question-create-form",
    });
}

async function createQuestion(form, submit) {
  const data = new FormData(form);
  const keyPoints = splitLines(data.get("key_points")).map((text) => ({ text, weight: 1 }));
  if (!keyPoints.length) throw new Error("请至少填写一个关键点");
  setBusy(submit, true, "正在索引");
  const knowledgeBaseId = data.get("knowledge_base_id");
  const item = await api(`${API}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/questions`, {
    method: "POST",
    body: {
      title: data.get("title"),
      knowledge_base_id: knowledgeBaseId,
      question_text: data.get("question_text"),
      standard_answer: data.get("standard_answer"),
      key_points: keyPoints,
      skills: splitComma(data.get("skills")),
      difficulty: data.get("difficulty"),
      type: data.get("type"),
      rubric: { semantic_weight: 0.45, key_point_weight: 0.35, communication_weight: 0.2 },
    },
  });
  await refreshCollection("questions");
  await refreshWorkflow();
  state.questionResults = null;
  closeModal();
  render();
  toast("题目已创建", `${item.title} 已校验并生成读题语音`);
}

function openQuestionDetails(id) {
  const question = state.questions.find((item) => item.id === id);
  if (!question) return;
  openModal("题目详情", `
    <div class="field"><label>题目</label><div class="evaluation-copy">${escapeHtml(question.question_text)}</div></div>
    <div class="field" style="margin-top:16px"><label>标准答案</label><div class="evaluation-copy">${escapeHtml(question.standard_answer)}</div></div>
    <div class="keypoint-group"><strong>关键点</strong><ul class="keypoint-list">${question.key_points.map((item) => `<li><i data-lucide="circle-check" aria-hidden="true"></i><span>${escapeHtml(item.text)}</span></li>`).join("")}</ul></div>
    <div class="tag-list" style="margin-top:16px">${question.skills.map(tag).join("")}</div>
  `, { submitLabel: null });
}

function openRoleModal() {
  if (!state.positions.length) {
    toast("暂时无法创建", "请先在招聘流程中创建岗位", "error");
    return;
  }
  openModal("新建岗位要求", `
    <form id="role-create-form"><div class="form-grid">
      ${field("归属岗位", `<select class="form-select" name="job_position_id" required>${state.positions.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")}</select>`, true)}
      ${field("岗位名称", `<input class="form-input" name="title" required />`, true)}
      ${field("岗位级别", `<select class="form-select" name="seniority"><option value="junior">初级</option><option value="mid">中级</option><option value="senior" selected>高级</option><option value="expert">专家</option></select>`)}
      ${field("岗位描述", `<textarea class="form-textarea" name="description" required></textarea>`, true)}
      ${field("必备技能", `<input class="form-input" name="must_have_skills" placeholder="python, redis, mysql" required />`, true)}
      ${field("加分技能", `<input class="form-input" name="nice_to_have_skills" placeholder="kubernetes, llm" />`)}
      ${field("面试时长（分钟）", `<input class="form-input" name="interview_duration_minutes" type="number" min="15" max="180" value="45" required />`)}
    </div></form>`, {
      submitLabel: "创建岗位",
      submitIcon: "briefcase-business",
      onSubmit: createRole,
      formId: "role-create-form",
    });
}

async function createRole(form, submit) {
  const data = new FormData(form);
  setBusy(submit, true, "创建中");
  const positionId = data.get("job_position_id");
  const item = await api(`${API}/job-positions/${encodeURIComponent(positionId)}/role-requirements`, {
    method: "POST",
    body: {
      title: data.get("title"),
      description: data.get("description"),
      must_have_skills: splitComma(data.get("must_have_skills")),
      nice_to_have_skills: splitComma(data.get("nice_to_have_skills")),
      seniority: data.get("seniority"),
      interview_duration_minutes: Number(data.get("interview_duration_minutes")),
    },
  });
  await refreshCollection("roles");
  closeModal();
  render();
  toast("岗位已创建", `${item.title} 的岗位画像已生成`);
}

function openPlanModal() {
  const scopedRoles = state.roles.filter((item) => item.job_position_id);
  if (!scopedRoles.length || !state.candidates.length || !state.knowledgeBases.length) {
    toast("暂时无法生成", "请先创建岗位要求、候选人和岗位题库", "error");
    return;
  }
  openModal("生成面试计划", `
    <form id="plan-create-form"><div class="form-grid">
      ${field("岗位要求", `<select class="form-select" name="role_requirement_id">${scopedRoles.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("")}</select>`, true)}
      ${field("候选人", `<select class="form-select" name="candidate_profile_id">${state.candidates.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")}</select>`, true)}
      ${field("题目数量", `<input class="form-input" name="question_count" type="number" min="1" max="20" value="${Math.min(8, Math.max(1, state.questions.length))}" required />`)}
      ${field("岗位题库", `<select class="form-select" name="knowledge_base_id">${state.knowledgeBases.map((item) => { const position = state.positions.find((value) => value.id === item.job_position_id); return `<option value="${escapeHtml(item.id)}">${escapeHtml(position?.name || "岗位")} · ${escapeHtml(item.name)}</option>`; }).join("")}</select>`, true)}
      ${field("优先覆盖维度", `<input class="form-input" name="coverage" placeholder="python, database, system_design" />`)}
      ${field("单技能最多题数", `<input class="form-input" name="max_same_skill_questions" type="number" min="1" max="20" value="3" required />`)}
    </div></form>`, {
      submitLabel: "生成计划",
      submitIcon: "wand-sparkles",
      onSubmit: generatePlan,
      formId: "plan-create-form",
      small: true,
    });
}

async function generatePlan(form, submit) {
  const data = new FormData(form);
  const role = state.roles.find((item) => item.id === data.get("role_requirement_id"));
  const knowledgeBase = state.knowledgeBases.find((item) => item.id === data.get("knowledge_base_id"));
  if (!role || !knowledgeBase || role.job_position_id !== knowledgeBase.job_position_id) {
    throw new Error("岗位要求与岗位题库必须属于同一岗位");
  }
  setBusy(submit, true, "生成中");
  const plan = await api(`${API}/interview-plans/generate`, {
    method: "POST",
    body: {
      role_requirement_id: data.get("role_requirement_id"),
      job_position_id: role.job_position_id,
      candidate_profile_id: data.get("candidate_profile_id"),
      knowledge_base_ids: [knowledgeBase.id],
      question_count: Number(data.get("question_count")),
      strategy: {
        coverage: splitComma(data.get("coverage")),
        allow_followups: true,
        max_same_skill_questions: Number(data.get("max_same_skill_questions")),
        difficulty_curve: true,
      },
    },
  });
  await refreshCollection("plans");
  closeModal();
  render();
  toast("计划草稿已生成", `已形成 ${planSlots(plan).length} 个抽题槽位，请审批后创建预约`);
}

async function approvePlan(id, button) {
  const plan = state.plans.find((item) => item.id === id);
  if (!plan) return;
  setBusy(button, true, "审批中");
  try {
    await api(`${API}/interview-plans/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: { expected_version: plan.version, status: "approved" },
    });
    await refreshCollection("plans");
    render();
    toast("计划已审批", "现在可以基于该快照创建正式面试");
  } catch (error) {
    setBusy(button, false);
    toast("审批失败", error.message, "error");
  }
}

function openAppointmentModal() {
  const approvedPlans = state.plans.filter(
    (plan) => plan.status === "approved" && plan.job_position_id && plan.candidate_profile_id
  );
  if (!approvedPlans.length) {
    toast("暂时无法创建", "请先审批候选人和岗位范围明确的面试计划", "error");
    return;
  }
  openModal("创建面试预约", `
    <form id="interview-create-form"><div class="form-grid">
      ${field("已审批候选人计划", `<select class="form-select" name="plan_id">${approvedPlans.map((plan) => { const role = state.roles.find((item) => item.id === plan.role_requirement_id); const candidate = state.candidates.find((item) => item.id === plan.candidate_profile_id); const position = state.positions.find((item) => item.id === plan.job_position_id); return `<option value="${escapeHtml(plan.id)}">${escapeHtml(candidate?.name || "候选人")} · ${escapeHtml(position?.name || role?.title || "岗位")} · ${planSlots(plan).length} 个槽位</option>`; }).join("")}</select>`, true)}
      ${field("开始时间", `<input class="form-input" name="scheduled_start_at" type="datetime-local" required />`)}
      ${field("结束时间", `<input class="form-input" name="scheduled_end_at" type="datetime-local" required />`)}
    </div></form>`, {
      submitLabel: "创建预约并生成邀请",
      submitIcon: "calendar-plus",
      onSubmit: createAppointment,
      formId: "interview-create-form",
    });
}

async function createAppointment(form, submit) {
  const data = new FormData(form);
  const plan = state.plans.find((item) => item.id === data.get("plan_id"));
  if (!plan) throw new Error("请选择有效的已审批计划");
  const scheduledStartAt = new Date(data.get("scheduled_start_at"));
  const scheduledEndAt = new Date(data.get("scheduled_end_at"));
  if (Number.isNaN(scheduledStartAt.getTime()) || Number.isNaN(scheduledEndAt.getTime()) || scheduledStartAt >= scheduledEndAt) {
    throw new Error("结束时间必须晚于开始时间");
  }
  setBusy(submit, true, "创建中");
  const appointment = await api(`${API}/interview-appointments`, {
    method: "POST",
    body: {
      plan_id: plan.id,
      candidate_profile_id: plan.candidate_profile_id,
      job_position_id: plan.job_position_id,
      scheduled_start_at: scheduledStartAt.toISOString(),
      scheduled_end_at: scheduledEndAt.toISOString(),
      settings: { record_audio: true, record_video: false, avatar_id: "avatar_default_cn" },
    },
  });
  const invited = await api(`${API}/interview-appointments/${encodeURIComponent(appointment.id)}/invite`, {
    method: "POST",
    body: { expires_at: scheduledEndAt.toISOString() },
  });
  await refreshCollection("appointments");
  closeModal();
  const invitationUrl = `${window.location.origin}${invited.join_url}`;
  openModal("预约与邀请已创建", `<div class="field"><label>候选人邀请链接</label><textarea class="form-textarea" readonly>${escapeHtml(invitationUrl)}</textarea><span class="field-hint">请通过企业认可的安全渠道发送给计划绑定的候选人。链接包含一次性凭据。</span></div><a class="button button-primary" href="${escapeHtml(invited.join_url)}" target="_blank" rel="noopener noreferrer" style="margin-top:16px;"><i data-lucide="external-link" aria-hidden="true"></i>预览邀请页</a>`, { submitLabel: null });
  toast("预约已创建", "一次性候选人邀请链接已生成");
}

async function controlInterview(id, action, reason, button) {
  const busyLabels = { pause: "暂停中", recover: "恢复中", skip: "跳过中", cancel: "取消中" };
  setBusy(button, true, busyLabels[action] || "处理中");
  try {
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      body: { reason },
    });
    state.report = state.selectedInterview.status === "report_ready"
      ? await api(`${API}/interviews/${encodeURIComponent(id)}/report`)
      : null;
    await refreshCollection("interviews");
    if (state.selectedInterview.status === "in_progress") connectSocket(id);
    else disconnectSocket();
    render();
    toast("会话状态已更新", statusLabel(state.selectedInterview.status));
  } catch (error) {
    setBusy(button, false);
    toast("状态更新失败", error.message, "error");
  }
}

async function completeInterview(id, button) {
  setBusy(button, true, "生成中");
  try {
    const result = await api(`${API}/interviews/${encodeURIComponent(id)}/complete`, { method: "POST" });
    state.selectedInterview = result.interview;
    state.report = result.report;
    state.activeEvaluation = null;
    await refreshCollection("interviews");
    disconnectSocket();
    render();
    toast("报告已生成", `综合得分 ${result.report.overall_score}`);
  } catch (error) {
    setBusy(button, false);
    toast("报告生成失败", error.message, "error");
  }
}

async function enableCandidateMedia() {
  if (!navigator.mediaDevices?.getUserMedia) {
    toast("设备不可用", "当前浏览器不支持摄像头和麦克风访问", "error");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
    });
    stopCandidateMedia();
    state.candidateMediaStream = stream;
    state.candidateAudioEnabled = true;
    state.candidateVideoEnabled = true;
    const devices = await navigator.mediaDevices.enumerateDevices();
    state.candidateDevices = {
      audio: devices.filter((device) => device.kind === "audioinput"),
      video: devices.filter((device) => device.kind === "videoinput"),
    };
    render();
    toast("设备已就绪", "麦克风和摄像头检查通过");
    try {
      await ensureCandidateSocket();
    } catch (error) {
      toast("实时连接暂不可用", `${error.message}，开始回答时会自动重试`, "error");
    }
  } catch (error) {
    const message = error.name === "NotAllowedError" ? "请在浏览器权限设置中允许摄像头和麦克风" : error.message;
    toast("无法启用设备", message, "error");
  }
}

async function switchCandidateDevices() {
  const audioDeviceId = document.querySelector("#candidate-audio-device")?.value;
  const videoDeviceId = document.querySelector("#candidate-video-device")?.value;
  if (!audioDeviceId && !videoDeviceId) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: audioDeviceId ? { deviceId: { exact: audioDeviceId }, echoCancellation: true, noiseSuppression: true } : true,
      video: videoDeviceId ? { deviceId: { exact: videoDeviceId }, width: { ideal: 1280 }, height: { ideal: 720 } } : true,
    });
    state.candidateMediaStream?.getTracks().forEach((track) => track.stop());
    state.candidateMediaStream = stream;
    attachCandidateStream();
    toast("设备已切换", "音视频输入源已更新");
  } catch (error) {
    toast("设备切换失败", error.message, "error");
  }
}

function attachCandidateStream() {
  const video = document.querySelector("#candidate-camera");
  if (video && state.candidateMediaStream && video.srcObject !== state.candidateMediaStream) {
    video.srcObject = state.candidateMediaStream;
  }
}

function toggleCandidateAudio() {
  if (!state.candidateMediaStream) return;
  state.candidateAudioEnabled = !state.candidateAudioEnabled;
  state.candidateMediaStream.getAudioTracks().forEach((track) => { track.enabled = state.candidateAudioEnabled; });
  render();
}

function toggleCandidateVideo() {
  if (!state.candidateMediaStream) return;
  state.candidateVideoEnabled = !state.candidateVideoEnabled;
  state.candidateMediaStream.getVideoTracks().forEach((track) => { track.enabled = state.candidateVideoEnabled; });
  render();
}

function stopCandidateMedia() {
  if (state.candidateTimer) window.clearInterval(state.candidateTimer);
  state.candidateTimer = null;
  if (state.candidateRecognition) {
    try { state.candidateRecognition.stop(); } catch { /* Recognition may already be stopped. */ }
  }
  state.candidateRecognition = null;
  if (state.candidateRecorder && state.candidateRecorder.state !== "inactive") {
    try { state.candidateRecorder.stop(); } catch { /* Recorder may already be stopped. */ }
  }
  state.candidateRecorder = null;
  state.candidateMediaStream?.getTracks().forEach((track) => track.stop());
  state.candidateMediaStream = null;
  state.candidateRecording = false;
  state.candidateRecordingPending = false;
  if (window.speechSynthesis) window.speechSynthesis.cancel();
}

async function speakCandidateQuestion() {
  const interview = state.selectedInterview;
  if (!interview?.current_turn_id) return;
  try {
    const response = await candidateApi("/avatar/speak", {
      method: "POST",
      body: { turn_id: interview.current_turn_id, language: "zh-CN", voice: "default" },
    });
    state.avatarSpeech = response;
    await playAvatarSpeech(response);
  } catch (error) {
    toast("数字人读题失败", error.message, "error");
  }
}

async function playAvatarSpeech(response) {
  const stage = document.querySelector("#candidate-avatar-stage");
  const label = document.querySelector("#avatar-state-label");
  const startSpeaking = () => {
    stage?.classList.add("is-speaking");
    if (label) label.textContent = `正在朗读 · ${response.provider?.provider_id || "avatar"}`;
  };
  const stopSpeaking = () => {
    stage?.classList.remove("is-speaking");
    if (label) label.textContent = "请开始回答";
  };

  if (response.mode === "audio" && response.audio_uri) {
    const audio = new Audio(response.audio_uri);
    audio.addEventListener("play", startSpeaking);
    audio.addEventListener("ended", stopSpeaking);
    audio.addEventListener("error", stopSpeaking);
    await audio.play();
    return;
  }

  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
    stopSpeaking();
    toast("浏览器无法朗读", "题目已显示，请直接阅读题干", "error");
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(response.text);
  utterance.lang = "zh-CN";
  utterance.rate = 0.95;
  utterance.pitch = 1;
  const voices = window.speechSynthesis.getVoices();
  const chineseVoice = voices.find((voice) => voice.lang?.toLowerCase().startsWith("zh"));
  if (chineseVoice) utterance.voice = chineseVoice;
  utterance.addEventListener("start", startSpeaking);
  utterance.addEventListener("end", stopSpeaking);
  utterance.addEventListener("error", stopSpeaking);
  window.speechSynthesis.speak(utterance);
}

function ensureCandidateSocket() {
  if (state.socket?.readyState === WebSocket.OPEN) return Promise.resolve(state.socket);
  if (!state.candidateToken) return Promise.reject(new Error("候选人会话 token 缺失"));
  disconnectSocket();
  return new Promise((resolve, reject) => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}/live?role=candidate&token=${encodeURIComponent(state.candidateToken)}`;
    const socket = new WebSocket(url);
    let opened = false;
    state.socket = socket;
    socket.addEventListener("open", () => {
      opened = true;
      socket.send(JSON.stringify({ type: "session.ready", payload: { source: "candidate_room" } }));
      resolve(socket);
    }, { once: true });
    socket.addEventListener("message", (event) => handleCandidateSocketEvent(event));
    socket.addEventListener("close", (event) => {
      if (event.code === 4403) toast("候选人链接已失效", "请联系面试官获取新链接", "error");
      if (!opened) reject(new Error("实时会话连接已关闭"));
    });
    socket.addEventListener("error", () => reject(new Error("实时会话连接失败")), { once: true });
  });
}

async function handleCandidateSocketEvent(messageEvent) {
  let event;
  try { event = JSON.parse(messageEvent.data); } catch { return; }
  const payload = event.payload || {};
  if (event.type === "media.recording.stopped") {
    state.candidateAudioUri = payload.audio_uri;
    state.candidateAudioMimeType = payload.mime_type || "audio/webm;codecs=opus";
    state.candidateRecordingPending = false;
    render();
    return;
  }
  if (event.type === "stt.transcript.partial") {
    state.candidateInterimTranscript = payload.text || "";
    const interim = document.querySelector("#candidate-interim-transcript");
    if (interim) interim.textContent = state.candidateInterimTranscript;
    return;
  }
  if (event.type === "evaluation.started") {
    const timer = document.querySelector("#recording-timer");
    if (timer) timer.textContent = "正在评分";
    return;
  }
  if (event.type === "evaluation.completed") {
    state.candidateLastEvaluation = payload;
    state.candidateTranscript = "";
    state.candidateInterimTranscript = "";
    state.candidateAudioUri = null;
    state.selectedInterview = await candidateApi();
    render();
    if (state.selectedInterview.current_turn_id) window.setTimeout(speakCandidateQuestion, 300);
    return;
  }
  if (event.type === "interview.completed") {
    state.selectedInterview = await candidateApi();
    render();
    return;
  }
  if (event.type === "error") {
    state.candidateRecordingPending = false;
    toast("实时会话错误", payload.message || payload.code || "未知错误", "error");
  }
}

async function startCandidateRecording() {
  if (!state.candidateMediaStream || !state.candidateAudioEnabled) {
    toast("麦克风不可用", "请启用麦克风后再开始回答", "error");
    return;
  }
  if (!window.MediaRecorder) {
    toast("录音不可用", "当前浏览器不支持 MediaRecorder，无法形成服务端权威转写", "error");
    return;
  }
  try {
    const socket = await ensureCandidateSocket();
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    document.querySelector("#candidate-avatar-stage")?.classList.remove("is-speaking");
    const audioStream = new MediaStream(state.candidateMediaStream.getAudioTracks());
    const mimeTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    const mimeType = mimeTypes.find((value) => MediaRecorder.isTypeSupported(value)) || "";
    const recorder = mimeType ? new MediaRecorder(audioStream, { mimeType }) : new MediaRecorder(audioStream);
    state.candidateRecorder = recorder;
    state.candidateAudioUri = null;
    state.candidateTranscript = "";
    state.candidateInterimTranscript = "";
    state.candidateRecording = true;
    state.candidateRecordingPending = false;
    state.candidateRecordingStartedAt = Date.now();
    socket.send(JSON.stringify({
      type: "candidate.media.start",
      turn_id: state.selectedInterview.current_turn_id,
      payload: { mime_type: recorder.mimeType || mimeType || "audio/webm", source: "microphone", timeslice_ms: 400 },
    }));
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size && socket.readyState === WebSocket.OPEN) socket.send(event.data);
    });
    recorder.addEventListener("stop", () => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "candidate.media.stop", turn_id: state.selectedInterview.current_turn_id, payload: {} }));
      }
      state.candidateRecorder = null;
      render();
    }, { once: true });
    recorder.start(400);
    startBrowserRecognition(socket);
    startCandidateTimer();
    render();
  } catch (error) {
    state.candidateRecording = false;
    toast("无法开始录音", error.message, "error");
  }
}

function startBrowserRecognition(socket) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return;
  const recognition = new Recognition();
  recognition.lang = "zh-CN";
  recognition.continuous = true;
  recognition.interimResults = true;
  state.candidateRecognition = recognition;
  recognition.addEventListener("result", (event) => {
    let interim = "";
    let finalText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const text = event.results[index][0].transcript;
      if (event.results[index].isFinal) finalText += text;
      else interim += text;
    }
    if (finalText) state.candidateTranscript = `${state.candidateTranscript} ${finalText}`.trim();
    state.candidateInterimTranscript = interim;
    const textarea = document.querySelector("#candidate-transcript");
    const interimElement = document.querySelector("#candidate-interim-transcript");
    if (textarea) textarea.value = state.candidateTranscript;
    if (interimElement) interimElement.textContent = interim;
    if (interim && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({
        type: "candidate.transcript.partial",
        turn_id: state.selectedInterview.current_turn_id,
        payload: { text: interim, confidence: 0, language: "zh-CN", source: "browser_speech_fallback" },
      }));
    }
  });
  recognition.addEventListener("error", (event) => {
    if (!['no-speech', 'aborted'].includes(event.error)) toast("实时转写不可用", "可以继续录音并手动修正文本", "error");
  });
  try { recognition.start(); } catch { state.candidateRecognition = null; }
}

function startCandidateTimer() {
  if (state.candidateTimer) window.clearInterval(state.candidateTimer);
  state.candidateTimer = window.setInterval(() => {
    state.candidateRecordingDuration = Math.floor((Date.now() - state.candidateRecordingStartedAt) / 1000);
    const timer = document.querySelector("#recording-timer");
    if (timer) timer.textContent = `录音中 ${formatDuration(state.candidateRecordingDuration)}`;
  }, 500);
}

function stopCandidateRecording() {
  if (!state.candidateRecording) return;
  state.candidateRecording = false;
  state.candidateRecordingPending = true;
  if (state.candidateTimer) window.clearInterval(state.candidateTimer);
  state.candidateTimer = null;
  if (state.candidateRecognition) {
    try { state.candidateRecognition.stop(); } catch { /* Recognition may already be stopped. */ }
    state.candidateRecognition = null;
  }
  if (state.candidateRecorder?.state !== "inactive") state.candidateRecorder.stop();
  render();
}

async function submitCandidateAnswer() {
  const text = state.candidateTranscript.trim();
  if (!state.candidateAudioUri || state.candidateRecording || state.candidateRecordingPending) return;
  try {
    const submit = document.querySelector('[data-action="candidate-submit-answer"]');
    setBusy(submit, true, "服务端转写中");
    const localDevelopment = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
    const body = {
      turn_id: state.selectedInterview.current_turn_id,
      audio_uri: state.candidateAudioUri,
      content_type: state.candidateAudioMimeType,
      language: "zh-CN",
      duration_seconds: state.candidateRecordingDuration,
      ...(localDevelopment && text ? { development_transcript: text, development_confidence: 0.85 } : {}),
    };
    const result = await candidateApi("/audio-answers", { method: "POST", body });
    state.candidateLastEvaluation = result.evaluation;
    state.candidateTranscript = "";
    state.candidateInterimTranscript = "";
    state.candidateAudioUri = null;
    state.selectedInterview = await candidateApi();
    render();
    toast("评分完成", `本题得分 ${result.evaluation.score}`);
    if (state.selectedInterview.current_turn_id) window.setTimeout(speakCandidateQuestion, 300);
  } catch (error) {
    toast("回答提交失败", error.message, "error");
  }
}

function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const remaining = Math.max(0, seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${remaining}`;
}

async function refreshInterviews() {
  try {
    await refreshCollection("interviews");
    render();
    toast("会话已刷新", `共 ${state.interviews.length} 场面试`);
  } catch (error) {
    toast("刷新失败", error.message, "error");
  }
}

function schemaFields(schema) {
  return schema?.fields || [];
}

function schemaForm(schema, values = {}, prefix = "schema") {
  return schemaFields(schema).map((item) => {
    const name = `${prefix}__${item.name}`;
    const raw = values[item.name] ?? item.default ?? "";
    const required = item.required && !(item.control === "secret" && values.__configured) ? "required" : "";
    let control;
    if (item.control === "select") {
      control = `<select class="form-select" name="${escapeHtml(name)}" ${required}>${(item.options || []).map((option) => `<option value="${escapeHtml(option.value)}" ${String(raw) === String(option.value) ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select>`;
    } else if (item.control === "switch") {
      control = `<select class="form-select" name="${escapeHtml(name)}"><option value="false" ${raw ? "" : "selected"}>否</option><option value="true" ${raw ? "selected" : ""}>是</option></select>`;
    } else if (item.control === "textarea" || item.control === "key_value") {
      const value = item.control === "key_value" && typeof raw === "object" ? JSON.stringify(raw, null, 2) : raw;
      control = `<textarea class="form-input" name="${escapeHtml(name)}" ${required} placeholder="${item.control === "key_value" ? "JSON 对象" : escapeHtml(item.placeholder || "")}">${escapeHtml(value)}</textarea>`;
    } else {
      const type = item.control === "secret" ? "password" : item.control === "number" ? "number" : "text";
      control = `<input class="form-input" name="${escapeHtml(name)}" type="${type}" value="${item.control === "secret" ? "" : escapeHtml(raw)}" ${required} ${item.min !== undefined ? `min="${item.min}"` : ""} ${item.max !== undefined ? `max="${item.max}"` : ""} placeholder="${escapeHtml(item.control === "secret" && values.__configured ? "留空则保留现有密钥" : item.placeholder || "")}" />`;
    }
    return field(item.label, control, false, item.help || "");
  }).join("");
}

function readSchemaForm(form, schema, prefix, { omitBlankSecrets = false } = {}) {
  const data = new FormData(form);
  const result = {};
  for (const item of schemaFields(schema)) {
    const raw = data.get(`${prefix}__${item.name}`);
    if (item.control === "secret" && omitBlankSecrets && !raw) continue;
    if (raw === "" && !item.required) continue;
    if (item.control === "number") result[item.name] = Number(raw);
    else if (item.control === "switch") result[item.name] = raw === "true";
    else if (item.control === "tags") result[item.name] = String(raw || "").split(",").map((value) => value.trim()).filter(Boolean);
    else if (item.control === "key_value") result[item.name] = raw ? JSON.parse(raw) : {};
    else result[item.name] = raw;
  }
  return result;
}

function openProviderModal() {
  const implemented = state.catalog.filter((item) => item.implemented);
  if (!implemented.length) return;
  const renderForm = (provider) => `
    <form id="provider-create-form"><div class="form-grid">
      ${field("Provider", `<select class="form-select" name="provider_id">${implemented.map((item) => `<option value="${escapeHtml(item.provider_id)}" ${item.provider_id === provider.provider_id ? "selected" : ""}>${escapeHtml(item.display_name)}</option>`).join("")}</select>`)}
      ${field("连接名称", `<input class="form-input" name="display_name" value="${escapeHtml(provider.display_name)}" required />`)}
      ${schemaForm(provider.connection_form, {}, "connection")}
      ${schemaForm(provider.credential_form, {}, "credential")}
    </div></form>`;
  const show = (provider) => {
    openModal("添加厂商连接", renderForm(provider), { submitLabel: "保存连接", submitIcon: "save", onSubmit: (form, submit) => createProviderConnection(provider, form, submit), formId: "provider-create-form" });
    modalRoot.querySelector('[name="provider_id"]')?.addEventListener("change", (event) => show(implemented.find((item) => item.provider_id === event.target.value)));
  };
  show(implemented[0]);
}

async function createProviderConnection(provider, form, submit) {
  const data = new FormData(form);
  setBusy(submit, true, "保存中");
  const item = await api(`${API}/admin/model-provider-connections`, { method: "POST", body: { provider_id: provider.provider_id, display_name: data.get("display_name"), enabled: true, connection_config: readSchemaForm(form, provider.connection_form, "connection"), credentials: readSchemaForm(form, provider.credential_form, "credential") } });
  try {
    const validated = await api(`${API}/admin/model-provider-connections/${encodeURIComponent(item.id)}/validate`, { method: "POST" });
    await refreshCollection("providerConnections"); closeModal(); render();
    toast("厂商连接已验证", validated.last_validation?.message || item.display_name);
  } catch (error) {
    await refreshCollection("providerConnections"); closeModal(); render();
    toast("连接已保存，但 API Key 校验失败", error.message, "error");
  }
}

function openProviderEditModal(id) {
  const connection = state.providerConnections.find((item) => item.id === id);
  const provider = state.catalog.find((item) => item.provider_id === connection?.provider_id);
  if (!connection || !provider) return;
  openModal("编辑厂商连接", `<form id="provider-edit-form"><div class="form-grid">${field("Provider", `<input class="form-input" value="${escapeHtml(provider.display_name)}" disabled />`)}${field("连接名称", `<input class="form-input" name="display_name" value="${escapeHtml(connection.display_name)}" required />`)}${field("状态", `<select class="form-select" name="enabled"><option value="true" ${connection.enabled ? "selected" : ""}>已启用</option><option value="false" ${connection.enabled ? "" : "selected"}>已停用</option></select>`)}${schemaForm(provider.connection_form, connection.connection_config, "connection")}${schemaForm(provider.credential_form, { __configured: true }, "credential")}</div></form>`, { submitLabel: "保存修改", onSubmit: (form, submit) => updateProviderConnection(connection, provider, form, submit), formId: "provider-edit-form" });
}

async function updateProviderConnection(current, provider, form, submit) {
  const data = new FormData(form);
  const credentials = readSchemaForm(form, provider.credential_form, "credential", { omitBlankSecrets: true });
  const body = { expected_version: current.version, display_name: data.get("display_name"), enabled: data.get("enabled") === "true", connection_config: readSchemaForm(form, provider.connection_form, "connection") };
  if (Object.keys(credentials).length) body.credentials = credentials;
  setBusy(submit, true, "保存中");
  const item = await api(`${API}/admin/model-provider-connections/${encodeURIComponent(current.id)}`, { method: "PATCH", body });
  await refreshCollection("providerConnections"); closeModal(); render(); toast("厂商连接已更新", item.display_name);
}

async function validateProviderConnection(id, button) {
  setBusy(button, true, "校验中");
  try { const item = await api(`${API}/admin/model-provider-connections/${encodeURIComponent(id)}/validate`, { method: "POST" }); await refreshCollection("providerConnections"); render(); toast("API Key 有效", item.last_validation?.message || item.credential_status); }
  catch (error) { toast("连接校验失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function openModelModal(id = null) {
  const current = state.modelConfigurations.find((item) => item.id === id);
  const connections = state.providerConnections.filter((item) => item.enabled);
  if (!connections.length) return;
  const initialConnection = current ? connections.find((item) => item.id === current.provider_connection_id) : connections[0];
  const catalog = await api(`${API}/admin/model-provider-connections/${encodeURIComponent(initialConnection.id)}/model-catalog`);
  showModelModal(current, connections, initialConnection, catalog);
}

function showModelModal(current, connections, connection, catalog, desiredModelId = null, desiredModelType = null) {
  const modelType = current?.model_type || desiredModelType || Object.keys(catalog.model_types || {})[0];
  const typeDefinition = catalog.model_types[modelType];
  const models = (catalog.models || []).filter((item) => item.model_type === modelType);
  const selected = models.find((item) => item.model_id === (current?.provider_model_id || desiredModelId)) || models.find((item) => item.default) || models[0];
  const predefined = typeDefinition.selection_mode === "predefined";
  const modelControl = predefined
    ? `<select class="form-select" name="provider_model_id">${models.map((item) => `<option value="${escapeHtml(item.model_id)}" ${item.model_id === selected?.model_id ? "selected" : ""}>${escapeHtml(item.label || item.model_id)}</option>`).join("")}</select>`
    : `<input class="form-input" name="provider_model_id" value="${escapeHtml(current?.provider_model_id || selected?.model_id || "")}" required />`;
  const configurationForm = selected?.configuration_form || typeDefinition.configuration_form || { fields: [] };
  const parameterForm = catalog.parameter_forms?.[modelType] || { fields: [] };
  openModal(current ? "编辑模型配置" : "添加模型配置", `<form id="model-config-form"><div class="form-grid">${field("厂商连接", `<select class="form-select" name="provider_connection_id" ${current ? "disabled" : ""}>${connections.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === connection.id ? "selected" : ""}>${escapeHtml(item.display_name)}</option>`).join("")}</select>`)}${field("模型类型", `<select class="form-select" name="model_type" ${current ? "disabled" : ""}>${Object.entries(catalog.model_types || {}).map(([key, value]) => `<option value="${escapeHtml(key)}" ${key === modelType ? "selected" : ""}>${escapeHtml(value.label || key)}</option>`).join("")}</select>`)}${field("厂商模型 ID", modelControl)}${field("显示名称", `<input class="form-input" name="display_name" value="${escapeHtml(current?.display_name || selected?.label || "")}" required />`)}${field("状态", `<select class="form-select" name="enabled"><option value="true" ${current?.enabled === false ? "" : "selected"}>已启用</option><option value="false" ${current?.enabled === false ? "selected" : ""}>已停用</option></select>`)}${schemaForm(configurationForm, current?.settings || {}, "settings")}${schemaForm(parameterForm, current?.default_parameters || {}, "parameters")}</div></form>`, { submitLabel: current ? "保存修改" : "保存模型", onSubmit: (form, submit) => saveModelConfiguration(current, configurationForm, parameterForm, form, submit), formId: "model-config-form" });
  const form = modalRoot.querySelector("#model-config-form");
  if (!current) {
    form.elements.provider_connection_id.addEventListener("change", async (event) => { const next = connections.find((item) => item.id === event.target.value); showModelModal(null, connections, next, await api(`${API}/admin/model-provider-connections/${encodeURIComponent(next.id)}/model-catalog`)); });
    form.elements.model_type.addEventListener("change", (event) => showModelModal(null, connections, connection, catalog, null, event.target.value));
    if (predefined) form.elements.provider_model_id.addEventListener("change", (event) => showModelModal(null, connections, connection, catalog, event.target.value, modelType));
  }
}

async function saveModelConfiguration(current, configurationForm, parameterForm, form, submit) {
  const data = new FormData(form);
  const body = { display_name: data.get("display_name"), enabled: data.get("enabled") === "true", settings: readSchemaForm(form, configurationForm, "settings"), default_parameters: readSchemaForm(form, parameterForm, "parameters") };
  let item;
  setBusy(submit, true, "保存中");
  if (current) item = await api(`${API}/admin/model-configurations/${encodeURIComponent(current.id)}`, { method: "PATCH", body: { ...body, expected_version: current.version } });
  else item = await api(`${API}/admin/model-configurations`, { method: "POST", body: { ...body, provider_connection_id: data.get("provider_connection_id"), model_type: data.get("model_type"), provider_model_id: data.get("provider_model_id") } });
  await refreshCollection("modelConfigurations"); closeModal(); render(); toast("模型配置已保存", item.display_name);
}

function openRouteModal() {
  const targets = state.modelConfigurations.filter((model) => model.enabled && model.status === "ready").flatMap((model) => (model.supported_capabilities || []).map((capability) => ({ model, capability })));
  if (!targets.length) {
    toast("暂时无法创建", "没有已启用且可调用的 Provider 配置", "error");
    return;
  }
  openModal("添加能力路由", `
    <form id="route-create-form"><div class="form-grid">
      ${field("调用用途", `<input class="form-input" name="purpose" value="answer_evaluation" required />`)}
      ${field("模型与能力", `<select class="form-select" name="target">${targets.map(({ model, capability }) => `<option value="${escapeHtml(`${model.id}::${capability}`)}">${escapeHtml(model.display_name)} · ${escapeHtml(capability)}</option>`).join("")}</select>`, true)}
      ${field("超时（秒）", `<input class="form-input" name="timeout_s" type="number" min="1" max="300" value="30" required />`)}
      ${field("重试次数", `<input class="form-input" name="retry_count" type="number" min="0" max="3" value="1" required />`)}
    </div></form>`, {
      submitLabel: "保存路由",
      submitIcon: "route",
      onSubmit: createRoute,
      formId: "route-create-form",
      small: true,
    });
}

async function createRoute(form, submit) {
  const data = new FormData(form);
  const [modelConfigurationId, capability] = String(data.get("target") || "").split("::", 2);
  if (!modelConfigurationId || !capability) throw new Error("请选择有效的模型与能力");
  setBusy(submit, true, "保存中");
  const route = await api(`${API}/admin/model-routes`, {
    method: "POST",
    body: {
      capability,
      purpose: data.get("purpose"),
      primary: {
        model_configuration_id: modelConfigurationId,
        timeout_s: Number(data.get("timeout_s")),
      },
      fallbacks: [],
      policy: { retry_count: Number(data.get("retry_count")) },
      enabled: true,
    },
  });
  await refreshCollection("routes");
  closeModal();
  render();
  toast("路由已保存", `${route.purpose} · ${route.capability}`);
}

async function testModelConfiguration(id, button) {
  setBusy(button, true, "测试中");
  try {
    const result = await api(`${API}/admin/model-configurations/${encodeURIComponent(id)}/test`, { method: "POST", body: {} });
    await refreshCollection("modelConfigurations"); render(); toast("模型可用", `${result.provider?.model || "model"} · ${result.latency_ms || result.provider?.latency_ms || 0} ms`);
  } catch (error) {
    toast("模型测试失败", error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function testRoute(id, button) {
  setBusy(button, true, "测试中");
  try {
    const result = await api(`${API}/admin/model-routes/${encodeURIComponent(id)}/test`, { method: "POST" });
    toast("路由可用", result.provider?.model || result.model || "调用成功");
  } catch (error) {
    toast("路由测试失败", error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function openModal(title, body, options = {}) {
  const { submitLabel = "保存", submitIcon = "save", onSubmit = null, formId = null, small = false } = options;
  modalRoot.innerHTML = `<div class="modal-backdrop" data-modal-backdrop><section class="modal ${small ? "modal-small" : ""}" role="dialog" aria-modal="true" aria-labelledby="modal-title"><header class="modal-header"><h2 id="modal-title">${escapeHtml(title)}</h2><button class="icon-button" type="button" title="关闭" aria-label="关闭" data-close-modal><i data-lucide="x" aria-hidden="true"></i></button></header><div class="modal-body">${body}</div><footer class="modal-footer"><button class="button button-secondary" type="button" data-close-modal>取消</button>${submitLabel ? `<button class="button button-primary" id="modal-submit" type="submit" ${formId ? `form="${formId}"` : ""}><i data-lucide="${submitIcon}" aria-hidden="true"></i>${escapeHtml(submitLabel)}</button>` : ""}</footer></section></div>`;
  modalRoot.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", closeModal));
  modalRoot.querySelector("[data-modal-backdrop]").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeModal();
  });
  const form = formId ? document.querySelector(`#${formId}`) : null;
  if (form && onSubmit) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await onSubmit(form, document.querySelector("#modal-submit"));
      } catch (error) {
        setBusy(document.querySelector("#modal-submit"), false);
        toast("操作失败", error.message, "error");
      }
    });
  }
  document.body.style.overflow = "hidden";
  iconize();
  requestAnimationFrame(() => modalRoot.querySelector("input, textarea, select, button")?.focus());
}

function closeModal() {
  modalRoot.innerHTML = "";
  document.body.style.overflow = "";
}

function field(label, control, full = false, hint = "") {
  return `<div class="field ${full ? "field-full" : ""}"><label>${label}</label>${control}${hint ? `<span class="field-hint">${hint}</span>` : ""}</div>`;
}

function connectSocket(interviewId) {
  disconnectSocket();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}${API}/interviews/${encodeURIComponent(interviewId)}/live`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    updateSocketStatus("实时通道已连接");
    socket.send(JSON.stringify({ type: "session.ready", payload: { participant: "interviewer", source: "web_console" } }));
  });
  socket.addEventListener("message", (event) => handleInterviewerSocketEvent(event));
  socket.addEventListener("close", () => updateSocketStatus("实时通道已断开"));
  socket.addEventListener("error", () => updateSocketStatus("实时通道异常"));
}

async function handleInterviewerSocketEvent(messageEvent) {
  let event;
  try { event = JSON.parse(messageEvent.data); } catch { return; }
  const payload = event.payload || {};
  if (event.type === "stt.transcript.partial" || event.type === "stt.transcript.final") {
    state.liveTranscript = payload.text || "";
    return;
  }
  if (event.type === "media.recording.started") {
    updateSocketStatus("候选人正在回答");
    return;
  }
  if (event.type === "evaluation.completed") {
    toast("候选人完成一题", `本题得分 ${payload.score}`);
    return;
  }
  if (event.type === "question.selected") {
    state.liveTranscript = "";
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}`);
    render();
    return;
  }
  if (event.type === "interview.completed") {
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}`);
    state.report = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}/report`);
    render();
  }
}

function disconnectSocket() {
  if (state.socket && state.socket.readyState < 2) state.socket.close();
  state.socket = null;
}

function updateSocketStatus(label) {
  const element = document.querySelector("#socket-status");
  if (element) element.textContent = label;
}

async function api(path, options = {}) {
  const fetchOptions = { method: options.method || "GET", headers: { Accept: "application/json", ...(options.headers || {}) } };
  if (options.body !== undefined) {
    if (options.body instanceof FormData) {
      fetchOptions.body = options.body;
    } else {
      fetchOptions.headers["Content-Type"] = "application/json";
      fetchOptions.body = JSON.stringify(options.body);
    }
  }
  let response;
  try {
    response = await fetch(path, fetchOptions);
  } catch (error) {
    throw new Error("无法连接 API 服务");
  }
  const text = await response.text();
  let payload = {};
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { message: text }; }
  }
  if (!response.ok) {
    const detail = payload.error?.message || payload.detail?.[0]?.msg || payload.message || `请求失败 (${response.status})`;
    throw new Error(detail);
  }
  return payload;
}

function setBusy(button, busy, label = "处理中") {
  if (!button) return;
  if (busy) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${escapeHtml(label)}`;
  } else {
    button.disabled = false;
    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    iconize();
  }
}

function toast(title, message, type = "success") {
  const element = document.createElement("div");
  element.className = `toast ${type === "error" ? "is-error" : ""}`;
  element.innerHTML = `<i data-lucide="${type === "error" ? "circle-alert" : "circle-check"}" aria-hidden="true"></i><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(message || "")}</small></div>`;
  toastRegion.appendChild(element);
  iconize();
  window.setTimeout(() => element.remove(), 3800);
}

function renderFatalError(error) {
  appContent.innerHTML = `<div class="empty-state"><div><i data-lucide="server-off" aria-hidden="true"></i><strong>工作区加载失败</strong><span>${escapeHtml(error.message)}</span><button class="button button-primary" type="button" style="margin-top:16px" onclick="window.location.reload()"><i data-lucide="refresh-cw" aria-hidden="true"></i>重新加载</button></div></div>`;
  iconize();
}

function openMobileNav() {
  sidebar.classList.add("is-open");
  mobileBackdrop.classList.add("is-visible");
}

function closeMobileNav() {
  sidebar.classList.remove("is-open");
  mobileBackdrop.classList.remove("is-visible");
}

function iconize() {
  if (window.lucide) window.lucide.createIcons();
}

function formatDate(value) {
  if (!value) return "未安排";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function formatLongDate(date) {
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" }).format(date);
}

function statusLabel(status) {
  return statusLabels[status] || status || "未知";
}

function initial(value) {
  return String(value || "候").trim().slice(0, 1);
}

function splitComma(value) {
  return String(value || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean);
}

function splitLines(value) {
  return String(value || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
