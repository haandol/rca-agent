<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import type { NodeData } from '~/composables/useTraceGraph';

defineProps<{ data: NodeData }>();

/**
 * A pipeline step. Only failure and still-running earn a colour — a completed
 * step is the expected case, and colouring every one leaves nothing for the two
 * that need finding.
 */
const statusClass: Record<string, string> = {
  COMPLETED: 'border-base-content/15 bg-base-100',
  FAILED: 'border-base-content/70 bg-base-200',
  RUNNING: 'border-primary/55 bg-primary/[0.06]',
  TIMED_OUT: 'border-base-content/12 bg-base-200',
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
    class="rounded-box border px-3.5 py-2 w-[152px] cursor-pointer transition-colors hover:border-base-content/35"
    :class="statusClass[data.status] || 'border-base-content/15 bg-base-100'"
  >
    <div class="flex items-center gap-1.5">
      <span
        v-if="data.status === 'RUNNING'"
        class="size-[6px] rounded-full bg-primary shrink-0 animate-ember"
      />
      <!-- The glyph is the only thing marking a failed span, and the palette has
           no red to reinforce it, so it carries a label rather than aria-hidden.
           Hidden, a screen reader heard this node exactly as a completed one. -->
      <span
        v-else-if="data.status === 'FAILED'"
        class="text-[11px] leading-none shrink-0"
        role="img"
        aria-label="실패"
        >✕</span
      >
      <span
        class="text-[11.5px] font-medium truncate"
        :class="data.status === 'FAILED' ? 'mark-broken' : ''"
        >{{ data.label }}</span
      >
    </div>
    <div
      v-if="data.durationMs"
      class="font-mono text-[10px] text-base-content/62 mt-1 tabular-nums"
    >
      {{ formatDuration(data.durationMs) }}
    </div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>
