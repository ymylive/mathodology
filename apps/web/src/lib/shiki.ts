// Lazy, singleton syntax-highlighter for Python source in CoderOutputView.
//
// Notes on bundle strategy:
// - All shiki imports are dynamic so Vite code-splits the engine, wasm, theme,
//   and grammar out of the initial chunk. The first call pays ~150 kB gzip
//   (wasm + engine + python.tmLanguage + github-dark), subsequent calls are
//   ~0-cost because the highlighter is cached on `highlighterPromise`.
// - We pin to ONE theme and ONE language on purpose — adding more roughly
//   doubles the fetched payload per extra grammar.
// - We only lazy-import from `shiki/core` (not `shiki`) so the heavyweight
//   "bundle-web" entry never gets pulled in.
//
// Shiki v4 API: `createHighlighterCore({ themes, langs, engine })` returns a
// `HighlighterCore` with a synchronous `codeToHtml(code, { lang, theme })`.

import type { HighlighterCore, ShikiTransformer } from "shiki/core";

let highlighterPromise: Promise<HighlighterCore> | null = null;

async function getHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = (async () => {
      const [{ createHighlighterCore }, { createOnigurumaEngine }, langPy, themeDark] =
        await Promise.all([
          import("shiki/core"),
          import("shiki/engine/oniguruma"),
          import("shiki/langs/python.mjs").then((m) => m.default),
          import("shiki/themes/github-dark.mjs").then((m) => m.default),
        ]);
      return createHighlighterCore({
        themes: [themeDark],
        langs: [langPy],
        engine: createOnigurumaEngine(import("shiki/wasm")),
      });
    })();
  }
  return highlighterPromise;
}

// Tag the outer <pre> with `shiki-python` so we can target it from
// component-scoped CSS without having to override shiki's own `pre.shiki`.
const addClassTransformer: ShikiTransformer = {
  name: "shiki-python-class",
  pre(node) {
    const prev =
      typeof node.properties["class"] === "string"
        ? (node.properties["class"] as string)
        : "";
    node.properties["class"] = `${prev} shiki-python`.trim();
  },
};

/**
 * Render Python `code` as syntax-highlighted HTML (github-dark theme).
 * Returns the full `<pre class="shiki shiki-python"><code>…</code></pre>`
 * string, safe to drop into `v-html`: shiki sanitizes the input by tokenising
 * against the TextMate grammar (no raw HTML passthrough).
 */
export async function renderPython(code: string): Promise<string> {
  const hl = await getHighlighter();
  return hl.codeToHtml(code, {
    lang: "python",
    theme: "github-dark",
    transformers: [addClassTransformer],
  });
}
