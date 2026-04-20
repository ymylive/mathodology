//! Model → adapter resolver + fallback chain.
//!
//! Retry semantics (see also `ProviderError::is_retryable`):
//! - We try each model in the chain at most once.
//! - Fallback fires only when the current attempt returns a retryable error
//!   *before* streaming starts. Network/5xx/408/425/429 qualify; any other
//!   4xx or a config/parse error is terminal.
//! - Once a stream has yielded its first chunk, we do NOT retry — mid-stream
//!   network failures surface to the client as an `error` SSE event, which
//!   matches OpenAI's behavior and avoids double-billing or token re-ordering.

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
        for (idx, model) in chain.iter().enumerate() {
            let Some(adapter) = self.resolve(model) else {
                tracing::warn!(model = %model, "no adapter supports model; skipping");
                continue;
            };
            let mut attempt = req.clone();
            attempt.model = model.clone();
            match adapter.complete(attempt).await {
                Ok(resp) => return Ok((model.clone(), resp)),
                Err(err) if err.is_retryable() => {
                    log_fallback(&err, adapter.name(), model, chain.get(idx + 1));
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
    /// only applies BEFORE any chunk has been yielded; once a stream starts,
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
        for (idx, model) in chain.iter().enumerate() {
            let Some(adapter) = self.resolve(model) else {
                tracing::warn!(model = %model, "no adapter supports model; skipping");
                continue;
            };
            let mut attempt = req.clone();
            attempt.model = model.clone();
            match adapter.stream(attempt).await {
                Ok(s) => return Ok((model.clone(), s)),
                Err(err) if err.is_retryable() => {
                    log_fallback(&err, adapter.name(), model, chain.get(idx + 1));
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

/// Shared fallback-transition log line. Emitted whenever a retryable error
/// causes us to move on to the next model in the chain.
fn log_fallback(
    err: &ProviderError,
    provider: &str,
    attempted_model: &str,
    next_model: Option<&String>,
) {
    let status = match err {
        ProviderError::HttpStatus(s, _) => s.as_u16().to_string(),
        ProviderError::Timeout => "timeout".to_string(),
        ProviderError::Network(_) => "network".to_string(),
        _ => "unknown".to_string(),
    };
    let next = next_model.map(String::as_str).unwrap_or("<none>");
    tracing::warn!(
        attempted_provider = provider,
        attempted_model = attempted_model,
        status = %status,
        next_model = %next,
        error = %err,
        "router falling back to next model"
    );
}
