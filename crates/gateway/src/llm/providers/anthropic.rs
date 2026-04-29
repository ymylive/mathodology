//! Anthropic `/v1/messages` adapter.
//!
//! Translates a [`CanonicalRequest`] (OpenAI chat/completions-shaped) into
//! Anthropic's Messages API schema and back. Streams Anthropic SSE
//! (`message_start` / `content_block_delta` / `message_delta` / `message_stop`)
//! into [`CanonicalChunk`]s that look identical to what `openai_compat`
//! emits, so the forwarder in `routes::llm` doesn't need to know which
//! vendor served the call.
//!
//! API contract reference: Messages API, `anthropic-version: 2023-06-01`
//! (the only stable version as of this writing — bump when Anthropic ships
//! a new stable version and re-verify the SSE event names).
//!
//! Intentionally out of scope for M8: tool use / function calling, image /
//! vision content blocks, prompt caching directives, extended thinking.

use std::sync::Arc;

use eventsource_stream::Eventsource;
use futures::stream::{self, BoxStream, StreamExt};
use reqwest::header::CONTENT_TYPE;
use reqwest::Client;
use serde_json::{json, Value};

use crate::llm::canonical::{CanonicalChunk, CanonicalRequest, CanonicalResponse, Usage};
use crate::llm::providers::{ProviderAdapter, ProviderError};

/// Anthropic API version pinned to the request header. Update this (and the
/// module doc comment) when Anthropic ships a new stable version and we
/// re-verify the SSE event shapes.
const ANTHROPIC_VERSION: &str = "2023-06-01";

/// Default `max_tokens` when the canonical request omits one. Anthropic
/// requires the field (unlike OpenAI). 4096 matches the `deepseek-chat`
/// default ceiling and is plenty for a single agent turn.
const DEFAULT_MAX_TOKENS: u32 = 4096;

/// Suffix appended to the system prompt when the canonical request asks for
/// JSON output via `response_format`. Anthropic's `/v1/messages` has no
/// dedicated knob for structured output, so we steer the model with text.
const JSON_RESPONSE_SUFFIX: &str = "\n\nRespond with a single valid JSON object only. \
    Do not include prose, markdown fences, or commentary before or after the JSON.";

/// Live adapter for `api.anthropic.com` (and any Anthropic-compatible proxy).
pub struct AnthropicAdapter {
    name: String,
    base_url: String,
    /// `x-api-key` header value. Empty string = omit (the registry will WARN
    /// at load time, but we still allow construction so tests with no key
    /// aren't blocked).
    api_key: String,
    models: Vec<String>,
    http: Client,
}

impl AnthropicAdapter {
    pub fn new(
        name: String,
        base_url: String,
        api_key: String,
        models: Vec<String>,
        http: Client,
    ) -> Self {
        // Strip trailing slash so `{base_url}/v1/messages` is well-formed
        // regardless of whether the configured URL ended with `/`.
        let base_url = base_url.trim_end_matches('/').to_string();
        Self {
            name,
            base_url,
            api_key,
            models,
            http,
        }
    }

    /// Build the Anthropic-shaped request body from a canonical request.
    ///
    /// Translation rules:
    /// - Any `role=system` messages are pulled out of `messages` and
    ///   concatenated into the top-level `system` field (joined by blank
    ///   lines). Anthropic rejects `role: system` inside `messages`.
    /// - The remaining `user`/`assistant` messages are passed through as-is.
    /// - `max_tokens` is required by Anthropic; we default to 4096.
    /// - `response_format: {"type": "json_object"}` has no native
    ///   counterpart on `/v1/messages`, so we append a firm instruction to
    ///   the system prompt telling the model to emit JSON only. Future
    ///   schemas (e.g. `json_schema`) would need their own handling.
    fn build_body(&self, req: &CanonicalRequest, stream: bool) -> Value {
        let mut system_parts: Vec<String> = Vec::new();
        let mut messages: Vec<Value> = Vec::with_capacity(req.messages.len());
        for m in &req.messages {
            if m.role == "system" {
                system_parts.push(m.content.clone());
            } else {
                messages.push(json!({
                    "role": m.role,
                    "content": m.content,
                }));
            }
        }

        let mut system = if system_parts.is_empty() {
            String::new()
        } else {
            system_parts.join("\n\n")
        };

        // Request JSON-only output. Anthropic has no first-class
        // `response_format` knob on /v1/messages; nudge via the system prompt.
        if let Some(rf) = &req.response_format {
            let kind = rf.get("type").and_then(|t| t.as_str()).unwrap_or("");
            if kind == "json_object" || kind == "json_schema" {
                if system.is_empty() {
                    system = JSON_RESPONSE_SUFFIX.trim_start().to_string();
                } else {
                    system.push_str(JSON_RESPONSE_SUFFIX);
                }
            }
        }

        // Extended-thinking budget: canonical reasoning_effort → token
        // budget. `off` and `None` emit nothing. `low/medium/high` map to
        // Anthropic's `thinking.budget_tokens`. When thinking is enabled
        // Anthropic requires `max_tokens > budget_tokens` and reasoning
        // tokens count toward the output quota, so bump max_tokens to
        // `budget + 1024` (a safe completion headroom above the budget).
        let thinking_budget: u32 = match req.reasoning_effort.as_deref() {
            Some("low") => 1024,
            Some("medium") => 4096,
            Some("high") => 16384,
            _ => 0,
        };

        let base_max_tokens = req.max_tokens.unwrap_or(DEFAULT_MAX_TOKENS);
        let max_tokens = if thinking_budget > 0 {
            base_max_tokens.max(thinking_budget + 1024)
        } else {
            base_max_tokens
        };

        let mut body = json!({
            "model": req.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "stream": stream,
        });
        if !system.is_empty() {
            body["system"] = json!(system);
        }
        if let Some(t) = req.temperature {
            body["temperature"] = json!(t);
        }
        if thinking_budget > 0 {
            body["thinking"] = json!({
                "type": "enabled",
                "budget_tokens": thinking_budget,
            });
        }
        body
    }

    fn build_request(&self, body: Value) -> reqwest::RequestBuilder {
        let url = format!("{}/v1/messages", self.base_url);
        let mut rb = self
            .http
            .post(url)
            .header(CONTENT_TYPE, "application/json")
            .header("anthropic-version", ANTHROPIC_VERSION);
        if !self.api_key.is_empty() {
            rb = rb.header("x-api-key", &self.api_key);
        }
        rb.json(&body)
    }
}

#[async_trait::async_trait]
impl ProviderAdapter for AnthropicAdapter {
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
        Ok(from_anthropic_response(v))
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

        // Byte stream -> eventsource-stream -> Event -> CanonicalChunk. Same
        // adapter pattern as openai_compat; Anthropic's SSE framing is
        // standards-compliant.
        let byte_stream = resp.bytes_stream().map(|r| {
            r.map_err(|e| -> std::io::Error {
                std::io::Error::new(std::io::ErrorKind::Other, e.to_string())
            })
        });
        let events = byte_stream.eventsource();

        let adapter_name: Arc<str> = Arc::from(self.name.clone().into_boxed_str());

        // Fold state across events: accumulate prompt_tokens from
        // message_start (authoritative) and final completion_tokens from
        // message_delta. Emit a synthetic Usage chunk once we have both, and
        // terminate on message_stop.
        let out = stream::unfold(
            (events, Accum::default(), false, adapter_name),
            |(mut events, mut acc, done, name)| async move {
                if done {
                    return None;
                }
                loop {
                    match events.next().await {
                        None => return None,
                        Some(Err(e)) => {
                            return Some((
                                Err(ProviderError::Network(format!("sse error: {e}"))),
                                (events, acc, true, name),
                            ));
                        }
                        Some(Ok(ev)) => {
                            match parse_anthropic_event(&ev.event, &ev.data, &mut acc) {
                                Ok(Some(chunk)) => {
                                    return Some((Ok(chunk), (events, acc, false, name)));
                                }
                                Ok(None) => {
                                    // Event was a no-op (ping, content_block_start,
                                    // content_block_stop, unknown type): keep reading.
                                    continue;
                                }
                                Err(e) => {
                                    return Some((Err(e), (events, acc, true, name)));
                                }
                            }
                        }
                    }
                }
            },
        );

        Ok(Box::pin(out))
    }
}

/// Running totals across an Anthropic SSE stream. `message_start` seeds
/// `prompt_tokens`; `message_delta` fills in `completion_tokens`; we emit
/// one synthetic usage-bearing chunk once the completion count is known.
#[derive(Default)]
struct Accum {
    prompt_tokens: u32,
    completion_tokens: u32,
    /// Whether we've already yielded the synthetic usage chunk.
    emitted_usage: bool,
}

/// Decode one SSE event. Returns `Ok(Some(chunk))` to forward, `Ok(None)` to
/// silently skip (pings, block boundary markers), or `Err` on parse failure.
///
/// Usage accounting strategy:
/// - `message_start.message.usage.input_tokens` is authoritative for prompt
///   tokens; we stash it on the accumulator but don't emit yet (clients
///   expect usage at the tail, matching OpenAI's shape).
/// - `message_delta.usage.output_tokens` is the final completion token
///   count; on receipt we emit one synthetic `CanonicalChunk` whose
///   `usage` field carries both numbers.
/// - `message_stop` closes the HTTP body; eventsource-stream yields `None`
///   next, unwinding the outer `unfold` cleanly.
fn parse_anthropic_event(
    event: &str,
    data: &str,
    acc: &mut Accum,
) -> Result<Option<CanonicalChunk>, ProviderError> {
    // Anthropic always sets the `event:` field. Fall back to parsing
    // `data.type` if some proxy stripped it.
    let v: Value = serde_json::from_str(data)
        .map_err(|e| ProviderError::Parse(format!("anthropic chunk json: {e}")))?;
    let evname = if event.is_empty() {
        v.get("type").and_then(|t| t.as_str()).unwrap_or("")
    } else {
        event
    };

    match evname {
        "message_start" => {
            if let Some(u) = v.pointer("/message/usage") {
                if let Some(t) = u.get("input_tokens").and_then(Value::as_u64) {
                    acc.prompt_tokens = t as u32;
                }
                // Anthropic's message_start carries output_tokens: 0. Ignore.
            }
            Ok(None)
        }
        "content_block_delta" => {
            let delta = v.get("delta");
            let dtype = delta
                .and_then(|d| d.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("");
            // We only map text deltas. `input_json_delta` (tool use) and
            // thinking deltas are intentionally skipped — tool use is out of
            // scope for M8.
            if dtype == "text_delta" {
                let text = delta
                    .and_then(|d| d.get("text"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                Ok(Some(CanonicalChunk {
                    delta_text: text,
                    usage: None,
                    raw: v,
                }))
            } else {
                Ok(None)
            }
        }
        "message_delta" => {
            if let Some(t) = v.pointer("/usage/output_tokens").and_then(Value::as_u64) {
                acc.completion_tokens = t as u32;
            }
            // Emit the synthetic usage chunk on the first message_delta we
            // see that has output_tokens. The forwarder will pick this up
            // and record cost when the stream ends.
            if !acc.emitted_usage && acc.completion_tokens > 0 {
                acc.emitted_usage = true;
                let usage = Usage {
                    prompt_tokens: acc.prompt_tokens,
                    completion_tokens: acc.completion_tokens,
                    total_tokens: acc.prompt_tokens + acc.completion_tokens,
                };
                return Ok(Some(CanonicalChunk {
                    delta_text: String::new(),
                    usage: Some(usage),
                    raw: v,
                }));
            }
            Ok(None)
        }
        "message_stop" => {
            // End of stream. If we somehow never saw a message_delta with
            // output_tokens, emit a best-effort usage chunk now so cost
            // accounting still fires. Otherwise just skip — the SSE body
            // closes right after `message_stop`, the eventsource iterator
            // yields `None` next, and the outer `unfold` unwinds cleanly.
            if !acc.emitted_usage && acc.prompt_tokens + acc.completion_tokens > 0 {
                acc.emitted_usage = true;
                let usage = Usage {
                    prompt_tokens: acc.prompt_tokens,
                    completion_tokens: acc.completion_tokens,
                    total_tokens: acc.prompt_tokens + acc.completion_tokens,
                };
                return Ok(Some(CanonicalChunk {
                    delta_text: String::new(),
                    usage: Some(usage),
                    raw: v,
                }));
            }
            Ok(None)
        }
        // Ignored: ping (keep-alive), content_block_start, content_block_stop,
        // and any future event types. Safe to ignore because they carry no
        // text or usage data we track.
        _ => Ok(None),
    }
}

/// Collapse a non-streaming Anthropic response into the canonical shape.
///
/// Concatenates every `content[].text` block (anything non-text, e.g. tool
/// use, is skipped — M8 doesn't surface tools). Usage maps straightforwardly:
/// `input_tokens → prompt_tokens`, `output_tokens → completion_tokens`.
fn from_anthropic_response(v: Value) -> CanonicalResponse {
    let id = v
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let model = v
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let stop_reason = v
        .get("stop_reason")
        .and_then(Value::as_str)
        .unwrap_or("end_turn")
        .to_string();

    let mut text = String::new();
    if let Some(arr) = v.get("content").and_then(Value::as_array) {
        for block in arr {
            if block.get("type").and_then(Value::as_str) == Some("text") {
                if let Some(t) = block.get("text").and_then(Value::as_str) {
                    text.push_str(t);
                }
            }
        }
    }

    let (prompt_tokens, completion_tokens) = v
        .get("usage")
        .map(|u| {
            let i = u.get("input_tokens").and_then(Value::as_u64).unwrap_or(0) as u32;
            let o = u.get("output_tokens").and_then(Value::as_u64).unwrap_or(0) as u32;
            (i, o)
        })
        .unwrap_or((0, 0));

    let usage = Usage {
        prompt_tokens,
        completion_tokens,
        total_tokens: prompt_tokens + completion_tokens,
    };

    // Re-shape into the OpenAI chat/completions response envelope so the
    // route handler (and any passthrough caller) can treat Anthropic and
    // OpenAI responses identically on the wire.
    let raw = json!({
        "id": id,
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text,
            },
            "finish_reason": anthropic_stop_to_openai(&stop_reason),
        }],
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        },
    });

    CanonicalResponse { raw, usage, model }
}

/// Map Anthropic `stop_reason` values into the closest OpenAI
/// `finish_reason` so downstream callers that key off the standard values
/// don't have to special-case.
fn anthropic_stop_to_openai(r: &str) -> &'static str {
    match r {
        "end_turn" | "stop_sequence" => "stop",
        "max_tokens" => "length",
        "tool_use" => "tool_calls",
        _ => "stop",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::canonical::ChatMessage;

    fn mk_req_with(messages: Vec<(&str, &str)>) -> CanonicalRequest {
        CanonicalRequest {
            model: "claude-sonnet-4-6".into(),
            messages: messages
                .into_iter()
                .map(|(r, c)| ChatMessage {
                    role: r.into(),
                    content: c.into(),
                    name: None,
                })
                .collect(),
            temperature: Some(0.2),
            max_tokens: None,
            stream: false,
            response_format: None,
            reasoning_effort: None,
        }
    }

    #[test]
    fn build_body_lifts_system_and_defaults_max_tokens() {
        let adapter = AnthropicAdapter::new(
            "anth".into(),
            "https://api.anthropic.com".into(),
            "sk".into(),
            vec!["claude-sonnet-4-6".into()],
            Client::new(),
        );
        let req = mk_req_with(vec![
            ("system", "be brief"),
            ("user", "hi"),
            ("assistant", "hello"),
            ("user", "go"),
        ]);
        let body = adapter.build_body(&req, true);
        assert_eq!(body["system"], "be brief");
        assert_eq!(body["max_tokens"], DEFAULT_MAX_TOKENS);
        assert_eq!(body["stream"], true);
        assert_eq!(body["model"], "claude-sonnet-4-6");
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 3);
        assert_eq!(msgs[0]["role"], "user");
        assert_eq!(msgs[1]["role"], "assistant");
    }

    #[test]
    fn build_body_concatenates_multiple_system_messages() {
        let adapter = AnthropicAdapter::new(
            "anth".into(),
            "https://x".into(),
            "k".into(),
            vec!["claude-sonnet-4-6".into()],
            Client::new(),
        );
        let req = mk_req_with(vec![("system", "one"), ("system", "two"), ("user", "go")]);
        let body = adapter.build_body(&req, false);
        assert_eq!(body["system"], "one\n\ntwo");
    }

    #[test]
    fn build_body_emits_thinking_for_high_and_bumps_max_tokens() {
        let adapter = AnthropicAdapter::new(
            "anth".into(),
            "https://x".into(),
            "k".into(),
            vec!["claude-sonnet-4-6".into()],
            Client::new(),
        );
        let mut req = mk_req_with(vec![("user", "go")]);
        req.reasoning_effort = Some("high".into());
        let body = adapter.build_body(&req, false);
        assert_eq!(body["thinking"]["type"], "enabled");
        assert_eq!(body["thinking"]["budget_tokens"], 16384);
        // max_tokens must be at least budget + 1024 (thinking headroom).
        let mt = body["max_tokens"].as_u64().unwrap();
        assert!(
            mt >= 16384 + 1024,
            "max_tokens {mt} must be at least budget+1024",
        );
    }

    #[test]
    fn build_body_emits_thinking_for_low_and_medium() {
        let adapter = AnthropicAdapter::new(
            "anth".into(),
            "https://x".into(),
            "k".into(),
            vec!["claude-sonnet-4-6".into()],
            Client::new(),
        );
        for (level, expected) in [("low", 1024_u64), ("medium", 4096_u64)] {
            let mut req = mk_req_with(vec![("user", "go")]);
            req.reasoning_effort = Some(level.into());
            let body = adapter.build_body(&req, false);
            assert_eq!(body["thinking"]["budget_tokens"], expected, "level {level}");
            let mt = body["max_tokens"].as_u64().unwrap();
            assert!(
                mt >= expected + 1024,
                "max_tokens {mt} < budget+1024 for {level}"
            );
        }
    }

    #[test]
    fn build_body_no_thinking_on_off_or_none() {
        let adapter = AnthropicAdapter::new(
            "anth".into(),
            "https://x".into(),
            "k".into(),
            vec!["claude-sonnet-4-6".into()],
            Client::new(),
        );
        // off
        let mut req = mk_req_with(vec![("user", "go")]);
        req.reasoning_effort = Some("off".into());
        let body = adapter.build_body(&req, false);
        assert!(body.get("thinking").is_none(), "off must suppress thinking");
        assert_eq!(body["max_tokens"], DEFAULT_MAX_TOKENS);

        // None (unset)
        let req = mk_req_with(vec![("user", "go")]);
        let body = adapter.build_body(&req, false);
        assert!(
            body.get("thinking").is_none(),
            "unset reasoning_effort must suppress thinking"
        );
        assert_eq!(body["max_tokens"], DEFAULT_MAX_TOKENS);
    }

    #[test]
    fn build_body_appends_json_suffix_on_response_format() {
        let adapter = AnthropicAdapter::new(
            "anth".into(),
            "https://x".into(),
            "k".into(),
            vec!["claude-sonnet-4-6".into()],
            Client::new(),
        );
        let mut req = mk_req_with(vec![("system", "be brief"), ("user", "go")]);
        req.response_format = Some(json!({"type": "json_object"}));
        let body = adapter.build_body(&req, false);
        let sys = body["system"].as_str().unwrap();
        assert!(sys.starts_with("be brief"));
        assert!(sys.contains("valid JSON object"));
    }

    #[test]
    fn from_anthropic_response_flattens_text_and_usage() {
        let v = json!({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        });
        let r = from_anthropic_response(v);
        assert_eq!(r.model, "claude-sonnet-4-6");
        assert_eq!(r.usage.prompt_tokens, 7);
        assert_eq!(r.usage.completion_tokens, 3);
        assert_eq!(r.usage.total_tokens, 10);
        assert_eq!(
            r.raw["choices"][0]["message"]["content"].as_str().unwrap(),
            "Hello world"
        );
        assert_eq!(r.raw["choices"][0]["finish_reason"], "stop");
    }

    #[test]
    fn parse_text_delta_and_usage() {
        let mut acc = Accum::default();
        let chunk = parse_anthropic_event(
            "message_start",
            r#"{"type":"message_start","message":{"id":"m","role":"assistant","model":"claude-sonnet-4-6","content":[],"usage":{"input_tokens":10,"output_tokens":0}}}"#,
            &mut acc,
        )
        .unwrap();
        assert!(chunk.is_none());
        assert_eq!(acc.prompt_tokens, 10);

        let chunk = parse_anthropic_event(
            "content_block_delta",
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}"#,
            &mut acc,
        )
        .unwrap()
        .expect("text delta should yield a chunk");
        assert_eq!(chunk.delta_text, "Hello");
        assert!(chunk.usage.is_none());

        let chunk = parse_anthropic_event(
            "message_delta",
            r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":25}}"#,
            &mut acc,
        )
        .unwrap()
        .expect("message_delta with usage should yield a chunk");
        assert_eq!(chunk.delta_text, "");
        let u = chunk.usage.unwrap();
        assert_eq!(u.prompt_tokens, 10);
        assert_eq!(u.completion_tokens, 25);
        assert_eq!(u.total_tokens, 35);

        let chunk =
            parse_anthropic_event("message_stop", r#"{"type":"message_stop"}"#, &mut acc).unwrap();
        assert!(chunk.is_none());
    }

    #[test]
    fn parse_ignores_ping_and_block_boundaries() {
        let mut acc = Accum::default();
        assert!(
            parse_anthropic_event("ping", r#"{"type":"ping"}"#, &mut acc,)
                .unwrap()
                .is_none()
        );
        assert!(parse_anthropic_event(
            "content_block_start",
            r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
            &mut acc,
        )
        .unwrap()
        .is_none());
        assert!(parse_anthropic_event(
            "content_block_stop",
            r#"{"type":"content_block_stop","index":0}"#,
            &mut acc,
        )
        .unwrap()
        .is_none());
    }
}
