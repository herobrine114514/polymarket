"""Receipt-verified official policy facts in the append-only research ledger."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from experiments.macro_policy import (VERSION, CALENDAR, MAX_BYTES, build_facts,
    encoded, utc, replay_directory, contract_context)

SOURCE_ID = 'fed_policy_history'
PROTOCOL = 'fomc-history-evidence-v1'
DIRECTORY = 'fomc-history-captures'


def read_json(path: Path) -> tuple[dict, str]:
    if path.is_symlink(): raise ValueError('FOMC evidence cannot follow a symlink')
    with path.open('rb') as stream: raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES: raise ValueError('Oversized FOMC evidence file')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def capture_path(data_dir: Path, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r'capture-[0-9a-f]{32}', name):
        raise ValueError('Invalid FOMC capture identity')
    root = (data_dir/DIRECTORY).resolve(); path = root/name
    if path.is_symlink() or path.resolve().parent != root: raise ValueError('FOMC capture leaves named directory')
    return path


def capture_descriptor(root: Path) -> dict:
    manifest, manifest_sha = read_json(root/'source-before.json')
    result, result_sha = read_json(root/'result.json')
    if (manifest.get('protocol') != 'fomc-history-capture-source-v1' or
            set(manifest.get('files', {})) != {'experiments/macro_policy.py', 'polycast/evidence.py'} or
            result.get('capture_source_manifest_sha256') != manifest_sha):
        raise ValueError('FOMC source manifest mismatch')
    for source in manifest['files'].values():
        raw = base64.b64decode(source['content_base64'], validate=True)
        if hashlib.sha256(raw).hexdigest() != source['sha256']: raise ValueError('FOMC source bytes changed')
    derivation_filename = result.get('derivation_source_filename', '')
    if not re.fullmatch(r'derivation-source-[0-9a-f]{32}\.json', derivation_filename):
        raise ValueError('Invalid FOMC derivation source identity')
    derivation, derivation_sha = read_json(root/derivation_filename)
    if (derivation.get('protocol') != 'fomc-history-derivation-source-v1' or
            set(derivation.get('files', {})) != {'experiments/macro_policy.py', 'polycast/evidence.py', 'polycast/market.py'} or
            result.get('derivation_source_manifest_sha256') != derivation_sha or
            not utc(manifest['recorded_at']) <= utc(derivation['recorded_at']) <= utc(result['facts']['as_of'])):
        raise ValueError('FOMC derivation source/version mismatch')
    for source in derivation['files'].values():
        raw = base64.b64decode(source['content_base64'], validate=True)
        if hashlib.sha256(raw).hexdigest() != source['sha256']: raise ValueError('FOMC derivation source bytes changed')
    table_source, table_source_sha = read_json(root/'rate-table-source-before.json')
    table_bytes = base64.b64decode(table_source['content_base64'], validate=True)
    if (table_source.get('protocol') != 'fomc-rate-table-source-before-v1' or
            table_source.get('filename') != 'experiments/macro_policy.py' or
            hashlib.sha256(table_bytes).hexdigest() != table_source.get('sha256') or
            not utc(manifest['recorded_at']) <= utc(table_source['recorded_at']) <= utc(result['facts']['source_receipts'][-1]['sent_at'])):
        raise ValueError('Rate table source was not frozen before its request')
    facts = result['facts']
    if utc(manifest['recorded_at']) > utc(facts['source_receipts'][0]['sent_at']):
        raise ValueError('FOMC source was not recorded before requests')
    replay_directory(root)
    if (result.get('model_calls') != 0 or result.get('orders_submitted') != 0 or
            result.get('production_database_written') is not False):
        raise ValueError('Unsupported FOMC capture side effects')
    return {'protocol': PROTOCOL, 'directory_name': root.name, 'result_sha256': result_sha,
            'source_manifest_sha256': manifest_sha, 'derivation_source_manifest_sha256': derivation_sha,
            'rate_table_source_manifest_sha256': table_source_sha,
            'raw_receipts_verified': len(facts['source_receipts']),
            'independent_signature_verified': False}


def model_facts(facts: dict) -> dict:
    """Probability-free facts shared by both blind prompts; no baseline output."""
    return {name: facts[name] for name in ('version', 'as_of', 'calendar_url', 'meetings', 'statements',
        'missing_past_statement_dates', 'history_vintage', 'historical_decision_time_availability_proven',
        'unlisted_intermeeting_changes_verified', 'rate_table_url', 'rate_change_rows', 'intermeeting_check_scope')} | {
        'source_first_received_at': facts['source_receipts'][0]['received_at'],
        'source_last_received_at': facts['source_receipts'][-1]['received_at']}


def validate_observation(observation: dict, cutoff: datetime) -> dict:
    e = observation['evidence']; content = json.loads(e['summary'])
    if (e.get('source_id') != SOURCE_ID or e.get('feed_url') != CALENDAR or e.get('url') != CALENDAR or
            e.get('license_scope') != 'official_source' or set(content) != {'schema_version', 'protocol', 'facts'} or
            type(content['schema_version']) is not int or content['schema_version'] != 1 or content['protocol'] != PROTOCOL):
        raise ValueError('FOMC observation source/protocol mismatch')
    facts = content['facts']; at = utc(facts['as_of']); observed = utc(observation['observed_at'])
    if (facts.get('version') != VERSION or facts.get('history_vintage') != 'current_official_archive_received_now' or
            facts.get('historical_decision_time_availability_proven') is not False or
            facts.get('unlisted_intermeeting_changes_verified') is not True or
            not 0 <= (cutoff-at).total_seconds() <= 86400 or not at <= observed <= cutoff or
            not utc(facts['source_first_received_at']) <= utc(facts['source_last_received_at']) <= at or
            not 0 <= (cutoff-utc(facts['source_last_received_at'])).total_seconds() <= 86400 or
            utc(e['published_at']) != at or utc(e['updated_at']) != at):
        raise ValueError('Stale, backdated or future FOMC observation')
    if not facts.get('statements'): raise ValueError('Empty official policy history')
    for statement in facts['statements']:
        if not utc(statement['released_at']) <= utc(statement['received_at']) <= utc(facts['source_last_received_at']):
            raise ValueError('Historical policy statement was unavailable at cutoff')
    descriptor = e.get('capture_descriptor') or {}
    if (descriptor.get('protocol') != PROTOCOL or descriptor.get('independent_signature_verified') is not False or
            type(descriptor.get('raw_receipts_verified')) is not int or descriptor['raw_receipts_verified'] != len(facts['statements'])+2 or
            not re.fullmatch(r'capture-[0-9a-f]{32}', descriptor.get('directory_name', ''))):
        raise ValueError('FOMC observation lacks valid provenance')
    for name in ('result_sha256', 'source_manifest_sha256', 'derivation_source_manifest_sha256', 'rate_table_source_manifest_sha256'):
        if not isinstance(descriptor.get(name), str) or not re.fullmatch(r'[0-9a-f]{64}', descriptor[name]):
            raise ValueError('Invalid FOMC provenance digest')
    return facts


def evidence_item(data_dir: Path, name: str, now: datetime | None = None) -> dict:
    root = capture_path(data_dir, name); descriptor = capture_descriptor(root)
    result, _ = read_json(root/'result.json'); facts = model_facts(result['facts'])
    item = {'source': 'Federal Reserve official policy history', 'source_id': SOURCE_ID,
            'feed_url': CALENDAR, 'license_scope': 'official_source', 'url': CALENDAR, 'origin_story_id': CALENDAR,
            'title': 'FOMC enacted policy ranges and official scheduled meetings',
            'summary': encoded({'schema_version': 1, 'protocol': PROTOCOL, 'facts': facts}),
            'published_at': facts['as_of'], 'updated_at': facts['as_of'], 'capture_descriptor': descriptor}
    now = now or datetime.now(timezone.utc)
    validate_observation({'evidence': item, 'observed_at': now.isoformat()}, now)
    return item


def verify_packet_captures(packet: dict, data_dir: Path) -> dict:
    verified = []; contexts = []
    for group in packet['evidence_groups']:
        for observation in group['members']:
            if observation['evidence'].get('source_id') != SOURCE_ID: continue
            if packet['category'] != 'macro': raise ValueError('FOMC history attached to an unrelated event')
            facts = validate_observation(observation, utc(packet['as_of']))
            descriptor = observation['evidence']['capture_descriptor']
            root = capture_path(data_dir, descriptor['directory_name'])
            if capture_descriptor(root) != descriptor: raise ValueError('FOMC capture changed after observation')
            result, _ = read_json(root/'result.json')
            if model_facts(result['facts']) != facts: raise ValueError('FOMC ledger differs from original receipts')
            contexts.append(contract_context(packet, result['facts']))
            verified.append({'observation_id': observation['observation_id'], **descriptor})
    return {'protocol': PROTOCOL, 'captures': len(verified), 'verified_captures': verified,
            'contract_contexts': contexts, 'network_calls': 0, 'contract_probabilities_validated': False,
            'historical_backfill': False, 'profitability_validated': False}
