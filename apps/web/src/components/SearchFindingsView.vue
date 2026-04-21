<script setup lang="ts">
import { computed, ref } from "vue";
import { Badge } from "@/components/ui/badge";
import {
  Database,
  ExternalLink,
  FileSearch,
} from "lucide-vue-next";

// Schema-aware renderer for SearchFindings. Payload is loosely typed as
// `Record<string, unknown>` (the store hands it through generically) so we
// narrow each field defensively — a malformed event degrades to an empty
// section rather than crashing the card.
//
// Layout mirrors the other output views (dark aesthetic, uppercase section
// headers, neutral palette) so this slot feels continuous with Analyzer /
// ModelSpec / CoderOutput / PaperDraft.
const props = defineProps<{
  output: Record<string, unknown>;
  runId?: string;
}>();

void props.runId; // reserved for future link-back UX; not used yet.

interface Paper {
  title: string;
  authors: string[];
  abstract: string;
  url: string;
  arxivId: string | null;
  published: string | null;
  publishedYear: string | null;
  relevanceReason: string | null;
}

function pickStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}

const queries = computed<string[]>(() =>
  pickStringArray(props.output["queries"]),
);

const keyFindings = computed<string[]>(() =>
  pickStringArray(props.output["key_findings"]),
);

const datasets = computed<string[]>(() =>
  pickStringArray(props.output["datasets_mentioned"]),
);

const papers = computed<Paper[]>(() => {
  const raw = props.output["papers"];
  if (!Array.isArray(raw)) return [];
  const list: Paper[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    const title = typeof rec["title"] === "string" ? rec["title"] : "";
    const url = typeof rec["url"] === "string" ? rec["url"] : "";
    const abstract =
      typeof rec["abstract"] === "string" ? rec["abstract"] : "";
    const authors = pickStringArray(rec["authors"]);
    const arxivIdRaw = rec["arxiv_id"];
    const arxivId =
      typeof arxivIdRaw === "string" && arxivIdRaw.length > 0
        ? arxivIdRaw
        : null;
    const publishedRaw = rec["published"];
    const published =
      typeof publishedRaw === "string" && publishedRaw.length > 0
        ? publishedRaw
        : null;
    const publishedYear = published ? published.slice(0, 4) : null;
    const reasonRaw = rec["relevance_reason"];
    const relevanceReason =
      typeof reasonRaw === "string" && reasonRaw.length > 0 ? reasonRaw : null;
    // Drop rows with no identifying content — an entirely empty paper object
    // isn't worth rendering.
    if (!title && !url && !abstract && authors.length === 0) continue;
    list.push({
      title,
      authors,
      abstract,
      url,
      arxivId,
      published,
      publishedYear,
      relevanceReason,
    });
  }
  // Sort newest first when publication dates are available, otherwise fall
  // back to the backend's triage order. Stable sort so papers without dates
  // keep their relative order.
  const hasAnyDate = list.some((p) => p.published !== null);
  if (!hasAnyDate) return list;
  return list.slice().sort((a, b) => {
    if (a.published === null && b.published === null) return 0;
    if (a.published === null) return 1;
    if (b.published === null) return -1;
    return b.published.localeCompare(a.published);
  });
});

// Each paper's abstract is line-clamped by default; clicking the row toggles
// the full text. Keyed by the paper's array index in the computed list so
// sort-stable expansion survives re-renders.
const expanded = ref<Set<number>>(new Set());

function toggleAbstract(i: number) {
  const next = new Set(expanded.value);
  if (next.has(i)) next.delete(i);
  else next.add(i);
  expanded.value = next;
}

function authorsLabel(authors: string[]): string {
  if (authors.length === 0) return "";
  if (authors.length <= 3) return authors.join(", ");
  return `${authors.slice(0, 3).join(", ")}, …`;
}

const hasAnyContent = computed(
  () =>
    queries.value.length > 0 ||
    keyFindings.value.length > 0 ||
    datasets.value.length > 0 ||
    papers.value.length > 0,
);
</script>

<template>
  <div class="space-y-4 text-sm text-neutral-200">
    <!-- Completely empty payload: show a single muted line so the card
         doesn't render as a void. -->
    <p v-if="!hasAnyContent" class="text-neutral-500 italic">
      No search findings emitted for this run.
    </p>

    <!-- Queries: compact inline badge list so the reader sees exactly what
         was asked of arXiv before skimming the hits. -->
    <div v-if="queries.length > 0">
      <h4
        class="text-xs uppercase tracking-wider text-neutral-500 mb-1 flex items-center gap-1.5"
      >
        <FileSearch class="h-3.5 w-3.5" aria-hidden="true" />
        <span>Searched for</span>
      </h4>
      <div class="flex flex-wrap gap-1.5">
        <Badge
          v-for="(q, i) in queries"
          :key="i"
          variant="outline"
          class="mono text-[11px] py-0 px-1.5 font-normal border-neutral-800 bg-neutral-900/60 text-neutral-300"
        >
          {{ q }}
        </Badge>
      </div>
    </div>

    <!-- Key findings: Writer cites these, so make them prominent. -->
    <div v-if="keyFindings.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Key findings
      </h4>
      <ul
        class="space-y-1 rounded-md border border-neutral-800 bg-neutral-900/40 p-3 list-disc list-inside marker:text-neutral-500"
      >
        <li
          v-for="(f, i) in keyFindings"
          :key="i"
          class="leading-relaxed text-neutral-100"
        >
          {{ f }}
        </li>
      </ul>
    </div>

    <!-- Datasets: one chip per dataset name. Horizontal wrap, no extra
         styling — the icon alone cues the section. -->
    <div v-if="datasets.length > 0">
      <h4
        class="text-xs uppercase tracking-wider text-neutral-500 mb-1 flex items-center gap-1.5"
      >
        <Database class="h-3.5 w-3.5" aria-hidden="true" />
        <span>Datasets mentioned</span>
      </h4>
      <div class="flex flex-wrap gap-1.5">
        <Badge
          v-for="(d, i) in datasets"
          :key="i"
          variant="outline"
          class="text-[11px] py-0 px-1.5 font-normal border-neutral-800 bg-neutral-900/60 text-neutral-200"
        >
          {{ d }}
        </Badge>
      </div>
    </div>

    <!-- Papers: the core artifact. -->
    <div v-if="queries.length > 0 || papers.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Papers
        <span
          v-if="papers.length > 0"
          class="mono text-[11px] normal-case text-neutral-600 ml-1"
        >
          ({{ papers.length }})
        </span>
      </h4>

      <p v-if="papers.length === 0" class="text-neutral-500 italic">
        No papers returned for these queries.
      </p>

      <ul v-else class="space-y-2">
        <li
          v-for="(p, i) in papers"
          :key="p.url || p.arxivId || i"
          class="rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2"
        >
          <!-- Title row: link + year + arxiv id. The header wraps on
               narrow columns so nothing overflows. -->
          <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <a
              v-if="p.url"
              :href="p.url"
              target="_blank"
              rel="noopener"
              class="text-neutral-100 font-medium hover:text-sky-300 inline-flex items-baseline gap-1 break-words"
            >
              <span>{{ p.title || p.url }}</span>
              <ExternalLink
                class="h-3 w-3 shrink-0 self-center text-neutral-500"
                aria-hidden="true"
              />
            </a>
            <span
              v-else
              class="text-neutral-100 font-medium break-words"
            >
              {{ p.title || "—" }}
            </span>
            <Badge
              v-if="p.publishedYear"
              variant="outline"
              class="mono text-[10px] py-0 px-1.5 font-normal text-neutral-400 border-neutral-800 tabular-nums"
            >
              {{ p.publishedYear }}
            </Badge>
            <Badge
              v-if="p.arxivId"
              variant="outline"
              class="mono text-[10px] py-0 px-1.5 font-normal text-neutral-400 border-neutral-800"
            >
              {{ p.arxivId }}
            </Badge>
          </div>

          <!-- Authors: short byline, muted. -->
          <p
            v-if="p.authors.length > 0"
            class="text-[12px] text-neutral-400 mt-0.5"
          >
            {{ authorsLabel(p.authors) }}
          </p>

          <!-- Abstract: clamped to 2 lines; click to expand. Using a
               <button> for a11y — the click target is the text itself,
               which matches the reader's expectation. -->
          <button
            v-if="p.abstract"
            type="button"
            :class="[
              'mt-1 block w-full text-left text-[13px] leading-relaxed text-neutral-300 hover:text-neutral-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-800 rounded',
              expanded.has(i) ? '' : 'abstract-clamp-2',
            ]"
            :aria-expanded="expanded.has(i)"
            @click="toggleAbstract(i)"
          >
            {{ p.abstract }}
          </button>

          <!-- Relevance reason: italic, separated from the abstract so
               scan-ability is the dominant read of the list. -->
          <p
            v-if="p.relevanceReason"
            class="italic text-[12px] text-sky-300/90 leading-snug mt-1"
          >
            {{ p.relevanceReason }}
          </p>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
/* Tailwind v4 doesn't ship `line-clamp-*` utilities without the plugin,
   and `@tailwindcss/line-clamp` isn't installed — so apply the standard
   three-property clamp directly. Two lines matches the brief. */
.abstract-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  line-clamp: 2;
  overflow: hidden;
}
</style>
