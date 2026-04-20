use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub host: String,
    pub port: u16,
    pub dev_auth_token: String,
    pub redis_url: String,
    pub database_url: String,
}

impl AppConfig {
    pub fn from_env() -> Result<Self> {
        let host = std::env::var("GATEWAY_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
        let port: u16 = std::env::var("GATEWAY_PORT")
            .unwrap_or_else(|_| "8080".to_string())
            .parse()
            .context("GATEWAY_PORT must be a valid u16")?;
        let dev_auth_token =
            std::env::var("DEV_AUTH_TOKEN").context("DEV_AUTH_TOKEN env var is required")?;
        let redis_url = std::env::var("REDIS_URL")
            .unwrap_or_else(|_| "redis://127.0.0.1:6379/0".to_string());
        let database_url =
            std::env::var("DATABASE_URL").context("DATABASE_URL env var is required")?;

        Ok(Self {
            host,
            port,
            dev_auth_token,
            redis_url,
            database_url,
        })
    }
}
