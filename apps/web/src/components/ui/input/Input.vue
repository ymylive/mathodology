<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/lib/utils";

// Minimal v-model binding without @vueuse/core so we don't pull in a new
// workspace dep just for this component.
const props = defineProps<{
  modelValue?: string | number;
  class?: string;
}>();

const emits = defineEmits<{
  (e: "update:modelValue", payload: string): void;
}>();

const value = computed({
  get: () => props.modelValue ?? "",
  set: (v) => emits("update:modelValue", String(v)),
});
</script>

<template>
  <input
    v-model="value"
    :class="
      cn(
        'flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
        props.class,
      )
    "
  />
</template>
