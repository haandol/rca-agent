<script setup lang="ts">
const props = defineProps<{
  steps: unknown[];
  rollbackContext?: unknown;
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
      rawOperation: step.raw_operation ?? step,
      hasRawOperation: [
        'commands',
        'metric_wait',
        'deployment_wait',
        'ecs_service_precondition',
      ].some((key) => step[key] != null),
      guard: step.ecs_service_precondition,
      deploymentWait: step.deployment_wait,
      deploymentSeconds:
        step.deployment_wait && typeof step.deployment_wait === 'object'
          ? (step.deployment_wait as Record<string, unknown>).max_wait_seconds
          : undefined,
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
  displaySteps.value.some(
    (step) => step.commands.length || step.metricWait || step.deploymentWait,
  ),
);
const allStepsDefined = computed(
  () =>
    displaySteps.value.length > 0 &&
    displaySteps.value.every(
      (step) =>
        [
          Boolean(step.commands.length),
          Boolean(step.metricWait),
          Boolean(step.deploymentWait),
        ].filter(Boolean).length === 1,
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
    <section
      v-if="rollbackContext"
      class="mt-4 min-w-0 rounded-md border border-warning/40 p-4"
    >
      <h4 class="font-semibold text-warning">확인된 정상 근거와 현재 배포</h4>
      <p class="detail-body mt-2">
        분석 서버가 저장 원본을 검증해 기록한 값입니다. 아래 저장
        근거·계정·리전·서비스·현재 배포 ID·결함 이미지·정상 태스크 정의와 이미지
        지문·유지할 설정이 승인 사본에 그대로 포함됩니다. 승인 시 실제 ECS
        상태를 다시 조회하며 달라지면 거부합니다.
      </p>
      <pre
        class="plan-command"
        tabindex="0"
        aria-label="rollback_context 전체"
      ><code>{{ JSON.stringify(rollbackContext, null, 2) }}</code></pre>
    </section>
    <ol
      v-if="steps.length"
      class="recovery-steps mt-5"
      aria-label="사고별 복구 계획 단계"
    >
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
        <section v-if="step.guard != null" class="mt-4 min-w-0">
          <h4 class="font-semibold text-warning">
            실행 직전 현재 배포 전제 · ecs_service_precondition
          </h4>
          <pre
            class="plan-command"
            tabindex="0"
          ><code>{{ JSON.stringify(step.guard, null, 2) }}</code></pre>
        </section>
        <section v-if="step.deploymentWait != null" class="mt-4 min-w-0">
          <h4 class="font-semibold text-info">
            정상 배포 수렴 확인 · deployment_wait
          </h4>
          <p class="detail-body mt-2">
            새 배포와 모든 앱 태스크가 정상 태스크 정의·이미지·상태로 전환되고
            기존 결함 태스크가 중지됐는지 확인합니다. API 성공만으로 수렴을
            판정하지 않습니다.
          </p>
          <p class="detail-body mt-2">
            최대 대기시간 ·
            {{
              step.deploymentSeconds === 900
                ? '900초 (15분)'
                : step.deploymentSeconds === undefined
                  ? '미기록'
                  : `${step.deploymentSeconds}초`
            }}
          </p>
          <pre
            class="plan-command"
            tabindex="0"
          ><code>{{ JSON.stringify(step.deploymentWait, null, 2) }}</code></pre>
        </section>
        <details v-if="!executable && step.hasRawOperation" class="mt-4">
          <summary class="cursor-pointer text-primary">
            검증되지 않은 단계 원문 전체
          </summary>
          <pre
            class="plan-command"
            tabindex="0"
          ><code>{{ JSON.stringify(step.rawOperation, null, 2) }}</code></pre>
        </details>
        <MetricWaitDetails v-if="step.metricWait" :wait="step.metricWait" />
        <p
          v-if="
            !step.commands.length && !step.metricWait && !step.deploymentWait
          "
          class="mt-4 text-[14px] text-warning"
        >
          명령 미생성 · 새 분석 필요. 이 단계는 자연어 계획으로만 남아 있으며
          승인할 수 없습니다.
        </p>
      </li>
    </ol>
  </div>
</template>
