<script setup lang="ts">
// Dark-terminal live event log with a 4-stat footer.
//
// Rendering: we iterate the full ordered event list but collapse noisy
// kinds (`token` becomes a counter shown in the header instead of many
// lines). Each line is one div so auto-scroll is cheap.
//
// Auto-scroll behaviour: pinned to bottom while the user is already at
// the bottom; if they scroll up, new events stop forcing a scroll. When
// they scroll back to the bottom, auto-scroll resumes.
import { computed, nextTick, onMounted, ref, watch } from "vue";
import type { AgentEvent } from "@mathodology/contracts";
import { useRunStore } from "@/stores/run";
import { useI18n } from "@/composables/useI18n";
import { useCountUp } from "@/composables/useCountUp";
import T from "./T.vue";

const props = defineProps<{ runId: string; now: number }>();

const run = useRunStore();
const i18n = useI18n();
const logEl = ref<HTMLDivElement | null>(null);
const pinned = ref(true);

function onScroll() {
  const el = logEl.value;
  if (!el) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
  pinned.value = atBottom;
}

interface Line {
  key: string; // seq + kind discriminator for v-for
  ts: string;  // HH:MM:SS
  agent: string;
  agentCls: string; // .lv-agent by default; `.lv-info` for system agents
  msgHtml: string;  // pre-escaped; may contain <span class="hl">
  cls: string;      // level class: lv-info / lv-ok / lv-warn / lv-err / lv-agent
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function agentLabel(ev: AgentEvent): string {
  return ev.agent ?? "gateway";
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function lineFor(ev: AgentEvent): Line | null {
  const ts = fmtTime(ev.ts);
  const agent = agentLabel(ev);
  const agentLabelDisplay = `[${agent}]`;

  let cls = "lv-agent";
  let msg = "";
  let agentCls = "lv-agent";

  switch (ev.kind) {
    case "stage.start": {
      cls = "lv-ok";
      msg = i18n.t("stage started", "阶段开始");
      break;
    }
    case "stage.done": {
      cls = "lv-ok";
      const p = ev.payload as { duration_ms?: unknown; cost_rmb?: unknown };
      const dur =
        typeof p.duration_ms === "number" ? (p.duration_ms / 1000).toFixed(1) + "s" : null;
      const cost =
        typeof p.cost_rmb === "number" ? `¥${p.cost_rmb.toFixed(3)}` : null;
      const parts = ["✓", dur, cost].filter((x): x is string => typeof x === "string");
      msg = parts.join(" · ");
      break;
    }
    case "log": {
      const p = ev.payload as { message?: unknown; level?: unknown };
      const level =
        typeof p.level === "string" && p.level.toLowerCase() === "warn"
          ? "warn"
          : "info";
      cls = level === "warn" ? "lv-warn" : "lv-info";
      const m = typeof p.message === "string" ? p.message : "";
      msg = escapeHtml(m);
      break;
    }
    case "cost": {
      const p = ev.payload as { model?: unknown; delta_rmb?: unknown };
      const model = typeof p.model === "string" ? p.model : "—";
      const delta =
        typeof p.delta_rmb === "number" ? `¥${p.delta_rmb.toFixed(3)}` : "";
      cls = "lv-info";
      msg = `${escapeHtml(model)} · ${delta}`;
      break;
    }
    case "error": {
      cls = "lv-err";
      const p = ev.payload as { message?: unknown };
      msg = escapeHtml(typeof p.message === "string" ? p.message : "error");
      break;
    }
    case "done": {
      cls = "lv-ok";
      const p = ev.payload as { status?: unknown; cost_rmb?: unknown };
      const status = typeof p.status === "string" ? p.status : "done";
      const cost =
        typeof p.cost_rmb === "number" ? ` · ¥${p.cost_rmb.toFixed(3)}` : "";
      msg = `run ${escapeHtml(status)}${cost}`;
      agentCls = "lv-info";
      break;
    }
    case "kernel.stdout": {
      // Folded into cells; skip from log.
      return null;
    }
    case "kernel.figure": {
      cls = "lv-info";
      const p = ev.payload as { path?: unknown };
      const path = typeof p.path === "string" ? p.path : "figure";
      msg = `figure · ${escapeHtml(path)}`;
      break;
    }
    case "agent.output": {
      // Store hides this from events[], so we won't see it here. Keep a
      // branch for safety.
      cls = "lv-ok";
      const p = ev.payload as { schema_name?: unknown };
      const schema = typeof p.schema_name === "string" ? p.schema_name : "output";
      msg = `agent.output emitted (${escapeHtml(schema)})`;
      break;
    }
    case "token": {
      // Folded into a counter in the header.
      return null;
    }
    default:
      msg = escapeHtml(ev.kind);
  }

  return {
    key: `${ev.seq}-${ev.kind}`,
    ts,
    agent: agentLabelDisplay,
    agentCls,
    msgHtml: msg,
    cls,
  };
}

const lines = computed<Line[]>(() => {
  // orderedEvents already filters tokens / agent.output / kernel.stdout.
  const out: Line[] = [];
  for (const ev of run.orderedEvents) {
    const l = lineFor(ev);
    if (l) out.push(l);
  }

  // Live streaming indicators — one per agent currently receiving token
  // deltas. Without these the terminal sits silent for 30-60s while gpt-5.4
  // streams its response, making the UI feel frozen. Stable `key` lets Vue
  // update the message in place instead of churning DOM.
  if (run.status === "running" || run.status === "queued") {
    const streamingAgents = Object.entries(run.tokens).filter(
      ([, s]) => s.text.length > 0,
    );

    if (streamingAgents.length > 0) {
      for (const [agent, s] of streamingAgents) {
        // Show the live tail of the agent's current stream — the last ~180
        // chars, whitespace-collapsed, with a caret. This gives a real
        // "what's happening right now" window into the model's output,
        // instead of an abstract character counter.
        const collapsed = s.text.replace(/\s+/g, " ").trim();
        const tail =
          collapsed.length > 180 ? "…" + collapsed.slice(-180) : collapsed;
        out.push({
          key: `stream-${agent}`,
          ts: fmtTime(s.updatedAt),
          agent: `[${agent}]`,
          agentCls: "lv-agent",
          msgHtml:
            `<span class="stream-tail">${escapeHtml(tail)}</span>` +
            `<span class="caret"></span>`,
          cls: "lv-info",
        });
      }
    } else if (out.length > 0) {
      const last = run.orderedEvents[run.orderedEvents.length - 1];
      out.push({
        key: `synth-${last.seq}`,
        ts: fmtTime(new Date().toISOString()),
        agent: "[…]",
        agentCls: "dim",
        msgHtml: "",
        cls: "dim",
      });
    }
  }
  return out;
});

// token counter shown in the header
const tokenCount = computed(() => {
  // Count synthesized from the per-agent stream buffers (length of token
  // text isn't token-accurate, but per-agent `usage` sums are).
  let prompt = 0;
  let completion = 0;
  for (const k of Object.keys(run.usage)) {
    const u = run.usage[k];
    prompt += u.promptTokens;
    completion += u.completionTokens;
  }
  return { prompt, completion };
});

// 4-stat footer — elapsed / est. total / tokens / cost
const startIso = computed<string | null>(() => {
  const evs = run.orderedEvents;
  if (evs.length === 0) return null;
  return evs[0].ts;
});

const elapsedMs = computed(() => {
  if (!startIso.value) return 0;
  const start = Date.parse(startIso.value);
  if (Number.isNaN(start)) return 0;
  return Math.max(0, props.now - start);
});

// `est. total` can't be predicted from progress-fraction without per-agent
// historical timings (no such endpoint). Until the run terminates we show
// `—`; once it's done we freeze the value at the final elapsed time so the
// viewer sees the actual total.
const estTotalMs = computed<number | null>(() => {
  if (run.status === "done" || run.status === "failed") return elapsedMs.value;
  return null;
});

function fmtMs(ms: number | null): string {
  if (ms === null || ms <= 0) return "—";
  const s = ms / 1000;
  if (s < 60) return `${Math.floor(s)}s`;
  const m = Math.floor(s / 60);
  const r = Math.floor(s % 60);
  return `${m}:${r.toString().padStart(2, "0")}`;
}

function fmtK(n: number): string {
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

// --- motion: smooth count-up for cost + token totals in the footer --------
// Each live update re-tweens from the current display value (300ms) rather
// than snapping, so `¥0.001 → ¥0.018` feels like progress, not flicker.
const costTarget = computed(() => run.costRmb);
const promptTarget = computed(() => tokenCount.value.prompt);
const completionTarget = computed(() => tokenCount.value.completion);

const costDisplay = useCountUp(costTarget, { duration: 300 });
const promptDisplay = useCountUp(promptTarget, { duration: 300 });
const completionDisplay = useCountUp(completionTarget, { duration: 300 });

// Auto-scroll to bottom when new events arrive if the user is still
// pinned to the bottom.
watch(
  () => lines.value.length,
  async () => {
    if (!pinned.value) return;
    await nextTick();
    const el = logEl.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);

onMounted(async () => {
  await nextTick();
  const el = logEl.value;
  if (el) el.scrollTop = el.scrollHeight;
});
</script>

<template>
  <div class="panel">
    <div
      class="panel-h"
      style="background:#0D0C09; color:#DDD3BD; border-bottom-color:#2A261F;"
    >
      <div class="eyebrow" style="color:#7A7264;">
        <T en="Live events" zh="实时事件" />
      </div>
      <span class="mono" style="font-size:10.5px; color:#7A7264;">
        ws · /runs/{{ runId.slice(0, 8) }}
        <template v-if="tokenCount.prompt > 0 || tokenCount.completion > 0">
          · tokens {{ fmtK(Math.round(promptDisplay)) }}/{{ fmtK(Math.round(completionDisplay)) }}
        </template>
      </span>
    </div>
    <div
      ref="logEl"
      class="log"
      style="border-radius:0; max-height: 360px;"
      @scroll="onScroll"
    >
      <div v-if="lines.length === 0" class="dim">
        <T en="Waiting for events…" zh="等待事件…" />
      </div>
      <div v-for="l in lines" :key="l.key">
        <span class="ts">{{ l.ts }}</span>
        <span :class="l.agentCls">{{ l.agent }}</span>
        <span :class="l.cls" v-html="l.msgHtml"></span>
      </div>
    </div>
    <div class="stat2">
      <div>
        <div class="k"><T en="elapsed" zh="已用" /></div>
        <div class="v">{{ fmtMs(elapsedMs) }}</div>
      </div>
      <div>
        <div class="k"><T en="est. total" zh="预计" /></div>
        <div class="v">{{ fmtMs(estTotalMs) }}</div>
      </div>
      <div>
        <div class="k"><T en="tokens" zh="token" /></div>
        <div class="v">
          {{ fmtK(Math.round(promptDisplay)) }} / {{ fmtK(Math.round(completionDisplay)) }}
        </div>
      </div>
      <div>
        <div class="k"><T en="cost" zh="成本" /></div>
        <div class="v">¥ {{ costDisplay.toFixed(3) }}</div>
      </div>
    </div>
  </div>
</template>
