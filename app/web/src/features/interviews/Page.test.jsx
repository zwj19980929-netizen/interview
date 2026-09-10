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

describe("interview list removal", () => {
  it("removes only terminal interviews after confirmation and preserves evidence messaging", async () => {
    const cancelled = {
      id: "iv_cancelled",
      status: "cancelled",
      termination_reason: "appointment_window_expired",
      version: 3,
      candidate: { name: "已取消候选人" },
      created_at: "2026-09-09T18:20:00Z",
    };
    workbench.data.interviews = [
      cancelled,
      {
        id: "iv_live",
        status: "in_progress",
        version: 5,
        candidate: { name: "面试中候选人" },
        created_at: "2026-09-09T18:22:00Z",
      },
    ];
    workbench.request.mockImplementation(async (path, options = {}) => {
      if (path === "/api/v1/interviews/iv_cancelled" && !options.method) return cancelled;
      if (path === "/api/v1/interviews/iv_cancelled?expected_version=3" && options.method === "DELETE") {
        return { ...cancelled, version: 4, list_removed_at: "2026-09-09T19:00:00Z" };
      }
      throw new Error(`Unexpected synthetic request: ${path}`);
    });

    await act(async () => show());
    expect(host.querySelector(".interview-list-head").textContent).toContain("候选人创建时间状态操作");
    const rows = [...host.querySelectorAll(".interview-row")];
    expect(rows).toHaveLength(2);
    expect(rows[0].querySelector(".candidate-avatar").textContent).toBe("已");
    expect(rows[0].textContent).toContain("已超时结束");
    expect(rows[0].querySelector('[aria-label="已取消候选人的更多操作"]')).not.toBeNull();
    expect(rows[1].querySelector('[aria-label="面试中候选人的更多操作"]')).toBeNull();
    expect(rows[1].querySelector(".interview-action-state").textContent).toBe("正在面试中");
    expect(rows.every((row) => row.querySelector(".interview-secondary-action"))).toBe(true);
    expect(rows[0].textContent).toContain("查看详情");
    const removeButtons = [...host.querySelectorAll("button")].filter((node) => node.textContent === "移出列表");
    expect(removeButtons).toHaveLength(1);

    await act(async () => removeButtons[0].click());
    expect(modal.title).toBe("移除 已取消候选人");
    expect(host.textContent).toContain("不会删除候选人资料、回答、报告、录音或审计记录");
    await act(async () => submit());

    expect(workbench.request).toHaveBeenNthCalledWith(1, "/api/v1/interviews/iv_cancelled");
    expect(workbench.request).toHaveBeenNthCalledWith(
      2,
      "/api/v1/interviews/iv_cancelled?expected_version=3",
      { method: "DELETE" },
    );
    expect(workbench.refresh).toHaveBeenCalledWith("interviews");
    expect(workbench.closeModal).toHaveBeenCalled();
    expect(workbench.toast).toHaveBeenCalledWith(
      "已从面试列表移除",
      "候选人资料、回答、报告、录音和审计记录仍然保留",
    );
  });
});

describe("interview list search and filtering", () => {
  const interviews = [
    {
      id: "iv_ready",
      status: "report_ready",
      candidate: { name: "张文君" },
      created_at: "2026-09-09T18:22:00Z",
    },
    {
      id: "iv_live",
      status: "in_progress",
      candidate: { name: "李雷" },
      created_at: "2026-09-09T18:20:00Z",
    },
    {
      id: "iv_scoring",
      status: "in_progress",
      candidate_input_completed_at: "2026-09-09T18:30:00Z",
      candidate: { name: "李娜" },
      created_at: "2026-09-09T18:10:00Z",
    },
    {
      id: "iv_expired",
      status: "cancelled",
      termination_reason: "appointment_window_expired",
      candidate: { name: "王芳" },
      created_at: "2026-09-08T18:00:00Z",
    },
  ];

  it("searches candidate names immediately and clears all active filters", async () => {
    workbench.data.interviews = interviews;
    await act(async () => show());

    const search = host.querySelector('[aria-label="搜索候选人姓名"]');
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(search, " 李 ");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect([...host.querySelectorAll(".interview-row")]).toHaveLength(2);
    expect(host.textContent).toContain("显示 2 / 共 4 场面试");
    expect(host.textContent).toContain("李雷");
    expect(host.textContent).toContain("李娜");
    expect(host.textContent).not.toContain("张文君");

    await act(async () => button("清除筛选").click());
    expect(search.value).toBe("");
    expect(host.querySelector('[aria-label="按面试状态筛选"]').value).toBe("all");
    expect([...host.querySelectorAll(".interview-row")]).toHaveLength(4);
  });

  it("filters by effective display status and shows a useful empty state", async () => {
    workbench.data.interviews = interviews;
    await act(async () => show());
    const status = host.querySelector('[aria-label="按面试状态筛选"]');

    await act(async () => {
      status.value = "expired";
      status.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect([...host.querySelectorAll(".interview-row")]).toHaveLength(1);
    expect(host.querySelector(".interview-row").textContent).toContain("王芳");
    expect(host.querySelector(".interview-row").textContent).toContain("已超时结束");

    await act(async () => {
      status.value = "processing";
      status.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect([...host.querySelectorAll(".interview-row")]).toHaveLength(1);
    expect(host.querySelector(".interview-row").textContent).toContain("李娜");
    expect(host.querySelector(".interview-row").textContent).toContain("已提交，后台评分中");

    const search = host.querySelector('[aria-label="搜索候选人姓名"]');
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(search, "不存在");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(host.querySelectorAll(".interview-row")).toHaveLength(0);
    expect(host.textContent).toContain("没有匹配的面试");
    expect(host.textContent).toContain("0 条结果");
  });
});
