<script setup lang="ts">
import dagre from '@dagrejs/dagre';
import {
  VueFlow,
  Handle,
  Position,
  type Node,
  type Edge,
  MarkerType,
  type VueFlowStore,
} from '@vue-flow/core';
import GraphControls from './flow/GraphControls.vue';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import './flow/graph.css';
import {
  STATE_DESC,
  STATE_LABEL,
  TERMINAL_STATES,
  isTerminalState,
} from '~/utils/sessionState';

const props = defineProps<{
  currentState: string;
  engine: string;
}>();

const ANALYSIS_TRANSITIONS: Record<string, string[]> = {
  ALARM_RECEIVED: ['SCOPING'],
  SCOPING: ['HYPOTHESIS_GENERATION'],
  HYPOTHESIS_GENERATION: ['HYPOTHESIS_PRIORITIZATION'],
  HYPOTHESIS_PRIORITIZATION: ['EVIDENCE_COLLECTION'],
  EVIDENCE_COLLECTION: ['HYPOTHESIS_VALIDATION'],
  HYPOTHESIS_VALIDATION: [
    'REPORT_GENERATION',
    'HYPOTHESIS_PRIORITIZATION',
    'EVIDENCE_COLLECTION',
    'HYPOTHESIS_GENERATION',
  ],
  REPORT_GENERATION: ['COMPLETED'],
};

const ANALYSIS_HAPPY_PATH = [
  'ALARM_RECEIVED',
  'SCOPING',
  'HYPOTHESIS_GENERATION',
  'HYPOTHESIS_PRIORITIZATION',
  'EVIDENCE_COLLECTION',
  'HYPOTHESIS_VALIDATION',
  'REPORT_GENERATION',
  'COMPLETED',
];

const LEGACY_HEADLESS_HAPPY_PATH = ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'];

// 성공 종료(COMPLETED)는 해피 패스 안에서 이미 그려지므로, 그래프가 따로 배치하는
// 종료 상태는 중단 경로뿐이다. 노드 생성·레이아웃 제외·배치가 모두 같은 목록을 봐야
// 한 곳만 고쳤을 때 배치되지 않는 노드가 생기지 않는다.
const abortStates = TERMINAL_STATES.filter((s) => s !== 'COMPLETED');
const abortStateSet = new Set<string>(abortStates);

const happyPath = computed(() =>
  props.currentState === 'ANALYZING'
    ? LEGACY_HEADLESS_HAPPY_PATH
    : ANALYSIS_HAPPY_PATH,
);
const transitions = computed(() =>
  props.currentState === 'ANALYZING'
    ? { ALARM_RECEIVED: ['ANALYZING'], ANALYZING: ['COMPLETED'] }
    : ANALYSIS_TRANSITIONS,
);
const pipelineStates = computed(() =>
  happyPath.value.filter((s) => !isTerminalState(s)),
);

function isVisited(state: string): boolean {
  const currentIdx = happyPath.value.indexOf(props.currentState);
  const stateIdx = happyPath.value.indexOf(state);
  // An abort replaces the active stage. It cannot prove which earlier stages
  // ran, so only the recorded terminal state is known to have been reached.
  if (currentIdx < 0) return state === props.currentState;
  if (stateIdx < 0) return state === props.currentState;
  return stateIdx <= currentIdx;
}

const selectedState = ref<string | null>(null);
const flow = shallowRef<VueFlowStore>();

function stateTone(state: string): string {
  if (state === props.currentState) {
    if (state === 'FAILED' || state === 'CANCELLED') return 'error';
    if (state === 'OUTDATED') return 'neutral';
    if (state === 'COMPLETED') return 'success';
    return 'info';
  }
  return isVisited(state) ? 'success' : 'neutral';
}

function stateProgress(state: string): string {
  if (state === props.currentState)
    return isTerminalState(state) ? '현재 · 종료' : '현재 · 진행 중';
  if (abortStateSet.has(props.currentState) && happyPath.value.includes(state))
    return '도달 미확인';
  return isVisited(state) ? '완료' : '대기';
}

function onNodeClick(e: { node: { id: string } }) {
  selectedState.value = e.node.id;
}

const graph = computed(() => {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const states = happyPath.value;

  for (const state of states) {
    const isCurrent = state === props.currentState;
    const visited = isVisited(state);
    nodes.push({
      id: state,
      type: 'stateNode',
      position: { x: 0, y: 0 },
      data: {
        label: STATE_LABEL[state] || state,
        state,
        isCurrent,
        visited,
        isTerminal: isTerminalState(state),
        isSelected: false,
        step: states.indexOf(state) + 1,
      },
    });
  }

  for (const state of abortStates) {
    const isCurrent = state === props.currentState;
    nodes.push({
      id: state,
      type: 'stateNode',
      position: { x: 0, y: 0 },
      data: {
        label: STATE_LABEL[state] || state,
        state,
        isCurrent,
        visited: isCurrent,
        isTerminal: true,
        isSelected: false,
        step: null,
      },
    });
  }

  for (let i = 0; i < states.length - 1; i++) {
    const from = states[i]!;
    const to = states[i + 1]!;
    edges.push({
      id: `e-${from}-${to}`,
      source: from,
      target: to,
      markerEnd: MarkerType.ArrowClosed,
      style: {
        stroke: isVisited(to)
          ? 'var(--color-success)'
          : 'color-mix(in srgb, var(--color-base-content) 35%, var(--color-base-100))',
        strokeWidth: isVisited(to) ? 2.5 : 1.5,
      },
    });
  }

  const loopEdges: [string, string, string][] =
    props.currentState === 'ANALYZING'
      ? []
      : [
          ['HYPOTHESIS_VALIDATION', 'HYPOTHESIS_GENERATION', '재생성'],
          ['HYPOTHESIS_VALIDATION', 'HYPOTHESIS_PRIORITIZATION', '재우선순위'],
          ['HYPOTHESIS_VALIDATION', 'EVIDENCE_COLLECTION', '추가 증거'],
        ];
  for (const [from, to, label] of loopEdges) {
    edges.push({
      id: `e-loop-${from}-${to}`,
      source: from,
      target: to,
      sourceHandle: `${from}-left`,
      targetHandle: `${to}-left`,
      label,
      type: 'smoothstep',
      markerEnd: MarkerType.ArrowClosed,
      style: {
        stroke: 'var(--color-primary)',
        strokeWidth: 1.5,
        strokeDasharray: '6 4',
      },
      labelStyle: {
        fontSize: '12px',
        fontWeight: 600,
        fill: 'var(--color-base-content)',
      },
      labelBgStyle: { fill: 'var(--color-base-100)', fillOpacity: 1 },
      labelBgPadding: [7, 5],
      labelBgBorderRadius: 4,
    });
  }

  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: 'TB',
    ranksep: 24,
    nodesep: 48,
    marginx: 20,
    marginy: 20,
  });

  for (const node of nodes) {
    if (abortStateSet.has(node.id)) continue;
    g.setNode(node.id, { width: 184, height: 64 });
  }
  for (const edge of edges) {
    if (abortStateSet.has(edge.source) || abortStateSet.has(edge.target))
      continue;
    // Retry connections remain visible but do not stretch the main stage layout.
    if (edge.id.startsWith('e-loop-')) continue;
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  for (const node of nodes) {
    if (abortStateSet.has(node.id)) continue;
    const pos = g.node(node.id);
    if (pos) {
      node.position = { x: pos.x - 92, y: pos.y - 32 };
    }
  }

  const anchorNode = nodes.find(
    (n) => n.id === states[Math.max(0, states.length - 4)],
  );
  const baseX = anchorNode ? anchorNode.position.x + 248 : 250;
  const baseY = anchorNode ? anchorNode.position.y : 300;
  for (let i = 0; i < abortStates.length; i++) {
    const node = nodes.find((n) => n.id === abortStates[i]);
    if (node) {
      node.position = { x: baseX, y: baseY + i * 88 };
    }
  }

  return { nodes, edges };
});

const graphNodes = computed(() => {
  return graph.value.nodes.map((n) => ({
    ...n,
    data: { ...n.data, isSelected: n.id === selectedState.value },
  }));
});
</script>

<template>
  <div class="grid gap-4">
    <div class="rca-graph">
      <div class="rca-graph__toolbar">
        <div>
          <p class="text-sm font-semibold">분석 파이프라인</p>
          <p class="text-xs text-base-content/75 mt-1">
            현재 상태 · {{ STATE_LABEL[currentState] || currentState }}
          </p>
        </div>
        <GraphControls :flow="flow" />
      </div>
      <div class="state-graph-canvas">
        <VueFlow
          :nodes="graphNodes"
          :edges="graph.edges"
          :default-viewport="{ zoom: 0.95, x: 20, y: 10 }"
          :min-zoom="0.5"
          :max-zoom="1.5"
          :pan-on-drag="true"
          :zoom-on-scroll="false"
          @init="
            flow = $event;
            flow.fitView({ padding: 0.2 });
          "
          @nodes-initialized="flow?.fitView({ padding: 0.2 })"
          @node-click="onNodeClick"
        >
          <template #node-stateNode="{ data }">
            <div
              class="state-node"
              :class="[
                `state-node--${stateTone(data.state)}`,
                {
                  'is-selected': data.isSelected,
                  'is-current': data.isCurrent,
                },
              ]"
              :title="STATE_DESC[data.state]"
            >
              <div class="state-node__meta">
                <span class="font-mono">{{
                  data.step ? String(data.step).padStart(2, '0') : '종료'
                }}</span>
                <span class="font-semibold">{{
                  stateProgress(data.state)
                }}</span>
              </div>
              <div class="state-node__label">{{ data.label }}</div>
            </div>
            <Handle
              type="target"
              :position="Position.Top"
              class="!bg-transparent !border-0 !w-0 !h-0"
            />
            <Handle
              :id="`${data.state}-left`"
              type="target"
              :position="Position.Left"
              class="!bg-transparent !border-0 !w-0 !h-0"
            />
            <Handle
              type="source"
              :position="Position.Bottom"
              class="!bg-transparent !border-0 !w-0 !h-0"
            />
            <Handle
              :id="`${data.state}-left`"
              type="source"
              :position="Position.Left"
              class="!bg-transparent !border-0 !w-0 !h-0"
            />
          </template>
        </VueFlow>
      </div>
      <div class="rca-graph__legend" aria-label="상태 전이 범례">
        <span><i class="text-info" aria-hidden="true">●</i>진행 중</span>
        <span><i class="text-success" aria-hidden="true">✓</i>완료</span>
        <span><i class="text-error" aria-hidden="true">×</i>실패 · 중단</span>
        <span><i aria-hidden="true">○</i>대기 · 스킵 · 도달 미확인</span>
        <span
          ><i
            class="graph-line graph-line--loop text-primary"
            aria-hidden="true"
          />재검토 경로</span
        >
      </div>
    </div>

    <div
      class="rounded-lg border border-base-content/15 bg-base-100 p-4"
      aria-live="polite"
    >
      <template v-if="selectedState">
        <h4 class="font-bold text-sm">
          {{ STATE_LABEL[selectedState] || selectedState }}
        </h4>
        <span
          class="state-detail-status mt-2"
          :class="`state-node--${stateTone(selectedState)}`"
        >
          {{ stateProgress(selectedState) }}
        </span>
        <p class="text-sm text-base-content/80 leading-relaxed mt-3">
          {{ STATE_DESC[selectedState] || '' }}
        </p>
        <div v-if="transitions[selectedState]" class="mt-3">
          <div class="text-xs font-semibold mb-2">전이 가능</div>
          <div class="flex flex-wrap gap-1">
            <button
              v-for="t in transitions[selectedState]"
              :key="t"
              type="button"
              class="state-transition"
              @click="selectedState = t"
            >
              {{ STATE_LABEL[t] || t }}
            </button>
          </div>
        </div>
        <div v-if="pipelineStates.includes(selectedState)" class="mt-2">
          <div class="text-xs font-semibold mb-2">중단 전이</div>
          <div class="flex flex-wrap gap-1">
            <span
              v-for="t in abortStates"
              :key="t"
              class="text-xs border border-base-content/20 rounded-md px-2 py-1"
              >{{ STATE_LABEL[t] }}</span
            >
          </div>
        </div>
      </template>
      <template v-else>
        <div class="flex items-center text-base-content/80 gap-3">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            class="size-6"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122"
            />
          </svg>
          <p class="text-sm">
            노드를 선택하면 상태 설명과 전이 가능한 상태를 확인할 수 있습니다.
          </p>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.state-graph-canvas {
  height: 640px;
}
.state-node {
  --node-tone: color-mix(
    in srgb,
    var(--color-base-content) 40%,
    var(--color-base-100)
  );
  box-sizing: border-box;
  width: 184px;
  height: 64px;
  padding: 9px 12px;
  border: 1px solid var(--node-tone);
  border-left: 4px solid var(--node-tone);
  border-radius: 8px;
  background: var(--color-base-100);
  color: var(--color-base-content);
  cursor: pointer;
  transition: background-color 150ms;
}
.state-node--info {
  --node-tone: var(--color-info);
}
.state-node--success {
  --node-tone: var(--color-success);
}
.state-node--error {
  --node-tone: var(--color-error);
}
.state-node--neutral {
  --node-tone: color-mix(
    in srgb,
    var(--color-base-content) 40%,
    var(--color-base-100)
  );
}
.state-node.is-current {
  background: color-mix(in srgb, var(--node-tone) 12%, var(--color-base-100));
  border-width: 2px;
  border-left-width: 4px;
}
.state-node.is-selected {
  outline: 2px solid var(--color-primary);
  outline-offset: 3px;
}
.state-node:hover {
  background: var(--color-base-300);
}
.state-node__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 11px;
}
.state-node__label {
  margin-top: 5px;
  font-size: 14px;
  line-height: 1.35;
  font-weight: 650;
}
.state-detail-status {
  display: inline-flex;
  border: 1px solid var(--node-tone);
  border-radius: 999px;
  padding: 3px 9px;
  font-size: 12px;
}
.state-transition {
  min-height: 32px;
  padding: 4px 10px;
  border: 1px solid
    color-mix(in srgb, var(--color-base-content) 22%, transparent);
  border-radius: 6px;
  background: var(--color-base-200);
  font-size: 12px;
  cursor: pointer;
}
.state-transition:hover {
  background: var(--color-base-300);
}
.state-transition:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}
@media (max-width: 640px) {
  .state-graph-canvas {
    height: 560px;
  }
}
</style>
