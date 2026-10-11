"""Identify and reuse the dashboard owned by this data directory."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

from .config import Config


def database_identity(config: Config) -> str:
    path = str(config.database.resolve())
    return hashlib.sha256(os.path.normcase(path).encode()).hexdigest()


def running_dashboard(config: Config) -> dict | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=2) as response:
            value = json.loads(response.read(4096))
        if (value.get("service") == "polycast" and value.get("mode") == "paper-only"
                and value.get("database_identity") == database_identity(config)
                and type(value.get("pid")) is int and value["pid"] > 0):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def reuse_registered_dashboard(config: Config) -> bool:
    if running_dashboard(config):
        return True
    if os.name != "nt":
        return False
    marker = config.data_dir / "realtime-task.json"
    try:
        registration = json.loads(marker.read_text(encoding="utf-8-sig"))
        if (registration.get("task_name") != "PolycastRealtime" or
                Path(registration["workspace"]).resolve() != config.data_dir.resolve().parent):
            return False
        from .scheduler_control import manage_task
        manage_task("run", target="realtime")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if running_dashboard(config):
            return True
        time.sleep(.25)
    return False
