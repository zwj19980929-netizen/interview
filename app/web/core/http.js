export class HttpError extends Error {
  constructor(message, { status = 0, code = "HTTP_ERROR", details = {}, payload = null } = {}) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.payload = payload;
  }
}

export function createHttpClient({
  tokenStorage = window.sessionStorage,
  tokenKey = "interviewer-access-token",
  fetchImpl = window.fetch.bind(window),
  defaultTimeoutMs = 20000,
  onUnauthorized = () => {},
} = {}) {
  let accessToken = tokenStorage.getItem(tokenKey) || "";

  function setAccessToken(token) {
    accessToken = String(token || "").trim();
    if (accessToken) tokenStorage.setItem(tokenKey, accessToken);
    else tokenStorage.removeItem(tokenKey);
  }

  function clearAccessToken() {
    setAccessToken("");
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeoutMs = options.timeoutMs ?? defaultTimeoutMs;
    let abortReason = "cancelled";
    const cancelFromCaller = () => {
      abortReason = "cancelled";
      controller.abort(options.signal?.reason);
    };
    if (options.signal?.aborted) cancelFromCaller();
    else options.signal?.addEventListener("abort", cancelFromCaller, { once: true });
    const timeout = timeoutMs > 0 ? window.setTimeout(() => {
      abortReason = "timeout";
      controller.abort();
    }, timeoutMs) : null;
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (accessToken && !String(path).startsWith("/api/v1/public/")) {
      headers.Authorization = `Bearer ${accessToken}`;
    }
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    const fetchOptions = {
      method: options.method || "GET",
      headers,
      signal: controller.signal,
    };
    if (options.body !== undefined) {
      if (options.body instanceof FormData) fetchOptions.body = options.body;
      else {
        headers["Content-Type"] = "application/json";
        fetchOptions.body = JSON.stringify(options.body);
      }
    }
    try {
      const response = await fetchImpl(path, fetchOptions);
      const text = await response.text();
      let payload = {};
      if (text) {
        try { payload = JSON.parse(text); } catch { payload = { message: text }; }
      }
      if (!response.ok) {
        const detail = payload.error?.message || payload.detail?.[0]?.msg || payload.message || `请求失败 (${response.status})`;
        const error = new HttpError(detail, {
          status: response.status,
          code: payload.error?.code || "HTTP_ERROR",
          details: payload.error?.details || {},
          payload,
        });
        if (response.status === 401) onUnauthorized(error);
        throw error;
      }
      return payload;
    } catch (error) {
      if (error instanceof HttpError) throw error;
      if (error.name === "AbortError") {
        const timedOut = abortReason === "timeout";
        throw new HttpError(timedOut ? "请求超时" : "请求已取消", {
          code: timedOut ? "REQUEST_TIMEOUT" : "REQUEST_CANCELLED",
        });
      }
      throw new HttpError("无法连接 API 服务", { code: "NETWORK_UNAVAILABLE" });
    } finally {
      if (timeout) window.clearTimeout(timeout);
      options.signal?.removeEventListener("abort", cancelFromCaller);
    }
  }

  return {
    request,
    setAccessToken,
    clearAccessToken,
    getAccessToken: () => accessToken,
  };
}
