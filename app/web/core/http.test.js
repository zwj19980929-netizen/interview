import { expect, it, vi } from "vitest";
import { ReadableStream } from "node:stream/web";
import { TextEncoder } from "node:util";
import { createHttpClient } from "./http.js";

const encoder = new TextEncoder();
const frame = (event, value) => `event: ${event}\ndata: ${JSON.stringify(value)}\n\n`;
function streamed(text, { chunkSize = 7, abortSignal, hold = false } = {}) {
  const bytes = encoder.encode(text);
  let offset = 0;
  return { ok: true, headers: { get: () => "text/event-stream; charset=utf-8" }, body: new ReadableStream({
    start(controller) {
      abortSignal?.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")), { once: true });
    },
    pull(controller) {
      if (offset === bytes.length) { if (!hold) controller.close(); return; }
      controller.enqueue(bytes.slice(offset, offset + chunkSize)); offset = Math.min(bytes.length, offset + chunkSize);
    },
  }) };
}
const storage = () => ({ getItem: () => "token", setItem: vi.fn(), removeItem: vi.fn() });

it("preserves authentication and parses split UTF8 frames without treating progress as a result", async () => {
  const progress = vi.fn();
  const plan = { id: "plan_1", status: "approved", title: "面试计划" };
  const fetchImpl = vi.fn(async () => streamed(": keep-alive\n\n" + frame("progress", { stage: "preparing", completed_points: 2 }) + frame("complete", plan), { chunkSize: 1 }));
  const client = createHttpClient({ fetchImpl, tokenStorage: storage() });
  const result = await client.request("/api/v1/interview-plans/prepare", { method: "POST", headers: { Accept: "text/event-stream" }, body: { candidate_profile_id: "c" }, onProgress: progress });
  expect(result).toEqual(plan);
  expect(progress).toHaveBeenCalledExactlyOnceWith({ stage: "preparing", completed_points: 2 });
  expect(fetchImpl.mock.calls[0][1].headers).toMatchObject({ Authorization: "Bearer token", Accept: "text/event-stream", "Content-Type": "application/json" });
});

it("returns a terminal structured error with its status and safe message", async () => {
  const client = createHttpClient({ tokenStorage: storage(), fetchImpl: async () => streamed(frame("error", {
    status: 503, error: { code: "INQUIRY_UNIT_GENERATION_TIMEOUT", message: "模型整理问题超时", details: {} },
  })) });
  await expect(client.request("/prepare")).rejects.toMatchObject({ code: "INQUIRY_UNIT_GENERATION_TIMEOUT", status: 503, message: "模型整理问题超时" });
});

it.each(["", frame("progress", { stage: "reading" }), "event: complete\ndata: {bad}\n\n", "event: complete\ndata: {}"])("never treats incomplete or malformed stream as success", async (text) => {
  const client = createHttpClient({ tokenStorage: storage(), fetchImpl: async () => streamed(text) });
  await expect(client.request("/prepare")).rejects.toMatchObject({ code: "PROGRESS_STREAM_INTERRUPTED" });
});

it.each(["timeout", "cancel"])("retains %s behavior while reading a pending stream", async (mode) => {
  const controller = new AbortController();
  const client = createHttpClient({ tokenStorage: storage(), fetchImpl: async (_, options) => streamed(frame("progress", { stage: "reading" }), { hold: true, abortSignal: options.signal }) });
  const pending = client.request("/prepare", { signal: controller.signal, timeoutMs: mode === "timeout" ? 5 : 1000 });
  if (mode === "cancel") setTimeout(() => controller.abort(), 1);
  await expect(pending).rejects.toMatchObject({ code: mode === "timeout" ? "REQUEST_TIMEOUT" : "REQUEST_CANCELLED" });
});

it("keeps ordinary JSON errors and unauthorized handling before stream headers", async () => {
  const onUnauthorized = vi.fn();
  const client = createHttpClient({ tokenStorage: storage(), onUnauthorized, fetchImpl: async () => ({ ok: false, status: 401, text: async () => JSON.stringify({ error: { code: "UNAUTHORIZED", message: "请登录" } }) }) });
  await expect(client.request("/prepare", { headers: { Accept: "text/event-stream" } })).rejects.toMatchObject({ status: 401, code: "UNAUTHORIZED" });
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
});
