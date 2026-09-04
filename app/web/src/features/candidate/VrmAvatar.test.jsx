import React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { VrmAvatar } from "./VrmAvatar.jsx";


describe("candidate VRM authorization bootstrap", () => {
  it("authorizes the asset with the candidate session before starting the renderer", async () => {
    const config = {
      asset_url: "/api/v1/public/interviews/iv_1/avatar-model?grant=signed",
      asset_sha256: "a".repeat(64),
    };
    const verifyAsset = vi.fn(async () => config);
    const dispose = vi.fn();
    const rendererFactory = vi.fn(async (_canvas, options) => {
      options.onReady({ sha256: config.asset_sha256 });
      return { dispose, setPerformanceState: vi.fn() };
    });
    const onReady = vi.fn();
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    await act(async () => {
      root.render(<VrmAvatar
        apiBase="/api/v1"
        interviewId="iv_1"
        candidateSessionToken="candidate-session-token"
        avatar={{ status: "idle" }}
        verifyAsset={verifyAsset}
        rendererFactory={rendererFactory}
        onReady={onReady}
      />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(verifyAsset).toHaveBeenCalledWith({
      apiBase: "/api/v1",
      interviewId: "iv_1",
      candidateSessionToken: "candidate-session-token",
    });
    expect(rendererFactory).toHaveBeenCalledWith(
      expect.any(HTMLCanvasElement),
      expect.objectContaining({ avatarConfig: config }),
    );
    expect(onReady).toHaveBeenCalledOnce();

    await act(async () => root.unmount());
    expect(dispose).toHaveBeenCalledOnce();
    host.remove();
  });

  it("claims a server pause only after the fatal report is confirmed", async () => {
    const pause = vi.fn(async () => ({ pauseConfirmed: true }));
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    await act(async () => {
      root.render(<VrmAvatar
        apiBase="/api/v1"
        interviewId="iv_1"
        candidateSessionToken="candidate-session-token"
        avatar={{ status: "idle" }}
        verifyAsset={vi.fn(async () => { throw new Error("资产加载失败"); })}
        onFatalProblem={pause}
      />);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(pause).toHaveBeenCalledOnce();
    expect(host.textContent).toContain("面试已在服务器暂停");
    expect(pause.mock.calls[0][0].candidateProblemCode).toBe("AVATAR_ASSET_UNAVAILABLE");

    await act(async () => root.unmount());
    host.remove();
  });
});
