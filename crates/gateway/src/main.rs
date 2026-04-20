use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::Context;
use sqlx::postgres::PgPoolOptions;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

use gateway::app;
use gateway::config::AppConfig;
use gateway::llm::LlmContext;
use gateway::state::AppState;

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

    // Postgres pool + forward-only migrations.
    let pg = PgPoolOptions::new()
        .max_connections(8)
        .connect(&cfg.database_url)
        .await
        .context("failed to connect to Postgres")?;
    sqlx::migrate!("./migrations")
        .run(&pg)
        .await
        .context("failed to run sqlx migrations")?;
    tracing::info!("postgres connected and migrations applied");

    let llm = LlmContext::bootstrap(&cfg.providers_path)
        .context("failed to bootstrap LLM provider registry")?;
    tracing::info!(path = %cfg.providers_path.display(), "LLM provider registry loaded");

    let state = AppState {
        redis,
        pg,
        config: cfg.clone(),
        llm,
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
