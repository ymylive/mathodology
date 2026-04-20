//! Stub. Actual Anthropic adapter lands in M8. Included so the registry can
//! acknowledge `kind=anthropic` entries in providers.toml without crashing;
//! the loader skips them with a WARN.

use futures::stream::BoxStream;

use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, CanonicalResponse};
use crate::llm::providers::{ProviderAdapter, ProviderError};

pub struct AnthropicAdapter;

impl AnthropicAdapter {
    /// M8 placeholder — constructing this panics to make misuse loud in dev.
    #[allow(dead_code, clippy::new_without_default)]
    pub fn new() -> Self {
        panic!("AnthropicAdapter is not implemented until M8");
    }
}

#[async_trait::async_trait]
impl ProviderAdapter for AnthropicAdapter {
    fn name(&self) -> &str {
        "anthropic"
    }
    fn supports(&self, _model: &str) -> bool {
        false
    }
    async fn complete(
        &self,
        _req: CanonicalRequest,
    ) -> Result<CanonicalResponse, ProviderError> {
        Err(ProviderError::BadConfig(
            "anthropic adapter unimplemented (M8)".into(),
        ))
    }
    async fn stream(
        &self,
        _req: CanonicalRequest,
    ) -> Result<BoxStream<'static, Result<CanonicalChunk, ProviderError>>, ProviderError>
    {
        Err(ProviderError::BadConfig(
            "anthropic adapter unimplemented (M8)".into(),
        ))
    }
}
