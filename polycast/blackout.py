from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Blackout time must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Blackout time must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Blackout time must include a timezone")
    return parsed.astimezone(timezone.utc)


def active_window(path: Path, risk_group: str, now: datetime | None = None) -> str | None:
    """Return a configured release blackout; invalid schedules fail closed."""
    if not path.exists():
        return None
    try:
        windows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Blackout schedule cannot be read: {exc}") from exc
    if not isinstance(windows, list) or len(windows) > 200:
        raise ValueError("Blackout schedule must be a list of at most 200 windows")
    instant = now or datetime.now(timezone.utc)
    active: str | None = None
    for item in windows:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Blackout window needs a name")
        starts = _timestamp(item.get("starts_at"))
        ends = _timestamp(item.get("ends_at"))
        groups = item.get("risk_groups")
        if starts >= ends or not isinstance(groups, list) or not groups or any(
                not isinstance(group, str) or not group for group in groups):
            raise ValueError("Blackout window has invalid bounds or groups")
        if starts <= instant < ends and ("all" in groups or risk_group in groups):
            active = item["name"]
    return active


def evidence_cooling(first_seen_at: str, published_at: str, minutes: int,
                     now: datetime | None = None) -> bool:
    """Hold fresh releases briefly; old archives first fetched today do not count."""
    if minutes <= 0:
        return False
    instant = now or datetime.now(timezone.utc)
    first_seen = _timestamp(first_seen_at)
    published = _timestamp(published_at)
    return (instant - timedelta(minutes=minutes) <= first_seen <= instant and
            instant - timedelta(hours=24) <= published <= instant)
