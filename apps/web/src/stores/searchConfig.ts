// Persistent search-source preferences for the Workbench form.
//
// Mirrors the `SearchConfig` payload that ProblemInput will carry through to
// the worker. We keep the shape local here (rather than importing from
// ts-contracts) because Agent A adds the exported types in a separate
// change — the field names + literals must match the contract.
//
// Persisted under `mathodology.search_config` so refresh/reopen keeps the
// user's last choice. Capabilities are NOT persisted — we refetch on every
// app start via App.vue onMounted(), because the gateway's env can change
// between sessions (operator configures TAVILY_API_KEY, restarts, etc.).

import { defineStore } from "pinia";
import {
  fetchSearchCapabilities,
  type SearchCapabilities,
} from "@/api/search";

export type SearchPrimary = "tavily" | "open_websearch" | "none";

// Full set of engines the worker understands. The UI filters this against
// `capabilities.available_engines` to hide ones the backend can't reach.
export type SearchEngine =
  | "bing"
  | "baidu"
  | "duckduckgo"
  | "csdn"
  | "juejin"
  | "brave"
  | "exa"
  | "startpage";

export const ALL_ENGINES: readonly SearchEngine[] = [
  "baidu",
  "csdn",
  "juejin",
  "duckduckgo",
  "bing",
  "brave",
  "exa",
  "startpage",
] as const;

export type TavilyDepth = "basic" | "advanced";

export interface SearchConfig {
  primary: SearchPrimary;
  engines: SearchEngine[];
  tavily_depth: TavilyDepth;
  fallback_threshold: number;
}

const LS_KEY = "mathodology.search_config";

const DEFAULTS: SearchConfig = {
  primary: "tavily",
  engines: ["baidu", "csdn", "juejin", "duckduckgo"],
  tavily_depth: "basic",
  fallback_threshold: 3,
};

const PRIMARIES: readonly SearchPrimary[] = [
  "tavily",
  "open_websearch",
  "none",
] as const;

const DEPTHS: readonly TavilyDepth[] = ["basic", "advanced"] as const;

function isEngine(v: unknown): v is SearchEngine {
  return typeof v === "string" && (ALL_ENGINES as readonly string[]).includes(v);
}

function coercePrimary(v: unknown): SearchPrimary {
  return typeof v === "string" && (PRIMARIES as readonly string[]).includes(v)
    ? (v as SearchPrimary)
    : DEFAULTS.primary;
}

function coerceDepth(v: unknown): TavilyDepth {
  return typeof v === "string" && (DEPTHS as readonly string[]).includes(v)
    ? (v as TavilyDepth)
    : DEFAULTS.tavily_depth;
}

function coerceThreshold(v: unknown): number {
  if (typeof v !== "number" || !Number.isFinite(v)) return DEFAULTS.fallback_threshold;
  const n = Math.round(v);
  // 0 disables fallback; cap at 50 so the input can't be absurd.
  return Math.max(0, Math.min(50, n));
}

function load(): SearchConfig {
  if (typeof window === "undefined") return { ...DEFAULTS, engines: [...DEFAULTS.engines] };
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return { ...DEFAULTS, engines: [...DEFAULTS.engines] };
    const parsed = JSON.parse(raw) as Partial<SearchConfig>;
    return {
      primary: coercePrimary(parsed.primary),
      engines: Array.isArray(parsed.engines)
        ? parsed.engines.filter(isEngine)
        : [...DEFAULTS.engines],
      tavily_depth: coerceDepth(parsed.tavily_depth),
      fallback_threshold: coerceThreshold(parsed.fallback_threshold),
    };
  } catch {
    return { ...DEFAULTS, engines: [...DEFAULTS.engines] };
  }
}

interface State extends SearchConfig {
  /** Populated once by loadCapabilities() at app start. Null until then;
   *  components treat null as "assume everything available" so the form
   *  never appears blank if the gateway is slow. */
  capabilities: SearchCapabilities | null;
}

export const useSearchConfigStore = defineStore("searchConfig", {
  state: (): State => ({
    ...load(),
    capabilities: null,
  }),

  getters: {
    // If the operator hasn't configured Tavily but the user's saved choice
    // is "tavily", the worker should silently fall back to open_websearch.
    // The UI shows a hint for this; this getter is what gets sent over the
    // wire so the backend doesn't have to re-derive it.
    effectivePrimary(state): SearchPrimary {
      if (
        state.primary === "tavily" &&
        state.capabilities &&
        state.capabilities.tavily_available === false
      ) {
        return "open_websearch";
      }
      return state.primary;
    },

    // Snapshot shaped for the API payload. Uses `effectivePrimary` so the
    // backend receives the actually-usable primary even if the form still
    // shows "tavily" with a disabled radio.
    payload(): SearchConfig {
      return {
        primary: this.effectivePrimary,
        engines: [...this.engines],
        tavily_depth: this.tavily_depth,
        fallback_threshold: this.fallback_threshold,
      };
    },

    // True when the saved primary is Tavily but the gateway has no key —
    // the panel shows a small "using open-websearch" hint in that case.
    tavilyFallbackActive(state): boolean {
      return (
        state.primary === "tavily" &&
        !!state.capabilities &&
        state.capabilities.tavily_available === false
      );
    },
  },

  actions: {
    persist() {
      if (typeof window === "undefined") return;
      try {
        window.localStorage.setItem(
          LS_KEY,
          JSON.stringify({
            primary: this.primary,
            engines: this.engines,
            tavily_depth: this.tavily_depth,
            fallback_threshold: this.fallback_threshold,
          }),
        );
      } catch {
        /* quota / private-mode — settings are convenience state */
      }
    },

    setPrimary(p: SearchPrimary) {
      this.primary = p;
      this.persist();
    },

    toggleEngine(e: SearchEngine) {
      const idx = this.engines.indexOf(e);
      if (idx === -1) {
        this.engines = [...this.engines, e];
      } else {
        this.engines = this.engines.filter((x) => x !== e);
      }
      this.persist();
    },

    setTavilyDepth(d: TavilyDepth) {
      this.tavily_depth = d;
      this.persist();
    },

    setFallbackThreshold(n: number) {
      this.fallback_threshold = coerceThreshold(n);
      this.persist();
    },

    resetToDefaults() {
      this.primary = DEFAULTS.primary;
      this.engines = [...DEFAULTS.engines];
      this.tavily_depth = DEFAULTS.tavily_depth;
      this.fallback_threshold = DEFAULTS.fallback_threshold;
      this.persist();
    },

    // Called once from App.vue at app start. Failures are swallowed inside
    // fetchSearchCapabilities (it returns a conservative fallback), so this
    // always resolves.
    async loadCapabilities() {
      this.capabilities = await fetchSearchCapabilities();
    },
  },
});
