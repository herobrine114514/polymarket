from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from experiments.calibration_live_shadow import (EXPERIMENT, PACKAGES, PARAMETERS, ROOT,
    calibrate, connect_readonly, evaluate, fingerprint, observe, register, source_hashes,
    target_record, TARGET_SQL, write_once)
from polycast.research_shadow import shadow_status
from polycast.store import (database, save_analysis_draft, save_analysis_market_baseline,
                            save_market_baseline, save_prediction, upsert_markets)


def at(minutes=0, seconds=0):
    return datetime(2026, 9, 1, 12, minutes, seconds, tzinfo=timezone.utc)


class CalibrationLiveShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.db = self.data / "test.sqlite3"
        self.output = self.data / "research"
        self.manifest = {"experiment": EXPERIMENT, "registered_at": at().isoformat(),
            "database_identity": hashlib.sha256(str(self.db.resolve()).casefold().encode()).hexdigest(),
            "parameters": PARAMETERS, "packages": PACKAGES, "source_hashes": source_hashes(ROOT),
            "model_version": "deepseek-v4-pro", "prompt_version": "btc5m-v6-explicit-utc-volatility-units"}
        self.market = {"id": "1", "event_id": "event-1", "event_title": "BTC 5m",
            "event_slug": "btc-test", "question": "BTC up?", "description": "TWAP",
            "end_at": at(5).isoformat(), "yes_token": "up", "no_token": "down",
            "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
            "tick_size": .01, "fee_rate": 0, "category": "crypto5m"}
        with database(self.db) as conn:
            upsert_markets(conn, [self.market])
        self.package_patch = patch("experiments.calibration_live_shadow.version", side_effect=lambda name: PACKAGES[name])
        self.package_patch.start()
        self.addCleanup(self.package_patch.stop)

    def forecast(self, p=.7, key="first", abstain=False, approved=True, minute=1):
        stamp = at(minute).isoformat()
        payload = {"market_id": "1", "category": "crypto5m", "as_of": stamp,
                   "end_at": self.market["end_at"], "evidence": []}
        result = {"market_id": "1", "as_of": stamp, "p_yes": p, "abstain": abstain,
            "analysis_abstain": abstain, "model_version": self.manifest["model_version"],
            "prompt_version": self.manifest["prompt_version"], "reviewed_at": at(minute, 5).isoformat(),
            "review": {"approved": approved}}
        with database(self.db) as conn:
            save_analysis_draft(conn, "1", "rules", key, payload, result)
            conn.execute("UPDATE analysis_drafts SET created_at=? WHERE evidence_hash=?",
                         (at(minute, 2).isoformat(), key))
            prediction = save_prediction(conn, "1", "rules", key, {**result, "abstain": abstain or not approved})
            save_analysis_market_baseline(conn, "1", "rules", key, stamp, stamp, stamp, .4, .6)
            save_market_baseline(conn, prediction, stamp, stamp, .4, .6)
        return prediction

    def row(self):
        conn = connect_readonly(self.db)
        try:
            return dict(conn.execute(TARGET_SQL, (self.manifest["registered_at"],)).fetchone())
        finally:
            conn.close()

    def history(self):
        start = at() - timedelta(hours=5)
        return [{"event_id": str(i+100), "prediction_id": i+100,
            "model_version": self.manifest["model_version"], "prompt_version": self.manifest["prompt_version"],
            "analysis_abstain": False, "model": .3 if i%2 else .7, "market": .5, "outcome": i%2,
            "as_of": (start+timedelta(minutes=i*5)).isoformat(),
            "end_at": (start+timedelta(minutes=i*5+4)).isoformat(),
            "resolved_at": (start+timedelta(minutes=i*5+5)).isoformat(),
            "recorded_at": (start+timedelta(minutes=i*5+6)).isoformat()} for i in range(20)]

    def test_targets_reject_backfill_late_review_outcome_and_version_drift(self):
        self.forecast()
        row = self.row()
        self.assertIsNotNone(target_record(row, self.manifest, at(2)))
        for change in ({"winning_side": "YES"}, {"draft_created_at": at(6).isoformat()},
                       {"event_markets": 2}, {"as_of": (at()-timedelta(seconds=1)).isoformat()}):
            with self.subTest(change=change):
                self.assertIsNone(target_record({**row, **change}, self.manifest, at(2)))
        for change in ({"reviewed_at": at(5).isoformat()}, {"model_version": "deepseek-flash"},
                       {"prompt_version": "old"}, {"p_yes": .1}):
            output = json.loads(row["model_output_json"])
            output.update(change)
            self.assertIsNone(target_record({**row,"model_output_json":json.dumps(output)}, self.manifest, at(2)))
        self.assertIsNone(target_record(row, self.manifest, at(5)))

    def test_readonly_first_record_retains_rejection_and_never_replaced_by_later_good_result(self):
        first = self.forecast(approved=False)
        report = observe(self.db, self.output, self.manifest, clock=lambda:at(2))
        path = next((self.output/"events").glob("*.json"))
        saved = path.read_bytes()
        self.forecast(p=.9, key="later", minute=2)
        observe(self.db, self.output, self.manifest, clock=lambda:at(3))
        frozen = json.loads(saved)
        self.assertEqual(frozen["prediction_id"], first)
        self.assertFalse(frozen["review_approved"])
        self.assertEqual(report["status_counts"], {"insufficient_training":1})
        self.assertEqual(path.read_bytes(), saved)
        conn = connect_readonly(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0],0)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM predictions")
        finally:
            conn.close()

    def test_training_ignores_future_target_outcomes_and_keeps_embargo_versions_and_receipts(self):
        self.forecast()
        target = target_record(self.row(), self.manifest, at(2))
        training = self.history()
        excluded = copy.deepcopy(training[:5])
        for i,e in enumerate(excluded):
            e["event_id"] = "excluded"+str(i)
        excluded[0]["end_at"] = at().isoformat()
        excluded[1]["recorded_at"] = target["as_of"]
        excluded[2]["resolved_at"] = at(2).isoformat()
        excluded[3]["model_version"] = "other"
        excluded[4]["prompt_version"] = "other"
        seen=[]
        def fit(items, _):
            seen.extend(e["event_id"] for e in items)
            return {"slope":.8,"intercept":0}
        record = calibrate(target,training+excluded,PARAMETERS,fit)
        self.assertEqual(record["status"],"candidate")
        self.assertEqual(set(seen),{str(i+100) for i in range(20)})
        changed = calibrate({**target,"outcome":0},training+excluded,PARAMETERS,lambda *_:{"slope":.8,"intercept":0})
        self.assertEqual(record["calibrated"],changed["calibrated"])
        self.assertEqual(record["training_sha256"],changed["training_sha256"])
        self.assertNotIn("outcome",record)

    def test_fit_closes_read_snapshot_and_late_completion_cannot_publish(self):
        self.forecast()
        def fit(*_):
            conn = sqlite3.connect(self.db,timeout=.1)
            try:
                conn.execute("INSERT INTO evidence_withdrawals VALUES(?,?,?,?)",("test","test","test",at().isoformat()))
                conn.commit()
            finally:
                conn.close()
            return {"slope":.8,"intercept":0}
        with patch("experiments.calibration_live_shadow.build_report",return_value={"events":self.history()}):
            observe(self.db,self.output,self.manifest,clock=lambda:at(2),fit=fit)
        self.assertEqual(len(list((self.output/"events").glob("*.json"))),1)
        later_output = self.data/"late"
        times=iter([at(2),at(5),at(6)])
        with patch("experiments.calibration_live_shadow.build_report",return_value={"events":self.history()}):
            report=observe(self.db,later_output,self.manifest,clock=lambda:next(times),fit=lambda *_:{"slope":.8,"intercept":0})
        self.assertEqual(report["frozen_events"],0)

    def test_scoring_requires_early_publication_receipt_and_verified_later_result(self):
        self.forecast()
        report = observe(self.db,self.output,self.manifest,clock=lambda:at(2))
        self.assertEqual(report["pending_events"],1)
        with database(self.db) as conn:
            conn.execute("INSERT INTO resolutions VALUES(?,?,?,?,?)",("1","YES","https://example.test",at(6).isoformat(),at(7).isoformat()))
        report=observe(self.db,self.output,self.manifest,clock=lambda:at(8))
        self.assertEqual(report["resolved_raw_market"]["events"],1)
        self.assertEqual(report["resolved_raw_market"]["brier"],{"model":.09,"market":.25})
        self.assertEqual(report["resolved_calibrated"]["events"],0)
        path=next((self.output/"events").glob("*.json"))
        saved=json.loads(path.read_text())
        mh=fingerprint(self.manifest)
        resolutions={"1":{"winning_side":"YES","resolved_at":at(6).isoformat(),"recorded_at":at(7).isoformat()}}
        for published in (None,at(5).isoformat(),at(8).isoformat()):
            receipt=None if published is None else {"published_at":published,"record_sha256":saved["record_sha256"],"manifest_sha256":mh}
            result=evaluate([{**saved,"publication":receipt}],resolutions,mh,at(9))
            self.assertEqual(result["invalid_records"],1)
            self.assertEqual(result["resolved_raw"]["events"],0)

    def test_original_abstention_coverage_and_source_change_are_not_scored(self):
        self.forecast(abstain=True)
        report=observe(self.db,self.output,self.manifest,clock=lambda:at(2))
        self.assertEqual(report["status_counts"],{"abstained":1})
        self.assertEqual(report["resolved_raw"]["events"],0)
        changed={**self.manifest,"source_hashes":{}}
        with self.assertRaises(ValueError):
            observe(self.db,self.output,changed,clock=lambda:at(2))
        self.assertEqual(len(list((self.output/"events").glob("*.json"))),1)

    def test_manifest_and_records_publish_once(self):
        parent=self.data/"parent.json"
        parent.write_text(json.dumps({"experiment":"btc5m-logit-calibration-v1","parameters":PARAMETERS}))
        with patch("experiments.calibration_live_shadow.utcnow",return_value=at()):
            registered=register(self.db,self.output,parent)
            with self.assertRaises(FileExistsError):
                register(self.db,self.output,parent)
        self.assertEqual(registered["registered_at"],at().isoformat())
        target=self.data/"immutable.json"
        self.assertTrue(write_once(target,{"n":1}))
        self.assertFalse(write_once(target,{"n":2}))
        self.assertEqual(json.loads(target.read_text()),{"n":1})

    def test_after_write_publication_time_controls_credit_and_corrupt_or_duplicate_records_are_excluded(self):
        self.forecast()
        times=iter([at(2),at(2,1),at(2,2),at(5,1),at(6)])
        report=observe(self.db,self.output,self.manifest,clock=lambda:next(times))
        self.assertEqual(report["frozen_events"],1)
        self.assertEqual(report["invalid_records"],1)
        saved=json.loads(next((self.output/"events").glob("*.json")).read_text())
        mh=fingerprint(self.manifest)
        receipt={"published_at":at(2,3).isoformat(),"record_sha256":saved["record_sha256"],"manifest_sha256":mh}
        valid={**saved,"publication":receipt}
        result=evaluate([valid,copy.deepcopy(valid)],{},mh,at(3))
        self.assertEqual(result["invalid_records"],2)
        corrupt={**valid,"model":float('nan')}
        self.assertEqual(evaluate([corrupt],{},mh,at(3))["invalid_records"],1)

    def test_insufficient_class_coverage_duplicates_and_invalid_fit_cannot_generate_candidate(self):
        self.forecast()
        target=target_record(self.row(),self.manifest,at(2))
        history=self.history()
        for r in history:
            r["outcome"]=1
        self.assertEqual(calibrate(target,history,PARAMETERS)["status"],"insufficient_outcome_coverage")
        history=self.history()
        with self.assertRaises(ValueError):
            calibrate(target,history+[copy.deepcopy(history[0])],PARAMETERS)
        for slope in (0,-1,float('nan')):
            result=calibrate(target,history,PARAMETERS,lambda *_:{"slope":slope,"intercept":0})
            self.assertEqual(result["status"],"nonmonotonic_or_invalid_fit")
            self.assertIsNone(result["calibrated"])

    def test_parent_pipe_close_stops_actual_watch_process(self):
        self.output.mkdir()
        (self.output/"manifest.json").write_text('{}')
        process=subprocess.Popen([sys.executable,str(ROOT/"experiments/calibration_live_shadow.py"),
            "watch","--parent-pipe","--database",str(self.db),"--output",str(self.output)],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        try:
            deadline=time.monotonic()+8
            while not (self.output/"status.json").exists() and time.monotonic()<deadline:
                self.assertIsNone(process.poll())
                time.sleep(.05)
            self.assertTrue((self.output/"status.json").exists())
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5),0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()

    def test_real_native_fit_finishes_while_owner_pipe_remains_open(self):
        runtime = ROOT/".polycast/oss-calibration-venv"/("Scripts/python.exe" if sys.platform=='win32' else "bin/python")
        if not runtime.is_file():
            self.skipTest("Isolated calibration runtime is not installed")
        ready=self.data/"native-fit.json"
        code = """import json,sys,threading
from pathlib import Path
from experiments.calibration_live_shadow import start_parent_monitor, PARAMETERS
from experiments.calibration_walkforward import fit_sigmoid
stop=threading.Event()
start_parent_monitor(stop,sys.stdin.fileno())
training=[{'model':.2 if i%2 else .8,'outcome':i%2} for i in range(40)]
fitted=fit_sigmoid(training,PARAMETERS)
Path(sys.argv[1]).write_text(json.dumps(fitted))
stop.wait(30)
"""
        process=subprocess.Popen([str(runtime),'-c',code,str(ready)],cwd=ROOT,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        try:
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                self.assertIsNone(process.poll())
                time.sleep(.05)
            self.assertTrue(ready.exists(),"Native import/fit blocked with an open owner pipe")
            self.assertIsNone(process.poll())
            self.assertTrue(abs(json.loads(ready.read_text())["slope"])>0)
            # Pending bytes cannot hide a subsequent parent EOF.
            process.stdin.write(b'ownership-test')
            process.stdin.flush()
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5),0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()

    def test_public_status_whitelists_and_marks_stale_reports(self):
        directory=self.data/"calibration-prospective-v1"
        directory.mkdir()
        value={"experiment":EXPERIMENT,"status":"running","checked_at":at().isoformat(),
            "api_key":"hidden-value","source_hashes":{"private":"secret"},
            "status_counts":{"candidate":3,"unknown":"secret"},
            "resolved_calibrated_market":{"events":2,"brier":{"model":.2,"market":float('nan'),"calibrated":"secret"}}}
        (directory/"status.json").write_text(json.dumps(value))
        public=shadow_status(self.data)
        self.assertFalse(public["fresh"])
        self.assertNotIn("hidden-value",json.dumps(public))
        self.assertNotIn("secret",json.dumps(public))
        self.assertIsNone(public["resolved_calibrated_market"]["brier"]["market"])


if __name__ == "__main__":
    unittest.main()
