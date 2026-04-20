//! Library surface of the gateway crate. The binary `main.rs` is a thin
//! wrapper around `run()`; tests and other consumers go through `lib`.

pub mod app;
pub mod audit;
pub mod auth;
pub mod config;
pub mod dispatch;
pub mod error;
pub mod llm;
pub mod routes;
pub mod state;
