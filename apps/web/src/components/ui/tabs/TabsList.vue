<script setup lang="ts">
import { TabsList, type TabsListProps } from "radix-vue";
import { cn } from "@/lib/utils";

interface Props extends TabsListProps {
  class?: string;
}

const props = defineProps<Props>();
</script>

<template>
  <TabsList
    v-bind="props"
    :class="
      cn(
        'inline-flex h-9 items-center justify-center rounded-lg bg-muted p-1 text-muted-foreground',
        props.class,
      )
    "
  >
    <slot />
  </TabsList>
</template>
