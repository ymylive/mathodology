use std::sync::Arc;

use crate::config::AppConfig;
use crate::llm::LlmContext;

#[derive(Clone)]
pub struct AppState {
    pub redis: redis::aio::ConnectionManager,
    pub pg: sqlx::PgPool,
    pub config: Arc<AppConfig>,
    pub llm: Arc<LlmContext>,
}
