// Read-only diagnosis of verified journal extracts; no sockets or trading.
import { createReadStream, readFileSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { PolymarketState, sourceTime } from '../polycast/microstructure_frames.mjs';

const [input, output] = process.argv.slice(2);
if (!input || !output) throw new Error('Verified journal extract and new output required');
const state = new PolymarketState(), counts = {}, examples = [];
let current = null, currentMessage = null, clock = null, markets = [];
let first = null, last = null, records = 0, frames = 0;

function decorateBooks() {
  for (const [asset, book] of state.books) {
    const invalidate = book.invalidate.bind(book);
    book.invalidate = reason => {
      if (current && !['awaiting_snapshot', 'reconnect'].includes(reason)) {
        const eventNow = Math.round(current.received_at_ms + (clock?.offset_ms ?? 0));
        const market = markets.find(item => [item.up_asset, item.down_asset].includes(asset));
        const stage = !market ? 'unknown' : eventNow < market.start_at_ms ? 'future' :
          eventNow >= market.end_at_ms ? 'ended' : 'active';
        const key = `${reason}:${stage}`; counts[key] = (counts[key] || 0) + 1;
        if (examples.length < 12) {
          const change = currentMessage?.price_changes?.filter(item => item.asset_id === asset).at(-1);
          let bid = null, ask = null;
          for (const price of book.bids.keys()) bid = bid === null ? price : Math.max(bid, price);
          for (const price of book.asks.keys()) ask = ask === null ? price : Math.min(ask, price);
          examples.push({ reason, stage, market_id: market?.market_id ?? null, record_seq: current.record_seq,
            received_at_ms: current.received_at_ms, message_type: currentMessage?.event_type,
            message_source_ms: sourceTime(currentMessage, 'polymarket'), previous_or_current_book_source_ms: book.source,
            reconstructed_bid: bid, reconstructed_ask: ask,
            reported_bid: change?.best_bid ?? null, reported_ask: change?.best_ask ?? null,
            bid_levels: book.bids.size, ask_levels: book.asks.size, public_sequence_verified: false });
        }
      }
      invalidate(reason);
    };
  }
}

// Initial clock is copied from the first feature record, at most one tick later.
// This approximation is declared and is not used to score a forecasting model.
for (const line of readFileSync(input, 'utf8').split('\n')) {
  if (!line) continue;
  const row = JSON.parse(line);
  if (row.kind === 'feature_snapshot' && row.clock_reference) { clock = row.clock_reference; break; }
}
for await (const line of createInterface({ input: createReadStream(input), crlfDelay: Infinity })) {
  if (!line) continue;
  current = JSON.parse(line); records++;
  first ??= current.record_seq; last = current.record_seq;
  if (current.kind === 'market_selection') markets = current.markets;
  else if (current.kind === 'feature_snapshot') clock = current.clock_reference;
  else if (current.kind === 'connection' && current.venue === 'polymarket') {
    state.reset(current.action === 'connecting' ? current.connection_id : null,
      current.action === 'connecting' ? current.assets : []);
    decorateBooks();
  } else if (current.kind === 'raw_frame' && current.venue === 'polymarket') {
    let payload;
    try { payload = JSON.parse(current.raw_text); } catch { continue; }
    frames++;
    for (const message of Array.isArray(payload) ? payload : [payload]) {
      currentMessage = message;
      state.ingest(message, current.received_at_ms, Math.round(current.received_at_ms + (clock?.offset_ms ?? 0)));
    }
  }
}
const result = { status: 'readonly_book_diagnostic', records, frames, first_record_seq: first, last_record_seq: last,
  invalidation_counts_by_stage: counts, examples, model_or_profitability_validated: false,
  limitations: ['Uses the first feature clock as an initial clock approximation',
    'A reported quote discrepancy does not establish whether feed delivery, protocol interpretation or local reducer caused it',
    'Incomplete or non-dense public protocol cannot be assumed to contain every venue update'] };
writeFileSync(output, JSON.stringify(result, null, 2) + '\n', { flag: 'wx' });
process.stdout.write(JSON.stringify({ ...result, examples: result.examples.slice(0, 3) }) + '\n');
