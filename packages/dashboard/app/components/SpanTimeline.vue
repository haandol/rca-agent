<script setup lang="ts">
interface SpanItem {
  spanId: string
  spanType: string
  spanStatus: string
  parentSpanId: string | null
  loopIndex: number | null
  startTime: string
  endTime: string | null
  durationMs: number | null
  inputSummary: string
  outputSummary: string
  error: string | null
  metadata: Record<string, unknown> | null
}

const props = defineProps<{ spans: SpanItem[] }>()

const expandedId = ref<string | null>(null)

function toggle(spanId: string) {
  expandedId.value = expandedId.value === spanId ? null : spanId
}

const statusColor: Record<string, string> = {
  COMPLETED: 'bg-green-500',
  FAILED: 'bg-red-500',
  RUNNING: 'bg-yellow-500',
  TIMED_OUT: 'bg-gray-500',
}

const statusBadgeColor: Record<string, string> = {
  COMPLETED: 'success',
  FAILED: 'error',
  RUNNING: 'warning',
  TIMED_OUT: 'neutral',
}

interface TreeSpan extends SpanItem {
  children: TreeSpan[]
  depth: number
}

const spanTree = computed<TreeSpan[]>(() => {
  const byId = new Map<string, TreeSpan>()
  for (const s of props.spans) {
    byId.set(s.spanId, { ...s, children: [], depth: 0 })
  }
  const roots: TreeSpan[] = []
  for (const s of byId.values()) {
    if (s.parentSpanId && byId.has(s.parentSpanId)) {
      const parent = byId.get(s.parentSpanId)!
      s.depth = parent.depth + 1
      parent.children.push(s)
    } else {
      roots.push(s)
    }
  }
  return roots
})

function flatten(nodes: TreeSpan[]): TreeSpan[] {
  const result: TreeSpan[] = []
  for (const n of nodes) {
    result.push(n)
    result.push(...flatten(n.children))
  }
  return result
}

const flatSpans = computed(() => flatten(spanTree.value))

const totalDuration = computed(() => {
  if (!props.spans.length) return 1
  const starts = props.spans.map((s) => new Date(s.startTime).getTime())
  const ends = props.spans
    .filter((s) => s.endTime)
    .map((s) => new Date(s.endTime!).getTime())
  const min = Math.min(...starts)
  const max = ends.length ? Math.max(...ends) : min + 1000
  return Math.max(max - min, 1)
})

const minStart = computed(() => {
  if (!props.spans.length) return 0
  return Math.min(...props.spans.map((s) => new Date(s.startTime).getTime()))
})

function barLeft(span: SpanItem): string {
  const start = new Date(span.startTime).getTime()
  return `${((start - minStart.value) / totalDuration.value) * 100}%`
}

function barWidth(span: SpanItem): string {
  const dur = span.durationMs || 100
  const pct = (dur / totalDuration.value) * 100
  return `${Math.max(pct, 0.5)}%`
}

function formatDuration(ms: number | null): string {
  if (ms === null) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

const spanLabelMap: Record<string, string> = {
  SCOPING: '스코핑',
  HYPOTHESIS_GENERATION: '가설 생성',
  PRIORITIZATION: '우선순위 결정',
  EVIDENCE_COLLECTION: '증거 수집',
  VALIDATION: '가설 검증',
  BRANCHING: '가설 분기',
  TERMINATION: '종료 판단',
  REPORT: '보고서 생성',
  PLAYBOOK: '플레이북 생성',
  REMEDIATION: '자동 복구',
  VERIFICATION: '복구 검증',
  NOTIFICATION: '알림 발송',
  VALIDATION_LOOP: '검증 루프',
}

function spanLabel(type: string): string {
  return spanLabelMap[type] || type.replace(/_/g, ' ')
}
</script>

<template>
  <div class="space-y-0.5">
    <div
      v-for="span in flatSpans"
      :key="span.spanId"
    >
      <div
        class="flex items-center gap-2 py-1.5 px-2 rounded cursor-pointer hover:bg-gray-800/50 transition-colors"
        :style="{ paddingLeft: `${span.depth * 24 + 8}px` }"
        @click="toggle(span.spanId)"
      >
        <!-- Type label -->
        <span class="text-xs text-gray-400 w-40 shrink-0 uppercase truncate">
          {{ spanLabel(span.spanType) }}
          <span v-if="span.loopIndex !== null" class="text-gray-600">#{{ span.loopIndex }}</span>
        </span>

        <!-- Bar -->
        <div class="flex-1 h-5 bg-gray-800/30 rounded relative overflow-hidden">
          <div
            class="absolute h-full rounded"
            :class="statusColor[span.spanStatus] || 'bg-gray-600'"
            :style="{ left: barLeft(span), width: barWidth(span), opacity: 0.7 }"
          />
        </div>

        <!-- Duration + Status -->
        <span class="text-xs text-gray-500 font-mono w-16 text-right shrink-0">
          {{ formatDuration(span.durationMs) }}
        </span>
        <UBadge
          :color="(statusBadgeColor[span.spanStatus] as any) || 'neutral'"
          variant="subtle"
          size="xs"
        >
          {{ span.spanStatus }}
        </UBadge>
      </div>

      <!-- Detail panel -->
      <div
        v-if="expandedId === span.spanId"
        class="ml-12 mr-4 mb-2 p-3 bg-gray-900 border border-gray-800 rounded-lg"
      >
        <SpanDetail :span="span" />
      </div>
    </div>
  </div>
</template>
