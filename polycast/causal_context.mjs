// Bounded, causal input capture. Public reference relay remains an unsigned proxy.
import { openSync, closeSync, readSync, fstatSync } from 'node:fs';
import { createHash } from 'node:crypto';

export const CAUSAL_VERSION = 'btc5m-causal-features-v3';
const MAX_READ = 2 * 1024 ** 2, LOOKBACK = 360000;
function exactPriceKey(point) {
  if(point.price_encoding==='e18'){
    const n=BigInt(point.full_accuracy_value),scale=10n**18n;
    return `${n/scale}.${(n%scale).toString().padStart(18,'0').replace(/0+$/,'')}`;
  }
  const [whole,fraction='']=point.full_accuracy_value.split('.');
  return `${BigInt(whole)}.${fraction.replace(/0+$/,'')}`;
}

export function validReference(item) {
  if (!item || item.schema_version !== 1 || !Number.isSafeInteger(item.timestamp_ms) || item.timestamp_ms <= 0 ||
      !Number.isSafeInteger(item.received_at_ms) || item.received_at_ms <= 0 ||
      item.received_at_ms < item.timestamp_ms - 5000 ||
      item.symbol !== 'btcusd' || item.quote_currency !== 'USD' || item.price_source !== 'chainlink' || item.window_seconds !== 60 ||
      item.sequence_contiguous === false || typeof item.full_accuracy_value !== 'string') return false;
  if (item.feed_protocol === 'rtds') return item.price_encoding === 'e18' && /^\d{15,30}$/.test(item.full_accuracy_value) && /[1-9]/.test(item.full_accuracy_value);
  return ['polybolt_raw_v1','polybolt_sdk_v1'].includes(item.feed_protocol) && item.price_encoding === 'decimal_usd' &&
    /^\d{1,10}(?:\.\d{1,30})?$/.test(item.full_accuracy_value) && /[1-9]/.test(item.full_accuracy_value) &&
    typeof item.connection_id === 'string' && item.connection_id.length > 0 &&
    Number.isSafeInteger(item.channel_seq) && item.channel_seq >= 0 && item.sequence_contiguous === true;
}

export class ReferenceCursor {
  constructor(path) { this.path = path; this.position = null; this.identity = null; this.buffer = ''; this.points = new Map(); this.issue = 'not_read'; this.capacityFailed=false; }
  poll(append, utc) {
    if(this.capacityFailed){this.issue='reference_version_capacity';return;}
    if(utc===null){this.issue='clock_unverified';return;}
    let fd;
    try {
      fd = openSync(this.path, 'r'); const stat = fstatSync(fd);
      const identity = `${stat.dev}:${stat.ino}`;
      for(const [key,row] of this.points)if(row.point.timestamp_ms < utc-LOOKBACK)this.points.delete(key);
      let discard = false;
      if (this.identity !== identity || this.position === null || stat.size < this.position) {
        if (this.identity !== null) append({kind:'reference_read_gap',reason:'file_identity_or_size_changed'});
        this.identity = identity; this.position = Math.max(0, stat.size - MAX_READ); this.buffer = ''; this.points.clear();
        discard = this.position > 0;
      }
      const amount = Math.min(MAX_READ, stat.size - this.position), bytes = Buffer.alloc(amount);
      const length = amount ? readSync(fd, bytes, 0, amount, this.position) : 0;
      this.position += length;
      let text = this.buffer + bytes.subarray(0,length).toString('utf8');
      if (discard) { const first = text.indexOf('\n'); text = first < 0 ? '' : text.slice(first+1); }
      const lines = text.split('\n'); this.buffer = lines.pop();
      if (this.buffer.length > 16384) throw new Error('oversize_reference_line');
      for (const raw of lines) {
        if (raw.length > 16384) continue;
        let item; try { item = JSON.parse(raw); } catch { continue; }
        if (!validReference(item) || item.timestamp_ms > utc+5000 || item.timestamp_ms < utc - LOOKBACK) continue;
        const key = `${item.timestamp_ms}:${item.full_accuracy_value}:${item.price_encoding}`;
        if (this.points.has(key)) continue;
        if(this.points.size>=2048){
          this.capacityFailed=true;this.issue='reference_version_capacity';append({kind:'reference_read_gap',reason:this.issue});return;
        }
        const received = Date.now(), mono = process.hrtime.bigint().toString();
        const row = {kind:'reference_observation',raw_text:raw,raw_sha256:createHash('sha256').update(raw).digest('hex'),
          received_at_ms:received,monotonic_ns:mono,source_timestamp_ms:item.timestamp_ms};
        const seq = append(row);
        if (seq !== null) this.points.set(key,{raw_text:raw,raw_sha256:row.raw_sha256,journal_seq:seq,
          first_read_at_ms:received,first_read_monotonic_ns:mono,point:item});
      }
      if (utc !== null) for (const [key,row] of this.points) if(row.point.timestamp_ms < utc-LOOKBACK)this.points.delete(key);
      this.issue = this.position < stat.size ? 'catching_up' : null;
    } catch { this.issue = 'reference_read_error'; }
    finally { if(fd!==undefined)closeSync(fd); }
  }
  context(start, now, utc, mono) {
    const rows = [...this.points.values()].filter(row=>row.point.timestamp_ms >= start-3000 && row.point.timestamp_ms <= utc &&
      row.point.received_at_ms <= now && row.first_read_at_ms <= now && BigInt(row.first_read_monotonic_ns) <= BigInt(mono));
    const versions = new Map();
    for (const row of rows) {
      const p = row.point, value = exactPriceKey(p);
      const set = versions.get(p.timestamp_ms) ?? new Set(); set.add(value); versions.set(p.timestamp_ms,set);
    }
    const conflicts = [...versions].filter(([,values])=>values.size>1).map(([stamp])=>stamp);
    const near = rows.filter(row=>Math.abs(row.point.timestamp_ms-start)<=3000);
    const latest = rows.reduce((best,row)=>!best||row.point.timestamp_ms>best.point.timestamp_ms?row:best,null);
    const complete = !this.issue && near.length>0 && latest && utc-latest.point.timestamp_ms<=15000 &&
      now-latest.point.received_at_ms<=30000 && !conflicts.some(stamp=>Math.abs(stamp-start)<=3000||utc-stamp<=15000);
    return {status:complete?'proxy_available':'proxy_unavailable',read_issue:this.issue,points:rows,
      independent_reference_verified:false,opening_reference_kind:'near-start-relay-proxy'};
  }
}

export class CausalHistory {
  constructor(){this.coinbase=[];this.polymarket=new Map();}
  clear(){this.coinbase=[];this.polymarket.clear();}
  update(record,seq) {
    const common={input_snapshot_record_seq:seq,received_at_ms:record.received_at_ms,monotonic_ns:record.monotonic_ns,
      as_of_utc_ms:record.as_of_utc_ms,clock_reference:record.clock_reference};
    const point=(book,connection)=>({...common,connection_id:connection,valid:Boolean(book),midpoint:book?.midpoint??null,
      book_received_at_ms:book?.received_at_ms??null,source_timestamp_ms:book?.source_timestamp_ms??null});
    if(this.coinbase.at(-1)?.received_at_ms!==record.received_at_ms)this.coinbase.push(point(record.coinbase.book,record.coinbase.connection_id));
    this.coinbase=this.coinbase.filter(row=>record.received_at_ms-row.received_at_ms<=65000).slice(-130);
    const id=record.market.market_id, rows=this.polymarket.get(id)??[];
    rows.push(point(record.polymarket.up,record.polymarket.connection_id));
    this.polymarket.set(id,rows.filter(row=>record.received_at_ms-row.received_at_ms<=15000).slice(-30));
    for(const [key,value]of this.polymarket)if(record.received_at_ms-value.at(-1).received_at_ms>300000)this.polymarket.delete(key);
    return {coinbase:this.coinbase,polymarket_up:this.polymarket.get(id),history_clock:'checked-UTC-1s-observations'};
  }
}
