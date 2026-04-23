//! Paper export endpoint.
//!
//! `GET /runs/:run_id/export/:format?template=<t>`
//!
//! Consumes:
//!   * `<runs_dir>/<run_id>/paper.meta.json` — structured meta written by the
//!     worker's Writer agent (see project CLAUDE).
//!   * `<runs_dir>/<run_id>/paper.md`        — pre-rendered markdown for the
//!     `md` and `docx` paths.
//!   * `<runs_dir>/<run_id>/figures/*.png|svg` — referenced via `[[FIG:id]]`
//!     placeholders inside section bodies.
//!
//! Produces one of: `md | tex | pdf | docx`.
//!
//! External tools:
//!   * `pandoc`  — converts per-section markdown bodies to LaTeX, and the
//!                 whole `paper.md` to `.docx`.
//!   * `tectonic` — compiles the rendered `.tex` to PDF.
//!
//! Security: path-traversal defense is the same `canonicalize + starts_with`
//! pattern used by `figures::serve_figure`. All external commands are spawned
//! via `tokio::process::Command` (argv, no shell interpolation). LaTeX
//! injection from the structured meta is mitigated by a `latex_escape` Tera
//! filter applied to every direct-to-LaTeX field (`title`, `abstract`,
//! section titles, references). Section bodies pass through `pandoc`, which
//! itself escapes LaTeX-special characters on conversion.

use std::collections::HashMap;
use std::path::{Path as StdPath, PathBuf};
use std::sync::OnceLock;

use axum::body::Body;
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use serde::Deserialize;
use tera::Tera;
use uuid::Uuid;

use crate::error::AppError;
use crate::state::AppState;

/// Timeout for tectonic/pandoc invocations. Tectonic's first run downloads
/// ~200 MB of TeXLive bundles, so we keep this generous.
const COMPILE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(180);

/// Upper bound on how much stderr we embed in the 500 error body.
const STDERR_SNIP_MAX: usize = 4 * 1024;

// ---------------------------------------------------------------------------
// Contract: paper.meta.json
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct PaperMeta {
    pub title: String,
    #[serde(default)]
    pub r#abstract: String,
    #[serde(default)]
    pub competition_type: Option<String>,
    #[serde(default)]
    pub problem_text: String,
    #[serde(default)]
    pub sections: Vec<PaperSection>,
    #[serde(default)]
    pub references: Vec<String>,
    #[serde(default)]
    pub figures: Vec<FigureRef>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PaperSection {
    pub title: String,
    #[serde(default)]
    pub body_markdown: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FigureRef {
    pub id: String,
    #[serde(default)]
    pub caption: String,
    pub path_png: String,
    #[serde(default)]
    pub path_svg: Option<String>,
    #[serde(default = "default_width")]
    pub width: f32,
}

fn default_width() -> f32 {
    0.8
}

// ---------------------------------------------------------------------------
// Query / path params
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ExportQuery {
    #[serde(default)]
    pub template: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExportFormat {
    Md,
    Tex,
    Pdf,
    Docx,
}

impl ExportFormat {
    fn parse(s: &str) -> Option<Self> {
        match s {
            "md" => Some(Self::Md),
            "tex" => Some(Self::Tex),
            "pdf" => Some(Self::Pdf),
            "docx" => Some(Self::Docx),
            _ => None,
        }
    }

    fn ext(&self) -> &'static str {
        match self {
            Self::Md => "md",
            Self::Tex => "tex",
            Self::Pdf => "pdf",
            Self::Docx => "docx",
        }
    }

    fn content_type(&self) -> &'static str {
        match self {
            Self::Md => "text/markdown; charset=utf-8",
            Self::Tex => "application/x-tex",
            Self::Pdf => "application/pdf",
            Self::Docx => {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TemplateKind {
    Cumcm,
    Huashu,
    Mcm,
}

impl TemplateKind {
    fn parse(s: &str) -> Option<Self> {
        match s {
            "cumcm" => Some(Self::Cumcm),
            "huashu" => Some(Self::Huashu),
            // `icm` shares the English MCM layout.
            "mcm" | "icm" => Some(Self::Mcm),
            _ => None,
        }
    }

    fn file(&self) -> &'static str {
        match self {
            Self::Cumcm => "cumcm.tex.tera",
            Self::Huashu => "huashu.tex.tera",
            Self::Mcm => "mcm.tex.tera",
        }
    }

    /// Resolve the competition_type → TemplateKind default (used when no
    /// explicit `template=` query is present). `other` degrades to `cumcm`
    /// per spec.
    fn from_competition(ct: Option<&str>) -> Self {
        match ct.unwrap_or("").to_ascii_lowercase().as_str() {
            "mcm" | "icm" => Self::Mcm,
            "huashu" => Self::Huashu,
            "cumcm" => Self::Cumcm,
            _ => Self::Cumcm,
        }
    }
}

// ---------------------------------------------------------------------------
// Tera instance — templates are bundled at build time and rendered from
// in-memory strings so the binary is portable.
// ---------------------------------------------------------------------------

fn tera() -> &'static Tera {
    static INSTANCE: OnceLock<Tera> = OnceLock::new();
    INSTANCE.get_or_init(|| {
        let mut t = Tera::default();
        t.add_raw_templates(vec![
            (
                "cumcm.tex.tera",
                include_str!("../../templates/cumcm.tex.tera"),
            ),
            (
                "huashu.tex.tera",
                include_str!("../../templates/huashu.tex.tera"),
            ),
            (
                "mcm.tex.tera",
                include_str!("../../templates/mcm.tex.tera"),
            ),
        ])
        .expect("bundled Tera templates parse");
        t.register_filter("latex_escape", latex_escape_filter);
        t
    })
}

// ---------------------------------------------------------------------------
// Route handler
// ---------------------------------------------------------------------------

#[tracing::instrument(skip_all, fields(%run_id, %format))]
pub async fn export_paper(
    State(state): State<AppState>,
    Path((run_id, format)): Path<(Uuid, String)>,
    Query(q): Query<ExportQuery>,
) -> Result<Response, AppError> {
    let fmt = ExportFormat::parse(&format).ok_or_else(|| {
        AppError::UnprocessableEntity(format!(
            "unsupported format {format:?}; expected one of md|tex|pdf|docx"
        ))
    })?;

    let run_root = state.runs_dir.join(run_id.to_string());
    let canonical_run_root = tokio::fs::canonicalize(&run_root).await.map_err(|e| {
        tracing::debug!(error = %e, "canonicalize run root failed");
        AppError::NotFound
    })?;

    // md shortcut: no meta.json required, but we still go through path
    // resolution to produce a stable attachment filename.
    if fmt == ExportFormat::Md {
        let paper_md = resolve_within(&canonical_run_root, &canonical_run_root.join("paper.md"))
            .await?;
        let bytes = read_capped(&paper_md).await?;
        return build_binary_response(fmt, run_id, bytes);
    }

    // Everything else needs paper.meta.json.
    let meta_path = resolve_within(
        &canonical_run_root,
        &canonical_run_root.join("paper.meta.json"),
    )
    .await?;
    let meta_bytes = tokio::fs::read(&meta_path).await.map_err(|e| {
        tracing::debug!(error = %e, "read paper.meta.json");
        AppError::NotFound
    })?;
    let meta: PaperMeta = serde_json::from_slice(&meta_bytes).map_err(|e| {
        AppError::UnprocessableEntity(format!("paper.meta.json invalid: {e}"))
    })?;

    // Template resolution: query override > meta competition_type > cumcm.
    let template = match q.template.as_deref() {
        Some(s) => TemplateKind::parse(s).ok_or_else(|| {
            AppError::UnprocessableEntity(format!(
                "unsupported template {s:?}; expected one of mcm|icm|cumcm|huashu"
            ))
        })?,
        None => TemplateKind::from_competition(meta.competition_type.as_deref()),
    };

    match fmt {
        ExportFormat::Md => unreachable!("handled above"),
        ExportFormat::Tex => {
            let tex = render_tex(&meta, template, &canonical_run_root).await?;
            build_binary_response(fmt, run_id, tex.into_bytes())
        }
        ExportFormat::Pdf => {
            let tex = render_tex(&meta, template, &canonical_run_root).await?;
            let pdf = compile_pdf(&tex).await?;
            build_binary_response(fmt, run_id, pdf)
        }
        ExportFormat::Docx => {
            // Simpler path (per spec's "thought 2"): pandoc consumes the
            // already-rendered paper.md directly. Figure paths inside the md
            // are relative, so we pass --resource-path=<run_root>.
            let paper_md = resolve_within(
                &canonical_run_root,
                &canonical_run_root.join("paper.md"),
            )
            .await?;
            let docx = compile_docx(&paper_md, &canonical_run_root).await?;
            build_binary_response(fmt, run_id, docx)
        }
    }
}

// ---------------------------------------------------------------------------
// TeX rendering pipeline
// ---------------------------------------------------------------------------

async fn render_tex(
    meta: &PaperMeta,
    template: TemplateKind,
    run_root: &StdPath,
) -> Result<String, AppError> {
    // Build figure lookup by id.
    let mut figure_map: HashMap<&str, &FigureRef> = HashMap::new();
    for fig in &meta.figures {
        figure_map.insert(fig.id.as_str(), fig);
    }

    // For each section: substitute [[FIG:id]] placeholders with raw-LaTeX
    // blocks (via pandoc's `raw_attribute` extension), then shell out to
    // pandoc to convert the substituted markdown to LaTeX.
    let mut rendered_sections: Vec<serde_json::Value> = Vec::with_capacity(meta.sections.len());
    for sec in &meta.sections {
        let substituted = substitute_figures(&sec.body_markdown, &figure_map);
        let body_latex = md_to_latex(&substituted).await?;
        rendered_sections.push(serde_json::json!({
            "title": sec.title,
            "body_latex": body_latex,
        }));
    }

    // Figures are saved by the worker at paths relative to run_root (e.g.
    // `figures/foo.png`). Tectonic compiles in a tmpdir, so we inject an
    // absolute `\graphicspath{{<run_root>/}}` into the template preamble.
    // LaTeX requires trailing slash and braces per path.
    let mut graphics_root = run_root.to_string_lossy().into_owned();
    if !graphics_root.ends_with('/') {
        graphics_root.push('/');
    }

    let mut ctx = tera::Context::new();
    ctx.insert("title", &meta.title);
    ctx.insert("abstract", &meta.r#abstract);
    ctx.insert("problem_text", &meta.problem_text);
    ctx.insert("sections", &rendered_sections);
    ctx.insert("references", &meta.references);
    ctx.insert("team_id", "");
    ctx.insert("problem_id", "");
    ctx.insert("graphics_root", &graphics_root);

    let rendered = tera()
        .render(template.file(), &ctx)
        .map_err(|e| AppError::Internal(format!("tera render: {e}")))?;
    Ok(rendered)
}

/// Replace `[[FIG:<id>]]` placeholders with raw-LaTeX figure blocks.
///
/// The replacement is wrapped in pandoc's `raw_attribute` syntax so that when
/// we pipe the result through `pandoc -f markdown+raw_attribute -t latex`,
/// the block is copied verbatim to the output rather than re-escaped. Missing
/// ids are removed (not left as-is) with a `tracing::warn`.
fn substitute_figures(body: &str, figures: &HashMap<&str, &FigureRef>) -> String {
    // Cheap scanner over the specific `[[FIG:...]]` syntax. We avoid pulling
    // in regex for a single fixed token.
    let needle_open = "[[FIG:";
    let needle_close = "]]";
    let mut out = String::with_capacity(body.len());
    let mut rest = body;
    while let Some(i) = rest.find(needle_open) {
        out.push_str(&rest[..i]);
        let after = &rest[i + needle_open.len()..];
        if let Some(j) = after.find(needle_close) {
            let id = &after[..j];
            if let Some(fig) = figures.get(id) {
                out.push_str(&figure_latex_block(fig));
            } else {
                tracing::warn!(fig_id = %id, "unknown figure id in [[FIG:...]] placeholder");
            }
            rest = &after[j + needle_close.len()..];
        } else {
            // Unterminated `[[FIG:` — keep the original and bail out so we
            // don't accidentally swallow content.
            out.push_str(needle_open);
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

fn figure_latex_block(fig: &FigureRef) -> String {
    // LaTeX's graphicx package doesn't natively support SVG (would require
    // shelling out to inkscape + the `svg` package). Always use PNG here;
    // the worker's bootstrap sets savefig.dpi=300 so PNGs are print-quality.
    let path = fig.path_png.as_str();
    let width = if fig.width.is_finite() && fig.width > 0.0 && fig.width <= 1.0 {
        fig.width
    } else {
        0.8
    };
    let caption = latex_escape(&fig.caption);
    // Figure id is a [a-z0-9_]+ slug per the worker's Figure contract — no
    // LaTeX-special chars, so we do NOT latex_escape it. Escaping the `_`
    // inside \label{fig:foo_bar} would produce `\_` which breaks \ref/hyperref.
    let id = fig.id.as_str();
    let block = format!(
        "\\begin{{figure}}[htbp]\n\\centering\n\\includegraphics[width={w}\\textwidth]{{{p}}}\n\\caption{{{c}}}\n\\label{{fig:{l}}}\n\\end{{figure}}",
        w = width,
        p = path,
        c = caption,
        l = id,
    );
    // Surround with blank lines + pandoc raw_attribute to pass through
    // verbatim when we convert the substituted markdown to LaTeX.
    format!("\n\n```{{=latex}}\n{block}\n```\n\n")
}

/// Convert a markdown string to LaTeX via pandoc. Returns the converted body.
async fn md_to_latex(md: &str) -> Result<String, AppError> {
    if md.trim().is_empty() {
        return Ok(String::new());
    }
    let out = spawn_with_stdin(
        "pandoc",
        &[
            "-f",
            "markdown+raw_attribute+tex_math_dollars",
            "-t",
            "latex",
            "--wrap=preserve",
            "--no-highlight",
        ],
        md.as_bytes(),
    )
    .await?;
    String::from_utf8(out)
        .map_err(|e| AppError::Internal(format!("pandoc output not utf-8: {e}")))
}

async fn compile_pdf(tex: &str) -> Result<Vec<u8>, AppError> {
    let tmp = tempfile::TempDir::new()
        .map_err(|e| AppError::Internal(format!("tempdir: {e}")))?;
    let tex_path = tmp.path().join("paper.tex");
    tokio::fs::write(&tex_path, tex)
        .await
        .map_err(|e| AppError::Internal(format!("write tex: {e}")))?;

    let outdir_str = tmp.path().to_string_lossy().into_owned();

    let run = tokio::process::Command::new("tectonic")
        .args(["-X", "compile", "--outdir", &outdir_str, "--keep-logs"])
        .arg(&tex_path)
        .kill_on_drop(true)
        .output();

    let output = match tokio::time::timeout(COMPILE_TIMEOUT, run).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(AppError::ServiceUnavailable(
                "tectonic not found on PATH; install tectonic to enable PDF export".into(),
            ));
        }
        Ok(Err(e)) => {
            return Err(AppError::Internal(format!("tectonic spawn: {e}")))
        }
        Err(_) => {
            return Err(AppError::Internal(
                "tectonic compile timed out after 180s".into(),
            ))
        }
    };

    if !output.status.success() {
        let snip = snip_stderr(&output.stderr);
        return Err(AppError::Internal(format!("tectonic failed:\n{snip}")));
    }

    let pdf_path = tmp.path().join("paper.pdf");
    tokio::fs::read(&pdf_path)
        .await
        .map_err(|e| AppError::Internal(format!("read compiled pdf: {e}")))
}

async fn compile_docx(paper_md: &StdPath, run_root: &StdPath) -> Result<Vec<u8>, AppError> {
    let tmp = tempfile::TempDir::new()
        .map_err(|e| AppError::Internal(format!("tempdir: {e}")))?;
    let out_path = tmp.path().join("paper.docx");

    let run = tokio::process::Command::new("pandoc")
        .arg(paper_md)
        .args(["-f", "markdown", "-o"])
        .arg(&out_path)
        .arg("--resource-path")
        .arg(run_root)
        .kill_on_drop(true)
        .output();

    let output = match tokio::time::timeout(COMPILE_TIMEOUT, run).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(AppError::ServiceUnavailable(
                "pandoc not found on PATH; install pandoc to enable DOCX export".into(),
            ));
        }
        Ok(Err(e)) => return Err(AppError::Internal(format!("pandoc spawn: {e}"))),
        Err(_) => {
            return Err(AppError::Internal(
                "pandoc docx compile timed out after 180s".into(),
            ))
        }
    };

    if !output.status.success() {
        let snip = snip_stderr(&output.stderr);
        return Err(AppError::Internal(format!("pandoc failed:\n{snip}")));
    }

    tokio::fs::read(&out_path)
        .await
        .map_err(|e| AppError::Internal(format!("read compiled docx: {e}")))
}

/// Spawn `cmd args...`, feed `stdin` on stdin, return captured stdout.
///
/// Maps `ENOENT` from the fork to `ServiceUnavailable` so callers can turn it
/// into a clean 503.
async fn spawn_with_stdin(
    cmd: &str,
    args: &[&str],
    stdin: &[u8],
) -> Result<Vec<u8>, AppError> {
    use tokio::io::AsyncWriteExt;
    let mut child = match tokio::process::Command::new(cmd)
        .args(args)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
    {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(AppError::ServiceUnavailable(format!(
                "{cmd} not found on PATH; install it to enable this export format"
            )))
        }
        Err(e) => return Err(AppError::Internal(format!("spawn {cmd}: {e}"))),
    };

    if let Some(mut w) = child.stdin.take() {
        w.write_all(stdin)
            .await
            .map_err(|e| AppError::Internal(format!("write {cmd} stdin: {e}")))?;
        drop(w);
    }

    let output = match tokio::time::timeout(COMPILE_TIMEOUT, child.wait_with_output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(AppError::Internal(format!("{cmd} wait: {e}"))),
        Err(_) => {
            return Err(AppError::Internal(format!(
                "{cmd} timed out after 180s"
            )))
        }
    };

    if !output.status.success() {
        let snip = snip_stderr(&output.stderr);
        return Err(AppError::Internal(format!("{cmd} failed:\n{snip}")));
    }
    Ok(output.stdout)
}

fn snip_stderr(bytes: &[u8]) -> String {
    let s = String::from_utf8_lossy(bytes);
    if s.len() <= STDERR_SNIP_MAX {
        s.into_owned()
    } else {
        format!("{}...[truncated]", &s[..STDERR_SNIP_MAX])
    }
}

// ---------------------------------------------------------------------------
// LaTeX escaping
// ---------------------------------------------------------------------------

/// Escape a string so it is safe to embed in LaTeX body text.
///
/// Handles the 10 LaTeX "special" characters. `\` is mapped to
/// `\textbackslash{}` and `~` / `^` become `\textasciitilde{}` /
/// `\textasciicircum{}` so they survive without being interpreted as
/// accents or non-breaking space.
pub fn latex_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len() + input.len() / 8);
    for c in input.chars() {
        match c {
            '\\' => out.push_str("\\textbackslash{}"),
            '&' => out.push_str("\\&"),
            '%' => out.push_str("\\%"),
            '$' => out.push_str("\\$"),
            '#' => out.push_str("\\#"),
            '_' => out.push_str("\\_"),
            '{' => out.push_str("\\{"),
            '}' => out.push_str("\\}"),
            '~' => out.push_str("\\textasciitilde{}"),
            '^' => out.push_str("\\textasciicircum{}"),
            other => out.push(other),
        }
    }
    out
}

fn latex_escape_filter(
    value: &tera::Value,
    _args: &HashMap<String, tera::Value>,
) -> tera::Result<tera::Value> {
    let s = value.as_str().map(|s| s.to_string()).unwrap_or_else(|| value.to_string());
    Ok(tera::Value::String(latex_escape(&s)))
}

// ---------------------------------------------------------------------------
// Helpers shared with figures.rs (kept separate to avoid churning that file)
// ---------------------------------------------------------------------------

async fn resolve_within(prefix: &StdPath, requested: &StdPath) -> Result<PathBuf, AppError> {
    let canonical_prefix = tokio::fs::canonicalize(prefix)
        .await
        .map_err(|_| AppError::NotFound)?;
    let canonical = tokio::fs::canonicalize(requested).await.map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            AppError::NotFound
        } else {
            tracing::warn!(error = %e, path = %requested.display(), "canonicalize failed");
            AppError::NotFound
        }
    })?;
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

async fn read_capped(path: &StdPath) -> Result<Vec<u8>, AppError> {
    // The 16 MiB figures cap is unnecessarily tight for a paper PDF; we keep
    // the same order of magnitude but bump to 32 MiB for exports.
    const MAX_BYTES: u64 = 32 * 1024 * 1024;
    let meta = tokio::fs::metadata(path).await.map_err(|_| AppError::NotFound)?;
    if !meta.is_file() {
        return Err(AppError::NotFound);
    }
    if meta.len() > MAX_BYTES {
        return Err(AppError::PayloadTooLarge);
    }
    tokio::fs::read(path)
        .await
        .map_err(|e| AppError::Internal(format!("read {}: {e}", path.display())))
}

fn build_binary_response(
    fmt: ExportFormat,
    run_id: Uuid,
    bytes: Vec<u8>,
) -> Result<Response, AppError> {
    let mut headers = HeaderMap::new();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static(fmt.content_type()),
    );
    let len = bytes.len().to_string();
    if let Ok(hv) = HeaderValue::from_str(&len) {
        headers.insert(header::CONTENT_LENGTH, hv);
    }
    let disposition = format!("attachment; filename=\"paper-{}.{}\"", run_id, fmt.ext());
    if let Ok(hv) = HeaderValue::from_str(&disposition) {
        headers.insert(header::CONTENT_DISPOSITION, hv);
    }
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("private, no-store"),
    );

    let mut resp = Response::builder()
        .status(StatusCode::OK)
        .body(Body::from(bytes))
        .map_err(|e| AppError::Internal(format!("response build: {e}")))?;
    *resp.headers_mut() = headers;
    Ok(resp)
}

// ---------------------------------------------------------------------------
// Unit tests — no external binaries required.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latex_escape_handles_all_specials() {
        let input = r"&%$#_{}~^\";
        let out = latex_escape(input);
        assert_eq!(
            out,
            "\\&\\%\\$\\#\\_\\{\\}\\textasciitilde{}\\textasciicircum{}\\textbackslash{}"
        );
    }

    #[test]
    fn latex_escape_passes_through_plain_text() {
        assert_eq!(latex_escape("hello world"), "hello world");
        assert_eq!(latex_escape("中文 abc"), "中文 abc");
    }

    #[test]
    fn format_parser() {
        assert_eq!(ExportFormat::parse("md"), Some(ExportFormat::Md));
        assert_eq!(ExportFormat::parse("tex"), Some(ExportFormat::Tex));
        assert_eq!(ExportFormat::parse("pdf"), Some(ExportFormat::Pdf));
        assert_eq!(ExportFormat::parse("docx"), Some(ExportFormat::Docx));
        assert_eq!(ExportFormat::parse("exe"), None);
        assert_eq!(ExportFormat::parse(""), None);
    }

    #[test]
    fn template_parser_and_defaults() {
        assert_eq!(TemplateKind::parse("cumcm"), Some(TemplateKind::Cumcm));
        assert_eq!(TemplateKind::parse("huashu"), Some(TemplateKind::Huashu));
        assert_eq!(TemplateKind::parse("mcm"), Some(TemplateKind::Mcm));
        assert_eq!(TemplateKind::parse("icm"), Some(TemplateKind::Mcm));
        assert_eq!(TemplateKind::parse("other"), None);

        // competition_type → default
        assert_eq!(
            TemplateKind::from_competition(Some("cumcm")),
            TemplateKind::Cumcm
        );
        assert_eq!(
            TemplateKind::from_competition(Some("huashu")),
            TemplateKind::Huashu
        );
        assert_eq!(
            TemplateKind::from_competition(Some("mcm")),
            TemplateKind::Mcm
        );
        assert_eq!(
            TemplateKind::from_competition(Some("icm")),
            TemplateKind::Mcm
        );
        // `other` and unknown degrade to CUMCM.
        assert_eq!(
            TemplateKind::from_competition(Some("other")),
            TemplateKind::Cumcm
        );
        assert_eq!(TemplateKind::from_competition(None), TemplateKind::Cumcm);
    }

    fn fixture_meta() -> PaperMeta {
        PaperMeta {
            title: "关于 $P=NP$ 的讨论 & 实验".into(),
            r#abstract: "本文研究 … 50% 的边界条件_v2。".into(),
            competition_type: Some("cumcm".into()),
            problem_text: "给定数据集 X_1, …, X_n。".into(),
            sections: vec![
                PaperSection {
                    title: "模型建立".into(),
                    body_markdown: "设 $f(x) = x^2$。参见 [[FIG:plot1]]。".into(),
                },
                PaperSection {
                    title: "Results".into(),
                    body_markdown: "Details here. See [[FIG:plot1]] below.\n\n[[FIG:unknown]]"
                        .into(),
                },
            ],
            references: vec![
                "Knuth, D. (1984). Literate Programming.".into(),
                "Lamport, L. LaTeX: A Document Preparation System.".into(),
            ],
            figures: vec![FigureRef {
                id: "plot1".into(),
                caption: "Loss curve for 100% training set".into(),
                path_png: "figures/plot1.png".into(),
                path_svg: Some("figures/plot1.svg".into()),
                width: 0.8,
            }],
        }
    }

    #[test]
    fn substitute_figures_replaces_known_ids() {
        let meta = fixture_meta();
        let mut map: HashMap<&str, &FigureRef> = HashMap::new();
        for f in &meta.figures {
            map.insert(f.id.as_str(), f);
        }
        let out = substitute_figures("Pre [[FIG:plot1]] post.", &map);
        assert!(out.contains("\\begin{figure}"));
        // LaTeX graphicx doesn't support SVG natively — we always use PNG.
        assert!(out.contains("figures/plot1.png"), "uses PNG path");
        assert!(!out.contains("figures/plot1.svg"), "does not use SVG");
        assert!(out.contains("\\caption{Loss curve for 100\\% training set}"));
        assert!(out.contains("```{=latex}"), "wraps in pandoc raw block");
    }

    #[test]
    fn substitute_figures_drops_unknown_ids() {
        let map: HashMap<&str, &FigureRef> = HashMap::new();
        let out = substitute_figures("A [[FIG:missing]] B", &map);
        // Placeholder removed, surrounding text intact.
        assert!(out.contains('A'));
        assert!(out.contains('B'));
        assert!(!out.contains("[[FIG:missing]]"));
    }

    #[test]
    fn substitute_figures_preserves_unterminated_token() {
        let map: HashMap<&str, &FigureRef> = HashMap::new();
        let out = substitute_figures("keep [[FIG: unterminated", &map);
        assert!(out.contains("[[FIG:"));
    }

    #[test]
    fn figure_block_clamps_width() {
        let fig = FigureRef {
            id: "f".into(),
            caption: "x".into(),
            path_png: "figures/f.png".into(),
            path_svg: None,
            width: 2.5, // out of range
        };
        let block = figure_latex_block(&fig);
        assert!(block.contains("width=0.8\\textwidth"));
        assert!(block.contains("figures/f.png"));
    }

    fn render_template(template: TemplateKind) -> String {
        let meta = fixture_meta();

        // In unit tests we can't shell out to pandoc — fake body_latex so we
        // exercise the Tera rendering path end-to-end without external deps.
        let sections: Vec<serde_json::Value> = meta
            .sections
            .iter()
            .map(|s| {
                serde_json::json!({
                    "title": s.title,
                    "body_latex": format!("%% section placeholder body for {}", s.title),
                })
            })
            .collect();

        let mut ctx = tera::Context::new();
        ctx.insert("title", &meta.title);
        ctx.insert("abstract", &meta.r#abstract);
        ctx.insert("problem_text", &meta.problem_text);
        ctx.insert("sections", &sections);
        ctx.insert("references", &meta.references);
        ctx.insert("team_id", "");
        ctx.insert("problem_id", "");
        ctx.insert("graphics_root", "/tmp/fake-run/");

        tera()
            .render(template.file(), &ctx)
            .unwrap_or_else(|e| panic!("render {}: {e}", template.file()))
    }

    #[test]
    fn cumcm_template_renders() {
        let out = render_template(TemplateKind::Cumcm);
        assert!(out.contains("\\documentclass"));
        assert!(out.contains("ctexart"));
        assert!(out.contains("全国大学生数学建模竞赛"));
        // title got escaped: `&` -> `\&`, `$` -> `\$`
        assert!(out.contains("\\& 实验"));
        assert!(out.contains("\\$P=NP\\$"));
        // sections rendered
        assert!(out.contains("\\section{模型建立}"));
        // references rendered
        assert!(out.contains("\\begin{thebibliography}"));
        assert!(out.contains("Knuth, D."));
    }

    #[test]
    fn huashu_template_renders() {
        let out = render_template(TemplateKind::Huashu);
        assert!(out.contains("华数杯全国大学生数学建模竞赛"));
        assert!(out.contains("\\section{模型建立}"));
    }

    #[test]
    fn mcm_template_renders() {
        let out = render_template(TemplateKind::Mcm);
        assert!(out.contains("\\documentclass[a4paper,11pt]{article}"));
        assert!(out.contains("Team Control Number"));
        assert!(out.contains("\\section{Results}"));
    }

    #[test]
    fn meta_deserializes_other_degrades_to_cumcm() {
        let raw = r#"{
            "title": "T",
            "abstract": "A",
            "competition_type": "other",
            "problem_text": "",
            "sections": [],
            "references": [],
            "figures": []
        }"#;
        let meta: PaperMeta = serde_json::from_str(raw).unwrap();
        assert_eq!(
            TemplateKind::from_competition(meta.competition_type.as_deref()),
            TemplateKind::Cumcm
        );
    }
}
