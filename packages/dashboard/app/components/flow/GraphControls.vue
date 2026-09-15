<script setup lang="ts">
import type { VueFlowStore } from '@vue-flow/core';

defineProps<{ flow?: VueFlowStore }>();
</script>

<template>
  <div class="graph-controls" role="group" aria-label="그래프 배율">
    <button
      type="button"
      :disabled="!flow"
      aria-label="그래프 축소"
      title="축소"
      @click="flow?.zoomOut()"
    >
      −
    </button>
    <button
      type="button"
      :disabled="!flow"
      aria-label="그래프 확대"
      title="확대"
      @click="flow?.zoomIn()"
    >
      +
    </button>
    <button
      type="button"
      :disabled="!flow"
      @click="flow?.fitView({ padding: 0.15 })"
    >
      전체 보기
    </button>
  </div>
</template>

<style scoped>
.graph-controls {
  display: flex;
  align-items: center;
  gap: 4px;
}
button {
  min-width: 32px;
  min-height: 32px;
  padding: 4px 9px;
  border: 1px solid
    color-mix(in srgb, var(--color-base-content) 18%, transparent);
  border-radius: 6px;
  background: var(--color-base-100);
  color: var(--color-base-content);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
button:hover {
  background: var(--color-base-300);
}
button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
