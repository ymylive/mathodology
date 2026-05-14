//! OpenAI-compatible adapter. Handles OpenAI, DeepSeek, Moonshot, vLLM, and
//! local Ollama `/v1`. Single implementation because all of these speak the
//! same JSON chat/completions shape and SSE event format.

use std::sync::Arc;

use eventsource_stream::Eventsource;
use futures::stream::{self, BoxStream, StreamExt};
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE};
use reqwest::Client;
use serde_json::{json, Value};

use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, CanonicalResponse, Usage};
use crate::llm::providers::{ProviderAdapter, ProviderError};

/// Live adapter for OpenAI-compatible HTTP endpoints.
pub struct OpenAICompatAdapter {
    name: String,
    base_url: String,
    /// Authorization bearer. Empty string = omit the Authorization header
    /// entirely (Ollama-local case).
    api_key: String,
    models: Vec<String>,
    http: Client,
}

impl OpenAICompatAdapter {
    pub fn new(
        name: String,
        base_url: String,
        api_key: String,
        models: Vec<String>,
        http: Client,
    ) -> Self {
        // Normalize base URL (strip trailing slash).
        let base_url = base_url.trim_end_matches('/').to_string();
        Self {
            name,
            base_url,
            api_key,
            models,
            http,
        }
    }

    fn build_body(&self, req: &CanonicalRequest, stream: bool) -> Value {
        // Translate canonical messages to JSON. When a message asks to be a
        // prompt-cache breakpoint we attach `cache_control: { type:
        // "ephemeral" }` at the message level — Anthropic-backed OpenAI-compat
        // proxies (e.g. cornna's cdnapi.cornna.xyz) forward it to the
        // upstream Claude `/v1/messages` call; vanilla OpenAI providers
        // ignore the extra field silently, so this is safe to emit
        // unconditionally for breakpoint messages.
        let messages: Vec<Value> = req
            .messages
            .iter()
            .map(|m| {
                let mut obj = serde_json::Map::new();
                obj.insert("role".into(), json!(m.role));
                obj.insert("content".into(), json!(m.content));
                if let Some(ref n) = m.name {
                    obj.insert("name".into(), json!(n));
                }
                if m.cache_breakpoint {
                    obj.insert(
                        "cache_control".into(),
                        json!({ "type": "ephemeral" }),
                    );
                }
                Value::Object(obj)
            })
            .collect();

        let mut body = json!({
            "model": req.model,
            "messages": messages,
            "stream": stream,
        });
        if let Some(t) = req.temperature {
            body["temperature"] = json!(t);
        }
        if let Some(m) = req.max_tokens {
            body["max_tokens"] = json!(m);
        }
        if let Some(ref rf) = req.response_format {
            body["response_format"] = rf.clone();
        }
        // OpenAI spec: streaming usage is opt-in. Must-have for cost_ledger.
        // Some proxies (DeepSeek / Moonshot) auto-emit usage on their own,
        // but strict OpenAI-compliant proxies (e.g. the cdnapi.cornna.xyz
        // gpt-5.4 route) only include it when this flag is set.
        if stream {
            body["stream_options"] = json!({"include_usage": true});
        }
        // Reasoning-effort translation. Older OpenAI-compat providers
        // (DeepSeek-Reasoner, Moonshot/Kimi, etc.) read `reasoning_effort` at
        // the top level. Newer OpenAI variants use `reasoning: {effort: ...}`.
        // Emit both; providers that don't know a field silently ignore it.
        // `"off"` is an explicit suppression signal — emit neither field.
        if let Some(level) = req.reasoning_effort.as_deref() {
            if matches!(level, "low" | "medium" | "high") {
                body["reasoning_effort"] = json!(level);
                body["reasoning"] = json!({ "effort": level });
            }
        }
        body
    }

    fn build_request(&self, body: Value) -> reqwest::RequestBuilder {
        let url = format!("{}/chat/completions", self.base_url);
        let mut rb = self.http.post(url).header(CONTENT_TYPE, "application/json");
        if !self.api_key.is_empty() {
            rb = rb.header(AUTHORIZATION, format!("Bearer {}", self.api_key));
        }
        rb.json(&body)
    }
}

#[async_trait::async_trait]
impl ProviderAdapter for OpenAICompatAdapter {
    fn name(&self) -> &str {
        &self.name
    }

    fn supports(&self, model: &str) -> bool {
        self.models.iter().any(|m| m == model)
    }

    async fn complete(&self, req: CanonicalRequest) -> Result<CanonicalResponse, ProviderError> {
        let body = self.build_body(&req, false);
        let resp = self.build_request(body).send().await?;
        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            return Err(ProviderError::HttpStatus(status, text));
        }
        let v: Value = resp
            .json()
            .await
            .map_err(|e| ProviderError::Parse(e.to_string()))?;
        Ok(CanonicalResponse::from_openai_json(v))
    }

    async fn stream(
        &self,
        req: CanonicalRequest,
    ) -> Result<BoxStream<'static, Result<CanonicalChunk, ProviderError>>, ProviderError> {
        let body = self.build_body(&req, true);
        let resp = self
            .build_request(body)
            .header("Accept", "text/event-stream")
            .send()
            .await?;
        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            return Err(ProviderError::HttpStatus(status, text));
        }

        // Byte stream -> eventsource-stream -> Event -> CanonicalChunk.
        let byte_stream = resp.bytes_stream().map(|r| {
            r.map_err(|e| -> std::io::Error {
                std::io::Error::new(std::io::ErrorKind::Other, e.to_string())
            })
        });
        let events = byte_stream.eventsource();

        let adapter_name: Arc<str> = Arc::from(self.name.clone().into_boxed_str());

        // Terminate cleanly on `[DONE]` and translate errors.
        let out = stream::unfold(
            (events, false, adapter_name),
            |(mut events, done, name)| async move {
                if done {
                    return None;
                }
                match events.next().await {
                    None => None,
                    Some(Err(e)) => Some((
                        Err(ProviderError::Network(format!("sse error: {e}"))),
                        (events, true, name),
                    )),
                    Some(Ok(ev)) => {
                        let data = ev.data;
                        if data.trim() == "[DONE]" {
                            return None;
                        }
                        match parse_openai_chunk(&data) {
                            Ok(chunk) => Some((Ok(chunk), (events, false, name))),
                            Err(e) => Some((Err(e), (events, true, name))),
                        }
                    }
                }
            },
        );

        Ok(Box::pin(out))
    }
}

/// Parse a single `data:` SSE frame into a canonical chunk. Extracts the text
/// delta from `choices[0].delta.content` and `usage` if present.
fn parse_openai_chunk(data: &str) -> Result<CanonicalChunk, ProviderError> {
    let v: Value =
        serde_json::from_str(data).map_err(|e| ProviderError::Parse(format!("chunk json: {e}")))?;

    let delta_text = v
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c0| c0.get("delta"))
        .and_then(|d| d.get("content"))
        .and_then(|c| c.as_str())
        .unwrap_or("")
        .to_string();

    let usage = v
        .get("usage")
        .cloned()
        .and_then(|u| serde_json::from_value::<Usage>(u).ok());

    Ok(CanonicalChunk {
        delta_text,
        usage,
        raw: v,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::canonical::ChatMessage;

    fn mk_req() -> CanonicalRequest {
        CanonicalRequest {
            model: "gpt-5".into(),
            messages: vec![ChatMessage {
                role: "user".into(),
                content: "hi".into(),
                name: None,
                cache_breakpoint: false,
            }],
            temperature: Some(0.2),
            max_tokens: None,
            stream: false,
            response_format: None,
            reasoning_effort: None,
        }
    }

    fn mk_adapter() -> OpenAICompatAdapter {
        OpenAICompatAdapter::new(
            "mock".into(),
            "https://example.invalid".into(),
            "sk".into(),
            vec!["gpt-5".into()],
            Client::new(),
        )
    }

    #[test]
    fn build_body_emits_both_reasoning_fields_when_high() {
        let adapter = mk_adapter();
        let mut req = mk_req();
        req.reasoning_effort = Some("high".into());
        let body = adapter.build_body(&req, false);
        assert_eq!(body["reasoning_effort"], "high");
        assert_eq!(body["reasoning"]["effort"], "high");
    }

    #[test]
    fn build_body_emits_both_reasoning_fields_when_low() {
        let adapter = mk_adapter();
        let mut req = mk_req();
        req.reasoning_effort = Some("low".into());
        let body = adapter.build_body(&req, true);
        assert_eq!(body["reasoning_effort"], "low");
        assert_eq!(body["reasoning"]["effort"], "low");
    }

    #[test]
    fn build_body_suppresses_on_off() {
        let adapter = mk_adapter();
        let mut req = mk_req();
        req.reasoning_effort = Some("off".into());
        let body = adapter.build_body(&req, false);
        assert!(body.get("reasoning_effort").is_none());
        assert!(body.get("reasoning").is_none());
    }

    #[test]
    fn build_body_suppresses_on_none() {
        let adapter = mk_adapter();
        let req = mk_req();
        let body = adapter.build_body(&req, false);
        assert!(body.get("reasoning_effort").is_none());
        assert!(body.get("reasoning").is_none());
    }

    /// `cache_breakpoint = true` on a canonical message must surface as
    /// `cache_control: { type: "ephemeral" }` at the message level in the
    /// outgoing body. Anthropic-backed OpenAI-compat proxies (cornna's
    /// cdnapi.cornna.xyz pointing at Claude) honour this; vanilla OpenAI
    /// ignores the extra field, which is the design goal — single canonical
    /// shape that's safe to emit unconditionally for opt-in messages.
    #[test]
    fn build_body_forwards_cache_breakpoint_as_cache_control() {
        let adapter = mk_adapter();
        let req = CanonicalRequest {
            model: "gpt-5".into(),
            messages: vec![
                ChatMessage {
                    role: "system".into(),
                    content: "big stable system prompt".into(),
                    name: None,
                    cache_breakpoint: true,
                },
                ChatMessage {
                    role: "user".into(),
                    content: "go".into(),
                    name: None,
                    cache_breakpoint: false,
                },
            ],
            temperature: None,
            max_tokens: None,
            stream: false,
            response_format: None,
            reasoning_effort: None,
        };
        let body = adapter.build_body(&req, false);
        let msgs = body["messages"].as_array().expect("messages array");
        assert_eq!(msgs.len(), 2);

        // System message: tagged.
        assert_eq!(msgs[0]["role"], "system");
        assert_eq!(msgs[0]["content"], "big stable system prompt");
        assert_eq!(
            msgs[0]["cache_control"]["type"], "ephemeral",
            "system breakpoint must surface cache_control"
        );

        // User message: untouched.
        assert_eq!(msgs[1]["role"], "user");
        assert_eq!(msgs[1]["content"], "go");
        assert!(
            msgs[1].get("cache_control").is_none(),
            "non-breakpoint messages must NOT carry cache_control"
        );
    }

    /// Without any breakpoints, the message JSON shape is unchanged: bare
    /// role + content keys, no extra `cache_control` noise on the wire.
    #[test]
    fn build_body_omits_cache_control_without_breakpoint() {
        let adapter = mk_adapter();
        let req = mk_req();
        let body = adapter.build_body(&req, false);
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 1);
        assert!(msgs[0].get("cache_control").is_none());
    }
}
