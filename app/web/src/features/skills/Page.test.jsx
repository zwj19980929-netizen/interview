import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SkillsPage from "./Page.jsx";
import { skillsFeature } from "./index.js";

let wb, root, host, server;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => wb }));
const emptyCompany = () => ({ company_name: "", business_overview: "", products_services: "", additional_info: "" });
const make = (version = 3, instructions = "# 我的方法\n先聊实际项目。", companyName = "示例企业") => ({
  version, skill: instructions ? { skill_id: "skill_1", name: "默认面试方法", instructions, revision: 2, status: "active" } : null,
  company_profile: { ...emptyCompany(), company_name: companyName, business_overview: companyName ? "提供企业软件服务。" : "" }, updated_at: null,
});
const button = (label) => [...host.querySelectorAll("button")].find((node) => node.textContent === label);
const click = (label) => act(async () => button(label).click());
const field = (name) => host.querySelector(`[name="${name}"]`);
const render = () => act(async () => root.render(<SkillsPage />));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const enter = (name, value) => act(async () => {
  const input = field(name);
  const prototype = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value").set.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});
const submit = (name) => act(async () => host.querySelector(`form[aria-label="${name}"]`).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
const patches = () => wb.request.mock.calls.filter(([, options]) => options?.method === "PATCH");
const persist = (body) => {
  if (body.expected_version !== server.version) throw Object.assign(new Error("conflict"), { status: 409, code: "PERSISTENCE_CONFLICT" });
  server = { ...server, version: server.version + 1,
    ...(Object.hasOwn(body, "skill_instructions") ? { skill: body.skill_instructions.trim() ? {
      skill_id: "skill_1", name: "默认面试方法", instructions: body.skill_instructions, revision: (server.skill?.revision || 0) + 1, status: "active",
    } : null } : {}), ...(body.company_profile ? { company_profile: { ...body.company_profile } } : {}),
  };
  return structuredClone(server);
};
beforeEach(() => {
  server = make();
  wb = { API: "/api/v1", auth: { roles: ["interviewer"], organization_id: "org_a", actor_id: "interviewer_a" },
    request: vi.fn(async (_path, options) => options?.method === "PATCH" ? persist(options.body) : structuredClone(server)) };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });

it("presents two optional modules with page-local navigation and no selection or approval workflow", async () => {
  await render();
  expect(skillsFeature.label).toBe("面试定制");
  expect(host.querySelector("h1").textContent).toBe("面试定制");
  expect([...host.querySelectorAll("h2")].map((item) => item.textContent)).toEqual(["Skill 定制", "企业资料"]);
  expect(host.textContent).toContain("保存后，新面试默认使用；未填写则不使用");
  expect(host.textContent).toContain("只填写可以向候选人介绍的信息");
  expect(field("skill_instructions").value).toBe(server.skill.instructions);
  expect(field("company_name").value).toBe("示例企业");
  expect(field("skill_instructions").required).toBe(false);
  expect(field("skill_instructions").maxLength).toBe(6000);
  expect(field("name")).toBeNull(); expect(host.querySelector("select")).toBeNull();
  expect(wb.request).toHaveBeenCalledWith("/api/v1/interview-customization");
  expect(host.textContent).not.toContain("审批"); expect(host.textContent).not.toContain("创建 Skill");
  const hash = window.location.hash;
  await click("企业资料");
  expect(window.location.hash).toBe(hash);
  expect(document.activeElement).toBe(host.querySelector("#customization-company-title"));
});

it("does not fetch or expose forms to a user without permission", async () => {
  wb.auth.roles = ["reviewer"]; await render();
  expect(host.textContent).toContain("需要面试配置权限");
  expect(host.querySelector("form")).toBeNull(); expect(wb.request).not.toHaveBeenCalled();
});

it("saves free Markdown without overwriting the company draft and then saves that module at the next version", async () => {
  await render();
  const markdown = "  # 我的方法\n\n- 一次只问一个重点。\n```python\npass\n```\n参考：https://example.com/interview\n";
  await enter("skill_instructions", markdown); await enter("company_name", "尚未保存的企业名称"); await enter("additional_info", "尚未保存的补充");
  await click("保存 Skill");
  expect(patches()).toEqual([["/api/v1/interview-customization", { method: "PATCH", body: { expected_version: 3, skill_instructions: markdown } }]]);
  expect(field("company_name").value).toBe("尚未保存的企业名称"); expect(field("additional_info").value).toBe("尚未保存的补充");
  expect(server.company_profile.company_name).toBe("示例企业");
  expect(host.textContent).toContain("Skill 已保存，新面试会默认使用");
  await click("保存企业资料");
  expect(patches()[1][1].body).toEqual({ expected_version: 4, company_profile: {
    company_name: "尚未保存的企业名称", business_overview: "提供企业软件服务。", products_services: "", additional_info: "尚未保存的补充",
  } });
});

it("saves company data while retaining the unsaved Skill draft", async () => {
  await render(); await enter("skill_instructions", "还没保存的 Skill"); await enter("products_services", "订单履约平台"); await click("保存企业资料");
  expect(patches()[0][1].body).toEqual({ expected_version: 3, company_profile: { ...make().company_profile, products_services: "订单履约平台" } });
  expect(field("skill_instructions").value).toBe("还没保存的 Skill"); expect(server.skill.instructions).toBe(make().skill.instructions);
  expect(host.textContent).toContain("企业资料已保存，新面试会默认使用");
});

it("allows empty initial configuration and independent clearing", async () => {
  server = make(0, "", ""); await render();
  expect([...host.querySelectorAll(".customization-state")].every((node) => node.textContent === "未设置")).toBe(true);
  await enter("skill_instructions", "\n  "); await click("保存 Skill");
  expect(patches()[0][1].body).toEqual({ expected_version: 0, skill_instructions: "\n  " });
  expect(host.textContent).toContain("新面试不使用 Skill 定制");
  await click("保存企业资料");
  expect(patches()[1][1].body).toEqual({ expected_version: 1, company_profile: emptyCompany() });
  expect(host.textContent).toContain("新面试不使用企业资料");
});

it("clears saved company data without clearing the current Skill", async () => {
  await render(); await enter("company_name", ""); await enter("business_overview", ""); await click("保存企业资料");
  expect(server.company_profile).toEqual(emptyCompany()); expect(server.skill.instructions).toBe(make().skill.instructions);
  expect(patches()[0][1].body).not.toHaveProperty("skill_instructions");
});

it.each(["retired", "revoked"])("keeps inactive %s Skill content editable without claiming it is used", async (status) => {
  server.skill.status = status; await render();
  expect(field("skill_instructions").value).toBe(server.skill.instructions);
  expect(host.textContent).toContain("当前 Skill 已停用，新面试暂不使用；保存后会重新启用");
  expect(host.querySelector(".customization-state").textContent).toBe("已停用");
  await click("保存 Skill"); expect(server.skill.status).toBe("active");
  expect(host.querySelector(".customization-state").textContent).toBe("已设置");
});

it("requires an explicit update after conflict and never replaces local drafts when reading latest", async () => {
  await render(); await enter("skill_instructions", "我尚未保存的 Skill"); await enter("company_name", "我尚未保存的企业名称");
  server = make(9, "他人最新保存的 Skill", "他人最新保存的企业名称");
  await click("保存 Skill"); expect(host.textContent).toContain("保存内容已发生变化");
  expect(button("保存 Skill").disabled).toBe(true); expect(button("保存企业资料").disabled).toBe(true);
  expect(patches()).toHaveLength(1);
  await click("读取最新版本");
  expect(field("skill_instructions").value).toBe("我尚未保存的 Skill"); expect(field("company_name").value).toBe("我尚未保存的企业名称");
  expect(host.textContent).toContain("他人最新保存的 Skill"); expect(host.textContent).toContain("他人最新保存的企业名称");
  expect(host.textContent).toContain("选择“用当前内容更新”会替换对应部分"); expect(patches()).toHaveLength(1);
  await click("用当前内容更新 Skill");
  expect(patches()[1][1].body).toEqual({ expected_version: 9, skill_instructions: "我尚未保存的 Skill" });
  expect(server.company_profile.company_name).toBe("他人最新保存的企业名称"); expect(field("company_name").value).toBe("我尚未保存的企业名称");
  expect(button("用当前内容更新企业资料").disabled).toBe(false);
});

it("shows safe validation feedback while retaining failed edits", async () => {
  const original = wb.request;
  wb.request = vi.fn(async (path, options) => {
    if (options?.method === "PATCH") throw Object.assign(new Error("Request validation failed."), { status: 422, details: { fields: [
      { message: "Value error, 企业资料连同栏目标题不能超过 12000 字。" }, { message: {} },
    ] } });
    return original(path, options);
  });
  await render(); await enter("company_name", "我新填写的企业名称"); await click("保存企业资料");
  expect(host.textContent).toContain("企业资料连同栏目标题不能超过 12000 字"); expect(host.textContent).not.toContain("[object Object]");
  expect(field("company_name").value).toBe("我新填写的企业名称"); expect(button("保存企业资料").disabled).toBe(false); expect(patches()).toHaveLength(1);
});

it("validates Skill length and company title overhead before submission", async () => {
  server = make(0, "", ""); await render();
  await enter("skill_instructions", "字".repeat(6001)); await submit("Skill 定制");
  expect(host.textContent).toContain("Skill 正文最多 6000 字");
  await enter("company_name", "字".repeat(12000)); await submit("企业资料");
  expect(host.textContent).toContain("企业资料连同栏目标题不能超过 12000 字"); expect(patches()).toHaveLength(0);
});

it("prevents duplicate or overlapping saves but allows drafting the other module", async () => {
  const pending = deferred(); wb.request = vi.fn((_path, options) => options?.method === "PATCH" ? pending.promise : Promise.resolve(structuredClone(server)));
  await render(); await enter("skill_instructions", "正在保存的 Skill");
  await submit("Skill 定制"); await submit("Skill 定制"); await submit("企业资料"); expect(patches()).toHaveLength(1);
  expect(field("company_name").disabled).toBe(false); await enter("company_name", "等待期间填写的企业名称");
  await act(async () => pending.resolve(persist(patches()[0][1].body)));
  expect(field("company_name").value).toBe("等待期间填写的企业名称"); expect(button("保存企业资料").disabled).toBe(false);
});

it("does not retry uncertain saves or discard their edits", async () => {
  const original = wb.request;
  wb.request = vi.fn(async (path, options) => {
    if (options?.method === "PATCH") throw Object.assign(new Error("timeout"), { code: "REQUEST_TIMEOUT", status: 0 });
    return original(path, options);
  });
  await render(); await enter("skill_instructions", "可能已经保存的修改"); await click("保存 Skill");
  expect(host.textContent).toContain("保存结果尚未确认"); expect(field("skill_instructions").value).toBe("可能已经保存的修改");
  expect(button("保存 Skill").disabled).toBe(true); expect(patches()).toHaveLength(1);
});

it("recovers an initial load failure without saving against an unknown version", async () => {
  let reads = 0; wb.request = vi.fn(async () => { if (++reads === 1) throw new Error("unavailable"); return structuredClone(server); });
  await render(); expect(host.textContent).toContain("面试定制暂时无法读取"); expect(button("保存 Skill").disabled).toBe(true);
  await click("重新读取"); expect(field("skill_instructions").value).toBe(server.skill.instructions);
  expect(button("保存 Skill").disabled).toBe(false); expect(patches()).toHaveLength(0);
});

it("ignores an old tenant's late read", async () => {
  const first = deferred();
  wb.request = vi.fn(() => wb.auth.organization_id === "org_a" ? first.promise : Promise.resolve(make(5, "组织乙的 Skill", "组织乙")));
  await render(); wb.auth = { ...wb.auth, organization_id: "org_b" }; await render();
  expect(field("skill_instructions").value).toBe("组织乙的 Skill");
  await act(async () => first.resolve(make(99, "组织甲的迟到内容", "组织甲")));
  expect(field("skill_instructions").value).toBe("组织乙的 Skill"); expect(host.textContent).not.toContain("组织甲");
});

it("ignores a save finishing after the actor changes", async () => {
  const pending = deferred(); wb.request = vi.fn((_path, options) => options?.method === "PATCH" ? pending.promise : Promise.resolve(structuredClone(server)));
  await render(); await enter("skill_instructions", "前一用户的修改"); await click("保存 Skill");
  server = make(5, "当前用户的内容", "当前企业"); wb.auth = { ...wb.auth, actor_id: "interviewer_b" }; await render();
  await act(async () => pending.resolve(make(4, "前一用户的修改", "旧企业")));
  expect(field("skill_instructions").value).toBe("当前用户的内容"); expect(field("company_name").value).toBe("当前企业");
  expect(host.textContent).not.toContain("Skill 已保存");
});

it("drops late load results when permission is removed", async () => {
  const pending = deferred(); wb.request = vi.fn(() => pending.promise);
  await render(); wb.auth.roles = ["reviewer"]; await render(); await act(async () => pending.resolve(make()));
  expect(host.textContent).toContain("需要面试配置权限"); expect(host.querySelector("textarea")).toBeNull();
});
