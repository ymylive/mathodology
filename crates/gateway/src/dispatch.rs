use chrono::Utc;
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde_json::Value;
use uuid::Uuid;

use crate::error::AppError;

pub const JOBS_STREAM: &str = "mm:jobs";
pub const JOBS_MAXLEN: usize = 10_000;

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

/// Lightweight Redis PING for /health.
pub async fn ping_redis(redis: &mut ConnectionManager) -> bool {
    let res: redis::RedisResult<String> = redis::cmd("PING").query_async(redis).await;
    matches!(res, Ok(ref s) if s == "PONG")
}
