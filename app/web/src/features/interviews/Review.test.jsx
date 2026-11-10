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
const button = (text) => [...host.querySelectorAll("button")].find((node) => node.textContent.startsWith(text));
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

const unresolvedReview = () => {
  const value = warningReview();
  value.processing = { ...value.processing, report_status: "pending_verification" };
  value.report = { ...value.report, overall_score: null, score_status: "pending_verification", manual_review_required: true,
    job_fit_level: "manual_review", pending_verification_answer_ids: ["answer_1"],
    recognition_notice: "本场存在未确认的回答内容，请回听录音并核验转写后再评分。" };
  value.turns[0].evaluation = { ...value.turns[0].evaluation, score: null, manual_review_required: true };
  return value;
};

it.each(["manual_review_required", "pending_verification_answer_ids"])("explains %s as human transcript verification instead of automatic scoring", async field => {
  const value = unresolvedReview();
  if (field === "manual_review_required") delete value.report.pending_verification_answer_ids;
  else delete value.report.manual_review_required;
  await show(value);
  expect(host.textContent).toContain("回答转写待人工核验");
  expect(host.textContent).toContain(value.report.recognition_notice);
  expect(host.textContent).not.toContain("正在等待有效评分");
  expect(host.textContent).not.toContain("回听和纠错均为可选");
  expect(host.textContent).toContain("回听并核验转写");
  expect(button("第 1 题 · 转写待核验")).toBeTruthy();
  expect(button("播放本题音频").disabled).toBe(false);
  expect(button("保存核验并重新评分").disabled).toBe(true);
  expect(host.querySelector(".score-value")).toBeNull();
  expect(workbench.request).not.toHaveBeenCalled();
});

it("retains unresolved verification notices for readers without granting transcript editing", async () => {
  const value = unresolvedReview();
  await act(async () => root.render(<InterviewReview interviewId="synthetic" review={value} reload={vi.fn()} canManage={false} />));
  expect(host.textContent).toContain("回答转写待人工核验");
  expect(host.textContent).toContain(value.report.recognition_notice);
  expect(button("播放本题音频").disabled).toBe(false);
  expect(host.querySelector(".review-verification")).toBeNull();
  expect(workbench.request).not.toHaveBeenCalled();
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
  expect(host.textContent).toContain("失败 2 份");
  expect(host.textContent).toContain("评分输出达到模型上限");
  await act(async () => button("第 2 题 · 评分失败").click());
  expect(host.textContent).toContain("回答原文2");
  await act(async () => button("播放本题音频").click());
  expect(workbench.request).toHaveBeenCalledWith("/api/v1/interviews/synthetic/answers/answer_2/audio-url", { method: "POST" });
  expect(host.querySelector("audio").getAttribute("src")).toContain("synthetic_audio");
});

it("explains supplier billing failures while preserving the answer and explicit retry", async () => {
  await show({ ...review, turns: review.turns.map((turn) => ({ ...turn,
    answer: { ...turn.answer, evaluation_failure_code: "provider_billing_required" },
  })) });
  expect(host.textContent).toContain("评分模型供应商账户计费或额度需要处理");
  expect(host.textContent).toContain("请管理员处理后重试评分");
  expect(host.textContent).toContain("回答原文1");
  expect(button("重试未完成处理")).toBeTruthy();
  expect(workbench.request).not.toHaveBeenCalled();
});

it("opens each question's review and playback inside that same question card", async () => {
  workbench.request.mockResolvedValue({ url: "/api/v1/private-media/second_answer" });
  await show();
  const first = host.querySelector('[data-turn-id="turn_1"]');
  const second = host.querySelector('[data-turn-id="turn_2"]');
  expect(first.querySelector(".review-answer")).not.toBeNull();
  expect(second.querySelector(".review-answer")).toBeNull();
  expect(second.textContent).toContain("查看回答与回放");
  expect(second.textContent).not.toContain("回答原文2");
  await act(async () => second.querySelector("button").click());
  expect(first.querySelector(".review-answer")).toBeNull();
  expect(second.querySelector(".review-transcript").textContent).toBe("回答原文2");
  expect(second.querySelectorAll(".review-answer button")).toHaveLength(2);
  await act(async () => button("播放本题音频").click());
  expect(second.querySelector("audio").getAttribute("src")).toContain("second_answer");
  expect(first.querySelector("audio")).toBeNull();
  await act(async () => second.querySelector("button").click());
  expect(second.querySelector(".review-answer")).toBeNull();
  expect(host.querySelector("audio")).toBeNull();
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

it.each([
  ["configuration", "媒体存储配置待修复", false],
  ["retry_exhausted", "自动重试次数已用完", true],
  ["integrity", "录像完整性或访问保护校验未通过", false],
  ["provider", "录像服务暂不可用", true],
  ["unexpected_private_error", "请管理员检查后恢复封存", false],
])("shows a safe %s recording failure instead of an endless finishing state", async (reason, text, canRetry) => {
  await show({ ...review, processing: { ...review.processing, can_retry: false },
    recording: { status: "stopping", processing_status: "failed", available: false, hash_verified: false,
      failure_reason: reason, can_retry: canRetry, failure_code: "NEVER_RENDER_INTERNAL_CODE",
      raw_error: "NEVER_RENDER_SECRET_PATH" },
    turns: [{ ...review.turns[0], playback: { audio_available: true, video_available: false } }],
  });
  expect(host.textContent).toContain("录像封存失败");
  expect(host.textContent).toContain(text);
  expect(host.textContent).not.toContain("录像正在收尾");
  expect(host.textContent).not.toContain("NEVER_RENDER");
  expect(host.textContent).not.toContain("unexpected_private_error");
  expect(!!button("重试未完成处理")).toBe(canRetry);
  expect(button("播放整场音视频")).toBeUndefined();
  expect(button("播放本题音频").disabled).toBe(false);
  expect(workbench.request).not.toHaveBeenCalled();
});

it("honors an explicit recording retry prohibition while genuine sealing is in progress", async () => {
  await show({ ...review, processing: { ...review.processing, can_retry: false },
    recording: { status: "hash_pending", processing_status: "processing", available: false, hash_verified: false, can_retry: false },
  });
  expect(host.textContent).toContain("录像正在封存校验");
  expect(host.textContent).not.toContain("录像封存失败");
  expect(button("重试未完成处理")).toBeUndefined();
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

it("shows the live followup over a stale REST question and hides captions belonging to the old turn", async () => {
  workbench.route = { view: "live" };
  workbench.data = { selectedInterview: { id: "synthetic", status: "in_progress", current_turn_id: "old",
    turns: [{ id: "old", order: 1, question_spoken_text: "REST旧主问题" }] } };
  workbench.request.mockResolvedValue({ ...review, processing: { report_status: "awaiting_submission" } });
  let publish;
  const state = { connection: { control: "connected", media: "connected" }, session: { status: "in_progress", current_turn_id: "child",
    current_question: { turn_id: "child", question_text: "实时追问", is_followup: true } }, floor: "candidate", takeover: null,
    captions: [{ text: "旧回答字幕", turnId: "old" }], acts: [], candidateStream: null, problem: null };
  monitorOpen.mockResolvedValue({ close: vi.fn(), subscribe: (callback) => { publish = callback; callback(state); return () => {}; } });
  await act(async () => root.render(<InterviewsPage />));
  expect(host.querySelector(".question-panel h2").textContent).toBe("实时追问");
  expect(host.querySelector(".question-panel").textContent).not.toContain("旧回答字幕");
  await act(async () => publish({ ...state, captions: [...state.captions, { text: "当前追问字幕", turnId: "child" }] }));
  expect(host.querySelector(".question-panel").textContent).toContain("当前追问字幕");
  await act(async () => publish({ ...state, session: { ...state.session, current_turn_id: null, current_question: null } }));
  expect(host.querySelector(".question-panel h2").textContent).toBe("当前没有待答题目");
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
  expect(host.textContent).toContain("结束原因：候选人主动提前退出");
  expect(host.textContent).toContain("提前退出待人工复核，最终决定由企业人员完成。");
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

it("keeps confirmed withdrawal separate from scores and displays the human review notice", async () => {
  const value = warningReview();
  value.candidate_input_completion_reason = "candidate_requested";
  value.report.scope_notice = "候选人确认提前退出；待人工复核。已取得的评分证据保留，未考察范围不计零分。";
  await show(value);
  expect(host.textContent).toContain("候选人确认提前退出；待人工复核");
  expect(host.textContent).toContain("提前退出待人工复核，最终决定由企业人员完成。");
  expect(host.querySelector(".score-value").textContent).toContain("76");
  expect(host.textContent).not.toContain("已淘汰");
});

it("shows withdrawal awaiting human review while the report is still processing", async () => {
  await show({ ...review, report: null, candidate_input_completion_reason: "candidate_requested",
    processing: { ...review.processing, failed: 0, pending: 2, report_status: "processing" } });
  expect(host.textContent).toContain("结束原因：候选人主动提前退出");
  expect(host.textContent).toContain("提前退出待人工复核，最终决定由企业人员完成。");
  expect(host.querySelector(".score-value")).toBeNull();
});

it.each([
  [{ trigger: "time_limit", limit_minutes: 45 }, "达到 45 分钟时长上限"],
  [{ trigger: "primary_question_limit", primary_question_limit: 10 }, "达到 10 道主问题上限"],
  [{ trigger: "candidate_pool_exhausted" }, "本场可用问题已问完"],
])("distinguishes the recorded completion limit %j", async (completion, label) => {
  await show({ ...warningReview(), candidate_input_completion_reason: "budget_exhausted", completion });
  expect(host.textContent).toContain(`结束原因：${label}`);
  expect(host.textContent).not.toContain("考察时间或题数预算已用完");
});

it("keeps an unknown historical budget trigger ambiguous instead of inferring a new ten-question limit", async () => {
  await show({ ...warningReview(), candidate_input_completion_reason: "budget_exhausted",
    completion: { trigger: "unrecognized_private_cause", primary_question_limit: 10 } });
  expect(host.textContent).toContain("考察时间或题数预算已用完");
  expect(host.textContent).not.toContain("达到 10 道主问题上限");
  expect(host.textContent).not.toContain("unrecognized_private_cause");
});

it("keeps actual main and followup counts distinct from scored answer counts", async () => {
  await show({ ...warningReview(), candidate_input_completion_reason: "budget_exhausted",
    completion: { trigger: "time_limit", limit_minutes: 45, elapsed_seconds: 2751 },
    question_counts: { primary: 14, followup: 2 },
    processing: { report_status: "ready", total: 16, completed: 16, pending: 0, failed: 0 },
    turns: [...review.turns, { turn_id: "never_presented", order: 17, status: "pending", playback: {} }],
  });
  expect(host.textContent).toContain("已提问 14 道主问题 · 2 道追问");
  expect(host.textContent).toContain("本场用时 45 分 51 秒");
  expect(host.textContent).toContain("已评分 16 / 16 份回答");
  expect(host.textContent).not.toContain("已评分 16 / 16 题");
  expect(host.textContent).not.toContain("17 道主问题");
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

const frozenScope = () => ({ schema_version: "session_assessment_scope.v1", policy: "budgeted_core_competencies.v1",
  selected_units: Array.from({ length: 9 }, (_, index) => ({ question_id: `q_${index}`, inquiry_unit_id: `unit_${index}` })),
  source_question_counts: { position_bank: 6, resume_experience: 3 },
  included_competency_ids: ["服务可靠性", "项目复盘"], excluded_competency_ids: ["底层编译原理"],
  available_competency_ids: ["服务可靠性", "项目复盘", "底层编译原理"], estimated_required_seconds: 1800,
});

it("labels the new score against its frozen session scope and distinguishes the unused bank scope", async () => {
  const value = warningReview();
  value.report = { ...value.report, score_scope: "session_assessment", assessment_scope: frozenScope() };
  await show(value);
  expect(host.textContent).toContain("本场考察综合分");
  const scope = host.querySelector('[aria-label="本场事前约定的考察范围"]');
  expect(scope.textContent).toContain("岗位题 6 道 · 简历题 3 道");
  expect(scope.textContent).toContain("服务可靠性、项目复盘");
  expect(scope.textContent).toContain("未纳入本场考察的能力（1 项）");
  expect(scope.textContent).toContain("底层编译原理");
  expect(scope.textContent).toContain("题库其他范围不计入本场综合分");
  expect(host.querySelector(".score-value").textContent).toContain("76");
  expect(workbench.request).not.toHaveBeenCalled();
});

it.each([
  [{ position_bank: 8 }, "岗位题 8 道 · 简历题 0 道"],
  [{ resume_experience: 8 }, "岗位题 0 道 · 简历题 8 道"],
])("displays an absent source as zero in the frozen scope", async (sources, text) => {
  const value = warningReview();
  value.report = { ...value.report, score_scope: "session_assessment",
    assessment_scope: { ...frozenScope(), source_question_counts: sources } };
  await show(value);
  expect(host.querySelector('[aria-label="本场事前约定的考察范围"]').textContent).toContain(text);
});

it("does not convert historical partial coverage into a score or relabel its original contract", async () => {
  const value = warningReview();
  value.report = { ...value.report, overall_score: null, coverage_status: "insufficient_evidence",
    assessment_coverage: { covered_competencies: 24, total_competencies: 41 } };
  await show(value);
  expect(host.textContent).toContain("考察覆盖不足，暂不形成总分");
  expect(host.textContent).not.toContain("本场考察综合分");
  expect(host.querySelector('[aria-label="本场事前约定的考察范围"]')).toBeNull();
  expect(host.querySelector(".score-value")).toBeNull();
  expect(host.textContent).not.toContain("58.5");
});

it("keeps a new session score unavailable until the frozen scope has sufficient evidence", async () => {
  const value = warningReview();
  value.report = { ...value.report, overall_score: null, coverage_status: "insufficient_evidence",
    score_scope: "session_assessment", assessment_scope: frozenScope() };
  await show(value);
  expect(host.textContent).toContain("本场约定范围证据不足，暂不形成综合分");
  expect(host.querySelector(".score-value")).toBeNull();
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

it("nests persisted followups under their parent with separate answers and their own playback", async () => {
  const parent = { ...warningReview().turns[0], root_turn_id: "turn_1", parent_turn_id: null, question_spoken_text: "实际主问题" };
  const child = { ...warningReview().turns[1], is_followup: true, root_turn_id: "turn_1", parent_turn_id: "turn_1", followup_depth: 1,
    question_spoken_text: "实际追问", answer: { ...warningReview().turns[1].answer, final_transcript: "只属于追问的回答" } };
  workbench.request.mockResolvedValue({ url: "/api/v1/private-media/child_answer" });
  await show({ ...warningReview(), execution_schema_version: 3, turns: [parent, child] });
  const childNode = host.querySelector('[data-turn-id="turn_1"] [data-turn-id="turn_2"]');
  expect(childNode).not.toBeNull();
  expect(childNode.textContent).toContain("实际追问");
  expect(childNode.textContent).not.toContain("只属于追问的回答");
  expect(childNode.textContent).not.toContain("回答原文1");
  await act(async () => childNode.querySelector("button").click());
  expect(childNode.querySelector(".review-answer")).not.toBeNull();
  expect(host.querySelector(".review-answer h3").textContent).toBe("实际追问");
  expect(host.querySelector(".review-answer").textContent).toContain("所属主问题：实际主问题");
  expect(host.querySelector(".review-transcript").textContent).toBe("只属于追问的回答");
  expect(host.querySelector(".review-answer").textContent).toContain("不重复计权");
  await act(async () => button("播放本题音频").click());
  expect(workbench.request).toHaveBeenCalledWith("/api/v1/interviews/synthetic/answers/answer_2/audio-url", { method: "POST" });
});
