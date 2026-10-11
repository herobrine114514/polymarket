"""Optional isolated research worker owned by the windowless dashboard task."""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .market import parse_utc


class ResearchShadow:
    def __init__(self, workspace: Path, data_dir: Path, database: Path):
        self.workspace, self.data_dir, self.database = workspace, data_dir, database
        self._stop = threading.Event()
        self._process = None
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._watch, daemon=True,
                                            name="polycast-research-shadow")
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._process and self._process.poll() is None:
            if self._process.stdin:
                self._process.stdin.close()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.terminate()

    def _watch(self):
        interpreter = self.data_dir / "oss-calibration-venv" / "Scripts" / "python.exe"
        if os.name != "nt":
            interpreter = self.data_dir / "oss-calibration-venv" / "bin" / "python"
        script = self.workspace / "experiments" / "calibration_live_shadow.py"
        output = self.data_dir / "calibration-prospective-v1"
        while not self._stop.is_set():
            if (output / "closed.json").is_file():
                return  # Preserve the registered study after production changed.
            if (interpreter.is_file() and script.is_file() and (output / "manifest.json").is_file()
                    and (not self._process or self._process.poll() is not None)):
                try:
                    if self._process and self._process.stdin:
                        self._process.stdin.close()
                    with (output / "worker.log").open("a", encoding="utf-8") as log:
                        self._process = subprocess.Popen(
                            [str(interpreter), str(script), "watch", "--parent-pipe",
                             "--database", str(self.database), "--output", str(output)],
                            cwd=self.workspace, stdin=subprocess.PIPE, stdout=log, stderr=log,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                except OSError:
                    pass  # Research failure does not take down the dashboard.
            self._stop.wait(10)


def shadow_status(data_dir: Path) -> dict:
    """Expose a fixed public report only; no paths, logs or manifest contents."""
    if (data_dir / "calibration-prospective-v1/closed.json").is_file():
        return {"status": "closed", "fresh": False,
                "reason": "Registered production changed; original research records preserved"}
    try:
        value = json.loads((data_dir / "calibration-prospective-v1/status.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("experiment") != "btc5m-prospective-calibration-v1":
            raise ValueError("Unknown research report")
        at = parse_utc(value.get("checked_at"))
        fresh = bool(at and 0 <= (datetime.now(timezone.utc) - at).total_seconds() <= 35)
        status = value.get("status")
        result = {"status": status if status in ("running", "error") else "unknown",
                  "fresh": fresh, "checked_at": at.isoformat() if at else None}
        registered = parse_utc(value.get("registered_at"))
        result["registered_at"] = registered.isoformat() if registered else None
        result["model_version"] = "deepseek-v4-pro" if value.get("model_version") == "deepseek-v4-pro" else None
        result["prompt_version"] = ("btc5m-v6-explicit-utc-volatility-units"
                                    if value.get("prompt_version") == "btc5m-v6-explicit-utc-volatility-units" else None)
        def count(v):
            return v if type(v) is int and 0 <= v <= 10**9 else 0
        for key in ("training_minimum", "embargo_minutes", "frozen_events", "invalid_records",
                    "malformed_records", "pending_events"):
            result[key] = count(value.get(key))
        for key, names in (("status_counts", ("candidate", "abstained", "insufficient_training",
                            "insufficient_outcome_coverage", "nonmonotonic_or_invalid_fit", "fit_failed")),
                           ("coverage", ("forecast_events_after_registration",
                            "forecast_events_without_record", "ended_without_record"))):
            original = value.get(key) if isinstance(value.get(key), dict) else {}
            result[key] = {name: count(original.get(name)) for name in names}
        for key in ("resolved_raw", "resolved_raw_market", "resolved_calibrated", "resolved_calibrated_market"):
            original = value.get(key) if isinstance(value.get(key), dict) else {}
            brier = original.get("brier") if isinstance(original.get("brier"), dict) else {}
            result[key] = {"events": count(original.get("events")), "brier": {}}
            for name in ("model", "calibrated", "market"):
                v = brier.get(name)
                result[key]["brier"][name] = (v if type(v) in (int, float) and
                                              math.isfinite(v) and 0 <= v <= 1 else None)
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "fresh": False}
