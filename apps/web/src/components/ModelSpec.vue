<script setup lang="ts">
// ModelSpec: chosen_approach, variables table, equations (KaTeX display),
// algorithm outline, validation strategy, and HMML consulted-methods list.
import { computed } from "vue";
import { renderDisplay, renderInline } from "@/lib/render-math";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown> }>();

interface Variable {
  symbol: string;
  name: string;
  unit: string | null;
  description: string;
}
interface Equation {
  latex: string;
  description: string;
}
interface ConsultedMethod {
  id: string;
  name: string;
  reason: string;
}

function pickStrings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}

const chosenApproach = computed<string>(() => {
  const v = props.output["chosen_approach"];
  return typeof v === "string" ? v : "";
});

const rationale = computed<string>(() => {
  const v = props.output["rationale"];
  return typeof v === "string" ? v : "";
});

const variables = computed<Variable[]>(() => {
  const raw = props.output["variables"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item): Variable | null => {
      if (!item || typeof item !== "object") return null;
      const r = item as Record<string, unknown>;
      const symbol = typeof r["symbol"] === "string" ? r["symbol"] : "";
      const name = typeof r["name"] === "string" ? r["name"] : "";
      const unitRaw = r["unit"];
      const unit = typeof unitRaw === "string" && unitRaw.length > 0 ? unitRaw : null;
      const description =
        typeof r["description"] === "string" ? r["description"] : "";
      if (!symbol && !name && !description) return null;
      return { symbol, name, unit, description };
    })
    .filter((x): x is Variable => x !== null);
});

const equations = computed<Equation[]>(() => {
  const raw = props.output["equations"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item): Equation | null => {
      if (!item || typeof item !== "object") return null;
      const r = item as Record<string, unknown>;
      const latex = typeof r["latex"] === "string" ? r["latex"] : "";
      const description =
        typeof r["description"] === "string" ? r["description"] : "";
      if (!latex && !description) return null;
      return { latex, description };
    })
    .filter((x): x is Equation => x !== null);
});

const algorithm = computed<string[]>(() =>
  pickStrings(props.output["algorithm_outline"]),
);

const validation = computed<string>(() => {
  const v = props.output["validation_strategy"];
  return typeof v === "string" ? v : "";
});

const consultedMethods = computed<ConsultedMethod[]>(() => {
  const raw = props.output["consulted_methods"];
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item): ConsultedMethod | null => {
      if (!item || typeof item !== "object") return null;
      const r = item as Record<string, unknown>;
      const id = typeof r["id"] === "string" ? r["id"] : "";
      const name = typeof r["name"] === "string" ? r["name"] : "";
      const reason = typeof r["reason"] === "string" ? r["reason"] : "";
      if (!id && !name && !reason) return null;
      return { id, name, reason };
    })
    .filter((x): x is ConsultedMethod => x !== null);
});

// Keyword-based tone classification for the consulted-methods rows. Same
// scheme used by the previous implementation — green for selected,
// navy for hybrid, muted for rejected.
type Tone = "primary" | "hybrid" | "rejected" | "default";
function tone(reason: string): Tone {
  const r = reason.toLowerCase();
  if (/\b(selected|primary|chosen)\b/.test(r)) return "primary";
  if (/\b(hybrid|partial|partially|combined)\b/.test(r)) return "hybrid";
  if (/\b(inferior|unsuitable|not\b|rejected|discarded)\b/.test(r)) return "rejected";
  return "default";
}
</script>

<template>
  <div class="output-panel">
    <div v-if="chosenApproach || rationale">
      <h4><T en="Chosen approach" zh="选定方案" /></h4>
      <div v-if="chosenApproach" style="margin-bottom: 8px;">
        <span class="chip" style="background: var(--paper); border-color: var(--rule);">
          {{ chosenApproach }}
        </span>
      </div>
      <p
        v-if="rationale"
        style="font-size: 14px; line-height: 1.55; color: var(--ink-2); white-space: pre-wrap;"
      >
        {{ rationale }}
      </p>
    </div>

    <div v-if="variables.length > 0" style="margin-top: 16px;">
      <h4><T en="Variables" zh="变量" /></h4>
      <table class="var-table">
        <thead>
          <tr>
            <th><T en="Symbol" zh="符号" /></th>
            <th><T en="Name" zh="名称" /></th>
            <th><T en="Unit" zh="单位" /></th>
            <th><T en="Description" zh="说明" /></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(v, i) in variables" :key="i">
            <td v-html="v.symbol ? renderInline(v.symbol) : '&mdash;'"></td>
            <td>{{ v.name }}</td>
            <td class="mono">
              <span v-if="v.unit">{{ v.unit }}</span>
              <span v-else style="color: var(--ink-3);">—</span>
            </td>
            <td>{{ v.description }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="equations.length > 0" style="margin-top: 16px;">
      <h4><T en="Equations" zh="方程" /></h4>
      <div
        v-for="(eq, i) in equations"
        :key="i"
        class="eq-row"
      >
        <div v-if="eq.latex" v-html="renderDisplay(eq.latex)"></div>
        <p v-if="eq.description" class="eq-desc">{{ eq.description }}</p>
      </div>
    </div>

    <div v-if="algorithm.length > 0" style="margin-top: 16px;">
      <h4><T en="Algorithm" zh="算法" /></h4>
      <ol>
        <li v-for="(step, i) in algorithm" :key="i">{{ step }}</li>
      </ol>
    </div>

    <div v-if="validation" style="margin-top: 16px;">
      <h4><T en="Validation" zh="验证" /></h4>
      <p style="font-size: 14px; line-height: 1.55; color: var(--ink-2); white-space: pre-wrap;">
        {{ validation }}
      </p>
    </div>

    <div v-if="consultedMethods.length > 0" style="margin-top: 16px;">
      <h4><T en="Consulted methods · HMML" zh="候选方法 · HMML" /></h4>
      <div>
        <div
          v-for="(m, i) in consultedMethods"
          :key="m.id || i"
          :class="['hmml-row', tone(m.reason)]"
        >
          <div>
            <span class="n">{{ m.name || m.id || "—" }}</span>
            <span v-if="m.id" class="id">{{ m.id }}</span>
          </div>
          <p v-if="m.reason" class="r">{{ m.reason }}</p>
        </div>
      </div>
    </div>
  </div>
</template>
