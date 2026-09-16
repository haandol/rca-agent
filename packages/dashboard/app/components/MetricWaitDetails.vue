<script setup lang="ts">
const props = defineProps<{ wait: Record<string, unknown> }>();

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function display(value: unknown): string {
  if (value === undefined || value === null || value === '') return '미기록';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

const metrics = computed(() =>
  Object.entries(record(props.wait.metrics)).map(([role, value]) => ({
    role,
    namespace: record(value).namespace,
    metric_name: record(value).metric_name,
    dimensions: record(value).dimensions,
  })),
);
const roleLabels: Record<string, string> = {
  attempts: '시도',
  failures: '실패',
  latency: '지연',
};
</script>

<template>
  <div class="mt-4 min-w-0">
    <h4 class="text-[15px] font-semibold text-info">조치 후 고정 구간 관측</h4>
    <p v-if="wait.deployment_step_id" class="detail-body mt-2">
      최초 수렴 확인 시각이 속한 분의 다음 분부터 두 완결된 60초 구간을
      관측합니다. 관측 중 같은 배포가 유지되어야 하며, 실제 쓰기 완료 증거는
      별도로 확인합니다.
    </p>
    <dl class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div>
        <dt class="detail-label">
          {{
            wait.deployment_step_id
              ? '관측 기준이 되는 배포 수렴 단계'
              : '관측 기준이 되는 조치 단계'
          }}
        </dt>
        <dd class="detail-body font-mono">
          {{ display(wait.deployment_step_id ?? wait.action_step_id) }}
        </dd>
      </div>
      <div>
        <dt class="detail-label">리전</dt>
        <dd class="detail-body font-mono">{{ display(wait.region) }}</dd>
      </div>
      <div>
        <dt class="detail-label">실패 알람</dt>
        <dd class="detail-body font-mono">
          {{ display(wait.failure_alarm_name) }}
        </dd>
      </div>
      <div v-if="wait.latency_alarm_name">
        <dt class="detail-label">지연 알람</dt>
        <dd class="detail-body font-mono">
          {{ display(wait.latency_alarm_name) }}
        </dd>
      </div>
      <div>
        <dt class="detail-label">최대 대기시간</dt>
        <dd class="detail-body">
          {{
            wait.max_wait_seconds === undefined
              ? '900초 (15분, 기본값)'
              : wait.max_wait_seconds === 900
                ? '900초 (15분)'
                : `${display(wait.max_wait_seconds)}초`
          }}
        </dd>
      </div>
    </dl>
    <ul class="mt-4 space-y-3" aria-label="관측 대상 메트릭">
      <li
        v-for="metric in metrics"
        :key="metric.role"
        class="rounded-md border border-base-content/15 p-3"
      >
        <p class="detail-label">
          {{ roleLabels[metric.role] || metric.role }} 메트릭
        </p>
        <p class="detail-body font-mono mt-1">
          {{ display(metric.namespace) }} / {{ display(metric.metric_name) }}
        </p>
        <p class="detail-label mt-2">대상 조건 (dimensions)</p>
        <p class="detail-body font-mono">{{ display(metric.dimensions) }}</p>
      </li>
    </ul>
    <details class="mt-4">
      <summary class="cursor-pointer text-[13px] text-primary py-2">
        고정 관측 설정 JSON 전체
      </summary>
      <pre
        class="plan-command"
        tabindex="0"
        aria-label="고정 관측 설정 JSON"
      ><code>{{ JSON.stringify(wait, null, 2) }}</code></pre>
    </details>
  </div>
</template>
