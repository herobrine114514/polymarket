"""Windowless Scheduled Task entry point with a persistent diagnostic log."""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

workspace = Path(__file__).resolve().parent
log_dir = workspace / ".polycast"
log_dir.mkdir(parents=True, exist_ok=True)
with (log_dir / "task.log").open("a", encoding="utf-8", buffering=1) as log:
    sys.stdout = log
    sys.stderr = log
    try:
        from polycast.cli import main

        print(f"\n[{datetime.now(timezone.utc).isoformat()}] scheduled cycle started")
        code = main(["cycle"])
        print(f"[{datetime.now(timezone.utc).isoformat()}] exit {code}")
    except BaseException:
        traceback.print_exc()
        code = 1
    raise SystemExit(code)
