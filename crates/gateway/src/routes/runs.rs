use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::dispatch::enqueue_job;
use crate::error::AppError;
use crate::state::AppState;

/// Minimal mirror of ProblemInput from openapi.yaml. `extra` soaks up fields we
/// don't care about in the gateway (attachments, competition_type, etc.) and
/// hands them through to the worker verbatim.
#[derive(Debug, Deserialize, Serialize)]
pub struct ProblemInput {
    pub problem_text: String,
    #[serde(flatten)]
    pub extra: std::collections::BTreeMap<String, Value>,
}

#[derive(Debug, Serialize)]
pub struct RunCreated {
    pub run_id: Uuid,
    pub status: &'static str,
}

#[tracing::instrument(skip_all)]
pub async fn create_run(
    State(mut state): State<AppState>,
    Json(input): Json<ProblemInput>,
) -> Result<(StatusCode, Json<RunCreated>), AppError> {
    if input.problem_text.trim().is_empty() {
        return Err(AppError::BadRequest(
            "problem_text must be non-empty".to_string(),
        ));
    }

    let run_id = Uuid::new_v4();
    let payload = serde_json::to_value(&input)?;

    let stream_id = enqueue_job(&mut state.redis, &run_id, &payload).await?;
    tracing::info!(%run_id, stream_id, "run enqueued to mm:jobs");

    Ok((
        StatusCode::CREATED,
        Json(RunCreated {
            run_id,
            status: "queued",
        }),
    ))
}

/// M1 stub: Postgres isn't wired yet. Return 501 per spec.
#[tracing::instrument(skip_all)]
pub async fn get_run_stub(
    State(_state): State<AppState>,
    Path(run_id): Path<Uuid>,
) -> Result<Json<Value>, AppError> {
    tracing::debug!(%run_id, "get_run_stub called (M1: no-op)");
    Err(AppError::NotImplemented)
}
