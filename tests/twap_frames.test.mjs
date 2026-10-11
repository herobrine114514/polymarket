import assert from 'node:assert/strict';
import test from 'node:test';
import { acceptedTwapPoints, TwapFrameDecoder } from '../polycast/twap_frames.mjs';

const now = 1_790_600_000_000;
const value = '83763475604562545475584';
const base = { topic: 'crypto_prices_twap_sixty', type: 'update',
  payload: { symbol: 'btc/usd', window_s: 60, timestamp: now - 1000,
    full_accuracy_value: value } };

test('live direct RTDS update is accepted', () => {
  assert.deepEqual(acceptedTwapPoints(base, now),
    [{ timestamp_ms: now - 1000, full_accuracy_value: value }]);
});

test('batch, stale, duplicate, and wrong-symbol frames', () => {
  const batch = { ...base, payload: { ...base.payload, data: [
    { timestamp: now - 2000, full_accuracy_value: value },
    { timestamp: now - 1000, full_accuracy_value: value },
  ] } };
  assert.equal(acceptedTwapPoints(batch, now).length, 2);
  assert.deepEqual(acceptedTwapPoints(base, now, now - 1000), []);
  assert.deepEqual(acceptedTwapPoints(base, now + 121_000), []);
  assert.deepEqual(acceptedTwapPoints({ ...base, payload: { ...base.payload,
    symbol: 'eth/usd' } }, now), []);
});

const polybolt = { v: 1, channel: 'price.crypto.twap', seq: 3, ts: now,
  snapshot: true, payload: { symbol: 'btcusd', source: 'chainlink', window_seconds: 60,
    timestamp: now - 1000, value: 83763.47, full_accuracy_value: '83763.475604562545475584' } };

test('raw decimal and legacy fixed-point records have explicit distinct units', () => {
  const decoder = new TwapFrameDecoder('connection-1');
  const [record] = decoder.decode(polybolt, now);
  assert.equal(record.full_accuracy_value, '83763.475604562545475584');
  assert.equal(record.price_encoding, 'decimal_usd');
  assert.equal(record.received_at_ms, now);
  const [legacy] = decoder.decode(base, now);
  assert.equal(legacy.price_encoding, 'e18');
  assert.equal(legacy.full_accuracy_value, value);
});

test('a gap quarantines subsequent updates until a fresh snapshot', () => {
  const decoder = new TwapFrameDecoder('connection-1');
  assert.equal(decoder.decode(polybolt, now).length, 1);
  assert.equal(decoder.decode({ ...polybolt, snapshot: false, seq: 5 }, now).length, 0);
  assert.equal(decoder.decode({ ...polybolt, snapshot: false, seq: 6 }, now).length, 0);
  assert.equal(decoder.decode({ ...polybolt, snapshot: true, seq: 7 }, now).length, 1);
  assert.equal(decoder.decode({ ...polybolt, snapshot: false, seq: 8 }, now).length, 1);
});

test('sequence is connection-local and repeated snapshots cannot rewind it', () => {
  const first = new TwapFrameDecoder('connection-1');
  first.decode(polybolt, now);
  assert.deepEqual(first.decode(polybolt, now), []);
  const next = new TwapFrameDecoder('connection-2');
  assert.equal(next.decode({ ...polybolt, seq: 1 }, now).length, 1);
  assert.equal(next.decode({ ...polybolt, seq: 0 }, now).length, 0);
});

test('missing bootstrap, dropped frames and display-only values are rejected', () => {
  assert.deepEqual(new TwapFrameDecoder('connection-1').decode({ ...polybolt, snapshot: false }, now), []);
  assert.deepEqual(new TwapFrameDecoder('connection-1').decode({ ...polybolt, dropped: 1 }, now), []);
  const missing = { ...polybolt, payload: { ...polybolt.payload, full_accuracy_value: undefined } };
  assert.deepEqual(new TwapFrameDecoder('connection-1').decode(missing, now), []);
  for (const changes of [{ source: 'pyth' }, { window_seconds: 30 }, { symbol: 'btcusdt' }]) {
    assert.deepEqual(new TwapFrameDecoder('connection-1').decode(
      { ...polybolt, payload: { ...polybolt.payload, ...changes } }, now), []);
  }
});

test('SDK decimal-string snapshots and ISO timestamps normalize without floats', () => {
  const decoder = new TwapFrameDecoder('sdk-connection');
  const snapshot = { topic: 'prices.crypto.twap', type: 'subscribe', seq: 1,
    payload: { symbol: 'btcusd', windowSeconds: 60,
      data: [{ timestamp: new Date(now - 1000).toISOString(), value: '83763.475604562545475584' }] } };
  const [record] = decoder.decode(snapshot, now);
  assert.equal(record.timestamp_ms, now - 1000);
  assert.equal(record.feed_protocol, 'polybolt_sdk_v1');
  assert.equal(record.full_accuracy_value, '83763.475604562545475584');
  assert.deepEqual(decoder.decode({ ...snapshot, seq: 2,
    payload: { ...snapshot.payload, data: [{ timestamp: now - 1000, value: 83763.47 }] } }, now), []);
});

test('empty history can bootstrap and legacy out-of-order history is sorted', () => {
  const decoder = new TwapFrameDecoder('connection-1');
  assert.deepEqual(decoder.decode({ ...polybolt, payload: { ...polybolt.payload, data: [] } }, now), []);
  assert.equal(decoder.decode({ ...polybolt, snapshot: false, seq: 4 }, now).length, 1);
  const rows = decoder.decode({ ...base, payload: { ...base.payload, data: [
    { timestamp: now - 1000, full_accuracy_value: value },
    { timestamp: now - 2000, full_accuracy_value: value },
  ] } }, now);
  assert.deepEqual(rows.map(r => r.timestamp_ms), [now - 2000, now - 1000]);
});
