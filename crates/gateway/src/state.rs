use std::sync::Arc;

use crate::config::AppConfig;

#[derive(Clone)]
pub struct AppState {
    pub redis: redis::aio::ConnectionManager,
    pub config: Arc<AppConfig>,
}
