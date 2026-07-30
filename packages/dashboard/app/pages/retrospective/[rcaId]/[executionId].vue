<script setup lang="ts">
const route = useRoute();
const rcaId = route.params.rcaId as string;
const executionId = route.params.executionId as string;

/**
 * A retrospective revises a playbook without anyone approving the revision, so
 * this page exists to make that revision auditable after the fact: the issue,
 * the procedure that ran, what happened, and what changed — side by side. Read
 * apart, none of the four tells you whether the update was justified.
 */
const { data, status, error } = useFetch(
  `/api/retrospectives/${rcaId}/${executionId}`,
);

type Step = {
  step_id?: string;
  intent?: string;
  action?: string;
  success_criteria?: string;
};

const beforeSteps = computed<Step[]>(() => {
  const steps = data.value?.playbookBefore?.execution_steps;
  return Array.isArray(steps) ? (steps as Step[]) : [];
});

const evidenceSteps = computed(() => {
  const steps = data.value?.evidence?.steps;
  return Array.isArray(steps) ? (steps as Record<string, unknown>[]) : [];
});

const diff = computed<Record<string, unknown> | null>(
  () => (data.value?.diff as Record<string, unknown> | null) ?? null,
);

function stringList(field: string): string[] {
  const value = diff.value?.[field];
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : [];
}

const correctedSteps = computed(() => {
  const corrected = diff.value?.corrected_steps;
  return Array.isArray(corrected)
    ? (corrected as Record<string, unknown>[])
    : [];
});

function changeEntries(
  changes: unknown,
): { field: string; before: string; after: string }[] {
  if (changes === null || typeof changes !== 'object') return [];
  return Object.entries(changes as Record<string, unknown>).map(
    ([field, value]) => {
      const change = (value ?? {}) as Record<string, unknown>;
      return {
        field,
        before: String(change.before ?? ''),
        after: String(change.after ?? ''),
      };
    },
  );
}

function attemptsOf(step: Record<string, unknown>): Record<string, unknown>[] {
  const attempts = step.attempts;
  return Array.isArray(attempts) ? (attempts as Record<string, unknown>[]) : [];
}

useHead({ title: () => `Retrospective ${executionId.slice(0, 8)}` });
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-center gap-3">
      <NuxtLink :to="`/report/${rcaId}`">
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
        <h1 class="text-xl font-bold tracking-tight">플레이북 회고</h1>
        <p class="text-sm text-base-content/50">
          실행 증거가 절차를 어떻게 교정했는지 대조합니다.
        </p>
      </div>
      <span
        v-if="data?.execution"
        class="badge badge-sm"
        :class="
          data.execution.state === 'RESOLVED' ? 'badge-success' : 'badge-ghost'
        "
      >
        {{ data.execution.stateLabel }}
      </span>
    </div>

    <div
      v-if="status === 'pending'"
      class="bg-base-100 rounded-xl border border-base-content/5 py-16 flex flex-col items-center text-base-content/40"
    >
      <span class="loading loading-spinner loading-md" />
      <p class="mt-3 text-sm">회고 로딩 중...</p>
    </div>

    <div
      v-else-if="error"
      class="bg-base-100 rounded-xl border border-base-content/5 py-16 text-center text-sm text-base-content/50"
    >
      회고를 불러오지 못했습니다.
    </div>

    <template v-else-if="data">
      <!-- 1. The issue -->
      <section
        class="bg-base-100 rounded-xl border border-base-content/5 p-5 space-y-3"
      >
        <h2 class="text-sm font-bold">1. 이슈</h2>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <div
              class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
            >
              알람
            </div>
            <div class="text-sm mt-1">{{ data.issue.alarmName || '-' }}</div>
          </div>
          <div>
            <div
              class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
            >
              엔진
            </div>
            <div class="text-sm font-mono mt-1">{{ data.issue.engine }}</div>
          </div>
          <div>
            <div
              class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
            >
              원인 확정
            </div>
            <span
              class="badge badge-sm mt-1"
              :class="data.issue.confirmed ? 'badge-success' : 'badge-ghost'"
            >
              {{ data.issue.confirmed ? '확정' : '미확정' }}
            </span>
          </div>
          <div>
            <div
              class="text-[11px] font-medium text-base-content/40 uppercase tracking-wider"
            >
              실행 회차
            </div>
            <div class="text-sm mt-1">{{ data.execution.attempt }}회</div>
          </div>
        </div>
        <p v-if="data.issue.rootCause" class="text-sm text-base-content/70">
          {{ data.issue.rootCause }}
        </p>
      </section>

      <!-- 2. The playbook as it stood before the run -->
      <section
        class="bg-base-100 rounded-xl border border-base-content/5 p-5 space-y-3"
      >
        <h2 class="text-sm font-bold">2. 실행 전 플레이북</h2>
        <p
          v-if="!data.playbookBefore"
          class="text-sm text-base-content/40 bg-base-200/40 rounded-lg px-3 py-2"
        >
          갱신 전 사본이 보존 기간을 지나 조회할 수 없습니다. 아래 diff의 기준을
          확인할 수 없습니다.
        </p>
        <ol v-else-if="beforeSteps.length" class="space-y-2">
          <li
            v-for="step in beforeSteps"
            :key="step.step_id"
            class="border border-base-content/5 rounded-lg px-3 py-2"
          >
            <span class="badge badge-xs badge-ghost font-mono">
              {{ step.step_id }}
            </span>
            <p class="text-sm mt-1">{{ step.action }}</p>
            <p class="text-xs text-base-content/50 mt-1">
              성공 판정: {{ step.success_criteria }}
            </p>
          </li>
        </ol>
        <p v-else class="text-sm text-base-content/40">절차가 없었습니다.</p>
      </section>

      <!-- 3. What was attempted and what failed -->
      <section
        class="bg-base-100 rounded-xl border border-base-content/5 p-5 space-y-3"
      >
        <h2 class="text-sm font-bold">3. 실행 증거</h2>
        <p
          v-if="!data.evidence"
          class="text-sm text-base-content/40 bg-base-200/40 rounded-lg px-3 py-2"
        >
          실행 증거를 조회할 수 없습니다.
        </p>
        <template v-else>
          <p
            v-if="data.evidence.resolution_observation"
            class="text-sm text-base-content/70 bg-base-200/40 rounded-lg px-3 py-2 whitespace-pre-line"
          >
            {{ data.evidence.resolution_observation }}
          </p>
          <div
            v-for="step in evidenceSteps"
            :key="String(step.step_id)"
            class="border border-base-content/5 rounded-lg px-3 py-2 space-y-1"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="badge badge-xs badge-ghost font-mono">
                {{ step.step_id }}
              </span>
              <span
                class="badge badge-xs"
                :class="step.succeeded ? 'badge-success' : 'badge-error'"
              >
                {{ step.succeeded ? '성공' : '실패' }}
              </span>
              <span v-if="step.blocked" class="badge badge-xs badge-warning">
                차단
              </span>
              <span
                v-if="step.manual_action_required"
                class="badge badge-xs badge-warning"
              >
                수동 조치
              </span>
            </div>
            <p v-if="step.observation" class="text-xs text-base-content/60">
              관측: {{ step.observation }}
            </p>
            <div
              v-for="(attempt, index) in attemptsOf(step)"
              :key="index"
              class="text-xs font-mono bg-base-200/40 rounded px-2 py-1"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-base-content/40">
                  #{{ attempt.attempt_index }}
                </span>
                <span
                  v-if="attempt.failure_class"
                  class="badge badge-xs badge-ghost"
                >
                  {{ attempt.failure_class }}
                </span>
              </div>
              <p class="text-base-content/70 break-all">
                {{ attempt.command }}
              </p>
              <p v-if="attempt.block_reason" class="text-warning/80 break-all">
                {{ attempt.block_reason }}
              </p>
              <p
                v-else-if="attempt.error_output"
                class="text-error/70 break-all"
              >
                {{ attempt.error_output }}
              </p>
            </div>
          </div>
        </template>
      </section>

      <!-- 4. How the procedure changed -->
      <section
        class="bg-base-100 rounded-xl border border-base-content/5 p-5 space-y-3"
      >
        <h2 class="text-sm font-bold">4. 갱신 diff</h2>
        <p
          v-if="data.execution.retrospectiveSummary"
          class="text-sm text-base-content/70 bg-base-200/40 rounded-lg px-3 py-2"
        >
          {{ data.execution.retrospectiveSummary }}
        </p>
        <p
          v-if="!diff"
          class="text-sm text-base-content/40 bg-base-200/40 rounded-lg px-3 py-2"
        >
          갱신 diff가 없습니다. 교정할 절차 결함이 없었거나 회고가 실행되지
          않았습니다.
        </p>
        <template v-else>
          <div
            v-for="corrected in correctedSteps"
            :key="String(corrected.step_id)"
            class="border border-base-content/5 rounded-lg px-3 py-2 space-y-2"
          >
            <span class="badge badge-xs badge-info font-mono">
              {{ corrected.step_id }} 교정
            </span>
            <div
              v-for="change in changeEntries(corrected.changes)"
              :key="change.field"
              class="text-xs space-y-1"
            >
              <div class="text-base-content/40 uppercase tracking-wider">
                {{ change.field }}
              </div>
              <p class="text-error/70 line-through break-words">
                {{ change.before }}
              </p>
              <p class="text-success/80 break-words">{{ change.after }}</p>
            </div>
          </div>
          <div v-if="stringList('added_steps').length" class="text-xs">
            <span class="badge badge-xs badge-success">추가된 절차</span>
            <span class="ml-2 font-mono text-base-content/60">
              {{ stringList('added_steps').join(', ') }}
            </span>
          </div>
          <div v-if="stringList('changed_fields').length" class="text-xs">
            <span class="badge badge-xs badge-ghost">변경된 필드</span>
            <span class="ml-2 font-mono text-base-content/60">
              {{ stringList('changed_fields').join(', ') }}
            </span>
          </div>
          <!-- Deletion never happens, so the preserved list shows what survived. -->
          <div v-if="stringList('preserved_steps').length" class="text-xs">
            <span class="badge badge-xs badge-ghost">유지된 절차</span>
            <span class="ml-2 font-mono text-base-content/60">
              {{ stringList('preserved_steps').join(', ') }}
            </span>
          </div>
          <p v-if="diff.rationale" class="text-xs text-base-content/50">
            근거: {{ diff.rationale }}
          </p>
        </template>
      </section>
    </template>
  </div>
</template>
