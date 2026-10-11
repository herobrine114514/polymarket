from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .net import fetch_bytes

OFFICIAL_FEEDS = (
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve Speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("Federal Reserve Testimony", "https://www.federalreserve.gov/feeds/testimony.xml"),
    ("BLS CPI", "https://www.bls.gov/feed/cpi.rss"),
    ("BLS Employment", "https://www.bls.gov/feed/empsit.rss"),
    ("BEA Releases", "https://apps.bea.gov/rss/rss.xml"),
    ("SEC Press", "https://www.sec.gov/rss/news/press.xml"),
    ("CFTC General", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml"),
    ("White House Presidential Actions", "https://www.whitehouse.gov/presidential-actions/feed/"),
)
ATOM = "{http://www.w3.org/2005/Atom}"


class ArticleText(HTMLParser):
    def __init__(self, container_id: str = "article") -> None:
        super().__init__()
        self.container_id = container_id
        self.depth = 0
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif attributes.get("id") == self.container_id:
                self.depth = 1
        if self.depth and tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag in ("script", "style") and self.skip:
            self.skip -= 1
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth and not self.skip:
            self.parts.append(data)


def clean_text(text: str | None, limit: int = 1800) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(text or ""))).strip()[:limit]


def parse_bls_release(raw: bytes) -> tuple[str, datetime | None]:
    """Extract the official summary and embargo lift, never infer it from RSS pubDate."""
    parser = ArticleText("bodytext")
    parser.feed(raw.decode("utf-8-sig", "replace"))
    body = clean_text(" ".join(parser.parts), 5000)
    header = body[:700]
    match = re.search(r"embargoed until.{0,150}?\b(\d{1,2}:\d{2})\s*([ap])\.m\.\s*"
                      r"\(ET\)\s*(?:[A-Za-z]+,\s*)?([A-Za-z]+\s+\d{1,2},\s*\d{4})",
                      header, re.I)
    if not match:
        return body, None
    try:
        local = datetime.strptime(f"{match.group(1)} {match.group(2).upper()}M {match.group(3)}",
                                  "%I:%M %p %B %d, %Y")
        return body, local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    except ValueError:
        return body, None


def parse_rss(xml_data: bytes, source: str, feed_url: str) -> list[dict]:
    root = ET.fromstring(xml_data)
    items: list[dict] = []
    if root.tag == ATOM + "feed":
        for node in root.findall(ATOM + "entry")[:40]:
            title = clean_text(node.findtext(ATOM + "title"))
            link_node = node.find(ATOM + "link")
            link = (link_node.attrib.get("href") or "") if link_node is not None else ""
            summary = clean_text(node.findtext(ATOM + "content") or node.findtext(ATOM + "summary"))
            published_text = node.findtext(ATOM + "published") or node.findtext(ATOM + "updated")
            try:
                published = datetime.fromisoformat(published_text.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (AttributeError, ValueError):
                continue
            updated_text = node.findtext(ATOM + "updated")
            try:
                updated = datetime.fromisoformat(updated_text.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (AttributeError, ValueError):
                updated = published
            if not title or urlparse(link).scheme != "https" or published > datetime.now(timezone.utc):
                continue
            entry_id = node.findtext(ATOM + "id") or link
            origin_id = entry_id if urlparse(entry_id).scheme == "https" else feed_url + "#" + entry_id
            items.append({"source": source, "url": link, "title": title, "summary": summary,
                          "published_at": published.isoformat(timespec="seconds"),
                          "feed_published_at": published.isoformat(timespec="seconds"),
                          "updated_at": updated.isoformat(timespec="seconds"),
                          "origin_story_id": origin_id, "feed_url": feed_url})
        return items
    for node in root.findall(".//item")[:40]:
        title = clean_text(node.findtext("title"))
        link = clean_text(node.findtext("link"))
        summary = clean_text(node.findtext("description"))
        if not title or not link:
            continue
        parsed = urlparse(link)
        if parsed.scheme != "https" or not parsed.netloc:
            continue
        pub = node.findtext("pubDate")
        if pub:
            try:
                published = parsedate_to_datetime(pub).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
        else:
            continue  # Do not invent publication times for a trading signal.
        if published > datetime.now(timezone.utc):
            continue
        guid = node.findtext("guid") or link
        origin_id = guid if urlparse(guid).scheme == "https" else feed_url + "#" + guid
        items.append({"source": source, "url": link, "title": title,
                      "summary": summary, "published_at": published.isoformat(timespec="seconds"),
                      "feed_published_at": published.isoformat(timespec="seconds"),
                      "updated_at": published.isoformat(timespec="seconds"),
                      "origin_story_id": origin_id, "feed_url": feed_url})
    return items


def collect_official() -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    for source, url in OFFICIAL_FEEDS:
        try:
            feed_items = parse_rss(fetch_bytes(url, accept="application/rss+xml,application/xml,text/xml"), source, url)
            if source == "White House Presidential Actions":
                feed_items = [item for item in feed_items
                              if urlparse(item["url"]).hostname == "www.whitehouse.gov"
                              and urlparse(item["url"]).path.startswith("/presidential-actions/")]
            if source in ("Federal Reserve", "Federal Reserve Speeches", "Federal Reserve Testimony"):
                feed_items = [item for item in feed_items
                              if urlparse(item["url"]).hostname == "www.federalreserve.gov"]
            if source == "Federal Reserve Testimony":
                feed_items = [item for item in feed_items if any(
                    term in item["title"].lower() for term in (
                        "monetary policy", "economic outlook", "inflation", "employment",
                        "federal funds", "semiannual monetary")) and
                              urlparse(item["url"]).path.startswith("/newsevents/testimony/")]
            if source in ("Federal Reserve", "Federal Reserve Speeches", "Federal Reserve Testimony"):
                full_text_limit = (8 if source == "Federal Reserve" else
                                   4 if source == "Federal Reserve Speeches" else 2)
                for item in feed_items[:full_text_limit]:
                    try:
                        parser = ArticleText()
                        parser.feed(fetch_bytes(item["url"], accept="text/html").decode("utf-8-sig", "replace"))
                        article = clean_text(" ".join(parser.parts), 3500)
                        if len(article) > len(item["summary"]) + 50:
                            item["summary"] = article
                    except Exception as exc:
                        errors.append(f"Article {item['url']}: {exc}")
            elif source in ("BLS CPI", "BLS Employment"):
                safe_items: list[dict] = []
                instant = datetime.now(timezone.utc)
                for index, item in enumerate(feed_items):
                    feed_time = datetime.fromisoformat(item["published_at"])
                    recent = (instant - feed_time).total_seconds() < 48 * 3600
                    if urlparse(item["url"]).hostname != "www.bls.gov":
                        errors.append(f"Skipped non-BLS article in {source}: {item['url']}")
                        continue
                    if index < 2 or recent:
                        try:
                            body, public_at = parse_bls_release(
                                fetch_bytes(item["url"], accept="text/html"))
                            if public_at is None:
                                if recent:
                                    errors.append(f"Skipped recent BLS release without verified public time: {item['url']}")
                                    continue
                            else:
                                if public_at > datetime.now(timezone.utc):
                                    errors.append(f"Skipped embargoed BLS release until {public_at.isoformat()}: {item['url']}")
                                    continue
                                item["published_at"] = public_at.isoformat(timespec="seconds")
                                if datetime.fromisoformat(item["updated_at"]) < public_at:
                                    item["updated_at"] = item["published_at"]
                            if len(body) > len(item["summary"]) + 50:
                                item["summary"] = body
                        except Exception as exc:
                            errors.append(f"BLS article {item['url']}: {exc}")
                            if recent:
                                continue
                    safe_items.append(item)
                feed_items = safe_items
            items.extend(feed_items)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    return items, errors


def collect_configured(path: Path) -> tuple[list[dict], list[str], list[dict]]:
    """Optional licensed RSS feeds; no source is enabled by default."""
    if not path.exists():
        return [], [], []
    try:
        feeds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"Configured feeds: {exc}"], []
    if not isinstance(feeds, list) or len(feeds) > 12:
        return [], ["Configured feeds must be a list of at most twelve entries"], []
    items: list[dict] = []
    errors: list[str] = []
    sources: list[dict] = []
    seen_ids: set[str] = set()
    for entry in feeds:
        if not isinstance(entry, dict):
            errors.append("Configured feed entry must be an object")
            continue
        name, url, topic = entry.get("name"), entry.get("url"), entry.get("topic")
        source_id = entry.get("source_id")
        license_reference = entry.get("license_reference")
        license_checked_at = entry.get("license_checked_at")
        parsed = urlparse(str(url or ""))
        try:
            checked = datetime.fromisoformat(str(license_checked_at).replace("Z", "+00:00"))
            valid_checked = checked.tzinfo is not None and checked <= datetime.now(timezone.utc)
        except ValueError:
            valid_checked = False
        if (entry.get("license_confirmed") is not True or not isinstance(name, str) or
                not 1 <= len(name) <= 50 or ":" in name or topic not in ("fed", "macro", "crypto", "daily") or
                parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or
                not isinstance(source_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{2,39}", source_id) or
                source_id in seen_ids or
                entry.get("license_scope") != "summary_storage_and_model_analysis" or
                not isinstance(license_reference, str) or not 4 <= len(license_reference) <= 200 or
                not valid_checked):
            errors.append(f"Skipped invalid/unlicensed feed: {str(name)[:50]}")
            continue
        seen_ids.add(source_id)
        if parsed.netloc.lower().removeprefix("www.") in ("x.com", "twitter.com", "reddit.com", "t.me", "telegram.org"):
            errors.append(f"Social platform terms need separate review: {name}")
            continue
        source = f"News {topic.title()}: {name}"
        descriptor = {"source_id": "news_" + source_id, "source": source,
                      "feed_url": url,
                      "license_scope": "summary_storage_and_model_analysis",
                      "license_reference": license_reference,
                      "license_checked_at": checked.astimezone(timezone.utc).isoformat(timespec="seconds")}
        sources.append(descriptor)
        try:
            feed_items = parse_rss(fetch_bytes(url, accept="application/rss+xml,application/atom+xml,application/xml,text/xml"),
                                   source, url)[:30]
            items.extend([{**item, "source_id": descriptor["source_id"],
                           "license_scope": descriptor["license_scope"]} for item in feed_items])
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return items, errors, sources

