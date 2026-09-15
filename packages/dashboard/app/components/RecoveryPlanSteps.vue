<script setup lang="ts">
const props = defineProps<{
  steps: unknown[];
  validationError?: string;
  executable: boolean;
}>();

// Legacy records and rejected plans still need a readable representation.
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

const displaySteps = computed(() =>
  props.steps.map((value, index) => {
    const step =
      value && typeof value === 'object'
        ? (value as Record<string, unknown>)
        : {};
    return {
      step_id: text(step.step_id) || String(index + 1),
      action: text(step.action),
      intent: text(step.intent),
      success_criteria: text(step.success_criteria),
      commands: Array.isArray(step.commands)
        ? step.commands.filter(
            (command): command is string =>
              typeof command === 'string' && Boolean(command.trim()),
          )
        : [],
      metricWait:
        step.metric_wait !== null &&
        typeof step.metric_wait === 'object' &&
        !Array.isArray(step.metric_wait)
          ? (step.metric_wait as Record<string, unknown>)
          : null,
    };
  }),
);
const hasDefinitions = computed(() =>
  displaySteps.value.some((step) => step.commands.length || step.metricWait),
);
const allStepsDefined = computed(
  () =>
    displaySteps.value.length > 0 &&
    displaySteps.value.every(
      (step) => Boolean(step.commands.length) !== Boolean(step.metricWait),
    ),
);
</script>

<template>
  <div>
    <div class="plan-notice">
      <p class="font-semibold">
        {{
          executable && allStepsDefined
            ? '사전 확정 런북 · 승인 전 검토'
            : hasDefinitions
              ? '실행 정의 확인 필요 · 승인 불가'
              : '명령 미생성 · 새 분석 필요'
        }}
      </p>
      <p v-if="hasDefinitions" class="mt-1">
        각 단계에 기록된 고정 명령 또는 관측 설정을 표시합니다. 승인 전 명령의
        대상과 리전, 성공 판정 기준을 확인하세요.
      </p>
      <p v-else class="mt-1">
        작업과 성공 판정을 자연어로 기록한 계획입니다. 복사해서 실행할 구체
        명령은 생성되어 있지 않습니다. 명령이 확정된 런북을 얻으려면 새 분석이
        필요합니다.
      </p>
    </div>
    <p
      v-if="!executable && validationError && steps.length"
      class="detail-empty mt-3"
    >
      {{ validationError }}
    </p>
    <div v-if="!steps.length" class="detail-empty mt-4">
      <p>표시할 수 있는 복구 계획 단계가 없습니다.</p>
      <p v-if="validationError" class="mt-1">{{ validationError }}</p>
    </div>
    <ol v-else class="recovery-steps mt-5" aria-label="사고별 복구 계획 단계">
      <li
        v-for="(step, index) in displaySteps"
        :key="`${index}-${step.step_id}`"
        class="step-row"
      >
        <span class="step-ord" aria-hidden="true">
          {{ String(index + 1).padStart(2, '0') }}
        </span>
        <dl class="space-y-3">
          <div>
            <dt class="detail-label">계획된 작업</dt>
            <dd class="detail-body mt-1 whitespace-pre-wrap">
              {{ step.action }}
            </dd>
          </div>
          <div v-if="step.intent">
            <dt class="detail-label">목적</dt>
            <dd class="detail-body mt-1 whitespace-pre-wrap">
              {{ step.intent }}
            </dd>
          </div>
          <div v-if="step.success_criteria" class="plan-criteria">
            <dt class="detail-label text-info">성공 판정 기준</dt>
            <dd class="detail-body mt-1 whitespace-pre-wrap">
              {{ step.success_criteria }}
            </dd>
          </div>
        </dl>
        <div v-if="step.commands.length" class="mt-4 min-w-0">
          <h4 class="text-[15px] font-semibold">
            {{ executable && allStepsDefined ? '사전 확정' : '기록된' }} AWS CLI
            명령 · {{ step.commands.length }}개
          </h4>
          <p class="detail-label mt-1">
            대상과 리전을 포함한 명령 원문입니다. 승인되면 아래 순서대로
            실행됩니다.
          </p>
          <div
            v-for="(command, commandIndex) in step.commands"
            :key="commandIndex"
            class="mt-3"
          >
            <p class="detail-label">명령 {{ commandIndex + 1 }}</p>
            <pre
              class="plan-command"
              tabindex="0"
              :aria-label="`${index + 1}단계 명령 ${commandIndex + 1}`"
            ><code>{{ command }}</code></pre>
          </div>
        </div>
        <MetricWaitDetails v-if="step.metricWait" :wait="step.metricWait" />
        <p
          v-if="!step.commands.length && !step.metricWait"
          class="mt-4 text-[14px] text-warning"
        >
          명령 미생성 · 새 분석 필요. 이 단계는 자연어 계획으로만 남아 있으며
          승인할 수 없습니다.
        </p>
      </li>
    </ol>
  </div>
</template>
