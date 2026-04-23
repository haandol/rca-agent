<script setup lang="ts">
const { data: sessions, status, refresh } = useFetch('/api/sessions')

const stateColor: Record<string, string> = {
  COMPLETED: 'badge-success',
  FAILED: 'badge-error',
  ALARM_RECEIVED: 'badge-info',
  SCOPING: 'badge-warning',
  HYPOTHESIS_GENERATION: 'badge-warning',
  HYPOTHESIS_PRIORITIZATION: 'badge-warning',
  EVIDENCE_COLLECTION: 'badge-warning',
  HYPOTHESIS_VALIDATION: 'badge-warning',
  REPORT_GENERATION: 'badge-warning',
  REMEDIATION: 'badge-warning',
  VERIFICATION: 'badge-warning',
}

function formatTime(iso: string) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString()
}

const deleteTarget = ref<string | null>(null)
const deleting = ref(false)
const deleteModalRef = ref<HTMLDialogElement | null>(null)

function openDeleteModal(rcaId: string) {
  deleteTarget.value = rcaId
  deleteModalRef.value?.showModal()
}

function closeDeleteModal() {
  deleteTarget.value = null
  deleteModalRef.value?.close()
}

async function deleteSession() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await $fetch(`/api/sessions/${deleteTarget.value}`, { method: 'DELETE' })
    await refresh()
  } finally {
    deleting.value = false
    closeDeleteModal()
  }
}

useHead({ title: 'RCA Dashboard' })
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h1 class="text-2xl font-bold">RCA Sessions</h1>
      <button class="btn btn-ghost btn-sm gap-2" :class="{ 'loading': status === 'pending' }" @click="refresh()">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
        Refresh
      </button>
    </div>

    <!-- Stats -->
    <div class="stats stats-vertical sm:stats-horizontal shadow w-full" v-if="sessions">
      <div class="stat">
        <div class="stat-title">Total</div>
        <div class="stat-value text-2xl">{{ sessions.length }}</div>
      </div>
      <div class="stat">
        <div class="stat-title">Completed</div>
        <div class="stat-value text-2xl text-success">{{ sessions.filter(s => s.state === 'COMPLETED').length }}</div>
      </div>
      <div class="stat">
        <div class="stat-title">Failed</div>
        <div class="stat-value text-2xl text-error">{{ sessions.filter(s => s.state === 'FAILED').length }}</div>
      </div>
      <div class="stat">
        <div class="stat-title">In Progress</div>
        <div class="stat-value text-2xl text-warning">{{ sessions.filter(s => !['COMPLETED', 'FAILED'].includes(s.state)).length }}</div>
      </div>
    </div>

    <!-- Sessions Table -->
    <div class="card bg-base-100 shadow">
      <div v-if="status === 'pending' && !sessions" class="card-body items-center text-base-content/60">
        <span class="loading loading-spinner loading-md" />
        <p>Loading sessions...</p>
      </div>
      <div v-else-if="!sessions?.length" class="card-body items-center text-base-content/60">
        No RCA sessions found.
      </div>
      <div v-else class="overflow-x-auto">
        <table class="table">
          <thead>
            <tr>
              <th>State</th>
              <th>Alarm</th>
              <th>Engine</th>
              <th>Root Cause</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="session in sessions" :key="session.rcaId" class="hover">
              <td>
                <span class="badge badge-sm" :class="stateColor[session.state] || 'badge-ghost'">
                  {{ session.state }}
                </span>
              </td>
              <td>
                <div class="font-medium">{{ session.alarmName }}</div>
                <div class="text-xs opacity-50 font-mono">{{ session.rcaId.slice(0, 8) }}...</div>
              </td>
              <td>
                <span class="badge badge-sm" :class="session.engine === 'cc-headless' ? 'badge-info' : 'badge-ghost'">
                  {{ session.engine }}
                </span>
              </td>
              <td class="max-w-xs">
                <p v-if="session.rootCause" class="text-sm truncate">{{ session.rootCause }}</p>
                <p v-else-if="session.errorReason" class="text-sm text-error truncate">{{ session.errorReason }}</p>
                <p v-else class="text-sm opacity-40">-</p>
              </td>
              <td class="text-sm opacity-60 whitespace-nowrap">{{ formatTime(session.createdAt) }}</td>
              <td>
                <div class="flex gap-1">
                  <NuxtLink :to="`/trace/${session.rcaId}`">
                    <button class="btn btn-ghost btn-xs" title="Trace">
                      <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 3v12M18 9a3 3 0 11-6 0 3 3 0 016 0zM6 21a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
                    </button>
                  </NuxtLink>
                  <NuxtLink :to="`/report/${session.rcaId}`">
                    <button class="btn btn-ghost btn-xs" title="Report">
                      <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
                    </button>
                  </NuxtLink>
                  <button class="btn btn-ghost btn-xs text-error" title="Delete" @click="openDeleteModal(session.rcaId)">
                    <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Delete Modal -->
    <dialog ref="deleteModalRef" class="modal">
      <div class="modal-box">
        <h3 class="font-bold text-lg flex items-center gap-2">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-5 text-error" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
          세션 삭제
        </h3>
        <p class="py-4">
          세션 <span class="font-mono font-medium">{{ deleteTarget?.slice(0, 8) }}...</span>의
          모든 데이터(세션, 트레이스, 가설, 리포트)가 삭제됩니다. 이 작업은 되돌릴 수 없습니다.
        </p>
        <div class="modal-action">
          <button class="btn btn-ghost" @click="closeDeleteModal()">취소</button>
          <button class="btn btn-error" :class="{ 'loading': deleting }" @click="deleteSession()">삭제</button>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop"><button type="submit">close</button></form>
    </dialog>
  </div>
</template>
