from __future__ import annotations
import copy
import unittest

from polycast.forecast_audit import VERSION, audit_relations


def proof():
    return {'protocol': VERSION, 'atoms': ['ab', 'a_only', 'b_only', 'neither'],
        'propositions': [{'id': 'A', 'atoms': ['ab', 'a_only']}, {'id': 'not_A', 'atoms': ['b_only', 'neither']},
                         {'id': 'B', 'atoms': ['ab', 'b_only']}, {'id': 'AB', 'atoms': ['ab']}],
        'relations': [{'id': 'complement', 'kind': 'complement', 'members': ['A', 'not_A']},
                      {'id': 'implication', 'kind': 'implies', 'members': ['AB', 'A']},
                      {'id': 'intersection', 'kind': 'intersection', 'members': ['A', 'B', 'AB']}]}


def claims(a=.7, b=.6, ab=.4):
    return [{'id': 'A', 'probability': a}, {'id': 'not_A', 'probability': 1-a},
            {'id': 'B', 'probability': b}, {'id': 'AB', 'probability': ab}]


class ForecastAuditTests(unittest.TestCase):
    def test_valid_relationships_do_not_change_inputs_or_grant_semantic_approval(self):
        raw = claims(); frozen = copy.deepcopy(raw)
        result = audit_relations(raw, proof())
        self.assertTrue(result['accepted']); self.assertEqual(result['relation_coverage'], 1)
        self.assertFalse(result['semantic_review_completed']); self.assertFalse(result['live_ready'])
        self.assertEqual(raw, frozen)

    def test_frechet_lower_upper_and_implication_violations(self):
        for ab in (.1, .65, .8):
            with self.subTest(ab=ab):
                result = audit_relations(claims(ab=ab), proof())
                self.assertFalse(result['accepted'])
                self.assertEqual(result['relation_coverage'], 1)
                self.assertTrue(any(r['status'] == 'violated' for r in result['relations']))

    def test_complement_does_not_normalize_inconsistent_claim(self):
        raw = claims(); raw[1]['probability'] = .4
        self.assertFalse(audit_relations(raw, proof())['accepted'])
        self.assertEqual(raw[1]['probability'], .4)

    def test_invalid_nonfinite_boolean_or_out_of_range_is_never_clipped(self):
        for value in (float('nan'), float('inf'), -float('inf'), -.2, 1.2, True, None, '0.7'):
            with self.subTest(value=value):
                raw = claims(); raw[0]['probability'] = value
                result = audit_relations(raw, proof())
                self.assertFalse(result['accepted']); self.assertEqual(result['valid_unique_claims'], 3)
                self.assertLess(result['relation_coverage'], 1)
                self.assertEqual(result['input_errors'][0]['reason'], 'invalid_probability')

    def test_duplicate_values_reject_both_occurrences_and_never_overwrite(self):
        raw = claims()+[{'id': 'A', 'probability': .7}, {'id': 'A', 'probability': .9}]
        result = audit_relations(raw, proof())
        self.assertFalse(result['accepted']); self.assertEqual(result['valid_unique_claims'], 3)
        self.assertEqual(sum(e['reason']=='duplicate_claim' for e in result['input_errors']), 2)

    def test_missing_and_unknown_are_failures_and_coverage_is_disclosed(self):
        result = audit_relations(claims()[:-1]+[{'id': 'unknown', 'probability': .2}], proof())
        self.assertFalse(result['accepted']); self.assertEqual(result['input_coverage'], .75)
        self.assertEqual(result['relation_coverage'], 1/3)
        self.assertEqual({r['reason'] for r in result['input_errors']}, {'missing_claim', 'unknown_claim'})

    def test_empty_or_malformed_input_is_not_an_empty_success(self):
        for raw in ([], [False], [{'id':'A'}]):
            result = audit_relations(raw, proof())
            self.assertFalse(result['accepted']); self.assertEqual(result['relation_coverage'], 0)

    def test_set_proof_rejects_unproved_cross_event_relationships(self):
        for kind, members in (('implies', ['A', 'B']), ('complement', ['A', 'B']),
                              ('intersection', ['A', 'B', 'not_A']), ('partition', ['A', 'B'])):
            invalid = proof(); invalid['relations'] = [{'id':'bad','kind':kind,'members':members}]
            with self.assertRaisesRegex(ValueError, 'not proved'): audit_relations(claims(), invalid)

    def test_bad_identity_and_duplicate_atoms_or_relations_are_rejected(self):
        cases=[]
        p=proof(); p['atoms'].append('ab'); cases.append(p)
        p=proof(); p['propositions'].append(p['propositions'][0]); cases.append(p)
        p=proof(); p['relations'].append(p['relations'][0]); cases.append(p)
        p=proof(); p['relations'][0]['members']=['A','A']; cases.append(p)
        for p in cases:
            with self.assertRaises(ValueError): audit_relations(claims(), p)

    def test_disjoint_union_requires_correct_aggregate_and_partition(self):
        p={'protocol':VERSION,'atoms':['x','y','z'], 'propositions':[
            {'id':'x','atoms':['x']},{'id':'y','atoms':['y']},{'id':'z','atoms':['z']},{'id':'xy','atoms':['x','y']}],
            'relations':[{'id':'all','kind':'partition','members':['x','y','z']},
                         {'id':'sum','kind':'disjoint_union','members':['xy','x','y']}]}
        raw=[{'id':k,'probability':v} for k,v in {'x':.2,'y':.3,'z':.5,'xy':.5}.items()]
        self.assertTrue(audit_relations(raw,p)['accepted'])
        raw[-1]['probability']=.4
        self.assertFalse(audit_relations(raw,p)['accepted'])
        p['propositions'][1]['atoms']=['x','y']
        with self.assertRaises(ValueError): audit_relations(raw,p)


if __name__ == '__main__': unittest.main()
