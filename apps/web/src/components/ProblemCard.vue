<script setup lang="ts">
// Problem card — empty state (editable) and active state (read-only).
// Emits `start` with {problemText, competition} when the user clicks Start.
import { ref, watch } from "vue";
import T from "./T.vue";
import { useRunSettingsStore, type Competition } from "@/stores/runSettings";
import { useI18n } from "@/composables/useI18n";

const props = defineProps<{
  mode: "new" | "active";
  problemText?: string;
  runId?: string;
  competition?: string;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  (e: "start", payload: { problemText: string; competition: Competition }): void;
}>();

const settings = useRunSettingsStore();
const i18n = useI18n();

// Local draft for the textarea. When the view flips to active mode we show
// the server-side problem text instead; in new mode we manage a local draft
// here so Pinia doesn't need a separate store.
const draft = ref<string>("");

watch(
  () => props.problemText ?? "",
  (next) => {
    if (props.mode === "active") draft.value = next;
  },
  { immediate: true },
);

const COMPS: { id: Competition; en: string; zh: string }[] = [
  { id: "mcm", en: "MCM / ICM", zh: "MCM / ICM" },
  { id: "cumcm", en: "CUMCM", zh: "CUMCM" },
  { id: "huashu", en: "华数杯", zh: "华数杯" },
  { id: "general", en: "general", zh: "通用" },
];

function pickCompetition(c: Competition) {
  if (props.disabled || props.mode === "active") return;
  settings.setCompetition(c);
}

function submit() {
  const text = draft.value.trim();
  if (!text) return;
  emit("start", { problemText: text, competition: settings.competition });
}
</script>

<template>
  <div class="panel">
    <div class="panel-h">
      <div class="eyebrow">
        01 · <T en="Problem" zh="问题" />
      </div>
      <span
        v-if="mode === 'active' && runId"
        class="mono"
        style="font-size: 10.5px; color: var(--ink-3);"
      >
        run {{ runId.slice(0, 8) }}
      </span>
    </div>
    <div class="panel-b">
      <div class="field">
        <label>
          <T en="Competition" zh="赛事" />
        </label>
        <div class="chipstrip">
          <button
            v-for="c in COMPS"
            :key="c.id"
            type="button"
            :disabled="disabled || mode === 'active'"
            :class="{ on: (mode === 'active' ? (competition ?? '').toLowerCase() === c.id : settings.competition === c.id) }"
            @click="pickCompetition(c.id)"
          >
            {{ i18n.t(c.en, c.zh) }}
          </button>
        </div>
      </div>

      <div class="field">
        <label>
          <T en="Prompt · problem.md" zh="题目 · problem.md" />
        </label>
        <textarea
          v-if="mode === 'new'"
          v-model="draft"
          :placeholder="
            i18n.t(
              'Paste the problem statement. Describe data, objective, and constraints.',
              '粘贴题目描述。说明数据、目标与约束。',
            )
          "
        ></textarea>
        <textarea
          v-else
          :value="props.problemText ?? ''"
          readonly
          :rows="Math.min(10, (props.problemText ?? '').split('\n').length + 2)"
        ></textarea>
      </div>

      <div v-if="mode === 'new'" style="display:flex; gap:8px; margin-top: 4px;">
        <button
          type="button"
          class="btn hi"
          :disabled="disabled || draft.trim().length === 0"
          @click="submit"
        >
          ▶ <T en="Start a run" zh="开始运行" />
        </button>
      </div>
    </div>
  </div>
</template>
