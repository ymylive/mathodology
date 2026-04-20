use axum::extract::State;
use axum::Json;
use serde_json::{json, Value};

use crate::dispatch::ping_redis;
use crate::state::AppState;

#[tracing::instrument(skip_all)]
pub async fn health(State(mut state): State<AppState>) -> Json<Value> {
    let redis_ok = ping_redis(&mut state.redis).await;

    Json(json!({
        "status": if redis_ok { "ok" } else { "degraded" },
        "version": env!("CARGO_PKG_VERSION"),
        "redis_ok": redis_ok,
        "postgres_ok": Value::Null,
    }))
}
