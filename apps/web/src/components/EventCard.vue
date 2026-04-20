<script setup lang="ts">
import { computed } from "vue";
import type { AgentEvent } from "@mathodology/contracts";
import { useRunStore } from "@/stores/run";
import { figureUrl } from "@/api/figures";

const props = defineProps<{ event: AgentEvent }>();

const store = useRunStore();

// Cell-boundary log pattern — mirrors the store's regex. Matching these
// renders a "Cell N ▶" pill instead of the raw "[info] executing cell N".
const EXECUTING_CELL_RE = /^executing cell (\d+)$/;

const executingCellIndex = computed<number | null>(() => {
  if (props.event.kind !== "log") return null;
  const p = props.event.payload as { message?: unknown };
  if (typeof p.message !== "string") return null;
  const m = EXECUTING_CELL_RE.exec(p.message);
  if (!m) return null;
  const n = Number.parseInt(m[1], 10);
  return Number.isNaN(n) ? null : n;
});

const figureInfo = computed<{ path: string; src: string } | null>(() => {
  if (props.event.kind !== "kernel.figure") return null;
  const p = props.event.payload as { path?: unknown };
  const path = typeof p.path === "string" ? p.path : "";
  if (!path || !store.runId) return null;
  return { path, src: figureUrl(store.runId, path) };
});

const shortTs = computed(() => {
  // ISO 8601 → HH:MM:SS.mmm (local-ish, trimmed).
  const d = new Date(props.event.ts);
  if (Number.isNaN(d.getTime())) return props.event.ts;
  return d.toISOString().slice(11, 23);
});

// Per-kind badge color. `token` and `agent.output` are filtered out at the
// store / feed level (see FEED_HIDDEN_KINDS + isFeedVisible); we also
// early-return here as defense-in-depth so a stray chatty event never leaks
// into the feed.
const kindClass = computed(() => {
  switch (props.event.kind) {
    case "error":
      return "bg-red-950 text-red-300 border-red-900";
    case "done":
      return "bg-emerald-950 text-emerald-300 border-emerald-900";
    case "stage.start":
      return "bg-sky-950 text-sky-300 border-sky-900";
    case "stage.done":
      return "bg-indigo-950 text-indigo-300 border-indigo-900";
    case "cost":
      return "bg-amber-950 text-amber-300 border-amber-900";
    case "log":
      return "bg-neutral-800 text-neutral-300 border-neutral-700";
    case "kernel.stdout":
      return "bg-violet-950 text-violet-300 border-violet-900";
    case "kernel.figure":
      return "bg-fuchsia-950 text-fuchsia-300 border-fuchsia-900";
    default:
      return "bg-neutral-800 text-neutral-400 border-neutral-700";
  }
});

const kindIcon = computed(() => {
  switch (props.event.kind) {
    case "stage.start":
      return "▶";
    case "stage.done":
      return "■";
    case "log":
      return "·";
    case "cost":
      return "¥";
    case "kernel.stdout":
      return ">";
    case "kernel.figure":
      return "◆";
    case "error":
      return "!";
    case "done":
      return "✓";
    default:
      return "•";
  }
});

const summary = computed(() => {
  const p = props.event.payload ?? {};
  // Per-kind summary pickers. We keep this narrow on purpose — the full
  // payload is always available via devtools / the future detail view.
  switch (props.event.kind) {
    case "stage.start":
    case "stage.done": {
      const stage = (p as { stage?: string }).stage;
      const dur = (p as { duration_ms?: number }).duration_ms;
      if (typeof dur === "number") {
        // Format durations >= 1s as seconds with one decimal; sub-second
        // stays in ms so short LLM turns still read naturally.
        const label =
          dur >= 1000 ? `${(dur / 1000).toFixed(1)} s` : `${dur} ms`;
        return `${stage ?? ""} (${label})`;
      }
      return stage ?? "";
    }
    case "log": {
      const level = (p as { level?: string }).level;
      const msg = (p as { message?: string }).message ?? "";
      return level ? `[${level}] ${msg}` : msg;
    }
    case "cost": {
      const total = (p as { run_total_rmb?: number }).run_total_rmb;
      const delta = (p as { delta_rmb?: number }).delta_rmb;
      const model = (p as { model?: string }).model;
      const parts: string[] = [];
      if (typeof delta === "number") parts.push(`+¥${delta.toFixed(6)}`);
      if (typeof total === "number") parts.push(`total ¥${total.toFixed(6)}`);
      if (model) parts.push(model);
      return parts.join("  ·  ");
    }
    case "error": {
      const code = (p as { code?: string }).code;
      const msg = (p as { message?: string }).message ?? "";
      return code ? `[${code}] ${msg}` : msg;
    }
    case "done": {
      const status = (p as { status?: string }).status ?? "";
      const cost = (p as { cost_rmb?: number }).cost_rmb;
      if (typeof cost === "number") return `${status}  ·  ¥${cost.toFixed(6)}`;
      return status;
    }
    case "kernel.stdout": {
      const text = (p as { text?: string; message?: string }).text
        ?? (p as { message?: string }).message
        ?? "";
      return text;
    }
    default: {
      const val =
        (p as { message?: string }).message ??
        (p as { stage?: string }).stage ??
        (p as { text?: string }).text ??
        "";
      return typeof val === "string" ? val : JSON.stringify(val);
    }
  }
});

const truncatedSummary = computed(() => {
  const s = summary.value ?? "";
  return s.length > 200 ? s.slice(0, 200) + "…" : s;
});
</script>

<template>
  <!-- Defense-in-depth: `token`, `agent.output`, and `kernel.stdout` events
       should never hit this component (they're filtered in HomeView +
       store.isFeedVisible), but if one slips through we render nothing
       rather than spam the feed. -->
  <template
    v-if="
      event.kind !== 'token' &&
      event.kind !== 'agent.output' &&
      event.kind !== 'kernel.stdout'
    "
  >
    <div class="flex items-start gap-3 px-3 py-2 border-b border-neutral-800">
      <span class="mono text-xs text-neutral-500 shrink-0 w-[96px] tabular-nums">
        {{ shortTs }}
      </span>
      <span class="mono text-xs text-neutral-500 shrink-0 w-10 text-right tabular-nums">
        #{{ event.seq }}
      </span>
      <span
        class="mono text-[11px] px-1.5 py-0.5 rounded border shrink-0 inline-flex items-center gap-1"
        :class="kindClass"
      >
        <span aria-hidden="true">{{ kindIcon }}</span>
        <span>{{ event.kind }}</span>
      </span>
      <span
        v-if="event.agent"
        class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-300 shrink-0"
      >
        {{ event.agent }}
      </span>

      <!-- Cell-boundary log: dedicated pill instead of the raw text. -->
      <span
        v-if="executingCellIndex !== null"
        class="mono text-[11px] px-1.5 py-0.5 rounded border border-violet-900 bg-violet-950/60 text-violet-300 inline-flex items-center gap-1"
      >
        <span aria-hidden="true">▶</span>
        <span>Cell {{ executingCellIndex }}</span>
      </span>

      <!-- Figure: inline thumbnail next to its path. -->
      <template v-else-if="figureInfo">
        <a
          :href="figureInfo.src"
          target="_blank"
          rel="noopener"
          class="block shrink-0 rounded border border-neutral-800 bg-neutral-950/60 overflow-hidden hover:border-fuchsia-700"
          :aria-label="`Open figure ${figureInfo.path}`"
        >
          <img
            :src="figureInfo.src"
            :alt="figureInfo.path"
            class="block w-[60px] h-[45px] object-contain bg-neutral-950"
            loading="lazy"
          />
        </a>
        <span
          class="mono text-xs text-neutral-300 break-all min-w-0 truncate"
          :title="figureInfo.path"
        >
          {{ figureInfo.path }}
        </span>
      </template>

      <span
        v-else
        class="text-sm text-neutral-200 break-words min-w-0 whitespace-pre-wrap"
      >
        {{ truncatedSummary }}
      </span>
    </div>
  </template>
</template>
