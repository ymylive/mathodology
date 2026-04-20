use axum::extract::{Request, State};
use axum::http::header::AUTHORIZATION;
use axum::middleware::Next;
use axum::response::Response;

use crate::error::AppError;
use crate::state::AppState;

/// Require `Authorization: Bearer <token>` OR `?token=<token>` matching DEV_AUTH_TOKEN.
///
/// Applied to `/runs` and `/ws/runs/*`. NOT applied to `/health`.
pub async fn require_dev_token(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, AppError> {
    let expected = state.config.dev_auth_token.as_str();

    // 1) Check Authorization header.
    if let Some(val) = req.headers().get(AUTHORIZATION) {
        if let Ok(s) = val.to_str() {
            if let Some(tok) = s.strip_prefix("Bearer ") {
                if constant_time_eq(tok.as_bytes(), expected.as_bytes()) {
                    return Ok(next.run(req).await);
                }
            }
        }
    }

    // 2) Check ?token=<> query param (for browser WS that can't set headers).
    if let Some(q) = req.uri().query() {
        for pair in q.split('&') {
            let mut it = pair.splitn(2, '=');
            let k = it.next().unwrap_or("");
            let v = it.next().unwrap_or("");
            if k == "token" && constant_time_eq(v.as_bytes(), expected.as_bytes()) {
                return Ok(next.run(req).await);
            }
        }
    }

    Err(AppError::Unauthorized)
}

/// Constant-time comparison to avoid token-length timing leaks.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}
