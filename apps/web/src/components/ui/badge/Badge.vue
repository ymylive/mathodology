<script setup lang="ts">
import type { HTMLAttributes } from "vue";
import { cn } from "@/lib/utils";
import { badgeVariants, type BadgeVariants } from "./variants";

// `class` is typed as HTMLAttributes["class"] (string | array | record) so
// callers can pass the same shapes Vue accepts in `:class`. `cn()` +
// clsx happily normalize any of those.
const props = defineProps<{
  variant?: BadgeVariants["variant"];
  class?: HTMLAttributes["class"];
}>();
</script>

<template>
  <div :class="cn(badgeVariants({ variant: props.variant }), props.class)">
    <slot />
  </div>
</template>
