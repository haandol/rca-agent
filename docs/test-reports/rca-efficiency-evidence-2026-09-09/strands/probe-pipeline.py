import sys, time, json
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
        raise RuntimeError('Audit forbids network: ' + event)
sys.addaudithook(offline)
from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory, HypothesisStatus, ScopingResult, ValidationResult, ValidationJudgment, AlarmPayload
from rca_agent.services.hypothesis import HypothesisOutput, run_hypothesis_generation
from rca_agent.services.pipeline import PipelineOrchestrator, RunContext, ValidationLoopState
from rca_agent.services.evidence import EvidenceCollectionSummary, EvidenceOutput, run_evidence_collection
from rca_agent.services.branching import BranchingOutput
from rca_agent.services.scoping import ScopingOutput, run_scoping

container=MagicMock(); trace=MagicMock()
run=RunContext('audit', 'fake', 1, trace, time.monotonic())
span=SimpleNamespace(span_id='loop'); gate=SimpleNamespace(expansion_blocked=False)
scoping=ScopingResult(alarm_summary='fixed alarm')
output=HypothesisOutput.model_validate({'hypotheses':[{'description':f'candidate {i}', 'category':'DEPLOYMENT', 'confidence_score':0.5, 'required_evidence':['fixed query']} for i in range(3)]})
container.hypothesis_agent.return_value=SimpleNamespace(structured_output=output)
first=run_hypothesis_generation(scoping, container.hypothesis_agent)
state=ValidationLoopState(hypotheses=list(first.hypotheses))
orch=PipelineOrchestrator(container); collected=[]
def collect(hypotheses, *_args, **_kwargs):
    collected.extend(h.description for h in hypotheses)
    return EvidenceCollectionSummary(evidence_map={h.hypothesis_id:'fixed evidence' for h in hypotheses})
with patch('rca_agent.services.pipeline.run_evidence_collection', side_effect=collect):
    for round_index in range(3):
        orch._loop_evidence(state, scoping, state.hypotheses, run, span)
        if round_index==2: break
        for h in state.hypotheses: h.status=HypothesisStatus.REJECTED
        state.rejected_descriptions=[h.description for h in state.hypotheses]
        state.all_judgments=[ValidationJudgment(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.REJECTED, confidence_score=0.1, reasoning='REFUTED_BY_FIXED_EVIDENCE') for h in state.hypotheses]
        orch._maybe_regenerate(state, scoping, ValidationResult(tree_id=state.hypotheses[0].tree_id, judgments=state.all_judgments, all_rejected=True), gate, run, span)
prompts=[call.args[0] for call in container.hypothesis_agent.call_args_list]
print('REGEN', json.dumps({'generation_calls':len(prompts), 'distinct_user_prompts':len(set(prompts)), 'rejection_reason_passed':any('REFUTED_BY_FIXED_EVIDENCE' in p for p in prompts), 'collected_hypotheses':len(collected), 'unique_descriptions':len(set(collected)), 'cached_evidence_entries':len(state.evidence_map)}))
parent=Hypothesis(hypothesis_id='parent', description='parent cause', category=HypothesisCategory.DEPLOYMENT, confidence_score=0.5, status=HypothesisStatus.NEEDS_INVESTIGATION, tree_id='tree')
container.branching_agent.return_value=SimpleNamespace(structured_output=BranchingOutput.model_validate({'children':[{'description':f'child {i}', 'category':'DEPLOYMENT', 'confidence_score':0.5} for i in range(3)]}))
state=ValidationLoopState(hypotheses=[parent], evidence_map={'parent':'fixed summary'}, all_judgments=[ValidationJudgment(hypothesis_id='parent', status=HypothesisStatus.NEEDS_INVESTIGATION, confidence_score=0.5)])
for _ in range(2): orch._loop_branching(state, gate, trace, span)
children=[h for h in state.hypotheses if h.parent_id=='parent']
print('BRANCH', json.dumps({'branch_calls':container.branching_agent.call_count, 'child_nodes':len(children), 'unique_child_descriptions':len({h.description for h in children}), 'unique_child_ids':len({h.hypothesis_id for h in children})}))
starts=[]
def slow_search(_):
    time.sleep(0.03)
    return []
def slow_scope(*args, **kwargs):
    starts.append(time.perf_counter()); time.sleep(0.05)
    return SimpleNamespace(structured_output=ScopingOutput(alarm_summary='mock'))
start=time.perf_counter()
scoped=run_scoping(AlarmPayload(alarm_name='audit'), MagicMock(side_effect=slow_scope), report_store=SimpleNamespace(search_similar=slow_search), timeout_seconds=0.02)
print('SCOPING', json.dumps({'configured_budget_ms':20, 'agent_started_after_ms':round((starts[0]-start)*1000,2), 'total_ms':round((time.perf_counter()-start)*1000,2), 'fallback':scoped.alarm_summary.startswith('[Timeout]')}))
intervals=[]
def slow_evidence(*args, **kwargs):
    start=time.perf_counter(); time.sleep(0.02); intervals.append((start,time.perf_counter()))
    return SimpleNamespace(structured_output=EvidenceOutput(combined_summary='mock evidence'))
start=time.perf_counter()
with patch('rca_agent.services.evidence.create_evidence_collection_agent', return_value=MagicMock(side_effect=slow_evidence)):
    result=run_evidence_collection(first.hypotheses, scoping, timeout_seconds=1)
print('SERIAL', json.dumps({'calls':len(intervals), 'synthetic_work_ms_each':20, 'total_ms':round((time.perf_counter()-start)*1000,2), 'any_overlap':any(intervals[i][0]<intervals[i-1][1] for i in range(1,len(intervals))), 'failed':len(result.failed_ids)}))
