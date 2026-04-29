//! Integration tests for `GET /runs/:run_id/export/:format`.
//!
//! Pure-HTTP tests (no external binaries): 401, 422 format / 422 template,
//! 404 when paper.meta.json is missing, md passthrough attachment.
//!
//! Compile / binary-dependent tests (`pdf`, `docx`) are behind
//! `#[ignore]`; run them explicitly with:
//!   cargo test -p gateway --test export_paper -- --ignored
//!
//! All tests use the same pattern as `figures_serve.rs` — a live Redis +
//! Postgres are expected at `TEST_REDIS_URL` / `TEST_DATABASE_URL` (defaults
//! mirror `.env.example`). `AppState` holds the pools even though the export
//! handler doesn't touch them.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::serve;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_DISPOSITION, CONTENT_TYPE};
use sqlx::postgres::PgPoolOptions;
use tempfile::{NamedTempFile, TempDir};
use tokio::net::TcpListener;
use tokio::time::timeout;
use uuid::Uuid;

use gateway::app::build_router;
use gateway::config::AppConfig;
use gateway::llm::LlmContext;
use gateway::state::AppState;

const DEV_TOKEN: &str = "test-token-export";

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
        static_dir: None,
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

/// Boot a router and seed a run directory with `paper.md` + `paper.meta.json`.
/// Returns (addr, runs_tempdir, providers_file, run_id).
async fn boot_server_with_paper(seed_meta: bool) -> (SocketAddr, TempDir, NamedTempFile, Uuid) {
    let runs_tmp = tempfile::tempdir().expect("runs tempdir");
    let runs_dir = tokio::fs::canonicalize(runs_tmp.path())
        .await
        .expect("canonicalize runs tempdir");

    let run_id = Uuid::new_v4();
    let run_root = runs_dir.join(run_id.to_string());
    tokio::fs::create_dir_all(&run_root).await.expect("mkdir run");
    tokio::fs::create_dir_all(run_root.join("figures"))
        .await
        .expect("mkdir figures");

    // Always drop a minimal paper.md so the `md` route has something to serve.
    let md = "# Title\n\nBody paragraph.\n";
    tokio::fs::write(run_root.join("paper.md"), md)
        .await
        .expect("write paper.md");

    if seed_meta {
        let meta = serde_json::json!({
            "title": "Test Paper",
            "abstract": "A short abstract.",
            "competition_type": "cumcm",
            "problem_text": "Some problem text.",
            "sections": [
                {"title": "Intro", "body_markdown": "Hello world."}
            ],
            "references": ["Ref 1."],
            "figures": []
        });
        tokio::fs::write(
            run_root.join("paper.meta.json"),
            serde_json::to_vec_pretty(&meta).unwrap(),
        )
        .await
        .expect("write paper.meta.json");
    }

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
async fn export_md_returns_paper_with_attachment_disposition() {
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = timeout(
        Duration::from_secs(5),
        client
            .get(format!("http://{addr}/runs/{run_id}/export/md"))
            .headers(auth_headers())
            .send(),
    )
    .await
    .expect("no timeout")
    .expect("request sent");

    assert_eq!(resp.status(), 200);
    let ct = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert_eq!(ct, "text/markdown; charset=utf-8");

    let cd = resp
        .headers()
        .get(CONTENT_DISPOSITION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert!(cd.contains("attachment"));
    assert!(cd.contains(&format!("paper-{run_id}.md")));

    let body = resp.text().await.expect("body");
    assert!(body.contains("Title"));
}

#[tokio::test]
async fn export_requires_auth() {
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("http://{addr}/runs/{run_id}/export/md"))
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 401);
}

#[tokio::test]
async fn export_unknown_format_is_422() {
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("http://{addr}/runs/{run_id}/export/exe"))
        .headers(auth_headers())
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 422);
}

#[tokio::test]
async fn export_unknown_template_is_422() {
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!(
            "http://{addr}/runs/{run_id}/export/tex?template=bogus"
        ))
        .headers(auth_headers())
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 422);
}

#[tokio::test]
async fn export_missing_meta_is_404_for_tex() {
    // seed_meta=false → paper.md exists but paper.meta.json does not
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(false).await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("http://{addr}/runs/{run_id}/export/tex"))
        .headers(auth_headers())
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 404);
}

#[tokio::test]
async fn export_missing_run_is_404() {
    let (addr, _tmp, _prov, _run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let other = Uuid::new_v4();
    let resp = client
        .get(format!("http://{addr}/runs/{other}/export/md"))
        .headers(auth_headers())
        .send()
        .await
        .expect("request sent");
    assert_eq!(resp.status(), 404);
}

// ---------------------------------------------------------------------------
// Binary-dependent tests. Only run when tectonic + pandoc are on PATH.
// ---------------------------------------------------------------------------

fn have_binary(name: &str) -> bool {
    std::process::Command::new(name)
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[tokio::test]
#[ignore = "requires pandoc on PATH; run with --ignored"]
async fn export_tex_renders_when_pandoc_present() {
    if !have_binary("pandoc") {
        eprintln!("skipping: pandoc not on PATH");
        return;
    }
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = timeout(
        Duration::from_secs(60),
        client
            .get(format!("http://{addr}/runs/{run_id}/export/tex"))
            .headers(auth_headers())
            .send(),
    )
    .await
    .expect("no timeout")
    .expect("request sent");

    assert_eq!(resp.status(), 200);
    let body = resp.text().await.expect("body");
    assert!(body.contains("\\documentclass"));
    assert!(body.contains("\\begin{document}"));
}

#[tokio::test]
#[ignore = "requires pandoc on PATH; run with --ignored"]
async fn export_docx_is_a_zip_when_pandoc_present() {
    if !have_binary("pandoc") {
        eprintln!("skipping: pandoc not on PATH");
        return;
    }
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = timeout(
        Duration::from_secs(60),
        client
            .get(format!("http://{addr}/runs/{run_id}/export/docx"))
            .headers(auth_headers())
            .send(),
    )
    .await
    .expect("no timeout")
    .expect("request sent");

    assert_eq!(resp.status(), 200);
    let bytes = resp.bytes().await.expect("body");
    // DOCX is a ZIP → starts with `PK\x03\x04`.
    assert!(bytes.len() > 0);
    assert_eq!(&bytes[..4], b"PK\x03\x04", "docx must be zip-framed");
}

#[tokio::test]
#[ignore = "requires tectonic + pandoc on PATH; run with --ignored"]
async fn export_pdf_has_pdf_magic_when_toolchain_present() {
    if !have_binary("pandoc") || !have_binary("tectonic") {
        eprintln!("skipping: pandoc/tectonic not on PATH");
        return;
    }
    let (addr, _tmp, _prov, run_id) = boot_server_with_paper(true).await;
    let client = reqwest::Client::new();

    let resp = timeout(
        Duration::from_secs(240),
        client
            .get(format!("http://{addr}/runs/{run_id}/export/pdf"))
            .headers(auth_headers())
            .send(),
    )
    .await
    .expect("no timeout")
    .expect("request sent");

    assert_eq!(resp.status(), 200);
    let bytes = resp.bytes().await.expect("body");
    assert!(bytes.len() > 0);
    assert_eq!(&bytes[..4], b"%PDF", "pdf must start with %PDF");
}
