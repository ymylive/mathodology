<script setup lang="ts">
import { computed, ref } from "vue";
import { useRunStore } from "@/stores/run";
import { figureUrl, notebookUrl } from "@/api/figures";

// Schema-aware renderer for CoderOutput. The payload comes in as a loose
// `Record<string, unknown>` from the store — narrow each field defensively
// so a malformed event just hides the affected section rather than crashing
// the card.
const props = defineProps<{
  output: Record<string, unknown>;
}>();

interface CoderCell {
  index: number;
  source: string;
  stdout: string;
  stderr: string;
  resultText: string;
  figurePaths: string[];
  durationMs: number | null;
  errored: boolean;
}

const store = useRunStore();

const finalSummary = computed<string>(() => {
  const v = props.output["final_summary"];
  return typeof v === "string" ? v : "";
});

// Prefer the output's notebook_path; fall back to the store's copy which may
// have been populated from the terminal `done` event.
const notebookRelPath = computed<string | null>(() => {
  const v = props.output["notebook_path"];
  if (typeof v === "string" && v.length > 0) return v;
  return store.notebookPath;
});

const downloadHref = computed<string | null>(() => {
  if (!store.runId) return null;
  return notebookUrl(store.runId);
});

const cells = computed<CoderCell[]>(() => {
  const raw = props.output["cells"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item, i): CoderCell | null => {
      if (!item || typeof item !== "object") return null;
      const rec = item as Record<string, unknown>;
      const index =
        typeof rec["index"] === "number" ? rec["index"] : i;
      const source = typeof rec["source"] === "string" ? rec["source"] : "";
      const stdout = typeof rec["stdout"] === "string" ? rec["stdout"] : "";
      const stderr = typeof rec["stderr"] === "string" ? rec["stderr"] : "";
      const resultText =
        typeof rec["result_text"] === "string" ? rec["result_text"] : "";
      const figurePaths = pickStringArray(rec["figure_paths"]);
      const durationMs =
        typeof rec["duration_ms"] === "number" ? rec["duration_ms"] : null;
      const errored =
        typeof rec["errored"] === "boolean" ? rec["errored"] : stderr.length > 0;
      return {
        index,
        source,
        stdout,
        stderr,
        resultText,
        figurePaths,
        durationMs,
        errored,
      };
    })
    .filter((x): x is CoderCell => x !== null)
    .sort((a, b) => a.index - b.index);
});

function pickStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}

function formatDuration(ms: number | null): string | null {
  if (ms === null) return null;
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

// Expanded-row tracking. Keyed by cell index. Default-closed; power users
// can pop each open as needed. (KernelActivityPanel is the live view.)
const expandedCells = ref<Record<number, boolean>>({});

function toggle(idx: number) {
  expandedCells.value = {
    ...expandedCells.value,
    [idx]: !expandedCells.value[idx],
  };
}

function figUrlFor(relPath: string): string {
  if (!store.runId) return "";
  return figureUrl(store.runId, relPath);
}

// Replace a failed <img> with an inline placeholder. Figures may 404 if the
// worker's write lags the event — rare but not impossible.
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
</script>

<template>
  <div class="space-y-4 text-sm text-neutral-200">
    <!-- Final summary: highlighted block at the top. -->
    <div
      v-if="finalSummary"
      class="rounded-md border border-emerald-900/60 bg-emerald-950/20 p-3"
    >
      <h4 class="text-xs uppercase tracking-wider text-emerald-400 mb-1">
        Final summary
      </h4>
      <p class="text-neutral-100 leading-relaxed whitespace-pre-wrap">
        {{ finalSummary }}
      </p>
    </div>

    <!-- Notebook download. -->
    <div v-if="downloadHref && notebookRelPath" class="flex items-center gap-2">
      <a
        :href="downloadHref"
        download
        aria-label="Download notebook.ipynb"
        class="mono text-xs px-2 py-1 rounded border border-sky-900 bg-sky-950/40 text-sky-300 hover:bg-sky-950/80 hover:border-sky-700 inline-flex items-center gap-1.5"
      >
        <span aria-hidden="true">⬇</span>
        <span>Download notebook</span>
      </a>
      <span class="mono text-[11px] text-neutral-500 truncate">
        {{ notebookRelPath }}
      </span>
    </div>

    <!-- Cells list. -->
    <div v-if="cells.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-2">
        Cells ({{ cells.length }})
      </h4>
      <div class="space-y-1">
        <div
          v-for="cell in cells"
          :key="cell.index"
          class="rounded-md border overflow-hidden"
          :class="
            cell.errored
              ? 'border-red-900/60 bg-red-950/10'
              : 'border-neutral-800 bg-neutral-950/40'
          "
        >
          <button
            type="button"
            class="w-full px-3 py-2 flex items-center gap-2 text-left hover:bg-neutral-900/60 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-700"
            :aria-expanded="!!expandedCells[cell.index]"
            :aria-controls="`coder-cell-body-${cell.index}`"
            @click="toggle(cell.index)"
          >
            <span class="mono text-[11px] text-neutral-500 tabular-nums">
              #{{ cell.index }}
            </span>
            <span class="text-sm text-neutral-200">Cell {{ cell.index }}</span>
            <span
              v-if="cell.errored"
              class="mono text-[11px] px-1.5 py-0.5 rounded border border-red-900 bg-red-950/60 text-red-300"
            >
              error
            </span>
            <span
              v-if="formatDuration(cell.durationMs)"
              class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-400 tabular-nums"
            >
              {{ formatDuration(cell.durationMs) }}
            </span>
            <span
              v-if="cell.figurePaths.length > 0"
              class="mono text-[11px] text-neutral-500"
            >
              {{ cell.figurePaths.length }}
              fig{{ cell.figurePaths.length === 1 ? "" : "s" }}
            </span>
            <span
              class="ml-auto mono text-[11px] text-neutral-500 w-3 text-center"
              aria-hidden="true"
            >
              {{ expandedCells[cell.index] ? "▾" : "▸" }}
            </span>
          </button>

          <div
            v-show="expandedCells[cell.index]"
            :id="`coder-cell-body-${cell.index}`"
            class="px-3 py-3 border-t border-neutral-800 space-y-2"
          >
            <!-- Source -->
            <div v-if="cell.source">
              <div class="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">
                Source
              </div>
              <pre
                class="mono text-xs text-neutral-200 whitespace-pre-wrap break-words bg-neutral-950/80 rounded border border-neutral-800 p-2 overflow-auto max-h-[40vh]"
              >{{ cell.source }}</pre>
            </div>

            <!-- stdout -->
            <div v-if="cell.stdout">
              <div class="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">
                stdout
              </div>
              <pre
                class="mono text-xs text-neutral-300 whitespace-pre-wrap break-words bg-neutral-900 rounded border border-neutral-800 p-2 overflow-auto max-h-[30vh]"
              >{{ cell.stdout }}</pre>
            </div>

            <!-- stderr -->
            <div v-if="cell.stderr">
              <div class="text-[11px] uppercase tracking-wider text-red-400 mb-1">
                stderr
              </div>
              <pre
                class="mono text-xs text-red-200 whitespace-pre-wrap break-words bg-red-950/30 rounded border border-red-900/60 p-2 overflow-auto max-h-[30vh]"
              >{{ cell.stderr }}</pre>
            </div>

            <!-- result -->
            <div v-if="cell.resultText">
              <div class="text-[11px] uppercase tracking-wider text-emerald-400 mb-1">
                Result
              </div>
              <pre
                class="mono text-xs text-emerald-100 whitespace-pre-wrap break-words bg-emerald-950/20 rounded border border-emerald-900/50 p-2 overflow-auto max-h-[30vh]"
              >{{ cell.resultText }}</pre>
            </div>

            <!-- figures -->
            <div v-if="cell.figurePaths.length > 0">
              <div class="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">
                Figures
              </div>
              <div class="flex flex-wrap gap-2">
                <a
                  v-for="(fpath, i) in cell.figurePaths"
                  :key="fpath"
                  :href="figUrlFor(fpath)"
                  target="_blank"
                  rel="noopener"
                  class="block rounded border border-neutral-800 bg-neutral-950/60 overflow-hidden hover:border-sky-700"
                >
                  <img
                    :src="figUrlFor(fpath)"
                    :alt="`Cell ${cell.index} figure ${i + 1}`"
                    class="block w-[160px] h-[120px] object-contain bg-neutral-950"
                    loading="lazy"
                    @error="onFigureError"
                  />
                </a>
              </div>
            </div>

            <div
              v-if="
                !cell.source &&
                !cell.stdout &&
                !cell.stderr &&
                !cell.resultText &&
                cell.figurePaths.length === 0
              "
              class="mono text-xs text-neutral-600 italic"
            >
              (empty cell)
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
