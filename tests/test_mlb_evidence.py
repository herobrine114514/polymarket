from __future__ import annotations
import base64
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from experiments.mlb_evidence import (SOURCE_ID, FEED_URL, DIRECTORY, evidence_item, verify_packet_captures,
                                     capture_descriptor, validate_observation)
from experiments.mlb_pregame import build_facts, available_baseline
from experiments.event_study import collect_mlb_statistics, run
from polycast.config import Config
from polycast.event_research import (make_packet, observations_at, model_view, control_payload,
                                    freeze_packet, verify_packet_observations)
from polycast.store import database, insert_evidence, upsert_markets, _upsert_source, withdraw_evidence
from tests import test_mlb_pregame as mlb_fixture
from tests.test_event_research import fed_markets


class MLBEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.config = Config(data_dir=Path(temporary.name))
        self.name = 'capture-' + 'a'*32
        self.root = self.config.data_dir / DIRECTORY / self.name
        helper = mlb_fixture.MLBPregameTests('test_full_five_response_replay_is_identical_without_network')
        helper.setUp(); self.addCleanup(helper.doCleanups)
        shutil.copytree(helper.root, self.root)
        self.base = datetime.now(timezone.utc)-timedelta(seconds=20)
        self.fact_at = self.base+timedelta(seconds=10)
        self.cutoff = self.base+timedelta(seconds=15)
        self.end = (self.base+timedelta(hours=4)).replace(second=0, microsecond=0)
        for index, path in enumerate(sorted(self.root.glob('*.receipt.json'))):
            # Use the recipe's fixed request ordering rather than filename order.
            order = ['schedule', 'away-team', 'away-pitcher', 'home-team', 'home-pitcher'].index(path.name.removesuffix('.receipt.json'))
            receipt = json.loads(path.read_text())
            receipt.update(sent_at=(self.base+timedelta(seconds=order*2)).isoformat(),
                           received_at=(self.base+timedelta(seconds=order*2+1)).isoformat())
            if path.name == 'schedule.receipt.json':
                raw_path = self.root/receipt['raw_filename']; body = json.loads(raw_path.read_bytes())
                body['dates'][0]['games'][0].update(gameDate=self.end.isoformat(), season=str(self.base.year))
                raw = json.dumps(body).encode(); raw_path.write_bytes(raw)
                receipt.update(raw_sha256=hashlib.sha256(raw).hexdigest(), raw_bytes=len(raw))
            path.write_text(json.dumps(receipt))
        facts = build_facts(self.root, 1, self.fact_at)
        source = Path('experiments/mlb_pregame.py').read_bytes(); source_sha = hashlib.sha256(source).hexdigest()
        manifest = {'protocol': 'mlb-pregame-capture-source-v1', 'recorded_at': (self.base-timedelta(seconds=1)).isoformat(),
                    'filename': 'experiments/mlb_pregame.py', 'sha256': source_sha, 'content_base64': base64.b64encode(source).decode()}
        (self.root/'source-before.json').write_text(json.dumps(manifest))
        result = {'facts': facts, 'conditional_baseline': available_baseline(facts), 'capture_source_sha256': source_sha,
                  'model_calls': 0, 'orders_submitted': 0, 'production_database_written': False}
        (self.root/'result.json').write_text(json.dumps(result))
        et = self.end.astimezone(ZoneInfo('America/New_York'))
        rule = f"This game is scheduled for {et.strftime('%B')} {et.day}, {et.year} at {et.strftime('%I:%M %p')} ET. "
        self.market = {**fed_markets(self.base)[0], 'event_id': 'daily1', 'event_slug': 'mlb-fixture',
                       'category': 'daily', 'event_title': 'Away vs Home', 'question': 'Away vs Home',
                       'source_ref': '1', 'yes_label': 'Away', 'no_label': 'Home', 'end_at': self.end.isoformat(),
                       'description': rule+'Away wins pays YES; Home wins pays NO. Tied or entirely cancelled with no makeup resolves 50-50.'}
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            for source, name in ((SOURCE_ID, 'MLB Stats API full pregame statistics'), ('mlb_schedule', 'MLB Stats API')):
                _upsert_source(conn, source, name, 'official', FEED_URL, 'official_source', FEED_URL, '', 1)
        self.item = evidence_item(self.config.data_dir, self.name, self.market, self.cutoff)

    def add(self, conn, item, time=None):
        observed = time or self.cutoff
        with patch('polycast.store.datetime') as dt, patch('polycast.store.now_iso', return_value=observed.isoformat()):
            dt.now.return_value = observed
            insert_evidence(conn, [item], activate_current=item['source_id'] != SOURCE_ID)

    def short_item(self):
        return {**{key: self.item[key] for key in ('url', 'origin_story_id', 'feed_url', 'license_scope', 'published_at', 'updated_at')},
                'source': 'MLB Stats API', 'source_id': 'mlb_schedule', 'title': 'Official MLB pregame schedule',
                'summary': 'Game 1, Away vs Home. A short schedule summary without full season statistics.'}

    def packet(self, conn):
        obs, audit = observations_at(conn, self.cutoff.isoformat())
        return make_packet([self.market], obs, self.cutoff.isoformat(), audit)

    def test_short_schedule_update_preserves_full_statistics_and_groups_one_provider(self):
        with database(self.config.database) as conn:
            self.add(conn, self.item, self.cutoff-timedelta(seconds=1))
            self.add(conn, self.short_item())
            packet = self.packet(conn)
            self.assertEqual(len(packet['evidence_groups']), 1)
            self.assertEqual(len(packet['evidence_groups'][0]['members']), 2)
            self.assertEqual(verify_packet_observations(conn, packet)['observations'], 2)
        proof = verify_packet_captures(packet, self.config.data_dir)
        self.assertEqual(proof['captures'], 1)
        self.assertEqual(proof['network_calls'], 0)
        view = model_view(packet); control = control_payload(packet)
        self.assertEqual({v['summary'] for g in view['evidence_groups'] for v in g['variants']},
                         {v['summary'] for v in control['evidence']})
        self.assertNotIn('conditional_baseline', json.dumps(view))
        self.assertNotIn('directory_name', json.dumps(view))
        self.assertNotIn('capture_source_sha256', json.dumps(control))

    def test_research_statistics_do_not_replace_the_live_daily_schedule_view(self):
        with database(self.config.database) as conn:
            self.add(conn, self.short_item(), self.cutoff-timedelta(seconds=1))
            before = dict(conn.execute('SELECT * FROM evidence_current').fetchone())
            self.add(conn, self.item)
            self.assertEqual(dict(conn.execute('SELECT * FROM evidence_current').fetchone()), before)
            self.assertEqual(len(self.packet(conn)['evidence_groups'][0]['members']), 2)

    def test_fractional_second_capture_is_not_backdated_by_first_seen_rounding(self):
        observed = self.fact_at+timedelta(microseconds=1)
        with database(self.config.database) as conn:
            with patch('polycast.store.datetime') as dt, patch('polycast.store.now_iso', return_value=observed.replace(microsecond=0).isoformat()):
                dt.now.return_value = observed
                insert_evidence(conn, [self.item], activate_current=False)
            row = conn.execute('SELECT first_seen_at FROM evidence WHERE source_id=?', (SOURCE_ID,)).fetchone()
            self.assertEqual(row['first_seen_at'], observed.isoformat(timespec='microseconds'))
            self.assertEqual(len(observations_at(conn, self.cutoff.isoformat())[0]), 1)

    def test_source_byte_tamper_prevents_reusing_ledger_credit(self):
        with database(self.config.database) as conn:
            self.add(conn, self.item); packet = self.packet(conn)
        path = self.root/'home-pitcher.raw.json'; path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError, 'bytes changed'): verify_packet_captures(packet, self.config.data_dir)

    def test_self_consistent_modified_ledger_facts_still_must_match_original_responses(self):
        modified = copy.deepcopy(self.item)
        content = json.loads(modified['summary'])
        content['facts']['teams']['away']['regular_season_team_statistics']['hitting'][0]['stat']['runs'] += 10
        modified['summary'] = json.dumps(content)
        with database(self.config.database) as conn:
            self.add(conn, modified); packet = self.packet(conn)
        with self.assertRaisesRegex(ValueError, 'ledger facts differ'): verify_packet_captures(packet, self.config.data_dir)

    def test_capture_directory_traversal_and_contract_mismatch_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'directory identity'): evidence_item(self.config.data_dir, '../capture', self.market, self.cutoff)
        for change in ({'yes_label': 'Other'}, {'end_at': (self.end+timedelta(hours=1)).isoformat()}, {'source_ref': '2'}):
            with self.assertRaisesRegex(ValueError, 'frozen contract'):
                evidence_item(self.config.data_dir, self.name, {**self.market, **change}, self.cutoff)

    def test_source_manifest_after_request_cannot_prove_prefrozen_capture(self):
        path = self.root/'source-before.json'; manifest = json.loads(path.read_text())
        manifest['recorded_at'] = (self.base+timedelta(seconds=1)).isoformat(); path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'before the first'): capture_descriptor(self.root)

    def test_old_capture_is_not_made_fresh_by_a_new_ledger_observation(self):
        with self.assertRaisesRegex(ValueError, 'Stale'):
            validate_observation({'evidence': self.item, 'observed_at': (self.cutoff+timedelta(minutes=3)).isoformat()},
                                 self.cutoff+timedelta(minutes=3), self.market)

    def test_withdrawal_applies_to_both_views_of_the_same_official_url(self):
        with database(self.config.database) as conn:
            self.add(conn, self.item); self.add(conn, self.short_item())
            with patch('polycast.store.now_iso', return_value=self.cutoff.isoformat()):
                withdraw_evidence(conn, self.item['url'], 'https://statsapi.mlb.com/correction', 'Withdraw invalid official snapshot')
            obs, audit = observations_at(conn, self.cutoff.isoformat())
            self.assertEqual(obs, [])
            self.assertEqual(len(audit['exclusions']), 2)

    def test_invalid_latest_statistics_never_fall_back_to_an_older_statistics_version(self):
        bad = copy.deepcopy(self.item); bad['summary'] = '{"invalid": true}'
        with database(self.config.database) as conn:
            self.add(conn, self.item, self.cutoff-timedelta(seconds=1))
            self.add(conn, self.short_item(), self.cutoff-timedelta(seconds=1))
            self.add(conn, bad)
            obs, audit = observations_at(conn, self.cutoff.isoformat())
            self.assertEqual([o['evidence']['source_id'] for o in obs], ['mlb_schedule'])
            self.assertEqual(len(audit['exclusions']), 1)

    def test_daily_pilot_requires_statistics_before_reserving_budget(self):
        with database(self.config.database) as conn:
            self.add(conn, self.short_item()); packet = self.packet(conn)
        path = self.config.data_dir/'short-input.json'; freeze_packet(path, packet)
        with patch('polycast.store.reserve_model_call', side_effect=AssertionError('No budget should be spent')):
            with self.assertRaisesRegex(ValueError, 'Full official pregame statistics'):
                run(self.config, path, self.config.data_dir/'pilot')

    def test_verified_full_statistics_feed_both_pilot_calls_without_trades(self):
        with database(self.config.database) as conn:
            self.add(conn, self.item); packet = self.packet(conn)
        path = self.config.data_dir/'full-input.json'; freeze_packet(path, packet)
        control = {'event_id': 'daily1', 'as_of': packet['as_of'], 'abstain': False,
                   'probabilities': {'YES': .55, 'NO': .4, 'HALF': .05}, 'supporting_evidence_ids': [],
                   'counter_evidence_ids': [], 'reasoning': 'Synthetic fixture; no performance evidence.'}
        structured = {**control, 'resolution_conditions': ['Contract'], 'base_rate_reasoning': 'Synthetic',
                      'necessary_factors': [], 'missing_information': [], 'update_triggers': []}
        output = self.config.data_dir/'pilot'
        with patch('polycast.analysis._run_json', side_effect=[control, structured]) as model:
            summary = run(self.config, path, output)
        self.assertEqual(model.call_count, 2)
        self.assertTrue(summary['paired_schema_valid_forecasts'])
        self.assertFalse(summary['paired_eligible_forecasts'])
        start = json.loads((output/'started.json').read_text())
        self.assertTrue(start['require_daily_statistics'])
        self.assertEqual(start['official_mlb_capture_proof']['captures'], 1)
        bundle = json.loads((output/'source-bundle.json').read_text())
        self.assertIn('experiments/mlb_evidence.py', bundle['files'])
        with database(self.config.database) as conn:
            for table in ('predictions', 'paper_orders', 'trade_intents'):
                self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)

    def test_collection_does_not_hold_a_writer_through_public_requests(self):
        def public_capture(root, game_id):
            import sqlite3
            conn = sqlite3.connect(self.config.database, timeout=.1)
            try: conn.execute('BEGIN IMMEDIATE'); conn.rollback()
            finally: conn.close()
            shutil.copytree(self.root, root)
        with patch('experiments.mlb_pregame.collect', side_effect=public_capture):
            summary = collect_mlb_statistics(self.config, 'daily1')
        self.assertEqual(summary['new_observations'], 1)
        self.assertEqual(summary['model_calls'], 0)

    def test_contract_revision_during_network_work_cannot_enter_the_ledger(self):
        def changed_contract(root, game_id):
            shutil.copytree(self.root, root)
            with database(self.config.database) as conn:
                conn.execute("UPDATE markets SET description='Revised rules' WHERE id='1'")
        with patch('experiments.mlb_pregame.collect', side_effect=changed_contract):
            with self.assertRaisesRegex(ValueError, 'contract changed'): collect_mlb_statistics(self.config, 'daily1')
        with database(self.config.database) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
