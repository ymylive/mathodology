// Search capabilities — one-shot GET /search/capabilities at app start.
//
// The gateway reports which search backends are usable under its current
// env (TAVILY_API_KEY, open-websearch sidecar, etc.). The UI uses this to
// disable the Tavily radio when the key is missing, and to trim the engines
// checkbox list to what open-websearch actually supports.
//
// The endpoint is optional — an older gateway may not have it yet. We fall
// back to a conservative default rather than blocking the form:
//   tavily_available: false  (assume unconfigured)
//   open_websearch_available: true
//   available_engines: the 5 engines most commonly present in the sidecar
//
// Auth + BASE switch mirrors api/http.ts so dev uses the Vite `/api` proxy.

const TOKEN = import.meta.env.VITE_DEV_AUTH_TOKEN;
const BASE =
  import.meta.env.DEV ? "/api" : (import.meta.env.VITE_GATEWAY_HTTP ?? "");

// Timeout before we give up and use the fallback. Keep it short so the form
// paints without a visible stall if the gateway is down.
const FETCH_TIMEOUT_MS = 2500;

export interface SearchCapabilities {
  tavily_available: boolean;
  open_websearch_available: boolean;
  available_engines: string[];
}

const FALLBACK: SearchCapabilities = {
  tavily_available: false,
  open_websearch_available: true,
  available_engines: ["baidu", "csdn", "juejin", "duckduckgo", "bing"],
};

export async function fetchSearchCapabilities(): Promise<SearchCapabilities> {
  const headers = new Headers({ accept: "application/json" });
  if (TOKEN) headers.set("authorization", `Bearer ${TOKEN}`);

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}/search/capabilities`, {
      method: "GET",
      headers,
      signal: ctrl.signal,
    });
    if (!res.ok) {
      // 404 (older gateway), 401/403 (misconfigured token in dev), etc. —
      // any non-2xx yields the conservative default.
      return { ...FALLBACK };
    }
    const body = (await res.json()) as Partial<SearchCapabilities> | null;
    if (!body || typeof body !== "object") return { ...FALLBACK };
    return {
      tavily_available:
        typeof body.tavily_available === "boolean"
          ? body.tavily_available
          : FALLBACK.tavily_available,
      open_websearch_available:
        typeof body.open_websearch_available === "boolean"
          ? body.open_websearch_available
          : FALLBACK.open_websearch_available,
      available_engines: Array.isArray(body.available_engines)
        ? body.available_engines.filter((e): e is string => typeof e === "string")
        : [...FALLBACK.available_engines],
    };
  } catch {
    // AbortError (timeout), network failure, JSON parse error — fall back.
    return { ...FALLBACK };
  } finally {
    clearTimeout(timer);
  }
}
