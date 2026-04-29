import dagre from '@dagrejs/dagre'
import type { Node, Edge } from '@vue-flow/core'

interface SpanItem {
  spanId: string
  spanType: string
  spanStatus: string
  parentSpanId: string | null
  loopIndex: number | null
  startTime: string
  endTime: string | null
  durationMs: number | null
  inputSummary: string
  outputSummary: string
  error: string | null
  metadata: Record<string, unknown> | null
}

interface HypothesisItem {
  hypothesisId: string
  treeId: string
  parentId: string | null
  depth: number
  title?: string
  description: string
  category: string
  confidenceScore: number
  status: string
  requiredEvidence: string[]
  referencedPlaybookId: string | null
  evidenceSummary: string
  judgmentReasoning: string
  judgmentConfidence: number | null
  createdAt: string
  updatedAt: string
}

export interface NodeData {
  nodeType: 'span' | 'hypothesis'
  label: string
  status: string
  durationMs?: number | null
  detail: string
  error?: string | null
  metadata?: Record<string, unknown> | null
  spanId?: string
  spanType?: string
  category?: string
  confidenceScore?: number
  loopIndex?: number | null
  hypothesisId?: string
  title?: string
  description?: string
  evidenceSummary?: string
  judgmentReasoning?: string
}

const SPAN_LABEL: Record<string, string> = {
  SCOPING: '스코핑',
  HYPOTHESIS_GENERATION: '가설 생성',
  REPORT: '보고서',
  PLAYBOOK: '플레이북',
  REMEDIATION: '자동 복구',
  VERIFICATION: '복구 검증',
  NOTIFICATION: '알림',
}

// Internal loop steps — hide from the graph
const HIDDEN_SPAN_TYPES = new Set([
  'VALIDATION_LOOP',
  'PRIORITIZATION',
  'EVIDENCE_COLLECTION',
  'VALIDATION',
  'BRANCHING',
  'TERMINATION',
])

export function buildTraceGraph(spans: SpanItem[], hypotheses: HypothesisItem[]) {
  const nodes: Node<NodeData>[] = []
  const edges: Edge[] = []

  const sorted = [...spans].sort((a, b) => a.startTime.localeCompare(b.startTime))
  const visible = sorted.filter(s => !HIDDEN_SPAN_TYPES.has(s.spanType))

  // Span nodes — top-level pipeline steps only
  for (const s of visible) {
    const label = SPAN_LABEL[s.spanType] || s.spanType.replace(/_/g, ' ')

    nodes.push({
      id: `span-${s.spanId}`,
      type: 'spanNode',
      position: { x: 0, y: 0 },
      data: {
        nodeType: 'span',
        label,
        status: s.spanStatus,
        durationMs: s.durationMs,
        detail: s.outputSummary || s.inputSummary,
        error: s.error,
        metadata: s.metadata,
        spanId: s.spanId,
        spanType: s.spanType,
        loopIndex: s.loopIndex,
      },
    })
  }

  // Sequential edges between visible root spans
  for (let i = 1; i < visible.length; i++) {
    const prev = visible[i - 1]!
    const curr = visible[i]!
    edges.push({
      id: `e-seq-${prev.spanId}-${curr.spanId}`,
      source: `span-${prev.spanId}`,
      target: `span-${curr.spanId}`,
    })
  }

  // Hypothesis nodes
  const hypoById = new Map<string, HypothesisItem>()
  for (const h of hypotheses) hypoById.set(h.hypothesisId, h)

  for (const h of hypotheses) {
    const firstLine = h.description.split('\n')[0] ?? h.description
    const nodeLabel = h.title?.trim()
      ? h.title
      : firstLine.length > 60
        ? firstLine.slice(0, 57) + '...'
        : firstLine
    nodes.push({
      id: `hypo-${h.hypothesisId}`,
      type: 'hypoNode',
      position: { x: 0, y: 0 },
      data: {
        nodeType: 'hypothesis',
        label: nodeLabel,
        status: h.status,
        detail: h.evidenceSummary || h.judgmentReasoning,
        category: h.category,
        confidenceScore: h.confidenceScore,
        hypothesisId: h.hypothesisId,
        title: h.title,
        description: h.description,
        evidenceSummary: h.evidenceSummary,
        judgmentReasoning: h.judgmentReasoning,
      },
    })

    if (h.parentId && hypoById.has(h.parentId)) {
      edges.push({
        id: `e-hypo-${h.parentId}-${h.hypothesisId}`,
        source: `hypo-${h.parentId}`,
        target: `hypo-${h.hypothesisId}`,
      })
    }
  }

  // Connect hypothesis roots to the first HYPOTHESIS_GENERATION span
  const hypoGenSpan = visible.find(s => s.spanType === 'HYPOTHESIS_GENERATION')
  if (hypoGenSpan) {
    const rootHypos = hypotheses.filter(h => !h.parentId || !hypoById.has(h.parentId))
    for (const h of rootHypos) {
      edges.push({
        id: `e-gen-${hypoGenSpan.spanId}-${h.hypothesisId}`,
        source: `span-${hypoGenSpan.spanId}`,
        target: `hypo-${h.hypothesisId}`,
        style: { strokeDasharray: '5 5' },
      })
    }
  }

  // Connect the last hypothesis generation span → report span
  const reportSpan = visible.find(s => s.spanType === 'REPORT')
  if (hypoGenSpan && reportSpan) {
    // Remove the direct sequential edge between hypo gen and report if exists
    const directEdgeIdx = edges.findIndex(
      e => e.source === `span-${hypoGenSpan.spanId}` && e.target === `span-${reportSpan.spanId}`,
    )
    if (directEdgeIdx >= 0) edges.splice(directEdgeIdx, 1)
  }

  // Auto-layout with dagre
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'TB', ranksep: 60, nodesep: 40 })

  for (const node of nodes) {
    const w = node.type === 'hypoNode' ? 220 : 160
    const h = node.type === 'hypoNode' ? 80 : 50
    g.setNode(node.id, { width: w, height: h })
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target)
  }

  dagre.layout(g)

  for (const node of nodes) {
    const pos = g.node(node.id)
    if (pos) {
      const w = node.type === 'hypoNode' ? 220 : 160
      const h = node.type === 'hypoNode' ? 80 : 50
      node.position = { x: pos.x - w / 2, y: pos.y - h / 2 }
    }
  }

  return { nodes, edges }
}
