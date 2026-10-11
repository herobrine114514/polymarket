import hashlib
import json
import unittest
from datetime import timedelta

from experiments.calibration_live_shadow import fingerprint
from experiments.jev_btc5m_shadow import PARAMETERS, QUESTIONS
from experiments.jev_gateway_probe import ENDPOINT, MODEL, request_body, utcnow
from experiments.jev_shadow_evaluation import frozen_state, validate_record


class JevEvaluationTests(unittest.TestCase):
    def fixture(self):
        now = utcnow()
        as_of, end = now-timedelta(seconds=5), now+timedelta(minutes=3)
        payload = {"market_id":"fixture", "as_of":as_of.isoformat(), "end_at":end.isoformat(),
                   "question":"fixture", "resolution_rules":"fixture", "window_timing_utc":{},
                   "gamma_crypto_config":{}, "evidence": [{"id":1,"summary":"public",
                       "published_at":(as_of-timedelta(seconds=1)).isoformat(),
                       "first_seen_at":(as_of-timedelta(seconds=1)).isoformat()}]}
        encoded=json.dumps(payload)
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        manifest={"registered_at":(as_of-timedelta(seconds=1)).isoformat(), "candidate_model":MODEL,
                  "endpoint":ENDPOINT, "questions":QUESTIONS, "parameters":PARAMETERS}
        answers={"up":.6,"unclear_direction":.8,"unverified_opening_reference":.96}
        record={"requested_model":MODEL,"manifest_sha256":fingerprint(manifest),"status":"completed",
                "production_database_written":False,"orders_submitted":0,"independent_review_run":False,
                "started_at":(now-timedelta(seconds=3)).isoformat(),"completed_at":(now-timedelta(seconds=2)).isoformat(),
                "finished_before_event_end":True,"parent_payload_sha256":digest,"candidate_p_up":.6,
                "answers":answers,"research_abstain":True,
                "request_sha256":fingerprint(request_body(frozen_state(payload),QUESTIONS)),
                "baseline":{"market_id":"fixture","as_of":as_of.isoformat(),"end_at":end.isoformat(),
                    "reviewed_at":(now-timedelta(seconds=4)).isoformat(),"payload_sha256":digest,"analysis_abstain":False}}
        record["record_sha256"]=fingerprint(record)
        receipt={"record_sha256":record["record_sha256"],"manifest_sha256":fingerprint(manifest),
                 "published_at":(now-timedelta(seconds=1)).isoformat()}
        return record,receipt,manifest,encoded

    def rehash(self,record,receipt):
        clone=dict(record);clone.pop("record_sha256")
        record["record_sha256"]=fingerprint(clone)
        receipt["record_sha256"]=record["record_sha256"]

    def test_valid_publication_and_input_provenance(self):
        args=self.fixture()
        self.assertEqual(validate_record(*args)["market_id"],"fixture")

    def test_record_tampering_is_rejected(self):
        record,receipt,manifest,payload=self.fixture()
        record["candidate_p_up"]=.9
        with self.assertRaises(ValueError):validate_record(record,receipt,manifest,payload)

    def test_rehashed_abstention_or_probability_mismatch_is_rejected(self):
        for key,value in [("candidate_p_up",.9),("research_abstain",False)]:
            record,receipt,manifest,payload=self.fixture()
            record[key]=value;self.rehash(record,receipt)
            with self.assertRaises(ValueError):validate_record(record,receipt,manifest,payload)

    def test_late_or_stale_receipt_is_rejected(self):
        for offset in [31,200]:
            record,receipt,manifest,payload=self.fixture()
            from polycast.market import parse_utc
            receipt["published_at"]=(parse_utc(record["baseline"]["as_of"])+timedelta(seconds=offset)).isoformat()
            with self.assertRaises(ValueError):validate_record(record,receipt,manifest,payload)

    def test_original_input_cannot_be_replaced_after_publication(self):
        record,receipt,manifest,payload=self.fixture()
        changed=json.loads(payload);changed["evidence"][0]["summary"]="rewritten"
        with self.assertRaises(ValueError):validate_record(record,receipt,manifest,json.dumps(changed))

    def test_threshold_cannot_be_changed_after_observing_results(self):
        record,receipt,manifest,payload=self.fixture()
        manifest["parameters"]={**PARAMETERS,"abstain_threshold":.99}
        record["manifest_sha256"]=fingerprint(manifest);receipt["manifest_sha256"]=fingerprint(manifest)
        self.rehash(record,receipt)
        with self.assertRaises(ValueError):validate_record(record,receipt,manifest,payload)


if __name__=="__main__":unittest.main()
