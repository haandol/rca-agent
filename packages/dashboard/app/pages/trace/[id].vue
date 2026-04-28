<script setup lang="ts">
import { VueFlow } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { marked } from 'marked'
import { buildTraceGraph, type NodeData } from '~/composables/useTraceGraph'
import SpanNode from '~/components/flow/SpanNode.vue'
import HypoNode from '~/components/flow/HypoNode.vue'

function md(text: string | undefined | null): string {
  if (!text) return ''
  const normalized = text
    .replace(/\\n/g, '\n')
    .replace(/(?<!\n)(\d+)\.\s/g, '\n$1. ')
    .trim()
  return marked.parse(normalized, { async: false, breaks: true }) as string
}

const route = useRoute()
const id = route.params.id as string
const engine = (route.query.engine as string) || ''

const { data: trace, status, error } = useFetch(`/api/traces/${id}`, {
  query: engine ? { engine } : undefined,
})

const fullEvidence = ref<string | null>(null)
const fullEvidenceLoading = ref(false)
const evidenceModalRef = ref<HTMLDialogElement | null>(null)

async function showFullEvidence(hypothesisId: string) {
  fullEvidence.value = null
  fullEvidenceLoading.value = true
  evidenceModalRef.value?.showModal()
  try {
    const data = await $fetch(`/api/evidence/${id}/${hypothesisId}`)
    fullEvidence.value = (data as any).markdown || ''
  } catch {
    fullEvidence.value = '증거를 불러올 수 없습니다.'
  } finally {
    fullEvidenceLoading.value = false
  }
}

const stateStyle: Record<string, { bg: string; text: string }> = {
  COMPLETED: { bg: 'bg-success/10', text: 'text-success' },
  FAILED: { bg: 'bg-error/10', text: 'text-error' },
  CANCELLED: { bg: 'bg-base-content/5', text: 'text-base-content/60' },
  OUTDATED: { bg: 'bg-base-content/5', text: 'text-base-content/40' },
}

const STATE_LABEL: Record<string, string> = {
  ALARM_RECEIVED: '알람 수신',
  SCOPING: '스코핑',
  HYPOTHESIS_GENERATION: '가설 생성',
  HYPOTHESIS_PRIORITIZATION: '우선순위 결정',
  EVIDENCE_COLLECTION: '증거 수집',
  HYPOTHESIS_VALIDATION: '가설 검증',
  REPORT_GENERATION: '보고서 생성',
  REMEDIATION: '자동 복구',
  VERIFICATION: '복구 검증',
  ANALYZING: '분석 중',
  COMPLETED: '완료',
  FAILED: '실패',
  CANCELLED: '중단됨',
  OUTDATED: '만료됨',
}

const stateModalRef = ref<HTMLDialogElement | null>(null)

function openStateModal() {
  stateModalRef.value?.showModal()
}

function formatTime(iso: string) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString()
}

const graph = computed(() => {
  if (!trace.value) return { nodes: [], edges: [] }
  return buildTraceGraph(trace.value.spans, trace.value.hypotheses)
})

const selectedNode = ref<NodeData | null>(null)

function onNodeClick(e: { node: { data: NodeData } }) {
  selectedNode.value = e.node.data
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

useHead({ title: () => `Trace ${id.slice(0, 8)}` })
</script>

<template>
  <div class="space-y-4">
    <!-- Header -->
    <div class="flex items-center gap-3">
      <NuxtLink to="/">
        <button class="btn btn-ghost btn-sm btn-circle rounded-lg">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7" /></svg>
        </button>
      </NuxtLink>
      <div class="flex-1">
        <h1 class="text-xl font-bold tracking-tight">실행 트레이스</h1>
      </div>
      <span
        v-if="trace?.session"
        class="inline-flex items-center text-xs font-medium px-2 py-1 rounded-md cursor-pointer hover:ring-1 hover:ring-base-content/20 transition-shadow"
        :class="[
          stateStyle[trace.session.state]?.bg || 'bg-warning/10',
          stateStyle[trace.session.state]?.text || 'text-warning',
        ]"
        title="클릭하여 상태 설명 보기"
        @click="openStateModal()"
      >
        {{ STATE_LABEL[trace.session.state] || trace.session.state }}
      </span>
      <NuxtLink :to="engine ? `/report/${id}?engine=${engine}` : `/report/${id}`">
        <button class="btn btn-ghost btn-sm gap-1.5 rounded-lg">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
          보고서
        </button>
      </NuxtLink>
      <NuxtLink :to="engine ? `/playbook/${id}?engine=${engine}` : `/playbook/${id}`">
        <button class="btn btn-ghost btn-sm gap-1.5 rounded-lg">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
          플레이북
        </button>
      </NuxtLink>
    </div>

    <!-- Loading -->
    <div v-if="status === 'pending'" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16 text-base-content/40">
        <span class="loading loading-spinner loading-md" />
        <p class="mt-3 text-sm">트레이스 로딩 중...</p>
      </div>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-8 text-error/60" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
        <p class="text-sm text-base-content/50 mt-2">트레이스 데이터 로드 실패</p>
      </div>
    </div>

    <template v-else-if="trace">
      <!-- Session Info -->
      <div v-if="trace.session" class="bg-base-100 rounded-xl border border-base-content/5 p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">RCA 아이디</div>
          <div class="text-sm font-mono mt-1 truncate">{{ id }}</div>
        </div>
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">알람</div>
          <div class="text-sm mt-1">{{ trace.session.alarmName }}</div>
        </div>
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">엔진</div>
          <div class="text-sm font-mono mt-1">{{ trace.session.engine }}</div>
        </div>
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">생성일시</div>
          <div class="text-sm mt-1 text-base-content/60">{{ formatTime(trace.session.createdAt) }}</div>
        </div>
      </div>

      <!-- Canvas + Detail Panel -->
      <div class="flex gap-4" style="height: calc(100vh - 260px); min-height: 500px;">
        <!-- Flow Canvas -->
        <div class="bg-base-100 rounded-xl border border-base-content/5 flex-1 overflow-hidden">
          <VueFlow
            :nodes="graph.nodes"
            :edges="graph.edges"
            :default-viewport="{ zoom: 0.9, x: 50, y: 20 }"
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

        <!-- Detail Panel -->
        <div class="bg-base-100 rounded-xl border border-base-content/5 w-80 shrink-0 overflow-y-auto">
          <div class="p-4">
            <template v-if="selectedNode">
              <!-- Playbook detail -->
              <template v-if="selectedNode.nodeType === 'span' && selectedNode.spanType === 'PLAYBOOK' && selectedNode.metadata">
                <h3 class="font-bold text-sm">플레이북</h3>
                <div class="flex gap-2 mt-2">
                  <span class="badge badge-sm" :class="{
                    'badge-success': selectedNode.status === 'COMPLETED',
                    'badge-error': selectedNode.status === 'FAILED',
                    'badge-warning': selectedNode.status === 'RUNNING',
                  }">{{ selectedNode.status }}</span>
                  <span v-if="selectedNode.durationMs" class="badge badge-sm badge-ghost font-mono">{{ formatDuration(selectedNode.durationMs) }}</span>
                </div>
                <div v-if="selectedNode.spanId" class="mt-2">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-0.5">Span ID</div>
                  <div class="text-[10px] font-mono text-base-content/50 truncate select-all" :title="selectedNode.spanId">{{ selectedNode.spanId }}</div>
                </div>

                <div v-if="selectedNode.metadata.failure_type" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1">장애 유형</div>
                  <span class="badge badge-sm badge-outline">{{ selectedNode.metadata.failure_type }}</span>
                </div>

                <div v-if="selectedNode.metadata.symptom_pattern" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">증상 패턴</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.metadata.symptom_pattern as string)" />
                </div>

                <div v-if="(selectedNode.metadata.verification_steps as string[] | undefined)?.length" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">검증 절차</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md((selectedNode.metadata.verification_steps as string[]).map((s, i) => `${i + 1}. ${s}`).join('\n'))" />
                </div>

                <div v-if="selectedNode.metadata.temporary_mitigation" class="mt-3">
                  <div class="text-[11px] font-medium text-warning uppercase tracking-wider mb-1.5">임시 완화</div>
                  <div class="prose prose-xs max-w-none bg-warning/5 border border-warning/10 rounded-lg p-3 break-words" v-html="md(selectedNode.metadata.temporary_mitigation as string)" />
                </div>

                <div v-if="selectedNode.metadata.permanent_remediation" class="mt-3">
                  <div class="text-[11px] font-medium text-success uppercase tracking-wider mb-1.5">영구 복구</div>
                  <div class="prose prose-xs max-w-none bg-success/5 border border-success/10 rounded-lg p-3 break-words" v-html="md(selectedNode.metadata.permanent_remediation as string)" />
                </div>

                <div v-if="(selectedNode.metadata.prevention_measures as string[] | undefined)?.length" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">재발 방지</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md((selectedNode.metadata.prevention_measures as string[]).map((s, i) => `${i + 1}. ${s}`).join('\n'))" />
                </div>

                <div v-if="(selectedNode.metadata.tags as string[] | undefined)?.length" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">태그</div>
                  <div class="flex flex-wrap gap-1">
                    <span v-for="tag in (selectedNode.metadata.tags as string[])" :key="tag" class="badge badge-xs badge-ghost">{{ tag }}</span>
                  </div>
                </div>

                <div v-if="selectedNode.metadata.playbook_id" class="mt-3 pt-3 border-t border-base-content/5">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1">Playbook ID</div>
                  <div class="text-[10px] font-mono text-base-content/40 truncate">{{ selectedNode.metadata.playbook_id }}</div>
                </div>

                <div v-if="selectedNode.error" class="mt-3">
                  <div class="text-[11px] font-medium text-error/60 uppercase tracking-wider mb-1.5">오류</div>
                  <div class="prose prose-xs max-w-none bg-error/5 text-error rounded-lg p-3 break-words" v-html="md(selectedNode.error)" />
                </div>
              </template>

              <!-- Span detail (generic) -->
              <template v-else-if="selectedNode.nodeType === 'span'">
                <h3 class="font-bold text-sm">{{ selectedNode.label }}</h3>
                <div class="flex gap-2 mt-2">
                  <span class="badge badge-sm" :class="{
                    'badge-success': selectedNode.status === 'COMPLETED',
                    'badge-error': selectedNode.status === 'FAILED',
                    'badge-warning': selectedNode.status === 'RUNNING',
                  }">{{ selectedNode.status }}</span>
                  <span v-if="selectedNode.durationMs" class="badge badge-sm badge-ghost font-mono">{{ formatDuration(selectedNode.durationMs) }}</span>
                </div>
                <div v-if="selectedNode.spanId" class="mt-2">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-0.5">Span ID</div>
                  <div class="text-[10px] font-mono text-base-content/50 truncate select-all" :title="selectedNode.spanId">{{ selectedNode.spanId }}</div>
                </div>

                <div v-if="selectedNode.detail" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">출력</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.detail)" />
                </div>

                <div v-if="selectedNode.error" class="mt-3">
                  <div class="text-[11px] font-medium text-error/60 uppercase tracking-wider mb-1.5">오류</div>
                  <div class="prose prose-xs max-w-none bg-error/5 text-error rounded-lg p-3 break-words" v-html="md(selectedNode.error)" />
                </div>

                <div v-if="selectedNode.metadata" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">메타데이터</div>
                  <div class="bg-base-200/60 rounded-lg p-3 space-y-1.5">
                    <div v-for="(val, key) in selectedNode.metadata" :key="String(key)" class="flex gap-2 text-xs">
                      <span class="text-base-content/40 shrink-0">{{ key }}</span>
                      <span class="font-mono text-base-content/80">{{ val }}</span>
                    </div>
                  </div>
                </div>
              </template>

              <!-- Hypothesis detail -->
              <template v-else>
                <div class="flex items-center gap-2 flex-wrap">
                  <span v-if="selectedNode.category" class="badge badge-sm" :class="{
                    'badge-info': selectedNode.category === 'DEPLOYMENT',
                    'badge-warning': selectedNode.category === 'INFRASTRUCTURE',
                    'badge-success': selectedNode.category === 'TRAFFIC',
                    'badge-error': selectedNode.category === 'DEPENDENCY',
                  }">{{ selectedNode.category }}</span>
                  <span class="badge badge-sm" :class="{
                    'badge-success': selectedNode.status === 'CONFIRMED',
                    'badge-error': selectedNode.status === 'REJECTED',
                    'badge-ghost': selectedNode.status === 'CLOSED',
                    'badge-warning': selectedNode.status === 'NEEDS_INVESTIGATION',
                  }">{{ selectedNode.status }}</span>
                  <span
                    v-if="selectedNode.confidenceScore !== undefined"
                    class="text-xs font-mono ml-auto"
                    :class="selectedNode.confidenceScore >= 0.8 ? 'text-success' : selectedNode.confidenceScore >= 0.5 ? 'text-warning' : 'text-base-content/40'"
                  >
                    {{ (selectedNode.confidenceScore * 100).toFixed(0) }}%
                  </span>
                </div>

                <div class="prose prose-sm max-w-none mt-3" v-html="md(selectedNode.description || selectedNode.label)" />

                <div v-if="selectedNode.evidenceSummary" class="mt-3">
                  <div class="flex items-center justify-between mb-1.5">
                    <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">증거 요약</div>
                    <button
                      v-if="selectedNode.hypothesisId"
                      class="btn btn-ghost btn-xs text-primary"
                      @click="showFullEvidence(selectedNode.hypothesisId!)"
                    >상세 보기</button>
                  </div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.evidenceSummary)" />
                </div>

                <div v-if="selectedNode.judgmentReasoning" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">판단 근거</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.judgmentReasoning)" />
                </div>
              </template>
            </template>

            <template v-else>
              <div class="flex flex-col items-center justify-center h-full text-base-content/30 gap-3">
                <svg xmlns="http://www.w3.org/2000/svg" class="size-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" /></svg>
                <p class="text-sm">노드를 클릭하면 상세 정보가 표시됩니다</p>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>

    <!-- Full Evidence Modal -->
    <dialog ref="evidenceModalRef" class="modal">
      <div class="modal-box max-w-3xl max-h-[80vh]">
        <h3 class="font-bold text-lg mb-4">상세 증거</h3>
        <div v-if="fullEvidenceLoading" class="flex items-center justify-center py-12">
          <span class="loading loading-spinner loading-md" />
        </div>
        <div v-else class="prose prose-sm max-w-none overflow-y-auto max-h-[60vh]" v-html="md(fullEvidence)" />
        <div class="modal-action">
          <form method="dialog"><button class="btn btn-ghost btn-sm">닫기</button></form>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop"><button type="submit">close</button></form>
    </dialog>

    <!-- State Transition Graph Modal -->
    <dialog ref="stateModalRef" class="modal">
      <div class="modal-box max-w-3xl max-h-[85vh]">
        <h3 class="font-bold text-lg mb-4">파이프라인 상태 전이</h3>
        <StateGraph v-if="trace?.session" :current-state="trace.session.state" />
        <div class="modal-action">
          <form method="dialog"><button class="btn btn-ghost btn-sm">닫기</button></form>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop"><button type="submit">close</button></form>
    </dialog>
  </div>
</template>
