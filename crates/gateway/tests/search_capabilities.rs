//! Integration test for `GET /search/capabilities`.
//!
//! Scope:
//! - 401 without a bearer token (it's an authed route).
//! - 200 + spec-shaped JSON with the dev token.
//! - `Cache-Control: no-store` is set (env flips shouldn't be cached).
//! - Response body never echoes a configured API key.
//!
//! Env vars are process-global, so we carefully snapshot and restore
//! `TAVILY_API_KEY` / `OPEN_WEBSEARCH_DISABLED` / `OPEN_WEBSEARCH_ENGINES`
//! inside ONE serialized test. Branch coverage for the probe helpers lives
//! in unit tests inside `routes/search.rs`, where env isn't touched at all.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::serve;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CACHE_CONTROL, CONTENT_TYPE};
use sqlx::postgres::PgPoolOptions;
use tempfile::NamedTempFile;
use tokio::net::TcpListener;
use tokio::time::timeout;

use gateway::app::build_router;
use gateway::config::AppConfig;
use gateway::llm::LlmContext;
use gateway::state::AppState;

const DEV_TOKEN: &str = "test-token-search-caps";
const FAKE_TAVILY_KEY: &str = "tvly-integration-fake-key-xyz";

async fn write_providers_toml() -> NamedTempFile {
    let file = NamedTempFile::new().expect("tempfile");
    let toml = r#"
[[providers]]
name = "mock"
kind = "openai_compat"
base_url = "http://127.0.0.1:1/v1"
api_key_env = ""
models = ["deepseek-chat"]
price_input_per_1m = 1.0
price_output_per_1m = 2.0

[router]
default_model = "deepseek-chat"
fallback = []
"#;
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
    let llm = LlmContext::bootstrap(&providers_path).expect("LlmContext::bootstrap");

    AppState {
        redis,
        pg,
        config: Arc::new(cfg),
        llm,
        runs_dir: Arc::new(runs_dir),
    }
}

/// Snapshot + restore guard for env vars that this test mutates. The
/// `/search/capabilities` handler reads directly from `std::env`, so running
/// more than one HTTP assertion in parallel against this endpoint would race.
/// We keep all HTTP-level assertions inside a single `#[tokio::test]` and
/// restore each var on drop.
struct EnvGuard {
    vars: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn capture(keys: &[&'static str]) -> Self {
        let vars = keys
            .iter()
            .map(|k| (*k, std::env::var(k).ok()))
            .collect();
        Self { vars }
    }

    fn set(&self, key: &str, value: &str) {
        std::env::set_var(key, value);
    }

    fn unset(&self, key: &str) {
        std::env::remove_var(key);
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (k, v) in &self.vars {
            match v {
                Some(val) => std::env::set_var(k, val),
                None => std::env::remove_var(k),
            }
        }
    }
}

#[tokio::test]
async fn search_capabilities_end_to_end() {
    let providers_file = write_providers_toml().await;
    let state = build_state(providers_file.path().to_path_buf()).await;

    let router = build_router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = serve(listener, router).await;
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    let client = reqwest::Client::new();
    let url = format!("http://{addr}/search/capabilities");

    // ------------------------------------------------------------------
    // 1) Unauthenticated request is rejected.
    // ------------------------------------------------------------------
    let resp = timeout(Duration::from_secs(5), client.get(&url).send())
        .await
        .expect("unauth request did not time out")
        .expect("unauth request sent");
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::UNAUTHORIZED,
        "capabilities must require dev token"
    );

    // ------------------------------------------------------------------
    // 2) Authed request with Tavily key set + engine subset.
    //    Assert shape, Cache-Control, and no key-leak.
    // ------------------------------------------------------------------
    let guard = EnvGuard::capture(&[
        "TAVILY_API_KEY",
        "OPEN_WEBSEARCH_DISABLED",
        "OPEN_WEBSEARCH_ENGINES",
    ]);
    guard.set("TAVILY_API_KEY", FAKE_TAVILY_KEY);
    guard.unset("OPEN_WEBSEARCH_DISABLED");
    guard.set("OPEN_WEBSEARCH_ENGINES", "baidu, csdn");

    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {DEV_TOKEN}")).unwrap(),
    );

    let resp = timeout(
        Duration::from_secs(5),
        client.get(&url).headers(headers.clone()).send(),
    )
    .await
    .expect("authed request did not time out")
    .expect("authed request sent");

    assert_eq!(resp.status(), 200, "authed capabilities should be 200");

    let cc = resp
        .headers()
        .get(CACHE_CONTROL)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert_eq!(cc, "no-store", "Cache-Control must be no-store");

    let ct = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert!(
        ct.starts_with("application/json"),
        "expected application/json, got {ct}"
    );

    let body_bytes = resp.bytes().await.expect("body bytes");
    let body_text = String::from_utf8(body_bytes.to_vec()).expect("utf8 body");

    // The API key must NEVER make it into the response, not even a prefix.
    assert!(
        !body_text.contains(FAKE_TAVILY_KEY),
        "response body leaked the tavily api key"
    );
    assert!(
        !body_text.contains("tvly-"),
        "response body leaked a tavily key prefix"
    );

    let json: serde_json::Value =
        serde_json::from_str(&body_text).expect("response body is valid json");
    assert_eq!(json["tavily_available"], serde_json::json!(true));
    assert_eq!(json["open_websearch_available"], serde_json::json!(true));
    assert_eq!(
        json["available_engines"],
        serde_json::json!(["baidu", "csdn"])
    );

    // ------------------------------------------------------------------
    // 3) With Tavily unset + open-websearch disabled + engines unset,
    //    the flags flip and the default 8-engine list is returned.
    // ------------------------------------------------------------------
    guard.unset("TAVILY_API_KEY");
    guard.set("OPEN_WEBSEARCH_DISABLED", "1");
    guard.unset("OPEN_WEBSEARCH_ENGINES");

    let resp = timeout(
        Duration::from_secs(5),
        client.get(&url).headers(headers.clone()).send(),
    )
    .await
    .expect("authed request did not time out")
    .expect("authed request sent");

    assert_eq!(resp.status(), 200);
    let json: serde_json::Value = resp.json().await.expect("json");
    assert_eq!(json["tavily_available"], serde_json::json!(false));
    assert_eq!(json["open_websearch_available"], serde_json::json!(false));
    assert_eq!(
        json["available_engines"],
        serde_json::json!([
            "bing", "baidu", "duckduckgo", "csdn", "juejin", "brave", "exa", "startpage"
        ])
    );

    // ------------------------------------------------------------------
    // 4) Empty Tavily key is treated as unavailable.
    // ------------------------------------------------------------------
    guard.set("TAVILY_API_KEY", "");
    guard.unset("OPEN_WEBSEARCH_DISABLED");

    let resp = timeout(
        Duration::from_secs(5),
        client.get(&url).headers(headers.clone()).send(),
    )
    .await
    .expect("authed request did not time out")
    .expect("authed request sent");
    let json: serde_json::Value = resp.json().await.expect("json");
    assert_eq!(
        json["tavily_available"],
        serde_json::json!(false),
        "empty TAVILY_API_KEY must be treated as unavailable"
    );

    drop(guard);
    server_handle.abort();
}
