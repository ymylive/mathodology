use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::Context;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

mod app;
mod auth;
mod config;
mod dispatch;
mod error;
mod routes;
mod state;

use crate::config::AppConfig;
use crate::state::AppState;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load .env if present (best-effort; missing file is fine in prod).
    let _ = dotenvy::dotenv();

    init_tracing();

    let cfg = AppConfig::from_env().context("failed to load AppConfig from env")?;
    let cfg = Arc::new(cfg);

    // Redis connection manager (auto-reconnects).
    let client = redis::Client::open(cfg.redis_url.clone())
        .context("failed to parse REDIS_URL into a redis::Client")?;
    let redis = redis::aio::ConnectionManager::new(client)
        .await
        .context("failed to connect to Redis")?;

    let state = AppState {
        redis,
        config: cfg.clone(),
    };

    let addr: SocketAddr = format!("{}:{}", cfg.host, cfg.port)
        .parse()
        .context("failed to parse GATEWAY_HOST:GATEWAY_PORT into SocketAddr")?;

    let router = app::build_router(state);

    tracing::info!(%addr, "gateway listening");
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("failed to bind {}", addr))?;
    axum::serve(listener, router)
        .await
        .context("axum::serve terminated")?;

    Ok(())
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));

    // If RUST_LOG is explicitly set, emit JSON; otherwise pretty for human readability.
    if std::env::var("RUST_LOG").is_ok() {
        tracing_subscriber::registry()
            .with(filter)
            .with(fmt::layer().json())
            .init();
    } else {
        tracing_subscriber::registry()
            .with(filter)
            .with(fmt::layer().pretty())
            .init();
    }
}
