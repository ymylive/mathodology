<script setup lang="ts">
// 5-step pill header (Analyzer → Searcher → Modeler → Coder → Writer).
// State per agent is derived from run.events:
//   - `done` after a `stage.done` for that agent
//   - `run`  between `stage.start` and `stage.done`
//   - `q`    otherwise (no stage.start seen)
//
// Durations are computed from the `stage.start` / `stage.done` timestamps;
// active stages show live elapsed time (updated every ~500ms by the parent).
import { computed, ref, watch } from "vue";
import type { AgentEvent, AgentName } from "@mathodology/contracts";
import { useRunStore } from "@/stores/run";
import { useI18n } from "@/composables/useI18n";

const props = defineProps<{ now: number }>();

const run = useRunStore();
const i18n = useI18n();

type PillState = "done" | "run" | "q";

interface Pill {
  agent: Exclude<AgentName, null>;
  num: string;
  labelEn: string;
  labelZh: string;
  state: PillState;
  durationMs: number | null;
  costRmb: number; // running total for this agent
}

const AGENTS: { agent: Exclude<AgentName, null>; num: string; en: string; zh: string }[] = [
  { agent: "analyzer", num: "A · 01", en: "Analyzer", zh: "分析员" },
  { agent: "searcher", num: "B · 02", en: "Searcher", zh: "检索员" },
  { agent: "modeler",  num: "C · 03", en: "Modeler",  zh: "建模员" },
  { agent: "coder",    num: "D · 04", en: "Coder",    zh: "编程员" },
  { agent: "writer",   num: "E · 05", en: "Writer",   zh: "撰写员" },
];

function firstStartFor(agent: AgentName, events: AgentEvent[]): AgentEvent | null {
  for (const ev of events) {
    if (ev.agent === agent && ev.kind === "stage.start") return ev;
  }
  return null;
}

function firstDoneFor(agent: AgentName, events: AgentEvent[]): AgentEvent | null {
  for (const ev of events) {
    if (ev.agent === agent && ev.kind === "stage.done") return ev;
  }
  return null;
}

const pills = computed<Pill[]>(() =>
  AGENTS.map(({ agent, num, en, zh }) => {
    const evs = run.orderedEvents;
    const start = firstStartFor(agent, evs);
    const done = firstDoneFor(agent, evs);

    let state: PillState = "q";
    let durationMs: number | null = null;
    if (done) {
      state = "done";
      if (start) {
        const a = Date.parse(start.ts);
        const b = Date.parse(done.ts);
        if (!Number.isNaN(a) && !Number.isNaN(b)) durationMs = b - a;
      }
    } else if (start) {
      state = "run";
      const a = Date.parse(start.ts);
      if (!Number.isNaN(a)) durationMs = props.now - a;
    }

    const usage = run.usage[agent];
    const costRmb = usage ? usage.costRmb : 0;

    return {
      agent,
      num,
      labelEn: en,
      labelZh: zh,
      state,
      durationMs,
      costRmb,
    };
  }),
);

function fmtDuration(ms: number | null): string {
  if (ms === null || ms < 0) return "—";
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const r = Math.floor(s % 60);
  return `${m}:${r.toString().padStart(2, "0")}`;
}

function detail(p: Pill): string {
  if (p.state === "q") return i18n.t("queued", "排队中");
  const dur = fmtDuration(p.durationMs);
  if (p.costRmb > 0) return `${dur} · ¥${p.costRmb.toFixed(3)}`;
  return dur;
}

// --- motion: one-shot pulse when a pill transitions q -> run ---------------
// `running` stays true for the whole stage; we only want to animate on the
// transition edge, so we gate the class on a per-agent flag that auto-clears
// after 280ms. No re-render will replay this because we watch the derived
// state, not the template's re-paint.
const pulsing = ref<Record<string, boolean>>({});
const prevState = new Map<string, PillState>();

watch(
  pills,
  (next) => {
    for (const p of next) {
      const prev = prevState.get(p.agent);
      if (p.state === "run" && prev === "q") {
        pulsing.value = { ...pulsing.value, [p.agent]: true };
        const agent = p.agent;
        window.setTimeout(() => {
          pulsing.value = { ...pulsing.value, [agent]: false };
        }, 280);
      }
      prevState.set(p.agent, p.state);
    }
  },
  { immediate: true, deep: false },
);
</script>

<template>
  <div class="steps">
    <div
      v-for="p in pills"
      :key="p.agent"
      :class="['pill', p.state, pulsing[p.agent] ? 'running-enter' : '']"
    >
      <div class="n">{{ p.num }}</div>
      <div class="l">{{ i18n.t(p.labelEn, p.labelZh) }}</div>
      <div class="d">{{ detail(p) }}</div>
    </div>
  </div>
</template>
