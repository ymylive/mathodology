<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useRunStore, type KernelCellState } from "@/stores/run";
import { figureUrl } from "@/api/figures";

// Live view of kernel execution. Complements CoderOutputView (which renders
// the structured CoderOutput). This panel is updated incrementally as
// `kernel.stdout` / `kernel.figure` events arrive.
//
// Layout: one accordion row per cell_index. The latest cell is default-open;
// older cells collapse once a newer one shows up but can be re-expanded.

const store = useRunStore();

interface CellRow {
  index: number;
  state: KernelCellState;
}

const rows = computed<CellRow[]>(() => {
  return Object.entries(store.kernelCells)
    .map(([k, state]) => ({ index: Number.parseInt(k, 10), state }))
    .filter((r) => !Number.isNaN(r.index))
    .sort((a, b) => a.index - b.index);
});

const latestIndex = computed<number | null>(() => {
  if (rows.value.length === 0) return null;
  return rows.value[rows.value.length - 1].index;
});

// User-toggled overrides. If absent for a given index, we default to
// "expanded if this is the latest cell".
const overrides = ref<Record<number, boolean>>({});

function isExpanded(idx: number): boolean {
  if (idx in overrides.value) return overrides.value[idx];
  return idx === latestIndex.value;
}

function toggle(idx: number) {
  overrides.value = { ...overrides.value, [idx]: !isExpanded(idx) };
}

function figUrlFor(relPath: string): string {
  if (!store.runId) return "";
  return figureUrl(store.runId, relPath);
}

function onFigureError(ev: Event) {
  const img = ev.target as HTMLImageElement;
  img.style.display = "none";
  const parent = img.parentElement;
  if (parent && !parent.querySelector(".fig-placeholder")) {
    const ph = document.createElement("span");
    ph.className =
      "fig-placeholder mono text-[10px] text-neutral-500 inline-flex items-center justify-center w-[60px] h-[45px] border border-dashed border-neutral-700 rounded";
    ph.textContent = "404";
    parent.appendChild(ph);
  }
}

function stdoutLineCount(s: string): number {
  if (!s) return 0;
  // Count newlines; trailing newline doesn't add an empty line.
  const trimmed = s.endsWith("\n") ? s.slice(0, -1) : s;
  return trimmed.length === 0 ? 0 : trimmed.split("\n").length;
}

function formatDuration(state: KernelCellState): string | null {
  if (!state.startTs) return null;
  const end = state.doneTs ?? new Date().toISOString();
  const startMs = Date.parse(state.startTs);
  const endMs = Date.parse(end);
  if (Number.isNaN(startMs) || Number.isNaN(endMs)) return null;
  const ms = Math.max(0, endMs - startMs);
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

// Auto-scroll the expanded stdout panes to the bottom as new text arrives.
// Keyed by cell index so each pane tracks its own scroll state independently.
const scrollers = ref<Record<number, HTMLPreElement | null>>({});
const autoScroll = ref<Record<number, boolean>>({});

function setScroller(idx: number, el: HTMLPreElement | Element | null) {
  scrollers.value[idx] = el as HTMLPreElement | null;
}

function onStdoutScroll(idx: number, ev: Event) {
  const el = ev.target as HTMLPreElement;
  autoScroll.value[idx] = el.scrollTop + el.clientHeight >= el.scrollHeight - 16;
}

watch(
  () => rows.value.map((r) => r.state.stdout),
  async () => {
    await nextTick();
    for (const { index } of rows.value) {
      const el = scrollers.value[index];
      if (!el) continue;
      // Default to auto-scroll on until the user scrolls away.
      if (autoScroll.value[index] === false) continue;
      el.scrollTop = el.scrollHeight;
    }
  },
);
</script>

<template>
  <section
    v-if="rows.length > 0"
    class="rounded-md border border-violet-900/60 bg-violet-950/10 overflow-hidden"
    aria-label="Live kernel activity"
  >
    <header
      class="px-3 py-2 border-b border-violet-900/60 flex items-center gap-2"
    >
      <span
        class="inline-block w-1.5 h-1.5 rounded-full bg-violet-400 shrink-0"
        aria-hidden="true"
      />
      <span class="text-sm text-neutral-200">Kernel activity</span>
      <span class="ml-auto mono text-[11px] text-neutral-500 tabular-nums">
        {{ rows.length }} cell{{ rows.length === 1 ? "" : "s" }}
      </span>
    </header>

    <div>
      <div
        v-for="row in rows"
        :key="row.index"
        class="border-b border-violet-900/40 last:border-b-0"
      >
        <button
          type="button"
          class="w-full px-3 py-2 flex items-center gap-2 text-left hover:bg-violet-950/30 focus:outline-none focus-visible:ring-1 focus-visible:ring-violet-700"
          :aria-expanded="isExpanded(row.index)"
          :aria-controls="`kernel-cell-body-${row.index}`"
          @click="toggle(row.index)"
        >
          <span class="text-sm text-neutral-200">Cell {{ row.index }}</span>
          <span
            v-if="!row.state.doneTs && row.state.startTs"
            class="mono text-[11px] text-emerald-400 inline-flex items-center gap-1"
            aria-label="running"
          >
            <span
              class="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"
            />
            running
          </span>
          <span
            class="mono text-[11px] text-neutral-500 tabular-nums"
          >
            stdout {{ stdoutLineCount(row.state.stdout) }} line{{
              stdoutLineCount(row.state.stdout) === 1 ? "" : "s"
            }}
          </span>
          <span
            v-if="row.state.figures.length > 0"
            class="mono text-[11px] text-fuchsia-400 tabular-nums"
          >
            figures {{ row.state.figures.length }}
          </span>
          <span
            v-if="row.state.stderr.length > 0"
            class="mono text-[11px] text-red-400"
          >
            stderr
          </span>
          <span
            v-if="formatDuration(row.state)"
            class="mono text-[11px] text-neutral-400 tabular-nums"
          >
            {{ formatDuration(row.state) }}
          </span>
          <span
            class="ml-auto mono text-[11px] text-neutral-500 w-3 text-center"
            aria-hidden="true"
          >
            {{ isExpanded(row.index) ? "▾" : "▸" }}
          </span>
        </button>

        <div
          v-show="isExpanded(row.index)"
          :id="`kernel-cell-body-${row.index}`"
          class="px-3 py-2 border-t border-violet-900/40 space-y-2"
        >
          <pre
            v-if="row.state.stdout"
            :ref="(el) => setScroller(row.index, el as HTMLPreElement | Element | null)"
            class="mono text-xs text-neutral-200 whitespace-pre-wrap break-words bg-neutral-950 rounded border border-neutral-800 p-2 overflow-auto max-h-[30vh]"
            @scroll="(ev) => onStdoutScroll(row.index, ev)"
            >{{ row.state.stdout }}</pre>
          <pre
            v-if="row.state.stderr"
            class="mono text-xs text-red-200 whitespace-pre-wrap break-words bg-red-950/30 rounded border border-red-900/60 p-2 overflow-auto max-h-[20vh]"
            >{{ row.state.stderr }}</pre>

          <div v-if="row.state.figures.length > 0" class="flex flex-wrap gap-2">
            <a
              v-for="(fig, i) in row.state.figures"
              :key="fig.path"
              :href="figUrlFor(fig.path)"
              target="_blank"
              rel="noopener"
              class="block rounded border border-neutral-800 bg-neutral-950/60 overflow-hidden hover:border-violet-700"
            >
              <img
                :src="figUrlFor(fig.path)"
                :alt="`Cell ${row.index} figure ${i + 1}`"
                class="block w-[160px] h-[120px] object-contain bg-neutral-950"
                loading="lazy"
                @error="onFigureError"
              />
            </a>
          </div>

          <p
            v-if="
              !row.state.stdout &&
              !row.state.stderr &&
              row.state.figures.length === 0
            "
            class="mono text-xs text-neutral-600 italic"
          >
            waiting for kernel output…
          </p>
        </div>
      </div>
    </div>
  </section>
</template>
