<script setup lang="ts">
import { TabsRoot, type TabsRootProps, useForwardPropsEmits } from "radix-vue";

const props = defineProps<TabsRootProps>();
const emits = defineEmits<{
  (e: "update:modelValue", value: string | number): void;
}>();

const forwarded = useForwardPropsEmits(props, emits);
</script>

<template>
  <TabsRoot v-bind="forwarded">
    <slot />
  </TabsRoot>
</template>
