// Typed wrappers around the aggregation endpoints added in M7.
//
// - GET /stats/summary?window=24h|7d|all   — headline counts + median/p95
// - GET /stats/providers?window=24h|7d|all — cost share by model
// - GET /providers                          — registered LLM providers
//
// All three reuse the Bearer-auth fetch helper from `api/http.ts`. We keep
// the return types explicit (rather than `any`) so callers get compile-time
// safety against the column names changing on the backend.

import { http } from "./http";

export type StatsWindow = "24h" | "7d" | "all";

export interface StatsSummary {
  window: string;
  total_runs: number;
  success_runs: number;
  failed_runs: number;
  /** 0.0 – 1.0 — multiply by 100 for a percent. */
  success_rate: number;
  /** RMB directly. Nullable when the window has no rows. */
  median_cost_rmb: number | null;
  /** Milliseconds. Nullable when the window has no completed rows. */
  p95_latency_ms: number | null;
}

export interface ProviderShareItem {
  model: string;
  cost_rmb: number;
  /** 0 – 100. */
  share_pct: number;
}

export interface ProviderShare {
  window: string;
  total_cost_rmb: number;
  items: ProviderShareItem[];
}

export interface ProviderInfo {
  name: string;
  kind: string;
  models: string[];
  price_input_per_1m: number;
  price_output_per_1m: number;
  has_key: boolean;
}

export interface ProvidersResp {
  items: ProviderInfo[];
}

export function fetchSummary(window: StatsWindow): Promise<StatsSummary> {
  return http.get<StatsSummary>(`/stats/summary?window=${window}`);
}

export function fetchProviderShare(window: StatsWindow): Promise<ProviderShare> {
  return http.get<ProviderShare>(`/stats/providers?window=${window}`);
}

export function fetchProviders(): Promise<ProvidersResp> {
  return http.get<ProvidersResp>("/providers");
}
