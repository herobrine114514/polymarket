"""Synthetic failure scenarios for the isolated execution research journal."""
import concurrent.futures
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from experiments.execution_replay import ExecutionReplay, ReplayError, ReconcilePending


def reservation(intent="intent-1", **changes):
    return {"intent_id": intent, "expected_order_id": "synthetic-order-" + intent,
            "risk_group": "btc-window-1", "condition_id": "synthetic-condition", "token_id": "12345",
            "order_type": "FAK", "shares": "10", "limit_price": "0.5",
            "maximum_fee_micro": 200_000, "reserve_micro": 5_200_000, **changes}


def trade(status="CONFIRMED", size="10", ident="trade-1", **changes):
    return {"id": ident, "takerOrderId": "synthetic-order-intent-1", "market": "synthetic-condition",
            "tokenId": "12345", "side": "BUY", "traderSide": "TAKER", "size": size,
            "price": "0.48", "status": status, "fee_paid_micro": 100_000, **changes}


def reconciliation(replay, trades, **changes):
    from decimal import Decimal
    size = sum((Decimal(t["size"]) for t in trades), Decimal(0))
    confirmed = [t for t in trades if t["status"].removeprefix("TRADE_STATUS_") == "CONFIRMED"]
    filled = sum((Decimal(t["size"]) for t in confirmed), Decimal(0))
    spent = sum(int(Decimal(t["size"]) * Decimal(t["price"]) * 1_000_000) + t["fee_paid_micro"] for t in confirmed)
    intent = replay.snapshot()["intents"]["intent-1"]
    return {"intent_id": "intent-1", "observed_seq": replay.snapshot()["seq"], "complete": True,
            "captured_at": 1700000000, "checked_at": 1700000001,
            "order": {"id": "synthetic-order-intent-1", "assetId": "12345",
                      "conditionId": "synthetic-condition", "side": "BUY", "originalSize": "10",
                      "price": "0.5", "orderType": intent["order_type"], "status": "CANCELED" if size < 10 else "MATCHED",
                      "associateTrades": [t["id"] for t in trades], "sizeMatched": str(size)},
            "trades": copy.deepcopy(trades), "open_order_ids": [],
            "cash_micro": 50_000_000 - spent, "positions": {"12345": str(filled)} if filled else {}, **changes}


class ExecutionReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polycast-execution-replay-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "replay.sqlite3"
        self.replay = ExecutionReplay.create(self.path)
        self.replay.command("reserve-1", "reserve", reservation())

    def submitted(self, ack="matched"):
        self.replay.command("begin-1", "begin_submit", {"intent_id": "intent-1"})
        if ack:
            self.replay.command("ack-1", "ack", {"intent_id": "intent-1",
                "order_id": "synthetic-order-intent-1", "status": ack})

    def assertHeld(self, phase=None):
        state = self.replay.snapshot()
        self.assertEqual(state["risk"]["held_micro"], 5_200_000)
        self.assertEqual(state["risk"]["position_cost_micro"], 0)
        self.assertTrue(state["risk"]["halted"])
        if phase:
            self.assertEqual(state["intents"]["intent-1"]["phase"], phase)

    def test_process_crash_after_submit_barrier_retains_hold_and_prevents_retry(self):
        code = ("import os;from pathlib import Path;from experiments.execution_replay import ExecutionReplay;"
                "r=ExecutionReplay.open(Path(os.environ['REPLAY_TEST_DB']));"
                "r.command('begin-crash','begin_submit',{'intent_id':'intent-1'});os._exit(23)")
        env = {"REPLAY_TEST_DB": str(self.path), **{k:v for k,v in os.environ.items()
                if k.lower() in {"systemroot", "windir", "path", "temp", "tmp"}}}
        child = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            env=env, capture_output=True, timeout=10, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(child.returncode, 23, child.stderr.decode(errors="replace"))
        self.replay = ExecutionReplay.open(self.path)
        self.assertHeld("SUBMITTING")
        with self.assertRaisesRegex(ReplayError, "Never automatically resubmit"):
            self.replay.command("retry", "begin_submit", {"intent_id": "intent-1"})

    def test_timeout_404_auth_failure_and_local_expiry_cannot_free_external_funds(self):
        self.submitted(ack=None)
        for i, (kind,reason) in enumerate([("timeout",None), ("observation_error","http_404"),
                                        ("observation_error","http_401")]):
            payload = {"intent_id": "intent-1"}
            if reason:
                payload["reason"] = reason
            self.replay.command(f"unknown-{i}", kind, payload)
            self.replay = ExecutionReplay.open(self.path)
            self.assertHeld("UNKNOWN")
        with self.assertRaisesRegex(ReplayError, "Local timeout"):
            self.replay.command("expire", "local_expire", {"intent_id": "intent-1"})
        with self.assertRaisesRegex(ReplayError, "unresolved"):
            self.replay.command("new-order", "reserve", reservation("intent-2", risk_group="other"))

    def test_ack_matched_mined_and_retrying_do_not_book_final_position(self):
        self.submitted()
        for i, status in enumerate(["MATCHED_NOT_BROADCASTED", "MATCHED", "MINED", "RETRYING", "CONFIRMED"]):
            self.replay.command(f"trade-{i}", "trade", {"intent_id": "intent-1", "trade": trade(status)})
            self.assertHeld("ACKNOWLEDGED")

    def test_duplicate_and_reordered_trade_after_reconciliation_do_not_double_count(self):
        self.submitted()
        payload = {"intent_id": "intent-1", "trade": trade()}
        self.replay.command("confirmed", "trade", payload)
        self.assertTrue(self.replay.command("confirmed", "trade", payload)["duplicate"])
        self.replay.command("complete", "reconcile", reconciliation(self.replay, [trade()]))
        self.replay = ExecutionReplay.open(self.path)
        self.replay.command("late-mined", "trade", {"intent_id": "intent-1", "trade": trade("MINED",size="10.0",price="0.480000")})
        self.replay.command("late-confirmed", "trade", payload)
        state = self.replay.snapshot()
        self.assertEqual(state["risk"]["held_micro"], 0)
        self.assertEqual(state["risk"]["position_cost_micro"], 4_900_000)
        self.assertFalse(state["risk"]["halted"])
        self.assertEqual(len(state["intents"]["intent-1"]["trades"]), 1)

    def test_cancel_and_partial_fill_race_releases_only_after_complete_account_reconcile(self):
        self.submitted("live")
        self.replay.command("cancel", "cancel_request", {"intent_id": "intent-1"})
        partial = trade(size="4", fee_paid_micro=40_000)
        self.replay.command("fill-after-cancel", "trade", {"intent_id": "intent-1", "trade": partial})
        self.assertHeld()
        self.replay.command("cancel-reconciled", "reconcile", reconciliation(self.replay, [partial]))
        state = self.replay.snapshot()
        self.assertEqual(state["risk"]["held_micro"], 0)
        self.assertEqual(state["risk"]["position_cost_micro"], 1_960_000)
        self.assertFalse(state["risk"]["halted"])
        self.assertEqual(state["intents"]["intent-1"]["phase"], "DONE")

    def test_websocket_disconnect_rest_can_recover_unseen_confirmed_trade(self):
        self.submitted()
        self.replay.command("disconnect", "stream_gap", {})
        self.replay = ExecutionReplay.open(self.path)
        self.assertHeld()
        self.replay.command("rest-recovery", "reconcile", reconciliation(self.replay, [trade()]))
        self.assertEqual(self.replay.snapshot()["risk"]["position_cost_micro"], 4_900_000)
        self.assertFalse(self.replay.snapshot()["stream_gap"])

    def test_incomplete_pages_and_pending_finality_retain_hold_and_allow_fresh_retry(self):
        self.submitted()
        for i, snapshot in enumerate([reconciliation(self.replay,[trade()],complete=False),
                                    reconciliation(self.replay,[trade("MINED")])]):
            snapshot["observed_seq"] = self.replay.snapshot()["seq"]
            with self.assertRaises(ReconcilePending):
                self.replay.command(f"incomplete-{i}", "reconcile", snapshot)
            self.assertHeld("UNKNOWN")
        self.replay.command("fresh-full", "reconcile", reconciliation(self.replay, [trade()]))
        self.assertEqual(self.replay.snapshot()["risk"]["held_micro"], 0)

    def test_fok_unexpected_partial_confirmation_halts_and_keeps_original_hold(self):
        self.replay.command("expire-local", "local_expire", {"intent_id": "intent-1"})
        self.replay.command("reserve-fok", "reserve", reservation(order_type="FOK", intent_id="fok",expected_order_id="synthetic-fok"))
        self.replay.command("begin-fok", "begin_submit", {"intent_id": "fok"})
        partial = trade(size="4",takerOrderId="synthetic-fok",fee_paid_micro=40_000)
        snapshot = reconciliation(self.replay, [trade(size="4",fee_paid_micro=40_000)])
        snapshot["intent_id"] = "fok"
        snapshot["order"]["id"] = "synthetic-fok"
        snapshot["order"]["orderType"] = "FOK"
        snapshot["trades"] = [partial]
        with self.assertRaisesRegex(ReplayError, "FOK partial"):
            self.replay.command("partial-fok", "reconcile", snapshot)
        state = ExecutionReplay.open(self.path).snapshot()
        self.assertEqual(state["intents"]["fok"]["phase"], "FAULT")
        self.assertEqual(state["risk"]["held_micro"], 5_200_000)

    def test_failed_trade_release_requires_unchanged_cash_and_position_confirmation(self):
        self.submitted()
        failed = trade("FAILED",fee_paid_micro=0)
        self.replay.command("failed", "trade", {"intent_id": "intent-1", "trade": failed})
        self.assertHeld()
        self.replay.command("failure-reconciled", "reconcile", reconciliation(self.replay, [failed]))
        state = self.replay.snapshot()
        self.assertEqual(state["risk"]["held_micro"], 0)
        self.assertEqual(state["risk"]["position_cost_micro"], 0)

    def test_balance_or_position_mismatch_halts_without_credit(self):
        self.submitted()
        with self.assertRaisesRegex(ReplayError, "cash does not reconcile"):
            self.replay.command("wrong-balance", "reconcile", reconciliation(self.replay,[trade()],cash_micro=50_000_000))
        self.assertHeld("FAULT")
        with self.assertRaisesRegex(ReplayError, "manual recovery"):
            self.replay.command("later-correct", "reconcile", reconciliation(self.replay,[trade()]))

    def test_foreign_position_or_unreconciled_open_order_prevents_budget_release(self):
        self.submitted()
        with self.assertRaises(ReconcilePending):
            self.replay.command("open-foreign", "reconcile", reconciliation(self.replay,[trade()],open_order_ids=["unknown-order"]))
        self.assertHeld("UNKNOWN")
        with self.assertRaisesRegex(ReplayError, "positions do not reconcile"):
            self.replay.command("foreign-position", "reconcile", reconciliation(self.replay,[trade()],positions={"12345":"10","unknown":"1"}))
        self.assertHeld("FAULT")

    def test_missing_associated_trade_page_defers_then_full_snapshot_recovers(self):
        self.submitted()
        full = reconciliation(self.replay,[trade()])
        partial = copy.deepcopy(full)
        partial["trades"] = []
        with self.assertRaisesRegex(ReconcilePending,"associated trade page"):
            self.replay.command("missing-page", "reconcile", partial)
        self.assertHeld("UNKNOWN")
        self.replay.command("complete-page", "reconcile", reconciliation(self.replay,[trade()]))
        self.assertEqual(self.replay.snapshot()["risk"]["position_cost_micro"],4_900_000)

    def test_token_limit_and_conflicting_finality_are_persistent_faults(self):
        self.submitted()
        self.replay.command("confirmed", "trade", {"intent_id": "intent-1", "trade": trade()})
        with self.assertRaisesRegex(ReplayError, "Conflicting terminal"):
            self.replay.command("contradictory", "trade", {"intent_id": "intent-1", "trade": trade("FAILED")})
        self.replay = ExecutionReplay.open(self.path)
        self.assertHeld("FAULT")
        for i, changed in enumerate([trade(tokenId="wrong"), trade(price="0.51")]):
            with self.assertRaises(ReplayError):
                self.replay.command(f"invalid-{i}", "trade", {"intent_id": "intent-1", "trade": changed})
            self.assertHeld("FAULT")

    def test_snapshot_invalidated_by_new_journal_event_is_not_applied(self):
        self.submitted()
        snapshot = reconciliation(self.replay,[trade()])
        self.replay.command("cancel-after-snapshot", "cancel_request", {"intent_id": "intent-1"})
        with self.assertRaisesRegex(ReplayError,"predates journal"):
            self.replay.command("stale", "reconcile", snapshot)
        self.assertHeld()
        self.replay.command("refetch", "reconcile", reconciliation(self.replay,[trade()]))
        self.assertEqual(self.replay.snapshot()["risk"]["held_micro"], 0)

    def test_stale_snapshot_does_not_release_and_can_be_retried(self):
        self.submitted()
        with self.assertRaises(ReconcilePending):
            self.replay.command("old-rest", "reconcile", reconciliation(self.replay,[trade()],checked_at=1700000031))
        self.assertHeld("UNKNOWN")
        self.replay.command("new-rest", "reconcile", reconciliation(self.replay,[trade()]))
        self.assertEqual(self.replay.snapshot()["risk"]["held_micro"], 0)

    def test_event_identity_reuse_cannot_change_a_reserved_order(self):
        original = reservation()
        self.assertTrue(self.replay.command("reserve-1", "reserve", original)["duplicate"])
        with self.assertRaisesRegex(ReplayError,"identity reused"):
            self.replay.command("reserve-1", "reserve", reservation(shares="9"))
        self.assertEqual(self.replay.snapshot()["risk"]["held_micro"], 5_200_000)

    def test_concurrent_reservations_atomically_share_event_group_budget(self):
        self.replay.command("expire", "local_expire", {"intent_id": "intent-1"})
        barrier = threading.Barrier(2)
        def reserve(index):
            peer = ExecutionReplay.open(self.path)
            barrier.wait(timeout=5)
            try:
                peer.command(f"concurrent-{index}", "reserve", reservation(f"peer-{index}"))
                return "reserved"
            except ReplayError as error:
                return str(error)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve,range(2)))
        self.assertEqual(results.count("reserved"), 1)
        self.assertIn("Event group limit exceeded", results)
        self.assertEqual(self.replay.snapshot()["risk"]["held_micro"], 5_200_000)

    def test_production_database_is_forbidden(self):
        with self.assertRaises(ReplayError):
            ExecutionReplay.open(Path(__file__).resolve().parents[1]/".polycast"/"polycast.sqlite3")


if __name__ == "__main__":
    unittest.main()
