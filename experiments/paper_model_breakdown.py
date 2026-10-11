"""Read-only paper bet results by original model/prompt/execution method."""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def summarize(rows):
    settled = [r for r in rows if r['verified_settlement']]
    won = sum(r['settlement'] == 1 for r in settled)
    return {'orders': len(rows), 'independent_events': len({r['event_id'] for r in rows}),
            'verified_settled_orders': len(settled), 'won': won, 'lost': len(settled) - won,
            'open': sum(r['status'] == 'open' for r in rows),
            'unverified_settled': sum(r['status'] == 'settled' and not r['verified_settlement'] for r in rows),
            'winning_fraction': round(won / len(settled), 6) if settled else None,
            'paper_pnl_after_trading_fee': round(sum(r['pnl_after_trading_fee'] for r in settled), 5),
            'all_in_net_pnl': None}


def report(database, model):
    now, rows = datetime.now(timezone.utc), []
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        for row in conn.execute('''SELECT o.*,p.model_output_json,r.winning_side,r.source_url,
            r.resolved_at,r.recorded_at,a.method AS execution_method FROM paper_orders o
            JOIN predictions p ON p.id=o.prediction_id JOIN markets m ON m.id=o.market_id
            LEFT JOIN resolutions r ON r.market_id=o.market_id
            LEFT JOIN paper_execution_audits a ON a.intent_id=o.intent_id
            WHERE m.category='crypto5m' ORDER BY o.id'''):
            out = json.loads(row['model_output_json'])
            if out.get('model_version') != model:
                continue
            saved = {k: row[k] for k in ['id', 'prediction_id', 'market_id', 'event_id', 'created_at',
                    'side', 'shares', 'avg_price', 'fee', 'total_cost', 'expected_edge', 'status',
                    'settlement', 'winning_side', 'source_url', 'resolved_at', 'recorded_at']}
            saved.update(model_version=model, prompt_version=out.get('prompt_version'),
                         side_probability=out['p_yes'] if row['side'] == 'YES' else 1 - out['p_yes'],
                         execution_method=row['execution_method'] or 'legacy_snapshot_paper',
                         verified_settlement=False, pnl_after_trading_fee=None)
            if (row['status'] == 'settled' and row['winning_side'] in ['YES', 'NO'] and
                    row['settlement'] in [0., 1.] and row['resolved_at'] and row['recorded_at'] and
                    datetime.fromisoformat(row['recorded_at']) <= now):
                saved['verified_settlement'] = row['settlement'] == float(row['side'] == row['winning_side'])
                if saved['verified_settlement']:
                    saved['pnl_after_trading_fee'] = float(Decimal(str(row['shares'])) *
                        Decimal(str(row['settlement'])) - Decimal(str(row['total_cost'])))
            rows.append(saved)
    keys = sorted({(r['prompt_version'], r['execution_method']) for r in rows})
    return {'checked_at': now.isoformat(), 'scope': 'Actual paper orders; direction accuracy has a different denominator',
            'model': model, 'overall': summarize(rows),
            'by_prompt_and_execution': [{'prompt': p, 'execution_method': e,
                **summarize([r for r in rows if (r['prompt_version'], r['execution_method']) == (p, e)])}
                for p, e in keys], 'rows': rows,
            'rows_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            'limitations': ['Open or unverified orders are not wins/losses',
                'Trading fees included; unknown model/data/infrastructure costs excluded',
                'Different historical execution methods are separated; no frozen live-strategy proof']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('.polycast/polycast.sqlite3'))
    parser.add_argument('--model', default='deepseek-v4-pro')
    parser.add_argument('--output', type=Path, default=Path('.polycast/paper-model-breakdown-20261003.json'))
    args = parser.parse_args()
    data = report(args.database, args.model)
    data['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: data[k] for k in ['checked_at', 'model', 'overall', 'by_prompt_and_execution']}, ensure_ascii=False))
