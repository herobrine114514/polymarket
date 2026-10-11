"""Strict finite-state probability checks for isolated forecast research.

Relations use an application-supplied atomic outcome space, never relationships
invented by the model. Set algebra verifies the declared relation; it does not
prove that a real-world contract has those outcomes. Inputs remain unmodified.
"""
from __future__ import annotations

import math

VERSION = 'finite-state-probability-audit-v1'
TOLERANCE = 1e-6


def probability(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def audit_relations(claims: list[dict], proof: dict) -> dict:
    """Check independently supplied claims against verified finite-set relations.

    Missing/invalid/duplicate/unknown claims fail, with coverage in the report.
    In particular, NaN is not clipped, and duplicates never overwrite a value.
    """
    if not isinstance(proof, dict): raise ValueError('Invalid finite-state relation proof')
    atoms = proof.get('atoms')
    definitions, relations = proof.get('propositions'), proof.get('relations')
    if (proof.get('protocol') != VERSION or not isinstance(atoms, list) or not 1 <= len(atoms) <= 32 or
            any(not isinstance(a, str) or not a for a in atoms) or len(set(atoms)) != len(atoms) or
            not isinstance(definitions, list) or not 1 <= len(definitions) <= 64 or
            not isinstance(relations, list) or not 1 <= len(relations) <= 256):
        raise ValueError('Invalid finite-state relation proof')
    universe, sets = set(atoms), {}
    for definition in definitions:
        if not isinstance(definition, dict) or set(definition) != {'id', 'atoms'}:
            raise ValueError('Invalid proposition definition')
        key, members = definition['id'], definition['atoms']
        if (not isinstance(key, str) or not key or key in sets or not isinstance(members, list) or
                any(not isinstance(a, str) for a in members) or len(set(members)) != len(members) or
                not set(members) <= universe):
            raise ValueError('Duplicate/invalid proposition identity or atom set')
        sets[key] = set(members)
    verified = []
    for relation in relations:
        if not isinstance(relation, dict) or set(relation) != {'id', 'kind', 'members'}:
            raise ValueError('Invalid relation definition')
        key, kind, members = relation['id'], relation['kind'], relation['members']
        if (not isinstance(key, str) or not key or key in {r['id'] for r in verified} or
                not isinstance(members, list) or not members or any(not isinstance(m, str) or m not in sets for m in members) or
                len(set(members)) != len(members)):
            raise ValueError('Duplicate/unknown relation identity or member')
        operands = [sets[m] for m in members]
        disjoint = all(not a & b for i, a in enumerate(operands) for b in operands[i+1:])
        valid = ((kind == 'partition' and disjoint and set.union(*operands) == universe) or
                 (kind == 'complement' and len(operands) == 2 and disjoint and operands[0] | operands[1] == universe) or
                 (kind == 'implies' and len(operands) == 2 and operands[0] <= operands[1]) or
                 (kind == 'intersection' and len(operands) == 3 and operands[2] == operands[0] & operands[1]) or
                 (kind == 'disjoint_union' and len(operands) >= 2 and
                  all(not a & b for i, a in enumerate(operands[1:]) for b in operands[i+2:]) and
                  operands[0] == set.union(*operands[1:])))
        if not valid:
            raise ValueError('Relation is not proved by its atomic outcome sets')
        verified.append(relation)
    if not isinstance(claims, list) or len(claims) > 256:
        raise ValueError('Probability claims must be a bounded list')
    values, errors, seen, invalid = {}, [], set(), set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != {'id', 'probability'} or not isinstance(claim['id'], str):
            errors.append({'index': index, 'reason': 'invalid_claim_schema'}); continue
        key = claim['id']
        if key in seen:
            errors.append({'index': index, 'id': key, 'reason': 'duplicate_claim'}); invalid.add(key); values.pop(key, None); continue
        seen.add(key)
        if key not in sets:
            errors.append({'index': index, 'id': key, 'reason': 'unknown_claim'}); continue
        if not probability(claim['probability']):
            errors.append({'index': index, 'id': key, 'reason': 'invalid_probability'}); invalid.add(key); continue
        values[key] = claim['probability']
    missing = sorted(set(sets) - seen)
    errors.extend({'id': key, 'reason': 'missing_claim'} for key in missing)
    results = []
    for relation in verified:
        members = relation['members']
        if any(m not in values or m in invalid for m in members):
            results.append({'id': relation['id'], 'kind': relation['kind'], 'status': 'incomplete'}); continue
        ps = [values[m] for m in members]; kind = relation['kind']
        if kind in ('partition', 'complement'):
            error = abs(sum(ps)-1)
        elif kind == 'implies':
            error = max(0., ps[0]-ps[1])
        elif kind == 'intersection':
            error = max(0., max(0., ps[0]+ps[1]-1)-ps[2], ps[2]-min(ps[:2]))
        else:
            error = abs(ps[0]-sum(ps[1:]))
        results.append({'id': relation['id'], 'kind': kind, 'status': 'passed' if error <= TOLERANCE else 'violated',
                        'violation_magnitude': error})
    complete = sum(r['status'] != 'incomplete' for r in results)
    return {'protocol': VERSION, 'accepted': not errors and all(r['status'] == 'passed' for r in results),
            'input_errors': errors, 'required_claims': len(sets), 'valid_unique_claims': len(values),
            'input_coverage': len(values)/len(sets), 'relations_total': len(results), 'relations_checked': complete,
            'relation_coverage': complete/len(results), 'relations': results, 'tolerance': TOLERANCE,
            'raw_probabilities_changed': False, 'semantic_review_completed': False, 'live_ready': False,
            'proof_scope': 'finite-set algebra only; contract/outcome interpretation requires separate verification'}


def state_proof(packet: dict) -> dict:
    """One frozen event's partition; no cross-horizon contract inference."""
    keys = [o['key'] for o in packet['options']]
    return {'protocol': VERSION, 'atoms': keys,
            'propositions': [{'id': k, 'atoms': [k]} for k in keys],
            'relations': [{'id': 'exclusive_exhaustive_states', 'kind': 'partition', 'members': keys}]}


def state_audit(packet: dict, probabilities: dict) -> dict:
    if not isinstance(probabilities, dict): raise ValueError('Missing state probability object')
    return audit_relations([{'id': k, 'probability': p} for k, p in probabilities.items()], state_proof(packet))


def macro_proof(packet: dict) -> dict:
    from .market import FED_OUTCOME_SLOTS, fed_outcome_slot, parse_utc
    labels = [o['label'] for o in packet['options']]
    keys = [o['key'] for o in packet['options']]
    if (packet.get('category') != 'macro' or packet.get('kind') != 'exclusive_multiple_choice' or
            len(labels) != 5 or set(labels) != set(FED_OUTCOME_SLOTS) or len(set(keys)) != 5 or
            any(fed_outcome_slot(o['question']) != o['label'] for o in packet['options'])):
        raise ValueError('Macro atom names differ from the frozen contract states')
    contracts = packet.get('contract_snapshot') or []
    by_id = {m['id']: m for m in contracts}
    if (len(contracts) != 5 or set(by_id) != set(keys) or packet.get('market_ids') != sorted(keys) or
            parse_utc(packet.get('end_at')) is None or any(
                by_id[o['key']]['question'] != o['question'] or by_id[o['key']]['event_id'] != packet['event_id'] or
                by_id[o['key']]['category'] != 'macro' or by_id[o['key']]['description'] != packet['resolution_rules'] or
                parse_utc(by_id[o['key']]['end_at']) != parse_utc(packet['end_at']) for o in packet['options'])):
        raise ValueError('Macro relation proof differs from the frozen contract mapping/rules/cutoff')
    directions = {'decrease': list(FED_OUTCOME_SLOTS[:2]), 'no_change': ['no_change'], 'increase': list(FED_OUTCOME_SLOTS[3:])}
    definitions = [{'id': 'state:'+s, 'atoms': [s]} for s in FED_OUTCOME_SLOTS]
    definitions += [{'id': 'direction:'+d, 'atoms': states} for d, states in directions.items()]
    relations = [{'id': 'five_states', 'kind': 'partition', 'members': ['state:'+s for s in FED_OUTCOME_SLOTS]},
                 {'id': 'three_directions', 'kind': 'partition', 'members': ['direction:'+d for d in directions]}]
    for direction, states in directions.items():
        relations.append({'id': 'aggregate:'+direction, 'kind': 'disjoint_union',
                          'members': ['direction:'+direction]+['state:'+s for s in states]})
    return {'protocol': VERSION, 'atoms': list(FED_OUTCOME_SLOTS), 'propositions': definitions, 'relations': relations}


def macro_audit(packet: dict, raw: dict) -> dict:
    claims = []
    for field, prefix in (('probabilities_by_state', 'state:'), ('direction_probabilities', 'direction:')):
        if not isinstance(raw.get(field), dict): raise ValueError('Missing macro probability claims')
        claims.extend({'id': prefix+k, 'probability': v} for k, v in raw[field].items())
    return audit_relations(claims, macro_proof(packet))
