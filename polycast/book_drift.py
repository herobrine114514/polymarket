"""Invalidate stale five-minute forecast bases without changing their probabilities."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .market import parse_utc

DRIFT_METHOD = "btc5m_preforecast_book_drift_v1"


def book_top(book: dict) -> tuple[float, float] | None:
    try:
        bids = [float(level['price']) for level in book.get('bids', []) if float(level['size']) > 0]
        asks = [float(level['price']) for level in book.get('asks', []) if float(level['size']) > 0]
        if not bids or not asks or not all(math.isfinite(p) and 0 < p < 1 for p in bids + asks):
            return None
        bid, ask = max(bids), min(asks)
        return (bid, ask) if bid <= ask else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def forecast_basis(conn, prediction) -> dict | None:
    row = conn.execute('SELECT * FROM market_baselines WHERE prediction_id=?',
                       (prediction['id'],)).fetchone()
    if row is None:
        return None
    return {'prediction_id': int(prediction['id']), 'forecast_as_of': prediction['as_of'],
            'captured_at': row['captured_at'], 'book_timestamp': row['book_timestamp'],
            'yes_bid': row['yes_best_bid'], 'yes_ask': row['yes_best_ask'],
            'p_yes_mid': row['p_yes_mid']}


def assess_book_drift(basis: dict | None, quote: dict, config,
                      *, now: datetime | None = None) -> dict:
    instant = now or datetime.now(timezone.utc)
    audit = {'method': DRIFT_METHOD, 'checked_at': instant.isoformat(timespec='microseconds'),
             'status': 'invalid', 'allowed': False,
             'max_mid_change': config.btc5m_book_max_drift,
             'max_forecast_age_seconds': config.btc5m_basis_max_age_seconds,
             'threshold_scope': 'Engineering validity cap; predictive benefit not validated'}
    try:
        if basis is None:
            raise ValueError('Missing immutable pre-forecast book')
        forecast, captured, source = (parse_utc(basis.get(key)) for key in
                                      ('forecast_as_of', 'captured_at', 'book_timestamp'))
        if (forecast is None or captured is None or source is None or
                captured > forecast or source > forecast or captured < source):
            raise ValueError('Forecast book time does not precede the forecast')
        age = (instant - forecast).total_seconds()
        audit['forecast_age_seconds'] = age
        if not 0 <= age <= config.btc5m_basis_max_age_seconds:
            audit['status'] = 'expired'
            raise ValueError('Original forecast input expired before entry')
        if (captured - source).total_seconds() > 10:
            raise ValueError('Original forecast book was stale')
        bid, ask, prior = (float(basis[key]) for key in ('yes_bid', 'yes_ask', 'p_yes_mid'))
        current_bid, current_ask = (float(quote[key]) for key in ('book_bid', 'book_ask'))
        if (not all(math.isfinite(x) for x in (bid, ask, prior, current_bid, current_ask)) or
                not 0 < bid <= ask < 1 or not 0 < current_bid <= current_ask < 1 or
                not math.isclose(prior, (bid + ask) / 2, rel_tol=0, abs_tol=1e-6)):
            raise ValueError('Forecast or execution book midpoint is invalid')
        if quote.get('side') not in ('YES', 'NO'):
            raise ValueError('Invalid quote side')
        mid = (current_bid + current_ask) / 2
        current_yes = mid if quote['side'] == 'YES' else 1 - mid
        change = current_yes - prior
        audit.update(prediction_id=basis['prediction_id'], forecast_p_yes_mid=prior,
                     current_implied_yes_mid=current_yes, midpoint_change=change,
                     current_mid_from_side=quote['side'], basis_captured_at=basis['captured_at'])
        if abs(change) > config.btc5m_book_max_drift + 1e-12:
            audit['status'] = 'drifted'
            raise ValueError(f'Book midpoint changed {change:+.4f}; recompute or abstain')
        audit.update(status='valid', allowed=True, reason='Forecast basis remains within registered validity caps')
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        audit['reason'] = str(exc)[:200]
    return audit
