import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
} from "vue-router";
import Showcase from "@/views/Showcase.vue";
import Dashboard from "@/views/Dashboard.vue";
import Workbench from "@/views/Workbench.vue";

const routes: RouteRecordRaw[] = [
  { path: "/", name: "showcase", component: Showcase },
  { path: "/dashboard", name: "dashboard", component: Dashboard },
  // ONE named route with optional :run_id param. Using the same name for
  // both the empty-state path (/workbench) and the active path
  // (/workbench/:run_id) keeps Vue Router from unmounting/remounting the
  // component on submit — otherwise `onBeforeUnmount` fires
  // `run.reset()` which closes the WebSocket the user just opened.
  {
    path: "/workbench/:run_id?",
    name: "workbench",
    component: Workbench,
    props: true,
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});
