"""Replay previously captured public SDK book pairs; zero live calls or DB writes."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from polycast.config import Config
from polycast.execution import fill_totals,match_levels,FEE_METHOD
from polycast.paper_execution import PaperExecutionError,sample_fok_execution


def main():
    source=ROOT/'.polycast/polymarket-sdk-probe-20261002.json'
    original=json.loads(source.read_text(encoding='utf-8'))
    recorded=original['market'];labels=recorded['outcomes'];trading=recorded['trading']
    saved={'id':original['market_id'],'yes_token':labels['yes']['tokenId'],'no_token':labels['no']['tokenId'],
           'yes_label':labels['yes']['label'],'no_label':labels['no']['label'],
           'min_order_size':float(trading['minimumOrderSize']),'tick_size':trading['minimumTickSize'],
           'fee_rate':float(trading['feeSchedule']['rate']),'end_at':recorded['state']['endDate']}
    live={'conditionId':recorded['condition_id'],'orderMinSize':saved['min_order_size'],
          'orderPriceMinTickSize':saved['tick_size'],'secondsDelay':0}
    results=[]
    for sample in original['book_samples']:
        shares=sample['shares'];first=match_levels(sample['initial']['raw'],shares,'asks')
        gross,average,fee=fill_totals(first,shares,saved['fee_rate'])
        quote={'side':sample['label'],'shares':shares,'avg_ask':average,'worst_ask':first[-1]['price'],
               'fills':first,'fee_method':FEE_METHOD,'fee_per_share':fee/shares,'total_cost':gross+fee,
               # This replay is ONLY a fillability check; it supplies NO model probability.
               'expected_edge':0.0}
        receipt=datetime.fromisoformat(sample['later']['received_at'].replace('Z','+00:00'))
        class ReplayDate(datetime):
            @classmethod
            def now(cls,tz=None):return receipt if tz else receipt.replace(tzinfo=None)
        clock=[0.0]
        def sleep(seconds):clock[0]+=seconds
        with patch('polycast.paper_execution.datetime',ReplayDate),patch(
                'polycast.paper_execution.time.monotonic',side_effect=lambda:clock[0]),patch(
                'polycast.paper_execution.time.sleep',side_effect=sleep),patch(
                'polycast.paper_execution.clob_market_info',return_value=recorded['raw_clob_info']),patch(
                'polycast.paper_execution.order_book',return_value=sample['later']['raw']):
            try:
                execution,audit=sample_fok_execution(saved,quote,Config(),live)
                result={'label':sample['label'],'result':'fillable','shares':shares,'frozen_limit':quote['worst_ask'],
                        'later_average':execution['avg_ask'],'later_cost':execution['total_cost']}
            except PaperExecutionError as error:
                audit=error.audit
                result={'label':sample['label'],'result':'rejected','shares':shares,
                        'frozen_limit':quote['worst_ask'],'reason':str(error)}
        result.update(original_receipt_gap_ms=sample['observed_receipt_gap_ms'],audit=audit)
        results.append(result)
    assert [r['result'] for r in results]==['fillable','rejected']
    output={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'historical_public_book_pairs_only',
            'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'network_calls':0,
            'production_database_written':False,'orders_submitted':0,'prediction_credit':False,
            'strategy_profit_credit':False,'simulated_clock':True,'results':results}
    path=ROOT/'.polycast/delayed-fok-book-replay-20261002.json'
    with path.open('x',encoding='utf-8') as file:json.dump(output,file,indent=2)
    print(json.dumps({'checked_pairs':len(results),'fillable':1,'rejected':1,'output':str(path)}))


if __name__=='__main__':main()
