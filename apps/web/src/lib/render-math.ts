// KaTeX rendering helpers for the Mathodology web app.
//
// Design goals (M7 Stream B):
//   - Render inline / display LaTeX from raw expressions (used by ModelSpec
//     equations[].latex and variables[].symbol).
//   - Post-process marked-rendered HTML to replace `$...$` and `$$...$$`
//     with KaTeX output, while leaving code / pre / script / style spans
//     alone so code samples that contain a literal `$` don't get mangled.
//
// We deliberately avoid pulling in a full HTML parser (cheerio / parse5):
// the input is markdown output we produced ourselves, so a tiny tag-aware
// scanner over the flat string is sufficient and keeps the bundle lean.

import katex from "katex";

export function renderInline(expr: string): string {
  try {
    return katex.renderToString(expr, {
      throwOnError: false,
      displayMode: false,
      strict: "ignore",
      output: "html",
    });
  } catch {
    return escapeHtml(expr);
  }
}

export function renderDisplay(expr: string): string {
  try {
    return katex.renderToString(expr, {
      throwOnError: false,
      displayMode: true,
      strict: "ignore",
      output: "html",
    });
  } catch {
    return escapeHtml(expr);
  }
}

// Tags whose textual content must NOT be touched by math substitution.
// Case-insensitive; closing tags matched against the same set.
const SKIP_TAGS = new Set(["code", "pre", "script", "style"]);

/**
 * Replace `$$...$$` and `$...$` in a markdown-rendered HTML string with
 * KaTeX output. Runs AFTER marked — callers do:
 *
 *   substituteMath(marked.parse(body))
 *
 * Tag-awareness: the scanner splits the input into alternating HTML-tag
 * spans and text spans. Text spans inside `<pre>`, `<code>`, `<script>`
 * or `<style>` are passed through unchanged. All other text spans get
 * math substitution applied.
 */
export function substituteMath(html: string): string {
  if (!html || (html.indexOf("$") === -1)) return html;

  const out: string[] = [];
  const skipStack: string[] = []; // lowercased tag names currently suppressing math

  let i = 0;
  const len = html.length;
  let textStart = 0;

  const flushText = (end: number) => {
    if (end <= textStart) return;
    const slice = html.slice(textStart, end);
    if (skipStack.length > 0) {
      out.push(slice);
    } else {
      out.push(applyMath(slice));
    }
  };

  while (i < len) {
    const ch = html.charCodeAt(i);
    if (ch !== 60 /* '<' */) {
      i++;
      continue;
    }

    // A `<` in HTML could be the start of a tag, a comment, CDATA, or a
    // stray `<`. Peek ahead to decide.
    const next = i + 1 < len ? html[i + 1] : "";

    // Comment `<!-- ... -->`
    if (next === "!" && html.startsWith("<!--", i)) {
      flushText(i);
      const end = html.indexOf("-->", i + 4);
      const stop = end === -1 ? len : end + 3;
      out.push(html.slice(i, stop));
      i = stop;
      textStart = i;
      continue;
    }

    // Tag start (open or close). A valid tag name starts with a letter or `/`.
    const isClose = next === "/";
    const nameStart = isClose ? i + 2 : i + 1;
    const nameChar0 = html.charCodeAt(nameStart);
    const isLetter =
      (nameChar0 >= 65 && nameChar0 <= 90) ||
      (nameChar0 >= 97 && nameChar0 <= 122);
    if (!isLetter) {
      // Not a tag — just a literal `<`. Treat as plain text.
      i++;
      continue;
    }

    // Scan the tag name.
    let j = nameStart;
    while (j < len) {
      const c = html.charCodeAt(j);
      const letter = (c >= 65 && c <= 90) || (c >= 97 && c <= 122);
      const digit = c >= 48 && c <= 57;
      if (!letter && !digit) break;
      j++;
    }
    const tagName = html.slice(nameStart, j).toLowerCase();

    // Find the end of the tag. For `<script>` / `<style>` we still need to
    // close the opening tag normally; the raw-text handling happens when we
    // push onto skipStack below.
    const tagEnd = html.indexOf(">", j);
    if (tagEnd === -1) {
      // Malformed — bail: flush the rest as text (skip-aware).
      flushText(len);
      textStart = len;
      i = len;
      break;
    }

    // Flush preceding text span before emitting the tag verbatim.
    flushText(i);
    out.push(html.slice(i, tagEnd + 1));

    // Self-closing? (`<br/>`, `<img ... />`, also HTML void elements.)
    const selfClosing = html.charCodeAt(tagEnd - 1) === 47 /* '/' */;

    if (!selfClosing && SKIP_TAGS.has(tagName)) {
      if (isClose) {
        // Pop matching skip tag if present.
        for (let k = skipStack.length - 1; k >= 0; k--) {
          if (skipStack[k] === tagName) {
            skipStack.splice(k, 1);
            break;
          }
        }
      } else {
        skipStack.push(tagName);
      }
    }

    i = tagEnd + 1;
    textStart = i;
  }

  flushText(len);
  return out.join("");
}

// --- internals -------------------------------------------------------------

// Apply display + inline math substitution to a plain text span. The text
// is presumed to already be HTML-escaped (marked escapes `<`/`>`/`&`), so
// KaTeX output (which contains `<span>`s) is safe to splice in.
function applyMath(text: string): string {
  // Display math first — longer delimiter, must not be swallowed by the
  // inline pass. Non-greedy, multi-line.
  let withDisplay = text.replace(
    /\$\$([\s\S]+?)\$\$/g,
    (_m, expr: string) => renderDisplay(expr),
  );

  // Inline math. Single-line; allow `\$` escaped dollars inside. Leading
  // negative look-behind keeps an escaped `\$...` from opening a math span.
  // We pass `\$` through to KaTeX unchanged — KaTeX renders `\$` as a dollar
  // glyph, which matches LaTeX conventions and avoids mangling prose.
  withDisplay = withDisplay.replace(
    /(?<!\\)\$((?:\\\$|[^$\n])+?)\$/g,
    (m, expr: string) => {
      // Defensive: if the expression ends with an unpaired backslash the
      // match is ambiguous — leave the source text alone.
      if (expr.endsWith("\\")) return m;
      return renderInline(expr);
    },
  );

  return withDisplay;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
