//! Redis-backed prompt cache, non-streaming only in M3.
//!
//! Key = `mm:cache:<sha256(model|messages|temperature|max_tokens|response_format)>`,
//! value = the raw provider JSON response, TTL = 1h.

use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::llm::canonical::{CanonicalRequest, CanonicalResponse};

pub const CACHE_TTL_SECS: u64 = 3600;

fn cache_redis_key(hex_digest: &str) -> String {
    format!("mm:cache:{hex_digest}")
}

/// Produce a stable content-addressed cache key for a request.
///
/// Stability guarantees:
/// - Key is invariant under JSON object-key reorderings (we serialize via
///   `serde_json::to_vec` on a canonical `Value`, where all objects we
///   construct are built field-by-field; inner `response_format` is
///   canonicalized by recursive sort).
/// - Streaming flag is NOT included: the same prompt, cached in non-stream
///   mode, is a valid answer whether the caller asked for stream or not.
///   (Spec says streaming never consults the cache anyway.)
pub fn cache_key(req: &CanonicalRequest) -> String {
    let messages: Vec<Value> = req
        .messages
        .iter()
        .map(|m| {
            let mut obj = serde_json::Map::new();
            obj.insert("role".into(), Value::String(m.role.clone()));
            obj.insert("content".into(), Value::String(m.content.clone()));
            if let Some(ref n) = m.name {
                obj.insert("name".into(), Value::String(n.clone()));
            }
            Value::Object(obj)
        })
        .collect();

    let rf = req
        .response_format
        .as_ref()
        .map(canonicalize)
        .unwrap_or(Value::Null);

    let canonical = json!({
        "model": req.model,
        "messages": messages,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
        "response_format": rf,
        // Reasoning-effort changes the downstream request body (OpenAI-compat
        // reasoning fields / Anthropic thinking budget), so cached responses
        // at different efforts are not interchangeable.
        "reasoning_effort": req.reasoning_effort,
    });

    // `serde_json::to_vec` on a Value preserves insertion order of the outer
    // object, which is the order we just built above. No stray reorderings.
    let bytes = serde_json::to_vec(&canonical).expect("canonical key serializes");
    let digest = Sha256::digest(&bytes);
    hex::encode(digest)
}

/// Recursively sort object keys so semantically equal JSON values hash to
/// the same bytes regardless of producer-side key ordering.
fn canonicalize(v: &Value) -> Value {
    match v {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let mut out = serde_json::Map::with_capacity(map.len());
            for k in keys {
                out.insert(k.clone(), canonicalize(&map[k]));
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(canonicalize).collect()),
        _ => v.clone(),
    }
}

/// Look up a cached non-stream response. Deserializes the raw provider JSON
/// and reconstructs a `CanonicalResponse`.
pub async fn get(
    redis: &mut ConnectionManager,
    key: &str,
) -> redis::RedisResult<Option<CanonicalResponse>> {
    let rk = cache_redis_key(key);
    let got: Option<String> = redis.get(rk).await?;
    let Some(s) = got else { return Ok(None) };
    let Ok(v) = serde_json::from_str::<Value>(&s) else {
        return Ok(None);
    };
    Ok(Some(CanonicalResponse::from_openai_json(v)))
}

/// SETEX the raw response JSON under this key.
pub async fn set(
    redis: &mut ConnectionManager,
    key: &str,
    resp: &CanonicalResponse,
) -> redis::RedisResult<()> {
    let rk = cache_redis_key(key);
    let body = serde_json::to_string(&resp.raw).unwrap_or_default();
    redis.set_ex::<_, _, ()>(rk, body, CACHE_TTL_SECS).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::canonical::{CanonicalRequest, ChatMessage};
    use serde_json::json;

    fn req() -> CanonicalRequest {
        CanonicalRequest {
            model: "deepseek-chat".into(),
            messages: vec![
                ChatMessage {
                    role: "system".into(),
                    content: "you are helpful".into(),
                    name: None,
                },
                ChatMessage {
                    role: "user".into(),
                    content: "hello".into(),
                    name: None,
                },
            ],
            temperature: Some(0.2),
            max_tokens: Some(64),
            stream: false,
            response_format: Some(json!({
                "type": "json_object",
                "schema": {"a": 1, "b": 2}
            })),
            reasoning_effort: None,
        }
    }

    #[test]
    fn same_request_same_key() {
        let a = cache_key(&req());
        let b = cache_key(&req());
        assert_eq!(a, b, "identical requests must hash identically");
        assert_eq!(a.len(), 64, "sha256 hex is 64 chars");
    }

    #[test]
    fn response_format_key_order_is_stable() {
        let mut r1 = req();
        r1.response_format = Some(json!({
            "type": "json_object",
            "schema": {"a": 1, "b": 2}
        }));
        let mut r2 = req();
        r2.response_format = Some(json!({
            "schema": {"b": 2, "a": 1},
            "type": "json_object"
        }));
        assert_eq!(
            cache_key(&r1),
            cache_key(&r2),
            "response_format key ordering must not affect cache key"
        );
    }

    #[test]
    fn stream_flag_is_ignored() {
        let mut a = req();
        let mut b = req();
        a.stream = false;
        b.stream = true;
        assert_eq!(
            cache_key(&a),
            cache_key(&b),
            "stream flag must not influence cache key"
        );
    }

    #[test]
    fn different_content_different_key() {
        let a = cache_key(&req());
        let mut other = req();
        other.messages[1].content = "goodbye".into();
        let b = cache_key(&other);
        assert_ne!(a, b, "different prompt must hash differently");
    }
}
