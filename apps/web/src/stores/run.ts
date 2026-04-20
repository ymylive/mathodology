import { defineStore } from "pinia";
import type { AgentEvent, AgentName } from "@mathodology/contracts";
import { http, devAuthToken } from "@/api/http";
import { RunWsClient } from "@/api/ws";

export type RunStatus = "idle" | "queued" | "running" | "done" | "failed";

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

// The WebSocket client is kept outside reactive state so Vue doesn't try to
// proxy its internals (and so we don't log the whole socket into devtools).
let ws: RunWsClient | null = null;

export const useRunStore = defineStore("run", {
  state: (): State => ({
    runId: null,
    status: "idle",
    events: [],
    error: null,
    costRmb: 0,
    wsConnected: false,
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
  },

  actions: {
    reset() {
      ws?.close();
      ws = null;
      this.runId = null;
      this.status = "idle";
      this.events = [];
      this.error = null;
      this.costRmb = 0;
      this.wsConnected = false;
      this.tokens = {};
      this.usage = {};
      this.outputs = {};
      this.kernelCells = {};
      this.notebookPath = null;
    },

    async startRun(problemText: string) {
      if (this.status === "running" || this.status === "queued") return;
      this.reset();
      this.status = "queued";
      try {
        const res = await http.post<RunCreated>("/runs", {
          problem_text: problemText,
        });
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
            if (this.status === "queued") this.status = "running";
          },
          onClose: () => {
            this.wsConnected = false;
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
        const existing = this.tokens[agentKey] ?? emptyStream();
        this.tokens[agentKey] = {
          text: existing.text + delta,
          model:
            typeof payload.model === "string" ? payload.model : existing.model,
          updatedAt: ev.ts,
        };
        return;
      }

      // Kernel stdout/stderr: incremental text per cell. Folded into
      // kernelCells; kept out of the ordered feed (too chatty for the feed).
      if (ev.kind === "kernel.stdout") {
        const p = ev.payload as {
          text?: unknown;
          name?: unknown;
          cell_index?: unknown;
        };
        const text = typeof p.text === "string" ? p.text : "";
        const name = p.name === "stderr" ? "stderr" : "stdout";
        const ci = typeof p.cell_index === "number" ? p.cell_index : 0;
        const cell = this.kernelCells[ci] ?? emptyCell();
        if (name === "stderr") {
          cell.stderr = cell.stderr + text;
        } else {
          cell.stdout = cell.stdout + text;
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
        this.status = p.status === "failed" ? "failed" : "done";
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
