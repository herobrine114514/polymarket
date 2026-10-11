"""PyInstaller entry point. Double-click opens the local read-only dashboard."""

from __future__ import annotations

import sys
import threading
import webbrowser
import os
from pathlib import Path

if __name__ == "__main__":
    if getattr(sys, "frozen", False) and "POLYCAST_DATA_DIR" not in os.environ:
        executable_dir = Path(sys.executable).resolve().parent
        app_dir = (executable_dir.parent if executable_dir.name.lower() == "dist" and
                   (executable_dir.parent / ".polycast").is_dir() else executable_dir)
        os.environ["POLYCAST_DATA_DIR"] = str(app_dir / ".polycast")
    from polycast.cli import main
    if len(sys.argv) == 1:
        from polycast.config import Config
        from polycast.runtime import reuse_registered_dashboard
        if reuse_registered_dashboard(Config()):
            webbrowser.open("http://127.0.0.1:8765/")
            raise SystemExit(0)
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8765/")).start()
        raise SystemExit(main(["serve"]))
    raise SystemExit(main())
