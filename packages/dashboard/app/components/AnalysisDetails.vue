<script setup lang="ts">
import { buildTraceGraph, type NodeData } from '~/composables/useTraceGraph';
import { renderMarkdown } from '~/utils/markdown';
const props = defineProps<{ rcaId: string; engine: string }>();
const emit = defineEmits<{ evidence: [hypothesisId: string] }>();
const {
  data: trace,
  status,
  error,
  refresh,
} = useFetch(() => `/api/traces/${props.rcaId}`, {
  query: computed(() => ({ engine: props.engine })),
});
const graph = computed(() =>
  buildTraceGraph(trace.value?.spans ?? [], trace.value?.hypotheses ?? []),
);
const selected = ref<NodeData | null>(null);
const labels: Record<string, string> = {
  CONFIRMED: '채택',
  REJECTED: '기각',
  CLOSED: '검증 못 함',
  NEEDS_INVESTIGATION: '추가 조사',
  PENDING: '검증 안 됨',
};
</script>
<template>
  <ReadState
    label="분석 경로"
    :pending="status === 'pending'"
    :error="error"
    @retry="refresh()"
  >
    <div v-if="trace" class="space-y-5">
      <ClientOnly>
        <TraceGraph
          :nodes="graph.nodes"
          :edges="graph.edges"
          :current-state="trace.session?.state"
          @node-click="selected = $event.node.data"
        />
      </ClientOnly>
      <section v-if="selected" class="ops-panel p-4 space-y-3">
        <h3 class="font-semibold">{{ selected.title || selected.label }}</h3>
        <div
          class="prose-field"
          v-html="renderMarkdown(selected.detail || '')"
        />
        <div
          v-if="selected.error"
          class="prose-field text-error"
          v-html="renderMarkdown(selected.error)"
        />
      </section>
      <details class="ops-panel p-4">
        <summary class="cursor-pointer font-semibold text-sm">
          파이프라인 상태
        </summary>
        <ClientOnly
          ><StateGraph
            v-if="trace.session"
            :current-state="trace.session.state"
            :engine="trace.session.engine"
        /></ClientOnly>
      </details>
      <h3 class="detail-section-title">검토한 가설과 판단 근거</h3>
      <section
        v-for="hypothesis in trace.hypotheses"
        :key="hypothesis.hypothesisId"
        class="ops-panel p-4 space-y-3"
      >
        <div class="flex flex-wrap items-center gap-3">
          <span
            class="status-chip"
            :class="
              hypothesis.status === 'CONFIRMED'
                ? 'text-info'
                : 'text-base-content/70'
            "
            >{{ labels[hypothesis.status] || hypothesis.status }}</span
          >
          <h4 class="font-semibold text-sm">{{ hypothesis.title }}</h4>
          <span
            v-if="hypothesis.confidenceScore != null"
            class="text-xs font-mono"
            >{{ Math.round(hypothesis.confidenceScore * 100) }}%</span
          >
        </div>
        <div
          class="prose-field"
          v-html="renderMarkdown(hypothesis.description)"
        />
        <div
          v-if="hypothesis.judgmentReasoning"
          class="prose-field"
          v-html="renderMarkdown(hypothesis.judgmentReasoning)"
        />
        <div
          v-if="hypothesis.evidenceSummary"
          class="prose-field"
          v-html="renderMarkdown(hypothesis.evidenceSummary)"
        />
        <button
          class="btn btn-outline btn-sm"
          @click="emit('evidence', hypothesis.hypothesisId)"
        >
          전체 증거 보기
        </button>
      </section>
      <p v-if="!trace.hypotheses.length" class="detail-empty">
        개별 가설 기록이 제공되지 않았습니다.
      </p>
    </div>
  </ReadState>
</template>
