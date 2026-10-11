"""Finite, public-only connectivity probe. No analysis or order requests."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polycast.market import discover_btc_5m
from polycast.microstructure import selected_markets, public_collector_env

if __name__ == "__main__":
    directory = Path(sys.argv[1]).resolve()
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    directory.mkdir(parents=True, exist_ok=True)
    selection = selected_markets(discover_btc_5m(), datetime.now(timezone.utc))
    (directory / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
    env = public_collector_env()
    with (directory / "collector.err.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen([shutil.which("node"), str(Path(__file__).resolve().parents[1] /
            "polycast/microstructure_collector.mjs"), str(directory), str(directory / "selection.json"),
            str(seconds), str(128 * 1024 ** 2)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=log, env=env,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        try:
            process.wait(timeout=seconds + 20)
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=5)
    state = json.loads((directory / "state.json").read_text())
    print(json.dumps(state, ensure_ascii=False))
