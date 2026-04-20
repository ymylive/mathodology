<script setup lang="ts">
import { computed } from "vue";
import { Marked } from "marked";
import type { Token, Tokens } from "marked";
import { figureUrl } from "@/api/figures";

// Schema-aware renderer for PaperDraft. The run produces a full markdown
// paper; we render it inline (per-section) so the user can read it without
// downloading. LaTeX (`$...$`, `$$...$$`) is intentionally left as literal
// text on M6 — KaTeX integration is deferred to M7.
//
// Image rewrite: markdown like `![caption](figures/fig-0.png)` would produce
// an `<img src="figures/fig-0.png">` which never resolves against the SPA's
// origin. We walk each section's tokens and replace relative `figures/*`
// hrefs with the gateway URL (includes the ?token= dev auth). Absolute URLs
// (http://, https://, protocol-relative //) are left untouched.
//
// Download button: deliberately NOT rendered. M6 does not ship a /runs/:id
// /paper endpoint, so a button would 404. Inline rendering is the M6 UX.
const props = defineProps<{
  output: Record<string, unknown>;
  runId: string;
}>();

interface PaperSection {
  title: string;
  bodyHtml: string;
}

function pickStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}

// `figures/...` or `./figures/...` are relative refs the worker produced.
// Absolute URLs and data: URIs must pass through unchanged.
function isRelativeFigureHref(href: string): boolean {
  if (!href) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return false; // http:, https:, data:, etc.
  if (href.startsWith("//")) return false; // protocol-relative
  if (href.startsWith("/")) return false; // site-absolute
  // Strip a leading "./"
  const normalized = href.startsWith("./") ? href.slice(2) : href;
  return normalized.startsWith("figures/");
}

// Per-render Marked instance: we need the runId closed over in walkTokens,
// and the runId is reactive across renders. Building a fresh instance per
// section keeps the extension simple and is cheap for paper-sized input.
function makeMarked(runId: string): Marked {
  const m = new Marked({
    gfm: true,
    breaks: false,
    walkTokens(token: Token) {
      if (token.type !== "image") return;
      const img = token as Tokens.Image;
      if (isRelativeFigureHref(img.href)) {
        const normalized = img.href.startsWith("./")
          ? img.href.slice(2)
          : img.href;
        img.href = figureUrl(runId, normalized);
      }
    },
  });
  return m;
}

const title = computed<string>(() => {
  const v = props.output["title"];
  return typeof v === "string" ? v : "";
});

const abstract = computed<string>(() => {
  const v = props.output["abstract"];
  return typeof v === "string" ? v : "";
});

const sections = computed<PaperSection[]>(() => {
  const raw = props.output["sections"];
  if (!Array.isArray(raw)) return [];
  const md = makeMarked(props.runId);
  return raw
    .map((item): PaperSection | null => {
      if (!item || typeof item !== "object") return null;
      const rec = item as Record<string, unknown>;
      const secTitle =
        typeof rec["title"] === "string" ? rec["title"] : "";
      const body =
        typeof rec["body_markdown"] === "string" ? rec["body_markdown"] : "";
      if (!secTitle && !body) return null;
      // marked.parse with async:false returns a string synchronously.
      const bodyHtml = md.parse(body, { async: false }) as string;
      return { title: secTitle, bodyHtml };
    })
    .filter((x): x is PaperSection => x !== null);
});

const references = computed<string[]>(() =>
  pickStringArray(props.output["references"]),
);

const figureRefs = computed<string[]>(() =>
  pickStringArray(props.output["figure_refs"]),
);

function figUrlFor(relPath: string): string {
  const normalized = relPath.startsWith("./") ? relPath.slice(2) : relPath;
  return figureUrl(props.runId, normalized);
}

function onFigureError(ev: Event) {
  const img = ev.target as HTMLImageElement;
  img.style.display = "none";
  const parent = img.parentElement;
  if (parent && !parent.querySelector(".fig-placeholder")) {
    const ph = document.createElement("span");
    ph.className =
      "fig-placeholder mono text-[10px] text-neutral-500 inline-flex items-center justify-center w-[120px] h-[90px] border border-dashed border-neutral-700 rounded";
    ph.textContent = "404";
    parent.appendChild(ph);
  }
}
</script>

<template>
  <article class="space-y-6 text-sm text-neutral-200 paper-body">
    <!-- Title -->
    <h1
      v-if="title"
      class="text-2xl font-medium text-neutral-100 leading-tight"
    >
      {{ title }}
    </h1>

    <!-- Abstract -->
    <div v-if="abstract">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Abstract
      </h4>
      <p class="italic text-neutral-200 leading-relaxed whitespace-pre-wrap">
        {{ abstract }}
      </p>
    </div>

    <!-- Sections -->
    <section v-for="(s, i) in sections" :key="i" class="space-y-2">
      <h2
        v-if="s.title"
        class="text-lg font-medium text-neutral-100 border-b border-neutral-800 pb-1"
      >
        {{ s.title }}
      </h2>
      <!-- marked v14 escapes HTML by default; inputs come from our own
           Writer agent so XSS exposure is low. No DOMPurify per brief. -->
      <div class="markdown-body leading-relaxed" v-html="s.bodyHtml" />
    </section>

    <!-- References -->
    <div v-if="references.length > 0">
      <h2
        class="text-lg font-medium text-neutral-100 border-b border-neutral-800 pb-1 mb-2"
      >
        References
      </h2>
      <ol class="list-decimal list-inside space-y-1 marker:text-neutral-500">
        <li
          v-for="(r, i) in references"
          :key="i"
          class="leading-relaxed text-neutral-300"
        >
          {{ r }}
        </li>
      </ol>
    </div>

    <!-- Figures gallery -->
    <div v-if="figureRefs.length > 0">
      <h2
        class="text-lg font-medium text-neutral-100 border-b border-neutral-800 pb-1 mb-2"
      >
        Figures
      </h2>
      <div class="flex flex-wrap gap-3">
        <a
          v-for="(fpath, i) in figureRefs"
          :key="fpath"
          :href="figUrlFor(fpath)"
          target="_blank"
          rel="noopener"
          class="block rounded border border-neutral-800 bg-neutral-950/60 overflow-hidden hover:border-sky-700"
        >
          <img
            :src="figUrlFor(fpath)"
            :alt="`Figure ${i + 1}`"
            class="block w-[200px] h-[150px] object-contain bg-neutral-950"
            loading="lazy"
            @error="onFigureError"
          />
        </a>
      </div>
    </div>

    <!-- Download: intentionally omitted for M6 — the /paper endpoint is
         not implemented yet. Revisit when M7+ backend lands. -->
  </article>
</template>

<style scoped>
/* Scoped styles for rendered markdown. Tailwind's `prose` plugin isn't
   installed and adding it would balloon bundle size; these selectors target
   the v-html output directly. */
.markdown-body :deep(h1) {
  font-size: 1.25rem;
  color: var(--tw-prose-headings, #e5e5e5);
  margin-top: 1rem;
  margin-bottom: 0.5rem;
}
.markdown-body :deep(h2) {
  font-size: 1.125rem;
  color: #e5e5e5;
  margin-top: 0.75rem;
  margin-bottom: 0.5rem;
}
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  font-size: 1rem;
  color: #e5e5e5;
  margin-top: 0.5rem;
  margin-bottom: 0.25rem;
}
.markdown-body :deep(p) {
  margin-bottom: 0.75rem;
  color: #d4d4d4;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin-left: 1.25rem;
  margin-bottom: 0.75rem;
  color: #d4d4d4;
}
.markdown-body :deep(ul) {
  list-style: disc;
}
.markdown-body :deep(ol) {
  list-style: decimal;
}
.markdown-body :deep(li) {
  margin-bottom: 0.125rem;
}
.markdown-body :deep(code) {
  font-family:
    ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, monospace;
  font-size: 0.85em;
  background: rgba(38, 38, 38, 0.8);
  border: 1px solid #262626;
  border-radius: 0.25rem;
  padding: 0.0625rem 0.25rem;
  color: #e5e5e5;
}
.markdown-body :deep(pre) {
  background: rgba(10, 10, 10, 0.8);
  border: 1px solid #262626;
  border-radius: 0.375rem;
  padding: 0.5rem;
  overflow: auto;
  margin-bottom: 0.75rem;
}
.markdown-body :deep(pre code) {
  background: transparent;
  border: none;
  padding: 0;
}
.markdown-body :deep(blockquote) {
  border-left: 2px solid #404040;
  padding-left: 0.75rem;
  color: #a3a3a3;
  font-style: italic;
  margin-bottom: 0.75rem;
}
.markdown-body :deep(a) {
  color: #7dd3fc;
  text-decoration: underline;
  text-decoration-color: rgba(125, 211, 252, 0.4);
}
.markdown-body :deep(a:hover) {
  text-decoration-color: #7dd3fc;
}
.markdown-body :deep(img) {
  max-width: 100%;
  border-radius: 0.375rem;
  border: 1px solid #262626;
  margin: 0.5rem 0;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin-bottom: 0.75rem;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #262626;
  padding: 0.25rem 0.5rem;
  text-align: left;
}
.markdown-body :deep(th) {
  background: rgba(23, 23, 23, 0.6);
  font-weight: 500;
}
.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid #262626;
  margin: 1rem 0;
}
</style>
