// Persistent per-browser run settings for the Workbench.
//
// These are form defaults the user picks before pressing Start. None of
// them are sent through POST /runs today (the backend doesn't accept the
// fields yet) — the store exists so the form holds state across page
// reloads and across the empty-state → active-state transition.
//
// Persisted under a single JSON blob so adding a field later is cheap.

import { defineStore } from "pinia";

export type Routing = "cost" | "balanced" | "latency";
export type RetryBudget = 0 | 1 | 2 | 3;
export type Competition = "mcm" | "cumcm" | "huashu" | "general";
export type ReasoningEffort = "off" | "low" | "medium" | "high";

const REASONING_EFFORTS: readonly ReasoningEffort[] = [
  "off",
  "low",
  "medium",
  "high",
] as const;

interface Settings {
  competition: Competition;
  model: string;
  routing: Routing;
  retry: RetryBudget;
  hmml: boolean;
  reasoningEffort: ReasoningEffort;
  /** Opt-in 1M-token max_tokens ceiling. Only viable on models that advertise
   *  long-context (Claude 3.5 Sonnet 1M, Gemini 2.0, gpt-5-1m, etc.). */
  longContext: boolean;
}

const LS_KEY = "mathodology.runSettings.v1";

const DEFAULTS: Settings = {
  competition: "cumcm",
  model: "deepseek-chat",
  routing: "balanced",
  retry: 3,
  hmml: true,
  reasoningEffort: "high",
  longContext: false,
};

function coerceReasoningEffort(v: unknown): ReasoningEffort {
  return typeof v === "string" && (REASONING_EFFORTS as readonly string[]).includes(v)
    ? (v as ReasoningEffort)
    : DEFAULTS.reasoningEffort;
}

function load(): Settings {
  if (typeof window === "undefined") return { ...DEFAULTS };
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw) as Partial<Settings>;
    return {
      ...DEFAULTS,
      ...parsed,
      reasoningEffort: coerceReasoningEffort(parsed.reasoningEffort),
    };
  } catch {
    return { ...DEFAULTS };
  }
}

export const useRunSettingsStore = defineStore("runSettings", {
  state: (): Settings => load(),
  actions: {
    persist() {
      if (typeof window === "undefined") return;
      try {
        window.localStorage.setItem(
          LS_KEY,
          JSON.stringify({
            competition: this.competition,
            model: this.model,
            routing: this.routing,
            retry: this.retry,
            hmml: this.hmml,
            reasoningEffort: this.reasoningEffort,
            longContext: this.longContext,
          }),
        );
      } catch {
        /* ignore quota errors — settings are convenience state */
      }
    },
    setCompetition(v: Competition) {
      this.competition = v;
      this.persist();
    },
    setModel(v: string) {
      this.model = v;
      this.persist();
    },
    setRouting(v: Routing) {
      this.routing = v;
      this.persist();
    },
    setRetry(v: RetryBudget) {
      this.retry = v;
      this.persist();
    },
    setHmml(v: boolean) {
      this.hmml = v;
      this.persist();
    },
    setReasoningEffort(v: ReasoningEffort) {
      this.reasoningEffort = v;
      this.persist();
    },
    setLongContext(v: boolean) {
      this.longContext = v;
      this.persist();
    },
  },
});
