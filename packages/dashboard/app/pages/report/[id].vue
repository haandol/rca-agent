<script setup lang="ts">
const route = useRoute()
const id = route.params.id as string

const { data: report, status, error } = useFetch(`/api/reports/${id}`)
const { data: sessions } = useFetch('/api/sessions')

const session = computed(() => sessions.value?.find(s => s.rcaId === id))

useHead({ title: () => `Report ${id.slice(0, 8)}` })
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center gap-3">
      <NuxtLink to="/">
        <UButton icon="i-lucide-arrow-left" variant="ghost" color="neutral" size="sm" />
      </NuxtLink>
      <h1 class="text-xl font-bold text-white">RCA Report</h1>
      <UBadge v-if="session" :color="session.state === 'COMPLETED' ? 'success' : session.state === 'FAILED' ? 'error' : 'warning'" variant="subtle">
        {{ session.state }}
      </UBadge>
    </div>

    <!-- Session Info -->
    <div v-if="session" class="bg-gray-900 border border-gray-800 rounded-xl p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
      <div>
        <div class="text-xs text-gray-500 uppercase">RCA ID</div>
        <div class="text-sm text-white font-mono">{{ id }}</div>
      </div>
      <div>
        <div class="text-xs text-gray-500 uppercase">Alarm</div>
        <div class="text-sm text-white">{{ session.alarmName }}</div>
      </div>
      <div>
        <div class="text-xs text-gray-500 uppercase">Engine</div>
        <div class="text-sm text-white">{{ session.engine }}</div>
      </div>
      <div>
        <div class="text-xs text-gray-500 uppercase">Confirmed</div>
        <div class="text-sm">
          <UBadge :color="session.confirmed ? 'success' : 'neutral'" variant="subtle" size="sm">
            {{ session.confirmed ? 'Yes' : 'No' }}
          </UBadge>
        </div>
      </div>
    </div>

    <!-- Report Content -->
    <div v-if="status === 'pending'" class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-gray-400">
      <UIcon name="i-lucide-loader-2" class="size-6 animate-spin" />
      <p class="mt-2">Loading report...</p>
    </div>

    <div v-else-if="error" class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
      <UIcon name="i-lucide-file-x" class="size-8 text-red-400" />
      <p class="mt-2 text-gray-400">{{ error.statusCode === 404 ? 'Report not found in S3.' : 'Failed to load report.' }}</p>
      <p class="text-sm text-gray-500 mt-1">reports/{{ id }}.md</p>
    </div>

    <div v-else-if="report" class="bg-gray-900 border border-gray-800 rounded-xl p-6 prose prose-invert prose-sm max-w-none">
      <div v-html="renderMarkdown(report.markdown)" />
    </div>
  </div>
</template>

<script lang="ts">
function renderMarkdown(md: string): string {
  return md
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/^(?!<[hup]|<li|<ul)(.+)$/gm, '<p>$1</p>')
}
</script>
