<script setup lang="ts">
import type {
  CritiqueChecklistItem,
  CritiqueFinding,
  CritiqueReport,
  RoleCritique,
} from "@mathodology/contracts";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown> }>();
const report = props.output as unknown as CritiqueReport;
const roles: RoleCritique[] = report.roles ?? [];
const checklist: CritiqueChecklistItem[] = report.checklist ?? [];
const findings: CritiqueFinding[] = report.findings ?? [];
const requiredChanges: string[] = report.required_changes ?? [];
const checklistPassRate =
  checklist.length === 0
    ? 1
    : checklist.filter((item) => item.passed).length / checklist.length;

function severityClass(finding: CritiqueFinding): string {
  return `sev-${finding.severity}`;
}

function roleLabel(role: string): string {
  return role.replace(/_/g, " ");
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
        <div class="critique-meta">
          <span v-if="checklist.length">
            <T en="Checklist" zh="检查项" />
            {{ Math.round(checklistPassRate * 100) }}%
          </span>
          <span v-if="report.max_revision_rounds !== undefined">
            <T en="Round" zh="轮次" />
            {{ report.revision_round ?? 0 }} / {{ report.max_revision_rounds }}
          </span>
          <span v-if="report.budget_exhausted" class="budget">
            <T en="Budget exhausted" zh="预算已用尽" />
          </span>
        </div>
      </div>
      <span :class="['badge', report.passed ? 'ok' : 'fail']">
        {{ report.passed ? "PASS" : "REVISE" }}
      </span>
    </div>

    <p>{{ report.summary }}</p>

    <div v-if="roles.length" class="role-grid">
      <div v-for="role in roles" :key="role.role" class="role-card">
        <div class="role-head">
          <strong>{{ roleLabel(role.role) }}</strong>
          <span :class="['badge', role.passed ? 'ok' : 'fail']">
            {{ Math.round(role.score * 100) }}%
          </span>
        </div>
        <p>{{ role.summary }}</p>
        <div
          v-for="(finding, idx) in role.findings"
          :key="`${role.role}-${finding.area}-${idx}`"
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
    </div>

    <div v-if="checklist.length" class="checklist">
      <div
        v-for="item in checklist"
        :key="item.id"
        :class="['check-item', item.passed ? 'ok' : 'fail']"
      >
        <span>{{ item.passed ? "✓" : "!" }}</span>
        <div>
          <strong>{{ item.label }}</strong>
          <p class="muted">{{ item.evidence }}</p>
        </div>
      </div>
    </div>

    <div v-if="findings.length" class="findings">
      <div
        v-for="(finding, idx) in findings"
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

    <ul v-if="requiredChanges.length" class="required">
      <li v-for="change in requiredChanges" :key="change">
        {{ change }}
      </li>
    </ul>
  </div>
</template>
