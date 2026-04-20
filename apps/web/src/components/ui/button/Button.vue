<script setup lang="ts">
import { Primitive, type PrimitiveProps } from "radix-vue";
import { cn } from "@/lib/utils";
import { buttonVariants, type ButtonVariants } from "./variants";

interface Props extends PrimitiveProps {
  variant?: ButtonVariants["variant"];
  size?: ButtonVariants["size"];
  class?: string;
}

const props = withDefaults(defineProps<Props>(), {
  as: "button",
});
</script>

<template>
  <Primitive
    :as="as"
    :as-child="asChild"
    :class="cn(buttonVariants({ variant: props.variant, size: props.size }), props.class)"
  >
    <slot />
  </Primitive>
</template>
