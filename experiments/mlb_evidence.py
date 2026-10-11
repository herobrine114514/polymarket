"""Verified official pregame facts for the append-only event research ledger.

The model receives facts from five public responses, with no conditional model
output. Capture paths and source code stay in provenance, outside blind views.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from experiments.mlb_pregame import (VERSION, positive_id, replay_directory,
                                    url, utc, checked_stat)

SOURCE_ID = 'mlb_pregame_stats'
FEED_URL = 'https://statsapi.mlb.com/api/v1/schedule'
PROTOCOL = 'mlb-pregame-evidence-v1'
DIRECTORY = 'mlb-pregame-captures'
NAMES = ('schedule', 'away-team', 'away-pitcher', 'home-team', 'home-pitcher')
MAX_BYTES = 12 * 1024 ** 2


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest_file(path: Path) -> tuple[dict, str]:
    if path.is_symlink():
        raise ValueError('MLB capture cannot follow a symlink')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Oversized MLB evidence file')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def capture_path(data_dir: Path, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r'capture-[0-9a-f]{32}', name):
        raise ValueError('Invalid MLB capture directory identity')
    root = (data_dir / DIRECTORY).resolve()
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('MLB capture leaves its named data directory')
    return path


def capture_descriptor(root: Path) -> dict:
    manifest, source_digest = digest_file(root/'source-before.json')
    result, result_digest = digest_file(root/'result.json')
    source = base64.b64decode(manifest['content_base64'], validate=True)
    if (manifest.get('protocol') != 'mlb-pregame-capture-source-v1' or
            manifest.get('filename') != 'experiments/mlb_pregame.py' or
            hashlib.sha256(source).hexdigest() != manifest.get('sha256') or
            result.get('capture_source_sha256') != manifest['sha256']):
        raise ValueError('MLB capture source manifest mismatch')
    if utc(manifest['recorded_at']) > utc(result['facts']['source_receipts'][0]['sent_at']):
        raise ValueError('MLB source manifest was not recorded before the first request')
    replay_directory(root)
    if result.get('orders_submitted') != 0 or result.get('model_calls') != 0 or result.get('production_database_written') is not False:
        raise ValueError('Unsupported MLB capture side effects')
    return {'protocol': PROTOCOL, 'directory_name': root.name, 'result_sha256': result_digest,
            'source_manifest_sha256': source_digest, 'capture_source_sha256': manifest['sha256'],
            'raw_receipts_verified': 5, 'independent_signature_verified': False}


def validate_observation(observation: dict, cutoff: datetime, market: dict | None = None) -> dict:
    e = observation['evidence']
    if e.get('source_id') != SOURCE_ID or e.get('feed_url') != FEED_URL or e.get('license_scope') != 'official_source':
        raise ValueError('MLB statistics source identity mismatch')
    content = json.loads(e['summary'])
    if set(content) != {'schema_version', 'protocol', 'facts'} or type(content['schema_version']) is not int or content['schema_version'] != 1 or content['protocol'] != PROTOCOL:
        raise ValueError('Unsupported MLB statistics evidence protocol')
    facts = content['facts']
    game_id = positive_id(facts['game_id']); at = utc(facts['as_of'])
    start = utc(facts['scheduled_at'])
    if (facts.get('version') != VERSION or facts.get('statistics_game_type') != 'R' or
            facts.get('independent_signature_verified') is not False or
            not 0 <= (cutoff-at).total_seconds() <= 120 or start <= cutoff or
            e.get('origin_story_id') != str(game_id) or utc(e['published_at']) != at or utc(e['updated_at']) != at or
            not at <= utc(observation['observed_at']) <= cutoff):
        raise ValueError('Stale, future or mismatched MLB facts')
    descriptor = e.get('capture_descriptor') or {}
    if (descriptor.get('protocol') != PROTOCOL or type(descriptor.get('raw_receipts_verified')) is not int or descriptor.get('raw_receipts_verified') != 5 or
            descriptor.get('independent_signature_verified') is not False):
        raise ValueError('Missing MLB receipt provenance')
    if not isinstance(descriptor.get('directory_name'), str) or not re.fullmatch(r'capture-[0-9a-f]{32}', descriptor['directory_name']):
        raise ValueError('Invalid MLB capture directory identity')
    for name in ('result_sha256', 'source_manifest_sha256', 'capture_source_sha256'):
        if not isinstance(descriptor.get(name), str) or not re.fullmatch(r'[0-9a-f]{64}', descriptor[name]):
            raise ValueError('Invalid MLB capture digest')
    expected = [url('schedule', gamePk=game_id, hydrate='team,probablePitcher')]
    for lane in ('away', 'home'):
        team = facts['teams'][lane]; team_id = positive_id(team['team']['id']); pitcher_id = positive_id(team['probable_pitcher']['id'])
        params = {'season': facts['season'], 'stats': 'season', 'group': 'hitting,pitching', 'gameType': 'R'}
        expected.append(url(f'teams/{team_id}/stats', **params)); params['group'] = 'pitching'
        expected.append(url(f'people/{pitcher_id}/stats', **params))
        for group, splits in team['regular_season_team_statistics'].items():
            if group not in ('hitting', 'pitching') or len(splits) != 1 or splits[0]['team']['id'] != team_id:
                raise ValueError('MLB team statistics identity mismatch')
            checked_stat(splits[0]['stat'])
        if set(team['regular_season_team_statistics']) != {'hitting', 'pitching'}:
            raise ValueError('Incomplete MLB season statistics')
        pitchers = team['regular_season_pitcher_splits']
        if set(pitchers) != {'pitching'} or not pitchers['pitching']:
            raise ValueError('Incomplete MLB starter statistics')
        for split in pitchers['pitching']:
            if split['player']['id'] != pitcher_id: raise ValueError('MLB starter identity mismatch')
            checked_stat(split['stat'])
    receipts = facts['source_receipts']
    if len(receipts) != 5 or [r['url'] for r in receipts] != expected or e.get('url') != expected[0]:
        raise ValueError('MLB receipt URLs do not match the five fixed requests')
    for name, receipt in zip(NAMES, receipts):
        if (receipt.get('http_status') != 200 or receipt.get('raw_filename') != name+'.raw.json' or
                not utc(receipt['sent_at']) <= utc(receipt['received_at']) <= at):
            raise ValueError('MLB receipt was unavailable at the fact cutoff')
    if market is not None:
        if (market.get('category') != 'daily' or market.get('source_ref') != str(game_id) or
                utc(market['end_at']) != start or
                market.get('yes_label') != facts['teams']['away']['team']['name'] or
                market.get('no_label') != facts['teams']['home']['team']['name']):
            raise ValueError('Official MLB capture differs from the frozen contract')
    return facts


def evidence_item(data_dir: Path, capture_name: str, market: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    root = capture_path(data_dir, capture_name)
    descriptor = capture_descriptor(root)
    result, _ = digest_file(root/'result.json'); facts = result['facts']
    item = {'source': 'MLB Stats API full pregame statistics', 'source_id': SOURCE_ID,
            'feed_url': FEED_URL, 'license_scope': 'official_source',
            'url': facts['source_receipts'][0]['url'], 'origin_story_id': str(facts['game_id']),
            'title': f"MLB official pregame season statistics for {market['event_title']}",
            'summary': encoded({'schema_version': 1, 'protocol': PROTOCOL, 'facts': facts}),
            'published_at': facts['as_of'], 'updated_at': facts['as_of'], 'capture_descriptor': descriptor}
    validate_observation({'evidence': item, 'observed_at': now.isoformat()}, now, market)
    return item


def verify_packet_captures(packet: dict, data_dir: Path) -> dict:
    verified = []
    for group in packet['evidence_groups']:
        for observation in group['members']:
            e = observation['evidence']
            if e.get('source_id') != SOURCE_ID: continue
            if packet.get('category') != 'daily' or len(packet['contract_snapshot']) != 1:
                raise ValueError('MLB statistics attached to an unrelated event')
            facts = validate_observation(observation, utc(packet['as_of']), packet['contract_snapshot'][0])
            descriptor = e['capture_descriptor']
            root = capture_path(data_dir, descriptor['directory_name'])
            if capture_descriptor(root) != descriptor:
                raise ValueError('MLB source capture changed after ledger observation')
            result, _ = digest_file(root/'result.json')
            if result['facts'] != facts:
                raise ValueError('MLB ledger facts differ from verified public responses')
            verified.append({'observation_id': observation['observation_id'], **descriptor})
    return {'protocol': PROTOCOL, 'verified_captures': verified, 'captures': len(verified),
            'network_calls': 0, 'independent_signature_verified': False, 'profitability_validated': False}
