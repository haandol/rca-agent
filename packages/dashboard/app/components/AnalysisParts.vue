<script setup lang="ts">
import { renderMarkdownDocument } from '~/utils/markdown';
import { analysisPartInterrupted } from '~/utils/analysisParts';
const props = defineProps<{
  parts: Record<string, any>[];
  approvalEligible?: boolean;
  parentState?: string;
  historicalView?: boolean;
  summaryOnly?: boolean;
  selectedPart?: string;
}>();
defineEmits<{ review: []; retry: []; detail: [part: string] }>();
/** Optional lists remain absent when malformed; never invent successful results. */
function list(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}
/** Reuse only source snapshots already returned with these same incident parts; never fetch a latest revision. */
const artifacts = computed(() =>
  props.parts.flatMap((part) =>
    part.available
      ? [
          ...list(part.payload?.result?.source_artifacts),
          ...list(part.payload?.result?.control_artifacts),
        ]
      : [],
  ),
);
/** Preserve the recorded test status; absence is neither NOT_RUN nor a passing test. */
function testStatus(value: unknown): string {
  return (
    (
      {
        NOT_RUN: '미실행 (NOT_RUN)',
        PASSED: '통과 기록 (PASSED)',
        FAILED: '실패 기록 (FAILED)',
      } as Record<string, string>
    )[String(value)] ?? '상태 미제공'
  );
}
const visibleParts = computed(() =>
  props.selectedPart
    ? props.parts.filter((part) => part.part === props.selectedPart)
    : props.parts,
);
const names: Record<string, string> = {
  recovery: '1. 빠른 정상화',
  root_cause: '2. 근본원인 · 코드 PR',
  operations: '3. 운영 개선 · 예방',
};
const labels: Record<string, string> = {
  WAITING: '대기',
  RUNNING: '분석 중',
  COMPLETED: '완료',
  FAILED: '실패',
  SKIPPED: '건너뜀',
};
</script>
<template>
  <div class="space-y-5" data-testid="analysis-parts">
    <section
      v-for="part in visibleParts"
      :key="part.part"
      :data-part="part.part"
      class="ops-panel p-5"
    >
      <div class="flex flex-wrap items-center gap-3 mb-3">
        <h2 class="detail-section-title">{{ names[part.part] }}</h2>
        <span class="status-chip">{{
          historicalView
            ? `이전 엔진의 마지막 기록: ${part.status}`
            : analysisPartInterrupted(parentState ?? '', part.status)
              ? part.status === 'RUNNING'
                ? '작업 중단'
                : '시작 기록 없음'
              : labels[part.status] || '상태 미확인'
        }}</span>
        <span v-if="part.part === 'recovery'" class="text-sm">{{
          part.approval_status === 'READY' && approvalEligible
            ? '승인 검토 가능'
            : part.approval_status === 'REVOKED'
              ? '제안 철회'
              : '승인 불가'
        }}</span>
      </div>
      <p
        v-if="
          !historicalView &&
          analysisPartInterrupted(parentState ?? '', part.status)
        "
        class="mt-3 text-sm"
        data-testid="interrupted-part"
      >
        부모 분석이 {{ parentState }}로 종료됐습니다. 마지막 저장 상태는
        {{ part.status }}이며 새 완료 결과는 기록되지 않았습니다.
      </p>
      <p class="whitespace-pre-wrap">{{ part.summary }}</p>
      <p v-if="part.error" role="alert" class="text-error mt-3">
        {{ part.error }}
        <button class="btn btn-sm btn-outline" @click="$emit('retry')">
          다시 조회
        </button>
      </p>
      <div v-if="summaryOnly" class="flex flex-wrap gap-2 mt-3">
        <button
          class="btn btn-outline btn-sm"
          @click="$emit('detail', part.part)"
        >
          {{ names[part.part] }} · 근거와 상세
        </button>
        <button
          v-if="
            part.part === 'recovery' &&
            part.approval_status === 'READY' &&
            approvalEligible
          "
          class="btn btn-primary btn-sm"
          @click="$emit('review')"
        >
          전체 런북 검토 · 승인
        </button>
      </div>
      <template v-if="!summaryOnly && part.available && part.payload">
        <p class="detail-body mt-3">{{ part.payload.result.title }}</p>
        <p v-if="part.payload.result.reason" class="mt-3 whitespace-pre-wrap">
          {{ part.payload.result.reason }}
        </p>
        <template v-if="part.part === 'recovery'">
          <p class="mt-3">
            임시 정상화 제안이며 근본원인 확정이나 복구 성공을 뜻하지 않습니다.
          </p>
          <button
            v-if="part.approval_status === 'READY' && approvalEligible"
            class="btn btn-primary btn-sm mt-3"
            @click="$emit('review')"
          >
            전체 런북 검토 · 승인
          </button>
        </template>
        <template v-if="part.part === 'root_cause'">
          <p class="mt-3">
            원인
            {{
              part.payload.result.root_cause?.confirmed === true
                ? '확정'
                : '미확정'
            }}
            · {{ part.payload.result.root_cause?.description }}
          </p>
          <div
            v-if="part.payload.result.report_markdown"
            class="prose-report mt-4"
            v-html="renderMarkdownDocument(part.payload.result.report_markdown)"
          />
          <section v-if="part.payload.result.code_proposal" class="mt-4">
            <h3 class="detail-section-title">
              코드 PR 미리보기 · {{ part.payload.result.code_proposal.status }}
            </h3>
            <p>제안이며 실제 게시·적용·머지 상태가 아닙니다.</p>
            <p>{{ part.payload.result.code_proposal.title }}</p>
            <p class="break-all">
              저장소:
              {{ part.payload.result.code_proposal.repository || '미제공' }} ·
              비교 기준:
              {{ part.payload.result.code_proposal.base_revision || '미제공' }}
            </p>
            <p>
              기록된 테스트 상태:
              {{ testStatus(part.payload.result.code_proposal.tests_status) }}
            </p>
            <h4 class="detail-label mt-3">테스트 계획</h4>
            <ul>
              <li
                v-for="(plan, index) in list(
                  part.payload.result.code_proposal.test_plan,
                )"
                :key="index"
              >
                {{ plan }}
              </li>
            </ul>
            <p v-if="!list(part.payload.result.code_proposal.test_plan).length">
              테스트 계획이 제공되지 않았습니다.
            </p>
            <ul class="mt-3" data-testid="code-proposal-limitations">
              <li
                v-for="(limit, index) in list(
                  part.payload.result.code_proposal.limitations,
                )"
                :key="index"
              >
                {{ limit }}
              </li>
            </ul>
            <p
              v-if="!list(part.payload.result.code_proposal.files).length"
              class="mt-3"
            >
              구체 코드 수정 파일이 제공되지 않았습니다.
            </p>
            <div
              v-for="(file, index) in list(
                part.payload.result.code_proposal.files,
              )"
              :key="index"
              class="mt-3"
            >
              <p class="font-mono break-all">
                {{ file.path }}:{{ file.start_line }}–{{ file.end_line }}
              </p>
              <p class="detail-label mt-3">기록된 원본 코드</p>
              <pre
                v-if="typeof file.original === 'string'"
                class="overflow-auto whitespace-pre-wrap text-xs"
                data-testid="proposal-original"
                tabindex="0"
                >{{ file.original }}</pre>
              <p v-else>원본 코드가 제공되지 않았습니다.</p>
              <p class="detail-label mt-3">제안 코드</p>
              <pre
                v-if="typeof file.proposed === 'string'"
                class="overflow-auto whitespace-pre-wrap text-xs"
                data-testid="proposal-proposed"
                tabindex="0"
                >{{ file.proposed }}</pre>
              <p v-else>제안 코드가 제공되지 않았습니다.</p>
              <p class="detail-label mt-3">기록된 수정 diff</p>
              <pre
                v-if="typeof file.unified_diff === 'string'"
                class="overflow-auto whitespace-pre-wrap text-xs"
                data-testid="proposal-diff"
                tabindex="0"
                >{{ file.unified_diff }}</pre>
              <p v-else>수정 diff가 제공되지 않았습니다.</p>
              <AnalysisEvidenceRefs
                :references="file.evidence_refs"
                :artifacts="artifacts"
              />
            </div>
          </section>
        </template>
        <template v-if="part.part === 'operations'">
          <p class="mt-3">개선 제안은 실제 운영 통제 적용과 구분합니다.</p>
          <div
            v-for="(finding, index) in list(part.payload.result.findings)"
            :key="index"
            class="mt-3"
          >
            <span>{{
              finding.status === 'OBSERVED'
                ? '관측 기록 (OBSERVED)'
                : finding.status === 'UNVERIFIED'
                  ? '미검증 (UNVERIFIED)'
                  : '관측 상태 미제공'
            }}</span>
            · {{ finding.statement }}
            <AnalysisEvidenceRefs
              :references="finding.evidence_refs"
              :artifacts="artifacts"
            />
          </div>
          <div
            v-for="(proposal, index) in list(
              part.payload.result.recommendations,
            )"
            :key="index"
            class="mt-4 border-t border-base-content/15 pt-3"
          >
            <h3>{{ proposal.title }}</h3>
            <p>{{ proposal.description }}</p>
            <p>
              단계: {{ proposal.stage }} · 우선순위: {{ proposal.priority }} ·
              담당: {{ proposal.owner || '미지정' }}
            </p>
            <p>검사: {{ proposal.check }}</p>
            <p>차단 조건: {{ proposal.failure_condition }}</p>
            <p>검증 계획: {{ proposal.verification_plan }}</p>
            <p>
              기록된 검증 상태: {{ testStatus(proposal.validation_status) }}
            </p>
            <AnalysisEvidenceRefs
              :references="proposal.evidence_refs"
              :artifacts="artifacts"
            />
          </div>
        </template>
        <ul class="mt-3">
          <li
            v-for="(limit, index) in [
              ...list(part.payload.limitations),
              ...list(part.payload.result.limitations),
            ]"
            :key="index"
          >
            {{ limit }}
          </li>
        </ul>
        <details class="mt-4">
          <summary>원본 결과 · 증거 참조</summary>
          <pre class="overflow-auto whitespace-pre-wrap text-xs">{{
            JSON.stringify(part.payload, null, 2)
          }}</pre>
        </details>
      </template>
      <p v-else-if="!part.error && !part.available" class="mt-3 text-sm">
        {{
          historicalView
            ? '인계 전 기록입니다. 현재 담당 엔진의 진행 상황은 위 이동 링크에서 확인하세요.'
            : analysisPartInterrupted(parentState ?? '', part.status)
              ? '중단 전 기록과 남아 있는 증거를 보존합니다.'
              : part.status === 'RUNNING'
                ? '분석 결과를 준비하고 있습니다.'
                : '아직 공개된 원본이 없습니다.'
        }}
      </p>
    </section>
  </div>
</template>
