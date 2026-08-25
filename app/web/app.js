const API = "/api/v1";

const state = {
  view: "overview",
  questions: [],
  roles: [],
  plans: [],
  interviews: [],
  catalog: [],
  providerConfigs: [],
  routes: [],
  questionResults: null,
  selectedInterview: null,
  activeEvaluation: null,
  report: null,
  socket: null,
  candidateToken: null,
  candidateMediaStream: null,
  candidateRecorder: null,
  candidateRecognition: null,
  candidateDevices: { audio: [], video: [] },
  candidateAudioEnabled: true,
  candidateVideoEnabled: true,
  candidateRecording: false,
  candidateRecordingPending: false,
  candidateAudioUri: null,
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
  plans: "面试计划",
  interviews: "面试会话",
  live: "实时面试",
  models: "模型服务",
  candidate: "候选人面试",
};

const statusLabels = {
  active: "可用",
  indexed: "已索引",
  pending: "处理中",
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
  if (state.view === "candidate") {
    await hydrateCandidateRoute();
    return;
  }
  stopCandidateMedia();
  if (!state.catalog.length) await loadWorkspace();
  await hydrateRoute();
}

async function loadWorkspace() {
  const [questions, roles, plans, interviews, catalog, providerConfigs, routes] = await Promise.all([
    api(`${API}/questions`),
    api(`${API}/role-requirements`),
    api(`${API}/interview-plans`),
    api(`${API}/interviews`),
    api(`${API}/admin/model-providers/catalog`),
    api(`${API}/admin/model-provider-configs`),
    api(`${API}/admin/model-routes`),
  ]);
  state.questions = newestFirst(questions.items);
  state.roles = newestFirst(roles.items);
  state.plans = newestFirst(plans.items);
  state.interviews = newestFirst(interviews.items);
  state.catalog = catalog.items;
  state.providerConfigs = newestFirst(providerConfigs.items);
  state.routes = newestFirst(routes.items);
}

async function refreshCollection(name) {
  const endpoints = {
    questions: `${API}/questions`,
    roles: `${API}/role-requirements`,
    plans: `${API}/interview-plans`,
    interviews: `${API}/interviews`,
    providerConfigs: `${API}/admin/model-provider-configs`,
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
    state.candidateToken = new URLSearchParams(query).get("token");
  } else if (["overview", "questions", "plans", "interviews", "models"].includes(view)) {
    state.view = view;
    state.selectedInterviewId = null;
  } else {
    state.view = "overview";
    state.selectedInterviewId = null;
  }
}

async function hydrateCandidateRoute() {
  disconnectSocket();
  if (!state.selectedInterviewId) return;
  try {
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterviewId)}`);
    state.report = null;
    if (state.selectedInterview.status === "report_ready") {
      state.report = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterviewId)}/report`);
    }
  } catch (error) {
    state.selectedInterview = null;
    toast("无法进入面试", error.message, "error");
  }
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
    plans: renderPlans,
    interviews: renderInterviews,
    live: renderLiveInterview,
    models: renderModels,
    candidate: renderCandidateRoom,
  };
  appContent.innerHTML = renderers[state.view]();
  bindViewEvents();
  iconize();
}

function updateShell() {
  document.body.classList.toggle("candidate-mode", state.view === "candidate");
  document.querySelector("#topbar-title").textContent = viewTitles[state.view];
  document.querySelectorAll("[data-view]").forEach((button) => {
    const target = state.view === "live" ? "interviews" : state.view;
    button.classList.toggle("is-active", button.dataset.view === target);
  });

  const actions = {
    overview: ["新建题目", "plus"],
    questions: ["新建题目", "plus"],
    plans: ["新建岗位", "briefcase-business"],
    interviews: ["创建面试", "calendar-plus"],
    live: ["返回会话", "arrow-left"],
    models: ["添加配置", "plug-zap"],
    candidate: ["候选人面试", "video"],
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
          <i data-lucide="calendar-plus" aria-hidden="true"></i>创建面试
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

function renderQuestions() {
  const items = state.questionResults === null ? state.questions : state.questionResults;
  const resultLabel = state.questionResults === null ? `${state.questions.length} 道题目` : `${items.length} 条搜索结果`;
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
      <button class="button button-secondary" type="submit"><i data-lucide="search" aria-hidden="true"></i>检索</button>
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
        <button class="button button-primary" type="button" data-action="create-interview" ${state.plans.length ? "" : "disabled"}><i data-lucide="calendar-plus" aria-hidden="true"></i>创建面试</button>
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
        <a class="candidate-brand" href="#candidate/${escapeHtml(interview.id)}?token=${escapeHtml(state.candidateToken || "")}" aria-label="Interviewer 候选人面试">
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

          ${interview.status === "scheduled" ? `
            <div class="candidate-waiting-row">
              <span><strong>面试尚未开始</strong><small>${mediaReady ? "设备检查已完成" : "请先完成设备检查"}</small></span>
              <button class="button button-primary" type="button" data-action="candidate-start-session" ${mediaReady ? "" : "disabled"}><i data-lucide="play" aria-hidden="true"></i>进入面试</button>
            </div>
          ` : renderCandidateAnswerControls(currentTurn, mediaReady)}
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
        <span id="recording-timer">${state.candidateRecording ? "录音中 00:00" : state.candidateAudioUri ? "录音已保存" : recognitionAvailable ? "浏览器语音识别可用" : "请使用文本补答"}</span>
      </div>
      <textarea class="form-textarea candidate-transcript" id="candidate-transcript" placeholder="回答内容将在这里实时显示，也可以手动修正" ${mediaReady ? "" : "disabled"}>${transcript}</textarea>
      <div class="interim-transcript" id="candidate-interim-transcript">${escapeHtml(state.candidateInterimTranscript)}</div>
    </div>
    <div class="candidate-answer-actions">
      <div class="recording-actions">
        <button class="button button-record" type="button" data-action="candidate-record" ${mediaReady && !state.candidateRecording ? "" : "disabled"}><i data-lucide="circle" aria-hidden="true"></i>开始回答</button>
        <button class="button button-secondary" type="button" data-action="candidate-stop-recording" ${state.candidateRecording ? "" : "disabled"}><i data-lucide="square" aria-hidden="true"></i>结束录音</button>
      </div>
      <button class="button button-primary" type="button" data-action="candidate-submit-answer" ${!state.candidateRecording && !state.candidateRecordingPending && state.candidateTranscript.trim() ? "" : "disabled"}><i data-lucide="send" aria-hidden="true"></i>提交本题</button>
    </div>
    ${state.candidateLastEvaluation ? `<div class="candidate-score-strip"><span><i data-lucide="circle-check" aria-hidden="true"></i>上一题已完成评分</span><strong>${state.candidateLastEvaluation.score} 分</strong></div>` : ""}
  `;
}

function renderCandidateCompletion(interview) {
  return `<section class="candidate-completion"><span class="completion-icon"><i data-lucide="badge-check" aria-hidden="true"></i></span><h1>面试已完成</h1><p>你的回答已经提交，面试报告将由面试官审核。</p><div class="candidate-completion-meta"><span><strong>${interview.turns?.length || 0}</strong><small>问答轮次</small></span><span><strong>${interview.answers?.length || 0}</strong><small>已提交回答</small></span></div></section>`;
}

function renderModels() {
  return `
    <section class="page-header">
      <div><h1>模型服务</h1><p>Provider 插件、组织配置与能力路由</p></div>
      <div class="page-actions">
        <button class="button button-secondary" type="button" data-action="create-route" ${state.providerConfigs.length ? "" : "disabled"}><i data-lucide="route" aria-hidden="true"></i>添加路由</button>
        <button class="button button-primary" type="button" data-action="create-provider"><i data-lucide="plug-zap" aria-hidden="true"></i>添加配置</button>
      </div>
    </section>

    <section>
      <div class="section-title-row"><div><h2>已安装插件</h2><p>${state.catalog.length} 个 Provider · ${state.catalog.filter((item) => item.implemented).length} 个可调用</p></div></div>
      <div class="provider-grid">${state.catalog.map(providerCard).join("")}</div>
    </section>

    <section class="section-block">
      <div class="section-title-row"><div><h2>组织配置</h2><p>凭证仅显示引用地址</p></div></div>
      ${state.providerConfigs.length ? providerConfigTable() : emptyState("plug", "暂无 Provider 配置", "当前组织没有模型配置")}
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

function planDetails(plan) {
  const role = state.roles.find((item) => item.id === plan.role_requirement_id);
  const assembly = plan.assembly_summary || {};
  const coverage = (assembly.coverage || []).map((item) => `<span class="tag">${escapeHtml(item.dimension)} ${item.selected_count}/${item.target_count}</span>`).join("");
  const warnings = (assembly.warnings || []).map((item) => `<div class="plan-warning"><i data-lucide="triangle-alert" aria-hidden="true"></i><span>${escapeHtml(item)}</span></div>`).join("");
  return `<details class="plan-item">
    <summary>
      <span class="plan-summary-title"><strong>${escapeHtml(role?.title || "面试计划")}</strong><small>${formatDate(plan.created_at)} · ${plan.items.length} 道题</small></span>
      <span class="muted">${plan.estimated_minutes} 分钟</span>
      <span class="status-badge status-${escapeHtml(plan.status)}">${statusLabel(plan.status)}</span>
      <i class="plan-chevron" data-lucide="chevron-right" aria-hidden="true"></i>
    </summary>
    <div class="plan-questions">
      ${(coverage || warnings) ? `<div class="plan-assembly-summary"><div><strong>装配覆盖</strong><span class="muted">候选 ${assembly.candidate_count || 0} · 已选 ${assembly.selected_question_count || plan.items.length}/${assembly.requested_question_count || plan.items.length}</span></div>${coverage ? `<div class="plan-coverage">${coverage}</div>` : ""}${warnings}</div>` : ""}
      ${plan.items.map((item) => {
        const question = state.questions.find((candidate) => candidate.id === item.question_id);
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
      <button class="icon-button" type="button" title="打开候选人房间" aria-label="打开候选人房间" data-action="open-candidate-room" data-id="${escapeHtml(interview.id)}"><i data-lucide="monitor-play" aria-hidden="true"></i></button>
      ${interview.status === "scheduled" ? `<button class="button button-primary button-small" type="button" data-action="start-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="play" aria-hidden="true"></i>开始</button>` : `<button class="button button-secondary button-small" type="button" data-action="open-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="external-link" aria-hidden="true"></i>${interview.status === "report_ready" ? "报告" : "进入"}</button>`}
    </div>
  </article>`;
}

function providerCard(provider) {
  const descriptions = {
    mock: "本地确定性模型，用于开发、检索和评分闭环。",
    openai_compatible: "兼容 Chat Completions 与 Embeddings 协议。",
  };
  return `<article class="provider-card"><div class="provider-card-head"><div><h3>${escapeHtml(provider.display_name)}</h3><span class="mono">${escapeHtml(provider.provider_id)}</span></div><span class="status-badge ${provider.implemented ? "" : "status-draft"}">${provider.implemented ? "可调用" : "待实现"}</span></div><p>${escapeHtml(descriptions[provider.provider_id] || "Provider 插件清单已安装，真实调用尚未接入。")}</p><div class="provider-capabilities">${(provider.capabilities || []).slice(0, 4).map(tag).join("")}${provider.capabilities.length > 4 ? `<span class="tag">+${provider.capabilities.length - 4}</span>` : ""}</div></article>`;
}

function providerConfigTable() {
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>配置名称</th><th>Provider</th><th>状态</th><th>凭证引用</th><th class="text-right">操作</th></tr></thead><tbody>${state.providerConfigs.map((config) => `<tr><td><span class="cell-title">${escapeHtml(config.display_name)}</span><span class="cell-subtitle">${formatDate(config.updated_at)}</span></td><td class="mono">${escapeHtml(config.provider_id)}</td><td><span class="status-badge ${config.enabled ? "" : "status-draft"}">${config.enabled ? "已启用" : "已停用"}</span></td><td class="mono">${escapeHtml(config.credential_ref || "-")}</td><td><div class="table-actions"><button class="button button-secondary button-small" type="button" data-action="test-provider" data-id="${escapeHtml(config.id)}"><i data-lucide="flask-conical" aria-hidden="true"></i>测试</button></div></td></tr>`).join("")}</tbody></table></div>`;
}

function routeTable() {
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>用途</th><th>能力</th><th>主配置</th><th>模型</th><th class="text-right">操作</th></tr></thead><tbody>${state.routes.map((route) => {
    const config = state.providerConfigs.find((item) => item.id === route.primary?.provider_config_id);
    return `<tr><td><span class="cell-title">${escapeHtml(route.purpose)}</span></td><td class="mono">${escapeHtml(route.capability)}</td><td>${escapeHtml(config?.display_name || route.primary?.provider_config_id || "-")}</td><td class="mono">${escapeHtml(route.primary?.model || "默认模型")}</td><td><div class="table-actions"><button class="button button-secondary button-small" type="button" data-action="test-route" data-id="${escapeHtml(route.id)}"><i data-lucide="flask-conical" aria-hidden="true"></i>测试</button></div></td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function renderQuestionPanel(interview, currentTurn, canAnswer, canComplete) {
  if (interview.status === "scheduled") {
    return `<span class="question-eyebrow">面试待开始</span><h2>会话已创建，候选人资料和题目计划已就绪。</h2><button class="button button-primary" type="button" data-action="start-interview" data-id="${escapeHtml(interview.id)}"><i data-lucide="play" aria-hidden="true"></i>开始面试</button>`;
  }
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
  return `<span class="question-eyebrow">第 ${currentTurn.order} 题</span><h2>${escapeHtml(currentTurn.question_spoken_text)}</h2><form id="answer-form"><textarea class="form-textarea answer-box" name="final_transcript" placeholder="候选人实时转写或人工记录" required ${canAnswer ? "" : "disabled"}>${escapeHtml(state.liveTranscript)}</textarea><div class="session-action-row"><small>实时转写 · 文本兜底</small><div class="table-actions"><button class="button button-secondary" type="button" data-action="skip-interview-turn" data-id="${escapeHtml(interview.id)}" ${canAnswer ? "" : "disabled"}><i data-lucide="skip-forward" aria-hidden="true"></i>跳过本题</button><button class="button button-primary" type="submit" ${canAnswer ? "" : "disabled"}><i data-lucide="send" aria-hidden="true"></i>提交回答</button></div></div></form>`;
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
  const answerForm = document.querySelector("#answer-form");
  if (answerForm) answerForm.addEventListener("submit", submitAnswer);
  const candidateTranscript = document.querySelector("#candidate-transcript");
  if (candidateTranscript) {
    candidateTranscript.addEventListener("input", (event) => {
      state.candidateTranscript = event.target.value;
      const submit = document.querySelector('[data-action="candidate-submit-answer"]');
      if (submit) submit.disabled = state.candidateRecording || !state.candidateTranscript.trim();
    });
  }
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
    "create-role": openRoleModal,
    "generate-plan": openPlanModal,
    "approve-plan": () => approvePlan(id, button),
    "create-interview": openInterviewModal,
    "create-provider": openProviderModal,
    "create-route": openRouteModal,
    "go-questions": () => navigate("questions"),
    "go-interviews": () => navigate("interviews"),
    "clear-search": () => { state.questionResults = null; render(); },
    "view-question": () => openQuestionDetails(id),
    "open-interview": () => navigate("interviews", id),
    "open-candidate-room": () => openCandidateRoom(id),
    "start-interview": () => startInterview(id, button),
    "pause-interview": () => controlInterview(id, "pause", "interviewer paused", button),
    "recover-interview": () => controlInterview(id, "recover", "interviewer resumed", button),
    "skip-interview-turn": () => controlInterview(id, "skip", "interviewer skipped turn", button),
    "cancel-interview": () => controlInterview(id, "cancel", "interviewer cancelled", button),
    "complete-interview": () => completeInterview(id, button),
    "refresh-interviews": refreshInterviews,
    "test-provider": () => testProvider(id, button),
    "test-route": () => testRoute(id, button),
    "candidate-enable-media": enableCandidateMedia,
    "candidate-toggle-audio": toggleCandidateAudio,
    "candidate-toggle-video": toggleCandidateVideo,
    "candidate-start-session": startCandidateSession,
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
    plans: openRoleModal,
    interviews: openInterviewModal,
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
    const result = await api(`${API}/questions/search`, {
      method: "POST",
      body: {
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

function openQuestionModal() {
  openModal("新建题目", `
    <form id="question-create-form">
      <div class="form-grid">
        ${field("题目标题", `<input class="form-input" name="title" required maxlength="120" />`, true)}
        ${field("题库 ID", `<input class="form-input" name="knowledge_base_id" value="kb_backend" required />`)}
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
  const item = await api(`${API}/questions`, {
    method: "POST",
    body: {
      title: data.get("title"),
      knowledge_base_id: data.get("knowledge_base_id"),
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
  state.questionResults = null;
  closeModal();
  render();
  toast("题目已创建", `${item.title} 已完成索引`);
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
  openModal("新建岗位要求", `
    <form id="role-create-form"><div class="form-grid">
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
  const item = await api(`${API}/role-requirements`, {
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
  if (!state.roles.length || !state.questions.length) {
    toast("暂时无法生成", "请先创建岗位要求和题目", "error");
    return;
  }
  openModal("生成面试计划", `
    <form id="plan-create-form"><div class="form-grid">
      ${field("岗位要求", `<select class="form-select" name="role_requirement_id">${state.roles.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("")}</select>`, true)}
      ${field("题目数量", `<input class="form-input" name="question_count" type="number" min="1" max="20" value="${Math.min(8, Math.max(1, state.questions.length))}" required />`)}
      ${field("题库 ID", `<input class="form-input" name="knowledge_base_ids" value="kb_backend" />`, true)}
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
  setBusy(submit, true, "生成中");
  const plan = await api(`${API}/interview-plans/generate`, {
    method: "POST",
    body: {
      role_requirement_id: data.get("role_requirement_id"),
      knowledge_base_ids: splitComma(data.get("knowledge_base_ids")),
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
  toast("计划草稿已生成", `已选择 ${plan.items.length} 道题目，请审批后创建面试`);
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

function openInterviewModal() {
  const approvedPlans = state.plans.filter((plan) => plan.status === "approved");
  if (!approvedPlans.length) {
    toast("暂时无法创建", "请先审批至少一份面试计划", "error");
    return;
  }
  openModal("创建面试", `
    <form id="interview-create-form"><div class="form-grid">
      ${field("已审批面试计划", `<select class="form-select" name="plan_id">${approvedPlans.map((plan) => { const role = state.roles.find((item) => item.id === plan.role_requirement_id); return `<option value="${escapeHtml(plan.id)}">${escapeHtml(role?.title || "面试计划")} · ${plan.items.length} 题</option>`; }).join("")}</select>`, true)}
      ${field("候选人姓名", `<input class="form-input" name="candidate_name" required />`)}
      ${field("候选人邮箱", `<input class="form-input" name="candidate_email" type="email" />`)}
      ${field("计划时间", `<input class="form-input" name="scheduled_at" type="datetime-local" />`)}
    </div></form>`, {
      submitLabel: "创建会话",
      submitIcon: "calendar-plus",
      onSubmit: createInterview,
      formId: "interview-create-form",
    });
}

async function createInterview(form, submit) {
  const data = new FormData(form);
  setBusy(submit, true, "创建中");
  const interview = await api(`${API}/interviews`, {
    method: "POST",
    body: {
      plan_id: data.get("plan_id"),
      candidate: { name: data.get("candidate_name"), email: data.get("candidate_email") || null },
      scheduled_at: data.get("scheduled_at") || null,
      settings: { record_audio: false, record_video: false, allow_text_fallback: true, avatar_id: "avatar_default_cn" },
    },
  });
  await refreshCollection("interviews");
  closeModal();
  toast("面试已创建", `${interview.candidate.name} 的会话已就绪`);
  navigate("interviews", interview.id);
}

async function startInterview(id, button) {
  setBusy(button, true, "启动中");
  try {
    await api(`${API}/interviews/${encodeURIComponent(id)}/start`, { method: "POST" });
    await refreshCollection("interviews");
    navigate("interviews", id);
    toast("面试已开始", "实时会话已连接");
  } catch (error) {
    setBusy(button, false);
    toast("启动失败", error.message, "error");
  }
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

async function submitAnswer(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  const data = new FormData(form);
  setBusy(submit, true, "评分中");
  try {
    const result = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}/answers`, {
      method: "POST",
      body: {
        turn_id: state.selectedInterview.current_turn_id,
        final_transcript: data.get("final_transcript"),
        language: "zh-CN",
        duration_seconds: 0,
      },
    });
    state.activeEvaluation = result.evaluation;
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}`);
    state.report = state.selectedInterview.status === "report_ready"
      ? await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}/report`)
      : null;
    await refreshCollection("interviews");
    render();
    toast("评分完成", `本题得分 ${result.evaluation.score}`);
  } catch (error) {
    setBusy(submit, false);
    toast("提交失败", error.message, "error");
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

function openCandidateRoom(id) {
  const interview = state.interviews.find((item) => item.id === id);
  if (!interview?.candidate_join_url) {
    toast("候选人链接不可用", "请重新创建面试会话", "error");
    return;
  }
  window.open(interview.candidate_join_url, "_blank", "noopener,noreferrer");
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

async function startCandidateSession() {
  if (!state.selectedInterview || !state.candidateMediaStream) return;
  try {
    await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}/start`, { method: "POST" });
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}`);
    await ensureCandidateSocket();
    render();
    window.setTimeout(speakCandidateQuestion, 250);
  } catch (error) {
    toast("无法开始面试", error.message, "error");
  }
}

async function speakCandidateQuestion() {
  const interview = state.selectedInterview;
  if (!interview?.current_turn_id) return;
  try {
    const response = await api(`${API}/interviews/${encodeURIComponent(interview.id)}/avatar/speak`, {
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
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}`);
    render();
    if (state.selectedInterview.current_turn_id) window.setTimeout(speakCandidateQuestion, 300);
    return;
  }
  if (event.type === "interview.completed") {
    state.selectedInterview = await api(`${API}/interviews/${encodeURIComponent(state.selectedInterview.id)}`);
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
    toast("录音不可用", "当前浏览器不支持 MediaRecorder，请使用文本补答", "error");
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
  if (!text || state.candidateRecording || state.candidateRecordingPending) return;
  try {
    const socket = await ensureCandidateSocket();
    socket.send(JSON.stringify({
      type: "candidate.transcript.final",
      turn_id: state.selectedInterview.current_turn_id,
      payload: {
        text,
        confidence: state.candidateRecognition ? 0 : 0.5,
        language: "zh-CN",
        source: "browser_speech_fallback",
        duration_seconds: state.candidateRecordingDuration,
        audio_uri: state.candidateAudioUri,
      },
    }));
    const submit = document.querySelector('[data-action="candidate-submit-answer"]');
    setBusy(submit, true, "正在评分");
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

function openProviderModal() {
  const implemented = state.catalog.filter((item) => item.implemented);
  openModal("添加 Provider 配置", `
    <form id="provider-create-form"><div class="form-grid">
      ${field("Provider", `<select class="form-select" name="provider_id">${implemented.map((item) => `<option value="${escapeHtml(item.provider_id)}">${escapeHtml(item.display_name)}</option>`).join("")}</select>`)}
      ${field("配置名称", `<input class="form-input" name="display_name" value="开发模型" required />`)}
      ${field("Base URL", `<input class="form-input" name="base_url" placeholder="https://api.example.com/v1" />`, true)}
      ${field("测试模型", `<input class="form-input" name="test_model" placeholder="chat-model-default" />`)}
      ${field("API Key", `<input class="form-input" name="api_key" type="password" autocomplete="new-password" />`)}
    </div></form>`, {
      submitLabel: "保存配置",
      submitIcon: "save",
      onSubmit: createProvider,
      formId: "provider-create-form",
    });
}

async function createProvider(form, submit) {
  const data = new FormData(form);
  const config = {};
  if (data.get("base_url")) config.base_url = data.get("base_url");
  if (data.get("test_model")) config.test_model = data.get("test_model");
  setBusy(submit, true, "保存中");
  const item = await api(`${API}/admin/model-provider-configs`, {
    method: "POST",
    body: {
      provider_id: data.get("provider_id"),
      display_name: data.get("display_name"),
      enabled: true,
      config,
      credentials: data.get("api_key") ? { api_key: data.get("api_key") } : {},
    },
  });
  await refreshCollection("providerConfigs");
  closeModal();
  render();
  toast("配置已保存", item.display_name);
}

function openRouteModal() {
  if (!state.providerConfigs.length) return;
  const capabilities = ["llm.chat_json", "embedding.text"];
  openModal("添加能力路由", `
    <form id="route-create-form"><div class="form-grid">
      ${field("调用用途", `<input class="form-input" name="purpose" value="answer_evaluation" required />`)}
      ${field("模型能力", `<select class="form-select" name="capability">${capabilities.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("")}</select>`)}
      ${field("Provider 配置", `<select class="form-select" name="provider_config_id">${state.providerConfigs.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.display_name)}</option>`).join("")}</select>`)}
      ${field("模型 ID", `<input class="form-input" name="model" value="mock-json" required />`)}
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
  setBusy(submit, true, "保存中");
  const route = await api(`${API}/admin/model-routes`, {
    method: "POST",
    body: {
      capability: data.get("capability"),
      purpose: data.get("purpose"),
      primary: { provider_config_id: data.get("provider_config_id"), model: data.get("model"), timeout_s: 30 },
      fallbacks: [],
      policy: { max_retries: 1 },
      enabled: true,
    },
  });
  await refreshCollection("routes");
  closeModal();
  render();
  toast("路由已保存", `${route.purpose} · ${route.capability}`);
}

async function testProvider(id, button) {
  setBusy(button, true, "测试中");
  try {
    const result = await api(`${API}/admin/model-provider-configs/${encodeURIComponent(id)}/test`, { method: "POST" });
    toast("Provider 可用", `${result.provider?.provider_id || "provider"} · ${result.latency_ms || result.provider?.latency_ms || 0} ms`);
  } catch (error) {
    toast("Provider 测试失败", error.message, "error");
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
    const textarea = document.querySelector("#answer-form textarea");
    if (textarea) textarea.value = state.liveTranscript;
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
  const fetchOptions = { method: options.method || "GET", headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    fetchOptions.headers["Content-Type"] = "application/json";
    fetchOptions.body = JSON.stringify(options.body);
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
