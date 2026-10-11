"""Synthetic typed output from the test packet's received reference class."""
import copy
from polycast.event_research import sha
from polycast.macro_grounding import checked_registry, FUTURE_REFERENCE, VISIBILITY


def grounded_output(packet, *, probabilities=None, abstain=False):
    registry=checked_registry(packet)
    values=probabilities or {'decrease_50_plus':.01,'decrease_25':.05,'no_change':.30,'increase_25':.60,'increase_50_plus':.04}
    directions={'decrease':sum(p for s,p in values.items() if s.startswith('decrease')),
                'no_change':values['no_change'],'increase':sum(p for s,p in values.items() if s.startswith('increase'))}
    reference=registry['reference_classes']['all_received_scheduled']
    return {'event_id':packet['event_id'],'as_of':packet['as_of'],'abstain':abstain,
        'probabilities_by_state':dict(values),'direction_probabilities':directions,
        'most_likely_state':max(values,key=values.get),'most_likely_direction':max(directions,key=directions.get),
        'supporting_evidence_ids':[registry['evidence_id']],'counter_evidence_ids':[],
        'update_triggers':['Future official release'],'reasoning':'Synthetic subjective estimate, not a historical or profitable prediction.',
        'fact_registry_sha256':sha(registry),'latest_captured_upper_bps':registry['latest_received_policy']['upper_bps'],
        'future_reference_status':FUTURE_REFERENCE,'assume_no_intermeeting_change':False,'market_visibility':VISIBILITY,
        'history_reference_class':{k:copy.deepcopy(reference[k]) for k in ('id','events','counts_by_state')},
        'resolution_rule_ids':list(registry['resolution_rules']),
        'necessary_factor_fact_ids':['latest_received_policy_range','future_premeeting_reference_unobserved'],
        'missing_information_ids':['future_policy_decision'],
        'subjective_adjustment_reasoning':'Synthetic opinion; the recorded frequency is not treated as a calibrated probability.'}
