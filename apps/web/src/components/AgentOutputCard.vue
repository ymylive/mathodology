<script setup lang="ts">
import { computed, ref } from "vue";
import AnalyzerOutputView from "@/components/AnalyzerOutputView.vue";
import CoderOutputView from "@/components/CoderOutputView.vue";

// Displays the structured `agent.output` payload for a single agent.
// Collapsed by default so the stream card above stays the visual focus.
// When a schema-specific view exists (e.g. AnalyzerOutput) we render it;
// otherwise we fall back to a JSON pre block so arbitrary schemas still
// surface something useful without needing a code change.
const props = defineProps<{
  agent: string;
  schemaName: string;
  output: Record<string, unknown>;
  durationMs: number | null;
}>();

const expanded = ref(false);
const copied = ref(false);
let copyTimer: number | null = null;

function toggle() {
  expanded.value = !expanded.value;
}

async function copyJson(ev: Event) {
  // Prevent the wrapping <button> header from toggling the card when the
  // user's intent is just to grab the JSON.
  ev.stopPropagation();
  const text = JSON.stringify(props.output);
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback for non-secure contexts (e.g. http://127.0.0.1 in some
      // browsers). Plain-text, never inserted into the DOM tree the user
      // sees.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    copied.value = true;
    if (copyTimer !== null) window.clearTimeout(copyTimer);
    copyTimer = window.setTimeout(() => {
      copied.value = false;
      copyTimer = null;
    }, 1000);
  } catch {
    // Clipboard permission denied — silently swallow; the JSON is still
    // visible in the expanded view for manual copy.
  }
}

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

const durationLabel = computed(() => {
  if (props.durationMs === null) return null;
  if (props.durationMs < 1000) return `${props.durationMs} ms`;
  return `${(props.durationMs / 1000).toFixed(1)} s`;
});

const isAnalyzer = computed(() => props.schemaName === "AnalyzerOutput");
const isCoder = computed(() => props.schemaName === "CoderOutput");

const prettyJson = computed(() => JSON.stringify(props.output, null, 2));
</script>

<template>
  <section
    class="rounded-md border border-sky-900/60 bg-sky-950/20 overflow-hidden"
    :aria-label="`Structured output from ${agent}`"
  >
    <!-- Header is a real <button> for keyboard + screen-reader semantics. -->
    <button
      type="button"
      class="w-full px-3 py-2 flex items-center gap-2 text-left hover:bg-sky-950/40 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-700"
      :aria-expanded="expanded"
      :aria-controls="`agent-output-body-${agent}`"
      @click="toggle"
    >
      <span
        class="inline-block w-1.5 h-1.5 rounded-full shrink-0"
        :style="{ backgroundColor: agentColorVar }"
        aria-hidden="true"
      />
      <span class="text-sm text-neutral-200 capitalize">{{ agent }}</span>
      <span
        class="mono text-[11px] px-1.5 py-0.5 rounded border border-sky-900 bg-sky-950/60 text-sky-300"
      >
        {{ schemaName }}
      </span>
      <span
        v-if="durationLabel"
        class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-400 tabular-nums"
      >
        {{ durationLabel }}
      </span>

      <span class="ml-auto inline-flex items-center gap-2">
        <span
          v-if="copied"
          class="mono text-[11px] text-emerald-400"
          aria-live="polite"
        >
          copied
        </span>
        <!-- Inner <span> with role=button so we don't nest <button>s. -->
        <span
          role="button"
          tabindex="0"
          aria-label="Copy JSON"
          class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-400 hover:text-neutral-200 hover:border-neutral-600 cursor-pointer"
          @click="copyJson"
          @keydown.enter.stop.prevent="copyJson($event)"
          @keydown.space.stop.prevent="copyJson($event)"
        >
          copy
        </span>
        <span
          class="mono text-[11px] text-neutral-500 w-3 text-center"
          aria-hidden="true"
        >
          {{ expanded ? "▾" : "▸" }}
        </span>
      </span>
    </button>

    <div
      v-show="expanded"
      :id="`agent-output-body-${agent}`"
      class="px-3 py-3 border-t border-sky-900/60"
    >
      <AnalyzerOutputView v-if="isAnalyzer" :output="output" />
      <CoderOutputView v-else-if="isCoder" :output="output" />
      <pre
        v-else
        class="mono text-xs text-neutral-200 whitespace-pre-wrap break-words overflow-auto max-h-[50vh] bg-neutral-950/60 rounded border border-neutral-800 p-2"
      >{{ prettyJson }}</pre>
    </div>
  </section>
</template>
