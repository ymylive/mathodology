//! POST /llm/chat/completions.
//!
//! OpenAI-compatible chat completions passthrough. Two paths:
//! - `stream: false` (or missing) → buffered JSON response, consults prompt cache.
//! - `stream: true` → SSE response, forwards upstream chunks verbatim and
//!   fans `kind=token` / `kind=cost` events into `mm:events:<run_id>` when
//!   `X-Run-Id` is present.
//!
//! Client-disconnect aborts the upstream stream so we stop paying for tokens.

use std::convert::Infallible;

use axum::extract::State;
use axum::http::HeaderMap;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::Utc;
use futures::stream::{BoxStream, Stream, StreamExt};
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::dispatch::events_stream_key;
use crate::error::AppError;
use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, Usage};
use crate::llm::providers::ProviderError;
use crate::llm::{cache, cost, stream as llm_stream};
use crate::state::AppState;

/// Stream MAXLEN for `mm:events:<run_id>`. Matches worker emitter.
const EVENTS_MAXLEN: usize = 5000;

/// Entry point registered in `app::build_router`.
#[tracing::instrument(skip_all)]
pub async fn chat_completions(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Response, AppError> {
    // Parse the body into our canonical request. Extra fields are ignored.
    let req: CanonicalRequest = serde_json::from_value(body.clone()).map_err(|e| {
        AppError::BadRequest(format!("invalid chat completion body: {e}"))
    })?;

    let run_id = parse_run_id(&headers)?;
    let agent = header_str(&headers, "x-agent");

    if req.stream {
        stream_path(state, req, run_id, agent).await
    } else {
        complete_path(state, req, run_id, agent).await
    }
}

fn parse_run_id(headers: &HeaderMap) -> Result<Option<Uuid>, AppError> {
    let Some(val) = headers.get("x-run-id") else {
        return Ok(None);
    };
    let s = val
        .to_str()
        .map_err(|_| AppError::BadRequest("X-Run-Id is not valid UTF-8".into()))?;
    let id = Uuid::parse_str(s)
        .map_err(|_| AppError::BadRequest("X-Run-Id is not a valid UUID".into()))?;
    Ok(Some(id))
}

fn header_str(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

// -------------------- Non-stream path --------------------

#[tracing::instrument(skip_all, fields(model = %req.model, run_id = ?run_id))]
async fn complete_path(
    mut state: AppState,
    req: CanonicalRequest,
    run_id: Option<Uuid>,
    agent: Option<String>,
) -> Result<Response, AppError> {
    let key = cache::cache_key(&req);

    // Cache consult (non-stream only).
    if let Some(cached) = cache::get(&mut state.redis, &key).await.unwrap_or(None) {
        tracing::info!(cache_hit = true, "prompt cache hit");
        let usage = cached.usage.clone();
        let model = if cached.model.is_empty() {
            req.model.clone()
        } else {
            cached.model.clone()
        };
        let _ = cost::record_completion_cost(
            &state.pg,
            &mut state.redis,
            &state.llm.prices,
            run_id,
            agent.as_deref(),
            &model,
            &usage,
            true,
        )
        .await
        .map_err(|e| tracing::warn!(error = %e, "cost accounting failed for cache hit"));

        return Ok(Json(cached.raw).into_response());
    }

    let (served_model, response) = state
        .llm
        .router
        .complete_with_fallback(req.clone())
        .await
        .map_err(AppError::from)?;

    // Best-effort cache write. Failures are non-fatal.
    if let Err(e) = cache::set(&mut state.redis, &key, &response).await {
        tracing::warn!(error = %e, "cache SETEX failed");
    }

    let model_for_cost = if response.model.is_empty() {
        served_model
    } else {
        response.model.clone()
    };
    let _ = cost::record_completion_cost(
        &state.pg,
        &mut state.redis,
        &state.llm.prices,
        run_id,
        agent.as_deref(),
        &model_for_cost,
        &response.usage,
        false,
    )
    .await
    .map_err(|e| tracing::warn!(error = %e, "cost accounting failed"));

    Ok(Json(response.raw).into_response())
}

// -------------------- Stream path --------------------

#[tracing::instrument(skip_all, fields(model = %req.model, run_id = ?run_id))]
async fn stream_path(
    state: AppState,
    req: CanonicalRequest,
    run_id: Option<Uuid>,
    agent: Option<String>,
) -> Result<Response, AppError> {
    let (served_model, upstream) = state
        .llm
        .router
        .stream_with_fallback(req.clone())
        .await
        .map_err(AppError::from)?;

    let out_stream = build_forward_stream(
        upstream,
        state.pg.clone(),
        state.redis.clone(),
        state.llm.clone(),
        run_id,
        agent,
        served_model,
    );

    // Axum's SSE wrapper sets Content-Type: text/event-stream and handles
    // connection-close detection on the hyper side. When the client drops,
    // the Sse response is dropped, which drops our stream, which drops the
    // upstream reqwest body -> upstream is torn down promptly.
    let sse = Sse::new(out_stream).keep_alive(KeepAlive::default());
    Ok(sse.into_response())
}

/// Translate a CanonicalChunk stream into SSE Events, fanning tokens and
/// computing final cost. Produces `Event::default().data("[DONE]")` at the end.
#[allow(clippy::too_many_arguments)]
fn build_forward_stream(
    upstream: BoxStream<'static, Result<CanonicalChunk, ProviderError>>,
    pg: sqlx::PgPool,
    redis: ConnectionManager,
    llm: std::sync::Arc<crate::llm::LlmContext>,
    run_id: Option<Uuid>,
    agent: Option<String>,
    served_model: String,
) -> impl Stream<Item = Result<Event, Infallible>> + Send + 'static {
    // State carried across the fold: accumulated usage, whether we've sent [DONE],
    // connections, etc.
    struct S {
        upstream: BoxStream<'static, Result<CanonicalChunk, ProviderError>>,
        redis: ConnectionManager,
        pg: sqlx::PgPool,
        llm: std::sync::Arc<crate::llm::LlmContext>,
        run_id: Option<Uuid>,
        agent: Option<String>,
        served_model: String,
        usage: Option<Usage>,
        done_sent: bool,
        cost_finalized: bool,
    }

    let init = S {
        upstream,
        redis,
        pg,
        llm,
        run_id,
        agent,
        served_model,
        usage: None,
        done_sent: false,
        cost_finalized: false,
    };

    futures::stream::unfold(init, |mut s| async move {
        if s.done_sent {
            return None;
        }
        let next = s.upstream.next().await;
        match next {
            Some(Ok(chunk)) => {
                // Accumulate usage if this chunk carries it.
                if let Some(u) = chunk.usage.clone() {
                    s.usage = Some(u);
                }

                // Token fan-out (non-empty deltas only, run_id required).
                if let Some(rid) = s.run_id {
                    if !chunk.delta_text.is_empty() {
                        if let Err(e) = xadd_token(
                            &mut s.redis,
                            rid,
                            s.agent.as_deref(),
                            &chunk.delta_text,
                            &s.served_model,
                        )
                        .await
                        {
                            tracing::warn!(error = %e, "token XADD failed");
                        }
                    }
                }

                let data = serde_json::to_string(&chunk.raw).unwrap_or_default();
                let ev = Event::default().data(data);
                Some((Ok(ev), s))
            }
            Some(Err(err)) => {
                tracing::warn!(error = %err, "upstream stream error; emitting error event");
                let data = json!({
                    "error": err.to_string(),
                })
                .to_string();
                let ev = Event::default().event("error").data(data);
                s.done_sent = true;
                Some((Ok(ev), s))
            }
            None => {
                // Upstream exhausted cleanly. Finalize cost + emit [DONE].
                if !s.cost_finalized {
                    s.cost_finalized = true;
                    let usage = s.usage.clone().unwrap_or_default();
                    if let Err(e) = cost::record_completion_cost(
                        &s.pg,
                        &mut s.redis,
                        &s.llm.prices,
                        s.run_id,
                        s.agent.as_deref(),
                        &s.served_model,
                        &usage,
                        false,
                    )
                    .await
                    {
                        tracing::warn!(error = %e, "cost accounting failed on stream end");
                    }
                }
                s.done_sent = true;
                Some((Ok(Event::default().data("[DONE]")), s))
            }
        }
    })
}

async fn xadd_token(
    redis: &mut ConnectionManager,
    run_id: Uuid,
    agent: Option<&str>,
    text: &str,
    model: &str,
) -> redis::RedisResult<()> {
    let seq = llm_stream::next_seq(redis, &run_id).await?;
    let payload = json!({
        "run_id": run_id,
        "agent": agent,
        "kind": "token",
        "seq": seq,
        "ts": Utc::now(),
        "payload": { "text": text, "model": model },
    });
    let payload_str = serde_json::to_string(&payload).unwrap_or_default();
    let stream_key = events_stream_key(&run_id);
    let _: String = redis
        .xadd_maxlen(
            stream_key,
            redis::streams::StreamMaxlen::Approx(EVENTS_MAXLEN),
            "*",
            &[("payload", payload_str.as_str())],
        )
        .await?;
    Ok(())
}

