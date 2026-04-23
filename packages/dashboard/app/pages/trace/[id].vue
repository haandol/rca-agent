<script setup lang="ts">
const route = useRoute()
const id = route.params.id as string

const { data: trace, status, error } = useFetch(`/api/traces/${id}`)

const stateColor: Record<string, string> = {
  COMPLETED: 'success',
  FAILED: 'error',
  ALARM_RECEIVED: 'info',
  SCOPING: 'warning',
  HYPOTHESIS_GENERATION: 'warning',
  EVIDENCE_COLLECTION: 'warning',
  HYPOTHESIS_VALIDATION: 'warning',
  REPORT_GENERATION: 'warning',
  REMEDIATION: 'warning',
  VERIFICATION: 'warning',
}

function formatTime(iso: string) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString()
}

useHead({ title: () => `Trace ${id.slice(0, 8)}` })
</script>

<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center gap-3">
      <NuxtLink to="/">
        <UButton icon="i-lucide-arrow-left" variant="ghost" color="neutral" size="sm" />
      </NuxtLink>
      <h1 class="text-xl font-bold text-white">실행 트레이스</h1>
      <UBadge
        v-if="trace?.session"
        :color="(stateColor[trace.session.state] as any) || 'neutral'"
        variant="subtle"
      >
        {{ trace.session.state }}
      </UBadge>
      <div class="flex-1" />
      <NuxtLink :to="`/report/${id}`">
        <UButton icon="i-lucide-file-text" variant="ghost" color="neutral" size="sm">보고서</UButton>
      </NuxtLink>
    </div>

    <!-- Loading -->
    <div v-if="status === 'pending'" class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-gray-400">
      <UIcon name="i-lucide-loader-2" class="size-6 animate-spin" />
      <p class="mt-2">트레이스 로딩 중...</p>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
      <UIcon name="i-lucide-alert-triangle" class="size-8 text-red-400" />
      <p class="mt-2 text-gray-400">트레이스 데이터 로드 실패</p>
    </div>

    <template v-else-if="trace">
      <!-- Session Info -->
      <div v-if="trace.session" class="bg-gray-900 border border-gray-800 rounded-xl p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
          <div class="text-xs text-gray-500 uppercase">RCA ID</div>
          <div class="text-sm text-white font-mono truncate">{{ id }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-500 uppercase">알람</div>
          <div class="text-sm text-white">{{ trace.session.alarmName }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-500 uppercase">엔진</div>
          <div class="text-sm text-white">{{ trace.session.engine }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-500 uppercase">생성일시</div>
          <div class="text-sm text-gray-300">{{ formatTime(trace.session.createdAt) }}</div>
        </div>
      </div>

      <!-- Span Timeline -->
      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div class="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
          <UIcon name="i-lucide-gantt-chart" class="size-4 text-gray-400" />
          <h2 class="text-sm font-semibold text-white">파이프라인 타임라인</h2>
          <span class="text-xs text-gray-500">{{ trace.spans.length }}개 스팬</span>
        </div>
        <div v-if="trace.spans.length" class="p-2">
          <SpanTimeline :spans="trace.spans" />
        </div>
        <div v-else class="p-6 text-center text-sm text-gray-500">
          이 세션의 스팬 데이터가 없습니다.
        </div>
      </div>

      <!-- Hypothesis Tree -->
      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div class="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
          <UIcon name="i-lucide-git-branch" class="size-4 text-gray-400" />
          <h2 class="text-sm font-semibold text-white">가설 트리</h2>
          <span class="text-xs text-gray-500">{{ trace.hypotheses.length }}개 가설</span>
        </div>
        <div v-if="trace.hypotheses.length" class="p-3">
          <HypothesisTree :hypotheses="trace.hypotheses" />
        </div>
        <div v-else class="p-6 text-center text-sm text-gray-500">
          이 세션의 가설 데이터가 없습니다.
        </div>
      </div>
    </template>
  </div>
</template>
