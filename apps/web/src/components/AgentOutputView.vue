<script setup lang="ts">
// Schema dispatcher for the per-agent `agent.output` events. The store
// keys each output by agent; this component picks a specific renderer by
// schema_name. Unknown schemas render a muted "unknown schema" notice so
// a contract rev doesn't silently break the surface.
import AnalyzerOutput from "./AnalyzerOutput.vue";
import SearchFindings from "./SearchFindings.vue";
import ModelSpec from "./ModelSpec.vue";
import T from "./T.vue";

defineProps<{
  schemaName: string;
  output: Record<string, unknown>;
  runId: string;
}>();
</script>

<template>
  <component
    :is="AnalyzerOutput"
    v-if="schemaName === 'AnalyzerOutput'"
    :output="output"
  />
  <component
    :is="SearchFindings"
    v-else-if="schemaName === 'SearchFindings'"
    :output="output"
  />
  <component
    :is="ModelSpec"
    v-else-if="schemaName === 'ModelSpec'"
    :output="output"
  />
  <div v-else class="output-panel">
    <p style="font-style: italic; color: var(--ink-3);">
      <T
        en="unknown output schema — raw payload hidden"
        zh="未知 output schema — 已隐藏原始数据"
      />
    </p>
  </div>
</template>
