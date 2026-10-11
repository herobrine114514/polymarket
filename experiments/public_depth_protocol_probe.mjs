// Finite public-only diagnostic. No forecast, order, account or private imports.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { RawJournal, PolymarketState, sourceTime } from '../polycast/microstructure_frames.mjs';

const [directory, selectionPath, clockPath, secondsText = '45', mode = 'active'] = process.argv.slice(2);
const seconds = Number(secondsText);
if (!directory || !selectionPath || !clockPath || !Number.isInteger(seconds) || seconds < 10 || seconds > 120 || !['active', 'next'].includes(mode)) process.exit(2);
const selection = JSON.parse(readFileSync(selectionPath, 'utf8'));
const clock = JSON.parse(readFileSync(clockPath, 'utf8'));
if (clock.status !== 'venues_agree' || !Number.isFinite(clock.offset_ms) || Math.abs(clock.offset_ms) > 10000 ||
    Date.now() - clock.checked_at_local_ms > 30000) throw new Error('Fresh checked public clock required');
const utcNow = () => Math.round(Date.now() + clock.offset_ms);
const market = mode === 'active' ? selection.markets.find(item => item.start_at_ms <= utcNow() && utcNow() < item.end_at_ms) :
  selection.markets.filter(item => item.start_at_ms > utcNow()).sort((a,b)=>a.start_at_ms-b.start_at_ms)[0];
if (!market || !/^\d+$/.test(market.up_asset) || !/^\d+$/.test(market.down_asset)) throw new Error('Selected BTC 5m asset pair required');
const assets = [market.up_asset, market.down_asset], session = randomUUID();
const journal = new RawJournal(directory, session, { totalBytes: 128 * 1024 ** 2 });
const lanes = new Map(), sockets = [], pending = new Set();
let stopping = false, requests = null, tick = null;
const report = { status: 'starting', public_only: true, diagnostic_only: true, market, session_id: session,
  clock_reference: clock, market_mode: mode, started_at_ms: Date.now(), ws_levels: {}, rest: { requests: 0, succeeded: 0, failed: 0,
    rtt_ms: [], invalidations: {}, ticks: 0, paired_ticks: 0, all_ticks: 0, paired_all_ticks: 0 }, forecasts_or_orders: false };

function append(value) {
  try { journal.append({ ...value, received_at_ms: Date.now(), monotonic_ns: process.hrtime.bigint().toString() }); }
  catch { stop('storage_error', 3); }
}
function makeState(name, detail) {
  const state = new PolymarketState(); state.reset(name, assets);
  for (const book of state.books.values()) {
    const invalidate = book.invalidate.bind(book);
    book.invalidate = reason => {
      if (!['awaiting_snapshot', 'reconnect'].includes(reason)) {
        detail.invalidations[reason] = (detail.invalidations[reason] || 0) + 1;
        append({ kind: 'reducer_invalidated', lane: name, reason });
      }
      invalidate(reason);
    };
  }
  return state;
}
for (const [name, level] of [['default', null], ['level2', 2], ['level3', 3]]) {
  const detail = report.ws_levels[name] = { level, frames: 0, types: {}, invalidations: {}, ticks: 0, paired_ticks: 0,
    all_ticks: 0, paired_all_ticks: 0, errors: 0 };
  const state = makeState(name, detail); lanes.set(name, state);
  const socket = new WebSocket('wss://ws-subscriptions-clob.polymarket.com/ws/market'); sockets.push(socket);
  socket.onopen = () => socket.send(JSON.stringify({ type: 'market', assets_ids: assets,
    initial_dump: true, custom_feature_enabled: true, ...(level !== null ? { level } : {}) }));
  socket.onmessage = event => {
    if (stopping) return;
    const now = Date.now(), raw = String(event.data); detail.frames++;
    append({ kind: 'raw_ws', lane: name, raw_text: raw });
    let payload; try { payload = JSON.parse(raw); } catch { return; }
    for (const message of Array.isArray(payload) ? payload : [payload]) {
      const type = message?.event_type ?? 'unknown'; detail.types[type] = (detail.types[type] || 0) + 1;
      state.ingest(message, now, Math.round(now + clock.offset_ms));
    }
    // Intentionally preserve the connection after invalidation to observe full snapshots later.
    // Invalid state never becomes a forecast or a usable production feature.
  };
  socket.onerror = () => detail.errors++;
  socket.onclose = () => { detail.closed = true; };
}
const restState = makeState('rest', report.rest);
lanes.set('rest', restState);
async function restSnapshot(asset) {
  report.rest.requests++;
  const sent = Date.now(), request = randomUUID(), mono = process.hrtime.bigint();
  append({ kind: 'http_request', request_id: request, asset, sent_at_ms: sent });
  try {
    const response = await fetch('https://clob.polymarket.com/book?token_id=' + asset,
      { signal: AbortSignal.timeout(4000), headers: { 'Accept': 'application/json' } });
    const raw = await response.text(), received = Date.now();
    const rtt = Number(process.hrtime.bigint() - mono) / 1e6; report.rest.rtt_ms.push(rtt);
    append({ kind: 'raw_http', request_id: request, asset, sent_at_ms: sent, status: response.status, rtt_ms: rtt, raw_text: raw });
    const body = JSON.parse(raw);
    if (!response.ok || body.asset_id !== asset || sourceTime(body, 'polymarket') === null) throw new Error('invalid_book_response');
    restState.ingest({ ...body, event_type: 'book' }, received, Math.round(received + clock.offset_ms));
    report.rest.succeeded++;
  } catch (error) {
    report.rest.failed++;
    append({ kind: 'http_failure', request_id: request, asset, reason: error.name });
  }
}
function fetchPair() {
  for (const asset of assets) {
    if (pending.has(asset)) continue;
    pending.add(asset); restSnapshot(asset).finally(() => pending.delete(asset));
  }
}
fetchPair();
requests = setInterval(fetchPair, 2000);
tick = setInterval(() => {
  const now = Date.now(), utc = Math.round(now + clock.offset_ms);
  const active = market.start_at_ms <= utc && utc < market.end_at_ms;
  const details = {};
  for (const [name, state] of lanes) {
    const detail = name === 'rest' ? report.rest : report.ws_levels[name];
    const up = state.summary(assets[0], now, utc), down = state.summary(assets[1], now, utc);
    detail.all_ticks++; if (up && down) detail.paired_all_ticks++;
    if (active) { detail.ticks++; if (up && down) detail.paired_ticks++; }
    details[name] = { up, down, active, up_quality: state.quality(assets[0],now,utc), down_quality: state.quality(assets[1],now,utc) };
  }
  append({ kind: 'diagnostic_tick', as_of_utc_ms: utc, lanes: details });
  for (const socket of sockets) if (socket.readyState === 1 && now % 10000 < 1500) socket.send('PING');
}, 1000);
function stop(reason = 'duration_complete', code = 0) {
  if (stopping) return;
  stopping = true; clearInterval(requests); clearInterval(tick);
  for (const socket of sockets) { socket.onclose = null; if (socket.readyState <= 1) socket.close(); }
  journal.close();
  report.status = reason; report.finished_at_ms = Date.now(); report.raw_records = journal.sequence;
  report.limitations = ['Finite connectivity/coverage diagnostic only; no prediction or execution performance',
    'Connections start at slightly different times; traffic counts are not synchronized event comparisons',
    'REST and WS snapshots can refer to different venue states even at adjacent receipt times',
    'No inferred level deletions, manufactured depth or missed-event estimates'];
  writeFileSync(join(directory, 'probe.json'), JSON.stringify(report, null, 2));
  process.exit(code);
}
if (process.env.POLYCAST_PARENT_PIPE === '1') {
  process.stdin.on('end', () => stop('owner_closed')); process.stdin.resume();
}
process.on('SIGTERM', () => stop('interrupted'));
setTimeout(() => stop(), seconds * 1000);
