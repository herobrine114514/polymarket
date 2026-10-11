/** Offline research only. No client, credentials, wallet, network or DB writes. */
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const root = new URL('../', import.meta.url);
const requireSdk = createRequire(new URL('.polycast/oss-chainlink-sdk/package.json', root));
const { decodeReport } = requireSdk('@chainlink/data-streams-sdk');
const { AbiCoder, ZeroHash } = requireSdk('ethers');
const sdkPackage = requireSdk('@chainlink/data-streams-sdk/package.json');
assert.equal(sdkPackage.version, '1.3.0');
const abi = new AbiCoder();
const outerTypes = ['bytes32[3]', 'bytes', 'bytes32[]', 'bytes32[]', 'bytes32'];
const v2Types = ['bytes32', 'uint32', 'uint32', 'uint192', 'uint192', 'uint32', 'int192'];
// These identifiers are deliberately synthetic, never configured production feeds.
const syntheticV2 = `0x0002${'11'.repeat(30)}`;
const differentV2 = `0x0002${'22'.repeat(30)}`;
const syntheticV3 = `0x0003${'33'.repeat(30)}`;
const syntheticPrice = 61800123456789012345678n;
const validFrom = 1700000008, observed = 1700000010, expires = 1700000310;
const unsigned = abi.encode(outerTypes, [
  [ZeroHash, ZeroHash, ZeroHash],
  abi.encode(v2Types, [syntheticV2, validFrom, observed, 0, 0, expires, syntheticPrice]),
  [], [], ZeroHash,
]);

/** Structural checks only. This can NEVER grant signature/settlement credit. */
function inspectV2Twap(fullReport, expectedFeed, boundary, metadata) {
  assert.match(expectedFeed, /^0x0002[0-9a-f]{60}$/i, 'V2 feed required');
  assert.equal(metadata.kind, 'twap');
  assert.equal(metadata.windowSeconds, 60);
  assert.equal(metadata.decimals, 18);
  assert.ok(Number.isSafeInteger(boundary), 'Boundary must be integer Unix seconds');
  const outer = abi.decode(outerTypes, fullReport);
  const body = abi.decode(v2Types, outer[1]);
  assert.equal((outer[1].length - 2) / 2, 7 * 32, 'Exact V2 body length required');
  assert.equal(body[0].toLowerCase(), expectedFeed.toLowerCase(), 'Embedded feed differs');
  const from = Number(body[1]), through = Number(body[2]), expiry = Number(body[5]);
  assert.ok(from <= through && through <= expiry, 'Invalid time interval');
  assert.ok(from <= boundary && boundary <= through, 'Boundary outside declared validity');
  const decoded = decodeReport(fullReport, expectedFeed);
  assert.equal(decoded.version, 'V2');
  assert.equal(decoded.price, body[6]);
  assert.ok(decoded.price > 0n);
  return {
    feed_id: body[0], valid_from: from, observations_timestamp: through,
    expires_at: expiry, boundary, price_integer: decoded.price.toString(),
    decimals: metadata.decimals, window_seconds: metadata.windowSeconds,
    supplied_signature_count: outer[2].length,
    signature_verified: false, settlement_eligible: false,
    trust_status: 'decoded_not_verified',
  };
}

const checks = [];
function check(name, run) { run(); checks.push({name, passed: true}); }
const metadata = {kind: 'twap', windowSeconds: 60, decimals: 18};
let synthetic, published;
check('synthetic_unsigned_v2_decodes_with_exact_bigint_precision', () => {
  synthetic = inspectV2Twap(unsigned, syntheticV2, validFrom, metadata);
  assert.equal(synthetic.price_integer, syntheticPrice.toString());
});
check('unsigned_decode_never_grants_verification_or_settlement_credit', () => {
  assert.equal(synthetic.supplied_signature_count, 0);
  assert.equal(synthetic.signature_verified, false);
  assert.equal(synthetic.settlement_eligible, false);
});
check('sdk_accepts_same_schema_but_different_requested_feed', () => {
  // Regression evidence: the SDK selects a schema; callers must check identity.
  assert.equal(decodeReport(unsigned, differentV2).price, syntheticPrice);
});
check('structural_inspection_rejects_embedded_feed_mismatch', () => {
  assert.throws(() => inspectV2Twap(unsigned, differentV2, validFrom, metadata), /Embedded feed/);
});
check('declared_validity_covers_both_endpoints_and_rejects_outside', () => {
  inspectV2Twap(unsigned, syntheticV2, observed, metadata);
  for (const boundary of [validFrom - 1, observed + 1]) {
    assert.throws(() => inspectV2Twap(unsigned, syntheticV2, boundary, metadata), /outside/);
  }
});
check('v3_spot_identifier_is_rejected_as_v2_twap', () => {
  assert.throws(() => inspectV2Twap(unsigned, syntheticV3, validFrom, metadata), /V2 feed/);
});
check('incorrect_window_or_spot_metadata_is_rejected', () => {
  assert.throws(() => inspectV2Twap(unsigned, syntheticV2, validFrom, {...metadata,windowSeconds: 30}));
  assert.throws(() => inspectV2Twap(unsigned, syntheticV2, validFrom, {...metadata,kind: 'spot'}));
});
check('malformed_report_is_rejected_by_sdk', () => {
  assert.throws(() => decodeReport('0x1234', syntheticV2));
});
check('published_historical_eth_testnet_v3_fixture_decodes', () => {
  const fixture = JSON.parse(readFileSync(new URL('.polycast/chainlink-docs-fixture-20261002.json', root), 'utf8'));
  const outer = abi.decode(outerTypes, fixture.full_report);
  const body = abi.decode([...v2Types, 'int192', 'int192'], outer[1]);
  assert.equal(body[0].toLowerCase(), fixture.feed_id.toLowerCase());
  const decoded = decodeReport(fixture.full_report, fixture.feed_id);
  assert.equal(decoded.version, 'V3');
  assert.equal(decoded.price, body[6]);
  assert.ok(decoded.bid <= decoded.price && decoded.price <= decoded.ask);
  published = {
    source_url: fixture.source_url, fixture_kind: fixture.fixture_kind,
    full_report_sha256: createHash('sha256').update(Buffer.from(fixture.full_report.slice(2), 'hex')).digest('hex'),
    feed_id: body[0], valid_from: Number(body[1]), observations_timestamp: Number(body[2]),
    price_integer: decoded.price.toString(), supplied_signature_count: outer[2].length,
    signature_verified: false, settlement_eligible: false,
    trust_status: 'decoded_not_verified',
  };
});
check('published_v3_fixture_cannot_be_accepted_as_synthetic_v2_twap', () => {
  const fixture = JSON.parse(readFileSync(new URL('.polycast/chainlink-docs-fixture-20261002.json', root), 'utf8'));
  assert.throws(() => inspectV2Twap(fixture.full_report, syntheticV2, validFrom, metadata));
});
const report = {
  checked_at: new Date().toISOString(), sdk_version: sdkPackage.version, sdk_license: sdkPackage.license,
  package_lock_sha256: createHash('sha256').update(readFileSync(new URL('.polycast/oss-chainlink-sdk/package-lock.json', root))).digest('hex'),
  scope: 'Isolated offline SDK compatibility/structural probe. No current BTC report or cryptographic verification; no production integration.',
  checks, synthetic_unsigned_v2: synthetic, published_historical_v3: published,
  network_requests: 0, model_calls: 0, order_calls: 0, database_writes: 0,
};
const output = process.argv[2];
if (output) writeFileSync(output, JSON.stringify(report, null, 2) + '\n', 'utf8');
console.log(JSON.stringify({checks_passed: checks.length, sdk_version: sdkPackage.version,
  output: output || null, signature_verified: false, settlement_eligible: false}));
