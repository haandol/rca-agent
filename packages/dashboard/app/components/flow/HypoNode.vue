<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import type { NodeData } from '~/composables/useTraceGraph';

const props = defineProps<{ data: NodeData; selected?: boolean }>();

/**
 * A hypothesis in the graph, read by its verdict first.
 *
 * Rejected hypotheses stay neutral: discarding a hypothesis is not a failed step.
 */
const statusClass: Record<string, string> = {
  CONFIRMED: 'hypo-node--confirmed',
  REJECTED: 'hypo-node--neutral',
  CLOSED: 'hypo-node--neutral',
  NEEDS_INVESTIGATION: 'hypo-node--investigate',
  PENDING: 'hypo-node--neutral',
};

const statusLabel: Record<string, string> = {
  CONFIRMED: '채택',
  REJECTED: '기각',
  CLOSED: '검증 못 함',
  NEEDS_INVESTIGATION: '추가 조사',
  PENDING: '검증 안 됨',
};

const confidence = computed(() =>
  props.data.confidenceScore === undefined
    ? null
    : Math.round(props.data.confidenceScore * 100),
);
</script>

<template>
  <div
    class="hypo-node"
    :class="[statusClass[data.status], { 'is-selected': selected }]"
    :title="data.title || data.description || data.label"
  >
    <div class="hypo-node__meta">
      <span class="hypo-node__status">
        <span aria-hidden="true" class="hypo-node__icon">{{
          data.status === 'CONFIRMED'
            ? '✓'
            : data.status === 'NEEDS_INVESTIGATION'
              ? '!'
              : '◇'
        }}</span>
        {{ statusLabel[data.status] || data.status }}
      </span>
      <span v-if="confidence !== null" class="hypo-node__confidence">
        신뢰도 {{ confidence }}%
      </span>
    </div>
    <div class="hypo-node__label">
      {{ data.label }}
    </div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>

<style scoped>
.hypo-node {
  --node-tone: color-mix(in srgb, var(--color-base-content) 45%, transparent);
  box-sizing: border-box;
  width: 260px;
  height: 112px;
  padding: 13px 15px;
  border: 1px solid
    color-mix(in srgb, var(--node-tone) 55%, var(--color-base-100));
  border-top: 4px solid var(--node-tone);
  border-radius: 8px;
  background: var(--color-base-100);
  color: var(--color-base-content);
  cursor: pointer;
  transition:
    outline-color 150ms,
    background-color 150ms;
}
.hypo-node--confirmed {
  --node-tone: var(--color-success);
}
.hypo-node--investigate {
  --node-tone: var(--color-warning);
}
.hypo-node--confirmed,
.hypo-node--investigate {
  background: color-mix(in srgb, var(--node-tone) 7%, var(--color-base-100));
}
.hypo-node:hover {
  background: var(--color-base-300);
}
.hypo-node.is-selected {
  outline: 2px solid var(--color-primary);
  outline-offset: 3px;
}
.hypo-node__meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hypo-node__status {
  font-size: 11px;
  font-weight: 650;
}
.hypo-node__icon {
  color: var(--node-tone);
  margin-right: 3px;
}
.hypo-node__confidence {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.hypo-node__label {
  margin-top: 9px;
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 550;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
}
@media (prefers-reduced-motion: reduce) {
  .hypo-node {
    transition: none;
  }
}
</style>
