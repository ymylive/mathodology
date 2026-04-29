//! Static file serving for run artifacts.
//!
//! The Python worker writes figures under `<runs_dir>/<run_id>/figures/*.png`
//! (plus `.jpg`, `.svg`) and a notebook at `<runs_dir>/<run_id>/notebook.ipynb`.
//! The Vue UI fetches them through the gateway so the browser shares the
//! gateway auth token instead of hitting the filesystem directly.
//!
//! Security posture:
//! * `require_dev_token` middleware gates every request (wired in `app.rs`).
//! * Tail segments containing `..` are rejected before we touch the disk.
//! * After joining, the path is canonicalized and we assert the real path is
//!   a descendant of `<runs_dir>/<run_id>/figures`. Symlink escapes out of the
//!   run-scoped prefix therefore 403.
//! * Only a small allow-list of extensions is served (`png`, `jpg`, `jpeg`,
//!   `svg`) to avoid accidentally shipping arbitrary files the worker writes.
//! * Hard 16 MiB cap on file size; anything larger returns 413.

use std::path::{Path as StdPath, PathBuf};

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use tokio_util::io::ReaderStream;
use uuid::Uuid;

use crate::error::AppError;
use crate::state::AppState;

/// Anything >5 MiB is streamed instead of loaded fully into memory. Figures
/// are typically well under this, so the common path stays a single `read`.
const STREAM_THRESHOLD_BYTES: u64 = 5 * 1024 * 1024;

/// Absolute hard cap on served artifact size.
const MAX_FILE_BYTES: u64 = 16 * 1024 * 1024;

/// `GET /runs/:run_id/figures/*path` — serve a figure written by the worker.
#[tracing::instrument(skip_all, fields(%run_id, %tail))]
pub async fn serve_figure(
    State(state): State<AppState>,
    Path((run_id, tail)): Path<(Uuid, String)>,
) -> Result<Response, AppError> {
    // Belt-and-braces: reject any `..` segment up front. Canonicalize below
    // is the authoritative check; this is just a cheap early-out.
    if tail.split(['/', '\\']).any(|seg| seg == "..") {
        return Err(AppError::Forbidden);
    }
    if tail.is_empty() {
        return Err(AppError::NotFound);
    }

    // Extension allow-list.
    let content_type = match extension_lower(&tail).as_deref() {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("svg") => "image/svg+xml",
        _ => return Err(AppError::UnsupportedMediaType),
    };

    let figures_root = state.runs_dir.join(run_id.to_string()).join("figures");
    let requested = figures_root.join(&tail);

    let canonical = resolve_within(&figures_root, &requested).await?;

    serve_file(&canonical, content_type, None).await
}

/// `GET /runs/:run_id/notebook` — serve the executed `.ipynb` as a download.
#[tracing::instrument(skip_all, fields(%run_id))]
pub async fn serve_notebook(
    State(state): State<AppState>,
    Path(run_id): Path<Uuid>,
) -> Result<Response, AppError> {
    let run_root = state.runs_dir.join(run_id.to_string());
    let requested = run_root.join("notebook.ipynb");

    let canonical = resolve_within(&run_root, &requested).await?;

    let disposition = format!("attachment; filename=\"run-{run_id}.ipynb\"");
    serve_file(&canonical, "application/x-ipynb+json", Some(&disposition)).await
}

/// `GET /runs/:run_id/paper?inline=1` — serve the Writer's paper.md.
///
/// Query string `inline=1` renders in-browser (Content-Disposition: inline)
/// for the on-page preview; omitted forces a download.
#[tracing::instrument(skip_all, fields(%run_id))]
pub async fn serve_paper(
    State(state): State<AppState>,
    Path(run_id): Path<Uuid>,
    axum::extract::Query(q): axum::extract::Query<PaperQuery>,
) -> Result<Response, AppError> {
    let run_root = state.runs_dir.join(run_id.to_string());
    let requested = run_root.join("paper.md");

    let canonical = resolve_within(&run_root, &requested).await?;

    let disposition = if q.inline.unwrap_or(false) {
        format!("inline; filename=\"run-{run_id}.md\"")
    } else {
        format!("attachment; filename=\"run-{run_id}.md\"")
    };
    serve_file(
        &canonical,
        "text/markdown; charset=utf-8",
        Some(&disposition),
    )
    .await
}

#[derive(Debug, serde::Deserialize)]
pub struct PaperQuery {
    pub inline: Option<bool>,
}

/// Canonicalize `requested` and assert it lives under `prefix` (also canonical
/// or canonicalizable). Missing file → 404; escape → 403.
async fn resolve_within(prefix: &StdPath, requested: &StdPath) -> Result<PathBuf, AppError> {
    // `prefix` may not exist yet (e.g. a run with no figures). If we can't
    // canonicalize the prefix, the file underneath cannot exist either → 404.
    let canonical_prefix = match tokio::fs::canonicalize(prefix).await {
        Ok(p) => p,
        Err(_) => return Err(AppError::NotFound),
    };
    let canonical = match tokio::fs::canonicalize(requested).await {
        Ok(p) => p,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(AppError::NotFound),
        Err(e) => {
            tracing::warn!(error = %e, path = %requested.display(), "canonicalize failed");
            return Err(AppError::NotFound);
        }
    };

    if !canonical.starts_with(&canonical_prefix) {
        tracing::warn!(
            requested = %requested.display(),
            canonical = %canonical.display(),
            prefix = %canonical_prefix.display(),
            "path traversal blocked"
        );
        return Err(AppError::Forbidden);
    }

    Ok(canonical)
}

/// Stream-or-read the file at `path` with the given Content-Type and optional
/// Content-Disposition. Enforces `Cache-Control: private, max-age=3600` and
/// the 16 MiB hard cap.
async fn serve_file(
    path: &StdPath,
    content_type: &'static str,
    content_disposition: Option<&str>,
) -> Result<Response, AppError> {
    let meta = tokio::fs::metadata(path).await.map_err(|e| {
        tracing::warn!(error = %e, path = %path.display(), "metadata failed");
        AppError::NotFound
    })?;
    if !meta.is_file() {
        return Err(AppError::NotFound);
    }
    let size = meta.len();
    if size > MAX_FILE_BYTES {
        return Err(AppError::PayloadTooLarge);
    }

    let mut headers = HeaderMap::new();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("private, max-age=3600"),
    );
    if let Ok(len_hv) = HeaderValue::from_str(&size.to_string()) {
        headers.insert(header::CONTENT_LENGTH, len_hv);
    }
    if let Some(cd) = content_disposition {
        if let Ok(hv) = HeaderValue::from_str(cd) {
            headers.insert(header::CONTENT_DISPOSITION, hv);
        }
    }

    let body = if size > STREAM_THRESHOLD_BYTES {
        let file = tokio::fs::File::open(path).await.map_err(|e| {
            tracing::warn!(error = %e, path = %path.display(), "open failed");
            AppError::NotFound
        })?;
        Body::from_stream(ReaderStream::new(file))
    } else {
        let bytes = tokio::fs::read(path).await.map_err(|e| {
            tracing::warn!(error = %e, path = %path.display(), "read failed");
            AppError::NotFound
        })?;
        Body::from(bytes)
    };

    let mut resp = Response::builder()
        .status(StatusCode::OK)
        .body(body)
        .map_err(|e| AppError::Internal(format!("response build: {e}")))?;
    *resp.headers_mut() = headers;
    Ok(resp)
}

fn extension_lower(tail: &str) -> Option<String> {
    StdPath::new(tail)
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| s.to_ascii_lowercase())
}
