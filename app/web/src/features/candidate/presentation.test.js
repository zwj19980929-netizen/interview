import { describe, expect, it } from "vitest";
import { candidateEntryMessage, candidateNotice } from "./presentation.js";

describe("candidate copy separates actions from diagnostics", () => {
  it.each(["UNDERSTANDING_UNAVAILABLE", "UNDERSTANDING_RETRY_EXHAUSTED", "CAPTURE_RECOVERING", "CAPTURE_RETRY_REQUIRED", "UNEXPECTED_PROVIDER_ERROR"])("does not expose backend details for %s", (code) => {
    const notice = candidateNotice({ code, message: "secret-endpoint WebRTC provider_schema_invalid", recoverable: true });
    expect(JSON.stringify(notice)).not.toMatch(/secret|WebRTC|provider|schema/);
    expect(notice.title).toBeTruthy();
    expect(Boolean(notice.retry)).toBe(code === "UNDERSTANDING_RETRY_EXHAUSTED");
    expect(notice.urgent).not.toBe(true);
  });
  it("shows a stop action for genuine disconnection and claims pause only after acknowledgment", () => {
    const problem = { recoverable: false, message: "internal credential details" };
    expect(candidateNotice(problem).title).toContain("停止作答");
    expect(candidateNotice(problem).title).not.toContain("已暂停");
    expect(candidateNotice({ ...problem, pausePending: true }).title).toBe("正在暂停面试");
    expect(candidateNotice(problem, true).title).toBe("面试已暂停");
    expect(candidateNotice(problem).urgent).toBe(true);
  });
  it("gives useful permission instructions without displaying exception text", () => {
    expect(candidateEntryMessage({ name: "NotAllowedError", message: "secret" })).toContain("请允许");
    expect(candidateEntryMessage(new Error("secret"))).toContain("重新打开邀请链接");
    expect(candidateEntryMessage(new Error("secret"))).not.toContain("secret");
  });
});
