<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import type { NodeData } from '~/composables/useTraceGraph';

const props = defineProps<{ data: NodeData }>();

/**
 * A hypothesis in the graph, read by its verdict first.
 *
 * Only the confirmed one earns ink: it is the branch the report's chain came
 * from. What was rejected recedes rather than being coloured as an error — the
 * search discarding a hypothesis is the search working, not something failing.
 */
const statusClass: Record<string, string> = {
  CONFIRMED: 'border-primary/55 bg-primary/[0.06]',
  REJECTED: 'border-base-content/12 bg-base-100 opacity-55',
  CLOSED: 'border-base-content/12 bg-base-100 opacity-65',
  NEEDS_INVESTIGATION: 'border-base-content/30 bg-base-200',
  PENDING: 'border-base-content/15 bg-base-100',
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
    class="rounded-box border px-3.5 py-2.5 w-[212px] cursor-pointer transition-colors hover:border-base-content/35"
    :class="statusClass[data.status] || 'border-base-content/15 bg-base-100'"
  >
    <div class="flex items-baseline gap-2">
      <span
        class="text-[10px] font-medium"
        :class="
          data.status === 'CONFIRMED'
            ? 'text-primary'
            : data.status === 'NEEDS_INVESTIGATION'
              ? 'text-base-content/78'
              : 'text-base-content/65'
        "
      >
        {{ statusLabel[data.status] || data.status }}
      </span>
      <span
        v-if="confidence !== null"
        class="ml-auto font-mono text-[10px] tabular-nums text-base-content/65"
      >
        {{ confidence }}%
      </span>
    </div>
    <div class="font-serif text-[12px] leading-snug line-clamp-2 mt-1.5">
      {{ data.label }}
    </div>
  </div>
  <Handle type="target" :position="Position.Top" />
  <Handle type="source" :position="Position.Bottom" />
</template>
