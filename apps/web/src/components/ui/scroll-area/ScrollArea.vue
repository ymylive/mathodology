<script setup lang="ts">
import {
  ScrollAreaCorner,
  ScrollAreaRoot,
  type ScrollAreaRootProps,
  ScrollAreaViewport,
} from "radix-vue";
import { cn } from "@/lib/utils";
import ScrollBar from "./ScrollBar.vue";

interface Props extends ScrollAreaRootProps {
  class?: string;
}

const props = defineProps<Props>();
</script>

<template>
  <ScrollAreaRoot
    :type="props.type"
    :dir="props.dir"
    :scroll-hide-delay="props.scrollHideDelay"
    :class="cn('relative overflow-hidden', props.class)"
  >
    <ScrollAreaViewport class="h-full w-full rounded-[inherit]">
      <slot />
    </ScrollAreaViewport>
    <ScrollBar />
    <ScrollAreaCorner />
  </ScrollAreaRoot>
</template>
