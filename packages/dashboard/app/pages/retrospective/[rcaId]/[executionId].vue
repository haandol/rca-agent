<script setup lang="ts">
const route = useRoute();
const rcaId = route.params.rcaId as string;
const executionId = route.params.executionId as string;

/**
 * A retrospective revises a playbook without anyone approving the revision, so
 * this page exists to make that revision auditable after the fact: the issue,
 * the procedure that ran, what happened, and what changed — side by side. Read
 * apart, none of the four tells you whether the update was justified.
 *
 * The four are numbered because they are a chain of custody, not a menu: the
 * diff in step 4 is only defensible in terms of the evidence in step 3, which is
 * only readable against the procedure in step 2.
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

/**
 * The attempts worth reading first.
 *
 * A single step can record over thirty attempts, and a run of four steps reaches
 * a hundred — rendered flat, the ones that explain the revision are buried among
 * the ones that simply worked. Blocked and failed attempts are what the
 * retrospective corrected against, so those stay open and the rest fold away.
 */
function notableAttempts(
  step: Record<string, unknown>,
): Record<string, unknown>[] {
  return attemptsOf(step).filter(
    (attempt) =>
      Boolean(attempt.block_reason) ||
      Boolean(attempt.error_output) ||
      Boolean(attempt.failure_class),
  );
}

function routineAttempts(
  step: Record<string, unknown>,
): Record<string, unknown>[] {
  const notable = new Set(notableAttempts(step));
  return attemptsOf(step).filter((attempt) => !notable.has(attempt));
}

useHead({ title: () => `회고 ${executionId.slice(0, 8)}` });
</script>

<template>
  <div>
    <header class="mb-10">
      <NuxtLink
        :to="`/report/${rcaId}`"
        class="text-[12px] text-base-content/45 hover:text-primary inline-flex items-center gap-1.5 mb-4"
      >
        <span aria-hidden="true">←</span> 보고서로
      </NuxtLink>

      <h1 class="font-serif text-[26px] leading-tight tracking-tight">
        실행이 절차를 어떻게 고쳤는지
      </h1>
      <p class="text-[13px] text-base-content/50 mt-2.5 max-w-[62ch]">
        회고는 사람의 승인 없이 플레이북을 고칩니다. 그 수정이 정당했는지는 아래
        네 가지를 순서대로 읽어야 판단할 수 있습니다.
      </p>

      <div
        v-if="data"
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 mt-4 text-[12px] text-base-content/50"
      >
        <span
          :class="
            data.execution.state === 'RESOLVED'
              ? 'text-success font-medium'
              : ''
          "
        >
          {{ data.execution.stateLabel }}
        </span>
        <span>{{ data.execution.attempt }}회차</span>
        <span v-if="data.issue.alarmName">{{ data.issue.alarmName }}</span>
        <span class="font-mono">{{ data.issue.engine }}</span>
      </div>
    </header>

    <div
      v-if="status === 'pending'"
      class="py-20 text-center text-[13px] text-base-content/40"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">회고를 읽고 있습니다</p>
    </div>

    <div v-else-if="error" class="py-20 text-center font-serif text-[17px]">
      회고를 불러오지 못했습니다
    </div>

    <template v-else-if="data">
      <!-- 1. The issue -->
      <section class="mb-11">
        <h2
          class="flex items-baseline gap-2.5 label-sm uppercase tracking-[0.1em] font-semibold mb-4"
        >
          <span class="font-mono text-[11px] text-primary">01</span>
          이슈
        </h2>
        <p
          v-if="data.issue.rootCause"
          class="font-serif text-[14px] text-base-content/70"
        >
          {{ data.issue.rootCause }}
        </p>
        <p v-else class="text-[13px] text-base-content/40">
          확정된 근본원인이 기록되지 않았습니다.
        </p>
      </section>

      <!-- 2. The playbook as it stood before the run -->
      <section class="mb-11">
        <h2
          class="flex items-baseline gap-2.5 label-sm uppercase tracking-[0.1em] font-semibold mb-4"
        >
          <span class="font-mono text-[11px] text-primary">02</span>
          실행 전 플레이북
        </h2>
        <p
          v-if="!data.playbookBefore"
          class="text-[13px] text-base-content/45 bg-base-200 rounded-box px-4 py-3"
        >
          갱신 전 사본이 보존 기간을 지나 조회할 수 없습니다. 아래 diff의 기준을
          확인할 수 없습니다.
        </p>
        <ol
          v-else-if="beforeSteps.length"
          class="divide-y divide-base-content/[0.07]"
        >
          <li
            v-for="(step, index) in beforeSteps"
            :key="step.step_id"
            class="step-row"
          >
            <span class="step-ord" aria-hidden="true">{{
              String(index + 1).padStart(2, '0')
            }}</span>
            <p class="text-[13.5px] leading-snug">{{ step.action }}</p>
            <p
              v-if="step.success_criteria"
              class="text-[12px] text-base-content/50 mt-1.5"
            >
              성공 판정 · {{ step.success_criteria }}
            </p>
          </li>
        </ol>
        <p v-else class="text-[13px] text-base-content/40">
          절차가 없었습니다.
        </p>
      </section>

      <!-- 3. What was attempted and what failed -->
      <section class="mb-11">
        <h2
          class="flex items-baseline gap-2.5 label-sm uppercase tracking-[0.1em] font-semibold mb-4"
        >
          <span class="font-mono text-[11px] text-primary">03</span>
          실행 증거
        </h2>
        <p
          v-if="!data.evidence"
          class="text-[13px] text-base-content/45 bg-base-200 rounded-box px-4 py-3"
        >
          실행 증거를 조회할 수 없습니다.
        </p>
        <template v-else>
          <p
            v-if="data.evidence.resolution_observation"
            class="font-serif text-[14px] text-base-content/70 bg-base-200 rounded-box px-4 py-3 whitespace-pre-line"
          >
            {{ data.evidence.resolution_observation }}
          </p>
          <div
            v-for="step in evidenceSteps"
            :key="String(step.step_id)"
            class="py-3.5 border-t border-base-content/[0.07] space-y-1.5"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-mono text-[11px] text-base-content/40">{{
                step.step_id
              }}</span>
              <span
                class="text-[11px] font-medium"
                :class="step.succeeded ? 'text-success' : 'text-error'"
                >{{ step.succeeded ? '성공' : '실패' }}</span
              >
              <span v-if="step.blocked" class="text-[11px] text-warning"
                >차단</span
              >
              <span
                v-if="step.manual_action_required"
                class="text-[11px] text-warning"
                >수동 조치</span
              >
            </div>
            <p v-if="step.observation" class="text-xs text-base-content/60">
              관측 · {{ step.observation }}
            </p>

            <!-- Blocked and failed attempts: what the revision answers to -->
            <div
              v-for="(attempt, index) in notableAttempts(step)"
              :key="`n-${index}`"
              class="text-xs font-mono bg-base-200 rounded px-2.5 py-1.5"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-base-content/35">
                  #{{ attempt.attempt_index }}
                </span>
                <span
                  v-if="attempt.failure_class"
                  class="text-base-content/45"
                  >{{ attempt.failure_class }}</span
                >
              </div>
              <p class="text-base-content/70 break-all mt-1">
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

            <!-- Attempts that simply ran. Kept, but out of the way. -->
            <details v-if="routineAttempts(step).length" class="group/d">
              <summary
                class="text-xs text-base-content/45 cursor-pointer hover:text-base-content/70 transition-colors list-none flex items-center gap-1.5"
              >
                <span
                  class="inline-block transition-transform group-open/d:rotate-90"
                  aria-hidden="true"
                  >▸</span
                >
                통과한 명령 {{ routineAttempts(step).length }}건
              </summary>
              <div class="space-y-1 mt-2">
                <p
                  v-for="(attempt, index) in routineAttempts(step)"
                  :key="`r-${index}`"
                  class="text-xs font-mono text-base-content/50 bg-base-200 rounded px-2.5 py-1 break-all"
                >
                  <span class="text-base-content/30"
                    >#{{ attempt.attempt_index }}</span
                  >
                  {{ attempt.command }}
                </p>
              </div>
            </details>
          </div>
        </template>
      </section>

      <!-- 4. How the procedure changed -->
      <section class="mb-11">
        <h2
          class="flex items-baseline gap-2.5 label-sm uppercase tracking-[0.1em] font-semibold mb-4"
        >
          <span class="font-mono text-[11px] text-primary">04</span>
          갱신 diff
        </h2>
        <p
          v-if="data.execution.retrospectiveSummary"
          class="font-serif text-[14px] text-base-content/70 bg-base-200 rounded-box px-4 py-3"
        >
          {{ data.execution.retrospectiveSummary }}
        </p>
        <p
          v-if="!diff"
          class="text-[13px] text-base-content/45 bg-base-200 rounded-box px-4 py-3"
        >
          갱신 diff가 없습니다. 교정할 절차 결함이 없었거나 회고가 실행되지
          않았습니다.
        </p>
        <template v-else>
          <div
            v-for="corrected in correctedSteps"
            :key="String(corrected.step_id)"
            class="py-3.5 border-t border-base-content/[0.07] space-y-2.5"
          >
            <span class="font-mono text-[11px] text-info"
              >{{ corrected.step_id }} 교정</span
            >
            <div
              v-for="change in changeEntries(corrected.changes)"
              :key="change.field"
              class="text-xs space-y-1"
            >
              <div class="label-sm">{{ change.field }}</div>
              <p class="text-base-content/40 line-through break-words">
                {{ change.before }}
              </p>
              <p class="text-success break-words">{{ change.after }}</p>
            </div>
          </div>
          <div
            v-if="stringList('added_steps').length"
            class="flex items-center gap-2 text-xs"
          >
            <span class="text-[11px] text-success">추가된 절차</span>
            <span class="font-mono text-base-content/60">
              {{ stringList('added_steps').join(', ') }}
            </span>
          </div>
          <div
            v-if="stringList('changed_fields').length"
            class="flex items-center gap-2 text-xs"
          >
            <span class="text-[11px] text-base-content/45">변경된 필드</span>
            <span class="font-mono text-base-content/60">
              {{ stringList('changed_fields').join(', ') }}
            </span>
          </div>
          <!-- Deletion never happens, so the preserved list shows what survived. -->
          <div
            v-if="stringList('preserved_steps').length"
            class="flex items-center gap-2 text-xs"
          >
            <span class="text-[11px] text-base-content/45">유지된 절차</span>
            <span class="font-mono text-base-content/60">
              {{ stringList('preserved_steps').join(', ') }}
            </span>
          </div>
          <p v-if="diff.rationale" class="text-xs text-base-content/50">
            근거 · {{ diff.rationale }}
          </p>
        </template>
      </section>
    </template>
  </div>
</template>
