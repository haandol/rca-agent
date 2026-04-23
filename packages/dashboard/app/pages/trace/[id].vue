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

const stateStyle: Record<string, { bg: string; text: string }> = {
  COMPLETED: { bg: 'bg-success/10', text: 'text-success' },
  FAILED: { bg: 'bg-error/10', text: 'text-error' },
  CANCELLED: { bg: 'bg-base-content/5', text: 'text-base-content/60' },
  OUTDATED: { bg: 'bg-base-content/5', text: 'text-base-content/40' },
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
        <h1 class="text-xl font-bold tracking-tight">Trace</h1>
      </div>
      <span
        v-if="trace?.session"
        class="inline-flex items-center text-xs font-medium px-2 py-1 rounded-md"
        :class="[
          stateStyle[trace.session.state]?.bg || 'bg-warning/10',
          stateStyle[trace.session.state]?.text || 'text-warning',
        ]"
      >
        {{ trace.session.state }}
      </span>
      <NuxtLink :to="`/report/${id}`">
        <button class="btn btn-ghost btn-sm gap-1.5 rounded-lg">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
          Report
        </button>
      </NuxtLink>
    </div>

    <!-- Loading -->
    <div v-if="status === 'pending'" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16 text-base-content/40">
        <span class="loading loading-spinner loading-md" />
        <p class="mt-3 text-sm">Loading trace...</p>
      </div>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-8 text-error/60" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
        <p class="text-sm text-base-content/50 mt-2">Failed to load trace data</p>
      </div>
    </div>

    <template v-else-if="trace">
      <!-- Session Info -->
      <div v-if="trace.session" class="bg-base-100 rounded-xl border border-base-content/5 p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">RCA ID</div>
          <div class="text-sm font-mono mt-1 truncate">{{ id }}</div>
        </div>
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">Alarm</div>
          <div class="text-sm mt-1">{{ trace.session.alarmName }}</div>
        </div>
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">Engine</div>
          <div class="text-sm font-mono mt-1">{{ trace.session.engine }}</div>
        </div>
        <div>
          <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">Created</div>
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
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">Output</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.detail)" />
                </div>

                <div v-if="selectedNode.error" class="mt-3">
                  <div class="text-[11px] font-medium text-error/60 uppercase tracking-wider mb-1.5">Error</div>
                  <div class="prose prose-xs max-w-none bg-error/5 text-error rounded-lg p-3 break-words" v-html="md(selectedNode.error)" />
                </div>

                <div v-if="selectedNode.metadata" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">Metadata</div>
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
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">Evidence</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.evidenceSummary)" />
                </div>

                <div v-if="selectedNode.judgmentReasoning" class="mt-3">
                  <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-1.5">Reasoning</div>
                  <div class="prose prose-xs max-w-none bg-base-200/60 rounded-lg p-3 break-words" v-html="md(selectedNode.judgmentReasoning)" />
                </div>
              </template>
            </template>

            <template v-else>
              <div class="flex flex-col items-center justify-center h-full text-base-content/30 gap-3">
                <svg xmlns="http://www.w3.org/2000/svg" class="size-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" /></svg>
                <p class="text-sm">Select a node to view details</p>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
