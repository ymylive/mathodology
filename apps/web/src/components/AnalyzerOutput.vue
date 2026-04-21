<script setup lang="ts">
// AnalyzerOutput renderer. Schema fields we display:
//   - restated_problem   (italic serif block quote)
//   - sub_questions      (ordered list)
//   - proposed_approaches[{ name, rationale, methods[] }]  (mini-cards)
//
// Each field is optional; missing ones just hide that section.
import { computed } from "vue";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown> }>();

interface Approach {
  name: string;
  rationale: string;
  methods: string[];
}

function pickStrings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}

const restated = computed<string>(() => {
  const v = props.output["restated_problem"];
  return typeof v === "string" ? v : "";
});

const subQuestions = computed<string[]>(() =>
  pickStrings(props.output["sub_questions"]),
);

const approaches = computed<Approach[]>(() => {
  const raw = props.output["proposed_approaches"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item): Approach | null => {
      if (!item || typeof item !== "object") return null;
      const rec = item as Record<string, unknown>;
      const name = typeof rec["name"] === "string" ? rec["name"] : "";
      const rationale =
        typeof rec["rationale"] === "string" ? rec["rationale"] : "";
      const methods = pickStrings(rec["methods"]);
      if (!name && !rationale && methods.length === 0) return null;
      return { name, rationale, methods };
    })
    .filter((x): x is Approach => x !== null);
});
</script>

<template>
  <div class="output-panel">
    <div v-if="restated">
      <h4><T en="Restated problem" zh="题目重述" /></h4>
      <p class="restated">{{ restated }}</p>
    </div>

    <div v-if="subQuestions.length > 0">
      <h4><T en="Sub-questions" zh="子问题" /></h4>
      <ol>
        <li v-for="(q, i) in subQuestions" :key="i">{{ q }}</li>
      </ol>
    </div>

    <div v-if="approaches.length > 0">
      <h4><T en="Proposed approaches" zh="候选方案" /></h4>
      <div
        v-for="(a, i) in approaches"
        :key="i"
        class="approach-card"
      >
        <div v-if="a.name" class="name">{{ a.name }}</div>
        <p v-if="a.rationale" class="rationale">{{ a.rationale }}</p>
        <div v-if="a.methods.length > 0" style="display:flex; flex-wrap: wrap; gap: 6px;">
          <span v-for="(m, j) in a.methods" :key="j" class="chip">{{ m }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
