<script setup lang="ts">
const props = defineProps<{ references?: unknown; artifacts?: unknown }>();
/** Display only recorded reference strings; do not turn missing sources into evidence of absence. */
const references = computed(() =>
  Array.isArray(props.references)
    ? props.references.filter((ref): ref is string => typeof ref === 'string')
    : [],
);
/** Join by the recorded source reference, retaining distinct versions instead of choosing a current source. */
function sources(ref: string): Record<string, any>[] {
  if (!Array.isArray(props.artifacts)) return [];
  const matches = props.artifacts.filter(
    (source) =>
      source && typeof source === 'object' && source.source_ref === ref,
  );
  return [
    ...new Map(
      matches.map((source) => [
        JSON.stringify([
          source.source_ref,
          source.path,
          source.sha256,
          source.text,
        ]),
        source,
      ]),
    ).values(),
  ];
}
</script>
<template>
  <section class="mt-3 text-sm" data-testid="analysis-evidence-refs">
    <p class="detail-label">증거 참조</p>
    <p v-if="!references.length" class="text-base-content/65">
      연결된 증거 참조가 제공되지 않았습니다.
    </p>
    <ul v-else class="space-y-2 mt-2">
      <li v-for="(ref, index) in references" :key="index">
        <p class="font-mono break-all">{{ ref }}</p>
        <p v-if="!sources(ref).length" class="text-base-content/65">
          이 응답에는 연결된 원본 본문이 없습니다. 자료 부재를 검사 부재로
          판단하지 않습니다.
        </p>
        <div
          v-for="(source, sourceIndex) in sources(ref)"
          :key="sourceIndex"
          class="mt-2"
        >
          <p class="font-mono break-all">{{ source.path || '경로 미제공' }}</p>
          <p v-if="source.repository" class="break-all">
            저장소: {{ source.repository }}
          </p>
          <p v-if="source.revision || source.base_revision" class="break-all">
            기록된 비교 기준: {{ source.revision || source.base_revision }}
          </p>
          <p v-if="source.source_kind">자료 종류: {{ source.source_kind }}</p>
          <p v-if="source.source_phase">관측 구간: {{ source.source_phase }}</p>
          <p v-if="source.sha256" class="font-mono break-all">
            기록된 SHA-256: {{ source.sha256 }}
          </p>
          <details v-if="typeof source.text === 'string'" class="mt-2">
            <summary>참조한 원본 코드·설정</summary>
            <pre
              class="overflow-auto whitespace-pre-wrap text-xs"
              tabindex="0"
              >{{ source.text }}</pre>
          </details>
        </div>
      </li>
    </ul>
  </section>
</template>
