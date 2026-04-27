<script setup lang="ts">
const { data: sessions, status, refresh } = useFetch('/api/sessions')

const stateStyle: Record<string, { bg: string; text: string; dot: string }> = {
  COMPLETED: { bg: 'bg-success/10', text: 'text-success', dot: 'bg-success' },
  FAILED: { bg: 'bg-error/10', text: 'text-error', dot: 'bg-error' },
  CANCELLED: { bg: 'bg-base-content/5', text: 'text-base-content/60', dot: 'bg-base-content/40' },
  OUTDATED: { bg: 'bg-base-content/5', text: 'text-base-content/40', dot: 'bg-base-content/20' },
  ALARM_RECEIVED: { bg: 'bg-info/10', text: 'text-info', dot: 'bg-info animate-pulse' },
  SCOPING: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  HYPOTHESIS_GENERATION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  HYPOTHESIS_PRIORITIZATION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  EVIDENCE_COLLECTION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  HYPOTHESIS_VALIDATION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  REPORT_GENERATION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  REMEDIATION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
  VERIFICATION: { bg: 'bg-warning/10', text: 'text-warning', dot: 'bg-warning animate-pulse' },
}

const TERMINAL_STATES = ['COMPLETED', 'FAILED', 'CANCELLED', 'OUTDATED']

const STATE_LABEL: Record<string, string> = {
  ALARM_RECEIVED: '알람 수신',
  SCOPING: '스코핑',
  HYPOTHESIS_GENERATION: '가설 생성',
  HYPOTHESIS_PRIORITIZATION: '우선순위 결정',
  EVIDENCE_COLLECTION: '증거 수집',
  HYPOTHESIS_VALIDATION: '가설 검증',
  REPORT_GENERATION: '보고서 생성',
  REMEDIATION: '자동 복구',
  VERIFICATION: '복구 검증',
  ANALYZING: '분석 중',
  COMPLETED: '완료',
  FAILED: '실패',
  CANCELLED: '중단됨',
  OUTDATED: '만료됨',
}

const STATE_DESC: Record<string, string> = {
  ALARM_RECEIVED: 'CloudWatch 알람이 SNS→SQS 경로로 수신되어 RCA 세션이 생성된 초기 상태',
  SCOPING: '알람 메트릭과 관련 메트릭을 조회하여 영향범위와 심각도를 판단하는 단계',
  HYPOTHESIS_GENERATION: '스코핑 결과를 바탕으로 3~5개의 근본원인 가설을 생성하는 단계',
  HYPOTHESIS_PRIORITIZATION: '생성된 가설의 우선순위를 결정하고 상위 빔을 선택하는 단계',
  EVIDENCE_COLLECTION: 'CloudWatch, CloudTrail, GitHub 등에서 가설 검증을 위한 증거를 수집하는 단계',
  HYPOTHESIS_VALIDATION: '수집된 증거를 바탕으로 가설을 확정(CONFIRMED), 기각(REJECTED), 또는 추가 조사(NEEDS_INVESTIGATION)로 분류하는 단계',
  REPORT_GENERATION: '확정된 근본원인과 증거를 기반으로 한글 RCA 보고서를 생성하는 단계',
  REMEDIATION: '근본원인에 맞는 자동 복구 조치(장애 리셋, ECS 재배포 등)를 수행하는 단계',
  VERIFICATION: '복구 후 메트릭을 재조회하여 정상화 여부를 확인하는 단계',
  ANALYZING: 'CC Headless 엔진이 프롬프트 주도로 전체 파이프라인을 자율 실행 중인 상태',
  COMPLETED: 'RCA 분석이 정상 완료되어 보고서가 S3에 저장되고 알림이 발송된 상태',
  FAILED: '파이프라인 실행 중 오류가 발생하여 분석이 중단된 상태',
  CANCELLED: '사용자가 대시보드에서 수동으로 분석을 중단한 상태',
  OUTDATED: 'TTL 만료 등으로 더 이상 유효하지 않은 세션',
}

const stateModalRef = ref<HTMLDialogElement | null>(null)
const stateModalTarget = ref('')

function openStateModal(state: string) {
  stateModalTarget.value = state
  stateModalRef.value?.showModal()
}

function formatTime(iso: string) {
  if (!iso) return '-'
  const d = new Date(iso)
  const now = new Date()
  const diff = now.getTime() - d.getTime()
  if (diff < 60000) return '방금 전'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}분 전`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}시간 전`
  return d.toLocaleDateString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const stats = computed(() => {
  if (!sessions.value) return null
  const total = sessions.value.length
  const completed = sessions.value.filter(s => s.state === 'COMPLETED').length
  const failed = sessions.value.filter(s => s.state === 'FAILED').length
  const inProgress = sessions.value.filter(s => !TERMINAL_STATES.includes(s.state)).length
  return { total, completed, failed, inProgress }
})

type SortKey = 'state' | 'alarmName' | 'engine' | 'createdAt'
type SortDir = 'asc' | 'desc'

const sortKey = ref<SortKey>('createdAt')
const sortDir = ref<SortDir>('desc')

function toggleSort(key: SortKey) {
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortDir.value = key === 'createdAt' ? 'desc' : 'asc'
  }
}

const sortedSessions = computed(() => {
  if (!sessions.value) return []
  return [...sessions.value].sort((a, b) => {
    const key = sortKey.value
    const dir = sortDir.value === 'asc' ? 1 : -1
    const va = (a as Record<string, unknown>)[key] ?? ''
    const vb = (b as Record<string, unknown>)[key] ?? ''
    if (va < vb) return -1 * dir
    if (va > vb) return 1 * dir
    return 0
  })
})

const cancelTarget = ref<string | null>(null)
const cancelling = ref(false)
const cancelModalRef = ref<HTMLDialogElement | null>(null)

function openCancelModal(rcaId: string) {
  cancelTarget.value = rcaId
  cancelModalRef.value?.showModal()
}

function closeCancelModal() {
  cancelTarget.value = null
  cancelModalRef.value?.close()
}

async function cancelSession() {
  if (!cancelTarget.value) return
  cancelling.value = true
  try {
    await $fetch(`/api/sessions/${cancelTarget.value}/cancel`, { method: 'POST' })
    await refresh()
  } finally {
    cancelling.value = false
    closeCancelModal()
  }
}

const deleteTarget = ref<{ rcaId: string; engine: string } | null>(null)
const deleting = ref(false)
const deleteModalRef = ref<HTMLDialogElement | null>(null)

function openDeleteModal(rcaId: string, engine: string) {
  deleteTarget.value = { rcaId, engine }
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
    await $fetch(`/api/sessions/${deleteTarget.value.rcaId}?engine=${deleteTarget.value.engine}`, { method: 'DELETE' })
    await refresh()
  } finally {
    deleting.value = false
    closeDeleteModal()
  }
}

useHead({ title: 'RCA 대시보드' })
</script>

<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">세션 목록</h1>
        <p class="text-sm text-base-content/50 mt-0.5">Root Cause Analysis 파이프라인 실행 이력</p>
      </div>
      <button
        class="btn btn-sm btn-ghost gap-2 rounded-lg"
        :class="{ 'loading': status === 'pending' }"
        @click="refresh()"
      >
        <svg xmlns="http://www.w3.org/2000/svg" class="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
        새로고침
      </button>
    </div>

    <!-- Stats -->
    <div v-if="stats" class="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <div class="stat-card">
        <div class="text-xs font-medium text-base-content/50 uppercase tracking-wider">전체</div>
        <div class="text-2xl font-bold mt-1">{{ stats.total }}</div>
      </div>
      <div class="stat-card">
        <div class="text-xs font-medium text-success uppercase tracking-wider">완료</div>
        <div class="text-2xl font-bold mt-1 text-success">{{ stats.completed }}</div>
      </div>
      <div class="stat-card">
        <div class="text-xs font-medium text-error uppercase tracking-wider">실패</div>
        <div class="text-2xl font-bold mt-1 text-error">{{ stats.failed }}</div>
      </div>
      <div class="stat-card">
        <div class="text-xs font-medium text-warning uppercase tracking-wider">진행 중</div>
        <div class="text-2xl font-bold mt-1 text-warning">{{ stats.inProgress }}</div>
      </div>
    </div>

    <!-- Sessions Table -->
    <div class="bg-base-100 rounded-xl border border-base-content/5 overflow-hidden">
      <div v-if="status === 'pending' && !sessions" class="flex flex-col items-center justify-center py-16 text-base-content/40">
        <span class="loading loading-spinner loading-md" />
        <p class="mt-3 text-sm">세션 로딩 중...</p>
      </div>
      <div v-else-if="!sessions?.length" class="flex flex-col items-center justify-center py-16 text-base-content/40">
        <svg xmlns="http://www.w3.org/2000/svg" class="size-10 mb-2 opacity-40" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
        <p class="text-sm">세션이 없습니다</p>
      </div>
      <table v-else class="table w-full">
        <thead>
          <tr class="border-b border-base-content/5">
            <th class="sortable-th pl-5" @click="toggleSort('state')">
              상태
              <span v-if="sortKey === 'state'" class="sort-indicator">{{ sortDir === 'asc' ? '▲' : '▼' }}</span>
            </th>
            <th class="sortable-th" @click="toggleSort('alarmName')">
              알람
              <span v-if="sortKey === 'alarmName'" class="sort-indicator">{{ sortDir === 'asc' ? '▲' : '▼' }}</span>
            </th>
            <th class="sortable-th" @click="toggleSort('engine')">
              엔진
              <span v-if="sortKey === 'engine'" class="sort-indicator">{{ sortDir === 'asc' ? '▲' : '▼' }}</span>
            </th>
            <th class="text-xs font-medium text-base-content/50 uppercase tracking-wider">결과</th>
            <th class="sortable-th" @click="toggleSort('createdAt')">
              시간
              <span v-if="sortKey === 'createdAt'" class="sort-indicator">{{ sortDir === 'asc' ? '▲' : '▼' }}</span>
            </th>
            <th class="w-28"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="session in sortedSessions" :key="`${session.rcaId}-${session.engine}`" class="session-row border-b border-base-content/5 last:border-0">
            <td class="pl-5">
              <span
                class="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-md cursor-pointer hover:ring-1 hover:ring-base-content/20 transition-shadow"
                :class="[
                  stateStyle[session.state]?.bg || 'bg-base-content/5',
                  stateStyle[session.state]?.text || 'text-base-content/50',
                ]"
                @click.stop="openStateModal(session.state)"
              >
                <span class="size-1.5 rounded-full" :class="stateStyle[session.state]?.dot || 'bg-base-content/30'" />
                {{ STATE_LABEL[session.state] || session.state }}
              </span>
            </td>
            <td>
              <div class="font-medium text-sm">{{ session.alarmName }}</div>
              <div class="text-[11px] text-base-content/35 font-mono mt-0.5">{{ session.rcaId.slice(0, 8) }}</div>
            </td>
            <td>
              <span class="text-xs font-mono text-base-content/60">{{ session.engine }}</span>
            </td>
            <td class="max-w-xs">
              <p v-if="session.rootCause" class="text-sm truncate">{{ session.rootCause }}</p>
              <p v-else-if="session.errorReason" class="text-sm text-error/80 truncate">{{ session.errorReason }}</p>
              <span v-else class="text-base-content/25">-</span>
            </td>
            <td class="text-xs text-base-content/45 whitespace-nowrap">{{ formatTime(session.createdAt) }}</td>
            <td>
              <div class="flex items-center justify-end gap-0.5 pr-2">
                <NuxtLink :to="`/trace/${session.rcaId}?engine=${session.engine}`">
                  <button class="action-btn" title="트레이스">
                    <svg xmlns="http://www.w3.org/2000/svg" class="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>
                  </button>
                </NuxtLink>
                <NuxtLink :to="`/report/${session.rcaId}?engine=${session.engine}`">
                  <button class="action-btn" title="보고서">
                    <svg xmlns="http://www.w3.org/2000/svg" class="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
                  </button>
                </NuxtLink>
                <NuxtLink :to="`/playbook/${session.rcaId}?engine=${session.engine}`">
                  <button class="action-btn" title="플레이북">
                    <svg xmlns="http://www.w3.org/2000/svg" class="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
                  </button>
                </NuxtLink>
                <button
                  v-if="!TERMINAL_STATES.includes(session.state)"
                  class="action-btn text-warning"
                  title="중단"
                  @click="openCancelModal(session.rcaId)"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" class="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /><path stroke-linecap="round" stroke-linejoin="round" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" /></svg>
                </button>
                <button class="action-btn text-error/70" title="삭제" @click="openDeleteModal(session.rcaId, session.engine)">
                  <svg xmlns="http://www.w3.org/2000/svg" class="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Cancel Modal -->
    <dialog ref="cancelModalRef" class="modal">
      <div class="modal-box max-w-sm">
        <h3 class="font-bold text-lg">분석 중단</h3>
        <p class="py-4 text-sm text-base-content/70">
          세션 <span class="font-mono font-medium text-base-content">{{ cancelTarget?.slice(0, 8) }}</span>의
          RCA 파이프라인을 중단합니다.<br />
          다음 단계 전환 시점에 파이프라인이 종료됩니다.
        </p>
        <div class="modal-action">
          <button class="btn btn-ghost btn-sm" @click="closeCancelModal()">닫기</button>
          <button class="btn btn-warning btn-sm" :class="{ 'loading': cancelling }" @click="cancelSession()">중단</button>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop"><button type="submit">close</button></form>
    </dialog>

    <!-- Delete Modal -->
    <dialog ref="deleteModalRef" class="modal">
      <div class="modal-box max-w-sm">
        <h3 class="font-bold text-lg">세션 삭제</h3>
        <p class="py-4 text-sm text-base-content/70">
          세션 <span class="font-mono font-medium text-base-content">{{ deleteTarget?.rcaId.slice(0, 8) }}</span>
          <span class="font-mono text-xs text-base-content/50">({{ deleteTarget?.engine }})</span>의
          데이터가 삭제됩니다. 이 작업은 되돌릴 수 없습니다.
        </p>
        <div class="modal-action">
          <button class="btn btn-ghost btn-sm" @click="closeDeleteModal()">취소</button>
          <button class="btn btn-error btn-sm" :class="{ 'loading': deleting }" @click="deleteSession()">삭제</button>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop"><button type="submit">close</button></form>
    </dialog>

    <!-- State Description Modal -->
    <dialog ref="stateModalRef" class="modal">
      <div class="modal-box max-w-lg">
        <h3 class="font-bold text-lg mb-4">파이프라인 상태 설명</h3>
        <div class="space-y-2 max-h-96 overflow-y-auto">
          <div
            v-for="(desc, state) in STATE_DESC"
            :key="state"
            class="flex gap-3 p-2.5 rounded-lg"
            :class="stateModalTarget === state ? 'bg-base-content/5 ring-1 ring-base-content/10' : ''"
          >
            <span
              class="inline-flex items-center gap-1 text-[11px] font-medium px-1.5 py-0.5 rounded shrink-0 h-fit mt-0.5"
              :class="[
                stateStyle[state]?.bg || 'bg-warning/10',
                stateStyle[state]?.text || 'text-warning',
              ]"
            >
              <span class="size-1.5 rounded-full" :class="stateStyle[state]?.dot || 'bg-warning'" />
              {{ STATE_LABEL[state] || state }}
            </span>
            <p class="text-xs text-base-content/70 leading-relaxed">{{ desc }}</p>
          </div>
        </div>
        <div class="modal-action">
          <form method="dialog"><button class="btn btn-ghost btn-sm">닫기</button></form>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop"><button type="submit">close</button></form>
    </dialog>
  </div>
</template>
