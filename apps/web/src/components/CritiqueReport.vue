<script setup lang="ts">
import type { CritiqueFinding, CritiqueReport } from "@mathodology/contracts";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown> }>();
const report = props.output as unknown as CritiqueReport;

function severityClass(finding: CritiqueFinding): string {
  return `sev-${finding.severity}`;
}
</script>

<template>
  <div class="output-panel critique-report">
    <div class="critique-head">
      <div>
        <div class="eyebrow">
          <T en="Critic review" zh="审查报告" />
          · {{ report.target_agent }} / {{ report.target_schema }}
        </div>
        <h3>
          {{ report.passed ? "Passed" : "Needs revision" }} ·
          {{ Math.round(report.score * 100) }}%
        </h3>
      </div>
      <span :class="['badge', report.passed ? 'ok' : 'fail']">
        {{ report.passed ? "PASS" : "REVISE" }}
      </span>
    </div>

    <p>{{ report.summary }}</p>

    <div v-if="report.findings.length" class="findings">
      <div
        v-for="(finding, idx) in report.findings"
        :key="`${finding.area}-${idx}`"
        :class="['finding', severityClass(finding)]"
      >
        <div class="finding-title">
          <strong>{{ finding.severity }}</strong>
          <span>{{ finding.area }}</span>
        </div>
        <p>{{ finding.message }}</p>
        <p class="muted"><strong>Evidence:</strong> {{ finding.evidence }}</p>
        <p class="muted">
          <strong>Required change:</strong> {{ finding.required_change }}
        </p>
      </div>
    </div>

    <ul v-if="report.required_changes.length" class="required">
      <li v-for="change in report.required_changes" :key="change">
        {{ change }}
      </li>
    </ul>
  </div>
</template>
