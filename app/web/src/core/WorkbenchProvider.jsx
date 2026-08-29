import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { createHttpClient, HttpError } from "../../core/http.js";
import { allowedViews, parseRoute } from "../../core/router.js";
import { createWorkspaceQuery } from "../../core/workspace.js";

const API = "/api/v1";
const WorkbenchContext = createContext(null);

function initialData() {
  return {
    questions: [], positions: [], knowledgeBases: [], candidates: [], appointments: [], roles: [], plans: [], interviews: [],
    catalog: [], providerConnections: [], modelConfigurations: [], routes: [], questionResults: null,
    selectedKnowledgeBase: null, speechBuilds: [], speechOptions: { current: null, items: [], candidates: [] },
    questionGenerationOptions: { positioning: "", tags: [], items: [], candidates: [] }, questionGenerationBatches: [], selectedGenerationBatch: null,
    questionOverview: { total: 0, ready: 0, recent: [] }, selectedInterview: null, report: null,
    invitation: null, candidateToken: null,
  };
}

const newestFirst = (items = []) => [...items].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));

export function WorkbenchProvider({ children }) {
  const dataRef = useRef(initialData());
  const [, renderVersion] = useState(0);
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const [auth, setAuth] = useState(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [fatalError, setFatalError] = useState("");
  const [modal, setModal] = useState(null);
  const [toasts, setToasts] = useState([]);

  const touch = useCallback(() => renderVersion((value) => value + 1), []);
  const http = useMemo(() => createHttpClient({
    onUnauthorized: () => { setAuth(null); setAuthRequired(true); },
  }), []);
  const request = useCallback((path, options) => http.request(path, options), [http]);
  const query = useMemo(() => createWorkspaceQuery({ api: request, state: dataRef.current, newestFirst }), [request]);

  const toast = useCallback((title, message, type = "success") => {
    const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    setToasts((items) => [...items, { id, title, message, type }]);
    window.setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 4200);
  }, []);

  const navigate = useCallback((view, id = null, { replace = false } = {}) => {
    const hash = `#${id ? `${view}/${id}` : view}`;
    if (replace) window.history.replaceState(null, "", hash);
    else window.location.hash = hash;
    setRoute(parseRoute(hash));
  }, []);

  useEffect(() => {
    const sync = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  const load = useCallback(async (activeRoute = route) => {
    setLoading(true);
    setFatalError("");
    try {
      if (activeRoute.view === "invite") {
        dataRef.current.invitation = await request(`${API}/public/interview-invitations/${encodeURIComponent(activeRoute.invitationToken || "")}`);
        touch();
        return;
      }
      if (activeRoute.view === "candidate") {
        dataRef.current.candidateToken = activeRoute.candidateToken;
        dataRef.current.selectedInterview = await request(`${API}/public/interviews/${encodeURIComponent(activeRoute.selectedInterviewId || "")}`, {
          headers: { "X-Candidate-Session-Token": activeRoute.candidateToken || "" },
        });
        if (activeRoute.clearCandidateTokenFromHash) navigate("candidate", activeRoute.selectedInterviewId, { replace: true });
        touch();
        return;
      }
      let principal = auth;
      if (!principal) {
        try {
          principal = await request(`${API}/auth/session`);
          setAuth(principal);
          setAuthRequired(false);
        } catch (error) {
          if (error instanceof HttpError && error.status === 401) {
            setAuthRequired(true);
            return;
          }
          throw error;
        }
      }
      const permitted = allowedViews(principal.roles);
      if (!permitted.has(activeRoute.view) && activeRoute.view !== "live") {
        const target = permitted.has("interviews") ? "interviews" : [...permitted][0];
        if (target) navigate(target, null, { replace: true });
        return;
      }
      await query.load(activeRoute.view, principal.roles, activeRoute);
      if (activeRoute.view === "live" && activeRoute.selectedInterviewId) {
        dataRef.current.selectedInterview = await request(`${API}/interviews/${encodeURIComponent(activeRoute.selectedInterviewId)}`);
        if (dataRef.current.selectedInterview.status === "report_ready") {
          dataRef.current.report = await request(`${API}/interviews/${encodeURIComponent(activeRoute.selectedInterviewId)}/report`);
        }
      }
      touch();
    } catch (error) {
      setFatalError(error?.message || "工作区加载失败");
    } finally {
      setLoading(false);
    }
  }, [auth, navigate, query, request, route, touch]);

  useEffect(() => { load(route); }, [route.view, route.knowledgeBaseId, route.generation, route.generationBatchId, route.providerConnectionId, route.selectedInterviewId, route.invitationToken]);

  const login = useCallback(async (token) => {
    http.setAccessToken(token);
    setAuth(null);
    setAuthRequired(false);
    const principal = await request(`${API}/auth/session`);
    setAuth(principal);
    const permitted = allowedViews(principal.roles);
    const target = permitted.has("overview") ? "overview" : [...permitted][0];
    navigate(target, null, { replace: true });
    await query.load(target, principal.roles, { view: target });
    touch();
  }, [http, navigate, query, request, touch]);

  const logout = useCallback(() => {
    http.clearAccessToken();
    setAuth(null);
    setAuthRequired(true);
  }, [http]);

  const refresh = useCallback(async (name) => {
    await query.refresh(name);
    touch();
  }, [query, touch]);

  const reloadRoute = useCallback(async (...names) => {
    query.invalidate(...names);
    await query.load(route.view, auth?.roles || [], route);
    touch();
  }, [auth, query, route, touch]);

  const setResource = useCallback((name, value) => {
    dataRef.current[name] = value;
    touch();
  }, [touch]);

  const value = {
    API, data: dataRef.current, route, auth, authRequired, loading, fatalError, request, login, logout, navigate, load,
    refresh, reloadRoute, setResource, touch, modal, openModal: setModal, closeModal: () => setModal(null), toast, toasts,
  };
  return <WorkbenchContext.Provider value={value}>{children}</WorkbenchContext.Provider>;
}

export function useWorkbench() {
  const value = useContext(WorkbenchContext);
  if (!value) throw new Error("useWorkbench must be used inside WorkbenchProvider");
  return value;
}
