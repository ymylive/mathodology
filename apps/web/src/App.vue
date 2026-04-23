<script setup lang="ts">
// Thin shell: sticky appbar + routed view. All page content lives in views/.
import { onMounted } from "vue";
import AppBar from "@/components/AppBar.vue";
import { useI18n } from "@/composables/useI18n";
import { useSearchConfigStore } from "@/stores/searchConfig";

const i18n = useI18n();
const searchConfig = useSearchConfigStore();

onMounted(() => {
  // Apply body.zh before first paint of the routed view so font swaps
  // don't flash from latin → CJK on a persisted Chinese selection.
  i18n.init();
  // Fire-and-forget capability probe. Resolves to a conservative fallback
  // on 404 / timeout so the SearchConfigPanel is never blocked on it.
  void searchConfig.loadCapabilities();
});
</script>

<template>
  <AppBar />
  <div class="router-shell">
    <RouterView v-slot="{ Component }">
      <Transition name="page" mode="out-in">
        <component :is="Component" />
      </Transition>
    </RouterView>
  </div>
</template>

<style scoped>
/* Scoped shell keeps the absolute-positioned leaving page from escaping. */
.router-shell { position: relative; }
</style>
