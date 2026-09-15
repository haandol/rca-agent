<script setup lang="ts">
import { renderMarkdownDocument } from '~/utils/markdown';
import {
  parseCausalChain,
  parseTimeline,
  stripInlineMarkup,
} from '~/utils/causalChain';
import {
  OUTCOME_LABEL,
  OUTCOME_TONE,
  READINESS_LABEL,
  outcomeOf,
} from '~/utils/sessionState';

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
// This page needs one session, so it reads that session rather than the list —
// searching a paged list would miss anything past the first page.
const { data: session, refresh: refreshSession } = useFetch(
  `/api/sessions/${id}`,
  {
    query: engine ? { engine } : undefined,
  },
);
// The playbook is part of this report, not a separate artifact: a person
// approves the procedure while reading the analysis that produced it.
const { data: playbook, refresh: refreshPlaybook } = useFetch(
  `/api/playbooks/${id}`,
  {
    query: engine ? { engine } : undefined,
  },
);
const { data: executionHistory, refresh: refreshExecutions } = useFetch(
  `/api/executions/${id}`,
  {
    query: { engine },
  },
);

const outcome = computed(() =>
  session.value ? outcomeOf(session.value) : null,
);

const rootCause = computed(() => stripInlineMarkup(session.value?.rootCause));

const chain = computed(() => parseCausalChain(report.value?.markdown));
const timeline = computed(() => parseTimeline(report.value?.markdown));
const renderedHtml = computed(() =>
  renderMarkdownDocument(report.value?.markdown),
);

const executions = computed(() => executionHistory.value?.executions ?? []);
const latest = computed(() => executions.value[0] ?? null);
const inFlight = computed(() =>
  executions.value.some((execution) =>
    ['PENDING_APPROVAL', 'EXECUTING', 'VERIFYING'].includes(execution.state),
  ),
);
const executionSteps = computed(() => playbook.value?.execution_steps ?? []);
// Keep legacy prose read-only even while an older API is still deployed.
const hasFixedDefinitions = computed(
  () =>
    executionSteps.value.length > 0 &&
    executionSteps.value.every((value) => {
      const step =
        value !== null && typeof value === 'object'
          ? (value as Record<string, unknown>)
          : {};
      const commands =
        Array.isArray(step.commands) &&
        step.commands.length > 0 &&
        step.commands.every(
          (command) => typeof command === 'string' && command.trim(),
        );
      const metricWait =
        step.metric_wait !== null &&
        typeof step.metric_wait === 'object' &&
        !Array.isArray(step.metric_wait);
      return Boolean(commands) !== metricWait;
    }),
);
// Anything other than the recorded VERIFIED reads as a draft: an unproven
// procedure must never look proven to whoever is deciding to approve it.
const isVerifiedPlaybook = computed(
  () => playbook.value?.verification_status === 'VERIFIED',
);

// Approval is only meaningful when there is a confirmed procedure to approve and
// nothing already running against it. Same conditions the server enforces.
const canApprove = computed(
  () =>
    Boolean(session.value?.engine) &&
    session.value?.state === 'COMPLETED' &&
    session.value?.confirmed === true &&
    executionSteps.value.length > 0 &&
    playbook.value?.executable === true &&
    /^[a-f0-9]{64}$/.test(playbook.value?.playbookDigest ?? '') &&
    hasFixedDefinitions.value &&
    !inFlight.value,
);

/**
 * Whether approving is the thing this page is asking for.
 *
 * A report can be executed again after it resolved, so `canApprove` stays true
 * for a run that is already finished — and giving that the same emphasis as an
 * unapproved report made a resolved incident shout for attention. The loud
 * treatment is reserved for the report nobody has acted on yet; re-running stays
 * available but quiet.
 */
const isPendingDecision = computed(
  () => canApprove.value && session.value?.readiness === 'AWAITING_APPROVAL',
);

/** Why approval is unavailable, in the reader's terms. */
const blockedReason = computed(() => {
  if (canApprove.value) return '';
  if (inFlight.value) return '이미 진행 중인 실행이 있습니다';
  if (!session.value) return '세션 정보를 확인할 수 없어 승인할 수 없습니다';
  if (session.value.confirmed !== true)
    return '근본원인이 확정되지 않아 승인할 수 없습니다';
  if (!hasFixedDefinitions.value)
    return '명령 미생성 또는 실행 정의 불완전 · 새 분석 필요';
  if (!playbook.value?.playbookDigest)
    return '검토한 런북의 버전을 확인할 수 없습니다. 최신 내용을 다시 불러오세요.';
  if (playbook.value?.executable !== true)
    return (
      playbook.value?.validationError ||
      '명령이 확정되지 않아 승인할 수 없습니다. 새 분석이 필요합니다.'
    );
  if (!executionSteps.value.length)
    return (
      playbook.value?.validationError || '승인할 복구 계획 단계가 없습니다'
    );
  if (session.value?.state !== 'COMPLETED') return '분석이 완료되지 않았습니다';
  return '';
});

const approving = ref(false);
const approvalError = ref('');
const approvalModal = ref<HTMLDialogElement | null>(null);
const pendingApprovalId = ref<string | null>(null);
const reviewedDigest = ref('');

function openApproval() {
  if (!canApprove.value) return;
  const digest = playbook.value?.playbookDigest ?? '';
  if (reviewedDigest.value !== digest) pendingApprovalId.value = null;
  reviewedDigest.value = digest;
  approvalError.value = '';
  approvalModal.value?.showModal();
}

async function reloadPlanForReview() {
  approvalModal.value?.close();
  await refreshPlaybook();
  reviewedDigest.value = '';
  pendingApprovalId.value = null;
  approvalError.value = '';
}

async function approveExecution() {
  if (!canApprove.value) {
    approvalError.value = blockedReason.value;
    return;
  }
  if (reviewedDigest.value !== playbook.value?.playbookDigest) {
    approvalError.value =
      '검토 중 런북 내용이 변경되었습니다. 최신 런북을 다시 검토하세요.';
    return;
  }
  approving.value = true;
  approvalError.value = '';
  pendingApprovalId.value ??= crypto.randomUUID();
  try {
    await $fetch('/api/executions', {
      method: 'POST',
      body: {
        rcaId: id,
        engine: session.value?.engine,
        approvalId: pendingApprovalId.value,
        expectedPlaybookDigest: reviewedDigest.value,
      },
    });
    pendingApprovalId.value = null;
    approvalModal.value?.close();
    await refreshExecutions();
  } catch (err) {
    // `$fetch` fills `statusMessage` from the status line ('Conflict'), while the
    // sentence the handler wrote — which of the three approval conditions failed —
    // is in the parsed body. Reading the body is what tells the approver why.
    const data = (
      err as { data?: { statusMessage?: string; message?: string } }
    )?.data;
    approvalError.value =
      data?.statusMessage || data?.message || '실행 요청에 실패했습니다.';
  } finally {
    approving.value = false;
  }
}

function executionTone(state: string): string {
  if (state === 'UNRESOLVED' || state === 'FAILED') return 'text-error';
  if (state === 'EXECUTING' || state === 'VERIFYING') return 'text-info';
  if (state === 'RESOLVED') return 'text-success';
  if (state === 'PENDING_APPROVAL') return 'text-warning';
  return 'text-base-content';
}

/**
 * Rendered in one fixed zone so the server and the browser agree.
 *
 * Left to the ambient timezone, the page reports one clock during server render
 * and another after hydration — and a page about when things happened cannot
 * quietly change its own timestamps.
 */
function formatClock(iso: string): string {
  if (!iso) return '';
  return new Date(iso).toLocaleString('ko-KR', {
    timeZone: 'Asia/Seoul',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

let executionPollTimer: number | undefined;
let executionPolling = false;

async function pollExecutionStatus() {
  if (
    document.visibilityState !== 'visible' ||
    !inFlight.value ||
    executionPolling
  )
    return;
  executionPolling = true;
  try {
    await Promise.allSettled([refreshExecutions(), refreshSession()]);
  } finally {
    executionPolling = false;
  }
}

onMounted(() => {
  executionPollTimer = window.setInterval(pollExecutionStatus, 5000);
  document.addEventListener('visibilitychange', pollExecutionStatus);
});

onBeforeUnmount(() => {
  window.clearInterval(executionPollTimer);
  document.removeEventListener('visibilitychange', pollExecutionStatus);
});

useHead({ title: () => `${session.value?.alarmName ?? '보고서'} · 장애 기록` });
</script>

<template>
  <div>
    <!-- Identity: what broke, when, and what became of it -->
    <header class="mb-7">
      <NuxtLink
        to="/"
        class="mb-4 inline-flex items-center gap-1.5 text-[11px] text-base-content/85 hover:text-primary"
      >
        <span aria-hidden="true">←</span> 기록으로
      </NuxtLink>

      <p class="page-eyebrow">Root Cause Analysis</p>
      <h1 v-if="session" class="page-title break-words">
        {{ session.alarmName }}
      </h1>
      <h1 v-else class="page-title">RCA 보고서</h1>

      <div
        class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-[11px] text-base-content/85"
      >
        <span v-if="outcome" :class="OUTCOME_TONE[outcome]" class="status-chip">
          {{ OUTCOME_LABEL[outcome] }}
        </span>
        <time v-if="session?.createdAt" :datetime="session.createdAt">
          {{ formatClock(session.createdAt) }}
        </time>
        <span class="font-mono">{{ session?.engine }}</span>
        <span
          v-if="session?.confirmed === false"
          class="text-base-content/85"
          title="세션에 기록된 원인 확정 여부"
        >
          원인 미확정
        </span>
        <span class="font-mono text-base-content/85 select-all" :title="id">
          {{ id.slice(0, 8) }}
        </span>
      </div>
    </header>

    <!-- Loading / missing -->
    <div
      v-if="status === 'pending'"
      class="py-20 text-center text-[13px] text-base-content/85"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">보고서를 읽고 있습니다</p>
    </div>

    <div v-else-if="error" class="py-20 text-center">
      <p class="font-semibold text-[17px]">
        {{
          error.statusCode === 404
            ? '이 세션에는 보고서가 없습니다'
            : '보고서를 불러오지 못했습니다'
        }}
      </p>
      <p class="text-[12px] text-base-content/85 font-mono mt-2">
        reports/{{ id }}.md
      </p>
    </div>

    <template v-else-if="report">
      <nav class="section-nav mb-5" aria-label="보고서 섹션 이동">
        <a href="#cause-summary">원인 요약</a>
        <a href="#report-body">보고서 전문 · 근거</a>
        <a v-if="chain.length" href="#causal-chain">원인 사슬</a>
        <a v-if="timeline.length" href="#incident-timeline">타임라인</a>
        <a v-if="playbook" href="#recovery-plan">복구 계획 · 승인</a>
        <a v-if="playbook && executions.length" href="#execution-history"
          >실행 이력</a
        >
      </nav>

      <section
        id="cause-summary"
        class="ops-panel detail-section p-5 sm:p-6 mb-5"
      >
        <div class="flex flex-wrap items-center gap-3 mb-3">
          <h2 class="detail-section-title">원인 요약</h2>
          <span
            v-if="session"
            class="status-chip"
            :class="session.confirmed ? 'text-info' : 'text-warning'"
          >
            {{ session.confirmed ? '원인 확정' : '원인 미확정' }}
          </span>
        </div>
        <p
          v-if="rootCause"
          class="detail-body max-w-[78ch] whitespace-pre-wrap"
        >
          {{ rootCause }}
        </p>
        <p v-else class="detail-empty">
          별도 원인 요약이 제공되지 않았습니다. 아래 보고서 본문에서 분석 결과와
          근거를 확인하세요.
        </p>
      </section>

      <div
        class="grid grid-cols-1 gap-5"
        :class="
          chain.length || timeline.length
            ? 'xl:grid-cols-[minmax(0,1fr)_320px]'
            : ''
        "
      >
        <section
          id="report-body"
          class="ops-panel detail-section min-w-0 p-5 sm:p-6"
          aria-labelledby="report-body-title"
        >
          <h2 id="report-body-title" class="detail-section-title mb-2">
            보고서 전문 · 분석 근거
          </h2>
          <p class="detail-label mb-6">
            영향 범위, 관측 근거와 분석 결론을 원문 순서로 읽습니다.
          </p>
          <div
            v-if="report.markdown?.trim()"
            class="prose-report"
            v-html="renderedHtml"
          />
          <p v-else class="detail-empty">보고서 본문이 비어 있습니다.</p>
        </section>

        <aside v-if="chain.length || timeline.length" class="min-w-0 space-y-5">
          <div
            v-if="chain.length"
            id="causal-chain"
            class="ops-panel detail-section p-5"
          >
            <CausalChain :links="chain" />
          </div>
          <section
            v-if="timeline.length"
            id="incident-timeline"
            class="ops-panel detail-section p-5"
          >
            <h2 class="detail-section-title mb-2">사고 타임라인</h2>
            <p class="detail-label mb-4">보고서에 기록된 시각 기준</p>
            <ol class="space-y-4">
              <li
                v-for="(moment, i) in timeline"
                :key="i"
                class="border-l-2 border-info/40 pl-3"
              >
                <span class="font-mono text-[12px] text-info tabular-nums">{{
                  moment.time
                }}</span>
                <p class="detail-body mt-1">{{ moment.event }}</p>
              </li>
            </ol>
          </section>
        </aside>
      </div>

      <!-- The approval gate. Set apart, because approving starts writes. -->
      <section
        v-if="playbook"
        id="recovery-plan"
        class="ops-panel detail-section mt-5 border-l-[3px] p-5 sm:p-6"
        :class="isPendingDecision ? 'border-warning' : 'border-base-content/15'"
      >
        <div class="flex flex-wrap items-baseline justify-between gap-3 mb-2">
          <h2 class="detail-section-title">
            현재 사고의 복구 계획과 실행 승인
          </h2>
          <span
            class="status-chip"
            :class="isVerifiedPlaybook ? 'text-success' : 'text-warning'"
          >
            {{ isVerifiedPlaybook ? '플레이북 검증됨' : '플레이북 초안' }}
          </span>
        </div>
        <p class="detail-body max-w-[78ch] mb-4">
          각 단계의 작업 목적과 성공 판정 기준, 사전 확정된 명령 또는 관측
          설정을 검토하세요. 승인 가능한 런북만 실행을 요청할 수 있습니다.
        </p>

        <NuxtLink
          v-if="playbook.revisedByExecutionId"
          :to="`/retrospective/${id}/${playbook.revisedByExecutionId}`"
          class="inline-block text-[12px] text-primary hover:underline underline-offset-2 mt-4"
        >
          이전 실행의 회고가 이 절차를 교정했습니다 — 무엇이 왜 바뀌었는지 →
        </NuxtLink>

        <RecoveryPlanSteps
          class="mt-5"
          :steps="executionSteps"
          :validation-error="playbook.validationError"
          :executable="playbook.executable === true"
        />

        <div class="flex flex-wrap items-center gap-4 mt-7">
          <!-- A re-run is offered without being urged: only an undecided report
               gets the filled button. -->
          <button
            class="btn btn-sm"
            :class="isPendingDecision ? 'btn-warning' : 'btn-outline'"
            :disabled="!canApprove"
            @click="openApproval()"
          >
            {{ executions.length ? '다시 실행 승인' : '실행 승인' }}
          </button>
          <span v-if="blockedReason" class="text-[12px] text-base-content/85">
            {{ blockedReason }}
          </span>
          <span
            v-else-if="session?.readiness"
            class="text-[12px] text-base-content/85"
          >
            {{ READINESS_LABEL[session.readiness] }} · 절차
            {{ executionSteps.length }}개
          </span>
        </div>

        <!-- Execution history: a failed attempt's evidence stays readable -->
        <section
          v-if="executions.length"
          id="execution-history"
          class="detail-section mt-8 border-t border-base-content/15 pt-6"
        >
          <h3 class="detail-section-title mb-3">실행 이력</h3>
          <ul class="divide-y divide-base-content/[0.07]">
            <li
              v-for="execution in executions"
              :key="execution.executionId"
              class="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2.5 text-[12.5px]"
            >
              <span class="status-chip" :class="executionTone(execution.state)">
                {{ execution.stateLabel }}
              </span>
              <span class="text-base-content/85">
                {{ execution.attempt }}회차 · 절차
                {{ execution.attemptedStepCount }}건
              </span>
              <span v-if="execution.blockedCount" class="text-base-content/85">
                수동 조치 {{ execution.blockedCount }}
              </span>
              <span
                v-if="execution.failedStepCount"
                class="text-error font-semibold"
              >
                실패 {{ execution.failedStepCount }}
              </span>
              <span
                v-if="execution.errorReason"
                class="text-base-content/85 truncate max-w-[40ch]"
                :title="execution.errorReason"
              >
                {{ execution.errorReason }}
              </span>
              <NuxtLink
                v-if="execution.retrospectiveStatus"
                :to="`/retrospective/${id}/${execution.executionId}`"
                class="text-primary hover:underline underline-offset-2 ml-auto"
              >
                회고 {{ execution.retrospectiveStatus }}
              </NuxtLink>
            </li>
          </ul>
        </section>
      </section>

      <!-- Where the rest lives -->
      <nav class="ops-panel mt-5 flex flex-wrap gap-2 p-3 text-[12px]">
        <NuxtLink
          :to="engine ? `/trace/${id}?engine=${engine}` : `/trace/${id}`"
          class="btn btn-ghost btn-sm"
        >
          분석이 실제로 거친 경로
        </NuxtLink>
        <NuxtLink
          :to="engine ? `/playbook/${id}?engine=${engine}` : `/playbook/${id}`"
          class="btn btn-ghost btn-sm"
        >
          장애 유형별 플레이북 지식
        </NuxtLink>
      </nav>
    </template>

    <!-- Approval confirmation: writing starts only after this -->
    <dialog ref="approvalModal" class="modal">
      <div class="modal-box">
        <h3 class="text-[19px] font-semibold">실행을 승인하시겠습니까?</h3>
        <p class="text-[13.5px] text-base-content/85 mt-3 leading-relaxed">
          실행 에이전트가 {{ executionSteps.length }}개 단계에 기록된 고정 명령
          또는 관측 설정을 순서대로 수행합니다. 검토한 대상과 리전이 맞는지
          확인하세요.
        </p>
        <p v-if="latest" class="text-[12px] text-base-content/85 mt-3">
          이 리포트의 마지막 실행 · {{ latest.stateLabel }}
        </p>
        <p v-if="approvalError" class="text-[13px] text-error mt-3">
          {{ approvalError }}
        </p>
        <button
          v-if="approvalError"
          class="btn btn-ghost btn-sm mt-3"
          :disabled="approving"
          @click="reloadPlanForReview"
        >
          최신 런북을 불러와 다시 검토
        </button>
        <div class="modal-action">
          <button
            class="btn btn-ghost btn-sm"
            :disabled="approving"
            @click="approvalModal?.close()"
          >
            아직 승인하지 않기
          </button>
          <button
            class="btn btn-warning btn-sm"
            :disabled="approving || !canApprove"
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
