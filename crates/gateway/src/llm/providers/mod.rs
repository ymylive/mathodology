//! Provider adapter trait and shared error type.

use futures::stream::BoxStream;
use reqwest::StatusCode;
use thiserror::Error;

use crate::error::AppError;
use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, CanonicalResponse};

pub mod anthropic;
pub mod openai_compat;

/// Errors a provider adapter may surface. The router inspects these to decide
/// whether fallback is appropriate (see `Router::stream_with_fallback`).
#[derive(Debug, Error)]
pub enum ProviderError {
    #[error("bad provider config: {0}")]
    BadConfig(String),

    #[error("network error: {0}")]
    Network(String),

    #[error("http status {0}: {1}")]
    HttpStatus(StatusCode, String),

    #[error("parse error: {0}")]
    Parse(String),

    #[error("timed out")]
    Timeout,
}

impl ProviderError {
    /// Router consults this to decide whether to attempt the next provider in
    /// the fallback chain. 429 / 5xx / network / timeout are retryable; other
    /// 4xx (client error) is terminal.
    pub fn is_retryable(&self) -> bool {
        match self {
            ProviderError::Network(_) | ProviderError::Timeout => true,
            ProviderError::HttpStatus(s, _) => {
                s.as_u16() == 429 || s.as_u16() >= 500
            }
            ProviderError::BadConfig(_) | ProviderError::Parse(_) => false,
            // HttpStatus 4xx (non-429) and similar treated as terminal above.
        }
    }
}

impl From<reqwest::Error> for ProviderError {
    fn from(err: reqwest::Error) -> Self {
        if err.is_timeout() {
            ProviderError::Timeout
        } else {
            ProviderError::Network(err.to_string())
        }
    }
}

impl From<ProviderError> for AppError {
    fn from(err: ProviderError) -> Self {
        match &err {
            ProviderError::BadConfig(_) => AppError::Internal(err.to_string()),
            ProviderError::Network(_) => AppError::Internal(err.to_string()),
            ProviderError::Timeout => AppError::Internal(err.to_string()),
            ProviderError::HttpStatus(s, _) if s.is_client_error() => {
                AppError::BadRequest(err.to_string())
            }
            ProviderError::HttpStatus(_, _) => AppError::Internal(err.to_string()),
            ProviderError::Parse(_) => AppError::Internal(err.to_string()),
        }
    }
}

/// Adapter that can route a canonical request to a concrete upstream
/// provider. Each live adapter holds its own HTTP client, base URL, and key.
#[async_trait::async_trait]
pub trait ProviderAdapter: Send + Sync {
    fn name(&self) -> &str;
    fn supports(&self, model: &str) -> bool;
    async fn complete(
        &self,
        req: CanonicalRequest,
    ) -> Result<CanonicalResponse, ProviderError>;
    async fn stream(
        &self,
        req: CanonicalRequest,
    ) -> Result<BoxStream<'static, Result<CanonicalChunk, ProviderError>>, ProviderError>;
}
