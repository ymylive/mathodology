<script setup lang="ts">
// Export panel — consolidates paper + notebook downloads into one card.
//
// Lets the user pick a competition template, then pulls the rendered bytes
// from the gateway as PDF / DOCX / LaTeX / Markdown / Notebook. The ipynb
// path reuses the existing /runs/:id/notebook endpoint; everything else
// goes through /runs/:id/export/:format?template=<...>.
//
// Buttons disable until the paper is ready (`paperReady`). Per-button
// loading state is tracked so concurrent clicks don't collide.
import { computed, ref, watch } from "vue";
import T from "./T.vue";
import { useI18n } from "@/composables/useI18n";
import {
  exportPaper,
  type ExportFormat,
  type ExportTemplate,
  type ExportError,
} from "@/api/export";

const props = defineProps<{
  runId: string;
  /** Server-reported competition_type from GET /runs/:id. Values: mcm | icm |
   *  cumcm | huashu | other. Used to seed the template selector. */
  competitionType?: string | null;
  /** True once the paper.md artifact exists on disk (i.e. writer finished). */
  paperReady: boolean;
  /** True once notebook.ipynb exists. Independent from the paper flag because
   *  notebook is produced earlier in the pipeline. */
  notebookReady: boolean;
}>();

const i18n = useI18n();

const TEMPLATES: { id: ExportTemplate; en: string; zh: string }[] = [
  { id: "mcm", en: "MCM / ICM", zh: "MCM / ICM" },
  { id: "icm", en: "ICM", zh: "ICM" },
  { id: "cumcm", en: "CUMCM", zh: "CUMCM" },
  { id: "huashu", en: "Huashu Cup", zh: "华数杯" },
];

// Map the server's competition_type onto a sensible template default.
// `other` falls through to cumcm per the spec (Chinese-friendly default).
function templateForCompetition(c: string | null | undefined): ExportTemplate {
  switch ((c ?? "").toLowerCase()) {
    case "mcm":
    case "icm":
      return "mcm";
    case "cumcm":
      return "cumcm";
    case "huashu":
      return "huashu";
    default:
      return "cumcm";
  }
}

const template = ref<ExportTemplate>(templateForCompetition(props.competitionType));

// If the run record loads after the component mounts, re-seed the selector
// from the server-supplied competition_type (but only if the user hasn't
// manually touched it yet).
const userTouched = ref(false);
watch(
  () => props.competitionType,
  (next) => {
    if (userTouched.value) return;
    template.value = templateForCompetition(next);
  },
);

function onTemplateChange(e: Event) {
  const v = (e.target as HTMLSelectElement).value as ExportTemplate;
  template.value = v;
  userTouched.value = true;
}

interface FormatSpec {
  id: ExportFormat;
  icon: string;
  en: string;
  zh: string;
  primary?: boolean;
  /** Some formats don't need the paper artifact — notebook is always safe
   *  as long as notebook.ipynb exists. */
  requires: "paper" | "notebook";
}

const FORMATS: FormatSpec[] = [
  { id: "pdf", icon: "PDF", en: "PDF", zh: "PDF", primary: true, requires: "paper" },
  { id: "docx", icon: "DOCX", en: "DOCX", zh: "DOCX", requires: "paper" },
  { id: "tex", icon: "TeX", en: "LaTeX", zh: "LaTeX", requires: "paper" },
  { id: "md", icon: "MD", en: "Markdown", zh: "Markdown", requires: "paper" },
  { id: "ipynb", icon: "IPYNB", en: "Notebook", zh: "Notebook", requires: "notebook" },
];

// Per-format loading flags keyed by format id.
const busy = ref<Record<string, boolean>>({});
const lastError = ref<string | null>(null);

function isReady(spec: FormatSpec): boolean {
  return spec.requires === "paper" ? props.paperReady : props.notebookReady;
}

function isBusy(spec: FormatSpec): boolean {
  return !!busy.value[spec.id];
}

const anyBusy = computed(() => Object.values(busy.value).some(Boolean));

function messageForError(err: unknown): string {
  const e = err as ExportError;
  const status = typeof e?.status === "number" ? e.status : 0;
  if (status === 404) {
    return i18n.t(
      "Paper is not ready yet.",
      "论文还未生成。",
    );
  }
  if (status === 422) {
    return i18n.t(
      "Template or format not supported.",
      "模板或格式不支持。",
    );
  }
  if (status === 503) {
    return i18n.t(
      "Server is missing tectonic/pandoc — contact the administrator.",
      "服务器缺少 tectonic/pandoc,请联系管理员。",
    );
  }
  if (status === 500) {
    const tail = e?.detail ? `: ${e.detail}` : "";
    return i18n.t(
      `Compile failed${tail}`,
      `编译失败${tail}`,
    );
  }
  const raw = err instanceof Error ? err.message : String(err);
  return i18n.t(`Export failed: ${raw}`, `导出失败:${raw}`);
}

async function onExport(spec: FormatSpec) {
  if (isBusy(spec) || !isReady(spec)) return;
  lastError.value = null;
  busy.value = { ...busy.value, [spec.id]: true };
  try {
    await exportPaper({
      runId: props.runId,
      format: spec.id,
      // Notebook ignores the template param (it's a direct artifact fetch),
      // but we pass it anyway for everything else.
      template: spec.id === "ipynb" ? undefined : template.value,
    });
  } catch (err) {
    console.error("[ExportPanel] export failed", err);
    lastError.value = messageForError(err);
  } finally {
    busy.value = { ...busy.value, [spec.id]: false };
  }
}
</script>

<template>
  <div class="panel export-panel">
    <div class="panel-h">
      <div class="eyebrow">
        <T en="Export" zh="导出" />
      </div>
      <span class="mono hint" v-if="!paperReady">
        <T en="paper not ready" zh="论文未就绪" />
      </span>
    </div>
    <div class="panel-b">
      <div class="field">
        <label for="export-template">
          <T en="Template" zh="模板" />
        </label>
        <select
          id="export-template"
          :value="template"
          :disabled="anyBusy"
          @change="onTemplateChange"
        >
          <option v-for="t in TEMPLATES" :key="t.id" :value="t.id">
            {{ i18n.t(t.en, t.zh) }}
          </option>
        </select>
      </div>

      <div class="field" style="margin-bottom: 0;">
        <label>
          <T en="Format" zh="格式" />
        </label>
        <div class="fmt-row">
          <button
            v-for="spec in FORMATS"
            :key="spec.id"
            type="button"
            class="btn"
            :class="{ hi: spec.primary, ghost: !spec.primary }"
            :disabled="!isReady(spec) || isBusy(spec) || anyBusy"
            :aria-label="i18n.t(`Download ${spec.en}`, `下载 ${spec.zh}`)"
            :title="
              !isReady(spec)
                ? i18n.t('Not ready yet', '尚未就绪')
                : i18n.t(`Download ${spec.en}`, `下载 ${spec.zh}`)
            "
            @click="onExport(spec)"
          >
            <template v-if="isBusy(spec)">
              <span class="spinner" aria-hidden="true">○</span>
              <T en="…generating" zh="生成中…" />
            </template>
            <template v-else>
              <span class="fmt-icon mono" aria-hidden="true">{{ spec.icon }}</span>
              <T :en="spec.en" :zh="spec.zh" />
            </template>
          </button>
        </div>
      </div>

      <div
        v-if="lastError"
        class="export-error"
        role="alert"
      >
        {{ lastError }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.export-panel {
  margin-top: 20px;
}
.hint {
  font-size: 10.5px;
  color: var(--ink-3);
}
.fmt-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.fmt-row .btn {
  min-width: 118px;
  justify-content: center;
}
.fmt-icon {
  font-size: 10px;
  letter-spacing: 0.08em;
  opacity: 0.75;
}
.btn.hi .fmt-icon { opacity: 1; }

.spinner {
  display: inline-block;
  animation: export-spin 0.9s linear infinite;
}
@keyframes export-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.export-error {
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--warn);
  background: #F8E1D8;
  border-radius: 2px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  line-height: 1.5;
  color: #6B1F0C;
  word-break: break-word;
}

@media (max-width: 560px) {
  .fmt-row .btn { min-width: 0; flex: 1 1 calc(50% - 4px); }
}
</style>
