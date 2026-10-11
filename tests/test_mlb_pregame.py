from __future__ import annotations
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from experiments.mlb_pregame import (VERSION, build_facts, conditional_runs_baseline, innings_outs,
                                    read_response, replay_directory, schedule_game, season_statistics, url, available_baseline)


class MLBPregameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.start = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.cutoff = self.start + timedelta(minutes=1)
        self.game = {'gamePk': 1, 'gameDate': '2026-10-07T20:00:00Z', 'season': '2026', 'gameType': 'D',
                     'status': {'abstractGameState': 'Preview', 'detailedState': 'Scheduled'},
                     'teams': {'away': {'team': {'id': 114, 'name': 'Away'}, 'probablePitcher': {'id': 11, 'fullName': 'A'}},
                               'home': {'team': {'id': 145, 'name': 'Home'}, 'probablePitcher': {'id': 22, 'fullName': 'B'}}}}
        self.schedule = {'dates': [{'games': [self.game]}]}
        self.write('schedule', url('schedule', gamePk=1, hydrate='team,probablePitcher'), self.schedule, 0)
        for index, lane in enumerate(('away', 'home')):
            ids = self.game['teams'][lane]
            params = {'season': '2026', 'stats': 'season', 'group': 'hitting,pitching', 'gameType': 'R'}
            self.write(lane+'-team', url(f'teams/{ids["team"]["id"]}/stats', **params), self.team_stats(ids['team']['id']), 1+index*2)
            params['group'] = 'pitching'
            self.write(lane+'-pitcher', url(f'people/{ids["probablePitcher"]["id"]}/stats', **params), self.pitcher_stats(ids['probablePitcher']['id'], ids['team']['id']), 2+index*2)

    @staticmethod
    def team_stats(identity):
        return {'stats': [{'group': {'displayName': group}, 'type': {'displayName': 'season'},
                          'splits': [{'season': '2026', 'team': {'id': identity},
                                      'stat': {'gamesPlayed': 162, 'runs': 700} if group == 'hitting' else
                                              {'gamesPlayed': 162, 'runs': 600, 'inningsPitched': '1437.2', 'outs': 4313}}]}
                         for group in ('hitting', 'pitching')]}

    @staticmethod
    def pitcher_stats(identity, team):
        return {'stats': [{'group': {'displayName': 'pitching'}, 'type': {'displayName': 'season'},
                          'splits': [{'season': '2026', 'gameType': 'R', 'team': {'id': team}, 'player': {'id': identity},
                                      'stat': {'gamesPlayed': 14, 'runs': 7, 'inningsPitched': '15.1', 'outs': 46}}]}]}

    def write(self, name, address, body, order):
        raw = json.dumps(body).encode()
        (self.root/(name+'.raw.json')).write_bytes(raw)
        receipt = {'url': address, 'sent_at': (self.start+timedelta(seconds=order*2)).isoformat(),
                   'received_at': (self.start+timedelta(seconds=order*2+1)).isoformat(), 'http_status': 200,
                   'elapsed_ms': 1000, 'raw_filename': name+'.raw.json', 'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_bytes': len(raw)}
        (self.root/(name+'.receipt.json')).write_text(json.dumps(receipt), encoding='utf-8')

    def save_result(self):
        facts = build_facts(self.root, 1, self.cutoff)
        (self.root/'result.json').write_text(json.dumps({'facts': facts, 'conditional_baseline': conditional_runs_baseline(facts)}), encoding='utf-8')
        return facts

    def test_baseball_innings_are_outs_instead_of_decimal_fractions(self):
        self.assertEqual(innings_outs('15.1'), 46)
        self.assertEqual(innings_outs('1437.2'), 4313)
        for bad in ('15.3', '15.10', '-1.0', 15.1):
            with self.assertRaises(ValueError): innings_outs(bad)

    def test_full_five_response_replay_is_identical_without_network(self):
        facts = self.save_result()
        self.assertEqual(facts['version'], VERSION)
        self.assertEqual(facts['target_game_type'], 'D')
        self.assertEqual(facts['statistics_game_type'], 'R')
        self.assertEqual(facts['teams']['away']['regular_season_team_statistics']['hitting'][0]['game_type_basis'], 'request_selector_only')
        with patch('experiments.mlb_pregame.fetch', side_effect=AssertionError('Network must stay unused')):
            replay = replay_directory(self.root)
        self.assertEqual(replay['status'], 'replayed_identical')
        self.assertFalse(replay['contract_forecast_eligible'])

    def test_late_source_receipt_cannot_be_part_of_an_earlier_frozen_input(self):
        path = self.root/'home-pitcher.receipt.json'
        receipt = json.loads(path.read_text())
        receipt['received_at'] = (self.cutoff+timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'Late or reversed'): build_facts(self.root, 1, self.cutoff)

    def test_byte_tamper_and_recomputed_hash_with_wrong_selector_are_rejected(self):
        path = self.root/'away-team.raw.json'
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'bytes changed'): build_facts(self.root, 1, self.cutoff)
        wrong = self.team_stats(114)
        wrong['stats'][0]['splits'][0]['gameType'] = 'D'
        self.write('away-team', url('teams/114/stats', season='2026', stats='season', group='hitting,pitching', gameType='R'), wrong, 1)
        with self.assertRaisesRegex(ValueError, 'season/game type/entity mismatch'): build_facts(self.root, 1, self.cutoff)

    def test_wrong_season_entity_empty_stats_and_outs_mismatch_are_rejected(self):
        for change in ('season', 'entity', 'empty', 'outs'):
            body = self.team_stats(114)
            if change == 'season': body['stats'][0]['splits'][0]['season'] = '2025'
            elif change == 'entity': body['stats'][0]['splits'][0]['team']['id'] = 145
            elif change == 'empty': body['stats'][0]['splits'] = []
            else: body['stats'][1]['splits'][0]['stat']['outs'] = 4312
            with self.assertRaises(ValueError): season_statistics(body, season='2026', identity=114, entity='team', requested_game_type='R')

    def test_traded_pitcher_splits_are_preserved_without_double_counting(self):
        body = self.pitcher_stats(11, 114)
        second = copy.deepcopy(body['stats'][0]['splits'][0]); second['team']['id'] = 145
        body['stats'][0]['splits'].append(second)
        stats = season_statistics(body, season='2026', identity=11, entity='player', requested_game_type='R')
        self.assertEqual(len(stats['pitching']), 2)
        self.assertEqual([row['team']['id'] for row in stats['pitching']], [114, 145])

    def test_started_game_or_missing_starter_does_not_receive_a_pregame_prediction(self):
        for change in ('started', 'status', 'starter'):
            body = copy.deepcopy(self.schedule)
            game = body['dates'][0]['games'][0]
            if change == 'started': game['gameDate'] = '2026-10-07T11:00:00Z'
            elif change == 'status': game['status']['abstractGameState'] = 'Live'
            else: del game['teams']['home']['probablePitcher']
            with self.assertRaises(ValueError): schedule_game(body, 1, self.cutoff)

    def test_independent_known_odds_and_no_invented_half_or_payout(self):
        facts = build_facts(self.root, 1, self.cutoff)
        for lane, runs_for, runs_against in (('away', 100, 50), ('home', 50, 100)):
            stats = facts['teams'][lane]['regular_season_team_statistics']
            stats['hitting'][0]['stat'].update(gamesPlayed=50, runs=runs_for)
            stats['pitching'][0]['stat'].update(gamesPlayed=50, runs=runs_against)
        result = conditional_runs_baseline(facts)
        # Independent matchup odds: (2/1)^2 / (1/2)^2 = 16, hence 16/17.
        self.assertAlmostEqual(result['away_win_probability_given_decisive_game'], 16/17)
        self.assertIsNone(result['contract_state_probabilities'])
        self.assertIsNone(result['contract_mean_payout'])
        self.assertFalse(result['production_eligible'])
        facts['teams']['away']['regular_season_team_statistics']['hitting'][0]['stat']['gamesPlayed'] = 20
        with self.assertRaisesRegex(ValueError, 'Too few'): conditional_runs_baseline(facts)

    def test_rewritten_saved_result_fails_replay_against_original_sources(self):
        self.save_result()
        path = self.root/'result.json'
        result = json.loads(path.read_text()); result['facts']['teams']['away']['team']['name'] = 'Other team'
        path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'do not match'): replay_directory(self.root)

    def test_changed_request_order_is_rejected(self):
        path = self.root/'away-team.receipt.json'
        receipt = json.loads(path.read_text()); receipt['sent_at'] = self.start.isoformat()
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'request order'): build_facts(self.root, 1, self.cutoff)

    def test_insufficient_baseline_sample_keeps_official_facts_available(self):
        facts = build_facts(self.root, 1, self.cutoff)
        facts['teams']['away']['regular_season_team_statistics']['hitting'][0]['stat']['gamesPlayed'] = 10
        result = available_baseline(facts)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['contract_mean_payout'])
        self.assertFalse(result['production_eligible'])


if __name__ == '__main__':
    unittest.main()
