<script setup lang="ts">
import { useI18n } from "@/composables/useI18n";

const i18n = useI18n();
</script>

<template>
  <div class="lang-toggle" role="group" aria-label="Language">
    <button
      type="button"
      :class="{ on: i18n.lang === 'en' }"
      @click="i18n.toggle('en')"
    >
      EN
    </button>
    <button
      type="button"
      :class="{ on: i18n.lang === 'zh' }"
      @click="i18n.toggle('zh')"
    >
      中文
    </button>
  </div>
</template>
