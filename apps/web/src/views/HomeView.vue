<script setup lang="ts">
import { computed, ref } from "vue";
import { isFeedVisible, useRunStore } from "@/stores/run";
import EventCard from "@/components/EventCard.vue";
import AgentStreamCard from "@/components/AgentStreamCard.vue";
import AgentOutputCard from "@/components/AgentOutputCard.vue";
import CostMeter from "@/components/CostMeter.vue";
import KernelActivityPanel from "@/components/KernelActivityPanel.vue";

const store = useRunStore();
const problemText = ref(
  "Sanity check: print hello-world from the kernel and emit one cost event.",
);

const isBusy = computed(
  () => store.status === "queued" || store.status === "running",
);

// "Reconnecting" badge: WS dropped mid-run but we haven't hit a terminal
// state yet. The ws.ts client reconnects up to 3x with backoff; we just
// mirror its observable state.
const isReconnecting = computed(
  () =>
    !store.wsConnected &&
    (store.status === "running" || store.status === "queued") &&
    store.runId !== null,
);

const statusPillClass = computed(() => {
  switch (store.status) {
    case "running":
      return "bg-sky-950 text-sky-300 border-sky-900";
    case "queued":
      return "bg-amber-950 text-amber-300 border-amber-900";
    case "done":
      return "bg-emerald-950 text-emerald-300 border-emerald-900";
    case "failed":
      return "bg-red-950 text-red-300 border-red-900";
    default:
      return "bg-neutral-800 text-neutral-400 border-neutral-700";
  }
});

// Feed = everything except `token` events (which live in the stream cards).
// Sort by seq so a WS replay after reconnect renders in order.
const feedEvents = computed(() =>
  store.orderedEvents.filter((ev) => isFeedVisible(ev.kind)),
);

// Stream cards: one per agent that has either produced tokens or recorded
// usage. Sorted by a stable agent-order for predictable layout.
const AGENT_ORDER = [
  "analyzer",
  "modeler",
  "coder",
  "writer",
  "critic",
  "searcher",
];

const streamAgents = computed(() => {
  const keys = new Set<string>([
    ...Object.keys(store.tokens),
    ...Object.keys(store.usage),
    ...Object.keys(store.outputs),
  ]);
  const arr = Array.from(keys);
  arr.sort((a, b) => {
    const ia = AGENT_ORDER.indexOf(a);
    const ib = AGENT_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  return arr;
});

// An agent is "active" if the most recent stage.start for it has not yet
// been matched by a stage.done. Cheap O(N) scan over the feed.
const activeAgents = computed(() => {
  const active = new Set<string>();
  for (const ev of store.orderedEvents) {
    if (!ev.agent) continue;
    if (ev.kind === "stage.start") active.add(ev.agent);
    else if (ev.kind === "stage.done") active.delete(ev.agent);
  }
  // Once the run reaches a terminal state, nobody is streaming.
  if (store.status === "done" || store.status === "failed") return new Set<string>();
  return active;
});

async function run() {
  const text = problemText.value.trim();
  if (!text || isBusy.value) return;
  await store.startRun(text);
}
</script>

<template>
  <div class="mx-auto max-w-[1024px] px-4 sm:px-6 py-6 space-y-4">
    <!-- Header: title, status, cost meter -->
    <header class="flex items-center gap-3 flex-wrap">
      <h1 class="text-base font-medium text-neutral-200 tracking-wide">
        Mathodology
      </h1>
      <span
        class="mono text-[11px] px-2 py-0.5 rounded border"
        :class="statusPillClass"
      >
        {{ store.status }}
      </span>
      <span
        v-if="isReconnecting"
        class="mono text-[11px] px-2 py-0.5 rounded border border-amber-900 bg-amber-950 text-amber-300 inline-flex items-center gap-1"
      >
        <span class="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
        reconnecting…
      </span>
      <span
        v-if="store.runId"
        class="mono text-xs text-neutral-500 truncate"
        :title="store.runId"
      >
        run {{ store.runId.slice(0, 8) }}…
      </span>
      <span class="ml-auto">
        <CostMeter :total-rmb="store.costRmb" />
      </span>
    </header>

    <!-- Input: sticks to top once events start flowing so the run controls
         stay reachable while the feed scrolls. -->
    <section
      class="space-y-2 bg-neutral-950/80 backdrop-blur"
      :class="feedEvents.length > 0 ? 'sticky top-0 z-10 py-2' : ''"
    >
      <label for="problem" class="text-sm text-neutral-400">
        Problem input
      </label>
      <textarea
        id="problem"
        v-model="problemText"
        rows="4"
        class="w-full rounded-md bg-neutral-950 border border-neutral-800 text-neutral-100 p-3 mono text-sm focus:outline-none focus:ring-1 focus:ring-sky-700"
        :disabled="isBusy"
        placeholder="Paste a math/modelling problem here..."
      />
      <div class="flex items-center gap-3 flex-wrap">
        <button
          class="px-3 py-1.5 text-sm rounded-md bg-sky-700 hover:bg-sky-600 disabled:bg-neutral-800 disabled:text-neutral-500 disabled:cursor-not-allowed transition-colors"
          :disabled="isBusy || !problemText.trim()"
          @click="run"
        >
          {{ isBusy ? "Running..." : "Run" }}
        </button>
        <button
          class="px-3 py-1.5 text-sm rounded-md bg-neutral-800 hover:bg-neutral-700 transition-colors"
          @click="store.reset()"
        >
          Reset
        </button>

        <span
          class="mono text-xs"
          :class="store.wsConnected ? 'text-emerald-400' : 'text-neutral-500'"
        >
          ws {{ store.wsConnected ? "●" : "○" }}
        </span>
      </div>

      <p v-if="store.error" class="text-sm text-red-400 mono">
        {{ store.error }}
      </p>
    </section>

    <!-- Two-column grid: feed on the left, live streams on the right.
         Collapses to a single column below 768px. -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <!-- LEFT: filtered event feed -->
      <section
        class="rounded-md border border-neutral-800 bg-neutral-950/60 overflow-hidden"
      >
        <header
          class="px-3 py-2 border-b border-neutral-800 flex items-center justify-between"
        >
          <span class="text-sm text-neutral-300">Event feed</span>
          <span class="mono text-xs text-neutral-500 tabular-nums">
            {{ feedEvents.length }} event{{ feedEvents.length === 1 ? "" : "s" }}
          </span>
        </header>
        <div v-if="feedEvents.length === 0" class="px-4 py-8 text-center">
          <p class="text-sm text-neutral-500">
            No events yet. Click <span class="mono">Run</span> to start a stream.
          </p>
        </div>
        <div v-else class="max-h-[60vh] overflow-y-auto">
          <EventCard
            v-for="ev in feedEvents"
            :key="`${ev.run_id}-${ev.seq}`"
            :event="ev"
          />
        </div>
      </section>

      <!-- RIGHT: live per-agent streams -->
      <section class="space-y-3">
        <div
          v-if="streamAgents.length === 0"
          class="rounded-md border border-neutral-800 bg-neutral-950/60 px-4 py-8 text-center"
        >
          <p class="text-sm text-neutral-500">
            Live agent streams will appear here once a run starts.
          </p>
        </div>
        <div
          v-for="agent in streamAgents"
          :key="agent"
          class="space-y-2"
        >
          <AgentStreamCard
            :agent="agent"
            :text="store.tokens[agent]?.text ?? ''"
            :model="store.tokens[agent]?.model ?? null"
            :usage="store.usage[agent] ?? null"
            :active="activeAgents.has(agent)"
          />
          <!-- Coder: live kernel activity between stream + structured output.
               The panel hides itself when there are no cells. -->
          <KernelActivityPanel v-if="agent === 'coder'" />
          <AgentOutputCard
            v-if="store.outputs[agent]"
            :agent="agent"
            :schema-name="store.outputs[agent].schemaName"
            :output="store.outputs[agent].output"
            :duration-ms="store.outputs[agent].durationMs"
          />
        </div>
      </section>
    </div>
  </div>
</template>
