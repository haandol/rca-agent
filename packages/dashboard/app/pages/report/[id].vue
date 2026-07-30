<script setup lang="ts">
import { marked } from 'marked';

const route = useRoute();
const id = route.params.id as string;
const engine = (route.query.engine as string) || '';

const {
  data: report,
  status,
  error,
} = useFetch(`/api/reports/${id}`, {
  query: engine ? { engine } : undefined,
});
const { data: sessions } = useFetch('/api/sessions');
// The playbook is part of this report, not a separate artifact: a person
// approves the procedure while reading the analysis that produced it.
const { data: playbook } = useFetch(`/api/playbooks/${id}`, {
  query: engine ? { engine } : undefined,
});
const { data: executionHistory, refresh: refreshExecutions } = useFetch(
  `/api/executions/${id}`,
);

const session = computed(() => {
  if (!sessions.value) return undefined;
  if (engine)
    return sessions.value.find((s) => s.rcaId === id && s.engine === engine);
  return sessions.value.find((s) => s.rcaId === id);
});
const renderedHtml = computed(() => {
  if (!report.value?.markdown) return '';
  return marked.parse(report.value.markdown) as string;
});

const executions = computed(() => executionHistory.value?.executions ?? []);
const latest = computed(() => executions.value[0] ?? null);
const inFlight = computed(() =>
  executions.value.some((execution) =>
    ['PENDING_APPROVAL', 'EXECUTING', 'VERIFYING'].includes(execution.state),
  ),
);
const executionSteps = computed(() => playbook.value?.execution_steps ?? []);

// Approval is only meaningful when there is a confirmed procedure to approve and
// nothing already running against it.
const canApprove = computed(
  () =>
    Boolean(session.value?.engine) &&
    session.value?.state === 'COMPLETED' &&
    executionSteps.value.length > 0 &&
    !inFlight.value,
);

const approving = ref(false);
const approvalError = ref('');
const approvalModal = ref<HTMLDialogElement | null>(null);

async function approveExecution() {
  approving.value = true;
  approvalError.value = '';
  try {
    await $fetch('/api/executions', {
      method: 'POST',
      body: { rcaId: id, engine: session.value?.engine },
    });
    approvalModal.value?.close();
    await refreshExecutions();
  } catch (err) {
    approvalError.value =
      (err as { statusMessage?: string })?.statusMessage ||
      '실행 요청에 실패했습니다.';
  } finally {
    approving.value = false;
  }
}

function executionBadgeClass(state: string): string {
  if (state === 'RESOLVED') return 'badge-success';
  if (state === 'UNRESOLVED' || state === 'FAILED') return 'badge-error';
  if (state === 'EXECUTING' || state === 'VERIFYING') return 'badge-warning';
  return 'badge-ghost';
}

useHead({ title: () => `Report ${id.slice(0, 8)}` });
</script>

<template>
  <div class="space-y-5">
    <!-- Header -->
    <div class="flex items-center gap-3">
      <NuxtLink to="/">
        <button class="btn btn-ghost btn-sm btn-circle rounded-lg">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            class="size-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="2"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              d="M15 19l-7-7 7-7"
            />
          </svg>
        </button>
      </NuxtLink>
      <div class="flex-1">
        <h1 class="text-xl font-bold tracking-tight">RCA 보고서</h1>
      </div>
      <span
        v-if="session"
        class="badge badge-sm"
        :class="
          session.state === 'COMPLETED'
            ? 'badge-success'
            : session.state === 'FAILED'
              ? 'badge-error'
              : 'badge-warning'
        "
      >
        {{ session.state }}
      </span>
    </div>

    <!-- Session Info -->
    <div
      v-if="session"
      class="bg-base-100 rounded-xl border border-base-content/5 p-4 grid grid-cols-2 md:grid-cols-4 gap-4"
    >
      <div>
        <div
          class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
        >
          RCA 아이디
        </div>
        <div class="text-sm font-mono mt-1 truncate">{{ id }}</div>
      </div>
      <div>
        <div
          class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
        >
          알람
        </div>
        <div class="text-sm mt-1">{{ session.alarmName }}</div>
      </div>
      <div>
        <div
          class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
        >
          엔진
        </div>
        <div class="text-sm font-mono mt-1">{{ session.engine }}</div>
      </div>
      <div>
        <div
          class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
        >
          채택 여부
        </div>
        <span
          class="badge badge-sm mt-1"
          :class="session.confirmed ? 'badge-success' : 'badge-ghost'"
        >
          {{ session.confirmed ? '채택' : '미채택' }}
        </span>
      </div>
    </div>

    <!-- Report Content -->
    <div
      v-if="status === 'pending'"
      class="bg-base-100 rounded-xl border border-base-content/5"
    >
      <div
        class="flex flex-col items-center justify-center py-16 text-base-content/40"
      >
        <span class="loading loading-spinner loading-md" />
        <p class="mt-3 text-sm">보고서 로딩 중...</p>
      </div>
    </div>

    <div
      v-else-if="error"
      class="bg-base-100 rounded-xl border border-base-content/5"
    >
      <div class="flex flex-col items-center justify-center py-16">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          class="size-8 text-error/60"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="1.5"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
        <p class="text-sm text-base-content/50 mt-2">
          {{
            error.statusCode === 404
              ? 'S3에 보고서가 없습니다.'
              : '보고서 로드에 실패했습니다.'
          }}
        </p>
        <p class="text-[11px] text-base-content/30 font-mono mt-1">
          reports/{{ id }}.md
        </p>
      </div>
    </div>

    <div
      v-else-if="report"
      class="bg-base-100 rounded-xl border border-base-content/5 p-6 md:p-8"
    >
      <div class="prose max-w-none" v-html="renderedHtml" />
    </div>

    <!-- Playbook execution — the approval gate -->
    <div
      v-if="report && playbook"
      class="bg-base-100 rounded-xl border border-base-content/5 p-6 md:p-8 space-y-4"
    >
      <div class="flex items-start gap-3">
        <div class="flex-1">
          <h2 class="text-lg font-bold tracking-tight">실행 절차</h2>
          <p class="text-sm text-base-content/50 mt-1">
            승인하면 별도 실행 에이전트가 아래 절차를 순서대로 수행합니다.
          </p>
        </div>
        <span
          class="badge badge-sm"
          :class="
            playbook.verification_status === 'DRAFT'
              ? 'badge-warning'
              : 'badge-success'
          "
          title="실행과 회고를 거치기 전의 플레이북은 초안입니다"
        >
          {{ playbook.verification_status }}
        </span>
      </div>

      <div
        v-if="playbook.revisedByExecutionId"
        class="text-xs text-info/80 bg-info/10 rounded-lg px-3 py-2"
      >
        이전 실행의 회고가 이 절차를 교정했습니다.
      </div>

      <div
        v-if="!executionSteps.length"
        class="text-sm text-base-content/50 bg-base-200/40 rounded-lg px-4 py-3"
      >
        확정된 근본원인이 없어 실행할 절차가 없습니다. 추가 조사가 필요합니다.
      </div>

      <ol v-else class="space-y-3">
        <li
          v-for="(step, index) in executionSteps"
          :key="step.step_id"
          class="border border-base-content/5 rounded-lg p-4"
        >
          <div class="flex items-center gap-2">
            <span class="badge badge-xs badge-ghost font-mono">
              {{ step.step_id }}
            </span>
            <span class="text-xs text-base-content/40"
              >{{ index + 1 }}단계</span
            >
          </div>
          <p class="text-sm mt-2">{{ step.action }}</p>
          <p v-if="step.intent" class="text-xs text-base-content/50 mt-1">
            의도: {{ step.intent }}
          </p>
          <p class="text-xs text-base-content/50 mt-1">
            성공 판정: {{ step.success_criteria }}
          </p>
        </li>
      </ol>

      <div class="flex items-center gap-3 pt-2">
        <button
          class="btn btn-primary btn-sm rounded-lg"
          :disabled="!canApprove"
          @click="approvalModal?.showModal()"
        >
          실행 승인
        </button>
        <span v-if="inFlight" class="text-xs text-warning/80">
          이미 진행 중인 실행이 있습니다.
        </span>
        <span
          v-else-if="!executionSteps.length"
          class="text-xs text-base-content/40"
        >
          승인할 절차가 없습니다.
        </span>
        <span
          v-else-if="session?.state !== 'COMPLETED'"
          class="text-xs text-base-content/40"
        >
          분석이 완료되지 않았습니다.
        </span>
      </div>

      <!-- Execution history: a failed attempt's evidence stays readable -->
      <div v-if="executions.length" class="pt-2 space-y-2">
        <h3
          class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
        >
          실행 이력
        </h3>
        <div
          v-for="execution in executions"
          :key="execution.executionId"
          class="flex flex-wrap items-center gap-2 border border-base-content/5 rounded-lg px-3 py-2"
        >
          <span
            class="badge badge-xs"
            :class="executionBadgeClass(execution.state)"
          >
            {{ execution.stateLabel }}
          </span>
          <span class="text-xs text-base-content/50">
            {{ execution.attempt }}회차 · 절차
            {{ execution.attemptedStepCount }}건
          </span>
          <span
            v-if="execution.blockedCount"
            class="badge badge-xs badge-warning"
          >
            차단 {{ execution.blockedCount }}
          </span>
          <span
            v-if="execution.failedStepCount"
            class="badge badge-xs badge-error"
          >
            실패 {{ execution.failedStepCount }}
          </span>
          <span
            v-if="execution.errorReason"
            class="text-xs text-base-content/40 truncate max-w-md"
          >
            {{ execution.errorReason }}
          </span>
          <NuxtLink
            v-if="execution.retrospectiveStatus"
            :to="`/retrospective/${id}/${execution.executionId}`"
            class="badge badge-xs badge-info"
          >
            회고 {{ execution.retrospectiveStatus }}
          </NuxtLink>
        </div>
      </div>
    </div>

    <!-- Approval confirmation: writing starts only after this -->
    <dialog ref="approvalModal" class="modal">
      <div class="modal-box rounded-xl">
        <h3 class="text-lg font-bold">실행을 승인하시겠습니까?</h3>
        <p class="text-sm text-base-content/60 mt-2">
          실행 에이전트가 {{ executionSteps.length }}개 절차를 대상 리소스에
          수행합니다. 되돌릴 수 없는 조치는 서버가 거부하고 해당 절차는 수동
          조치로 남습니다.
        </p>
        <p v-if="latest" class="text-xs text-base-content/40 mt-2">
          이 리포트의 마지막 실행: {{ latest.stateLabel }}
        </p>
        <p v-if="approvalError" class="text-sm text-error mt-3">
          {{ approvalError }}
        </p>
        <div class="modal-action">
          <button
            class="btn btn-ghost btn-sm rounded-lg"
            :disabled="approving"
            @click="approvalModal?.close()"
          >
            취소
          </button>
          <button
            class="btn btn-primary btn-sm rounded-lg"
            :disabled="approving"
            @click="approveExecution()"
          >
            <span v-if="approving" class="loading loading-spinner loading-xs" />
            승인하고 실행
          </button>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop">
        <button>close</button>
      </form>
    </dialog>
  </div>
</template>
