<script setup lang="ts">
import { computed } from "vue";

// Schema-aware renderer for AnalyzerOutput. The payload is typed loosely
// because the store only guarantees `Record<string, unknown>` — we narrow
// field-by-field and render defensively so a malformed event doesn't crash
// the card.
const props = defineProps<{
  output: Record<string, unknown>;
}>();

interface DataRequirement {
  name: string;
  description: string;
  sourceHint: string | null;
}

interface ProposedApproach {
  name: string;
  rationale: string;
  methods: string[];
}

const restatedProblem = computed<string>(() => {
  const v = props.output["restated_problem"];
  return typeof v === "string" ? v : "";
});

const subQuestions = computed<string[]>(() =>
  pickStringArray(props.output["sub_questions"]),
);

const assumptions = computed<string[]>(() =>
  pickStringArray(props.output["assumptions"]),
);

const dataRequirements = computed<DataRequirement[]>(() => {
  const raw = props.output["data_requirements"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const rec = item as Record<string, unknown>;
      const name = typeof rec["name"] === "string" ? rec["name"] : "";
      const description =
        typeof rec["description"] === "string" ? rec["description"] : "";
      const hint = rec["source_hint"];
      const sourceHint = typeof hint === "string" && hint.length > 0 ? hint : null;
      if (!name && !description) return null;
      return { name, description, sourceHint } satisfies DataRequirement;
    })
    .filter((x): x is DataRequirement => x !== null);
});

const proposedApproaches = computed<ProposedApproach[]>(() => {
  const raw = props.output["proposed_approaches"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const rec = item as Record<string, unknown>;
      const name = typeof rec["name"] === "string" ? rec["name"] : "";
      const rationale =
        typeof rec["rationale"] === "string" ? rec["rationale"] : "";
      const methods = pickStringArray(rec["methods"]);
      if (!name && !rationale && methods.length === 0) return null;
      return { name, rationale, methods } satisfies ProposedApproach;
    })
    .filter((x): x is ProposedApproach => x !== null);
});

function pickStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}
</script>

<template>
  <div class="space-y-4 text-sm text-neutral-200">
    <!-- Restated problem -->
    <div v-if="restatedProblem">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Restated problem
      </h4>
      <p class="italic text-neutral-100 leading-relaxed whitespace-pre-wrap">
        {{ restatedProblem }}
      </p>
    </div>

    <!-- Sub-questions -->
    <div v-if="subQuestions.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Sub-questions
      </h4>
      <ol class="list-decimal list-inside space-y-1 marker:text-neutral-500">
        <li v-for="(q, i) in subQuestions" :key="i" class="leading-relaxed">
          {{ q }}
        </li>
      </ol>
    </div>

    <!-- Assumptions (hidden if empty) -->
    <div v-if="assumptions.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Assumptions
      </h4>
      <ul class="list-disc list-inside space-y-1 marker:text-neutral-500">
        <li v-for="(a, i) in assumptions" :key="i" class="leading-relaxed">
          {{ a }}
        </li>
      </ul>
    </div>

    <!-- Data requirements -->
    <div v-if="dataRequirements.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Data requirements
      </h4>
      <ul class="space-y-1">
        <li
          v-for="(d, i) in dataRequirements"
          :key="i"
          class="leading-relaxed"
        >
          <span class="text-neutral-100 font-medium">{{ d.name }}</span>
          <span v-if="d.description" class="text-neutral-300">
            <span class="text-neutral-600"> — </span>{{ d.description }}
          </span>
          <span
            v-if="d.sourceHint"
            class="mono text-[11px] text-neutral-400 ml-2"
          >
            [{{ d.sourceHint }}]
          </span>
        </li>
      </ul>
    </div>

    <!-- Proposed approaches -->
    <div v-if="proposedApproaches.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-2">
        Proposed approaches
      </h4>
      <div class="space-y-2">
        <div
          v-for="(a, i) in proposedApproaches"
          :key="i"
          class="rounded-md border border-neutral-800 bg-neutral-900/60 p-3"
        >
          <h5 v-if="a.name" class="text-sm text-neutral-100 font-medium mb-1">
            {{ a.name }}
          </h5>
          <p
            v-if="a.rationale"
            class="text-sm text-neutral-300 leading-relaxed mb-2"
          >
            {{ a.rationale }}
          </p>
          <div v-if="a.methods.length > 0" class="flex flex-wrap gap-1">
            <span
              v-for="(m, j) in a.methods"
              :key="j"
              class="mono text-[11px] px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-300 bg-neutral-950/60"
            >
              {{ m }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
