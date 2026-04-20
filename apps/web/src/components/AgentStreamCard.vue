<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { AgentUsage } from "@/stores/run";

// Renders the live token stream for a single agent. Text is bound as a
// single `{{ text }}` interpolation — Vue diffs the text node rather than
// re-parsing on each token, which is the reactivity pattern we want for
// many-per-second updates.
//
// Auto-scroll policy: always scroll to bottom on new text UNLESS the user
// has scrolled up (threshold: 16px from the bottom). Once they're pinned
// away from the bottom, we stop chasing until they scroll back down.
const props = defineProps<{
  agent: string;
  text: string;
  model: string | null;
  usage: AgentUsage | null;
  active: boolean;
}>();

const scroller = ref<HTMLDivElement | null>(null);
const autoScroll = ref(true);

function onScroll() {
  const el = scroller.value;
  if (!el) return;
  autoScroll.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 16;
}

watch(
  () => props.text,
  async () => {
    if (!autoScroll.value) return;
    await nextTick();
    const el = scroller.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);

// Per-agent accent color, wired to CSS variables in styles.css so the palette
// stays in one place. Falls back to neutral for agents we don't know.
const agentColorVar = computed(() => {
  switch (props.agent) {
    case "analyzer":
    case "modeler":
    case "coder":
    case "writer":
    case "critic":
    case "searcher":
      return `var(--color-agent-${props.agent})`;
    default:
      return "var(--color-agent-default)";
  }
});

const hasText = computed(() => props.text.length > 0);
</script>

<template>
  <section
    class="rounded-md border border-neutral-800 bg-neutral-950/60 overflow-hidden flex flex-col"
    :aria-label="`Live stream for ${agent}`"
  >
    <header
      class="px-3 py-2 border-b border-neutral-800 flex items-center gap-2"
    >
      <span
        class="inline-block w-1.5 h-1.5 rounded-full shrink-0"
        :style="{ backgroundColor: agentColorVar }"
        aria-hidden="true"
      />
      <span class="text-sm text-neutral-200 capitalize">{{ agent }}</span>
      <span
        v-if="model"
        class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-400"
      >
        {{ model }}
      </span>
      <span
        v-if="active"
        class="ml-auto mono text-[11px] text-emerald-400 inline-flex items-center gap-1"
        aria-label="streaming"
      >
        <span class="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
        streaming
      </span>
    </header>

    <div
      ref="scroller"
      class="mono text-xs text-neutral-200 p-3 overflow-y-auto whitespace-pre-wrap break-words max-h-[40vh] min-h-[120px]"
      @scroll="onScroll"
    >
      <template v-if="hasText">
        <!-- Plain text only for M3 — Vue escapes by default.
             Single text binding so Vue can diff the text node. -->
        {{ text }}<span
          v-if="active"
          class="inline-block w-[0.5ch] h-[1em] align-[-0.15em] bg-neutral-300 animate-pulse ml-0.5"
          aria-hidden="true"
        />
      </template>
      <span v-else class="text-neutral-600 italic">
        waiting for tokens…
      </span>
    </div>

    <footer
      v-if="usage"
      class="px-3 py-1.5 border-t border-neutral-800 flex items-center gap-3 mono text-[11px] text-neutral-400 tabular-nums"
    >
      <span>P:{{ usage.promptTokens }}</span>
      <span>C:{{ usage.completionTokens }}</span>
      <span class="ml-auto">¥{{ usage.costRmb.toFixed(4) }}</span>
    </footer>
  </section>
</template>
