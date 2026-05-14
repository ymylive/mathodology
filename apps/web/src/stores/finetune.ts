// Finetune chat store.
//
// Mirrors the shape of `useRunStore` but holds the natural-language
// fine-tuning conversation that runs on top of a completed pipeline. The
// gateway exposes `POST /runs/<run_id>/finetune` which queues a worker job;
// the worker emits events on the same WebSocket as the main run, tagged
// with `kind: "finetune.{token,tool_call,tool_result,done}"`.
//
// The run store doesn't know how to parse those; instead it forwards any
// event whose `kind` starts with `"finetune."` to `handleEvent` here. This
// keeps a single WS connection per run and avoids touching the existing
// pipeline event handling.
//
// One chat per page load — no session persistence (out of scope for round 1).

import { defineStore } from "pinia";
import type { AgentEvent } from "@mathodology/contracts";
import { http } from "@/api/http";

export type FinetuneStatus =
  | "idle"
  | "running"
  | "compiling"
  | "done"
  | "error";

export type ToolName =
  | "read_paper"
  | "edit_section"
  | "edit_constant"
  | "run_cell"
  | "regenerate_figure"
  | "recompile_pdf"
  | (string & {}); // tolerate new tools without a recompile

export interface ToolCall {
  id: string;
  tool: ToolName;
  args: Record<string, unknown>;
  status: "pending" | "ok" | "error";
  result?: string;
  error?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;          // accumulates from finetune.token events
  reasoning: string;     // accumulates from finetune.reasoning events (if any)
  toolCalls: ToolCall[];
  done: boolean;         // true once finetune.done arrives
  summary?: string;
  error?: string;
  createdAt: string;
}

interface State {
  sessionId: string;
  messages: Message[];
  isRunning: boolean;
  status: FinetuneStatus;
  // Tool name currently executing — surfaced in the status indicator
  // (e.g. "Running tool: edit_section…"). Cleared on tool_result.
  activeTool: string | null;
  /** Bumps each time a finetune.done arrives so the workbench can re-fetch
   *  the paper. We expose this as a counter rather than a Vue event so the
   *  view layer can watch() it without coupling to a specific component. */
  paperUpdatedAt: number;
  /** Last error from the POST /runs/:id/finetune call. */
  postError: string | null;
}

interface FinetuneCreated {
  session_id: string;
  status: string;
}

function genId(): string {
  // crypto.randomUUID is available in all evergreen browsers + jsdom 22+.
  // Fall back to a timestamp counter just to keep this safe in older runners.
  if (
    typeof globalThis !== "undefined" &&
    typeof globalThis.crypto?.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  return `id-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

function emptyMessage(role: Message["role"]): Message {
  return {
    id: genId(),
    role,
    text: "",
    reasoning: "",
    toolCalls: [],
    done: false,
    createdAt: new Date().toISOString(),
  };
}

// Names of the kinds we listen to. Centralised so the prefix check and the
// switch stay in sync.
export const FINETUNE_KIND_PREFIX = "finetune.";

export function isFinetuneKind(kind: string): boolean {
  return kind.startsWith(FINETUNE_KIND_PREFIX);
}

export const useFinetuneStore = defineStore("finetune", {
  state: (): State => ({
    sessionId: "",
    messages: [],
    isRunning: false,
    status: "idle",
    activeTool: null,
    paperUpdatedAt: 0,
    postError: null,
  }),

  getters: {
    // Convenience: the assistant message currently receiving streamed output,
    // or null if none. Always the latest assistant message, by construction
    // — we push exactly one assistant message per `send()`.
    activeAssistant(state): Message | null {
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        const m = state.messages[i];
        if (m.role === "assistant" && !m.done) return m;
      }
      return null;
    },
    // True if there is at least one assistant message that performed at least
    // one edit-style tool call. Used to decide if `paper-updated` makes sense.
    didEdit(state): boolean {
      const EDIT_TOOLS = new Set([
        "edit_section",
        "edit_constant",
        "regenerate_figure",
        "recompile_pdf",
      ]);
      for (const m of state.messages) {
        for (const tc of m.toolCalls) {
          if (tc.status === "ok" && EDIT_TOOLS.has(tc.tool)) return true;
        }
      }
      return false;
    },
  },

  actions: {
    reset() {
      this.sessionId = "";
      this.messages = [];
      this.isRunning = false;
      this.status = "idle";
      this.activeTool = null;
      this.paperUpdatedAt = 0;
      this.postError = null;
    },

    async send(runId: string, message: string) {
      const trimmed = message.trim();
      if (!trimmed) return;
      if (this.isRunning) return;

      // 1. Optimistically append the user message — keeps the UI snappy.
      const userMsg = emptyMessage("user");
      userMsg.text = trimmed;
      userMsg.done = true;
      this.messages.push(userMsg);

      this.isRunning = true;
      this.status = "running";
      this.activeTool = null;
      this.postError = null;

      try {
        const body: Record<string, unknown> = { message: trimmed };
        if (this.sessionId) body["session_id"] = this.sessionId;
        const res = await http.post<FinetuneCreated>(
          `/runs/${runId}/finetune`,
          body,
        );
        if (res.session_id) this.sessionId = res.session_id;

        // 2. Append the empty assistant message that the WS will stream into.
        this.messages.push(emptyMessage("assistant"));
      } catch (err) {
        // Surface the failure in the user's message bubble. Mark the run as
        // not running so the input is re-enabled.
        this.isRunning = false;
        this.status = "error";
        this.postError = err instanceof Error ? err.message : String(err);
        // Add a synthetic assistant message so the failure is visible inline.
        const failed = emptyMessage("assistant");
        failed.done = true;
        failed.error = this.postError;
        this.messages.push(failed);
      }
    },

    // Called by the run store when it sees an event with
    // `kind.startsWith("finetune.")`. The event envelope is the standard
    // AgentEvent; this store only inspects `kind` + `payload`.
    //
    // The `EventKind` union in the contracts package covers only pipeline
    // events; finetune events ride the same WS but aren't in the union.
    // Widen to `string` once here so the switch below stays type-clean.
    handleEvent(ev: AgentEvent) {
      const kind: string = ev.kind as unknown as string;
      const payload = ev.payload as Record<string, unknown>;
      const target = this.activeAssistant;

      if (kind === "finetune.token") {
        const text = typeof payload["text"] === "string" ? payload["text"] : "";
        if (!text || !target) return;
        target.text = target.text + text;
        return;
      }

      if (kind === "finetune.reasoning") {
        // Optional channel: some models emit reasoning deltas before any
        // visible output. Treat it like token text but into the reasoning
        // buffer so the UI can collapse it.
        const text = typeof payload["text"] === "string" ? payload["text"] : "";
        if (!text || !target) return;
        target.reasoning = target.reasoning + text;
        return;
      }

      if (kind === "finetune.tool_call") {
        if (!target) return;
        const tool =
          typeof payload["tool"] === "string" ? payload["tool"] : "unknown";
        const args =
          payload["args"] && typeof payload["args"] === "object"
            ? (payload["args"] as Record<string, unknown>)
            : {};
        const callId =
          typeof payload["call_id"] === "string"
            ? payload["call_id"]
            : genId();
        target.toolCalls.push({
          id: callId,
          tool,
          args,
          status: "pending",
        });
        this.activeTool = tool;
        // recompile_pdf -> the UI should display "Compiling PDF…" so the
        // long tail of pdflatex doesn't read as a stall.
        if (tool === "recompile_pdf") this.status = "compiling";
        return;
      }

      if (kind === "finetune.tool_result") {
        if (!target) return;
        const callId =
          typeof payload["call_id"] === "string" ? payload["call_id"] : "";
        const ok = payload["ok"] === true;
        const result =
          typeof payload["result"] === "string"
            ? payload["result"]
            : payload["result"] !== undefined
              ? JSON.stringify(payload["result"])
              : undefined;
        const error =
          typeof payload["error"] === "string" ? payload["error"] : undefined;
        // Find by call_id; fall back to the last pending entry. The latter
        // lets us survive a worker that forgets call_id (defensive).
        let tc = callId
          ? target.toolCalls.find((t) => t.id === callId)
          : undefined;
        if (!tc) {
          for (let i = target.toolCalls.length - 1; i >= 0; i -= 1) {
            if (target.toolCalls[i].status === "pending") {
              tc = target.toolCalls[i];
              break;
            }
          }
        }
        if (tc) {
          tc.status = ok ? "ok" : "error";
          if (result !== undefined) tc.result = result;
          if (error !== undefined) tc.error = error;
        }
        this.activeTool = null;
        if (this.status === "compiling" && ok) {
          // Compilation finished; if the worker hasn't yet emitted `done`
          // we still want the status to look settled.
          this.status = "running";
        }
        return;
      }

      if (kind === "finetune.done") {
        if (target) {
          target.done = true;
          const summary =
            typeof payload["summary"] === "string"
              ? payload["summary"]
              : undefined;
          if (summary) target.summary = summary;
          const errMsg =
            typeof payload["error"] === "string"
              ? payload["error"]
              : undefined;
          if (errMsg) target.error = errMsg;
        }
        this.isRunning = false;
        this.activeTool = null;
        const ok = payload["error"] === undefined;
        this.status = ok ? "done" : "error";
        // Bump the watch token so the Workbench can refetch paper.md.
        this.paperUpdatedAt = Date.now();
        return;
      }

      if (kind === "finetune.error") {
        if (target) {
          target.error =
            typeof payload["message"] === "string"
              ? payload["message"]
              : "unknown error";
          target.done = true;
        }
        this.isRunning = false;
        this.activeTool = null;
        this.status = "error";
        return;
      }
    },
  },
});
