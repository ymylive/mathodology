//! Integration tests for `GET /runs/:run_id/figures/*path`.
//!
//! Verifies:
//!   * 200 + correct Content-Type on a known PNG under runs_dir
//!   * 401 without a bearer token
//!   * 403 on a path-traversal attempt (`../../../etc/passwd`)
//!   * 404 on a non-existent file
//!   * 415 on a disallowed extension (`.exe`)
//!
//! Like `llm_stream.rs`, this test relies on local Redis + Postgres being
//! reachable at the defaults in `.env.example` — `AppState` holds live pools
//! even though the figures handler itself does not touch them.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::serve;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CACHE_CONTROL, CONTENT_TYPE};
use sqlx::postgres::PgPoolOptions;
use tempfile::{NamedTempFile, TempDir};
use tokio::net::TcpListener;
use tokio::time::timeout;
use uuid::Uuid;

use gateway::app::build_router;
use gateway::config::AppConfig;
use gateway::llm::LlmContext;
use gateway::state::AppState;

const DEV_TOKEN: &str = "test-token-figures";

/// 12 bytes that start with the PNG magic signature. The handler dispatches
/// Content-Type purely from the file extension, so exact PNG validity is
/// not required for the assertion — we just want deterministic bytes.
const FAKE_PNG_BYTES: &[u8] = &[
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0xDE, 0xAD, 0xBE, 0xEF,
];

async fn write_providers_toml() -> NamedTempFile {
    let file = NamedTempFile::new().expect("tempfile");
    // Minimal valid provider config — the LLM endpoints aren't exercised here
    // but `LlmContext::bootstrap` is called during state construction.
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

async fn build_state(providers_path: PathBuf, runs_dir: PathBuf) -> AppState {
    let redis_url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/0".into());
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

/// Boot the full router on a random port. Returns (addr, tempdir, run_id).
/// The tempdir is returned so the caller keeps it alive for the duration of
/// the test (drop deletes the scratch files).
async fn boot_server() -> (SocketAddr, TempDir, NamedTempFile, Uuid) {
    let runs_tmp = tempfile::tempdir().expect("runs tempdir");
    let runs_dir = tokio::fs::canonicalize(runs_tmp.path())
        .await
        .expect("canonicalize runs tempdir");

    // Seed a run's figures dir with a fake PNG.
    let run_id = Uuid::new_v4();
    let figures_dir = runs_dir.join(run_id.to_string()).join("figures");
    tokio::fs::create_dir_all(&figures_dir)
        .await
        .expect("mkdir figures");
    tokio::fs::write(figures_dir.join("fig-0.png"), FAKE_PNG_BYTES)
        .await
        .expect("write fig-0.png");
    // Also drop an .exe to exercise the extension deny-list path.
    tokio::fs::write(figures_dir.join("evil.exe"), b"MZ\x90\x00")
        .await
        .expect("write evil.exe");

    let providers_file = write_providers_toml().await;
    let state = build_state(providers_file.path().to_path_buf(), runs_dir).await;

    let router = build_router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = serve(listener, router).await;
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    (addr, runs_tmp, providers_file, run_id)
}

fn auth_headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {DEV_TOKEN}")).unwrap(),
    );
    h
}

#[tokio::test]
async fn figure_served_ok() {
    let (addr, _tmp, _prov, run_id) = boot_server().await;
    let client = reqwest::Client::new();

    let resp = timeout(
        Duration::from_secs(5),
        client
            .get(format!("http://{addr}/runs/{run_id}/figures/fig-0.png"))
            .headers(auth_headers())
            .send(),
    )
    .await
    .expect("request did not time out")
    .expect("request sent");

    assert_eq!(resp.status(), 200, "200 OK for existing figure");
    let ct = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert_eq!(ct, "image/png", "Content-Type from extension");
    let cc = resp
        .headers()
        .get(CACHE_CONTROL)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert_eq!(cc, "private, max-age=3600", "Cache-Control header");

    let body = resp.bytes().await.expect("body bytes");
    assert_eq!(body.as_ref(), FAKE_PNG_BYTES, "body matches written bytes");
}

#[tokio::test]
async fn figure_requires_auth() {
    let (addr, _tmp, _prov, run_id) = boot_server().await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("http://{addr}/runs/{run_id}/figures/fig-0.png"))
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 401, "401 without Authorization");
}

#[tokio::test]
async fn figure_path_traversal_blocked() {
    let (addr, _tmp, _prov, run_id) = boot_server().await;

    // A plain URL containing `../../../etc/passwd` gets normalized by
    // reqwest/url at parse time (RFC 3986 dot-segment removal), so the
    // server never sees `..`. To verify the defense in the handler we send
    // a raw HTTP/1.1 request where the request-target still contains `..`.
    let target = format!("/runs/{run_id}/figures/../../../etc/passwd");
    let status = raw_http_get_status(addr, &target, Some(&format!("Bearer {DEV_TOKEN}"))).await;
    assert_eq!(status, 403, "path traversal must 403, got {status}");
}

/// Send a crude HTTP/1.1 GET request over a raw TCP socket so that the
/// request-target is transmitted verbatim (no client-side dot-segment
/// normalization). Returns the numeric status code.
async fn raw_http_get_status(addr: SocketAddr, target: &str, auth: Option<&str>) -> u16 {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let mut req = format!(
        "GET {target} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n"
    );
    if let Some(a) = auth {
        req.push_str(&format!("Authorization: {a}\r\n"));
    }
    req.push_str("\r\n");

    let mut sock = tokio::net::TcpStream::connect(addr).await.expect("connect");
    sock.write_all(req.as_bytes()).await.expect("write req");
    let mut buf = Vec::with_capacity(256);
    // Read just enough to parse the status line.
    let mut tmp = [0u8; 256];
    let n = timeout(Duration::from_secs(5), sock.read(&mut tmp))
        .await
        .expect("no timeout")
        .expect("read status");
    buf.extend_from_slice(&tmp[..n]);
    let head = String::from_utf8_lossy(&buf);
    let status_line = head.lines().next().unwrap_or("");
    // "HTTP/1.1 403 Forbidden"
    status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
}

#[tokio::test]
async fn figure_missing_is_404() {
    let (addr, _tmp, _prov, run_id) = boot_server().await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!(
            "http://{addr}/runs/{run_id}/figures/does-not-exist.png"
        ))
        .headers(auth_headers())
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 404, "missing file → 404");
}

#[tokio::test]
async fn figure_bad_extension_is_415() {
    let (addr, _tmp, _prov, run_id) = boot_server().await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("http://{addr}/runs/{run_id}/figures/evil.exe"))
        .headers(auth_headers())
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 415, "disallowed extension → 415");
}
