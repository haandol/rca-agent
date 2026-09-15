<script setup lang="ts">
import type {
  PlaybookComparison,
  PlaybookUsedReference,
} from '../../shared/types/playbook-library';
const props = defineProps<{
  comparison: PlaybookComparison | null;
  rcaId?: string;
  engine?: string;
  pending?: boolean;
  error?: string;
}>();
const baseline = computed(
  () =>
    props.comparison?.baseline?.playbook ??
    props.comparison?.proposal?.before ??
    null,
);
defineEmits<{
  apply: [];
  reject: [];
}>();
const labels = {
  UPDATE_PROPOSED: '변경 제안',
  NO_CHANGE: '변경 불필요',
  NO_MATCH: '유사 후보 없음',
  NO_APPLICABLE_MATCH: '적용 가능한 후보 없음',
  SEARCH_FAILED: '검색·비교 실패',
};
/**
 * Keep recorded before/after values inspectable without interpreting them as markup.
 * Preserve strings verbatim, serialize structured values and label absent values.
 */
function display(value: unknown) {
  return typeof value === 'string'
    ? value
    : (JSON.stringify(value, null, 2) ?? '미제공');
}

const referenceRoles: Record<string, string> = {
  'knowledge-reuse': '재사용한 대응 지식',
  'current-runbook-input': '현재 사고의 런북 생성 입력',
  'comparison-evidence': '판단에 인용한 관측 근거',
};
const referencePurposes: Record<string, string> = {
  'knowledge-reuse':
    '장애 유형별 대응 지식의 기준으로 재사용한 원문입니다. 현재 사고의 실행 명령과 구분합니다.',
  'current-runbook-input':
    '이번 사고의 런북을 작성할 때 입력으로 제공한 원문입니다.',
  'comparison-evidence':
    '변경 여부 판단에 실제로 인용한 관측값입니다. 같은 참조의 관측값을 모두 보존합니다.',
};
/** Resolve each recorded use to its frozen source, never to a newer library lookup or a considered candidate. */
function referenceBody(reference: PlaybookUsedReference): unknown {
  const comparison = props.comparison;
  if (!comparison) return undefined;
  if (reference.role === 'knowledge-reuse') {
    const saved = comparison.baseline;
    if (
      !saved ||
      (reference.playbook_id && reference.playbook_id !== saved.playbook_id) ||
      (reference.revision && reference.revision !== saved.revision) ||
      (reference.source_rca_id &&
        reference.source_rca_id !== saved.source_rca_id) ||
      (reference.source_engine &&
        reference.source_engine !== saved.source_engine)
    )
      return undefined;
    return saved.playbook;
  }
  if (reference.role === 'current-runbook-input') {
    if (reference.ref === 'current_report')
      return comparison.inputs?.current_report;
    if (reference.ref === 'analysis') return comparison.inputs?.analysis;
    return undefined;
  }
  if (reference.role === 'comparison-evidence') {
    const observations =
      comparison.evidence?.filter((entry) => entry.ref === reference.ref) ?? [];
    return observations.length ? observations : undefined;
  }
  return undefined;
}
/** Show recorded baseline identity or the API-bound current incident; missing provenance stays visibly missing. */
function referenceIdentity(reference: PlaybookUsedReference) {
  const historical =
    reference.role === 'knowledge-reuse' ? props.comparison?.baseline : null;
  const current =
    reference.role === 'current-runbook-input' ||
    reference.role === 'comparison-evidence';
  return {
    rca:
      historical?.source_rca_id ||
      (current ? props.rcaId : '') ||
      reference.source_rca_id ||
      '',
    engine:
      historical?.source_engine ||
      (current ? props.engine : '') ||
      reference.source_engine ||
      '',
    revision: historical?.revision || reference.revision || '',
    playbook: historical?.playbook_id || reference.playbook_id || '',
  };
}
</script>
<template>
  <div v-if="comparison" class="space-y-5">
    <div>
      <p class="status-chip">{{ labels[comparison.status] }}</p>
    </div>
    <ol
      v-if="comparison.used_references?.length"
      class="space-y-4"
      aria-label="생성에 실제 사용한 참고 자료"
    >
      <li
        v-for="(reference, index) in comparison.used_references"
        :key="`${reference.role}-${reference.ref}-${index}`"
        class="ops-panel p-4 space-y-3"
      >
        <div class="flex flex-wrap items-center gap-3">
          <h3 class="detail-section-title">
            {{ index + 1 }}.
            {{ referenceRoles[reference.role] || reference.role }}
          </h3>
          <span class="font-mono text-xs text-info break-all">{{
            reference.ref
          }}</span>
        </div>
        <p class="detail-body">
          {{
            referencePurposes[reference.role] ||
            '기록된 역할의 활용 설명은 제공되지 않았습니다.'
          }}
        </p>
        <dl class="flex flex-wrap gap-x-5 gap-y-2 text-xs">
          <div>
            <dt class="detail-label">원본 RCA</dt>
            <dd class="font-mono break-all">
              {{ referenceIdentity(reference).rca || '미기록' }}
            </dd>
          </div>
          <div>
            <dt class="detail-label">엔진</dt>
            <dd class="font-mono">
              {{ referenceIdentity(reference).engine || '미기록' }}
            </dd>
          </div>
          <div v-if="referenceIdentity(reference).playbook">
            <dt class="detail-label">플레이북</dt>
            <dd class="font-mono break-all">
              {{ referenceIdentity(reference).playbook }}
            </dd>
          </div>
          <div v-if="referenceIdentity(reference).revision">
            <dt class="detail-label">당시 개정본</dt>
            <dd class="font-mono break-all">
              {{ referenceIdentity(reference).revision }}
            </dd>
          </div>
        </dl>
        <p
          v-if="reference.source_path"
          class="detail-label font-mono break-all"
        >
          원문 위치 · {{ reference.source_path }}
        </p>
        <details v-if="referenceBody(reference) !== undefined">
          <summary class="cursor-pointer text-sm font-semibold text-primary">
            이 자료가 제공한 내용 · 고정 원문 보기
          </summary>
          <pre class="mt-3 whitespace-pre-wrap break-all text-sm">{{
            display(referenceBody(reference))
          }}</pre>
        </details>
        <p v-else class="detail-empty">
          기록된 참조에 해당하는 고정 원문을 조회할 수 없습니다.
        </p>
      </li>
    </ol>
    <template v-else>
      <p class="detail-empty">
        이 과거 기록에는 실제 사용한 참고 자료 목록이 기록되지 않았습니다. 검색
        후보를 생성 출처로 간주하지 않습니다.
      </p>
      <details v-if="baseline" class="ops-panel p-4">
        <summary class="cursor-pointer text-sm font-semibold">
          비교 기록의 기준 원문 · 실제 사용 여부 미기록
        </summary>
        <p
          v-if="comparison.baseline"
          class="detail-label mt-3 font-mono break-all"
        >
          {{ comparison.baseline.playbook_id }} ·
          {{ comparison.baseline.revision }} ·
          {{ comparison.baseline.source_rca_id }} ·
          {{ comparison.baseline.source_engine }}
        </p>
        <pre class="mt-3 whitespace-pre-wrap break-all text-xs">{{
          display(baseline)
        }}</pre>
      </details>
      <details v-if="comparison.evidence?.length" class="ops-panel p-4">
        <summary class="cursor-pointer text-sm font-semibold">
          비교 기록에 보존된 관측값
        </summary>
        <pre class="mt-3 whitespace-pre-wrap break-all text-xs">{{
          display(comparison.evidence)
        }}</pre>
      </details>
    </template>
    <template v-if="comparison.proposal">
      <section class="ops-panel p-4 space-y-3">
        <div class="flex flex-wrap gap-3 items-center">
          <h3 class="detail-section-title">대응 지식 업데이트 제안</h3>
          <span
            class="status-chip"
            :class="
              comparison.proposal.state === 'PENDING'
                ? 'text-warning'
                : 'text-info'
            "
          >
            {{
              { PENDING: '검토 대기', APPLIED: '반영됨', REJECTED: '기각됨' }[
                comparison.proposal.state
              ]
            }}
          </span>
          <span
            v-if="comparison.proposal.publication_status"
            class="status-chip"
          >
            {{
              comparison.proposal.publication_status === 'PUBLISHED'
                ? '검색 게시 완료'
                : '검색 게시 대기'
            }}
          </span>
        </div>
        <p class="detail-body whitespace-pre-wrap">
          {{ comparison.proposal.rationale }}
        </p>
        <p class="detail-label font-mono break-all">
          기준 개정본 · {{ comparison.proposal.base_revision }}
        </p>
        <h4 class="font-semibold text-sm">이번 사고의 근거</h4>
        <ul class="list-disc pl-5 text-sm space-y-2">
          <li
            v-for="(evidence, i) in comparison.proposal.evidence"
            :key="i"
            class="whitespace-pre-wrap break-words"
          >
            {{ evidence }}
          </li>
        </ul>
      </section>
      <section
        v-for="change in comparison.proposal.changes"
        :key="change.field"
        class="ops-panel p-4"
      >
        <h4 class="font-mono text-sm mb-3">{{ change.field }}</h4>
        <div class="grid gap-4 md:grid-cols-2">
          <div>
            <p class="detail-label mb-2">변경 전</p>
            <pre class="whitespace-pre-wrap break-words text-sm">{{
              display(change.before)
            }}</pre>
          </div>
          <div>
            <p class="detail-label text-info mb-2">제안 내용</p>
            <pre class="whitespace-pre-wrap break-words text-sm">{{
              display(change.after)
            }}</pre>
          </div>
        </div>
      </section>
      <details class="ops-panel p-4">
        <summary class="cursor-pointer text-sm font-semibold">
          고정된 전체 비교 원본
        </summary>
        <div class="grid gap-4 mt-4 md:grid-cols-2">
          <div>
            <p class="detail-label">기존 플레이북 전체</p>
            <pre class="mt-2 whitespace-pre-wrap break-all text-xs">{{
              display(comparison.proposal.before)
            }}</pre>
          </div>
          <div>
            <p class="detail-label">반영 후 플레이북 전체</p>
            <pre class="mt-2 whitespace-pre-wrap break-all text-xs">{{
              display(comparison.proposal.after)
            }}</pre>
          </div>
        </div>
      </details>
      <p
        v-if="error || comparison.proposal.publication_error"
        role="alert"
        class="text-sm text-error whitespace-pre-wrap"
      >
        {{ error || comparison.proposal.publication_error }}
      </p>
      <div class="border-t border-base-content/15 pt-4">
        <p class="detail-label mb-3">
          대응 지식만 반영합니다. 현재 사고의 런북 실행 승인은 별도입니다.
        </p>
        <div v-if="comparison.proposal.state === 'PENDING'" class="flex gap-3">
          <button
            class="btn btn-primary btn-sm"
            :disabled="pending"
            @click="$emit('apply')"
          >
            지식 반영
          </button>
          <button
            class="btn btn-outline btn-sm"
            :disabled="pending"
            @click="$emit('reject')"
          >
            제안 기각
          </button>
        </div>
        <button
          v-else-if="
            comparison.proposal.state === 'APPLIED' &&
            comparison.proposal.publication_status === 'PENDING'
          "
          class="btn btn-primary btn-sm"
          :disabled="pending"
          @click="$emit('apply')"
        >
          검색 게시 재시도
        </button>
      </div>
    </template>
  </div>
  <p v-else class="detail-empty">
    열람할 수 있는 생성 참고 자료 원문이 없습니다.
  </p>
</template>
