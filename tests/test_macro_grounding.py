from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import unittest
import sys
import types
from unittest.mock import patch

from polycast.event_research import model_view,control_payload,read_packet,encoded,sha
from polycast.macro_grounding import build_registry,checked_registry,audit_facts,checked_forecast,prompt
from polycast.store import read_database
from experiments.event_study import run
from tests import test_macro_policy as macro_test_helpers
from tests.macro_grounding_fixture import grounded_output


class MacroGroundingTests(unittest.TestCase):
    def setUp(self):
        fixture=macro_test_helpers.MacroEvidenceTests(methodName='runTest')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.packet,self.path=fixture.packet()

    def test_registry_derives_exact_counts_without_comparator_probabilities(self):
        registry=checked_registry(self.packet)
        self.assertEqual(registry['reference_classes']['all_received_scheduled']['events'],2)
        counts=registry['reference_classes']['all_received_scheduled']['counts_by_state']
        self.assertEqual(counts['no_change'],1);self.assertEqual(counts['decrease_50_plus'],1)
        self.assertEqual(sum(counts.values()),2)
        self.assertIsNone(registry['future_reference_upper_bps'])
        self.assertFalse(registry['comparator_probabilities_included'])
        self.assertNotIn('research_state_probabilities',encoded(registry))

    def test_control_and_candidate_get_identical_fact_registry_and_no_baseline(self):
        control=control_payload(self.packet); candidate=model_view(self.packet)
        self.assertEqual(control['macro_fact_registry'],candidate['macro_fact_registry'])
        self.assertEqual(control['macro_fact_registry_sha256'],candidate['macro_fact_registry_sha256'])
        self.assertIn('future_reference_upper_bps',encoded(candidate))
        self.assertNotIn('contract_state_probabilities',encoded(control))
        self.assertNotIn('research_state_probabilities',prompt(self.packet))

    def test_valid_output_preserves_probabilities_and_explicitly_renders_fact_ids(self):
        raw=grounded_output(self.packet);before=copy.deepcopy(raw)
        self.assertTrue(audit_facts(self.packet,raw)['accepted'])
        result=checked_forecast(self.packet,raw)
        expected={o['key']:raw['probabilities_by_state'][o['label']] for o in self.packet['options']}
        self.assertEqual(result['probabilities'],expected);self.assertEqual(raw,before)
        self.assertIn('no_change=1/2',result['base_rate_reasoning'])
        self.assertEqual(len(result['resolution_conditions']),3)
        self.assertTrue(any('future quantity' in s for s in result['necessary_factors']))
        self.assertFalse(audit_facts(self.packet,raw)['free_text_semantics_verified'])

    def test_reference_class_unknown_wrong_count_boolean_and_other_class_fail(self):
        raw=grounded_output(self.packet)
        edits=[('id','invented'),('events',40),('events',True),('counts_by_state',dict.fromkeys(raw['history_reference_class']['counts_by_state'],0))]
        for key,value in edits:
            bad=copy.deepcopy(raw);bad['history_reference_class'][key]=value
            with self.subTest(key=key,value=value):
                self.assertFalse(audit_facts(self.packet,bad)['accepted'])
                with self.assertRaises(ValueError):checked_forecast(self.packet,bad)

    def test_future_reference_hidden_market_latest_value_and_hash_are_strict(self):
        for key,value in [('future_reference_status','known_400_bps'),('market_visibility','missing_market'),
                          ('latest_captured_upper_bps',400),('fact_registry_sha256','0'*64)]:
            raw=grounded_output(self.packet);raw[key]=value
            with self.subTest(key=key):self.assertFalse(audit_facts(self.packet,raw)['accepted'])

    def test_unknown_missing_market_and_omitted_fallback_cannot_be_rendered(self):
        for key,value in [('missing_information_ids',['current_market_prices']),
                          ('necessary_factor_fact_ids',['future_premeeting_upper_400']),
                          ('resolution_rule_ids',['reference_delta','rounding']),
                          ('resolution_rule_ids',['reference_delta','rounding','rounding'])]:
            raw=grounded_output(self.packet);raw[key]=value
            with self.subTest(key=key):self.assertFalse(audit_facts(self.packet,raw)['accepted'])

    def test_extra_numeric_future_value_and_legacy_unbound_text_are_rejected(self):
        for key,value in [('future_reference_upper_bps',500),('base_rate_reasoning','Historical holds 70%')]:
            raw=grounded_output(self.packet);raw[key]=value
            self.assertFalse(audit_facts(self.packet,raw)['accepted'])

    def test_explicit_no_intermeeting_change_is_rendered_as_assumption_only(self):
        raw=grounded_output(self.packet);raw['assume_no_intermeeting_change']=True
        result=checked_forecast(self.packet,raw)
        self.assertIn('Model assumption only',result['necessary_factors'][-1])
        self.assertIn('not an observed future fact',result['necessary_factors'][-1])

    def test_forged_registry_even_with_new_hash_cannot_enter_model_view_or_read_packet(self):
        wrapper=json.loads(self.path.read_text());packet=wrapper['packet']
        packet['input_audit']['macro_fact_registry']['reference_classes']['all_received_scheduled']['events']=99
        packet['input_audit']['macro_fact_registry_sha256']=sha(packet['input_audit']['macro_fact_registry'])
        wrapper['packet_sha256']=sha(packet);wrapper['bundle_sha256']=sha({k:v for k,v in wrapper.items() if k!='bundle_sha256'})
        badpath=Path(self.fixture.temp.name)/'tampered.json';badpath.write_text(encoded(wrapper),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'registry differs'):read_packet(badpath)
        with self.assertRaises(ValueError):model_view(packet)

    def test_duplicate_history_and_future_source_receipt_fail_registry_construction(self):
        for kind in ('duplicate','future'):
            packet=copy.deepcopy(self.packet)
            observation=next(m for g in packet['evidence_groups'] for m in g['members'] if m['evidence']['source_id']=='fed_policy_history')
            content=json.loads(observation['evidence']['summary'])
            if kind=='duplicate':content['facts']['statements'].append(content['facts']['statements'][-1])
            else:content['facts']['statements'][-1]['received_at']='2099-01-01T00:00:00+00:00'
            observation['evidence']['summary']=encoded(content)
            with self.assertRaises(ValueError):build_registry(packet)

    def test_changed_input_registry_fails_before_budget(self):
        wrapper=json.loads(self.path.read_text());wrapper['packet']['input_audit']['macro_fact_registry_sha256']='0'*64
        wrapper['packet_sha256']=sha(wrapper['packet']);wrapper['bundle_sha256']=sha({k:v for k,v in wrapper.items() if k!='bundle_sha256'})
        badpath=Path(self.fixture.temp.name)/'bad.json';badpath.write_text(encoded(wrapper),encoding='utf-8')
        with patch('polycast.store.reserve_model_call') as reserve, self.assertRaises(ValueError):
            run(self.fixture.config,badpath,Path(self.fixture.temp.name)/'run')
        reserve.assert_not_called()

    def test_actual_pilot_path_records_rejected_grounding_and_original_output(self):
        packet=self.packet;control={'event_id':packet['event_id'],'as_of':packet['as_of'],'abstain':False,
            'probabilities':{o['key']:.2 for o in packet['options']}}
        raw=grounded_output(packet);raw['history_reference_class']['events']=70
        out=Path(self.fixture.temp.name)/'run'
        with patch('polycast.analysis.run_event_codex',return_value=control),patch('polycast.analysis._run_json',return_value=raw):
            summary=run(self.fixture.config,self.path,out)
        receipt=json.loads((out/'structured.json').read_text())
        self.assertEqual(receipt['status'],'failed_or_late');self.assertIsNone(receipt['result'])
        self.assertEqual(receipt['provider_result'],raw)
        self.assertFalse(receipt['output_fact_audit']['accepted'])
        self.assertTrue(receipt['output_probability_audit']['accepted'])
        self.assertFalse(summary['paired_eligible_forecasts'])
        with read_database(self.fixture.config.database) as conn:
            for table in ('predictions','paper_orders','trade_intents'):
                self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_external_framework_mapping_keeps_the_same_fact_registry(self):
        from experiments.event_study import framework_mapping
        class Question:
            def __init__(self,**kwargs):self.__dict__.update(kwargs)
        fake=types.SimpleNamespace(BinaryQuestion=Question,MultipleChoiceQuestion=Question)
        with patch('importlib.metadata.version',return_value='0.3.1'),patch.dict(sys.modules,{'forecasting_tools':fake}):
            result=framework_mapping(self.packet)
        view=model_view(self.packet)
        expected={k:view[k] for k in ('evidence_groups','macro_fact_registry','macro_fact_registry_sha256')}
        self.assertEqual(result['background_sha256'],hashlib.sha256(encoded(expected).encode()).hexdigest())
        self.assertEqual(result['macro_fact_registry_sha256'],view['macro_fact_registry_sha256'])
        self.assertEqual(result['model_calls'],0)


if __name__=='__main__':unittest.main()
