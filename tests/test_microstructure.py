from __future__ import annotations
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from polycast.config import Config
from polycast.microstructure import MicrostructureCapture, selected_markets, public_collector_env
from polycast.microstructure_archive import ArchiveFailure


class MicrostructureTests(unittest.TestCase):
    def test_public_child_does_not_inherit_private_credentials(self):
        with patch("polycast.microstructure.node_collector_env", return_value={
            "PATH": "node", "HTTPS_PROXY": "http://local-proxy", "DEEPSEEK_API_KEY": "private",
            "POLYMARKET_PRIVATE_KEY": "wallet", "NODE_OPTIONS": "--import untrusted.mjs"}):
            env = public_collector_env()
        self.assertEqual(env, {"PATH": "node", "HTTPS_PROXY": "http://local-proxy", "POLYCAST_PARENT_PIPE": "1"})

    def test_only_rule_matched_directional_five_minute_assets_are_selected(self):
        now = datetime(2026, 10, 6, 0, 0, 0, tzinfo=timezone.utc)
        market = {"id": "1", "event_id": "2", "category": "crypto5m", "yes_label": "Up", "no_label": "Down",
                  "source_ref": f"BTCUSD:{int(now.timestamp())}", "end_at": "2026-10-06T00:05:00Z",
                  "yes_token": "100", "no_token": "200", "description": "matching rules"}
        selection = selected_markets([market, {**market, "id": "3", "category": "crypto"},
                                      {**market, "id": "4", "no_token": "100"}], now)
        self.assertEqual(len(selection["markets"]), 1)
        self.assertEqual(selection["markets"][0]["up_asset"], "100")
        self.assertEqual(selection["markets"][0]["rules"], "matching rules")

    def test_saved_state_requires_current_live_process_and_fresh_time(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = MicrostructureCapture(Config(data_dir=Path(directory)))
            owner.directory.mkdir()
            owner.process = Mock(pid=100)
            owner.process.poll.return_value = None
            path = owner.directory / "state.json"
            path.write_text(json.dumps({"pid": 99, "status": "running", "updated_at_ms": int(time.time() * 1000)}))
            self.assertFalse(owner.state()["state_fresh"])
            path.write_text(json.dumps({"pid": 100, "status": "running", "updated_at_ms": int(time.time() * 1000) - 1000}))
            self.assertTrue(owner.state()["state_fresh"])
            owner.process.poll.return_value = 0
            self.assertFalse(owner.state()["alive"])
            self.assertFalse(owner.state()["state_fresh"])

    def test_storage_error_does_not_restart_forever_or_erase_data(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = MicrostructureCapture(Config(data_dir=Path(directory)))
            owner.directory.mkdir()
            (owner.directory / "state.json").write_text('{"status":"storage_limit"}')
            with patch("polycast.microstructure.subprocess.Popen") as launch:
                owner._ensure()
            launch.assert_not_called()
            self.assertIn("retained", owner.state()["last_error"])

    def test_capacity_restart_requires_verified_space_recovered_by_this_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = MicrostructureCapture(Config(data_dir=Path(directory)))
            owner.directory.mkdir()
            path = owner.directory / "state.json"
            path.write_text('{"status":"storage_limit"}')
            with patch("polycast.microstructure.subprocess.Popen") as launch, patch("polycast.microstructure.shutil.which", return_value="node"):
                owner._ensure()
                launch.assert_not_called()
                owner._archive_progress = True
                owner._ensure()
                launch.assert_called_once()
                self.assertFalse(owner._archive_progress)
            owner.process = None
            path.write_text('{"status":"storage_error"}')
            owner._archive_progress = True
            with patch("polycast.microstructure.subprocess.Popen") as launch:
                owner._ensure()
            launch.assert_not_called()

    def test_failed_archive_is_quarantined_with_original_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = MicrostructureCapture(Config(data_dir=Path(directory)))
            owner.directory.mkdir()
            name = "raw-closed-00000.jsonl"
            original = owner.directory / name
            original.write_bytes(b"original data\n")
            with patch("polycast.microstructure.archive_one", side_effect=ArchiveFailure(name, "hash mismatch")):
                owner._archive()
            self.assertEqual(original.read_bytes(), b"original data\n")
            self.assertIn(name, owner._bad_archives)
            self.assertFalse(owner._archive_progress)
            marker = json.loads((owner.directory / (name + ".archive-error.json")).read_text(encoding="utf-8"))
            self.assertTrue(marker["original_retained"])

    def test_clock_failure_replaces_old_success_instead_of_reusing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = MicrostructureCapture(Config(data_dir=Path(directory)))
            owner._clock_state = {"status": "venues_agree", "offset_ms": -3000}
            with patch("polycast.microstructure.observe_public_clock", side_effect=ValueError("disagree")):
                owner._check_clock()
            self.assertEqual(owner.state()["public_clock"]["status"], "unverified")
            clock = json.loads((owner.directory / "clock.json").read_text(encoding="utf-8"))
            self.assertNotIn("offset_ms", clock)


if __name__ == "__main__":
    unittest.main()
