<script setup lang="ts">
import { VueFlow } from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import { buildTraceGraph, type NodeData } from '~/composables/useTraceGraph';
import SpanNode from '~/components/flow/SpanNode.vue';
import HypoNode from '~/components/flow/HypoNode.vue';
import { renderMarkdown as md } from '~/utils/markdown';

const route = useRoute();
const id = route.params.id as string;
const engine = (route.query.engine as string) || '';

const {
  data: trace,
  status,
  error,
} = useFetch(`/api/traces/${id}`, {
  query: engine ? { engine } : undefined,
});

const fullEvidence = ref<string | null>(null);
const fullEvidenceLoading = ref(false);
const evidenceModalRef = ref<HTMLDialogElement | null>(null);

async function showFullEvidence(hypothesisId: string) {
  fullEvidence.value = null;
  fullEvidenceLoading.value = true;
  evidenceModalRef.value?.showModal();
  try {
    const data = await $fetch(`/api/evidence/${id}/${hypothesisId}`);
    fullEvidence.value = (data as { markdown?: string }).markdown || '';
  } catch {
    fullEvidence.value = '증거를 불러올 수 없습니다.';
  } finally {
    fullEvidenceLoading.value = false;
  }
}

const HYPO_STATUS_LABEL: Record<string, string> = {
  CONFIRMED: '채택',
  REJECTED: '기각',
  CLOSED: '검증 못 함',
  NEEDS_INVESTIGATION: '추가 조사',
  PENDING: '검증 안 됨',
};

const stateModalRef = ref<HTMLDialogElement | null>(null);

/**
 * What the search tried, sorted by what became of it.
 *
 * The report already states the one chain that survived, so this page's job is
 * the opposite: everything the search considered and discarded. Confirmed first,
 * then what is still open, then the rejected — a reader coming here wants to know
 * whether something was missed.
 */
const STATUS_ORDER = [
  'CONFIRMED',
  'NEEDS_INVESTIGATION',
  'PENDING',
  'CLOSED',
  'REJECTED',
];

const hypotheses = computed(() => {
  const list = [...(trace.value?.hypotheses ?? [])];
  return list.sort((a, b) => {
    const rank =
      STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
    if (rank !== 0) return rank;
    return (b.confidenceScore ?? 0) - (a.confidenceScore ?? 0);
  });
});

const confirmedCount = computed(
  () => hypotheses.value.filter((h) => h.status === 'CONFIRMED').length,
);
const rejectedCount = computed(
  () => hypotheses.value.filter((h) => h.status === 'REJECTED').length,
);

function statusTone(status: string): string {
  if (status === 'CONFIRMED') return 'text-success';
  if (status === 'NEEDS_INVESTIGATION') return 'text-warning';
  if (status === 'REJECTED') return 'text-base-content/35';
  return 'text-base-content/45';
}

function executionTone(state: string): string {
  if (state === 'RESOLVED') return 'text-success';
  if (state === 'UNRESOLVED' || state === 'FAILED') return 'text-error';
  if (state === 'EXECUTING' || state === 'VERIFYING') return 'text-primary';
  return 'text-base-content/45';
}

const graph = computed(() => {
  if (!trace.value) return { nodes: [], edges: [] };
  return buildTraceGraph(trace.value.spans, trace.value.hypotheses);
});

const selectedNode = ref<NodeData | null>(null);

function onNodeClick(e: { node: { data: NodeData } }) {
  selectedNode.value = e.node.data;
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}초`;
  return `${Math.round(ms / 60_000)}분`;
}

/** The graph is the same facts drawn as a shape; it opens on request. */
const showGraph = ref(false);

const selectedHypothesis = ref<string>('');

function toggleHypothesis(hypothesisId: string) {
  selectedHypothesis.value =
    selectedHypothesis.value === hypothesisId ? '' : hypothesisId;
}

const reportLink = computed(() =>
  engine ? `/report/${id}?engine=${engine}` : `/report/${id}`,
);

useHead({
  title: () =>
    `분석 경로 · ${trace.value?.session?.alarmName ?? id.slice(0, 8)}`,
});
</script>

<template>
  <div>
    <header class="mb-9">
      <NuxtLink
        :to="reportLink"
        class="text-[12px] text-base-content/45 hover:text-primary inline-flex items-center gap-1.5 mb-4"
      >
        <span aria-hidden="true">←</span> 보고서로
      </NuxtLink>

      <h1 class="font-serif text-[26px] leading-tight tracking-tight">
        분석이 실제로 시도한 것
      </h1>
      <p class="text-[13px] text-base-content/50 mt-2.5 max-w-[62ch]">
        보고서는 살아남은 하나의 사슬만 말합니다. 여기에는 그 사슬에 이르기까지
        함께 세워졌던 가설과, 무엇이 왜 기각됐는지가 남아 있습니다.
      </p>

      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 mt-4 text-[12px] text-base-content/50"
      >
        <span v-if="trace?.session">{{ trace.session.alarmName }}</span>
        <span class="font-mono">{{ trace?.session?.engine }}</span>
        <button
          v-if="trace?.session"
          class="text-base-content/45 hover:text-primary"
          @click="stateModalRef?.showModal()"
        >
          상태 전이 보기
        </button>
      </div>
    </header>

    <div
      v-if="status === 'pending'"
      class="py-20 text-center text-[13px] text-base-content/40"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">분석 경로를 읽고 있습니다</p>
    </div>

    <div v-else-if="error" class="py-20 text-center">
      <p class="font-serif text-[17px]">분석 경로를 불러오지 못했습니다</p>
    </div>

    <template v-else-if="trace">
      <!-- Executions have their own lifecycle, so they sit beside the analysis
           rather than folded into it. -->
      <section
        v-if="trace.executions?.length"
        class="mb-11 pb-7 border-b border-base-content/10"
      >
        <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-3">
          이 리포트로 수행된 실행
        </h2>
        <ul class="divide-y divide-base-content/[0.07]">
          <li
            v-for="execution in trace.executions"
            :key="execution.executionId"
            class="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2.5 text-[12.5px]"
          >
            <span
              class="font-medium w-16"
              :class="executionTone(execution.state)"
            >
              {{ execution.stateLabel }}
            </span>
            <span class="text-base-content/50">
              {{ execution.attempt }}회차 · 절차
              {{ execution.attemptedStepCount }}건
            </span>
            <span v-if="execution.blockedCount" class="text-warning">
              수동 조치 {{ execution.blockedCount }}
            </span>
            <span v-if="execution.failedStepCount" class="text-error">
              실패 {{ execution.failedStepCount }}
            </span>
            <NuxtLink
              v-if="execution.retrospectiveStatus"
              :to="`/retrospective/${id}/${execution.executionId}`"
              class="text-info hover:underline underline-offset-2 ml-auto"
            >
              회고 {{ execution.retrospectiveStatus }}
            </NuxtLink>
            <span
              v-if="execution.errorReason"
              class="text-base-content/40 w-full"
            >
              {{ execution.errorReason }}
            </span>
          </li>
        </ul>
      </section>

      <!-- The hypotheses, as a list with the verdict as the organising fact -->
      <section v-if="hypotheses.length">
        <div class="flex items-baseline gap-3 mb-5">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold">
            세워진 가설
          </h2>
          <span class="text-[11px] text-base-content/35">
            {{ hypotheses.length }}개 중 채택 {{ confirmedCount }} · 기각
            {{ rejectedCount }}
          </span>
        </div>

        <ul class="divide-y divide-base-content/[0.07]">
          <li v-for="hypothesis in hypotheses" :key="hypothesis.hypothesisId">
            <button
              class="w-full text-left py-3.5 group"
              :aria-expanded="selectedHypothesis === hypothesis.hypothesisId"
              @click="toggleHypothesis(hypothesis.hypothesisId)"
            >
              <div class="flex items-baseline gap-3">
                <span
                  class="text-[12px] font-medium w-[68px] shrink-0"
                  :class="statusTone(hypothesis.status)"
                >
                  {{
                    HYPO_STATUS_LABEL[hypothesis.status] || hypothesis.status
                  }}
                </span>
                <span
                  class="font-serif text-[15px] leading-snug flex-1 min-w-0 group-hover:text-primary transition-colors"
                  :class="
                    hypothesis.status === 'REJECTED'
                      ? 'text-base-content/50'
                      : ''
                  "
                >
                  {{
                    hypothesis.title || hypothesis.description.split('\n')[0]
                  }}
                </span>
                <span
                  class="font-mono text-[11px] tabular-nums shrink-0"
                  :class="
                    hypothesis.confidenceScore >= 0.8
                      ? 'text-success'
                      : 'text-base-content/35'
                  "
                >
                  {{ Math.round((hypothesis.confidenceScore ?? 0) * 100) }}%
                </span>
              </div>
            </button>

            <div
              v-if="selectedHypothesis === hypothesis.hypothesisId"
              class="pb-5 pl-[80px] pr-2 space-y-4"
            >
              <div
                v-if="hypothesis.description"
                class="prose-field"
                v-html="md(hypothesis.description)"
              />

              <div v-if="hypothesis.judgmentReasoning">
                <h3 class="label-sm mb-1.5">이렇게 판단한 이유</h3>
                <div
                  class="prose-field"
                  v-html="md(hypothesis.judgmentReasoning)"
                />
              </div>

              <div v-if="hypothesis.evidenceSummary">
                <div class="flex items-baseline justify-between gap-3 mb-1.5">
                  <h3 class="label-sm">모은 증거</h3>
                  <button
                    class="text-[12px] text-primary hover:underline underline-offset-2"
                    @click.stop="showFullEvidence(hypothesis.hypothesisId)"
                  >
                    전체 보기
                  </button>
                </div>
                <div
                  class="prose-field"
                  v-html="md(hypothesis.evidenceSummary)"
                />
              </div>
            </div>
          </li>
        </ul>
      </section>

      <p v-else class="font-serif text-[15px] text-base-content/50 py-8">
        기록된 가설이 없습니다. 이 엔진은 가설을 개별 항목으로 남기지 않거나,
        분석이 가설 생성 전에 멈췄습니다.
      </p>

      <!-- The same facts as a shape, for anyone who reads structure faster -->
      <section class="mt-12 pt-7 border-t border-base-content/10">
        <button
          class="flex items-baseline gap-2 text-[13px] text-base-content/55 hover:text-primary transition-colors"
          :aria-expanded="showGraph"
          @click="showGraph = !showGraph"
        >
          <span class="font-mono text-[11px]">{{ showGraph ? '−' : '+' }}</span>
          파이프라인 그래프
          <span class="text-[11px] text-base-content/35">
            스팬과 가설의 연결 구조
          </span>
        </button>

        <div v-if="showGraph" class="mt-6 flex flex-col lg:flex-row gap-5">
          <!-- Vue Flow numbers its instances from a module-level counter, so the
               server and the browser assign different ids and hydration reports a
               mismatch on every graph. Nothing here needs server rendering — the
               graph only exists once someone opens it. -->
          <ClientOnly>
            <div
              class="sheet-ruled flex-1 overflow-hidden"
              style="height: 540px"
            >
              <VueFlow
                :nodes="graph.nodes"
                :edges="graph.edges"
                :default-viewport="{ zoom: 0.85, x: 40, y: 20 }"
                fit-view-on-init
                :min-zoom="0.3"
                :max-zoom="2"
                @node-click="onNodeClick"
              >
                <template #node-spanNode="props">
                  <SpanNode v-bind="props" />
                </template>
                <template #node-hypoNode="props">
                  <HypoNode v-bind="props" />
                </template>
              </VueFlow>
            </div>
          </ClientOnly>

          <aside class="w-full lg:w-[280px] shrink-0">
            <template v-if="selectedNode">
              <h3 class="font-serif text-[16px] leading-snug">
                {{ selectedNode.title || selectedNode.label }}
              </h3>
              <div
                class="flex flex-wrap items-baseline gap-x-3 gap-y-1 mt-2 text-[12px] text-base-content/50"
              >
                <span
                  v-if="selectedNode.status"
                  :class="statusTone(selectedNode.status)"
                >
                  {{
                    HYPO_STATUS_LABEL[selectedNode.status] ||
                    selectedNode.status
                  }}
                </span>
                <span v-if="selectedNode.durationMs" class="font-mono">
                  {{ formatDuration(selectedNode.durationMs) }}
                </span>
              </div>

              <div
                v-if="selectedNode.detail"
                class="prose-field mt-4"
                v-html="md(selectedNode.detail)"
              />
              <div
                v-if="selectedNode.error"
                class="prose-field mt-4 text-error"
                v-html="md(selectedNode.error)"
              />
            </template>
            <p v-else class="text-[12.5px] text-base-content/40">
              노드를 선택하면 그 단계의 입출력이 표시됩니다.
            </p>
          </aside>
        </div>
      </section>
    </template>

    <!-- Full evidence -->
    <dialog ref="evidenceModalRef" class="modal">
      <div class="modal-box max-w-3xl max-h-[82vh]">
        <h3 class="font-serif text-[19px] mb-4">모은 증거</h3>
        <div
          v-if="fullEvidenceLoading"
          class="flex items-center justify-center py-14"
        >
          <span class="loading loading-spinner loading-sm" />
        </div>
        <div
          v-else
          class="prose-field overflow-y-auto max-h-[62vh]"
          v-html="md(fullEvidence)"
        />
        <div class="modal-action">
          <form method="dialog">
            <button class="btn btn-ghost btn-sm">닫기</button>
          </form>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop">
        <button type="submit">close</button>
      </form>
    </dialog>

    <!-- State transitions -->
    <dialog ref="stateModalRef" class="modal">
      <div class="modal-box max-w-3xl max-h-[88vh]">
        <h3 class="font-serif text-[19px] mb-4">파이프라인 상태 전이</h3>
        <!-- Also Vue Flow, so also client-only: its instance ids come from a
             module counter that server and browser number differently. -->
        <ClientOnly>
          <StateGraph
            v-if="trace?.session"
            :current-state="trace.session.state"
            :engine="trace.session.engine"
          />
        </ClientOnly>
        <div class="modal-action">
          <form method="dialog">
            <button class="btn btn-ghost btn-sm">닫기</button>
          </form>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop">
        <button type="submit">close</button>
      </form>
    </dialog>
  </div>
</template>
