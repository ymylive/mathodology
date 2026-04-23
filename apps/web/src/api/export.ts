// Paper export — hits the gateway, streams a blob, triggers a browser download.
//
// `ipynb` is a special case: it reuses the existing GET /runs/:id/notebook
// route rather than /export/ipynb (which doesn't exist on the gateway).
// Every other format goes through GET /runs/:id/export/:format?template=<...>
// and returns a binary body with a Content-Disposition attachment filename.
//
// Auth: matches http.ts — Bearer header when available, same BASE switch
// (dev uses the Vite `/api` proxy so the browser sees same-origin).

const TOKEN = import.meta.env.VITE_DEV_AUTH_TOKEN;
const BASE =
  import.meta.env.DEV ? "/api" : (import.meta.env.VITE_GATEWAY_HTTP ?? "");

export type ExportFormat = "pdf" | "docx" | "tex" | "md" | "ipynb";
export type ExportTemplate = "mcm" | "icm" | "cumcm" | "huashu";

export interface ExportParams {
  runId: string;
  format: ExportFormat;
  template?: ExportTemplate;
}

export interface ExportError extends Error {
  status: number;
  /** Short server-supplied detail (e.g. stderr tail on 500). Plain-text. */
  detail?: string;
}

const EXT: Record<ExportFormat, string> = {
  pdf: "pdf",
  docx: "docx",
  tex: "tex",
  md: "md",
  ipynb: "ipynb",
};

function buildUrl(params: ExportParams): string {
  if (params.format === "ipynb") {
    return `${BASE}/runs/${params.runId}/notebook`;
  }
  const base = `${BASE}/runs/${params.runId}/export/${params.format}`;
  return params.template
    ? `${base}?template=${encodeURIComponent(params.template)}`
    : base;
}

/**
 * Fetch the export bytes and trigger a browser download.
 *
 * Throws ExportError with a `status` field so the UI can map 404/422/500/503
 * to specific messages. The server may include a short stderr tail in the
 * body on 500; we attach it as `detail` truncated to 200 chars.
 */
export async function exportPaper(params: ExportParams): Promise<void> {
  const url = buildUrl(params);
  const headers = new Headers({ accept: "*/*" });
  if (TOKEN) headers.set("authorization", `Bearer ${TOKEN}`);

  const res = await fetch(url, { method: "GET", headers });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const err = new Error(
      `HTTP ${res.status} ${res.statusText} for ${url}`,
    ) as ExportError;
    err.status = res.status;
    if (text) err.detail = text.slice(0, 200);
    throw err;
  }

  const blob = await res.blob();

  // Prefer the server-supplied filename from Content-Disposition; fall back
  // to a sensible default so the browser doesn't save a name-less file.
  const cd = res.headers.get("content-disposition") ?? "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const fallback = `paper-${params.runId.slice(0, 8)}.${EXT[params.format]}`;
  const filename = match?.[1] ? decodeURIComponent(match[1]) : fallback;

  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke on the next tick — some browsers race the click navigation if
  // revoked synchronously.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}
