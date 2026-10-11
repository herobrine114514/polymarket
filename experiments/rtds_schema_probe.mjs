// Public, bounded RTDS schema inventory. No trading DB or model call.
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const output = process.argv[2];
if (!output) process.exit(2);
const startedAt = new Date().toISOString();
const samples = [];
const socket = new WebSocket('wss://ws-live-data.polymarket.com');
let ended = false;
function finish(status) {
  if (ended) return;
  ended = true;
  const result = {
    started_at: startedAt, finished_at: new Date().toISOString(), status,
    endpoint: 'wss://ws-live-data.polymarket.com', topic: 'crypto_prices_twap_sixty',
    symbol: 'btc/usd', window_seconds: 60, samples,
    scope: 'Only the observed public message schema; not a signature verifier or settlement input',
  };
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
  socket.close();
  console.log(JSON.stringify({ status, frames: samples.length,
    report_fields_seen: samples.some(s => s.report_fields.length > 0) }));
  process.exit(status === 'sampled' ? 0 : 1);
}
const timer = setTimeout(() => finish(samples.length ? 'sampled' : 'unavailable'), 20000);
socket.onopen = () => socket.send(JSON.stringify({ action: 'subscribe', subscriptions: [{
  topic: 'crypto_prices_twap_sixty', type: 'update', filters: JSON.stringify({ symbol: 'btc/usd' }),
}] }));
socket.onmessage = event => {
  try {
    const frame = JSON.parse(String(event.data));
    if (frame.topic !== 'crypto_prices_twap_sixty' || frame.payload?.symbol !== 'btc/usd' ||
        frame.payload.window_s !== 60) return;
    const points = Array.isArray(frame.payload.data) ? frame.payload.data : [frame.payload];
    const pointKeys = [...new Set(points.flatMap(p => Object.keys(p)))].sort();
    const keys = [...Object.keys(frame), ...Object.keys(frame.payload), ...pointKeys];
    const timestamps = points.map(p => Number(p.timestamp)).filter(Number.isSafeInteger);
    samples.push({ received_at: new Date().toISOString(), frame_keys: Object.keys(frame).sort(),
      payload_keys: Object.keys(frame.payload).sort(), point_keys: pointKeys,
      points: points.length, first_timestamp_ms: timestamps.length ? Math.min(...timestamps) : null,
      last_timestamp_ms: timestamps.length ? Math.max(...timestamps) : null,
      report_fields: [...new Set(keys.filter(k => /report|signature|digest|signer/i.test(k)))].sort(),
    });
    if (samples.length >= 3) { clearTimeout(timer); finish('sampled'); }
  } catch { /* Non-JSON heartbeat messages are outside this inventory. */ }
};
socket.onerror = () => { clearTimeout(timer); finish('connection_error'); };
