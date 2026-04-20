<script setup lang="ts">
import { computed } from "vue";
import type { AgentEvent } from "@mathodology/contracts";
import { useRunStore } from "@/stores/run";
import { figureUrl } from "@/api/figures";
import { Badge } from "@/components/ui/badge";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDollarSign,
  Dot,
  Image as ImageIcon,
  Library,
  Play,
  Square,
  Terminal,
} from "lucide-vue-next";

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

// M9: "HMML retrieved: OLS, PCA, ARIMA, ..." — surface the comma list as
// a compact sky-tinted pill next to a Library icon so the reader can
// skim which methods the Modeler evaluated without reading the raw log.
const HMML_RETRIEVED_RE = /^HMML retrieved:\s*(.+)$/;

const hmmlRetrieved = computed<string | null>(() => {
  if (props.event.kind !== "log") return null;
  const p = props.event.payload as { message?: unknown };
  if (typeof p.message !== "string") return null;
  const m = HMML_RETRIEVED_RE.exec(p.message);
  return m ? m[1].trim() : null;
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
      return "bg-secondary text-secondary-foreground border-border";
    case "kernel.stdout":
      return "bg-violet-950 text-violet-300 border-violet-900";
    case "kernel.figure":
      return "bg-fuchsia-950 text-fuchsia-300 border-fuchsia-900";
    default:
      return "bg-secondary text-muted-foreground border-border";
  }
});

// Map kinds → lucide icon component. Using a component reference keeps
// the template declarative and preserves tree-shaking (each import is its
// own symbol).
const kindIcon = computed(() => {
  switch (props.event.kind) {
    case "stage.start":
      return Play;
    case "stage.done":
      return Square;
    case "log":
      return Dot;
    case "cost":
      return CircleDollarSign;
    case "kernel.stdout":
      return Terminal;
    case "kernel.figure":
      return ImageIcon;
    case "error":
      return AlertTriangle;
    case "done":
      return CheckCircle2;
    default:
      return Dot;
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
    <div class="flex items-start gap-3 px-3 py-2 border-b">
      <span class="mono text-xs text-muted-foreground shrink-0 w-[96px] tabular-nums">
        {{ shortTs }}
      </span>
      <span class="mono text-xs text-muted-foreground shrink-0 w-10 text-right tabular-nums">
        #{{ event.seq }}
      </span>
      <Badge
        variant="outline"
        :class="['mono text-[11px] py-0 px-1.5 font-normal gap-1 border', kindClass]"
      >
        <component :is="kindIcon" class="h-3 w-3" aria-hidden="true" />
        <span>{{ event.kind }}</span>
      </Badge>
      <Badge
        v-if="event.agent"
        variant="outline"
        class="mono text-[11px] py-0 px-1.5 font-normal text-foreground"
      >
        {{ event.agent }}
      </Badge>

      <!-- Cell-boundary log: dedicated pill instead of the raw text. -->
      <Badge
        v-if="executingCellIndex !== null"
        variant="outline"
        class="mono text-[11px] py-0 px-1.5 font-normal gap-1 border-violet-900 bg-violet-950/60 text-violet-300"
      >
        <Play class="h-3 w-3" aria-hidden="true" />
        <span>Cell {{ executingCellIndex }}</span>
      </Badge>

      <!-- HMML retrieval log: sky-tinted pill with a Library icon, then
           the comma-separated method list in the normal summary slot so
           long lists still wrap gracefully rather than overflow. -->
      <template v-else-if="hmmlRetrieved !== null">
        <Badge
          variant="outline"
          class="mono text-[11px] py-0 px-1.5 font-normal gap-1 border-sky-900 bg-sky-950/60 text-sky-300"
        >
          <Library class="h-3 w-3" aria-hidden="true" />
          <span>HMML</span>
        </Badge>
        <span
          class="text-sm text-foreground break-words min-w-0 whitespace-pre-wrap"
        >
          {{ hmmlRetrieved }}
        </span>
      </template>

      <!-- Figure: inline thumbnail next to its path. -->
      <template v-else-if="figureInfo">
        <a
          :href="figureInfo.src"
          target="_blank"
          rel="noopener"
          class="block shrink-0 rounded border border-border bg-card/60 overflow-hidden hover:border-fuchsia-700"
          :aria-label="`Open figure ${figureInfo.path}`"
        >
          <img
            :src="figureInfo.src"
            :alt="figureInfo.path"
            class="block w-[60px] h-[45px] object-contain bg-background"
            loading="lazy"
          />
        </a>
        <span
          class="mono text-xs text-foreground break-all min-w-0 truncate"
          :title="figureInfo.path"
        >
          {{ figureInfo.path }}
        </span>
      </template>

      <span
        v-else
        class="text-sm text-foreground break-words min-w-0 whitespace-pre-wrap"
      >
        {{ truncatedSummary }}
      </span>
    </div>
  </template>
</template>
