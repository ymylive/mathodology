use std::time::Duration;

use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::response::IntoResponse;
use redis::aio::ConnectionManager;
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::AsyncCommands;
use serde::Deserialize;
use serde_json::Value;
use uuid::Uuid;

use crate::dispatch::events_stream_key;
use crate::state::AppState;

/// Optional first client frame.
#[derive(Debug, Deserialize)]
struct Hello {
    #[serde(rename = "type")]
    _type: String,
    #[allow(dead_code)]
    run_id: Option<String>,
    last_seq: Option<u64>,
}

#[tracing::instrument(skip_all, fields(%run_id))]
pub async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
    Path(run_id): Path<Uuid>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| async move {
        if let Err(e) = handle_socket(socket, state, run_id).await {
            tracing::warn!(%run_id, error = %e, "ws session ended with error");
        }
    })
}

async fn handle_socket(
    mut socket: WebSocket,
    state: AppState,
    run_id: Uuid,
) -> anyhow::Result<()> {
    let stream_key = events_stream_key(&run_id);
    let mut redis = state.redis.clone();

    // --- Optional hello frame with `last_seq` ------------------------------
    //
    // Worker XADDs with `*` (auto-assigned timestamp IDs), so a client-facing
    // `seq` has no direct mapping to a Redis stream ID. We always XREAD from
    // `0-0` and filter forwarded entries by payload.seq > last_seq instead.
    // Redis per-run streams are small (MAXLEN ~5000), so re-scanning is cheap.
    let mut last_seq: u64 = 0;
    tokio::select! {
        maybe_msg = socket.recv() => {
            if let Some(Ok(Message::Text(txt))) = maybe_msg {
                if let Ok(hello) = serde_json::from_str::<Hello>(&txt) {
                    if let Some(s) = hello.last_seq { last_seq = s; }
                }
            }
        }
        _ = tokio::time::sleep(Duration::from_millis(200)) => {}
    }

    let mut last_id = "0-0".to_string();
    tracing::debug!(%run_id, stream_key, last_seq, "ws subscribed to events stream");

    // --- Main loop: XREAD with short block timeout, interleaved with client recv ---
    let opts = StreamReadOptions::default().block(500).count(32);

    loop {
        tokio::select! {
            // (a) Redis-side pull.
            read_res = xread_once(&mut redis, &stream_key, &last_id, &opts) => {
                match read_res {
                    Ok(Some(entries)) => {
                        for (entry_id, payload) in entries {
                            // Advance Redis cursor for the next read regardless of filter.
                            last_id = entry_id;

                            // Skip events already seen by the client.
                            if event_seq(&payload).is_some_and(|s| s <= last_seq) {
                                continue;
                            }

                            if socket.send(Message::Text(payload.clone())).await.is_err() {
                                tracing::debug!(%run_id, "client send failed; closing");
                                return Ok(());
                            }

                            // If this was a `done` event, flush + close cleanly.
                            if is_done_event(&payload) {
                                tracing::info!(%run_id, "done event forwarded; closing ws");
                                tokio::time::sleep(Duration::from_millis(50)).await;
                                let _ = socket.send(Message::Close(Some(CloseFrame {
                                    code: axum::extract::ws::close_code::NORMAL,
                                    reason: std::borrow::Cow::Borrowed("run done"),
                                }))).await;
                                return Ok(());
                            }
                        }
                    }
                    Ok(None) => {
                        // XREAD timed out with no entries. Loop back and check client too.
                    }
                    Err(e) => {
                        tracing::warn!(%run_id, error = %e, "XREAD error; ending ws");
                        return Ok(());
                    }
                }
            }

            // (b) Client-side recv (disconnect / ping / text we ignore).
            msg = socket.recv() => {
                match msg {
                    None => {
                        tracing::debug!(%run_id, "client closed ws");
                        return Ok(());
                    }
                    Some(Err(e)) => {
                        tracing::debug!(%run_id, error = %e, "client recv error");
                        return Ok(());
                    }
                    Some(Ok(Message::Close(_))) => {
                        tracing::debug!(%run_id, "client sent close frame");
                        return Ok(());
                    }
                    Some(Ok(_)) => {
                        // Ignore further text/binary/ping/pong frames in M1.
                    }
                }
            }
        }
    }
}

/// Single XREAD call. Returns `Ok(None)` if the command blocked and returned
/// no entries (timeout), `Ok(Some(...))` otherwise.
async fn xread_once(
    redis: &mut ConnectionManager,
    stream_key: &str,
    last_id: &str,
    opts: &StreamReadOptions,
) -> redis::RedisResult<Option<Vec<(String, String)>>> {
    let reply: Option<StreamReadReply> = redis
        .xread_options(&[stream_key], &[last_id], opts)
        .await?;

    let Some(reply) = reply else {
        return Ok(None);
    };

    let mut out = Vec::new();
    for key in reply.keys {
        for entry in key.ids {
            // Events are stored with a single `payload` field holding the JSON string.
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

/// True if the JSON payload has `"kind":"done"` at the top level.
fn is_done_event(payload: &str) -> bool {
    serde_json::from_str::<Value>(payload)
        .ok()
        .and_then(|v| v.get("kind").and_then(|k| k.as_str()).map(str::to_owned))
        .as_deref()
        == Some("done")
}

/// Extract the `seq` field from a JSON event payload.
fn event_seq(payload: &str) -> Option<u64> {
    serde_json::from_str::<Value>(payload)
        .ok()
        .and_then(|v| v.get("seq").and_then(|s| s.as_u64()))
}
