from __future__ import annotations
import copy
import hashlib
import json
import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polycast.event_research import (INPUT_VERSION, CONDITIONAL_INPUT_VERSION, DEDUP_VERSION,
    forecast_target, freeze_packet, read_packet, sha, audit_daily_schedule)
from polycast.event_scoring import (checked_probabilities, expected_payouts, score_distribution,
    verify_daily_resolution, score_pilot)
from experiments.event_score import evaluate


def packet_fixture(*, fractional=True, ended=True):
    now = datetime.now(timezone.utc)
    end = now-timedelta(days=1) if ended else now+timedelta(days=1)
    cutoff = end-timedelta(hours=2) if ended else now-timedelta(seconds=10)
    rules = "Away vs Home; final winner pays 1, loser 0."
    if fractional:
        rules += " Canceled with no reschedule or tied games resolve 50-50."
    contract = {"id":"123","event_id":"456","event_slug":"mlb-away-home-2026-10-05",
        "question":"Away vs Home","description":rules,"end_at":end.isoformat(),
        "yes_token":"100","no_token":"200","yes_label":"Away","no_label":"Home",
        "source_ref":"987","category":"daily","event_title":"Away vs Home"}
    options=[{"key":"YES","label":"Away"},{"key":"NO","label":"Home"}]
    if fractional:
        options.append({"key":"HALF","label":"Official 50-50 settlement"})
    packet={"schema_version":1,"input_version":INPUT_VERSION,"dedup_version":DEDUP_VERSION,
        "event_id":"456","market_ids":["123"],"category":"daily",
        "kind":"settlement_multiple_choice" if fractional else "binary","as_of":cutoff.isoformat(),
        "end_at":end.isoformat(),"event_title":"Away vs Home","resolution_rules":rules,
        "options":options,"forecast_target":forecast_target("daily",rules),
        "contract_snapshot":[contract],"evidence_groups":[{"members":[{"evidence":{"id":1}}]}],
        "market_prices_included":False}
    gamma={"id":"456","slug":contract["event_slug"],"markets":[{
        **{k:contract[k] for k in ("id","question","description")},
        "clobTokenIds":["100","200"],"outcomes":["Away","Home"],
        "resolutionSource":"https://www.mlb.com/","gameStartTime":end.isoformat(),
        "closed":True,"umaResolutionStatus":"resolved","closedTime":(end+timedelta(hours=1)).isoformat(),
        "outcomePrices":["1","0"]}]}
    mlb={"dates":[{"games":[{"gamePk":987,"gameDate":end.isoformat(),
        "status":{"abstractGameState":"Final"},"teams":{
            "away":{"team":{"name":"Away"},"score":4},
            "home":{"team":{"name":"Home"},"score":2}}}]}]}
    return packet,gamma,mlb,now


def receipts_fixture(packet):
    probabilities={"YES":.6,"NO":.3,"HALF":.1} if packet["kind"]!="binary" else {"YES":.6,"NO":.4}
    base={"event_id":packet["event_id"],"as_of":packet["as_of"],"abstain":False,
          "probabilities":probabilities,"supporting_evidence_ids":[1],"counter_evidence_ids":[],"reasoning":"Synthetic test forecast"}
    structured={**base,"resolution_conditions":["Fixture rules"],"base_rate_reasoning":"Synthetic prior",
                "necessary_factors":[],"missing_information":[],"update_triggers":[]}
    result=[]
    for style in ("control","structured"):
        output=structured if style=="structured" else base
        if style=="control" and packet["kind"]=="binary":
            output={"market_id":"123","as_of":packet["as_of"],"abstain":False,"p_yes":.6}
        result.append({"style":style,"status":"published_research_forecast","packet_sha256":sha(packet),
            "as_of":packet["as_of"],"started_at":(datetime.fromisoformat(packet["as_of"])+timedelta(seconds=1)).isoformat(),
            "published_at":(datetime.fromisoformat(packet["as_of"])+timedelta(seconds=2)).isoformat(),
            "abstain":False,"result":output})
    return result


class DailySettlementScoringTests(unittest.TestCase):
    def setUp(self):
        self.packet,self.gamma,self.mlb,self.now=packet_fixture()

    def proof(self):
        return verify_daily_resolution(self.packet,self.gamma,self.mlb,self.now.isoformat())

    def test_expected_payout_and_full_distribution_score_differ(self):
        p={"YES":.6,"NO":.3,"HALF":.1}
        self.assertEqual(expected_payouts(self.packet,p),{"YES":.65,"NO":.35})
        score=score_distribution(self.packet,p,"HALF")
        self.assertAlmostEqual(score["multiclass_brier_sum"],1.26)
        self.assertAlmostEqual(score["yes_payout_squared_error"],.0225)
        self.assertEqual(score["realized_payouts"],{"YES":.5,"NO":.5})
        self.assertFalse(score["probabilities_clipped_or_normalized"])

    def test_invalid_or_partial_probabilities_are_not_normalized(self):
        for p in ({"YES":.6,"NO":.4}, {"YES":.6,"NO":.3,"HALF":.2},
                  {"YES":True,"NO":0,"HALF":0}, {"YES":float("nan"),"NO":.3,"HALF":.1}):
            with self.assertRaises(ValueError):checked_probabilities(self.packet,p)

    def test_zero_assigned_probability_is_infinite_loss_not_dropped(self):
        score=score_distribution(self.packet,{"YES":.6,"NO":.4,"HALF":0},"HALF")
        self.assertIsNone(score["log_loss"])
        self.assertEqual(score["log_loss_status"],"positive_infinity")
        self.assertEqual(score["probabilities"]["HALF"],0)
        self.assertGreater(score["multiclass_brier_sum"],1)

    def test_rule_clock_conflict_is_explicit_and_cannot_be_scored_as_verified(self):
        packet=copy.deepcopy(self.packet)
        packet['end_at']='2026-10-07T01:30:00+00:00'
        packet['resolution_rules']='Game scheduled for October 6 at 3:33AM ET; canceled entirely resolves 50-50.'
        audit=audit_daily_schedule(packet)
        self.assertEqual(audit['status'],'conflict')
        self.assertEqual(audit['rule_start_utc'],'2026-10-06T07:33:00+00:00')
        self.assertEqual(audit['rule_minus_contract_seconds'],-64620)
        proof=verify_daily_resolution(packet,self.gamma,self.mlb,'2026-10-08T01:30:00+00:00')
        self.assertEqual(proof['status'],'pending')
        self.assertEqual(proof['reason'],'contract_rule_time_requires_clarification')
        packet['resolution_rules']='Game scheduled for October 6 at 9:30PM ET; canceled entirely resolves 50-50.'
        self.assertEqual(audit_daily_schedule(packet)['status'],'matches')

    def test_ambiguous_and_nonexistent_dst_times_are_not_silently_chosen(self):
        packet=copy.deepcopy(self.packet)
        packet['end_at']='2026-11-01T07:00:00+00:00'
        packet['resolution_rules']='Game scheduled for November 1, 2026 at 1:30AM ET.'
        self.assertEqual(audit_daily_schedule(packet)['status'],'ambiguous_rule_schedule')
        packet['end_at']='2026-03-08T08:00:00+00:00'
        packet['resolution_rules']='Game scheduled for March 8, 2026 at 2:30AM ET.'
        self.assertEqual(audit_daily_schedule(packet)['status'],'invalid_rule_schedule')

    def test_normal_final_game_requires_matching_terminal_oracle_status(self):
        proof=self.proof()
        self.assertEqual(proof["status"],"verified")
        self.assertEqual(proof["official_state"],"YES")
        self.assertFalse(proof["onchain_payout_verified"])
        self.gamma["markets"][0]["umaResolutionStatus"]="proposed"
        self.assertEqual(self.proof()["status"],"pending")
        self.gamma["markets"][0]["umaResolutionStatus"]="resolved"
        self.gamma["markets"][0]["closedTime"]=self.now.isoformat()
        self.assertEqual(self.proof()["status"],"pending")

    def test_official_half_tie_is_retained_but_cancellation_is_not_assumed(self):
        self.gamma["markets"][0]["outcomePrices"]=["0.5","0.5"]
        self.mlb["dates"][0]["games"][0]["teams"]["home"]["score"]=4
        self.assertEqual(self.proof()["official_state"],"HALF")
        self.mlb["dates"][0]["games"][0]["status"]["abstractGameState"]="Cancelled"
        proof=self.proof()
        self.assertEqual(proof["status"],"pending")
        self.assertIn("reschedule",proof["reason"])
        self.mlb["dates"][0]["games"][0]["status"]={"abstractGameState":"Final","detailedState":"Cancelled"}
        self.assertEqual(self.proof()["status"],"pending")

    def test_half_last_price_or_unfinal_game_does_not_establish_half(self):
        self.gamma["markets"][0]["outcomePrices"]=["0.5","0.5"]
        self.mlb["dates"][0]["games"][0]["status"]["abstractGameState"]="Live"
        self.assertEqual(self.proof()["status"],"pending")

    def test_contract_identity_clocks_and_independent_result_must_agree(self):
        changes=[
            lambda g,m:g["markets"][0].update(description="Changed rules"),
            lambda g,m:g["markets"][0].update(clobTokenIds=["200","100"]),
            lambda g,m:g["markets"][0].update(outcomes=["Home","Away"]),
            lambda g,m:m["dates"][0]["games"][0].update(gamePk=999),
            lambda g,m:m["dates"][0]["games"][0].update(gameDate=self.now.isoformat()),
            lambda g,m:m["dates"][0]["games"][0]["teams"]["home"].update(score=5),
        ]
        for change in changes:
            gamma,mlb=copy.deepcopy(self.gamma),copy.deepcopy(self.mlb)
            change(gamma,mlb)
            self.assertEqual(verify_daily_resolution(self.packet,gamma,mlb,self.now.isoformat())["status"],"pending")

    def test_binary_contract_cannot_accept_half_state(self):
        packet,gamma,mlb,now=packet_fixture(fractional=False)
        gamma["markets"][0]["outcomePrices"]=["0.5","0.5"]
        mlb["dates"][0]["games"][0]["teams"]["home"]["score"]=4
        self.assertEqual(verify_daily_resolution(packet,gamma,mlb,now.isoformat())["status"],"pending")

    def test_paired_scores_compare_only_expected_payout_to_single_market_price(self):
        receipts=receipts_fixture(self.packet)
        wrapper={"packet":self.packet,"market_baselines":{"123":{"status":"captured",
            "p_yes_mid":.45,"received_at":self.packet["as_of"],"book_timestamp":self.packet["as_of"]}}}
        result=score_pilot(wrapper,receipts,self.proof())
        self.assertTrue(result["paired_schema_valid_scores"])
        self.assertFalse(result["paired_model_selection_eligible"])
        self.assertFalse(result["market_baseline"]["distribution_comparison_eligible"])
        self.assertIsNone(result["market_baseline"]["multiclass_brier_sum"])
        self.assertIsNone(result["net_profit"])

    def test_late_abstained_or_mismatched_forecasts_remain_in_exclusions(self):
        receipts=receipts_fixture(self.packet)
        receipts[0]["result"]["abstain"]=True
        receipts[1]["published_at"]=self.packet["end_at"]
        result=score_pilot({"packet":self.packet},receipts,self.proof())
        self.assertEqual(result["scores"],{})
        self.assertEqual(set(result["exclusions"]),{"control","structured"})
        with self.assertRaises(ValueError):score_pilot({"packet":self.packet},[receipts[0],receipts[0]],self.proof())

    def test_conditional_v2_stays_auditable_but_cannot_be_rescored_as_ternary(self):
        packet=copy.deepcopy(self.packet)
        packet.update(input_version=CONDITIONAL_INPUT_VERSION,kind="binary")
        packet["options"]=packet["options"][:2]
        packet["forecast_target"]=forecast_target("daily",packet["resolution_rules"],version=CONDITIONAL_INPUT_VERSION)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"v2.json"
            freeze_packet(path,packet)
            self.assertEqual(read_packet(path)["packet"]["forecast_target"]["conditioning"],packet["forecast_target"]["conditioning"])
        with self.assertRaises(ValueError):expected_payouts(packet,{"YES":.6,"NO":.4})
        with self.assertRaises(ValueError):score_pilot({"packet":packet},receipts_fixture(packet),self.proof())

    def make_pilot(self,directory,packet):
        directory=Path(directory);input_path=directory/"input.json";pilot=directory/"pilot"
        wrapper=freeze_packet(input_path,packet)
        files={"fixture.py":{"sha256":hashlib.sha256(b"synthetic").hexdigest(),
                             "content_base64":base64.b64encode(b"synthetic").decode()}}
        bundle={"schema_version":1,"files":files};bundle["bundle_sha256"]=sha(bundle)
        pilot.mkdir()
        start={"packet_sha256":wrapper["packet_sha256"],
               "packet_file_sha256":hashlib.sha256(input_path.read_bytes()).hexdigest(),
               "input_version":INPUT_VERSION,"orders_enabled":False,"started_at":packet["as_of"],
               "source_bundle_sha256":bundle["bundle_sha256"],"source_hashes":{k:v["sha256"] for k,v in files.items()}}
        for name,value in [("started.json",start),("source-bundle.json",bundle)]+[(r["style"]+".json",r) for r in receipts_fixture(packet)]:
            (pilot/name).write_text(json.dumps(value),encoding="utf-8")
        return input_path,pilot

    def test_finite_readonly_evaluator_archives_two_responses_and_blocks_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path,pilot=self.make_pilot(directory,self.packet)
            requests=[]
            def fetch(url):
                requests.append(url)
                return self.gamma if "gamma-api" in url else self.mlb
            output=Path(directory)/"scores"
            result=evaluate(input_path,pilot,output,fetcher=fetch)
            self.assertEqual(result["scored_styles"],["control","structured"])
            self.assertEqual(len(requests),2)
            saved=json.loads((output/"public-responses.json").read_text())
            for r in saved["requests"]:self.assertEqual(r["body_sha256"],sha(r["body"]))
            with self.assertRaises(FileExistsError):evaluate(input_path,pilot,output,fetcher=fetch)
            self.assertEqual(len(requests),2)
            self.assertEqual(result["orders_submitted"],0)

    def test_future_event_does_not_fetch_result_or_drop_pending_attempts(self):
        packet,_,_,_=packet_fixture(ended=False)
        with tempfile.TemporaryDirectory() as directory:
            input_path,pilot=self.make_pilot(directory,packet)
            def fetch(url):raise AssertionError("Future event must not fetch official outcomes")
            result=evaluate(input_path,pilot,Path(directory)/"scores",fetcher=fetch)
            self.assertEqual(result["public_requests"],0)
            self.assertEqual(len(result["exclusions"]),2)
            self.assertEqual(result["official_reason"],"event_not_ended")


if __name__=="__main__":unittest.main()

