<script setup lang="ts">
import { renderMarkdown } from '~/utils/markdown';
const props = defineProps<{ playbook: Record<string, unknown> }>();
const fields = [
  ['failure_type', '장애 유형'],
  ['symptom_pattern', '증상 패턴'],
  ['related_metrics', '관측 메트릭'],
  ['severity_criteria', '심각도 판단'],
  ['verification_steps', '검증 절차'],
  ['temporary_mitigation', '임시 완화'],
  ['permanent_remediation', '영구 대책'],
  ['prevention_measures', '재발 방지'],
  ['escalation_criteria', '에스컬레이션'],
  ['tags', '태그'],
] as const;
/**
 * Present reusable knowledge through the safe Markdown renderer.
 * Convert only recorded strings or string-list entries; missing fields remain empty.
 */
function content(field: string) {
  const value = props.playbook[field];
  return typeof value === 'string'
    ? value
    : Array.isArray(value)
      ? value
          .filter((item) => typeof item === 'string')
          .map((item) => `- ${item}`)
          .join('\n')
      : '';
}
</script>
<template>
  <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
    <section
      v-for="[field, label] in fields"
      :key="field"
      class="ops-panel min-w-0 p-4"
    >
      <h3 class="detail-section-title mb-3">{{ label }}</h3>
      <div
        v-if="content(field)"
        class="prose-field break-words"
        v-html="renderMarkdown(content(field))"
      />
      <p v-else class="detail-empty">미제공</p>
    </section>
  </div>
</template>
