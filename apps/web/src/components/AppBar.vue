<script setup lang="ts">
// Top appbar: brand + route tabs + language toggle + primary CTA.
// The active-tab highlight is driven by the route NAME (not path) so
// /workbench and /workbench/:run_id both mark the Workbench tab.
import { useRoute } from "vue-router";
import LangToggle from "./LangToggle.vue";
import T from "./T.vue";

const route = useRoute();
</script>

<template>
  <header class="appbar">
    <div class="appbar-inner">
      <RouterLink
        :to="{ name: 'showcase' }"
        class="brand"
        style="text-decoration: none"
      >
        <svg width="26" height="26" viewBox="0 0 28 28" fill="none" aria-hidden="true">
          <rect x="1" y="1" width="26" height="26" stroke="#14130F" stroke-width="1.2" />
          <path d="M5 20 L11 8 L14 14 L17 11 L23 20" stroke="#14130F" stroke-width="1.4" fill="none" />
          <circle cx="17" cy="11" r="1.6" fill="#D4E85A" stroke="#14130F" stroke-width="1" />
        </svg>
        <span class="serif">Mathodology</span>
      </RouterLink>

      <nav class="tabs" aria-label="Primary">
        <RouterLink
          :to="{ name: 'showcase' }"
          :class="{ on: route.name === 'showcase' }"
        >
          <T en="Overview" zh="概览" />
        </RouterLink>
        <RouterLink
          :to="{ name: 'dashboard' }"
          :class="{ on: route.name === 'dashboard' }"
        >
          <T en="Dashboard" zh="仪表盘" />
        </RouterLink>
        <RouterLink
          :to="{ name: 'workbench' }"
          :class="{ on: route.name === 'workbench' }"
        >
          <T en="Workbench" zh="工作台" />
        </RouterLink>
      </nav>

      <div style="display:flex;gap:10px;align-items:center;">
        <LangToggle />
        <RouterLink class="btn hi" :to="{ name: 'workbench' }">
          ▶ <T en="Try a run" zh="立即试用" />
        </RouterLink>
      </div>
    </div>
  </header>
</template>
