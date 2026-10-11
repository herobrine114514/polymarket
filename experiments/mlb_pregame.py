"""Bounded public MLB pregame statistics capture and offline replay.

Five GETs at most, no model/API credentials, production writes or orders. Full
regular-season statistics are kept separate from the target game's game type.
The fixed runs baseline is a conditional intermediate, not a contract forecast:
it has no calibrated tie/cancellation mass and must not enter the trading path.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VERSION = 'mlb-pregame-official-stats-v1'
ORIGIN = 'https://statsapi.mlb.com/api/v1/'
MAX_RESPONSE_BYTES = 2 * 1024 ** 2


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Naive MLB receipt/game clock')
    return parsed.astimezone(timezone.utc)


def positive_id(value) -> int:
    if type(value) is not int or value < 1:
        raise ValueError('Invalid MLB identity')
    return value


def url(path: str, **params) -> str:
    return ORIGIN + path + '?' + urllib.parse.urlencode(params)


def innings_outs(value: str) -> int:
    # Baseball .1/.2 denotes outs, not tenths of an inning.
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.[012]', value):
        raise ValueError('Invalid baseball innings notation')
    innings, outs = value.split('.')
    return int(innings) * 3 + int(outs)


def checked_stat(stat: dict) -> dict:
    if not isinstance(stat, dict):
        raise ValueError('Missing MLB statistics')
    result = dict(stat)
    for key in ('gamesPlayed', 'runs'):
        if type(stat.get(key)) is not int or stat[key] < 0:
            raise ValueError('Invalid MLB games/runs')
    if 'inningsPitched' in stat:
        outs = innings_outs(stat['inningsPitched'])
        if type(stat.get('outs')) is not int or stat['outs'] != outs:
            raise ValueError('Pitching innings and outs disagree')
        result['normalized_outs'] = outs
    return result


def schedule_game(body: dict, game_id: int, as_of: datetime) -> dict:
    positive_id(game_id)
    games = [game for date in body['dates'] for game in date['games'] if game.get('gamePk') == game_id]
    if len(games) != 1:
        raise ValueError('Missing/ambiguous official game')
    game = games[0]
    start = utc(game['gameDate'])
    if (start <= as_of or game['status'].get('abstractGameState') != 'Preview' or
            game['status'].get('detailedState') not in ('Scheduled', 'Pre-Game')):
        raise ValueError('Game is not future pregame at the capture cutoff')
    season = game['season']
    if not isinstance(season, str) or not re.fullmatch(r'\d{4}', season) or int(season) != as_of.year:
        raise ValueError('Statistics require the current official season')
    if game.get('gameType') not in ('R', 'D', 'F', 'L', 'W'):
        raise ValueError('Unsupported MLB target game type')
    for lane in ('away', 'home'):
        positive_id(game['teams'][lane]['team']['id'])
        pitcher = game['teams'][lane].get('probablePitcher')
        if not pitcher:
            raise ValueError('Official probable starter is missing; do not invent one')
        positive_id(pitcher['id'])
    if game['teams']['away']['team']['id'] == game['teams']['home']['team']['id']:
        raise ValueError('Duplicate game teams')
    return game


def season_statistics(body: dict, *, season: str, identity: int, entity: str, requested_game_type: str) -> dict:
    if requested_game_type != 'R' or entity not in ('team', 'player'):
        raise ValueError('Unsupported statistics selector/entity')
    expected = {'hitting', 'pitching'} if entity == 'team' else {'pitching'}
    found = {}
    for group in body['stats']:
        name = group['group']['displayName']
        if name not in expected:
            continue
        if name in found or group['type']['displayName'] != 'season':
            raise ValueError('Duplicate/wrong MLB statistics group')
        splits = group.get('splits')
        if not isinstance(splits, list) or not splits:
            raise ValueError('Official season statistics are empty')
        normalized = []
        for split in splits:
            entity_key = 'team' if entity == 'team' else 'player'
            if (split.get('season') != season or split.get('gameType', requested_game_type) != 'R' or
                    split.get(entity_key, {}).get('id') != identity):
                raise ValueError('Statistics season/game type/entity mismatch')
            normalized.append({'team': split.get('team'), 'player': split.get('player'),
                               'game_type_basis': 'response_and_request' if 'gameType' in split else 'request_selector_only',
                               'stat': checked_stat(split['stat'])})
        if entity == 'team' and len(normalized) != 1:
            raise ValueError('Ambiguous team aggregate; do not sum overlapping splits')
        # Traded pitchers may have several team splits; retain each explicitly.
        found[name] = normalized
    if set(found) != expected:
        raise ValueError('Missing required season statistics group')
    return found


def conditional_runs_baseline(facts: dict) -> dict:
    """Fixed exponent=2, zero home adjustment; no fitted gains claimed."""
    strengths = {}
    for lane in ('away', 'home'):
        statistics = facts['teams'][lane]['regular_season_team_statistics']
        hitting = statistics['hitting'][0]['stat']
        pitching = statistics['pitching'][0]['stat']
        if hitting['gamesPlayed'] < 30 or pitching['gamesPlayed'] < 30:
            raise ValueError('Too few team games for the fixed baseline')
        # Rates avoid silently assuming the two season counts are identical.
        scored = hitting['runs'] / hitting['gamesPlayed']
        allowed = pitching['runs'] / pitching['gamesPlayed']
        if scored <= 0 or allowed <= 0:
            raise ValueError('Baseline requires positive run rates')
        strengths[lane] = scored ** 2 / (scored ** 2 + allowed ** 2)
    a, b = strengths['away'], strengths['home']
    probability = a * (1 - b) / (a * (1 - b) + (1 - a) * b)
    return {'version': 'regular-season-runs-log5-conditional-v1',
            'status': 'conditional_intermediate_only', 'team_strength': strengths,
            'away_win_probability_given_decisive_game': probability,
            'home_win_probability_given_decisive_game': 1 - probability,
            'contract_state_probabilities': None, 'contract_mean_payout': None,
            'production_eligible': False, 'profitability_validated': False,
            'limitations': ['No calibrated HALF/cancellation probability',
                           'No home, starter, bullpen, lineup or opponent-strength adjustment',
                           'No out-of-sample performance measured; not a replacement for a full contract-state predictor']}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(root: Path, name: str, address: str) -> dict:
    if not address.startswith(ORIGIN):
        raise ValueError('Only the official public MLB endpoint is allowed')
    sent = datetime.now(timezone.utc)
    monotonic = time.monotonic_ns()
    receipt = {'url': address, 'sent_at': sent.isoformat()}
    try:
        request = urllib.request.Request(address, headers={'Accept': 'application/json', 'User-Agent': VERSION})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            receipt.update(http_status=response.status, response_date=response.headers.get('Date'))
        receipt.update(received_at=datetime.now(timezone.utc).isoformat(), elapsed_ms=(time.monotonic_ns()-monotonic)/1e6,
                       raw_filename=name+'.raw.json', raw_sha256=hashlib.sha256(raw).hexdigest(), raw_bytes=len(raw))
        with (root / receipt['raw_filename']).open('xb') as stream:
            stream.write(raw)
        if len(raw) > MAX_RESPONSE_BYTES or receipt['http_status'] != 200:
            raise ValueError('Oversized/non-success official MLB response')
        json.loads(raw)
    except Exception as exc:
        receipt['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        with (root / (name+'.receipt.json')).open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return receipt


def read_response(root: Path, name: str, expected_url: str, cutoff: datetime | None = None) -> tuple[dict, dict]:
    receipt_path = root / (name+'.receipt.json')
    if receipt_path.is_symlink() or receipt_path.resolve().parent != root.resolve():
        raise ValueError('MLB receipt leaves capture directory')
    with receipt_path.open('rb') as stream:
        receipt_raw = stream.read(64 * 1024 + 1)
    if len(receipt_raw) > 64 * 1024:
        raise ValueError('Oversized MLB receipt')
    receipt = json.loads(receipt_raw)
    if (receipt.get('error') or receipt.get('url') != expected_url or receipt.get('http_status') != 200 or
            receipt.get('raw_filename') != name+'.raw.json'):
        raise ValueError('Official MLB response receipt does not match the fixed request')
    path = root / receipt['raw_filename']
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError('MLB raw response leaves capture directory')
    with path.open('rb') as stream:
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
    if (len(raw) > MAX_RESPONSE_BYTES or len(raw) != receipt['raw_bytes'] or
            hashlib.sha256(raw).hexdigest() != receipt['raw_sha256']):
        raise ValueError('Official MLB response bytes changed')
    sent, received = utc(receipt['sent_at']), utc(receipt['received_at'])
    if received < sent or cutoff is not None and received > cutoff:
        raise ValueError('Late or reversed official MLB receipt clock')
    return json.loads(raw), receipt


def build_facts(root: Path, game_id: int, as_of: datetime) -> dict:
    if as_of.tzinfo is None:
        raise ValueError('Naive MLB capture cutoff')
    schedule_url = url('schedule', gamePk=positive_id(game_id), hydrate='team,probablePitcher')
    body, receipt = read_response(root, 'schedule', schedule_url, as_of)
    game = schedule_game(body, game_id, as_of)
    facts = {'schema_version': 1, 'version': VERSION, 'as_of': as_of.isoformat(),
             'game_id': game_id, 'scheduled_at': game['gameDate'], 'season': game['season'],
             'target_game_type': game['gameType'], 'statistics_game_type': 'R',
             'status_at_schedule_receipt': game['status'], 'teams': {}, 'source_receipts': [receipt],
             'independent_signature_verified': False}
    for lane in ('away', 'home'):
        team = game['teams'][lane]['team']
        pitcher = game['teams'][lane]['probablePitcher']
        params = {'season': game['season'], 'stats': 'season', 'group': 'hitting,pitching', 'gameType': 'R'}
        team_body, team_receipt = read_response(root, lane+'-team', url(f'teams/{team["id"]}/stats', **params), as_of)
        params['group'] = 'pitching'
        pitcher_body, pitcher_receipt = read_response(root, lane+'-pitcher', url(f'people/{pitcher["id"]}/stats', **params), as_of)
        facts['teams'][lane] = {'team': {'id': team['id'], 'name': team['name']},
                               'probable_pitcher': {'id': pitcher['id'], 'name': pitcher['fullName']},
                               'regular_season_team_statistics': season_statistics(team_body, season=game['season'], identity=team['id'], entity='team', requested_game_type='R'),
                               'regular_season_pitcher_splits': season_statistics(pitcher_body, season=game['season'], identity=pitcher['id'], entity='player', requested_game_type='R')}
        facts['source_receipts'].extend((team_receipt, pitcher_receipt))
    for earlier, later in zip(facts['source_receipts'], facts['source_receipts'][1:]):
        if utc(earlier['received_at']) > utc(later['sent_at']):
            raise ValueError('Official MLB receipt request order changed')
    return facts


def replay_directory(root: Path) -> dict:
    """Recompute the saved same-cutoff facts/baseline with zero network calls."""
    result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    facts = result['facts']
    rebuilt = build_facts(root, facts['game_id'], utc(facts['as_of']))
    baseline = available_baseline(rebuilt)
    if rebuilt != facts or baseline != result['conditional_baseline']:
        raise ValueError('Saved MLB facts/baseline do not match verified original receipts')
    return {'status': 'replayed_identical', 'version': VERSION, 'game_id': facts['game_id'],
            'responses_verified': 5, 'network_calls': 0, 'contract_forecast_eligible': False,
            'model_calls': 0, 'orders_submitted': 0}


def available_baseline(facts: dict) -> dict:
    try:
        return conditional_runs_baseline(facts)
    except ValueError as exc:
        # A baseline's sample gate must not discard otherwise valid official facts.
        return {'version': 'regular-season-runs-log5-conditional-v1', 'status': 'unavailable',
                'reason': str(exc), 'contract_state_probabilities': None, 'contract_mean_payout': None,
                'production_eligible': False, 'profitability_validated': False}


def collect(root: Path, game_id: int) -> dict:
    positive_id(game_id)
    root.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).read_bytes()
    source_manifest = {'protocol': 'mlb-pregame-capture-source-v1', 'recorded_at': datetime.now(timezone.utc).isoformat(),
                       'filename': 'experiments/mlb_pregame.py', 'sha256': hashlib.sha256(source).hexdigest(),
                       'content_base64': base64.b64encode(source).decode('ascii')}
    with (root / 'source-before.json').open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(source_manifest, indent=2)+'\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())
    schedule_url = url('schedule', gamePk=game_id, hydrate='team,probablePitcher')
    fetch(root, 'schedule', schedule_url)
    body, receipt = read_response(root, 'schedule', schedule_url)
    game = schedule_game(body, game_id, utc(receipt['received_at']))
    for lane in ('away', 'home'):
        ids = game['teams'][lane]
        params = {'season': game['season'], 'stats': 'season', 'group': 'hitting,pitching', 'gameType': 'R'}
        fetch(root, lane+'-team', url(f'teams/{ids["team"]["id"]}/stats', **params))
        params['group'] = 'pitching'
        fetch(root, lane+'-pitcher', url(f'people/{ids["probablePitcher"]["id"]}/stats', **params))
    facts = build_facts(root, game_id, datetime.now(timezone.utc))
    if Path(__file__).read_bytes() != source:
        raise ValueError('MLB capture source changed during requests')
    result = {'facts': facts, 'conditional_baseline': available_baseline(facts),
              'capture_source_sha256': source_manifest['sha256'],
              'model_calls': 0, 'orders_submitted': 0, 'production_database_written': False}
    with (root / 'result.json').open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--game-id', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.output, args.game_id)
    print(json.dumps({'game_id': result['facts']['game_id'], 'as_of': result['facts']['as_of'],
                      'responses': len(result['facts']['source_receipts']), 'conditional_baseline': result['conditional_baseline'],
                      'model_calls': 0, 'orders_submitted': 0}, ensure_ascii=False))
