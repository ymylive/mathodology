<script setup lang="ts">
// Model / routing / retry / HMML. Persisted via useRunSettingsStore.
//
// The model list is fetched live from GET /providers. Each option carries
// the per-1M-token price read from the gateway config (no hand-maintained
// price table here). Providers that lack an API key are shown disabled with
// a `(no API key)` suffix so the user sees them but can't select them.
//
// Routing priority / retry budget / HMML are kept as cosmetic controls —
// the backend doesn't honour them yet; they exist so the form shape is
// ready for when it does.
import { computed, onMounted, ref, watch } from "vue";
import T from "./T.vue";
import {
  useRunSettingsStore,
  type ReasoningEffort,
  type Routing,
  type RetryBudget,
} from "@/stores/runSettings";
import { fetchProviders, type ProviderInfo } from "@/api/stats";

defineProps<{ readonly?: boolean }>();

const settings = useRunSettingsStore();

interface ModelOption {
  id: string;
  label: string;
  disabled: boolean;
}

const providers = ref<ProviderInfo[]>([]);
const loaded = ref(false);

const modelOptions = computed<ModelOption[]>(() => {
  const opts: ModelOption[] = [];
  for (const p of providers.value) {
    const disabled = !p.has_key;
    const priceTag = `¥${p.price_input_per_1m}/${p.price_output_per_1m} per 1M`;
    const suffix = disabled ? " · (no API key)" : ` · ${priceTag}`;
    for (const m of p.models) {
      opts.push({
        id: m,
        label: `${m}${suffix}`,
        disabled,
      });
    }
  }
  return opts;
});

// Preselect the first provider's first enabled model if the stored model
// isn't present in the live list. The stored default "deepseek-chat" may
// not match whatever the gateway actually exposes.
watch(
  modelOptions,
  (opts) => {
    if (opts.length === 0) return;
    const current = opts.find((o) => o.id === settings.model);
    if (current && !current.disabled) return;
    const firstEnabled = opts.find((o) => !o.disabled);
    if (firstEnabled) settings.setModel(firstEnabled.id);
  },
);

onMounted(async () => {
  try {
    const res = await fetchProviders();
    providers.value = Array.isArray(res.items) ? res.items : [];
  } catch (err) {
    console.error("[SettingsPanel] /providers fetch failed", err);
    providers.value = [];
  } finally {
    loaded.value = true;
  }
});

const ROUTES: { id: Routing; en: string; zh: string }[] = [
  { id: "cost", en: "cost", zh: "成本" },
  { id: "balanced", en: "balanced", zh: "均衡" },
  { id: "latency", en: "latency", zh: "速度" },
];

const RETRIES: RetryBudget[] = [0, 1, 2, 3];

const EFFORTS: { id: ReasoningEffort; en: string; zh: string }[] = [
  { id: "off", en: "off", zh: "关" },
  { id: "low", en: "low", zh: "低" },
  { id: "medium", en: "medium", zh: "中" },
  { id: "high", en: "high", zh: "高" },
];

// Models that actually honour reasoning_effort / thinking budget. The list
// matches the gateway's per-provider adapter — if a model isn't here the
// backend will silently drop the field, so we surface that with an
// "(ignored)" chip while still keeping the control interactive.
const REASONING_MODEL_RE =
  /^(gpt-5|o1|o3|claude-(haiku-4|sonnet-4|opus-4)|deepseek-reasoner)/i;

const isReasoningModel = computed(() => REASONING_MODEL_RE.test(settings.model));
</script>

<template>
  <div class="panel">
    <div class="panel-h">
      <div class="eyebrow">
        02 · <T en="Settings" zh="设置" />
      </div>
    </div>
    <div class="panel-b">
      <div class="field">
        <label><T en="Model" zh="模型" /></label>
        <select
          :value="settings.model"
          :disabled="readonly || !loaded || modelOptions.length === 0"
          @change="settings.setModel(($event.target as HTMLSelectElement).value)"
        >
          <option v-if="!loaded" :value="settings.model">
            {{ settings.model }} · loading…
          </option>
          <option v-else-if="modelOptions.length === 0" :value="settings.model">
            <T en="no providers configured" zh="未配置模型" />
          </option>
          <option
            v-for="m in modelOptions"
            :key="m.id"
            :value="m.id"
            :disabled="m.disabled"
          >
            {{ m.label }}
          </option>
        </select>
      </div>

      <div class="field">
        <label>
          <T en="Thinking" zh="思考强度" />
          <span v-if="!isReasoningModel" class="muted-chip">(ignored)</span>
        </label>
        <div class="seg">
          <button
            v-for="e in EFFORTS"
            :key="e.id"
            type="button"
            :class="{ on: settings.reasoningEffort === e.id }"
            :disabled="readonly"
            @click="settings.setReasoningEffort(e.id)"
          >
            <T :en="e.en" :zh="e.zh" />
          </button>
        </div>
        <div class="field-hint mono">
          <T
            en="Maps to OpenAI reasoning_effort or Anthropic thinking budget. Ignored by non-reasoning models."
            zh="映射为 OpenAI reasoning_effort 或 Anthropic thinking 预算；普通模型会忽略。"
          />
        </div>
      </div>

      <div class="field">
        <label>
          <T en="Long context (1M)" zh="长上下文 (1M)" />
        </label>
        <div class="seg">
          <button
            type="button"
            :class="{ on: !settings.longContext }"
            :disabled="readonly"
            @click="settings.setLongContext(false)"
          >
            <T en="20k" zh="20k" />
          </button>
          <button
            type="button"
            :class="{ on: settings.longContext }"
            :disabled="readonly"
            @click="settings.setLongContext(true)"
          >
            <T en="1M" zh="1M" />
          </button>
        </div>
        <div class="field-hint mono">
          <T
            en="1M lifts the output cap to 1,000,000 tokens. Only works on 1M-capable models (Claude 3.5 Sonnet 1M, Gemini 2.0, gpt-5-1m). Unsupported models will error."
            zh="启用 1M 将输出上限提升到 1,000,000 token。只对支持 1M 上下文的模型有效(Claude 3.5 Sonnet 1M、Gemini 2.0、gpt-5-1m 等)。其他模型会报错。"
          />
        </div>
      </div>

      <div class="field">
        <label><T en="Routing priority" zh="路由优先" /></label>
        <div class="seg">
          <button
            v-for="r in ROUTES"
            :key="r.id"
            type="button"
            :class="{ on: settings.routing === r.id }"
            :disabled="readonly"
            @click="settings.setRouting(r.id)"
          >
            <T :en="r.en" :zh="r.zh" />
          </button>
        </div>
      </div>

      <div class="field">
        <label><T en="Retry budget" zh="重试次数" /></label>
        <div class="seg">
          <button
            v-for="n in RETRIES"
            :key="n"
            type="button"
            :class="{ on: settings.retry === n }"
            :disabled="readonly"
            @click="settings.setRetry(n)"
          >
            {{ n }}
          </button>
        </div>
      </div>

      <div class="field" style="margin-bottom: 0;">
        <label><T en="Knowledge base (HMML)" zh="知识库 (HMML)" /></label>
        <div class="seg">
          <button
            type="button"
            :class="{ on: !settings.hmml }"
            :disabled="readonly"
            @click="settings.setHmml(false)"
          >
            <T en="off" zh="关" />
          </button>
          <button
            type="button"
            :class="{ on: settings.hmml }"
            :disabled="readonly"
            @click="settings.setHmml(true)"
          >
            <T en="on" zh="开" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.field-hint {
  font-size: 10px;
  color: var(--ink-3);
  margin-top: 6px;
  letter-spacing: 0.04em;
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
