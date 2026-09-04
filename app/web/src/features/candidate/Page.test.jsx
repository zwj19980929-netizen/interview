import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { WarmupPanel } from "./Page.jsx";

describe("candidate warm-up recovery", () => {
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
