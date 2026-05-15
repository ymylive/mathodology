<script setup lang="ts">
// FinetuneChat — natural-language editing panel rendered below the paper.
//
// The store does all the bookkeeping; this file is presentation only:
//   - render the message history with collapsible reasoning blocks
//   - render tool calls as monospace cards with args + result/error
//   - stream new tokens into the live assistant bubble
//   - submit on Enter (Shift+Enter = newline)
//
// Marked is configured exactly like PaperDraft (gfm, no breaks). We skip
// KaTeX inside chat — overkill for ad-hoc messages, and the assistant rarely
// emits raw $...$ in chat replies (it edits sections instead).
import { computed, nextTick, ref, watch } from "vue";
import { Marked } from "marked";
import { useFinetuneStore } from "@/stores/finetune";
import type { Message, ToolCall } from "@/stores/finetune";
import { useI18n } from "@/composables/useI18n";
import T from "./T.vue";

const i18n = useI18n();

const props = defineProps<{ runId: string }>();
const emit = defineEmits<{
  // Fired when the assistant signals `finetune.done` AND at least one
  // edit-style tool succeeded — the Workbench re-fetches paper.md on this.
  (e: "paper-updated"): void;
}>();

const store = useFinetuneStore();

// --- markdown rendering ---------------------------------------------------
// Plain GFM with no walker — chat doesn't need figure URL rewriting.
const md = new Marked({ gfm: true, breaks: false });

function renderMd(text: string): string {
  if (!text) return "";
  return md.parse(text, { async: false }) as string;
}

// --- input handling -------------------------------------------------------
const input = ref<string>("");
const taRef = ref<HTMLTextAreaElement | null>(null);

// Auto-grow textarea: clamp between 1 and 6 line-heights to keep the panel
// stable on long drafts. The line-height matches the .ft-input rule below.
function autosize(): void {
  const el = taRef.value;
  if (!el) return;
  el.style.height = "auto";
  // 22px per line × max 6 lines + 20px padding budget. Hard ceiling avoids
  // the textarea eating the entire viewport if the user pastes a wall.
  const max = 22 * 6 + 20;
  el.style.height = `${Math.min(el.scrollHeight, max)}px`;
}

watch(input, () => {
  void nextTick(autosize);
});

function onKeydown(ev: KeyboardEvent): void {
  // Enter submits; Shift+Enter inserts a newline (standard chat UX).
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    void submit();
  }
}

async function submit(): Promise<void> {
  const text = input.value.trim();
  if (!text) return;
  if (store.isRunning) return;
  input.value = "";
  await nextTick(autosize);
  await store.send(props.runId, text);
}

// --- example prompts (empty-state chips) ---------------------------------
// Real natural-language phrases the user can click to seed the input.
// Mirrors the Claude Code chat pattern: instead of explaining what tools
// exist, show what asks the agent can fulfil. Each entry has en/zh because
// the panel is bilingual.
interface ExamplePrompt {
  en: string;
  zh: string;
}
const EXAMPLE_PROMPTS: ExamplePrompt[] = [
  {
    en: "Tighten the abstract to 180 words and re-emphasize the dual benefit.",
    zh: "把摘要精简到 180 字以内,并重点突出双重收益。",
  },
  {
    en: "Add a sensitivity analysis paragraph after the model derivation.",
    zh: "在模型推导后增加一段灵敏度分析。",
  },
  {
    en: "Fix the typo \"lampery\" → \"lamprey\" everywhere.",
    zh: "把全文中的 \"lampery\" 改为 \"lamprey\"。",
  },
  {
    en: "Bump K_R to 5000 and regenerate the heatmap.",
    zh: "把 K_R 调到 5000,重新生成热力图。",
  },
];

async function pickExample(p: ExamplePrompt): Promise<void> {
  input.value = i18n.lang === "zh" ? p.zh : p.en;
  await nextTick();
  autosize();
  taRef.value?.focus();
}

// --- capability hints (above the input) ----------------------------------
// What the agent can do, in human terms. Hover shows the underlying tool
// name for power-users who want to know.
interface Capability {
  en: string;
  zh: string;
  tool: string;
}
const CAPABILITIES: Capability[] = [
  { en: "Edit prose", zh: "改写文字", tool: "surgical_edit / edit_section" },
  { en: "Tune constants", zh: "调整常量", tool: "edit_constant" },
  { en: "Redraw figures", zh: "重绘图表", tool: "regenerate_figure" },
  { en: "Recompile PDF", zh: "重新编译 PDF", tool: "recompile_pdf" },
];

// --- paper-updated hook ---------------------------------------------------
// Bumps each time the store finishes a turn that included a successful
// edit. We forward to the Workbench so it re-fetches paper.md.
const lastEmittedToken = ref<number>(0);
watch(
  () => store.paperUpdatedAt,
  (now) => {
    if (now === 0 || now === lastEmittedToken.value) return;
    if (!store.didEdit) return;
    lastEmittedToken.value = now;
    emit("paper-updated");
  },
);

// --- auto-scroll ----------------------------------------------------------
const historyEl = ref<HTMLDivElement | null>(null);
const pinned = ref(true);

function onHistoryScroll(): void {
  const el = historyEl.value;
  if (!el) return;
  pinned.value = el.scrollHeight - el.scrollTop - el.clientHeight < 12;
}

// Watch a coarse trigger so we don't run nextTick on every token character.
const liveSize = computed<number>(() => {
  let n = store.messages.length;
  const last = store.messages[store.messages.length - 1];
  if (last) n += last.text.length + last.toolCalls.length * 1000;
  return n;
});

watch(liveSize, async () => {
  if (!pinned.value) return;
  await nextTick();
  const el = historyEl.value;
  if (el) el.scrollTop = el.scrollHeight;
});

// --- per-message UI state -------------------------------------------------
// Track which message ids have an expanded reasoning panel. Default
// collapsed — most assistant turns won't need to expand at all.
const expandedReasoning = ref<Set<string>>(new Set());
const expandedToolArgs = ref<Set<string>>(new Set()); // keyed by tool call id

function toggleReasoning(id: string): void {
  if (expandedReasoning.value.has(id)) expandedReasoning.value.delete(id);
  else expandedReasoning.value.add(id);
}

function toggleToolArgs(id: string): void {
  if (expandedToolArgs.value.has(id)) expandedToolArgs.value.delete(id);
  else expandedToolArgs.value.add(id);
}

// --- formatting helpers ---------------------------------------------------
const ARG_TRUNCATE = 200;

function fmtArgValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

interface ArgEntry {
  key: string;
  display: string; // possibly-truncated
  full: string;
  truncated: boolean;
}

function argEntries(tc: ToolCall): ArgEntry[] {
  const entries = Object.entries(tc.args ?? {});
  return entries.map(([k, v]) => {
    const full = fmtArgValue(v);
    const truncated = full.length > ARG_TRUNCATE;
    return {
      key: k,
      display: truncated ? full.slice(0, ARG_TRUNCATE) + "…" : full,
      full,
      truncated,
    };
  });
}

function toolStatusSymbol(s: ToolCall["status"]): string {
  if (s === "ok") return "✓";
  if (s === "error") return "✗";
  return "…";
}

function toolStatusClass(s: ToolCall["status"]): string {
  if (s === "ok") return "ft-ok";
  if (s === "error") return "ft-err";
  return "ft-pending";
}

// --- status indicator -----------------------------------------------------
const statusLine = computed<{ en: string; zh: string }>(() => {
  if (store.status === "idle") {
    return { en: "Idle", zh: "空闲" };
  }
  if (store.status === "compiling") {
    return { en: "Compiling PDF…", zh: "正在编译 PDF…" };
  }
  if (store.status === "done") {
    return { en: "Done", zh: "完成" };
  }
  if (store.status === "error") {
    return { en: "Error", zh: "错误" };
  }
  // running
  if (store.activeTool) {
    return {
      en: `Running tool: ${store.activeTool}…`,
      zh: `正在执行工具：${store.activeTool}…`,
    };
  }
  return { en: "Thinking…", zh: "思考中…" };
});

const statusDotClass = computed<string>(() => {
  switch (store.status) {
    case "running":
    case "compiling":
      return "dot-run";
    case "done":
      return "dot-ok";
    case "error":
      return "dot-err";
    default:
      return "dot-idle";
  }
});

// Convenience: typed list for the template.
const messages = computed<Message[]>(() => store.messages);
</script>

<template>
  <section class="ft-panel panel">
    <div class="panel-h">
      <div class="eyebrow">
        06 ·
        <T en="Fine-tune with natural language" zh="自然语言调优论文" />
      </div>
      <span class="ft-status mono">
        <span class="ft-dot" :class="statusDotClass"></span>
        <T :en="statusLine.en" :zh="statusLine.zh" />
      </span>
    </div>

    <div class="panel-b">
      <div
        ref="historyEl"
        class="ft-history"
        @scroll="onHistoryScroll"
      >
        <div v-if="messages.length === 0" class="ft-onboarding">
          <h3 class="ft-onb-title">
            <T en="What would you like to change?" zh="想改哪里?" />
          </h3>
          <p class="ft-onb-sub">
            <T
              en="Describe the change in plain English or Chinese — the agent reads the paper, makes the edit, and recompiles the PDF."
              zh="用自然语言描述改动 — Agent 会读取论文、执行编辑、重新编译 PDF。"
            />
          </p>
          <div class="ft-examples" role="list">
            <button
              v-for="(p, i) in EXAMPLE_PROMPTS"
              :key="i"
              type="button"
              class="ft-example"
              role="listitem"
              :aria-label="i18n.t('Use example prompt', '使用示例提示')"
              @click="pickExample(p)"
            >
              <span class="ft-example-arrow" aria-hidden="true">↗</span>
              <span class="ft-example-text">
                <T :en="p.en" :zh="p.zh" />
              </span>
            </button>
          </div>
        </div>

        <div
          v-for="m in messages"
          :key="m.id"
          class="ft-msg"
          :class="m.role === 'user' ? 'ft-user' : 'ft-asst'"
        >
          <!-- USER bubble: right-aligned plain text -->
          <template v-if="m.role === 'user'">
            <div class="ft-bubble ft-bubble-user">{{ m.text }}</div>
          </template>

          <!-- ASSISTANT: reasoning (collapsed) + tool calls + streamed text -->
          <template v-else>
            <div class="ft-bubble ft-bubble-asst">
              <!-- collapsible reasoning -->
              <details
                v-if="m.reasoning"
                class="ft-reasoning"
                :open="expandedReasoning.has(m.id)"
              >
                <summary
                  :aria-expanded="expandedReasoning.has(m.id)"
                  @click.prevent="toggleReasoning(m.id)"
                >
                  <span class="mono ft-reasoning-toggle">
                    {{ expandedReasoning.has(m.id) ? "▾" : "▸" }}
                    <T en="reasoning" zh="推理" />
                  </span>
                </summary>
                <div class="ft-reasoning-body mono">{{ m.reasoning }}</div>
              </details>

              <!-- tool calls -->
              <div
                v-for="tc in m.toolCalls"
                :key="tc.id"
                class="ft-tool"
                :class="toolStatusClass(tc.status)"
              >
                <div class="ft-tool-h mono">
                  <span class="ft-tool-icon">🔧</span>
                  <span class="ft-tool-name">{{ tc.tool }}</span>
                  <span class="ft-tool-status" :class="toolStatusClass(tc.status)">
                    {{ toolStatusSymbol(tc.status) }}
                  </span>
                </div>
                <div class="ft-tool-args mono">
                  <div
                    v-for="entry in argEntries(tc)"
                    :key="entry.key"
                    class="ft-arg"
                  >
                    <span class="ft-arg-k">{{ entry.key }}:</span>
                    <span class="ft-arg-v">
                      <template v-if="entry.truncated && !expandedToolArgs.has(tc.id)">
                        "{{ entry.display }}"
                      </template>
                      <template v-else>
                        "{{ entry.full }}"
                      </template>
                    </span>
                    <button
                      v-if="entry.truncated"
                      type="button"
                      class="ft-arg-toggle mono"
                      :aria-expanded="expandedToolArgs.has(tc.id)"
                      :aria-label="
                        expandedToolArgs.has(tc.id)
                          ? i18n.t('Collapse tool args', '收起参数')
                          : i18n.t('Expand tool args', '展开参数')
                      "
                      @click="toggleToolArgs(tc.id)"
                    >
                      {{ expandedToolArgs.has(tc.id) ? "−" : "+" }}
                    </button>
                  </div>
                </div>
                <div v-if="tc.status === 'ok' && tc.result" class="ft-tool-result mono">
                  → {{ tc.result }}
                </div>
                <div v-else-if="tc.status === 'error'" class="ft-tool-result ft-err mono">
                  → {{ tc.error ?? "tool failed" }}
                </div>
                <div v-else-if="tc.status === 'pending'" class="ft-tool-result ft-pending mono">
                  → <T en="running…" zh="执行中…" />
                </div>
              </div>

              <!-- streamed assistant text (markdown) -->
              <div
                v-if="m.text"
                class="ft-asst-text markdown-body"
                v-html="renderMd(m.text)"
              ></div>

              <!-- error footer if the turn failed -->
              <div v-if="m.error" class="ft-msg-error mono">
                {{ m.error }}
              </div>

              <!-- caret while we're still streaming this message -->
              <span
                v-if="!m.done && store.isRunning"
                class="ft-caret"
                aria-hidden="true"
              ></span>
            </div>
          </template>
        </div>
      </div>

      <!-- POST error banner (separate from per-message errors) -->
      <div v-if="store.postError" class="ft-post-error mono">
        {{ store.postError }}
      </div>

      <!-- capabilities strip — what the agent can do, plain English -->
      <div class="ft-caps" v-if="!store.isRunning">
        <span class="ft-caps-label mono">
          <T en="Agent can:" zh="Agent 可以:" />
        </span>
        <span
          v-for="c in CAPABILITIES"
          :key="c.tool"
          class="ft-cap mono"
          :title="c.tool"
        >
          <T :en="c.en" :zh="c.zh" />
        </span>
      </div>

      <!-- input bar -->
      <form class="ft-input-bar" @submit.prevent="submit">
        <textarea
          ref="taRef"
          v-model="input"
          class="ft-input"
          rows="1"
          :placeholder="
            store.isRunning
              ? ''
              : i18n.t(
                  'Describe a change to the paper — e.g. “Tighten the abstract”',
                  '描述对论文的改动 — 例如 “精简摘要”',
                )
          "
          :disabled="store.isRunning"
          @keydown="onKeydown"
        ></textarea>
        <button
          type="submit"
          class="btn hi ft-submit"
          :disabled="store.isRunning || input.trim().length === 0"
        >
          <T en="Send" zh="发送" /> →
        </button>
      </form>

      <!-- keyboard hint row -->
      <div class="ft-kbd-hint mono">
        <kbd>↵</kbd>
        <T en="to send" zh="发送" />
        <span class="ft-kbd-sep">·</span>
        <kbd>⇧↵</kbd>
        <T en="for newline" zh="换行" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.ft-panel {
  margin-top: 18px;
}

.ft-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 10.5px;
  color: var(--ink-3);
}
.ft-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ink-3);
  display: inline-block;
}
.ft-dot.dot-idle {
  background: var(--ink-4);
}
.ft-dot.dot-run {
  background: var(--hi);
  box-shadow: 0 0 0 0 rgba(212, 232, 90, 0.55);
  animation: pulse 1.4s infinite;
}
.ft-dot.dot-ok {
  background: var(--ok);
}
.ft-dot.dot-err {
  background: var(--err);
}

/* --- history scroller -------------------------------------------------- */
.ft-history {
  max-height: 320px;
  overflow-y: auto;
  padding: 8px 4px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
/* --- onboarding (empty-state) ----------------------------------------- */
.ft-onboarding {
  padding: 18px 14px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ft-onb-title {
  margin: 0;
  font-family: 'Instrument Serif', serif;
  font-size: 26px;
  line-height: 1.15;
  letter-spacing: -0.01em;
  color: var(--ink);
  font-weight: 400;
}
body.zh .ft-onb-title {
  font-family: 'Noto Serif SC', serif;
}
.ft-onb-sub {
  margin: 0;
  color: var(--ink-3);
  font-size: 12.5px;
  line-height: 1.6;
  max-width: 56ch;
}
.ft-examples {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ft-example {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  border-radius: 2px;
  color: var(--ink-2);
  font-family: 'Inter', sans-serif;
  font-size: 12.5px;
  line-height: 1.45;
  text-align: left;
  cursor: pointer;
  transition: border-color 120ms ease, background 120ms ease, transform 80ms ease;
}
.ft-example:hover,
.ft-example:focus-visible {
  border-color: var(--ink);
  background: var(--paper-2);
  outline: none;
  transform: translateX(2px);
}
.ft-example-arrow {
  flex: 0 0 auto;
  color: var(--ink-3);
  font-size: 11px;
}
.ft-example:hover .ft-example-arrow {
  color: var(--hi);
}
.ft-example-text {
  flex: 1 1 auto;
}

/* --- message rows ------------------------------------------------------ */
.ft-msg {
  display: flex;
  width: 100%;
}
.ft-msg.ft-user {
  justify-content: flex-end;
}
.ft-msg.ft-asst {
  justify-content: flex-start;
}

.ft-bubble {
  max-width: 88%;
  padding: 10px 14px;
  border-radius: 2px;
  font-size: 13.5px;
  line-height: 1.55;
  border: 1px solid var(--rule-soft);
}
.ft-bubble-user {
  background: var(--accent);
  color: #ECE5D7;
  border-color: var(--accent);
  white-space: pre-wrap;
  word-break: break-word;
}
.ft-bubble-asst {
  background: var(--paper-2);
  color: var(--ink);
}

/* --- reasoning collapsible -------------------------------------------- */
.ft-reasoning {
  margin-bottom: 8px;
}
.ft-reasoning summary {
  cursor: pointer;
  list-style: none;
}
.ft-reasoning summary::-webkit-details-marker {
  display: none;
}
.ft-reasoning-toggle {
  font-size: 10.5px;
  color: var(--ink-3);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.ft-reasoning-body {
  margin-top: 6px;
  padding: 8px 10px;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  border-radius: 2px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

/* --- tool-call card --------------------------------------------------- */
.ft-tool {
  border: 1px solid var(--rule-soft);
  border-left: 3px solid var(--ink-3);
  border-radius: 2px;
  background: var(--paper);
  padding: 8px 10px;
  margin: 8px 0;
}
.ft-tool.ft-ok {
  border-left-color: var(--ok);
}
.ft-tool.ft-err {
  border-left-color: var(--err);
}
.ft-tool.ft-pending {
  border-left-color: var(--hi);
  animation: cell-breathe 2.4s ease-in-out infinite;
}
.ft-tool-h {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  margin-bottom: 6px;
}
.ft-tool-icon {
  font-size: 11px;
}
.ft-tool-name {
  font-weight: 600;
  color: var(--ink);
  letter-spacing: 0.02em;
}
.ft-tool-status {
  margin-left: auto;
  font-size: 12px;
  line-height: 1;
}
.ft-tool-status.ft-ok {
  color: var(--ok);
}
.ft-tool-status.ft-err {
  color: var(--err);
}
.ft-tool-status.ft-pending {
  color: var(--ink-3);
}

.ft-tool-args {
  font-size: 11px;
  line-height: 1.55;
  color: var(--ink-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ft-arg {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
}
.ft-arg-k {
  color: var(--ink-3);
}
.ft-arg-v {
  flex: 1 1 60%;
  word-break: break-word;
  white-space: pre-wrap;
}
.ft-arg-toggle {
  font-size: 10px;
  border: 1px solid var(--rule-soft);
  background: var(--paper-2);
  color: var(--ink-2);
  padding: 0 5px;
  height: 16px;
  border-radius: 2px;
  cursor: pointer;
  align-self: flex-start;
}
.ft-arg-toggle:hover {
  background: var(--ink);
  color: var(--paper);
}

.ft-tool-result {
  margin-top: 6px;
  font-size: 11px;
  color: var(--ink-2);
  word-break: break-word;
  white-space: pre-wrap;
  line-height: 1.55;
}
.ft-tool-result.ft-err {
  color: var(--err);
}
.ft-tool-result.ft-pending {
  color: var(--ink-3);
  font-style: italic;
}

/* --- streamed assistant text ----------------------------------------- */
.ft-asst-text {
  margin-top: 4px;
  font-size: 13.5px;
  line-height: 1.6;
}
.ft-asst-text :deep(p) {
  margin: 0 0 8px;
}
.ft-asst-text :deep(p):last-child {
  margin-bottom: 0;
}
.ft-asst-text :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  padding: 0.5px 4px;
  border-radius: 2px;
}
.ft-asst-text :deep(pre) {
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  padding: 8px 10px;
  border-radius: 2px;
  overflow: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11.5px;
  margin: 4px 0 8px;
}
.ft-asst-text :deep(ul),
.ft-asst-text :deep(ol) {
  padding-left: 22px;
  margin: 4px 0 8px;
}

.ft-msg-error {
  margin-top: 8px;
  padding: 6px 8px;
  font-size: 11px;
  color: #6B1F0C;
  background: #F8E1D8;
  border: 1px solid var(--rule-soft);
  border-left: 2px solid var(--err);
  border-radius: 2px;
}

.ft-caret {
  display: inline-block;
  width: 7px;
  height: 14px;
  background: var(--hi);
  margin-left: 4px;
  vertical-align: text-bottom;
  animation: blink 1s step-end infinite;
}

/* --- post-error banner (POST failed) --------------------------------- */
.ft-post-error {
  margin: 8px 0 0;
  padding: 8px 10px;
  font-size: 11.5px;
  background: #F8E1D8;
  border: 1px solid var(--warn);
  color: #6B1F0C;
  border-radius: 2px;
}

/* --- capabilities strip ------------------------------------------------ */
.ft-caps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin: 12px 0 6px;
  font-size: 10.5px;
  letter-spacing: 0.03em;
  color: var(--ink-3);
}
.ft-caps-label {
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-right: 2px;
}
.ft-cap {
  padding: 2px 8px;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  border-radius: 999px;
  color: var(--ink-2);
  cursor: help;
}
.ft-cap:hover {
  border-color: var(--ink-3);
  color: var(--ink);
}

/* --- input bar -------------------------------------------------------- */
.ft-input-bar {
  margin-top: 4px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.ft-input {
  flex: 1;
  min-height: 44px;
  max-height: 152px;
  padding: 12px 14px;
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  line-height: 22px;
  background: var(--paper);
  color: var(--ink);
  border: 1px solid var(--rule);
  border-radius: 2px;
  resize: none;
  overflow-y: auto;
}
.ft-input::placeholder {
  color: var(--ink-4);
  font-style: italic;
}
body.zh .ft-input {
  font-family: 'Inter', system-ui, sans-serif;
}
body.zh .ft-input::placeholder {
  font-style: normal;
}
.ft-input:focus {
  outline: none;
  border-color: var(--ink);
  box-shadow: 0 0 0 1px var(--ink);
}
.ft-input[disabled] {
  background: var(--paper-2);
  cursor: not-allowed;
}
.ft-submit {
  flex: 0 0 auto;
  padding: 11px 16px;
  font-size: 11.5px;
}

/* --- keyboard hint row ------------------------------------------------- */
.ft-kbd-hint {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  color: var(--ink-4);
  letter-spacing: 0.04em;
}
.ft-kbd-hint kbd {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  padding: 1px 5px;
  background: var(--paper);
  border: 1px solid var(--rule-soft);
  border-bottom-width: 2px;
  border-radius: 2px;
  color: var(--ink-2);
}
.ft-kbd-sep {
  opacity: 0.5;
  margin: 0 2px;
}
</style>
