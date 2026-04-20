use std::path::PathBuf;
use std::sync::Arc;

use crate::config::AppConfig;
use crate::llm::LlmContext;

#[derive(Clone)]
pub struct AppState {
    pub redis: redis::aio::ConnectionManager,
    pub pg: sqlx::PgPool,
    pub config: Arc<AppConfig>,
    pub llm: Arc<LlmContext>,
    /// Canonicalized filesystem root for run artifacts. Handlers serving
    /// static figures / notebooks join this with `<run_id>/...` and then
    /// check that the canonical final path is a descendant of this prefix
    /// to defend against path traversal.
    pub runs_dir: Arc<PathBuf>,
}
