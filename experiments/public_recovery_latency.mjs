// Offline analysis of hash-checked finite-probe records; no sockets or order imports.
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { PolymarketState } from '../polycast/microstructure_frames.mjs';
const [directory, output] = process.argv.slice(2);
const probe = JSON.parse(readFileSync(join(directory, 'probe.json'), 'utf8'));
const lanes = new Map();
let arrived = null, sequence = 0;
for (const name of Object.keys(probe.ws_levels)) {
  const state = new PolymarketState(); state.reset(name, [probe.market.up_asset, probe.market.down_asset]);
  const lane = { state, waiting: new Map(), recovered: [], failures: 0 };
  for (const [asset, book] of state.books) {
    const invalidate = book.invalidate.bind(book);
    book.invalidate = reason => {
      if (!['awaiting_snapshot', 'reconnect'].includes(reason)) {
        lane.failures++;
        if (!lane.waiting.has(asset)) lane.waiting.set(asset, { asset_id: asset, since_ms: arrived, reason });
      }
      invalidate(reason);
    };
  }
  lanes.set(name, lane);
}
const seals = readdirSync(directory).filter(name => name.endsWith('.jsonl.seal.json')).sort();
for (const name of seals) {
  const seal = JSON.parse(readFileSync(join(directory, name), 'utf8'));
  const raw = readFileSync(join(directory, seal.filename));
  if (seal.session_id !== probe.session_id || raw.length !== seal.bytes ||
      createHash('sha256').update(raw).digest('hex') !== seal.sha256 || seal.first_record_seq !== sequence + 1)
    throw new Error('Probe seal/hash/prefix mismatch');
  for (const line of raw.toString('utf8').trimEnd().split('\n')) {
    const row = JSON.parse(line);
    if (row.record_seq !== ++sequence || row.session_id !== probe.session_id) throw new Error('Probe sequence mismatch');
    if (row.kind !== 'raw_ws') continue;
    arrived = row.received_at_ms;
    const lane = lanes.get(row.lane); let data;
    try { data = JSON.parse(row.raw_text); } catch { continue; }
    for (const message of Array.isArray(data) ? data : [data]) {
      lane.state.ingest(message, arrived, Math.round(arrived + probe.clock_reference.offset_ms));
      if (message?.event_type !== 'book') continue;
      const waiting = lane.waiting.get(message.asset_id), book = lane.state.books.get(message.asset_id);
      if (waiting && book?.valid) {
        lane.recovered.push({ ...waiting, full_snapshot_at_ms: arrived, latency_ms: arrived - waiting.since_ms });
        lane.waiting.delete(message.asset_id);
      }
    }
  }
  if (sequence !== seal.last_record_seq) throw new Error('Probe seal endpoints mismatch');
}
const report = { status: 'verified_finite_probe_recovery_latency', session_id: probe.session_id,
  market_id: probe.market.market_id, market_mode: probe.market_mode, last_verified_record_seq: sequence,
  lanes: Object.fromEntries([...lanes].map(([name, lane]) => [name, {
    invalidation_calls: lane.failures, restored_by_full_snapshot: lane.recovered.length,
    restored_within_1000ms: lane.recovered.filter(row => row.latency_ms <= 1000).length,
    max_recovery_ms: lane.recovered.length ? Math.max(...lane.recovered.map(row=>row.latency_ms)) : null,
    unresolved: [...lane.waiting.values()], recovered: lane.recovered
  }])), limitations: ['Finite public-market observation, not predictive or execution performance',
    'Full snapshot restores reducer validity only; one-sided/stale/future books still cannot produce paired inputs',
    'The probe deliberately did not reconnect; production results with a grace period require future observation'] };
writeFileSync(output, JSON.stringify(report, null, 2), { flag: 'wx' });
process.stdout.write(JSON.stringify({ market_id: report.market_id, lanes: Object.fromEntries(
  Object.entries(report.lanes).map(([key,value])=>[key,{...value,recovered:undefined}])) })+'\n');
