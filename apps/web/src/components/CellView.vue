<script setup lang="ts">
// Single Jupyter cell row. Shows the source (syntax-highlighted Python),
// stdout / stderr panels, and any kernel.figure images produced for this
// cell. `active` highlights the currently-executing cell with a running
// caret in the header.
import { ref, watchEffect } from "vue";
import type { KernelCellState } from "@/stores/run";
import { figureUrl } from "@/api/figures";
import { renderPython } from "@/lib/shiki";

const props = defineProps<{
  index: number;
  runId: string;
  source: string;
  cell: KernelCellState;
  active: boolean;
}>();

const sourceHtml = ref<string>("");

watchEffect(async () => {
  if (!props.source) {
    sourceHtml.value = "";
    return;
  }
  try {
    sourceHtml.value = await renderPython(props.source);
  } catch {
    // Fall back to a plain escape if shiki fails (wasm fetch, etc.)
    sourceHtml.value = `<pre>${escapeHtml(props.source)}</pre>`;
  }
});

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function durationLabel(): string {
  if (props.active) return "";
  if (!props.cell.startTs || !props.cell.doneTs) return "";
  const a = Date.parse(props.cell.startTs);
  const b = Date.parse(props.cell.doneTs);
  if (Number.isNaN(a) || Number.isNaN(b)) return "";
  const s = (b - a) / 1000;
  if (s < 10) return `✓ ${s.toFixed(2)}s`;
  return `✓ ${s.toFixed(1)}s`;
}
</script>

<template>
  <div :class="['cell', active ? 'run' : '']">
    <div class="h">
      <span>
        In [{{ index }}] · cell {{ index }}
        <span v-if="active" class="caret"></span>
      </span>
      <span v-if="active" style="color: var(--hi);">●</span>
      <span v-else-if="durationLabel()" style="color: var(--ok);">
        {{ durationLabel() }}
      </span>
    </div>
    <div class="cell-body" v-html="sourceHtml"></div>
    <div
      v-if="cell.stdout || cell.stderr || cell.figures.length > 0"
      class="cell-outputs"
    >
      <div v-if="cell.stdout" class="cell-stdout">{{ cell.stdout }}</div>
      <div v-if="cell.stderr" class="cell-stderr">{{ cell.stderr }}</div>
      <div v-if="cell.figures.length > 0" class="cell-figs">
        <img
          v-for="(f, i) in cell.figures"
          :key="i"
          :src="figureUrl(runId, f.path)"
          :alt="`cell ${index} figure ${i + 1}`"
          loading="lazy"
        />
      </div>
    </div>
  </div>
</template>
