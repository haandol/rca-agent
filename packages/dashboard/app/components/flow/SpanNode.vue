<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import type { NodeData } from '~/composables/useTraceGraph';

defineProps<{ data: NodeData; selected?: boolean }>();

/**
 * State is carried by text and a semantic rail; selection has its own outline.
 */
const statusClass: Record<string, string> = {
  COMPLETED: 'span-node--success',
  FAILED: 'span-node--error',
  RUNNING: 'span-node--running',
  TIMED_OUT: 'span-node--error',
};

const statusLabel: Record<string, string> = {
  COMPLETED: '완료',
  FAILED: '실패',
  RUNNING: '진행 중',
  TIMED_OUT: '시간 초과',
};

const statusIcon: Record<string, string> = {
  COMPLETED: '✓',
  FAILED: '×',
  RUNNING: '●',
  TIMED_OUT: '!',
};

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}초`;
  return `${Math.round(ms / 60_000)}분`;
}
</script>

<template>
  <div
    class="span-node"
    :class="[statusClass[data.status], { 'is-selected': selected }]"
    :title="data.label"
  >
    <div class="span-node__meta">
      <span class="span-node__status">
        <span aria-hidden="true" class="span-node__icon">{{
          statusIcon[data.status] || '·'
        }}</span>
        {{ statusLabel[data.status] || data.status }}
      </span>
      <span v-if="data.durationMs != null" class="span-node__duration">
        {{ formatDuration(data.durationMs) }}
      </span>
    </div>
    <div class="span-node__label">{{ data.label }}</div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>

<style scoped>
.span-node {
  --node-tone: color-mix(in srgb, var(--color-base-content) 45%, transparent);
  box-sizing: border-box;
  width: 200px;
  height: 88px;
  padding: 12px 14px;
  border: 1px solid
    color-mix(in srgb, var(--node-tone) 55%, var(--color-base-100));
  border-left: 4px solid var(--node-tone);
  border-radius: 8px;
  background: var(--color-base-100);
  color: var(--color-base-content);
  cursor: pointer;
  transition:
    outline-color 150ms,
    background-color 150ms;
}
.span-node--success {
  --node-tone: var(--color-success);
}
.span-node--error {
  --node-tone: var(--color-error);
}
.span-node--running {
  --node-tone: var(--color-info);
}
.span-node--error,
.span-node--running {
  background: color-mix(in srgb, var(--node-tone) 8%, var(--color-base-100));
}
.span-node:hover {
  background: var(--color-base-300);
}
.span-node.is-selected {
  outline: 2px solid var(--color-primary);
  outline-offset: 3px;
}
.span-node__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.span-node__status {
  font-size: 11px;
  font-weight: 650;
}
.span-node__icon {
  color: var(--node-tone);
  margin-right: 3px;
}
.span-node__duration {
  font-family: var(--font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.span-node__label {
  margin-top: 7px;
  font-size: 14px;
  font-weight: 650;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
}
@media (prefers-reduced-motion: reduce) {
  .span-node {
    transition: none;
  }
}
</style>
