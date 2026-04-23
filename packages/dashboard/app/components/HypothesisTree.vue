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

const props = defineProps<{ hypotheses: HypothesisItem[] }>()

const tree = computed<HypothesisItem[]>(() => {
  const byId = new Map<string, HypothesisItem>()
  for (const h of props.hypotheses) {
    byId.set(h.hypothesisId, { ...h, children: [] })
  }
  const roots: HypothesisItem[] = []
  for (const h of byId.values()) {
    if (h.parentId && byId.has(h.parentId)) {
      byId.get(h.parentId)!.children!.push(h)
    } else {
      roots.push(h)
    }
  }
  return roots
})

const stats = computed(() => {
  const total = props.hypotheses.length
  const confirmed = props.hypotheses.filter((h) => h.status === 'CONFIRMED').length
  const rejected = props.hypotheses.filter((h) => h.status === 'REJECTED').length
  const investigating = props.hypotheses.filter((h) => h.status === 'NEEDS_INVESTIGATION').length
  const pending = props.hypotheses.filter((h) => h.status === 'PENDING').length
  return { total, confirmed, rejected, investigating, pending }
})
</script>

<template>
  <div class="space-y-3">
    <!-- Stats bar -->
    <div class="flex items-center gap-4 text-xs text-gray-400">
      <span>전체: <span class="text-white font-medium">{{ stats.total }}</span></span>
      <span v-if="stats.confirmed">확정: <span class="text-green-400">{{ stats.confirmed }}</span></span>
      <span v-if="stats.rejected">기각: <span class="text-red-400">{{ stats.rejected }}</span></span>
      <span v-if="stats.investigating">조사중: <span class="text-yellow-400">{{ stats.investigating }}</span></span>
      <span v-if="stats.pending">대기: <span class="text-gray-500">{{ stats.pending }}</span></span>
    </div>

    <!-- Tree -->
    <div v-if="tree.length">
      <HypothesisNode v-for="node in tree" :key="node.hypothesisId" :node="node" />
    </div>
    <div v-else class="text-sm text-gray-500 py-4 text-center">
      가설 데이터가 없습니다.
    </div>
  </div>
</template>
