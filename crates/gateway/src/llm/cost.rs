//! Cost accounting.
//!
//! Formula: `cost_rmb = prompt_tokens/1e6 * price_input + completion_tokens/1e6 * price_output`.
//! Numbers are carried as f64 in-memory and written as text casts to
//! Postgres NUMERIC (same trick as M2's GET /runs/:id) so we don't need the
//! bigdecimal sqlx feature.

use chrono::Utc;
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde_json::json;
use uuid::Uuid;

use crate::dispatch::events_stream_key;
use crate::llm::canonical::Usage;
use crate::llm::config::{Price, PriceTable};
use crate::llm::stream::next_seq;

/// Stream MAXLEN for `mm:events:<run_id>`. Matches worker emitter.
const EVENTS_MAXLEN: usize = 5000;

pub fn compute_cost_rmb(price: Price, usage: &Usage) -> f64 {
    (usage.prompt_tokens as f64 / 1_000_000.0) * price.input_per_1m
        + (usage.completion_tokens as f64 / 1_000_000.0) * price.output_per_1m
}

/// Record a completion against the cost ledger.
///
/// - INSERTs a `cost_ledger` row.
/// - If `run_id` is set: `UPDATE runs SET cost_rmb = cost_rmb + delta` and
///   emits a `kind=cost` event to `mm:events:<run_id>`.
///
/// Returns `delta_rmb` so the caller can log / trace it.
#[allow(clippy::too_many_arguments)]
pub async fn record_completion_cost(
    pg: &sqlx::PgPool,
    redis: &mut ConnectionManager,
    prices: &PriceTable,
    run_id: Option<Uuid>,
    agent: Option<&str>,
    model: &str,
    usage: &Usage,
    cache_hit: bool,
) -> anyhow::Result<f64> {
    let price = prices.get(model).unwrap_or(Price {
        input_per_1m: 0.0,
        output_per_1m: 0.0,
    });
    let delta = if cache_hit {
        0.0
    } else {
        compute_cost_rmb(price, usage)
    };
    let delta_str = format!("{delta:.6}");

    // Ledger insert is only performed when a run_id is present (cost_ledger
    // has a NOT NULL run_id FK). Orphan LLM calls (no X-Run-Id) are counted
    // in tracing only.
    if let Some(rid) = run_id {
        sqlx::query(
            r#"
            INSERT INTO cost_ledger
              (run_id, ts, agent, model, prompt_tokens, completion_tokens, cost_rmb, cache_hit)
            VALUES ($1, now(), $2, $3, $4, $5, $6::numeric, $7)
            "#,
        )
        .bind(rid)
        .bind(agent)
        .bind(model)
        .bind(usage.prompt_tokens as i32)
        .bind(usage.completion_tokens as i32)
        .bind(&delta_str)
        .bind(cache_hit)
        .execute(pg)
        .await?;

        // Bump runs.cost_rmb and read back the new total in one round-trip.
        // Text-cast keeps us free of BigDecimal.
        let row: (String,) = sqlx::query_as(
            r#"
            UPDATE runs
               SET cost_rmb = cost_rmb + $2::numeric,
                   updated_at = now()
             WHERE id = $1
         RETURNING cost_rmb::text
            "#,
        )
        .bind(rid)
        .bind(&delta_str)
        .fetch_one(pg)
        .await?;
        let run_total: f64 = row.0.parse().unwrap_or(0.0);

        // XADD kind=cost event to the run stream. Seq from shared counter.
        let seq = next_seq(redis, &rid).await?;
        let payload = json!({
            "run_id": rid,
            "agent": agent,
            "kind": "cost",
            "seq": seq,
            "ts": Utc::now(),
            "payload": {
                "run_total_rmb": run_total,
                "delta_rmb": delta,
                "model": model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            },
        });
        let payload_str = serde_json::to_string(&payload)?;
        let stream_key = events_stream_key(&rid);
        let _: String = redis
            .xadd_maxlen(
                stream_key,
                redis::streams::StreamMaxlen::Approx(EVENTS_MAXLEN),
                "*",
                &[("payload", payload_str.as_str())],
            )
            .await?;
    }

    Ok(delta)
}
