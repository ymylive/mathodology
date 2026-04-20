<script setup lang="ts">
import { computed } from "vue";
import type { AgentEvent } from "@mathodology/contracts";

const props = defineProps<{ event: AgentEvent }>();

const shortTs = computed(() => {
  // ISO 8601 → HH:MM:SS.mmm (local-ish, trimmed).
  const d = new Date(props.event.ts);
  if (Number.isNaN(d.getTime())) return props.event.ts;
  return d.toISOString().slice(11, 23);
});

const kindClass = computed(() => {
  switch (props.event.kind) {
    case "error":
      return "bg-red-950 text-red-300 border-red-900";
    case "done":
      return "bg-emerald-950 text-emerald-300 border-emerald-900";
    case "stage.start":
    case "stage.done":
      return "bg-sky-950 text-sky-300 border-sky-900";
    case "cost":
      return "bg-amber-950 text-amber-300 border-amber-900";
    case "token":
      return "bg-neutral-800 text-neutral-300 border-neutral-700";
    default:
      return "bg-neutral-800 text-neutral-400 border-neutral-700";
  }
});

const summary = computed(() => {
  const p = props.event.payload ?? {};
  const val =
    (p as { message?: string }).message ??
    (p as { stage?: string }).stage ??
    (p as { text?: string }).text ??
    "";
  const str = typeof val === "string" ? val : JSON.stringify(val);
  return str.length > 160 ? str.slice(0, 160) + "…" : str;
});
</script>

<template>
  <div class="flex items-start gap-3 px-3 py-2 border-b border-neutral-800">
    <span class="mono text-xs text-neutral-500 shrink-0 w-[96px]">
      {{ shortTs }}
    </span>
    <span class="mono text-xs text-neutral-500 shrink-0 w-10 text-right">
      #{{ event.seq }}
    </span>
    <span
      class="mono text-[11px] px-1.5 py-0.5 rounded border shrink-0"
      :class="kindClass"
    >
      {{ event.kind }}
    </span>
    <span
      v-if="event.agent"
      class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-300 shrink-0"
    >
      {{ event.agent }}
    </span>
    <span class="text-sm text-neutral-200 break-words min-w-0">
      {{ summary }}
    </span>
  </div>
</template>
