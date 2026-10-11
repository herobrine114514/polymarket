"""Serve the dashboard shell on a spare port for UI work. No collectors, no auto analysis.

Reads the same local data directory as the real dashboard, so the page shows real state;
worker-dependent fields (collector freshness) are empty because no worker runs here.
The POST controls are live: they act on the real scheduler and settings. Look, don't click.
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from polycast.config import Config
from polycast.dashboard import make_server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = make_server(Config(), "127.0.0.1", args.port)
    print(f"preview on http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
