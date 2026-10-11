from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from experiments.macro_policy import (ORIGIN, CALENDAR, RATE_TABLE, MIN_BASELINE_EVENTS, LOOKBACK_EVENTS,
    parse_calendar, parse_statement, parse_rate_table, outcome_slot, reconcile_history, research_baseline,
    collect, read_response, replay_directory, encoded, utc, fetch)
from experiments.macro_evidence import (DIRECTORY, SOURCE_ID, capture_descriptor, evidence_item,
    validate_observation, verify_packet_captures)
from experiments.event_study import observe_macro_capture, prepare, run
from polycast.config import Config
from polycast.event_research import read_packet, model_view, control_payload, checked_macro_label_forecast, macro_label_schema
from polycast.store import database, upsert_markets, read_database
from tests.test_event_research import fed_markets
from tests.macro_grounding_fixture import grounded_output


def statement(day='2024-09-18', action='lower', by='by 1/2 percentage point ', lower='4-3/4', upper='5'):
    at = datetime.fromisoformat(day).replace(hour=14, tzinfo=ZoneInfo('America/New_York'))
    prep = 'at' if action in ('maintain', 'keep') else 'to'
    return (f'<meta content="decided to raise the target range for the federal funds rate to 10 to 11 percent">'
            f'<div id="article"><p>{at.strftime("%B")} {at.day}, {at.year}</p>'
            f'<h3>Federal Reserve issues FOMC statement</h3><p>For release at 2:00 p.m. {at.tzname()}</p>'
            f'<p>The Committee decided to {action} the target range for the federal funds rate '
            f'{by}{prep} {lower} to {upper} percent.</p>'
            '<p>A dissenter preferred to lower the target range by 1/4 percentage point.</p></div>').encode()


def address(day): return ORIGIN+'/newsevents/pressreleases/monetary'+day.replace('-', '')+'a.htm'


def calendar_row(day, *, linked=True, month=None, days=None):
    at = date.fromisoformat(day)
    return (f'<div class="row fomc-meeting"><div class="fomc-meeting__month">{month or at.strftime("%B")}</div>'
            f'<div class="fomc-meeting__date">{days or at.day}</div>'
            + (f'<a href="{address(day).removeprefix(ORIGIN)}">HTML</a>' if linked else '') + '</div>')


def table(rows):
    return ''.join(f'<h4>{year}</h4><table><tr><th>Date</th><th>Increase</th><th>Decrease</th><th>Level (%)</th></tr>'
        + ''.join('<tr>'+''.join(f'<td>{v}</td>' for v in values)+'</tr>' for values in values_list)+'</table>'
        for year, values_list in rows).encode()


class MacroParsingTests(unittest.TestCase):
    def test_calendar_cross_month_and_strategy_only_vote(self):
        raw = ('<h4>2024 FOMC Meetings</h4>'+calendar_row('2024-05-01', month='Apr/May', days='30-1')
               +'<h4>2025 FOMC Meetings</h4><div class="row fomc-meeting"><div class="fomc-meeting__month">August</div>'
               '<div class="fomc-meeting__date">22 (notation vote)</div>'
               '<a href="/newsevents/pressreleases/monetary20250822a.htm">Statement on Longer-Run Goals and Monetary Policy Strategy</a></div>').encode()
        self.assertEqual([r['meeting_date'] for r in parse_calendar(raw)], ['2024-05-01'])

    def test_calendar_rejects_mismatched_duplicate_or_untyped_dates(self):
        body = '<h4>2024 FOMC Meetings</h4>'+calendar_row('2024-09-18')
        for bad in (body.replace('monetary20240918', 'monetary20240919'), body+calendar_row('2024-09-18'),
                    body.replace('2024 FOMC Meetings', 'Unknown'), body.replace('>18</div>', '>unknown</div>')):
            with self.subTest(bad=bad), self.assertRaises(ValueError): parse_calendar(bad.encode())

    def test_mixed_fraction_nonbreaking_hyphen_and_unicode_quarter(self):
        for lower, upper in [('4‑3/4', '5'), ('4¾', '5'), ('4-3/4', '5'), ('4.75', '5.0')]:
            parsed = parse_statement(statement(lower=lower, upper=upper), address('2024-09-18'))
            self.assertEqual((parsed['lower_bps'], parsed['upper_bps'], parsed['explicit_change_bps']), (475, 500, -50))
            self.assertEqual(parsed['released_at'], '2024-09-18T18:00:00+00:00')

    def test_statement_metadata_and_dissent_are_not_the_enacted_policy(self):
        parsed = parse_statement(statement(), address('2024-09-18'))
        self.assertEqual(parsed['upper_bps'], 500)
        raw = statement()+statement()
        with self.assertRaisesRegex(ValueError, 'ambiguous'): parse_statement(raw, address('2024-09-18'))

    def test_statement_date_timezone_and_interval_are_validated(self):
        for raw, url in [(statement(), address('2024-09-19')),
                         (statement().replace(b'EDT', b'EST'), address('2024-09-18')),
                         (statement(lower='6'), address('2024-09-18')),
                         (statement().replace(b'id="article"', b'id="other"'), address('2024-09-18'))]:
            with self.subTest(url=url), self.assertRaises(ValueError): parse_statement(raw, url)

    def test_change_without_by_remains_unknown_until_official_table(self):
        parsed = parse_statement(statement(by=''), address('2024-09-18'))
        self.assertIsNone(parsed['explicit_change_bps'])
        changes = [{'effective_date': '2023-07-27', 'lower_bps': 525, 'upper_bps': 550, 'change_bps': 25},
                   {'effective_date': '2024-09-19', 'lower_bps': 475, 'upper_bps': 500, 'change_bps': -50}]
        reconcile_history([parsed], changes)
        self.assertEqual(parsed['enacted_change_bps'], -50)
        self.assertEqual(parsed['effective_date'], '2024-09-19')
        self.assertEqual(parsed['released_at'], '2024-09-18T18:00:00+00:00')

    def test_table_parses_effective_dates_and_correction_footnote(self):
        rows = parse_rate_table(table([(2020, [('March 16', '0', '100', '0-0.25'),
                                              ('March 4 *', '0', '50', '1.00-1.25')])]))
        self.assertEqual(rows[0]['effective_date'], '2020-03-04')
        self.assertEqual(rows[1]['change_bps'], -100)

    def test_table_rejects_missing_unit_schema_or_inconsistent_level(self):
        for raw in [table([(2024, [('September 19', '50', '50', '4.75-5.00')])]),
                    table([(2024, [('September 19', '0', '50', '4.75-5.00'), ('November 8', '0', '25', '4.0-4.25')])]),
                    table([(2024, [('September 19', '0', '50', '5.00')])])]:
            with self.assertRaises(ValueError): parse_rate_table(raw)

    def test_unlisted_intermeeting_change_is_not_assumed_to_be_meeting_action(self):
        parsed = parse_statement(statement(), address('2024-09-18'))
        changes = [{'effective_date': '2023-07-27', 'lower_bps': 525, 'upper_bps': 550, 'change_bps': 25},
                   {'effective_date': '2024-09-19', 'lower_bps': 475, 'upper_bps': 500, 'change_bps': -50},
                   {'effective_date': '2024-09-25', 'lower_bps': 450, 'upper_bps': 475, 'change_bps': -25}]
        with self.assertRaisesRegex(ValueError, 'intermeeting'): reconcile_history([parsed], changes)

    def test_table_disagreement_cannot_silently_relabel_a_statement(self):
        parsed = parse_statement(statement(), address('2024-09-18'))
        changes = [{'effective_date': '2024-09-19', 'lower_bps': 475, 'upper_bps': 500, 'change_bps': -25}]
        with self.assertRaisesRegex(ValueError, 'magnitude'): reconcile_history([parsed], changes)

    def test_contract_magnitude_rounding_and_invalid_numeric_values(self):
        expected = {0: 'no_change', 12.5: 'increase_25', -25: 'decrease_25', 25.001: 'increase_50_plus', -75: 'decrease_50_plus'}
        for delta, slot in expected.items(): self.assertEqual(outcome_slot(delta), slot)
        for delta in (True, float('nan'), float('inf'), '25'):
            with self.assertRaises(ValueError): outcome_slot(delta)

    def test_frequency_keeps_all_hikes_and_uses_only_fixed_recent_window(self):
        statements = [{'meeting_date': str(i), 'enacted_change_bps': 75 if i < 10 else 0} for i in range(50)]
        facts = {'statements': statements, 'missing_past_statement_dates': [], 'unlisted_intermeeting_changes_verified': True}
        baseline = research_baseline(facts)
        self.assertEqual(baseline['sample_events'], LOOKBACK_EVENTS)
        self.assertEqual(baseline['counts']['increase_50_plus'], 0)
        self.assertAlmostEqual(sum(baseline['research_state_probabilities'].values()), 1)
        self.assertIsNone(baseline['conditioning']); self.assertFalse(baseline['production_eligible'])
        facts['statements'] = statements[:MIN_BASELINE_EVENTS]
        self.assertEqual(research_baseline(facts)['counts']['increase_50_plus'], 10)
        facts['statements'][0].pop('enacted_change_bps')
        self.assertIsNone(research_baseline(facts)['research_state_probabilities'])

    def test_calendar_gaps_and_too_few_events_withhold_baseline(self):
        facts = {'statements': [{'meeting_date': '2024-09-18', 'enacted_change_bps': -50}],
                 'missing_past_statement_dates': [], 'unlisted_intermeeting_changes_verified': True}
        self.assertIsNone(research_baseline(facts)['research_state_probabilities'])
        facts['statements'] *= 40; facts['missing_past_statement_dates'] = ['2024-01-31']
        self.assertIsNone(research_baseline(facts)['research_state_probabilities'])


class MacroEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = Config(data_dir=Path(self.temp.name)); self.name = 'capture-'+'a'*32
        self.root = self.config.data_dir/DIRECTORY/self.name
        self.base = datetime.now(timezone.utc); self.end = self.base+timedelta(days=20)
        target = self.end.date().isoformat()
        self.calendar = ('<h4>2024 FOMC Meetings</h4>'+calendar_row('2024-01-31')+calendar_row('2024-09-18')
                         +f'<h4>{self.end.year} FOMC Meetings</h4>'+calendar_row(target, linked=False)).encode()
        self.responses = {'calendar': self.calendar, '2024-01-31': statement('2024-01-31', 'keep', '', '5-1/4', '5-1/2'),
                          '2024-09-18': statement(by=''), 'rate-table': table([
                              (2023, [('July 27', '25', '0', '5.25-5.50')]),
                              (2024, [('September 19', '0', '50', '4.75-5.00')])])}
        self.calls = []
        def fake_fetch(root, name, url):
            self.calls.append(url); now = datetime.now(timezone.utc).isoformat(); raw = self.responses[name]
            receipt = {'url': url, 'sent_at': now, 'received_at': datetime.now(timezone.utc).isoformat(),
                       'http_status': 200, 'raw_filename': name+'.raw.html', 'raw_bytes': len(raw),
                       'raw_sha256': hashlib.sha256(raw).hexdigest()}
            (root/receipt['raw_filename']).write_bytes(raw)
            (root/(name+'.receipt.json')).write_text(encoded(receipt)+'\n', encoding='utf-8')
            return receipt
        with patch('experiments.macro_policy.fetch', side_effect=fake_fetch): self.result = collect(self.root)
        self.markets = fed_markets(self.base)
        rule = ('The upper bound of the target federal funds range is changed versus the level prior to the meeting. '
                'The change is rounded up to the nearest 25. '
                f'The meeting scheduled for {self.end.strftime("%B")} {self.end.day}, {self.end.year}. '
                'If no statement is released by the next scheduled meeting deadline resolve No change.')
        for market in self.markets: market['description'] = rule
        with database(self.config.database) as conn: upsert_markets(conn, self.markets)

    def packet(self):
        observe_macro_capture(self.config, '11', self.name)
        dest = self.config.data_dir/'packet.json'
        prepare(self.config, '11', dest, capture_baselines=False)
        return read_packet(dest)['packet'], dest

    def test_full_capture_is_reproducible_and_keeps_history_available_now(self):
        proof = capture_descriptor(self.root)
        self.assertEqual(proof['raw_receipts_verified'], 4)
        with patch('urllib.request.build_opener', side_effect=AssertionError('network')):
            self.assertEqual(replay_directory(self.root)['network_calls'], 0)
        self.assertFalse(self.result['facts']['historical_decision_time_availability_proven'])
        self.assertEqual(self.result['facts']['statements'][1]['enacted_change_bps'], -50)
        self.assertEqual(len(self.calls), 4)

    def test_model_views_receive_the_same_facts_without_numeric_baseline(self):
        packet, _ = self.packet()
        proof = verify_packet_captures(packet, self.config.data_dir)
        self.assertEqual(proof['captures'], 1)
        self.assertEqual(proof['contract_contexts'][0]['latest_announced_upper_bps'], 500)
        # Even an audit field containing a comparator's probabilities stays out.
        packet['input_audit']['test_baseline_sentinel'] = {'research_state_probabilities': {'no_change': .99}}
        for view in (model_view(packet), control_payload(packet)):
            self.assertNotIn('research_state_probabilities', encoded(view))
            self.assertIn('current_official_archive_received_now', encoded(view))

    def test_raw_mutation_fails_packet_proof_before_any_paid_call(self):
        packet, dest = self.packet()
        with (self.root/'2024-09-18.raw.html').open('ab') as stream: stream.write(b'changed')
        with patch('polycast.store.reserve_model_call') as reserve, self.assertRaisesRegex(ValueError, 'response changed'):
            run(self.config, dest, self.config.data_dir/'run')
        reserve.assert_not_called()

    def test_backdating_observation_or_availability_is_rejected(self):
        item = evidence_item(self.config.data_dir, self.name)
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError): validate_observation({'evidence': item, 'observed_at': (now-timedelta(days=1)).isoformat()}, now)
        content = json.loads(item['summary']); content['facts']['historical_decision_time_availability_proven'] = True
        item['summary'] = encoded(content)
        with self.assertRaises(ValueError): validate_observation({'evidence': item, 'observed_at': now.isoformat()}, now)

    def test_late_received_original_file_cannot_enter_an_earlier_cutoff(self):
        receipt = json.loads((self.root/'calendar.receipt.json').read_text())
        cutoff = utc(receipt['received_at'])-timedelta(microseconds=1)
        with self.assertRaisesRegex(ValueError, 'unavailable'): read_response(self.root, 'calendar', CALENDAR, cutoff)

    def test_rederiving_old_responses_does_not_reset_data_freshness(self):
        item = evidence_item(self.config.data_dir, self.name)
        content = json.loads(item['summary']); now = datetime.now(timezone.utc)
        content['facts']['source_first_received_at'] = (now-timedelta(days=3)).isoformat()
        content['facts']['source_last_received_at'] = (now-timedelta(days=2)).isoformat()
        item['summary'] = encoded(content)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            validate_observation({'evidence': item, 'observed_at': now.isoformat()}, now)

    def test_unsupported_rules_do_not_write_an_observation(self):
        with database(self.config.database) as conn: conn.execute("UPDATE markets SET description='EFFR at end of year'")
        with self.assertRaisesRegex(ValueError, 'relative-upper'): observe_macro_capture(self.config, '11', self.name)
        with read_database(self.config.database) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0], 0)

    def test_matching_macro_pilot_remains_budgeted_paper_research(self):
        packet, dest = self.packet()
        control = {'event_id': '11', 'as_of': packet['as_of'], 'abstain': True, 'probabilities': {o['key']: .2 for o in packet['options']}}
        structured = grounded_output(packet, probabilities={o['label']:.2 for o in packet['options']}, abstain=True)
        out = self.config.data_dir/'run'
        with patch('polycast.analysis.run_event_codex', return_value=control), patch('polycast.analysis._run_json', return_value=structured):
            result = run(self.config, dest, out)
        self.assertEqual(result['model_calls'], 2); self.assertFalse(result['live_ready'])
        self.assertEqual(json.loads((out/'started.json').read_text())['official_fomc_capture_proof']['captures'], 1)
        receipt = json.loads((out/'structured.json').read_text())
        self.assertEqual(receipt['provider_result'], structured)
        self.assertEqual(receipt['result']['probabilities'], control['probabilities'])
        with read_database(self.config.database) as conn:
            for name in ('predictions', 'paper_orders', 'trade_intents'):
                self.assertEqual(conn.execute('SELECT count(*) FROM '+name).fetchone()[0], 0)

    def test_fetch_rejects_official_looking_external_or_relative_urls(self):
        for url in ('https://www.federalreserve.gov.evil.test/newsevents/pressreleases/monetary20240918a.htm',
                    '/newsevents/pressreleases/monetary20240918a.htm'):
            with patch('urllib.request.build_opener') as opener, self.assertRaises(ValueError): fetch(self.root, 'bad', url)
            opener.assert_not_called()

    def test_failed_direction_audit_keeps_original_content_and_budget_failure(self):
        from polycast.model_output import emit_model_output
        packet, dest = self.packet()
        control = {'event_id': '11', 'as_of': packet['as_of'], 'abstain': False,
                   'probabilities': {o['key']: .2 for o in packet['options']}}
        raw = grounded_output(packet, probabilities={o['label']:.2 for o in packet['options']})
        raw.update(direction_probabilities={'decrease':.8,'no_change':.1,'increase':.1},most_likely_direction='decrease')
        content = encoded(raw)
        def respond(*args, **kwargs):
            emit_model_output(content, provider='deepseek', model='deepseek-v4-pro', finish_reason='stop')
            return raw
        out = self.config.data_dir/'run'
        with patch('polycast.analysis.run_event_codex', return_value=control), patch('polycast.analysis._run_json', side_effect=respond):
            summary = run(self.config, dest, out)
        receipt = json.loads((out/'structured.json').read_text())
        response = json.loads((out/'structured-raw-response-01.json').read_text())
        self.assertEqual(summary['model_calls'], 2); self.assertFalse(summary['paired_schema_valid_forecasts'])
        self.assertEqual(receipt['status'], 'failed_or_late'); self.assertIsNone(receipt['result'])
        self.assertFalse(receipt['output_probability_audit']['accepted'])
        self.assertEqual(receipt['provider_result'], raw); self.assertEqual(response['raw_content'], content)
        self.assertTrue(receipt['raw_response_capture_complete'])
        with read_database(self.config.database) as conn:
            row = conn.execute('SELECT error FROM model_runs WHERE id=?', (receipt['model_call_id'],)).fetchone()
            self.assertIn('relationship', row['error'])
            self.assertEqual(conn.execute('SELECT count(*) FROM paper_orders').fetchone()[0], 0)

    def test_control_cannot_gain_audit_credit_from_normalized_bad_original(self):
        packet, dest = self.packet()
        control = {'event_id': '11', 'as_of': packet['as_of'], 'abstain': False,
            'probabilities': {o['key']: .2 for o in packet['options']},
            'raw_probabilities': {o['key']: .3 for o in packet['options']}}
        with patch('polycast.analysis.run_event_codex', return_value=control), \
             patch('polycast.analysis._run_json', side_effect=RuntimeError('failed response')):
            run(self.config, dest, self.config.data_dir/'run')
        receipt = json.loads((self.config.data_dir/'run/control.json').read_text())
        self.assertEqual(receipt['status'], 'failed_or_late')
        self.assertFalse(receipt['output_probability_audit']['accepted'])
        self.assertEqual(receipt['result'], control)

    def test_duplicate_json_output_remains_failed_even_when_parsed_distribution_is_valid(self):
        from polycast.model_output import emit_model_output
        packet, dest = self.packet()
        control = {'event_id': '11', 'as_of': packet['as_of'], 'abstain': False,
                   'probabilities': {o['key']: .2 for o in packet['options']}}
        content = '{"duplicate":1,"duplicate":2}'
        def respond(*args, **kwargs):
            emit_model_output(content, provider='deepseek', model='deepseek-v4-pro', finish_reason='stop')
            return control
        with patch('polycast.analysis.run_event_codex', side_effect=respond), \
             patch('polycast.analysis._run_json', side_effect=RuntimeError('failed response')):
            run(self.config, dest, self.config.data_dir/'run')
        receipt = json.loads((self.config.data_dir/'run/control.json').read_text())
        self.assertEqual(receipt['status'], 'failed_or_late')
        self.assertTrue(receipt['output_probability_audit']['accepted'])
        self.assertFalse(receipt['raw_response_receipts'][0]['strict_json_object_valid'])
        response = json.loads((self.config.data_dir/'run/control-raw-response-01.json').read_text())
        self.assertEqual(response['raw_content'], content)

    def test_labelled_probabilities_map_direction_to_id_without_rewriting_raw(self):
        packet, _ = self.packet()
        raw = {'event_id': '11', 'as_of': packet['as_of'], 'abstain': False,
               'probabilities_by_state': {'decrease_50_plus': .01, 'decrease_25': .05, 'no_change': .30,
                                          'increase_25': .60, 'increase_50_plus': .04},
               'most_likely_state': 'increase_25', 'resolution_conditions': ['Rule'], 'base_rate_reasoning': 'Research.',
               'direction_probabilities': {'decrease': .06, 'no_change': .30, 'increase': .64}, 'most_likely_direction': 'increase',
               'necessary_factors': [], 'supporting_evidence_ids': [], 'counter_evidence_ids': [],
               'missing_information': [], 'update_triggers': ['Release'], 'reasoning': 'Final estimate favours a 25bps increase.'}
        original = copy.deepcopy(raw); result = checked_macro_label_forecast(packet, raw)
        key = next(o['key'] for o in packet['options'] if o['label'] == 'increase_25')
        self.assertEqual(result['probabilities'][key], .60); self.assertEqual(raw, original)
        bad = copy.deepcopy(raw); bad['most_likely_state'] = 'decrease_25'
        with self.assertRaisesRegex(ValueError, 'contradicts'): checked_macro_label_forecast(packet, bad)
        bad = copy.deepcopy(raw); bad['probabilities_by_state']['increase_25'] = True
        with self.assertRaises(ValueError): checked_macro_label_forecast(packet, bad)
        bad = copy.deepcopy(raw); bad['reasoning'] = 'x'*1601
        with self.assertRaises(ValueError): checked_macro_label_forecast(packet, bad)
        bad = copy.deepcopy(raw); bad['direction_probabilities'] = {'decrease': .64, 'no_change': .30, 'increase': .06}
        with self.assertRaisesRegex(ValueError, 'relationship'): checked_macro_label_forecast(packet, bad)
        bad = copy.deepcopy(raw); bad['most_likely_direction'] = 'decrease'
        with self.assertRaisesRegex(ValueError, 'contradicts'): checked_macro_label_forecast(packet, bad)
        bad = copy.deepcopy(raw); bad['most_likely_direction'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'contradicts'): checked_macro_label_forecast(packet, bad)
        bad = copy.deepcopy(packet); bad['options'][0]['label'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'atom names'): checked_macro_label_forecast(bad, raw)
        bad = copy.deepcopy(packet); bad['contract_snapshot'][0]['event_id'] = 'other-event'
        with self.assertRaisesRegex(ValueError, 'contract mapping'): checked_macro_label_forecast(bad, raw)
        bad = copy.deepcopy(packet); bad['options'][0]['key'] = bad['options'][1]['key']
        with self.assertRaisesRegex(ValueError, 'atom names'): checked_macro_label_forecast(bad, raw)


if __name__ == '__main__': unittest.main()
