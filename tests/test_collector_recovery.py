from datetime import datetime,timedelta,timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch

from polycast.auto5m import AutoFiveMinute
from polycast.config import Config


class CollectorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker=AutoFiveMinute(Config(data_dir=Path(self.temp.name)))
        self.child=Mock()
        self.child.poll.return_value=None
        self.child.stdin.closed=False
        self.child.stdin.close.side_effect=lambda: setattr(self.child.stdin,'closed',True)
        self.worker._collector=self.child
        self.worker._collector_started_at=datetime.now(timezone.utc)-timedelta(seconds=70)

    def launch(self):
        return patch('polycast.auto5m.subprocess.Popen')

    def test_stale_live_collector_retires_pipe_and_reloads_current_proxy(self):
        with patch.object(self.worker,'_last_collector_tick_ms',return_value=1),self.launch() as popen,patch(
                'polycast.auto5m.shutil.which',return_value='node'),patch(
                'polycast.auto5m.node_collector_env',return_value={'HTTPS_PROXY':'http://example.invalid:1'}) as env:
            self.worker._ensure_collector()
            self.child.stdin.close.assert_called_once()
            self.child.wait.assert_called_once_with(timeout=2)
            self.child.terminate.assert_not_called()
            env.assert_called_once()
            self.assertEqual(popen.call_args.kwargs['env']['HTTPS_PROXY'],'http://example.invalid:1')
            self.assertEqual(self.worker._state['collector_recoveries'],1)

    def test_startup_grace_and_fresh_tick_do_not_restart_live_process(self):
        self.worker._collector_started_at=datetime.now(timezone.utc)-timedelta(seconds=20)
        with patch.object(self.worker,'_last_collector_tick_ms',return_value=None),self.launch() as popen:
            self.worker._ensure_collector();popen.assert_not_called();self.child.wait.assert_not_called()
        self.worker._collector_started_at=datetime.now(timezone.utc)-timedelta(seconds=70)
        tick=int(datetime.now(timezone.utc).timestamp()*1000)
        with patch.object(self.worker,'_last_collector_tick_ms',return_value=tick),self.launch() as popen:
            self.worker._ensure_collector();popen.assert_not_called();self.child.wait.assert_not_called()

    def test_data_arriving_during_recheck_prevents_restart(self):
        tick=int(datetime.now(timezone.utc).timestamp()*1000)
        with patch.object(self.worker,'_last_collector_tick_ms',side_effect=[1,tick]),self.launch() as popen:
            self.worker._ensure_collector();popen.assert_not_called();self.child.wait.assert_not_called()

    def test_process_that_wont_exit_never_creates_second_collector(self):
        self.child.wait.side_effect=subprocess.TimeoutExpired('collector',2)
        with patch.object(self.worker,'_last_collector_tick_ms',return_value=None),self.launch() as popen:
            self.worker._ensure_collector();popen.assert_not_called()
        self.child.terminate.assert_called_once()
        self.assertEqual(self.worker._state['collector_recoveries'],0)


if __name__=='__main__':unittest.main()
