import type { AgentEvent } from "@mathodology/contracts";

// Thin WebSocket wrapper for `/ws/runs/{run_id}`.
// - Sends a `hello` frame on open with last_seq for replay.
// - Reconnects up to 3 times on unexpected close (500 / 1500 / 5000 ms).
// - Does NOT reconnect after a clean close (code 1000) or after we get a
//   terminal `done` event.

const RECONNECT_BACKOFFS_MS = [500, 1500, 5000];

export interface WsHandlers {
  onEvent(ev: AgentEvent): void;
  onOpen?(): void;
  onClose?(ev: CloseEvent): void;
  onError?(err: Event): void;
}

export interface RunWsOptions {
  runId: string;
  wsBase: string; // e.g. ws://127.0.0.1:8080
  token: string;
  handlers: WsHandlers;
}

export class RunWsClient {
  private ws: WebSocket | null = null;
  private attempts = 0;
  // Starts at 0 not -1: the gateway deserializes `last_seq` into u64, which
  // silently fails on negative values and defaults back to 0 anyway. Being
  // explicit keeps the contract honest and avoids a wasted parse.
  private lastSeq = 0;
  private closedByUser = false;
  private terminal = false;

  constructor(private readonly opts: RunWsOptions) {}

  connect(): void {
    if (this.closedByUser || this.terminal) return;
    const url = `${this.opts.wsBase}/ws/runs/${this.opts.runId}?token=${encodeURIComponent(this.opts.token)}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.attempts = 0;
      ws.send(
        JSON.stringify({
          type: "hello",
          run_id: this.opts.runId,
          last_seq: this.lastSeq,
        }),
      );
      this.opts.handlers.onOpen?.();
    };

    ws.onmessage = (m) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(typeof m.data === "string" ? m.data : "");
      } catch {
        return;
      }
      if (!isAgentEvent(parsed)) return;
      if (parsed.seq > this.lastSeq) this.lastSeq = parsed.seq;
      if (parsed.kind === "done") this.terminal = true;
      this.opts.handlers.onEvent(parsed);
    };

    ws.onerror = (e) => this.opts.handlers.onError?.(e);

    ws.onclose = (e) => {
      this.opts.handlers.onClose?.(e);
      this.ws = null;
      if (this.closedByUser || this.terminal) return;
      if (e.code === 1000) return;
      const delay = RECONNECT_BACKOFFS_MS[this.attempts];
      if (delay === undefined) return;
      this.attempts += 1;
      setTimeout(() => this.connect(), delay);
    };
  }

  close(): void {
    this.closedByUser = true;
    this.ws?.close(1000, "client-closed");
    this.ws = null;
  }
}

function isAgentEvent(v: unknown): v is AgentEvent {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.run_id === "string" &&
    typeof o.kind === "string" &&
    typeof o.seq === "number" &&
    typeof o.ts === "string"
  );
}
