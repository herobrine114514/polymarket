from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from .analysis import CODEX_MODEL, PROMPT_VERSION
from .config import Config
from .provider import DEEPSEEK_MODEL, active_provider
from .store import database, digest_text, now_iso

LEASE_DURATION = timedelta(minutes=45)


@contextmanager
def local_cycle_lock(path: Path) -> Iterator[None]:
    """An OS lock is released on process death, unlike a stale PID or DB row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError("A Polycast cycle is already running") from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _configuration_hash(config: Config) -> str:
    values = asdict(config)
    values["data_dir"] = str(values["data_dir"])
    return digest_text(values)


def _start_run(config: Config, owner: str) -> int:
    model = DEEPSEEK_MODEL if active_provider(config.data_dir) == "deepseek" else CODEX_MODEL
    with database(config.database) as conn:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # The OS lock is held, so no earlier local process can still own this run.
            conn.execute("""UPDATE cycle_runs SET status='abandoned',finished_at=?,
                errors_json=? WHERE status='running'""",
                (now_iso(), json.dumps(["Previous process ended before recording completion"])))
            started = now_iso()
            expires = (datetime.now(timezone.utc) + LEASE_DURATION).isoformat(timespec="seconds")
            conn.execute("""INSERT INTO cycle_leases(name,owner_token,acquired_at,expires_at)
                VALUES('cycle',?,?,?) ON CONFLICT(name) DO UPDATE SET
                owner_token=excluded.owner_token,acquired_at=excluded.acquired_at,
                expires_at=excluded.expires_at""", (owner, started, expires))
            prior_model_id = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM model_runs"
            ).fetchone()[0]
            cursor = conn.execute("""INSERT INTO cycle_runs
                (owner_token,started_at,status,config_hash,prompt_version,model_version,
                 model_run_id_before) VALUES(?,?,'running',?,?,?,?)""",
                (owner, started, _configuration_hash(config), PROMPT_VERSION,
                 model, prior_model_id))
            conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            conn.rollback()
            raise


def _summarize(result: dict) -> dict:
    errors: list[str] = []
    errors.extend("discovery " + str(error)[:200]
                  for error in result.get("discovery", {}).get("errors", []))
    errors.extend(str(error)[:200] for error in result.get("sources", {}).get("errors", []))
    for source, state in result.get("calendars", {}).items():
        if state.get("status") == "error":
            errors.append(f"{source}: {state.get('reason', '')}"[:200])
    for item in result.get("analyses", []):
        if item.get("status") == "error":
            errors.append(f"{item.get('market_id') or item.get('event_id')}: {item.get('message', '')}"[:200])
        for forecast in ([item] if not item.get("predictions") else item["predictions"]):
            paper = forecast.get("paper") or {}
            if paper.get("status") == "error":
                errors.append(f"{forecast.get('market_id')} paper: {paper.get('message', '')}"[:200])
    for item in result.get("paper_checks", []):
        if item.get("status") == "error":
            errors.append(f"{item.get('market_id')} paper: {item.get('message', '')}"[:200])
    analysis_paper = [forecast.get("paper") or {}
                      for item in result.get("analyses", [])
                      for forecast in ([item] if not item.get("predictions") else item["predictions"])]
    paper_fills = sum(item.get("status") == "paper_filled" for item in analysis_paper)
    paper_fills += sum(item.get("status") == "paper_filled"
                       for item in result.get("paper_checks", []))
    return {
        "status": "partial" if errors else "succeeded",
        "watchlist_markets": int(result.get("watchlist_markets") or 0),
        "evidence_fetched": int(result.get("sources", {}).get("fetched") or 0),
        "new_evidence_versions": int(result.get("sources", {}).get("new_versions") or 0),
        "analysis_attempts": len(result.get("analyses", [])),
        "paper_checks": len(result.get("paper_checks", [])) +
                        sum(bool(item) for item in analysis_paper),
        "paper_fills": paper_fills,
        "error_count": len(errors),
        "errors": errors[:20],
    }


def _finish_run(config: Config, owner: str, run_id: int, summary: dict) -> None:
    with database(config.database) as conn:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            lease = conn.execute(
                "SELECT owner_token FROM cycle_leases WHERE name='cycle'"
            ).fetchone()
            if lease is None or lease["owner_token"] != owner:
                raise RuntimeError("Polycast cycle lease was lost")
            run = conn.execute(
                "SELECT model_run_id_before,status FROM cycle_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError("Polycast cycle record is no longer running")
            model_calls = conn.execute(
                "SELECT COUNT(*) FROM model_runs WHERE id>?",
                (run["model_run_id_before"],)).fetchone()[0]
            versions = [row[0] for row in conn.execute(
                "SELECT DISTINCT model_version FROM model_runs WHERE id>?",
                (run["model_run_id_before"],))]
            # Attribute attempted calls, including failures. A provider switch
            # during the cycle must not silently relabel earlier calls.
            if versions:
                conn.execute("UPDATE cycle_runs SET model_version=? WHERE id=?",
                             (versions[0] if len(versions) == 1 else "mixed", run_id))
            conn.execute("""UPDATE cycle_runs SET finished_at=?,status=?,model_calls=?,
                watchlist_markets=?,evidence_fetched=?,new_evidence_versions=?,
                analysis_attempts=?,paper_checks=?,paper_fills=?,error_count=?,
                errors_json=? WHERE id=?""",
                (now_iso(), summary["status"], model_calls,
                 summary["watchlist_markets"], summary["evidence_fetched"],
                 summary["new_evidence_versions"], summary["analysis_attempts"],
                 summary["paper_checks"], summary["paper_fills"], summary["error_count"],
                 json.dumps(summary["errors"], ensure_ascii=False), run_id))
            conn.execute(
                "DELETE FROM cycle_leases WHERE name='cycle' AND owner_token=?", (owner,)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def run_recorded(config: Config, operation: Callable[[], dict]) -> dict:
    """Run one bounded paper cycle, keeping a durable outcome for every attempt."""
    with local_cycle_lock(config.data_dir / "cycle.lock"):
        owner = uuid.uuid4().hex
        run_id = _start_run(config, owner)
        try:
            result = operation()
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            failed = {"status": "failed", "watchlist_markets": 0,
                      "evidence_fetched": 0, "new_evidence_versions": 0,
                      "analysis_attempts": 0, "paper_checks": 0,
                      "paper_fills": 0, "error_count": 1, "errors": [error]}
            _finish_run(config, owner, run_id, failed)
            raise
        summary = _summarize(result)
        _finish_run(config, owner, run_id, summary)
        return {"run_id": run_id, "status": summary["status"],
                "summary": summary, "result": result}
