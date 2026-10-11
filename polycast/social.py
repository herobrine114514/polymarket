"""Small, verifiable public social feed for official policy leads."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from .evidence import clean_text
from .net import fetch_json

FED_DID = "did:plc:d3vmaxc5ytftnf4gwdwptupg"
FED_HANDLE = "federalreserve.gov"
FED_FEED_URL = ("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
                f"?actor={FED_DID}&filter=posts_no_replies&limit=30")


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def parse_fed_feed(payload: dict, now: datetime | None = None) -> list[dict]:
    """Use AppView indexing time, not an author-controlled backdated timestamp."""
    now = now or datetime.now(timezone.utc)
    entries = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Invalid Bluesky author feed")
    items: list[dict] = []
    for entry in entries[:30]:
        if not isinstance(entry, dict) or entry.get("reason"):
            continue
        post = entry.get("post")
        if not isinstance(post, dict):
            continue
        author, record = post.get("author"), post.get("record")
        if not isinstance(author, dict) or not isinstance(record, dict):
            continue
        if (author.get("did") != FED_DID or author.get("handle") != FED_HANDLE or
                record.get("$type") != "app.bsky.feed.post" or record.get("reply")):
            continue
        author_labels = author.get("labels") if isinstance(author.get("labels"), list) else []
        post_labels = post.get("labels") if isinstance(post.get("labels"), list) else []
        labels = author_labels + post_labels
        if any(isinstance(label, dict) and label.get("val") == "!no-unauthenticated"
               for label in labels):
            continue
        uri = post.get("uri")
        prefix = f"at://{FED_DID}/app.bsky.feed.post/"
        if not isinstance(uri, str) or not uri.startswith(prefix):
            continue
        rkey = uri.removeprefix(prefix)
        if not rkey or not rkey.isalnum():
            continue
        created, indexed = _instant(record.get("createdAt")), _instant(post.get("indexedAt"))
        if (created is None or indexed is None or created > indexed or indexed > now or
                (now - indexed).days > 30):
            continue
        raw_text = record.get("text")
        content = clean_text(raw_text, 600) if isinstance(raw_text, str) else ""
        if not content:
            continue
        origin = uri
        facets = record.get("facets") if isinstance(record.get("facets"), list) else []
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            features = facet.get("features") if isinstance(facet.get("features"), list) else []
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                link = feature.get("uri")
                try:
                    parsed = urlparse(link) if isinstance(link, str) else None
                except ValueError:
                    parsed = None
                if parsed and parsed.scheme == "https" and parsed.hostname in (
                        "federalreserve.gov", "www.federalreserve.gov"):
                    origin = link
                    break
            if origin != uri:
                break
        items.append({"source": "Federal Reserve Bluesky",
                      "url": f"https://bsky.app/profile/{FED_HANDLE}/post/{rkey}",
                      "title": content[:180], "summary": content,
                      "published_at": indexed.isoformat(timespec="seconds"),
                      "feed_published_at": created.isoformat(timespec="seconds"),
                      "updated_at": indexed.isoformat(timespec="seconds"),
                      "origin_story_id": origin, "feed_url": FED_FEED_URL})
    return items


def collect_fed_bluesky() -> tuple[list[dict], list[str]]:
    try:
        return parse_fed_feed(fetch_json(FED_FEED_URL)), []
    except Exception as exc:
        return [], [f"Federal Reserve Bluesky: {type(exc).__name__}: {exc}"]
