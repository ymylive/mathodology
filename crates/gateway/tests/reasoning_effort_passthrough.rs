//! Integration test: reasoning_effort from the caller flows through the
//! `/llm/chat/completions` route and reaches the upstream OpenAI-compat
//! provider with BOTH `reasoning_effort` and `reasoning.effort` fields set.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::serve;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE};
use serde_json::Value;
use sqlx::postgres::PgPoolOptions;
use tempfile::NamedTempFile;
use tokio::net::TcpListener;
use tokio::time::timeout;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

use gateway::app::build_router;
use gateway::config::AppConfig;
use gateway::llm::LlmContext;
use gateway::state::AppState;

const DEV_TOKEN: &str = "test-token-xyz";

fn openai_nonstream_body() -> Value {
    serde_json::json!({
        "id": "c1",
        "model": "gpt-5",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
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
models = ["gpt-5"]
price_input_per_1m = 1.0
price_output_per_1m = 2.0

[router]
default_model = "gpt-5"
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
async fn openai_compat_receives_reasoning_fields() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "application/json")
                .set_body_json(openai_nonstream_body()),
        )
        .mount(&mock)
        .await;

    let providers_file = write_providers_toml(&mock.uri()).await;
    let state = build_state(providers_file.path().to_path_buf()).await;

    let router = build_router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = serve(listener, router).await;
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    let mut headers = HeaderMap::new();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {DEV_TOKEN}")).unwrap(),
    );

    // Non-streaming request with reasoning_effort=high.
    // Embed a UUID in the user message so the prompt cache never returns a
    // stale hit from a prior test run (shared dev Redis).
    let unique_prompt = format!("hi-{}", uuid::Uuid::new_v4());
    let client = reqwest::Client::new();
    let resp = timeout(
        Duration::from_secs(10),
        client
            .post(format!("http://{addr}/llm/chat/completions"))
            .headers(headers)
            .json(&serde_json::json!({
                "model": "gpt-5",
                "messages": [{"role": "user", "content": unique_prompt}],
                "reasoning_effort": "high",
            }))
            .send(),
    )
    .await
    .expect("request did not time out")
    .expect("request sent");

    assert_eq!(resp.status(), 200, "non-stream endpoint returns 200");

    // Inspect the wiremock-captured request: both fields must be present.
    let requests: Vec<Request> = mock.received_requests().await.unwrap_or_default();
    assert!(!requests.is_empty(), "upstream provider must be called");
    let upstream_body: Value = serde_json::from_slice(&requests[0].body)
        .expect("upstream body is JSON");
    assert_eq!(
        upstream_body["reasoning_effort"], "high",
        "top-level reasoning_effort must survive the translation"
    );
    assert_eq!(
        upstream_body["reasoning"]["effort"], "high",
        "nested reasoning.effort must also be set"
    );

    server_handle.abort();
}

#[tokio::test]
async fn openai_compat_off_strips_reasoning_fields() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "application/json")
                .set_body_json(openai_nonstream_body()),
        )
        .mount(&mock)
        .await;

    let providers_file = write_providers_toml(&mock.uri()).await;
    let state = build_state(providers_file.path().to_path_buf()).await;

    let router = build_router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = serve(listener, router).await;
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    let mut headers = HeaderMap::new();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {DEV_TOKEN}")).unwrap(),
    );

    let unique_prompt = format!("hi-{}", uuid::Uuid::new_v4());
    let client = reqwest::Client::new();
    let resp = timeout(
        Duration::from_secs(10),
        client
            .post(format!("http://{addr}/llm/chat/completions"))
            .headers(headers)
            .json(&serde_json::json!({
                "model": "gpt-5",
                "messages": [{"role": "user", "content": unique_prompt}],
                "reasoning_effort": "off",
            }))
            .send(),
    )
    .await
    .expect("request did not time out")
    .expect("request sent");

    assert_eq!(resp.status(), 200);

    let requests: Vec<Request> = mock.received_requests().await.unwrap_or_default();
    assert!(!requests.is_empty());
    let upstream_body: Value = serde_json::from_slice(&requests[0].body)
        .expect("upstream body is JSON");
    assert!(
        upstream_body.get("reasoning_effort").is_none(),
        "`off` must NOT emit reasoning_effort (got {upstream_body})"
    );
    assert!(
        upstream_body.get("reasoning").is_none(),
        "`off` must NOT emit reasoning.effort (got {upstream_body})"
    );

    server_handle.abort();
}
