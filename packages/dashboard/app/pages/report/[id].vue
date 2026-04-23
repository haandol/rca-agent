<script setup lang="ts">
import { marked } from 'marked'

const route = useRoute()
const id = route.params.id as string

const { data: report, status, error } = useFetch(`/api/reports/${id}`)
const { data: sessions } = useFetch('/api/sessions')

const session = computed(() => sessions.value?.find(s => s.rcaId === id))
const renderedHtml = computed(() => {
  if (!report.value?.markdown) return ''
  return marked.parse(report.value.markdown) as string
})

useHead({ title: () => `Report ${id.slice(0, 8)}` })
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center gap-3">
      <NuxtLink to="/">
        <button class="btn btn-ghost btn-sm btn-circle">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7" /></svg>
        </button>
      </NuxtLink>
      <h1 class="text-xl font-bold">RCA Report</h1>
      <span v-if="session" class="badge" :class="session.state === 'COMPLETED' ? 'badge-success' : session.state === 'FAILED' ? 'badge-error' : 'badge-warning'">
        {{ session.state }}
      </span>
    </div>

    <!-- Session Info -->
    <div v-if="session" class="card bg-base-100 shadow">
      <div class="card-body p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
          <div class="text-xs uppercase opacity-50">RCA ID</div>
          <div class="text-sm font-mono">{{ id }}</div>
        </div>
        <div>
          <div class="text-xs uppercase opacity-50">Alarm</div>
          <div class="text-sm">{{ session.alarmName }}</div>
        </div>
        <div>
          <div class="text-xs uppercase opacity-50">Engine</div>
          <div class="text-sm">{{ session.engine }}</div>
        </div>
        <div>
          <div class="text-xs uppercase opacity-50">Confirmed</div>
          <span class="badge badge-sm" :class="session.confirmed ? 'badge-success' : 'badge-ghost'">
            {{ session.confirmed ? 'Yes' : 'No' }}
          </span>
        </div>
      </div>
    </div>

    <!-- Report Content -->
    <div v-if="status === 'pending'" class="card bg-base-100 shadow">
      <div class="card-body items-center text-base-content/60">
        <span class="loading loading-spinner loading-md" />
        <p>Loading report...</p>
      </div>
    </div>

    <div v-else-if="error" class="card bg-base-100 shadow">
      <div class="card-body items-center">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-8 text-error" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
        <p class="text-base-content/60">{{ error.statusCode === 404 ? 'Report not found in S3.' : 'Failed to load report.' }}</p>
        <p class="text-xs opacity-40">reports/{{ id }}.md</p>
      </div>
    </div>

    <div v-else-if="report" class="card bg-base-100 shadow">
      <div class="card-body prose max-w-none">
        <div v-html="renderedHtml" />
      </div>
    </div>
  </div>
</template>
