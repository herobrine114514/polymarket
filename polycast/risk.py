from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import Config
from .execution import fill_totals, match_levels
from .market import order_book, parse_utc
from .store import balance, digest_text, now_iso


def _open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT o.id,o.market_id,o.side,o.shares,o.total_cost,
        m.yes_token,m.no_token,m.fee_rate,m.end_at FROM paper_orders o
        JOIN markets m ON m.id=o.market_id WHERE o.status='open' ORDER BY o.id""").fetchall()


def _fingerprint(positions: list[sqlite3.Row]) -> str:
    return digest_text([(row["id"], row["market_id"], row["side"],
                         row["shares"], row["total_cost"]) for row in positions])


def _record_halt(conn: sqlite3.Connection, reason: str, details: str) -> None:
    conn.execute("""INSERT OR IGNORE INTO risk_halts(reason,details,triggered_at)
        VALUES(?,?,?)""", (reason, details[:250], now_iso()))


def mark_account(conn: sqlite3.Connection, config: Config) -> dict:
    """Value all open paper positions against current executable bids."""
    positions = _open_positions(conn)
    fingerprint = _fingerprint(positions)
    liquidation_value = 0.0
    try:
        grouped: dict[str, dict] = {}
        now = datetime.now(timezone.utc)
        for row in positions:
            end_at = parse_utc(row["end_at"])
            if end_at is not None and end_at <= now:
                # Closed positions cannot be sold; mark them at zero until verified settlement.
                continue
            token = row["yes_token"] if row["side"] == "YES" else row["no_token"]
            group = grouped.setdefault(token, {"shares": 0.0, "fee_rate": row["fee_rate"]})
            group["shares"] += float(row["shares"])
        for token, group in grouped.items():
            book = order_book(token)
            book_time = datetime.fromtimestamp(int(book["timestamp"]) / 1000, timezone.utc)
            age = (datetime.now(timezone.utc) - book_time).total_seconds()
            if age < -5 or age > config.quote_max_age_seconds:
                raise RuntimeError("Position order book is stale")
            fills = match_levels(book, group["shares"], "bids")
            if fills is None:
                continue  # No executable depth: conservatively value this position at zero.
            gross, _, fee = fill_totals(fills, group["shares"], float(group["fee_rate"]))
            liquidation_value += max(0.0, gross - fee)
    except Exception as exc:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _record_halt(conn, "valuation_unavailable",
                         f"{type(exc).__name__}: {exc}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        raise RuntimeError("Paper account valuation unavailable; new risk halted") from exc

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _fingerprint(_open_positions(conn)) != fingerprint:
            raise RuntimeError("Open positions changed during valuation")
        cash = balance(conn, config.initial_cash)["cash_available"]
        equity = cash + liquidation_value
        timestamp = now_iso()
        day = timestamp[:10]
        existing_day = conn.execute(
            "SELECT day_start_equity FROM risk_days WHERE utc_day=?", (day,)
        ).fetchone()
        if existing_day:
            day_start = float(existing_day["day_start_equity"])
        else:
            previous = conn.execute(
                "SELECT equity FROM risk_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if previous is None and positions:
                _record_halt(conn, "missing_risk_history",
                             "Open paper positions existed before the first equity snapshot")
            day_start = float(previous["equity"]) if previous else config.initial_cash
            conn.execute("""INSERT INTO risk_days(utc_day,day_start_equity,created_at)
                VALUES(?,?,?)""", (day, day_start, timestamp))
        prior_peak = conn.execute(
            "SELECT MAX(equity) FROM risk_snapshots"
        ).fetchone()[0]
        high_water = max(config.initial_cash, float(prior_peak or 0), equity)
        conn.execute("""INSERT INTO risk_snapshots(captured_at,cash_available,
            liquidation_value,equity,open_fingerprint,day_start_equity,high_water_equity)
            VALUES(?,?,?,?,?,?,?)""",
            (timestamp, cash, liquidation_value, equity, fingerprint, day_start, high_water))
        daily_loss = max(0.0, (day_start - equity) / day_start)
        drawdown = max(0.0, (high_water - equity) / high_water)
        if daily_loss >= config.max_daily_loss_fraction:
            _record_halt(conn, "daily_loss",
                         f"Daily loss {daily_loss:.4%} reached {config.max_daily_loss_fraction:.4%}")
        if drawdown >= config.max_drawdown_fraction:
            _record_halt(conn, "drawdown",
                         f"Drawdown {drawdown:.4%} reached {config.max_drawdown_fraction:.4%}")
        halts = [row["reason"] for row in conn.execute(
            "SELECT reason FROM risk_halts WHERE cleared_at IS NULL ORDER BY id"
        )]
        conn.commit()
        return {"captured_at": timestamp, "cash_available": round(cash, 6),
                "liquidation_value": round(liquidation_value, 6),
                "equity": round(equity, 6), "day_start_equity": round(day_start, 6),
                "high_water_equity": round(high_water, 6),
                "daily_loss_fraction": round(daily_loss, 6),
                "drawdown_fraction": round(drawdown, 6), "active_halts": halts}
    except Exception:
        conn.rollback()
        raise


def active_halts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT reason,details,triggered_at FROM risk_halts WHERE cleared_at IS NULL ORDER BY id"
    )]


def assert_entry_allowed(conn: sqlite3.Connection, config: Config) -> None:
    halts = active_halts(conn)
    if halts:
        raise ValueError("Paper risk halted: " + ", ".join(row["reason"] for row in halts))
    snapshot = conn.execute(
        "SELECT * FROM risk_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if snapshot is None:
        raise ValueError("No paper account valuation")
    captured = parse_utc(snapshot["captured_at"])
    if (captured is None or captured.date() != datetime.now(timezone.utc).date() or
            not 0 <= (datetime.now(timezone.utc) - captured).total_seconds()
            <= config.quote_max_age_seconds):
        raise ValueError("Paper account valuation is stale")
    if snapshot["open_fingerprint"] != _fingerprint(_open_positions(conn)):
        raise ValueError("Paper positions changed since valuation")
    if (snapshot["day_start_equity"] - snapshot["equity"]) / snapshot["day_start_equity"] >= config.max_daily_loss_fraction:
        raise ValueError("Daily loss stop")
    if (snapshot["high_water_equity"] - snapshot["equity"]) / snapshot["high_water_equity"] >= config.max_drawdown_fraction:
        raise ValueError("Drawdown stop")


def resume_after_reconciliation(conn: sqlite3.Connection, config: Config,
                                reason: str) -> dict:
    """Explicitly clear paper-only stops after a fresh mark and documented review."""
    reason = reason.strip()
    if len(reason) < 12:
        raise ValueError("A specific reconciliation and recovery reason is required")
    marked = mark_account(conn, config)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        snapshot = conn.execute(
            "SELECT * FROM risk_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if (snapshot is None or snapshot["open_fingerprint"] !=
                _fingerprint(_open_positions(conn))):
            raise ValueError("Positions changed after reconciliation mark")
        if snapshot["captured_at"] != marked["captured_at"]:
            raise ValueError("Risk mark was superseded")
        if ((snapshot["day_start_equity"] - snapshot["equity"]) /
                snapshot["day_start_equity"] >= config.max_daily_loss_fraction or
                (snapshot["high_water_equity"] - snapshot["equity"]) /
                snapshot["high_water_equity"] >= config.max_drawdown_fraction):
            raise ValueError("Loss or drawdown threshold is still breached")
        cursor = conn.execute("""UPDATE risk_halts SET cleared_at=?,clear_reason=?
            WHERE cleared_at IS NULL""", (now_iso(), reason))
        conn.commit()
        return {"cleared_halts": cursor.rowcount, "reason": reason,
                "equity": marked["equity"]}
    except Exception:
        conn.rollback()
        raise
