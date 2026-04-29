//! Canonical request / response shapes that are adapter-independent.
//!
//! Every provider adapter accepts a `CanonicalRequest` and emits either a
//! `CanonicalResponse` (non-stream) or a stream of `CanonicalChunk`. The
//! route handler is then free to serialize back to whatever the OpenAI wire
//! format is without knowing which vendor fulfilled the call.
//!
//! These shapes intentionally mirror OpenAI's chat/completions schema since
//! every live adapter is OpenAI-compatible.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CanonicalRequest {
    pub model: String,
    pub messages: Vec<ChatMessage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    #[serde(default)]
    pub stream: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_format: Option<Value>,
    /// Canonical reasoning-effort hint. Accepted values:
    /// `"off" | "low" | "medium" | "high"`.
    ///
    /// Adapters translate per provider:
    /// - OpenAI-compat: emit both `reasoning_effort` and `reasoning.effort`
    ///   at the top of the body.
    /// - Anthropic: emit `thinking: {type: "enabled", budget_tokens: N}`
    ///   where `low=1024`, `medium=4096`, `high=16384` and bump
    ///   `max_tokens >= budget + 1024`.
    ///
    /// `"off"` and `None` both suppress emission entirely.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Usage {
    #[serde(default)]
    pub prompt_tokens: u32,
    #[serde(default)]
    pub completion_tokens: u32,
    #[serde(default)]
    pub total_tokens: u32,
}

/// Streaming chunk as emitted by upstream SSE. `delta_text` is the
/// concatenated text delta (may be empty for role-only chunks). `raw` is the
/// upstream JSON object, which the route handler re-emits verbatim so clients
/// see the exact OpenAI-shape payload regardless of which provider served it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CanonicalChunk {
    /// Best-effort extracted text delta. Empty string if none in this chunk.
    pub delta_text: String,
    /// Usage, only populated on the terminal chunk that carries it (if any).
    pub usage: Option<Usage>,
    /// The raw upstream chunk JSON, re-emitted to the client as-is.
    pub raw: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CanonicalResponse {
    /// The full upstream response JSON, passed back to the client unchanged.
    pub raw: Value,
    pub usage: Usage,
    pub model: String,
}

impl CanonicalResponse {
    pub fn from_openai_json(v: Value) -> Self {
        let usage = v
            .get("usage")
            .cloned()
            .and_then(|u| serde_json::from_value::<Usage>(u).ok())
            .unwrap_or_default();
        let model = v
            .get("model")
            .and_then(|m| m.as_str())
            .unwrap_or_default()
            .to_string();
        Self {
            raw: v,
            usage,
            model,
        }
    }
}
