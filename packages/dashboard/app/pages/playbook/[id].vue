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

const reportLink = computed(() =>
  engine ? `/report/${id}?engine=${engine}` : `/report/${id}`,
);

useHead({
  title: () => `플레이북 · ${session.value?.alarmName ?? id.slice(0, 8)}`,
});
</script>

<template>
  <div>
    <header class="mb-9">
      <NuxtLink
        :to="reportLink"
        class="text-[12px] text-base-content/45 hover:text-primary inline-flex items-center gap-1.5 mb-4"
      >
        <span aria-hidden="true">←</span> 보고서로
      </NuxtLink>

      <h1 class="font-serif text-[26px] leading-tight tracking-tight">
        이 장애 유형에 대한 플레이북
      </h1>
      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 mt-3 text-[12px] text-base-content/50"
      >
        <span
          v-if="playbook"
          :class="isVerified ? 'text-success font-medium' : ''"
          :title="
            isVerified
              ? '이 절차는 실행으로 이슈를 해소하고 회고를 거쳤습니다'
              : '실행과 회고를 거치기 전의 플레이북은 초안입니다'
          "
        >
          {{ isVerified ? '검증된 절차' : '초안' }}
        </span>
        <span v-if="session">{{ session.alarmName }}</span>
        <span class="font-mono">{{ session?.engine }}</span>
      </div>
    </header>

    <div
      v-if="status === 'pending'"
      class="py-20 text-center text-[13px] text-base-content/40"
    >
      <span class="loading loading-spinner loading-sm" />
      <p class="mt-3">플레이북을 읽고 있습니다</p>
    </div>

    <div v-else-if="error" class="py-20 text-center">
      <p class="font-serif text-[17px]">플레이북이 없습니다</p>
      <p class="text-[12px] text-base-content/45 mt-2">
        RCA가 완료된 세션에서만 생성됩니다.
      </p>
    </div>

    <div v-else-if="playbook && playbook.spanStatus === 'FAILED'" class="py-8">
      <h2 class="font-serif text-[18px] text-error">
        플레이북 생성이 실패했습니다
      </h2>
      <p
        v-if="playbook.error"
        class="text-[12.5px] font-mono bg-error/5 text-error rounded-box p-4 mt-4 break-words"
      >
        {{ playbook.error }}
      </p>
    </div>

    <template v-else-if="playbook">
      <NuxtLink
        v-if="playbook.revisedByExecutionId"
        :to="`/retrospective/${id}/${playbook.revisedByExecutionId}`"
        class="inline-block text-[12px] text-info hover:underline underline-offset-2 mb-8"
      >
        이전 실행의 회고가 이 절차를 교정했습니다 — 무엇이 왜 바뀌었는지 →
      </NuxtLink>

      <!-- The procedure leads: it is what the execution agent acts on. -->
      <section class="mb-14">
        <div class="flex items-baseline gap-3 mb-1">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold">
            실행 절차
          </h2>
          <span class="text-[11px] text-base-content/35">
            승인 시 이 순서대로 수행됩니다
          </span>
        </div>
        <p
          v-if="!executionSteps.length"
          class="font-serif text-[15px] text-base-content/55 mt-5"
        >
          근본원인이 확정되지 않아 실행할 절차가 없습니다.
        </p>
        <ol v-else class="mt-5 divide-y divide-base-content/[0.07]">
          <li
            v-for="(step, index) in executionSteps"
            :key="step.step_id"
            class="step-row"
          >
            <span class="step-ord" aria-hidden="true">{{
              String(index + 1).padStart(2, '0')
            }}</span>
            <p v-if="step.intent" class="text-[14px] leading-snug">
              {{ step.intent }}
            </p>
            <p
              v-if="step.action"
              class="font-serif text-[13.5px] text-base-content/60 mt-1.5"
            >
              {{ step.action }}
            </p>
            <p
              v-if="step.success_criteria"
              class="text-[12px] text-success/85 mt-1.5"
            >
              성공 판정 · {{ step.success_criteria }}
            </p>
          </li>
        </ol>
        <NuxtLink
          :to="reportLink"
          class="inline-block text-[12.5px] text-primary hover:underline underline-offset-2 mt-6"
        >
          보고서에서 검토하고 승인 →
        </NuxtLink>
      </section>

      <!-- What this playbook recognises, and what to do about it -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-10">
        <section v-if="playbook.symptom_pattern">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-2.5">
            이런 증상일 때
          </h2>
          <div class="prose-field" v-html="md(playbook.symptom_pattern)" />
          <p
            v-if="playbook.failure_type"
            class="text-[12px] text-base-content/45 mt-3"
          >
            유형 · {{ playbook.failure_type }}
          </p>
        </section>

        <section v-if="playbook.severity_criteria">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-2.5">
            심각도 판단
          </h2>
          <div class="prose-field" v-html="md(playbook.severity_criteria)" />
        </section>

        <section v-if="playbook.temporary_mitigation">
          <h2
            class="text-[11px] font-semibold uppercase tracking-[0.1em] text-warning mb-2.5"
          >
            우선 멈추려면
          </h2>
          <div class="prose-field" v-html="md(playbook.temporary_mitigation)" />
        </section>

        <section v-if="playbook.permanent_remediation">
          <h2
            class="text-[11px] font-semibold uppercase tracking-[0.1em] text-success mb-2.5"
          >
            다시 안 나게 하려면
          </h2>
          <div
            class="prose-field"
            v-html="md(playbook.permanent_remediation)"
          />
        </section>

        <section v-if="playbook.escalation_criteria">
          <h2
            class="text-[11px] font-semibold uppercase tracking-[0.1em] text-error mb-2.5"
          >
            사람을 불러야 할 때
          </h2>
          <div class="prose-field" v-html="md(playbook.escalation_criteria)" />
        </section>

        <section v-if="playbook.verification_steps?.length">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-2.5">
            확인 절차
          </h2>
          <div
            v-for="(step, i) in playbook.verification_steps"
            :key="i"
            class="prose-field [&:not(:last-child)]:mb-2"
            v-html="md(step)"
          />
        </section>

        <section v-if="playbook.prevention_measures?.length">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-2.5">
            재발 방지
          </h2>
          <div
            v-for="(m, i) in playbook.prevention_measures"
            :key="i"
            class="prose-field [&:not(:last-child)]:mb-2"
            v-html="md(m)"
          />
        </section>

        <section v-if="playbook.related_metrics?.length">
          <h2 class="label-sm uppercase tracking-[0.1em] font-semibold mb-2.5">
            함께 볼 메트릭
          </h2>
          <div
            v-for="(m, i) in playbook.related_metrics"
            :key="i"
            class="prose-field [&:not(:last-child)]:mb-1.5"
            v-html="md(m)"
          />
        </section>
      </div>

      <div
        v-if="playbook.tags?.length"
        class="mt-12 pt-6 border-t border-base-content/10 flex flex-wrap items-baseline gap-x-3 gap-y-1.5"
      >
        <span class="label-sm">태그</span>
        <span
          v-for="tag in playbook.tags"
          :key="tag"
          class="font-mono text-[11px] text-base-content/50"
        >
          {{ tag }}
        </span>
      </div>

      <div
        v-if="
          !playbook.failure_type && !playbook.symptom_pattern && !playbook.error
        "
        class="py-16 text-center"
      >
        <p class="font-serif text-[17px]">플레이북 내용이 비어 있습니다</p>
        <p class="text-[12px] text-base-content/45 mt-2">
          메타데이터 기록 기능 배포 전에 실행된 세션일 수 있습니다.
        </p>
      </div>
    </template>
  </div>
</template>
