// A POST response stream, read through the same authenticated HTTP client.
export async function readProgressStream(response, onProgress, HttpError) {
  const interrupted = () => new HttpError("连接已中断，创建结果尚未确认，请先到计划列表查看后再重试。", { code: "PROGRESS_STREAM_INTERRUPTED" });
  if (!response.body?.getReader) throw interrupted();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      if (buffer.length > 8 * 1024 * 1024) throw interrupted();
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let name = "";
        const lines = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) name = line.slice(6).trim();
          if (line.startsWith("data:")) lines.push(line.slice(5).replace(/^ /, ""));
        }
        if (!lines.length) continue; // Heartbeat; never advances progress.
        const payload = JSON.parse(lines.join("\n"));
        if (name === "complete") return payload;
        if (name === "error") throw new HttpError(payload.error?.message || "创建计划失败，请稍后重试。", {
          status: payload.status || 500, code: payload.error?.code || "HTTP_ERROR",
          details: payload.error?.details || {}, payload,
        });
        if (name === "progress") onProgress?.(payload);
      }
      if (done) throw interrupted(); // EOF is not a successful creation.
    }
  } catch (error) {
    if (error instanceof HttpError || error.name === "AbortError") throw error;
    throw interrupted();
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
