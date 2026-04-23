<script setup lang="ts">
interface HypothesisItem {
  hypothesisId: string
  treeId: string
  parentId: string | null
  depth: number
  description: string
  category: string
  confidenceScore: number
  status: string
  requiredEvidence: string[]
  referencedPlaybookId: string | null
  evidenceSummary: string
  judgmentReasoning: string
  judgmentConfidence: number | null
  createdAt: string
  updatedAt: string
  children?: HypothesisItem[]
}

defineProps<{ node: HypothesisItem }>()

const expanded = ref(false)

const statusColor: Record<string, string> = {
  CONFIRMED: 'success',
  REJECTED: 'error',
  NEEDS_INVESTIGATION: 'warning',
  PENDING: 'neutral',
}

const categoryColor: Record<string, string> = {
  DEPLOYMENT: 'info',
  INFRASTRUCTURE: 'warning',
  TRAFFIC: 'success',
  DEPENDENCY: 'error',
  CONFIGURATION: 'neutral',
}
</script>

<template>
  <div>
    <div
      class="flex items-start gap-3 py-2 px-3 rounded cursor-pointer hover:bg-gray-800/50 transition-colors"
      @click="expanded = !expanded"
    >
      <!-- Expand icon -->
      <UIcon
        :name="expanded ? 'i-lucide-chevron-down' : 'i-lucide-chevron-right'"
        class="size-4 text-gray-500 mt-0.5 shrink-0"
      />

      <!-- Main content -->
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <UBadge :color="(categoryColor[node.category] as any) || 'neutral'" variant="subtle" size="xs">
            {{ node.category }}
          </UBadge>
          <UBadge :color="(statusColor[node.status] as any) || 'neutral'" variant="subtle" size="xs">
            {{ node.status }}
          </UBadge>
          <span class="text-xs text-gray-500">깊이={{ node.depth }}</span>
        </div>
        <p class="text-sm text-gray-300 mt-1 line-clamp-2">{{ node.description }}</p>
      </div>

      <!-- Confidence -->
      <div class="shrink-0 w-20 text-right">
        <div class="text-xs text-gray-500">신뢰도</div>
        <div class="text-sm font-mono" :class="node.confidenceScore >= 0.8 ? 'text-green-400' : node.confidenceScore >= 0.5 ? 'text-yellow-400' : 'text-gray-400'">
          {{ (node.confidenceScore * 100).toFixed(0) }}%
        </div>
        <div class="w-full h-1 bg-gray-800 rounded mt-1 overflow-hidden">
          <div
            class="h-full rounded"
            :class="node.confidenceScore >= 0.8 ? 'bg-green-500' : node.confidenceScore >= 0.5 ? 'bg-yellow-500' : 'bg-gray-600'"
            :style="{ width: `${node.confidenceScore * 100}%` }"
          />
        </div>
      </div>
    </div>

    <!-- Expanded detail -->
    <div v-if="expanded" class="ml-10 mr-4 mb-2 p-3 bg-gray-900 border border-gray-800 rounded-lg space-y-2 text-sm">
      <div v-if="node.evidenceSummary">
        <div class="text-xs text-gray-500 uppercase mb-1">증거 요약</div>
        <div class="text-gray-400 text-xs bg-gray-800/50 rounded px-2 py-1 break-all">{{ node.evidenceSummary }}</div>
      </div>
      <div v-if="node.judgmentReasoning">
        <div class="text-xs text-gray-500 uppercase mb-1">판단 근거</div>
        <div class="text-gray-400 text-xs bg-gray-800/50 rounded px-2 py-1 break-all">{{ node.judgmentReasoning }}</div>
      </div>
      <div v-if="node.judgmentConfidence !== null">
        <span class="text-xs text-gray-500">판단 신뢰도: </span>
        <span class="text-xs text-gray-300 font-mono">{{ ((node.judgmentConfidence ?? 0) * 100).toFixed(0) }}%</span>
      </div>
      <div v-if="node.requiredEvidence.length">
        <div class="text-xs text-gray-500 uppercase mb-1">필요 증거</div>
        <div class="flex flex-wrap gap-1">
          <UBadge v-for="e in node.requiredEvidence" :key="e" color="neutral" variant="subtle" size="xs">{{ e }}</UBadge>
        </div>
      </div>
      <div class="text-xs text-gray-600 font-mono">id: {{ node.hypothesisId.slice(0, 8) }}</div>
    </div>

    <!-- Children (recursive) -->
    <div v-if="node.children?.length" class="ml-6 border-l border-gray-800 pl-2">
      <HypothesisNode v-for="child in node.children" :key="child.hypothesisId" :node="child" />
    </div>
  </div>
</template>
