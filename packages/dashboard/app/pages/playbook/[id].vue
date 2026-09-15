<script setup lang="ts">
import { renderMarkdown as md } from '~/utils/markdown';

const route = useRoute();
const id = route.params.id as string;
const engine = (route.query.engine as string) || '';

const {
  data: playbook,
  status,
  error,
} = useFetch(`/api/playbooks/${id}`, {
  query: engine ? { engine } : undefined,
});

// This page needs one session, so it reads that session rather than the list —
// searching a paged list would miss anything past the first page.
const { data: session } = useFetch(`/api/sessions/${id}`, {
  query: engine ? { engine } : undefined,
});

// Anything other than the recorded VERIFIED reads as a draft: a procedure that
// has never run must not look proven.
const isVerified = computed(
  () => playbook.value?.verification_status === 'VERIFIED',
);
const executionSteps = computed(() => playbook.value?.execution_steps ?? []);

// Knowledge is reusable across incidents; the selected incident's plan follows it.
const knowledgeSections = computed(() => {
  const book = playbook.value;
  if (!book) return [];
  return [
    {
      id: 'metrics',
      title: '관측 메트릭',
      description: '장애를 식별할 때 함께 확인할 지표',
      items: book.related_metrics,
      ordered: false,
    },
    {
      id: 'symptoms',
      title: '증상과 장애 유형',
      description: '이 플레이북을 적용할 상황',
      items: [book.symptom_pattern],
      ordered: false,
    },
    {
      id: 'severity',
      title: '심각도 판단',
      description: '영향과 대응 우선순위를 판단하는 기준',
      items: [book.severity_criteria],
      ordered: false,
    },
    {
      id: 'verification',
      title: '검증 절차',
      description: '상태를 확인하기 위한 점검',
      items: book.verification_steps,
      ordered: true,
    },
    {
      id: 'mitigation',
      title: '임시 완화',
      description: '진행 중인 영향을 줄이는 대응 지식',
      items: [book.temporary_mitigation],
      ordered: false,
    },
    {
      id: 'remediation',
      title: '영구 대책',
      description: '원인을 제거하기 위한 대응 지식',
      items: [book.permanent_remediation],
      ordered: false,
    },
    {
      id: 'prevention',
      title: '재발 방지',
      description: '반복 발생을 줄이기 위한 개선 사항',
      items: book.prevention_measures,
      ordered: false,
    },
    {
      id: 'escalation',
      title: '에스컬레이션',
      description: '다른 담당자나 팀에 대응을 요청할 조건',
      items: [book.escalation_criteria],
      ordered: false,
    },
  ].map((section) => ({
    ...section,
    items: section.items.filter((item) => item.trim()),
  }));
});

const reportLink = computed(() =>
  engine ? `/report/${id}?engine=${engine}` : `/report/${id}`,
);

useHead({
  title: () => `플레이북 · ${session.value?.alarmName ?? id.slice(0, 8)}`,
});
</script>

<template>
  <div>
    <header class="mb-7">
      <NuxtLink
        :to="reportLink"
        class="mb-4 inline-flex items-center gap-1.5 text-[11px] text-base-content/85 hover:text-primary"
      >
        <span aria-hidden="true">←</span> 보고서로
      </NuxtLink>

      <p class="page-eyebrow">Remediation Playbook</p>
      <h1 class="page-title">이 장애 유형에 대한 플레이북</h1>
      <p class="page-description">
        장애를 알아보고, 검증하고, 대응할 때 참고하는 지식입니다. 이번 사고의
        복구 계획은 지식 뒤에 별도로 표시합니다.
      </p>
      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 mt-3 text-[12px] text-base-content/85"
      >
        <span
          v-if="playbook"
          class="status-chip"
          :class="isVerified ? 'text-success' : 'text-warning'"
          :title="
            isVerified
              ? '플레이북에 기록된 상태: VERIFIED'
              : '플레이북에 검증 완료 상태가 기록되지 않았습니다'
          "
        >
          {{ isVerified ? '플레이북 검증됨' : '플레이북 초안' }}
        </span>
        <span v-if="session">{{ session.alarmName }}</span>
        <span class="font-mono">{{ session?.engine }}</span>
      </div>
    </header>

    <div
      v-if="status === 'pending'"
      class="py-20 text-center text-[13px] text-base-content/85"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">플레이북을 읽고 있습니다</p>
    </div>

    <div v-else-if="error" class="py-20 text-center">
      <p class="text-[17px] font-semibold">
        {{
          error.statusCode === 404
            ? '이 세션의 플레이북을 찾을 수 없습니다'
            : '플레이북을 불러오지 못했습니다'
        }}
      </p>
      <p class="detail-body mt-2">보고서에서 분석 상태를 확인하세요.</p>
    </div>

    <div v-else-if="playbook && playbook.spanStatus === 'FAILED'" class="py-8">
      <h2 class="font-serif text-[18px] mark-broken">
        플레이북 생성이 실패했습니다
      </h2>
      <p
        v-if="playbook.error"
        class="text-[12.5px] font-mono bg-base-200 rounded-box p-4 mt-4 break-words"
      >
        {{ playbook.error }}
      </p>
    </div>

    <template v-else-if="playbook">
      <NuxtLink
        v-if="playbook.revisedByExecutionId"
        :to="`/retrospective/${id}/${playbook.revisedByExecutionId}`"
        class="inline-block text-[12px] text-primary hover:underline underline-offset-2 mb-8"
      >
        이전 실행의 회고가 이 절차를 교정했습니다 — 무엇이 왜 바뀌었는지 →
      </NuxtLink>

      <nav class="section-nav mb-5" aria-label="플레이북 섹션 이동">
        <a
          v-for="section in knowledgeSections"
          :key="section.id"
          :href="`#${section.id}`"
          >{{ section.title }}</a
        >
        <a href="#incident-plan">이번 사고의 복구 계획</a>
      </nav>

      <div class="grid min-w-0 grid-cols-1 gap-5 xl:grid-cols-2">
        <section
          v-for="section in knowledgeSections"
          :id="section.id"
          :key="section.id"
          class="ops-panel detail-section min-w-0 p-5 sm:p-6"
        >
          <h2 class="detail-section-title">{{ section.title }}</h2>
          <p class="detail-label mt-1 mb-4">{{ section.description }}</p>
          <p
            v-if="section.id === 'symptoms' && playbook.failure_type"
            class="mb-4 text-[13px] text-info break-words"
          >
            장애 유형 · {{ playbook.failure_type }}
          </p>
          <template v-if="section.items.length">
            <ol v-if="section.ordered" class="knowledge-list list-decimal">
              <li v-for="(item, i) in section.items" :key="i">
                <div class="prose-field" v-html="md(item)" />
              </li>
            </ol>
            <ul
              v-else-if="section.items.length > 1"
              class="knowledge-list list-disc"
            >
              <li v-for="(item, i) in section.items" :key="i">
                <div class="prose-field" v-html="md(item)" />
              </li>
            </ul>
            <div v-else class="prose-field" v-html="md(section.items[0])" />
          </template>
          <p v-else class="detail-empty">
            이 항목은 플레이북에 기록되어 있지 않습니다.
          </p>
        </section>
      </div>

      <section
        id="incident-plan"
        class="ops-panel detail-section mt-7 border-t-[3px] border-t-warning p-5 sm:p-6"
      >
        <h2 class="detail-section-title">이번 사고의 복구 계획</h2>
        <p class="detail-body mt-2 mb-5">
          위 지식을 바탕으로 이번 사고에 작성된 계획입니다. 작업 대상과 근거는
          연결된 보고서에서 검토하세요.
        </p>
        <RecoveryPlanSteps
          :steps="executionSteps"
          :validation-error="playbook.validationError"
          :executable="playbook.executable === true"
        />
        <NuxtLink
          :to="`${reportLink}#recovery-plan`"
          class="btn btn-outline btn-sm mt-6"
        >
          보고서에서 계획과 승인 가능 여부 확인 →
        </NuxtLink>
      </section>

      <div
        v-if="playbook.tags?.length"
        class="mt-12 pt-6 border-t border-base-content/10 flex flex-wrap items-baseline gap-x-3 gap-y-1.5"
      >
        <span class="label-sm">태그</span>
        <span
          v-for="tag in playbook.tags"
          :key="tag"
          class="font-mono text-[11px] text-base-content/85"
        >
          {{ tag }}
        </span>
      </div>
    </template>
  </div>
</template>
