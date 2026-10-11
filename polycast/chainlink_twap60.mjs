// Persist public Polymarket RTDS BTC/USD 60s TWAP observations for paper research.
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { TwapFrameDecoder } from './twap_frames.mjs';

const output = process.argv[2];
const durationSeconds = Number(process.argv[3] || 1800);
if (!output || !Number.isInteger(durationSeconds) || durationSeconds < 60 || durationSeconds > 7200) {
  process.exit(2);
}
mkdirSync(dirname(output), { recursive: true });
const deadline = Date.now() + durationSeconds * 1000;
let socket;
let reconnectTimer;
let refreshTimer;
const observedVersions = new Map();

function stop() {
  clearTimeout(reconnectTimer);
  clearTimeout(refreshTimer);
  if (socket && socket.readyState <= 1) socket.close();
  process.exit(0);
}
// The dashboard owns the write end of this pipe. Windows task termination
// closes it even when Python cannot run its normal child cleanup.
if (process.env.POLYCAST_PARENT_PIPE === '1') {
  process.stdin.on('end', stop);
  process.stdin.on('error', () => process.exit(1));
  process.stdin.resume();
}

function connect() {
  if (Date.now() >= deadline) return;
  const decoder = new TwapFrameDecoder(randomUUID());
  socket = new WebSocket('wss://ws-live-data.polymarket.com');
  socket.onopen = () => {
    socket.send(JSON.stringify({
      action: 'subscribe',
      subscriptions: [{ topic: 'crypto_prices_twap_sixty', type: 'update',
        filters: JSON.stringify({ symbol: 'btc/usd' }) }],
    }));
    // The relay sometimes sends only an initial 60s batch. Refresh the
    // subscription before that batch falls out of the observation window.
    refreshTimer = setTimeout(() => socket.close(), 12000);
  };
  socket.onmessage = (event) => {
    try {
      const frame = JSON.parse(String(event.data));
      const receivedAt = Date.now();
      for (const point of decoder.decode(frame, receivedAt)) {
        // Retain corrections for conflict diagnostics, but not identical reconnect history.
        const key = `${point.timestamp_ms}:${point.full_accuracy_value}`;
        if (observedVersions.has(key)) continue;
        appendFileSync(output, JSON.stringify(point) + '\n');
        observedVersions.set(key, true);
        if (observedVersions.size > 1000) observedVersions.delete(observedVersions.keys().next().value);
      }
    } catch { /* Ignore malformed transport frames. */ }
  };
  socket.onclose = () => {
    clearTimeout(refreshTimer);
    if (Date.now() < deadline) reconnectTimer = setTimeout(connect, 2000);
  };
  socket.onerror = (event) => {
    process.stderr.write(`RTDS websocket error: ${event.error?.message || 'connection failed'}\n`);
    socket.close();
  };
}
connect();
setTimeout(stop, durationSeconds * 1000);
