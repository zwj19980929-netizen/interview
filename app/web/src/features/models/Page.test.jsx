import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ModelsPage from "./Page.jsx";

let workbench;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => workbench }));

const PURPOSES = [
  ["warmup_calibration", "试音校准", "stt.streaming"],
  ["interview_turn_understanding", "面试轮次理解", "llm.chat_json"],
  ["controlled_followup", "受控追问", "llm.chat_json"],
  ["interview_agent_expression", "面试官语音表达", "tts.synthesize"],
  ["candidate_followup_dialogue", "实时语音追问", "speech.dialogue_realtime"],
];
const makeRoute = (purpose, capability, status = "untested", overrides = {}) => ({
  id: `route_${purpose}`, purpose, capability, enabled: true,
  primary: { model_configuration_id: "model_one" },
  readiness: { status, reason_code: null, checked_at: null, expires_at: null, retry_at: null },
  ...overrides,
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
let host, root;
const row = (purpose) => host.querySelector(`[data-testid="route-purpose-${purpose}"]`);
const testButton = (purpose) => host.querySelector(`[data-testid="route-test-route_${purpose}"]`);
const render = () => act(async () => root.render(<ModelsPage />));

beforeEach(() => {
  workbench = {
    API: "/api/v1", route: {},
    data: {
      routes: [], catalog: [], providerConnections: [],
      modelConfigurations: [{
        id: "model_one", enabled: true, status: "ready", display_name: "合成测试模型",
        provider_model_id: "synthetic", supported_capabilities: [...new Set(PURPOSES.map((item) => item[2]))],
      }],
    },
    navigate: vi.fn(), openModal: vi.fn(), closeModal: vi.fn(),
    request: vi.fn().mockResolvedValue({}), refresh: vi.fn().mockResolvedValue(undefined), toast: vi.fn(),
  };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});

describe("model route purpose coverage", () => {
  it.each(PURPOSES)("tests the exact configured route for %s", async (purpose, label, capability) => {
    workbench.data.routes = [makeRoute(purpose, capability)];
    await render();
    expect(row(purpose).textContent).toContain(label);
    expect(row(purpose).textContent.includes("可选")).toBe(purpose === "candidate_followup_dialogue");
    await act(async () => testButton(purpose).click());
    expect(workbench.request).toHaveBeenCalledExactlyOnceWith(`/api/v1/admin/model-routes/route_${purpose}/test`, { method: "POST", body: {}, timeoutMs: 35000 });
    expect(workbench.refresh).toHaveBeenCalledExactlyOnceWith("routes");
  });

  it.each(PURPOSES)("configures the correct purpose and capability for %s", async (purpose, label, capability) => {
    await render();
    await act(async () => row(purpose).querySelector("button").click());
    const modal = workbench.openModal.mock.calls[0][0];
    await act(async () => root.render(modal.body));
    expect(host.querySelector('select[name="purpose"]').value).toBe(purpose);
    expect(host.textContent).toContain(label);
    await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(workbench.request).toHaveBeenCalledExactlyOnceWith("/api/v1/admin/model-routes", {
      method: "POST",
      body: {
        capability, purpose, primary: { model_configuration_id: "model_one", timeout_s: 30 },
        fallbacks: [], policy: { retry_count: 1 }, enabled: true,
      },
    });
  });
});

describe("server-authoritative route readiness", () => {
  it("distinguishes every projected status, configured coverage and healthy coverage", async () => {
    const states = [
      ["warmup_calibration", "stt.streaming", "healthy", "健康有效"],
      ["interview_turn_understanding", "llm.chat_json", "expired", "已过期，需重测"],
      ["controlled_followup", "llm.chat_json", "untested", "未测试"],
      ["interview_agent_expression", "tts.synthesize", "checking", "检测中"],
      ["candidate_answer_transcription", "stt.streaming", "failed", "检测失败"],
      ["candidate_answer_repair", "stt.batch", "configuration_invalid", "配置不可用"],
    ];
    workbench.data.routes = states.map(([purpose, capability, status]) => makeRoute(purpose, capability, status));
    await render();
    for (const [purpose, , , label] of states) {
      expect(row(purpose).querySelector(".status-badge").textContent).toBe(label);
    }
    const summary = host.querySelector('[aria-label="模型服务摘要"]');
    expect(summary.textContent).toContain("6/11核心用途已配置");
    expect(summary.textContent).toContain("1 项健康有效");
    expect(host.querySelectorAll(".route-coverage-item.is-configured")).toHaveLength(1);
    expect(testButton("interview_agent_expression").disabled).toBe(true);
    await act(async () => testButton("interview_agent_expression").click());
    expect(workbench.request).not.toHaveBeenCalled();
  });

  it("never recomputes TTL or trusts legacy model and route health instead of readiness", async () => {
    workbench.data.routes = [
      makeRoute("warmup_calibration", "stt.streaming", "healthy", {
        readiness: { status: "healthy", checked_at: "2000-01-01T00:00:00Z", expires_at: "2000-01-02T00:00:00Z" },
      }),
      makeRoute("controlled_followup", "llm.chat_json", "expired", {
        readiness: { status: "expired", expires_at: "2099-01-02T00:00:00Z", retry_at: "2099-01-03T00:00:00Z" },
      }),
      makeRoute("interview_agent_expression", "tts.synthesize", "healthy", {
        readiness: undefined, health: { status: "healthy" },
      }),
      makeRoute("interview_turn_understanding", "llm.chat_json", "future_status", {
        readiness: { status: "future_status", checked_at: "not-a-date" },
      }),
    ];
    await render();
    expect(row("warmup_calibration").querySelector(".status-badge").textContent).toBe("健康有效");
    expect(row("warmup_calibration").querySelectorAll("time")).toHaveLength(2);
    expect(row("controlled_followup").querySelector(".status-badge").textContent).toBe("已过期，需重测");
    expect(row("controlled_followup").textContent).toContain("服务端重试时间");
    expect(row("interview_agent_expression").textContent).toContain("状态未知，待检测");
    expect(row("interview_turn_understanding").textContent).toContain("状态未知，待检测");
    expect(host.textContent).not.toContain("Invalid Date");
    expect(host.textContent).not.toContain("not-a-date");
    expect(workbench.request).not.toHaveBeenCalled();
  });

  it("keeps disabled routes visible but excludes them from enabled coverage and testing", async () => {
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "configuration_invalid", { enabled: false })];
    await render();
    expect(row("warmup_calibration").textContent).toContain("路由已停用");
    expect(host.querySelector('[aria-label="模型服务摘要"]').textContent).toContain("0/11核心用途已配置");
    expect(testButton("warmup_calibration").disabled).toBe(true);
    await act(async () => testButton("warmup_calibration").click());
    expect(workbench.request).not.toHaveBeenCalled();
  });

  it("explains safe reason codes without exposing unknown strings or confusing changed configuration with expiry", async () => {
    workbench.data.routes = [
      makeRoute("warmup_calibration", "stt.streaming", "failed", {
        readiness: { status: "failed", reason_code: "provider_auth_failed" },
      }),
      makeRoute("controlled_followup", "llm.chat_json", "expired", {
        readiness: { status: "expired", reason_code: "ROUTE_CONFIGURATION_CHANGED" },
      }),
      makeRoute("interview_agent_expression", "tts.synthesize", "failed", {
        readiness: { status: "failed", reason_code: "private-error-and-credential" },
      }),
      makeRoute("interview_turn_understanding", "llm.chat_json", "failed", {
        readiness: { status: "constructor", reason_code: "__proto__" },
      }),
      makeRoute("candidate_answer_transcription", "stt.streaming", "failed", {
        readiness: { status: "failed", reason_code: "provider_probe_cleanup_failed" },
      }),
    ];
    await render();
    expect(row("warmup_calibration").textContent).toContain("厂商认证失败，请检查授权配置");
    expect(row("warmup_calibration").querySelector("code").textContent).toBe("provider_auth_failed");
    expect(row("controlled_followup").textContent).toContain("配置已变更，旧健康证据不再适用");
    expect(row("controlled_followup").textContent).not.toContain("上次健康证据已过期");
    expect(row("interview_turn_understanding").textContent).toContain("状态未知，待检测");
    expect(row("candidate_answer_transcription").textContent).toContain("连接已建立，但测试连接未能正常回收");
    expect(host.textContent).not.toContain("private-error");
    expect(host.textContent).not.toContain("__proto__");
  });
});

describe("manual route tests", () => {
  it("deduplicates rapid clicks and stays pending until the authoritative list refresh finishes", async () => {
    const probe = deferred();
    const refresh = deferred();
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "expired")];
    workbench.request.mockReturnValue(probe.promise);
    workbench.refresh.mockImplementation(async () => {
      await refresh.promise;
      workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "healthy")];
    });
    await render();
    const button = testButton("warmup_calibration");
    await act(async () => { button.click(); button.click(); });
    expect(workbench.request).toHaveBeenCalledTimes(1);
    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe("检测中…");
    expect(row("warmup_calibration").querySelector('[role="status"]').textContent).toContain("正在检测路由");
    await act(async () => probe.resolve({ provider: { model: "must-not-render-provider-payload" } }));
    expect(workbench.refresh).toHaveBeenCalledExactlyOnceWith("routes");
    expect(button.disabled).toBe(true);
    expect(workbench.toast).not.toHaveBeenCalled();
    await act(async () => refresh.resolve());
    expect(button.disabled).toBe(false);
    expect(row("warmup_calibration").querySelector(".status-badge").textContent).toBe("健康有效");
    expect(row("warmup_calibration").querySelector('[role="status"]').textContent).toContain("检测成功");
    expect(workbench.toast).toHaveBeenCalledWith("路由检测成功", expect.any(String), "success");
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toContain("must-not-render-provider-payload");
    expect(host.textContent).not.toContain("must-not-render-provider-payload");
  });

  it("refreshes after failure, renders only safe copy, and allows an explicit retry", async () => {
    const probe = deferred();
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "expired")];
    workbench.request.mockReturnValueOnce(probe.promise);
    workbench.refresh.mockImplementation(async () => {
      workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "failed")];
    });
    await render();
    await act(async () => testButton("warmup_calibration").click());
    await act(async () => probe.reject(new Error("provider-secret-and-private-url")));
    expect(workbench.refresh).toHaveBeenCalledExactlyOnceWith("routes");
    expect(row("warmup_calibration").querySelector('[role="alert"]').textContent).toContain("路由检测未通过");
    expect(row("warmup_calibration").querySelector(".status-badge").textContent).toBe("检测失败");
    expect(testButton("warmup_calibration").disabled).toBe(false);
    expect(host.textContent).not.toContain("provider-secret");
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toContain("provider-secret");
    await act(async () => testButton("warmup_calibration").click());
    expect(workbench.request).toHaveBeenCalledTimes(2);
  });

  it("shows a safe timeout without treating the request timeout as a server health verdict", async () => {
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "checking")];
    await render();
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "expired")];
    workbench.request.mockRejectedValue(Object.assign(new Error("private upstream response"), { code: "REQUEST_TIMEOUT" }));
    await render();
    await act(async () => testButton("warmup_calibration").click());
    expect(row("warmup_calibration").querySelector('[role="alert"]').textContent).toContain("检测请求超时");
    expect(row("warmup_calibration").querySelector(".status-badge").textContent).toBe("已过期，需重测");
    expect(workbench.refresh).toHaveBeenCalledExactlyOnceWith("routes");
  });

  it("reports list refresh failure separately and never leaves the action stuck pending", async () => {
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "untested")];
    workbench.refresh.mockRejectedValue(new Error("private refresh detail"));
    await render();
    await act(async () => testButton("warmup_calibration").click());
    expect(row("warmup_calibration").querySelector('[role="alert"]').textContent).toContain("列表刷新失败");
    expect(row("warmup_calibration").querySelector(".status-badge").textContent).toBe("未测试");
    expect(testButton("warmup_calibration").disabled).toBe(false);
    expect(JSON.stringify(workbench.toast.mock.calls)).not.toContain("private refresh detail");
    expect(workbench.toast).toHaveBeenCalledWith("路由检测需要注意", expect.any(String), "error");
  });

  it("allows an explicit retry when the server disallows automatic refresh during failure cooldown", async () => {
    workbench.data.routes = [makeRoute("warmup_calibration", "stt.streaming", "failed", {
      readiness: { status: "failed", can_refresh: false, retry_at: "2099-01-01T00:00:00Z" },
    })];
    await render();
    expect(testButton("warmup_calibration").disabled).toBe(false);
    await act(async () => testButton("warmup_calibration").click());
    expect(workbench.request).toHaveBeenCalledTimes(1);
  });
});
