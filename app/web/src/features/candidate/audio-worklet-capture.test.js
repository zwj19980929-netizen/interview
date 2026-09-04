import { describe, expect, it, vi } from "vitest";

import { createSpeechActivityDetector } from "./audio-worklet-capture.js";

describe("candidate AudioWorklet speech activity detector", () => {
  it("uses sustained audio time instead of render-frame counts", () => {
    const onSpeechStarted = vi.fn();
    const onSpeechStopped = vi.fn();
    const detector = createSpeechActivityDetector({
      speechStartMs: 80,
      speechStopMs: 480,
      onSpeechStarted,
      onSpeechStopped,
    });

    for (let index = 0; index < 7; index += 1) {
      detector.observe({ rms: 0.03, durationMs: 10 });
    }
    expect(onSpeechStarted).not.toHaveBeenCalled();
    detector.observe({ rms: 0.03, durationMs: 10 });
    expect(onSpeechStarted).toHaveBeenCalledOnce();
    expect(detector.speaking).toBe(true);

    for (let index = 0; index < 47; index += 1) {
      detector.observe({ rms: 0, durationMs: 10 });
    }
    expect(onSpeechStopped).not.toHaveBeenCalled();
    detector.observe({ rms: 0, durationMs: 10 });
    expect(onSpeechStopped).toHaveBeenCalledOnce();
    expect(detector.speaking).toBe(false);
  });

  it("rejects speaker echo while preserving a confirmed sub-200ms barge-in", () => {
    const onSpeechStarted = vi.fn();
    const detector = createSpeechActivityDetector({
      isAgentSpeaking: () => true,
      speechThreshold: 0.018,
      agentSpeechThreshold: 0.05,
      speechStartMs: 80,
      agentSpeechStartMs: 160,
      onSpeechStarted,
    });

    for (let index = 0; index < 100; index += 1) {
      detector.observe({ rms: 0.03, durationMs: 10 });
    }
    expect(onSpeechStarted).not.toHaveBeenCalled();

    for (let index = 0; index < 15; index += 1) {
      detector.observe({ rms: 0.08, durationMs: 10 });
    }
    expect(onSpeechStarted).not.toHaveBeenCalled();
    detector.observe({ rms: 0.08, durationMs: 10, feedbackLatencyMs: 4 });
    expect(onSpeechStarted).toHaveBeenCalledOnce();
    expect(onSpeechStarted).toHaveBeenCalledWith(expect.objectContaining({
      agentSpeaking: true,
      feedbackLatencyMs: 4,
    }));
  });
});
