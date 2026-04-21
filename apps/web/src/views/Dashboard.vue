<script setup lang="ts">
// Dashboard — 24h headline, 4-stat strip, recent runs, 7-day activity,
// provider share. Every number is wired to a real endpoint; we render an
// em-dash + "pending" label when the backend has nothing to show rather
// than fabricating a sample.
//
// Data sources:
//   - GET /stats/summary?window=24h   — 24h counts + median cost + p95 latency
//     (refetched every 30s for an SWR-ish feel)
//   - GET /stats/providers?window=7d  — provider share bars
//   - GET /runs?limit=200             — recent list + 7-day activity bars
//
// No per-window comparison endpoint exists, so the "+12% vs prev" deltas from
// the mockup are omitted entirely rather than hand-rolled client-side.
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { http } from "@/api/http";
import T from "@/components/T.vue";
import { useI18n } from "@/composables/useI18n";
import { useCountUp } from "@/composables/useCountUp";
import {
  fetchSummary,
  fetchProviderShare,
  type StatsSummary,
  type ProviderShare,
} from "@/api/stats";

interface RunSummary {
  run_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  problem_text: string;
  competition_type: string;
  cost_rmb: number;
}

interface ListRunsResp {
  items: RunSummary[];
}

const i18n = useI18n();
const runs = ref<RunSummary[]>([]);
const runsLoaded = ref(false);

const summary = ref<StatsSummary | null>(null);
const summaryLoaded = ref(false);
const providers = ref<ProviderShare | null>(null);
const providersLoaded = ref(false);

let summaryTimer: number | null = null;

async function loadSummary() {
  try {
    summary.value = await fetchSummary("24h");
  } catch (err) {
    console.error("[Dashboard] /stats/summary fetch failed", err);
    summary.value = null;
  } finally {
    summaryLoaded.value = true;
  }
}

async function loadProviders() {
  try {
    providers.value = await fetchProviderShare("7d");
  } catch (err) {
    console.error("[Dashboard] /stats/providers fetch failed", err);
    providers.value = null;
  } finally {
    providersLoaded.value = true;
  }
}

async function loadRuns() {
  try {
    const res = await http.get<ListRunsResp>("/runs?limit=200");
    runs.value = Array.isArray(res.items) ? res.items : [];
  } catch (err) {
    console.error("[Dashboard] /runs fetch failed", err);
    runs.value = [];
  } finally {
    runsLoaded.value = true;
  }
}

onMounted(() => {
  void loadSummary();
  void loadProviders();
  void loadRuns();

  // SWR-ish: refetch the 24h summary every 30s. The summary endpoint is
  // cheap (single SQL aggregation) so this is fine to poll. We don't
  // re-run the full /runs list fetch or the providers fetch on this
  // cadence — both are heavier and less time-sensitive.
  summaryTimer = window.setInterval(() => {
    void loadSummary();
  }, 30_000);
});

onBeforeUnmount(() => {
  if (summaryTimer !== null) window.clearInterval(summaryTimer);
});

// --- headline + 4-stat strip -----------------------------------------------
const hasRuns24h = computed(
  () => summary.value !== null && summary.value.total_runs > 0,
);

// Count-up targets. We tween toward 0 when there is no data; the template
// renders the em-dash in that case rather than showing "0" which could read
// as a real zero-runs outcome.
const runs24hTarget = computed(() =>
  hasRuns24h.value && summary.value ? summary.value.total_runs : 0,
);
const successTarget = computed(() =>
  hasRuns24h.value && summary.value ? summary.value.success_rate * 100 : 0,
);
const costTarget = computed(() =>
  hasRuns24h.value && summary.value && summary.value.median_cost_rmb !== null
    ? summary.value.median_cost_rmb
    : 0,
);
const p95TargetSec = computed(() =>
  hasRuns24h.value && summary.value && summary.value.p95_latency_ms !== null
    ? summary.value.p95_latency_ms / 1000
    : 0,
);

const runs24hDisplay = useCountUp(runs24hTarget);
const successDisplay = useCountUp(successTarget);
const costDisplay = useCountUp(costTarget);
const p95Display = useCountUp(p95TargetSec);

function fmtLatencyNum(n: number): string {
  const m = Math.floor(n / 60);
  const s = Math.floor(n % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const costCellRender = computed(
  () =>
    hasRuns24h.value &&
    summary.value !== null &&
    summary.value.median_cost_rmb !== null,
);
const p95CellRender = computed(
  () =>
    hasRuns24h.value &&
    summary.value !== null &&
    summary.value.p95_latency_ms !== null,
);

// --- recent runs list (first 20) --------------------------------------------
const recent = computed(() => runs.value.slice(0, 20));

function shortId(id: string): string {
  return id.replace(/-/g, "").slice(0, 6);
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function durSec(r: RunSummary): number | null {
  const a = Date.parse(r.created_at);
  const b = Date.parse(r.updated_at);
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return null;
  return (b - a) / 1000;
}

function fmtDuration(r: RunSummary): string {
  if (r.status === "queued") return "—";
  const sec = durSec(r);
  if (sec === null) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  if (m === 0) return `0:${s.toString().padStart(2, "0")}s`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function statusTag(status: string): { cls: string; en: string; zh: string; glyph: string } {
  switch (status) {
    case "done":
      return { cls: "ok", en: "done", zh: "完成", glyph: "✓" };
    case "running":
      return { cls: "run", en: "running", zh: "运行中", glyph: "●" };
    case "queued":
      return { cls: "q", en: "queued", zh: "排队", glyph: "◷" };
    case "failed":
      return { cls: "err", en: "failed", zh: "失败", glyph: "!" };
    case "cancelled":
      return { cls: "q", en: "cancelled", zh: "已取消", glyph: "◌" };
    default:
      return { cls: "q", en: status, zh: status, glyph: "·" };
  }
}

function costTag(r: RunSummary): string {
  if (r.status === "queued") return "—";
  const c = r.cost_rmb;
  if (typeof c !== "number" || Number.isNaN(c)) return "—";
  return `¥${c.toFixed(2)}`;
}

// --- 7-day activity ---------------------------------------------------------
interface Bucket {
  labelShort: string;
  count: number;
  isToday: boolean;
}

const WEEKDAYS_EN: readonly string[] = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const WEEKDAYS_ZH: readonly string[] = ["日", "一", "二", "三", "四", "五", "六"];

const days = computed<Bucket[]>(() => {
  // Seven buckets ending today (inclusive). Keyed by local YYYY-MM-DD so
  // UTC vs local-midnight confusion doesn't cause off-by-one.
  const now = new Date();
  const buckets: Bucket[] = [];
  const keys: string[] = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    const key = `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
    const idx = d.getDay();
    const short = i18n.lang === "zh" ? WEEKDAYS_ZH[idx] : WEEKDAYS_EN[idx];
    buckets.push({
      labelShort: short,
      count: 0,
      isToday: i === 0,
    });
    keys.push(key);
  }

  for (const r of runs.value) {
    const d = new Date(r.created_at);
    if (Number.isNaN(d.getTime())) continue;
    const key = `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
    const idx = keys.indexOf(key);
    if (idx !== -1) buckets[idx].count += 1;
  }

  return buckets;
});

const weekTotal = computed(() =>
  days.value.reduce((acc, b) => acc + b.count, 0),
);

const weekMax = computed(() =>
  Math.max(1, ...days.value.map((b) => b.count)),
);

function barHeight(count: number): string {
  // When count is 0 we keep a hairline so the column is still visible in
  // its empty state — users shouldn't lose the day marker entirely.
  if (count === 0) return "0%";
  const pct = (count / weekMax.value) * 100;
  return `${Math.max(3, pct)}%`;
}
</script>

<template>
  <div class="dashboard">
    <main class="wrap">
      <div class="eyebrow" style="margin-bottom:8px;">
        <T en="Fleet · last 24h" zh="集群 · 过去 24 小时" />
      </div>
      <h1 class="hero-h">
        <template v-if="!summaryLoaded">
          <span class="dim-inline">
            <T en="Loading last 24h…" zh="加载过去 24 小时…" />
          </span>
        </template>
        <template v-else-if="hasRuns24h && summary">
          {{ summary.total_runs }}
          <T en="runs in the last 24h." zh="个运行，过去 24 小时。" />
        </template>
        <template v-else>
          <T en="No runs in the last 24 hours." zh="过去 24 小时内暂无运行。" />
        </template>
      </h1>

      <!-- 4-stat strip.  Deltas (+12%, −¥0.04) omitted: no prev-window endpoint. -->
      <div class="stats">
        <div class="stat">
          <div class="k"><T en="Runs · 24h" zh="运行 · 24h" /></div>
          <div class="v" v-if="hasRuns24h">{{ Math.round(runs24hDisplay) }}</div>
          <div class="v" v-else-if="summaryLoaded">—</div>
          <div class="v dim-inline" v-else>…</div>
        </div>
        <div class="stat">
          <div class="k"><T en="Success" zh="成功率" /></div>
          <div class="v" v-if="hasRuns24h">{{ successDisplay.toFixed(1) }}%</div>
          <div class="v" v-else-if="summaryLoaded">—</div>
          <div class="v dim-inline" v-else>…</div>
        </div>
        <div class="stat">
          <div class="k"><T en="Median cost" zh="成本中位" /></div>
          <div class="v" v-if="costCellRender">¥ {{ costDisplay.toFixed(2) }}</div>
          <div class="v" v-else-if="summaryLoaded">—</div>
          <div class="v dim-inline" v-else>…</div>
        </div>
        <div class="stat">
          <div class="k"><T en="p95 latency" zh="p95 延迟" /></div>
          <div class="v" v-if="p95CellRender">{{ fmtLatencyNum(p95Display) }}</div>
          <div class="v" v-else-if="summaryLoaded">—</div>
          <div class="v dim-inline" v-else>…</div>
        </div>
      </div>

      <!-- 2-col: runs + right column -->
      <div class="grid2">
        <!-- runs ledger -->
        <div class="panel">
          <div class="panel-h">
            <h3><T en="Recent runs" zh="最近运行" /></h3>
            <span class="mono" style="font-size:10.5px;color:var(--ink-3);">
              <T en="live" zh="实时" /> · <span class="caret"></span>
            </span>
          </div>
          <div class="panel-b">
            <div v-if="!runsLoaded" class="empty-runs">
              <T en="Loading runs…" zh="加载中…" />
            </div>
            <div v-else-if="recent.length === 0" class="empty-runs">
              <div>
                <T
                  en="No runs yet. Start one from the Workbench."
                  zh="暂无运行。请从工作台开启第一个。"
                />
              </div>
              <RouterLink class="btn hi" :to="{ name: 'workbench' }" style="margin-top: 12px;">
                ▶ <T en="Open workbench" zh="打开工作台" />
              </RouterLink>
            </div>
            <TransitionGroup v-else tag="ul" name="run-row" class="runs">
              <RouterLink
                v-for="(r, i) in recent"
                :key="r.run_id"
                :to="{ name: 'workbench', params: { run_id: r.run_id } }"
                :style="{
                  textDecoration: 'none',
                  color: 'inherit',
                  transitionDelay: (i * 40) + 'ms',
                }"
              >
                <li>
                  <span class="id">#{{ shortId(r.run_id) }}</span>
                  <div class="t">
                    {{ truncate(r.problem_text || "—", 48) }}
                    <span class="sub">{{ r.competition_type || "—" }}</span>
                  </div>
                  <span class="s">{{ fmtDuration(r) }}</span>
                  <span class="s">{{ costTag(r) }}</span>
                  <span>
                    <span :class="['tag', statusTag(r.status).cls]">
                      {{ statusTag(r.status).glyph }}
                      <T :en="statusTag(r.status).en" :zh="statusTag(r.status).zh" />
                    </span>
                    <span class="chev">→</span>
                  </span>
                </li>
              </RouterLink>
            </TransitionGroup>
          </div>
          <div
            v-if="runs.length > 20"
            class="panel-h"
            style="border-top:1px solid var(--rule-soft); border-bottom:none; justify-content:center;"
          >
            <span class="mono" style="font-size:11px; color:var(--ink-3);">
              <T en="Showing 20 of" zh="显示 20 / 共" /> {{ runs.length }}
            </span>
          </div>
        </div>

        <!-- right column -->
        <div>
          <!-- 7-day activity -->
          <div class="panel" style="margin-bottom:20px;">
            <div class="panel-h">
              <h3><T en="7-day activity" zh="7 日运行量" /></h3>
              <span class="mono" style="font-size:11px; color:var(--ink-3);">
                {{ weekTotal }} <T en="runs" zh="次" />
              </span>
            </div>
            <div class="days">
              <div
                v-for="(d, i) in days"
                :key="i"
                :class="['day', d.isToday ? 'hi' : '']"
              >
                <div
                  class="b"
                  :style="{ height: barHeight(d.count), '--i': i }"
                ></div>
                <div class="l">
                  <b v-if="d.isToday">{{ d.labelShort }}</b>
                  <template v-else>{{ d.labelShort }}</template>
                  <span class="count-label">{{ d.count }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- providers -->
          <div class="panel">
            <div class="panel-h">
              <h3><T en="Provider share" zh="模型占比" /></h3>
              <span
                v-if="providers && providers.items.length > 0"
                class="mono"
                style="font-size:11px; color:var(--ink-3);"
              >
                ¥ {{ providers.total_cost_rmb.toFixed(2) }} · 7d
              </span>
            </div>
            <div class="panel-b">
              <div v-if="!providersLoaded" class="empty-runs">
                <T en="Loading providers…" zh="加载中…" />
              </div>
              <div
                v-else-if="!providers || providers.items.length === 0"
                class="empty-runs"
              >
                <T
                  en="No LLM cost recorded yet."
                  zh="暂无 LLM 成本记录。"
                />
              </div>
              <div v-else>
                <div
                  v-for="(p, i) in providers.items"
                  :key="p.model"
                  class="prov"
                >
                  <div>
                    <div class="n">{{ p.model }}</div>
                    <div class="bar">
                      <span
                        :style="{
                          width: p.share_pct + '%',
                          background: i === 0 ? 'var(--accent)' : 'var(--ink)',
                          '--i': i,
                        }"
                      ></span>
                    </div>
                  </div>
                  <div class="pct">{{ p.share_pct.toFixed(1) }}%</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.dim-inline {
  color: var(--ink-3);
  font-style: italic;
}
.empty-runs {
  padding: 24px 18px;
  color: var(--ink-3);
  font-style: italic;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.count-label {
  margin-left: 4px;
  color: var(--ink-3);
  opacity: 0.6;
  font-weight: normal;
}
</style>
