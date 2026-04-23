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
  return marked.parse(text, { async: false }) as string
}

const route = useRoute()
const id = route.params.id as string

const { data: trace, status, error } = useFetch(`/api/traces/${id}`)

const stateColor: Record<string, string> = {
  COMPLETED: 'badge-success',
  FAILED: 'badge-error',
  ALARM_RECEIVED: 'badge-info',
  SCOPING: 'badge-warning',
  HYPOTHESIS_GENERATION: 'badge-warning',
  EVIDENCE_COLLECTION: 'badge-warning',
  HYPOTHESIS_VALIDATION: 'badge-warning',
  REPORT_GENERATION: 'badge-warning',
  REMEDIATION: 'badge-warning',
  VERIFICATION: 'badge-warning',
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
        <button class="btn btn-ghost btn-sm btn-circle">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7" /></svg>
        </button>
      </NuxtLink>
      <h1 class="text-xl font-bold">실행 트레이스</h1>
      <span
        v-if="trace?.session"
        class="badge"
        :class="stateColor[trace.session.state] || 'badge-ghost'"
      >
        {{ trace.session.state }}
      </span>
      <div class="flex-1" />
      <NuxtLink :to="`/report/${id}`">
        <button class="btn btn-ghost btn-sm gap-1">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
          보고서
        </button>
      </NuxtLink>
    </div>

    <!-- Loading -->
    <div v-if="status === 'pending'" class="card bg-base-100 shadow">
      <div class="card-body items-center text-base-content/60">
        <span class="loading loading-spinner loading-md" />
        <p>트레이스 로딩 중...</p>
      </div>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="card bg-base-100 shadow">
      <div class="card-body items-center">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-8 text-error" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
        <p class="text-base-content/60">트레이스 데이터 로드 실패</p>
      </div>
    </div>

    <template v-else-if="trace">
      <!-- Session Info -->
      <div v-if="trace.session" class="card bg-base-100 shadow">
        <div class="card-body p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <div class="text-xs uppercase opacity-50">RCA ID</div>
            <div class="text-sm font-mono truncate">{{ id }}</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50">알람</div>
            <div class="text-sm">{{ trace.session.alarmName }}</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50">엔진</div>
            <div class="text-sm">{{ trace.session.engine }}</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50">생성일시</div>
            <div class="text-sm opacity-70">{{ formatTime(trace.session.createdAt) }}</div>
          </div>
        </div>
      </div>

      <!-- Canvas + Detail Panel -->
      <div class="flex gap-4" style="height: calc(100vh - 260px); min-height: 500px;">
        <!-- Flow Canvas -->
        <div class="card bg-base-100 shadow flex-1 overflow-hidden">
          <div class="card-body p-0 h-full">
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
        </div>

        <!-- Detail Panel -->
        <div class="card bg-base-100 shadow w-80 shrink-0 overflow-y-auto">
          <div class="card-body p-4">
            <template v-if="selectedNode">
              <!-- Span detail -->
              <template v-if="selectedNode.nodeType === 'span'">
                <h3 class="font-bold text-sm">{{ selectedNode.label }}</h3>
                <div class="flex gap-2 mt-2">
                  <span class="badge badge-sm" :class="{
                    'badge-success': selectedNode.status === 'COMPLETED',
                    'badge-error': selectedNode.status === 'FAILED',
                    'badge-warning': selectedNode.status === 'RUNNING',
                  }">{{ selectedNode.status }}</span>
                  <span v-if="selectedNode.durationMs" class="badge badge-sm badge-ghost font-mono">{{ formatDuration(selectedNode.durationMs) }}</span>
                </div>

                <div v-if="selectedNode.detail" class="mt-3">
                  <div class="text-xs uppercase opacity-50 mb-1">출력</div>
                  <div class="prose prose-xs max-w-none bg-base-200 rounded p-2 break-words" v-html="md(selectedNode.detail)" />
                </div>

                <div v-if="selectedNode.error" class="mt-3">
                  <div class="text-xs uppercase opacity-50 mb-1">오류</div>
                  <div class="prose prose-xs max-w-none bg-error/10 text-error rounded p-2 break-words" v-html="md(selectedNode.error)" />
                </div>

                <div v-if="selectedNode.metadata" class="mt-3">
                  <div class="text-xs uppercase opacity-50 mb-1">메타데이터</div>
                  <div class="bg-base-200 rounded p-2 space-y-1">
                    <div v-for="(val, key) in selectedNode.metadata" :key="String(key)" class="flex gap-2 text-xs">
                      <span class="opacity-50">{{ key }}:</span>
                      <span class="font-mono">{{ val }}</span>
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
                    'badge-warning': selectedNode.status === 'NEEDS_INVESTIGATION',
                  }">{{ selectedNode.status }}</span>
                  <span
                    v-if="selectedNode.confidenceScore !== undefined"
                    class="text-xs font-mono ml-auto"
                    :class="selectedNode.confidenceScore >= 0.8 ? 'text-success' : selectedNode.confidenceScore >= 0.5 ? 'text-warning' : 'opacity-50'"
                  >
                    {{ (selectedNode.confidenceScore * 100).toFixed(0) }}%
                  </span>
                </div>

                <div class="prose prose-sm max-w-none mt-2" v-html="md(selectedNode.description || selectedNode.label)" />

                <div v-if="selectedNode.evidenceSummary" class="mt-3">
                  <div class="text-xs uppercase opacity-50 mb-1">증거 요약</div>
                  <div class="prose prose-xs max-w-none bg-base-200 rounded p-2 break-words" v-html="md(selectedNode.evidenceSummary)" />
                </div>

                <div v-if="selectedNode.judgmentReasoning" class="mt-3">
                  <div class="text-xs uppercase opacity-50 mb-1">판단 근거</div>
                  <div class="prose prose-xs max-w-none bg-base-200 rounded p-2 break-words" v-html="md(selectedNode.judgmentReasoning)" />
                </div>
              </template>
            </template>

            <template v-else>
              <div class="flex flex-col items-center justify-center h-full text-base-content/40 gap-2">
                <svg xmlns="http://www.w3.org/2000/svg" class="size-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" /></svg>
                <p class="text-sm">노드를 클릭하면 상세 정보가 표시됩니다</p>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
