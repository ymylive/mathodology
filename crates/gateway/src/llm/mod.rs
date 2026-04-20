//! LLM gateway: canonical types, provider adapters, router, cache, cost.

use std::path::Path;
use std::sync::Arc;

use anyhow::Result;

pub mod cache;
pub mod canonical;
pub mod config;
pub mod cost;
pub mod providers;
pub mod router;
pub mod stream;

use crate::llm::config::{PriceTable, ProviderRegistry};
use crate::llm::router::Router;

/// State bundle plugged into `AppState`.
pub struct LlmContext {
    pub router: Router,
    pub prices: PriceTable,
}

impl LlmContext {
    pub fn bootstrap(providers_path: &Path) -> Result<Arc<Self>> {
        let registry = ProviderRegistry::load(providers_path)?;
        let prices = registry.prices.clone();
        let router = Router::new(
            registry.providers,
            registry.default_model,
            registry.fallback,
        );
        Ok(Arc::new(Self { router, prices }))
    }
}
