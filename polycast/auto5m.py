"""Five-minute paper research while the local application is running."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .collector_env import node_collector_env
from .market import discover_btc_5m
from .runner import local_cycle_lock
from .short_cycle import collect_short, reconcile_short
from .store import database, evidence_for_market, get_market, now_iso, upsert_markets


def _market_claim_status(conn, market_id: str) -> str | None:
    if conn.execute("SELECT 1 FROM predictions WHERE market_id=? LIMIT 1",
                    (market_id,)).fetchone():
        return "predicted"
    if conn.execute("""SELECT 1 FROM model_runs WHERE market_id=?
        AND status='running' LIMIT 1""", (market_id,)).fetchone():
        return "running"
    return None


class AutoFiveMinute:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._collector: subprocess.Popen | None = None
        self._guard = threading.Lock()
        self._started_at: datetime | None = None
        self._collector_started_at: datetime | None = None
        self._state = {"status": "starting", "collector_alive": False,
                       "last_market_id": None, "last_action": None,
                       "last_at": None, "last_error": None, "collector_recoveries": 0,
                       "last_collector_recovery_at": None}

    def state(self) -> dict:
        with self._guard:
            result = dict(self._state)
        result["collector_alive"] = bool(self._collector and self._collector.poll() is None)
        now = datetime.now(timezone.utc)
        tick_ms = self._last_collector_tick_ms()
        result["collector_tick_at"] = (datetime.fromtimestamp(tick_ms / 1000, timezone.utc).isoformat()
                                       if tick_ms is not None else None)
        age = max(0, int(now.timestamp() - tick_ms / 1000)) if tick_ms is not None else None
        result["collector_data_age_seconds"] = age
        result["collector_fresh"] = bool(result["collector_alive"] and age is not None and age <= 30)
        waiting_too_long = self._started_at is not None and (now - self._started_at).total_seconds() > 30
        if result["collector_alive"] and not result["collector_fresh"] and waiting_too_long:
            result["status"] = "degraded"
            result["collector_warning"] = (f"Chainlink TWAP data is {age}s old"
                                           if age is not None else "No Chainlink TWAP data received")
        return result

    def _last_collector_tick_ms(self) -> int | None:
        path = self.config.data_dir / "chainlink_twap60.jsonl"
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 4096))
                lines = stream.read().splitlines()
            if not lines:
                return None
            tick_ms = json.loads(lines[-1])["timestamp_ms"]
            return tick_ms if isinstance(tick_ms, int) and tick_ms > 0 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _update(self, **changes) -> None:
        with self._guard:
            self._state.update(changes)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._run, name="polycast-btc5m", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._collector and self._collector.poll() is None:
            self._collector.terminate()
        if self._thread:
            self._thread.join(timeout=5)

    def _ensure_collector(self) -> None:
        if self._collector and self._collector.poll() is None:
            now = datetime.now(timezone.utc)
            started = self._collector_started_at
            tick = self._last_collector_tick_ms()
            if (started is None or (now - started).total_seconds() < 45 or
                    tick is not None and now.timestamp() - tick / 1000 <= 45):
                return
            # Recheck the actual data before retiring a live but stale collector.
            # A new process rereads Windows proxy settings after login/network changes.
            tick = self._last_collector_tick_ms()
            if tick is not None and now.timestamp() - tick / 1000 <= 45:
                return
            if self._collector.stdin:
                try:
                    self._collector.stdin.close()
                except OSError:
                    pass
            try:
                self._collector.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._collector.terminate()
                try:
                    self._collector.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    return  # Never launch a second collector while the first is live.
            self._update(collector_recoveries=self._state["collector_recoveries"] + 1,
                         last_collector_recovery_at=now_iso())
        if self._collector and self._collector.stdin and not self._collector.stdin.closed:
            try:
                self._collector.stdin.close()
            except OSError:
                pass
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js 22+ is required for BTC five-minute collection")
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        log = (self.config.data_dir / "chainlink-collector.err.log").open("a", encoding="utf-8")
        collector_env = node_collector_env()
        collector_env["POLYCAST_PARENT_PIPE"] = "1"
        try:
            self._collector = subprocess.Popen(
                [node, str(Path(__file__).with_name("chainlink_twap60.mjs")),
                 str(self.config.data_dir / "chainlink_twap60.jsonl"), "7200"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, env=collector_env,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            self._collector_started_at = datetime.now(timezone.utc)
        finally:
            log.close()
        self._update(collector_alive=True)

    def _run(self) -> None:
        try:
            with local_cycle_lock(self.config.data_dir / "btc5m.lock"):
                self._loop()
        except Exception as exc:
            self._update(status="error", last_error=f"{type(exc).__name__}: {exc}"[:180],
                         last_at=now_iso())
        finally:
            if self._collector and self._collector.poll() is None:
                self._collector.terminate()
            self._update(collector_alive=False)

    def _loop(self) -> None:
        from .cli import analyze, paper

        self._update(status="running")
        with database(self.config.database) as conn:
            attempted = {row["market_id"] for row in conn.execute("""SELECT DISTINCT p.market_id
                FROM predictions p JOIN markets m ON m.id=p.market_id
                WHERE m.category='crypto5m' AND julianday(m.end_at)>julianday(?)""",
                (now_iso(),))}
        format_retries: dict[str, int] = {}
        next_reconcile = datetime.min.replace(tzinfo=timezone.utc)
        while not self._stop.is_set():
            try:
                self._ensure_collector()
                now = datetime.now(timezone.utc)
                markets = discover_btc_5m(now)
                prediction_enabled = self.config.five_minute_auto_enabled()
                eligible = [m for m in markets if m["id"] not in attempted and
                            datetime.fromtimestamp(int(m["source_ref"].split(":")[1]), timezone.utc)
                            + timedelta(seconds=80) <= now <
                            datetime.fromisoformat(m["end_at"]) - timedelta(seconds=100)]
                if not prediction_enabled:
                    eligible = []
                if eligible:
                    market = eligible[0]
                    with database(self.config.database) as conn:
                        # The scheduled cycle may have analyzed this window after
                        # the worker started. Do not spend two model calls to
                        # replace its reviewed forecast with a later duplicate.
                        claim = _market_claim_status(conn, market["id"])
                        if claim == "predicted":
                            attempted.add(market["id"])
                            continue
                        if claim == "running":
                            self._stop.wait(10)
                            continue
                        upsert_markets(conn, [market], reset_watchlist=False)
                        collect_short(conn, [market], self.config.data_dir / "chainlink_twap60.jsonl")
                        evidence = evidence_for_market(conn, get_market(conn, market["id"]))
                    if any(item["source_id"] == "chainlink_btc_twap60" for item in evidence):
                        attempted.add(market["id"])
                        try:
                            forecast = analyze(self.config, market["id"], once_per_window=True)
                        except RuntimeError as exc:
                            if "already running" in str(exc):
                                # Another caller owns the OS analysis lock. Its
                                # outcome is not yet known, so retry the claim.
                                attempted.discard(market["id"])
                                self._stop.wait(10)
                                continue
                            # A malformed model JSON response is transient. Give the same
                            # window one more chance while the entry cutoff is still open.
                            if ("DeepSeek output does not match" in str(exc) or
                                    "DeepSeek returned invalid JSON" in str(exc)):
                                count = format_retries.get(market["id"], 0) + 1
                                format_retries[market["id"]] = count
                                if count < 2:
                                    attempted.discard(market["id"])
                            raise
                        decision = ({"status": "paused"} if not self.config.five_minute_auto_enabled() else
                                    paper(self.config, market["id"])
                                    if forecast.get("status") in ("analyzed", "unchanged") else None)
                        if forecast.get("status") == "paused":
                            attempted.discard(market["id"])
                        self._update(last_market_id=market["id"],
                                     last_action=(decision or forecast).get("status"),
                                     last_at=now_iso(), last_error=None)
                if now >= next_reconcile:
                    with database(self.config.database) as conn:
                        reconcile_short(conn, limit=12, categories=("crypto5m",),
                                        chainlink_path=self.config.data_dir / "chainlink_twap60.jsonl")
                    next_reconcile = datetime.now(timezone.utc) + timedelta(minutes=1)
                self._update(status=("running" if self.config.five_minute_auto_enabled() else "paused"),
                             collector_alive=True)
            except Exception as exc:
                self._update(status="degraded", last_error=f"{type(exc).__name__}: {exc}"[:180],
                             last_at=now_iso())
            self._stop.wait(10)
