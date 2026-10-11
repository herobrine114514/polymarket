from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polycast.config import Config
from polycast.runner import run_recorded
from polycast.store import database, now_iso


class RunnerModelAttributionTests(unittest.TestCase):
    def test_configured_model_and_actual_failed_or_mixed_calls_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            with database(config.database) as conn:
                conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,
                    question,description,yes_token,no_token,liquidity,volume_24h,
                    min_order_size,tick_size,fee_rate,discovered_at,updated_at)
                    VALUES('1','e','event','event','Up?','rules','yes','no',
                           10000,1000,5,.01,0,?,?)""", (now_iso(), now_iso()))

            def operation(versions: tuple[str, ...], fail: bool = False) -> dict:
                with database(config.database) as conn:
                    for version in versions:
                        conn.execute("""INSERT INTO model_runs
                            (market_id,stage,model_version,started_at,status)
                            VALUES('1','analysis',?,?,'failed')""", (version, now_iso()))
                if fail:
                    raise RuntimeError("model unavailable")
                return {}

            with patch("polycast.runner.active_provider", return_value="deepseek"), patch(
                    "polycast.runner.DEEPSEEK_MODEL", "deepseek-v4-pro"):
                run_recorded(config, lambda: operation(()))
                # Completion follows observed calls even if the configured
                # provider differed at start or a failed call ended the cycle.
                with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                    run_recorded(config, lambda: operation(("actual-codex",), fail=True))
                run_recorded(config, lambda: operation(("deepseek-v4-pro", "actual-codex")))
                run_recorded(config, lambda: operation(("deepseek-v4-pro", "deepseek-v4-pro")))

            with database(config.database) as conn:
                rows = [tuple(row) for row in conn.execute(
                    "SELECT model_version,model_calls,status FROM cycle_runs ORDER BY id")]
            self.assertEqual(rows, [
                ("deepseek-v4-pro", 0, "succeeded"),
                ("actual-codex", 1, "failed"),
                ("mixed", 2, "succeeded"),
                ("deepseek-v4-pro", 2, "succeeded"),
            ])
