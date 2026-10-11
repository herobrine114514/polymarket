import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoinbaseState, PolymarketState, RawJournal, StorageLimit, sourceTime, freezeDecision, activeMarketAssets } from '../polycast/microstructure_frames.mjs';

const base = Date.parse('2026-10-06T00:00:00Z');
const time = offset => new Date(base + offset).toISOString();
const snapshot = { type: 'snapshot', product_id: 'BTC-USD', bids: [['60000', '2']], asks: [['60001', '3']] };
const match = (id, side, offset) => ({ type: 'match', product_id: 'BTC-USD', trade_id: id,
  time: time(offset), side, size: '1', price: '60001' });
const book = { event_type: 'book', asset_id: '1', timestamp: String(base),
  bids: [{ price: '.4', size: '2' }], asks: [{ price: '.5', size: '3' }] };

test('maker sell is taker buy; last_match seed never becomes observed flow', () => {
  const state = new CoinbaseState(); state.reset('connection'); state.ingest(snapshot, base);
  state.ingest({ ...match(10, 'buy', -50000), type: 'last_match' }, base);
  state.ingest(match(11, 'sell', 11000), base + 11000);
  state.ingest({ type: 'heartbeat', product_id: 'BTC-USD', last_trade_id: 11 }, base + 11000);
  const summary = state.summary(base + 12000);
  assert.equal(summary.flow['10s'].complete, true); assert.equal(summary.flow['10s'].trades, 1);
  assert.equal(summary.flow['10s'].taker_imbalance, 1); assert.equal(summary.book, null, 'stale snapshot excluded');
});

test('trade gap, reconnect, and missing heartbeat prevent complete-window claims', () => {
  const state = new CoinbaseState(); state.reset('one'); state.ingest(snapshot, base);
  state.ingest({ ...match(10, 'buy', 0), type: 'last_match' }, base);
  state.ingest(match(12, 'buy', 11000), base + 11000);
  state.ingest({ type: 'heartbeat', product_id: 'BTC-USD', last_trade_id: 12 }, base + 11000);
  assert.equal(state.summary(base + 12000).flow['10s'].taker_imbalance, null);
  assert.equal(state.summary(base + 22000).flow['10s'].complete, false, 'heartbeat expired');
  state.reset('two'); assert.equal(state.summary(base + 23000).book, null);
  assert.equal(state.summary(base + 23000).flow['10s'].complete, false);
});

test('absolute L2 sizes replace depth; zero removes; unordered update invalidates', () => {
  const state = new CoinbaseState(); state.reset('one'); state.ingest(snapshot, base);
  state.ingest({ type: 'l2update', product_id: 'BTC-USD', time: time(100),
    changes: [['buy', '60000', '5'], ['buy', '59999', '4']] }, base + 101);
  assert.deepEqual(state.summary(base + 102).book.bids, [[60000, 5], [59999, 4]]);
  state.ingest({ type: 'l2update', product_id: 'BTC-USD', time: time(200), changes: [['buy', '60000', '0']] }, base + 201);
  assert.equal(state.summary(base + 202).book.bid, 59999);
  state.ingest({ type: 'l2update', product_id: 'BTC-USD', time: time(150), changes: [] }, base + 203);
  assert.equal(state.summary(base + 204).book, null);
});

test('uninitialized Polymarket deltas and future source timestamps cannot become features', () => {
  const state = new PolymarketState(); state.reset('connection', ['1']);
  state.ingest({ event_type: 'price_change', timestamp: String(base), price_changes: [
    { asset_id: '1', side: 'BUY', price: '.4', size: '2' }] }, base);
  assert.equal(state.summary('1', base), null);
  state.ingest({ ...book, timestamp: String(base + 2000) }, base + 1);
  assert.equal(state.summary('1', base + 1000), null);
  assert.equal(state.summary('1', base + 2001).midpoint, .45);
});

test('reported Polymarket top mismatch quarantines the book until a snapshot', () => {
  const state = new PolymarketState(); state.reset('connection', ['1']); state.ingest(book, base);
  state.ingest({ event_type: 'price_change', timestamp: String(base + 100), price_changes: [
    { asset_id: '1', side: 'BUY', price: '.41', size: '2', best_bid: '.42', best_ask: '.5' }] }, base + 101);
  assert.equal(state.needsSnapshot(), true); assert.equal(state.summary('1', base + 102), null);
  state.ingest({ ...book, timestamp: String(base + 200) }, base + 201);
  assert.equal(state.needsSnapshot(), false); assert.equal(state.summary('1', base + 202).midpoint, .45);
});

test('future book quarantine preserves active depth and becomes required on activation', () => {
  const markets = [
    { start_at_ms: base, end_at_ms: base + 300000, up_asset: '1', down_asset: '2' },
    { start_at_ms: base + 300000, end_at_ms: base + 600000, up_asset: '3', down_asset: '4' }
  ];
  const state = new PolymarketState(); state.reset('connection', ['1', '2', '3', '4']);
  for (const asset of ['1', '2', '3', '4']) state.ingest({ ...book, asset_id: asset }, base);
  state.ingest({ event_type: 'price_change', timestamp: String(base + 100), price_changes: [
    { asset_id: '3', side: 'BUY', price: '.41', size: '2', best_bid: '.42', best_ask: '.5' }] }, base + 101);
  const active = activeMarketAssets(markets, base + 102);
  assert.deepEqual(active, ['1', '2']);
  assert.equal(state.needsSnapshot(), true);
  assert.equal(state.needsSnapshot(active), false);
  assert.equal(state.summary('1', base + 102).midpoint, .45);
  assert.equal(state.summary('3', base + 102), null, 'quarantine is not repaired by scope filtering');
  assert.deepEqual(state.snapshotReasons(activeMarketAssets(markets, base + 300000)),
    [{ asset_id: '3', reason: 'reported_top_mismatch' }]);
  assert.deepEqual(activeMarketAssets(markets, base + 600000), []);
  state.ingest({ ...book, asset_id: '3', timestamp: String(base + 200) }, base + 201);
  assert.equal(state.needsSnapshot(), false, 'only a fresh full snapshot restores depth');
});

test('trade and ticker source clocks do not invalidate an unrelated depth stream', () => {
  const state = new PolymarketState(); state.reset('connection', ['1']); state.ingest(book, base);
  state.ingest({ event_type: 'last_trade_price', asset_id: '1', timestamp: String(base - 2), price: '.4' }, base + 1);
  assert.equal(state.needsSnapshot(), false); assert.equal(state.summary('1', base + 2).midpoint, .45);
});

test('journal rotates without overwrite and refuses cap overflow without deleting raw data', () => {
  const directory = mkdtempSync(join(tmpdir(), 'polycast-journal-'));
  try {
    const journal = new RawJournal(directory, 'session', { segmentBytes: 1024, totalBytes: 2048 });
    journal.append({ raw_text: 'x'.repeat(700) }); journal.append({ raw_text: 'y'.repeat(700) });
    assert.throws(() => journal.append({ raw_text: 'z'.repeat(700) }), StorageLimit); journal.close();
    const files = readdirSync(directory).filter(name => name.endsWith('.jsonl')).sort(); assert.equal(files.length, 2);
    assert.equal(JSON.parse(readFileSync(join(directory, files[0]))).record_seq, 1);
    assert.equal(JSON.parse(readFileSync(join(directory, files[1]))).record_seq, 2);
    const restarted = new RawJournal(directory, 'next', { segmentBytes: 1024, totalBytes: 2048 });
    assert.throws(() => restarted.append({ raw_text: 'z'.repeat(700) }), StorageLimit); restarted.close();
    assert.equal(readdirSync(directory).filter(name => name.endsWith('.jsonl')).length, 2);
    const seal = JSON.parse(readFileSync(join(directory, files[0] + '.seal.json')));
    assert.equal(seal.bytes, readFileSync(join(directory, files[0])).length);
    assert.equal(seal.first_record_seq, 1); assert.equal(seal.last_record_seq, 1);
  } finally { rmSync(directory, { recursive: true }); }
});

test('quality distinguishes stale data, one-sided books and missing initialization', () => {
  const state = new PolymarketState(); state.reset('connection', ['1']);
  assert.equal(state.quality('1', base).reason, 'awaiting_snapshot');
  state.ingest(book, base); assert.equal(state.quality('1', base + 6000).reason, 'stale_receipt');
  state.ingest({ ...book, timestamp: String(base + 7000), asks: [] }, base + 7000);
  assert.equal(state.quality('1', base + 7001).reason, 'one_sided_book');
});

test('timestamps require explicit source timezone and do not fabricate snapshot time', () => {
  assert.equal(sourceTime(snapshot, 'coinbase'), null);
  assert.equal(sourceTime({ time: '2026-10-06T00:00:00' }, 'coinbase'), null);
  assert.equal(sourceTime({ timestamp: String(base) }, 'polymarket'), base);
});

test('event decision freezes at the declared time and never replaces its first snapshot', () => {
  const directory = mkdtempSync(join(tmpdir(), 'polycast-frozen-'));
  const record = { market: { market_id: '1', start_at_ms: base }, paired: true, received_at_ms: base + 120000, q: .6 };
  try {
    assert.equal(freezeDecision(directory, { ...record, received_at_ms: base + 119999 }), false);
    assert.equal(freezeDecision(directory, { ...record, received_at_ms: base + 123001 }), false);
    assert.equal(freezeDecision(directory, { ...record, paired: false }), false);
    assert.equal(freezeDecision(directory, record), true);
    assert.equal(freezeDecision(directory, { ...record, q: .9 }), false);
    const frozen = JSON.parse(readFileSync(join(directory, 'decisions/event-1.json')));
    assert.equal(JSON.parse(frozen.snapshot_json).q, .6);
    assert.equal(readdirSync(join(directory, 'decisions')).length, 1);
  } finally { rmSync(directory, { recursive: true }); }
});

test('UTC checked decisions use the corrected time and preserve raw local receipts', () => {
  const directory = mkdtempSync(join(tmpdir(), 'polycast-utc-freeze-'));
  const record = { market: { market_id: '1', start_at_ms: base }, paired: true,
    feature_version: 'btc5m-public-microstructure-v2-clock-checked', received_at_ms: base + 123000,
    as_of_utc_ms: base + 120000, clock_reference: { status: 'venues_agree', offset_ms: -3000 } };
  try {
    assert.equal(freezeDecision(directory, { ...record, clock_reference: null }), false);
    assert.equal(freezeDecision(directory, { ...record, as_of_utc_ms: base + 119999 }), false);
    assert.equal(freezeDecision(directory, record), true);
    const saved = JSON.parse(readFileSync(join(directory, 'decisions/event-1.json')));
    assert.equal(saved.decision_protocol, 'utc-start-plus-120s-tolerance-3s-v2');
    assert.equal(JSON.parse(saved.snapshot_json).received_at_ms, base + 123000);
  } finally { rmSync(directory, { recursive: true }); }
});

test('complete causal V3 inputs use a separate immutable decision namespace',()=>{
  const directory=mkdtempSync(join(tmpdir(),'polycast-v3-freeze-'));
  const record={market:{market_id:'1',start_at_ms:base},paired:true,received_at_ms:base+120000,as_of_utc_ms:base+120000,
    clock_reference:{status:'venues_agree'},feature_version:'btc5m-causal-features-v3'};
  try{
    assert.equal(freezeDecision(directory,record),false);
    assert.equal(freezeDecision(directory,{...record,causal_complete:true}),true);
    assert.equal(freezeDecision(directory,{...record,causal_complete:true,changed:true}),false);
    const frozen=JSON.parse(readFileSync(join(directory,'decisions-v3/event-1.json')));
    assert.equal(frozen.decision_protocol,'utc-start-plus-120s-tolerance-3s-v3');
    assert.equal(readdirSync(directory).includes('decisions'),false);
  }finally{rmSync(directory,{recursive:true});}
});

test('source clock correction works when the local clock lags the venue', () => {
  const cb = new CoinbaseState(); cb.reset('clock'); cb.ingest(snapshot, base, base + 7000);
  cb.ingest({ type: 'l2update', product_id: 'BTC-USD', time: time(6000), changes: [] }, base + 1, base + 7001);
  assert.equal(cb.summary(base + 2).book, null);
  assert.equal(cb.summary(base + 2, base + 7002).book.midpoint, 60000.5);
});
