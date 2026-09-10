import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkbenchProvider, useWorkbench } from "../../core/WorkbenchProvider.jsx";
import CandidateFeaturePage from "./Page.jsx";

const runtime = vi.hoisted(() => ({ avatars: [], open: vi.fn(), report: vi.fn() }));
vi.mock("./VrmAvatar.jsx", () => ({
  VrmAvatar: (props) => {
    runtime.avatars.push(props);
    return <div data-avatar={props.interviewId}>{props.avatar.status}</div>;
  },
}));
vi.mock("./agent-experience.js", async (importOriginal) => ({
  ...await importOriginal(),
  createCandidateInterviewExperience: () => ({ open: runtime.open }),
  reportCandidateRuntimeProblem: (...args) => runtime.report(...args),
}));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status });
const interview = (id) => ({ id, status: "in_progress", turns: [], answers: [] });
const makeRun = () => ({ close: vi.fn(), subscribe: vi.fn(() => () => {}), act: vi.fn(async () => {}) });

describe("candidate route isolation with the real workbench loader", () => {
  let host, root, workbench, requests;
  function Probe() {
    workbench = useWorkbench();
    return <CandidateFeaturePage />;
  }
  async function mount(id = "new", token = "token-new") {
    window.history.replaceState(null, "", `#candidate/${id}?token=${token}`);
    await act(async () => root.render(<WorkbenchProvider><Probe /></WorkbenchProvider>));
  }
  async function navigate(id, token) {
    sessionStorage.setItem(`candidate-session:${id}`, token);
    await act(async () => workbench.navigate("candidate", id, { replace: true }));
  }
  async function deliver(index, payload, status = 200) {
    await act(async () => requests[index].resolve(response(payload, status)));
  }
  const avatar = () => runtime.avatars.at(-1);

  beforeEach(() => {
    sessionStorage.clear();
    runtime.avatars.length = 0;
    runtime.open.mockReset().mockImplementation(async () => makeRun());
    runtime.report.mockReset().mockResolvedValue({ status: "paused" });
    requests = [];
    vi.stubGlobal("fetch", vi.fn((path, options) => {
      const pending = { ...deferred(), path, options };
      requests.push(pending);
      return pending.promise;
    }));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    // Settle abandoned HTTP mocks so their timeout handles do not leak.
    await act(async () => requests.forEach((pending) => pending.resolve(response({}))));
    host.remove();
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("never pairs an admin's previous interview with the new candidate credential while loading", async () => {
    await mount();
    await act(async () => {
      workbench.setResource("selectedInterview", interview("old-admin-interview"));
      workbench.setResource("candidateToken", "old-token");
    });
    expect(host.textContent).toContain("正在进入面试…");
    expect(runtime.avatars).toHaveLength(0);
    expect(runtime.open).not.toHaveBeenCalled();
    await deliver(0, interview("new"));
    expect(runtime.avatars.length).toBeGreaterThan(0);
    expect(runtime.avatars.every((props) => props.interviewId === "new" && props.candidateSessionToken === "token-new")).toBe(true);
    expect(requests[0].options.headers["X-Candidate-Session-Token"]).toBe("token-new");
    expect(window.location.hash).toBe("#candidate/new");
  });

  it.each(["success", "failure"])("ignores a previous page's late %s after the new page has loaded", async (outcome) => {
    await mount("old", "token-old");
    await navigate("new", "token-new");
    await deliver(1, interview("new"));
    if (outcome === "success") await deliver(0, interview("old"));
    else await deliver(0, { error: { message: "旧页面授权错误", code: "CANDIDATE_SESSION_TOKEN_INVALID" } }, 403);
    expect(avatar().interviewId).toBe("new");
    expect(workbench.data.candidateSession.interview.id).toBe("new");
    expect(workbench.fatalError).toBe("");
    expect(host.textContent).not.toContain("旧页面授权错误");
    expect(window.location.hash).toBe("#candidate/new");
    expect(runtime.avatars.every((props) => props.interviewId === "new")).toBe(true);
  });

  it("closes the old runtime and isolates readiness, fatal callbacks, and late pause acknowledgments", async () => {
    const oldRun = makeRun();
    runtime.open.mockResolvedValueOnce(oldRun);
    const pause = deferred();
    runtime.report.mockReturnValueOnce(pause.promise);
    await mount("old", "token-old");
    await deliver(0, interview("old"));
    const oldAvatar = avatar();
    await act(async () => oldAvatar.onReady({ fps: 60 }));
    expect(runtime.open).toHaveBeenCalledTimes(1);
    await act(async () => { void oldAvatar.onFatalProblem(new Error("旧页面授权错误")); });
    expect(host.textContent).toContain("正在暂停面试");
    expect(host.textContent).not.toContain("旧页面授权错误");
    await navigate("new", "token-new");
    expect(oldRun.close).toHaveBeenCalledTimes(1);
    expect(runtime.open.mock.calls[0][0].signal.aborted).toBe(true);
    expect(host.textContent).not.toContain("旧页面授权错误");
    await deliver(1, interview("new"));
    expect(runtime.open).toHaveBeenCalledTimes(1); // New avatar must pass its own readiness check.
    await act(async () => {
      await oldAvatar.onFatalProblem(new Error("迟到的旧错误"));
      pause.reject(new Error("旧暂停请求失败"));
    });
    expect(runtime.report).toHaveBeenCalledTimes(1);
    expect(runtime.report).toHaveBeenCalledWith(expect.objectContaining({ interviewId: "old", candidateSessionToken: "token-old" }));
    expect(host.querySelector(".candidate-problem")).toBeNull();
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(runtime.open).toHaveBeenLastCalledWith(expect.objectContaining({ interviewId: "new", ticket: "token-new" }));
  });

  it("requires a fresh binding and renderer when a credential rotates on the same interview", async () => {
    await mount("same", "token-first");
    await deliver(0, interview("same"));
    await act(async () => avatar().onReady({ fps: 60 }));
    const oldRun = await runtime.open.mock.results[0].value;
    await navigate("same", "token-second");
    expect(host.querySelector("[data-avatar]")).toBeNull();
    expect(oldRun.close).toHaveBeenCalledOnce();
    await deliver(1, interview("same"));
    expect(avatar().candidateSessionToken).toBe("token-second");
    expect(runtime.open).toHaveBeenCalledTimes(1);
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(runtime.open).toHaveBeenLastCalledWith(expect.objectContaining({ interviewId: "same", ticket: "token-second" }));
  });

  it("still blocks an actually unauthorized current session and clears that error on navigation", async () => {
    await mount("invalid", "invalid-token");
    await deliver(0, { error: { code: "CANDIDATE_SESSION_TOKEN_INVALID", message: "本场会话授权未通过" } }, 403);
    expect(host.querySelector('[role="alert"]').textContent).toContain("暂时无法进入面试");
    expect(workbench.fatalError).toContain("本场会话授权未通过");
    expect(runtime.avatars).toHaveLength(0);
    expect(runtime.open).not.toHaveBeenCalled();
    await navigate("new", "token-new");
    expect(host.textContent).not.toContain("本场会话授权未通过");
    await deliver(1, interview("unexpected-id"));
    expect(host.textContent).toContain("暂时无法进入面试");
    expect(workbench.fatalError).toContain("面试会话响应与当前链接不一致");
    expect(runtime.avatars).toHaveLength(0);
  });

  it("closes a runtime that finishes opening after a fatal renderer error", async () => {
    const pending = deferred();
    const run = makeRun();
    runtime.open.mockReturnValueOnce(pending.promise);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    await act(async () => avatar().onFatalProblem(new Error("当前数字人故障")));
    await act(async () => pending.resolve(run));
    expect(run.close).toHaveBeenCalledOnce();
    expect(run.subscribe).not.toHaveBeenCalled();
    expect(host.textContent).toContain("面试已暂停");
    expect(avatar().avatar.status).toBe("idle");
  });

  it("stops showing a speaking avatar and active answer controls when the current renderer fails", async () => {
    const run = makeRun();
    run.subscribe.mockImplementation((listener) => {
      listener({
        phase: "speaking", floor: "agent", session: { status: "in_progress" },
        currentQuestion: { question_text: "合成测试问题", turn_id: "turn-current" },
        connection: { control: "connected", media: "connected" },
        microphone: { localDetected: false }, evidence: { ready: true },
        serverAudio: { received: false }, captions: { full: [], recent: [] }, endpoint: { active: true },
        calibration: { status: "completed" }, avatar: { status: "speaking" },
        speechPlayback: { status: "blocked", message: "播放受阻" },
      });
      return () => {};
    });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(avatar().avatar.status).toBe("speaking");
    await act(async () => avatar().onFatalProblem(new Error("当前数字人故障")));
    expect(avatar().avatar.status).toBe("idle");
    expect(run.act).toHaveBeenCalledWith(expect.objectContaining({ type: "pause" }));
    expect(host.textContent).not.toContain("面试官正在说话");
    expect(host.textContent).not.toContain("播放这句话");
    const answerButtons = [...host.querySelectorAll(".recording-actions button")];
    expect(answerButtons).toHaveLength(3);
    expect(answerButtons.every((button) => button.disabled)).toBe(true);
  });
});
