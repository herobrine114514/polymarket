from dataclasses import replace
from contextlib import closing
from datetime import datetime,timedelta,timezone
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from polycast.config import Config
from polycast.execution import FEE_METHOD
from polycast.paper import fill_paper_intent,place,prepare_paper_intent
from polycast.paper_execution import PaperExecutionError,execution_terms,sample_fok_execution
from polycast.store import balance,database,digest_text,now_iso,save_prediction,upsert_markets

CONDITION='0x'+'12'*32


def market():
    return {'id':'1','event_id':'event','event_title':'Fed decision','event_slug':'fed',
        'question':'Yes?','description':'Unchanged rules','end_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
        'yes_token':'11','no_token':'12','yes_label':'Yes','no_label':'No','category':'macro',
        'liquidity':10000,'volume_24h':1000,'min_order_size':5.,'tick_size':.01,'fee_rate':.04}


def live(saved):
    return {'id':saved['id'],'conditionId':CONDITION,'closed':False,'active':True,'acceptingOrders':True,
        'enableOrderBook':True,'clobTokenIds':'["11","12"]','outcomes':'["Yes","No"]',
        'description':saved['description'],'endDate':saved['end_at'],'orderMinSize':5,'orderPriceMinTickSize':.01,
        'feeSchedule':{'rate':.04,'exponent':1},'secondsDelay':0}


def info():
    return {'c':CONDITION,'ao':True,'v':'v1','mos':5,'mts':.01,'fd':{'r':.04,'e':1,'to':True},
        't':[{'t':'11','o':'Yes'},{'t':'12','o':'No'}],'itode':True}


def book(asks=None):
    return {'market':CONDITION,'asset_id':'11','timestamp':str(int(datetime.now(timezone.utc).timestamp()*1000)),
        'min_order_size':'5','tick_size':'.01','asks':asks or [{'price':'.55','size':'10'}], 'bids':[]}


class Clock:
    value=0.
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class DelayedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config=Config(data_dir=Path(self.temp.name))
        self.market=market();self.live=live(self.market)
        with database(self.config.database) as conn:
            upsert_markets(conn,[self.market])
            self.pid=save_prediction(conn,'1','rules','evidence',{'as_of':now_iso(),'p_yes':.75,'abstain':False,
                'review':{'approved':True},'reviewed_at':now_iso(),'valid_until':self.market['end_at']})
        self.quote={'market_id':'1','event_id':'event','risk_group':'fed-policy','prediction_id':self.pid,
            'side':'YES','captured_at':now_iso(),'book_timestamp':now_iso(),'shares':5.,'avg_ask':.6,
            'worst_ask':.6,'fee_per_share':.0096,'fee_method':FEE_METHOD,
            'fills':[{'price':.6,'shares':5.}],'total_cost':3.048,'expected_edge':.0904}
        for name,value in [('polycast.paper._prediction_current',True),('polycast.paper.active_official_window',None),
                           ('polycast.paper.assert_entry_allowed',None),('polycast.paper.live_market',self.live),
                           ('polycast.paper_execution.clob_market_info',info())]:
            p=patch(name,return_value=value);p.start();self.addCleanup(p.stop)
        self.clock=Clock()
        for name,fn in [('polycast.paper_execution.time.monotonic',self.clock.now),
                        ('polycast.paper_execution.time.sleep',self.clock.sleep)]:
            p=patch(name,side_effect=fn);p.start();self.addCleanup(p.stop)

    def test_price_improvement_uses_new_fills_fees_and_keeps_original_limit_reservation(self):
        with database(self.config.database) as conn,patch('polycast.paper_execution.order_book',return_value=book()):
            oid=place(conn,self.quote,self.config)
            actual=conn.execute('SELECT * FROM paper_orders WHERE id=?',(oid,)).fetchone()
            self.assertAlmostEqual(actual['avg_price'],.55)
            self.assertAlmostEqual(actual['total_cost'],2.7995)
            self.assertAlmostEqual(actual['expected_edge'],.1401)
            intent=conn.execute('SELECT * FROM trade_intents').fetchone()
            self.assertEqual(intent['limit_price'],.6)
            self.assertEqual(intent['max_total_cost'],3.048)
            self.assertEqual(balance(conn,1000)['reserved_cost'],0)
            audit=conn.execute('SELECT * FROM paper_execution_audits').fetchone()
            detail=json.loads(audit['audit_json'])
            self.assertGreaterEqual(detail['hold_elapsed_ms'],250)
            self.assertEqual(detail['external_orders_submitted'],0)

    def test_partial_depth_under_cap_cannot_be_filled_at_a_higher_price(self):
        later=book([{'price':'.6','size':'4'},{'price':'.61','size':'99'}])
        with database(self.config.database) as conn,patch('polycast.paper_execution.order_book',return_value=later):
            with self.assertRaisesRegex(PaperExecutionError,'depth disappeared'):
                place(conn,self.quote,self.config)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],0)
            self.assertEqual(balance(conn,1000)['reserved_cost'],0)

            row=conn.execute('SELECT * FROM paper_execution_audits').fetchone()
            self.assertEqual(row['result'],'rejected')
            self.assertIsNone(row['execution_quote_json'])
            self.assertEqual(conn.execute('SELECT status FROM trade_intents').fetchone()[0],'rejected')

    def test_price_improvement_recomputes_roi_before_second_validation(self):
        quote = {**self.quote, 'expected_edge': .1404, 'expected_roi': .1404 / .6096}
        with database(self.config.database) as conn, patch('polycast.paper_execution.order_book',return_value=book()):
            identifier = place(conn,quote,self.config)
            audit = conn.execute('SELECT execution_quote_json FROM paper_execution_audits').fetchone()
            executed = json.loads(audit[0])
            self.assertGreater(executed['expected_roi'],quote['expected_roi'])
            self.assertAlmostEqual(executed['expected_roi'],executed['expected_edge'] / (executed['total_cost'] / 5))
            self.assertEqual(conn.execute('SELECT status FROM trade_intents').fetchone()[0],'filled')
            self.assertGreater(identifier,0)

    def test_network_operation_can_write_through_independent_sqlite_connection(self):
        def request(_token):
            with closing(sqlite3.connect(self.config.database,timeout=.2)) as second:
                second.execute("UPDATE markets SET volume_24h=1234 WHERE id='1'")
                second.commit()
            return book()
        with database(self.config.database) as conn,patch('polycast.paper_execution.order_book',side_effect=request):
            self.assertGreater(place(conn,self.quote,self.config),0)
            self.assertEqual(conn.execute('SELECT volume_24h FROM markets').fetchone()[0],1234)

    def test_rules_changed_while_fetching_are_revalidated_before_atomic_fill(self):
        def request(_token):
            with closing(sqlite3.connect(self.config.database,timeout=.2)) as second:
                second.execute("UPDATE markets SET fee_rate=.07 WHERE id='1'")
                second.commit()
            return book()
        with database(self.config.database) as conn,patch('polycast.paper_execution.order_book',side_effect=request):
            with self.assertRaises(ValueError):place(conn,self.quote,self.config)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],0)
            self.assertEqual(balance(conn,1000)['reserved_cost'],0)

    def test_direct_fill_cannot_bypass_missing_delayed_audit(self):
        with database(self.config.database) as conn:
            ident=prepare_paper_intent(conn,self.quote,self.config)
            with self.assertRaisesRegex(ValueError,'no delayed execution audit'):
                fill_paper_intent(conn,ident,self.config)
            self.assertAlmostEqual(balance(conn,1000)['reserved_cost'],3.048)

    def test_audit_tampering_prevents_fill_without_spending_reserved_funds(self):
        from polycast.paper import _save_execution_audit
        with database(self.config.database) as conn,patch('polycast.paper_execution.order_book',return_value=book()):
            ident=prepare_paper_intent(conn,self.quote,self.config)
            updated,audit=sample_fok_execution(self.market,self.quote,self.config,self.live)
            _save_execution_audit(conn,ident,audit,updated)
            conn.execute("UPDATE paper_execution_audits SET execution_quote_json='{}'")
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                fill_paper_intent(conn,ident,self.config)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],0)
            self.assertAlmostEqual(balance(conn,1000)['reserved_cost'],3.048)

    def test_missing_or_wrong_book_identity_and_stale_book_are_rejected(self):
        variants=[]
        for field,value in [('asset_id','999'),('market','0x'+'34'*32),('timestamp','1'),('tick_size','.001')]:
            changed=book();changed[field]=value;variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed),patch('polycast.paper_execution.order_book',return_value=changed):
                with self.assertRaises(PaperExecutionError):sample_fok_execution(self.market,self.quote,self.config,self.live)

    def test_seconds_delay_and_absent_flag_preserve_explicit_conservative_floor(self):
        params=info();params.pop('itode')
        self.assertEqual(execution_terms(self.market,self.live,params)['sample_hold_ms'],250)
        slow={**self.live,'secondsDelay':3}
        with patch('polycast.paper_execution.order_book',return_value=book()):
            _,audit=sample_fok_execution(self.market,self.quote,self.config,slow)
        self.assertEqual(audit['terms']['sample_hold_ms'],3000)
        self.assertGreaterEqual(audit['hold_elapsed_ms'],3000)

    def test_unsupported_fee_protocol_changed_tokens_and_market_closure_fail_closed(self):
        for changed in [dict(info(),v='v2'),dict(info(),ao=False),dict(info(),fd={'r':.04,'e':2,'to':True}),
                        dict(info(),fd={'r':.05,'e':1,'to':True}),dict(info(),itode='true'),
                        dict(info(),t=[{'t':'11','o':'No'},{'t':'12','o':'Yes'}])]:
            with self.subTest(info=changed),self.assertRaises(ValueError):execution_terms(self.market,self.live,changed)

    def test_network_failure_has_rejected_audit_and_no_paper_order(self):
        with database(self.config.database) as conn,patch('polycast.paper_execution.order_book',side_effect=RuntimeError('HTTP 503 public book')):
            with self.assertRaises(PaperExecutionError):place(conn,self.quote,self.config)
            self.assertEqual(balance(conn,1000)['reserved_cost'],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_execution_audits').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],0)


if __name__=='__main__':unittest.main()
