//! Integration test for the Anthropic adapter wired through
//! `/llm/chat/completions`.
//!
//! Mirrors `llm_stream.rs` but:
//! - writes a providers.toml with a single `kind=anthropic` entry pointing
//!   at a wiremock that speaks Anthropic's `/v1/messages` SSE shape;
//! - asserts the client gets an SSE response whose forwarded chunks carry
//!   the text deltas the fake emitted, followed by `[DONE]`.
//!
//! Like `llm_stream.rs`, this test assumes local Redis + Postgres at the
//! dev defaults. Cost accounting for the non-X-Run-Id case short-circuits
//! without touching either pool.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::serve;
use reqwest::header::{HeaderMap, HeaderValue, CONTENT_TYPE};
use sqlx::postgres::PgPoolOptions;
use tempfile::NamedTempFile;
use tokio::net::TcpListener;
use tokio::time::timeout;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use gateway::app::build_router;
use gateway::config::AppConfig;
use gateway::llm::LlmContext;
use gateway::state::AppState;

const DEV_TOKEN: &str = "test-token-xyz";

/// Build the canonical Anthropic SSE event sequence: `message_start` with
/// input_tokens, two text deltas, a `message_delta` with output_tokens,
/// then `message_stop`. Intentionally also includes a `ping` and the
/// `content_block_start`/`content_block_stop` boundaries the adapter is
/// supposed to silently drop.
fn anthropic_sse_body() -> String {
    let events: &[(&str, &str)] = &[
        (
            "message_start",
            r#"{"type":"message_start","message":{"id":"msg_01","role":"assistant","model":"claude-sonnet-4-6","content":[],"usage":{"input_tokens":12,"output_tokens":0}}}"#,
        ),
        (
            "content_block_start",
            r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
        ),
        (
            "ping",
            r#"{"type":"ping"}"#,
        ),
        (
            "content_block_delta",
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}"#,
        ),
        (
            "content_block_delta",
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}"#,
        ),
        (
            "content_block_stop",
            r#"{"type":"content_block_stop","index":0}"#,
        ),
        (
            "message_delta",
            r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":25}}"#,
        ),
        (
            "message_stop",
            r#"{"type":"message_stop"}"#,
        ),
    ];
    let mut body = String::new();
    for (ev, data) in events {
        body.push_str("event: ");
        body.push_str(ev);
        body.push('\n');
        body.push_str("data: ");
        body.push_str(data);
        body.push_str("\n\n");
    }
    body
}

async fn write_providers_toml(mock_url: &str) -> NamedTempFile {
    let file = NamedTempFile::new().expect("tempfile");
    let toml = format!(
        r#"
[[providers]]
name = "mock-anthropic"
kind = "anthropic"
base_url = "{mock_url}"
api_key_env = ""
models = ["claude-sonnet-4-6"]
price_input_per_1m = 22.0
price_output_per_1m = 110.0

[router]
default_model = "claude-sonnet-4-6"
fallback = []
"#
    );
    std::fs::write(file.path(), toml).expect("write providers.toml");
    file
}

async fn build_state(providers_path: PathBuf) -> AppState {
    let redis_url = std::env::var("TEST_REDIS_URL")
        .unwrap_or_else(|_| "redis://127.0.0.1:6379/0".into());
    let database_url = std::env::var("TEST_DATABASE_URL")
        .unwrap_or_else(|_| "postgres://mm:mm@127.0.0.1:5432/mm".into());

    let client = redis::Client::open(redis_url.clone()).expect("redis client");
    let redis = redis::aio::ConnectionManager::new(client)
        .await
        .expect("redis connect");

    let pg = PgPoolOptions::new()
        .max_connections(2)
        .acquire_timeout(Duration::from_secs(3))
        .connect(&database_url)
        .await
        .expect("postgres connect");

    let runs_tmp = tempfile::tempdir().expect("runs tempdir");
    let runs_dir = tokio::fs::canonicalize(runs_tmp.path())
        .await
        .expect("canonicalize runs tempdir");
    std::mem::forget(runs_tmp);

    let cfg = AppConfig {
        host: "127.0.0.1".into(),
        port: 0,
        dev_auth_token: DEV_TOKEN.into(),
        redis_url,
        database_url,
        providers_path: providers_path.clone(),
        runs_dir: runs_dir.clone(),
        static_dir: None,
    };
    let llm =
        LlmContext::bootstrap(&providers_path).expect("LlmContext::bootstrap");

    AppState {
        redis,
        pg,
        config: Arc::new(cfg),
        llm,
        runs_dir: Arc::new(runs_dir),
    }
}

#[tokio::test]
async fn anthropic_sse_stream_forwards_text_deltas() {
    // 1. Wiremock /v1/messages returning the Anthropic SSE sequence.
    let mock = MockServer::start().await;
    let body = anthropic_sse_body();
    Mock::given(method("POST"))
        .and(wm_path("/v1/messages"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_raw(body.clone(), "text/event-stream"),
        )
        .mount(&mock)
        .await;

    // 2. providers.toml → the mock.
    let providers_file = write_providers_toml(&mock.uri()).await;
    let state = build_state(providers_file.path().to_path_buf()).await;

    // 3. Boot axum.
    let router = build_router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = serve(listener, router).await;
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    // 4. POST the streaming request.
    let mut headers = HeaderMap::new();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    headers.insert(
        reqwest::header::AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {DEV_TOKEN}")).unwrap(),
    );

    let client = reqwest::Client::new();
    let resp = timeout(
        Duration::from_secs(10),
        client
            .post(format!("http://{addr}/llm/chat/completions"))
            .headers(headers)
            .json(&serde_json::json!({
                "model": "claude-sonnet-4-6",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hi"}
                ],
                "stream": true,
            }))
            .send(),
    )
    .await
    .expect("request did not time out")
    .expect("request sent");

    assert_eq!(resp.status(), 200, "SSE endpoint returns 200");
    let ct = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert!(
        ct.starts_with("text/event-stream"),
        "expected text/event-stream, got {ct}"
    );

    // 5. Collect the body and verify each forwarded chunk is present.
    let body_bytes = timeout(Duration::from_secs(10), resp.bytes())
        .await
        .expect("body received")
        .expect("body bytes ok");
    let body_text = String::from_utf8_lossy(&body_bytes).into_owned();

    // Each text delta JSON rides in a `data:` frame.
    for needle in ["\"Hello\"", "\" world\""] {
        assert!(
            body_text.contains(needle),
            "expected forwarded delta {needle} in body: {body_text}"
        );
    }
    // The synthetic usage chunk (message_delta raw) should also be on the wire.
    assert!(
        body_text.contains("\"output_tokens\":25"),
        "expected usage frame in body: {body_text}"
    );
    assert!(
        body_text.contains("[DONE]"),
        "expected [DONE] terminator in body: {body_text}"
    );

    // Ignored events should NOT leak to the client.
    assert!(
        !body_text.contains("\"type\":\"ping\""),
        "ping frames should be dropped, got: {body_text}"
    );

    server_handle.abort();
}
