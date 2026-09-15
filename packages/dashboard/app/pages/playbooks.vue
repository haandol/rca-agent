<script setup lang="ts">
import type {
  LibraryItem,
  LibraryPage,
  LibraryDetail,
} from '../../shared/types/playbook-library';
const failureType = ref('');
const tag = ref('');
const verification = ref('');
const appliedFilters = ref({
  failure_type: '',
  tag: '',
  verification_status: '',
});
const { data, status, error, refresh } = await useFetch<LibraryPage>(
  '/api/playbook-library',
  { query: appliedFilters },
);
const additional = ref<LibraryItem[]>([]);
const cursor = ref<string | null>(null);
const paging = ref(false);
const pageError = ref('');
let generation = 0;
watch(
  data,
  (value) => {
    additional.value = [];
    cursor.value = value?.nextCursor ?? null;
  },
  { immediate: true },
);
const rows = computed(() => [
  ...new Map(
    [...(data.value?.items ?? []), ...additional.value].map((item) => [
      item.playbook_id,
      item,
    ]),
  ).values(),
]);
/** Start a new filtered listing and invalidate page results belonging to the previous filters. */
function filter() {
  generation++;
  additional.value = [];
  pageError.value = '';
  appliedFilters.value = {
    failure_type: failureType.value.trim(),
    tag: tag.value.trim(),
    verification_status: verification.value,
  };
}
/**
 * Append only a successful page from the current filter generation.
 * Preserve existing rows and the retry cursor when a page request fails.
 */
async function more() {
  if (!cursor.value || paging.value) return;
  const version = generation;
  paging.value = true;
  pageError.value = '';
  try {
    const page = await $fetch<LibraryPage>('/api/playbook-library', {
      query: { ...appliedFilters.value, cursor: cursor.value },
    });
    if (version !== generation) return;
    additional.value.push(...page.items);
    cursor.value = page.nextCursor;
  } catch {
    pageError.value = '다음 페이지를 읽지 못했습니다. 다시 시도하세요.';
  } finally {
    paging.value = false;
  }
}
const selected = ref<LibraryItem | null>(null);
const detail = ref<LibraryDetail | null>(null);
const detailPending = ref(false);
const detailError = ref(false);
/**
 * Inspect knowledge without leaving the library or clearing its filters.
 * A response for another selected playbook must not replace the open detail.
 */
async function open(item: LibraryItem) {
  selected.value = item;
  detail.value = null;
  detailError.value = false;
  detailPending.value = true;
  try {
    const result = await $fetch<LibraryDetail>(
      `/api/playbook-library/${encodeURIComponent(item.playbook_id)}`,
    );
    if (selected.value?.playbook_id === item.playbook_id) detail.value = result;
  } catch {
    if (selected.value?.playbook_id === item.playbook_id)
      detailError.value = true;
  } finally {
    if (selected.value?.playbook_id === item.playbook_id)
      detailPending.value = false;
  }
}
/** Keep SSR and browser timestamps in Seoul time, showing absent update times as missing. */
function date(value: string | null) {
  return value
    ? new Date(value).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })
    : '미제공';
}
useHead({ title: '플레이북 라이브러리 · RCA Control' });
</script>
<template>
  <div>
    <header class="mb-6">
      <p class="page-eyebrow">Playbook Library</p>
      <h1 class="page-title">플레이북 라이브러리</h1>
      <p class="page-description">
        장애 유형별 대응 지식과 연결된 사고를 찾아봅니다. 검증됨은 연결된 특정
        런북의 상태입니다.
      </p>
    </header>
    <form
      class="ops-panel flex flex-wrap gap-3 p-4 mb-5"
      @submit.prevent="filter"
    >
      <label class="text-xs flex-1 min-w-40"
        >장애 유형<input
          v-model="failureType"
          class="input input-bordered input-sm mt-1 w-full"
          placeholder="장애 유형 검색"
      /></label>
      <label class="text-xs flex-1 min-w-32"
        >태그<input
          v-model="tag"
          class="input input-bordered input-sm mt-1 w-full"
          placeholder="태그"
      /></label>
      <label class="text-xs"
        >런북 검증 상태<select
          v-model="verification"
          class="select select-bordered select-sm block mt-1"
        >
          <option value="">전체</option>
          <option value="DRAFT">초안</option>
          <option value="VERIFIED">검증됨</option>
        </select></label
      >
      <button class="btn btn-primary btn-sm self-end">검색</button>
    </form>
    <ReadState
      label="플레이북 목록"
      :pending="status === 'pending'"
      :error="error"
      @retry="refresh()"
    >
      <div class="space-y-3">
        <button
          v-for="item in rows"
          :key="item.playbook_id"
          class="ops-panel w-full p-4 text-left hover:border-primary/50 focus-visible:outline-2 focus-visible:outline-primary"
          @click="open(item)"
        >
          <div class="flex flex-wrap gap-3 items-center">
            <h2 class="font-semibold">
              {{ item.failure_type || '장애 유형 미제공' }}
            </h2>
            <span
              class="status-chip"
              :class="
                item.verification_status === 'VERIFIED'
                  ? 'text-success'
                  : 'text-warning'
              "
              >{{
                item.verification_status === 'VERIFIED'
                  ? '연결 런북 검증됨'
                  : '연결 런북 초안'
              }}</span
            >
            <span
              v-if="item.publication_status === 'PENDING'"
              class="status-chip text-warning"
              >검색 게시 대기</span
            >
            <span class="text-primary text-xs ml-auto">상세 보기</span>
          </div>
          <p class="text-sm mt-3 line-clamp-2">
            {{ item.symptom_pattern || '증상 미제공' }}
          </p>
          <p v-if="item.unavailable_reason" class="text-warning text-xs mt-2">
            {{ item.unavailable_reason }}
          </p>
          <div class="text-xs text-base-content/70 flex flex-wrap gap-3 mt-3">
            <span v-for="value in item.tags" :key="value">#{{ value }}</span>
            <span class="font-mono break-all">{{ item.playbook_id }}</span
            ><time>{{ date(item.updated_at) }}</time>
          </div>
        </button>
        <p v-if="!rows.length" class="detail-empty p-6">
          {{
            cursor
              ? '이 페이지에는 일치하는 플레이북이 없습니다. 다음 페이지를 확인하세요.'
              : '일치하는 플레이북이 없습니다.'
          }}
        </p>
      </div>
      <p v-if="pageError" role="alert" class="text-error text-sm mt-4">
        {{ pageError }}
      </p>
      <button
        v-if="cursor"
        class="btn btn-outline btn-sm mt-5"
        :disabled="paging"
        @click="more"
      >
        {{ paging ? '읽는 중' : '다음 페이지' }}
      </button>
    </ReadState>
    <DetailDialog
      :open="!!selected"
      title="플레이북 지식"
      @close="selected = null"
    >
      <ReadState
        label="플레이북 원문"
        :pending="detailPending"
        :error="detailError"
        @retry="selected && open(selected)"
      >
        <template v-if="detail">
          <p class="detail-label mb-4 break-all">
            연결된 사고 · {{ detail.item.source_rca_id }} ·
            {{ detail.item.engine }} · 개정본 {{ detail.item.revision }}
          </p>
          <p v-if="detail.item.unavailable_reason" class="text-warning text-sm">
            {{ detail.item.unavailable_reason }}
          </p>
          <template v-if="detail.playbook">
            <PlaybookKnowledge :playbook="detail.playbook" />
            <details class="ops-panel p-4 mt-5">
              <summary class="cursor-pointer font-semibold text-sm">
                연결된 사고의 런북 원문
              </summary>
              <p class="detail-label mt-3">
                이 기록의 명령은 해당 사고에만 해당하며 다른 사고의 실행을
                승인하지 않습니다.
              </p>
              <pre class="mt-4 whitespace-pre-wrap break-all text-xs">{{
                JSON.stringify(detail.playbook.execution_steps ?? [], null, 2)
              }}</pre>
            </details>
          </template>
        </template>
      </ReadState>
    </DetailDialog>
  </div>
</template>
