import { defineStore } from "pinia";
import type { AgentEvent, AgentName } from "@mathodology/contracts";
import { http, devAuthToken } from "@/api/http";
import { RunWsClient } from "@/api/ws";
import { useFinetuneStore, isFinetuneKind } from "@/stores/finetune";

export type RunStatus =
  | "idle"
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "cancelled";

interface RunCreated {
  run_id: string;
  status: "queued";
}

// Per-agent streaming buffer. Holds the concatenated `token.text` deltas for
// the agent's *current* stage. Reset on each `stage.start` for that agent.
export interface AgentStream {
  text: string;
  model: string | null;
  updatedAt: string;
}

// Per-agent running usage totals (accumulated across all `cost` events for
// that agent in this run).
export interface AgentUsage {
  promptTokens: number;
  completionTokens: number;
  costRmb: number;
}

// Latest structured output emitted by an agent (from `agent.output` events).
// One entry per agent; later `agent.output` events overwrite the previous
// one so the view always shows the most recent parsed result.
export interface AgentOutput {
  schemaName: string;
  output: Record<string, unknown>;
  durationMs: number | null;
  ts: string;
}

// A single Jupyter cell's live execution state. Built up incrementally from
// `kernel.stdout` / `kernel.figure` events and framed by
// `log:"executing cell N"` boundaries. The final structured result lives in
// the CoderOutput `agent.output`; this record is the live view.
export interface KernelCellState {
  stdout: string;
  stderr: string;
  figures: { path: string; format?: string }[];
  startTs?: string;
  doneTs?: string;
}

interface State {
  runId: string | null;
  status: RunStatus;
  events: AgentEvent[];
  error: string | null;
  costRmb: number;
  wsConnected: boolean;
  // True while the WS client is between attempts (closed but going to retry).
  // Distinct from `!wsConnected` which can also mean "terminal / never opened".
  wsReconnecting: boolean;
  tokens: Record<string, AgentStream>;
  usage: Record<string, AgentUsage>;
  outputs: Record<string, AgentOutput>;
  kernelCells: Record<number, KernelCellState>;
  notebookPath: string | null;
}

// Events we never push into the feed. `token` is chatty and folded into the
// stream buffer; `agent.output` is captured into `outputs` and rendered in a
// dedicated card, so it stays out of the feed to avoid duplicating the
// `stage.done` that follows. `kernel.stdout` can produce many frames per
// cell and has its own live panel — keep it out of the ordered feed.
const FEED_HIDDEN_KINDS = new Set([
  "token",
  "agent.output",
  "kernel.stdout",
]);

// `log` payloads that match this exact pattern are treated as cell-boundary
// markers. Frame is `{ level:"info", message:"executing cell 0" }`.
const EXECUTING_CELL_RE = /^executing cell (\d+)$/;

// Agent key used for events with a null `agent` field. Keeps the record keys
// stringly-typed so Vue can reactively track them.
const UNKNOWN_AGENT = "_";

function emptyStream(): AgentStream {
  return { text: "", model: null, updatedAt: new Date().toISOString() };
}

function emptyUsage(): AgentUsage {
  return { promptTokens: 0, completionTokens: 0, costRmb: 0 };
}

function emptyCell(): KernelCellState {
  return { stdout: "", stderr: "", figures: [] };
}

// Workers running matplotlib without an installed CJK font + missing the
// round-10 warnings filter emit a 2-line block per missing glyph per
// savefig call: a UserWarning header and the offending source line. A
// single Coder turn can generate hundreds of these and they crowd out
// real diagnostics. Drop them client-side so the cell-stderr panel
// stays useful regardless of which worker version ran the cell.
//
// Patterns we match (each is one source line; the source-line variant of
// "  plt.savefig(..." follows the warning header):
//   `/path/to/ipykernel.../*.py:NNN: UserWarning: Glyph N (\N{...}) missing from font(s) ...`
//   `  plt.savefig('figures/foo.png', dpi=...)`
//
// The savefig line is the warning's "source pointer" the Python warnings
// machinery prints after the message — useless without the message, so
// we drop both. We also drop standalone `Glyph N ... missing from font`
// lines for the rare proxy that prints only the message.
const _GLYPH_WARN_RE =
  /^.*UserWarning: Glyph \d+.*missing from font\(s\).*\n(?:\s+plt\.savefig\([^)]*\)\s*\n)?/gm;
const _GLYPH_BARE_RE =
  /^Glyph \d+ \(\\N\{[^}]+\}\) missing from font\(s\)[^\n]*\n?/gm;
const _SAVEFIG_BARE_RE =
  /^\s+plt\.savefig\(['"]figures\/[^)]+\)\s*\n?/gm;

function _stripMatplotlibGlyphWarnings(text: string): string {
  if (!text) return text;
  if (!text.includes("missing from font")) return text;
  return text
    .replace(_GLYPH_WARN_RE, "")
    .replace(_GLYPH_BARE_RE, "")
    .replace(_SAVEFIG_BARE_RE, "");
}

// The WebSocket client is kept outside reactive state so Vue doesn't try to
// proxy its internals (and so we don't log the whole socket into devtools).
let ws: RunWsClient | null = null;

// Token-batching buffer.
//
// Vue/Pinia re-renders on every reactive write. Writing each token directly
// to `this.tokens[agentKey].text` means ~5000 full re-renders per Coder
// stage, which silently jams the render queue — the UI freezes mid-stage
// even though the WS is still receiving tokens (a hard refresh "fixes"
// it because it resets reactivity from the latest snapshot).
//
// Instead: queue deltas into this plain Map keyed by agent. A single rAF
// loop drains the queue into reactive state once per animation frame, so
// the render rate is capped at ~60fps regardless of token rate.
const _pendingTokenDeltas: Map<string, { delta: string; model: string | null; ts: string }> = new Map();
let _flushScheduled = false;

export const useRunStore = defineStore("run", {
  state: (): State => ({
    runId: null,
    status: "idle",
    events: [],
    error: null,
    costRmb: 0,
    wsConnected: false,
    wsReconnecting: false,
    tokens: {},
    usage: {},
    outputs: {},
    kernelCells: {},
    notebookPath: null,
  }),

  getters: {
    // Sorted event list. The WS replay from `last_seq` means events can in
    // theory arrive slightly out of order after a reconnect; the feed UI
    // consumes this getter so it always paints in seq order.
    orderedEvents(state): AgentEvent[] {
      return [...state.events].sort((a, b) => a.seq - b.seq);
    },
    // Latest SearchFindings payload (from the searcher agent), or null if
    // the searcher hasn't emitted yet. Kept as a getter so views can
    // cheaply check `!!store.searcherFindings` before rendering the panel.
    searcherFindings(state): Record<string, unknown> | null {
      const ent = state.outputs["searcher"];
      if (!ent) return null;
      if (ent.schemaName !== "SearchFindings") return null;
      return ent.output;
    },
  },

  actions: {
    reset() {
      ws?.close();
      ws = null;
      // Drop any pending token deltas from the prior run so they don't
      // leak into the fresh `tokens` state on the next rAF flush.
      _pendingTokenDeltas.clear();
      this.runId = null;
      this.status = "idle";
      this.events = [];
      this.error = null;
      this.costRmb = 0;
      this.wsConnected = false;
      this.wsReconnecting = false;
      this.tokens = {};
      this.usage = {};
      this.outputs = {};
      this.kernelCells = {};
      this.notebookPath = null;
    },

    async startRun(
      problemText: string,
      opts: {
        reasoningEffort?: "off" | "low" | "medium" | "high";
        longContext?: boolean;
        modelOverride?: string;
        /** SearchConfig snapshot from useSearchConfigStore().payload. Passed
         *  through verbatim as `search_config` on POST /runs. Absent/null
         *  means "let the worker pick its default". Shape matches the
         *  `SearchConfig` interface in stores/searchConfig.ts — typed as
         *  `unknown` here to keep this store free of a cross-store import. */
        searchConfig?: unknown;
      } = {},
    ) {
      if (this.status === "running" || this.status === "queued") return;
      this.reset();
      this.status = "queued";
      try {
        const body: Record<string, unknown> = { problem_text: problemText };
        if (opts.reasoningEffort !== undefined) {
          body["reasoning_effort"] = opts.reasoningEffort;
        }
        if (opts.longContext !== undefined) {
          body["long_context"] = opts.longContext;
        }
        if (opts.modelOverride) {
          body["model_override"] = opts.modelOverride;
        }
        if (opts.searchConfig) {
          body["search_config"] = opts.searchConfig;
        }
        const res = await http.post<RunCreated>("/runs", body);
        this.runId = res.run_id;
        this.openWs(res.run_id);
      } catch (err) {
        this.status = "failed";
        this.error = err instanceof Error ? err.message : String(err);
      }
    },

    openWs(runId: string) {
      const wsBase = import.meta.env.VITE_GATEWAY_WS ?? "ws://127.0.0.1:8080";
      const client = new RunWsClient({
        runId,
        wsBase,
        token: devAuthToken ?? "",
        handlers: {
          onOpen: () => {
            this.wsConnected = true;
            this.wsReconnecting = false;
            if (this.status === "queued") this.status = "running";
          },
          onClose: (ev) => {
            this.wsConnected = false;
            // The RunWsClient already decides whether to attempt a reconnect
            // (closedByUser, terminal, code 1000 → no retry). We mirror its
            // rule here so the UI only shows "reconnecting" when one is
            // actually scheduled. The terminal `done` event flips status
            // and clears this on the next tick.
            const willRetry =
              ev.code !== 1000 &&
              this.status !== "done" &&
              this.status !== "failed" &&
              this.status !== "cancelled";
            this.wsReconnecting = willRetry;
          },
          onError: () => {
            this.wsConnected = false;
          },
          onEvent: (ev) => this.handleEvent(ev),
        },
      });
      ws = client;
      client.connect();
    },

    handleEvent(ev: AgentEvent) {
      // Fine-tune events ride the same WebSocket but belong to a different
      // store. Forward them and bail before any pipeline-handling runs.
      // The kind string isn't in EventKind's union (the contract is the
      // pipeline's), so we use a prefix check that accepts any `finetune.*`.
      if (isFinetuneKind(ev.kind as unknown as string)) {
        useFinetuneStore().handleEvent(ev);
        return;
      }

      const agentKey = agentKeyOf(ev.agent);

      // `token` events are extremely chatty. We keep them out of the main
      // events array and fold them into the per-agent streaming buffer.
      //
      // Replay safety: on WS reconnect the backend replays from `last_seq`.
      // A `stage.start` resets the buffer, so replayed tokens for the
      // current stage just re-build the same text. No dedup needed.
      if (ev.kind === "token") {
        const payload = ev.payload as { text?: unknown; model?: unknown };
        const delta = typeof payload.text === "string" ? payload.text : "";
        if (!delta) return;
        const model =
          typeof payload.model === "string" ? payload.model : null;
        // Accumulate into the non-reactive buffer. The pending entry holds
        // the CONCATENATED delta since the last flush, the latest model
        // value, and the newest event ts.
        const pending = _pendingTokenDeltas.get(agentKey);
        if (pending) {
          pending.delta += delta;
          if (model) pending.model = model;
          pending.ts = ev.ts;
        } else {
          _pendingTokenDeltas.set(agentKey, {
            delta,
            model,
            ts: ev.ts,
          });
        }
        // Schedule one rAF flush per frame. The flush mutates reactive
        // state, so Vue re-renders at most ~60 Hz instead of per-token.
        if (!_flushScheduled) {
          _flushScheduled = true;
          const flush = (): void => {
            _flushScheduled = false;
            if (_pendingTokenDeltas.size === 0) return;
            for (const [k, p] of _pendingTokenDeltas) {
              const existing = this.tokens[k] ?? emptyStream();
              this.tokens[k] = {
                text: existing.text + p.delta,
                model: p.model ?? existing.model,
                updatedAt: p.ts,
              };
            }
            _pendingTokenDeltas.clear();
          };
          if (typeof requestAnimationFrame !== "undefined") {
            requestAnimationFrame(flush);
          } else {
            // Headless fallback (tests / SSR).
            setTimeout(flush, 16);
          }
        }
        return;
      }

      // Kernel stdout/stderr: incremental text per cell. Folded into
      // kernelCells; kept out of the ordered feed (too chatty for the feed).
      // Hard cap per-stream length so a runaway warning flood (e.g. the
      // matplotlib glyph-missing warnings before round-10 kernel bootstrap
      // fix) can't blow up DOM size or freeze the render with a 5MB string.
      if (ev.kind === "kernel.stdout") {
        const p = ev.payload as {
          text?: unknown;
          name?: unknown;
          cell_index?: unknown;
        };
        let text = typeof p.text === "string" ? p.text : "";
        // Belt-and-braces drop of the matplotlib CJK-glyph warnings. The
        // kernel bootstrap silences them via warnings.filterwarnings, but
        // workers running pre-round-10 code still emit them. Filter the
        // exact 2-line pattern (warning header + savefig source line) so
        // the user never sees the flood regardless of worker version.
        text = _stripMatplotlibGlyphWarnings(text);
        if (!text) return;
        const name = p.name === "stderr" ? "stderr" : "stdout";
        const ci = typeof p.cell_index === "number" ? p.cell_index : 0;
        const cell = this.kernelCells[ci] ?? emptyCell();
        const MAX_STREAM_CHARS = 32_000;
        const truncate = (existing: string, delta: string): string => {
          const combined = existing + delta;
          if (combined.length <= MAX_STREAM_CHARS) return combined;
          // Keep the tail (most recent output is most relevant when
          // debugging); prepend a one-line note so the truncation is visible.
          const tail = combined.slice(-MAX_STREAM_CHARS);
          return (
            "[…truncated to last " +
            MAX_STREAM_CHARS.toString() +
            " chars…]\n" +
            tail
          );
        };
        if (name === "stderr") {
          cell.stderr = truncate(cell.stderr, text);
        } else {
          cell.stdout = truncate(cell.stdout, text);
        }
        this.kernelCells[ci] = cell;
        return;
      }

      // Kernel figure: push into cell's figures list. This event is rare
      // (1-3 per run) so it stays in the feed as a visible milestone.
      if (ev.kind === "kernel.figure") {
        const p = ev.payload as {
          path?: unknown;
          format?: unknown;
          cell_index?: unknown;
        };
        const path = typeof p.path === "string" ? p.path : "";
        const format = typeof p.format === "string" ? p.format : undefined;
        const ci = typeof p.cell_index === "number" ? p.cell_index : 0;
        if (path) {
          const cell = this.kernelCells[ci] ?? emptyCell();
          cell.figures = [...cell.figures, { path, format }];
          this.kernelCells[ci] = cell;
        }
        // Fall through: also append to the feed.
      }

      // `agent.output` carries the structured Pydantic result. We stash it
      // per-agent and keep it out of the feed — the immediately-following
      // `stage.done` already marks the boundary there.
      if (ev.kind === "agent.output") {
        const p = ev.payload as {
          schema_name?: unknown;
          output?: unknown;
          duration_ms?: unknown;
        };
        const schemaName =
          typeof p.schema_name === "string" ? p.schema_name : "unknown";
        const output =
          p.output && typeof p.output === "object" && !Array.isArray(p.output)
            ? (p.output as Record<string, unknown>)
            : {};
        const durationMs =
          typeof p.duration_ms === "number" ? p.duration_ms : null;
        this.outputs[agentKey] = {
          schemaName,
          output,
          durationMs,
          ts: ev.ts,
        };
        // CoderOutput may carry notebook_path; capture it so the download
        // button lights up even before the terminal `done` arrives.
        if (schemaName === "CoderOutput") {
          const nb = output["notebook_path"];
          if (typeof nb === "string" && nb.length > 0) {
            this.notebookPath = nb;
          }
        }
        return;
      }

      // Everything else goes into the ordered event feed.
      this.events.push(ev);

      if (ev.kind === "log") {
        // `executing cell N` is emitted by the worker as a cell boundary.
        // We stamp the cell's startTs; the previous cell (if any) gets its
        // doneTs so the panel can show durations.
        const p = ev.payload as { message?: unknown };
        const msg = typeof p.message === "string" ? p.message : "";
        const m = EXECUTING_CELL_RE.exec(msg);
        if (m) {
          const ci = Number.parseInt(m[1], 10);
          if (!Number.isNaN(ci)) {
            // Close out whichever cell currently has startTs but no doneTs.
            for (const k of Object.keys(this.kernelCells)) {
              const idx = Number.parseInt(k, 10);
              if (idx === ci) continue;
              const existing = this.kernelCells[idx];
              if (existing && existing.startTs && !existing.doneTs) {
                this.kernelCells[idx] = { ...existing, doneTs: ev.ts };
              }
            }
            const cell = this.kernelCells[ci] ?? emptyCell();
            this.kernelCells[ci] = { ...cell, startTs: cell.startTs ?? ev.ts };
          }
        }
        return;
      }

      if (ev.kind === "stage.start") {
        // New stage for this agent: clear the streaming buffer so the UI
        // shows fresh text rather than concatenating across stages.
        // Also drop any pending deltas for this agent so a late rAF flush
        // doesn't re-pollute the freshly-cleared buffer.
        _pendingTokenDeltas.delete(agentKey);
        this.tokens[agentKey] = {
          text: "",
          model: this.tokens[agentKey]?.model ?? null,
          updatedAt: ev.ts,
        };
        if (this.status === "queued") this.status = "running";
        return;
      }

      if (ev.kind === "cost") {
        const p = ev.payload as {
          run_total_rmb?: unknown;
          delta_rmb?: unknown;
          prompt_tokens?: unknown;
          completion_tokens?: unknown;
        };
        if (typeof p.run_total_rmb === "number") {
          // run_total_rmb is authoritative per the schema contract.
          this.costRmb = p.run_total_rmb;
        }
        const current = this.usage[agentKey] ?? emptyUsage();
        this.usage[agentKey] = {
          promptTokens:
            current.promptTokens +
            (typeof p.prompt_tokens === "number" ? p.prompt_tokens : 0),
          completionTokens:
            current.completionTokens +
            (typeof p.completion_tokens === "number"
              ? p.completion_tokens
              : 0),
          costRmb:
            current.costRmb +
            (typeof p.delta_rmb === "number" ? p.delta_rmb : 0),
        };
        return;
      }

      if (ev.kind === "error") {
        this.status = "failed";
        const msg = (ev.payload as { message?: string }).message;
        this.error = msg ?? "unknown error";
        return;
      }

      if (ev.kind === "done") {
        const p = ev.payload as {
          status?: string;
          cost_rmb?: number;
          notebook_path?: string;
        };
        if (typeof p.cost_rmb === "number") this.costRmb = p.cost_rmb;
        if (typeof p.notebook_path === "string" && p.notebook_path.length > 0) {
          this.notebookPath = p.notebook_path;
        }
        // Close any still-open cell on run termination so the panel stops
        // showing the spinner.
        for (const k of Object.keys(this.kernelCells)) {
          const idx = Number.parseInt(k, 10);
          const existing = this.kernelCells[idx];
          if (existing && existing.startTs && !existing.doneTs) {
            this.kernelCells[idx] = { ...existing, doneTs: ev.ts };
          }
        }
        // Three distinct terminal statuses. `cancelled` was added in round 7
        // (gateway-signalled mid-run cancel). Treat anything unrecognized as
        // `done` to stay forward-compatible with any future success label.
        this.status =
          p.status === "failed"
            ? "failed"
            : p.status === "cancelled"
              ? "cancelled"
              : "done";
        this.wsReconnecting = false;
        return;
      }
    },
  },
});

export function isFeedVisible(kind: AgentEvent["kind"]): boolean {
  return !FEED_HIDDEN_KINDS.has(kind);
}

function agentKeyOf(agent: AgentName | undefined): string {
  return typeof agent === "string" && agent.length > 0 ? agent : UNKNOWN_AGENT;
}
