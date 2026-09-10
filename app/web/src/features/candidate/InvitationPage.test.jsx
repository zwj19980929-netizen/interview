import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CandidateFeaturePage from "./Page.jsx";
import { prepareCandidateMedia, runCandidatePreflight, verifySpeaker } from "./preflight.js";
import { rememberPreflightReport } from "./agent-experience.js";

let workbench;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => workbench }));
vi.mock("./preflight.js", () => ({
  runCandidatePreflight: vi.fn(), prepareCandidateMedia: vi.fn(), verifySpeaker: vi.fn(),
  peekPreparedCandidateMedia: vi.fn(), recordPreparedAvatarFps: vi.fn(),
}));
vi.mock("./agent-experience.js", () => ({
  rememberPreflightReport: vi.fn(), createCandidateInterviewExperience: vi.fn(), reportCandidateRuntimeProblem: vi.fn(),
}));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const REPORT = {
  microphone_granted: true, camera_granted: true, webrtc_supported: true,
  audio_worklet_supported: true, webgl_supported: true, avatar_fps: 60,
  network_rtt_ms: 10, network_jitter_ms: 2,
};
let host, root;
const button = (text) => [...host.querySelectorAll("button")].find((node) => node.textContent === text);
const enterButton = () => button("我听到了测试音，进入面试");
const prepare = async () => {
  await act(async () => root.render(<CandidateFeaturePage />));
  await act(async () => button("检查设备并进入面试").click());
  await act(async () => button("播放扬声器测试音").click());
};

beforeEach(() => {
  vi.clearAllMocks();
  runCandidatePreflight.mockResolvedValue({ stream: { synthetic: true }, report: REPORT });
  verifySpeaker.mockResolvedValue(undefined);
  workbench = {
    API: "/api/v1", route: { view: "invite", invitationToken: "synthetic_token" },
    data: { invitation: {
      status: "registered", position_name: "合成测试岗位",
      scheduled_start_at: "2000-01-01T00:00:00Z", scheduled_end_at: "2099-01-01T00:00:00Z",
      consent: { required_scopes: ["audio_recording"] },
    } },
    request: vi.fn(), toast: vi.fn(),
  };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("candidate model readiness and start wait", () => {
  it("keeps a single entry attempt pending across separate bounded readiness and start requests", async () => {
    const readiness = deferred(), start = deferred();
    workbench.request.mockImplementation((path) => path.endsWith("/readiness") ? readiness.promise : start.promise);
    await prepare();
    const enter = enterButton();
    await act(async () => { enter.click(); enter.click(); });
    expect(workbench.request).toHaveBeenCalledExactlyOnceWith("/api/v1/public/interview-invitations/synthetic_token/readiness", {
      method: "POST", body: { ...REPORT, speaker_verified: true }, timeoutMs: 40000,
    });
    expect(prepareCandidateMedia).toHaveBeenCalledTimes(1);
    expect(rememberPreflightReport).toHaveBeenCalledTimes(1);
    expect(enter.disabled).toBe(true);
    expect(enter.textContent).toBe("正在准备面试…");
    expect(host.querySelector('[role="status"]').textContent).toContain("正在准备，请稍等片刻");
    await act(async () => readiness.resolve({ can_start: true }));
    expect(workbench.request).toHaveBeenLastCalledWith("/api/v1/public/interview-invitations/synthetic_token/start", {
      method: "POST", timeoutMs: 40000,
    });
    expect(enter.disabled).toBe(true);
    expect(enter.textContent).toBe("正在进入面试…");
    await act(async () => start.resolve({ candidate_join_url: "#candidate/synthetic_only" }));
    expect(location.hash).toBe("#candidate/synthetic_only");
    expect(workbench.request).toHaveBeenCalledTimes(2);
    expect(workbench.toast).not.toHaveBeenCalled();
  });

  it("does not start when readiness fails and allows an explicit retry without repeating local media checks", async () => {
    workbench.request.mockResolvedValue({ can_start: false });
    await prepare();
    await act(async () => enterButton().click());
    expect(workbench.request).toHaveBeenCalledTimes(1);
    expect(workbench.request.mock.calls[0][0]).toMatch(/\/readiness$/);
    expect(enterButton().disabled).toBe(false);
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(workbench.toast).toHaveBeenCalledWith("暂时无法进入面试", expect.stringContaining("检查网络或重新打开邀请链接"), "error");
    await act(async () => enterButton().click());
    expect(workbench.request).toHaveBeenCalledTimes(2);
    expect(runCandidatePreflight).toHaveBeenCalledTimes(1);
    expect(verifySpeaker).toHaveBeenCalledTimes(1);
  });

  it.each(["readiness", "start"])("unlocks entry after a failed %s request", async (phase) => {
    workbench.request.mockImplementation(async (path) => {
      if (path.endsWith(`/${phase}`)) throw new Error("Synthetic request timeout");
      return { can_start: true };
    });
    await prepare();
    await act(async () => enterButton().click());
    expect(enterButton().disabled).toBe(false);
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(workbench.toast).toHaveBeenCalledWith("暂时无法进入面试", "暂时无法进入面试，请检查网络或重新打开邀请链接。", "error");
    for (const [, options] of workbench.request.mock.calls) expect(options.timeoutMs).toBe(40000);
  });
});
