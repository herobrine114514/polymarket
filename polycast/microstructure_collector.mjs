// Same-process public BTC-USD and Polymarket capture. Never imports credentials or order clients.
import { readFileSync, writeFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { RawJournal, StorageLimit, CoinbaseState, PolymarketState, sourceTime, freezeDecision, activeMarketAssets } from './microstructure_frames.mjs';
import { ReferenceCursor, CausalHistory, CAUSAL_VERSION } from './causal_context.mjs';

const [directory, selectionPath, durationText = '7200', capText = String(8 * 1024 ** 3), referencePath] = process.argv.slice(2);
const duration = Number(durationText), capacity = Number(capText);
if (!directory || !selectionPath || !Number.isInteger(duration) || duration < 5 || duration > 7200 ||
    !Number.isSafeInteger(capacity) || capacity < 1024 * 1024) process.exit(2);
const session = randomUUID(), journal = new RawJournal(directory, session, {
  totalBytes: capacity, segmentBytes: Math.min(32 * 1024 ** 2, capacity) });
const deadline = Date.now() + duration * 1000;
const sourceHashes = Object.fromEntries(['microstructure_frames.mjs', 'microstructure_collector.mjs', 'causal_context.mjs'].map(name =>
  [name, createHash('sha256').update(readFileSync(new URL(name, import.meta.url))).digest('hex')]));
const statePath = join(directory, 'state.json');
const coinbase = new CoinbaseState(), polymarket = new PolymarketState();
const sockets = new Map(), retryTimers = new Map();
let stopping = false, markets = [], selectionDigest = null, lastSelectionCheck = 0;
let previousWall = Date.now(), previousMono = process.hrtime.bigint(), clockSteps = 0;
let clockReference = null;
let blockedClockId = null;
const history = new CausalHistory(), reference = new ReferenceCursor(referencePath ?? join(directory, '..', 'chainlink_twap60.jsonl'));
let observedQuarantines = new Set();
let recoveryTimer = null, recoverySince = null, recoverySinceMono = null;
const SNAPSHOT_GRACE_MS = 1000;
const status = { schema_version: 1, session_id: session, pid: process.pid, status: 'starting',
  started_at_ms: Date.now(), public_only: true, clock_verification: 'local_wall_clock_not_independently_verified',
  venues: { coinbase: {}, polymarket: {} }, feature_records: 0, paired_feature_records: 0, frozen_events: 0,
  capture_revision: 'public-microstructure-v3-causal-context', snapshot_grace_ms: SNAPSHOT_GRACE_MS,
  causal_feature_records: 0, causal_complete_records: 0, causal_frozen_events: 0,
  source_hashes: sourceHashes, gap_counts: {}, missing_book_counts: {} };

function receipt() { return { received_at_ms: Date.now(), monotonic_ns: process.hrtime.bigint().toString() }; }
function checkedClock(now) {
  try {
    const clock = JSON.parse(readFileSync(join(directory, 'clock.json'), 'utf8'));
    if (clock.schema_version !== 1 || clock.status !== 'venues_agree' || typeof clock.clock_id !== 'string' || clock.clock_id === blockedClockId ||
        !Number.isFinite(clock.offset_ms) || Math.abs(clock.offset_ms) > 10000 ||
        !Number.isFinite(clock.uncertainty_ms) || clock.uncertainty_ms < 0 || clock.uncertainty_ms > 750 ||
        !Number.isSafeInteger(clock.checked_at_local_ms) || now < clock.checked_at_local_ms ||
        now - clock.checked_at_local_ms > 120000) return null;
    return { status: clock.status, clock_id: clock.clock_id, offset_ms: clock.offset_ms,
      uncertainty_ms: clock.uncertainty_ms, checked_at_local_ms: clock.checked_at_local_ms };
  } catch { return null; }
}
function append(record) {
  try { return journal.append(record); }
  catch (error) { stop(error instanceof StorageLimit ? 'storage_limit' : 'storage_error', 3); return null; }
}
function saveStatus() {
  status.updated_at_ms = Date.now(); status.raw_records = journal.sequence;
  status.storage_bytes = journal.bytes; status.storage_limit_bytes = capacity; status.clock_steps = clockSteps;
  status.active_segment = journal.activeName; status.ingested_bytes = journal.writtenBytes;
  status.selection_digest = selectionDigest; status.market_ids = markets.map(market => market.market_id);
  try {
    writeFileSync(statePath + '.tmp', JSON.stringify(status)); renameSync(statePath + '.tmp', statePath);
  } catch { process.stderr.write('Microstructure status write failed\n'); }
}
function stop(reason = 'duration_complete', code = 0) {
  if (stopping) return;
  stopping = true; status.status = reason;
  clearTimeout(recoveryTimer);
  for (const timer of retryTimers.values()) clearTimeout(timer);
  for (const socket of sockets.values()) { socket.onclose = null; if (socket.readyState <= 1) socket.close(); }
  try { journal.close(); } catch { status.status = 'storage_error'; code = 3; }
  saveStatus(); process.exit(code);
}
function recoverPolymarket(ws, arrived, sourceNow) {
  if (sockets.get('polymarket') !== ws || ws.readyState !== 1) return;
  const all = polymarket.snapshotReasons();
  const keys = new Set(all.map(item => `${item.asset_id}:${item.reason}`));
  const newlyQuarantined = all.filter(item => !observedQuarantines.has(`${item.asset_id}:${item.reason}`));
  observedQuarantines = keys;
  status.quarantined_polymarket_books = all;
  const active = clockReference ? activeMarketAssets(markets, sourceNow) : null;
  if (newlyQuarantined.length) {
    for (const item of newlyQuarantined) status.gap_counts[item.reason] = (status.gap_counts[item.reason] || 0) + 1;
    append({ kind: 'book_quarantine', venue: 'polymarket', connection_id: polymarket.connection,
      reasons: newlyQuarantined, active_assets: active, recovery_scope: active === null ? 'clock_unverified_all' : 'active_market_only', ...arrived });
  }
  const reasons = polymarket.snapshotReasons(active);
  if (!reasons.length) {
    clearTimeout(recoveryTimer); recoveryTimer = null; recoverySince = null; recoverySinceMono = null;
    status.snapshot_wait_started_at_ms = null;
    return;
  }
  if (recoverySince === null) {
    recoverySince = arrived.received_at_ms;
    recoverySinceMono = BigInt(arrived.monotonic_ns);
    status.snapshot_wait_started_at_ms = recoverySince;
  }
  const waited = Number(BigInt(arrived.monotonic_ns) - recoverySinceMono) / 1e6;
  if (waited < SNAPSHOT_GRACE_MS) {
    if (recoveryTimer !== null) return;
    recoveryTimer = setTimeout(() => {
      recoveryTimer = null;
      if (stopping || sockets.get('polymarket') !== ws) return;
      const stamp = receipt(); clockReference = checkedClock(stamp.received_at_ms);
      recoverPolymarket(ws, stamp, clockReference ? Math.round(stamp.received_at_ms + clockReference.offset_ms) : stamp.received_at_ms);
    }, Math.ceil(SNAPSHOT_GRACE_MS - waited));
    recoveryTimer.unref();
    return;
  }
  clearTimeout(recoveryTimer); recoveryTimer = null;
  status.venues.polymarket.close_reason = 'snapshot_required';
  append({ kind: 'capture_gap', venue: 'polymarket', connection_id: polymarket.connection,
    reason: 'snapshot_required', reasons, active_assets: active, snapshot_wait_ms: waited,
    recovery_scope: active === null ? 'clock_unverified_all' : 'active_market_only', ...arrived });
  ws.close();
}
if (process.env.POLYCAST_PARENT_PIPE === '1') {
  process.stdin.on('end', () => stop('owner_closed')); process.stdin.on('error', () => stop('owner_error', 1)); process.stdin.resume();
}
process.on('SIGINT', () => stop('interrupted')); process.on('SIGTERM', () => stop('interrupted'));

function connect(venue) {
  if (stopping || Date.now() >= deadline) return;
  const connection = randomUUID();
  const assets = [...new Set(markets.flatMap(market => [market.up_asset, market.down_asset]))];
  if (venue === 'polymarket' && !assets.length) { status.venues[venue] = { status: 'awaiting_market' }; return; }
  const ws = new WebSocket(venue === 'coinbase' ? 'wss://ws-feed.exchange.coinbase.com' :
    'wss://ws-subscriptions-clob.polymarket.com/ws/market');
  sockets.set(venue, ws);
  if (venue === 'coinbase') coinbase.reset(connection);
  else {
    polymarket.reset(connection, assets); observedQuarantines = new Set(); status.quarantined_polymarket_books = [];
    clearTimeout(recoveryTimer); recoveryTimer = null; recoverySince = null; recoverySinceMono = null; status.snapshot_wait_started_at_ms = null;
  }
  const detail = status.venues[venue] = { status: 'connecting', connection_id: connection, messages: 0,
    last_received_at_ms: null, last_source_timestamp_ms: null, last_pong_at_ms: null, connected_at_ms: Date.now() };
  append({ kind: 'connection', venue, connection_id: connection, action: 'connecting', assets, ...receipt() });
  ws.onopen = () => {
    detail.status = 'open';
    ws.send(JSON.stringify(venue === 'coinbase' ? { type: 'subscribe', product_ids: ['BTC-USD'],
      channels: ['level2_batch', 'matches', 'heartbeat'] } : { type: 'market', assets_ids: assets, custom_feature_enabled: true }));
  };
  ws.onmessage = event => {
    if (sockets.get(venue) !== ws || stopping) return;
    const arrived = receipt(), raw = String(event.data);
    detail.messages++; detail.last_received_at_ms = arrived.received_at_ms;
    if (Buffer.byteLength(raw) > 16 * 1024 ** 2) {
      append({ kind: 'capture_gap', venue, connection_id: connection, reason: 'oversize_frame', ...arrived });
      ws.close(); return;
    }
    let payload = null;
    try { payload = JSON.parse(raw); } catch { /* PONG and unknown text are still saved. */ }
    const messages = Array.isArray(payload) ? payload : [payload];
    const timestamps = messages.map(message => sourceTime(message, venue)).filter(value => value !== null);
    const source = timestamps.length ? Math.max(...timestamps) : null;
    append({ kind: 'raw_frame', venue, connection_id: connection, source_timestamp_ms: source, raw_text: raw, ...arrived });
    if (source !== null) detail.last_source_timestamp_ms = source;
    if (raw === 'PONG') detail.last_pong_at_ms = arrived.received_at_ms;
    const sourceNow = clockReference ? Math.round(arrived.received_at_ms + clockReference.offset_ms) : arrived.received_at_ms;
    for (const message of messages) {
      if (venue === 'coinbase') coinbase.ingest(message, arrived.received_at_ms, sourceNow);
      else polymarket.ingest(message, arrived.received_at_ms, sourceNow);
      if (message?.type === 'error' || message?.event_type === 'error') detail.status = 'feed_error';
    }
    if (venue === 'polymarket') recoverPolymarket(ws, arrived, sourceNow);
    if (venue === 'coinbase' && coinbase.needsSnapshot() && ws.readyState === 1) {
      detail.close_reason = 'snapshot_required';
      const reasons = coinbase.snapshotReasons();
      for (const item of reasons) status.gap_counts[item.reason] = (status.gap_counts[item.reason] || 0) + 1;
      append({ kind: 'capture_gap', venue, connection_id: connection, reason: 'snapshot_required', reasons, ...arrived });
      ws.close();
    }
  };
  ws.onerror = () => { detail.status = 'transport_error'; ws.close(); };
  ws.onclose = () => {
    if (sockets.get(venue) !== ws) return;
    detail.status = 'closed';
    if (venue === 'coinbase') coinbase.reset(null);
    else { polymarket.reset(null, []); clearTimeout(recoveryTimer); recoveryTimer = null; recoverySince = null; recoverySinceMono = null; }
    append({ kind: 'connection', venue, connection_id: connection, action: 'closed',
      reason: detail.close_reason ?? 'transport_closed', ...receipt() });
    if (!stopping) retryTimers.set(venue, setTimeout(() => connect(venue), 2500));
  };
}

function selectMarkets() {
  if (Date.now() - lastSelectionCheck < 1000) return;
  lastSelectionCheck = Date.now();
  try {
    const bytes = readFileSync(selectionPath);
    const selection = JSON.parse(bytes);
    if (selection.schema_version !== 1 || !Number.isSafeInteger(selection.captured_at_ms) ||
        selection.captured_at_ms > Date.now() + 5000 || Date.now() - selection.captured_at_ms > 120000 ||
        !Array.isArray(selection.markets) || selection.markets.length > 4) throw new Error('invalid_selection');
    const localNow = Date.now();
    const eventNow = clockReference ? Math.round(localNow + clockReference.offset_ms) : localNow;
    const selected = selection.markets.filter(market => market.end_at_ms > eventNow);
    for (const market of selected) {
      if (typeof market.market_id !== 'string' || !/^\d+$/.test(market.market_id) ||
          !Number.isSafeInteger(market.start_at_ms) || market.end_at_ms - market.start_at_ms !== 300000 ||
          typeof market.up_asset !== 'string' || typeof market.down_asset !== 'string' ||
          !/^\d+$/.test(market.up_asset) || !/^\d+$/.test(market.down_asset) || market.up_asset === market.down_asset) {
        throw new Error('invalid_selection');
      }
    }
    const digest = createHash('sha256').update(JSON.stringify(selected)).digest('hex');
    status.selection_status = 'valid';
    if (digest === selectionDigest) return;
    selectionDigest = digest; markets = selected;
    append({ kind: 'market_selection', selection_digest: digest, markets, ...receipt() });
  } catch { status.selection_status = 'missing_or_invalid_or_stale'; markets = []; selectionDigest = null; }
  const old = sockets.get('polymarket');
  if (old && old.readyState <= 1) { status.venues.polymarket.close_reason = 'market_selection_change'; old.close(); }
  else { clearTimeout(retryTimers.get('polymarket')); connect('polymarket'); }
}

clockReference = checkedClock(Date.now()); selectMarkets(); connect('coinbase');
const timer = setInterval(() => {
  const pollAt = Date.now(); clockReference = checkedClock(pollAt);
  reference.poll(append, clockReference ? Math.round(pollAt + clockReference.offset_ms) : null);
  selectMarkets();
  const stamp = receipt(), now = stamp.received_at_ms, mono = BigInt(stamp.monotonic_ns);
  clockReference = checkedClock(now);
  const utc = clockReference ? Math.round(now + clockReference.offset_ms) : null;
  status.clock_verification = clockReference ? 'public_venues_checked' : 'unverified';
  const clockDifference = (now - previousWall) - Number(mono - previousMono) / 1e6;
  previousWall = now; previousMono = mono;
  if (Math.abs(clockDifference) > 1000) {
    try { blockedClockId = JSON.parse(readFileSync(join(directory, 'clock.json'), 'utf8')).clock_id; } catch { /* No usable clock. */ }
    clockReference = null; history.clear(); status.clock_verification = 'clock_step_waiting_for_new_check';
    clockSteps++; append({ kind: 'capture_gap', reason: 'wall_clock_step', delta_ms: clockDifference, ...stamp });
    for (const ws of sockets.values()) if (ws.readyState <= 1) ws.close();
  } else {
    const eventNow = utc ?? now;
    const pmSocket = sockets.get('polymarket');
    if (pmSocket) recoverPolymarket(pmSocket, stamp, eventNow);
    const cb = coinbase.summary(now, eventNow);
    for (const market of markets) {
      if (!(market.start_at_ms <= eventNow && eventNow < market.end_at_ms)) continue;
      const up = polymarket.summary(market.up_asset, now, eventNow), down = polymarket.summary(market.down_asset, now, eventNow);
      const paired = Boolean(cb.book && up && down);
      const record = { kind: 'feature_snapshot', feature_version: 'btc5m-public-microstructure-v2-clock-checked',
        market, selection_digest: selectionDigest, ...stamp, raw_through_seq: journal.sequence,
        as_of_utc_ms: utc, clock_reference: clockReference,
        seconds_remaining: utc !== null ? (market.end_at_ms - utc) / 1000 : null, coinbase: cb,
        polymarket: { connection_id: polymarket.connection, up, down,
          up_quality: polymarket.quality(market.up_asset, now, eventNow), down_quality: polymarket.quality(market.down_asset, now, eventNow),
          sequence_verification: 'no_dense_sequence_in_public_protocol' },
        paired, trade_window_clock: 'receipt', independent_reference_verified: false, source_hashes: sourceHashes };
      for (const quality of [cb.book_quality, record.polymarket.up_quality, record.polymarket.down_quality]) {
        if (!quality.valid) status.missing_book_counts[quality.reason ?? 'unknown'] =
          (status.missing_book_counts[quality.reason ?? 'unknown'] || 0) + 1;
      }
      const recordSeq = append(record);
      if (recordSeq !== null) {
        try {
          if (paired && utc !== null && utc - market.start_at_ms >= 120000 && utc - market.start_at_ms <= 123000) journal.flush();
          if (freezeDecision(directory, { ...record, session_id: session, record_seq: recordSeq })) status.frozen_events++;
        } catch { status.freeze_error = 'immutable_decision_write_failed'; }
        const temporal = history.update(record, recordSeq);
        const proxy = utc === null ? {status:'proxy_unavailable',read_issue:'clock_unverified',points:[],independent_reference_verified:false,
          opening_reference_kind:'near-start-relay-proxy'} : reference.context(market.start_at_ms, now, utc, stamp.monotonic_ns);
        const causal = {...record,raw_through_seq:journal.sequence,feature_version:CAUSAL_VERSION,base_snapshot_record_seq:recordSeq,
          causal_context:{schema_version:1,...temporal,reference:proxy},
          causal_complete:Boolean(utc!==null && paired && proxy.status==='proxy_available' && cb.flow['10s'].complete && cb.flow['30s'].complete)};
        const causalSeq=append(causal); status.causal_feature_records++;
        if(causal.causal_complete)status.causal_complete_records++;
        if(causalSeq!==null){
          try {
            if(causal.causal_complete && utc-market.start_at_ms>=120000 && utc-market.start_at_ms<=123000)journal.flush();
            if(freezeDecision(directory,{...causal,session_id:session,record_seq:causalSeq}))status.causal_frozen_events++;
          } catch {status.causal_freeze_error='immutable_decision_write_failed';}
        }
      }
      status.feature_records++; if (paired) status.paired_feature_records++;
    }
  }
  status.status = 'running';
  for (const [venue, ws] of sockets) {
    const detail = status.venues[venue];
    const age = now - (detail.last_received_at_ms ?? detail.connected_at_ms);
    if (age > 15000 && ws.readyState <= 1) { detail.close_reason = 'no_frames_15s'; ws.close(); }
    if (venue === 'polymarket' && ws.readyState === 1 && now % 10000 < 1500) ws.send('PING');
  }
  saveStatus();
}, 1000);
timer.unref(); setTimeout(() => stop(), duration * 1000);
saveStatus();
