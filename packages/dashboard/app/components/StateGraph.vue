<script setup lang="ts">
import dagre from '@dagrejs/dagre';
import {
  VueFlow,
  Handle,
  Position,
  type Node,
  type Edge,
  MarkerType,
} from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
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

const STRANDS_TRANSITIONS: Record<string, string[]> = {
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

const CC_HEADLESS_TRANSITIONS: Record<string, string[]> = {
  ALARM_RECEIVED: ['ANALYZING'],
  ANALYZING: ['COMPLETED'],
};

const STRANDS_HAPPY_PATH = [
  'ALARM_RECEIVED',
  'SCOPING',
  'HYPOTHESIS_GENERATION',
  'HYPOTHESIS_PRIORITIZATION',
  'EVIDENCE_COLLECTION',
  'HYPOTHESIS_VALIDATION',
  'REPORT_GENERATION',
  'COMPLETED',
];

const CC_HEADLESS_HAPPY_PATH = ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'];

// 성공 종료(COMPLETED)는 해피 패스 안에서 이미 그려지므로, 그래프가 따로 배치하는
// 종료 상태는 중단 경로뿐이다. 노드 생성·레이아웃 제외·배치가 모두 같은 목록을 봐야
// 한 곳만 고쳤을 때 배치되지 않는 노드가 생기지 않는다.
const abortStates = TERMINAL_STATES.filter((s) => s !== 'COMPLETED');
const abortStateSet = new Set<string>(abortStates);

const happyPath = computed(() =>
  props.engine === 'cc-headless' ? CC_HEADLESS_HAPPY_PATH : STRANDS_HAPPY_PATH,
);
const transitions = computed(() =>
  props.engine === 'cc-headless'
    ? CC_HEADLESS_TRANSITIONS
    : STRANDS_TRANSITIONS,
);
const pipelineStates = computed(() =>
  happyPath.value.filter((s) => !isTerminalState(s)),
);

function isVisited(state: string): boolean {
  const currentIdx = happyPath.value.indexOf(props.currentState);
  const stateIdx = happyPath.value.indexOf(state);
  if (currentIdx < 0) return isTerminalState(props.currentState);
  if (stateIdx < 0) return state === props.currentState;
  return stateIdx <= currentIdx;
}

const selectedState = ref<string | null>(null);

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
        stroke: isVisited(to) ? '#22c55e' : '#d1d5db',
        strokeWidth: isVisited(to) ? 2 : 1,
      },
    });
  }

  const loopEdges: [string, string, string][] =
    props.engine === 'cc-headless'
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
      style: { stroke: '#a78bfa', strokeWidth: 1.5, strokeDasharray: '6 3' },
      labelStyle: { fontSize: '10px', fill: '#8b5cf6' },
      labelBgStyle: { fill: 'oklch(var(--b1))', fillOpacity: 0.9 },
    });
  }

  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: 'TB',
    ranksep: 60,
    nodesep: 50,
    marginx: 20,
    marginy: 20,
  });

  for (const node of nodes) {
    if (abortStateSet.has(node.id)) continue;
    g.setNode(node.id, { width: 130, height: 44 });
  }
  for (const edge of edges) {
    if (abortStateSet.has(edge.source) || abortStateSet.has(edge.target))
      continue;
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  for (const node of nodes) {
    if (abortStateSet.has(node.id)) continue;
    const pos = g.node(node.id);
    if (pos) {
      node.position = { x: pos.x - 65, y: pos.y - 22 };
    }
  }

  const anchorNode = nodes.find((n) => n.id === states.at(-2));
  const baseX = anchorNode ? anchorNode.position.x + 180 : 250;
  const baseY = anchorNode ? anchorNode.position.y - 20 : 300;
  for (let i = 0; i < abortStates.length; i++) {
    const node = nodes.find((n) => n.id === abortStates[i]);
    if (node) {
      node.position = { x: baseX, y: baseY + i * 50 };
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
  <div class="flex gap-4" style="height: 520px">
    <div
      class="flex-1 rounded-box border border-base-content/10 overflow-hidden"
    >
      <VueFlow
        :nodes="graphNodes"
        :edges="graph.edges"
        :default-viewport="{ zoom: 0.95, x: 20, y: 10 }"
        fit-view-on-init
        :min-zoom="0.5"
        :max-zoom="1.5"
        :pan-on-drag="true"
        :zoom-on-scroll="false"
        @node-click="onNodeClick"
      >
        <template #node-stateNode="{ data }">
          <div
            class="rounded-box border px-3 py-2 text-center cursor-pointer transition-colors hover:border-base-content/40 min-w-[80px]"
            :class="[
              data.isCurrent
                ? 'border-primary bg-primary/15 ring-2 ring-primary/30'
                : data.visited
                  ? 'border-primary/40 bg-primary/[0.06]'
                  : data.isTerminal
                    ? data.state === 'FAILED'
                      ? 'border-base-content/45 bg-base-200'
                      : 'border-base-content/10 bg-base-200/50'
                    : 'border-base-content/10 bg-base-100',
              data.isSelected ? 'ring-2 ring-base-content/25' : '',
            ]"
          >
            <div
              class="text-xs font-semibold whitespace-nowrap"
              :class="data.isCurrent ? 'text-primary' : ''"
            >
              {{ data.label }}
            </div>
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

    <div class="w-56 shrink-0 overflow-y-auto">
      <template v-if="selectedState">
        <h4 class="font-bold text-sm">
          {{ STATE_LABEL[selectedState] || selectedState }}
        </h4>
        <span
          class="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded mt-1"
          :class="
            selectedState === currentState
              ? 'bg-primary/15 text-primary'
              : isVisited(selectedState)
                ? 'bg-primary/10 text-primary'
                : 'bg-base-content/5 text-base-content/65'
          "
        >
          {{
            selectedState === currentState
              ? '현재'
              : isVisited(selectedState)
                ? '완료'
                : '대기'
          }}
        </span>
        <p class="text-xs text-base-content/74 leading-relaxed mt-3">
          {{ STATE_DESC[selectedState] || '' }}
        </p>
        <div v-if="transitions[selectedState]" class="mt-3">
          <div
            class="text-[10px] font-medium text-base-content/65 uppercase tracking-wider mb-1"
          >
            전이 가능
          </div>
          <div class="flex flex-wrap gap-1">
            <span
              v-for="t in transitions[selectedState]"
              :key="t"
              class="badge badge-xs badge-ghost cursor-pointer hover:badge-outline"
              @click="selectedState = t"
              >{{ STATE_LABEL[t] || t }}</span
            >
          </div>
        </div>
        <div v-if="pipelineStates.includes(selectedState)" class="mt-2">
          <div
            class="text-[10px] font-medium text-base-content/65 uppercase tracking-wider mb-1"
          >
            중단 전이
          </div>
          <div class="flex flex-wrap gap-1">
            <span
              v-for="t in abortStates"
              :key="t"
              class="badge badge-xs badge-ghost"
              >{{ STATE_LABEL[t] }}</span
            >
          </div>
        </div>
      </template>
      <template v-else>
        <div
          class="flex flex-col items-center justify-center h-full text-base-content/64 gap-2"
        >
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
          <p class="text-xs text-center">
            노드를 클릭하면<br />상태 설명을 확인할 수 있습니다
          </p>
        </div>
      </template>
    </div>
  </div>
</template>
