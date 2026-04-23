<script setup lang="ts">
const { data: sessions, status, refresh } = useFetch('/api/sessions')

const stateColor: Record<string, string> = {
  COMPLETED: 'success',
  FAILED: 'error',
  ALARM_RECEIVED: 'info',
  SCOPING: 'warning',
  HYPOTHESIS_GENERATION: 'warning',
  HYPOTHESIS_PRIORITIZATION: 'warning',
  EVIDENCE_COLLECTION: 'warning',
  HYPOTHESIS_VALIDATION: 'warning',
  REPORT_GENERATION: 'warning',
  REMEDIATION: 'warning',
  VERIFICATION: 'warning',
}

const columns = [
  { key: 'state', label: 'State' },
  { key: 'alarmName', label: 'Alarm' },
  { key: 'engine', label: 'Engine' },
  { key: 'rootCause', label: 'Root Cause' },
  { key: 'createdAt', label: 'Created' },
  { key: 'actions', label: '' },
]

function formatTime(iso: string) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString()
}

useHead({ title: 'RCA Dashboard' })
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h1 class="text-2xl font-bold text-white">RCA Sessions</h1>
      <UButton icon="i-lucide-refresh-cw" variant="ghost" color="neutral" :loading="status === 'pending'" @click="refresh()">
        Refresh
      </UButton>
    </div>

    <!-- Stats -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-4" v-if="sessions">
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-sm text-gray-400">Total</div>
        <div class="text-2xl font-bold text-white">{{ sessions.length }}</div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-sm text-gray-400">Completed</div>
        <div class="text-2xl font-bold text-green-400">{{ sessions.filter(s => s.state === 'COMPLETED').length }}</div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-sm text-gray-400">Failed</div>
        <div class="text-2xl font-bold text-red-400">{{ sessions.filter(s => s.state === 'FAILED').length }}</div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-sm text-gray-400">In Progress</div>
        <div class="text-2xl font-bold text-yellow-400">{{ sessions.filter(s => !['COMPLETED', 'FAILED'].includes(s.state)).length }}</div>
      </div>
    </div>

    <!-- Sessions Table -->
    <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <div v-if="status === 'pending' && !sessions" class="p-8 text-center text-gray-400">
        <UIcon name="i-lucide-loader-2" class="size-6 animate-spin" />
        <p class="mt-2">Loading sessions...</p>
      </div>
      <div v-else-if="!sessions?.length" class="p-8 text-center text-gray-400">
        No RCA sessions found.
      </div>
      <table v-else class="w-full">
        <thead>
          <tr class="border-b border-gray-800">
            <th v-for="col in columns" :key="col.key" class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
              {{ col.label }}
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-800">
          <tr v-for="session in sessions" :key="session.rcaId" class="hover:bg-gray-800/50 transition-colors">
            <td class="px-4 py-3">
              <UBadge :color="(stateColor[session.state] as any) || 'neutral'" variant="subtle" size="sm">
                {{ session.state }}
              </UBadge>
            </td>
            <td class="px-4 py-3">
              <div class="text-sm text-white font-medium">{{ session.alarmName }}</div>
              <div class="text-xs text-gray-500 font-mono">{{ session.rcaId.slice(0, 8) }}...</div>
            </td>
            <td class="px-4 py-3">
              <UBadge :color="session.engine === 'cc-headless' ? 'info' : 'neutral'" variant="subtle" size="sm">
                {{ session.engine }}
              </UBadge>
            </td>
            <td class="px-4 py-3 max-w-xs">
              <p v-if="session.rootCause" class="text-sm text-gray-300 truncate">{{ session.rootCause }}</p>
              <p v-else-if="session.errorReason" class="text-sm text-red-400 truncate">{{ session.errorReason }}</p>
              <p v-else class="text-sm text-gray-500">-</p>
            </td>
            <td class="px-4 py-3 text-sm text-gray-400 whitespace-nowrap">{{ formatTime(session.createdAt) }}</td>
            <td class="px-4 py-3">
              <NuxtLink :to="`/report/${session.rcaId}`">
                <UButton icon="i-lucide-file-text" variant="ghost" color="neutral" size="xs" />
              </NuxtLink>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
