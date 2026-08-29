const API = "/api/v1";

export function createWorkspaceQuery({ api, state, newestFirst }) {
  const loaded = new Set();

  async function collection(name, endpoint) {
    const data = await api(endpoint);
    state[name] = newestFirst(data.items);
    loaded.add(name);
    return state[name];
  }

  async function questionCatalog() {
    if (loaded.has("questionCatalog")) return;
    const data = await api(`${API}/workspace/question-catalog`);
    state.positions = newestFirst(data.positions);
    state.knowledgeBases = newestFirst(data.knowledge_bases);
    state.questions = newestFirst(data.questions);
    loaded.add("positions");
    loaded.add("knowledgeBases");
    loaded.add("questions");
    loaded.add("questionCatalog");
  }

  async function positions() {
    if (!loaded.has("positions")) await collection("positions", `${API}/job-positions`);
  }

  async function knowledgeBases() {
    if (!loaded.has("knowledgeBases")) await collection("knowledgeBases", `${API}/knowledge-bases`);
  }

  async function questions() {
    if (!loaded.has("questionCatalog")) await questionCatalog();
  }

  async function ensure(name, endpoint) {
    if (!loaded.has(name)) await collection(name, endpoint);
  }

  async function load(view, roles = [], route = {}) {
    const isAdmin = roles.includes("admin");
    if (view === "models") {
      if (!isAdmin) return;
      await Promise.all([
        ensure("catalog", `${API}/admin/model-providers/catalog`),
        ensure("providerConnections", `${API}/admin/model-provider-connections`),
        ensure("modelConfigurations", `${API}/admin/model-configurations`),
        ensure("routes", `${API}/admin/model-routes`),
      ]);
      return;
    }
    if (view === "questions") {
      await Promise.all([positions(), knowledgeBases()]);
      if (route.knowledgeBaseId) {
        const id = encodeURIComponent(route.knowledgeBaseId);
        if (route.generation) {
          const [knowledgeBase, generationOptions, generationBatches, selectedBatch] = await Promise.all([
            api(`${API}/knowledge-bases/${id}`),
            api(`${API}/knowledge-bases/${id}/question-generation-options`),
            api(`${API}/knowledge-bases/${id}/question-generation-batches`),
            route.generationBatchId
              ? api(`${API}/question-generation-batches/${encodeURIComponent(route.generationBatchId)}`)
              : Promise.resolve(null),
          ]);
          state.selectedKnowledgeBase = knowledgeBase;
          state.questionGenerationOptions = generationOptions;
          state.questionGenerationBatches = newestFirst(generationBatches.items);
          state.selectedGenerationBatch = selectedBatch;
          loaded.add("knowledgeBaseDetail");
          loaded.add("questionGenerationOptions");
          loaded.add("questionGenerationBatches");
          loaded.add("selectedGenerationBatch");
          return;
        }
        const [knowledgeBase, questionData, buildData, speechOptions] = await Promise.all([
          api(`${API}/knowledge-bases/${id}`),
          api(`${API}/knowledge-bases/${id}/questions`),
          api(`${API}/knowledge-bases/${id}/speech-builds`),
          api(`${API}/knowledge-bases/${id}/speech-options`),
        ]);
        state.selectedKnowledgeBase = knowledgeBase;
        state.questions = newestFirst(questionData.items);
        state.speechBuilds = newestFirst(buildData.items);
        state.speechOptions = speechOptions;
        state.selectedGenerationBatch = null;
        loaded.add("questions");
        loaded.add("knowledgeBaseDetail");
        loaded.add("speechBuilds");
        loaded.add("speechOptions");
      } else {
        state.selectedKnowledgeBase = null;
        state.questions = [];
        state.speechBuilds = [];
        state.speechOptions = { current: null, items: [] };
        state.questionGenerationOptions = { positioning: "", tags: [], items: [], candidates: [] };
        state.questionGenerationBatches = [];
        state.selectedGenerationBatch = null;
      }
      return;
    }
    if (view === "workflow") {
      await Promise.all([
        positions(),
        ensure("candidates", `${API}/candidate-profiles`),
        ensure("appointments", `${API}/interview-appointments`),
        ensure("roles", `${API}/role-requirements`),
      ]);
      await knowledgeBases();
      return;
    }
    if (view === "plans") {
      await Promise.all([
        ensure("roles", `${API}/role-requirements`),
        ensure("plans", `${API}/interview-plans`),
        ensure("candidates", `${API}/candidate-profiles`),
      ]);
      await questions();
      return;
    }
    if (view === "interviews" || view === "live") {
      await ensure("interviews", `${API}/interviews`);
      if (roles.includes("reviewer") && !roles.includes("admin") && !roles.includes("interviewer")) return;
      await Promise.all([
        ensure("plans", `${API}/interview-plans`),
        ensure("roles", `${API}/role-requirements`),
        ensure("candidates", `${API}/candidate-profiles`),
        positions(),
      ]);
      return;
    }
    if (view === "overview") {
      await Promise.all([
        ensure("roles", `${API}/role-requirements`),
        ensure("plans", `${API}/interview-plans`),
        ensure("interviews", `${API}/interviews`),
        api(`${API}/workspace/question-overview`).then((data) => { state.questionOverview = data; }),
      ]);
      if (isAdmin) await ensure("catalog", `${API}/admin/model-providers/catalog`);
    }
  }

  async function refresh(name) {
    loaded.delete(name);
    if (name === "questions") {
      loaded.delete("questionCatalog");
      return questions();
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
      knowledgeBases: `${API}/knowledge-bases`,
    };
    return collection(name, endpoints[name]);
  }

  function invalidate(...names) {
    names.forEach((name) => loaded.delete(name));
    if (names.some((name) => ["questions", "knowledgeBases", "positions"].includes(name))) {
      loaded.delete("questionCatalog");
    }
  }

  return { load, refresh, invalidate };
}
