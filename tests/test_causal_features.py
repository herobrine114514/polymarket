from __future__ import annotations
import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from experiments.causal_features import VERSION, EXTRA_NAMES
from experiments.numeric_models import feature_vector, CAUSAL_FEATURE_NAMES
from experiments.journal_replay import verify_causal_journal


def fixture():
    start=1791331200000;utc=start+120000;now=utc+3000
    clock=lambda receipt:{'status':'venues_agree','offset_ms':-3000,'uncertainty_ms':100,'checked_at_local_ms':receipt-1000}
    up={'bid':.4,'ask':.5,'midpoint':.45,'spread':.1,'top5_imbalance':0,
        'received_at_ms':now-10,'source_timestamp_ms':utc-50,'bids':[[.4,3]],'asks':[[.5,4]]}
    down={**up,'bid':.5,'ask':.6,'midpoint':.55,'bids':[[.5,3]],'asks':[[.6,4]]}
    mid=60000*math.exp(.00001*60)
    cb={**up,'bid':mid-.5,'ask':mid+.5,'midpoint':mid,'spread':1.,'bids':[[mid-.5,3]],'asks':[[mid+.5,4]]}
    histories=[]
    for i in range(61):
        time=utc-60000+i*1000;receipt=time+3000
        histories.append({'input_snapshot_record_seq':i+1,'received_at_ms':receipt,'monotonic_ns':str(receipt*1000000),
          'as_of_utc_ms':time,'clock_reference':clock(receipt),'connection_id':'cb','valid':True,
          'midpoint':60000*math.exp(.00001*i),'book_received_at_ms':receipt-10,'source_timestamp_ms':time-50})
    pm=[]
    for i in range(11):
        row=copy.deepcopy(histories[-11+i]);row.update(connection_id='pm',midpoint=.449+.0001*i);pm.append(row)
    refs=[]
    for i in range(120):
        point={'schema_version':1,'timestamp_ms':start+i*1000,'received_at_ms':start+i*1000+3500,
          'full_accuracy_value':str(60000*10**18+i*10**16),'feed_protocol':'rtds','price_encoding':'e18',
          'price_source':'chainlink','symbol':'btcusd','quote_currency':'USD','window_seconds':60,'connection_id':'relay'}
        raw=json.dumps(point)
        refs.append({'raw_text':raw,'raw_sha256':hashlib.sha256(raw.encode()).hexdigest(),'point':point,'journal_seq':100+i,
          'first_read_at_ms':now-1,'first_read_monotonic_ns':str((now-1)*1000000)})
    return {'kind':'feature_snapshot','feature_version':VERSION,'session_id':'fixture','record_seq':301,'base_snapshot_record_seq':300,
      'received_at_ms':now,'monotonic_ns':str(now*1000000),'as_of_utc_ms':utc,'clock_reference':clock(now),'paired':True,
      'market':{'market_id':'1','event_id':'2','start_at_ms':start,'end_at_ms':start+300000},
      'polymarket':{'up':up,'down':down,'connection_id':'pm'},
      'coinbase':{'book':cb,'connection_id':'cb','flow':{key:{'complete':True,'taker_imbalance':0} for key in ('10s','30s')}},
      'causal_complete':True,'causal_context':{'schema_version':1,'history_clock':'checked-UTC-1s-observations',
        'coinbase':histories,'polymarket_up':pm,'reference':{'status':'proxy_available','read_issue':None,'points':refs,
          'independent_reference_verified':False,'opening_reference_kind':'near-start-relay-proxy'}}}


class CausalFeatureTests(unittest.TestCase):
    def test_units_and_window_math_match_independent_price_path(self):
        s=fixture();v=feature_vector(s);self.assertEqual(len(v),32)
        values=dict(zip(CAUSAL_FEATURE_NAMES,v))
        self.assertAlmostEqual(values['coinbase_return_10s_bps'],1.,places=8)
        self.assertAlmostEqual(values['coinbase_return_60s_bps'],6.,places=8)
        self.assertAlmostEqual(values['coinbase_realized_vol_30s_bps'],math.sqrt(30)*.1,places=8)
        self.assertAlmostEqual(values['up_mid_change_10s'],.001,places=12)
        self.assertEqual(values['seconds_remaining'],180)
        self.assertAlmostEqual(values['twap_distance_from_open_bps'],1.19/60000*10000,places=10)
        self.assertEqual(values['twap_source_age_s'],1.)
        self.assertEqual(values['twap_velocity_missing'],0.)

    def test_a_gap_or_reconnect_is_masked_without_interpolating_a_return(self):
        for modification in ('gap','reconnect','quarantine'):
            s=fixture(); rows=s['causal_context']['coinbase']
            if modification=='gap':del rows[40]
            if modification=='reconnect':rows[40]['connection_id']='another'
            if modification=='quarantine':rows[40]['valid']=False
            values=dict(zip(CAUSAL_FEATURE_NAMES,feature_vector(s)))
            self.assertEqual(values['coinbase_return_60s_missing'],1.)
            self.assertEqual(values['coinbase_return_60s_bps'],0.)
            self.assertEqual(values['coinbase_return_10s_missing'],0.)

    def test_future_consumer_receipt_or_raw_tamper_cannot_be_used(self):
        for kind in ('read','mono','bytes','source'):
            s=fixture(); row=s['causal_context']['reference']['points'][-1]
            if kind=='read':row['first_read_at_ms']=s['received_at_ms']+1
            elif kind=='mono':row['first_read_monotonic_ns']=str(int(s['monotonic_ns'])+1)
            elif kind=='bytes':row['point']['full_accuracy_value']='99999999999999999999999'
            else:
                row['point']['timestamp_ms']=s['as_of_utc_ms']+1;row['raw_text']=json.dumps(row['point']);row['raw_sha256']=hashlib.sha256(row['raw_text'].encode()).hexdigest()
            with self.assertRaises(ValueError):feature_vector(s)

    def test_known_conflict_or_stale_proxy_is_rejected_at_that_cutoff(self):
        s=fixture();row=copy.deepcopy(s['causal_context']['reference']['points'][0]);row['point']['full_accuracy_value']=str(60001*10**18)
        row['raw_text']=json.dumps(row['point']);row['raw_sha256']=hashlib.sha256(row['raw_text'].encode()).hexdigest();row['journal_seq']=250
        s['causal_context']['reference']['points'].append(row)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):feature_vector(s)
        s=fixture();s['causal_context']['reference']['points']=s['causal_context']['reference']['points'][:100]
        with self.assertRaisesRegex(ValueError,'Stale'):feature_vector(s)

    def test_missing_twap_path_is_masked_but_missing_opening_is_not(self):
        s=fixture();s['causal_context']['reference']['points']=[s['causal_context']['reference']['points'][0],s['causal_context']['reference']['points'][-1]]
        values=dict(zip(CAUSAL_FEATURE_NAMES,feature_vector(s)));self.assertEqual(values['twap_velocity_missing'],1)
        s['causal_context']['reference']['points']=s['causal_context']['reference']['points'][1:]
        with self.assertRaisesRegex(ValueError,'Opening'):feature_vector(s)

    def test_journal_verification_rejects_a_rewritten_past_price(self):
        s=fixture();rows=[];samples=s['causal_context']['coinbase'];pm={r['input_snapshot_record_seq']:r for r in s['causal_context']['polymarket_up']}
        for sample in samples:
            base=copy.deepcopy(s);base.pop('causal_context');base.pop('causal_complete');base.pop('base_snapshot_record_seq')
            base.update(schema_version=1,feature_version='btc5m-public-microstructure-v2-clock-checked',record_seq=sample['input_snapshot_record_seq'])
            for key in ('received_at_ms','monotonic_ns','as_of_utc_ms','clock_reference'):base[key]=sample[key]
            base['coinbase']['book'].update(midpoint=sample['midpoint'],received_at_ms=sample['book_received_at_ms'],source_timestamp_ms=sample['source_timestamp_ms'])
            if sample['input_snapshot_record_seq'] in pm:
                p=pm[sample['input_snapshot_record_seq']];base['polymarket']['up'].update(midpoint=p['midpoint'],received_at_ms=p['book_received_at_ms'],source_timestamp_ms=p['source_timestamp_ms'])
            rows.append(base)
        for sample in s['causal_context']['reference']['points']:
            rows.append({'schema_version':1,'session_id':'fixture','record_seq':sample['journal_seq'],'kind':'reference_observation',
              'raw_text':sample['raw_text'],'raw_sha256':sample['raw_sha256'],'received_at_ms':sample['first_read_at_ms'],
              'monotonic_ns':sample['first_read_monotonic_ns'],'source_timestamp_ms':sample['point']['timestamp_ms']})
        base={k:copy.deepcopy(v) for k,v in s.items() if k not in ('causal_context','causal_complete','base_snapshot_record_seq')}
        base.update(schema_version=1,record_seq=300,feature_version='btc5m-public-microstructure-v2-clock-checked');rows.append(base)
        rows.append({**s,'schema_version':1})
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);name='raw-fixture-00000.jsonl'
            def publish():
                existing={r['record_seq']:r for r in rows};ordered=[existing.get(i,{'schema_version':1,'session_id':'fixture','record_seq':i,'kind':'filler'}) for i in range(1,302)]
                raw=''.join(json.dumps(r)+'\n' for r in ordered).encode();(p/name).write_bytes(raw)
                (p/(name+'.seal.json')).write_text(json.dumps({'schema_version':1,'filename':name,'session_id':'fixture','bytes':len(raw),
                  'sha256':hashlib.sha256(raw).hexdigest(),'first_record_seq':1,'last_record_seq':301,'closed_at_ms':s['received_at_ms']}))
            publish();self.assertEqual(verify_causal_journal(p,s,'fixture-digest')['status'],'verified_causal_context')
            s['causal_context']['coinbase'][10]['midpoint']*=1.01;publish()
            with self.assertRaisesRegex(ValueError,'history changed'):verify_causal_journal(p,s,'fixture-digest')
