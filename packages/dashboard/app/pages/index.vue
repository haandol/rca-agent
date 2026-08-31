<script setup lang="ts">
import { stripInlineMarkup } from '~/utils/causalChain';
import {
  OUTCOME_LABEL,
  OUTCOME_TONE,
  READINESS_DESC,
  READINESS_LABEL,
  outcomeOf,
  stoppedAtLabel,
  type Outcome,
} from '~/utils/sessionState';

/**
 * The list arrives a page at a time, and the counts arrive separately.
 *
 * A page is read from the session index, newest first. The counts are their own
 * request because they have to describe the whole archive: '승인 대기 10건' turning
 * into '3건' because only the first page was counted would tell someone the wrong
 * amount of work is left.
 */
const {
  data: firstPage,
  status,
  refresh: refreshFirstPage,
} = useFetch('/api/sessions');

const { data: summary, refresh: refreshSummary } = useFetch(
  '/api/sessions-summary',
);

type SessionRow = NonNullable<typeof firstPage.value>['sessions'][number];

type Row = SessionRow & {
  outcome: Outcome;
  durationMs: number | null;
};

/** Pages fetched after the first, in order. */
const extraPages = ref<{ sessions: SessionRow[]; nextCursor: string }[]>([]);
const loadingMore = ref(false);

/**
 * Where the next page resumes.
 *
 * Derived rather than assigned from a watcher: a watcher does not run during
 * server rendering, so the server drew no sentinel while the hydrating client
 * drew one, and hydration reported a mismatched node. The last page fetched owns
 * the cursor, and the first page owns it until another arrives.
 */
const nextCursor = computed(() => {
  const lastExtra = extraPages.value.at(-1);
  if (lastExtra) return lastExtra.nextCursor;
  return firstPage.value?.nextCursor ?? '';
});

// A fresh first page invalidates everything fetched after it.
watch(firstPage, () => {
  extraPages.value = [];
});

const fetchedSessions = computed<SessionRow[]>(() => [
  ...(firstPage.value?.sessions ?? []),
  ...extraPages.value.flatMap((page) => page.sessions),
]);

/** Re-read both the list and the totals; used after a cancel or delete. */
async function refresh() {
  await Promise.all([refreshFirstPage(), refreshSummary()]);
}

/**
 * How long a run took.
 *
 * `updatedAt` is the last write, which for a terminal session is when it
 * finished — so the gap between the two timestamps is the run. A negative or
 * absurd gap means clock skew or a resumed session, and those are dropped rather
 * than drawn as a bar nobody can trust.
 */
const MAX_PLAUSIBLE_RUN_MS = 4 * 60 * 60 * 1000;

/** Below this, the gap is bookkeeping latency rather than analysis. */
const MIN_MEANINGFUL_RUN_MS = 1_000;

function runDuration(
  createdAt: string,
  updatedAt: string,
  state: string,
): number | null {
  // A skipped alarm never entered the pipeline, so the gap between its two
  // timestamps is how long the skip took to record — not a run to draw.
  if (state === 'OUTDATED') return null;
  if (!createdAt || !updatedAt) return null;
  const ms = new Date(updatedAt).getTime() - new Date(createdAt).getTime();
  if (!Number.isFinite(ms)) return null;
  if (ms < MIN_MEANINGFUL_RUN_MS || ms > MAX_PLAUSIBLE_RUN_MS) return null;
  return ms;
}

const rows = computed<Row[]>(() =>
  fetchedSessions.value.map((session) => ({
    ...session,
    outcome: outcomeOf(session),
    durationMs: runDuration(
      session.createdAt,
      session.updatedAt,
      session.state,
    ),
  })),
);

/** The longest run on screen sets the scale every bar is drawn against. */
const longestRunMs = computed(() =>
  Math.max(1, ...rows.value.map((row) => row.durationMs ?? 0)),
);

const MAX_BAR_PX = 132;

function barWidth(ms: number | null): string {
  if (!ms) return '3px';
  const px = Math.max(3, Math.round((ms / longestRunMs.value) * MAX_BAR_PX));
  return `${px}px`;
}

function formatRun(ms: number | null): string {
  if (!ms) return '';
  if (ms < 60_000) return `${Math.round(ms / 1000)}초`;
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return `${minutes}분`;
  const hours = Math.floor(minutes / 60);
  return `${hours}시간 ${minutes % 60}분`;
}

/**
 * Clock readings are rendered in one fixed zone.
 *
 * The server renders this page and the browser hydrates it; left to the ambient
 * timezone they disagree, and every entry's time is replaced after load — the
 * page reports one clock and then silently another. Naming the zone also matches
 * how these incidents are actually discussed, since the reports themselves are
 * written in a single zone.
 */
const DISPLAY_ZONE = 'Asia/Seoul';

function clockOf(iso: string): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('ko-KR', {
    timeZone: DISPLAY_ZONE,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** The calendar day an instant falls on, in the display zone. */
function dayKeyOf(iso: string): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-CA', { timeZone: DISPLAY_ZONE });
}

function dayLabelOf(iso: string): string {
  const now = Date.now();
  if (dayKeyOf(iso) === dayKeyOf(new Date(now).toISOString())) return '오늘';
  if (dayKeyOf(iso) === dayKeyOf(new Date(now - 86_400_000).toISOString()))
    return '어제';
  return new Date(iso).toLocaleDateString('ko-KR', {
    timeZone: DISPLAY_ZONE,
    month: 'long',
    day: 'numeric',
    weekday: 'short',
  });
}

/**
 * Every alarm here is prefixed with the stack and service, so the prefix repeats
 * on every row and pushes the part that differs out of the strongest position.
 */
function shortAlarm(name: string): string {
  return name.replace(/^RcaAgentDev-/, '').replace(/^Healthcare-/, '');
}

/** Which outcomes to show. Nothing is hidden unless the reader hides it. */
const hidden = ref<Set<Outcome>>(new Set());

function toggleFilter(outcome: Outcome) {
  const next = new Set(hidden.value);
  if (next.has(outcome)) next.delete(outcome);
  else next.add(outcome);
  hidden.value = next;
}

const matchingRows = computed(() =>
  rows.value.filter((row) => !hidden.value.has(row.outcome)),
);

/**
 * The rows on screen, and whether the archive has more.
 *
 * Paging is a real fetch now: the store returns the newest page and a cursor, so
 * the archive is never read whole. A cursor means more exists — the count of what
 * remains is not known, and claiming a number here would be a guess.
 *
 * Scrolling rather than numbered pages, because the spine is one continuous clock:
 * page 3 of a timeline has no meaning, and landing in the middle of one breaks the
 * only ordering the page has.
 */
const visibleRows = computed(() => matchingRows.value);
const hasMore = computed(() => Boolean(nextCursor.value));

async function showMore() {
  if (!nextCursor.value || loadingMore.value) return;
  loadingMore.value = true;
  try {
    const page = await $fetch('/api/sessions', {
      query: { cursor: nextCursor.value },
    });
    extraPages.value = [
      ...extraPages.value,
      { sessions: page.sessions, nextCursor: page.nextCursor ?? '' },
    ];
  } catch {
    // The cursor is unchanged, so the same page can be asked for again; clearing
    // it would claim the archive had ended.
  } finally {
    loadingMore.value = false;
  }
}

/**
 * Hiding an outcome can empty the fetched pages while the archive still holds
 * matches, and the reader would see 'no records' with rows still to come. Pulling
 * the next page keeps the filter honest about what it is filtering.
 */
watch([hidden, matchingRows], () => {
  if (!matchingRows.value.length && hasMore.value) void showMore();
});

/**
 * Reveals the next slice as the reader reaches the end of the spine.
 *
 * The sentinel is watched rather than the scroll position, so extending the list
 * costs nothing until the reader actually arrives. The margin is deliberately
 * generous: the next entries should already be there when the last one comes into
 * view, so scrolling down a timeline never stops.
 *
 * The button stays as the real control rather than a fallback — an observer is
 * not reachable by keyboard, and the reader who tabs to the end of the list still
 * has to be able to ask for more.
 */
const sentinel = useTemplateRef<HTMLElement>('sentinel');

onMounted(() => {
  if (!import.meta.client || typeof IntersectionObserver === 'undefined')
    return;

  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void showMore();
    },
    { rootMargin: '600px 0px' },
  );

  // The sentinel is mounted and unmounted with the remaining-rows block, so the
  // observer follows the element rather than being attached once.
  watch(
    sentinel,
    (el, _old, onCleanup) => {
      if (!el) return;
      observer.observe(el);
      onCleanup(() => observer.unobserve(el));
    },
    { immediate: true, flush: 'post' },
  );

  onBeforeUnmount(() => observer.disconnect());
});

/** Sessions grouped under the day they began, newest day first. */
const days = computed(() => {
  const groups: { key: string; label: string; rows: Row[] }[] = [];
  for (const row of visibleRows.value) {
    const key = dayKeyOf(row.createdAt);
    const last = groups.at(-1);
    if (last?.key === key) last.rows.push(row);
    else groups.push({ key, label: dayLabelOf(row.createdAt), rows: [row] });
  }
  return groups;
});

/**
 * Outcome counts for the whole archive, not for the page.
 *
 * Terminal analyses map straight from their state; a completed one depends on what
 * happened after it, so the summary sends each completed session's readiness and
 * execution state and the same shared vocabulary turns them into one word here.
 */
const counts = computed(() => {
  const tally = new Map<Outcome, number>();
  const bump = (outcome: Outcome, by = 1) =>
    tally.set(outcome, (tally.get(outcome) ?? 0) + by);

  const byState = summary.value?.byState ?? {};
  for (const [state, count] of Object.entries(byState)) {
    if (state === 'COMPLETED') continue;
    bump(outcomeOf({ state }), count);
  }
  for (const entry of summary.value?.completedOutcomes ?? []) {
    bump(
      outcomeOf({
        state: 'COMPLETED',
        readiness: entry.readiness,
        executionState: entry.executionState,
      }),
    );
  }
  return tally;
});

/** Everything the archive holds, however much of it has been fetched. */
const totalCount = computed(() => summary.value?.total ?? rows.value.length);

/** The filters worth offering: outcomes this archive actually contains. */
const FILTER_ORDER: Outcome[] = [
  'RUNNING',
  'AWAITING',
  'RESOLVED',
  'UNRESOLVED',
  'NO_CAUSE',
  'BROKEN',
  'SKIPPED',
];

const filters = computed(() =>
  FILTER_ORDER.filter((outcome) => counts.value.has(outcome)).map(
    (outcome) => ({
      outcome,
      label: OUTCOME_LABEL[outcome],
      count: counts.value.get(outcome) ?? 0,
    }),
  ),
);

const awaitingCount = computed(() => counts.value.get('AWAITING') ?? 0);
const runningCount = computed(() => counts.value.get('RUNNING') ?? 0);
const investigationCount = computed(
  () =>
    (counts.value.get('UNRESOLVED') ?? 0) +
    (counts.value.get('NO_CAUSE') ?? 0) +
    (counts.value.get('BROKEN') ?? 0),
);
const resolvedCount = computed(() => counts.value.get('RESOLVED') ?? 0);

const readinessModal = ref<HTMLDialogElement | null>(null);

const ALLOWED_ENGINES = new Set(['strands', 'codex-headless', 'cc-headless']);
const cancelTarget = ref<{ rcaId: string; engine: string } | null>(null);
const cancelling = ref(false);
const cancelModalRef = ref<HTMLDialogElement | null>(null);

function openCancelModal(rcaId: string, engine: string) {
  if (!ALLOWED_ENGINES.has(engine)) return;
  cancelError.value = '';
  cancelTarget.value = { rcaId, engine };
  cancelModalRef.value?.showModal();
}

function closeCancelModal() {
  cancelTarget.value = null;
  cancelError.value = '';
  cancelModalRef.value?.close();
}

/**
 * Why the last cancel or delete did not happen.
 *
 * The server refuses both on purpose — a live execution still holds a claim, and
 * removing the session it is fenced against would let a redelivered alarm run
 * beside it. Swallowing that refusal made the button look broken: the dialog
 * closed, the row stayed, and nothing said why. The reason is shown in the dialog
 * that asked for the action, which stays open so it can be read.
 */
const cancelError = ref('');
const deleteError = ref('');

/**
 * The row an action is currently working on.
 *
 * Re-reading the list is a full table scan and takes seconds, so between the
 * request finishing and the new list arriving the row still sits there looking
 * untouched — the operator cannot tell whether the click registered. Marking the
 * row keeps the feedback where the action was aimed.
 */
const busyRowKey = ref('');

function rowKeyOf(rcaId: string, engine: string): string {
  return `${rcaId}#${engine}`;
}

/**
 * A 404 means the session is already gone — someone deleted it from another
 * window, or this list was read before it went. That is the outcome the click was
 * asking for, so it is not reported as a failure: the list is re-read and the
 * dialog closes. Only a genuine refusal (a live execution, an active session)
 * keeps the dialog open with its reason.
 */
function isAlreadyGone(error: unknown): boolean {
  return (error as { statusCode?: number })?.statusCode === 404;
}

/**
 * The reason the server gave, not the HTTP status text.
 *
 * `$fetch` sets `statusMessage` from the response's status line — a 409 arrives as
 * 'Conflict' — while the sentence the handler wrote ('An execution is 실행 중 …')
 * is carried in the parsed body. Reading the body first is what makes the dialog
 * say why rather than restating the status code.
 */
function refusalMessage(error: unknown, fallback: string): string {
  const data = (
    error as { data?: { statusMessage?: string; message?: string } }
  )?.data;
  return data?.statusMessage || data?.message || fallback;
}

async function cancelSession() {
  if (!cancelTarget.value) return;
  cancelling.value = true;
  cancelError.value = '';
  busyRowKey.value = rowKeyOf(
    cancelTarget.value.rcaId,
    cancelTarget.value.engine,
  );
  try {
    await $fetch(`/api/sessions/${cancelTarget.value.rcaId}/cancel`, {
      method: 'POST',
      query: { engine: cancelTarget.value.engine },
    });
    await refresh();
    closeCancelModal();
  } catch (error) {
    // The list is re-read either way: a refusal often means the session already
    // moved on, and the row that prompted this is then out of date.
    if (isAlreadyGone(error)) closeCancelModal();
    else cancelError.value = refusalMessage(error, '중단하지 못했습니다.');
    await refresh();
  } finally {
    cancelling.value = false;
    busyRowKey.value = '';
  }
}

const deleteTarget = ref<{ rcaId: string; engine: string } | null>(null);
const deleting = ref(false);
const deleteModalRef = ref<HTMLDialogElement | null>(null);

function openDeleteModal(rcaId: string, engine: string) {
  deleteError.value = '';
  deleteTarget.value = { rcaId, engine };
  deleteModalRef.value?.showModal();
}

function closeDeleteModal() {
  deleteTarget.value = null;
  deleteError.value = '';
  deleteModalRef.value?.close();
}

async function deleteSession() {
  if (!deleteTarget.value) return;
  deleting.value = true;
  deleteError.value = '';
  busyRowKey.value = rowKeyOf(
    deleteTarget.value.rcaId,
    deleteTarget.value.engine,
  );
  try {
    await $fetch(
      `/api/sessions/${deleteTarget.value.rcaId}?engine=${deleteTarget.value.engine}`,
      { method: 'DELETE' },
    );
    await refresh();
    closeDeleteModal();
  } catch (error) {
    // Already deleted is the outcome that was wanted, so it closes quietly.
    if (isAlreadyGone(error)) closeDeleteModal();
    else deleteError.value = refusalMessage(error, '삭제하지 못했습니다.');
    await refresh();
  } finally {
    deleting.value = false;
    busyRowKey.value = '';
  }
}

useHead({ title: '장애 기록' });
</script>

<template>
  <div>
    <header class="flex flex-wrap items-start justify-between gap-5">
      <div>
        <p class="page-eyebrow">Incident Operations</p>
        <h1 class="page-title">RCA Operations</h1>
        <p class="page-description">
          자동 분석 결과를 한곳에서 비교하고, 근거와 복구 절차를 검토한 뒤
          실행을 승인합니다.
        </p>
      </div>
      <button class="btn btn-outline btn-sm gap-2" @click="refresh()">
        <span
          v-if="status === 'pending'"
          class="loading loading-spinner loading-xs"
        />
        데이터 새로고침
      </button>
    </header>

    <section
      class="grid grid-cols-2 xl:grid-cols-4 gap-3 mt-7"
      aria-label="운영 현황"
    >
      <div class="metric-card text-warning">
        <div class="metric-label">Awaiting approval</div>
        <div class="metric-value">{{ awaitingCount }}</div>
        <div class="metric-help">사람의 검토와 승인이 필요합니다</div>
      </div>
      <div class="metric-card text-info">
        <div class="metric-label">Analysis running</div>
        <div class="metric-value">{{ runningCount }}</div>
        <div class="metric-help">두 분석 엔진에서 진행 중입니다</div>
      </div>
      <div class="metric-card text-error">
        <div class="metric-label">Needs investigation</div>
        <div class="metric-value">{{ investigationCount }}</div>
        <div class="metric-help">미해결·원인 미확정·중단 합계</div>
      </div>
      <div class="metric-card text-success">
        <div class="metric-label">Resolved</div>
        <div class="metric-value">{{ resolvedCount }}</div>
        <div class="metric-help">전체 기록 {{ totalCount }}건</div>
      </div>
    </section>

    <section class="ops-panel mt-5">
      <div
        class="flex flex-wrap items-center gap-2 border-b border-base-content/10 px-4 py-3"
      >
        <span
          class="mr-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-base-content/42"
        >
          Outcome filter
        </span>
        <button
          v-for="filter in filters"
          :key="filter.outcome"
          class="filter-chip"
          :class="OUTCOME_TONE[filter.outcome]"
          :aria-pressed="!hidden.has(filter.outcome)"
          @click="toggleFilter(filter.outcome)"
        >
          <span class="size-1.5 rounded-full bg-current" aria-hidden="true" />
          <span>{{ filter.label }}</span>
          <span class="font-mono text-[10px] opacity-70">
            {{ filter.count }}
          </span>
        </button>
        <button
          class="ml-auto text-[11px] text-base-content/52 hover:text-primary"
          @click="readinessModal?.showModal()"
        >
          상태 기준 보기
        </button>
      </div>
      <div
        class="flex items-center justify-between gap-4 px-4 py-2.5 text-[11px] text-base-content/48"
      >
        <span>
          표시 중
          <strong class="font-mono font-medium text-base-content/72">
            {{ visibleRows.length }}
          </strong>
          건
        </span>
        <span class="hidden sm:inline">
          최신 순 · Asia/Seoul · 엔진별 독립 분석
        </span>
      </div>
    </section>

    <!-- Loading -->
    <div
      v-if="status === 'pending' && !firstPage"
      class="py-24 text-center text-[13px] text-base-content/65"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">기록을 읽고 있습니다</p>
    </div>

    <!-- Nothing recorded yet -->
    <div v-else-if="!totalCount" class="py-24 text-center">
      <p class="font-serif text-[19px]">아직 기록이 없습니다</p>
      <p class="text-[13px] text-base-content/68 mt-2.5 max-w-md mx-auto">
        CloudWatch 알람이 발생하면 두 엔진이 분석을 시작하고, 그 결과가
        시간순으로 여기 쌓입니다.
      </p>
    </div>

    <!-- Everything filtered out. Tested against the filter result rather than
         the page slice, which is never empty while anything matches. -->
    <div v-else-if="!matchingRows.length && !hasMore" class="py-24 text-center">
      <p class="font-serif text-[17px]">고른 상태에 해당하는 기록이 없습니다</p>
      <button class="btn btn-ghost btn-xs mt-4" @click="hidden = new Set()">
        전체 보기
      </button>
    </div>

    <!-- Dense queue: every operational fact needed for triage is visible. -->
    <div v-else class="incident-queue mt-4">
      <section v-for="day in days" :key="day.key" class="incident-day">
        <div class="incident-day-head">
          <span class="incident-day-label">{{ day.label }}</span>
          <span class="font-mono text-[10px] text-base-content/42">
            {{ day.rows.length }} incidents
          </span>
        </div>

        <article
          v-for="row in day.rows"
          :key="`${row.rcaId}-${row.engine}`"
          class="incident-row group"
          :class="[
            OUTCOME_TONE[row.outcome],
            busyRowKey === rowKeyOf(row.rcaId, row.engine)
              ? 'opacity-40 pointer-events-none'
              : '',
          ]"
          :aria-busy="busyRowKey === rowKeyOf(row.rcaId, row.engine)"
        >
          <div class="min-w-0 text-base-content">
            <div class="flex flex-wrap items-center gap-2">
              <span
                class="status-chip"
                :class="[
                  OUTCOME_TONE[row.outcome],
                  row.outcome === 'RUNNING' ? 'animate-ember' : '',
                ]"
              >
                {{ OUTCOME_LABEL[row.outcome] }}
              </span>
              <span class="pill-meta font-mono">{{ row.engine }}</span>
              <span
                v-if="row.retrospectiveStatus === 'UPDATED'"
                class="pill-meta text-accent"
              >
                회고 반영
              </span>
            </div>

            <NuxtLink
              :to="`/report/${row.rcaId}?engine=${row.engine}`"
              class="incident-title mt-2.5 inline-block"
              :title="row.alarmName"
            >
              {{ shortAlarm(row.alarmName) }}
            </NuxtLink>

            <p
              v-if="row.rootCause"
              class="incident-summary"
              :title="stripInlineMarkup(row.rootCause)"
            >
              {{ stripInlineMarkup(row.rootCause) }}
            </p>
            <p
              v-else-if="row.errorReason"
              class="incident-summary"
              :title="row.errorReason"
            >
              {{ row.errorReason }}
            </p>
            <p v-else class="incident-summary italic">
              분석 결과 요약이 아직 기록되지 않았습니다.
            </p>

            <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
              <span
                v-if="
                  row.outcome === 'BROKEN' &&
                  stoppedAtLabel(row.engine, row.stoppedAt)
                "
                class="text-[10.5px] text-error"
              >
                {{ stoppedAtLabel(row.engine, row.stoppedAt) }}
              </span>
              <span
                v-if="row.executionBlockedCount"
                class="text-[10.5px] text-warning"
                title="되돌릴 수 없는 조치라 차단되어 수동 조치로 남은 절차"
              >
                수동 조치 {{ row.executionBlockedCount }}건
              </span>
              <span
                v-if="row.executionAttempts > 1"
                class="text-[10.5px] text-base-content/48"
              >
                실행 {{ row.executionAttempts }}회
              </span>
            </div>
          </div>

          <div class="text-base-content">
            <div class="incident-data-label">Started</div>
            <time class="incident-data-value block" :datetime="row.createdAt">
              {{ clockOf(row.createdAt) }} KST
            </time>
            <div class="mt-3 font-mono text-[9.5px] text-base-content/38">
              {{ row.rcaId.slice(0, 8) }}
            </div>
          </div>

          <div class="text-base-content">
            <div class="incident-data-label">Duration</div>
            <div class="incident-data-value">
              {{ formatRun(row.durationMs) || '진행 중' }}
            </div>
            <div
              v-if="row.durationMs || row.outcome === 'RUNNING'"
              class="mt-3 flex h-4 items-center"
            >
              <span
                class="run-bar"
                :class="[
                  OUTCOME_TONE[row.outcome],
                  row.outcome === 'RUNNING' ? 'run-bar-open' : '',
                ]"
                :style="{ width: barWidth(row.durationMs) }"
              />
            </div>
          </div>

          <div class="incident-actions text-base-content">
            <NuxtLink
              :to="`/report/${row.rcaId}?engine=${row.engine}`"
              class="btn btn-sm w-full"
              :class="
                row.outcome === 'AWAITING'
                  ? 'btn-warning'
                  : 'btn-outline border-base-content/18'
              "
            >
              {{
                row.outcome === 'AWAITING'
                  ? `절차 ${row.executionStepCount}개 검토`
                  : '보고서 열기'
              }}
            </NuxtLink>
            <div class="incident-secondary-actions">
              <NuxtLink
                :to="`/trace/${row.rcaId}?engine=${row.engine}`"
                title="분석 과정"
              >
                Trace
              </NuxtLink>
              <NuxtLink
                :to="`/playbook/${row.rcaId}?engine=${row.engine}`"
                title="플레이북"
              >
                Playbook
              </NuxtLink>
              <button
                v-if="row.outcome === 'RUNNING'"
                class="hover:!text-warning"
                @click="openCancelModal(row.rcaId, row.engine)"
              >
                중단
              </button>
              <button
                class="hover:!text-error"
                @click="openDeleteModal(row.rcaId, row.engine)"
              >
                삭제
              </button>
            </div>
          </div>
        </article>
      </section>

      <div
        v-if="hasMore"
        ref="sentinel"
        class="border-t border-base-content/10 p-4 text-center"
      >
        <button
          class="btn btn-ghost btn-sm"
          :disabled="loadingMore"
          @click="showMore()"
        >
          <span v-if="loadingMore" class="loading loading-spinner loading-xs" />
          {{ loadingMore ? '불러오는 중' : '이전 기록 더 보기' }}
        </button>
      </div>
      <p
        v-else-if="matchingRows.length"
        class="border-t border-base-content/10 px-4 py-3 text-center text-[10.5px] text-base-content/38"
      >
        기록의 처음까지 보았습니다
      </p>
    </div>

    <!-- Readiness reference -->
    <dialog ref="readinessModal" class="modal">
      <div class="modal-box max-w-lg">
        <h3 class="font-serif text-[19px] mb-1">상태가 뜻하는 것</h3>
        <p class="text-[12px] text-base-content/68 mb-5">
          분석이 끝났다는 것과 할 일이 남았다는 것은 다릅니다.
        </p>
        <dl class="space-y-4">
          <div v-for="(desc, key) in READINESS_DESC" :key="key">
            <dt class="text-[13px] font-medium">
              {{ READINESS_LABEL[key] || key }}
            </dt>
            <dd
              class="font-serif text-[14px] text-base-content/76 leading-relaxed mt-1"
            >
              {{ desc }}
            </dd>
          </div>
        </dl>
        <div class="modal-action">
          <form method="dialog">
            <button class="btn btn-ghost btn-sm">닫기</button>
          </form>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop">
        <button type="submit">close</button>
      </form>
    </dialog>

    <!-- Cancel -->
    <dialog ref="cancelModalRef" class="modal">
      <div class="modal-box max-w-sm">
        <h3 class="font-serif text-[18px]">분석을 중단합니다</h3>
        <p class="text-[13px] text-base-content/76 mt-3 leading-relaxed">
          세션
          <span class="font-mono">{{ cancelTarget?.rcaId.slice(0, 8) }}</span>
          ({{ cancelTarget?.engine }})의 파이프라인이 다음 단계 전환 시점에
          종료됩니다.
        </p>
        <p
          v-if="cancelError"
          class="text-[13px] text-base-content mark-broken mt-3"
        >
          {{ cancelError }}
        </p>
        <div class="modal-action">
          <button
            class="btn btn-ghost btn-sm"
            :disabled="cancelling"
            @click="closeCancelModal()"
          >
            {{ cancelError ? '닫기' : '그대로 두기' }}
          </button>
          <button
            class="btn btn-warning btn-sm"
            :disabled="cancelling"
            @click="cancelSession()"
          >
            <span
              v-if="cancelling"
              class="loading loading-spinner loading-xs"
              aria-hidden="true"
            />
            {{ cancelling ? '중단 중' : '중단' }}
          </button>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop">
        <button type="submit">close</button>
      </form>
    </dialog>

    <!-- Delete -->
    <dialog ref="deleteModalRef" class="modal">
      <div class="modal-box max-w-sm">
        <h3 class="font-serif text-[18px]">기록을 삭제합니다</h3>
        <p class="text-[13px] text-base-content/76 mt-3 leading-relaxed">
          세션
          <span class="font-mono">{{ deleteTarget?.rcaId.slice(0, 8) }}</span>
          ({{ deleteTarget?.engine }})의 데이터가 사라집니다. 되돌릴 수
          없습니다.
        </p>
        <p
          v-if="deleteError"
          class="text-[13px] text-base-content mark-broken mt-3"
        >
          {{ deleteError }}
        </p>
        <div class="modal-action">
          <button
            class="btn btn-ghost btn-sm"
            :disabled="deleting"
            @click="closeDeleteModal()"
          >
            {{ deleteError ? '닫기' : '그대로 두기' }}
          </button>
          <!-- The spinner is its own element: in DaisyUI 5 `loading` is not a
               button modifier, so a bare `loading` class showed nothing while the
               request was in flight and left the button clickable. -->
          <button
            class="btn btn-error btn-sm"
            :disabled="deleting"
            @click="deleteSession()"
          >
            <span
              v-if="deleting"
              class="loading loading-spinner loading-xs"
              aria-hidden="true"
            />
            {{ deleting ? '삭제 중' : '삭제' }}
          </button>
        </div>
      </div>
      <form method="dialog" class="modal-backdrop">
        <button type="submit">close</button>
      </form>
    </dialog>
  </div>
</template>
