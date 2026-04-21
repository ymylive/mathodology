<script setup lang="ts">
// SearchFindings: a list of arXiv papers with abstracts + external links.
// Queries / datasets / key findings are optional headline chips.
import { computed } from "vue";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown> }>();

interface Paper {
  title: string;
  authors: string[];
  abstract: string;
  url: string;
  arxivId: string | null;
  year: string | null;
}

function pickStrings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}

const queries = computed<string[]>(() => pickStrings(props.output["queries"]));

const keyFindings = computed<string[]>(() =>
  pickStrings(props.output["key_findings"]),
);

const papers = computed<Paper[]>(() => {
  const raw = props.output["papers"];
  if (!Array.isArray(raw)) return [];
  const out: Paper[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const r = item as Record<string, unknown>;
    const title = typeof r["title"] === "string" ? r["title"] : "";
    const url = typeof r["url"] === "string" ? r["url"] : "";
    const abstract = typeof r["abstract"] === "string" ? r["abstract"] : "";
    const authors = pickStrings(r["authors"]);
    const arxiv = typeof r["arxiv_id"] === "string" && r["arxiv_id"].length > 0
      ? (r["arxiv_id"] as string)
      : null;
    const pub = typeof r["published"] === "string" ? r["published"] : null;
    const year = pub ? pub.slice(0, 4) : null;
    if (!title && !url && !abstract) continue;
    out.push({ title, authors, abstract, url, arxivId: arxiv, year });
  }
  return out;
});

function authorsLabel(authors: string[]): string {
  if (authors.length === 0) return "";
  if (authors.length <= 3) return authors.join(", ");
  return `${authors.slice(0, 3).join(", ")}, …`;
}
</script>

<template>
  <div class="output-panel">
    <div v-if="queries.length > 0">
      <h4><T en="Searched for" zh="搜索关键词" /></h4>
      <div style="display:flex; flex-wrap: wrap; gap:6px;">
        <span v-for="(q, i) in queries" :key="i" class="chip">{{ q }}</span>
      </div>
    </div>

    <div v-if="keyFindings.length > 0" style="margin-top: 14px;">
      <h4><T en="Key findings" zh="主要发现" /></h4>
      <ul>
        <li v-for="(f, i) in keyFindings" :key="i">{{ f }}</li>
      </ul>
    </div>

    <div v-if="papers.length > 0" style="margin-top: 14px;">
      <h4><T en="Papers" zh="文献" /></h4>
      <div>
        <div v-for="(p, i) in papers" :key="i" class="paper-row">
          <div class="title">
            <a
              v-if="p.url"
              :href="p.url"
              target="_blank"
              rel="noopener"
            >
              {{ p.title || p.url }}
            </a>
            <span v-else>{{ p.title || "—" }}</span>
            <span
              v-if="p.url"
              aria-hidden="true"
              class="mono"
              style="font-size: 10px; color: var(--ink-3);"
            >↗</span>
          </div>
          <div v-if="p.arxivId || p.year || p.authors.length > 0" class="meta">
            <span v-if="p.arxivId" class="arxiv">{{ p.arxivId }}</span>
            <span v-if="p.year">{{ p.year }}</span>
            <span v-if="p.authors.length > 0">{{ authorsLabel(p.authors) }}</span>
          </div>
          <p v-if="p.abstract" class="abs">{{ p.abstract }}</p>
        </div>
      </div>
    </div>
  </div>
</template>
