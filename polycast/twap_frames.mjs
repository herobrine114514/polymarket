// Parse the public RTDS 60-second BTC/USD TWAP without using display-only prices.
export function acceptedTwapPoints(frame, nowMs, lastTimestamp = 0) {
  if (frame?.topic !== 'crypto_prices_twap_sixty' ||
      frame.payload?.symbol !== 'btc/usd' || frame.payload?.window_s !== 60) return [];
  const points = Array.isArray(frame.payload.data) ? frame.payload.data : [frame.payload];
  const accepted = [];
  for (const point of points) {
    const timestamp = Number(point?.timestamp);
    const full = String(point?.full_accuracy_value || '');
    if (!Number.isSafeInteger(timestamp) || timestamp <= lastTimestamp ||
        timestamp > nowMs + 5000 || nowMs - timestamp > 120000 ||
        !/^\d{15,30}$/.test(full)) continue;
    accepted.push({ timestamp_ms: timestamp, full_accuracy_value: full });
    lastTimestamp = timestamp;
  }
  return accepted;
}

function pointTimestamp(value) {
  if (typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    value = Date.parse(value);
  }
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

// Sequence state belongs to this connection and this TWAP channel only.
// After a gap, updates stay unusable until a fresh snapshot seeds state.
export class TwapFrameDecoder {
  constructor(connectionId) {
    if (typeof connectionId !== 'string' || !connectionId) throw new Error('Missing connection ID');
    this.connectionId = connectionId;
    this.lastSequence = null;
    this.contiguous = false;
    this.lastIssue = null;
  }

  decode(frame, receivedAt) {
    this.lastIssue = null;
    if (!Number.isSafeInteger(receivedAt) || receivedAt <= 0) return [];
    const legacy = frame?.topic === 'crypto_prices_twap_sixty';
    const raw = frame?.channel === 'price.crypto.twap' && frame?.v === 1;
    const sdk = frame?.topic === 'prices.crypto.twap' &&
      (frame?.type === 'subscribe' || frame?.type === 'update');
    if (!legacy && !raw && !sdk) return [];
    const payload = frame.payload;
    if (!payload || typeof payload !== 'object') return [];
    const window = legacy ? payload.window_s : (raw ? payload.window_seconds :
      (payload.windowSeconds ?? payload.window_seconds));
    if (payload.symbol !== (legacy ? 'btc/usd' : 'btcusd') || window !== 60 ||
        (raw && payload.source !== 'chainlink')) return [];
    const snapshot = raw ? frame.snapshot === true : sdk && frame.type === 'subscribe';
    if (!legacy) {
      const seq = frame.seq;
      if (!Number.isSafeInteger(seq) || seq < 0 ||
          (frame.dropped !== undefined && (!Number.isSafeInteger(frame.dropped) || frame.dropped < 0))) {
        this.lastIssue = 'invalid_sequence';
        this.contiguous = false;
        return [];
      }
      // Out-of-order/repeated frames cannot reset state through an old snapshot.
      if (this.lastSequence !== null && seq <= this.lastSequence) {
        this.lastIssue = 'duplicate_or_reordered_frame';
        return [];
      }
      const gap = this.lastSequence !== null && seq !== this.lastSequence + 1;
      this.lastSequence = seq;
      if (snapshot) this.contiguous = !(frame.dropped > 0);
      else if (gap || frame.dropped > 0) this.contiguous = false;
      if (!this.contiguous) {
        this.lastIssue = 'snapshot_required_after_discontinuity';
        return [];
      }
    }
    const points = Array.isArray(payload.data) ? payload.data : [payload];
    const accepted = [];
    for (const point of points) {
      const timestamp = pointTimestamp(point?.timestamp);
      const full = sdk ? point?.value : point?.full_accuracy_value;
      const valid = legacy ? typeof full === 'string' && /^\d{15,30}$/.test(full) && /[1-9]/.test(full) :
        typeof full === 'string' && /^\d{1,10}(?:\.\d{1,30})?$/.test(full) && /[1-9]/.test(full);
      if (!timestamp || timestamp > receivedAt + 5000 || receivedAt - timestamp > 120000 || !valid) continue;
      accepted.push({ schema_version: 1,
        timestamp_ms: timestamp, received_at_ms: receivedAt, full_accuracy_value: full,
        feed_protocol: legacy ? 'rtds' : raw ? 'polybolt_raw_v1' : 'polybolt_sdk_v1',
        price_encoding: legacy ? 'e18' : 'decimal_usd', price_source: 'chainlink',
        symbol: 'btcusd', quote_currency: 'USD', window_seconds: 60,
        connection_id: this.connectionId,
        ...(legacy ? { continuity: 'unsequenced' } :
          { channel_seq: frame.seq, sequence_contiguous: true, snapshot }) });
    }
    return accepted.sort((a, b) => a.timestamp_ms - b.timestamp_ms);
  }
}
