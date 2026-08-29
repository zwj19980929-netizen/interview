import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import App from "./App.jsx";
import { publicFeatures, workspaceFeatures } from "./features/registry.js";

describe("React workbench shell", () => {
  it("renders every existing workspace route", async () => {
    window.history.replaceState(null, "", "#overview");
    globalThis.fetch = async (path) => {
      if (String(path).endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (String(path).endsWith("/workspace/question-overview")) return new Response(JSON.stringify({ total: 0, ready: 0, recent: [] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && host.querySelectorAll("[data-view]").length < 6; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect([...host.querySelectorAll("[data-view]")].map((node) => node.dataset.view)).toEqual([
      "overview", "questions", "workflow", "plans", "interviews", "models",
    ]);
    expect(host.querySelector(".content")).not.toBeNull();
    await act(async () => root.unmount());
  });

  it("keeps role and public route ownership in feature modules", () => {
    expect(workspaceFeatures.find((feature) => feature.id === "models").roles).toEqual(["admin"]);
    expect(workspaceFeatures.find((feature) => feature.id === "interviews").roles).toContain("reviewer");
    expect(publicFeatures[0].routes).toEqual(["candidate", "invite"]);
  });

  it("hydrates a reviewer into the read-only interviews route without admin requests", async () => {
    const calls = [];
    window.lucide = { createIcons() {} };
    window.history.replaceState(null, "", "#overview");
    globalThis.fetch = async (path) => {
      calls.push(String(path));
      if (String(path).endsWith("/auth/session")) {
        return new Response(JSON.stringify({ actor_id: "reviewer_1", organization_id: "org_1", roles: ["reviewer"], authenticated: true }));
      }
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !calls.includes("/api/v1/interviews"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    expect(window.location.hash).toBe("#interviews");
    expect(calls).toContain("/api/v1/interviews");
    expect(calls.some((path) => path.includes("/api/v1/admin/"))).toBe(false);
    expect(host.querySelector('[data-view="models"]')).toBeNull();
    await act(async () => root.unmount());
  });

  it("generates and enables a plan in one confirmation without self-approval", async () => {
    window.history.replaceState(null, "", "#plans");
    const calls = [];
    const position = { id: "position_1", name: "后端工程师", knowledge_base_ids: ["kb_1"] };
    const role = { id: "role_1", title: "后端岗位要求", job_position_id: "position_1", must_have_skills: ["python"] };
    const candidate = { id: "candidate_1", name: "候选人甲", job_position_id: "position_1" };
    const bank = { id: "kb_1", name: "后端题库", job_position_id: "position_1" };
    const question = { id: "question_1", knowledge_base_id: "kb_1", status: "active" };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/workspace/question-catalog")) return new Response(JSON.stringify({ positions: [position], knowledge_bases: [bank], questions: [question] }));
      if (url.endsWith("/role-requirements")) return new Response(JSON.stringify({ items: [role] }));
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: [candidate] }));
      if (url.endsWith("/interview-plans/generate") && options.method === "POST") {
        calls.push(JSON.parse(options.body));
        return new Response(JSON.stringify({ id: "plan_1", status: "approved", execution: { stages: [{ slots: [{ id: "slot_1" }] }] } }));
      }
      if (url.endsWith("/interview-plans")) return new Response(JSON.stringify({ items: [] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let generate;
    for (let attempt = 0; attempt < 30 && !generate; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); generate = [...host.querySelectorAll("button")].find((node) => node.textContent === "生成计划"); }
    await act(async () => generate.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="生成面试计划"]');
    expect(dialog.textContent).toContain("无需再次审批");
    expect([...dialog.querySelectorAll("button")].some((node) => node.textContent === "生成并启用计划")).toBe(true);
    await act(async () => dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls[0]).toMatchObject({ approve: true, role_requirement_id: "role_1", candidate_profile_id: "candidate_1", knowledge_base_ids: ["kb_1"] });
    expect(host.textContent).not.toContain("审批计划");
    await act(async () => root.unmount());
  });

  it("copies the one-time candidate invitation link with one click", async () => {
    window.history.replaceState(null, "", "#interviews");
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const plan = { id: "plan_1", status: "approved", candidate_profile_id: "candidate_1", job_position_id: "position_1" };
    const candidate = { id: "candidate_1", name: "候选人甲" };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/interview-appointments") && options.method === "POST") return new Response(JSON.stringify({ id: "appointment_1", scheduled_end_at: "2026-09-02T02:00:00.000Z" }));
      if (url.endsWith("/interview-appointments/appointment_1/invite") && options.method === "POST") return new Response(JSON.stringify({ join_url: "/#invite/token_123" }));
      if (url.endsWith("/interview-plans")) return new Response(JSON.stringify({ items: [plan] }));
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: [candidate] }));
      if (url.endsWith("/interviews") || url.endsWith("/role-requirements") || url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let create;
    for (let attempt = 0; attempt < 30 && !create; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); create = [...host.querySelectorAll("button")].find((node) => node.textContent === "创建预约"); }
    await act(async () => create.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="创建面试预约"]');
    const start = dialog.querySelector('[name="scheduled_start_at"]');
    const end = dialog.querySelector('[name="scheduled_end_at"]');
    await act(async () => {
      start.value = "2026-09-02T01:00"; start.dispatchEvent(new Event("input", { bubbles: true }));
      end.value = "2026-09-02T02:00"; end.dispatchEvent(new Event("input", { bubbles: true }));
      dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    let copy;
    for (let attempt = 0; attempt < 30 && !copy; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); copy = [...document.querySelectorAll("button")].find((node) => node.textContent === "复制链接"); }
    await act(async () => copy.click());
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/#invite/token_123`);
    expect(copy.textContent).toBe("已复制");
    await act(async () => root.unmount());
  });

  it("confirms an appointment after identity verification without starting the interview", async () => {
    window.history.replaceState(null, "", "#invite/token_confirm");
    const getUserMedia = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
    const invitation = { position_name: "后端工程师", status: "invited", scheduled_start_at: "2099-09-02T01:00:00.000Z", scheduled_end_at: "2099-09-02T02:00:00.000Z", consent: { version: "v1", privacy_notice: "隐私说明", recording_required: true } };
    const requests = [];
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/public/interview-invitations/token_confirm") && options.method === "GET") return new Response(JSON.stringify(invitation));
      if (url.endsWith("/public/interview-invitations/token_confirm/intake") && options.method === "POST") { requests.push(JSON.parse(options.body)); return new Response(JSON.stringify({ status: "registered", email_reminder: { status: "scheduled" } })); }
      throw new Error(`Unexpected request: ${url}`);
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let form;
    for (let attempt = 0; attempt < 30 && !form; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); form = host.querySelector("form"); }
    const setValue = (name, value) => { const input = form.querySelector(`[name="${name}"]`); input.value = value; input.dispatchEvent(new Event("input", { bubbles: true })); };
    setValue("name", "候选人甲"); setValue("email", "candidate@example.com"); setValue("phone", "13800138000");
    form.querySelector('[name="privacy_accepted"]').checked = true;
    form.querySelector('[name="recording_accepted"]').checked = true;
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("预约已确认"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(requests[0]).toMatchObject({ name: "候选人甲", email: "candidate@example.com", phone: "13800138000" });
    expect(host.textContent).toContain("身份核验通过");
    expect(host.textContent).toContain("面试开始前 30 分钟");
    expect(getUserMedia).not.toHaveBeenCalled();
    expect([...host.querySelectorAll("button")].find((node) => node.textContent === "尚未到面试时间")?.disabled).toBe(true);
    await act(async () => root.unmount());
  });

  it("starts with knowledge bases and keeps the first-bank path available in an empty workspace", async () => {
    window.history.replaceState(null, "", "#questions");
    globalThis.fetch = async (path) => {
      if (String(path).endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    let createButton;
    for (let attempt = 0; attempt < 30 && !createButton; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      createButton = [...host.querySelectorAll("button")].find((node) => node.textContent === "新建题库");
    }
    expect(createButton?.disabled).toBe(false);
    await act(async () => createButton.click());
    expect(document.querySelector('[role="dialog"][aria-label="新建题库"]')).not.toBeNull();
    expect(document.body.textContent).toContain("题库属于一个岗位");
    await act(async () => root.unmount());
  });

  it("shows a clearly labelled reading voice without exposing the internal model configuration id", async () => {
    window.history.replaceState(null, "", "#questions");
    const bank = {
      id: "kb_1",
      name: "测试岗位题库",
      description: "测试",
      job_position: { id: "position_1", name: "测试岗位" },
      question_count: 2,
      speech_ready_count: 2,
      speech_build_status: "ready",
      speech_profile: { model_configuration_id: "model_cfg_internal_123", voice_profile_id: "tongtong" },
    };
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("测试岗位题库"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(host.textContent).toContain("读题语音");
    expect(host.textContent).toContain("tongtong");
    expect(host.textContent).toContain("已配置");
    expect(host.textContent).not.toContain("model_cfg_internal_123");
    await act(async () => root.unmount());
  });

  it("assigns an existing knowledge base to a position and inherits its voice", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const position = { id: "position_1", name: "测试工程师", description: "", status: "active", version: 3, knowledge_base_ids: [] };
    const bank = { id: "kb_1", name: "自动化题库", job_position_id: "position_other", voice_profile_id: "legacy", speech_profile: { voice_profile_id: "tongtong" } };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions/position_1/knowledge-base-assignments") && options.method === "POST") {
        calls.push(JSON.parse(options.body));
        position.knowledge_base_ids = [bank.id];
        position.version += 1;
        return new Response(JSON.stringify({ position, knowledge_base: bank }));
      }
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [position] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    let button;
    for (let attempt = 0; attempt < 30 && !button; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      button = [...host.querySelectorAll("button")].find((node) => node.textContent === "关联题库");
    }
    await act(async () => button.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="管理 测试工程师 的题库"]');
    expect(dialog.textContent).toContain("自动化题库 · 音色 tongtong");
    expect(dialog.textContent).toContain("直接使用该题库已有题目、语音模型和音色");
    expect(dialog.querySelector('[name="voice_profile_id"]')).toBeNull();
    expect(dialog.querySelector('[name="language"]')).toBeNull();
    const select = dialog.querySelector('[name="knowledge_base_id"]');
    await act(async () => { select.value = bank.id; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await act(async () => dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toEqual([{ knowledge_base_id: "kb_1", expected_position_version: 3 }]);
    await act(async () => root.unmount());
  });

  it("keeps knowledge base management available when every bank is already assigned", async () => {
    window.history.replaceState(null, "", "#workflow");
    const position = { id: "position_1", name: "已关联岗位", description: "", status: "active", version: 3, knowledge_base_ids: ["kb_1"] };
    const bank = { id: "kb_1", name: "唯一题库", job_position_id: "position_other", speech_profile: { voice_profile_id: "tongtong" } };
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [position] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let manage;
    for (let attempt = 0; attempt < 30 && !manage; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); manage = [...host.querySelectorAll("button")].find((node) => node.textContent === "管理题库"); }
    expect(manage.disabled).toBe(false);
    await act(async () => manage.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="管理 已关联岗位 的题库"]');
    expect(dialog.textContent).toContain("已关联题库");
    expect(dialog.textContent).toContain("唯一题库 · tongtong");
    expect(dialog.textContent).toContain("现有题库均已关联到这个岗位");
    expect([...dialog.querySelectorAll("button")].some((node) => node.textContent === "前往题库管理")).toBe(true);
    expect([...dialog.querySelectorAll("button")].find((node) => node.textContent === "确认关联").disabled).toBe(true);
    await act(async () => root.unmount());
  });

  it("creates a position together with its initial screening requirement", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions") && options.method === "POST") {
        const body = JSON.parse(options.body); calls.push(body);
        return new Response(JSON.stringify({ id: "position_1", version: 1, ...body, initial_role_requirement: { id: "role_1", job_position_id: "position_1", ...body.initial_requirement } }));
      }
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let create;
    for (let attempt = 0; attempt < 30 && !create; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); create = [...host.querySelectorAll("button")].find((node) => node.textContent === "新建岗位"); }
    await act(async () => create.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="新建岗位与岗位要求"]');
    expect(dialog.textContent).toContain("岗位要求会和岗位一起创建");
    const values = {
      code: "python-backend", name: "Python 后端", description: "业务服务", requirement_description: "负责 Python API 与数据库设计",
      must_have_skills: "Python，PostgreSQL", nice_to_have_skills: "Redis, Kubernetes", interview_duration_minutes: "60",
    };
    await act(async () => { for (const [name, value] of Object.entries(values)) { const input = dialog.querySelector(`[name="${name}"]`); input.value = value; input.dispatchEvent(new Event("input", { bubbles: true })); } });
    await act(async () => dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls[0]).toMatchObject({
      code: "python-backend", name: "Python 后端",
      initial_requirement: {
        title: "首版岗位要求", description: "负责 Python API 与数据库设计",
        must_have_skills: ["Python", "PostgreSQL"], nice_to_have_skills: ["Redis", "Kubernetes"],
        seniority: "mid", interview_duration_minutes: 60,
      },
    });
    await act(async () => root.unmount());
  });

  it("lets an existing position add its first requirement", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const position = { id: "position_legacy", name: "既有岗位", description: "", status: "active", version: 1, knowledge_base_ids: [] };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions/position_legacy/role-requirements") && options.method === "POST") { calls.push(JSON.parse(options.body)); return new Response(JSON.stringify({ id: "role_legacy", job_position_id: position.id, ...JSON.parse(options.body) })); }
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [position] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let add;
    for (let attempt = 0; attempt < 30 && !add; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); add = [...host.querySelectorAll("button")].find((node) => node.textContent === "添加岗位要求"); }
    await act(async () => add.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="为 既有岗位 新增岗位要求"]');
    const values = { requirement_description: "负责服务研发", must_have_skills: "Go，MySQL" };
    await act(async () => { for (const [name, value] of Object.entries(values)) { const input = dialog.querySelector(`[name="${name}"]`); input.value = value; input.dispatchEvent(new Event("input", { bubbles: true })); } });
    await act(async () => dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls[0]).toMatchObject({ title: "既有岗位 岗位要求", description: "负责服务研发", must_have_skills: ["Go", "MySQL"] });
    await act(async () => root.unmount());
  });

  it("shows explainable candidate screening, resume access, review, and candidate CRUD", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const candidate = {
      id: "candidate_1", name: "候选人甲", email: "a***@example.com", phone: "138****0000", version: 3,
      retention_reason: "screening_unqualified", retention_expires_at: "2026-09-03T00:00:00Z",
      screening: {
        review_id: "review_1", review_version: 2, resume_document_id: "resume_1", job_position_name: "Python 后端",
        ai_recommendation: "unqualified", effective_outcome: "unqualified", score: 42, summary: "Python 证据不足",
        matched_requirements: [{ requirement: "项目交付", evidence: "完成 Java 项目" }],
        unmet_requirements: [{ requirement: "Python", reason: "未找到生产经验" }], human_review_status: "pending",
      },
    };
    window.open = vi.fn();
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/candidate-profiles/candidate_1/resumes/resume_1/content-url")) { calls.push({ url, method: options.method }); return new Response(JSON.stringify({ url: "/api/v1/private-files/grant_1" })); }
      if (url.endsWith("/candidate-profiles/candidate_1/resumes")) return new Response(JSON.stringify({ items: [{ id: "resume_1", original_file_name: "resume.pdf", status: "ready" }] }));
      if (url.endsWith("/resume-reviews/review_1/screening-review")) { calls.push({ url, method: options.method, body: JSON.parse(options.body) }); return new Response(JSON.stringify({ ...candidate.screening, human_decision: "qualified" })); }
      if (url.endsWith("/candidate-profiles/candidate_1") && options.method === "PATCH") { calls.push({ url, method: options.method }); return new Response(JSON.stringify({ ...candidate, version: 4 })); }
      if (url.includes("/candidate-profiles/candidate_1?expected_version=3") && options.method === "DELETE") { calls.push({ url, method: options.method }); return new Response(JSON.stringify({ ...candidate, status: "archived" })); }
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: [candidate] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("不符合"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(host.textContent).toContain("是否符合岗位要求");
    expect(host.textContent).toContain("0–59 分不符合");
    expect(host.textContent).toContain("75–100 分符合");
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent.includes("不符合")).click());
    for (let attempt = 0; attempt < 30 && !document.body.textContent.includes("Python 证据不足"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(document.body.textContent).toContain("入选依据");
    expect(document.body.textContent).toContain("淘汰/待补证据");
    expect(document.body.textContent).toContain("60–74 分待人工复核");
    expect(document.body.textContent).toContain("自动清理");
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "查看简历").click());
    expect(window.open).toHaveBeenCalledWith("/api/v1/private-files/grant_1", "_blank", "noopener,noreferrer");
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "复核为符合").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.url.includes("screening-review")); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls.find((call) => call.url.includes("screening-review")).body).toMatchObject({ expected_version: 2, decision: "qualified" });
    await act(async () => root.unmount());
  });

  it("requeues a failed resume screening from candidate detail", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const candidate = {
      id: "candidate_retry", name: "失败重试候选人", email: "r***@example.com", phone: "138****0004", version: 1,
      screening: {
        review_id: "review_retry", review_version: 8, resume_document_id: "resume_retry",
        job_position_id: "position_retry", job_position_name: "测试工程师",
        ai_recommendation: null, effective_outcome: "failed", score: null,
        summary: "简历初筛失败，请检查任务错误后重试。", matched_requirements: [], unmet_requirements: [],
        human_review_status: "pending", processing_stage: "failed",
        error: { code: "provider_circuit_open", message: "Provider circuit is open for this capability.", retryable: true },
      },
    };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/resume-reviews/review_retry/retry") && options.method === "POST") {
        calls.push({ url, body: JSON.parse(options.body) });
        return new Response(JSON.stringify({ review: { id: "review_retry", status: "queued", version: 9 }, job: { id: "work_retry", status: "pending" } }), { status: 202 });
      }
      if (url.endsWith("/candidate-profiles/candidate_retry/resumes")) return new Response(JSON.stringify({ items: [{ id: "resume_retry", file_name: "retry.pdf", status: "ready", version: 1 }] }));
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: [candidate] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let failed;
    for (let attempt = 0; attempt < 30 && !failed; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); failed = [...host.querySelectorAll("button")].find((node) => node.textContent.includes("处理失败")); }
    await act(async () => failed.click());
    let retry;
    for (let attempt = 0; attempt < 30 && !retry; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); retry = [...document.querySelectorAll("button")].find((node) => node.textContent === "重新初筛"); }
    expect(document.body.textContent).toContain("模型服务连续失败后已进入保护状态");
    expect(document.body.textContent).toContain("provider_circuit_open");
    await act(async () => retry.click());
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls[0].body).toEqual({ expected_version: 8, reason: "interviewer_requested_retry" });
    expect(document.body.textContent).toContain("失败的简历初筛已重新排队");
    await act(async () => root.unmount());
  });

  it("renames and deletes a resume from candidate detail with optimistic concurrency", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const candidate = { id: "candidate_1", name: "候选人简历管理", email: "a***@example.com", phone: "138****0000", version: 1, screening: null };
    let resumes = [{ id: "resume_2", candidate_profile_id: candidate.id, file_name: "latest.pdf", status: "processing", resume_version: 2, version: 2, created_at: "2026-08-28T06:52:13Z" }];
    const originalConfirm = window.confirm;
    window.confirm = vi.fn(() => true);
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/candidate-profiles/candidate_1/resumes/resume_2") && options.method === "PATCH") {
        const body = JSON.parse(options.body);
        calls.push({ url, method: options.method, body });
        resumes = [{ ...resumes[0], file_name: body.display_name, version: 3 }];
        return new Response(JSON.stringify(resumes[0]));
      }
      if (url.includes("/candidate-profiles/candidate_1/resumes/resume_2?expected_version=3") && options.method === "DELETE") {
        calls.push({ url, method: options.method });
        resumes = [];
        return new Response(JSON.stringify({ id: "resume_2", status: "deleted", version: 5 }));
      }
      if (url.endsWith("/candidate-profiles/candidate_1/resumes")) return new Response(JSON.stringify({ items: resumes }));
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: [candidate] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      await act(async () => root.render(<App />));
      let view;
      for (let attempt = 0; attempt < 30 && !view; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 10)); view = [...host.querySelectorAll("button")].find((node) => node.textContent === "查看"); }
      await act(async () => view.click());
      for (let attempt = 0; attempt < 30 && !document.body.textContent.includes("latest.pdf"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
      await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "修改名称").click());
      const nameInput = document.querySelector('[aria-label="简历名称"]');
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(nameInput, "renamed.pdf");
        nameInput.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "保存名称").click());
      for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "PATCH"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
      expect(calls.find((call) => call.method === "PATCH").body).toEqual({ expected_version: 2, display_name: "renamed.pdf" });
      expect(document.body.textContent).toContain("renamed.pdf");
      await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "删除简历").click());
      for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "DELETE"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
      expect(calls.find((call) => call.method === "DELETE").url).toContain("expected_version=3");
      expect(window.confirm).toHaveBeenCalled();
    } finally {
      window.confirm = originalConfirm;
      await act(async () => root.unmount());
    }
  });

  it("queues resume ingestion and screening without waiting for the LLM in the upload modal", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const position = { id: "position_1", name: "Python 后端", status: "active", version: 1, knowledge_base_ids: [] };
    const role = { id: "role_1", title: "Python 岗位要求", job_position_id: "position_1" };
    const candidate = { id: "candidate_1", name: "候选人乙", job_position_id: "position_1", email: "b***@example.com", phone: "138****0001", version: 1, screening: null };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/candidate-profiles/candidate_1/resumes/import-url") && options.method === "POST") {
        calls.push({ url, body: JSON.parse(options.body) });
        return new Response(JSON.stringify({ resume_document_id: "resume_1", ingestion_job_id: "work_1", ingestion_status: "pending" }), { status: 202 });
      }
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [position] }));
      if (url.endsWith("/role-requirements")) return new Response(JSON.stringify({ items: [role] }));
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: [candidate] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    let uploadButton;
    for (let attempt = 0; attempt < 30 && !uploadButton; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      uploadButton = [...host.querySelectorAll("button")].find((node) => node.textContent === "上传简历");
    }
    await act(async () => uploadButton.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="上传 候选人乙 的简历并初筛"]');
    const sourceUrl = dialog.querySelector('[name="source_url"]');
    const roleSelect = dialog.querySelector('[name="role_requirement_id"]');
    await act(async () => {
      sourceUrl.value = "https://example.com/resume.pdf";
      sourceUrl.dispatchEvent(new Event("input", { bubbles: true }));
      roleSelect.value = "role_1";
      roleSelect.dispatchEvent(new Event("change", { bubbles: true }));
      dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls[0].body).toMatchObject({
      job_position_id: "position_1",
      role_requirement_id: "role_1",
      url: "https://example.com/resume.pdf",
    });
    expect(calls.some((call) => call.url.includes("file-ingestion-jobs"))).toBe(false);
    expect(calls.some((call) => call.url.endsWith("/resume-reviews"))).toBe(false);
    expect(document.body.textContent).toContain("简历已进入后台处理");
    await act(async () => root.unmount());
  });

  it("edits a job position from the workflow card", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    const position = { id: "position_1", name: "旧岗位名", description: "旧说明", status: "active", version: 2, knowledge_base_ids: [] };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions/position_1") && options.method === "PATCH") {
        calls.push(JSON.parse(options.body));
        Object.assign(position, { name: "新岗位名", description: "新说明", version: 3 });
        return new Response(JSON.stringify(position));
      }
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [position] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    let menu;
    for (let attempt = 0; attempt < 100 && !menu; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      menu = host.querySelector(".position-action-menu");
    }
    expect(host.textContent).toContain("旧岗位名");
    expect(menu.open).toBe(false);
    await act(async () => menu.querySelector("summary").click());
    expect(menu.open).toBe(true);
    const edit = [...menu.querySelectorAll("button")].find((node) => node.textContent.trim() === "编辑岗位");
    expect(edit).not.toBeUndefined();
    await act(async () => edit.click());
    const dialog = document.querySelector('[role="dialog"][aria-label="编辑 旧岗位名"]');
    const name = dialog.querySelector('[name="name"]');
    const description = dialog.querySelector('[name="description"]');
    await act(async () => { name.value = "新岗位名"; name.dispatchEvent(new Event("input", { bubbles: true })); description.value = "新说明"; description.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toEqual([{ expected_version: 2, name: "新岗位名", description: "新说明" }]);
    await act(async () => root.unmount());
  });

  it("shows cascade impact and requires the position name before deletion", async () => {
    window.history.replaceState(null, "", "#workflow");
    const calls = [];
    let positions = [{ id: "position_1", name: "测试工程师", description: "", status: "active", version: 4, knowledge_base_ids: [] }];
    let candidates = [{ id: "candidate_1", name: "候选人甲", job_position_id: "position_1", email: "a***@example.com", phone: "138****0000" }];
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions/position_1/deletion-impact")) return new Response(JSON.stringify({ position_id: "position_1", position_name: "测试工程师", position_version: 4, candidate_count: 1, role_requirement_count: 2, plan_count: 3, appointment_count: 4 }));
      if (url.endsWith("/job-positions/position_1") && options.method === "DELETE") {
        calls.push(JSON.parse(options.body));
        positions = [];
        candidates = [];
        return new Response(JSON.stringify({ deleted: true, position_id: "position_1", candidate_count: 1 }));
      }
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: positions }));
      if (url.endsWith("/candidate-profiles")) return new Response(JSON.stringify({ items: candidates }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    let menu;
    for (let attempt = 0; attempt < 30 && !menu; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      menu = host.querySelector(".position-action-menu");
    }
    expect(menu.open).toBe(false);
    await act(async () => menu.querySelector("summary").click());
    const remove = [...menu.querySelectorAll("button")].find((node) => node.textContent === "删除岗位");
    await act(async () => remove.click());
    let dialog;
    for (let attempt = 0; attempt < 30 && !dialog; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      dialog = document.querySelector('[role="dialog"][aria-label="删除岗位 测试工程师"]');
    }
    expect(dialog.textContent).toContain("清除其下 1 位候选人的联系方式、简历、录音、转写和评分数据");
    expect(dialog.textContent).toContain("归档 2 条岗位要求、3 份计划并取消 4 个预约");
    expect(dialog.textContent).toContain("共享题库不会删除");
    const confirmation = dialog.querySelector('[name="confirmation"]');
    await act(async () => { confirmation.value = "测试工程师"; confirmation.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    for (let attempt = 0; attempt < 30 && !calls.length; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toEqual([{ expected_version: 4, confirmation: "测试工程师" }]);
    await act(async () => root.unmount());
  });

  it("drills into one provider for typed model CRUD and confirms provider cascade scope", async () => {
    window.history.replaceState(null, "", "#models");
    const calls = [];
    const provider = { id: "provider_1", provider_id: "mock", display_name: "Mock 厂商", enabled: true, credential_status: "valid", connection_config: {}, version: 3 };
    const model = { id: "model_1", provider_connection_id: provider.id, display_name: "评分模型", model_type: "llm", provider_model_id: "mock-json", enabled: true, status: "ready", supported_capabilities: ["llm.chat_json"], version: 2 };
    globalThis.fetch = async (path, options = {}) => {
      calls.push({ path: String(path), method: options.method || "GET" });
      if (String(path).endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (String(path).endsWith("/model-providers/catalog")) return new Response(JSON.stringify({ items: [] }));
      if (String(path).endsWith("/model-provider-connections")) return new Response(JSON.stringify({ items: [provider] }));
      if (String(path).endsWith("/model-configurations")) return new Response(JSON.stringify({ items: [model] }));
      if (String(path).endsWith("/model-routes")) return new Response(JSON.stringify({ items: [] }));
      if (String(path).includes("model-provider-connections/provider_1?expected_version=3")) return new Response(JSON.stringify({ deleted: true, deleted_model_configuration_ids: [model.id], deleted_model_route_ids: [] }));
      if (String(path).endsWith("/model-provider-connections/provider_1")) return new Response(JSON.stringify(provider));
      return new Response(JSON.stringify({}));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("Mock 厂商"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(host.textContent).toContain("1 个已接入厂商");
    await act(async () => host.querySelector(".model-provider-card").click());
    for (let attempt = 0; attempt < 30 && window.location.hash !== "#models/provider_1"; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(window.location.hash).toBe("#models/provider_1");
    expect(host.textContent).toContain("模型类型");
    expect(host.textContent).toContain("大语言模型");
    expect([...host.querySelectorAll(".provider-model-actions button")].map((node) => node.textContent)).toEqual(["查看", "编辑", "测试", "删除"]);
    await act(async () => [...host.querySelectorAll(".provider-model-actions button")].find((node) => node.textContent === "测试").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.path.endsWith("/model-configurations/model_1/test")); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toContainEqual({ path: "/api/v1/admin/model-configurations/model_1/test", method: "POST" });
    const providerDelete = host.querySelector(".model-provider-detail-header .button-danger");
    await act(async () => providerDelete.click());
    expect(document.body.textContent).toContain("其下 1 个模型");
    const confirm = [...document.querySelectorAll("button")].find((node) => node.textContent === "确认删除");
    await act(async () => confirm.click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "DELETE"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toContainEqual({ path: "/api/v1/admin/model-provider-connections/provider_1?expected_version=3", method: "DELETE" });
    await act(async () => root.unmount());
  });

  it("keeps provider and model display names aligned with the selected resource", async () => {
    window.history.replaceState(null, "", "#models");
    const providers = [
      { provider_id: "mock", display_name: "Mock Provider", implemented: true, connection_form: { fields: [] }, credential_form: { fields: [] } },
      { provider_id: "deepseek", display_name: "DeepSeek", implemented: true, connection_form: { fields: [] }, credential_form: { fields: [] } },
      { provider_id: "zhipuai", display_name: "智谱 BigModel", implemented: true, connection_form: { fields: [] }, credential_form: { fields: [] } },
    ];
    const connections = [
      { id: "deepseek_conn", provider_id: "deepseek", display_name: "DeepSeek 生产连接", enabled: true, credential_status: "valid", version: 1 },
      { id: "zhipu_conn", provider_id: "zhipuai", display_name: "智谱生产连接", enabled: true, credential_status: "valid", version: 1 },
    ];
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/model-providers/catalog")) return new Response(JSON.stringify({ items: providers }));
      if (url.endsWith("/model-provider-connections")) return new Response(JSON.stringify({ items: connections }));
      if (url.endsWith("/model-configurations") || url.endsWith("/model-routes")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/deepseek_conn/model-catalog")) return new Response(JSON.stringify({ model_types: { llm: { label: "大语言模型", selection_mode: "predefined", configuration_form: { fields: [] } } }, models: [{ model_id: "deepseek-chat", model_type: "llm", label: "DeepSeek Chat", default: true }], parameter_forms: { llm: { fields: [] } } }));
      if (url.endsWith("/zhipu_conn/model-catalog")) return new Response(JSON.stringify({ model_types: { llm: { label: "大语言模型", selection_mode: "predefined", configuration_form: { fields: [] } }, tts: { label: "语音合成", selection_mode: "predefined", configuration_form: { fields: [] } } }, models: [{ model_id: "glm-5.2", model_type: "llm", label: "GLM-5.2", default: true }, { model_id: "glm-tts", model_type: "tts", label: "GLM-TTS", default: true }], parameter_forms: { llm: { fields: [] }, tts: { fields: [] } } }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("DeepSeek 生产连接"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent.includes("接入厂商")).click());
    let dialog = document.querySelector('[role="dialog"]');
    const providerSelect = dialog.querySelector("select");
    await act(async () => { providerSelect.value = "mock"; providerSelect.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(dialog.querySelector('input[name="display_name"]').value).toBe("Mock Provider");
    await act(async () => { providerSelect.value = "zhipuai"; providerSelect.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(dialog.querySelector('input[name="display_name"]').value).toBe("智谱 BigModel");
    await act(async () => dialog.querySelector('button[aria-label="关闭"]').click());
    await act(async () => [...host.querySelectorAll(".model-provider-card")].find((node) => node.textContent.includes("智谱生产连接")).click());
    for (let attempt = 0; attempt < 30 && window.location.hash !== "#models/zhipu_conn"; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "添加模型").click());
    for (let attempt = 0; attempt < 30 && !document.body.textContent.includes("GLM-5.2"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    dialog = document.querySelector('[role="dialog"]');
    expect(dialog.textContent).toContain("智谱生产连接");
    expect(dialog.querySelector('input[name="display_name"]').value).toBe("GLM-5.2");
    const typeSelect = [...dialog.querySelectorAll("select")].find((node) => [...node.options].some((option) => option.value === "tts"));
    await act(async () => { typeSelect.value = "tts"; typeSelect.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(dialog.querySelector('input[name="display_name"]').value).toBe("GLM-TTS");
    await act(async () => root.unmount());
  });

  it("derives route capability from purpose and filters incompatible models", async () => {
    window.history.replaceState(null, "", "#models");
    const connection = { id: "provider_1", provider_id: "mock", display_name: "模型连接", enabled: true, credential_status: "valid", version: 1 };
    const models = [
      { id: "tts_1", provider_connection_id: connection.id, display_name: "语音模型", provider_model_id: "mock-tts", enabled: true, status: "ready", supported_capabilities: ["tts.synthesize"] },
      { id: "llm_1", provider_connection_id: connection.id, display_name: "评分模型", provider_model_id: "mock-json", enabled: true, status: "ready", supported_capabilities: ["llm.chat_json"] },
    ];
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/model-providers/catalog")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/model-provider-connections")) return new Response(JSON.stringify({ items: [connection] }));
      if (url.endsWith("/model-configurations")) return new Response(JSON.stringify({ items: models }));
      if (url.endsWith("/model-routes")) return new Response(JSON.stringify({ items: [] }));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("0/7 项核心用途"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent.includes("配置业务用途")).click());
    const dialog = document.querySelector('[role="dialog"]');
    const purpose = dialog.querySelector('select[name="purpose"]');
    expect([...dialog.querySelector('select[name="model_configuration_id"]').options].map((option) => option.value)).toEqual(["tts_1"]);
    await act(async () => { purpose.value = "answer_evaluation"; purpose.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(dialog.textContent).toContain("回答评分");
    expect(dialog.textContent).toContain("结构化文本模型");
    expect([...dialog.querySelector('select[name="model_configuration_id"]').options].map((option) => option.value)).toEqual(["llm_1"]);
    await act(async () => root.unmount());
  });

  it("loads one knowledge base detail and submits a bank-wide TTS rebuild", async () => {
    window.history.replaceState(null, "", "#questions/kb_1");
    const calls = [];
    const bank = { id: "kb_1", name: "Java 题库", job_position_id: "position_1", version: 4, speech_build_status: "ready", speech_profile: null };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path);
      calls.push({ path: url, method: options.method || "GET", body: options.body });
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/questions")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-builds")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-options")) return new Response(JSON.stringify({ current: null, items: [{ id: "tts_1", display_name: "中文 TTS", provider_model_id: "tts-model", voices: [{ voice_profile_id: "voice_a", label: "声音 A" }] }] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-profile") && options.method === "PUT") return new Response(JSON.stringify({ total: 3, status: "pending" }), { status: 202 });
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("Java 题库"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls.some((call) => call.path.endsWith("/workspace/question-catalog"))).toBe(false);
    const configure = [...host.querySelectorAll("button")].find((node) => node.textContent === "配置语音");
    await act(async () => configure.click());
    const submit = [...document.querySelectorAll("button")].find((node) => node.textContent === "保存并重新生成全部语音");
    await act(async () => submit.click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "PUT"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    const update = calls.find((call) => call.method === "PUT");
    expect(JSON.parse(update.body)).toMatchObject({ expected_version: 4, model_configuration_id: "tts_1", voice_profile_id: "voice_a" });
    await act(async () => root.unmount());
  });

  it("keeps speech configuration open when an added TTS model still needs testing", async () => {
    window.history.replaceState(null, "", "#questions/kb_1");
    const bank = { id: "kb_1", name: "语音题库", job_position_id: "position_1", version: 2, speech_build_status: "configuration_required", speech_profile: null };
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/questions") || url.endsWith("/knowledge-bases/kb_1/speech-builds")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-options")) return new Response(JSON.stringify({ current: null, items: [], candidates: [{ id: "tts_1", display_name: "GLM-TTS", provider_model_id: "glm-tts", status: "untested", enabled: true, selectable: false, unavailable_reason: "untested", voices: [{ voice_profile_id: "tongtong", label: "tongtong" }] }] }));
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("语音题库"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    const configure = [...host.querySelectorAll("button")].find((node) => node.textContent === "配置语音");
    expect(configure.disabled).toBe(false);
    await act(async () => configure.click());
    expect(document.body.textContent).toContain("GLM-TTS");
    expect(document.body.textContent).toContain("测试并启用");
    expect(document.querySelector('select[aria-label="TTS 模型"]')?.disabled ?? document.querySelectorAll("select")[0].disabled).toBe(false);
    expect([...document.querySelectorAll("button")].find((node) => node.textContent === "保存并重新生成全部语音").disabled).toBe(true);
    await act(async () => root.unmount());
  });

  it("explains that development mock speech has no playable audio", async () => {
    window.history.replaceState(null, "", "#questions/kb_1");
    const profile = { model_configuration_id: "model_cfg_mock_tts_synthesize", voice_profile_id: "voice_default_cn", language: "zh-CN", audio_format: "audio/wav", speaking_rate: 1, revision: 1, source: "development_mock" };
    const bank = { id: "kb_1", name: "模拟语音题库", job_position_id: "position_1", version: 2, speech_build_status: "ready", speech_profile: profile };
    const question = { id: "q_1", title: "模拟语音题", question_text: "请回答", standard_answer: "答案", key_points: [{ text: "关键点" }], skills: ["测试"], difficulty: "mid", type: "open_ended", validation_status: "valid", speech_status: "ready", speech_asset_id: "speech_mock", version: 1, speech_preview: { available: false, reason: "development_mock_asset", message: "当前是开发模拟语音，没有实际音频。请先配置已测试通过的语音模型和声音。" } };
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/questions")) return new Response(JSON.stringify({ items: [question] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-builds")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-options")) return new Response(JSON.stringify({ current: profile, items: [], candidates: [] }));
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("模拟语音题"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));

    expect(host.textContent).toContain("开发模拟语音（不可试听）");
    expect(host.textContent).toContain("当前题库使用的是开发模拟语音，只用于流程测试，不包含实际音频");
    expect(host.textContent).toContain("当前是开发模拟语音，没有实际音频");
    const preview = [...host.querySelectorAll("button")].find((node) => node.textContent === "试听");
    expect(preview.disabled).toBe(true);
    expect(preview.title).toContain("请先配置已测试通过的语音模型");
    await act(async () => root.unmount());
  });

  it("exposes question preview, view, edit, and delete actions", async () => {
    window.history.replaceState(null, "", "#questions/kb_1");
    const calls = [];
    const profile = { model_configuration_id: "tts_1", voice_profile_id: "voice_a", language: "zh-CN", audio_format: "audio/wav", speaking_rate: 1, revision: 1 };
    const bank = { id: "kb_1", name: "CRUD 题库", job_position_id: "position_1", version: 3, speech_build_status: "ready", speech_profile: profile };
    const question = { id: "q_1", title: "三次握手", question_text: "请解释三次握手", standard_answer: "SYN、SYN-ACK、ACK", key_points: [{ text: "SYN" }], skills: ["网络"], difficulty: "mid", type: "open_ended", validation_status: "valid", speech_status: "ready", speech_asset_id: "speech_1", version: 2 };
    globalThis.Audio = class { pause() {} play() { return Promise.resolve(); } };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path); calls.push({ path: url, method: options.method || "GET" });
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/questions")) return new Response(JSON.stringify({ items: [question] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-builds")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-options")) return new Response(JSON.stringify({ current: profile, items: [{ id: "tts_1", display_name: "TTS", provider_model_id: "tts", selectable: true, voices: [{ voice_profile_id: "voice_a", label: "声音 A" }] }], candidates: [] }));
      if (url.endsWith("/question-speech-assets/speech_1/content-url")) return new Response(JSON.stringify({ url: "/preview.wav", expires_in_seconds: 300 }));
      if (url.endsWith("/questions/q_1") && options.method === "PATCH") return new Response(JSON.stringify({ ...question, version: 3 }));
      if (url.includes("/questions/q_1?expected_version=2") && options.method === "DELETE") return new Response(JSON.stringify({ id: "q_1", deleted: true, status: "archived" }));
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("三次握手"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    for (const label of ["试听", "查看", "编辑", "删除"]) expect([...host.querySelectorAll("button")].some((node) => node.textContent === label)).toBe(true);
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "试听").click());
    expect(calls).toContainEqual({ path: "/api/v1/question-speech-assets/speech_1/content-url", method: "POST" });
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "编辑").click());
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "保存修改").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "PATCH"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toContainEqual({ path: "/api/v1/questions/q_1", method: "PATCH" });
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "删除").click());
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "确认删除").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "DELETE"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toContainEqual({ path: "/api/v1/questions/q_1?expected_version=2", method: "DELETE" });
    await act(async () => root.unmount());
  });

  it("generates reviewable question drafts before an explicit import", async () => {
    window.history.replaceState(null, "", "#questions/kb_1");
    const calls = [];
    const bank = { id: "kb_1", name: "智能题库", job_position_id: "position_1", version: 2, speech_build_status: "configuration_required", speech_profile: null };
    const model = { id: "llm_1", display_name: "生题模型", provider_model_id: "mock-json", status: "ready", selectable: true };
    let batch = null;
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path); calls.push({ path: url, method: options.method || "GET", body: options.body });
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/questions") || url.endsWith("/knowledge-bases/kb_1/speech-builds")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-options")) return new Response(JSON.stringify({ current: null, items: [], candidates: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-options")) return new Response(JSON.stringify({ positioning: "考察线上问题分析", tags: ["python", "数据库"], items: [model], candidates: [model] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-batches") && options.method === "POST") {
        batch = { id: "batch_1", version: 2, target_count: 2, status: "reviewing", available_actions: ["import"], context_snapshot: { positioning: "考察线上问题分析" }, drafts: [{ id: "draft_1", version: 4, title: "慢查询排查", question_text: "如何定位数据库慢查询？", standard_answer: "先观测再分析执行计划", key_points: [{ text: "执行计划" }], skills: ["数据库"], difficulty: "mid", type: "open_ended" }], imported_question_ids: [] };
        return new Response(JSON.stringify(batch), { status: 202 });
      }
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-batches")) return new Response(JSON.stringify({ items: batch ? [batch] : [] }));
      if (url.endsWith("/question-generation-batches/batch_1") && (!options.method || options.method === "GET")) return new Response(JSON.stringify(batch));
      if (url.endsWith("/question-generation-batches/batch_1/drafts/draft_1") && options.method === "PATCH") return new Response(JSON.stringify({ ...batch, version: 3 }));
      if (url.endsWith("/question-generation-batches/batch_1/drafts/draft_1/import") && options.method === "POST") {
        batch = { ...batch, version: 3, drafts: batch.drafts.map((draft) => ({ ...draft, import_status: "importing" })) };
        return new Response(JSON.stringify(batch), { status: 202 });
      }
      if (url.endsWith("/question-generation-batches/batch_1/import") && options.method === "POST") return new Response(JSON.stringify({ ...batch, status: "importing", version: 3 }), { status: 202 });
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("AI 智能生题"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "AI 智能生题").click());
    expect(document.body.textContent).not.toContain("先生成候选题，再由你决定是否入库");
    expect(document.body.textContent).not.toContain("收起配置");
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "新建生题任务").click());
    expect(document.querySelector('[role="dialog"][aria-label="新建生题任务"]')).not.toBeNull();
    expect(document.body.textContent).toContain("先生成候选题，再由你决定是否入库");
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "开始生成候选题").click());
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("慢查询排查"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    const generationCall = calls.find((call) => call.path.endsWith("/question-generation-batches") && call.method === "POST");
    expect(JSON.parse(generationCall.body)).toMatchObject({ model_configuration_id: "llm_1", target_count: 10, positioning: "考察线上问题分析", tags: ["python", "数据库"] });
    expect(host.textContent).toContain("待审核 · 1 道");
    expect(host.textContent).toContain("确认导入题库");
    expect(host.textContent).not.toContain("Worker 子任务");
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "编辑").click());
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "保存候选题").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.method === "PATCH"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls.some((call) => call.path.endsWith("/question-generation-batches/batch_1/drafts/draft_1") && call.method === "PATCH")).toBe(true);
    await act(async () => host.querySelector(".generation-draft-copy").click());
    expect(document.body.textContent).toContain("先观测再分析执行计划");
    expect(document.body.textContent).toContain("评分关键点");
    await act(async () => document.querySelector('button[aria-label="关闭"]').click());
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "单独导入").click());
    expect(document.body.textContent).toContain("只把这一道候选题导入正式题库");
    await act(async () => [...document.querySelectorAll("button")].find((node) => node.textContent === "确认导入这一题").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.path.endsWith("/drafts/draft_1/import") && call.method === "POST"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls.some((call) => call.path.endsWith("/question-generation-batches/batch_1/drafts/draft_1/import") && call.method === "POST")).toBe(true);
    const singleImport = calls.find((call) => call.path.endsWith("/question-generation-batches/batch_1/drafts/draft_1/import") && call.method === "POST");
    expect(JSON.parse(singleImport.body)).toMatchObject({ expected_version: 2, expected_draft_version: 4 });
    await act(async () => root.unmount());
  });

  it("shows compact generation progress without exposing the worker table", async () => {
    window.history.replaceState(null, "", "#questions/kb_1/generation/batch_1");
    const bank = { id: "kb_1", name: "并行生题题库", job_position_id: "position_1", version: 2, speech_build_status: "configuration_required", speech_profile: null };
    const batch = {
      id: "batch_1",
      version: 5,
      target_count: 10,
      status: "generating",
      phase: "generating",
      context_snapshot: { positioning: "生产故障分析" },
      drafts: [],
      generation_progress: {
        phase: "generating",
        planned_count: 10,
        completed_chunks: 2,
        total_chunks: 5,
        completed_slots: 4,
        target_count: 10,
        accepted_count: 0,
        rejected_count: 0,
        refill_round: 0,
      },
    };
    globalThis.fetch = async (path) => {
      const url = String(path);
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-batches")) return new Response(JSON.stringify({ items: [batch] }));
      if (url.endsWith("/question-generation-batches/batch_1")) return new Response(JSON.stringify(batch));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-options")) return new Response(JSON.stringify({ positioning: "生产故障分析", tags: ["可靠性"], items: [], candidates: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/questions") || url.endsWith("/knowledge-bases/kb_1/speech-builds")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/speech-options")) return new Response(JSON.stringify({ current: null, items: [], candidates: [] }));
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("2/5 个子任务完成"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(host.textContent).toContain("已规划 10 个方向");
    const progress = host.querySelector('[role="progressbar"][aria-label="智能生题子任务进度"]');
    expect(progress?.getAttribute("aria-valuenow")).toBe("2");
    expect(progress?.firstElementChild.style.width).toBe("40%");
    expect(host.textContent).not.toContain("Worker 子任务");
    expect(host.querySelector(".generation-task-table")).toBeNull();
    await act(async () => root.unmount());
  });

  it("stops a generation batch through the domain control endpoint", async () => {
    window.history.replaceState(null, "", "#questions/kb_1/generation/batch_1");
    const calls = [];
    const bank = { id: "kb_1", name: "任务控制题库", job_position_id: "position_1", version: 2 };
    const batch = { id: "batch_1", version: 4, execution_revision: 1, target_count: 4, status: "generating", phase: "generating", available_actions: ["stop"], context_snapshot: { positioning: "任务停止测试" }, drafts: [], tasks: [], generation_progress: { phase: "generating", target_count: 4, total_chunks: 2, completed_chunks: 0 } };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path); calls.push({ path: url, method: options.method || "GET", body: options.body });
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-options")) return new Response(JSON.stringify({ positioning: "任务停止测试", tags: ["可靠性"], items: [], candidates: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-batches")) return new Response(JSON.stringify({ items: [batch] }));
      if (url.endsWith("/question-generation-batches/batch_1/stop") && options.method === "POST") return new Response(JSON.stringify({ ...batch, version: 5, status: "stopped", available_actions: ["resume"] }));
      if (url.endsWith("/question-generation-batches/batch_1")) return new Response(JSON.stringify(batch));
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("停止"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "停止").click());
    const dialog = document.querySelector('[role="dialog"]');
    await act(async () => [...dialog.querySelectorAll("button")].find((node) => node.textContent === "停止任务").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.path.endsWith("/stop") && call.method === "POST"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    const stop = calls.find((call) => call.path.endsWith("/stop") && call.method === "POST");
    expect(JSON.parse(stop.body)).toMatchObject({ expected_version: 4 });
    expect(calls.some((call) => call.path.includes("/admin/work-items/"))).toBe(false);
    await act(async () => root.unmount());
  });

  it("retries one failed generation chunk without using generic work replay", async () => {
    window.history.replaceState(null, "", "#questions/kb_1/generation/batch_1");
    const calls = [];
    const bank = { id: "kb_1", name: "失败恢复题库", job_position_id: "position_1", version: 2 };
    const task = { id: "work_2", type: "generate_chunk", chunk_id: "chunk_2", slot_ids: ["slot_03", "slot_04"], status: "failed", execution_revision: 1, attempt_count: 3, max_attempts: 3, retryable: true, error: { code: "provider_timeout", message: "模型请求超时", retryable: true } };
    const batch = { id: "batch_1", version: 6, execution_revision: 1, target_count: 4, status: "failed", phase: "failed", available_actions: ["retry_failed"], context_snapshot: { positioning: "失败恢复测试" }, drafts: [], tasks: [task], generation_progress: { phase: "failed", target_count: 4, total_chunks: 2, completed_chunks: 1 } };
    globalThis.fetch = async (path, options = {}) => {
      const url = String(path); calls.push({ path: url, method: options.method || "GET", body: options.body });
      if (url.endsWith("/auth/session")) return new Response(JSON.stringify({ actor_id: "admin_1", organization_id: "org_1", roles: ["admin"], authenticated: true }));
      if (url.endsWith("/job-positions")) return new Response(JSON.stringify({ items: [{ id: "position_1", name: "后端" }] }));
      if (url.endsWith("/knowledge-bases")) return new Response(JSON.stringify({ items: [bank] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-options")) return new Response(JSON.stringify({ positioning: "失败恢复测试", tags: ["可靠性"], items: [], candidates: [] }));
      if (url.endsWith("/knowledge-bases/kb_1/question-generation-batches")) return new Response(JSON.stringify({ items: [batch] }));
      if (url.endsWith("/question-generation-batches/batch_1/chunks/chunk_2/retry") && options.method === "POST") return new Response(JSON.stringify({ ...batch, version: 7, status: "generating" }), { status: 202 });
      if (url.endsWith("/question-generation-batches/batch_1")) return new Response(JSON.stringify(batch));
      if (url.endsWith("/knowledge-bases/kb_1")) return new Response(JSON.stringify(bank));
      return new Response(JSON.stringify({ items: [] }));
    };
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    await act(async () => root.render(<App />));
    for (let attempt = 0; attempt < 30 && !host.textContent.includes("重试此分片"); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    await act(async () => [...host.querySelectorAll("button")].find((node) => node.textContent === "重试此分片").click());
    const dialog = document.querySelector('[role="dialog"]');
    await act(async () => [...dialog.querySelectorAll("button")].find((node) => node.textContent === "重试这个分片").click());
    for (let attempt = 0; attempt < 30 && !calls.some((call) => call.path.includes("/chunks/chunk_2/retry")); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls.some((call) => call.path.endsWith("/chunks/chunk_2/retry") && call.method === "POST")).toBe(true);
    expect(calls.some((call) => call.path.includes("/admin/work-items/"))).toBe(false);
    await act(async () => root.unmount());
  });
});
