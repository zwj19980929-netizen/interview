import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CandidateFeaturePage from "./Page.jsx";
import { prepareCandidateMedia, discardPreparedCandidateMedia, runCandidatePreflight, runCandidateNetworkCheck, verifySpeaker } from "./preflight.js";
import { rememberPreflightReport } from "./agent-experience.js";

let workbench;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => workbench }));
vi.mock("./preflight.js", () => ({
  runCandidatePreflight: vi.fn(), runCandidateNetworkCheck: vi.fn(), prepareCandidateMedia: vi.fn(), verifySpeaker: vi.fn(),
  peekPreparedCandidateMedia: vi.fn(), recordPreparedAvatarFps: vi.fn(), discardPreparedCandidateMedia: vi.fn(),
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
  browser_supported: true, media_recorder_supported: true,
  audio_content_type: "audio/webm;codecs=opus", video_content_type: "video/webm;codecs=vp9,opus",
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
  runCandidateNetworkCheck.mockResolvedValue({ rttMs: 20, jitterMs: 3 });
  verifySpeaker.mockResolvedValue(undefined);
  workbench = {
    API: "/api/v1", route: { view: "invite", invitationToken: "synthetic_token" },
    data: { invitation: {
      token: "synthetic_token", appointment_id: "appointment_synthetic",
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
    expect(host.querySelector('[role="alert"]').textContent).toContain("暂时无法完成入场检查");
    expect(host.querySelector('[role="alert"]').textContent).not.toContain("网络");
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
    expect(host.querySelector('[role="alert"]').textContent).toContain("暂时无法完成入场检查");
    expect(workbench.toast).not.toHaveBeenCalled();
    for (const [, options] of workbench.request.mock.calls) expect(options.timeoutMs).toBe(40000);
  });
});

describe("candidate network measurement and recovery", () => {
  const networkRow = () => [...host.querySelectorAll(".preflight-check")].find((node) => node.textContent.includes("服务连接"));
  const recheckButton = () => button("重新检测网络") || button("正在检测网络…");

  it.each([
    ["NETWORK_CHECK_TIMEOUT", "连接检测超时"],
    ["NETWORK_CHECK_FAILED", "未能连接面试服务"],
  ])("identifies an initial %s as a network problem without exposing transport details", async (code, copy) => {
    runCandidatePreflight.mockRejectedValue(Object.assign(new Error("private upstream detail"), { code }));
    await act(async () => root.render(<CandidateFeaturePage />));
    await act(async () => button("检查设备并进入面试").click());
    expect(workbench.toast).toHaveBeenCalledWith("网络检测未通过", expect.stringContaining(copy), "error");
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toContain("private upstream detail");
    expect(button("检查设备并进入面试").disabled).toBe(false);
    expect(workbench.request).not.toHaveBeenCalled();
  });

  it.each([
    [750, 15, "服务响应较慢"],
    [90, 180, "响应波动较大"],
    [600, 150, "服务响应较慢"],
  ])("shows the actual measurements and blocks entry for RTT=%s jitter=%s", async (rtt, jitter, reason) => {
    runCandidatePreflight.mockResolvedValue({ stream: { synthetic: true }, report: { ...REPORT, network_rtt_ms: rtt, network_jitter_ms: jitter } });
    await prepare();
    expect(networkRow().textContent).toContain(`响应 ${rtt} ms · 波动 ${jitter} ms`);
    expect(networkRow().textContent).toContain(reason);
    expect(networkRow().classList.contains("is-ready")).toBe(false);
    expect(enterButton().disabled).toBe(true);
    await act(async () => enterButton().click());
    expect(workbench.request).not.toHaveBeenCalled();
  });

  it("accepts the unchanged threshold boundaries", async () => {
    runCandidatePreflight.mockResolvedValue({ stream: { synthetic: true }, report: { ...REPORT, network_rtt_ms: 500, network_jitter_ms: 100 } });
    await prepare();
    expect(networkRow().textContent).toContain("连接检测通过");
    expect(enterButton().disabled).toBe(false);
  });

  it("rechecks without reacquiring devices or losing speaker confirmation, excludes duplicates and submits fresh measurements", async () => {
    const probe = deferred();
    runCandidateNetworkCheck.mockReturnValue(probe.promise);
    await prepare();
    const retry = recheckButton();
    const enter = enterButton();
    await act(async () => { retry.click(); retry.click(); enter.click(); });
    expect(runCandidateNetworkCheck).toHaveBeenCalledTimes(1);
    expect(runCandidateNetworkCheck).toHaveBeenCalledWith({ probeUrl: "/healthz", signal: expect.any(AbortSignal) });
    expect(enter.disabled).toBe(true);
    expect(retry.disabled).toBe(true);
    expect(networkRow().textContent).toContain("正在检测");
    expect(networkRow().textContent).not.toContain("响应 10 ms");
    expect(prepareCandidateMedia).toHaveBeenLastCalledWith({ synthetic: true }, expect.objectContaining({ network_rtt_ms: null, network_jitter_ms: null }));
    expect(workbench.request).not.toHaveBeenCalled();
    await act(async () => probe.resolve({ rttMs: 45, jitterMs: 8 }));
    expect(enter.disabled).toBe(false);
    expect(button("重新播放测试音")).toBeTruthy();
    expect(runCandidatePreflight).toHaveBeenCalledTimes(1);
    expect(verifySpeaker).toHaveBeenCalledTimes(1);
    expect(prepareCandidateMedia).toHaveBeenLastCalledWith({ synthetic: true }, expect.objectContaining({ network_rtt_ms: 45, network_jitter_ms: 8, speaker_verified: true }));
    workbench.request.mockResolvedValue({ can_start: false });
    await act(async () => enter.click());
    expect(workbench.request.mock.calls[0][1].body).toMatchObject({ network_rtt_ms: 45, network_jitter_ms: 8, speaker_verified: true });
  });

  it.each([
    ["NETWORK_CHECK_TIMEOUT", "连接检测超时"],
    ["NETWORK_CHECK_FAILED", "未能连接面试服务"],
  ])("discards a previously passing result when retry fails with %s", async (code, copy) => {
    await prepare();
    runCandidateNetworkCheck.mockRejectedValue(Object.assign(new Error("private upstream detail"), { code }));
    await act(async () => recheckButton().click());
    expect(networkRow().textContent).toContain(copy);
    expect(host.textContent).not.toContain("private upstream detail");
    expect(networkRow().textContent).not.toContain("响应 10 ms");
    expect(enterButton().disabled).toBe(true);
    expect(recheckButton().disabled).toBe(false);
    await act(async () => enterButton().click());
    expect(workbench.request).not.toHaveBeenCalled();
    runCandidateNetworkCheck.mockResolvedValue({ rttMs: 20, jitterMs: 2 });
    await act(async () => recheckButton().click());
    expect(enterButton().disabled).toBe(false);
  });

  it("keeps entry blocked after a completed but slow retry", async () => {
    await prepare();
    runCandidateNetworkCheck.mockResolvedValue({ rttMs: 650, jitterMs: 120 });
    await act(async () => recheckButton().click());
    expect(enterButton().disabled).toBe(true);
    expect(networkRow().textContent).toContain("服务响应较慢");
    expect(networkRow().textContent).toContain("响应波动较大");
  });

  it("does not start another network check while admission is pending", async () => {
    const readiness = deferred();
    workbench.request.mockReturnValue(readiness.promise);
    await prepare();
    const retry = recheckButton();
    await act(async () => { enterButton().click(); retry.click(); });
    expect(runCandidateNetworkCheck).not.toHaveBeenCalled();
    expect(retry.disabled).toBe(true);
    await act(async () => readiness.resolve({ can_start: false }));
    expect(retry.disabled).toBe(false);
  });

  it.each(["resolve", "reject"])("ignores a late retry %s after an invitation changes", async (settle) => {
    const probe = deferred();
    runCandidateNetworkCheck.mockReturnValue(probe.promise);
    await prepare();
    await act(async () => recheckButton().click());
    const signal = runCandidateNetworkCheck.mock.calls[0][0].signal;
    const preparedCalls = prepareCandidateMedia.mock.calls.length;
    workbench.route = { ...workbench.route, invitationToken: "different_synthetic_token" };
    await act(async () => root.render(<CandidateFeaturePage />));
    expect(signal.aborted).toBe(true);
    await act(async () => {
      if (settle === "resolve") probe.resolve({ rttMs: 12, jitterMs: 1 });
      else probe.reject(new Error("private late response"));
    });
    expect(prepareCandidateMedia).toHaveBeenCalledTimes(preparedCalls);
    expect(host.textContent).toContain("正在读取本次邀请");
    expect(networkRow()).toBeUndefined();
    expect(workbench.request).not.toHaveBeenCalled();
    expect(workbench.toast).not.toHaveBeenCalled();
  });

  it("does not advance a stale admission response after the invitation changes", async () => {
    const readiness = deferred();
    workbench.request.mockReturnValue(readiness.promise);
    await prepare();
    await act(async () => enterButton().click());
    workbench.route = { ...workbench.route, invitationToken: "different_synthetic_token" };
    await act(async () => root.render(<CandidateFeaturePage />));
    await act(async () => readiness.resolve({ can_start: true }));
    expect(workbench.request).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("正在读取本次邀请");
  });
});


describe("invitation admission diagnosis", () => {
  const networkRow = () => [...host.querySelectorAll(".preflight-check")].find((node) => node.textContent.includes("服务连接"));
  it("returns to registration when a healthy connection cannot admit an unregistered appointment", async () => {
    workbench.request.mockResolvedValue({ can_start: false, entry_blocker: { code: "INVITATION_REGISTRATION_REQUIRED" } });
    await prepare();
    expect(networkRow().classList.contains("is-ready")).toBe(true);
    await act(async () => enterButton().click());
    expect(host.querySelector('[role="alert"]').textContent).toContain("请先确认本次预约");
    expect(host.querySelector("form")).toBeTruthy();
    expect(host.textContent).not.toContain("身份核验通过");
    expect(workbench.request).toHaveBeenCalledTimes(1);
    expect(workbench.toast).not.toHaveBeenCalled();
  });
  it("keeps a successful connection distinct from unavailable interview services", async () => {
    workbench.request.mockResolvedValue({ can_start: false, entry_blocker: { code: "APPOINTMENT_NOT_READY" } });
    await prepare();
    await act(async () => enterButton().click());
    expect(networkRow().classList.contains("is-ready")).toBe(true);
    expect(host.querySelector('[role="alert"]').textContent).toContain("面试服务尚未准备好");
    expect(host.querySelector('[role="alert"]').textContent).not.toContain("网络");
    expect(enterButton().disabled).toBe(false);
  });
  it.each(["NETWORK_UNAVAILABLE", "REQUEST_TIMEOUT"])("invalidates the old green check after %s and recovers through a fresh probe", async (code) => {
    workbench.request.mockRejectedValue(Object.assign(new Error("private provider diagnostics"), { code }));
    await prepare();
    await act(async () => enterButton().click());
    expect(networkRow().classList.contains("is-ready")).toBe(false);
    expect(networkRow().textContent).not.toContain("响应 10 ms");
    expect(host.textContent).not.toContain("private provider diagnostics");
    expect(enterButton().disabled).toBe(true);
    expect(rememberPreflightReport).toHaveBeenLastCalledWith(expect.objectContaining({ network_rtt_ms: null, network_jitter_ms: null }));
    await act(async () => button("重新检测网络").click());
    expect(networkRow().classList.contains("is-ready")).toBe(true);
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(enterButton().disabled).toBe(false);
  });
  it.each([{ avatar_fps: 29.8 }, { media_recorder_supported: false }, { audio_content_type: "" }])("does not paint unsupported devices as ready for %j", async (change) => {
    runCandidatePreflight.mockResolvedValue({ stream: { synthetic: true }, report: { ...REPORT, ...change } });
    await prepare();
    expect(enterButton().disabled).toBe(true);
    expect(host.querySelectorAll(".preflight-check.is-failed").length).toBeGreaterThan(0);
    expect(workbench.request).not.toHaveBeenCalled();
  });
});

describe("invitation identity state isolation", () => {
  it("never uses an old registered projection while a new invited projection loads", async () => {
    await act(async () => root.render(<CandidateFeaturePage />));
    expect(host.textContent).toContain("身份核验通过");
    workbench.route = { ...workbench.route, invitationToken: "token_new" };
    await act(async () => root.render(<CandidateFeaturePage />));
    expect(host.textContent).toContain("正在读取本次邀请");
    expect(host.textContent).not.toContain("身份核验通过");
    workbench.data.invitation = { ...workbench.data.invitation, token: "token_new", status: "invited", appointment_id: "appointment_new" };
    await act(async () => root.render(<CandidateFeaturePage />));
    expect(host.querySelector("form")).toBeTruthy();
    expect(host.textContent).not.toContain("预约已确认");
    expect(runCandidatePreflight).not.toHaveBeenCalled();
  });
  it.each([{}, { matched: false, status: "registered", appointment_id: "appointment_synthetic" },
    { matched: true, status: "invited", appointment_id: "appointment_synthetic" },
    { matched: true, status: "registered", appointment_id: "wrong_appointment" }])("rejects an unverified registration receipt %j", async (receipt) => {
    workbench.data.invitation.status = "invited";
    workbench.request.mockResolvedValue(receipt);
    await act(async () => root.render(<CandidateFeaturePage />));
    await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(host.querySelector("form")).toBeTruthy();
    expect(host.textContent).not.toContain("预约已确认");
    expect(workbench.toast).toHaveBeenCalledWith("暂时无法确认预约", expect.any(String), "error");
  });
  it.each(["resolve", "reject"])("ignores late registration %s after changing invitations", async (settle) => {
    const receipt = deferred();
    workbench.data.invitation.status = "invited";
    workbench.request.mockReturnValue(receipt.promise);
    await act(async () => root.render(<CandidateFeaturePage />));
    await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    workbench.route = { ...workbench.route, invitationToken: "token_new" };
    workbench.data.invitation = { ...workbench.data.invitation, token: "token_new", appointment_id: "appointment_new" };
    await act(async () => root.render(<CandidateFeaturePage />));
    await act(async () => {
      if (settle === "resolve") receipt.resolve({ matched: true, status: "registered", appointment_id: "appointment_synthetic" });
      else receipt.reject(new Error("late failure"));
    });
    expect(host.querySelector("form")).toBeTruthy();
    expect(host.textContent).not.toContain("预约已确认");
    expect(workbench.toast).not.toHaveBeenCalled();
  });
});
