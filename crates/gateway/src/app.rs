use axum::routing::{get, post};
use axum::Router;
use tower_http::services::{ServeDir, ServeFile};
use tower_http::trace::TraceLayer;

use crate::auth::require_dev_token;
use crate::routes::{export, figures, health, llm, runs, search, stats, ws_run};
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
        .route("/runs/:run_id/paper", get(figures::serve_paper))
        .route(
            "/runs/:run_id/export/:format",
            get(export::export_paper),
        )
        .route("/ws/runs/:run_id", get(ws_run::ws_handler))
        .route("/llm/chat/completions", post(llm::chat_completions))
        .route("/stats/summary", get(stats::stats_summary))
        .route("/stats/providers", get(stats::stats_providers))
        .route("/providers", get(stats::list_providers))
        .route("/search/capabilities", get(search::capabilities))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            require_dev_token,
        ));

    // Public routes: /health only.
    let public = Router::new().route("/health", get(health::health));

    let mut router = public.merge(authed).with_state(state.clone());

    // If STATIC_DIR points at a usable Vue build, mount it as a fallback so
    // unmatched paths serve the SPA. Lets a single binary host the UI without
    // a separate nginx/caddy. SPA fallback to index.html is required for
    // client-side routing.
    if let Some(dir) = state.config.static_dir.as_ref() {
        if dir.is_dir() {
            let index = dir.join("index.html");
            let serve = ServeDir::new(dir).fallback(ServeFile::new(index));
            router = router.fallback_service(serve);
            tracing::info!(path = %dir.display(), "static UI mounted at /");
        } else {
            tracing::warn!(
                path = %dir.display(),
                "STATIC_DIR set but directory missing — UI will not be served"
            );
        }
    }

    router.layer(TraceLayer::new_for_http())
}
