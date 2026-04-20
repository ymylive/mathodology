<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/lib/utils";

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
  <textarea
    v-model="value"
    :class="
      cn(
        'flex min-h-[60px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
        props.class,
      )
    "
  />
</template>
