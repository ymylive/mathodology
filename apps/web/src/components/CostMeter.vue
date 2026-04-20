<script setup lang="ts">
import { ref, watch } from "vue";
import { Badge } from "@/components/ui/badge";
import { ArrowUp } from "lucide-vue-next";

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
  <Badge
    variant="outline"
    class="mono tabular-nums gap-1.5 font-normal text-foreground"
    :aria-label="`Run cost ¥${totalRmb.toFixed(6)}`"
  >
    <span class="text-muted-foreground">cost</span>
    <span>¥{{ totalRmb.toFixed(6) }}</span>
    <ArrowUp
      class="h-3 w-3 text-emerald-400 transition-opacity duration-150"
      :class="flash ? 'opacity-100' : 'opacity-0'"
      aria-hidden="true"
    />
  </Badge>
</template>
