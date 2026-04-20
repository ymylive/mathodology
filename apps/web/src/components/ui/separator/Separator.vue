<script setup lang="ts">
import { Separator, type SeparatorProps } from "radix-vue";
import { cn } from "@/lib/utils";

interface Props extends SeparatorProps {
  class?: string;
}

const props = withDefaults(defineProps<Props>(), {
  orientation: "horizontal",
  decorative: true,
});
</script>

<template>
  <Separator
    :orientation="props.orientation"
    :decorative="props.decorative"
    :class="
      cn(
        'shrink-0 bg-border',
        props.orientation === 'vertical' ? 'w-px h-full' : 'h-px w-full',
        props.class,
      )
    "
  />
</template>
