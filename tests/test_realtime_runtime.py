from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

from polycast.config import Config
from polycast.dashboard import make_server
from polycast.dashboard_state import invalidate_task_cache, scheduled_task
from polycast.runtime import database_identity, reuse_registered_dashboard, running_dashboard
from polycast.scheduler_control import manage_task


class RealtimeRuntimeTests(unittest.TestCase):
    def test_health_identifies_owner_without_state_queries_or_returning_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            server = make_server(config, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                before = config.database.read_bytes()
                with patch("polycast.dashboard.snapshot") as snapshot:
                    with urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
                        value = json.load(response)
                    snapshot.assert_not_called()
                self.assertEqual(value, {"service": "polycast", "mode": "paper-only",
                                         "pid": os.getpid(), "database_identity": database_identity(config)})
                self.assertEqual(config.database.read_bytes(), before)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_foreign_database_service_mode_or_invalid_pid_cannot_be_reused(self):
        config = Config()
        good = {"service": "polycast", "mode": "paper-only", "pid": 10,
                "database_identity": database_identity(config)}
        variants = [good, {**good, "database_identity": "another-data-directory"},
                    {**good, "service": "other-app"}, {**good, "mode": "live"},
                    {**good, "pid": True}, {**good, "pid": 0}]
        for index, value in enumerate(variants):
            with self.subTest(index=index), patch("polycast.runtime.urllib.request.urlopen",
                    return_value=nullcontext(Mock(read=Mock(return_value=json.dumps(value).encode())))):
                self.assertEqual(running_dashboard(config) is not None, index == 0)
        with patch("polycast.runtime.running_dashboard", return_value=good), patch(
                "polycast.scheduler_control.manage_task") as task:
            self.assertTrue(reuse_registered_dashboard(config))
            task.assert_not_called()

    def test_realtime_controls_reject_arbitrary_tasks_and_interval_changes(self):
        with patch("polycast.scheduler_control.os.name", "nt"), patch(
                "polycast.scheduler_control.subprocess.run", return_value=Mock(returncode=0)) as shell:
            for target in ("arbitrary-task", "realtime'; Remove-Item C:\\*; '"):
                with self.assertRaises(ValueError):
                    manage_task("run", target=target)
            with self.assertRaises(ValueError):
                manage_task("interval", 15, target="realtime")
            shell.assert_not_called()
            manage_task("run", target="realtime")
            self.assertIn("Start-ScheduledTask -TaskName 'PolycastRealtime'", shell.call_args.args[0][-1])
            self.assertEqual(shell.call_args.kwargs["creationflags"], 0x08000000)

    def test_scheduler_caches_each_known_task_independently(self):
        invalidate_task_cache()
        self.addCleanup(invalidate_task_cache)
        returns = [Mock(returncode=0, stdout=json.dumps({"available": True, "state": "Ready"})),
                   Mock(returncode=0, stdout=json.dumps({"available": True, "state": "Running"}))]
        with patch("polycast.dashboard_state.os.name", "nt"), patch(
                "polycast.dashboard_state.subprocess.run", side_effect=returns) as shell:
            self.assertEqual(scheduled_task()["state"], "Ready")
            self.assertEqual(scheduled_task("realtime")["state"], "Running")
            self.assertEqual(scheduled_task()["state"], "Ready")
            self.assertEqual(shell.call_count, 2)
