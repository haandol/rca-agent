<script setup lang="ts">
import type { ProposalResponse } from '../../../shared/types/playbook-library';
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
  refresh: refreshReport,
} = useFetch(`/api/reports/${id}`, {
  query: engine ? { engine } : undefined,
});
// This page needs one session, so it reads that session rather than the list —
// searching a paged list would miss anything past the first page.
const {
  data: session,
  error: sessionError,
  status: sessionStatus,
  refresh: refreshSession,
} = useFetch(`/api/sessions/${id}`, {
  query: engine ? { engine } : undefined,
});
// The playbook is part of this report, not a separate artifact: a person
// approves the procedure while reading the analysis that produced it.
const resolvedEngine = computed(
  () =>
    session.value?.engine ||
    engine ||
    (report.value?.engine === 'legacy' ? '' : report.value?.engine) ||
    '',
);
const {
  data: playbook,
  error: playbookError,
  status: playbookStatus,
  refresh: refreshPlaybook,
} = useFetch(`/api/playbooks/${id}`, {
  query: computed(() => ({ engine: resolvedEngine.value })),
});
const {
  data: executionHistory,
  error: executionError,
  status: executionStatus,
  refresh: refreshExecutions,
} = useFetch(`/api/executions/${id}`, {
  query: computed(() => ({ engine: resolvedEngine.value })),
});

const outcome = computed(() =>
  session.value ? outcomeOf(session.value) : null,
);

const rootCause = computed(() => stripInlineMarkup(session.value?.rootCause));

const chain = computed(() => parseCausalChain(report.value?.markdown));
const timeline = computed(() => parseTimeline(report.value?.markdown));
const renderedHtml = computed(() =>
  renderMarkdownDocument(
    report.value?.displayMarkdown ?? report.value?.markdown,
  ),
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
    Boolean(report.value) &&
    !error.value &&
    !sessionError.value &&
    Boolean(executionHistory.value) &&
    !executionError.value &&
    !playbookError.value &&
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
  if (executionError.value)
    return '실행 이력을 확인할 수 없어 승인할 수 없습니다';
  if (playbookError.value)
    return '현재 런북을 불러오지 못해 승인할 수 없습니다';
  if (error.value || !report.value)
    return '보고서를 확인할 수 없어 승인할 수 없습니다';
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
const detailOpen = ref(false);
const reviewed = ref(false);
const pendingApprovalId = ref<string | null>(null);
const reviewedDigest = ref('');

/**
 * Make an explicit reload start a fresh review of the current runbook.
 * Clear confirmation and the retry UUID before a new approval can be submitted.
 */
async function reloadPlanForReview() {
  reviewed.value = false;
  await refreshPlaybook();
  reviewedDigest.value = playbook.value?.playbookDigest ?? '';
  pendingApprovalId.value = null;
  approvalError.value = '';
}

/**
 * Submit only an eligible, reviewed runbook with its inspected digest.
 * Reuse the UUID after transport failure so a retry cannot authorize a second execution.
 */
async function approveExecution() {
  if (!canApprove.value || !reviewed.value) {
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
    reviewed.value = false;
    await Promise.allSettled([refreshExecutions(), refreshSession()]);
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

/** Keep failed and unresolved attempts visually distinct from running or resolved execution. */
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

/**
 * Refresh live execution outcomes without replacing the runbook being reviewed.
 * Skip hidden, terminal and overlapping polls; refresh failures remain independent.
 */
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

const detailTitles = {
  report: '보고서 전문 · 근거',
  action: '보고서에 기록된 조치 설명',
  cause: '원인 사슬 · 5 Whys',
  timeline: '사고 타임라인',
  analysis: '분석 경로 · 가설',
  evidence: '전체 증거',
  runbook: '현재 런북 · 실행 승인',
  knowledge: '이번 사고의 플레이북 지식',
  comparison: '생성에 사용한 참고 자료',
  executions: '실행 이력',
  retrospective: '실행 증거 · 회고',
} as const;
type DetailKind = keyof typeof detailTitles;
const activeDetail = ref<DetailKind>('report');
const visited = reactive<Record<string, boolean>>({});
/**
 * Switch the single dialog's content while retaining visited analysis state.
 * Opening the runbook captures its digest and requires a new explicit review.
 */
function openDetail(kind: DetailKind) {
  activeDetail.value = kind;
  visited[kind] = true;
  detailOpen.value = true;
  if (kind === 'runbook') {
    const digest = playbook.value?.playbookDigest ?? '';
    if (reviewedDigest.value !== digest) pendingApprovalId.value = null;
    reviewedDigest.value = digest;
    reviewed.value = false;
  }
}
const summary = computed(
  () =>
    (
      report.value as unknown as {
        summary?: {
          incidentSummary?: string | null;
          impactSummary?: string | null;
          severity?: string | null;
          confidence?: number | null;
          nextAction?: string | null;
        };
      } | null
    )?.summary,
);
const {
  data: proposals,
  status: proposalStatus,
  error: proposalError,
  refresh: refreshProposals,
} = useFetch<ProposalResponse>(`/api/playbook-proposals/${id}`, {
  query: computed(() => ({ engine: resolvedEngine.value })),
});
const referenceState = computed(
  () => proposals.value?.comparison ?? proposals.value?.summary ?? null,
);
const proposalBusy = ref(false);
const proposalActionError = ref('');
/**
 * Send a knowledge decision scoped to this incident, independently of execution approval.
 * Keep the comparison open and show server refusals or publication retry state in place.
 */
async function decide(action: 'apply' | 'reject') {
  const proposal = proposals.value?.comparison?.proposal;
  if (!proposal || proposalBusy.value) return;
  proposalBusy.value = true;
  proposalActionError.value = '';
  try {
    proposals.value = await $fetch<ProposalResponse>(
      `/api/playbook-proposals/${id}/${encodeURIComponent(proposal.proposal_id)}`,
      {
        method: 'POST',
        query: { engine: resolvedEngine.value },
        body: { action },
      },
    );
  } catch (err) {
    const data = (err as { data?: { statusMessage?: string } }).data;
    proposalActionError.value =
      data?.statusMessage || '제안을 처리하지 못했습니다. 다시 시도하세요.';
  } finally {
    proposalBusy.value = false;
  }
}
const evidenceId = ref('');
const evidence = ref('');
const evidencePending = ref(false);
const evidenceError = ref(false);
let evidenceRequest = 0;
/**
 * Read full evidence inside the existing modal without disturbing the incident summary.
 * Only the latest selected hypothesis request may populate the evidence panel.
 */
async function showEvidence(hypothesisId: string) {
  evidenceId.value = hypothesisId;
  openDetail('evidence');
  const request = ++evidenceRequest;
  evidencePending.value = true;
  evidenceError.value = false;
  evidence.value = '';
  try {
    const response = await $fetch(
      `/api/evidence/${id}/${encodeURIComponent(hypothesisId)}`,
    );
    if (request === evidenceRequest) evidence.value = response.markdown;
  } catch {
    if (request === evidenceRequest) evidenceError.value = true;
  } finally {
    if (request === evidenceRequest) evidencePending.value = false;
  }
}
const selectedExecution = ref('');
/** Bind retrospective evidence to the selected execution attempt in the same dialog. */
function showRetrospective(executionId: string) {
  selectedExecution.value = executionId;
  openDetail('retrospective');
}
const comparisonLabels = {
  UPDATE_PROPOSED: '변경 제안',
  NO_CHANGE: '변경 불필요',
  NO_MATCH: '유사 후보 없음',
  NO_APPLICABLE_MATCH: '적용 가능한 후보 없음',
  SEARCH_FAILED: '검색·비교 실패',
};
</script>

<template>
  <div>
    <header class="mb-6">
      <NuxtLink to="/" class="inline-block mb-4 text-xs text-primary"
        >← 장애 목록</NuxtLink
      >
      <p class="page-eyebrow">Incident Summary</p>
      <h1 class="page-title break-words">
        {{ session?.alarmName || 'RCA 보고서' }}
      </h1>
      <div class="flex flex-wrap gap-3 mt-3 text-xs text-base-content/75">
        <span
          v-if="outcome"
          class="status-chip"
          :class="OUTCOME_TONE[outcome]"
          >{{ OUTCOME_LABEL[outcome] }}</span
        >
        <time v-if="session?.createdAt">{{
          formatClock(session.createdAt)
        }}</time>
        <span class="font-mono">{{ resolvedEngine }}</span>
        <span class="font-mono break-all">{{ id }}</span>
      </div>
    </header>

    <ReadState
      label="사고 상태"
      :pending="sessionStatus === 'pending'"
      :error="sessionError"
      @retry="refreshSession()"
    >
      <section class="ops-panel p-5 mb-5">
        <div class="flex items-center gap-3 mb-3">
          <h2 class="detail-section-title">원인 요약</h2>
          <span
            class="status-chip"
            :class="session?.confirmed ? 'text-info' : 'text-warning'"
            >{{ session?.confirmed ? '원인 확정' : '원인 미확정' }}</span
          >
        </div>
        <p class="detail-body whitespace-pre-wrap">
          {{ rootCause || '별도 원인 요약이 제공되지 않았습니다.' }}
        </p>
        <section
          class="mt-5 border-t border-base-content/15 pt-4"
          data-testid="summary-five-whys"
        >
          <h3 class="detail-section-title mb-3">5 Whys · 증상에서 원인까지</h3>
          <ol v-if="chain.length" class="flex flex-wrap items-stretch gap-2">
            <li
              v-for="link in chain"
              :key="link.index"
              class="flex items-center gap-2"
            >
              <span v-if="link.index > 1" aria-hidden="true" class="text-info"
                >→</span
              >
              <button
                class="rounded-md border border-base-content/15 bg-base-200/50 p-3 text-left text-sm max-w-56 hover:border-primary focus-visible:outline-2 focus-visible:outline-primary"
                :title="`${link.question} — ${link.answer}`"
                @click="openDetail('cause')"
              >
                <span class="block font-mono text-xs text-info mb-1"
                  >Why {{ link.index }}</span
                >
                <span class="line-clamp-2">{{ link.answer }}</span>
              </button>
            </li>
          </ol>
          <p v-else class="detail-empty">
            분리해서 표시할 수 있는 5 Whys 사슬이 제공되지 않았습니다.
          </p>
          <button
            class="btn btn-ghost btn-sm mt-3"
            @click="openDetail('cause')"
          >
            {{ chain.length ? '단계별 질문과 전체 근거' : '원문 · 근거 확인' }}
          </button>
        </section>
        <div class="grid gap-4 mt-5 sm:grid-cols-3">
          <div>
            <p class="detail-label">영향 범위</p>
            <p class="text-sm mt-1 whitespace-pre-wrap">
              {{ summary?.impactSummary || '미제공' }}
            </p>
          </div>
          <div>
            <p class="detail-label">심각도</p>
            <p class="text-sm mt-1">{{ summary?.severity || '미제공' }}</p>
          </div>
          <div>
            <p class="detail-label">서버 판정 신뢰도</p>
            <p class="text-sm mt-1">
              {{
                summary?.confidence == null
                  ? '미제공'
                  : `${Math.round(summary.confidence * 100)}%`
              }}
            </p>
          </div>
        </div>
      </section>
    </ReadState>

    <div class="grid gap-5 lg:grid-cols-2 mb-5">
      <section
        class="ops-panel flex min-w-0 flex-col p-5"
        data-testid="next-action-card"
      >
        <h2 class="detail-section-title">지금 필요한 다음 조치</h2>
        <p class="detail-body mt-3">
          {{
            inFlight
              ? '승인한 런북의 실행 결과를 확인하세요.'
              : canApprove
                ? '현재 런북의 전체 명령을 검토한 뒤 실행을 승인할 수 있습니다.'
                : blockedReason
          }}
        </p>
        <p
          v-if="summary?.nextAction"
          class="mt-3 line-clamp-3 text-sm leading-relaxed text-base-content/75"
        >
          {{ summary.nextAction }}
        </p>
        <button
          v-if="summary?.nextAction"
          class="link link-hover mt-2 self-start text-xs text-primary"
          @click="openDetail('action')"
        >
          조치 설명 전체 보기
        </button>
        <div class="mt-auto pt-4">
          <div
            class="flex flex-wrap items-center gap-3 border-t border-base-content/10 pt-4"
          >
            <ReadState
              label="현재 런북"
              :pending="playbookStatus === 'pending'"
              :error="playbookError"
              @retry="refreshPlaybook()"
            >
              <span
                v-if="playbook"
                class="status-chip max-w-full"
                :class="isVerifiedPlaybook ? 'text-success' : 'text-warning'"
              >
                <template v-if="executionSteps.length">
                  {{
                    isVerifiedPlaybook ? '현재 런북 검증됨' : '현재 런북 초안'
                  }}
                  · {{ executionSteps.length }}개 단계
                </template>
                <template v-else>실행할 명령 없음</template>
              </span>
            </ReadState>
            <button
              class="btn btn-sm"
              :class="isPendingDecision ? 'btn-warning' : 'btn-outline'"
              @click="
                openDetail(executionSteps.length ? 'runbook' : 'knowledge')
              "
            >
              {{
                executionSteps.length
                  ? canApprove
                    ? '런북 전체 검토 · 실행 승인'
                    : '런북 전체 검토'
                  : '대응 지식 확인'
              }}
            </button>
          </div>
        </div>
      </section>
      <section class="ops-panel p-5">
        <h2 class="detail-section-title">생성에 사용한 참고 자료</h2>
        <ReadState
          label="비교 기록"
          :pending="proposalStatus === 'pending'"
          :error="proposalError"
          @retry="refreshProposals()"
        >
          <p class="detail-body mt-3">
            {{
              proposals?.comparison?.used_references
                ? `생성에 사용한 자료 ${proposals.comparison.used_references.length}건`
                : '실제 사용한 참고 자료 목록 미기록'
            }}
          </p>
          <p v-if="referenceState" class="detail-label mt-2">
            {{ comparisonLabels[referenceState.status] }}
          </p>
          <p v-if="referenceState?.proposal" class="detail-label mt-2">
            {{
              { PENDING: '검토 대기', APPLIED: '반영됨', REJECTED: '기각됨' }[
                referenceState.proposal.state
              ]
            }}
          </p>
        </ReadState>
        <button
          class="btn btn-outline btn-sm mt-4"
          data-testid="open-generation-references"
          @click="openDetail('comparison')"
        >
          참고 원본 · 변경 제안 검토
        </button>
      </section>
    </div>

    <section class="ops-panel p-5">
      <h2 class="detail-section-title mb-4">필요한 상세 확인</h2>
      <div class="flex flex-wrap gap-2">
        <button
          v-for="kind in [
            'report',
            'cause',
            'timeline',
            'analysis',
            'knowledge',
            'executions',
          ] as const"
          :key="kind"
          class="btn btn-outline btn-sm"
          @click="openDetail(kind)"
        >
          {{ detailTitles[kind] }}
        </button>
      </div>
      <p v-if="error" class="text-sm text-error mt-3">
        보고서를 불러오지 못했습니다. 상세에서 다시 시도할 수 있습니다.
      </p>
      <ReadState
        label="실행 이력"
        :pending="executionStatus === 'pending'"
        :error="executionError"
        @retry="refreshExecutions()"
      >
        <p class="detail-label mt-4">
          {{
            latest
              ? `최근 실행 · ${latest.stateLabel} · ${executions.length}회 시도`
              : '기록된 실행 없음'
          }}
        </p>
      </ReadState>
    </section>

    <DetailDialog
      :open="detailOpen"
      :title="detailTitles[activeDetail]"
      @close="detailOpen = false"
    >
      <nav class="flex flex-wrap gap-2 mb-5" aria-label="상세 자료 전환">
        <button
          v-for="kind in [
            'report',
            'cause',
            'timeline',
            'analysis',
            'runbook',
            'knowledge',
            'comparison',
            'executions',
          ] as const"
          :key="kind"
          class="btn btn-sm"
          :class="activeDetail === kind ? 'btn-primary' : 'btn-ghost'"
          :aria-pressed="activeDetail === kind"
          @click="openDetail(kind)"
        >
          {{ detailTitles[kind] }}
        </button>
      </nav>

      <ReadState
        v-if="['report', 'cause', 'timeline', 'action'].includes(activeDetail)"
        label="보고서"
        :pending="status === 'pending'"
        :error="error"
        @retry="refreshReport()"
      >
        <div
          v-if="activeDetail === 'report'"
          class="prose-report"
          v-html="renderedHtml"
        />
        <template v-else-if="activeDetail === 'action'">
          <p class="detail-label mb-4">
            분석 시점에 기록된 권고 사항입니다. 실제 실행 여부는 실행 이력에서
            확인할 수 있습니다.
          </p>
          <div
            v-if="summary?.nextAction"
            class="prose-report"
            v-html="renderMarkdownDocument(summary.nextAction)"
          />
          <p v-else class="detail-empty">
            이 보고서에는 조치 설명이 제공되지 않았습니다.
          </p>
        </template>
        <template v-else-if="activeDetail === 'cause'">
          <CausalChain v-if="chain.length" :links="chain" />
          <template v-else
            ><p class="detail-empty mb-4">
              원인 사슬을 안전하게 분리할 수 없어 보고서 원문을 표시합니다.
            </p>
            <div class="prose-report" v-html="renderedHtml"
          /></template>
        </template>
        <template v-else>
          <ol v-if="timeline.length" class="space-y-4">
            <li
              v-for="(moment, i) in timeline"
              :key="i"
              class="border-l-2 border-info/40 pl-4"
            >
              <span class="font-mono text-info text-sm">{{ moment.time }}</span>
              <p class="detail-body mt-1">{{ moment.event }}</p>
            </li>
          </ol>
          <template v-else
            ><p class="detail-empty mb-4">
              분리된 타임라인이 없어 보고서 원문을 표시합니다.
            </p>
            <div class="prose-report" v-html="renderedHtml"
          /></template>
        </template>
      </ReadState>

      <div
        v-if="visited.analysis"
        v-show="activeDetail === 'analysis'"
        data-detail-panel="analysis"
      >
        <AnalysisDetails
          :rca-id="id"
          :engine="resolvedEngine"
          @evidence="showEvidence"
        />
      </div>
      <ReadState
        v-if="activeDetail === 'evidence'"
        label="전체 증거"
        :pending="evidencePending"
        :error="evidenceError"
        @retry="showEvidence(evidenceId)"
      >
        <button
          class="btn btn-ghost btn-sm mb-4"
          @click="openDetail('analysis')"
        >
          ← 분석 경로로
        </button>
        <div class="prose-report" v-html="renderMarkdownDocument(evidence)" />
      </ReadState>

      <ReadState
        v-if="activeDetail === 'runbook' || activeDetail === 'knowledge'"
        label="현재 플레이북"
        :pending="playbookStatus === 'pending'"
        :error="playbookError"
        @retry="refreshPlaybook()"
      >
        <template v-if="playbook">
          <PlaybookKnowledge
            v-if="activeDetail === 'knowledge'"
            :playbook="playbook"
          />
          <template v-else>
            <p class="detail-body mb-4">
              현재 사고의 대상·리전·전체 명령과 순서·성공 기준을 검토하세요.
              보고서 원문은 분석 시점의 기록이며 아래 절차는 현재 개정본입니다.
            </p>
            <span
              class="status-chip mb-3"
              :class="isVerifiedPlaybook ? 'text-success' : 'text-warning'"
              >{{
                isVerifiedPlaybook ? '현재 런북 검증됨' : '현재 런북 초안'
              }}</span
            >
            <button
              v-if="playbook.revisedByExecutionId"
              class="btn btn-ghost btn-sm ml-3"
              @click="showRetrospective(playbook.revisedByExecutionId)"
            >
              이 절차를 바꾼 회고 보기
            </button>
            <RecoveryPlanSteps
              :steps="executionSteps"
              :validation-error="playbook.validationError"
              :executable="playbook.executable === true"
            />
            <div class="mt-6 border-t border-warning/40 pt-5 space-y-3">
              <p v-if="blockedReason" class="text-sm text-warning">
                {{ blockedReason }}
              </p>
              <label class="flex items-start gap-3 text-sm"
                ><input
                  v-model="reviewed"
                  type="checkbox"
                  class="checkbox checkbox-sm checkbox-warning"
                  :disabled="!canApprove"
                />이 사고의 전체 명령·대상·리전·순서·성공 기준을
                검토했습니다.</label
              >
              <p v-if="approvalError" role="alert" class="text-error text-sm">
                {{ approvalError }}
              </p>
              <div class="flex flex-wrap gap-3">
                <button
                  class="btn btn-warning btn-sm"
                  :disabled="approving || !canApprove || !reviewed"
                  @click="approveExecution"
                >
                  {{
                    approving
                      ? '승인 요청 중'
                      : executions.length
                        ? '검토한 런북 다시 실행 승인'
                        : '검토한 런북 승인하고 실행'
                  }}
                </button>
                <button
                  class="btn btn-outline btn-sm"
                  :disabled="approving"
                  @click="reloadPlanForReview"
                >
                  최신 런북 다시 불러오기
                </button>
                <button
                  class="btn btn-ghost btn-sm"
                  @click="openDetail('executions')"
                >
                  실행 이력
                </button>
              </div>
            </div>
          </template>
        </template>
        <p v-else class="detail-empty">현재 플레이북이 제공되지 않았습니다.</p>
      </ReadState>

      <ReadState
        v-if="activeDetail === 'comparison'"
        label="비교 기록"
        :pending="proposalStatus === 'pending'"
        :error="proposalError"
        @retry="refreshProposals()"
      >
        <div data-testid="generation-references">
          <p
            v-if="proposals?.unavailable_reason"
            class="text-sm text-warning mb-4"
          >
            {{ proposals.unavailable_reason }}
          </p>
          <p
            v-if="!proposals?.comparison && referenceState?.proposal"
            class="status-chip mb-4"
          >
            {{
              { PENDING: '검토 대기', APPLIED: '반영됨', REJECTED: '기각됨' }[
                referenceState.proposal.state
              ]
            }}
            · 원문 열람 및 새 반영 불가
          </p>
          <PlaybookComparison
            :comparison="proposals?.comparison ?? null"
            :rca-id="id"
            :engine="resolvedEngine"
            :pending="proposalBusy"
            :error="proposalActionError"
            @apply="decide('apply')"
            @reject="decide('reject')"
          />
        </div>
      </ReadState>
      <ReadState
        v-if="activeDetail === 'executions'"
        label="실행 이력"
        :pending="executionStatus === 'pending'"
        :error="executionError"
        @retry="refreshExecutions()"
      >
        <ul class="space-y-3">
          <li
            v-for="execution in executions"
            :key="execution.executionId"
            class="ops-panel p-4"
          >
            <div class="flex flex-wrap items-center gap-3">
              <span
                class="status-chip"
                :class="executionTone(execution.state)"
                >{{ execution.stateLabel }}</span
              >
              <span class="text-sm"
                >{{ execution.attempt }}회차 · 절차
                {{ execution.attemptedStepCount }}건 · 차단
                {{ execution.blockedCount }} · 실패
                {{ execution.failedStepCount }}</span
              >
              <button
                class="btn btn-outline btn-sm ml-auto"
                @click="showRetrospective(execution.executionId)"
              >
                실행 증거 · 회고
              </button>
            </div>
            <p
              v-if="execution.errorReason"
              class="text-sm text-error mt-3 whitespace-pre-wrap"
            >
              {{ execution.errorReason }}
            </p>
          </li>
        </ul>
        <p v-if="!executions.length" class="detail-empty">
          기록된 실행이 없습니다.
        </p>
      </ReadState>
      <template v-if="activeDetail === 'retrospective' && selectedExecution">
        <button
          class="btn btn-ghost btn-sm mb-4"
          @click="openDetail('executions')"
        >
          ← 실행 이력으로
        </button>
        <RetrospectiveDetails
          :key="selectedExecution"
          :rca-id="id"
          :execution-id="selectedExecution"
        />
      </template>
    </DetailDialog>
  </div>
</template>
