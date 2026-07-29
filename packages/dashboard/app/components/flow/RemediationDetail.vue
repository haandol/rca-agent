<script setup lang="ts">
import type { NodeData } from '~/composables/useTraceGraph';

const props = defineProps<{
  node: NodeData;
}>();

const detail = computed(() => {
  return {
    status: props.node.remediationStatus || 'UNKNOWN',
    success: props.node.remediationSuccess,
    faultType: props.node.remediationFaultType || '',
    endpoint: props.node.remediationEndpoint || '',
    summary: props.node.remediationSummary || '',
    error: props.node.remediationError || '',
    completedAt: props.node.remediationCompletedAt || '',
    verificationStatus: props.node.verificationStatus || '',
    metricsNormalized: props.node.metricsNormalized,
    verificationSummary: props.node.verificationSummary || '',
    remainingIssues: props.node.remainingIssues ?? [],
  };
});

function statusClass(status: string, success: boolean | null = null): string {
  if (status === 'COMPLETED') {
    if (success === true) return 'badge-success';
    if (success === false) return 'badge-error';
    return 'badge-ghost';
  }
  if (status === 'SUCCEEDED' || status === 'NORMALIZED') return 'badge-success';
  if (status === 'FAILED' || status === 'BREACHING') return 'badge-error';
  if (
    status === 'BLOCKED' ||
    status === 'PENDING' ||
    status === 'RUNNING' ||
    status === 'PROCESSING'
  ) {
    return 'badge-warning';
  }
  return 'badge-ghost';
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTime(iso: string): string {
  if (!iso) return '';
  return new Date(iso).toLocaleString();
}
</script>

<template>
  <div class="flex items-start justify-between gap-3">
    <div>
      <h3 class="font-bold text-sm">자동 복구</h3>
      <div
        v-if="node.spanId"
        class="text-[10px] font-mono text-base-content/40 mt-1 truncate select-all"
        :title="node.spanId"
      >
        {{ node.spanId }}
      </div>
    </div>
    <span
      class="badge badge-sm shrink-0"
      :class="statusClass(detail.status, detail.success)"
    >
      {{ detail.status }}
    </span>
  </div>

  <div
    v-if="node.durationMs != null || detail.faultType || detail.endpoint"
    class="mt-3 divide-y divide-base-content/5 border-y border-base-content/5"
  >
    <div
      v-if="node.durationMs != null"
      class="flex items-start justify-between gap-3 py-2 text-xs"
    >
      <span class="text-base-content/45">소요 시간</span>
      <span class="font-mono">{{ formatDuration(node.durationMs) }}</span>
    </div>
    <div
      v-if="detail.completedAt"
      class="flex items-start justify-between gap-3 py-2 text-xs"
    >
      <span class="text-base-content/45">완료 시각</span>
      <span class="text-right text-base-content/70">{{
        formatTime(detail.completedAt)
      }}</span>
    </div>
    <div
      v-if="detail.faultType"
      class="flex items-start justify-between gap-3 py-2 text-xs"
    >
      <span class="text-base-content/45">장애 유형</span>
      <span class="badge badge-xs badge-outline">{{ detail.faultType }}</span>
    </div>
    <div v-if="detail.endpoint" class="py-2 text-xs">
      <div class="text-base-content/45 mb-1">대상 엔드포인트</div>
      <code class="block text-[11px] break-all text-base-content/80">{{
        detail.endpoint
      }}</code>
    </div>
  </div>

  <div v-if="detail.summary" class="mt-3">
    <div
      class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5"
    >
      실행 요약
    </div>
    <p
      class="text-xs leading-relaxed whitespace-pre-wrap break-words bg-base-200/60 rounded-lg p-3"
    >
      {{ detail.summary }}
    </p>
  </div>

  <div
    v-if="detail.verificationStatus || detail.verificationSummary"
    class="mt-3 border-l-2 border-base-content/10 pl-3"
  >
    <div class="flex items-center justify-between gap-2">
      <div
        class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
      >
        복구 검증
      </div>
      <span
        v-if="detail.verificationStatus"
        class="badge badge-xs"
        :class="statusClass(detail.verificationStatus)"
      >
        {{ detail.verificationStatus }}
      </span>
    </div>
    <div v-if="detail.metricsNormalized !== null" class="mt-2 text-xs">
      <span
        class="badge badge-xs"
        :class="detail.metricsNormalized ? 'badge-success' : 'badge-error'"
      >
        메트릭 {{ detail.metricsNormalized ? '정상화' : '미정상화' }}
      </span>
    </div>
    <p
      v-if="detail.verificationSummary"
      class="text-xs text-base-content/70 leading-relaxed mt-2 whitespace-pre-wrap break-words"
    >
      {{ detail.verificationSummary }}
    </p>
    <ul
      v-if="detail.remainingIssues.length"
      class="mt-2 list-disc pl-4 text-xs text-error/80 space-y-1"
    >
      <li v-for="issue in detail.remainingIssues" :key="issue">{{ issue }}</li>
    </ul>
  </div>

  <div v-if="detail.error && detail.error !== detail.summary" class="mt-3">
    <div
      class="text-[11px] font-medium text-error/60 uppercase tracking-wider mb-1.5"
    >
      오류
    </div>
    <p
      class="text-xs leading-relaxed whitespace-pre-wrap break-words bg-error/5 text-error rounded-lg p-3"
    >
      {{ detail.error }}
    </p>
  </div>
</template>
