//! Aggregation endpoints for the Dashboard / Showcase UI surface.
//!
//! - `GET /stats/summary?window=24h|7d|all` — headline counts + median/p95.
//! - `GET /stats/providers?window=...`      — cost share by model.
//! - `GET /providers`                        — the registered LLM providers.

use axum::extract::{Query, State};
use axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::FromRow;

use crate::error::AppError;
use crate::state::AppState;

#[derive(Debug, Deserialize)]
pub struct WindowQuery {
    /// Time window: `24h`, `7d`, or `all`. Default: `24h`.
    pub window: Option<String>,
}

fn resolve_window(s: Option<&str>) -> &'static str {
    match s {
        Some("7d") => "7 days",
        Some("all") => "1000 years",
        _ => "1 day",
    }
}

#[derive(Debug, FromRow)]
struct SummaryRow {
    total: i64,
    success: i64,
    failed: i64,
    median_cost: Option<String>,
    p95_ms: Option<String>,
}

#[tracing::instrument(skip_all)]
pub async fn stats_summary(
    State(state): State<AppState>,
    Query(q): Query<WindowQuery>,
) -> Result<Json<Value>, AppError> {
    let interval = resolve_window(q.window.as_deref());

    let row: SummaryRow = sqlx::query_as::<_, SummaryRow>(&format!(
        r#"
        WITH w AS (
            SELECT status::text AS status,
                   cost_rmb,
                   (EXTRACT(EPOCH FROM (updated_at - created_at)) * 1000)::bigint AS ms
            FROM runs
            WHERE created_at > now() - interval '{interval}'
        )
        SELECT
            COUNT(*)::bigint                                            AS total,
            COUNT(*) FILTER (WHERE status = 'done')::bigint             AS success,
            COUNT(*) FILTER (WHERE status = 'failed')::bigint           AS failed,
            (percentile_cont(0.5) WITHIN GROUP (ORDER BY cost_rmb))::text  AS median_cost,
            (percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) FILTER (WHERE status = 'done'))::text AS p95_ms
        FROM w
        "#
    ))
    .fetch_one(&state.pg)
    .await?;

    Ok(Json(json!({
        "window": q.window.unwrap_or_else(|| "24h".into()),
        "total_runs":     row.total,
        "success_runs":   row.success,
        "failed_runs":    row.failed,
        "success_rate":   if row.total > 0 { row.success as f64 / row.total as f64 } else { 0.0 },
        "median_cost_rmb": row.median_cost.and_then(|s| s.parse::<f64>().ok()),
        "p95_latency_ms":  row.p95_ms.and_then(|s| s.parse::<f64>().ok()),
    })))
}

#[derive(Debug, FromRow)]
struct ProviderRow {
    model: String,
    cost_rmb: String,
}

#[tracing::instrument(skip_all)]
pub async fn stats_providers(
    State(state): State<AppState>,
    Query(q): Query<WindowQuery>,
) -> Result<Json<Value>, AppError> {
    let interval = resolve_window(q.window.as_deref());

    let rows: Vec<ProviderRow> = sqlx::query_as::<_, ProviderRow>(&format!(
        r#"
        SELECT model, SUM(cost_rmb)::text AS cost_rmb
        FROM cost_ledger
        WHERE ts > now() - interval '{interval}'
        GROUP BY model
        ORDER BY SUM(cost_rmb) DESC
        LIMIT 16
        "#
    ))
    .fetch_all(&state.pg)
    .await?;

    let parsed: Vec<(String, f64)> = rows
        .into_iter()
        .map(|r| (r.model, r.cost_rmb.parse::<f64>().unwrap_or(0.0)))
        .collect();
    let total: f64 = parsed.iter().map(|(_, c)| *c).sum();
    let items: Vec<Value> = parsed
        .into_iter()
        .map(|(m, c)| {
            json!({
                "model": m,
                "cost_rmb": c,
                "share_pct": if total > 0.0 { (c / total) * 100.0 } else { 0.0 },
            })
        })
        .collect();

    Ok(Json(json!({
        "window": q.window.unwrap_or_else(|| "24h".into()),
        "total_cost_rmb": total,
        "items": items,
    })))
}

#[derive(Debug, Serialize)]
struct ProviderInfo {
    name: String,
    kind: String,
    models: Vec<String>,
    price_input_per_1m: f64,
    price_output_per_1m: f64,
    has_key: bool,
}

#[tracing::instrument(skip_all)]
pub async fn list_providers(State(state): State<AppState>) -> Result<Json<Value>, AppError> {
    let items: Vec<ProviderInfo> = state
        .llm
        .providers_meta()
        .into_iter()
        .map(|p| ProviderInfo {
            name: p.name,
            kind: p.kind,
            models: p.models,
            price_input_per_1m: p.price_input_per_1m,
            price_output_per_1m: p.price_output_per_1m,
            has_key: p.has_key,
        })
        .collect();

    Ok(Json(json!({ "items": items })))
}
