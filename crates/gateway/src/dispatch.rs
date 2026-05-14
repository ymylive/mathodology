use chrono::Utc;
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde_json::Value;
use uuid::Uuid;

use crate::error::AppError;

pub const JOBS_STREAM: &str = "mm:jobs";
pub const JOBS_MAXLEN: usize = 10_000;

/// Separate stream for natural-language paper fine-tune requests. We keep
/// these off `mm:jobs` so worker concurrency budgets and ack semantics
/// don't interfere with full-pipeline runs.
pub const FINETUNE_STREAM: &str = "mm:finetune";
pub const FINETUNE_MAXLEN: usize = 5_000;

pub fn events_stream_key(run_id: &Uuid) -> String {
    format!("mm:events:{run_id}")
}

/// XADD mm:jobs with approximate maxlen, fields: run_id, payload, created_at.
pub async fn enqueue_job(
    redis: &mut ConnectionManager,
    run_id: &Uuid,
    payload: &Value,
) -> Result<String, AppError> {
    let payload_str = serde_json::to_string(payload)?;
    let created_at = Utc::now().to_rfc3339();
    let run_id_str = run_id.to_string();

    let fields: &[(&str, &str)] = &[
        ("run_id", run_id_str.as_str()),
        ("payload", payload_str.as_str()),
        ("created_at", created_at.as_str()),
    ];

    // XADD <stream> MAXLEN ~ <n> * field value [field value ...]
    let id: String = redis
        .xadd_maxlen(
            JOBS_STREAM,
            redis::streams::StreamMaxlen::Approx(JOBS_MAXLEN),
            "*",
            fields,
        )
        .await?;

    Ok(id)
}

/// Redis-key TTL for `mm:cancel:<run_id>` cancellation signal. Worker
/// polls between stages and raises `RunCancelled` when this is set.
/// 1 hour is plenty — a run never lives longer than ~90 minutes today.
pub const CANCEL_KEY_TTL_SECS: u64 = 3600;

pub fn cancel_key(run_id: &Uuid) -> String {
    format!("mm:cancel:{run_id}")
}

/// SET mm:cancel:<run_id> = "1" with TTL, signalling the worker to halt
/// the pipeline at its next stage boundary. Returns `true` if newly set,
/// `false` if it was already set (idempotent).
pub async fn signal_cancel(
    redis: &mut ConnectionManager,
    run_id: &Uuid,
) -> Result<bool, AppError> {
    let key = cancel_key(run_id);
    // SET key "1" EX <ttl> NX so a second click doesn't reset the TTL.
    let was_set: Option<String> = redis::cmd("SET")
        .arg(&key)
        .arg("1")
        .arg("EX")
        .arg(CANCEL_KEY_TTL_SECS)
        .arg("NX")
        .query_async(redis)
        .await?;
    Ok(was_set.is_some())
}

/// XADD mm:finetune. Fields: run_id, session_id, message, created_at.
pub async fn enqueue_finetune_job(
    redis: &mut ConnectionManager,
    run_id: &Uuid,
    session_id: &Uuid,
    message: &str,
) -> Result<String, AppError> {
    let created_at = Utc::now().to_rfc3339();
    let run_id_str = run_id.to_string();
    let session_id_str = session_id.to_string();
    let fields: &[(&str, &str)] = &[
        ("run_id", run_id_str.as_str()),
        ("session_id", session_id_str.as_str()),
        ("message", message),
        ("created_at", created_at.as_str()),
    ];
    let id: String = redis
        .xadd_maxlen(
            FINETUNE_STREAM,
            redis::streams::StreamMaxlen::Approx(FINETUNE_MAXLEN),
            "*",
            fields,
        )
        .await?;
    Ok(id)
}

/// Lightweight Redis PING for /health.
pub async fn ping_redis(redis: &mut ConnectionManager) -> bool {
    let res: redis::RedisResult<String> = redis::cmd("PING").query_async(redis).await;
    matches!(res, Ok(ref s) if s == "PONG")
}
