//! Loader for `config/providers.toml` → [`ProviderRegistry`].
//!
//! The TOML format is the public contract between ops and the gateway; keep
//! the file stable, version new fields additively.

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use dashmap::DashMap;
use reqwest::Client;
use serde::Deserialize;

use crate::llm::providers::anthropic::AnthropicAdapter;
use crate::llm::providers::openai_compat::OpenAICompatAdapter;
use crate::llm::providers::ProviderAdapter;

/// RMB-per-1M-token prices, indexed by model. The router looks up cost by
/// model name so it doesn't have to chase down the provider that served it.
#[derive(Debug, Clone, Default)]
pub struct PriceTable {
    inner: Arc<DashMap<String, Price>>,
}

#[derive(Debug, Clone, Copy)]
pub struct Price {
    pub input_per_1m: f64,
    pub output_per_1m: f64,
}

impl PriceTable {
    pub fn get(&self, model: &str) -> Option<Price> {
        self.inner.get(model).map(|r| *r.value())
    }
    pub fn insert(&self, model: String, price: Price) {
        self.inner.insert(model, price);
    }
}

#[derive(Debug, Deserialize)]
struct ProvidersFile {
    #[serde(default)]
    providers: Vec<ProviderEntry>,
    router: RouterEntry,
}

#[derive(Debug, Deserialize)]
struct ProviderEntry {
    name: String,
    kind: String,
    base_url: String,
    #[serde(default)]
    api_key_env: String,
    #[serde(default)]
    models: Vec<String>,
    #[serde(default)]
    price_input_per_1m: f64,
    #[serde(default)]
    price_output_per_1m: f64,
}

#[derive(Debug, Deserialize)]
struct RouterEntry {
    default_model: String,
    #[serde(default)]
    fallback: Vec<String>,
}

/// Holds the live set of adapters plus routing + pricing lookups.
pub struct ProviderRegistry {
    pub providers: Vec<Arc<dyn ProviderAdapter>>,
    pub default_model: String,
    pub fallback: Vec<String>,
    pub prices: PriceTable,
    pub meta: Vec<ProviderMeta>,
}

/// Read-only provider metadata for GET /providers. Mirrors ProviderEntry
/// fields kept after registry load, plus a `has_key` flag so the UI can
/// grey out providers missing their API key without leaking the key.
#[derive(Debug, Clone)]
pub struct ProviderMeta {
    pub name: String,
    pub kind: String,
    pub models: Vec<String>,
    pub price_input_per_1m: f64,
    pub price_output_per_1m: f64,
    pub has_key: bool,
}

impl ProviderRegistry {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading providers file {}", path.display()))?;
        let parsed: ProvidersFile = toml::from_str(&text)
            .with_context(|| format!("parsing providers toml {}", path.display()))?;

        // One shared HTTP client across all adapters. Long total timeout
        // tolerates slow model responses; short connect timeout fails fast
        // when a provider is down.
        let http = Client::builder()
            .timeout(Duration::from_secs(600))
            .connect_timeout(Duration::from_secs(5))
            .build()
            .context("building reqwest client")?;

        let prices = PriceTable::default();
        let mut providers: Vec<Arc<dyn ProviderAdapter>> = Vec::new();
        let mut meta: Vec<ProviderMeta> = Vec::new();

        for p in parsed.providers {
            // Price table is populated regardless of adapter availability so
            // that cost accounting still works if a future adapter lands.
            for m in &p.models {
                prices.insert(
                    m.clone(),
                    Price {
                        input_per_1m: p.price_input_per_1m,
                        output_per_1m: p.price_output_per_1m,
                    },
                );
            }

            let api_key = resolve_api_key(&p.api_key_env, &p.name);
            let has_key = !api_key.is_empty();

            match p.kind.as_str() {
                "openai_compat" => {
                    let adapter = OpenAICompatAdapter::new(
                        p.name.clone(),
                        p.base_url.clone(),
                        api_key,
                        p.models.clone(),
                        http.clone(),
                    );
                    providers.push(Arc::new(adapter));
                }
                "anthropic" => {
                    let adapter = AnthropicAdapter::new(
                        p.name.clone(),
                        p.base_url.clone(),
                        api_key,
                        p.models.clone(),
                        http.clone(),
                    );
                    providers.push(Arc::new(adapter));
                }
                other => {
                    tracing::warn!(
                        provider = %p.name,
                        kind = other,
                        "unknown provider kind; skipping"
                    );
                    continue;
                }
            }

            meta.push(ProviderMeta {
                name: p.name,
                kind: p.kind,
                models: p.models,
                price_input_per_1m: p.price_input_per_1m,
                price_output_per_1m: p.price_output_per_1m,
                has_key,
            });
        }

        Ok(Self {
            providers,
            default_model: parsed.router.default_model,
            fallback: parsed.router.fallback,
            prices,
            meta,
        })
    }
}

/// Read the API key from `api_key_env` and WARN if the env var is set but
/// resolves to empty (or unset). Returning `""` is valid — it tells the
/// adapter to omit the auth header, which is what the Ollama-local case
/// relies on. The warn is purely for operator observability.
fn resolve_api_key(env_name: &str, provider_name: &str) -> String {
    if env_name.is_empty() {
        return String::new();
    }
    let val = std::env::var(env_name).unwrap_or_default();
    if val.is_empty() {
        tracing::warn!(
            provider = %provider_name,
            env = %env_name,
            "api_key_env resolved to empty; provider will send requests without auth"
        );
    }
    val
}
