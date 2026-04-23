//! Search capability probe.
//!
//! `GET /search/capabilities` — reports which search backends the worker is
//! configured to drive. The UI calls this once at boot to decide whether to
//! grey out the Tavily toggle and/or individual `open-websearch` engines.
//!
//! This handler deliberately does NOT spawn `open-websearch` or ping Tavily;
//! it only inspects environment variables that the operator sets at deploy
//! time. Gateway never touches MCP — that's the worker's job.
//!
//! Security:
//! - `TAVILY_API_KEY` is reduced to a boolean. Its bytes never enter the
//!   response body or a log line.
//! - Authorized via `require_dev_token` like every other non-`/health` route,
//!   so opportunistic scanners can't enumerate the operator's env posture.

use axum::http::{header, HeaderMap, HeaderValue};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;

use crate::error::AppError;

/// Default engine list shipped with `open-websearch`. Returned when the
/// operator does not pin a subset via `OPEN_WEBSEARCH_ENGINES`.
const DEFAULT_ENGINES: &[&str] = &[
    "bing",
    "baidu",
    "duckduckgo",
    "csdn",
    "juejin",
    "brave",
    "exa",
    "startpage",
];

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct SearchCapabilities {
    pub tavily_available: bool,
    pub open_websearch_available: bool,
    pub available_engines: Vec<String>,
}

impl SearchCapabilities {
    /// Build capabilities from an env-reader closure. Keeping the closure
    /// injectable lets unit tests exercise every branch without mutating
    /// the process environment (which would race with parallel tests).
    fn from_env<F>(get: F) -> Self
    where
        F: Fn(&str) -> Option<String>,
    {
        SearchCapabilities {
            tavily_available: probe_tavily(&get),
            open_websearch_available: probe_open_websearch(&get),
            available_engines: probe_engines(&get),
        }
    }
}

fn probe_tavily<F>(get: &F) -> bool
where
    F: Fn(&str) -> Option<String>,
{
    matches!(get("TAVILY_API_KEY"), Some(v) if !v.is_empty())
}

fn probe_open_websearch<F>(get: &F) -> bool
where
    F: Fn(&str) -> Option<String>,
{
    !matches!(
        get("OPEN_WEBSEARCH_DISABLED").as_deref(),
        Some("1") | Some("true")
    )
}

fn probe_engines<F>(get: &F) -> Vec<String>
where
    F: Fn(&str) -> Option<String>,
{
    if let Some(raw) = get("OPEN_WEBSEARCH_ENGINES") {
        let parsed: Vec<String> = raw
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        if !parsed.is_empty() {
            return parsed;
        }
    }
    DEFAULT_ENGINES.iter().map(|s| (*s).to_string()).collect()
}

#[tracing::instrument(skip_all)]
pub async fn capabilities() -> Result<Response, AppError> {
    let caps = SearchCapabilities::from_env(|k| std::env::var(k).ok());

    let mut headers = HeaderMap::new();
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store"),
    );
    Ok((headers, Json(caps)).into_response())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// Build a reader closure from a static map. `None` means the env var
    /// is unset; `Some("")` means set-but-empty.
    fn reader(map: HashMap<&'static str, Option<&'static str>>) -> impl Fn(&str) -> Option<String> {
        move |k: &str| map.get(k).and_then(|v| v.map(|s| s.to_string()))
    }

    #[test]
    fn tavily_set_non_empty_is_available() {
        let get = reader(HashMap::from([("TAVILY_API_KEY", Some("tvly-abc123"))]));
        assert!(probe_tavily(&get));
    }

    #[test]
    fn tavily_unset_is_unavailable() {
        let get = reader(HashMap::new());
        assert!(!probe_tavily(&get));
    }

    #[test]
    fn tavily_empty_string_is_unavailable() {
        let get = reader(HashMap::from([("TAVILY_API_KEY", Some(""))]));
        assert!(!probe_tavily(&get));
    }

    #[test]
    fn open_websearch_default_is_available() {
        let get = reader(HashMap::new());
        assert!(probe_open_websearch(&get));
    }

    #[test]
    fn open_websearch_disabled_1_is_unavailable() {
        let get = reader(HashMap::from([("OPEN_WEBSEARCH_DISABLED", Some("1"))]));
        assert!(!probe_open_websearch(&get));
    }

    #[test]
    fn open_websearch_disabled_true_is_unavailable() {
        let get = reader(HashMap::from([("OPEN_WEBSEARCH_DISABLED", Some("true"))]));
        assert!(!probe_open_websearch(&get));
    }

    #[test]
    fn open_websearch_disabled_garbage_is_still_available() {
        // Anything that isn't exactly "1" or "true" leaves the feature on;
        // typos shouldn't silently break the worker.
        let get = reader(HashMap::from([("OPEN_WEBSEARCH_DISABLED", Some("nope"))]));
        assert!(probe_open_websearch(&get));
    }

    #[test]
    fn engines_default_is_all_eight() {
        let get = reader(HashMap::new());
        let engines = probe_engines(&get);
        assert_eq!(
            engines,
            vec![
                "bing", "baidu", "duckduckgo", "csdn", "juejin", "brave", "exa", "startpage",
            ]
        );
    }

    #[test]
    fn engines_csv_trimmed_subset() {
        let get = reader(HashMap::from([(
            "OPEN_WEBSEARCH_ENGINES",
            Some("baidu, csdn"),
        )]));
        assert_eq!(probe_engines(&get), vec!["baidu", "csdn"]);
    }

    #[test]
    fn engines_csv_skips_empty_tokens() {
        let get = reader(HashMap::from([(
            "OPEN_WEBSEARCH_ENGINES",
            Some("bing,,  ,baidu"),
        )]));
        assert_eq!(probe_engines(&get), vec!["bing", "baidu"]);
    }

    #[test]
    fn engines_empty_string_falls_back_to_defaults() {
        let get = reader(HashMap::from([("OPEN_WEBSEARCH_ENGINES", Some(""))]));
        assert_eq!(probe_engines(&get).len(), 8);
    }

    #[test]
    fn caps_struct_serializes_as_spec() {
        let get = reader(HashMap::from([
            ("TAVILY_API_KEY", Some("secret-xyz")),
            ("OPEN_WEBSEARCH_ENGINES", Some("bing, baidu")),
        ]));
        let caps = SearchCapabilities::from_env(get);
        let body = serde_json::to_string(&caps).unwrap();
        assert!(body.contains("\"tavily_available\":true"));
        assert!(body.contains("\"open_websearch_available\":true"));
        assert!(body.contains("\"available_engines\":[\"bing\",\"baidu\"]"));
        // The API key must never leak into the serialized payload.
        assert!(!body.contains("secret-xyz"));
    }
}
