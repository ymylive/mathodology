<script setup lang="ts">
// Search source / engine preferences for the new-run form.
//
// Lives alongside the Problem card on the empty-state Workbench. Every
// change writes through to localStorage (via the Pinia store) so there's
// no explicit save — what the user sees is what gets sent with POST /runs.
//
// Three knobs:
//   1. primary    — tavily | open_websearch | none
//   2. engines    — multi-select subset of open-websearch engines
//   3. tavily depth + fallback threshold
//
// Capability-awareness: when the gateway reports tavily_available=false we
// grey out the Tavily radio and show a short "admin didn't configure" hint.
// Engines not listed in `capabilities.available_engines` render disabled so
// the user sees the full set but can't opt into unreachable ones.

import { computed, ref } from "vue";
import T from "./T.vue";
import { useI18n } from "@/composables/useI18n";
import {
  useSearchConfigStore,
  type SearchEngine,
  type SearchPrimary,
  type TavilyDepth,
} from "@/stores/searchConfig";

defineProps<{
  /** Read-only mode (active-run view). Buttons become non-interactive but
   *  still reflect the current config so the user sees what was used. */
  readonly?: boolean;
}>();

const store = useSearchConfigStore();
const i18n = useI18n();

// Collapsed state. Defaults to expanded; we don't persist it because a
// hidden panel hides unrelated-looking knobs and users tend to toggle it
// with a specific run in mind.
const collapsed = ref(false);

// ---- derived availability -------------------------------------------------

const tavilyAvailable = computed<boolean>(() => {
  // While capabilities are loading (null), we assume Tavily is unavailable
  // rather than lying to the user. The fetch usually resolves in <200ms.
  const caps = store.capabilities;
  if (!caps) return false;
  return caps.tavily_available === true;
});

const openWebsearchAvailable = computed<boolean>(() => {
  const caps = store.capabilities;
  if (!caps) return true; // optimistic until we know otherwise
  return caps.open_websearch_available === true;
});

// When capabilities haven't loaded yet we permit every engine so the user
// can still pick their preferences on first render. Once caps arrive we
// trim to what the gateway reports.
function isEngineAvailable(e: SearchEngine): boolean {
  const caps = store.capabilities;
  if (!caps) return true;
  return caps.available_engines.includes(e);
}

// ---- UI metadata ----------------------------------------------------------

interface PrimaryOption {
  id: SearchPrimary;
  en: string;
  zh: string;
  /** Short zh/en blurb surfaced under the radio. */
  hintEn: string;
  hintZh: string;
}

const PRIMARIES: PrimaryOption[] = [
  {
    id: "tavily",
    en: "Tavily",
    zh: "Tavily",
    hintEn: "Managed API, best relevance for English queries.",
    hintZh: "托管 API,英文检索相关度最高。",
  },
  {
    id: "open_websearch",
    en: "open-webSearch",
    zh: "open-webSearch",
    hintEn: "Self-hosted scraper across the engines selected below.",
    hintZh: "自托管抓取,按下方勾选的引擎检索。",
  },
  {
    id: "none",
    en: "None (arXiv only)",
    zh: "不用(仅 arXiv)",
    hintEn: "Skip web search entirely; rely on the arXiv retriever.",
    hintZh: "完全跳过网络检索,仅使用 arXiv。",
  },
];

interface EngineOption {
  id: SearchEngine;
  label: string;
  /** Small advisory shown inline next to the label. Empty string = no note. */
  noteEn: string;
  noteZh: string;
}

// Label + optional advisory note per engine. Order here also drives the
// rendering order (two-column grid), so put the safer "primary-material"
// engines first and the captcha-prone ones lower.
const ENGINE_LIST: EngineOption[] = [
  { id: "baidu",      label: "Baidu",      noteEn: "", noteZh: "" },
  { id: "duckduckgo", label: "DuckDuckGo", noteEn: "", noteZh: "" },
  { id: "csdn",       label: "CSDN",       noteEn: "", noteZh: "" },
  { id: "juejin",     label: "Juejin",     noteEn: "", noteZh: "" },
  {
    id: "bing",
    label: "Bing",
    noteEn: "often captcha-walled; best as fallback",
    noteZh: "常被验证码挡,建议仅作保底",
  },
  { id: "brave",     label: "Brave",     noteEn: "", noteZh: "" },
  { id: "exa",       label: "Exa",       noteEn: "", noteZh: "" },
  { id: "startpage", label: "Startpage", noteEn: "", noteZh: "" },
];

const DEPTHS: { id: TavilyDepth; en: string; zh: string }[] = [
  { id: "basic",    en: "basic",    zh: "基础" },
  { id: "advanced", en: "advanced", zh: "深度" },
];

// ---- handlers -------------------------------------------------------------

function onPickPrimary(p: SearchPrimary) {
  if (p === "tavily" && !tavilyAvailable.value) return;
  if (p === "open_websearch" && !openWebsearchAvailable.value) return;
  store.setPrimary(p);
}

function onToggleEngine(e: SearchEngine) {
  if (!isEngineAvailable(e)) return;
  store.toggleEngine(e);
}

function onDepth(d: TavilyDepth) {
  store.setTavilyDepth(d);
}

function onThresholdInput(ev: Event) {
  const raw = (ev.target as HTMLInputElement).value;
  const n = Number.parseInt(raw, 10);
  if (!Number.isNaN(n)) store.setFallbackThreshold(n);
}

function isEngineChecked(e: SearchEngine): boolean {
  return store.engines.includes(e);
}

const tavilyDepthDisabled = computed(() => store.effectivePrimary !== "tavily");
</script>

<template>
  <div class="panel">
    <div class="panel-h">
      <div class="eyebrow">
        <T en="Search sources" zh="搜索配置" />
      </div>
      <button
        type="button"
        class="collapse-btn mono"
        :aria-expanded="!collapsed"
        @click="collapsed = !collapsed"
      >
        <template v-if="collapsed">
          <T en="expand" zh="展开" /> +
        </template>
        <template v-else>
          <T en="collapse" zh="折叠" /> −
        </template>
      </button>
    </div>

    <div v-if="!collapsed" class="panel-b">
      <!-- Primary source -->
      <div class="field">
        <label>
          <T en="Primary source" zh="主源" />
        </label>
        <div class="radio-list">
          <label
            v-for="p in PRIMARIES"
            :key="p.id"
            class="radio-row"
            :class="{
              disabled:
                (p.id === 'tavily' && !tavilyAvailable) ||
                (p.id === 'open_websearch' && !openWebsearchAvailable),
            }"
          >
            <input
              type="radio"
              name="search-primary"
              :value="p.id"
              :checked="store.primary === p.id"
              :disabled="
                readonly ||
                (p.id === 'tavily' && !tavilyAvailable) ||
                (p.id === 'open_websearch' && !openWebsearchAvailable)
              "
              @change="onPickPrimary(p.id)"
            />
            <span class="radio-label">
              <span class="radio-title">
                <T :en="p.en" :zh="p.zh" />
                <span
                  v-if="p.id === 'tavily'"
                  class="avail-chip mono"
                  :class="{ ok: tavilyAvailable, miss: !tavilyAvailable }"
                >
                  <template v-if="tavilyAvailable">
                    <T en="key ok" zh="密钥已配置" />
                  </template>
                  <template v-else>
                    <T en="no key" zh="未配置密钥" />
                  </template>
                </span>
              </span>
              <span class="radio-hint mono">
                <T :en="p.hintEn" :zh="p.hintZh" />
              </span>
              <span
                v-if="p.id === 'tavily' && !tavilyAvailable"
                class="radio-warn mono"
              >
                <T
                  en="Admin hasn't configured TAVILY_API_KEY; select open-webSearch instead."
                  zh="管理员未配置 TAVILY_API_KEY,请改用 open-webSearch。"
                />
              </span>
            </span>
          </label>
        </div>
        <div v-if="store.tavilyFallbackActive" class="field-hint mono">
          <T
            en="Saved preference is Tavily, but it's unavailable — falling back to open-webSearch for this run."
            zh="已保存主源为 Tavily,但服务器未配置密钥;本次运行将自动改用 open-webSearch。"
          />
        </div>
      </div>

      <!-- Engine subset -->
      <div class="field">
        <label>
          <T
            en="open-webSearch engines"
            zh="open-webSearch 引擎"
          />
        </label>
        <div class="engine-grid">
          <label
            v-for="e in ENGINE_LIST"
            :key="e.id"
            class="engine-row"
            :class="{ disabled: !isEngineAvailable(e.id) }"
          >
            <input
              type="checkbox"
              :checked="isEngineChecked(e.id)"
              :disabled="readonly || !isEngineAvailable(e.id)"
              @change="onToggleEngine(e.id)"
            />
            <span class="engine-label">
              <span class="engine-name">{{ e.label }}</span>
              <span
                v-if="e.noteEn || e.noteZh"
                class="engine-note mono"
              >
                {{ i18n.t(e.noteEn, e.noteZh) }}
              </span>
              <span
                v-else-if="!isEngineAvailable(e.id)"
                class="engine-note mono"
              >
                <T en="not available" zh="不可用" />
              </span>
            </span>
          </label>
        </div>
        <div class="field-hint mono">
          <T
            en="Used as the primary source when open-webSearch is selected, and as the fallback when Tavily returns too few results."
            zh="选择 open-webSearch 时作为主源;Tavily 结果不足时作为保底。"
          />
        </div>
      </div>

      <!-- Tavily depth -->
      <div class="field">
        <label>
          <T en="Tavily depth" zh="Tavily 检索深度" />
          <span v-if="tavilyDepthDisabled" class="muted-chip">(ignored)</span>
        </label>
        <div class="seg">
          <button
            v-for="d in DEPTHS"
            :key="d.id"
            type="button"
            :class="{ on: store.tavily_depth === d.id }"
            :disabled="readonly || tavilyDepthDisabled"
            @click="onDepth(d.id)"
          >
            <T :en="d.en" :zh="d.zh" />
          </button>
        </div>
      </div>

      <!-- Fallback threshold -->
      <div class="field">
        <label>
          <T en="Fallback threshold" zh="保底阈值" />
        </label>
        <div class="thresh-row">
          <input
            type="number"
            min="0"
            max="50"
            step="1"
            :value="store.fallback_threshold"
            :disabled="readonly"
            @input="onThresholdInput"
          />
          <span class="thresh-unit mono">
            <T en="papers" zh="篇论文" />
          </span>
        </div>
        <div class="field-hint mono">
          <T
            en="When the primary source returns fewer than this many results, the engines above are queried as a fallback. Set to 0 to disable fallback."
            zh="当主源返回结果少于该阈值时,启用上方引擎保底检索。设为 0 则不启用保底。"
          />
        </div>
      </div>

      <!-- Reset -->
      <div class="field" style="margin-bottom: 0;">
        <button
          type="button"
          class="btn ghost reset-btn"
          :disabled="readonly"
          @click="store.resetToDefaults()"
        >
          <T en="Reset defaults" zh="重置默认" />
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.collapse-btn {
  background: transparent;
  border: 1px solid var(--rule-soft);
  border-radius: 2px;
  padding: 3px 8px;
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-3);
  cursor: pointer;
  transition: background-color var(--dur-micro) ease-out, color var(--dur-micro) ease-out;
}
.collapse-btn:hover {
  background: var(--paper-2);
  color: var(--ink);
}

/* Primary-source radio list — stacked rows with a title + hint. */
.radio-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.radio-row {
  display: grid;
  grid-template-columns: 16px 1fr;
  gap: 10px;
  align-items: start;
  padding: 8px 10px;
  border: 1px solid var(--rule-soft);
  border-radius: 2px;
  background: var(--paper);
  cursor: pointer;
}
.radio-row.disabled {
  opacity: 0.55;
  cursor: not-allowed;
  background: var(--paper-2);
}
.radio-row input[type="radio"] {
  margin-top: 4px;
  accent-color: var(--ink);
  cursor: inherit;
}
.radio-label {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.radio-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ink);
  font-weight: 500;
}
.radio-hint {
  font-size: 10.5px;
  color: var(--ink-3);
  letter-spacing: 0.03em;
  line-height: 1.5;
}
.radio-warn {
  font-size: 10px;
  color: var(--warn);
  letter-spacing: 0.04em;
  line-height: 1.5;
  margin-top: 2px;
}

/* Availability chip next to the Tavily title. */
.avail-chip {
  font-size: 9.5px;
  padding: 1px 6px;
  border: 1px solid var(--rule-soft);
  border-radius: 2px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-3);
  background: var(--paper-2);
}
.avail-chip.ok   { color: var(--ok); }
.avail-chip.miss { color: var(--ink-4); }

/* Engine checkbox grid — 2 columns on normal widths. */
.engine-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 14px;
}
@media (max-width: 420px) {
  .engine-grid { grid-template-columns: 1fr; }
}
.engine-row {
  display: grid;
  grid-template-columns: 16px 1fr;
  gap: 8px;
  align-items: start;
  padding: 4px 0;
  cursor: pointer;
}
.engine-row.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.engine-row input[type="checkbox"] {
  margin-top: 3px;
  accent-color: var(--ink);
  cursor: inherit;
}
.engine-label {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-width: 0;
}
.engine-name {
  font-size: 13px;
  color: var(--ink);
}
.engine-note {
  font-size: 10px;
  color: var(--ink-3);
  letter-spacing: 0.03em;
  line-height: 1.4;
  margin-top: 1px;
}

/* Threshold number input. */
.thresh-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.thresh-row input[type="number"] {
  width: 72px;
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}
.thresh-unit {
  font-size: 11px;
  color: var(--ink-3);
  letter-spacing: 0.06em;
}

.reset-btn {
  font-size: 10.5px;
  padding: 7px 12px;
}

.field-hint {
  font-size: 10px;
  color: var(--ink-3);
  margin-top: 6px;
  letter-spacing: 0.04em;
  line-height: 1.5;
}
.muted-chip {
  margin-left: 6px;
  font-family: "JetBrains Mono", monospace;
  font-size: 9.5px;
  color: var(--ink-4);
  letter-spacing: 0.04em;
  text-transform: none;
}
</style>
