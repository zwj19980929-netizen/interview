import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import PlansPage from "./Page.jsx";

let wb, host, root;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => wb }));
const plan = (version = 2, question = "你会如何保证任务重试不会重复扣款？") => ({
  id: "plan_1", role_requirement_id: "role_1", job_position_id: "position_1", candidate_profile_id: "candidate_1", version,
  status: "draft", execution_schema_version: 3, experience_question_ids: ["e1", "e2"],
  created_at: "2026-09-10T10:00:00Z",
  enterprise_skill_id: "skill_1", enterprise_skill_snapshot: {
    skill_id: "skill_1", revision_id: "revision_4", revision: 4, content_hash: "skill_exact_hash", authorization_epoch: 1,
  },
  assessment_contract: {
    contract_hash: "contract_exact_hash", role_requirement_version: 3,
    competencies: [{ id: "可靠性", weight: 1, min_evidence_roots: 2, required: true }],
    budget: { min_root_questions: 2, max_root_questions: 6, max_followups_per_root: 1,
      max_total_followups: 4, max_duration_seconds: 1800, closing_reserve_seconds: 30 },
    candidate_questions: [{ question_id: "q_1", source_type: "position_bank", competency_ids: ["可靠性"],
      frozen_question: { title: "任务重试", version: 5, question_text: "原始综合系统设计题", standard_answer: "通过业务幂等键保证重复请求安全。",
        key_points: [{ id: "point_1", text: "业务幂等键" }], rubric: { excellent: "能解释原子性" } },
      inquiry_units: [{ id: "unit_1", question_text: question, competency_ids: ["可靠性"],
        assessed_rubric_point_ids: ["point_1"], standard_answer_quote: "通过业务幂等键保证重复请求安全。" }],
    }],
  },
});
const button = (text) => [...host.querySelectorAll("button")].find((node) => node.textContent.trim() === text || node.getAttribute("aria-label") === text);
const click = (text) => act(async () => button(text).click());
const render = () => act(async () => root.render(<PlansPage />));
const detail = async (id = "plan_1") => { wb.route = { view: "plans", selectedPlanId: id }; await render(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const enterSearch = (value) => act(async () => {
  const input = host.querySelector('[aria-label="搜索候选人或岗位"]');
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});
const selectTab = (label) => act(async () => {
  [...host.querySelectorAll('[role="tab"],button')].find((node) => node.textContent.trim().startsWith(label)).click();
});

beforeEach(() => {
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  wb = { API: "/api/v1", auth: { roles: ["interviewer"] }, route: { view: "plans" },
    data: { plans: [plan()], positions: [{ id: "position_1", name: "后端工程师", knowledge_base_ids: ["kb_1"] }],
      roles: [{ id: "role_1", job_position_id: "position_1", title: "后端岗位要求" }], candidates: [{ id: "candidate_1", name: "候选人甲" }],
      knowledgeBases: [{ id: "kb_1", name: "题库", job_position_id: "position_1" }], questions: [{ id: "q_1" }] },
    toast: vi.fn(), reloadRoute: vi.fn(async () => {}),
    navigate: vi.fn((view, id) => { wb.route = { view, ...(id === "new" ? { planAction: "create" } : id ? { selectedPlanId: id } : {}) }; root.render(<PlansPage />); }),
    request: vi.fn(async (path, options) => path.endsWith("/interview-skills") ? { items: [] }
      : options?.method === "PATCH" ? { ...plan(3), status: "approved" } : plan()),
  };
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });

it("distinguishes candidates sharing a role and opens a draft through its own route", async () => {
  wb.data.candidates.push({ id: "candidate_2", name: "候选人乙" });
  wb.data.plans.push({ ...plan(), id: "plan_2", candidate_profile_id: "candidate_2", status: "approved" });
  await render();
  expect(host.textContent).toContain("候选人甲");
  expect(host.textContent).toContain("候选人乙");
  expect(host.textContent).toContain("后端工程师");
  expect([...host.querySelectorAll(".plans-person-copy h2")].map((node) => node.textContent)).toEqual(["候选人甲", "候选人乙"]);
  expect(host.querySelector(".plans-row-meta small").textContent).toMatch(/\d{2}:\d{2}.*创建/);
  expect(button("审阅计划")).toBeTruthy();
  expect(button("查看计划")).toBeTruthy();
  expect(wb.request).not.toHaveBeenCalled();
  await click("审阅计划");
  expect(wb.navigate).toHaveBeenCalledWith("plans", "plan_1");
  expect(wb.request).toHaveBeenCalledWith("/api/v1/interview-plans/plan_1");
  await click("返回计划列表");
  expect(wb.route).toEqual({ view: "plans" });
});

it("opens creation as a full page route", async () => {
  await render(); await click("新建计划");
  expect(wb.navigate).toHaveBeenCalledWith("plans", "new");
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(button("创建面试计划 →")).toBeTruthy();
  expect(button("新建岗位要求")).toBeUndefined();
});

it("filters by candidate or role and status, then clears an empty result", async () => {
  wb.data.candidates.push({ id: "candidate_2", name: "候选人乙" }, { id: "candidate_3", name: "候选人丙" });
  wb.data.plans.push({ ...plan(), id: "plan_2", candidate_profile_id: "candidate_2", status: "approved" },
    { ...plan(), id: "plan_3", candidate_profile_id: "candidate_3", status: "archived" });
  await render(); await enterSearch("候选人乙");
  expect(host.textContent).toContain("候选人乙");
  expect(host.textContent).not.toContain("候选人甲");
  await enterSearch("后端"); await selectTab("待审阅");
  expect(host.textContent).toContain("候选人甲");
  expect(host.textContent).not.toContain("候选人乙");
  await selectTab("已启用");
  expect(host.textContent).toContain("候选人乙");
  expect(host.textContent).not.toContain("候选人甲");
  await selectTab("已归档");
  expect(host.textContent).toContain("候选人丙");
  await enterSearch("找不到的人");
  expect(button("清除筛选")).toBeTruthy();
  await click("清除筛选");
  expect(host.querySelector('[aria-label="搜索候选人或岗位"]').value).toBe("");
  expect(host.textContent).toContain("候选人甲");
  expect(host.textContent).toContain("候选人乙");
  expect(host.textContent).toContain("候选人丙");
});

it("freshly reads and displays actual questions before enabling exactly that version", async () => {
  wb.request = vi.fn(async (path, options) => options?.method === "PATCH"
    ? { ...plan(8, "刚审阅的新问题？"), status: "approved" } : plan(7, "刚审阅的新问题？"));
  await detail();
  expect(host.textContent).toContain("刚审阅的新问题？");
  expect(host.textContent).not.toContain("你会如何保证任务重试不会重复扣款？");
  expect(host.textContent).toContain("7");
  expect(host.textContent).toContain("skill_exact_hash");
  expect(host.textContent).toContain("100%");
  expect(host.textContent).toContain("业务幂等键");
  expect(host.textContent).toContain("原始综合系统设计题");
  const actual = [...host.querySelectorAll("p,strong,h3,h4")].find((node) => node.textContent === "刚审阅的新问题？");
  expect(actual).toBeTruthy();
  expect(actual.closest("details")).toBeNull();
  const technical = [...host.querySelectorAll("code")].find((node) => node.textContent === "skill_exact_hash");
  expect(technical.closest("details")).not.toBeNull();
  await click("确认并启用计划");
  expect(wb.request).toHaveBeenCalledWith("/api/v1/interview-plans/plan_1", {
    method: "PATCH", body: { expected_version: 7, status: "approved" },
  });
  expect(button("确认并启用计划")).toBeUndefined();
  expect(host.textContent).toContain("已启用");
});

it("requires a new read after a version conflict without automatically retrying approval", async () => {
  let reads = 0;
  wb.request = vi.fn(async (path, options) => {
    if (options?.method === "PATCH") throw Object.assign(new Error("conflict"), { code: "PERSISTENCE_CONFLICT" });
    return plan(++reads === 1 ? 7 : 8, reads === 1 ? "先前看到的问题？" : "冲突后修改的问题？");
  });
  await detail(); await click("确认并启用计划");
  expect(host.querySelector('[role="alert"]')).not.toBeNull();
  expect(button("确认并启用计划").disabled).toBe(true);
  expect(reads).toBe(1);
  await click("重新加载");
  expect(host.textContent).toContain("冲突后修改的问题？");
  expect(button("确认并启用计划").disabled).toBe(false);
  expect(wb.request.mock.calls.filter(([, options]) => options?.method === "PATCH")).toHaveLength(1);
});

it("does not issue duplicate approvals while the first result is pending", async () => {
  const pending = deferred();
  wb.request = vi.fn((path, options) => options?.method === "PATCH" ? pending.promise : Promise.resolve(plan()));
  await detail();
  await act(async () => { button("确认并启用计划").click(); button("确认并启用计划")?.click(); });
  expect(wb.request.mock.calls.filter(([, options]) => options?.method === "PATCH")).toHaveLength(1);
  await act(async () => pending.resolve({ ...plan(3), status: "approved" }));
});

it("does not let an older detail response replace a newer selected plan", async () => {
  const old = deferred();
  const next = { ...plan(8, "乙候选人的问题？"), id: "plan_2", candidate_profile_id: "candidate_2" };
  wb.data.plans.push(next); wb.data.candidates.push({ id: "candidate_2", name: "候选人乙" });
  wb.request = vi.fn((path) => path.endsWith("/plan_1") ? old.promise : Promise.resolve(next));
  await detail("plan_1"); await detail("plan_2");
  expect(host.textContent).toContain("乙候选人的问题？");
  await act(async () => old.resolve(plan(2, "过时问题不应显示？")));
  expect(host.textContent).toContain("乙候选人的问题？");
  expect(host.textContent).not.toContain("过时问题不应显示？");
});

it("keeps historical fixed plans read only after loading their current content", async () => {
  wb.request = vi.fn(async () => ({ ...plan(), execution_schema_version: 2, bank_slots: [{ id: "slot_1" }] }));
  await detail();
  expect(host.textContent).toContain("这份计划仅供查看");
  expect(button("确认并启用计划")).toBeUndefined();
  expect(wb.request.mock.calls.some(([, options]) => options?.method === "PATCH")).toBe(false);
});

it("shows saved v2 directions and frozen resume questions without substituting the current question bank", async () => {
  const historical = {
    id: "plan_1", version: 4, status: "approved", execution_schema_version: 2,
    candidate_profile_id: "candidate_1", job_position_id: "position_1", role_requirement_id: "role_1",
    created_at: "2026-08-20T09:15:00Z", estimated_minutes: 45,
    bank_slots: [
      { id: "slot_1", order: 1, dimension: "历史系统可靠性", expected_minutes: 7, difficulty: "senior", allow_followup: true,
        weight: 0.6, candidate_pool: [{ question_id: "q_legacy", question_version: 3 }] },
      { id: "slot_2", order: 2, dimension: "历史数据库事务", expected_minutes: 5, difficulty: "mid", allow_followup: false,
        weight: 0.4, candidate_pool: [{ question_id: "q_transaction", question_version: 2 }] },
    ],
    experience_question_ids: ["experience_1"],
    experience_question_snapshots: [{ id: "experience_1", version: 2, order: 1,
      question_text: "请说明你当时负责的订单迁移工作，以及一次实际遇到的回滚。",
      evaluation_focus: { description: "说明个人职责与回滚依据", points: ["职责边界", "实际结果"] } }],
  };
  wb.data.plans = [historical];
  wb.data.questions = [
    { id: "q_legacy", version: 9, question_text: "当前题库已修改：请设计全新的库存服务。" },
    { id: "q_transaction", version: 8, question_text: "当前题库已修改：请解释另一个数据库。" },
  ];
  wb.request = vi.fn(async () => historical);
  await detail();
  const content = host.querySelector('[aria-label="计划考察内容"]');
  expect(content.textContent).toContain("历史系统可靠性");
  expect(content.textContent).toContain("历史数据库事务");
  expect(content.textContent).toContain("约 7 分钟");
  expect(content.textContent).toContain("深入");
  expect(content.textContent).toContain("可追问");
  const frozenText = [...content.querySelectorAll("p")].find((node) => node.textContent === historical.experience_question_snapshots[0].question_text);
  expect(frozenText).toBeTruthy();
  expect(frozenText.closest("details")).toBeNull();
  expect(content.textContent).toContain("说明个人职责与回滚依据");
  expect(content.textContent).toContain("题目版本 3");
  expect(content.textContent).toContain("q_legacy");
  expect(content.textContent).not.toContain("当前题库已修改");
  expect(button("确认并启用计划")).toBeUndefined();
  expect(wb.request.mock.calls).toEqual([["/api/v1/interview-plans/plan_1"]]);
});

it.each([{ view: "plans" }, { view: "plans", planAction: "create" }, { view: "plans", selectedPlanId: "plan_1" }])("keeps private plan data and mutations inaccessible on $view routes", async (route) => {
  wb.auth.roles = ["candidate"]; wb.route = route;
  await render();
  expect(host.textContent).toContain("需要面试配置权限");
  expect(host.textContent).not.toContain("你会如何保证任务重试");
  expect(button("新建计划")).toBeUndefined();
  expect(button("确认并启用计划")).toBeUndefined();
  expect(wb.request).not.toHaveBeenCalled();
});

it.each(["skill", "units", "hash", "version"])("blocks approval when the %s snapshot needed for review is missing", async (missing) => {
  const broken = plan();
  if (missing === "skill") broken.enterprise_skill_snapshot = null;
  else if (missing === "units") broken.assessment_contract.candidate_questions[0].inquiry_units = [];
  else if (missing === "hash") delete broken.assessment_contract.contract_hash;
  else delete broken.version;
  wb.request = vi.fn(async () => broken);
  await detail();
  expect(button("确认并启用计划").disabled).toBe(true);
});

it("allows the base interviewer and describes missing optional company context", async () => {
  wb.request = vi.fn(async () => ({ ...plan(), enterprise_skill_id: null, enterprise_skill_snapshot: null }));
  await detail();
  expect(host.textContent).toContain("基础面试官");
  expect(host.textContent).toContain("岗位要求");
  expect(button("确认并启用计划").disabled).toBe(false);
});

it("shows optional company context separately from the selected Skill", async () => {
  wb.request = vi.fn(async () => ({ ...plan(), company_context: "公司业务是订单履约。" }));
  await detail();
  expect(host.textContent).toContain("Skill");
  expect(host.textContent).toContain("企业资料");
  expect(host.textContent).toContain("公司业务是订单履约。");
  expect(host.textContent).not.toContain("企业 Skill");
});
