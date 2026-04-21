use axum::routing::{get, post};
use axum::Router;
use tower_http::trace::TraceLayer;

use crate::auth::require_dev_token;
use crate::routes::{figures, health, llm, runs, stats, ws_run};
use crate::state::AppState;

pub fn build_router(state: AppState) -> Router {
    // Authed routes: /runs, /runs/:id, /ws/runs/:run_id, /llm/chat/completions,
    // plus the static artifact endpoints used by the UI.
    let authed = Router::new()
        .route("/runs", post(runs::create_run).get(runs::list_runs))
        .route("/runs/:run_id", get(runs::get_run))
        .route(
            "/runs/:run_id/figures/*path",
            get(figures::serve_figure),
        )
        .route("/runs/:run_id/notebook", get(figures::serve_notebook))
        .route("/ws/runs/:run_id", get(ws_run::ws_handler))
        .route("/llm/chat/completions", post(llm::chat_completions))
        .route("/stats/summary", get(stats::stats_summary))
        .route("/stats/providers", get(stats::stats_providers))
        .route("/providers", get(stats::list_providers))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            require_dev_token,
        ));

    // Public routes: /health only.
    let public = Router::new().route("/health", get(health::health));

    public
        .merge(authed)
        .with_state(state)
        .layer(TraceLayer::new_for_http())
}
