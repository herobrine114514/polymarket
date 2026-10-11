"""Prospective BTC 5m research. Reads the trading DB; never submits orders.

Register before observing forecasts. Immutable per-event records are written
before the window ends. A separate report joins verified outcomes afterward.
Run with the isolated, pinned calibration Python environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.btc5m_model_diagnostics import build_report, probability, strict_mid
from experiments.calibration_walkforward import fit_sigmoid, remap
from polycast.market import parse_utc

EXPERIMENT = "btc5m-prospective-calibration-v1"
PARAMETERS = {"C": 1.0, "solver": "lbfgs", "max_iter": 1000, "random_state": 0,
              "epsilon": 1e-6, "min_train_events": 20, "min_each_outcome": 5,
              "embargo_minutes": 60}
PACKAGES = {"scikit-learn": "1.9.1", "numpy": "2.4.6", "scipy": "1.17.1"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def start_parent_monitor(stop: threading.Event, fd: int) -> threading.Thread:
    """Watch EOF without a blocking read on Windows' CRT stdin descriptor.

    A blocked CRT read can stall NumPy's native initialization. The launcher
    uses this pipe only for ownership, so a kernel peek detects broken pipes;
    any unexpected bytes are drained using ReadFile, outside the CRT lock.
    """
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        peek.restype = wintypes.BOOL
        read = kernel.ReadFile
        read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        read.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(fd)

        def monitor():
            try:
                while not stop.is_set():
                    available = wintypes.DWORD()
                    if not peek(handle, None, 0, None, ctypes.byref(available), None):
                        break
                    if available.value:
                        buffer = ctypes.create_string_buffer(min(available.value, 4096))
                        received = wintypes.DWORD()
                        if not read(handle, buffer, len(buffer), ctypes.byref(received), None):
                            break
                    stop.wait(.25)
            finally:
                stop.set()
    else:
        import select
        def monitor():
            try:
                while not stop.is_set():
                    readable, _, _ = select.select([fd], [], [], .25)
                    if readable and not os.read(fd, 4096):
                        break
            finally:
                stop.set()
    thread = threading.Thread(target=monitor, daemon=True, name="research-parent-pipe")
    thread.start()
    return thread


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def source_hashes(root: Path) -> dict:
    paths = [*sorted((root / "polycast").glob("*.py")),
             *sorted((root / "polycast").glob("*.mjs")), root / "polycast_realtime.pyw",
             root / "experiments/btc5m_model_diagnostics.py",
             root / "experiments/calibration_walkforward.py", Path(__file__).resolve()]
    configuration = root / ".polycast/sources.json"
    if configuration.is_file():
        paths.append(configuration)
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths}


def write_once(path: Path, value: dict) -> bool:
    """Publish complete JSON without overwriting a previously frozen record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def connect_readonly(database: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def register(database: Path, output: Path, parent: Path, root: Path = ROOT) -> dict:
    prior = json.loads(parent.read_text(encoding="utf-8"))
    if prior.get("experiment") != "btc5m-logit-calibration-v1" or prior.get("parameters") != PARAMETERS:
        raise ValueError("Parent experiment parameters differ")
    packages = {name: version(name) for name in PACKAGES}
    if packages != PACKAGES:
        raise ValueError("Calibration dependency versions differ")
    conn = connect_readonly(database)
    try:
        conn.execute("SELECT id FROM predictions LIMIT 1").fetchone()
    finally:
        conn.close()
    manifest = {
        "experiment": EXPERIMENT, "registered_at": utcnow().isoformat(),
        "database_identity": hashlib.sha256(str(database.resolve()).casefold().encode()).hexdigest(),
        "model_version": "deepseek-v4-pro",
        "prompt_version": "btc5m-v6-explicit-utc-volatility-units",
        "parameters": PARAMETERS, "packages": packages,
        "parent_manifest_sha256": hashlib.sha256(parent.read_bytes()).hexdigest(),
        "source_hashes": source_hashes(root),
        "selection": "First consistent reviewed forecast per single-market event observed after registration and strictly before end/result; retain raw abstentions and both review decisions",
        "training": "Historical extractor's last eligible original nonabstaining forecast per verified event; exact model/prompt; result and receipt strictly before target as_of; event ends at least 60 minutes earlier",
        "evaluation": "Only probabilities actually published before end/result; verified outcomes later; raw/calibrated/strict premid on identical events; retain all coverage and exclusion counts",
        "scope": "Prospective research only; no order/forecast changes, no IID inference or live-admission threshold",
    }
    if not write_once(output / "manifest.json", manifest):
        raise FileExistsError("A registered experiment already exists")
    return manifest


TARGET_SQL = """SELECT p.*,m.event_id,m.end_at,
    (SELECT COUNT(*) FROM markets s WHERE s.event_id=m.event_id) AS event_markets,
    d.payload_json,d.result_json,d.created_at AS draft_created_at,
    b.captured_at,b.book_timestamp,b.p_yes_mid AS book_mid,
    b.yes_best_bid AS book_bid,b.yes_best_ask AS book_ask,
    a.captured_at AS staged_at,a.book_timestamp AS staged_book_at,
    a.yes_best_bid AS staged_bid,a.yes_best_ask AS staged_ask,
    r.winning_side,r.resolved_at,r.recorded_at
    FROM predictions p JOIN markets m ON m.id=p.market_id
    LEFT JOIN analysis_drafts d ON d.market_id=p.market_id AND d.as_of=p.as_of
      AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash
    LEFT JOIN market_baselines b ON b.prediction_id=p.id
    LEFT JOIN analysis_market_baselines a ON a.market_id=p.market_id AND a.as_of=p.as_of
      AND a.rules_hash=p.rules_hash AND a.evidence_hash=p.evidence_hash
    LEFT JOIN resolutions r ON r.market_id=p.market_id
    WHERE m.category='crypto5m' AND julianday(p.as_of)>=julianday(?)"""


def target_record(row: dict, manifest: dict, now: datetime) -> dict | None:
    try:
        output = json.loads(row["model_output_json"])
        payload = json.loads(row["payload_json"] or "null")
        draft = json.loads(row["result_json"] or "null")
        as_of, end = parse_utc(row["as_of"]), parse_utc(row["end_at"])
        reviewed, created = parse_utc(output.get("reviewed_at")), parse_utc(row["draft_created_at"])
        if (not all((as_of, end, reviewed, created)) or row["event_markets"] != 1 or
                not parse_utc(manifest["registered_at"]) <= as_of <= created <= reviewed <= now < end or
                row["winning_side"] is not None or
                output.get("model_version") != manifest["model_version"] or
                output.get("prompt_version") != manifest["prompt_version"]):
            return None
        if (not isinstance(payload, dict) or not isinstance(draft, dict) or
                payload.get("market_id") != row["market_id"] or payload.get("category") != "crypto5m" or
                parse_utc(payload.get("as_of")) != as_of or parse_utc(payload.get("end_at")) != end or
                draft.get("market_id") != row["market_id"] or parse_utc(draft.get("as_of")) != as_of or
                not isinstance(draft.get("abstain"), bool) or not probability(output.get("p_yes")) or
                draft.get("p_yes") != output["p_yes"] or
                ("analysis_abstain" in output and output["analysis_abstain"] is not draft["abstain"]) or
                not isinstance(output.get("review", {}).get("approved"), bool)):
            return None
        return {"event_id": row["event_id"], "market_id": row["market_id"], "prediction_id": row["id"],
                "as_of": as_of.isoformat(), "end_at": end.isoformat(), "reviewed_at": reviewed.isoformat(),
                "model_version": output["model_version"], "prompt_version": output["prompt_version"],
                "model": float(output["p_yes"]), "market": strict_mid(row, as_of),
                "analysis_abstain": draft["abstain"], "review_approved": output["review"]["approved"],
                "rules_hash": row["rules_hash"], "evidence_hash": row["evidence_hash"],
                "payload_sha256": hashlib.sha256(row["payload_json"].encode()).hexdigest(),
                "draft_sha256": hashlib.sha256(row["result_json"].encode()).hexdigest()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def calibrate(target: dict, history: list[dict], parameters: dict, fit=fit_sigmoid) -> dict:
    as_of = parse_utc(target["as_of"])
    cutoff = as_of - timedelta(minutes=parameters["embargo_minutes"])
    training = [e for e in history if e["event_id"] != target["event_id"] and
                not e["analysis_abstain"] and probability(e["model"]) and
                (e["model_version"], e["prompt_version"]) ==
                (target["model_version"], target["prompt_version"]) and
                parse_utc(e["end_at"]) <= cutoff and parse_utc(e["as_of"]) < as_of and
                parse_utc(e["resolved_at"]) < as_of and parse_utc(e["recorded_at"]) < as_of]
    training.sort(key=lambda e: (parse_utc(e["as_of"]), e["prediction_id"]))
    if len({e["event_id"] for e in training}) != len(training):
        raise ValueError("Duplicate training events")
    record = {**target, "training_events": len(training), "training_sha256": fingerprint(training),
              "training_event_ids": [e["event_id"] for e in training], "calibrated": None, "fitted": None}
    counts = Counter(e["outcome"] for e in training)
    if target["analysis_abstain"]:
        record["status"] = "abstained"
    elif len(training) < parameters["min_train_events"]:
        record["status"] = "insufficient_training"
    elif min(counts.get(0, 0), counts.get(1, 0)) < parameters["min_each_outcome"]:
        record["status"] = "insufficient_outcome_coverage"
    else:
        try:
            fitted = fit(training, parameters)
            if not all(math.isfinite(fitted[k]) for k in ("slope", "intercept")) or fitted["slope"] <= 0:
                record["status"] = "nonmonotonic_or_invalid_fit"
            else:
                record.update(status="candidate", fitted=fitted,
                              calibrated=remap(target["model"], fitted, parameters["epsilon"]))
        except Exception as exc:
            record.update(status="fit_failed", failure_type=type(exc).__name__)
    return record


def evaluate(records: list[dict], resolutions: dict, manifest_hash: str, now: datetime) -> dict:
    paired, raw, invalid, pending = [], [], 0, 0
    identifiers = Counter(r.get("event_id") for r in records)
    for saved in records:
        record = dict(saved)
        receipt = record.pop("publication", None)
        stored_hash = record.pop("record_sha256", None)
        try:
            intact = stored_hash == fingerprint(record) and record.get("manifest_sha256") == manifest_hash
        except (ValueError, TypeError):
            intact = False
        if not intact:
            invalid += 1
            continue
        try:
            at, end = parse_utc(record.get("recorded_at")), parse_utc(record.get("end_at"))
            published = parse_utc(receipt.get("published_at")) if isinstance(receipt, dict) else None
            if (not all((at, end, published)) or identifiers[record["event_id"]] != 1 or
                    receipt.get("record_sha256") != stored_hash or
                    receipt.get("manifest_sha256") != manifest_hash or
                    not parse_utc(record["reviewed_at"]) <= at <= published < end or published > now):
                raise ValueError("Invalid publication receipt")
            resolved = resolutions.get(record["market_id"])
            if not resolved or parse_utc(resolved["recorded_at"]) > now:
                pending += 1
                continue
            if (not published < parse_utc(resolved["resolved_at"]) or
                    resolved["winning_side"] not in ("YES", "NO")):
                raise ValueError("Result predates publication")
            record["outcome"] = int(resolved["winning_side"] == "YES")
            if not record["analysis_abstain"]:
                raw.append(record)
            if record["status"] == "candidate" and probability(record.get("calibrated")):
                paired.append(record)
        except (ValueError, KeyError, TypeError, AttributeError):
            invalid += 1

    def score(items: list[dict], fields: tuple[str, ...]) -> dict:
        selected = [r for r in items if all(probability(r.get(k)) for k in fields)]
        return {"events": len(selected), "brier": {
            k: round(sum((r[k] - r["outcome"]) ** 2 for r in selected) / len(selected), 6)
            if selected else None for k in fields}}

    return {"frozen_events": len(records), "invalid_records": invalid, "pending_events": pending,
            "status_counts": dict(Counter(r.get("status", "unknown") for r in records)),
            "resolved_raw": score(raw, ("model",)),
            "resolved_raw_market": score(raw, ("model", "market")),
            "resolved_calibrated": score(paired, ("model", "calibrated")),
            "resolved_calibrated_market": score(paired, ("model", "calibrated", "market"))}


def observe(database: Path, output: Path, manifest: dict, root: Path = ROOT,
            clock=utcnow, fit=fit_sigmoid) -> dict:
    now = clock()
    manifest_hash = fingerprint(manifest)
    if (manifest.get("experiment") != EXPERIMENT or manifest.get("parameters") != PARAMETERS or
            manifest.get("database_identity") != hashlib.sha256(str(database.resolve()).casefold().encode()).hexdigest() or
            manifest.get("source_hashes") != source_hashes(root) or
            manifest.get("packages") != {name: version(name) for name in PACKAGES}):
        raise ValueError("Registered source, database, parameters or dependencies changed")
    conn = connect_readonly(database)
    try:
        conn.execute("BEGIN")
        rows = [dict(r) for r in conn.execute(TARGET_SQL, (manifest["registered_at"],))]
        history = build_report(conn, now)["events"]
        resolutions = {r["market_id"]: dict(r) for r in conn.execute("SELECT * FROM resolutions")}
    finally:
        conn.close()
    targets = [t for r in rows if (t := target_record(r, manifest, now)) is not None]
    targets.sort(key=lambda r: (parse_utc(r["as_of"]), r["prediction_id"]))
    for target in targets:
        path = output / "events" / (hashlib.sha256(target["event_id"].encode()).hexdigest() + ".json")
        if path.exists():
            continue
        record = calibrate(target, history, manifest["parameters"], fit)
        finished = clock()
        # A long fit cannot turn a post-event calculation into a prospective prediction.
        if finished >= parse_utc(target["end_at"]):
            continue
        conn = connect_readonly(database)
        try:
            if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (target["market_id"],)).fetchone():
                continue
        finally:
            conn.close()
        finished = clock()
        if finished >= parse_utc(target["end_at"]):
            continue
        record.update(recorded_at=finished.isoformat(), manifest_sha256=manifest_hash)
        record["record_sha256"] = fingerprint(record)
        if write_once(path, record):
            # Receipt time is read AFTER the complete forecast was published.
            # Missing or late receipts can never receive prospective score credit.
            write_once(output / "receipts" / path.name, {
                "published_at": clock().isoformat(), "record_sha256": record["record_sha256"],
                "manifest_sha256": manifest_hash})
    records, malformed = [], 0
    for path in sorted((output / "events").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Expected a record")
            if path.stem != hashlib.sha256(value["event_id"].encode()).hexdigest():
                raise ValueError("Record identity does not match file")
            receipt = output / "receipts" / path.name
            value["publication"] = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
            records.append(value)
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            malformed += 1
    forecast_events = {r["event_id"] for r in rows}
    frozen_ids = {r["event_id"] for r in records}
    ended_without_record = {r["event_id"] for r in rows if r["event_id"] not in frozen_ids and
                            parse_utc(r["end_at"]) is not None and parse_utc(r["end_at"]) <= now}
    checked_at = clock()
    report = {"experiment": EXPERIMENT, "status": "running", "checked_at": checked_at.isoformat(),
              "registered_at": manifest["registered_at"], "manifest_sha256": manifest_hash,
              "model_version": manifest["model_version"], "prompt_version": manifest["prompt_version"],
              "training_minimum": PARAMETERS["min_train_events"], "embargo_minutes": PARAMETERS["embargo_minutes"],
              **evaluate(records, resolutions, manifest_hash, checked_at), "malformed_records": malformed,
              "coverage": {"forecast_events_after_registration": len(forecast_events),
                           "forecast_events_without_record": len(forecast_events - frozen_ids),
                           "ended_without_record": len(ended_without_record)},
              "scope": "Prospective research; no order or live-admission credit"}
    write_report(output / "status.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "once", "watch"))
    parser.add_argument("--database", type=Path, default=ROOT / ".polycast/polycast.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / ".polycast/calibration-prospective-v1")
    parser.add_argument("--parent", type=Path, default=ROOT / ".polycast/calibration-experiment-v1.json")
    parser.add_argument("--parent-pipe", action="store_true")
    args = parser.parse_args()
    if args.command == "register":
        manifest = register(args.database, args.output, args.parent)
        print(json.dumps({"experiment": manifest["experiment"], "registered_at": manifest["registered_at"]}))
        return
    manifest = json.loads((args.output / "manifest.json").read_text(encoding="utf-8"))
    stop = threading.Event()
    if args.parent_pipe:
        start_parent_monitor(stop, sys.stdin.fileno())
    while not stop.is_set():
        try:
            report = observe(args.database, args.output, manifest)
        except Exception as exc:
            report = {"experiment": EXPERIMENT, "status": "error", "checked_at": utcnow().isoformat(),
                      "failure_type": type(exc).__name__, "scope": "Prospective research; no order credit"}
            write_report(args.output / "status.json", report)
        if args.command == "once":
            print(json.dumps(report, ensure_ascii=False))
            return
        stop.wait(10)


if __name__ == "__main__":
    main()
