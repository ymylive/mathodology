//! Model → adapter resolver + fallback chain.

use std::sync::Arc;

use futures::stream::BoxStream;

use crate::llm::canonical::{
    CanonicalChunk, CanonicalRequest, CanonicalResponse,
};
use crate::llm::providers::{ProviderAdapter, ProviderError};

pub struct Router {
    providers: Vec<Arc<dyn ProviderAdapter>>,
    #[allow(dead_code)]
    default_model: String,
    fallback_models: Vec<String>,
}

impl Router {
    pub fn new(
        providers: Vec<Arc<dyn ProviderAdapter>>,
        default_model: String,
        fallback_models: Vec<String>,
    ) -> Self {
        Self {
            providers,
            default_model,
            fallback_models,
        }
    }

    /// First provider whose `supports(model)` returns true.
    pub fn resolve(&self, model: &str) -> Option<Arc<dyn ProviderAdapter>> {
        self.providers
            .iter()
            .find(|p| p.supports(model))
            .cloned()
    }

    #[allow(dead_code)]
    pub fn default_model(&self) -> &str {
        &self.default_model
    }

    /// Run `complete` against the primary model, falling back through
    /// `fallback_models` on retryable provider errors.
    ///
    /// Returns the final (model, response) pair so the caller can account for
    /// cost against the model that actually served the request.
    pub async fn complete_with_fallback(
        &self,
        req: CanonicalRequest,
    ) -> Result<(String, CanonicalResponse), ProviderError> {
        let primary = req.model.clone();
        let chain = self.build_chain(&primary);

        let mut last_err: Option<ProviderError> = None;
        for model in chain {
            let Some(adapter) = self.resolve(&model) else {
                tracing::warn!(model = %model, "no adapter supports model; skipping");
                continue;
            };
            let mut req = req.clone();
            req.model = model.clone();
            match adapter.complete(req).await {
                Ok(resp) => return Ok((model, resp)),
                Err(err) if err.is_retryable() => {
                    tracing::warn!(
                        provider = adapter.name(),
                        model = %model,
                        error = %err,
                        "provider retryable error; trying next fallback"
                    );
                    last_err = Some(err);
                    continue;
                }
                Err(err) => return Err(err),
            }
        }
        Err(last_err.unwrap_or_else(|| {
            ProviderError::BadConfig(format!("no adapter supports model {primary}"))
        }))
    }

    /// Streaming counterpart: tries primary, then fallback models. Fallback
    /// only applies BEFORE any byte has been yielded; once a stream starts,
    /// the caller owns teardown and retries are not attempted.
    pub async fn stream_with_fallback(
        &self,
        req: CanonicalRequest,
    ) -> Result<
        (String, BoxStream<'static, Result<CanonicalChunk, ProviderError>>),
        ProviderError,
    > {
        let primary = req.model.clone();
        let chain = self.build_chain(&primary);

        let mut last_err: Option<ProviderError> = None;
        for model in chain {
            let Some(adapter) = self.resolve(&model) else {
                tracing::warn!(model = %model, "no adapter supports model; skipping");
                continue;
            };
            let mut req = req.clone();
            req.model = model.clone();
            match adapter.stream(req).await {
                Ok(s) => return Ok((model, s)),
                Err(err) if err.is_retryable() => {
                    tracing::warn!(
                        provider = adapter.name(),
                        model = %model,
                        error = %err,
                        "provider retryable stream error; trying next fallback"
                    );
                    last_err = Some(err);
                    continue;
                }
                Err(err) => return Err(err),
            }
        }
        Err(last_err.unwrap_or_else(|| {
            ProviderError::BadConfig(format!("no adapter supports model {primary}"))
        }))
    }

    /// Build the ordered attempt chain: primary first, then up to
    /// `fallback.len()` distinct models from the fallback list, each skipped
    /// if it duplicates an earlier attempt.
    fn build_chain(&self, primary: &str) -> Vec<String> {
        let mut chain = Vec::with_capacity(1 + self.fallback_models.len());
        chain.push(primary.to_string());
        for fb in &self.fallback_models {
            if !chain.iter().any(|m| m == fb) {
                chain.push(fb.clone());
            }
        }
        chain
    }
}
