//! Audit task: durably copies events from the Redis stream
//! `mm:events:<run_id>` into the `events_audit` table, and drives the
//! `runs.status` lifecycle as stages progress.
//!
//! One task per run. Exits when a `done` or `error` event is observed, or
//! after a grace timeout (M2: 10 minutes).

use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use redis::aio::ConnectionManager;
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::AsyncCommands;
use serde::Deserialize;
use serde_json::Value;
use uuid::Uuid;

use crate::dispatch::events_stream_key;
use crate::state::AppState;

/// Overall grace timeout: if the run hasn't hit a terminal event within
/// this many seconds, we abandon it. Configurable via `AUDIT_GRACE_SECS`
/// env var. Default 3600s (1 hour) — real-LLM runs with slow providers
/// (gpt-5.4 via cdnapi routinely exceed the original 600s ceiling),
/// which caused DB state (notebook_path, paper_path, cost_rmb) to never
/// get persisted for long-running jobs.
fn audit_grace_secs() -> u64 {
    std::env::var("AUDIT_GRACE_SECS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3600)
}

/// Minimum parsed shape of the event envelope (events.schema.json).
#[derive(Debug, Deserialize)]
struct EventEnvelope {
    run_id: Uuid,
    #[serde(default)]
    agent: Option<String>,
    kind: String,
    seq: i64,
    ts: DateTime<Utc>,
    #[serde(default)]
    payload: Value,
}

/// Spawn the per-run audit task. Non-blocking; errors are logged, not returned.
pub fn spawn_audit_task(state: AppState, run_id: Uuid) {
    tokio::spawn(async move {
        let span = tracing::info_span!("audit", %run_id);
        let _enter = span.enter();
        if let Err(e) = run_audit(state, run_id).await {
            tracing::error!(%run_id, error = ?e, "audit task ended with error");
        } else {
            tracing::info!(%run_id, "audit task exited cleanly");
        }
    });
}

async fn run_audit(state: AppState, run_id: Uuid) -> anyhow::Result<()> {
    let mut redis = state.redis.clone();
    let stream_key = events_stream_key(&run_id);
    let mut last_id = "0-0".to_string();
    // 300ms block: fast enough to keep DB audit near-real-time, not so tight
    // that we hammer Redis while the run is idle.
    let opts = StreamReadOptions::default().block(300).count(100);
    let started_at = Instant::now();
    let grace_secs = audit_grace_secs();

    tracing::debug!(%run_id, stream_key, grace_secs, "audit task started");

    loop {
        // Grace timeout guard.
        if started_at.elapsed() > Duration::from_secs(grace_secs) {
            tracing::warn!(
                %run_id,
                grace_secs,
                "audit task grace timeout hit; exiting"
            );
            return Ok(());
        }

        let entries = match xread_one_batch(&mut redis, &stream_key, &last_id, &opts).await {
            Ok(Some(e)) => e,
            Ok(None) => continue, // XREAD block timeout; loop and re-check grace.
            Err(e) => {
                tracing::warn!(%run_id, error = %e, "XREAD failed; backing off 500ms");
                tokio::time::sleep(Duration::from_millis(500)).await;
                continue;
            }
        };

        for (entry_id, payload_str) in entries {
            // Update cursor even if a single row fails; otherwise we spin on it.
            last_id = entry_id;

            let env: EventEnvelope = match serde_json::from_str(&payload_str) {
                Ok(v) => v,
                Err(e) => {
                    tracing::warn!(
                        %run_id,
                        error = %e,
                        "event payload is not a valid envelope; skipping"
                    );
                    continue;
                }
            };

            // Sanity: mismatched run_id would indicate a producer bug.
            if env.run_id != run_id {
                tracing::warn!(
                    %run_id,
                    env_run_id = %env.run_id,
                    "envelope run_id does not match; skipping"
                );
                continue;
            }

            if let Err(e) = insert_audit_row(&state.pg, &env).await {
                tracing::warn!(
                    %run_id,
                    seq = env.seq,
                    kind = %env.kind,
                    error = %e,
                    "failed to insert audit row"
                );
                // Don't bail: keep consuming so one bad row doesn't stall the stream.
                continue;
            }

            if let Err(e) = apply_lifecycle(&state.pg, run_id, &env).await {
                tracing::warn!(
                    %run_id,
                    kind = %env.kind,
                    error = %e,
                    "failed to apply lifecycle update"
                );
            }

            if env.kind == "done" || env.kind == "error" {
                tracing::info!(%run_id, kind = %env.kind, "terminal event observed; exiting audit");
                return Ok(());
            }
        }
    }
}

/// Issue a single XREAD BLOCK and flatten to `(entry_id, payload_json_str)`.
async fn xread_one_batch(
    redis: &mut ConnectionManager,
    stream_key: &str,
    last_id: &str,
    opts: &StreamReadOptions,
) -> redis::RedisResult<Option<Vec<(String, String)>>> {
    let reply: Option<StreamReadReply> =
        redis.xread_options(&[stream_key], &[last_id], opts).await?;

    let Some(reply) = reply else { return Ok(None) };

    let mut out = Vec::new();
    for key in reply.keys {
        for entry in key.ids {
            let payload = match entry.map.get("payload") {
                Some(redis::Value::BulkString(bytes)) => {
                    String::from_utf8_lossy(bytes).into_owned()
                }
                Some(redis::Value::SimpleString(s)) => s.clone(),
                _ => continue,
            };
            out.push((entry.id, payload));
        }
    }
    Ok(Some(out))
}

/// Idempotent insert into events_audit (ON CONFLICT DO NOTHING).
async fn insert_audit_row(pg: &sqlx::PgPool, env: &EventEnvelope) -> sqlx::Result<()> {
    sqlx::query(
        r#"
        INSERT INTO events_audit (run_id, seq, ts, agent, kind, payload)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (run_id, seq) DO NOTHING
        "#,
    )
    .bind(env.run_id)
    .bind(env.seq)
    .bind(env.ts)
    .bind(env.agent.as_deref())
    .bind(&env.kind)
    .bind(&env.payload)
    .execute(pg)
    .await
    .map(|_| ())
}

/// Advance `runs.status` / `runs.cost_rmb` based on the event kind.
async fn apply_lifecycle(pg: &sqlx::PgPool, run_id: Uuid, env: &EventEnvelope) -> sqlx::Result<()> {
    match env.kind.as_str() {
        "stage.start" => {
            // First stage.start promotes queued -> running.
            sqlx::query(
                r#"
                UPDATE runs
                SET status = 'running', updated_at = now()
                WHERE id = $1 AND status = 'queued'
                "#,
            )
            .bind(run_id)
            .execute(pg)
            .await?;
        }
        "done" => {
            // done.payload may carry status, cost_rmb, notebook_path, paper_path.
            let terminal_status = env
                .payload
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("done");
            let db_status = match terminal_status {
                "success" | "done" => "done",
                "failed" => "failed",
                "cancelled" => "cancelled",
                other => {
                    tracing::warn!(status = other, "unknown done.status; defaulting to 'done'");
                    "done"
                }
            };

            let cost_rmb_str = env
                .payload
                .get("cost_rmb")
                .and_then(|v| v.as_f64())
                .map(|f| format!("{f:.4}"));
            let notebook_path = env.payload.get("notebook_path").and_then(|v| v.as_str());
            let paper_path = env.payload.get("paper_path").and_then(|v| v.as_str());

            // Cast cost via NUMERIC literal so we don't need bigdecimal.
            sqlx::query(
                r#"
                UPDATE runs SET
                    status = $2::run_status,
                    updated_at = now(),
                    cost_rmb = COALESCE($3::numeric, cost_rmb),
                    notebook_path = COALESCE($4, notebook_path),
                    paper_path = COALESCE($5, paper_path)
                WHERE id = $1
                "#,
            )
            .bind(run_id)
            .bind(db_status)
            .bind(cost_rmb_str)
            .bind(notebook_path)
            .bind(paper_path)
            .execute(pg)
            .await?;
        }
        "error" => {
            sqlx::query(
                r#"
                UPDATE runs SET status = 'failed', updated_at = now()
                WHERE id = $1 AND status <> 'done'
                "#,
            )
            .bind(run_id)
            .execute(pg)
            .await?;
        }
        _ => {}
    }
    Ok(())
}
