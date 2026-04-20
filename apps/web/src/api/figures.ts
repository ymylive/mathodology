// URL helpers for kernel-produced artifacts served by the gateway.
//
// Figures and notebooks require Bearer auth. `<img>` tags can't set headers,
// so we attach the dev token as a `?token=` query param — the gateway accepts
// both `Authorization: Bearer ...` and `?token=...` (see docs/auth.md).
//
// `VITE_GATEWAY_HTTP` is the direct gateway origin (e.g. http://127.0.0.1:8080).
// We don't use the Vite `/api` proxy here because `<img src>` happens after
// HTML parse time and we want one stable URL format across dev + build.

const BASE = import.meta.env.VITE_GATEWAY_HTTP ?? "";
const TOKEN = import.meta.env.VITE_DEV_AUTH_TOKEN ?? "";

export function figureUrl(runId: string, relPath: string): string {
  // `relPath` is produced by the worker as e.g. "figures/fig-0.png". It may
  // already contain slashes — encode each segment so a stray space or non-ascii
  // character survives the trip without percent-mangling path separators.
  const encoded = relPath
    .split("/")
    .map((s) => encodeURIComponent(s))
    .join("/");
  return `${BASE}/runs/${runId}/figures/${encoded}?token=${encodeURIComponent(TOKEN)}`;
}

export function notebookUrl(runId: string): string {
  return `${BASE}/runs/${runId}/notebook?token=${encodeURIComponent(TOKEN)}`;
}
