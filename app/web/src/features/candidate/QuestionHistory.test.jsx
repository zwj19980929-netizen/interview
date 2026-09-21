import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CandidateQuestionContext, CandidateQuestionHistory, useCandidateQuestionHistory } from "./QuestionHistory.jsx";

let host, root;
beforeEach(() => { host = document.createElement("div"); document.body.append(host); root = createRoot(host); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.useRealTimers(); });
const history = { id: "iv", turns: [
  { id: "parent", order: 1, question_spoken_text: "如何发布？" },
  { id: "child", order: 2, is_followup: true, parent_turn_id: "parent", root_turn_id: "parent", question_spoken_text: "怎样回滚？" },
  { id: "child2", order: 3, is_followup: true, parent_turn_id: "child", root_turn_id: "parent", question_spoken_text: "回滚失败呢？" },
  { id: "future", order: 4 },
], answers: [
  { turn_id: "parent", final_transcript: "主回答：滚动发布。" },
  { turn_id: "child", final_transcript: "子回答：恢复版本。" },
] };

it("shows an active nested followup with parent context and separately scoped persisted answers", async () => {
  const currentQuestion = { turn_id: "child2", is_followup: true, question_text: "回滚失败呢？" };
  await act(async () => root.render(<><CandidateQuestionContext history={history} currentQuestion={currentQuestion} />
    <CandidateQuestionHistory history={history} currentQuestion={currentQuestion} /></>));
  expect(host.querySelector(".candidate-parent-question").textContent).toContain("如何发布？");
  expect(host.querySelector("details").open).toBe(true);
  const parentAnswer = host.querySelector('[data-turn-id="parent"] > .question-record-answer');
  const childAnswer = host.querySelector('[data-turn-id="child"] > .question-record-answer');
  expect(parentAnswer.textContent).toContain("主回答");
  expect(parentAnswer.textContent).not.toContain("子回答");
  expect(childAnswer.textContent).toContain("子回答");
  expect(childAnswer.textContent).not.toContain("主回答");
  expect(host.querySelector('[data-turn-id="child"] [data-turn-id="child2"]')).not.toBeNull();
  expect(host.querySelector('[data-turn-id="child2"]').textContent).toContain("当前追问");
  expect(host.querySelector('[data-turn-id="future"]')).toBeNull();
});

it("lets the candidate collapse the current topic without unrelated renders reopening it", async () => {
  const currentQuestion = { turn_id: "child2", is_followup: true, question_text: "回滚失败呢？" };
  await act(async () => root.render(<CandidateQuestionHistory history={history} currentQuestion={currentQuestion} />));
  const details = host.querySelector("details");
  expect(details.open).toBe(true);
  await act(async () => details.querySelector("summary").click());
  expect(details.open).toBe(false);
  await act(async () => root.render(<CandidateQuestionHistory history={{ ...history }} currentQuestion={{ ...currentQuestion }} />));
  expect(details.open).toBe(false);
  await act(async () => details.querySelector("summary").click());
  expect(details.open).toBe(true);
  expect(details.textContent).toContain("子回答");
});

it("distinguishes a submitted answer awaiting text from current capture and a question without an answer", async () => {
  const retry = vi.fn();
  const value = { turns: [
    { id: "submitted", order: 1, question_spoken_text: "已回答的问题？" },
    { id: "capturing", order: 2, question_spoken_text: "正在回答的问题？" },
    { id: "unanswered", order: 3, question_spoken_text: "未回答的问题？" },
  ], answers: [{ id: "answer", turn_id: "submitted", evaluation_status: "completed" }] };
  const currentQuestion = { turn_id: "capturing", question_text: "正在回答的问题？" };
  await act(async () => root.render(<CandidateQuestionHistory history={value} currentQuestion={currentQuestion} retry={retry} />));
  expect(host.querySelector('[data-turn-id="submitted"]').textContent).toContain("回答已提交，文字记录暂未同步。");
  expect(host.querySelector('[data-turn-id="submitted"]').textContent).not.toContain("尚未形成已提交回答");
  expect(host.querySelector('[data-turn-id="capturing"]').textContent).toContain("正在交流，当前字幕显示在上方。");
  expect(host.querySelector('[data-turn-id="unanswered"]').textContent).toContain("尚未形成已提交回答。");
  await act(async () => host.querySelector("button").click());
  expect(retry).toHaveBeenCalledTimes(1);
  await act(async () => root.render(<CandidateQuestionHistory history={{ ...value, answers: [{ ...value.answers[0], final_transcript: "同步后的权威回答。" }] }} currentQuestion={currentQuestion} retry={retry} />));
  expect(host.querySelector('[data-turn-id="submitted"]').textContent).toContain("同步后的权威回答。");
  expect(host.textContent).not.toContain("文字记录暂未同步");
  expect(host.querySelector("button")).toBeNull();
});

it.each(["", "   ", null])("keeps the submitted state for an active answer with unavailable text (%s)", async final_transcript => {
  const value = { turns: [{ id: "active", order: 1, question_spoken_text: "当前问题？" }], answers: [{ id: "answer", turn_id: "active", final_transcript }] };
  await act(async () => root.render(<CandidateQuestionHistory history={value} currentQuestion={{ turn_id: "active", question_text: "当前问题？" }} />));
  expect(host.textContent).toContain("回答已提交，文字记录暂未同步。");
  expect(host.textContent).not.toContain("正在交流，当前字幕显示在上方。");
  expect(host.textContent).not.toContain("尚未形成已提交回答。");
});

function Harness(props) {
  const value = useCandidateQuestionHistory(props);
  return <CandidateQuestionHistory {...value} currentQuestion={props.currentQuestion} />;
}
const base = { apiBase: "/api/v1", interview: history, token: "synthetic-token", enabled: true, currentTurnId: "parent", finalRevision: "", completedAnswers: 0, completed: false };

it("refreshes only at question/answer boundaries and ignores a superseded REST response", async () => {
  vi.useFakeTimers();
  let oldResolve;
  const request = vi.fn().mockImplementationOnce(() => new Promise(resolve => { oldResolve = resolve; })).mockResolvedValue(history);
  await act(async () => root.render(<Harness {...base} request={request} />));
  await act(async () => vi.advanceTimersByTimeAsync(80));
  expect(request.mock.calls[0][1].headers).toEqual({ "X-Candidate-Session-Token": "synthetic-token" });
  await act(async () => root.render(<Harness {...base} request={request} currentTurnId="child" />));
  expect(request.mock.calls[0][1].signal.aborted).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(80));
  await act(async () => oldResolve({ id: "iv", turns: [{ id: "stale", question_spoken_text: "过时结果" }], answers: [] }));
  expect(host.textContent).not.toContain("过时结果");
  for (let frame = 0; frame < 10; frame++) {
    await act(async () => root.render(<Harness {...base} request={request} currentTurnId="child" partialText={`实时字幕${frame}`} />));
    await act(async () => vi.advanceTimersByTimeAsync(100));
  }
  expect(request).toHaveBeenCalledTimes(2);
  await act(async () => root.render(<Harness {...base} request={request} currentTurnId="child" finalRevision="完整子回答" />));
  await act(async () => vi.advanceTimersByTimeAsync(80));
  expect(request).toHaveBeenCalledTimes(3);
});

it("keeps the existing history after a failed or mismatched response and offers explicit retry", async () => {
  vi.useFakeTimers();
  const request = vi.fn().mockResolvedValueOnce({ id: "another", turns: [{ id: "foreign", question_spoken_text: "其他场次" }] }).mockResolvedValue(history);
  await act(async () => root.render(<Harness {...base} request={request} />));
  await act(async () => vi.advanceTimersByTimeAsync(80));
  expect(host.textContent).toContain("暂未同步");
  expect(host.textContent).not.toContain("其他场次");
  expect(host.textContent).toContain("主回答");
  await act(async () => host.querySelector("button").click());
  await act(async () => vi.advanceTimersByTimeAsync(80));
  expect(host.textContent).not.toContain("暂未同步");
});
