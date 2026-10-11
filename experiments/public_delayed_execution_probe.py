"""Exercise production delayed snapshot sampling on a current public BTC market.

No model, production DB, paper order, wallet, authentication or live order calls.
"""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from polycast.config import Config
from polycast.market import discover_btc_5m,parse_utc
from polycast.paper import _quote,refresh_execution
from polycast.paper_execution import PaperExecutionError


def main():
    now=datetime.now(timezone.utc)
    markets=[m for m in discover_btc_5m(now) if parse_utc(m['end_at'])>now]
    markets.sort(key=lambda m:m['end_at'])
    if not markets:raise RuntimeError('No public current BTC market')
    saved=markets[0];samples=[]
    for side in ('YES','NO'):
        quote=_quote(saved,side,max(5.,saved['min_order_size']),Config())
        if quote is None:
            samples.append({'side':side,'result':'initial_depth_unavailable'});continue
        quote.update(total_cost=quote['shares']*(quote['avg_ask']+quote['fee_per_share']),expected_edge=0.)
        try:
            later,audit=refresh_execution(saved,quote,Config())
            samples.append({'side':side,'result':'fillable','initial_quote':quote,'later_quote':later,'audit':audit})
        except PaperExecutionError as exc:
            samples.append({'side':side,'result':'rejected','reason':str(exc),'initial_quote':quote,'audit':exc.audit})
        except (ValueError,RuntimeError) as exc:
            samples.append({'side':side,'result':'unavailable','reason':str(exc)[:250]})
    report={'started_at':now.isoformat(),'completed_at':datetime.now(timezone.utc).isoformat(),
            'scope':'current_public_quote_sampling_only','market_id':saved['id'],'end_at':saved['end_at'],
            'model_calls':0,'production_database_written':False,'paper_orders_created':0,'live_orders_submitted':0,
            'credentials_used':False,'prediction_credit':False,'live_execution_gate_passed':False,'samples':samples}
    out=ROOT/'.polycast/public-delayed-execution-probe-20261002.json'
    with out.open('x',encoding='utf-8') as file:json.dump(report,file,ensure_ascii=False,indent=2)
    print(json.dumps({'market_id':saved['id'],'samples':[{'side':s['side'],'result':s['result'],
        'hold_ms':s.get('audit',{}).get('terms',{}).get('sample_hold_ms'),'reason':s.get('reason')} for s in samples]}))


if __name__=='__main__':main()
