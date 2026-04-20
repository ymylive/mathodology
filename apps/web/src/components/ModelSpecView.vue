<script setup lang="ts">
import { computed } from "vue";
import { renderDisplay, renderInline } from "@/lib/render-math";
import { Badge } from "@/components/ui/badge";
import { Library } from "lucide-vue-next";

// Schema-aware renderer for ModelSpec. Payload arrives from the store as a
// loose `Record<string, unknown>` — narrow each field defensively so a
// malformed event just hides the affected section rather than crashing the
// card. M7: `equations[].latex` renders as KaTeX display math, and
// `variables[].symbol` renders as KaTeX inline math (symbols are frequently
// compound, e.g. `\lambda_i`).
// M9: adds `consulted_methods` — a list of HMML methods the Modeler
// evaluated before picking its approach. Rendered as a read-at-a-glance
// list below Validation.
const props = defineProps<{
  output: Record<string, unknown>;
}>();

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
      const rec = item as Record<string, unknown>;
      const symbol = typeof rec["symbol"] === "string" ? rec["symbol"] : "";
      const name = typeof rec["name"] === "string" ? rec["name"] : "";
      const unitRaw = rec["unit"];
      const unit =
        typeof unitRaw === "string" && unitRaw.length > 0 ? unitRaw : null;
      const description =
        typeof rec["description"] === "string" ? rec["description"] : "";
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
      const rec = item as Record<string, unknown>;
      const latex = typeof rec["latex"] === "string" ? rec["latex"] : "";
      const description =
        typeof rec["description"] === "string" ? rec["description"] : "";
      if (!latex && !description) return null;
      return { latex, description };
    })
    .filter((x): x is Equation => x !== null);
});

const algorithm = computed<string[]>(() => {
  const raw = props.output["algorithm_outline"];
  if (!Array.isArray(raw)) return [];
  return raw.filter((x): x is string => typeof x === "string" && x.length > 0);
});

const complexity = computed<string | null>(() => {
  const v = props.output["complexity_notes"];
  return typeof v === "string" && v.length > 0 ? v : null;
});

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
      const rec = item as Record<string, unknown>;
      const id = typeof rec["id"] === "string" ? rec["id"] : "";
      const name = typeof rec["name"] === "string" ? rec["name"] : "";
      const reason = typeof rec["reason"] === "string" ? rec["reason"] : "";
      if (!id && !name && !reason) return null;
      return { id, name, reason };
    })
    .filter((x): x is ConsultedMethod => x !== null);
});

// Classify a reason string into a visual "tone" so rows get a meaningful
// left-border accent. Matching is case-insensitive and keyword-based; any
// unrecognised phrasing falls back to the neutral default.
type ReasonTone = "primary" | "rejected" | "hybrid" | "default";

function toneForReason(reason: string): ReasonTone {
  const r = reason.toLowerCase();
  if (/\b(selected|primary|chosen)\b/.test(r)) return "primary";
  if (/\b(hybrid|partial|partially|combined)\b/.test(r)) return "hybrid";
  if (/\b(inferior|unsuitable|not\b|rejected|discarded)\b/.test(r))
    return "rejected";
  return "default";
}

const TONE_CLASS: Record<ReasonTone, string> = {
  // Green accent — matches existing emerald language used elsewhere for
  // "done / success" states without piggybacking on agent-coder's color.
  primary: "border-l-emerald-500/70 bg-emerald-950/20",
  hybrid: "border-l-sky-500/70 bg-sky-950/20",
  rejected: "border-l-neutral-700 bg-neutral-900/40",
  default: "border-l-neutral-800 bg-neutral-900/30",
};
</script>

<template>
  <div class="space-y-4 text-sm text-neutral-200">
    <!-- Chosen approach + rationale -->
    <div v-if="chosenApproach || rationale">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Chosen approach
      </h4>
      <div v-if="chosenApproach" class="mb-2">
        <span
          class="inline-block mono text-[11px] px-2 py-0.5 rounded border border-sky-900 bg-sky-950/60 text-sky-300"
        >
          {{ chosenApproach }}
        </span>
      </div>
      <p
        v-if="rationale"
        class="text-neutral-100 leading-relaxed whitespace-pre-wrap"
      >
        {{ rationale }}
      </p>
    </div>

    <!-- Variables table -->
    <div v-if="variables.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Variables
      </h4>
      <div class="overflow-auto rounded border border-neutral-800">
        <table class="w-full text-sm">
          <thead>
            <tr class="bg-neutral-900/60 text-left text-neutral-400">
              <th class="px-2 py-1 font-normal text-[11px] uppercase tracking-wider">
                Symbol
              </th>
              <th class="px-2 py-1 font-normal text-[11px] uppercase tracking-wider">
                Name
              </th>
              <th class="px-2 py-1 font-normal text-[11px] uppercase tracking-wider">
                Unit
              </th>
              <th class="px-2 py-1 font-normal text-[11px] uppercase tracking-wider">
                Description
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(v, i) in variables"
              :key="i"
              class="border-t border-neutral-800 align-top"
            >
              <td
                class="px-2 py-1 text-neutral-100"
                v-html="v.symbol ? renderInline(v.symbol) : '&mdash;'"
              />
              <td class="px-2 py-1 text-neutral-200">{{ v.name }}</td>
              <td class="px-2 py-1 mono tabular-nums text-neutral-400">
                <span v-if="v.unit">{{ v.unit }}</span>
                <span v-else class="text-neutral-600">—</span>
              </td>
              <td class="px-2 py-1 text-neutral-300 leading-relaxed">
                {{ v.description }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Equations -->
    <div v-if="equations.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Equations
      </h4>
      <div class="space-y-2">
        <div
          v-for="(eq, i) in equations"
          :key="i"
          class="rounded-md border border-neutral-800 bg-neutral-900/40 p-2 space-y-1"
        >
          <div
            v-if="eq.latex"
            class="text-neutral-100 bg-neutral-950/80 rounded border border-neutral-800 p-2 overflow-auto"
            v-html="renderDisplay(eq.latex)"
          />
          <p
            v-if="eq.description"
            class="text-sm text-neutral-300 leading-relaxed"
          >
            {{ eq.description }}
          </p>
        </div>
      </div>
    </div>

    <!-- Algorithm outline -->
    <div v-if="algorithm.length > 0">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Algorithm
      </h4>
      <ol class="list-decimal list-inside space-y-1 marker:text-neutral-500">
        <li
          v-for="(step, i) in algorithm"
          :key="i"
          class="leading-relaxed text-neutral-200"
        >
          {{ step }}
        </li>
      </ol>
    </div>

    <!-- Complexity notes (optional) -->
    <div v-if="complexity">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Complexity
      </h4>
      <p class="text-neutral-200 leading-relaxed whitespace-pre-wrap">
        {{ complexity }}
      </p>
    </div>

    <!-- Validation strategy -->
    <div v-if="validation">
      <h4 class="text-xs uppercase tracking-wider text-neutral-500 mb-1">
        Validation
      </h4>
      <p class="text-neutral-200 leading-relaxed whitespace-pre-wrap">
        {{ validation }}
      </p>
    </div>

    <!-- Consulted methods (HMML) — hidden entirely when the field is
         missing or empty so legacy runs without M9 data show no noise. -->
    <div v-if="consultedMethods.length > 0">
      <h4
        class="text-xs uppercase tracking-wider text-neutral-500 mb-1 flex items-center gap-1.5"
      >
        <Library class="h-3.5 w-3.5" aria-hidden="true" />
        <span>Consulted methods</span>
      </h4>
      <ul class="space-y-1.5">
        <li
          v-for="(m, i) in consultedMethods"
          :key="m.id || i"
          :class="[
            'rounded border border-neutral-800 border-l-2 pl-3 pr-2 py-1.5',
            TONE_CLASS[toneForReason(m.reason)],
          ]"
        >
          <div class="flex items-baseline gap-2 flex-wrap">
            <span class="font-semibold text-neutral-100">
              {{ m.name || m.id || "—" }}
            </span>
            <Badge
              v-if="m.id"
              variant="outline"
              class="mono text-[10px] py-0 px-1.5 font-normal text-neutral-400 border-neutral-800"
            >
              {{ m.id }}
            </Badge>
          </div>
          <p
            v-if="m.reason"
            class="italic text-neutral-300 text-[13px] leading-snug mt-0.5 break-words"
          >
            {{ m.reason }}
          </p>
        </li>
      </ul>
    </div>
  </div>
</template>
