use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::Json;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::FromRow;
use uuid::Uuid;

use crate::audit::spawn_audit_task;
use crate::dispatch::enqueue_job;
use crate::error::AppError;
use crate::state::AppState;

/// Minimal mirror of ProblemInput from openapi.yaml. `extra` soaks up fields we
/// don't care about in the gateway (attachments, model_override, etc.) and
/// hands them through to the worker verbatim. `competition_type` is normalized
/// here so the worker never receives null / missing / empty on this field.
#[derive(Debug, Deserialize, Serialize)]
pub struct ProblemInput {
    pub problem_text: String,
    #[serde(
        default = "default_competition_type",
        deserialize_with = "deserialize_competition_type"
    )]
    pub competition_type: String,
    #[serde(flatten)]
    pub extra: std::collections::BTreeMap<String, Value>,
}

fn default_competition_type() -> String {
    "other".to_string()
}

fn deserialize_competition_type<'de, D>(d: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let v: Option<String> = Option::deserialize(d)?;
    Ok(v.filter(|s| !s.is_empty())
        .unwrap_or_else(default_competition_type))
}

#[derive(Debug, Serialize)]
pub struct RunCreated {
    pub run_id: Uuid,
    pub status: &'static str,
}

/// Matches the `runs` row schema. `cost_rmb` is cast to text in SQL so we
/// don't need the bigdecimal/rust_decimal sqlx feature; we parse to f64 on
/// the way out to the API.
#[derive(Debug, FromRow)]
struct RunRow {
    id: Uuid,
    status: String,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
    problem_text: String,
    #[allow(dead_code)]
    competition_type: String,
    cost_rmb: String,
    notebook_path: Option<String>,
    paper_path: Option<String>,
}

#[derive(Debug, FromRow)]
struct EventRow {
    run_id: Uuid,
    seq: i64,
    ts: DateTime<Utc>,
    agent: Option<String>,
    kind: String,
    payload: Value,
}

#[tracing::instrument(skip_all, fields(run_id))]
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
    tracing::Span::current().record("run_id", tracing::field::display(run_id));

    // 1) Persist the run row first. If this fails we must NOT XADD.
    sqlx::query(
        r#"
        INSERT INTO runs (id, problem_text, competition_type, status)
        VALUES ($1, $2, $3, 'queued')
        "#,
    )
    .bind(run_id)
    .bind(&input.problem_text)
    .bind(&input.competition_type)
    .execute(&state.pg)
    .await
    .map_err(|e| {
        tracing::error!(%run_id, error = %e, "failed to insert run row");
        AppError::Internal(format!("insert run: {e}"))
    })?;

    // 2) Enqueue job onto mm:jobs.
    let payload = serde_json::to_value(&input)?;
    let stream_id = enqueue_job(&mut state.redis, &run_id, &payload).await?;
    tracing::info!(%run_id, stream_id, "run enqueued to mm:jobs");

    // 3) Spawn the audit task that tails mm:events:<run_id> into events_audit.
    spawn_audit_task(state.clone(), run_id);

    Ok((
        StatusCode::CREATED,
        Json(RunCreated {
            run_id,
            status: "queued",
        }),
    ))
}

#[tracing::instrument(skip_all, fields(%run_id))]
pub async fn get_run(
    State(state): State<AppState>,
    Path(run_id): Path<Uuid>,
) -> Result<Json<Value>, AppError> {
    // Fetch the run row. RowNotFound -> 404.
    let run: RunRow = sqlx::query_as::<_, RunRow>(
        r#"
        SELECT id, status::text AS status, created_at, updated_at,
               problem_text, competition_type, cost_rmb::text AS cost_rmb,
               notebook_path, paper_path
        FROM runs
        WHERE id = $1
        "#,
    )
    .bind(run_id)
    .fetch_one(&state.pg)
    .await?;

    // Fetch all audit events for this run, ordered.
    let events: Vec<EventRow> = sqlx::query_as::<_, EventRow>(
        r#"
        SELECT run_id, seq, ts, agent, kind, payload
        FROM events_audit
        WHERE run_id = $1
        ORDER BY seq ASC
        "#,
    )
    .bind(run_id)
    .fetch_all(&state.pg)
    .await?;

    let events_json: Vec<Value> = events
        .into_iter()
        .map(|e| {
            json!({
                "run_id": e.run_id,
                "agent": e.agent,
                "kind": e.kind,
                "seq": e.seq,
                "ts": e.ts,
                "payload": e.payload,
            })
        })
        .collect();

    let cost_rmb_f64: f64 = run.cost_rmb.parse().unwrap_or(0.0);

    Ok(Json(json!({
        "run_id": run.id,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "problem_text": run.problem_text,
        "cost_rmb": cost_rmb_f64,
        "notebook_path": run.notebook_path,
        "paper_path": run.paper_path,
        "events": events_json,
    })))
}

#[derive(Debug, Deserialize)]
pub struct ListQuery {
    /// Max rows to return (default 20, cap 200).
    pub limit: Option<u32>,
    /// Filter by status (queued / running / done / failed / cancelled).
    pub status: Option<String>,
}

#[derive(Debug, FromRow)]
struct RunSummary {
    id: Uuid,
    status: String,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
    problem_text: String,
    competition_type: String,
    cost_rmb: String,
    notebook_path: Option<String>,
    paper_path: Option<String>,
}

/// Light-weight listing endpoint for the Dashboard. Returns run headers
/// sorted newest-first. No events joined — call GET /runs/:id for depth.
#[tracing::instrument(skip_all)]
pub async fn list_runs(
    State(state): State<AppState>,
    axum::extract::Query(q): axum::extract::Query<ListQuery>,
) -> Result<Json<Value>, AppError> {
    let limit = q.limit.unwrap_or(20).clamp(1, 200) as i64;

    let rows: Vec<RunSummary> = if let Some(st) = q.status.as_deref() {
        sqlx::query_as::<_, RunSummary>(
            r#"
            SELECT id, status::text AS status, created_at, updated_at,
                   problem_text, competition_type, cost_rmb::text AS cost_rmb,
                   notebook_path, paper_path
            FROM runs
            WHERE status::text = $1
            ORDER BY created_at DESC
            LIMIT $2
            "#,
        )
        .bind(st)
        .bind(limit)
        .fetch_all(&state.pg)
        .await?
    } else {
        sqlx::query_as::<_, RunSummary>(
            r#"
            SELECT id, status::text AS status, created_at, updated_at,
                   problem_text, competition_type, cost_rmb::text AS cost_rmb,
                   notebook_path, paper_path
            FROM runs
            ORDER BY created_at DESC
            LIMIT $1
            "#,
        )
        .bind(limit)
        .fetch_all(&state.pg)
        .await?
    };

    let items: Vec<Value> = rows
        .into_iter()
        .map(|r| {
            let cost = r.cost_rmb.parse::<f64>().unwrap_or(0.0);
            json!({
                "run_id": r.id,
                "status": r.status,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "problem_text": r.problem_text,
                "competition_type": r.competition_type,
                "cost_rmb": cost,
                "notebook_path": r.notebook_path,
                "paper_path": r.paper_path,
            })
        })
        .collect();

    Ok(Json(json!({ "items": items })))
}
