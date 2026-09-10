import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VrmAvatar } from "./VrmAvatar.jsx";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("VRM async lifetime", () => {
  let host, root, props;
  beforeEach(() => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    props = {
      apiBase: "/api/v1", interviewId: "old", candidateSessionToken: "token-old",
      onReady: vi.fn(), onFatalProblem: vi.fn(async () => ({ pauseConfirmed: true })),
      rendererFactory: vi.fn(async () => ({ dispose: vi.fn(), setPerformanceState: vi.fn() })),
    };
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it.each(["success", "failure"])("aborts and ignores a replaced session's late asset %s", async (outcome) => {
    const old = deferred();
    const current = deferred();
    props.verifyAsset = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
    await act(async () => root.render(<VrmAvatar {...props} />));
    const signal = props.verifyAsset.mock.calls[0][0].signal;
    await act(async () => root.render(<VrmAvatar {...props} interviewId="new" candidateSessionToken="token-new" />));
    expect(signal.aborted).toBe(true);
    await act(async () => {
      if (outcome === "success") old.resolve({ id: "old" });
      else old.reject(new Error("旧会话403"));
    });
    expect(props.rendererFactory).not.toHaveBeenCalled();
    expect(props.onFatalProblem).not.toHaveBeenCalled();
    await act(async () => current.resolve({ id: "new" }));
    expect(props.rendererFactory).toHaveBeenCalledOnce();
    expect(props.rendererFactory.mock.calls[0][1].avatarConfig).toEqual({ id: "new" });
  });

  it("disposes a late renderer and ignores its callbacks after navigation", async () => {
    const oldRenderer = deferred();
    const dispose = vi.fn();
    props.verifyAsset = vi.fn(async ({ interviewId }) => ({ id: interviewId }));
    props.rendererFactory.mockReturnValueOnce(oldRenderer.promise);
    await act(async () => root.render(<VrmAvatar {...props} />));
    const oldCallbacks = props.rendererFactory.mock.calls[0][1];
    await act(async () => root.render(<VrmAvatar {...props} interviewId="new" candidateSessionToken="token-new" />));
    await act(async () => {
      oldCallbacks.onReady({ fps: 60 });
      oldCallbacks.onProblem(new Error("旧 WebGL 错误"));
      oldRenderer.resolve({ dispose });
    });
    expect(dispose).toHaveBeenCalledOnce();
    expect(props.onReady).not.toHaveBeenCalled();
    expect(props.onFatalProblem).not.toHaveBeenCalled();
    expect(host.textContent).toContain("面试官正在准备");
  });

  it("continues reporting a current renderer failure and resets its status on navigation", async () => {
    props.verifyAsset = vi.fn().mockRejectedValueOnce(new Error("当前会话授权未通过")).mockReturnValue(new Promise(() => {}));
    await act(async () => root.render(<VrmAvatar {...props} />));
    expect(props.onFatalProblem).toHaveBeenCalledOnce();
    expect(host.textContent).toContain("面试已暂停");
    await act(async () => root.render(<VrmAvatar {...props} interviewId="new" candidateSessionToken="token-new" />));
    expect(host.textContent).toContain("面试官正在准备");
    expect(host.textContent).not.toContain("面试已暂停");
  });
});

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
      signal: expect.any(AbortSignal),
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
    expect(host.textContent).toContain("面试已暂停");
    expect(pause.mock.calls[0][0].candidateProblemCode).toBe("AVATAR_ASSET_UNAVAILABLE");

    await act(async () => root.unmount());
    host.remove();
  });
});
