"""Windowless, Task Scheduler-owned dashboard and BTC five-minute collector."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

workspace = Path(__file__).resolve().parent
os.chdir(workspace)
os.environ.setdefault("POLYCAST_DATA_DIR", str(workspace / ".polycast"))
data_dir = Path(os.environ["POLYCAST_DATA_DIR"])
data_dir.mkdir(parents=True, exist_ok=True)
with (data_dir / "realtime.log").open("a", encoding="utf-8", buffering=1) as log:
    sys.stdout = log
    sys.stderr = log
    try:
        from polycast.config import Config
        from polycast.dashboard import serve
        from polycast.runtime import running_dashboard
        config = Config()
        print(f"[{datetime.now(timezone.utc).isoformat()}] realtime dashboard started, PID {os.getpid()}")
        if running_dashboard(config):
            print("Existing dashboard for this database is already running")
        else:
            from polycast.research_shadow import ResearchShadow
            research = ResearchShadow(workspace, config.data_dir, config.database)
            research.start()
            try:
                serve(config, "127.0.0.1", 8765)
            finally:
                research.stop()
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1)
