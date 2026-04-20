<script setup lang="ts">
import { computed, ref } from "vue";
import { useRunStore } from "@/stores/run";
import EventCard from "@/components/EventCard.vue";

const store = useRunStore();
const problemText = ref(
  "Sanity check: print hello-world from the kernel and emit one cost event.",
);

const isBusy = computed(
  () => store.status === "queued" || store.status === "running",
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

async function run() {
  const text = problemText.value.trim();
  if (!text || isBusy.value) return;
  await store.startRun(text);
}
</script>

<template>
  <div class="mx-auto max-w-4xl px-6 py-6 space-y-4">
    <section class="space-y-2">
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
      <div class="flex items-center gap-3">
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
          class="mono text-[11px] px-2 py-0.5 rounded border"
          :class="statusPillClass"
        >
          {{ store.status }}
        </span>
        <span
          v-if="store.runId"
          class="mono text-xs text-neutral-500 truncate"
          :title="store.runId"
        >
          run {{ store.runId.slice(0, 8) }}…
        </span>
        <span
          class="mono text-xs"
          :class="
            store.wsConnected ? 'text-emerald-400' : 'text-neutral-500'
          "
        >
          ws {{ store.wsConnected ? "●" : "○" }}
        </span>
        <span class="ml-auto mono text-xs text-neutral-400">
          cost: ¥{{ store.costRmb.toFixed(4) }}
        </span>
      </div>

      <p v-if="store.error" class="text-sm text-red-400 mono">
        {{ store.error }}
      </p>
    </section>

    <section
      class="rounded-md border border-neutral-800 bg-neutral-950/60 overflow-hidden"
    >
      <header
        class="px-3 py-2 border-b border-neutral-800 flex items-center justify-between"
      >
        <span class="text-sm text-neutral-300">Event stream</span>
        <span class="mono text-xs text-neutral-500">
          {{ store.events.length }} event{{ store.events.length === 1 ? "" : "s" }}
        </span>
      </header>
      <div v-if="store.events.length === 0" class="px-4 py-8 text-center">
        <p class="text-sm text-neutral-500">
          No events yet. Click <span class="mono">Run</span> to start a stream.
        </p>
      </div>
      <div v-else class="max-h-[60vh] overflow-y-auto">
        <EventCard
          v-for="ev in store.events"
          :key="`${ev.run_id}-${ev.seq}`"
          :event="ev"
        />
      </div>
    </section>
  </div>
</template>
