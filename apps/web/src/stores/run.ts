import { defineStore } from "pinia";
import type { AgentEvent } from "@mathodology/contracts";
import { http, devAuthToken } from "@/api/http";
import { RunWsClient } from "@/api/ws";

export type RunStatus = "idle" | "queued" | "running" | "done" | "failed";

interface RunCreated {
  run_id: string;
  status: "queued";
}

interface State {
  runId: string | null;
  status: RunStatus;
  events: AgentEvent[];
  error: string | null;
  costRmb: number;
  wsConnected: boolean;
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
  }),

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
          onEvent: (ev) => {
            this.events.push(ev);
            if (ev.kind === "cost") {
              const total = (ev.payload as { run_total_rmb?: number })
                .run_total_rmb;
              if (typeof total === "number") this.costRmb = total;
            }
            if (ev.kind === "stage.start" && this.status === "queued") {
              this.status = "running";
            }
            if (ev.kind === "error") {
              this.status = "failed";
              const msg = (ev.payload as { message?: string }).message;
              this.error = msg ?? "unknown error";
            }
            if (ev.kind === "done") {
              const p = ev.payload as { status?: string; cost_rmb?: number };
              if (typeof p.cost_rmb === "number") this.costRmb = p.cost_rmb;
              this.status = p.status === "failed" ? "failed" : "done";
            }
          },
        },
      });
      ws = client;
      client.connect();
    },
  },
});
