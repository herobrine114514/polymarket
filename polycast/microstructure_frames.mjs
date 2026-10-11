// Public feed reducers. Receipt time and source time serve different purposes.
import { openSync, closeSync, writeSync, mkdirSync, readdirSync, lstatSync, writeFileSync, linkSync, unlinkSync, fsyncSync } from 'node:fs';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export function freezeDecision(directory, record) {
  const v3 = record.feature_version === 'btc5m-causal-features-v3';
  const usesUTC = v3 || record.feature_version === 'btc5m-public-microstructure-v2-clock-checked';
  if (usesUTC && (!Number.isSafeInteger(record.as_of_utc_ms) || record.clock_reference?.status !== 'venues_agree')) return false;
  const elapsed = (usesUTC ? record.as_of_utc_ms : record.received_at_ms) - record.market.start_at_ms;
  if (elapsed < 120000 || elapsed > 123000 || !record.paired || v3 && record.causal_complete !== true) return false;
  const folder = join(directory, v3 ? 'decisions-v3' : 'decisions'); mkdirSync(folder, { recursive: true });
  if (!/^\d+$/.test(record.market.market_id)) throw new Error('Invalid decision market');
  const snapshot = JSON.stringify(record);
  const value = JSON.stringify({ schema_version: 1, decision_protocol: v3 ? 'utc-start-plus-120s-tolerance-3s-v3' : usesUTC ? 'utc-start-plus-120s-tolerance-3s-v2' :
    'start-plus-120s-tolerance-3s-v1',
    snapshot_sha256: createHash('sha256').update(snapshot).digest('hex'), snapshot_json: snapshot });
  const destination = join(folder, `event-${record.market.market_id}.json`);
  const temporary = join(folder, `.pending-${randomUUID()}`);
  let descriptor;
  try {
    descriptor = openSync(temporary, 'wx'); writeFileSync(descriptor, value); fsyncSync(descriptor); closeSync(descriptor); descriptor = null;
    try { linkSync(temporary, destination); } catch (error) { if (error.code === 'EEXIST') return false; throw error; }
    return true;
  } finally { if (descriptor != null) closeSync(descriptor); unlinkSync(temporary); }
}

export function sourceTime(message, venue) {
  const value = venue === 'coinbase' ? message?.time : message?.timestamp;
  if (typeof value === 'string' && /^\d+$/.test(value)) {
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }
  if (typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(value)) {
    const number = Date.parse(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }
  return null;
}

export class StorageLimit extends Error {}

export class RawJournal {
  constructor(directory, session, { segmentBytes = 32 * 1024 ** 2, totalBytes = 2 * 1024 ** 3 } = {}) {
    if (!/^[a-zA-Z0-9-]+$/.test(session) || !Number.isSafeInteger(segmentBytes) || segmentBytes < 1024 ||
        !Number.isSafeInteger(totalBytes) || totalBytes < segmentBytes) throw new Error('Invalid journal configuration');
    mkdirSync(directory, { recursive: true });
    this.directory = directory; this.session = session; this.segmentBytes = segmentBytes;
    this.limit = totalBytes; this.bytes = 0; this.segment = 0; this.segmentSize = 0; this.sequence = 0; this.fd = null;
    this.activeName = null; this.segmentHash = null; this.writtenBytes = 0; this.lastSizeRefresh = 0;
    this.refreshStorage();
  }
  refreshStorage() {
    let bytes = 0;
    for (const name of readdirSync(this.directory)) {
      if (!/^raw-[a-zA-Z0-9-]+-\d{5}\.jsonl(?:\.gz)?$/.test(name)) continue;
      try {
        const stat = lstatSync(join(this.directory, name));
        if (stat.isFile() && !stat.isSymbolicLink()) bytes += stat.size;
      } catch (error) { if (error.code !== 'ENOENT') throw error; } // An archiver may publish between scans.
    }
    this.bytes = bytes; this.lastSizeRefresh = Date.now(); return bytes;
  }
  sealSegment() {
    if (this.fd === null) return;
    fsyncSync(this.fd); closeSync(this.fd); this.fd = null;
    const metadata = { schema_version: 1, filename: this.activeName, session_id: this.session,
      bytes: this.segmentSize, sha256: this.segmentHash.digest('hex'), first_record_seq: this.firstSequence,
      last_record_seq: this.sequence, closed_at_ms: Date.now() };
    writeFileSync(join(this.directory, this.activeName + '.seal.json'), JSON.stringify(metadata), { flag: 'wx' });
    this.activeName = null; this.segmentHash = null;
  }
  append(record) {
    const line = Buffer.from(JSON.stringify({ ...record, schema_version: 1, session_id: this.session,
      record_seq: this.sequence + 1 }) + '\n');
    if (Date.now() - this.lastSizeRefresh > 5000 || this.bytes + line.length > this.limit) this.refreshStorage();
    if (this.bytes + line.length > this.limit) throw new StorageLimit('Public research storage limit reached');
    if (this.fd === null || this.segmentSize + line.length > this.segmentBytes) {
      this.sealSegment();
      this.activeName = `raw-${this.session}-${String(this.segment++).padStart(5, '0')}.jsonl`;
      this.fd = openSync(join(this.directory, this.activeName), 'wx');
      this.segmentHash = createHash('sha256'); this.firstSequence = this.sequence + 1;
      this.segmentSize = 0;
    }
    let offset = 0;
    while (offset < line.length) offset += writeSync(this.fd, line, offset, line.length - offset);
    this.bytes += line.length; this.segmentSize += line.length; this.writtenBytes += line.length;
    this.segmentHash.update(line); this.sequence++;
    return this.sequence;
  }
  flush() { if (this.fd !== null) fsyncSync(this.fd); }
  close() { this.sealSegment(); }
}

function levels(rows, object = false) {
  if (!Array.isArray(rows)) throw new Error('Missing depth levels');
  const result = new Map();
  for (const row of rows) {
    const price = Number(object ? row?.price : row?.[0]), size = Number(object ? row?.size : row?.[1]);
    if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(size) || size < 0) throw new Error('Invalid depth level');
    if (size > 0) result.set(price, size);
  }
  return result;
}

class DepthBook {
  constructor() { this.invalidate('awaiting_snapshot'); }
  invalidate(reason) { this.valid = false; this.reason = reason; this.bids = new Map(); this.asks = new Map(); }
  snapshot(message, venue, receipt) {
    this.bids = levels(message.bids, venue === 'polymarket');
    this.asks = levels(message.asks, venue === 'polymarket');
    this.valid = true; this.reason = null; this.received = receipt;
    this.source = sourceTime(message, venue); // Coinbase snapshot has no engine timestamp.
  }
  change(side, price, size) {
    const p = Number(price), s = Number(size);
    if (!Number.isFinite(p) || p <= 0 || !Number.isFinite(s) || s < 0 || !['buy', 'sell'].includes(side)) {
      this.invalidate('invalid_change'); return;
    }
    const target = side === 'buy' ? this.bids : this.asks;
    if (s === 0) target.delete(p); else target.set(p, s);
  }
  summary(now, maxAge = 5000, sourceNow = now) {
    if (!this.valid || now < this.received || now - this.received > maxAge || this.source > sourceNow) return null;
    const bids = [...this.bids].sort((a, b) => b[0] - a[0]).slice(0, 5);
    const asks = [...this.asks].sort((a, b) => a[0] - b[0]).slice(0, 5);
    if (!bids.length || !asks.length || bids[0][0] >= asks[0][0]) return null;
    const bidSize = bids.reduce((sum, row) => sum + row[1], 0), askSize = asks.reduce((sum, row) => sum + row[1], 0);
    return { bid: bids[0][0], ask: asks[0][0], midpoint: (bids[0][0] + asks[0][0]) / 2,
      spread: asks[0][0] - bids[0][0], bids, asks, top5_imbalance: (bidSize - askSize) / (bidSize + askSize),
      received_at_ms: this.received, source_timestamp_ms: this.source };
  }
  quality(now, sourceNow = now) {
    let reason = this.reason;
    if (this.valid) {
      reason = now < this.received ? 'future_receipt' : now - this.received > 5000 ? 'stale_receipt' :
        this.source > sourceNow ? 'future_source' : !this.bids.size || !this.asks.size ? 'one_sided_book' : null;
      if (reason === null) {
        let bid = -Infinity, ask = Infinity;
        for (const price of this.bids.keys()) bid = Math.max(bid, price);
        for (const price of this.asks.keys()) ask = Math.min(ask, price);
        if (bid >= ask) reason = 'crossed_book';
      }
    }
    return { valid: this.valid && reason === null, reason, received_at_ms: this.received ?? null,
      source_timestamp_ms: this.source ?? null };
  }
}

export class CoinbaseState {
  constructor() { this.book = new DepthBook(); this.trades = []; this.lastTrade = null; this.gapAt = null; this.connection = null; }
  reset(connection) {
    this.book.invalidate('reconnect'); this.trades = []; this.lastTrade = null;
    this.gapAt = null; this.connectedAt = null; this.lastHeartbeat = null; this.connection = connection;
  }
  ingest(message, receipt, sourceNow = receipt) {
    if (message?.product_id !== 'BTC-USD') return;
    const source = sourceTime(message, 'coinbase');
    try {
      if (source !== null && source > sourceNow + 5000) throw new Error('future_source_clock');
      if (message.type === 'snapshot') {
        this.book.snapshot(message, 'coinbase', receipt); this.connectedAt ??= receipt;
      } else if (message.type === 'l2update' && this.book.valid) {
        if (source === null || source < this.book.source || !Array.isArray(message.changes)) throw new Error('unordered_depth');
        for (const [side, price, size] of message.changes) this.book.change(side, price, size);
        this.book.received = receipt; this.book.source = source;
      } else if (message.type === 'last_match') {
        if (Number.isSafeInteger(message.trade_id)) this.lastTrade = message.trade_id;
        this.connectedAt ??= receipt; // Historical seed is never counted as a newly arrived trade.
      } else if (message.type === 'match') {
        if (!Number.isSafeInteger(message.trade_id) || source === null) throw new Error('invalid_trade');
        if (this.lastTrade !== null && message.trade_id <= this.lastTrade) return;
        if (this.lastTrade === null || message.trade_id !== this.lastTrade + 1) this.gapAt = receipt;
        this.lastTrade = message.trade_id;
        const price = Number(message.price), size = Number(message.size);
        if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(size) || size <= 0 || !['buy', 'sell'].includes(message.side)) {
          throw new Error('invalid_trade');
        }
        this.trades.push({ received: receipt, source, price, size, taker_sign: message.side === 'sell' ? 1 : -1 });
      } else if (message.type === 'heartbeat' && Number.isSafeInteger(message.last_trade_id)) {
        this.lastHeartbeat = receipt;
        if (this.lastTrade === null || message.last_trade_id > this.lastTrade) this.gapAt = receipt;
      }
    } catch (error) { this.book.invalidate(error.message); this.gapAt = receipt; }
    this.trades = this.trades.filter(trade => receipt - trade.received <= 65000);
  }
  summary(now, sourceNow = now) {
    const book = this.book.summary(now, 5000, sourceNow);
    const flow = {};
    for (const seconds of [10, 30, 60]) {
      const complete = this.connectedAt !== null && now - this.connectedAt >= seconds * 1000 &&
        this.lastHeartbeat !== null && now >= this.lastHeartbeat && now - this.lastHeartbeat <= 5000 &&
        (this.gapAt === null || now - this.gapAt > seconds * 1000);
      const trades = this.trades.filter(t => t.received <= now && t.source <= sourceNow && now - t.received < seconds * 1000);
      const gross = trades.reduce((sum, t) => sum + t.size, 0);
      flow[`${seconds}s`] = { complete, trades: trades.length, volume_btc: gross,
        taker_imbalance: complete ? (gross ? trades.reduce((sum, t) => sum + t.size * t.taker_sign, 0) / gross : 0) : null };
    }
    return { connection_id: this.connection, book, flow, book_quality: this.book.quality(now, sourceNow),
      last_trade_id: this.lastTrade, last_gap_received_at_ms: this.gapAt };
  }
  needsSnapshot() { return Boolean(this.book.reason && !['awaiting_snapshot', 'reconnect'].includes(this.book.reason)); }
  snapshotReasons() { return this.needsSnapshot() ? [{ product_id: 'BTC-USD', reason: this.book.reason }] : []; }
}

export class PolymarketState {
  constructor() { this.books = new Map(); this.connection = null; }
  reset(connection, assets) {
    this.connection = connection; this.books = new Map(assets.map(id => [id, new DepthBook()]));
  }
  ingest(message, receipt, sourceNow = receipt) {
    if (!['book', 'price_change'].includes(message?.event_type)) return;
    const source = sourceTime(message, 'polymarket');
    const targets = message?.event_type === 'price_change' ? message.price_changes : [message];
    if (!Array.isArray(targets)) return;
    const touched = new Map();
    for (const change of targets) {
      const book = this.books.get(change?.asset_id);
      if (!book) continue;
      try {
        if (source === null || source > sourceNow + 5000 || (book.source !== null && source < book.source)) throw new Error('unordered_depth');
        if (message.event_type === 'book') book.snapshot(message, 'polymarket', receipt);
        else if (message.event_type === 'price_change' && book.valid) {
          book.change(String(change.side).toLowerCase(), change.price, change.size);
          book.source = source; book.received = receipt; touched.set(book, change);
        }
      } catch (error) { book.invalidate(error.message); }
    }
    for (const [book, change] of touched) {
      const top = book.summary(receipt, 5000, sourceNow);
      if (top && ((change.best_bid != null && Math.abs(top.bid - Number(change.best_bid)) > 1e-7) ||
                  (change.best_ask != null && Math.abs(top.ask - Number(change.best_ask)) > 1e-7))) book.invalidate('reported_top_mismatch');
    }
  }
  summary(asset, now, sourceNow = now) { return this.books.get(asset)?.summary(now, 5000, sourceNow) ?? null; }
  quality(asset, now, sourceNow = now) { return this.books.get(asset)?.quality(now, sourceNow) ?? { valid: false, reason: 'awaiting_connection' }; }
  snapshotReasons(assets = null) {
    const selected = assets === null ? null : new Set(assets);
    return [...this.books].filter(([asset, book]) => (selected === null || selected.has(asset)) && book.reason &&
    !['awaiting_snapshot', 'reconnect'].includes(book.reason)).map(([asset_id, book]) => ({ asset_id, reason: book.reason })); }
  needsSnapshot(assets = null) { return this.snapshotReasons(assets).length > 0; }
}

// Invalid future books stay quarantined. They require recovery when they become active.
export function activeMarketAssets(markets, utc) {
  return [...new Set(markets.filter(market => market.start_at_ms <= utc && utc < market.end_at_ms)
    .flatMap(market => [market.up_asset, market.down_asset]))];
}
