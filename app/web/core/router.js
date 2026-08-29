const ADMIN_VIEWS = new Set(["overview", "questions", "workflow", "plans", "interviews", "models"]);

export function parseRoute(hash, sessionStorage = window.sessionStorage) {
  const route = String(hash || "").replace(/^#\/?/, "") || "overview";
  const [path, query = ""] = route.split("?", 2);
  const [view, id, child, childId] = path.split("/");
  if (view === "interviews" && id) return { view: "live", selectedInterviewId: id };
  if (view === "candidate" && id) {
    const suppliedToken = new URLSearchParams(query).get("token");
    const storageKey = `candidate-session:${id}`;
    if (suppliedToken) sessionStorage.setItem(storageKey, suppliedToken);
    return {
      view: "candidate",
      selectedInterviewId: id,
      candidateToken: suppliedToken || sessionStorage.getItem(storageKey),
      clearCandidateTokenFromHash: Boolean(suppliedToken),
    };
  }
  if (view === "invite" && id) return { view: "invite", invitationToken: id };
  if (view === "questions" && id) return {
    view: "questions",
    knowledgeBaseId: id,
    generation: child === "generation",
    generationBatchId: child === "generation" ? childId || null : null,
  };
  if (view === "models" && id) return { view: "models", providerConnectionId: id };
  if (ADMIN_VIEWS.has(view)) return { view, selectedInterviewId: null };
  return { view: "overview", selectedInterviewId: null };
}

export function allowedViews(roles = []) {
  const roleSet = new Set(roles);
  if (roleSet.has("admin")) return new Set(ADMIN_VIEWS);
  if (roleSet.has("interviewer")) return new Set(["overview", "questions", "workflow", "plans", "interviews"]);
  if (roleSet.has("reviewer")) return new Set(["interviews"]);
  return new Set();
}
