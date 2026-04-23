<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeData } from '~/composables/useTraceGraph'

defineProps<{ data: NodeData }>()

const statusClass: Record<string, string> = {
  CONFIRMED: 'border-success bg-success/10',
  REJECTED: 'border-error bg-error/10 opacity-60',
  NEEDS_INVESTIGATION: 'border-warning bg-warning/10 animate-pulse',
  PENDING: 'border-base-300 bg-base-100',
}

const categoryBadge: Record<string, string> = {
  DEPLOYMENT: 'badge-info',
  INFRASTRUCTURE: 'badge-warning',
  TRAFFIC: 'badge-success',
  DEPENDENCY: 'badge-error',
  CONFIGURATION: 'badge-ghost',
}
</script>

<template>
  <div
    class="rounded-lg border-2 px-3 py-2 min-w-[200px] max-w-[220px] shadow-sm cursor-pointer transition-shadow hover:shadow-md"
    :class="statusClass[data.status] || 'border-base-300 bg-base-100'"
  >
    <div class="flex items-center gap-1 mb-1">
      <span v-if="data.category" class="badge badge-xs" :class="categoryBadge[data.category] || 'badge-ghost'">
        {{ data.category }}
      </span>
      <span
        v-if="data.confidenceScore !== undefined"
        class="text-[10px] font-mono ml-auto"
        :class="data.confidenceScore >= 0.8 ? 'text-success' : data.confidenceScore >= 0.5 ? 'text-warning' : 'opacity-50'"
      >
        {{ (data.confidenceScore * 100).toFixed(0) }}%
      </span>
    </div>
    <div class="text-xs leading-tight line-clamp-2">{{ data.label }}</div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>
