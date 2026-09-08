import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { AnswerRecoveryAction, CandidateSignalList, ConversationState, SpeechPlaybackState, WarmupPanel } from "./Page.jsx";

describe("candidate automatic answer completion", () => {
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
    expect(host.textContent).toContain("已收到结束确认，正在整理回答");
    expect(host.textContent).toContain("不需要重复确认");
    expect(host.textContent).toContain("仍可直接开口");
    expect(host.textContent).not.toContain("停止收音");
    expect(host.textContent).not.toContain("暂停");

    await act(async () => root.render(<ConversationState
      phase="listening" endpoint={{ active: true, deadlineAt: null }} formal
    />));
    expect(host.textContent).toContain("停顿5秒后，面试官会询问是否补充");
    expect(host.textContent).not.toContain("请点击");
    expect(host.textContent).not.toContain("回答完毕");

    await act(async () => root.render(<ConversationState
      phase="understanding" endpoint={{ active: false, deadlineAt: null }} formal
    />));
    expect(host.textContent).toContain("正在处理已提交的回答");
    expect(host.textContent).toContain("本段已停止收音");
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
    expect(host.textContent).toContain("试音静音后将自动收口");
    expect(host.textContent).toContain("继续说话会取消本次收口");
    await act(async () => root.unmount());
    host.remove();
  });
});

describe("candidate capture recovery presentation", () => {
  it("separates automatic repair and same-question retry from a human safety pause", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      for (const [phase, text] of [
        ["answer_recovering", "正在恢复语音识别，音频仍在保留"],
        ["answer_retry_required", "本题收音未能恢复，请重试本题"],
      ]) {
        await act(async () => root.render(<ConversationState phase={phase} endpoint={{ active: true, deadlineAt: 2500 }} formal />));
        expect(host.textContent).toContain(text);
        expect(host.textContent).not.toContain("面试已暂停");
        expect(host.textContent).not.toContain("人工");
        expect(host.textContent).not.toContain("试音静音后");
      }
      expect(host.textContent).toContain("不会提交不完整回答");
      await act(async () => root.render(<ConversationState phase="paused" endpoint={{ active: true, deadlineAt: null }} formal />));
      expect(host.textContent).toContain("面试已暂停");
      expect(host.textContent).not.toContain("仍在收音");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });

  it("cannot show green live-transcription indicators for paused or retry-required capture", async () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const staleSignals = { microphone: { localDetected: true, level: 0.8 }, serverAudio: { received: true }, captions: { forming: true } };
    try {
      for (const phase of ["paused", "answer_retry_required", "completed", "understanding"]) {
        await act(async () => root.render(<CandidateSignalList experience={{ ...staleSignals, phase }} />));
        expect(host.querySelectorAll(".is-active")).toHaveLength(0);
        expect(host.textContent).not.toContain("服务端正在转写");
        expect(host.querySelector(".microphone-level i").style.width).toBe("0%");
      }
      await act(async () => root.render(<CandidateSignalList experience={{ ...staleSignals, phase: "answer_recovering", captureRecovery: { status: "recovering" } }} />));
      expect(host.textContent).toContain("正在恢复语音识别");
      expect(host.textContent).toContain("音频仍在保留");
      expect(host.textContent).not.toContain("服务端正在转写");
      await act(async () => root.render(<CandidateSignalList experience={{ ...staleSignals, phase: "listening" }} blocked />));
      expect(host.querySelectorAll(".is-active")).toHaveLength(0);
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
      expect(button.textContent).toContain("正在重新开启本题收音");
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

    expect(host.querySelector(".transcript-toolbar")?.textContent).toContain("完整服务端字幕");
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
