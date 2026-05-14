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

use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, CanonicalResponse};
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

    /// First provider that BOTH `supports(model)` AND `has_credentials()`.
    /// A provider serving a model but with an empty auth key is skipped so
    /// the router moves on to the next fallback model instead of issuing
    /// an unauthenticated request that the upstream will reject with 401
    /// (which the worker then surfaces as a confusing "400 Bad Request"
    /// because client_error → AppError::BadRequest).
    pub fn resolve(&self, model: &str) -> Option<Arc<dyn ProviderAdapter>> {
        self.providers
            .iter()
            .find(|p| p.supports(model) && p.has_credentials())
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
        (
            String,
            BoxStream<'static, Result<CanonicalChunk, ProviderError>>,
        ),
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

#[cfg(test)]
mod tests {
    //! Regression coverage for the round-9 credentials gate.
    //!
    //! Before the fix the router happily resolved to a provider whose
    //! api_key was empty, issued the request, and propagated the
    //! upstream's confusing 401 back to the worker as a "400 Bad
    //! Request". With `has_credentials()` wired in, dead-key providers
    //! are skipped entirely so the request either lands on a viable
    //! provider or reports a clean "no adapter" error.
    use super::*;
    use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, CanonicalResponse};
    use crate::llm::providers::{ProviderAdapter, ProviderError};
    use async_trait::async_trait;
    use futures::stream::BoxStream;

    struct StubAdapter {
        name: String,
        models: Vec<String>,
        creds: bool,
    }

    #[async_trait]
    impl ProviderAdapter for StubAdapter {
        fn name(&self) -> &str {
            &self.name
        }
        fn supports(&self, m: &str) -> bool {
            self.models.iter().any(|s| s == m)
        }
        fn has_credentials(&self) -> bool {
            self.creds
        }
        async fn complete(&self, _: CanonicalRequest) -> Result<CanonicalResponse, ProviderError> {
            unreachable!("complete() not exercised in resolve()-only tests")
        }
        async fn stream(
            &self,
            _: CanonicalRequest,
        ) -> Result<BoxStream<'static, Result<CanonicalChunk, ProviderError>>, ProviderError>
        {
            unreachable!("stream() not exercised in resolve()-only tests")
        }
    }

    fn router_with(providers: Vec<Arc<dyn ProviderAdapter>>) -> Router {
        Router::new(providers, "gpt-5.5".to_string(), vec!["gpt-5.4".to_string()])
    }

    #[test]
    fn resolve_skips_dead_key_provider() {
        // Two providers both serve "deepseek-chat", the first has no
        // credentials. Without the gate the router would pick the first
        // and request would 401. With the gate it picks the second.
        let dead = Arc::new(StubAdapter {
            name: "dead".into(),
            models: vec!["deepseek-chat".into()],
            creds: false,
        });
        let alive = Arc::new(StubAdapter {
            name: "alive".into(),
            models: vec!["deepseek-chat".into()],
            creds: true,
        });
        let r = router_with(vec![dead, alive]);
        let chosen = r.resolve("deepseek-chat").expect("expected adapter");
        assert_eq!(chosen.name(), "alive");
    }

    #[test]
    fn resolve_returns_none_when_only_dead_key_provider_supports_model() {
        // If the ONLY provider supporting a model has empty credentials,
        // resolve must return None so stream_with_fallback moves on to
        // the next model in the chain (round-9 behaviour). Prior to the
        // fix it returned Some(dead), guaranteeing a 401.
        let dead = Arc::new(StubAdapter {
            name: "dead".into(),
            models: vec!["claude-opus".into()],
            creds: false,
        });
        let r = router_with(vec![dead]);
        assert!(r.resolve("claude-opus").is_none());
    }

    #[test]
    fn resolve_still_returns_alive_provider() {
        let alive = Arc::new(StubAdapter {
            name: "alive".into(),
            models: vec!["gpt-5.5".into()],
            creds: true,
        });
        let r = router_with(vec![alive]);
        assert!(r.resolve("gpt-5.5").is_some());
    }
}
