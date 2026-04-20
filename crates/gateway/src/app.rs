use axum::routing::{get, post};
use axum::Router;
use tower_http::trace::TraceLayer;

use crate::auth::require_dev_token;
use crate::routes::{health, runs, ws_run};
use crate::state::AppState;

pub fn build_router(state: AppState) -> Router {
    // Authed routes: /runs, /runs/:id, /ws/runs/:run_id
    let authed = Router::new()
        .route("/runs", post(runs::create_run))
        .route("/runs/:run_id", get(runs::get_run_stub))
        .route("/ws/runs/:run_id", get(ws_run::ws_handler))
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
