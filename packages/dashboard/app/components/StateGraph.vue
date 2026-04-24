<script setup lang="ts">
import dagre from '@dagrejs/dagre'
import { VueFlow, Handle, Position, type Node, type Edge, MarkerType } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'

const props = defineProps<{
  currentState: string
}>()

const STATE_LABEL: Record<string, string> = {
  ALARM_RECEIVED: '알람 수신',
  SCOPING: '스코핑',
  HYPOTHESIS_GENERATION: '가설 생성',
  HYPOTHESIS_PRIORITIZATION: '우선순위 결정',
  EVIDENCE_COLLECTION: '증거 수집',
  HYPOTHESIS_VALIDATION: '가설 검증',
  REPORT_GENERATION: '보고서 생성',
  COMPLETED: '완료',
  FAILED: '실패',
  CANCELLED: '중단됨',
  OUTDATED: '만료됨',
}

const STATE_DESC: Record<string, string> = {
  ALARM_RECEIVED: 'CloudWatch 알람이 SNS→SQS 경로로 수신되어 RCA 세션이 생성된 초기 상태',
  SCOPING: '알람 메트릭과 관련 메트릭을 조회하여 영향범위와 심각도를 판단하는 단계',
  HYPOTHESIS_GENERATION: '스코핑 결과를 바탕으로 3~5개의 근본원인 가설을 생성하는 단계',
  HYPOTHESIS_PRIORITIZATION: '생성된 가설의 우선순위를 결정하고 상위 빔을 선택하는 단계',
  EVIDENCE_COLLECTION: 'CloudWatch, CloudTrail, GitHub 등에서 가설 검증을 위한 증거를 수집하는 단계',
  HYPOTHESIS_VALIDATION: '수집된 증거를 바탕으로 가설을 확정/기각/추가조사로 분류하는 단계',
  REPORT_GENERATION: '확정된 근본원인과 증거를 기반으로 RCA 보고서를 생성하는 단계',
  COMPLETED: 'RCA 분석이 정상 완료된 상태',
  FAILED: '파이프라인 실행 중 오류가 발생하여 분석이 중단된 상태',
  CANCELLED: '사용자가 수동으로 분석을 중단한 상태',
  OUTDATED: 'TTL 만료 등으로 더 이상 유효하지 않은 세션',
}

const TRANSITIONS: Record<string, string[]> = {
  ALARM_RECEIVED: ['SCOPING'],
  SCOPING: ['HYPOTHESIS_GENERATION'],
  HYPOTHESIS_GENERATION: ['HYPOTHESIS_PRIORITIZATION'],
  HYPOTHESIS_PRIORITIZATION: ['EVIDENCE_COLLECTION'],
  EVIDENCE_COLLECTION: ['HYPOTHESIS_VALIDATION'],
  HYPOTHESIS_VALIDATION: ['REPORT_GENERATION', 'HYPOTHESIS_PRIORITIZATION', 'EVIDENCE_COLLECTION', 'HYPOTHESIS_GENERATION'],
  REPORT_GENERATION: ['COMPLETED'],
}

const HAPPY_PATH = [
  'ALARM_RECEIVED', 'SCOPING', 'HYPOTHESIS_GENERATION', 'HYPOTHESIS_PRIORITIZATION',
  'EVIDENCE_COLLECTION', 'HYPOTHESIS_VALIDATION', 'REPORT_GENERATION', 'COMPLETED',
]

const TERMINAL_STATES = ['COMPLETED', 'FAILED', 'CANCELLED', 'OUTDATED']
const PIPELINE_STATES = HAPPY_PATH.filter(s => !TERMINAL_STATES.includes(s))

function isVisited(state: string): boolean {
  const currentIdx = HAPPY_PATH.indexOf(props.currentState)
  const stateIdx = HAPPY_PATH.indexOf(state)
  if (currentIdx < 0) return TERMINAL_STATES.includes(props.currentState)
  if (stateIdx < 0) return state === props.currentState
  return stateIdx <= currentIdx
}

const selectedState = ref<string | null>(null)

function onNodeClick(e: { node: { id: string } }) {
  selectedState.value = e.node.id
}

const graph = computed(() => {
  const nodes: Node[] = []
  const edges: Edge[] = []

  for (const state of HAPPY_PATH) {
    const isCurrent = state === props.currentState
    const visited = isVisited(state)
    nodes.push({
      id: state,
      type: 'stateNode',
      position: { x: 0, y: 0 },
      data: {
        label: STATE_LABEL[state] || state,
        state,
        isCurrent,
        visited,
        isTerminal: TERMINAL_STATES.includes(state),
        isSelected: false,
      },
    })
  }

  for (const state of ['FAILED', 'CANCELLED', 'OUTDATED']) {
    const isCurrent = state === props.currentState
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
    })
  }

  for (let i = 0; i < HAPPY_PATH.length - 1; i++) {
    const from = HAPPY_PATH[i]!
    const to = HAPPY_PATH[i + 1]!
    edges.push({
      id: `e-${from}-${to}`,
      source: from,
      target: to,
      markerEnd: MarkerType.ArrowClosed,
      style: { stroke: isVisited(to) ? '#22c55e' : '#d1d5db', strokeWidth: isVisited(to) ? 2 : 1 },
    })
  }

  const loopEdges: [string, string, string][] = [
    ['HYPOTHESIS_VALIDATION', 'HYPOTHESIS_GENERATION', '재생성'],
    ['HYPOTHESIS_VALIDATION', 'HYPOTHESIS_PRIORITIZATION', '재우선순위'],
    ['HYPOTHESIS_VALIDATION', 'EVIDENCE_COLLECTION', '추가 증거'],
  ]
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
    })
  }

  const terminalSet = new Set(['FAILED', 'CANCELLED', 'OUTDATED'])
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'TB', ranksep: 60, nodesep: 50, marginx: 20, marginy: 20 })

  for (const node of nodes) {
    if (terminalSet.has(node.id)) continue
    g.setNode(node.id, { width: 130, height: 44 })
  }
  for (const edge of edges) {
    if (terminalSet.has(edge.source) || terminalSet.has(edge.target)) continue
    g.setEdge(edge.source, edge.target)
  }

  dagre.layout(g)

  for (const node of nodes) {
    if (terminalSet.has(node.id)) continue
    const pos = g.node(node.id)
    if (pos) {
      node.position = { x: pos.x - 65, y: pos.y - 22 }
    }
  }

  const validationNode = nodes.find(n => n.id === 'HYPOTHESIS_VALIDATION')
  const baseX = validationNode ? validationNode.position.x + 180 : 250
  const baseY = validationNode ? validationNode.position.y - 20 : 300
  const termOrder = ['FAILED', 'CANCELLED', 'OUTDATED']
  for (let i = 0; i < termOrder.length; i++) {
    const node = nodes.find(n => n.id === termOrder[i])
    if (node) {
      node.position = { x: baseX, y: baseY + i * 50 }
    }
  }

  return { nodes, edges }
})

const graphNodes = computed(() => {
  return graph.value.nodes.map(n => ({
    ...n,
    data: { ...n.data, isSelected: n.id === selectedState.value },
  }))
})
</script>

<template>
  <div class="flex gap-4" style="height: 520px">
    <div class="flex-1 rounded-lg border border-base-content/10 overflow-hidden">
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
            class="rounded-lg border px-3 py-2 text-center cursor-pointer transition-all hover:shadow-md min-w-[80px]"
            :class="[
              data.isCurrent
                ? 'border-primary bg-primary/15 ring-2 ring-primary/30'
                : data.visited
                  ? 'border-success/40 bg-success/8'
                  : data.isTerminal
                    ? data.state === 'FAILED' ? 'border-error/30 bg-error/5' : 'border-base-content/10 bg-base-200/50'
                    : 'border-base-content/10 bg-base-100',
              data.isSelected ? 'ring-2 ring-info/40' : '',
            ]"
          >
            <div class="text-xs font-semibold whitespace-nowrap" :class="data.isCurrent ? 'text-primary' : ''">
              {{ data.label }}
            </div>
          </div>
          <Handle type="target" :position="Position.Top" class="!bg-transparent !border-0 !w-0 !h-0" />
          <Handle :id="`${data.state}-left`" type="target" :position="Position.Left" class="!bg-transparent !border-0 !w-0 !h-0" />
          <Handle type="source" :position="Position.Bottom" class="!bg-transparent !border-0 !w-0 !h-0" />
          <Handle :id="`${data.state}-left`" type="source" :position="Position.Left" class="!bg-transparent !border-0 !w-0 !h-0" />
        </template>
      </VueFlow>
    </div>

    <div class="w-56 shrink-0 overflow-y-auto">
      <template v-if="selectedState">
        <h4 class="font-bold text-sm">{{ STATE_LABEL[selectedState] || selectedState }}</h4>
        <span
          class="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded mt-1"
          :class="
            selectedState === currentState
              ? 'bg-primary/15 text-primary'
              : isVisited(selectedState)
                ? 'bg-success/10 text-success'
                : 'bg-base-content/5 text-base-content/40'
          "
        >
          {{ selectedState === currentState ? '현재' : isVisited(selectedState) ? '완료' : '대기' }}
        </span>
        <p class="text-xs text-base-content/60 leading-relaxed mt-3">
          {{ STATE_DESC[selectedState] || '' }}
        </p>
        <div v-if="TRANSITIONS[selectedState]" class="mt-3">
          <div class="text-[10px] font-medium text-base-content/40 uppercase tracking-wider mb-1">전이 가능</div>
          <div class="flex flex-wrap gap-1">
            <span
              v-for="t in TRANSITIONS[selectedState]"
              :key="t"
              class="badge badge-xs badge-ghost cursor-pointer hover:badge-outline"
              @click="selectedState = t"
            >{{ STATE_LABEL[t] || t }}</span>
          </div>
        </div>
        <div v-if="PIPELINE_STATES.includes(selectedState)" class="mt-2">
          <div class="text-[10px] font-medium text-base-content/40 uppercase tracking-wider mb-1">중단 전이</div>
          <div class="flex flex-wrap gap-1">
            <span v-for="t in TERMINAL_STATES.filter(s => s !== 'COMPLETED')" :key="t" class="badge badge-xs badge-ghost">{{ STATE_LABEL[t] }}</span>
          </div>
        </div>
      </template>
      <template v-else>
        <div class="flex flex-col items-center justify-center h-full text-base-content/30 gap-2">
          <svg xmlns="http://www.w3.org/2000/svg" class="size-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" /></svg>
          <p class="text-xs text-center">노드를 클릭하면<br>상태 설명을 확인할 수 있습니다</p>
        </div>
      </template>
    </div>
  </div>
</template>
