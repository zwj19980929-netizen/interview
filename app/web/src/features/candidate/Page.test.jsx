import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { AnswerRecoveryAction, CandidateProgress, CandidateSignalList, CaptionPanel, ConversationState, SpeechPlaybackState, WarmupPanel } from "./Page.jsx";

describe("candidate tentative transcript", () => {
  it("keeps a stopped partial tentative and visible when expanded until the server replaces it", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const tentative = { text: "合成临时识别。对。", final: false };
    try {
      for (const forming of [true, false]) {
        for (const expanded of [false, true]) {
          await act(async () => root.render(<CaptionPanel captions={{ forming, recent: [tentative], full: [] }} expanded={expanded} />));
          expect(host.querySelector(".transcript-toolbar").textContent).toContain("暂定转写");
          expect(host.querySelector(".transcript-toolbar").textContent).not.toContain("完整回答");
          expect(host.querySelector(".candidate-live-captions .is-partial").textContent).toBe(tentative.text);
        }
      }
      const final = { text: "合成最终识别。", final: true };
      await act(async () => root.render(<CaptionPanel captions={{ forming: false, recent: [tentative], full: [final] }} expanded />));
      expect([...host.querySelectorAll(".candidate-live-captions p")].map(row => row.textContent)).toEqual([tentative.text]);
      await act(async () => root.render(<CaptionPanel captions={{ forming: false, recent: [final], full: [final] }} expanded />));
      expect(host.querySelector(".transcript-toolbar").textContent).toContain("完整回答");
      expect(host.textContent).not.toContain("暂定转写");
      expect(host.textContent).not.toContain(tentative.text);
      expect(host.querySelector(".candidate-live-captions .is-final").textContent).toBe(final.text);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
});

describe("candidate automatic answer completion", () => {
  it("asks about leaving the whole interview while continuing to receive a spoken reply", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      await act(async () => root.render(<ConversationState phase="awaiting_exit_confirmation"
        endpoint={{ active: false }} speechDetected formal />));
      expect(host.textContent).toContain("请确认是否结束本次面试");
      expect(host.textContent).toContain("确认退出，或告诉面试官继续面试");
      expect(host.textContent).toContain("确认前不会结束本次面试");
      expect(host.textContent).not.toContain("还有什么需要补充");
      expect(host.textContent).not.toContain("面试已完成");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("offers replay of only the current blocked speech and distinguishes listening from detected speech", async () => {
    const host = document.createElement("div"); document.body.append(host);
    const root = createRoot(host);
    const replay = vi.fn();
    try {
      await act(async () => root.render(<SpeechPlaybackState playback={{ status: "blocked", message: "浏览器阻止了语音播放" }} act={replay} />));
      await act(async () => host.querySelector("button").click());
      expect(replay).toHaveBeenCalledWith("retry_speech");
      await act(async () => root.render(<ConversationState phase="listening" endpoint={{ active: false }} speechDetected={false} formal />));
      expect(host.textContent).toContain("等待你开口");
      expect(host.textContent).not.toContain("正在听你说");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
  it("explains cancellable preparation without claiming capture has stopped", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<ConversationState
      phase="answer_preparing" endpoint={{ active: false, deadlineAt: null }} formal
    />));
    expect(host.textContent).toContain("正在整理你的回答");
    expect(host.textContent).toContain("不需要重复确认");
    expect(host.textContent).toContain("仍可直接开口");
    expect(host.textContent).not.toContain("停止收音");
    expect(host.textContent).not.toContain("暂停");

    await act(async () => root.render(<ConversationState
      phase="listening" endpoint={{ active: true, deadlineAt: null }} formal
    />));
    expect(host.textContent).toContain("需要思考、想换个话题或已经讲完");
    expect(host.textContent).not.toContain("5秒");
    expect(host.textContent).not.toContain("请点击");
    expect(host.textContent).not.toContain("回答完毕");

    await act(async () => root.render(<ConversationState
      phase="understanding" endpoint={{ active: false, deadlineAt: null }} formal
    />));
    expect(host.textContent).toContain("正在整理你的回答");
    expect(host.textContent).toContain("这段回答已收好");
    await act(async () => root.unmount());
    host.remove();
  });

  it("keeps warm-up's automatic-silence guidance separate from formal answers", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => root.render(<ConversationState
      phase="listening" endpoint={{ active: true, deadlineAt: 2500 }}
    />));
    expect(host.textContent).toContain("说完后稍等片刻，就能查看试音字幕");
    expect(host.textContent).toContain("说完后稍等片刻");
    await act(async () => root.unmount());
    host.remove();
  });
});

describe("candidate capture recovery presentation", () => {
  it.each(["retry_required", "retrying"])("keeps the microphone indicator waiting while follow-up audio is %s", async status => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      await act(async () => root.render(<CandidateSignalList experience={{
        phase: status === "retrying" ? "preparing" : "question_audio_retry_required",
        questionAudioRecovery: { status, turn_id: "turn_followup", selection_event_id: "selection_failed" },
        microphone: { localDetected: true, level: 0.8 }, evidence: { ready: false },
      }} />));
      expect(host.textContent).toContain("请先听完追问，再开始作答");
      expect(host.textContent).not.toMatch(/正在收到你的声音|麦克风正在连接|面试已暂停/);
      expect(host.querySelectorAll(".is-active")).toHaveLength(0);
      expect(host.querySelector(".microphone-level i").style.width).toBe("0%");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
  it("shows live warm-up input and explains why capture is waiting after the trial", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const experience = { phase: "listening", calibration: { status: "listening" }, microphone: { localDetected: true, level: 0.5 } };
    try {
      await act(async () => root.render(<CandidateSignalList experience={experience} />));
      expect(host.textContent).toContain("正在收到你的声音");
      expect(host.querySelectorAll(".is-active")).toHaveLength(1);
      expect(host.querySelector(".microphone-level i").style.width).toBe("50%");
      for (const [phase, status, detail] of [
        ["understanding", "awaiting_confirmation", "试音已收好，请确认下方字幕"],
        ["understanding", "confirming", "正在进入正式面试"],
        ["understanding", "completed", "这段回答已收好，面试官正在整理"],
        ["planning", "completed", "面试官正在准备下一话题"],
        ["paused", "listening", "面试已暂停"],
        ["completed", "completed", "面试已结束"],
      ]) {
        await act(async () => root.render(<CandidateSignalList experience={{ ...experience, phase, calibration: { status } }}
          planningContext={{ session: { completed_answers: 1 } }} />));
        expect(host.textContent).toContain(detail);
        expect(host.textContent).not.toContain("暂未收音");
        expect(host.querySelectorAll(".is-active")).toHaveLength(0);
        expect(host.querySelector(".microphone-level i").style.width).toBe("0%");
      }
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("separates automatic repair and same-question retry from a human safety pause", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      for (const [phase, text] of [
        ["answer_recovering", "连接有些慢，正在恢复"],
        ["answer_retry_required", "这段回答需要重试"],
      ]) {
        await act(async () => root.render(<ConversationState phase={phase} endpoint={{ active: true, deadlineAt: 2500 }} formal />));
        expect(host.textContent).toContain(text);
        expect(host.textContent).not.toContain("面试已暂停");
        expect(host.textContent).not.toContain("人工");
        expect(host.textContent).not.toContain("试音静音后");
      }
      expect(host.textContent).toContain("点击“重试本题”后，再回答一次");
      await act(async () => root.render(<ConversationState phase="paused" endpoint={{ active: true, deadlineAt: null }} formal />));
      expect(host.textContent).toContain("面试已暂停");
      expect(host.textContent).not.toContain("仍在收音");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("cannot show green live-transcription indicators for paused or retry-required capture", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const staleSignals = { microphone: { localDetected: true, level: 0.8 }, serverAudio: { received: true }, captions: { forming: true } };
    try {
      for (const phase of ["paused", "answer_retry_required", "completed", "understanding", "planning"]) {
        await act(async () => root.render(<CandidateSignalList experience={{ ...staleSignals, phase }} />));
        expect(host.querySelectorAll(".is-active")).toHaveLength(0);
        expect(host.textContent).not.toContain("服务端正在转写");
        expect(host.querySelector(".microphone-level i").style.width).toBe("0%");
      }
      await act(async () => root.render(<CandidateSignalList experience={{ ...staleSignals, phase: "answer_recovering", captureRecovery: { status: "recovering" } }} />));
      expect(host.textContent).toContain("连接有些慢，正在恢复");
      expect(host.textContent).toContain("已收到的回答会保留");
      expect(host.textContent).not.toContain("服务端正在转写");
      await act(async () => root.render(<CandidateSignalList experience={{ ...staleSignals, phase: "listening" }} blocked />));
      expect(host.querySelectorAll(".is-active")).toHaveLength(0);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("keeps the microphone available for a delivered clarification without making a completion claim", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const experience = { phase: "listening", evidence: { ready: true },
      problem: { code: "UNDERSTANDING_CLARIFICATION_REQUIRED", recoverable: true, action: "retry_answer_processing" },
      microphone: { localDetected: false, level: 0.4 } };
    try {
      await act(async () => root.render(<CandidateSignalList experience={experience} />));
      expect(host.textContent).toContain("麦克风已开启，可随时说话");
      expect(host.textContent).not.toMatch(/暂未就绪|已结束|已提交|已暂停/);
      await act(async () => root.render(<CandidateSignalList experience={{ ...experience,
        microphone: { ...experience.microphone, localDetected: true } }} />));
      expect(host.textContent).toContain("正在收到你的声音");
      expect(host.querySelectorAll(".is-active")).toHaveLength(1);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("prevents duplicate retries while sending and until the runtime clears pending", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    let resolve;
    const retry = vi.fn(() => new Promise((done) => { resolve = done; }));
    try {
      await act(async () => root.render(<AnswerRecoveryAction recovery={{ status: "retry_required", retryPending: false }} act={retry} />));
      const button = host.querySelector("button");
      await act(async () => { button.click(); button.click(); });
      expect(retry).toHaveBeenCalledExactlyOnceWith("continue_speaking");
      expect(button.disabled).toBe(true);
      expect(button.textContent).toContain("正在重试…");
      await act(async () => root.render(<AnswerRecoveryAction recovery={{ status: "retry_required", retryPending: true }} act={retry} />));
      await act(async () => resolve());
      expect(button.disabled).toBe(true);
      await act(async () => root.render(<AnswerRecoveryAction recovery={{ status: "retry_required", retryPending: false }} act={retry} />));
      expect(button.disabled).toBe(false);
      await act(async () => root.render(<AnswerRecoveryAction recovery={{ status: "retry_required", retryPending: false }} act={retry} disabled />));
      expect(button.disabled).toBe(true);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
});

describe("candidate warm-up recovery", () => {
  it("disables confirmation and retry while the trial is paused", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host); const send = vi.fn();
    try {
      await act(async () => root.render(<WarmupPanel calibration={{ status: "awaiting_confirmation" }}
        experience={{ phase: "paused", endpoint: { active: false }, captions: { recent: [], full: [] } }} act={send} />));
      expect(host.querySelectorAll("button")).toHaveLength(2);
      for (const button of host.querySelectorAll("button")) {
        expect(button.disabled).toBe(true); await act(async () => button.click());
      }
      expect(send).not.toHaveBeenCalled();
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
  it("labels the expanded warm-up transcript as complete", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    await act(async () => root.render(<WarmupPanel
      calibration={{ status: "awaiting_confirmation" }}
      experience={{
        phase: "understanding",
        endpoint: { active: false, deadlineAt: null },
        captions: {
          forming: false,
          recent: [{ text: "最后一句", final: true }],
          full: [
            { text: "第一句", final: true },
            { text: "最后一句", final: true },
          ],
        },
        problem: null,
      }}
      act={vi.fn()}
    />));

    expect(host.querySelector(".transcript-toolbar")?.textContent).toContain("完整回答");
    expect([...host.querySelectorAll(".candidate-live-captions p")].map((item) => item.textContent)).toEqual([
      "第一句",
      "最后一句",
    ]);

    await act(async () => root.unmount());
    host.remove();
  });

  it("offers an explicit retry only after the server marks warm-up retryable", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    const retry = vi.fn();
    const baseExperience = {
      phase: "preparing",
      endpoint: { active: false, deadlineAt: null },
      captions: { forming: false, recent: [], full: [] },
      problem: null,
    };

    await act(async () => root.render(<WarmupPanel
      calibration={{ status: "retrying" }}
      experience={baseExperience}
      act={retry}
    />));
    expect([...host.querySelectorAll("button")]).toHaveLength(0);

    await act(async () => root.render(<WarmupPanel
      calibration={{ status: "retrying", retryRequired: true }}
      experience={baseExperience}
      act={retry}
    />));
    expect([...host.querySelectorAll("button")].map((item) => item.textContent)).toEqual([
      "重新试音",
    ]);

    await act(async () => root.render(<WarmupPanel
      calibration={{ status: "retrying" }}
      experience={{
        ...baseExperience,
        problem: { recoverable: true, action: "retry_warmup" },
      }}
      act={retry}
    />));
    const button = [...host.querySelectorAll("button")].find(
      (item) => item.textContent === "重新试音",
    );
    expect(button).toBeTruthy();
    await act(async () => button.click());
    expect(retry).toHaveBeenCalledWith("warmup.retry");

    await act(async () => root.unmount());
    host.remove();
  });
});

describe("adaptive interview progress", () => {
  it("keeps planning neutral when HTTP has selected the first question ahead of the WebSocket snapshot", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const planningContext = { session: { completed_answers: 0, current_turn_id: null }, history: {
      current_turn_id: "first", turns: [{ id: "first", status: "asking", question_spoken_text: "合成第一题？" }], answers: [],
    } };
    try {
      await act(async () => root.render(<>
        <ConversationState phase="planning" endpoint={{ active: false }} formal planningContext={planningContext} />
        <CandidateSignalList experience={{ phase: "planning", microphone: { localDetected: false, level: 0 } }}
          planningContext={planningContext} />
      </>));
      expect(host.textContent).toContain("正在准备面试问题");
      expect(host.textContent).not.toMatch(/下一话题|第一题|回答已保留/);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("describes first-question preparation without inventing a retained answer", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const planningContext = { session: { completed_answers: 0 }, history: { turns: [], answers: [] } };
    try {
      await act(async () => root.render(<>
        <ConversationState phase="planning" endpoint={{ active: false }} formal planningContext={planningContext} />
        <CandidateSignalList experience={{ phase: "planning", session: planningContext.session,
          calibration: { status: "completed" }, microphone: { localDetected: true, level: 0.8 } }}
          planningContext={planningContext} />
      </>));
      expect(host.textContent).toContain("正在准备第一题");
      expect(host.textContent).toContain("试音已完成，请稍等面试官开始提问");
      expect(host.textContent).not.toMatch(/下一话题|回答已保留|正在收到你的声音/);
      expect(host.querySelectorAll(".is-active")).toHaveLength(0);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("keeps a previously presented unanswered topic separate from a retained answer", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const planningContext = { session: { completed_answers: 0 }, history: {
      turns: [{ id: "presented", is_followup: false, status: "completed", question_spoken_text: "合成已呈现问题？" }], answers: [],
    } };
    try {
      await act(async () => root.render(<ConversationState phase="planning" endpoint={{ active: false }} formal
        planningContext={planningContext} />));
      expect(host.textContent).toContain("正在准备下一话题");
      expect(host.textContent).not.toMatch(/第一题|回答已保留/);
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("shows completed topics without inventing a total and keeps legacy progress", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      await act(async () => root.render(<CandidateProgress answered={3} totalQuestions={null} adaptive />));
      expect(host.textContent).toBe("已完成 3 个话题");
      expect(host.textContent).not.toContain("/");
      expect(host.querySelector("[aria-label]").getAttribute("aria-label")).toBe("已完成 3 个话题");
      await act(async () => root.render(<CandidateProgress answered={3} totalQuestions={6} />));
      expect(host.textContent).toBe("3/6");
      await act(async () => root.render(<ConversationState phase="planning" endpoint={{ active: false }} formal
        planningContext={{ session: { completed_answers: 3 } }} />));
      expect(host.textContent).toContain("正在准备下一话题");
      expect(host.textContent).toContain("这段回答已保留");
      expect(host.textContent).not.toContain("已完成");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
});
