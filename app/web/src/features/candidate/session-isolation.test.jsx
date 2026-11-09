import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkbenchProvider, useWorkbench } from "../../core/WorkbenchProvider.jsx";
import CandidateFeaturePage from "./Page.jsx";
import { discardPreparedCandidateMedia, prepareCandidateMedia } from "./preflight.js";

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
    discardPreparedCandidateMedia();
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
    discardPreparedCandidateMedia();
  });

  it("shows a persisted pause without restarting devices, opening the agent, or reporting another failure", async () => {
    const getUserMedia = vi.fn();
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    const audio = { readyState: "live", stop: vi.fn() };
    const video = { readyState: "live", stop: vi.fn() };
    const stream = { getTracks: () => [audio, video], getAudioTracks: () => [audio], getVideoTracks: () => [video] };
    prepareCandidateMedia(stream, {});
    await mount("paused", "token-paused");
    await deliver(0, { ...interview("paused"), status: "paused" });
    await act(async () => {
      avatar().onReady({ fps: 60 });
      await avatar().onFatalProblem(new Error("synthetic renderer failure"));
    });
    expect(host.querySelector(".candidate-header-meta").textContent).toContain("面试已暂停");
    expect(host.querySelector(".candidate-warmup h1").textContent).toBe("面试已暂停");
    expect(host.querySelector(".candidate-camera").textContent).toContain("画面已关闭");
    expect(host.querySelector(".candidate-signal-list").textContent).toContain("面试已暂停，收音已停止");
    expect(host.textContent).not.toMatch(/正在连接|正在进入|synthetic/);
    expect(host.querySelector("video").srcObject).toBeNull();
    expect(audio.stop).toHaveBeenCalledOnce();
    expect(video.stop).toHaveBeenCalledOnce();
    expect(runtime.open).not.toHaveBeenCalled();
    expect(runtime.report).not.toHaveBeenCalled();
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it.each(["confirmed", "rejected"])("keeps startup failure surfaces consistent while pause is pending and then %s", async outcome => {
    const pause = deferred();
    runtime.open.mockRejectedValueOnce(new Error("synthetic control handshake failed"));
    runtime.report.mockReturnValueOnce(pause.promise);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(host.querySelector(".candidate-header-meta").textContent).toContain("正在暂停面试");
    expect(host.querySelector(".candidate-warmup h1").textContent).toBe("正在暂停面试");
    expect(host.querySelector(".candidate-camera").textContent).toContain("画面已关闭");
    expect(host.querySelector(".candidate-signal-list").textContent).toContain("正在确认暂停结果");
    expect(host.textContent).not.toMatch(/正在连接|正在进入|面试已暂停|synthetic/);
    await act(async () => {
      if (outcome === "confirmed") pause.resolve({ status: "paused" });
      else pause.reject(new Error("synthetic report failed"));
    });
    expect(host.textContent).not.toMatch(/正在连接|正在进入|正在暂停|synthetic/);
    if (outcome === "confirmed") {
      expect(host.querySelector(".candidate-header-meta").textContent).toContain("面试已暂停");
      expect(host.querySelector(".candidate-warmup h1").textContent).toBe("面试已暂停");
    } else {
      expect(host.querySelector(".candidate-header-meta").textContent).toContain("连接已中断");
      expect(host.querySelector(".candidate-warmup h1").textContent).toBe("暂时无法进入面试");
      expect(host.textContent).not.toContain("面试已暂停");
    }
    expect(runtime.open).toHaveBeenCalledOnce();
    expect(runtime.report).toHaveBeenCalledOnce();
  });

  it("retains the connecting display during an actual pending startup", async () => {
    const pending = deferred();
    runtime.open.mockReturnValueOnce(pending.promise);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(host.querySelector(".candidate-header-meta").textContent).toContain("正在连接");
    expect(host.querySelector(".candidate-camera").textContent).toContain("正在连接摄像头");
    expect(host.querySelector(".candidate-warmup h1").textContent).toBe("正在进入面试");
    expect(host.textContent).not.toContain("面试已暂停");
    await act(async () => pending.resolve(makeRun()));
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

  it("renders a persisted completion while scoring is pending without reopening devices or the agent", async () => {
    const getUserMedia = vi.fn();
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    await mount("finished", "token-finished");
    await deliver(0, { ...interview("finished"), completion: {
      interview_id: "finished", status: "submitted_for_human_review", submitted_at: "2026-09-23T00:00:00Z",
      recording_retention_notice: "录音按同意范围保留。", human_review_required: true,
    } });
    expect(host.textContent).toContain("面试已提交");
    expect(runtime.avatars).toHaveLength(0);
    expect(runtime.open).not.toHaveBeenCalled();
    expect(runtime.report).not.toHaveBeenCalled();
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("keeps the room mounted for a live terminal farewell and disables answer controls until receipt", async () => {
    const run = makeRun();
    let update;
    const receipt = { interview_id: "new", status: "report_ready", submitted_at: "2026-09-23T00:00:00Z",
      recording_retention_notice: "录音按同意范围保留。", human_review_required: true };
    const state = { phase: "listening", floor: "candidate", session: { status: "in_progress" },
      currentQuestion: { question_text: "合成测试问题", turn_id: "turn-current" },
      connection: { control: "connected", media: "connected" }, microphone: { localDetected: false, level: 0 }, evidence: { ready: true },
      serverAudio: { received: false }, captions: { forming: false, full: [], recent: [] }, endpoint: { active: false },
      calibration: { status: "completed" }, avatar: { status: "idle" }, problem: null,
    };
    run.subscribe.mockImplementation(listener => { update = listener; listener(state); return () => {}; });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    const closing = { ...state, phase: "closing", floor: "agent", currentQuestion: null, evidence: { ready: false },
      session: { status: "report_ready", completion: receipt, completion_presentation: "closing" } };
    await act(async () => update(closing));
    expect(host.textContent).toContain("感谢你的参与，请听完面试官的结束说明");
    expect(host.textContent).not.toContain("面试已提交");
    expect(run.close).not.toHaveBeenCalled();
    const controls = [...host.querySelectorAll("button")].filter(button => ["提前结束回答", "请再说一遍", "继续补充", "暂停面试"].includes(button.textContent));
    expect(controls).toHaveLength(4);
    expect(controls.every(button => button.disabled)).toBe(true);
    await act(async () => update({ ...closing, phase: "completed", completion: receipt }));
    expect(host.textContent).toContain("面试已提交");
    expect(run.close).toHaveBeenCalledOnce();
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
        problem: { code: "UNDERSTANDING_UNAVAILABLE", recoverable: true },
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
    expect(host.querySelector(".candidate-header-meta").textContent).toContain("面试已暂停");
    expect(host.querySelector(".candidate-camera").textContent).toContain("画面已关闭");
    expect(host.textContent).not.toContain("回答处理暂时遇到问题");
    const answerButtons = [...host.querySelectorAll(".recording-actions button")];
    expect(answerButtons).toHaveLength(3);
    expect(answerButtons.every((button) => button.disabled)).toBe(true);
  });

  it("uses an authoritative paused snapshot across all surfaces while retaining the current question and transcript", async () => {
    const run = makeRun();
    run.subscribe.mockImplementation(listener => {
      listener({ phase: "paused", floor: "none", session: { status: "paused" },
        currentQuestion: { question_text: "暂停中的合成问题", turn_id: "turn-current" },
        connection: { control: "connected", media: "connected" },
        microphone: { localDetected: true, level: .6 }, evidence: { ready: false },
        serverAudio: { received: false }, captions: { full: [], recent: [{ text: "暂停前的合成回答", final: false }] },
        endpoint: { active: false }, calibration: { status: "completed" }, avatar: { status: "idle" },
      });
      return () => {};
    });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(host.querySelector(".candidate-header-meta").textContent).toContain("面试已暂停");
    expect(host.querySelector(".candidate-camera").textContent).toContain("画面已关闭");
    expect(host.querySelector(".candidate-signal-list").textContent).toContain("面试已暂停，收音已停止");
    expect(host.textContent).toContain("暂停中的合成问题");
    expect(host.textContent).toContain("暂停前的合成回答");
    expect(host.textContent).not.toMatch(/正在连接|正在进入|已连接|正在听你说/);
    expect([...host.querySelectorAll(".candidate-answer-actions button")].every(button => button.disabled)).toBe(true);
    expect(runtime.report).not.toHaveBeenCalled();
  });

  it("requires a spoken withdrawal confirmation and keeps answer controls from authorizing it", async () => {
    const run = makeRun();
    run.subscribe.mockImplementation((listener) => {
      listener({
        phase: "awaiting_exit_confirmation", floor: "candidate", session: { status: "in_progress" },
        currentQuestion: { question_text: "合成测试问题", turn_id: "turn-current" },
        connection: { control: "connected", media: "connected" },
        microphone: { localDetected: false }, evidence: { ready: true },
        serverAudio: { received: false }, captions: { full: [], recent: [] }, endpoint: { active: false },
        calibration: { status: "completed" }, avatar: { status: "idle" },
      });
      return () => {};
    });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(host.textContent).toContain("请确认是否结束本次面试");
    const buttons = [...host.querySelectorAll(".recording-actions button")];
    const finish = buttons.find(button => button.textContent === "提前结束回答");
    const repeat = buttons.find(button => button.textContent === "请再说一遍");
    expect(finish.disabled).toBe(true);
    expect(repeat.disabled).toBe(false);
    await act(async () => finish.click());
    expect(run.act).not.toHaveBeenCalled();
    await act(async () => repeat.click());
    expect(run.act).toHaveBeenCalledOnce();
    expect(run.act).toHaveBeenCalledWith(expect.objectContaining({ type: "request_repeat" }));
  });

  it.each(["listening", "answer_preparing", "understanding"])("shows one failure state despite a stale %s phase and keeps microphone status factual", async phase => {
    const run = makeRun();
    run.subscribe.mockImplementation(listener => {
      listener({ phase, floor: "candidate", session: { status: "in_progress" },
        currentQuestion: { question_text: "合成测试问题", turn_id: "turn-current" },
        connection: { control: "connected", media: "connected" },
        microphone: { localDetected: false, level: 0 }, evidence: { ready: true },
        serverAudio: { received: false }, captions: { full: [], recent: [] }, endpoint: { active: false },
        calibration: { status: "completed" }, avatar: { status: "idle" },
        problem: { code: "UNDERSTANDING_RESULT_REJECTED", recoverable: true, message: "secret model response" },
      });
      return () => {};
    });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(host.textContent).toContain("回答处理需要重试");
    expect(host.textContent).toContain("无需重说整题");
    expect(host.textContent).toContain("麦克风已开启，可随时说话");
    expect(host.textContent).not.toMatch(/正在整理|等待你开口|还有什么需要补充|secret model/);
    expect(host.querySelectorAll(".candidate-question-band .candidate-waiting-row")).toHaveLength(1);
  });

  it.each([[true, false], [true, true], [false, false]])("offers retained-answer retry only for ready=%s and pending=%s", async (ready, pending) => {
    const run = makeRun();
    run.subscribe.mockImplementation(listener => {
      listener({ phase: "listening", floor: "candidate", session: { status: "in_progress" },
        currentQuestion: { question_text: "合成测试问题", turn_id: "turn-current" },
        connection: { control: "connected", media: "connected" },
        microphone: { localDetected: false, level: 0 }, evidence: { ready },
        serverAudio: { received: false }, captions: { full: [], recent: [] }, endpoint: { active: false },
        calibration: { status: "completed" }, avatar: { status: "idle" }, understandingRetryPending: pending,
        problem: { code: "UNDERSTANDING_RETRY_EXHAUSTED", recoverable: true },
      });
      return () => {};
    });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    const retry = [...host.querySelectorAll("button")].find(button => button.textContent === (pending ? "正在重试…" : "重试处理"));
    expect(retry.disabled).toBe(pending || !ready);
    expect(host.textContent).toContain("处理已有回答");
    if (!retry.disabled) {
      await act(async () => retry.click());
      expect(run.act).toHaveBeenCalledWith(expect.objectContaining({ type: "retry_answer_processing", turnId: "turn-current" }));
    }
  });

  it.each([["recovering", "connected"], ["resyncing", "connected"], ["connected", "reconnecting"]])("shows current-interview recovery for control=%s media=%s without reopening answer actions", async (control, media) => {
    const run = makeRun();
    let update;
    const state = { phase: "listening", floor: "candidate", session: { status: "in_progress" },
      currentQuestion: { question_text: "Celery 合成测试问题", turn_id: "turn-current", order: 3 },
      connection: { control, media }, microphone: { localDetected: true, level: .5 }, evidence: { ready: true },
      serverAudio: { received: true }, captions: { full: [], recent: [{ text: "当前题目合成回答", final: false }] },
      endpoint: { active: false }, calibration: { status: "completed" }, avatar: { status: "idle" },
      problem: { code: "UNDERSTANDING_RESULT_REJECTED", recoverable: true },
    };
    run.subscribe.mockImplementation(listener => { update = listener; listener(state); return () => {}; });
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    expect(host.textContent).toContain("正在恢复本场面试");
    expect(host.textContent).toContain("Celery 合成测试问题");
    expect(host.textContent).toContain("当前题目合成回答");
    expect(host.textContent).not.toMatch(/正在进入面试|正在听你说|麦克风已开启|回答处理需要重试/);
    expect(host.querySelectorAll(".candidate-question-band .candidate-waiting-row")).toHaveLength(1);
    const controls = [...host.querySelectorAll(".candidate-answer-actions button")];
    expect(controls.every(button => button.disabled)).toBe(true);
    await act(async () => controls.forEach(button => button.click()));
    expect(run.act).not.toHaveBeenCalled();
    await act(async () => update({ ...state, connection: { control: "connected", media: "connected" } }));
    expect(host.textContent).toContain("回答处理需要重试");
    expect(host.textContent).not.toContain("正在恢复本场面试");
    expect(host.textContent).not.toContain("正在听你说");
    expect(run.act).not.toHaveBeenCalled();
  });

  it("clears a local action send failure after authoritative reconnection without hiding business failure", async () => {
    const run = makeRun();
    let update;
    const state = { phase: "listening", floor: "candidate", session: { status: "in_progress" },
      currentQuestion: { question_text: "合成测试问题", turn_id: "turn-current" },
      connection: { control: "connected", media: "connected" }, microphone: { localDetected: false, level: 0 }, evidence: { ready: true },
      serverAudio: { received: false }, captions: { forming: false, full: [], recent: [] }, endpoint: { active: false },
      calibration: { status: "completed" }, avatar: { status: "idle" }, problem: null,
    };
    run.subscribe.mockImplementation(listener => { update = listener; listener(state); return () => {}; });
    run.act.mockRejectedValueOnce(new Error("synthetic control disconnect"));
    runtime.open.mockResolvedValueOnce(run);
    await mount();
    await deliver(0, interview("new"));
    await act(async () => avatar().onReady({ fps: 60 }));
    const repeat = [...host.querySelectorAll("button")].find(button => button.textContent === "请再说一遍");
    await act(async () => repeat.click());
    expect(host.textContent).toContain("暂时没有完成，请稍后再试");
    await act(async () => update({ ...state, connection: { ...state.connection, control: "recovering" } }));
    expect(host.textContent).toContain("正在恢复本场面试");
    await act(async () => update(state));
    expect(host.textContent).not.toContain("暂时没有完成，请稍后再试");
    expect(host.textContent).toContain("等待你开口");
    await act(async () => update({ ...state, problem: { code: "UNDERSTANDING_RESULT_REJECTED", recoverable: true } }));
    expect(host.textContent).toContain("回答处理需要重试");
  });
});
