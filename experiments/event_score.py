"""Finite, read-only official daily-result check for a frozen engineering pilot.

Each invocation publishes to a new directory and performs at most two public
JSON requests. No model, order, DB write, old forecast rewrite or repeat loop.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polycast.event_research import INPUT_VERSION, instant, publish_once, read_packet, sha
from polycast.event_scoring import VERSION, score_pilot, verify_daily_resolution


def read_pilot(packet_path: Path, pilot_dir: Path) -> tuple[dict, list[dict]]:
    wrapper = read_packet(packet_path)
    packet = wrapper["packet"]
    if packet.get("input_version") != INPUT_VERSION or packet["category"] != "daily":
        raise ValueError("Only new unconditional daily pilots can be scored")
    start = json.loads((pilot_dir/"started.json").read_text(encoding="utf-8"))
    bundle = json.loads((pilot_dir/"source-bundle.json").read_text(encoding="utf-8"))
    if (start.get("packet_sha256") != wrapper["packet_sha256"] or
            start.get("packet_file_sha256") != hashlib.sha256(packet_path.read_bytes()).hexdigest() or
            start.get("input_version") != INPUT_VERSION or start.get("orders_enabled") is not False or
            not instant(packet["as_of"]) <= instant(start["started_at"]) < instant(packet["end_at"]) or
            bundle.get("bundle_sha256") != sha({k:v for k,v in bundle.items() if k != "bundle_sha256"}) or
            bundle.get("bundle_sha256") != start.get("source_bundle_sha256")):
        raise ValueError("Pilot start/source bundle does not match the frozen input")
    if not bundle.get("files") or set(bundle["files"]) != set(start.get("source_hashes", {})):
        raise ValueError("Pilot source file set differs")
    for name, item in bundle["files"].items():
        blob = base64.b64decode(item["content_base64"], validate=True)
        if hashlib.sha256(blob).hexdigest() != item.get("sha256") or item["sha256"] != start["source_hashes"][name]:
            raise ValueError("Pilot source archive hash mismatch")
    receipts = []
    for style in ("control", "structured"):
        receipt = json.loads((pilot_dir/(style+".json")).read_text(encoding="utf-8"))
        if receipt.get("style") != style:
            raise ValueError("Pilot receipt style differs")
        receipts.append(receipt)
    return wrapper, receipts


def evaluate(packet_path: Path, pilot_dir: Path, output: Path, *, fetcher=None) -> dict:
    wrapper, receipts = read_pilot(packet_path, pilot_dir)
    packet = wrapper["packet"]
    contract = packet["contract_snapshot"][0]
    slug, game_id = contract["event_slug"], contract["source_ref"]
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug) or not game_id.isdigit():
        raise ValueError("Public result identity is invalid")
    urls = {"gamma": "https://gamma-api.polymarket.com/events/slug/"+slug,
            "mlb": "https://statsapi.mlb.com/api/v1/schedule?"+urlencode({"gamePk":game_id,"hydrate":"team"})}
    now = datetime.now(timezone.utc)
    root = Path(__file__).resolve().parents[1]
    source_names = ("polycast/event_research.py", "polycast/event_scoring.py", "experiments/event_score.py")
    start = {"schema_version":1,"scoring_version":VERSION,"started_at":now.isoformat(),
             "packet_sha256":wrapper["packet_sha256"],"max_public_requests":2,"urls":urls,
             "source_hashes":{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in source_names},
             "orders_enabled":False,"model_calls":0}
    publish_once(output/"started.json", start)
    observed = now.isoformat()
    requests, bodies, errors = [], {}, []
    if now > instant(packet["end_at"]):
        if fetcher is None:
            from polycast.net import fetch_json
            fetcher = fetch_json
        for source, url in urls.items():
            sent = datetime.now(timezone.utc).isoformat()
            try:
                body = fetcher(url)
                received = datetime.now(timezone.utc).isoformat()
                if not isinstance(body, dict):
                    raise ValueError("Public result must be a JSON object")
                bodies[source] = body
                record = {"source":source,"url":url,"request_started_at":sent,"received_at":received,
                          "body":body,"body_sha256":sha(body)}
                requests.append(record)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                errors.append({"source":source,"error_type":type(exc).__name__})
        observed = datetime.now(timezone.utc).isoformat()
    publish_once(output/"public-responses.json", {"requests":requests,"errors":errors,"observed_at":observed})
    if len(bodies) == 2:
        official = verify_daily_resolution(packet, bodies["gamma"], bodies["mlb"], observed)
    else:
        official = {"status":"pending","scoring_version":VERSION,"event_id":packet["event_id"],
                    "observed_at":observed,"contract_sha256":sha(packet["contract_snapshot"]),
                    "reason":"event_not_ended" if now <= instant(packet["end_at"]) else "public_result_unavailable"}
    result = score_pilot(wrapper, receipts, official)
    result.update(public_requests=len(requests)+len(errors), model_calls=0, orders_submitted=0)
    publish_once(output/"scores.json", result)
    return {"status":"official_daily_pilot_scored" if official["status"]=="verified" else "official_daily_result_pending",
            "event_id":packet["event_id"],"official_state":official.get("official_state"),
            "official_reason":official["reason"],"scored_styles":list(result["scores"]),
            "exclusions":result["exclusions"],"public_requests":result["public_requests"],
            "model_calls":0,"orders_submitted":0,"live_ready":False}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--pilot-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    print(json.dumps(evaluate(args.packet,args.pilot_dir,args.output),ensure_ascii=False))

