<script setup lang="ts">
import { renderMarkdownDocument } from '~/utils/markdown';
import { parseCausalChain, parseTimeline } from '~/utils/causalChain';
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
const { data: session } = useFetch(`/api/sessions/${id}`, {
  query: engine ? { engine } : undefined,
});
// The playbook is part of this report, not a separate artifact: a person
// approves the procedure while reading the analysis that produced it.
const { data: playbook } = useFetch(`/api/playbooks/${id}`, {
  query: engine ? { engine } : undefined,
});
const { data: executionHistory, refresh: refreshExecutions } = useFetch(
  `/api/executions/${id}`,
  {
    query: { engine },
  },
);

const outcome = computed(() =>
  session.value ? outcomeOf(session.value) : null,
);

/**
 * The finding, as a chain rather than a tree.
 *
 * The search ran in parallel and discarded most of what it tried; the report's 5
 * Whys is the linear account of what survived, and it is what a reader needs
 * first. Everything the search did instead stays on the trace page.
 */
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
  if (session.value?.confirmed !== true)
    return '근본원인이 확정되지 않아 승인할 수 없습니다';
  if (!executionSteps.value.length)
    return '근본원인이 확정되지 않아 승인할 절차가 없습니다';
  if (session.value?.state !== 'COMPLETED') return '분석이 완료되지 않았습니다';
  return '';
});

const approving = ref(false);
const approvalError = ref('');
const approvalModal = ref<HTMLDialogElement | null>(null);
const pendingApprovalId = ref<string | null>(null);

async function approveExecution() {
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
  if (state === 'RESOLVED') return 'text-success';
  if (state === 'UNRESOLVED' || state === 'FAILED') return 'text-error';
  if (state === 'EXECUTING' || state === 'VERIFYING') return 'text-primary';
  return 'text-base-content/45';
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

/** The full report body stays available, but folded — the chain leads. */
const showFullReport = ref(false);

useHead({ title: () => `${session.value?.alarmName ?? '보고서'} · 장애 기록` });
</script>

<template>
  <div>
    <!-- Identity: what broke, when, and what became of it -->
    <header class="mb-9">
      <NuxtLink
        to="/"
        class="text-[12px] text-base-content/45 hover:text-primary inline-flex items-center gap-1.5 mb-4"
      >
        <span aria-hidden="true">←</span> 기록으로
      </NuxtLink>

      <h1
        v-if="session"
        class="font-serif text-[26px] leading-tight tracking-tight"
      >
        {{ session.alarmName }}
      </h1>
      <h1 v-else class="font-serif text-[26px] leading-tight">RCA 보고서</h1>

      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 mt-3 text-[12px] text-base-content/50"
      >
        <span v-if="outcome" :class="OUTCOME_TONE[outcome]" class="font-medium">
          {{ OUTCOME_LABEL[outcome] }}
        </span>
        <time v-if="session?.createdAt" :datetime="session.createdAt">
          {{ formatClock(session.createdAt) }}
        </time>
        <span class="font-mono">{{ session?.engine }}</span>
        <span
          v-if="session?.confirmed === false"
          class="text-base-content/40"
          title="확정된 근본원인이 없으면 실행할 절차도 없습니다"
        >
          원인 미확정
        </span>
        <span class="font-mono text-base-content/30 select-all" :title="id">
          {{ id.slice(0, 8) }}
        </span>
      </div>
    </header>

    <!-- Loading / missing -->
    <div
      v-if="status === 'pending'"
      class="py-20 text-center text-[13px] text-base-content/40"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">보고서를 읽고 있습니다</p>
    </div>

    <div v-else-if="error" class="py-20 text-center">
      <p class="font-serif text-[17px]">
        {{
          error.statusCode === 404
            ? '이 세션에는 보고서가 없습니다'
            : '보고서를 불러오지 못했습니다'
        }}
      </p>
      <p class="text-[12px] text-base-content/40 font-mono mt-2">
        reports/{{ id }}.md
      </p>
    </div>

    <template v-else-if="report">
      <div class="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_264px] gap-x-14">
        <!-- The finding leads: one descent, symptom to fix -->
        <div class="min-w-0">
          <CausalChain v-if="chain.length" :links="chain" />

          <!-- No chain parsed: the body is the only account, so open it -->
          <div v-else class="prose-report">
            <div v-html="renderedHtml" />
          </div>

          <!-- The full report, folded behind the chain -->
          <div
            v-if="chain.length"
            class="mt-11 pt-7 border-t border-base-content/10"
          >
            <button
              class="flex items-baseline gap-2 text-[13px] text-base-content/55 hover:text-primary transition-colors"
              :aria-expanded="showFullReport"
              @click="showFullReport = !showFullReport"
            >
              <span class="font-mono text-[11px]">{{
                showFullReport ? '−' : '+'
              }}</span>
              보고서 전문
              <span class="text-[11px] text-base-content/35">
                영향 범위 · 증거 · 기각된 가설
              </span>
            </button>
            <div v-if="showFullReport" class="prose-report mt-7">
              <div v-html="renderedHtml" />
            </div>
          </div>
        </div>

        <!-- What happened when, kept beside the argument rather than inside it -->
        <aside v-if="timeline.length" class="mt-11 lg:mt-0">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-4">
            그날의 시각
          </h2>
          <ol class="space-y-3.5">
            <li v-for="(moment, i) in timeline" :key="i" class="flex gap-3">
              <span
                class="font-mono text-[11px] text-base-content/45 tabular-nums shrink-0 pt-[3px]"
              >
                {{ moment.time }}
              </span>
              <span class="text-[12.5px] leading-snug text-base-content/70">
                {{ moment.event }}
              </span>
            </li>
          </ol>
        </aside>
      </div>

      <!-- The approval gate. Set apart, because approving starts writes. -->
      <section
        v-if="playbook"
        class="mt-14 pt-9 border-t-2"
        :class="
          isPendingDecision ? 'border-primary/35' : 'border-base-content/10'
        "
      >
        <div class="flex flex-wrap items-baseline justify-between gap-3 mb-2">
          <h2 class="font-serif text-[21px] tracking-tight">
            {{
              isPendingDecision
                ? '이 절차를 승인하면 실행이 시작됩니다'
                : '복구 절차'
            }}
          </h2>
          <span
            class="text-[12px]"
            :class="
              isVerifiedPlaybook ? 'text-success' : 'text-base-content/45'
            "
            :title="
              isVerifiedPlaybook
                ? '이 절차는 실행으로 이슈를 해소하고 회고를 거쳤습니다'
                : '실행과 회고를 거치기 전의 플레이북은 초안입니다'
            "
          >
            {{ isVerifiedPlaybook ? '검증된 절차' : '초안' }}
          </span>
        </div>
        <p class="text-[13px] text-base-content/50 max-w-[62ch]">
          승인하면 쓰기 권한을 가진 별도 에이전트가 아래 순서대로 수행합니다.
          되돌릴 수 없는 조치는 서버가 거부하고 수동 조치로 남깁니다.
        </p>

        <NuxtLink
          v-if="playbook.revisedByExecutionId"
          :to="`/retrospective/${id}/${playbook.revisedByExecutionId}`"
          class="inline-block text-[12px] text-info hover:underline underline-offset-2 mt-4"
        >
          이전 실행의 회고가 이 절차를 교정했습니다 — 무엇이 왜 바뀌었는지 →
        </NuxtLink>

        <p
          v-if="!executionSteps.length"
          class="font-serif text-[15px] text-base-content/55 mt-6"
        >
          근본원인이 확정되지 않아 실행할 절차가 없습니다. 추가 조사가
          필요합니다.
        </p>

        <!-- Numbered because the agent runs them in this order. -->
        <ol v-else class="mt-6 divide-y divide-base-content/[0.07]">
          <li
            v-for="(step, index) in executionSteps"
            :key="step.step_id"
            class="step-row"
          >
            <span class="step-ord" aria-hidden="true">{{
              String(index + 1).padStart(2, '0')
            }}</span>
            <p class="text-[14px] leading-snug">{{ step.action }}</p>
            <p
              v-if="step.intent"
              class="font-serif text-[13.5px] text-base-content/55 mt-1.5"
            >
              {{ step.intent }}
            </p>
            <p
              v-if="step.success_criteria"
              class="text-[12px] text-success/85 mt-1.5"
            >
              성공 판정 · {{ step.success_criteria }}
            </p>
          </li>
        </ol>

        <div class="flex flex-wrap items-center gap-4 mt-7">
          <!-- A re-run is offered without being urged: only an undecided report
               gets the filled button. -->
          <button
            class="btn btn-sm"
            :class="isPendingDecision ? 'btn-primary' : 'btn-outline'"
            :disabled="!canApprove"
            @click="approvalModal?.showModal()"
          >
            {{ isPendingDecision ? '실행 승인' : '다시 실행 승인' }}
          </button>
          <span v-if="blockedReason" class="text-[12px] text-base-content/45">
            {{ blockedReason }}
          </span>
          <span
            v-else-if="session?.readiness"
            class="text-[12px] text-base-content/45"
          >
            {{ READINESS_LABEL[session.readiness] }} · 절차
            {{ executionSteps.length }}개
          </span>
        </div>

        <!-- Execution history: a failed attempt's evidence stays readable -->
        <div v-if="executions.length" class="mt-9">
          <h3 class="label-sm uppercase tracking-[0.1em] font-semibold mb-3">
            실행 이력
          </h3>
          <ul class="divide-y divide-base-content/[0.07]">
            <li
              v-for="execution in executions"
              :key="execution.executionId"
              class="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2.5 text-[12.5px]"
            >
              <span
                class="font-medium w-16"
                :class="executionTone(execution.state)"
              >
                {{ execution.stateLabel }}
              </span>
              <span class="text-base-content/50">
                {{ execution.attempt }}회차 · 절차
                {{ execution.attemptedStepCount }}건
              </span>
              <span v-if="execution.blockedCount" class="text-warning">
                수동 조치 {{ execution.blockedCount }}
              </span>
              <span v-if="execution.failedStepCount" class="text-error">
                실패 {{ execution.failedStepCount }}
              </span>
              <span
                v-if="execution.errorReason"
                class="text-base-content/40 truncate max-w-[40ch]"
                :title="execution.errorReason"
              >
                {{ execution.errorReason }}
              </span>
              <NuxtLink
                v-if="execution.retrospectiveStatus"
                :to="`/retrospective/${id}/${execution.executionId}`"
                class="text-info hover:underline underline-offset-2 ml-auto"
              >
                회고 {{ execution.retrospectiveStatus }}
              </NuxtLink>
            </li>
          </ul>
        </div>
      </section>

      <!-- Where the rest lives -->
      <nav
        class="mt-12 pt-6 border-t border-base-content/10 flex flex-wrap gap-x-6 gap-y-2 text-[13px]"
      >
        <NuxtLink
          :to="engine ? `/trace/${id}?engine=${engine}` : `/trace/${id}`"
          class="text-base-content/55 hover:text-primary"
        >
          분석이 실제로 거친 경로
        </NuxtLink>
        <NuxtLink
          :to="engine ? `/playbook/${id}?engine=${engine}` : `/playbook/${id}`"
          class="text-base-content/55 hover:text-primary"
        >
          플레이북 전체
        </NuxtLink>
      </nav>
    </template>

    <!-- Approval confirmation: writing starts only after this -->
    <dialog ref="approvalModal" class="modal">
      <div class="modal-box">
        <h3 class="font-serif text-[19px]">실행을 승인하시겠습니까?</h3>
        <p class="text-[13.5px] text-base-content/65 mt-3 leading-relaxed">
          실행 에이전트가 {{ executionSteps.length }}개 절차를 대상 리소스에
          수행합니다. 되돌릴 수 없는 조치는 서버가 거부하고 해당 절차는 수동
          조치로 남습니다.
        </p>
        <p v-if="latest" class="text-[12px] text-base-content/45 mt-3">
          이 리포트의 마지막 실행 · {{ latest.stateLabel }}
        </p>
        <p v-if="approvalError" class="text-[13px] text-error mt-3">
          {{ approvalError }}
        </p>
        <div class="modal-action">
          <button
            class="btn btn-ghost btn-sm"
            :disabled="approving"
            @click="approvalModal?.close()"
          >
            아직 승인하지 않기
          </button>
          <button
            class="btn btn-primary btn-sm"
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
