// Minimal fetch wrapper. Adds the dev Bearer token and JSON content-type.
// Keep this tiny — M1 has exactly one POST /runs endpoint.

const TOKEN = import.meta.env.VITE_DEV_AUTH_TOKEN;

// In dev, Vite proxies `/api/*` → VITE_GATEWAY_HTTP. In build previews without
// the proxy, we fall back to the full URL so the app still works.
const BASE =
  import.meta.env.DEV ? "/api" : (import.meta.env.VITE_GATEWAY_HTTP ?? "");

export interface HttpError extends Error {
  status: number;
  body: unknown;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers ?? {});
  headers.set("accept", "application/json");
  if (init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (TOKEN) headers.set("authorization", `Bearer ${TOKEN}`);

  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  const text = await res.text();
  const body = text ? safeJson(text) : null;

  if (!res.ok) {
    const err = new Error(
      `HTTP ${res.status} ${res.statusText} for ${path}`,
    ) as HttpError;
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const http = {
  get: <T>(path: string) => request<T>(path, { method: "GET" }),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
};

export const devAuthToken = TOKEN;
