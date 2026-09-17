import { afterEach, describe, expect, it, vi } from "vitest";

import { discardPreparedCandidateMedia, peekPreparedCandidateMedia, runCandidateNetworkCheck, runCandidatePreflight } from "./preflight.js";

afterEach(() => {
  discardPreparedCandidateMedia();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("candidate HTTP connection check", () => {
  it("keeps all four real samples and the existing average and adjacent-difference definitions", async () => {
    let clock = 0;
    const samples = [700, 20, 30, 10];
    const fetchImpl = vi.fn(async () => {
      clock += samples[fetchImpl.mock.calls.length - 1];
      return { ok: true };
    });
    await expect(runCandidateNetworkCheck({ probeUrl: "/healthz?scope=local", fetchImpl, now: () => clock }))
      .resolves.toEqual({ rttMs: 190, jitterMs: 236.7 });
    expect(fetchImpl).toHaveBeenCalledTimes(4);
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([0, 1, 2, 3].map((index) => `/healthz?scope=local&preflight_probe=${index}`));
    expect(fetchImpl.mock.calls.every(([, options]) => options.cache === "no-store" && options.credentials === "same-origin" && options.signal)).toBe(true);
  });

  it.each([{ probeUrl: undefined }, { probeUrl: "/healthz", fetchImpl: null }])("never treats missing measurement inputs as zero latency", async (options) => {
    await expect(runCandidateNetworkCheck(options)).rejects.toMatchObject({ code: "NETWORK_CHECK_UNAVAILABLE" });
  });

  it.each([false, true])("fails safely when the endpoint rejects or the transport throws (throw=%s)", async (throws) => {
    const fetchImpl = vi.fn(async () => {
      if (throws) throw new Error("private upstream detail");
      return { ok: false };
    });
    const error = await runCandidateNetworkCheck({ probeUrl: "/healthz", fetchImpl }).catch((value) => value);
    expect(error.code).toBe("NETWORK_CHECK_FAILED");
    expect(error.message).not.toContain("private");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("bounds a hung fetch even if the fetch implementation ignores abort", async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn(() => new Promise(() => {}));
    const result = runCandidateNetworkCheck({ probeUrl: "/healthz", fetchImpl, timeoutMs: 40 }).catch((error) => error);
    await vi.advanceTimersByTimeAsync(40);
    expect(await result).toMatchObject({ code: "NETWORK_CHECK_TIMEOUT" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels an active request and never samples again after a late response", async () => {
    const controller = new AbortController();
    let resolveFetch;
    const fetchImpl = vi.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));
    const result = runCandidateNetworkCheck({ probeUrl: "/healthz", fetchImpl, signal: controller.signal }).catch((error) => error);
    await Promise.resolve();
    controller.abort();
    expect(await result).toMatchObject({ code: "NETWORK_CHECK_ABORTED" });
    resolveFetch({ ok: true });
    await Promise.resolve();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("does not issue a fetch for an already cancelled check", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchImpl = vi.fn();
    await expect(runCandidateNetworkCheck({ probeUrl: "/healthz", fetchImpl, signal: controller.signal }))
      .rejects.toMatchObject({ code: "NETWORK_CHECK_ABORTED" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("device initialization and network sampling", () => {
  function fixture() {
    let clock = 0;
    let webglFinished = false;
    const stop = vi.fn();
    const track = { readyState: "live", stop };
    const stream = { getAudioTracks: () => [track], getVideoTracks: () => [track], getTracks: () => [track] };
    const mediaDevices = { getUserMedia: vi.fn(async () => stream) };
    const gl = {
      clearColor: vi.fn(), clear: vi.fn(), COLOR_BUFFER_BIT: 1,
      getExtension: () => ({ loseContext: () => { webglFinished = true; } }),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => {
      clock += 1500; // Synchronous first-context cost must not enter an HTTP sample.
      return gl;
    });
    const fetchImpl = vi.fn(async () => {
      expect(webglFinished).toBe(true);
      clock += 10;
      return { ok: true };
    });
    const options = {
      probeUrl: "/healthz", mediaDevices, fetchImpl, now: () => clock,
      frame: (callback) => { clock += 100; callback(clock); }, durationMs: 100,
    };
    return { options, stream, stop, fetchImpl };
  }

  it("finishes WebGL work before measuring the four HTTP samples", async () => {
    const { options, stream, fetchImpl } = fixture();
    const result = await runCandidatePreflight(options);
    expect(result.stream).toBe(stream);
    expect(result.report).toMatchObject({ network_rtt_ms: 10, network_jitter_ms: 0 });
    expect(fetchImpl).toHaveBeenCalledTimes(4);
    expect(peekPreparedCandidateMedia().report.network_rtt_ms).toBe(10);
  });

  it("stops media and never publishes a prepared stream if cancelled during sampling", async () => {
    const { options, stop } = fixture();
    const controller = new AbortController();
    options.fetchImpl = vi.fn(async () => { controller.abort(); return { ok: true }; });
    await expect(runCandidatePreflight({ ...options, signal: controller.signal }))
      .rejects.toMatchObject({ code: "NETWORK_CHECK_ABORTED" });
    expect(stop).toHaveBeenCalled();
    expect(peekPreparedCandidateMedia()).toBeNull();
  });
});
