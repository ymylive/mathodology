<script setup lang="ts">
// Tiny bilingual text helper. Prefer <T en="..." zh="..."/> in templates
// over the verbose {{ i18n.t('...', '...') }} interpolation.
//
// When `zh` is omitted it falls back to the English copy — useful for
// strings that are identical across languages (numbers, IDs, etc.).
import { useI18n } from "@/composables/useI18n";

const props = defineProps<{ en: string; zh?: string }>();
const i18n = useI18n();
</script>

<template>
  <span>{{ i18n.t(props.en, props.zh ?? props.en) }}</span>
</template>
