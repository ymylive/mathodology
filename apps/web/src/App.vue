<script setup lang="ts">
import { RouterView } from "vue-router";
import { TooltipProvider } from "@/components/ui/tooltip";
</script>

<template>
  <!-- TooltipProvider at the app root so individual <Tooltip> usages don't
       each need their own provider. The default 200ms delay is fine. -->
  <TooltipProvider>
    <div class="min-h-full flex flex-col bg-background text-foreground">
      <main class="flex-1">
        <RouterView />
      </main>
    </div>
  </TooltipProvider>
</template>
