"""Windowless, dashboard-owned public collection in an isolated research directory."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .collector_env import node_collector_env
from .config import Config
from .market import discover_btc_5m, parse_utc
from .microstructure_archive import ArchiveFailure, archive_one, publish_once, contained_file
from .public_clock import observe_public_clock

CAPACITY_BYTES = 8 * 1024 ** 3


def public_collector_env() -> dict[str, str]:
    # Feed subscriptions are public. Never pass wallet, model or CLOB secrets.
    permitted = {"path", "systemroot", "windir", "comspec", "temp", "tmp", "localappdata",
                 "https_proxy", "http_proxy", "all_proxy", "no_proxy", "node_use_env_proxy",
                 "node_use_system_ca", "node_extra_ca_certs"}
    env = {key: value for key, value in node_collector_env().items() if key.lower() in permitted}
    env["POLYCAST_PARENT_PIPE"] = "1"
    return env


def selected_markets(markets: list[dict], now: datetime) -> dict:
    selected = []
    for market in markets:
        if market.get("category") != "crypto5m" or market.get("yes_label") != "Up" or market.get("no_label") != "Down":
            continue
        try:
            start = int(market["source_ref"].split(":")[1]) * 1000
            end = int(parse_utc(market["end_at"]).timestamp() * 1000)
            up, down = str(market["yes_token"]), str(market["no_token"])
            identifier = str(market["id"])
            if (end - start != 300000 or not identifier.isdigit() or not up.isdigit() or
                    not down.isdigit() or up == down or end <= int(now.timestamp() * 1000)):
                continue
            selected.append({"market_id": identifier, "event_id": str(market["event_id"]),
                             "start_at_ms": start, "end_at_ms": end, "up_asset": up, "down_asset": down,
                             "source_ref": market["source_ref"], "rules": market.get("description", ""),
                             "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams"})
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    selected.sort(key=lambda item: (item["start_at_ms"], item["market_id"]))
    return {"schema_version": 1, "captured_at_ms": int(now.timestamp() * 1000), "markets": selected[:4]}


class MicrostructureCapture:
    def __init__(self, config: Config):
        self.config = config
        self.directory = config.data_dir / "microstructure-v1"
        self.process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._error: str | None = None
        self._last_discovery = 0.0
        self._archive_error: str | None = None
        self._archive_stats = {"enabled": True, "archived_segments": 0, "raw_bytes": 0, "archive_bytes": 0, "bytes_saved": 0}
        self._bad_archives: set[str] = set()
        self._archive_progress = False
        self._last_clock_check = 0.0
        self._clock_state: dict = {"status": "not_checked"}

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="polycast-public-data", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._stop_process()

    def _stop_process(self) -> None:
        with self._guard:
            process = self.process
            if process and process.poll() is None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass

    def state(self) -> dict:
        with self._guard:
            alive = bool(self.process and self.process.poll() is None)
            pid = self.process.pid if alive else None
            error = self._error
        try:
            value = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                value = {}
        except (OSError, ValueError):
            value = {}
        matches_process = bool(alive and value.get("pid") == pid)
        updated = value.get("updated_at_ms")
        age = time.time() * 1000 - updated if type(updated) is int else None
        if not matches_process:
            value = {"status": "starting" if alive else "inactive",
                     "previous_status": value.get("status"),
                     "storage_bytes": value.get("storage_bytes", 0),
                     "storage_limit_bytes": CAPACITY_BYTES}
        value.update(alive=alive, public_only=True, last_error=error,
                     state_fresh=bool(matches_process and age is not None and 0 <= age <= 5000))
        with self._guard:
            value["archive"] = {**self._archive_stats, "last_error": self._archive_error,
                                "quarantined_segments": len(self._bad_archives)}
            value["public_clock"] = {key: val for key, val in self._clock_state.items() if key != "samples"}
        return value

    def _check_clock(self) -> None:
        if time.monotonic() - self._last_clock_check < 60:
            return
        try:
            clock = observe_public_clock()
        except Exception as exc:
            clock = {"schema_version": 1, "status": "unverified", "error": type(exc).__name__,
                     "checked_at_local_ms": int(time.time() * 1000), "system_clock_changed": False}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "clock.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(clock), encoding="utf-8")
        temporary.replace(path)
        self._last_clock_check = time.monotonic()
        with self._guard:
            self._clock_state = clock

    def _archive(self) -> None:
        state = self.state_file()
        try:
            result = archive_one(self.directory, active_name=state.get("active_segment"), skip=self._bad_archives)
            if result:
                with self._guard:
                    if result["bytes_saved"] > 0:
                        self._archive_progress = True
                    self._archive_stats["archived_segments"] += 1
                    for key in ("raw_bytes", "archive_bytes", "bytes_saved"):
                        self._archive_stats[key] += result[key]
        except ArchiveFailure as exc:
            self._bad_archives.add(exc.filename)
            with self._guard:
                self._archive_error = f"{exc.filename}: {str(exc)}"
            publish_once(contained_file(self.directory, exc.filename + ".archive-error.json"),
                         {"filename": exc.filename, "error": str(exc),
                          "observed_at": datetime.now(timezone.utc).isoformat(), "original_retained": True})

    def _load_archive_stats(self) -> None:
        # Indexes record verification at archival time; this is not a fresh full-corpus rehash.
        for path in self.directory.glob("raw-*.jsonl.archive.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("method") != "gzip-roundtrip-sha256-v1" or not all(
                    type(value.get(key)) is int for key in ("raw_bytes", "archive_bytes", "bytes_saved")):
                    continue
                self._archive_stats["archived_segments"] += 1
                for key in ("raw_bytes", "archive_bytes", "bytes_saved"):
                    self._archive_stats[key] += value[key]
            except (OSError, ValueError, AttributeError):
                continue
        self._bad_archives = {path.name.removesuffix(".archive-error.json")
                              for path in self.directory.glob("raw-*.jsonl.archive-error.json")}
        if self._bad_archives:
            self._archive_error = "Archive integrity errors remain quarantined; originals retained"

    def _ensure(self) -> None:
        with self._guard:
            if self._stop.is_set() or self.process and self.process.poll() is None:
                return
            state = self.state_file()
            resumable = False
            if state.get("status") == "storage_limit" and self._archive_progress:
                size = sum(path.stat().st_size for path in self.directory.glob("raw-*.jsonl*" )
                           if path.is_file() and not path.is_symlink() and path.name.endswith((".jsonl", ".jsonl.gz")))
                resumable = size < CAPACITY_BYTES * .9
            if state.get("status") in ("storage_limit", "storage_error") and not resumable:
                self._error = "Public collection stopped at its storage limit/error; existing data retained"
                return
            if self.directory.is_dir() and shutil.disk_usage(self.directory).free < 128 * 1024 ** 2:
                self._error = "Public collection stopped: insufficient free staging space"
                return
            node = shutil.which("node")
            if not node:
                raise RuntimeError("Node.js is unavailable for public collection")
            self.directory.mkdir(parents=True, exist_ok=True)
            log = (self.directory / "collector.err.log").open("a", encoding="utf-8")
            try:
                self.process = subprocess.Popen(
                    [node, str(Path(__file__).with_name("microstructure_collector.mjs")), str(self.directory),
                     str(self.directory / "selection.json"), "7200", str(CAPACITY_BYTES)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, env=public_collector_env(),
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
                self._archive_progress = False
            finally:
                log.close()

    def state_file(self) -> dict:
        try:
            value = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _run(self) -> None:
        try:
            self._load_archive_stats()
            while not self._stop.is_set():
                try:
                    self._archive()
                    self._check_clock()
                    if time.monotonic() - self._last_discovery >= 30:
                        local_now = datetime.now(timezone.utc)
                        clock = self._clock_state
                        offset = clock["offset_ms"] if clock.get("status") == "venues_agree" else 0
                        event_now = local_now + timedelta(milliseconds=offset)
                        selection = selected_markets(discover_btc_5m(event_now), event_now)
                        selection["captured_at_ms"] = int(local_now.timestamp() * 1000)
                        self.directory.mkdir(parents=True, exist_ok=True)
                        target = self.directory / "selection.json"
                        temporary = target.with_suffix(".json.tmp")
                        temporary.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
                        temporary.replace(target)
                        self._last_discovery = time.monotonic()
                    self._ensure()
                    with self._guard:
                        if self.process and self.process.poll() is None:
                            self._error = None
                except Exception as exc:
                    # Endpoint failures may contain proxy URL credentials; keep only exception type.
                    with self._guard:
                        self._error = f"Public collector/discovery failed ({type(exc).__name__})"
                self._stop.wait(2)
        finally:
            self._stop_process()
