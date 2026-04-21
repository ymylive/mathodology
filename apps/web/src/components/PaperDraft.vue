<script setup lang="ts">
// PaperDraft: full-width draft card rendered under the 2-col grid.
// Markdown bodies are parsed with `marked`, then KaTeX-substituted, then
// passed through `v-html`. Relative `figures/...` image refs are rewritten
// to the gateway URL (with Bearer token query param).
import { computed } from "vue";
import { Marked } from "marked";
import type { Token, Tokens } from "marked";
import { figureUrl, notebookUrl, paperUrl } from "@/api/figures";
import { substituteMath } from "@/lib/render-math";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown>; runId: string }>();

interface Section {
  title: string;
  bodyHtml: string;
}

function isRelativeFigureHref(href: string): boolean {
  if (!href) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return false;
  if (href.startsWith("//")) return false;
  if (href.startsWith("/")) return false;
  const n = href.startsWith("./") ? href.slice(2) : href;
  return n.startsWith("figures/");
}

function makeMarked(runId: string): Marked {
  return new Marked({
    gfm: true,
    breaks: false,
    walkTokens(token: Token) {
      if (token.type !== "image") return;
      const img = token as Tokens.Image;
      if (isRelativeFigureHref(img.href)) {
        const n = img.href.startsWith("./") ? img.href.slice(2) : img.href;
        img.href = figureUrl(runId, n);
      }
    },
  });
}

const title = computed<string>(() => {
  const v = props.output["title"];
  return typeof v === "string" ? v : "";
});

const abstract = computed<string>(() => {
  const v = props.output["abstract"];
  return typeof v === "string" ? v : "";
});

const abstractHtml = computed<string>(() => {
  const raw = abstract.value;
  if (!raw) return "";
  const escaped = raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return substituteMath(escaped);
});

const sections = computed<Section[]>(() => {
  const raw = props.output["sections"];
  if (!Array.isArray(raw)) return [];
  const md = makeMarked(props.runId);
  return raw
    .map((item): Section | null => {
      if (!item || typeof item !== "object") return null;
      const r = item as Record<string, unknown>;
      const t = typeof r["title"] === "string" ? r["title"] : "";
      const b = typeof r["body_markdown"] === "string" ? r["body_markdown"] : "";
      if (!t && !b) return null;
      const rawHtml = md.parse(b, { async: false }) as string;
      return { title: t, bodyHtml: substituteMath(rawHtml) };
    })
    .filter((x): x is Section => x !== null);
});

const references = computed<string[]>(() => {
  const r = props.output["references"];
  if (!Array.isArray(r)) return [];
  return r.filter((x): x is string => typeof x === "string" && x.length > 0);
});
</script>

<template>
  <article class="paper-draft">
    <h1 v-if="title">{{ title }}</h1>

    <div v-if="abstract" class="abstract markdown-body" v-html="abstractHtml"></div>

    <section v-for="(s, i) in sections" :key="i">
      <h2 v-if="s.title">{{ s.title }}</h2>
      <div class="markdown-body" v-html="s.bodyHtml"></div>
    </section>

    <section v-if="references.length > 0">
      <h2><T en="References" zh="参考文献" /></h2>
      <ol>
        <li v-for="(r, i) in references" :key="i">{{ r }}</li>
      </ol>
    </section>

    <div style="display:flex; gap:10px; margin-top: 18px;">
      <a
        class="btn ghost"
        :href="notebookUrl(runId)"
        target="_blank"
        rel="noopener"
      >
        <T en="Download notebook" zh="下载 Notebook" /> →
      </a>
      <a
        class="btn ghost"
        :href="paperUrl(runId)"
        target="_blank"
        rel="noopener"
      >
        <T en="Download paper" zh="下载论文" /> →
      </a>
    </div>
  </article>
</template>
