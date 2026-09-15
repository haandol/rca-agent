<script setup lang="ts">
import {
  VueFlow,
  type Node,
  type Edge,
  type VueFlowStore,
} from '@vue-flow/core';
import type { NodeData } from '~/composables/useTraceGraph';
import SpanNode from './flow/SpanNode.vue';
import HypoNode from './flow/HypoNode.vue';
import GraphControls from './flow/GraphControls.vue';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import './flow/graph.css';
import { STATE_LABEL, isTerminalState } from '~/utils/sessionState';

defineProps<{
  nodes: Node<NodeData>[];
  edges: Edge[];
  currentState?: string;
}>();
const emit = defineEmits<{
  nodeClick: [event: { node: { data: NodeData } }];
}>();
const flow = shallowRef<VueFlowStore>();
</script>

<template>
  <div class="rca-graph flex-1">
    <div class="rca-graph__toolbar">
      <div>
        <p class="text-sm font-semibold">분석 단계와 가설</p>
        <p v-if="currentState" class="mt-2">
          <span
            class="status-chip"
            :class="
              !isTerminalState(currentState)
                ? 'text-info'
                : currentState === 'COMPLETED'
                  ? 'text-success'
                  : ['FAILED', 'CANCELLED'].includes(currentState)
                    ? 'text-error'
                    : 'text-base-content/80'
            "
          >
            현재 단계 · {{ STATE_LABEL[currentState] ?? currentState }}
          </span>
        </p>
        <p class="text-xs text-base-content/75 mt-1">
          노드를 선택해 입출력 확인 · 드래그로 이동
        </p>
      </div>
      <GraphControls :flow="flow" />
    </div>
    <div v-if="nodes.length" class="trace-graph__canvas">
      <VueFlow
        :nodes="nodes"
        :edges="edges"
        :default-viewport="{ zoom: 0.85, x: 40, y: 20 }"
        fit-view-on-init
        :min-zoom="0.3"
        :max-zoom="2"
        @init="flow = $event"
        @node-click="emit('nodeClick', $event)"
      >
        <template #node-spanNode="props"
          ><SpanNode :data="props.data" :selected="props.selected"
        /></template>
        <template #node-hypoNode="props"
          ><HypoNode :data="props.data" :selected="props.selected"
        /></template>
      </VueFlow>
    </div>
    <p v-else class="px-5 py-12 text-center text-sm text-base-content/80">
      그래프로 표시할 분석 단계나 가설이 없습니다.
    </p>
    <div class="rca-graph__legend" aria-label="그래프 범례">
      <span><i class="text-info" aria-hidden="true">●</i>진행 중</span>
      <span><i class="text-success" aria-hidden="true">✓</i>완료 · 채택</span>
      <span
        ><i class="text-error" aria-hidden="true">×</i>실패 · 시간 초과</span
      >
      <span><i class="text-warning" aria-hidden="true">!</i>추가 조사</span>
      <span><i aria-hidden="true">◇</i>기각 · 검증 못 함 · 검증 안 됨</span>
      <span><i class="graph-line" aria-hidden="true" />단계 · 가설 연결</span>
      <span
        ><i class="graph-line graph-line--loop" aria-hidden="true" />생성된
        가설</span
      >
    </div>
  </div>
</template>

<style scoped>
.trace-graph__canvas {
  height: 600px;
}
@media (max-width: 640px) {
  .trace-graph__canvas {
    height: 480px;
  }
}
</style>
