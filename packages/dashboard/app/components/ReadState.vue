<script setup lang="ts">
defineProps<{ pending?: boolean; error?: unknown; label: string }>();
defineEmits<{ retry: [] }>();
</script>
<template>
  <div v-if="pending" role="status" class="py-6 text-sm text-base-content/75">
    <span class="loading loading-spinner loading-sm mr-2" />{{ label }} 읽는 중
  </div>
  <div
    v-else-if="error"
    role="alert"
    class="rounded-lg border border-error/30 bg-error/5 p-4"
  >
    <p class="text-sm text-error">{{ label }} 불러오지 못했습니다.</p>
    <button class="btn btn-sm btn-outline mt-3" @click="$emit('retry')">
      다시 불러오기
    </button>
  </div>
  <slot v-else />
</template>
