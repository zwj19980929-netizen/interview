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
    expect(workbench.toast).toHaveBeenCalledWith("邀请签发失败", expect.any(String), "error");
  });
});

describe("saved appointment invitation diagnostics", () => {
  const failInvite = async (fields) => {
    workbench.request.mockImplementation(async (path) => {
      if (path.endsWith("/invite")) throw Object.assign(new Error("private-provider-response secret-token"), fields);
      return APPOINTMENT;
    });
    await prepareForm();
    await act(async () => submit());
    return host.querySelector('[aria-label="邀请签发失败原因"]');
  };

  it("explains an insufficient frozen assessment window as a scheduling issue", async () => {
    const failure = await failInvite({ code: "APPOINTMENT_NOT_READY", details: { checks: [
      { name: "assessment_time_budget", ready: false, invite_ready: false, required_seconds: 1830, available_seconds: 300 },
    ] } });
    expect(failure.textContent).toContain("本场考察所需时间");
    expect(failure.textContent).toContain("请延长预约或重新准备更短的计划");
    expect(failure.textContent).not.toMatch(/其他服务检查|检查配置|assessment_time_budget|稍后|等待/);
  });

  it.each(["development", "production"])("lists only actual invitation blockers in %s without exposing diagnostic payloads", async (environment) => {
    const failure = await failInvite({ code: "APPOINTMENT_NOT_READY", details: {
      runtime_environment: environment,
      private_payload: "secret-token",
      checks: [
        { name: "agent_expression_tts", ready: false, invite_ready: true },
        { name: "experience_question_speech", blocking: false, ready: false, invite_ready: false },
        { name: "livekit_media_plane", ready: true, invite_ready: false },
        { name: "stt_streaming", ready: true, production_ready: false },
        { name: "warmup_stt_streaming", ready: false, production_ready: true },
        { name: "turn_understanding", ready: false, route_readiness: { status: "failed", reason_code: "provider_timeout" } },
        { name: "controlled_followup", ready: true },
        { name: "question_speech", ready: false },
        { name: "secret-token", ready: false, route_readiness: { status: "constructor", reason_code: "__proto__", error: "secret-token" } },
      ],
    } });
    const rows = [...failure.querySelectorAll("li")].map((row) => row.textContent);
    expect(rows).toHaveLength(5);
    expect(failure.textContent).toContain("实时音视频服务（LiveKit）");
    expect(failure.textContent).toContain("对话理解服务：供应商响应超时");
    expect(failure.textContent).toContain("题目语音资源");
    expect(failure.textContent).toContain("其他服务检查");
    expect(failure.textContent).toContain(environment === "production" ? "实时语音识别服务" : "入场语音校准服务");
    expect(failure.textContent).not.toContain(environment === "production" ? "入场语音校准服务" : "实时语音识别服务");
    expect(failure.textContent).not.toContain("语音合成服务");
    expect(failure.textContent).not.toContain("经历题语音资源");
    expect(failure.textContent).not.toContain("追问生成服务");
    expect(host.textContent).not.toMatch(/secret-token|private-provider|__proto__|readiness/);
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toMatch(/secret-token|private-provider/);
    expect(workbench.request).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["provider_timeout", "供应商响应超时", true],
    ["provider_rate_limited", "供应商请求受限", true],
    ["provider_billing_required", "供应商账户计费或额度受限", false],
    ["provider_auth_failed", "供应商凭据验证失败", false],
    ["provider_cost_limit_exceeded", "应用配置的费用上限", false],
    ["provider_audio_final_missing", "供应商未返回完整语音", false],
    ["provider_audio_format_unsupported", "音频格式不受支持", false],
    ["ROUTE_DISABLED", "模型调用配置已停用", false],
  ])("explains %s without conflating billing, configuration and cooldown", async (reason, message, cooldown) => {
    const failure = await failInvite({ code: "APPOINTMENT_NOT_READY", details: { checks: [
      { name: "agent_expression_tts", ready: false, route_readiness: {
        status: reason === "ROUTE_DISABLED" ? "configuration_invalid" : "failed",
        reason_code: reason, can_refresh: false, retry_at: "2099-01-01T00:00:00Z",
      } },
    ] } });
    expect(failure.textContent).toContain("面试官语音合成服务（TTS）");
    expect(failure.textContent).toContain(message);
    expect(failure.textContent.includes("冷却期")).toBe(cooldown);
    if (reason === "provider_cost_limit_exceeded") expect(failure.textContent).not.toContain("供应商账户计费");
  });

  it.each([
    ["REQUEST_TIMEOUT", 0, "邀请请求超时"],
    ["NETWORK_UNAVAILABLE", 0, "无法连接邀请服务"],
    ["REQUEST_CANCELLED", 0, "邀请请求已取消"],
    ["AUTHENTICATION_INVALID", 401, "登录状态已失效"],
    ["AUTHORIZATION_FORBIDDEN", 403, "没有签发邀请的权限"],
    ["INVITATION_EXPIRY_INVALID", 400, "邀请有效期已过"],
    ["APPOINTMENT_NOT_INVITABLE", 409, "预约状态不允许签发邀请"],
    ["INTERVIEW_PLAN_NOT_APPROVED", 409, "面试计划未获批准"],
    ["provider_auth_failed", 400, "供应商凭据验证失败"],
    ["private-secret-code", 500, "邀请签发失败"],
  ])("keeps %s visible without reporting it as a readiness failure", async (code, status, message) => {
    const failure = await failInvite({ code, status, details: { raw: "secret-token" } });
    expect(failure.textContent).toContain(message);
    expect(failure.textContent).not.toContain("面试服务检查未通过");
    expect(host.textContent).not.toMatch(/readiness|secret-token|private-provider|private-secret-code/);
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toMatch(/secret-token|private-provider|private-secret-code/);
  });

  it("replaces the visible failure after a manual retry and preserves the saved appointment", async () => {
    await failInvite({ code: "REQUEST_TIMEOUT" });
    workbench.request.mockRejectedValueOnce(Object.assign(new Error("private retry body"), {
      code: "APPOINTMENT_NOT_READY", details: { checks: [
        { name: "agent_expression_tts", ready: false, route_readiness: { status: "failed", reason_code: "provider_billing_required" } },
      ] },
    }));
    await act(async () => button("重试签发邀请").click());
    const failure = host.querySelector('[aria-label="邀请签发失败原因"]');
    expect(failure.textContent).toContain("供应商账户计费或额度受限");
    expect(failure.textContent).not.toContain("邀请请求超时");
    expect(button("重试签发邀请").disabled).toBe(false);
    expect(workbench.request.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/interview-appointments",
      "/api/v1/interview-appointments/appointment_synthetic/invite",
      "/api/v1/interview-appointments/appointment_synthetic/invite",
    ]);
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toContain("private retry body");
  });

  it.each([undefined, { checks: "private-invalid-checks" }, { checks: [null, false, {}] }])(
    "uses safe guidance when readiness details are absent or malformed", async (details) => {
      const failure = await failInvite({ code: "APPOINTMENT_NOT_READY", details });
      expect(failure.textContent).toContain("未收到具体未通过项目");
      expect(failure.querySelectorAll("li")).toHaveLength(0);
      expect(host.textContent).not.toContain("private-invalid-checks");
    },
  );
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
