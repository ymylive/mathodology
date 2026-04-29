//! Integration test for the `/llm/chat/completions` SSE endpoint.
//!
//! A wiremock OpenAI-compatible fake serves 3 content chunks, then a usage
//! chunk, then `[DONE]`. The test:
//! - writes a temporary `providers.toml` pointing at the mock server
//! - boots an axum gateway on a random port
//! - POSTs with `stream:true` and no `X-Run-Id` (so we don't need run-row
//!   setup; cost recording silently short-circuits when run_id is None)
//! - asserts status 200, Content-Type text/event-stream, and that each
//!   upstream chunk is forwarded to the client, terminated by `[DONE]`.
//!
//! Note: the test relies on local Redis and Postgres being reachable at the
//! defaults in .env.example. The adapter and state wiring assume a live pool
//! exists, but the non-run-id stream path never touches either.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::serve;
use reqwest::header::{CONTENT_TYPE, HeaderMap, HeaderValue};
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

fn openai_sse_body() -> String {
    // Three content deltas + a terminal usage chunk + [DONE]. Matches the
    // shape DeepSeek and OpenAI emit: `usage` rides in the final data chunk
    // before [DONE] on the SSE wire.
    let chunks = [
        r#"{"id":"c1","model":"deepseek-chat","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}"#,
        r#"{"id":"c1","model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"lo "}}]}"#,
        r#"{"id":"c1","model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"world"}}]}"#,
        r#"{"id":"c1","model":"deepseek-chat","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}"#,
    ];
    let mut body = String::new();
    for c in &chunks {
        body.push_str("data: ");
        body.push_str(c);
        body.push_str("\n\n");
    }
    body.push_str("data: [DONE]\n\n");
    body
}

async fn write_providers_toml(mock_url: &str) -> NamedTempFile {
    let file = NamedTempFile::new().expect("tempfile");
    let toml = format!(
        r#"
[[providers]]
name = "mock"
kind = "openai_compat"
base_url = "{mock_url}/v1"
api_key_env = ""
models = ["deepseek-chat"]
price_input_per_1m = 1.0
price_output_per_1m = 2.0

[router]
default_model = "deepseek-chat"
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

    // LLM-stream test doesn't hit the figures routes, but AppState now
    // requires a canonical runs_dir. Point at an ephemeral tempdir.
    let runs_tmp = tempfile::tempdir().expect("runs tempdir");
    let runs_dir = tokio::fs::canonicalize(runs_tmp.path())
        .await
        .expect("canonicalize runs tempdir");
    // Leak the TempDir so it outlives the test; it's ephemeral anyway.
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
async fn sse_stream_forwards_chunks() {
    // --- 1. Start the wiremock fake. -------------------------------------
    let mock = MockServer::start().await;
    let body = openai_sse_body();
    Mock::given(method("POST"))
        .and(wm_path("/v1/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_raw(body.clone(), "text/event-stream"),
        )
        .mount(&mock)
        .await;

    // --- 2. Write providers.toml pointing at mock. -----------------------
    let providers_file = write_providers_toml(&mock.uri()).await;
    let state = build_state(providers_file.path().to_path_buf()).await;

    // --- 3. Boot the axum server on a random port. -----------------------
    let router = build_router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = serve(listener, router).await;
    });

    // Tiny pause to let the listener install; axum starts instantly but
    // reqwest racing the bind is technically possible.
    tokio::time::sleep(Duration::from_millis(20)).await;

    // --- 4. POST the streaming request. ----------------------------------
    let mut headers = HeaderMap::new();
    headers.insert(
        CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
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
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
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

    // --- 5. Read the whole body and check chunks are forwarded. ---------
    let body_bytes = timeout(Duration::from_secs(10), resp.bytes())
        .await
        .expect("body received")
        .expect("body bytes ok");
    let body_text = String::from_utf8_lossy(&body_bytes).into_owned();

    // Verify each upstream delta text made it through.
    for needle in ["\"Hel\"", "\"lo \"", "\"world\""] {
        assert!(
            body_text.contains(needle),
            "expected forwarded delta {needle} in body: {body_text}"
        );
    }
    assert!(
        body_text.contains("[DONE]"),
        "expected [DONE] terminator in body: {body_text}"
    );

    // Tear down the server task (best-effort; test is done regardless).
    server_handle.abort();
}
