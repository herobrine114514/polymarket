"""Read-only provenance and settlement evaluation of actual Jev publication files."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.calibration_live_shadow import connect_readonly, fingerprint, write_report
from experiments.jev_btc5m_shadow import PARAMETERS, QUESTIONS, frozen_state
from experiments.jev_gateway_probe import MODEL, ENDPOINT, request_body, utcnow, valid_probability
from polycast.market import parse_utc


def validate_record(record, receipt, manifest, payload_json):
    original = dict(record)
    stored = original.pop("record_sha256")
    baseline = record["baseline"]
    manifest_hash = fingerprint(manifest)
    if (fingerprint(original) != stored or receipt["record_sha256"] != stored
            or receipt["manifest_sha256"] != manifest_hash or record["manifest_sha256"] != manifest_hash
            or manifest["candidate_model"] != MODEL or manifest["endpoint"] != ENDPOINT
            or manifest["questions"] != QUESTIONS or record["requested_model"] != MODEL
            or not 1 <= manifest["parameters"]["maximum_calls"] <= PARAMETERS["maximum_calls"]
            or any(manifest["parameters"].get(k) != v for k,v in PARAMETERS.items() if k != "maximum_calls")
            or record["production_database_written"] is not False or record["orders_submitted"] != 0
            or record["independent_review_run"] is not False):
        raise ValueError("Publication identity or registered method mismatch")
    registered, as_of, started, completed, published, end = map(parse_utc, [manifest["registered_at"],
        baseline["as_of"], record["started_at"], record["completed_at"], receipt["published_at"], baseline["end_at"]])
    if (not all([registered, as_of, started, completed, published, end])
            or not registered <= as_of <= started <= completed <= published < end
            or not parse_utc(baseline["reviewed_at"]) <= started
            or (published-as_of).total_seconds() > manifest["parameters"]["max_input_age_seconds"]
            or record["finished_before_event_end"] is not True):
        raise ValueError("Missing timely pre-outcome publication")
    payload_digest = hashlib.sha256(payload_json.encode()).hexdigest()
    payload = json.loads(payload_json)
    request = request_body(frozen_state(payload), QUESTIONS)
    if (payload_digest != baseline["payload_sha256"] or payload_digest != record["parent_payload_sha256"]
            or record["request_sha256"] != fingerprint(request)
            or payload["market_id"] != baseline["market_id"] or parse_utc(payload["as_of"]) != as_of
            or parse_utc(payload["end_at"]) != end or baseline["analysis_abstain"] is not False):
        raise ValueError("Original input provenance mismatch")
    if record["status"] == "completed":
        answers = record["answers"]
        if (set(answers) != set(QUESTIONS) or not all(valid_probability(v) for v in answers.values())
                or record["candidate_p_up"] != answers["up"]
                or record["research_abstain"] is not (answers["unclear_direction"] >= manifest["parameters"]["abstain_threshold"])):
            raise ValueError("Candidate or abstention mismatch")
    return baseline


def evaluate(folders, connection, now):
    valid, rows, invalid = [], [], []
    for folder in folders:
        manifest = json.loads((folder/"manifest.json").read_text())
        intents = list((folder/"call-intents").glob("*.json"))
        if len(intents) > manifest["parameters"]["maximum_calls"]:
            invalid.append({"experiment": folder.name, "reason": "Call reservation budget exceeded"})
            continue
        for path in sorted((folder/"responses").glob("*.json")):
            try:
                record = json.loads(path.read_text())
                receipt = json.loads((folder/"receipts"/path.name).read_text())
                baseline = record["baseline"]
                if path.stem != hashlib.sha256(baseline["event_id"].encode()).hexdigest():
                    raise ValueError("Wrong event filename")
                source = connection.execute("""SELECT d.payload_json,d.result_json,m.event_id,p.model_output_json FROM predictions p
                    JOIN markets m ON m.id=p.market_id JOIN analysis_drafts d ON d.market_id=p.market_id
                    AND d.as_of=p.as_of AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash
                    WHERE p.id=? AND p.market_id=?""", (baseline["prediction_id"],baseline["market_id"])).fetchall()
                if (len(source) != 1 or source[0]["event_id"] != baseline["event_id"]
                        or hashlib.sha256(source[0]["result_json"].encode()).hexdigest() != baseline["draft_sha256"]):
                    raise ValueError("Parent analysis identity mismatch")
                parent = json.loads(source[0]["model_output_json"])
                draft = json.loads(source[0]["result_json"])
                if (parent["model_version"] != manifest["model_version"]
                        or parent["prompt_version"] != manifest["prompt_version"]
                        or parent["p_yes"] != baseline["model"] or draft["p_yes"] != baseline["model"]
                        or draft["abstain"] is not False or parent["review"]["approved"] is not baseline["review_approved"]):
                    raise ValueError("Parent model or review provenance mismatch")
                validate_record(record, receipt, manifest, source[0]["payload_json"])
                resolution = connection.execute("SELECT * FROM resolutions WHERE market_id=?", (baseline["market_id"],)).fetchone()
                settled = (resolution and resolution["winning_side"] in ("YES","NO")
                    and parse_utc(baseline["end_at"]) <= parse_utc(resolution["resolved_at"])
                    and parse_utc(resolution["resolved_at"]) <= parse_utc(resolution["recorded_at"]) <= now
                    and parse_utc(receipt["published_at"]) < parse_utc(resolution["resolved_at"]))
                row = {"experiment": folder.name, "event_id": baseline["event_id"],
                       "prediction_id": baseline["prediction_id"], "market_id": baseline["market_id"],
                       "as_of": baseline["as_of"], "end_at": baseline["end_at"],
                       "published_at": receipt["published_at"], "latency_seconds": record["elapsed_seconds"],
                       "status": record["status"], "research_abstain": record["research_abstain"],
                       "candidate_p_up": record["candidate_p_up"], "parent_p_up": baseline["model"],
                       "market_mid": baseline["market"], "parent_review_approved": baseline["review_approved"],
                       "usage": record.get("usage"), "input_and_publication_hashes_valid": True,
                       "winning_side": resolution["winning_side"] if settled else None,
                       "resolution_source": resolution["source_url"] if settled else None,
                       "resolved_at": resolution["resolved_at"] if settled else None}
                rows.append(row)
            except Exception as exc:
                invalid.append({"experiment": folder.name, "file": path.name, "failure_type": type(exc).__name__})
    counts = Counter(r["event_id"] for r in rows)
    duplicate_ids = {key for key,count in counts.items() if count > 1}
    valid = [r for r in rows if r["event_id"] not in duplicate_ids]
    completed = [r for r in valid if r["status"] == "completed"]
    scored = [r for r in completed if not r["research_abstain"] and r["winning_side"]]
    paired = [r for r in scored if valid_probability(r["market_mid"])]
    def brier(sample, key):
        return round(sum((r[key] - (r["winning_side"] == "YES"))**2 for r in sample)/len(sample),6) if sample else None
    return {"checked_at": now.isoformat(), "scope": "Actual timely Jev research publications; no orders or profit credit",
            "actual_completed_events": len(completed), "invalid_records": len(invalid),
            "invalid_details": invalid, "duplicate_event_ids": sorted(duplicate_ids),
            "abstained_events": sum(r["research_abstain"] for r in completed),
            "verified_resolved_events": sum(bool(r["winning_side"]) for r in completed),
            "pending_events": sum(not r["winning_side"] for r in completed),
            "scored_nonabstaining_events": len(scored), "strict_market_pair_events": len(paired),
            "brier_same_nonabstaining_market_events": {"jev": brier(paired,"candidate_p_up"),
                "parent": brier(paired,"parent_p_up"), "market": brier(paired,"market_mid")},
            "orders_submitted": 0, "production_database_written": False, "rows": valid,
            "limitations": ["Tiny short observation; no proof of forecasting calibration or alpha",
                "Abstentions are not executable forecasts or trade wins", "Gateway usage is recorded separately from the production model ledger"]}


def main():
    folders = [ROOT/".polycast"/name for name in
               ["jev-btc5m-shadow-20261003","jev-btc5m-shadow-v2-20261003"]]
    with connect_readonly(ROOT/".polycast/polycast.sqlite3") as connection:
        report = evaluate(folders, connection, utcnow())
    report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_report(ROOT/".polycast/jev-shadow-evaluation-20261003.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
