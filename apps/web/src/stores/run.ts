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

interface State {
  runId: string | null;
  status: RunStatus;
  events: AgentEvent[];
  error: string | null;
  costRmb: number;
  wsConnected: boolean;
  tokens: Record<string, AgentStream>;
  usage: Record<string, AgentUsage>;
}

// Events we never push into the feed (too noisy / handled elsewhere).
const FEED_HIDDEN_KINDS = new Set(["token"]);

// Agent key used for events with a null `agent` field. Keeps the record keys
// stringly-typed so Vue can reactively track them.
const UNKNOWN_AGENT = "_";

function emptyStream(): AgentStream {
  return { text: "", model: null, updatedAt: new Date().toISOString() };
}

function emptyUsage(): AgentUsage {
  return { promptTokens: 0, completionTokens: 0, costRmb: 0 };
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

      // Everything else goes into the ordered event feed.
      this.events.push(ev);

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
        const p = ev.payload as { status?: string; cost_rmb?: number };
        if (typeof p.cost_rmb === "number") this.costRmb = p.cost_rmb;
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
