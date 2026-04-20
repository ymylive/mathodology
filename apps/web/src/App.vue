<script setup lang="ts">
import { RouterView } from "vue-router";
</script>

<template>
  <div class="min-h-full flex flex-col">
    <header class="border-b border-neutral-800 px-6 py-3 flex items-center gap-3">
      <span class="text-sm font-mono tracking-wide text-neutral-400">mathodology</span>
      <span class="text-xs px-2 py-0.5 rounded bg-neutral-800 text-neutral-400 font-mono">
        M1 hello-world-ping
      </span>
    </header>
    <main class="flex-1">
      <RouterView />
    </main>
  </div>
</template>
