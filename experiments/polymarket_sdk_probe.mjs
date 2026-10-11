/** Bounded public GET research. No secure client, signing, account or order calls. */
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';

const root = new URL('../', import.meta.url);
const [marketId, yesToken, noToken] = process.argv.slice(2);
assert.match(marketId ?? '', /^\d+$/);
for (const token of [yesToken, noToken]) assert.match(token ?? '', /^\d+$/);
const checks = [], trace = [], receipts = [];
const started = new Date().toISOString();
const nativeFetch = globalThis.fetch;
const permitted = url => (
  url.origin === 'https://gamma-api.polymarket.com' && url.pathname === `/markets/${marketId}`
) || (
  url.origin === 'https://clob.polymarket.com' && (
    url.pathname === '/book' || /^\/clob-markets\/0x[0-9a-f]{64}$/i.test(url.pathname)
  )
);
// Install BEFORE importing the SDK, so ky's fetch also crosses this boundary.
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  const url = new URL(request.url);
  assert.equal(request.method, 'GET', 'Only public GET requests are permitted');
  assert.ok(permitted(url), 'Endpoint is outside the public research allowlist');
  for (const [name] of request.headers) {
    assert.ok(!/authorization|poly[-_]|api[-_]?key|cookie/i.test(name), 'Credential header forbidden');
  }
  if (url.pathname === '/book') {
    assert.ok([yesToken, noToken].includes(url.searchParams.get('token_id')), 'Unexpected token');
  }
  assert.ok(trace.length < 12, 'Public request budget exceeded');
  const entry = {method: 'GET', origin: url.origin, path: url.pathname,
    query: Object.fromEntries(url.searchParams), started_at: new Date().toISOString()};
  trace.push(entry);
  const before = performance.now();
  const response = await nativeFetch(request, {signal: AbortSignal.timeout(15000)});
  entry.http_status = response.status;
  entry.received_at = new Date().toISOString();
  entry.elapsed_ms = Number((performance.now() - before).toFixed(3));
  // Raw and normalized comparisons use this SAME response, never a second quote.
  const raw = await response.clone().json();
  receipts.push({trace_index: trace.length - 1, raw});
  return response;
};
function check(name, fn) { fn(); checks.push({name, passed: true}); }
function latestRaw(path) {
  const receipt = receipts.findLast(r => trace[r.trace_index].path === path);
  assert.ok(receipt, 'SDK request did not cross guarded fetch');
  return receipt.raw;
}
function walk(asks, shares, cap = 1) {
  let remaining = shares, gross = 0, worst = null, available = 0;
  for (const level of [...asks].sort((a,b) => Number(a.price) - Number(b.price))) {
    const price = Number(level.price), size = Number(level.size);
    assert.ok(Number.isFinite(price) && price > 0 && price < 1);
    assert.ok(Number.isFinite(size) && size > 0);
    if (price > cap + 1e-12) continue;
    available += size;
    const take = Math.min(remaining, size);
    if (take > 0) { gross += take * price; remaining -= take; worst = price; }
  }
  return {fully_fillable: remaining < 1e-9, gross_cost: Number(gross.toFixed(8)),
    filled_shares: Number((shares - remaining).toFixed(8)), worst_price: worst,
    available_at_or_below_cap: available};
}
function inspectBook(book, expectedToken, conditionId) {
  const raw = latestRaw('/book');
  check(`book_${expectedToken.slice(-8)}_${checks.length}_same_response_identity_and_levels`, () => {
    assert.equal(book.assetId, expectedToken);
    assert.equal(book.conditionId, conditionId);
    assert.equal(raw.asset_id, expectedToken);
    assert.equal(raw.market, conditionId);
    assert.deepEqual(book.asks, raw.asks);
    assert.deepEqual(book.bids, raw.bids);
    assert.equal(book.timestamp, Number(raw.timestamp));
    assert.equal(book.minOrderSize, String(raw.min_order_size));
    assert.equal(book.tickSize, Number(raw.tick_size));
    assert.equal(book.hash, raw.hash);
  });
  return {normalized: book, raw, received_at: trace.at(-1).received_at};
}
const report = {started_at: started, scope: 'public_get_only', market_id: marketId,
  credentials_used: false, signing_used: false, orders_submitted: 0,
  production_database_written: false, live_execution_gate_passed: false, checks, request_trace: trace};
try {
  const sdkDir = new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/', root);
  const pkg = JSON.parse(readFileSync(new URL('package.json', sdkDir), 'utf8'));
  assert.equal(pkg.version, '0.12.0');
  report.sdk = {name: pkg.name, version: pkg.version, license: pkg.license,
    node: process.version, package_lock_sha256: createHash('sha256').update(
      readFileSync(new URL('.polycast/oss-polymarket-sdk/package-lock.json', root))).digest('hex')};
  const {createPublicClient} = await import(new URL('dist/index.js', sdkDir));
  const {fetchMarketInfo} = await import(new URL('dist/actions/index.js', sdkDir));
  const client = createPublicClient();
  const market = await client.fetchMarket({id: marketId});
  const rawMarket = latestRaw(`/markets/${marketId}`);
  const rawTokens = JSON.parse(rawMarket.clobTokenIds);
  const rawOutcomes = JSON.parse(rawMarket.outcomes);
  check('gamma_market_mapping_tick_minimum_fee_same_response', () => {
    assert.equal(market.id, marketId);
    assert.equal(market.conditionId, rawMarket.conditionId);
    assert.deepEqual(rawTokens, [yesToken, noToken]);
    assert.equal(market.outcomes.yes.tokenId, yesToken);
    assert.equal(market.outcomes.no.tokenId, noToken);
    assert.deepEqual([market.outcomes.yes.label, market.outcomes.no.label], rawOutcomes);
    assert.equal(market.trading.minimumOrderSize, String(rawMarket.orderMinSize));
    assert.equal(market.trading.minimumTickSize, rawMarket.orderPriceMinTickSize);
    assert.equal(Number(market.trading.feeSchedule.rate), Number(rawMarket.feeSchedule.rate));
  });
  const info = await fetchMarketInfo(client, {conditionId: market.conditionId});
  const rawInfo = latestRaw(`/clob-markets/${market.conditionId}`);
  check('clob_fee_tick_and_tokens_same_response', () => {
    assert.equal(info.tickSize, rawInfo.mts);
    assert.equal(info.feeInfo.rate, rawInfo.fd.r);
    assert.equal(info.feeInfo.exponent, rawInfo.fd.e);
    assert.deepEqual(info.tokens.map(t => t.assetId), rawInfo.t.map(t => t.t));
    assert.deepEqual([...info.tokens.map(t => t.assetId)].sort(), [...rawTokens].sort());
    assert.equal(info.tickSize, market.trading.minimumTickSize);
    assert.equal(info.feeInfo.rate, Number(market.trading.feeSchedule.rate));
  });
  check('taker_delay_flag_is_explicit_boolean', () => assert.equal(typeof rawInfo.itode, 'boolean'));
  report.market = {question: market.question, condition_id: market.conditionId,
    version: market.version, state: market.state, outcomes: market.outcomes,
    trading: market.trading, clob_info: info, raw_clob_info: rawInfo,
    taker_delay_enabled: rawInfo.itode,
    documented_taker_hold_ms: rawInfo.itode ? 250 : 0,
    delay_source: 'https://docs.polymarket.com/concepts/order-lifecycle'};
  report.book_samples = [];
  for (const [label, token] of [['YES', yesToken], ['NO', noToken]]) {
    const initial = inspectBook(await client.fetchOrderBook({assetId: token}), token, market.conditionId);
    const shares = Number(initial.normalized.minOrderSize);
    const first = walk(initial.normalized.asks, shares);
    const cap = first.worst_price;
    const receiptClock = performance.now();
    await sleep(250);
    const later = inspectBook(await client.fetchOrderBook({assetId: token}), token, market.conditionId);
    const elapsed = performance.now() - receiptClock;
    check(`${label}_resample_wait_is_at_least_250ms`, () => assert.ok(elapsed >= 250));
    const second = cap === null ? null : walk(later.normalized.asks, shares, cap);
    report.book_samples.push({label, token, shares, frozen_limit_price: cap,
      initial, later, initial_quote: first, later_at_frozen_limit: second,
      observed_receipt_gap_ms: Number(elapsed.toFixed(3)),
      depth_changed: JSON.stringify(initial.normalized.asks) !== JSON.stringify(later.normalized.asks),
      interpretation: 'Public snapshot comparison only; includes HTTP latency, no server execution or fill proof'});
  }
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = {name: error.name, message: String(error.message).slice(0,200)};
  process.exitCode = 1;
} finally {
  report.completed_at = new Date().toISOString();
  const out = new URL('.polycast/polymarket-sdk-probe-20261002.json', root);
  writeFileSync(out, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({status: report.status, sdk: report.sdk?.version,
    checks: checks.length, public_requests: trace.length,
    taker_delay_enabled: report.market?.taker_delay_enabled,
    samples: report.book_samples?.map(s => ({label:s.label,gap_ms:s.observed_receipt_gap_ms,
      depth_changed:s.depth_changed,fill_before:s.initial_quote.fully_fillable,
      fill_later:s.later_at_frozen_limit?.fully_fillable})), error: report.error,
    output: out.pathname}));
}
