import { describe, expect, it } from "vitest";
import { candidateEntryMessage, candidateEntryFailure, candidateNotice } from "./presentation.js";

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
    expect(candidateEntryMessage(new Error("secret"))).toContain("请稍后重试");
    expect(candidateEntryMessage(new Error("secret"))).not.toContain("secret");
  });
});

it("routes a planning failure to its own retry and keeps diagnostics private", () => {
  const notice = candidateNotice({ recoverable: true, action: "retry_planning", code: "PLAN_ERROR", message: "secret prompt provider" });
  expect(notice).toMatchObject({ retry: true, action: "planning.retry" });
  expect(notice.detail).toContain("回答已保留");
  expect(JSON.stringify(notice)).not.toMatch(/secret|prompt|provider/);
});


it.each([
  ["INVITATION_REGISTRATION_REQUIRED", "请先确认本次预约"],
  ["CONSENT_REQUIRED", "请确认本次面试授权"],
  ["AUDIO_RECORDING_CONSENT_REQUIRED", "请确认本次面试授权"],
  ["VIDEO_RECORDING_CONSENT_REQUIRED", "请确认本次面试授权"],
  ["APPOINTMENT_TOO_EARLY", "尚未到入场时间"],
  ["APPOINTMENT_WINDOW_CLOSED", "本次预约时间已结束"],
  ["APPOINTMENT_DEVICE_NOT_READY", "设备检查尚未通过"],
  ["APPOINTMENT_NOT_READY", "面试服务尚未准备好"],
  ["INVITATION_EXPIRED", "邀请链接已过期"],
  ["NETWORK_UNAVAILABLE", "入场请求未能连接服务"],
  ["REQUEST_TIMEOUT", "入场检查超时"],
  ["unexpected", "暂时无法完成入场检查"],
])("provides safe actionable entry copy for %s", (code, title) => {
  const result = candidateEntryFailure({ code, message: "secret provider diagnostics", details: { secret: true } });
  expect(result.title).toBe(title);
  expect(JSON.stringify(result)).not.toMatch(/secret|provider/);
  expect(Boolean(result.invalidateNetwork)).toBe(["NETWORK_UNAVAILABLE", "REQUEST_TIMEOUT"].includes(code));
});
