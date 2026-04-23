<script setup lang="ts">
// Workbench — two modes:
//   new:    /workbench              — empty-state form, POST /runs on submit.
//   active: /workbench/:run_id      — live pipeline view wired to the store.
//
// The run_id param opens a WebSocket via the Pinia store. We ensure we
// always reset+open when navigating between run IDs so stale state from
// a previous run doesn't leak across.
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { http } from "@/api/http";
import { useRunStore } from "@/stores/run";
import { useI18n } from "@/composables/useI18n";
import { useCountUp } from "@/composables/useCountUp";
import StagePills from "@/components/StagePills.vue";
import ProblemCard from "@/components/ProblemCard.vue";
import SettingsPanel from "@/components/SettingsPanel.vue";
import { useRunSettingsStore } from "@/stores/runSettings";
import EventLog from "@/components/EventLog.vue";
import CellView from "@/components/CellView.vue";
import AgentOutputView from "@/components/AgentOutputView.vue";
import PaperDraft from "@/components/PaperDraft.vue";
import ExportPanel from "@/components/ExportPanel.vue";
import SearchConfigPanel from "@/components/SearchConfigPanel.vue";
import { useSearchConfigStore } from "@/stores/searchConfig";
import T from "@/components/T.vue";

// Shape of GET /runs/:id — we only need the metadata fields here, not the
// replayed events array (the WebSocket already carries those via the store).
interface RunRecord {
  run_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  problem_text: string;
  competition_type: string;
  cost_rmb: number;
  notebook_path: string | null;
  paper_path: string | null;
}

const route = useRoute();
const router = useRouter();
const run = useRunStore();
const settings = useRunSettingsStore();
const searchConfig = useSearchConfigStore();
const i18n = useI18n();

// --- live clock for elapsed counters & stage pill durations -----------------
const now = ref(Date.now());
let tickId: number | null = null;

onMounted(() => {
  tickId = window.setInterval(() => {
    now.value = Date.now();
  }, 500);
});

onBeforeUnmount(() => {
  if (tickId !== null) window.clearInterval(tickId);
});

// --- open the WS when we land on /workbench/:id -----------------------------
const routeRunId = computed<string | null>(() => {
  const p = route.params["run_id"];
  if (typeof p === "string" && p.length > 0) return p;
  return null;
});

// Metadata for the active run — fetched via GET /runs/:id on mount or when
// the route param changes. Null until the fetch resolves.
const runRecord = ref<RunRecord | null>(null);

async function loadRunRecord(id: string) {
  try {
    runRecord.value = await http.get<RunRecord>(`/runs/${id}`);
  } catch (err) {
    console.error("[Workbench] /runs/:id fetch failed", err);
    runRecord.value = null;
  }
}

watch(
  routeRunId,
  (next, prev) => {
    if (next === prev) return;
    if (next === null) {
      // Moved back to the empty-state page — drop the socket.
      run.reset();
      runRecord.value = null;
      return;
    }
    if (run.runId !== next) {
      run.reset();
      runRecord.value = null;
      run.runId = next;
      run.status = "running";
      run.openWs(next);
    }
    void loadRunRecord(next);
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  run.reset();
});

// --- start a new run from the empty-state form ------------------------------
async function onStart(payload: { problemText: string }) {
  // Reset any leftover error / done state from a prior attempt so the guard
  // in startRun doesn't swallow the new submission.
  if (run.status === "failed" || run.status === "done") {
    run.reset();
  }
  await run.startRun(payload.problemText, {
    reasoningEffort: settings.reasoningEffort,
    longContext: settings.longContext,
    modelOverride: settings.model,
    // Snapshot current preferences (applies effectivePrimary so the server
    // sees a real primary rather than "tavily" when no key is configured).
    searchConfig: searchConfig.payload,
  });
  if (run.runId) {
    router.push({ name: "workbench", params: { run_id: run.runId } });
  } else if (run.error) {
    // POST failed; leave the user on the form with the error banner visible.
    console.warn("[Workbench] startRun failed:", run.error);
  }
}

// --- live header bits -------------------------------------------------------
// Problem text is sourced from GET /runs/:id (served by the gateway) so
// runs opened from the Dashboard list still get a real title. The store
// itself doesn't carry problem_text; only runs started in-tab would.
const problemText = computed<string>(() => runRecord.value?.problem_text ?? "");

const firstLine = computed<string>(() => {
  const t = problemText.value;
  if (!t) {
    return i18n.t("Workbench · interactive run", "工作台 · 交互式运行");
  }
  const ln = t.split("\n")[0] ?? t;
  return ln.length > 80 ? ln.slice(0, 77) + "…" : ln;
});

const startLabel = computed<string>(() => {
  const iso = runRecord.value?.created_at;
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
});

// Tween the header cost so live ¥ updates feel continuous, matching the
// EventLog footer. We prefer store.costRmb once the WS has started emitting
// `cost` events (the value moves off zero); otherwise we fall back to the
// snapshot from GET /runs/:id so the header isn't blank while waiting.
const costTarget = computed(() => {
  if (run.costRmb > 0) return run.costRmb;
  return runRecord.value?.cost_rmb ?? 0;
});
const costDisplay = useCountUp(costTarget, { duration: 300 });

// --- active-agent hint for the "current cell" eyebrow ------------------------
const activeAgent = computed<string | null>(() => {
  // The last stage.start whose matching stage.done hasn't arrived yet.
  const evs = run.orderedEvents;
  const starts: Record<string, number> = {};
  const dones: Record<string, number> = {};
  for (const ev of evs) {
    if (!ev.agent) continue;
    if (ev.kind === "stage.start") starts[ev.agent] = (starts[ev.agent] ?? 0) + 1;
    if (ev.kind === "stage.done") dones[ev.agent] = (dones[ev.agent] ?? 0) + 1;
  }
  for (const k of Object.keys(starts)) {
    if ((dones[k] ?? 0) < starts[k]) return k;
  }
  return null;
});

// --- ordered cells for the live kernel panel -------------------------------
const cellIndices = computed<number[]>(() =>
  Object.keys(run.kernelCells)
    .map((k) => Number.parseInt(k, 10))
    .filter((n) => !Number.isNaN(n))
    .sort((a, b) => a - b),
);

// Find the cell index currently executing (startTs but no doneTs) for the
// `active` flag on CellView.
const runningCellIndex = computed<number | null>(() => {
  for (const i of cellIndices.value) {
    const c = run.kernelCells[i];
    if (c.startTs && !c.doneTs) return i;
  }
  return null;
});

// Coder agent output carries the per-cell source code. Extract it.
const coderCellSources = computed<Record<number, string>>(() => {
  const ent = run.outputs["coder"];
  const result: Record<number, string> = {};
  if (!ent) return result;
  const cells = (ent.output["cells"] ?? ent.output["notebook_cells"]) as unknown;
  if (!Array.isArray(cells)) return result;
  cells.forEach((c, i) => {
    if (!c || typeof c !== "object") return;
    const rec = c as Record<string, unknown>;
    const src =
      typeof rec["source"] === "string"
        ? rec["source"]
        : typeof rec["code"] === "string"
          ? rec["code"]
          : "";
    if (src) result[i] = src;
  });
  return result;
});

// --- agent output panels (non-coder) ----------------------------------------
interface OutputPanel {
  agent: string;
  schemaName: string;
  output: Record<string, unknown>;
  ts: string;
}

const outputPanels = computed<OutputPanel[]>(() => {
  const order = ["analyzer", "searcher", "modeler", "writer"];
  const out: OutputPanel[] = [];
  for (const k of order) {
    const ent = run.outputs[k];
    if (!ent) continue;
    if (k === "writer") continue; // PaperDraft rendered separately below
    out.push({
      agent: k,
      schemaName: ent.schemaName,
      output: ent.output,
      ts: ent.ts,
    });
  }
  return out;
});

const paperDraft = computed<Record<string, unknown> | null>(() => {
  const ent = run.outputs["writer"];
  if (!ent || ent.schemaName !== "PaperDraft") return null;
  return ent.output;
});

function agentTitle(agent: string): { en: string; zh: string; num: string } {
  switch (agent) {
    case "analyzer":
      return { en: "Analyzer", zh: "分析员", num: "01" };
    case "searcher":
      return { en: "Searcher", zh: "检索员", num: "02" };
    case "modeler":
      return { en: "Modeler", zh: "建模员", num: "03" };
    case "coder":
      return { en: "Coder", zh: "编程员", num: "04" };
    case "writer":
      return { en: "Writer", zh: "撰写员", num: "05" };
    default:
      return { en: agent, zh: agent, num: "??" };
  }
}
</script>

<template>
  <div class="workbench">
    <main class="wrap">
      <!-- ============================================================
           EMPTY STATE — /workbench
           ============================================================ -->
      <template v-if="routeRunId === null">
        <div
          style="display:flex; justify-content:space-between; align-items:end; gap:24px; margin-bottom:20px;"
        >
          <div>
            <div class="eyebrow" style="margin-bottom:6px;">
              <T en="Workbench · new run" zh="工作台 · 新建运行" />
            </div>
            <h1 class="hero-h">
              <T en="Pose a problem." zh="提出一个问题。" />
            </h1>
          </div>
        </div>

        <div class="ws">
          <div>
            <ProblemCard mode="new" :disabled="run.status === 'queued'" @start="onStart" />
            <!-- Surface any error from the POST /runs call so the user
                 isn't left staring at a silent "nothing happened" screen. -->
            <div
              v-if="run.status === 'failed' && run.error"
              class="error-banner"
              role="alert"
            >
              <strong><T en="Could not start run." zh="无法启动任务。" /></strong>
              <span class="mono">{{ run.error }}</span>
            </div>
          </div>
          <aside>
            <SettingsPanel />
            <SearchConfigPanel style="margin-top: 20px;" />
          </aside>
        </div>
      </template>

      <!-- ============================================================
           ACTIVE STATE — /workbench/:run_id
           ============================================================ -->
      <template v-else>
        <div
          style="display:flex; justify-content:space-between; align-items:end; gap:24px; margin-bottom:20px; flex-wrap: wrap;"
        >
          <div>
            <div class="eyebrow" style="margin-bottom:6px;">
              <T en="Workbench · interactive run" zh="工作台 · 交互式运行" />
            </div>
            <h1 class="hero-h">
              {{ firstLine }}
            </h1>
            <div
              class="mono"
              style="font-size:11px; color: var(--ink-3); margin-top:4px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;"
            >
              <span>run {{ routeRunId!.slice(0, 8) }}</span>
              <span>·</span>
              <span>
                <T en="started" zh="开始于" /> {{ startLabel }}
              </span>
              <span>·</span>
              <span>¥ {{ costDisplay.toFixed(3) }}</span>
            </div>
          </div>
          <div style="display:flex; gap:8px;">
            <button
              class="btn ghost"
              disabled
              :title="i18n.t('pause is not implemented yet', '暂停功能尚未实现')"
            >
              ⏸ <T en="Pause" zh="暂停" />
            </button>
            <RouterLink class="btn hi" :to="{ name: 'workbench' }">
              ▶ <T en="New run" zh="新建运行" />
            </RouterLink>
          </div>
        </div>

        <StagePills :now="now" />

        <div class="ws">
          <!-- LEFT: problem (read-only) + live cells + agent outputs -->
          <div>
            <ProblemCard
              mode="active"
              :run-id="routeRunId!"
              :problem-text="problemText"
              :competition="undefined"
              style="margin-bottom: 20px;"
            />

            <!-- Live kernel cells. The "03 · Coder · live kernel" eyebrow
                 from the mockup is shown once the coder is the active
                 agent or after it's produced cells. -->
            <div class="panel" v-if="cellIndices.length > 0 || activeAgent === 'coder'">
              <div class="panel-h">
                <div class="eyebrow">
                  04 · <T en="Coder · live kernel" zh="编程员 · 实时内核" />
                </div>
                <span class="mono" style="font-size:11px; color: var(--ink-3);">
                  <template v-if="cellIndices.length > 0">
                    <T en="cell" zh="单元" /> {{ cellIndices.length }} · python 3.11
                  </template>
                  <template v-else>python 3.11</template>
                </span>
              </div>
              <div class="panel-b">
                <div v-if="cellIndices.length === 0" class="cell-empty">
                  <T en="kernel booting…" zh="内核启动中…" />
                </div>
                <CellView
                  v-for="i in cellIndices"
                  :key="i"
                  :index="i"
                  :run-id="routeRunId!"
                  :source="coderCellSources[i] ?? ''"
                  :cell="run.kernelCells[i]"
                  :active="runningCellIndex === i"
                />
              </div>
            </div>

            <!-- Per-agent structured outputs (analyzer / searcher / modeler) -->
            <div
              v-for="p in outputPanels"
              :key="p.agent"
              class="panel"
              style="margin-top: 20px;"
            >
              <div class="panel-h">
                <div class="eyebrow">
                  {{ agentTitle(p.agent).num }} ·
                  <T :en="agentTitle(p.agent).en" :zh="agentTitle(p.agent).zh" />
                </div>
                <span class="mono" style="font-size: 10.5px; color: var(--ink-3);">
                  {{ p.schemaName }}
                </span>
              </div>
              <div class="panel-b">
                <AgentOutputView
                  :schema-name="p.schemaName"
                  :output="p.output"
                  :run-id="routeRunId!"
                />
              </div>
            </div>
          </div>

          <!-- RIGHT: settings (read-only) + live log -->
          <aside>
            <SettingsPanel readonly style="margin-bottom: 20px;" />
            <EventLog :run-id="routeRunId!" :now="now" />
          </aside>
        </div>

        <!-- Full-width paper draft below the 2-col grid -->
        <PaperDraft
          v-if="paperDraft"
          :output="paperDraft"
          :run-id="routeRunId!"
        />

        <!-- Consolidated export panel: template picker + PDF / DOCX / TeX /
             MD / Notebook. Replaces the two header download links. -->
        <ExportPanel
          :run-id="routeRunId!"
          :competition-type="runRecord?.competition_type ?? null"
          :paper-ready="!!runRecord?.paper_path"
          :notebook-ready="!!runRecord?.notebook_path"
        />

        <div
          v-if="run.status === 'failed' && run.error"
          class="panel"
          style="margin-top: 18px; border-color: var(--err);"
        >
          <div class="panel-h">
            <div class="eyebrow" style="color: var(--err);">
              <T en="run failed" zh="运行失败" />
            </div>
          </div>
          <div class="panel-b" style="color: var(--err); font-family: 'JetBrains Mono', monospace; font-size: 12px;">
            {{ run.error }}
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<style scoped>
.error-banner {
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid var(--warn);
  background: #F8E1D8;
  border-radius: 2px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11.5px;
  line-height: 1.5;
  color: #6B1F0C;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.error-banner strong {
  font-family: 'Inter', sans-serif;
  font-weight: 600;
  font-size: 12.5px;
}
</style>
