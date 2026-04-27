<script setup lang="ts">
import { marked } from 'marked'

function md(text: string | undefined | null): string {
  if (!text) return ''
  const normalized = text
    .replace(/\\n/g, '\n')
    .replace(/(?<!\n)(\d+)\.\s/g, '\n$1. ')
    .trim()
  return marked.parse(normalized, { async: false, breaks: true }) as string
}

const route = useRoute()
const id = route.params.id as string
const engine = (route.query.engine as string) || ''

const { data: playbook, status, error } = useFetch(`/api/playbooks/${id}`, {
  query: engine ? { engine } : undefined,
})

const { data: sessions } = useFetch('/api/sessions')

const session = computed(() => {
  if (!sessions.value) return undefined
  if (engine) return sessions.value.find(s => s.rcaId === id && s.engine === engine)
  return sessions.value.find(s => s.rcaId === id)
})

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

useHead({ title: () => `Playbook ${id.slice(0, 8)}` })
</script>

<template>
  <div class="space-y-5">
    <!-- Header -->
    <div class="flex items-center gap-3">
      <NuxtLink to="/">
        <button class="btn btn-ghost btn-sm btn-circle rounded-lg">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7" /></svg>
        </button>
      </NuxtLink>
      <div class="flex-1">
        <h1 class="text-xl font-bold tracking-tight">플레이북</h1>
      </div>
      <span v-if="playbook" class="badge badge-sm" :class="{
        'badge-success': playbook.spanStatus === 'COMPLETED',
        'badge-error': playbook.spanStatus === 'FAILED',
        'badge-warning': playbook.spanStatus === 'RUNNING',
      }">{{ playbook.spanStatus }}</span>
    </div>

    <!-- Session Info -->
    <div v-if="session" class="bg-base-100 rounded-xl border border-base-content/5 p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
      <div>
        <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">RCA 아이디</div>
        <div class="text-sm font-mono mt-1 truncate">{{ id }}</div>
      </div>
      <div>
        <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">알람</div>
        <div class="text-sm mt-1">{{ session.alarmName }}</div>
      </div>
      <div>
        <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">엔진</div>
        <div class="text-sm font-mono mt-1">{{ session.engine }}</div>
      </div>
      <div>
        <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">소요 시간</div>
        <div class="text-sm font-mono mt-1">{{ playbook ? formatDuration(playbook.durationMs) : '-' }}</div>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="status === 'pending'" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16 text-base-content/40">
        <span class="loading loading-spinner loading-md" />
        <p class="mt-3 text-sm">플레이북 로딩 중...</p>
      </div>
    </div>

    <!-- Error / Not found -->
    <div v-else-if="error" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-8 text-error/60" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
        <p class="text-sm text-base-content/50 mt-2">플레이북이 없습니다</p>
        <p class="text-[11px] text-base-content/30 mt-1">RCA가 완료된 세션에서만 플레이북이 생성됩니다.</p>
      </div>
    </div>

    <!-- Playbook: FAILED -->
    <template v-else-if="playbook && playbook.spanStatus === 'FAILED'">
      <div class="bg-base-100 rounded-xl border border-error/20 p-6">
        <div class="flex items-center gap-2 mb-3">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-5 text-error" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
          <h2 class="font-bold text-error">플레이북 생성 실패</h2>
        </div>
        <div v-if="playbook.error" class="text-sm bg-error/5 text-error rounded-lg p-4 font-mono break-words">{{ playbook.error }}</div>
        <p v-else class="text-sm text-base-content/60">플레이북 생성 중 오류가 발생했습니다.</p>
      </div>
    </template>

    <!-- Playbook Content -->
    <template v-else-if="playbook">
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <!-- Left column -->
        <div class="space-y-4">
          <!-- Failure type -->
          <div v-if="playbook.failure_type" class="bg-base-100 rounded-xl border border-base-content/5 p-5">
            <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-2">장애 유형</div>
            <span class="badge badge-outline">{{ playbook.failure_type }}</span>
          </div>

          <!-- Symptom pattern -->
          <div v-if="playbook.symptom_pattern" class="bg-base-100 rounded-xl border border-base-content/5 p-5">
            <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-2">증상 패턴</div>
            <div class="prose prose-sm max-w-none" v-html="md(playbook.symptom_pattern)" />
          </div>

          <!-- Verification steps -->
          <div v-if="playbook.verification_steps?.length" class="bg-base-100 rounded-xl border border-base-content/5 p-5">
            <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-2">검증 절차</div>
            <div v-for="(step, i) in playbook.verification_steps" :key="i" class="prose prose-sm max-w-none [&:not(:last-child)]:mb-3">
              <div v-html="md(step)" />
            </div>
          </div>
        </div>

        <!-- Right column -->
        <div class="space-y-4">
          <!-- Temporary mitigation -->
          <div v-if="playbook.temporary_mitigation" class="bg-warning/5 rounded-xl border border-warning/15 p-5">
            <div class="text-[11px] font-medium text-warning uppercase tracking-wider mb-2">임시 완화 조치</div>
            <div class="prose prose-sm max-w-none" v-html="md(playbook.temporary_mitigation)" />
          </div>

          <!-- Permanent remediation -->
          <div v-if="playbook.permanent_remediation" class="bg-success/5 rounded-xl border border-success/15 p-5">
            <div class="text-[11px] font-medium text-success uppercase tracking-wider mb-2">영구 복구 방안</div>
            <div class="prose prose-sm max-w-none" v-html="md(playbook.permanent_remediation)" />
          </div>

          <!-- Prevention measures -->
          <div v-if="playbook.prevention_measures?.length" class="bg-base-100 rounded-xl border border-base-content/5 p-5">
            <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-2">재발 방지</div>
            <div v-for="(m, i) in playbook.prevention_measures" :key="i" class="prose prose-sm max-w-none [&:not(:last-child)]:mb-2">
              <div v-html="md(m)" />
            </div>
          </div>

          <!-- Tags -->
          <div v-if="playbook.tags?.length" class="bg-base-100 rounded-xl border border-base-content/5 p-5">
            <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider mb-2">태그</div>
            <div class="flex flex-wrap gap-1.5">
              <span v-for="tag in playbook.tags" :key="tag" class="badge badge-sm badge-ghost">{{ tag }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Playbook ID footer -->
      <div v-if="playbook.playbook_id" class="text-[11px] text-base-content/30 font-mono text-right">
        playbook_id: {{ playbook.playbook_id }}
      </div>

      <!-- No metadata fallback -->
      <div v-if="!playbook.failure_type && !playbook.symptom_pattern && !playbook.error" class="bg-base-100 rounded-xl border border-base-content/5 p-6">
        <div class="flex flex-col items-center justify-center py-8 text-base-content/40">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-8 mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
          <p class="text-sm">플레이북 메타데이터가 없습니다</p>
          <p class="text-[11px] mt-1">이 세션은 메타데이터 기록 기능 배포 전에 실행되었을 수 있습니다.</p>
          <p v-if="playbook.outputSummary" class="text-xs font-mono mt-3 text-base-content/50">{{ playbook.outputSummary }}</p>
        </div>
      </div>
    </template>
  </div>
</template>
