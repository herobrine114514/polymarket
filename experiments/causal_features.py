"""Causal V3 feature formulas, with explicit missing masks and unsigned relay proxy.

Historical samples are 1s observations, not complete engine price paths. Features
do not infer an independent opening reference or future TWAP probability.
"""
from __future__ import annotations
import hashlib
import json
import math
from decimal import Decimal
from polycast.twap_records import decode_twap_record

VERSION = 'btc5m-causal-features-v3'
EXTRA_NAMES = ('seconds_remaining', 'twap_distance_from_open_bps', 'spot_minus_twap_bps',
    'twap_velocity_10s_bps_per_s', 'twap_source_age_s', 'twap_receipt_age_s', 'opening_proxy_offset_s',
    'coinbase_return_10s_bps', 'coinbase_return_10s_missing',
    'coinbase_return_60s_bps', 'coinbase_return_60s_missing',
    'coinbase_realized_vol_30s_bps', 'coinbase_realized_vol_30s_missing',
    'up_mid_change_10s', 'up_mid_change_10s_missing',
    'coinbase_depth_log_cash', 'up_ask_depth_log_cash', 'down_ask_depth_log_cash',
    'coinbase_book_age_s', 'up_book_age_s', 'down_book_age_s', 'twap_velocity_missing')


def checked_time(row: dict) -> int:
    raw, utc, clock = row.get('received_at_ms'), row.get('as_of_utc_ms'), row.get('clock_reference') or {}
    offset, width, checked = clock.get('offset_ms'), clock.get('uncertainty_ms'), clock.get('checked_at_local_ms')
    if (type(raw) is not int or type(utc) is not int or type(checked) is not int or
        clock.get('status') != 'venues_agree' or not 0 <= raw-checked <= 120000 or
        isinstance(offset,bool) or not isinstance(offset,(int,float)) or not math.isfinite(offset) or abs(offset)>10000 or
        isinstance(width,bool) or not isinstance(width,(int,float)) or not math.isfinite(width) or not 0<=width<=750 or
        abs(raw+offset-utc)>1):
        raise ValueError('Unverified causal history UTC')
    return utc


def reference_points(context: dict, snapshot: dict) -> list[dict]:
    if (context.get('status')!='proxy_available' or context.get('independent_reference_verified') is not False or
        context.get('opening_reference_kind')!='near-start-relay-proxy' or context.get('read_issue') is not None or
        not isinstance(context.get('points'),list) or len(context['points'])>2048):
        raise ValueError('Unsigned reference proxy unavailable or wrong provenance')
    now, utc = snapshot['received_at_ms'], snapshot['as_of_utc_ms']
    try: mono=int(snapshot['monotonic_ns'])
    except (ValueError,TypeError,KeyError): raise ValueError('Missing reference consumer clock')
    points={}; conflicts=set()
    for row in context['points']:
        raw=row.get('raw_text'); seq=row.get('journal_seq'); read=row.get('first_read_at_ms')
        if (not isinstance(raw,str) or len(raw)>16384 or hashlib.sha256(raw.encode()).hexdigest()!=row.get('raw_sha256') or
            type(seq) is not int or not 0<seq<snapshot['record_seq'] or type(read) is not int or read>now):
            raise ValueError('Invalid reference raw identity or availability')
        if int(row.get('first_read_monotonic_ns','-1'))<0 or int(row['first_read_monotonic_ns'])>mono:
            raise ValueError('Future reference consumer receipt')
        item=json.loads(raw)
        if item!=row.get('point'): raise ValueError('Reference point differs from captured raw bytes')
        point=decode_twap_record(item)
        if point['received_at_ms']>now or point['timestamp_ms']>utc or point['timestamp_ms']<snapshot['market']['start_at_ms']-3000:
            raise ValueError('Reference outside causal source/receipt interval')
        stamp=point['timestamp_ms']
        if stamp in points and points[stamp]['price']!=point['price']: conflicts.add(stamp)
        elif stamp not in points or point['received_at_ms']<points[stamp]['received_at_ms']: points[stamp]=point
    start=snapshot['market']['start_at_ms']
    if any(abs(stamp-start)<=3000 or utc-stamp<=15000 for stamp in conflicts):
        raise ValueError('Ambiguous opening/latest relay version')
    return [point for stamp,point in sorted(points.items()) if stamp not in conflicts]


def history_window(rows: list[dict], snapshot: dict, seconds: int, current_mid: float, connection: str) -> list[dict] | None:
    if not isinstance(rows,list) or len(rows)>130: raise ValueError('Invalid bounded history')
    now, utc, target = snapshot['received_at_ms'], snapshot['as_of_utc_ms'], snapshot['as_of_utc_ms']-seconds*1000
    mono=int(snapshot['monotonic_ns']); previous=-1
    for row in rows:
        seq, receipt = row.get('input_snapshot_record_seq'),row.get('received_at_ms')
        if (type(seq) is not int or not 0<seq<snapshot['record_seq'] or type(receipt) is not int or receipt>now or
            int(row.get('monotonic_ns','-1'))<0 or int(row['monotonic_ns'])>mono or receipt<previous):
            raise ValueError('Historical sample identity/availability mismatch')
        previous=receipt
    # Do not interpolate across invalid clocks, quarantines or missing observations.
    candidates=[]
    for index,row in enumerate(rows):
        try: time=checked_time(row)
        except ValueError: continue
        if time<=target and target-time<=1500: candidates.append(index)
    if not candidates:return None
    window=rows[candidates[-1]:]
    times=[]
    for row in window:
        try: time=checked_time(row)
        except ValueError:return None
        mid,receipt,source=row.get('midpoint'),row.get('book_received_at_ms'),row.get('source_timestamp_ms')
        if (row.get('valid') is not True or not connection or row.get('connection_id')!=connection or
            isinstance(mid,bool) or not isinstance(mid,(int,float)) or not math.isfinite(mid) or mid<=0 or
            type(receipt) is not int or not 0<=row['received_at_ms']-receipt<=5000 or
            source is not None and (type(source)is not int or source>time) or time>utc):return None
        times.append(time)
    if (not window or times[-1]!=utc or window[-1]['received_at_ms']!=now or
        abs(window[-1]['midpoint']-current_mid)>1e-8 or
        any(not 0<b-a<=1500 for a,b in zip(times,times[1:]))):return None
    return window


def depth_cash(book: dict, sides: tuple[str,...]) -> float:
    total=0.
    for side in sides:
        rows=book.get(side)
        if not isinstance(rows,list) or not 1<=len(rows)<=5:raise ValueError('Missing bounded top-five depth')
        previous=None
        for row in rows:
            if (not isinstance(row,list) or len(row)!=2 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or
                not math.isfinite(x) or x<=0 for x in row)):raise ValueError('Invalid top-five depth values')
            p,q=row
            if previous is not None and (p>=previous if side=='bids' else p<=previous):raise ValueError('Unsorted or duplicate depth')
            previous=p;total+=p*q
        if abs(rows[0][0]-book['bid' if side=='bids' else 'ask'])>1e-8:raise ValueError('Depth and top quote differ')
    return math.log1p(total)


def extras(snapshot: dict) -> list[float]:
    context=snapshot.get('causal_context') or {}
    if (snapshot.get('causal_complete') is not True or context.get('schema_version')!=1 or
        context.get('history_clock')!='checked-UTC-1s-observations'):
        raise ValueError('Causal V3 context incomplete')
    utc=checked_time(snapshot);now=snapshot['received_at_ms'];start=snapshot['market']['start_at_ms']
    points=reference_points(context.get('reference') or {},snapshot)
    near=[p for p in points if abs(p['timestamp_ms']-start)<=3000]
    if not near or not points:raise ValueError('Opening relay proxy missing')
    opening=min(near,key=lambda p:(abs(p['timestamp_ms']-start),p['timestamp_ms']))
    latest=points[-1]
    if not 0<=utc-latest['timestamp_ms']<=15000 or not 0<=now-latest['received_at_ms']<=30000:
        raise ValueError('Stale relay proxy')
    up,down,cb=snapshot['polymarket']['up'],snapshot['polymarket']['down'],snapshot['coinbase']['book']
    prices=[p for p in points if p['timestamp_ms']<=latest['timestamp_ms']-10000 and latest['timestamp_ms']-10000-p['timestamp_ms']<=1500]
    velocity=0.;missing_velocity=1.
    if prices:
        prior=prices[-1];window=[p for p in points if prior['timestamp_ms']<=p['timestamp_ms']<=latest['timestamp_ms']]
        if all(0<b['timestamp_ms']-a['timestamp_ms']<=2000 for a,b in zip(window,window[1:])):
            velocity=float((latest['price']/prior['price']-Decimal(1))*10000)/((latest['timestamp_ms']-prior['timestamp_ms'])/1000)
            missing_velocity=0.
    values=[(snapshot['market']['end_at_ms']-utc)/1000,
        float((latest['price']/opening['price']-Decimal(1))*10000),
        (cb['midpoint']/float(latest['price'])-1)*10000,velocity,
        (utc-latest['timestamp_ms'])/1000,(now-latest['received_at_ms'])/1000,(opening['timestamp_ms']-start)/1000]
    windows={seconds:history_window(context.get('coinbase'),snapshot,seconds,cb['midpoint'],snapshot['coinbase'].get('connection_id')) for seconds in (10,30,60)}
    for seconds in (10,60):
        path=windows[seconds]
        values.extend([math.log(path[-1]['midpoint']/path[0]['midpoint'])*10000 if path else 0.,0. if path else 1.])
    path=windows[30]
    values.extend([math.sqrt(sum(math.log(b['midpoint']/a['midpoint'])**2 for a,b in zip(path,path[1:])))*10000 if path else 0.,0. if path else 1.])
    path=history_window(context.get('polymarket_up'),snapshot,10,up['midpoint'],snapshot['polymarket'].get('connection_id'))
    values.extend([path[-1]['midpoint']-path[0]['midpoint'] if path else 0.,0. if path else 1.])
    values.extend([depth_cash(cb,('bids','asks')),depth_cash(up,('asks',)),depth_cash(down,('asks',)),
        (now-cb['received_at_ms'])/1000,(now-up['received_at_ms'])/1000,(now-down['received_at_ms'])/1000,missing_velocity])
    if len(values)!=len(EXTRA_NAMES) or not all(math.isfinite(v) for v in values):raise ValueError('Invalid causal feature output')
    return values
