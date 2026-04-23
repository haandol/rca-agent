<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeData } from '~/composables/useTraceGraph'

defineProps<{ data: NodeData }>()

const statusClass: Record<string, string> = {
  COMPLETED: 'border-success bg-success/10',
  FAILED: 'border-error bg-error/10',
  RUNNING: 'border-warning bg-warning/10 animate-pulse',
  TIMED_OUT: 'border-base-300 bg-base-200',
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
</script>

<template>
  <div
    class="rounded-lg border-2 px-3 py-2 text-center min-w-[140px] shadow-sm cursor-pointer transition-shadow hover:shadow-md"
    :class="statusClass[data.status] || 'border-base-300 bg-base-100'"
  >
    <div class="text-xs font-semibold truncate">{{ data.label }}</div>
    <div v-if="data.durationMs" class="text-[10px] opacity-60 font-mono mt-0.5">{{ formatDuration(data.durationMs) }}</div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>
