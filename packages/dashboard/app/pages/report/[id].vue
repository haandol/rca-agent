<script setup lang="ts">
import { marked } from 'marked'

const route = useRoute()
const id = route.params.id as string
const engine = (route.query.engine as string) || ''

const { data: report, status, error } = useFetch(`/api/reports/${id}`)
const { data: sessions } = useFetch('/api/sessions')

const session = computed(() => {
  if (!sessions.value) return undefined
  if (engine) return sessions.value.find(s => s.rcaId === id && s.engine === engine)
  return sessions.value.find(s => s.rcaId === id)
})
const renderedHtml = computed(() => {
  if (!report.value?.markdown) return ''
  return marked.parse(report.value.markdown) as string
})

useHead({ title: () => `Report ${id.slice(0, 8)}` })
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
        <h1 class="text-xl font-bold tracking-tight">RCA 보고서</h1>
      </div>
      <span v-if="session" class="badge badge-sm" :class="session.state === 'COMPLETED' ? 'badge-success' : session.state === 'FAILED' ? 'badge-error' : 'badge-warning'">
        {{ session.state }}
      </span>
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
        <div class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider">확정 여부</div>
        <span class="badge badge-sm mt-1" :class="session.confirmed ? 'badge-success' : 'badge-ghost'">
          {{ session.confirmed ? '예' : '아니오' }}
        </span>
      </div>
    </div>

    <!-- Report Content -->
    <div v-if="status === 'pending'" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16 text-base-content/40">
        <span class="loading loading-spinner loading-md" />
        <p class="mt-3 text-sm">보고서 로딩 중...</p>
      </div>
    </div>

    <div v-else-if="error" class="bg-base-100 rounded-xl border border-base-content/5">
      <div class="flex flex-col items-center justify-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-8 text-error/60" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
        <p class="text-sm text-base-content/50 mt-2">{{ error.statusCode === 404 ? 'S3에 보고서가 없습니다.' : '보고서 로드에 실패했습니다.' }}</p>
        <p class="text-[11px] text-base-content/30 font-mono mt-1">reports/{{ id }}.md</p>
      </div>
    </div>

    <div v-else-if="report" class="bg-base-100 rounded-xl border border-base-content/5 p-6 md:p-8">
      <div class="prose max-w-none" v-html="renderedHtml" />
    </div>
  </div>
</template>
