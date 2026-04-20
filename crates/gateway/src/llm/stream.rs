//! Stream-side helpers.
//!
//! The SSE byte → CanonicalChunk parse lives in `providers::openai_compat`
//! because it's adapter-specific. This module houses shared stream glue
//! (redis XADD for per-token events, sequence counter helpers).

use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use uuid::Uuid;

/// Redis key that holds the monotonic per-run sequence counter. The gateway
/// and the Python worker both INCR this key so ordering is globally
/// consistent regardless of which side emitted first.
pub fn seq_key(run_id: &Uuid) -> String {
    format!("mm:seq:{run_id}")
}

/// Get the next sequence number for a run. Monotonic, global across all
/// producers writing to `mm:events:<run_id>`.
pub async fn next_seq(
    redis: &mut ConnectionManager,
    run_id: &Uuid,
) -> redis::RedisResult<i64> {
    let key = seq_key(run_id);
    redis.incr(key, 1).await
}
