"""Verified closed-journal reads for offline replay, including lossless gzip.

Active/unsealed files are never read as complete data. No network, orders,
training or historical forecast rewrites. Consumers publish replay results only
after fully consuming the requested sequence; a failed segment aborts the run.
"""
from __future__ import annotations
import gzip
import hashlib
import json
from pathlib import Path

from polycast.microstructure_archive import contained_file, read_seal

MAX_SEGMENT_BYTES = 64 * 1024 ** 2


def segment_records(directory: Path, seal_path: Path):
    """Verify bytes and every record identity before yielding this segment."""
    original, seal = read_seal(directory, seal_path)
    size = seal["bytes"]
    first, last = seal.get("first_record_seq"), seal.get("last_record_seq")
    if (size > MAX_SEGMENT_BYTES or type(first) is not int or type(last) is not int or not 1 <= first <= last):
        raise ValueError("Unusable segment size/sequence seal")
    index_path = contained_file(directory, original.name + ".archive.json")
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        compressed = contained_file(directory, original.name + ".gz")
        if (type(index.get("schema_version")) is not int or index["schema_version"] != 1 or
                index.get("method") != "gzip-roundtrip-sha256-v1" or
                index.get("filename") != original.name or index.get("archive") != compressed.name or
                index.get("raw_sha256") != seal["sha256"] or index.get("raw_bytes") != size or
                index.get("first_record_seq") != first or index.get("last_record_seq") != last or
                type(index.get("archive_bytes")) is not int or not 0 < index["archive_bytes"] <= MAX_SEGMENT_BYTES):
            raise ValueError("Archive index disagrees with segment seal")
        with compressed.open("rb") as stream:
            encoded = stream.read(MAX_SEGMENT_BYTES + 1)
        if len(encoded) != index["archive_bytes"] or hashlib.sha256(encoded).hexdigest() != index.get("archive_sha256"):
            raise ValueError("Compressed archive changed since publication")
        # The decompression bound also rejects corrupt indexes or expansion bombs.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(encoded), mode="rb") as stream:
            raw = stream.read(size + 1)
    else:
        with original.open("rb") as stream:
            raw = stream.read(size + 1)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != seal["sha256"] or not raw.endswith(b"\n"):
        raise ValueError("Closed journal content/hash/length mismatch")
    # Do a full structural preflight before exposing any records to a replay.
    expected = first
    import io
    for line in io.BytesIO(raw):
        item = json.loads(line)
        if (not isinstance(item, dict) or type(item.get("schema_version")) is not int or item["schema_version"] != 1 or
                item.get("session_id") != seal["session_id"] or type(item.get("record_seq")) is not int or
                item["record_seq"] != expected):
            raise ValueError("Closed journal identity/sequence mismatch")
        expected += 1
    if expected != last + 1:
        raise ValueError("Closed journal sequence endpoints mismatch")
    for line in io.BytesIO(raw):
        yield json.loads(line)


def session_records(directory: Path, session_id: str):
    """Read all currently sealed segments, requiring a dense session prefix."""
    import re
    if not re.fullmatch(r"[a-zA-Z0-9-]+", session_id):
        raise ValueError("Invalid journal session")
    seals = sorted(directory.glob(f"raw-{session_id}-*.jsonl.seal.json"))
    expected = 1
    for number, path in enumerate(seals):
        original, seal = read_seal(directory, path)
        if (original.name != f"raw-{session_id}-{number:05d}.jsonl" or
                type(seal.get("first_record_seq")) is not int or seal["first_record_seq"] != expected or
                type(seal.get("last_record_seq")) is not int):
            raise ValueError("Missing or reordered closed session segment")
        expected = seal["last_record_seq"] + 1
    for path in seals:
        yield from segment_records(directory, path)


def verify_frozen_journal(directory: Path, decision_path: Path) -> dict:
    from experiments.numeric_dataset import read_decision
    snapshot, digest = read_decision(decision_path)
    session, sequence = snapshot.get("session_id"), snapshot.get("record_seq")
    import re
    if (not isinstance(session, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", session) or
            type(sequence) is not int or sequence < 1):
        raise ValueError("Frozen decision has no journal identity")
    if snapshot.get('feature_version')=='btc5m-causal-features-v3':
        return verify_causal_journal(directory,snapshot,digest)
    for path in sorted(directory.glob(f"raw-{session}-*.jsonl.seal.json")):
        original, seal = read_seal(directory, path)
        if seal.get("first_record_seq", 0) <= sequence <= seal.get("last_record_seq", 0):
            for item in segment_records(directory, path):
                if item["record_seq"] != sequence:
                    continue
                # RawJournal adds the envelope schema; the frozen snapshot predates that addition.
                equivalent = {key: value for key, value in item.items() if key != "schema_version"}
                if equivalent != snapshot:
                    raise ValueError("Frozen snapshot differs from verified journal record")
                return {"status": "verified", "filename": original.name, "session_id": session,
                        "record_seq": sequence, "snapshot_sha256": digest,
                        "raw_sha256": seal["sha256"], "training_or_profitability_validated": False}
            raise ValueError("Frozen record absent from its sealed sequence")
    raise ValueError("Frozen record segment is not yet sealed; do not use active files as complete replay")


def _causal_references(snapshot: dict) -> set[int]:
    import re
    session, sequence = snapshot.get('session_id'), snapshot.get('record_seq')
    if (not isinstance(session, str) or not re.fullmatch(r'[a-zA-Z0-9-]+', session) or
            type(sequence) is not int or sequence < 1):
        raise ValueError('Frozen decision has no journal identity')
    if snapshot.get('feature_version') != 'btc5m-causal-features-v3':
        raise ValueError('Unknown causal journal version')
    context = snapshot['causal_context']
    histories = context['coinbase'] + context['polymarket_up']
    references = context['reference']['points']
    if len(histories) + len(references) > 2200:
        raise ValueError('Unbounded or future V3 journal reference')
    sequences = [sequence, snapshot['base_snapshot_record_seq']] + [row['input_snapshot_record_seq'] for row in histories] + [row['journal_seq'] for row in references]
    # Check before set deduplication: True must not disappear as an alias of 1.
    if any(type(seq) is not int or not 0 < seq <= sequence for seq in sequences):
        raise ValueError('Unbounded or future V3 journal reference')
    wanted = set(sequences)
    if len(wanted) > 2200:
        raise ValueError('Unbounded or future V3 journal reference')
    return wanted


def _verify_causal_journal(directory: Path, snapshot: dict, digest: str, wanted: set[int], selected_records) -> dict:
    context=snapshot['causal_context']; sequence=snapshot['record_seq']; session=snapshot['session_id']
    histories=context['coinbase']+context['polymarket_up']; references=context['reference']['points']
    found={}; hashes={};expected=1
    for number,path in enumerate(sorted(directory.glob(f'raw-{session}-*.jsonl.seal.json'))):
        original,seal=read_seal(directory,path)
        if original.name!=f'raw-{session}-{number:05d}.jsonl' or seal.get('first_record_seq')!=expected:
            raise ValueError('Missing V3 source journal prefix')
        first,last=seal['first_record_seq'],seal['last_record_seq'];expected=last+1
        if any(first<=seq<=last for seq in wanted):
            records = selected_records(path, original, seal, wanted)
            for seq in wanted:
                if seq in records: found[seq] = records[seq]
            hashes[original.name]=seal['sha256']
        if last>=sequence:break
    if set(found)!=wanted:raise ValueError('V3 source record not yet sealed or missing')
    if {k:v for k,v in found[sequence].items() if k!='schema_version'}!=snapshot:
        raise ValueError('V3 frozen snapshot differs from journal')
    base=found[snapshot['base_snapshot_record_seq']]
    if base.get('feature_version')!='btc5m-public-microstructure-v2-clock-checked':raise ValueError('V3 base is not its original V2 snapshot')
    for key,value in base.items():
        if key not in ('schema_version','record_seq','feature_version','raw_through_seq') and snapshot.get(key)!=value:
            raise ValueError('V3 base inputs changed')
    for lane,items in (('coinbase',context['coinbase']),('polymarket_up',context['polymarket_up'])):
        for sample in items:
            row=found[sample['input_snapshot_record_seq']]
            if row.get('kind')!='feature_snapshot' or row.get('feature_version')!='btc5m-public-microstructure-v2-clock-checked':
                raise ValueError('Historical price does not reference a captured base snapshot')
            if lane=='coinbase':book=row['coinbase']['book'];connection=row['coinbase']['connection_id']
            else:
                if row['market']['market_id']!=snapshot['market']['market_id']:raise ValueError('History belongs to another outcome market')
                book=row['polymarket']['up'];connection=row['polymarket']['connection_id']
            expected_point={key:row[key] for key in ('received_at_ms','monotonic_ns','as_of_utc_ms','clock_reference')}
            expected_point.update(input_snapshot_record_seq=row['record_seq'],connection_id=connection,valid=bool(book),
                midpoint=book['midpoint'] if book else None,book_received_at_ms=book['received_at_ms'] if book else None,
                source_timestamp_ms=book['source_timestamp_ms'] if book else None)
            if expected_point!=sample:raise ValueError('Causal history changed from original receipt-time snapshot')
    for sample in references:
        row=found[sample['journal_seq']]
        if (row.get('kind')!='reference_observation' or row.get('raw_text')!=sample['raw_text'] or
            row.get('raw_sha256')!=sample['raw_sha256'] or row.get('received_at_ms')!=sample['first_read_at_ms'] or
            row.get('monotonic_ns')!=sample['first_read_monotonic_ns'] or row.get('source_timestamp_ms')!=sample['point']['timestamp_ms']):
            raise ValueError('Reference proxy changed from journal observation')
    return {'status':'verified_causal_context','session_id':session,'record_seq':sequence,'snapshot_sha256':digest,
      'verified_source_records':len(wanted),'verified_history_samples':len(histories),'verified_reference_observations':len(references),
      'source_segment_sha256':hashes,'training_or_profitability_validated':False,'independent_reference_verified':False}


def verify_causal_journal(directory: Path, snapshot: dict, digest: str) -> dict:
    """Match every used history/reference receipt to earlier immutable journal rows."""
    def selected_records(path, original, seal, wanted):
        return {row['record_seq']: row for row in segment_records(directory, path) if row['record_seq'] in wanted}
    return _verify_causal_journal(directory, snapshot, digest, _causal_references(snapshot), selected_records)


def verify_causal_batch(directory: Path, decisions: list[tuple[dict, str]]) -> dict:
    """Decode each needed sealed segment once, without a persistent trust cache.

    Full segment hash/identity preflight still runs before any retained row is
    used. Results keep input order and isolate invalid decisions. Only requested
    rows survive decoding; active files and failed reads are never credited.
    """
    unions, requests, results = {}, {}, [None] * len(decisions)
    for index, (snapshot, digest) in enumerate(decisions):
        try:
            wanted = _causal_references(snapshot)
            requests[index] = wanted
            unions.setdefault(snapshot['session_id'], set()).update(wanted)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            results[index] = {'status': 'rejected', 'error': str(exc)}
    if sum(len(rows) for rows in unions.values()) > 500_000:
        raise ValueError('Causal journal batch exceeds retained-record bound')
    cache, failures = {}, {}
    diagnostics = {'method': 'ephemeral-selected-records-batch-v1', 'decisions': len(decisions),
                   'segments_decoded': 0, 'source_records_decoded': 0, 'source_records_retained': 0,
                   'segment_cache_hits': 0}

    def selected_records(path, original, seal, wanted):
        # A changed seal cannot reuse bytes verified against an earlier seal.
        identity = json.dumps(seal, sort_keys=True, separators=(',', ':'))
        key = original.name
        if key in failures:
            raise ValueError(failures[key])
        if key in cache:
            previous, records = cache[key]
            if previous != identity:
                failures[key] = 'Segment seal changed during causal batch'
                raise ValueError(failures[key])
            diagnostics['segment_cache_hits'] += 1
            return records
        records, count = {}, 0
        try:
            for row in segment_records(directory, path):
                count += 1
                if row['record_seq'] in unions[seal['session_id']]:
                    records[row['record_seq']] = row
            # Also catch a seal replacement during the byte read.
            actual, current = read_seal(directory, path)
            if actual.name != original.name or json.dumps(current, sort_keys=True, separators=(',', ':')) != identity:
                raise ValueError('Segment seal changed during causal batch')
        except (ValueError, KeyError, TypeError, OSError, EOFError) as exc:
            failures[key] = str(exc)
            raise ValueError(str(exc)) from exc
        cache[key] = (identity, records)
        diagnostics['segments_decoded'] += 1
        diagnostics['source_records_decoded'] += count
        diagnostics['source_records_retained'] += len(records)
        return records

    for index, (snapshot, digest) in enumerate(decisions):
        if results[index] is not None:
            continue
        try:
            results[index] = _verify_causal_journal(directory, snapshot, digest, requests[index], selected_records)
        except (ValueError, KeyError, TypeError, OSError, EOFError) as exc:
            results[index] = {'status': 'rejected', 'error': str(exc)}
    # No earlier result is published against a seal observed to change later.
    for index, result in enumerate(results):
        changed = set(result.get('source_segment_sha256', {})).intersection(failures)
        if changed:
            results[index] = {'status': 'rejected', 'error': failures[sorted(changed)[0]]}
    diagnostics['verified_decisions'] = sum(row['status'] == 'verified_causal_context' for row in results)
    diagnostics['rejected_decisions'] = len(results) - diagnostics['verified_decisions']
    return {'results': results, 'diagnostics': diagnostics}
