from __future__ import annotations
import copy
import base64
import hashlib
import json
import math
import sqlite3
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from polycast.config import Config
from polycast.event_research import (canonical_url,checked_forecast,control_payload,deduplicate,encoded,
    freeze_packet,make_packet,model_view,observations_at,publish_once,read_packet,verify_packet_observations,instant,
    forecast_target,relevant,sha,LEGACY_INPUT_VERSION,CONDITIONAL_INPUT_VERSION)
from polycast.store import database,insert_evidence,sync_news_sources,upsert_markets,withdraw_evidence,_upsert_source


def fed_markets(now):
    questions=['Will the Fed decrease interest rates by 50+ bps after the October 2026 meeting?',
        'Will the Fed decrease interest rates by 25 bps after the October 2026 meeting?',
        'Will there be no change in Fed interest rates after the October 2026 meeting?',
        'Will the Fed increase interest rates by 25 bps after the October 2026 meeting?',
        'Will the Fed increase interest rates by 50+ bps after the October 2026 meeting?']
    return [{'id':str(i+1),'event_id':'11','event_title':'Fed Decision in October?',
        'question':q,'description':'Change in target range after the official meeting; five exclusive brackets.',
        'event_slug':'fed-october','end_at':(now+timedelta(days=20)).isoformat(),
        'category':'macro','yes_label':'Yes','no_label':'No','yes_token':str(i+100),'no_token':str(i+200),
        'liquidity':10000,'volume_24h':1000,'min_order_size':5,'tick_size':.01,'fee_rate':0}
        for i,q in enumerate(questions)]


class EventEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=Config(data_dir=Path(self.temp.name));self.base=datetime.now(timezone.utc)-timedelta(seconds=10)
        self.markets=fed_markets(self.base)
        with database(self.config.database) as c:upsert_markets(c,self.markets)

    def add(self,c,t,*,url='https://www.federalreserve.gov/release',summary='A monetary policy statement with inflation and employment facts. '*3,**fields):
        item={'source':'Federal Reserve','url':url,'title':'FOMC monetary policy announcement',
              'summary':summary,'published_at':(self.base-timedelta(days=1)).isoformat(),**fields}
        with patch('polycast.store.datetime') as dt,patch('polycast.store.now_iso',return_value=t.isoformat()):
            dt.now.return_value=t
            insert_evidence(c,[item])
        return item

    def packet(self,c,cutoff):
        obs,audit=observations_at(c,cutoff.isoformat())
        return make_packet(self.markets,obs,cutoff.isoformat(),audit)

    def test_a_b_a_revisions_and_original_link_changes_remain_causal(self):
        with database(self.config.database) as c:
            t=self.base;original=self.add(c,t)
            self.add(c,t+timedelta(seconds=2),summary='Revised policy evidence. '*8)
            self.add(c,t+timedelta(seconds=4),origin_story_id='https://origin.test/original')
            one,_=observations_at(c,(t+timedelta(seconds=1)).isoformat())
            two,_=observations_at(c,(t+timedelta(seconds=3)).isoformat())
            three,_=observations_at(c,(t+timedelta(seconds=5)).isoformat())
            self.assertEqual(one[0]['evidence']['summary'],original['summary'])
            self.assertIn('Revised',two[0]['evidence']['summary'])
            self.assertEqual(three[0]['evidence']['id'],one[0]['evidence']['id'])
            self.assertEqual(three[0]['evidence']['origin_story_id'],'https://origin.test/original')
            self.assertEqual(c.execute('SELECT count(*) FROM evidence_observations').fetchone()[0],3)
            self.assertNotEqual(one[0]['observation_id'],three[0]['observation_id'])

    def test_observations_are_append_only_and_legacy_versions_are_not_backfilled(self):
        with database(self.config.database) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM evidence_observations').fetchone()[0],0)
            self.add(c,self.base)
            for sql in ('UPDATE evidence_observations SET observed_at=\'2000-01-01\'','DELETE FROM evidence_observations'):
                with self.assertRaisesRegex(sqlite3.IntegrityError,'append-only'):c.execute(sql)
            before,_=observations_at(c,(self.base-timedelta(seconds=1)).isoformat())
            self.assertEqual(before,[])

    def test_future_updated_revision_cannot_fall_back_to_prior_text(self):
        with database(self.config.database) as c:
            self.add(c,self.base)
            self.add(c,self.base+timedelta(seconds=2),summary='Future publisher correction '*8,
                updated_at=(self.base+timedelta(seconds=30)).isoformat())
            obs,audit=observations_at(c,(self.base+timedelta(seconds=3)).isoformat())
            self.assertEqual(obs,[]);self.assertIn('unavailable',audit['exclusions'][0]['reason'])

    def test_withdrawal_is_applied_only_after_its_observation_clock(self):
        with database(self.config.database) as c:
            self.add(c,self.base)
            with patch('polycast.store.now_iso',return_value=(self.base+timedelta(seconds=3)).isoformat()):
                withdraw_evidence(c,'https://www.federalreserve.gov/release','https://official.test/correction','Official publication was withdrawn')
            before,_=observations_at(c,(self.base+timedelta(seconds=1)).isoformat())
            after,audit=observations_at(c,(self.base+timedelta(seconds=4)).isoformat())
            self.assertEqual(len(before),1);self.assertEqual(after,[])
            self.assertEqual(audit['exclusions'][0]['reason'],'withdrawn_at_cutoff')

    def test_ledger_hash_and_current_authorization_are_checked(self):
        with database(self.config.database) as c:
            self.add(c,self.base)
            c.execute("UPDATE source_registry SET enabled=0 WHERE source_id='fed_monetary'")
            obs,_=observations_at(c,(self.base+timedelta(seconds=1)).isoformat());self.assertEqual(obs,[])
            c.execute('DROP TRIGGER evidence_observation_no_update')
            c.execute("UPDATE evidence_observations SET observation_json='{}'")
            with self.assertRaisesRegex(ValueError,'hash mismatch'):observations_at(c,(self.base+timedelta(seconds=1)).isoformat())

    def test_tracking_fields_are_removed_but_game_and_article_ids_survive(self):
        self.assertEqual(canonical_url('https://www.SOURCE.test/Article?id=2&utm_source=x#fragment'),'https://source.test/Article?id=2')
        self.assertNotEqual(canonical_url('https://source.test/Article?id=2'),canonical_url('https://source.test/Article?id=3'))
        with self.assertRaises(ValueError):canonical_url('https://user:password@source.test/a')
        with self.assertRaises(ValueError):instant('2026-10-06T12:00:00')

    def test_official_and_social_same_origin_are_one_group_with_variants(self):
        with database(self.config.database) as c:
            self.add(c,self.base)
            self.add(c,self.base+timedelta(seconds=1),source='Federal Reserve Bluesky',
                url='https://bsky.app/profile/federalreserve.gov/post/abc',
                origin_story_id='https://federalreserve.gov/release',summary='Link to the original FOMC announcement. '*3)
            obs,_=observations_at(c,(self.base+timedelta(seconds=2)).isoformat());groups=deduplicate(obs)
            self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['members']),2)
            self.assertEqual(groups[0]['representative']['evidence']['source_id'],'fed_monetary')
            self.assertEqual(groups[0]['distinct_content_versions'],2)
            self.assertFalse(groups[0]['independence_verified'])

    def test_long_exact_reprints_group_within_24h_but_opaque_guids_do_not_cross_sources(self):
        with database(self.config.database) as c:
            self.add(c,self.base,url='https://original.test/a',origin_story_id='publisher-guid')
            self.add(c,self.base+timedelta(seconds=1),url='https://reprint.test/a',origin_story_id='another-guid')
            self.add(c,self.base+timedelta(seconds=2),url='https://later.test/a',origin_story_id='publisher-guid',
                source='Federal Reserve Speeches',published_at=(self.base-timedelta(days=3)).isoformat())
            obs,_=observations_at(c,(self.base+timedelta(seconds=3)).isoformat());groups=deduplicate(obs)
            self.assertEqual(len(groups),2)
            duplicate=next(g for g in groups if len(g['members'])==2)
            self.assertEqual({o['evidence']['url'] for o in duplicate['members']},{'https://original.test/a','https://reprint.test/a'})
            short=copy.deepcopy(obs)
            for o in short:o['evidence']['summary']='Short'
            self.assertEqual(len(deduplicate(short)),3)

    def test_daily_schedule_requires_fresh_matching_game_and_maps_binary_labels(self):
        daily={**self.markets[0],'event_id':'daily1','event_title':'Away vs Home','question':'Away vs Home',
            'category':'daily','source_ref':'123','yes_label':'Away','no_label':'Home'}
        with database(self.config.database) as c:
            _upsert_source(c,'mlb_schedule','MLB Stats API','official','https://statsapi.test/api','official_source','','',1)
            self.add(c,self.base,source='MLB Stats API',source_id='mlb_schedule',feed_url='https://statsapi.test/api',
                license_scope='official_source',origin_story_id='123',url='https://statsapi.test/schedule?gamePk=123')
            obs,audit=observations_at(c,(self.base+timedelta(seconds=1)).isoformat())
            packet=make_packet([daily],obs,(self.base+timedelta(seconds=1)).isoformat(),audit)
            self.assertEqual(packet['kind'],'binary');self.assertEqual(control_payload(packet)['outcome_a'],'Away')
            with self.assertRaisesRegex(ValueError,'No new causal'):make_packet([daily],obs,(self.base+timedelta(minutes=3)).isoformat(),audit)

    def test_blind_views_share_cutoff_and_evidence_without_baseline_fields(self):
        with database(self.config.database) as c:
            self.add(c,self.base);packet=self.packet(c,self.base+timedelta(seconds=1))
            packet['market_prior']={'p_market_mid':.999};packet['positions']={'held':100}
            view=model_view(packet);control=control_payload(packet)
            self.assertNotIn('market_prior',view);self.assertNotIn('positions',view)
            self.assertNotIn('market_prior',control)
            self.assertEqual(control['as_of'],view['as_of'])
            self.assertEqual(control['evidence'][0]['summary'],view['evidence_groups'][0]['variants'][0]['summary'])
            self.assertEqual(len(control['markets']),5)

    def test_educational_social_keywords_and_lookalike_hosts_are_not_policy_evidence(self):
        observation={'evidence':{'source_id':'fed_bluesky','source':'Federal Reserve Bluesky',
            'published_at':self.base.isoformat(),'title':'Teaching Monetary Policy webinar; inflation and employment',
            'summary':'Education event, not a rate decision.', 'url':'at://did:plc:fed/app.bsky.feed.post/1',
            'origin_story_id':'https://www.federalreserve.gov/education/webinar.htm'}}
        self.assertFalse(relevant(observation,self.markets[0],self.base))
        observation['evidence']['origin_story_id']='https://federalreserve.gov.fake.test/newsevents/pressreleases/monetary20261006a.htm'
        self.assertFalse(relevant(observation,self.markets[0],self.base))
        observation['evidence']['origin_story_id']='https://www.federalreserve.gov/newsevents/pressreleases/monetary20261006a.htm'
        self.assertTrue(relevant(observation,self.markets[0],self.base))
        observation['evidence'].update(origin_story_id=observation['evidence']['url'],title='FOMC statement: target range for federal funds rate')
        self.assertTrue(relevant(observation,self.markets[0],self.base))
        observation['evidence'].update(source_id='fed_speeches',title='The Dual Mandate in Rural America')
        self.assertTrue(relevant(observation,self.markets[0],self.base))

    def test_fractional_daily_settlement_uses_unconditional_states_and_payout_comparison(self):
        with database(self.config.database) as c:
            self.add(c,self.base);packet=self.packet(c,self.base+timedelta(seconds=1))
        packet.update(category='daily',kind='settlement_multiple_choice',resolution_rules='Winner resolves YES or NO; canceled or tied game resolves 50-50.')
        packet['options']=[{'key':'YES','label':'Away'},{'key':'NO','label':'Home'},{'key':'HALF','label':'Official 50-50 settlement'}]
        packet['forecast_target']=forecast_target(packet['category'],packet['resolution_rules'])
        self.assertEqual(packet['forecast_target']['quantity'],'exclusive_official_settlement_state_probabilities')
        self.assertEqual(model_view(packet)['forecast_target'],control_payload(packet)['forecast_target'])
        path=Path(self.temp.name)/'fractional.json'
        wrapper=freeze_packet(path,packet,{'1':{'status':'captured','p_yes_mid':.45}})
        self.assertTrue(wrapper['market_baselines']['1']['comparison_eligible'])
        self.assertFalse(wrapper['market_baselines']['1']['distribution_comparison_eligible'])
        self.assertTrue(packet['forecast_target']['expected_payout_available'])
        self.assertIn('never exclude',packet['forecast_target']['official_fractional_result_policy'])
        bad=copy.deepcopy(wrapper);bad['packet']['forecast_target']['quantity']='binary_official_resolution_probability'
        bad['packet_sha256']=sha(bad['packet']);bad['bundle_sha256']=sha({k:v for k,v in bad.items() if k!='bundle_sha256'})
        path.write_text(encoded(bad),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'target differs'):read_packet(path)

    def test_legacy_inputs_remain_auditable_but_cannot_start_new_paid_pilots(self):
        from experiments.event_study import run
        with database(self.config.database) as c:
            self.add(c,self.base);packet=self.packet(c,self.base+timedelta(seconds=1))
        packet['input_version']=LEGACY_INPUT_VERSION;packet.pop('forecast_target')
        path=Path(self.temp.name)/'legacy.json';freeze_packet(path,packet)
        self.assertEqual(read_packet(path)['packet']['input_version'],LEGACY_INPUT_VERSION)
        with self.assertRaisesRegex(ValueError,'Legacy packets'):run(self.config,path,Path(self.temp.name)/'legacy-run')
        self.assertFalse((Path(self.temp.name)/'legacy-run').exists())
        with database(self.config.database) as c:self.assertEqual(c.execute('SELECT count(*) FROM model_runs').fetchone()[0],0)

    def test_frozen_packet_is_exclusive_and_rehashed_text_still_fails_ledger_match(self):
        with database(self.config.database) as c:
            self.add(c,self.base);packet=self.packet(c,self.base+timedelta(seconds=1))
            path=Path(self.temp.name)/'packet.json';freeze_packet(path,packet)
            old=path.read_bytes()
            with self.assertRaises(FileExistsError):freeze_packet(path,packet)
            self.assertEqual(path.read_bytes(),old);self.assertEqual(list(path.parent.glob('.event-study-*.tmp')),[])
            frozen=read_packet(path)['packet'];verify_packet_observations(c,frozen)
            frozen['evidence_groups'][0]['members'][0]['evidence']['summary']='Changed after freezing'
            with self.assertRaisesRegex(ValueError,'differs'):verify_packet_observations(c,frozen)

    def test_structured_distribution_cannot_normalize_or_cite_unknown_ids(self):
        with database(self.config.database) as c:
            self.add(c,self.base);packet=self.packet(c,self.base+timedelta(seconds=1))
            result={'event_id':packet['event_id'],'as_of':packet['as_of'],'abstain':False,
                'probabilities':{o['key']:.2 for o in packet['options']},'resolution_conditions':['Rule'],
                'base_rate_reasoning':'No verified empirical rate; subjective prior only.','necessary_factors':['Factors'],
                'supporting_evidence_ids':[packet['evidence_groups'][0]['members'][0]['evidence']['id']],
                'counter_evidence_ids':[],'missing_information':[],'update_triggers':['Official release'],'reasoning':'Evidence estimate.'}
            self.assertIs(checked_forecast(packet,result),result)
            for invalid in (1.2,True,float('nan')):
                bad=copy.deepcopy(result);bad['probabilities'][packet['options'][0]['key']]=invalid
                with self.assertRaises(ValueError):checked_forecast(packet,bad)
            bad=copy.deepcopy(result);bad['supporting_evidence_ids']=[999]
            with self.assertRaisesRegex(ValueError,'unknown'):checked_forecast(packet,bad)

    def test_finite_pilot_shares_budget_has_no_orders_and_cannot_repeat_calls(self):
        from experiments.event_study import run
        with database(self.config.database) as c:
            self.add(c,self.base);packet=self.packet(c,self.base+timedelta(seconds=1))
        path=Path(self.temp.name)/'pilot.json';freeze_packet(path,packet);out=Path(self.temp.name)/'pilot'
        control={'event_id':packet['event_id'],'as_of':packet['as_of'],'abstain':True,'probabilities':{o['key']:.2 for o in packet['options']}}
        structured={**control,'resolution_conditions':['Rule'],'base_rate_reasoning':'Unknown empirical base rate.',
            'necessary_factors':[],'supporting_evidence_ids':[],'counter_evidence_ids':[],
            'missing_information':['Future data'],'update_triggers':['Release'],'reasoning':'Insufficient evidence.'}
        with patch('polycast.analysis.run_event_codex',return_value=control),patch('polycast.analysis._run_json',return_value=structured):
            summary=run(self.config,path,out,require_macro_history=False)
            self.assertEqual(summary['model_calls'],2);self.assertFalse(summary['paired_schema_valid_forecasts'])
            self.assertFalse(summary['paired_eligible_forecasts'])
            self.assertEqual(summary['styles'][0]['cost_status'],'unknown_not_zero')
            bundle=json.loads((out/'source-bundle.json').read_text())
            start=json.loads((out/'started.json').read_text())
            self.assertEqual(bundle['bundle_sha256'],start['source_bundle_sha256'])
            self.assertEqual(bundle['bundle_sha256'],sha({k:v for k,v in bundle.items() if k!='bundle_sha256'}))
            for name,item in bundle['files'].items():
                self.assertEqual(hashlib.sha256(base64.b64decode(item['content_base64'])).hexdigest(),start['source_hashes'][name])
            with self.assertRaises(FileExistsError):run(self.config,path,out,require_macro_history=False)
        with database(self.config.database) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM model_runs').fetchone()[0],2)
            for table in ('predictions','paper_orders','trade_intents'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_new_macro_pilot_requires_receipt_history_before_budget(self):
        from experiments.event_study import run
        with database(self.config.database) as c:
            self.add(c,self.base); packet=self.packet(c,self.base+timedelta(seconds=1))
        path=Path(self.temp.name)/'no-history.json'; freeze_packet(path,packet)
        with patch('polycast.store.reserve_model_call') as reserve, self.assertRaisesRegex(ValueError,'official policy history'):
            run(self.config,path,Path(self.temp.name)/'no-history-run')
        reserve.assert_not_called()
        self.assertFalse((Path(self.temp.name)/'no-history-run').exists())

    def test_fractional_daily_pilot_uses_adapted_control_and_no_fed_prompt(self):
        from experiments.event_study import run
        daily={**self.markets[0],'event_id':'daily1','event_title':'Away vs Home','question':'Away vs Home',
            'category':'daily','source_ref':'123','yes_label':'Away','no_label':'Home',
            'description':'Away win pays YES; Home win pays NO; cancelled with no reschedule or tied resolves 50-50.'}
        with database(self.config.database) as c:
            upsert_markets(c,[daily])
            _upsert_source(c,'mlb_schedule','MLB Stats API','official','https://statsapi.test/api','official_source','','',1)
            self.add(c,self.base,source='MLB Stats API',source_id='mlb_schedule',feed_url='https://statsapi.test/api',
                license_scope='official_source',origin_story_id='123',url='https://statsapi.test/schedule?gamePk=123')
            cutoff=(self.base+timedelta(seconds=1)).isoformat();obs,audit=observations_at(c,cutoff)
            packet=make_packet([daily],obs,cutoff,audit)
        self.assertEqual(packet['kind'],'settlement_multiple_choice')
        self.assertEqual([o['key'] for o in packet['options']],['YES','NO','HALF'])
        control={'event_id':'daily1','as_of':cutoff,'abstain':False,
            'probabilities':{'YES':.55,'NO':.4,'HALF':.05},'supporting_evidence_ids':[],
            'counter_evidence_ids':[],'reasoning':'Synthetic engineering fixture, not a market result.'}
        structured={**control,'resolution_conditions':['Supplied rules'],'base_rate_reasoning':'Synthetic prior',
                    'necessary_factors':[],'missing_information':[],'update_triggers':[]}
        path=Path(self.temp.name)/'ternary-input.json';out=Path(self.temp.name)/'ternary-pilot';freeze_packet(path,packet)
        with (patch('polycast.analysis.run_event_codex',side_effect=AssertionError('MLB cannot use Fed prompt')),
              patch('polycast.analysis.run_codex',side_effect=AssertionError('Conditional binary control is not this target')),
              patch('polycast.analysis._run_json',side_effect=[control,structured]) as model):
            summary=run(self.config,path,out,require_daily_statistics=False)
        self.assertEqual(model.call_count,2);self.assertTrue(summary['paired_schema_valid_forecasts'])
        start=json.loads((out/'started.json').read_text())
        self.assertTrue(start['control_target_adapted'])
        self.assertEqual(start['control_prompt_version'],'daily-settlement-control-v1')
        with database(self.config.database) as c:
            for table in ('predictions','paper_orders','trade_intents'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_preparation_freezes_separate_market_prices_and_budget_failure_is_recorded(self):
        from experiments.event_study import prepare,run
        with database(self.config.database) as c:self.add(c,self.base)
        path=Path(self.temp.name)/'prepared.json'
        book={'asset_id':'100','timestamp':str(int(datetime.now(timezone.utc).timestamp()*1000)),
            'bids':[{'price':'.4','size':'10'}],'asks':[{'price':'.5','size':'10'}]}
        with patch('polycast.market.order_book',return_value=book):
            prepare(self.config,'11',path)
        wrapper=read_packet(path)
        self.assertEqual(wrapper['market_baselines']['1']['p_yes_mid'],.45)
        self.assertNotIn('market_baselines',model_view(wrapper['packet']))
        with patch('polycast.store.reserve_model_call',side_effect=RuntimeError('Daily call limit reached')):
            summary=run(self.config,path,Path(self.temp.name)/'budget-failed',require_macro_history=False)
        self.assertEqual(summary['model_calls'],0)
        self.assertTrue(all(s['status']=='not_called_budget_or_pause' for s in summary['styles']))


if __name__=='__main__':unittest.main()
