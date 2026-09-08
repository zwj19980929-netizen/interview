import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import InterviewsPage from "./Page.jsx";

let workbench;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => workbench }));
const APPOINTMENT = { id: "appointment_synthetic", scheduled_end_at: "2099-01-01T02:00:00Z" };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
let host, root, modal;
const show = () => root.render(<><InterviewsPage />{modal?.body}</>);
const button = (text) => [...host.querySelectorAll("button")].find((node) => node.textContent === text);
const submit = () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
const prepareForm = async () => {
  await act(async () => show());
  await act(async () => button("创建预约").click());
  host.querySelector('[name="scheduled_start_at"]').value = "2099-01-01T01:00";
  host.querySelector('[name="scheduled_end_at"]').value = "2099-01-01T02:00";
};

beforeEach(() => {
  modal = null;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  workbench = {
    API: "/api/v1", route: { view: "interviews" }, auth: { roles: ["admin"] },
    data: {
      interviews: [], candidates: [{ id: "synthetic_candidate", name: "合成测试候选人" }],
      plans: [{ id: "synthetic_plan", status: "approved", candidate_profile_id: "synthetic_candidate", job_position_id: "synthetic_position" }],
    },
    navigate: vi.fn(), refresh: vi.fn().mockResolvedValue(undefined), toast: vi.fn(),
    request: vi.fn(),
    openModal: vi.fn((value) => { modal = value; show(); }),
    closeModal: vi.fn(() => { modal = null; show(); }),
  };
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("appointment invitation readiness wait", () => {
  it("prevents rapid duplicate creation, then retries the saved appointment with a bounded model-check wait", async () => {
    const creation = deferred(), invitation = deferred(), retry = deferred();
    workbench.request.mockImplementation((path) => {
      if (path === "/api/v1/interview-appointments") return creation.promise;
      if (path.endsWith("/invite")) return invitation.promise;
      throw new Error("Unexpected synthetic request");
    });
    await prepareForm();
    await act(async () => { submit(); submit(); });
    expect(workbench.request).toHaveBeenCalledTimes(1);
    expect(host.querySelector('[role="status"]').textContent).toContain("正在保存预约");
    // Appointment creation keeps the ordinary request budget; only admission probes wait longer.
    expect(workbench.request.mock.calls[0][1]).not.toHaveProperty("timeoutMs");
    await act(async () => creation.resolve(APPOINTMENT));
    expect(workbench.request).toHaveBeenLastCalledWith("/api/v1/interview-appointments/appointment_synthetic/invite", {
      method: "POST", body: { expires_at: APPOINTMENT.scheduled_end_at }, timeoutMs: 40000,
    });
    expect(host.querySelector('[role="status"]').textContent).toContain("正在检查模型服务");
    expect(host.querySelector('[type="submit"]').disabled).toBe(true);
    await act(async () => invitation.reject(new Error("Readiness not available")));
    expect(modal.title).toBe("预约已保留，邀请待重试");
    expect(host.textContent).toContain("预约已经安全保存，不会重复创建");
    workbench.request.mockImplementation((path) => {
      expect(path).toBe("/api/v1/interview-appointments/appointment_synthetic/invite");
      return retry.promise;
    });
    const retryButton = button("重试签发邀请");
    await act(async () => { retryButton.click(); retryButton.click(); });
    expect(workbench.request).toHaveBeenCalledTimes(3);
    expect(retryButton.disabled).toBe(true);
    expect(retryButton.textContent).toBe("正在检查模型服务…");
    expect(host.querySelector('[role="status"]').textContent).toContain("约 30 秒");
    expect(workbench.request.mock.calls[2][1].timeoutMs).toBe(40000);
    await act(async () => retry.resolve({ join_url: "/#invite/synthetic_only" }));
    expect(modal.title).toBe("候选人邀请");
    expect(workbench.request.mock.calls.filter(([path]) => path === "/api/v1/interview-appointments")).toHaveLength(1);
  });

  it("retains the successful invitation if a later list refresh fails", async () => {
    workbench.request.mockImplementation(async (path) => path.endsWith("/invite")
      ? { join_url: "/#invite/synthetic_saved" } : APPOINTMENT);
    workbench.refresh.mockRejectedValueOnce(new Error("Synthetic refresh failure"));
    await prepareForm();
    await act(async () => submit());
    expect(workbench.request).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("预约已保存，重试不会重复创建预约");
    expect(host.querySelector('[type="submit"]').disabled).toBe(false);
    await act(async () => submit());
    expect(workbench.request).toHaveBeenCalledTimes(2);
    expect(workbench.refresh).toHaveBeenCalledTimes(2);
    expect(modal.title).toBe("候选人邀请");
    expect(host.querySelector('[aria-label="候选人邀请链接"]').value).toContain("synthetic_saved");
  });

  it("retries only the saved appointment if both invitation and subsequent refresh failed", async () => {
    let invites = 0;
    workbench.request.mockImplementation(async (path) => {
      if (!path.endsWith("/invite")) return APPOINTMENT;
      if (++invites === 1) throw new Error("Synthetic admission failure");
      return { join_url: "/#invite/synthetic_retry" };
    });
    workbench.refresh.mockRejectedValueOnce(new Error("Synthetic refresh failure"));
    await prepareForm();
    await act(async () => submit());
    expect(host.textContent).toContain("预约已保存，重试不会重复创建预约");
    await act(async () => submit());
    expect(workbench.request.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/interview-appointments",
      "/api/v1/interview-appointments/appointment_synthetic/invite",
      "/api/v1/interview-appointments/appointment_synthetic/invite",
    ]);
    expect(modal.title).toBe("候选人邀请");
  });

  it("re-enables retry after a failed readiness check without creating a new appointment", async () => {
    workbench.request.mockImplementation(async (path) => {
      if (path.endsWith("/invite")) throw new Error("Synthetic readiness unavailable");
      return APPOINTMENT;
    });
    await prepareForm();
    await act(async () => submit());
    await act(async () => button("重试签发邀请").click());
    expect(button("重试签发邀请").disabled).toBe(false);
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(workbench.request.mock.calls.filter(([path]) => path === "/api/v1/interview-appointments")).toHaveLength(1);
    expect(workbench.toast).toHaveBeenCalledWith("邀请仍未就绪", expect.any(String), "error");
  });
});
