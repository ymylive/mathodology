import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import HomeView from "@/views/HomeView.vue";

const routes: RouteRecordRaw[] = [
  { path: "/", name: "home", component: HomeView },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});
