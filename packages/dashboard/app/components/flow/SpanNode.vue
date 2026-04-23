<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeData } from '~/composables/useTraceGraph'

defineProps<{ data: NodeData }>()

const statusClass: Record<string, string> = {
  COMPLETED: 'border-success/40 bg-success/8',
  FAILED: 'border-error/40 bg-error/8',
  RUNNING: 'border-warning/40 bg-warning/8 animate-pulse',
  TIMED_OUT: 'border-base-content/10 bg-base-200/50',
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
</script>

<template>
  <div
    class="rounded-xl border px-3.5 py-2 text-center min-w-[140px] cursor-pointer transition-all hover:shadow-md hover:-translate-y-0.5"
    :class="statusClass[data.status] || 'border-base-content/10 bg-base-100'"
  >
    <div class="text-xs font-semibold truncate">{{ data.label }}</div>
    <div v-if="data.durationMs" class="text-[10px] text-base-content/40 font-mono mt-0.5">{{ formatDuration(data.durationMs) }}</div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>
