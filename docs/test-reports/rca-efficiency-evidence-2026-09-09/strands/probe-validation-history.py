import sys, json, copy

def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
        raise RuntimeError('Audit forbids network: ' + event)
sys.addaudithook(offline)
from strands.models.model import Model
from rca_agent.agent_factory import create_validation_agent
from rca_agent.services.validation import run_validation
from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory
class Recorder(Model):
    def __init__(self): self.inputs=[]
    def update_config(self, **kwargs): pass
    def get_config(self): return {}
    async def structured_output(self, *args, **kwargs):
        raise AssertionError('unexpected legacy structured output')
        yield
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        data={'judgment': {'status':'NEEDS_INVESTIGATION', 'confidence_score':0.5, 'reasoning':'mock', 'evidence_summary':['mock evidence'], 'validated_fault_type':'UNSUPPORTED'}}
        yield {'messageStart': {'role': 'assistant'}}
        yield {'contentBlockStart': {'start': {'toolUse': {'toolUseId': str(len(self.inputs)), 'name': tool_specs[-1]['name']}}, 'contentBlockIndex': 0}}
        yield {'contentBlockDelta': {'delta': {'toolUse': {'input': json.dumps(data)}}, 'contentBlockIndex': 0}}
        yield {'contentBlockStop': {'contentBlockIndex': 0}}
        yield {'messageStop': {'stopReason': 'tool_use'}}
        yield {'metadata': {'usage': {'inputTokens': 0, 'outputTokens': 0, 'totalTokens': 0}, 'metrics': {'latencyMs': 0}}}
model=Recorder()
agent=create_validation_agent(model=model)
agent.callback_handler=lambda **_: None
hyps=[Hypothesis(hypothesis_id=f'h-{i}', description=f'independent hypothesis {i}', category=HypothesisCategory.DEPLOYMENT, confidence_score=0.5, tree_id='t') for i in range(3)]
result=run_validation(hyps, {h.hypothesis_id: f'UNIQUE_EVIDENCE_{i}' for i,h in enumerate(hyps)}, agent)
assert len(model.inputs)==3
print(json.dumps({'judgments':len(result.judgments), 'model_invocations':len(model.inputs), 'input_message_counts':[len(m) for m in model.inputs], 'third_input_contains_first_evidence': 'UNIQUE_EVIDENCE_0' in json.dumps(model.inputs[2]), 'input_json_chars':[len(json.dumps(m)) for m in model.inputs]}, indent=2))
