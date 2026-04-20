use std::time::Duration;

use axum::extract::State;
use axum::Json;
use serde_json::{json, Value};

use crate::dispatch::ping_redis;
use crate::state::AppState;

#[tracing::instrument(skip_all)]
pub async fn health(State(mut state): State<AppState>) -> Json<Value> {
    let redis_ok = ping_redis(&mut state.redis).await;
    let postgres_ok = ping_postgres(&state.pg).await;

    Json(json!({
        "status": if redis_ok && postgres_ok { "ok" } else { "degraded" },
        "version": env!("CARGO_PKG_VERSION"),
        "redis_ok": redis_ok,
        "postgres_ok": postgres_ok,
    }))
}

/// `SELECT 1` with a hard 500ms ceiling. Never returns an error; callers only
/// care about true/false.
async fn ping_postgres(pg: &sqlx::PgPool) -> bool {
    let fut = sqlx::query_scalar::<_, i32>("SELECT 1").fetch_one(pg);
    match tokio::time::timeout(Duration::from_millis(500), fut).await {
        Ok(Ok(_)) => true,
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "postgres health probe failed");
            false
        }
        Err(_) => {
            tracing::warn!("postgres health probe timed out");
            false
        }
    }
}
