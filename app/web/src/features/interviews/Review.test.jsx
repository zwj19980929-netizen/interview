import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import InterviewsPage from "./Page.jsx";
import InterviewReview from "./Review.jsx";

let workbench, host, root;
vi.mock("../../core/WorkbenchProvider.jsx", () => ({ useWorkbench: () => workbench }));
const monitorOpen = vi.hoisted(() => vi.fn());
vi.mock("./agent-monitor.js", () => ({ createEnterpriseInterviewMonitor: () => ({ open: monitorOpen }) }));
const review = {
  status: "in_progress", interview_id: "synthetic", report: null,
  processing: { submitted_at: "2026-09-09T00:00:00Z", total: 2, completed: 0, failed: 2, pending: 0, can_retry: true, report_status: "failed" },
  recording: { status: "completed", available: true, hash_verified: true },
  turns: [1, 2].map((n) => ({ order: n, turn_id: `turn_${n}`, question: { question_text: `合成题目${n}` },
    answer: { id: `answer_${n}`, final_transcript: `回答原文${n}`, evaluation_status: "failed", evaluation_failure_code: "provider_output_truncated" },
    playback: { audio_available: true, video_available: true, start_seconds: n * 10, end_seconds: n * 10 + 5, timing_source: "server_turn_window" } })),
};
const button = (text) => [...host.querySelectorAll("button")].find((node) => node.textContent === text);
beforeEach(() => {
  monitorOpen.mockReset();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  workbench = { API: "/api/v1", request: vi.fn(), toast: vi.fn(), auth: { roles: ["admin"] }, navigate: vi.fn() };
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.useRealTimers(); });
const show = async (value = review, reload = vi.fn()) => act(async () => root.render(<InterviewReview interviewId="synthetic" review={value} reload={reload} canManage />));

const warningReview = () => ({ ...review,
  processing: { ...review.processing, report_status: "ready", completed: 2, failed: 0 },
  report: { overall_score: 76, score_status: "available", job_fit_level: "match", recognition_notice: "识别不确定性仅作提示" },
  turns: review.turns.map((turn) => ({ ...turn,
    answer: { ...turn.answer, evaluation_status: "completed", stt_confidence: null, stt_confidence_source: "unavailable" },
    evaluation: { id: `eval_${turn.order}`, score: 66, score_status: "available", recognition_warning: true, review_flags: ["transcription_ambiguity"] },
  })),
});

it("shows numeric scores and total even when recognition is uncertain", async () => {
  await show(warningReview());
  expect(host.textContent).not.toContain("总分待核验");
  expect(host.textContent).toContain("评分与依据 · 66 分");
  expect(host.textContent).toContain("回听和纠错均为可选");
  expect(host.textContent).toContain("未知（未提供可用数值）");
  expect(host.textContent).not.toContain("null分");
  expect(host.querySelector(".score-value").textContent).toContain("76");
  expect(button("保存核验并重新评分").disabled).toBe(true);
});

it("requires explicit audio review and submits the selected revision without a client score", async () => {
  const reload = vi.fn();
  workbench.request.mockResolvedValue({ status: "queued" });
  await show(warningReview(), reload);
  const reason = host.querySelector('input[aria-label="核验说明"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(reason, "已回听确认术语");
    reason.dispatchEvent(new Event("input", { bubbles: true }));
    host.querySelector('input[type="checkbox"]').click();
  });
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  const [url, request] = workbench.request.mock.calls[0];
  expect(url).toContain("answers/answer_1/transcription-verification");
  expect(JSON.parse(request.body)).toEqual({ expected_evaluation_id: "eval_1", expected_transcript_revision: 1,
    audio_reviewed: true, reason: "已回听确认术语", final_transcript: "回答原文1" });
  expect(reload).toHaveBeenCalledTimes(1);
});

it("clears the audio review attestation when switching questions", async () => {
  await show(warningReview());
  await act(async () => host.querySelector('input[type="checkbox"]').click());
  await act(async () => button("第 2 题 · 66分").click());
  expect(host.querySelector('input[type="checkbox"]').checked).toBe(false);
  expect(host.querySelector('textarea[aria-label="核验后的转写"]').value).toBe("回答原文2");
});

it("shows failure and uses the selected answer's signed recording", async () => {
  workbench.request.mockResolvedValue({ url: "/api/v1/private-media/synthetic_audio" });
  await show();
  expect(host.textContent).toContain("失败 2 题");
  expect(host.textContent).toContain("评分输出达到模型上限");
  await act(async () => button("第 2 题 · 评分失败").click());
  expect(host.textContent).toContain("回答原文2");
  await act(async () => button("播放本题音频").click());
  expect(workbench.request).toHaveBeenCalledWith("/api/v1/interviews/synthetic/answers/answer_2/audio-url", { method: "POST" });
  expect(host.querySelector("audio").getAttribute("src")).toContain("synthetic_audio");
});

it("drops a signed audio response when the reviewer switches questions", async () => {
  let resolve;
  workbench.request.mockReturnValue(new Promise((done) => { resolve = done; }));
  await show();
  await act(async () => button("播放本题音频").click());
  await act(async () => button("第 2 题 · 评分失败").click());
  await act(async () => resolve({ url: "/api/v1/private-media/old_question" }));
  expect(host.querySelector("audio")).toBeNull();
});

it("seeks video to the question and stops at the end of its window", async () => {
  workbench.request.mockResolvedValue({ url: "/api/v1/private-media/synthetic_video" });
  await show();
  await act(async () => button("第 2 题 · 评分失败").click());
  await act(async () => button("播放本题视频").click());
  const video = host.querySelector("video");
  video.play = vi.fn().mockResolvedValue(undefined); video.pause = vi.fn();
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(video.currentTime).toBe(20);
  video.currentTime = 25;
  await act(async () => video.dispatchEvent(new Event("timeupdate")));
  expect(video.pause).toHaveBeenCalledOnce();
});

it("does not call an unverified recording playable and makes retry idempotent in the UI", async () => {
  let resolve;
  const reload = vi.fn();
  workbench.request.mockReturnValue(new Promise((done) => { resolve = done; }));
  await show({ ...review, recording: { status: "hash_pending", available: false, hash_verified: false } }, reload);
  expect(button("播放整场音视频")).toBeUndefined();
  expect(host.textContent).toContain("尚未通过完整性校验");
  await act(async () => { button("重试未完成处理").click(); button("正在排队…")?.click(); });
  expect(workbench.request).toHaveBeenCalledTimes(1);
  await act(async () => resolve({ status: "queued" }));
  expect(reload).toHaveBeenCalledOnce();
});

it("loads submitted interviews without opening a dead live monitor and refreshes the finished report", async () => {
  vi.useFakeTimers();
  workbench.route = { view: "live" };
  workbench.data = { selectedInterview: { id: "synthetic", status: "in_progress", candidate_input_completed_at: review.processing.submitted_at } };
  workbench.request.mockResolvedValueOnce(review).mockResolvedValue({ ...review, status: "report_ready", processing: { ...review.processing, report_status: "ready", completed: 2, failed: 0 }, report: { overall_score: 85, job_fit_level: "strong_match" } });
  await act(async () => root.render(<InterviewsPage />));
  expect(monitorOpen).not.toHaveBeenCalled();
  expect(host.textContent).not.toContain("等待候选人媒体轨");
  expect(host.textContent).toContain("报告待处理");
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(host.textContent).toContain("报告已生成");
  expect(host.textContent).toContain("高度匹配");
});

it("labels regrading explicitly while retaining the previous evidence for reference", async () => {
  const value = warningReview();
  value.turns[0].answer.evaluation_status = "pending";
  await show(value);
  expect(button("第 1 题 · 正在评分")).toBeTruthy();
  expect(host.textContent).toContain("下方为上一版本的评分依据");
});

it("labels missing resume speech as a system skip without a candidate score", async () => {
  await show({ ...review, turns: [{ turn_id: "resume", order: 7, status: "skipped",
    skip_reason: "resume_speech_not_ready", question: { question_text: "简历项目追问" }, playback: {} }] });
  expect(button("第 7 题 · 语音未就绪，已跳过")).toBeTruthy();
  expect(host.textContent).toContain("不计入评分");
  expect(button("播放本题音频").disabled).toBe(true);
  expect(host.textContent).not.toContain("0分");
});

it("shows fixed competency weights and unassessed scope without pretending a partial report is still scoring", async () => {
  const value = warningReview();
  value.execution_schema_version = 3;
  value.candidate_input_completion_reason = "candidate_requested";
  value.report = { ...value.report, overall_score: null, coverage_status: "insufficient_evidence", job_fit_level: "insufficient_evidence",
    dimension_scores: [
      { dimension: "Python", weight: .75, score: 90, evidence_root_count: 1, evidence_status: "sufficient" },
      { dimension: "数据库", weight: .25, score: null, evidence_root_count: 0, evidence_status: "insufficient_evidence" },
    ] };
  value.turns[0] = { ...value.turns[0], presented_unit_ids: ["complete_question"], assessed_rubric_point_ids: ["p1", "p2"] };
  await show(value);
  expect(host.textContent).toContain("结束原因：候选人主动结束");
  expect(host.textContent).toContain("考察覆盖不足，暂不形成总分");
  expect(host.textContent).not.toContain("正在等待有效评分");
  expect(host.textContent).not.toContain("null分");
  expect(host.textContent).toContain("不分摊到其他能力");
  const table = host.querySelector('section[aria-label="能力维度评分"] table');
  expect(table.textContent).toContain("75%");
  expect(table.textContent).toContain("25%");
  expect(table.textContent).toContain("90 分");
  expect(table.textContent).toContain("未考察");
  expect(host.textContent).toContain("全部 2 项关键点");
  expect(host.querySelector(".score-value")).toBeNull();
});

it("keeps genuine zero competency scores visible and identifies followup evidence", async () => {
  const value = warningReview();
  value.execution_schema_version = 3;
  value.report = { ...value.report, overall_score: 75, coverage_status: "sufficient", dimension_scores: [
    { dimension: "Python", weight: .75, score: 100, evidence_root_count: 2, evidence_status: "sufficient" },
    { dimension: "数据库", weight: .25, score: 0, evidence_root_count: 1, evidence_status: "sufficient" },
  ] };
  value.turns[0].is_followup = true;
  await show(value);
  expect(host.querySelector(".score-value").textContent).toContain("75");
  expect(host.querySelector('section[aria-label="能力维度评分"] table').textContent).toContain("0 分");
  expect(host.textContent).toContain("此追问作为主问题的补充证据");
  expect(host.textContent).not.toContain("考察覆盖不足");
});

it("explains deadline skips and keeps real processing distinct from insufficient coverage", async () => {
  await show({ ...review, execution_schema_version: 3, candidate_input_completion_reason: "appointment_window_expired",
    turns: [{ turn_id: "stopped", order: 2, status: "skipped", skip_reason: "appointment_window_expired", question: { question_text: "未完成题目" }, playback: {} }],
    report: { overall_score: null, score_status: "processing", coverage_status: "insufficient_evidence" } });
  expect(host.textContent).toContain("预约时间已结束");
  expect(host.textContent).toContain("本题没有形成完整回答证据，不计零分");
  expect(button("第 2 题 · 面试结束，未考察")).toBeTruthy();
  expect(host.textContent).toContain("正在重新评分");
});

it("shows scoped short questions and safe committed planning metadata", async () => {
  const value = warningReview();
  value.execution_schema_version = 3;
  value.turns[0] = { ...value.turns[0], inquiry_unit_id: "unit1", presented_unit_ids: ["unit1"], assessed_rubric_point_ids: ["p1"],
    question: { ...value.turns[0].question, not_assessed_rubric_point_ids: ["p2", "p3"] } };
  value.report.scope_notice = "本报告只评价实际呈现的考察单元。";
  value.planning = { decisions: [{ decision_id: "decision1", reason_code: "coverage_gap", model_calls: 2,
    prompt_version: "interviewer_supervisor.v1", tools_used: ["questions.search", "specialists.consult"],
    raw_prompt: "NEVER_RENDER_RAW_MODEL_CONTEXT", raw_response: "NEVER_RENDER_MODEL_RESPONSE" }] };
  await show(value);
  expect(host.textContent).toContain("本题仅考察当前问题对应的 1 项关键点");
  expect(host.textContent).toContain("原题另外 2 项不在本次提问的评分范围内");
  expect(host.textContent).toContain("补充能力证据");
  expect(host.textContent).toContain("检索批准题池、咨询专业证据专家");
  expect(host.textContent).toContain("模型调用 2 次");
  expect(host.textContent).not.toContain("NEVER_RENDER");
});
