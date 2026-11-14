"""Budget evidence separates whole-job counters from complete recorded phase sums."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import pytest
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3,core_hash,evidence_hash,EvidenceBuildError

FIXTURES=Path(__file__).parent/'fixtures/narrative_v3'
def fixture(name='F4'):return json.loads((FIXTURES/(name+'.json')).read_text())
def facts(e):return {f.fact_id:f for f in e.facts}
def record(e,identifier):return json.loads(next(s.text for s in e.sources if s.evidence_id==identifier))
def build(r):return build_evidence_v3(r,policy_version='1.1')
def phase_report(main,pilot,total):
 r=fixture();templates={phase:next(t for t in r['trials'] if t['phase']==phase) for phase in ['main','pilot']};r['trials']=[];r['decision']=None;r['selected_algorithm_id']=None;r['selection_status']='not_reached';r['budget']={'known_started':total}
 for phase,values in [('main',main),('pilot',pilot)]:
  for i,value in enumerate(values):
   t=deepcopy(templates[phase]);t['trial_id']=f'00000000-0000-4000-8000-{len(r["trials"])+1:012d}';t['started_evaluations']=value;t['replicate_index']=i;r['trials'].append(t)
 return r

@pytest.mark.parametrize('name,expected',[('F1','1e30f8700e96c7a57052bbefeffebb0c16e0e199f2fd56c3e37b889a0c5060b3'),('F4','8199ca023d946a17e89ffdf01869e83ea3ec4c0925e92c245e018f374fddd2bc')])
def test_legacy_policy_10_evidence_keeps_its_historical_hash_and_no_new_scopes(name,expected):
 e=build_evidence_v3(fixture(name));assert evidence_hash(e)==expected
 assert 'scopes' not in record(e,'report:budget')
 assert 'budget.main_plus_pilot_started' not in facts(e) and 'budget.other_started' not in facts(e)

@pytest.mark.parametrize('name',['F1','F4'])
def test_actual_l4_budget_quantities_and_whole_job_scope_are_distinct(name):
 r=fixture(name);before=deepcopy(r);old_core=core_hash(r);e=build(r);f=facts(e);budget=record(e,'report:budget');scopes=budget['scopes']
 assert {key:f['budget.'+key].value for key in ['main_started','pilot_started','total_started','main_plus_pilot_started','other_started']}=={'main_started':14998,'pilot_started':4500,'total_started':20000,'main_plus_pilot_started':19498,'other_started':502}
 assert scopes['budget.total_started']['scope']=='whole_job_known_started'
 assert scopes['budget.total_started']['source_paths']==['/budget/known_started']
 assert scopes['budget.main_started']['scope']==scopes['budget.pilot_started']['scope']=='all_recorded_trials_in_phase'
 assert scopes['budget.main_plus_pilot_started']['scope']=='main_and_pilot_only'
 assert scopes['budget.other_started']['scope']=='whole_job_outside_main_and_pilot_unattributed'
 assert scopes['budget.other_started']['phase_attribution'] is None and scopes['budget.other_started']['not_a_phase_breakdown'] is True
 assert 'phase_counts' not in scopes['budget.other_started']
 assert e.source_core_sha256==old_core==core_hash(r) and r==before
 assert 'budget.main_plus_pilot_started' not in next(s for s in e.sections if s.section_id=='verification_and_limits').required_fact_refs

@pytest.mark.parametrize('identifier,operation,inputs,values',[('budget.main_plus_pilot_started','sum',['budget.main_started','budget.pilot_started'],[14998,4500]),('budget.other_started','subtract',['budget.total_started','budget.main_plus_pilot_started'],[20000,19498])])
def test_derivations_have_exact_input_refs_values_scope_and_hash(identifier,operation,inputs,values):
 e=build(fixture());f=facts(e)[identifier];source=next(s for s in e.sources if s.evidence_id=='derived:'+identifier);p=json.loads(source.text)
 assert f.origin=='derived' and f.derivation.operation==operation and f.derivation.input_fact_refs==inputs
 assert p['operation']==operation and p['inputs']==inputs and p['input_values']==values and p['value']==f.value and p['complete'] is True
 assert source.content_sha256==sha256(source.text.encode()).hexdigest()
 assert 'report:budget' in f.evidence_refs and source.evidence_id in f.evidence_refs
 assert f.source_paths==(['/trials'] if operation=='sum' else ['/budget/known_started','/trials'])

@pytest.mark.parametrize('main,pilot,total,subtotal,other',[
 ([14998],[500,None,500],20000,None,None),([None],[500],20000,None,None),([14998],[],20000,None,None),([],[500],20000,None,None),([],[],None,None,None),([14998],[500],None,15498,None),([0],[],0,None,None)])
def test_missing_inputs_never_become_zero_or_partial_sum(main,pilot,total,subtotal,other):
 r=phase_report(main,pilot,total);e=build(r);f=facts(e);s=record(e,'report:budget')['scopes']
 for name,value in [('main_plus_pilot_started',subtotal),('other_started',other)]:
  fact=f['budget.'+name];assert fact.value==value and fact.availability==('missing' if value is None else 'available')
  assert fact.display_text==('ثبت نشده' if value is None else str(value))
  assert s['budget.'+name]['complete']==(value is not None)
 for phase,counts in [('main',main),('pilot',pilot)]:
  assert s['budget.'+phase+'_started']['input_vector']==counts
  complete=bool(counts) and all(type(v) is int and v>=0 for v in counts)
  assert s['budget.'+phase+'_started']['complete']==complete
  assert f['budget.'+phase+'_counts'].value==(counts if complete else None)
  assert f['budget.'+phase+'_started'].value==(sum(counts) if complete else None)
 observed=[t['started_evaluations'] for t in record(e,'report:trials')]
 assert observed==main+pilot

@pytest.mark.parametrize('main,pilot,total,expected',[([14998],[4500],19498,0),([0],[0],0,0),([0],[0],7,7)])
def test_zero_remainder_is_available_and_zero_recorded_counts_are_not_missing(main,pilot,total,expected):
 f=facts(build(phase_report(main,pilot,total)))
 assert f['budget.other_started'].value==expected and f['budget.other_started'].availability=='available'
 assert f['budget.main_plus_pilot_started'].availability=='available'

@pytest.mark.parametrize('phase,bad',[('main',True),('pilot',True),('main',-1),('pilot',500.0)])
def test_invalid_phase_counts_are_rejected_without_coercion(phase,bad):
 with pytest.raises(EvidenceBuildError):build(phase_report([bad] if phase=='main' else [1],[bad] if phase=='pilot' else [1],20))

@pytest.mark.parametrize('total',[True,-1,20000.0])
def test_invalid_whole_job_counter_is_rejected_without_coercion(total):
 with pytest.raises(EvidenceBuildError):build(phase_report([1],[1],total))

@pytest.mark.parametrize('main,pilot,total',[([14998],[4500],19497),([25000],[None],20000),([None],[25000],20000),([1],[0],0)])
def test_provably_inconsistent_complete_count_fails_closed_even_if_other_phase_is_unknown(main,pilot,total):
 with pytest.raises(EvidenceBuildError) as error:build(phase_report(main,pilot,total))
 assert error.value.code=='CONTEXT_MISMATCH'

def test_phase_totals_include_all_recorded_failed_and_completed_trials():
 r=phase_report([4,6],[2,3],20);r['trials'][0]['execution_status']='failed';r['trials'][-1]['execution_status']='failed';e=build(r);f=facts(e);s=record(e,'report:budget')['scopes']
 assert f['budget.main_counts'].value==[4,6] and f['budget.pilot_counts'].value==[2,3]
 assert f['budget.main_started'].value==10 and f['budget.pilot_started'].value==5
 assert f['budget.main_plus_pilot_started'].value==15 and f['budget.other_started'].value==5
 assert s['budget.main_started']['includes_failed_trials'] is s['budget.pilot_started']['includes_failed_trials'] is True
 assert s['budget.main_started']['does_not_imply']=='successful_or_completed_evaluations'
 assert [t['execution_status'] for t in record(e,'report:trials')]==['failed','completed','completed','failed']
