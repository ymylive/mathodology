<script setup lang="ts">
import { ref, watch } from "vue";

// Cost meter with a brief "↑" flash whenever the total increases. The flash
// is debounced to 150ms so rapid cost events don't strobe the UI.
const props = defineProps<{ totalRmb: number }>();

const flash = ref(false);
let flashTimer: number | null = null;

watch(
  () => props.totalRmb,
  (next, prev) => {
    if (typeof prev !== "number") return;
    if (next <= prev) return;
    flash.value = true;
    if (flashTimer !== null) window.clearTimeout(flashTimer);
    flashTimer = window.setTimeout(() => {
      flash.value = false;
      flashTimer = null;
    }, 150);
  },
);
</script>

<template>
  <span
    class="mono text-xs text-neutral-300 inline-flex items-center gap-1.5 tabular-nums"
    :aria-label="`Run cost ¥${totalRmb.toFixed(6)}`"
  >
    <span class="text-neutral-500">cost</span>
    <span>¥{{ totalRmb.toFixed(6) }}</span>
    <span
      class="transition-opacity duration-150 text-emerald-400"
      :class="flash ? 'opacity-100' : 'opacity-0'"
      aria-hidden="true"
    >
      ↑
    </span>
  </span>
</template>
