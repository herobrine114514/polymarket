"""Captured official-SDK fixture -> durable journal fault and restart checks."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from experiments.execution_replay import ExecutionReplay, ReplayError, ReconcilePending
from experiments.sdk_receipt_bridge import (acknowledge_sdk, digest, fixture_bundle,
                                           fixture_reservation, reconcile_sdk)


class SdkReceiptBridgeTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads((Path(__file__).parent / "fixtures/sdk-authenticated-0.12.0.json").read_text())
        self.temp = tempfile.TemporaryDirectory(prefix="polycast-sdk-receipt-")
        self.addCleanup(self.temp.cleanup)
        self.bundle = fixture_bundle(self.report)
        self.reservation = fixture_reservation(self.report)
        self.replay = self.fresh("initial")

    def fresh(self, label):
        replay = ExecutionReplay.create(Path(self.temp.name) / (label + ".sqlite3"))
        replay.command("reserve", "reserve", self.reservation)
        replay.command("begin", "begin_submit", {"intent_id": "fixture-intent"})
        acknowledge_sdk(replay, "ack", "fixture-intent", self.report["ack"])
        return replay

    def reconcile(self, bundle=None, *, event="observe", **changes):
        return reconcile_sdk(self.replay, event, "fixture-intent", bundle or self.bundle,
                             **{"observed_seq": self.replay.snapshot()["seq"], "captured_at": 1700000000,
                                "checked_at": 1700000001, **changes})

    def held(self, phase="UNKNOWN"):
        self.replay = ExecutionReplay.open(self.replay.path)
        state = self.replay.snapshot()
        self.assertEqual(state["intents"]["fixture-intent"]["phase"], phase)
        self.assertEqual(state["risk"]["held_micro"], 2_687_360)
        self.assertEqual(state["risk"]["position_cost_micro"], 0)
        self.assertTrue(state["risk"]["halted"])
        self.assertEqual(state["intents"]["fixture-intent"]["submit_attempts"], 1)

    def test_actual_sdk_aliases_status_and_pages_reconcile_once_across_restart(self):
        seq = self.replay.snapshot()["seq"]
        result = self.reconcile()
        self.assertEqual(result["risk"]["held_micro"], 0)
        self.assertEqual(result["risk"]["position_cost_micro"], 2_687_350)
        self.assertEqual(result["intents"]["fixture-intent"]["trades"]["fixture-trade"]["status"], "CONFIRMED")
        self.replay = ExecutionReplay.open(self.replay.path)
        result = self.reconcile(observed_seq=seq)
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["risk"]["halted"])
        self.assertEqual(result["risk"]["position_cost_micro"], 2_687_350)

    def test_ack_alone_never_releases_funds_or_permits_second_submission(self):
        self.held("ACKNOWLEDGED")
        with self.assertRaisesRegex(ReplayError, "Never automatically resubmit"):
            self.replay.command("retry", "begin_submit", {"intent_id": "fixture-intent"})
        self.held("ACKNOWLEDGED")

    def test_missing_paid_fee_not_filled_from_rate_or_untrusted_sdk_extra_field(self):
        self.bundle["account"]["fee_receipts"] = {}
        self.bundle["sdk_normalized"]["account_trade_pages"][0]["items"][0]["fee_paid_micro"] = 0
        with self.assertRaisesRegex(ReconcilePending, "Measured fee receipt missing"):
            self.reconcile()
        self.held()
        with self.replay.connect() as db:
            event = json.loads(db.execute("SELECT event_json FROM journal ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(event["payload"]["observation_sha256"], digest(self.bundle))
        self.assertIn("feeRateBps", event["payload"]["reason"])
        self.bundle = fixture_bundle(self.report)
        self.assertEqual(self.reconcile(event="fresh-fee")["risk"]["held_micro"], 0)

    def test_missing_reordered_or_filtered_pages_keep_entire_hold(self):
        variants = []
        missing = copy.deepcopy(self.bundle)
        missing["sdk_normalized"]["account_trade_pages"].pop()
        variants.append(missing)
        reordered = copy.deepcopy(self.bundle)
        reordered["sdk_normalized"]["account_trade_pages"].reverse()
        variants.append(reordered)
        filtered = copy.deepcopy(self.bundle)
        next(r for r in filtered["requests"] if r["path"] == "/data/trades")["query"] = {"market": "hidden"}
        variants.append(filtered)
        for index, bundle in enumerate(variants):
            with self.subTest(index=index):
                self.replay = self.fresh(f"pages-{index}")
                with self.assertRaises(ReconcilePending):
                    self.reconcile(bundle)
                self.held()

    def test_missing_cursor_or_nonboolean_terminal_marker_is_not_complete(self):
        for index, (field, value) in enumerate([("nextCursor", None), ("hasMore", 1)]):
            with self.subTest(field=field):
                self.replay = self.fresh(f"cursor-{index}")
                bundle = copy.deepcopy(self.bundle)
                bundle["sdk_normalized"]["account_trade_pages"][0][field] = value
                with self.assertRaises(ReconcilePending):
                    self.reconcile(bundle)
                self.held()

    def test_conflicting_asset_alias_and_foreign_funder_are_not_admitted(self):
        for index, (field, value) in enumerate([("tokenId", "foreign-token"), ("makerAddress", "0x" + "66" * 20)]):
            with self.subTest(field=field):
                self.replay = self.fresh(f"identity-{index}")
                bundle = copy.deepcopy(self.bundle)
                bundle["sdk_normalized"]["order"][field] = value
                with self.assertRaises(ReconcilePending):
                    self.reconcile(bundle)
                self.held()

    def test_order_identity_mismatch_persists_fault_across_restart(self):
        self.bundle["sdk_normalized"]["order"]["id"] = "foreign-order"
        with self.assertRaisesRegex(ReplayError, "REST order identity mismatch"):
            self.reconcile()
        self.held("FAULT")

    def test_duplicate_trade_and_association_receipts_do_not_double_credit(self):
        for index, collection in enumerate(["trade", "association"]):
            with self.subTest(collection=collection):
                self.replay = self.fresh(f"duplicate-{index}")
                bundle = copy.deepcopy(self.bundle)
                if collection == "trade":
                    items = bundle["sdk_normalized"]["account_trade_pages"][0]["items"]
                    items.append(copy.deepcopy(items[0]))
                else:
                    bundle["sdk_normalized"]["order"]["associateTrades"].append("fixture-trade")
                with self.assertRaises(ReconcilePending):
                    self.reconcile(bundle)
                self.held()

    def test_fee_receipt_transaction_binding_and_frozen_fee_cap_are_required(self):
        for index, (field, value) in enumerate([("transaction_hash", "0x" + "66" * 32),
                                              ("fee_paid_micro", 87361), ("fee_paid_micro", True)]):
            with self.subTest(index=index):
                self.replay = self.fresh(f"fees-{index}")
                bundle = copy.deepcopy(self.bundle)
                bundle["account"]["fee_receipts"]["fixture-trade"][field] = value
                with self.assertRaises(ReconcilePending):
                    self.reconcile(bundle)
                self.held()

    def test_incomplete_finality_defers_then_complete_receipt_recovers(self):
        self.bundle["sdk_normalized"]["account_trade_pages"][0]["items"][0]["status"] = "TRADE_STATUS_MINED"
        self.bundle["account"]["fee_receipts"]["fixture-trade"]["status"] = "MINED"
        with self.assertRaisesRegex(ReconcilePending, "finality"):
            self.reconcile()
        self.held()
        self.bundle = fixture_bundle(self.report)
        self.assertEqual(self.reconcile(event="confirmed")["risk"]["held_micro"], 0)

    def test_wrong_cash_or_foreign_positions_persist_fault_without_releasing_hold(self):
        for index, field in enumerate(["cash_micro", "positions"]):
            with self.subTest(field=field):
                self.replay = self.fresh(f"account-{index}")
                bundle = copy.deepcopy(self.bundle)
                if field == "cash_micro":
                    bundle["account"][field] += 1
                else:
                    bundle["account"][field]["foreign-token"] = "1"
                with self.assertRaises(ReplayError):
                    self.reconcile(bundle)
                self.held("FAULT")

    def test_stale_clock_and_journal_sequence_never_release_funds(self):
        with self.assertRaisesRegex(ReconcilePending, "Stale or future"):
            self.reconcile(checked_at=1700000031)
        self.held()
        seq = self.replay.snapshot()["seq"]
        self.replay.command("cancel", "cancel_request", {"intent_id": "fixture-intent"})
        with self.assertRaisesRegex(ReplayError, "predates journal"):
            self.reconcile(event="stale-journal", observed_seq=seq)
        self.held()

    def test_ack_for_wrong_order_is_a_persistent_fault(self):
        ack = {**self.report["ack"], "orderId": "foreign-order"}
        with self.assertRaisesRegex(ReplayError, "Acknowledgement order identity mismatch"):
            acknowledge_sdk(self.replay, "foreign-ack", "fixture-intent", ack)
        self.held("FAULT")

    def test_live_scope_is_rejected_and_cannot_be_mistaken_for_account_gate_pass(self):
        report = copy.deepcopy(self.report)
        report["real_credentials_used"] = True
        with self.assertRaisesRegex(ReplayError, "Real account observations"):
            fixture_bundle(report)
        bundle = copy.deepcopy(self.bundle)
        bundle["scope"] = "live"
        with self.assertRaisesRegex(ReconcilePending, "scope/version"):
            self.reconcile(bundle)
        self.held()

    def test_malformed_position_snapshot_is_durably_deferred(self):
        self.bundle["account"]["positions"] = []
        with self.assertRaisesRegex(ReconcilePending, "Malformed account position"):
            self.reconcile()
        self.held()


if __name__ == "__main__":
    unittest.main()
