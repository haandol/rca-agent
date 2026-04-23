<script setup lang="ts">
defineProps<{
  span: {
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
}>()

function formatDuration(ms: number | null): string {
  if (ms === null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  return new Date(iso).toLocaleTimeString()
}
</script>

<template>
  <div class="space-y-3 text-sm">
    <div class="grid grid-cols-2 gap-3">
      <div>
        <div class="text-xs text-gray-500 uppercase">시작 시각</div>
        <div class="text-gray-300 font-mono text-xs">{{ formatTime(span.startTime) }}</div>
      </div>
      <div>
        <div class="text-xs text-gray-500 uppercase">소요 시간</div>
        <div class="text-gray-300 font-mono text-xs">{{ formatDuration(span.durationMs) }}</div>
      </div>
    </div>

    <div v-if="span.inputSummary">
      <div class="text-xs text-gray-500 uppercase mb-1">입력</div>
      <div class="text-gray-400 bg-gray-800/50 rounded px-2 py-1 font-mono text-xs break-all">{{ span.inputSummary }}</div>
    </div>

    <div v-if="span.outputSummary">
      <div class="text-xs text-gray-500 uppercase mb-1">출력</div>
      <div class="text-gray-300 bg-gray-800/50 rounded px-2 py-1 font-mono text-xs break-all">{{ span.outputSummary }}</div>
    </div>

    <div v-if="span.error">
      <div class="text-xs text-gray-500 uppercase mb-1">오류</div>
      <div class="text-red-400 bg-red-950/30 rounded px-2 py-1 font-mono text-xs break-all">{{ span.error }}</div>
    </div>

    <div v-if="span.metadata">
      <div class="text-xs text-gray-500 uppercase mb-1">메타데이터</div>
      <div class="bg-gray-800/50 rounded px-2 py-1 space-y-0.5">
        <div v-for="(val, key) in span.metadata" :key="String(key)" class="flex gap-2 text-xs">
          <span class="text-gray-500">{{ key }}:</span>
          <span class="text-gray-300 font-mono">{{ val }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
