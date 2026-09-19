<script setup lang="ts">
import { PUBLICATION_LABELS } from '~/utils/publication';
import type { PublicationHistory } from '../../shared/types/retrospective-publication';
const emit = defineEmits<{ refresh: [] }>();
const props = defineProps<{
  execution: PublicationHistory;
  compact?: boolean;
}>();
const attempts = computed(() =>
  props.compact
    ? (props.execution.publicationAttempts ?? []).slice(0, 1)
    : (props.execution.publicationAttempts ?? []),
);
const labels: Record<string, string> = {
  COMPLETED: '검토 완료',
  RUNNING: '검토 중',
  FAILED: '실패',
  UPDATED: '갱신 완료',
  NO_CHANGE: '변경 없음',
};
/** Explicit UTC avoids hydration changing the recorded attempt time. */
function clock(value: number): string {
  return new Date(value * 1000).toISOString();
}
</script>

<template>
  <section class="mt-3 space-y-2 text-sm" aria-label="회고 검토와 공용 반영">
    <p v-if="execution.retrospectiveStatus">
      {{ attempts.length ? '원래 회고 기록' : '회고 기록' }} ·
      {{
        labels[execution.retrospectiveStatus] ?? execution.retrospectiveStatus
      }}
      <span class="font-mono">({{ execution.retrospectiveStatus }})</span>
    </p>
    <p
      v-if="execution.publicationReadState === 'UNAVAILABLE'"
      class="text-warning"
    >
      일부 공용 반영 기록의 연결이나 보존 기한을 확인할 수 없습니다. 아래 유효한
      이력만으로 현재 반영 완료를 판단하지 마세요.
      <button
        type="button"
        class="btn btn-ghost btn-xs"
        @click="emit('refresh')"
      >
        다시 조회
      </button>
    </p>
    <p
      v-else-if="
        !attempts.length && execution.retrospectiveStatus === 'COMPLETED'
      "
      class="text-base-content/70"
    >
      회고 검토는 완료됐습니다. 공용 반영 상태는 아직 기록되지 않았습니다.
    </p>
    <ol v-if="attempts.length" class="space-y-3">
      <li
        v-for="attempt in attempts"
        :key="attempt.attemptId"
        class="border-l-2 border-base-300 pl-3"
      >
        <p>
          회고 검토 완료 ·
          <strong
            :class="
              attempt.status === 'PUBLISHED'
                ? 'text-success'
                : ['FAILED', 'BLOCKED'].includes(attempt.status)
                  ? 'text-error'
                  : 'text-warning'
            "
            >{{ PUBLICATION_LABELS[attempt.status] }}</strong
          >
        </p>
        <p
          v-if="attempt.status === 'PUBLISHED'"
          class="text-xs font-mono break-all"
        >
          공용 원문 {{ attempt.publicPlaybookId }} · 개정
          {{ attempt.publishedRevision }}
        </p>
        <p v-if="attempt.reason" class="whitespace-pre-wrap break-words">
          {{ attempt.reason }}
        </p>
        <p class="text-xs font-mono break-all">
          후속 시도 {{ attempt.attemptId }}
        </p>
        <p class="text-xs text-base-content/65">
          갱신 {{ clock(attempt.updatedAt) }}
        </p>
      </li>
    </ol>
  </section>
</template>
