import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import PlanCreate from "./PlanCreate.jsx";

let wb, root, host, onCreated, onCancel;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => wb }));
const newPlan = { id: "plan_new", version: 1, status: "approved", execution_schema_version: 3 };
const customization = (skill = null, company = "") => ({ version: 2, skill,
  company_profile: { company_name: company, business_overview: "", products_services: "", additional_info: "" }, updated_at: null,
});
const activeSkill = { skill_id: "skill_new", name: "自然交流", instructions: "先从实际项目聊起", status: "active", revision: 2 };
const button = (label) => [...host.querySelectorAll("button")].find((node) => node.textContent === label);
const click = (label) => act(async () => button(label).click());
const render = (props = {}) => act(async () => root.render(<PlanCreate onCreated={onCreated} onCancel={onCancel} {...props} />));
const submit = () => act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const field = (name) => host.querySelector(`[name="${name}"]`);
const choose = (name, value) => act(async () => {
  const input = field(name);
  input.value = value;
  input.dispatchEvent(new Event("change", { bubbles: true }));
});
const enter = (name, value) => act(async () => {
  const input = field(name);
  const prototype = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value").set.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});
const posts = () => wb.request.mock.calls.filter(([, options]) => options?.method === "POST");
const openDetails = (title) => act(async () => {
  const details = [...host.querySelectorAll("details")].find((node) => node.querySelector("summary").textContent.includes(title));
  details.open = true;
  details.dispatchEvent(new Event("toggle"));
});

beforeEach(() => {
  onCreated = vi.fn(); onCancel = vi.fn();
  wb = { API: "/api/v1", data: {
    roles: [
      { id: "role_1", title: "后端开发", job_position_id: "position_1", interview_duration_minutes: 45 },
      { id: "role_2", title: "前端开发", job_position_id: "position_2", interview_duration_minutes: 30 },
    ],
    positions: [
      { id: "position_1", name: "后端开发", knowledge_base_ids: ["bank_shared"] },
      { id: "position_2", name: "前端开发", knowledge_base_ids: ["bank_shared"] },
    ],
    candidates: [
      { id: "candidate_1", name: "陈晨", job_position_id: "position_1" },
      { id: "candidate_2", name: "李明", job_position_id: "position_2" },
      { id: "candidate_legacy", name: "王晓" },
    ],
    knowledgeBases: [
      { id: "bank_1", name: "后端题库", status: "ready", job_position_id: "position_1" },
      { id: "bank_2", name: "前端题库", status: "ready", job_position_id: "position_2" },
      { id: "bank_shared", name: "基础能力题库", status: "ready" },
    ],
  }, request: vi.fn(async (path) => path.endsWith("/interview-customization") ? customization(activeSkill, "示例企业") : newPlan) };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks();
});

it("prepares a usable plan in one submission without role, coverage, depth or approval input", async () => {
  wb.data.roles = [];
  await render();
  expect(field("role_requirement_id")).toBeNull();
  expect(field("min_root_questions")).toBeNull();
  expect(field("coverage")).toBeNull();
  expect(field("skill_id")).toBeNull();
  expect(field("company_context")).toBeNull();
  expect(button("下一步")).toBeUndefined();
  expect(host.querySelectorAll("details[open]")).toHaveLength(0);
  await click("创建面试计划 →");
  expect(posts()).toHaveLength(1);
  expect(posts()[0]).toEqual(["/api/v1/interview-plans/prepare", { method: "POST", timeoutMs: 210000, signal: expect.any(AbortSignal), headers: { Accept: "text/event-stream" }, onProgress: expect.any(Function), body: {
    job_position_id: "position_1", candidate_profile_id: "candidate_1", knowledge_base_ids: ["bank_1"], duration_minutes: 45,
  } }]);
  expect(onCreated).toHaveBeenCalledWith(newPlan);
});

it("carries the selected candidate's position and bank into preparation", async () => {
  await render({ initialCandidateId: "candidate_2" });
  expect(field("job_position_id").value).toBe("position_2");
  expect(field("candidate_profile_id").value).toBe("candidate_2");
  expect(field("knowledge_base_id").value).toBe("bank_2");
});

it("changes position with valid dependent defaults and excludes unlinked banks", async () => {
  await render();
  await choose("job_position_id", "position_2");
  expect(field("candidate_profile_id").value).toBe("candidate_2");
  expect(field("knowledge_base_id").value).toBe("bank_2");
  expect([...field("knowledge_base_id").options].map((item) => item.value)).toEqual(["", "bank_2", "bank_shared"]);
  await choose("knowledge_base_id", "bank_shared");
  await submit();
  expect(posts()[0][1].body.knowledge_base_ids).toEqual(["bank_shared"]);
});

it("offers optional duration and saved customization without requiring either module", async () => {
  wb.request.mockImplementation(async (path) => path.endsWith("/interview-customization") ? customization() : newPlan);
  await render();
  expect(host.textContent).toContain("未设置 Skill");
  expect(host.textContent).toContain("未设置企业资料");
  await openDetails("时长与面试定制");
  await choose("duration_minutes", "30");
  await submit();
  expect(posts()[0][1].body.duration_minutes).toBe(30);
  expect(onCreated).toHaveBeenCalled();
});

it("keeps preparation available when optional customization status cannot load", async () => {
  wb.request.mockImplementation(async (path) => { if (path.endsWith("/interview-customization")) throw new Error("offline"); return newPlan; });
  await render();
  expect(host.textContent).toContain("生成时应用已保存定制");
  await submit();
  expect(onCreated).toHaveBeenCalledWith(newPlan);
});

it("prevents double submission and freezes inputs while preparation runs", async () => {
  const pending = deferred();
  wb.request.mockImplementation((path) => path.endsWith("/interview-customization") ? Promise.resolve(customization()) : pending.promise);
  await render();
  await submit(); await submit();
  expect(posts()).toHaveLength(1);
  expect(host.querySelector("fieldset").disabled).toBe(true);
  expect(host.querySelector('[role="status"]').textContent).toContain("正在读取题库");
  await act(async () => pending.resolve(newPlan));
  expect(onCreated).toHaveBeenCalledTimes(1);
});

it("retains choices and exposes the actual preparation error for a retry", async () => {
  wb.request.mockImplementation(async (path) => { if (path.endsWith("/interview-customization")) return customization(); throw new Error("题库内容发生变化，请重新准备面试。"); });
  await render(); await submit();
  expect(host.querySelector('[role="alert"]').textContent).toContain("题库内容发生变化");
  expect(field("knowledge_base_id").value).toBe("bank_1");
  expect(host.querySelector("fieldset").disabled).toBe(false);
  expect(onCreated).not.toHaveBeenCalled();
});

it("does not submit unavailable banks or invalidated candidate selections", async () => {
  wb.data.knowledgeBases.forEach((item) => { item.status = "building"; });
  await render(); await submit();
  expect(posts()).toHaveLength(0);
  expect(host.textContent).toContain("查看题库进度");
  wb.data.knowledgeBases[0].status = "ready";
  await render(); await choose("knowledge_base_id", "bank_1");
  wb.data.candidates = [];
  await render(); await submit();
  expect(posts()).toHaveLength(0);
});

it("ignores a late result after changing organization scope", async () => {
  const pending = deferred();
  wb.auth = { organization_id: "old" };
  wb.request.mockImplementation((path) => path.endsWith("/interview-customization") ? Promise.resolve(customization()) : pending.promise);
  await render(); await submit();
  wb.auth = { organization_id: "new" };
  await render();
  await act(async () => pending.resolve(newPlan));
  expect(onCreated).not.toHaveBeenCalled();
});

it("rejects an unexpected draft response instead of claiming preparation succeeded", async () => {
  wb.request.mockImplementation(async (path) => path.endsWith("/interview-customization") ? customization() : { ...newPlan, status: "draft" });
  await render(); await submit();
  expect(onCreated).not.toHaveBeenCalled();
  expect(host.querySelector('[role="alert"]').textContent).toContain("结果尚未确认");
});

it("cancels without creating a plan", async () => {
  await render(); await click("取消");
  expect(onCancel).toHaveBeenCalledOnce(); expect(posts()).toHaveLength(0);
});

it("shows only actual completed points, reused points, then saving", async () => {
  const pending = deferred();
  wb.request.mockImplementation((path) => path.endsWith("/interview-customization") ? Promise.resolve(customization()) : pending.promise);
  await render(); await submit();
  const { onProgress } = posts()[0][1];
  await act(async () => onProgress({ stage: "preparing", completed_points: 8, total_points: 20, reused_points: 8 }));
  expect(host.textContent).toContain("已完成 8 / 20 个考察点");
  expect(host.textContent).toContain("8 个考察点直接复用");
  expect(host.querySelector("progress").value).toBe(8);
  await act(async () => onProgress({ stage: "preparing", completed_points: 22, total_points: 20, reused_points: 8 }));
  expect(host.querySelector("progress").value).toBe(8);
  await act(async () => onProgress({ stage: "preparing", completed_points: 20, total_points: 20, reused_points: 8 }));
  await act(async () => onProgress({ stage: "saving" }));
  expect(host.textContent).toContain("正在校验并保存计划");
  expect(onCreated).not.toHaveBeenCalled();
  await act(async () => pending.resolve(newPlan));
  expect(onCreated).toHaveBeenCalledWith(newPlan);
});

it("aborts the pending stream on scope change and ignores its later progress", async () => {
  const pending = deferred();
  wb.auth = { organization_id: "old" };
  wb.request.mockImplementation((path) => path.endsWith("/interview-customization") ? Promise.resolve(customization()) : pending.promise);
  await render(); await submit();
  const options = posts()[0][1];
  wb.auth = { organization_id: "new" }; await render();
  expect(options.signal.aborted).toBe(true);
  await act(async () => options.onProgress({ stage: "preparing", completed_points: 1, total_points: 2, reused_points: 0 }));
  expect(host.querySelector("progress")).toBeNull();
  await act(async () => pending.resolve(newPlan));
  expect(onCreated).not.toHaveBeenCalled();
});

it("does not claim a timed-out request failed to save and retains selections", async () => {
  wb.request.mockImplementation(async (path) => {
    if (path.endsWith("/interview-customization")) return customization();
    throw Object.assign(new Error("请求超时"), { code: "REQUEST_TIMEOUT" });
  });
  await render(); await submit();
  expect(host.querySelector('[role="alert"]').textContent).toContain("请先到计划列表查看");
  expect(field("knowledge_base_id").value).toBe("bank_1");
  expect(host.querySelector("fieldset").disabled).toBe(false);
});
