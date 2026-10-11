from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS markets (
  id TEXT PRIMARY KEY, event_id TEXT NOT NULL, event_title TEXT NOT NULL,
  event_slug TEXT NOT NULL, question TEXT NOT NULL, description TEXT NOT NULL,
  end_at TEXT, yes_token TEXT NOT NULL, no_token TEXT NOT NULL,
  liquidity REAL NOT NULL, volume_24h REAL NOT NULL,
  min_order_size REAL NOT NULL, tick_size REAL NOT NULL, fee_rate REAL NOT NULL,
  category TEXT NOT NULL DEFAULT 'macro', yes_label TEXT NOT NULL DEFAULT 'Yes',
  no_label TEXT NOT NULL DEFAULT 'No', source_ref TEXT,
  discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  in_watchlist INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS markets_event ON markets(event_id);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL, published_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, content_hash TEXT NOT NULL,
  source_id TEXT, feed_url TEXT, license_scope TEXT,
  updated_at TEXT, fetched_at TEXT, last_seen_at TEXT, origin_story_id TEXT,
  feed_published_at TEXT,
  UNIQUE(url,content_hash)
);
CREATE INDEX IF NOT EXISTS evidence_published ON evidence(published_at);
CREATE TABLE IF NOT EXISTS evidence_current (
  url TEXT PRIMARY KEY, evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  activation_seq INTEGER NOT NULL, activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  observed_at TEXT NOT NULL, observation_json TEXT NOT NULL,
  observation_sha256 TEXT NOT NULL,
  UNIQUE(evidence_id,observed_at,observation_sha256)
);
CREATE INDEX IF NOT EXISTS evidence_observation_time ON evidence_observations(observed_at,id);
CREATE TRIGGER IF NOT EXISTS evidence_observation_no_update
  BEFORE UPDATE ON evidence_observations BEGIN
    SELECT RAISE(ABORT,'Evidence observations are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS evidence_observation_no_delete
  BEFORE DELETE ON evidence_observations BEGIN
    SELECT RAISE(ABORT,'Evidence observations are append-only');
  END;
CREATE TABLE IF NOT EXISTS source_registry (
  source_id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL
    CHECK(kind IN ('official','news')),
  feed_url TEXT NOT NULL, license_scope TEXT NOT NULL,
  license_reference TEXT NOT NULL, license_checked_at TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_withdrawals (
  url TEXT PRIMARY KEY, source_url TEXT NOT NULL,
  reason TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_fetches (
  source TEXT PRIMARY KEY, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_windows (
  source TEXT NOT NULL, event_uid TEXT NOT NULL, name TEXT NOT NULL,
  event_at TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
  risk_groups_json TEXT NOT NULL, source_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL, PRIMARY KEY(source,event_uid)
);
CREATE INDEX IF NOT EXISTS calendar_windows_time ON calendar_windows(starts_at,ends_at);
CREATE TABLE IF NOT EXISTS calendar_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
  event_uid TEXT NOT NULL, content_hash TEXT NOT NULL,
  observed_at TEXT NOT NULL, name TEXT NOT NULL, event_at TEXT NOT NULL,
  starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
  UNIQUE(source,event_uid,content_hash)
);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL REFERENCES markets(id),
  as_of TEXT NOT NULL, rules_hash TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  probability REAL, abstain INTEGER NOT NULL, reasoning TEXT NOT NULL,
  assumptions_json TEXT NOT NULL, supports_json TEXT NOT NULL,
  counters_json TEXT NOT NULL, model_output_json TEXT NOT NULL,
  UNIQUE(market_id,rules_hash,evidence_hash)
);
CREATE INDEX IF NOT EXISTS predictions_market ON predictions(market_id,as_of DESC);
CREATE TABLE IF NOT EXISTS market_baselines (
  prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
  captured_at TEXT NOT NULL, book_timestamp TEXT NOT NULL,
  yes_best_bid REAL NOT NULL, yes_best_ask REAL NOT NULL,
  p_yes_mid REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL REFERENCES markets(id),
  side TEXT NOT NULL CHECK(side IN ('YES','NO')), captured_at TEXT NOT NULL,
  book_timestamp TEXT NOT NULL, shares REAL NOT NULL, avg_ask REAL NOT NULL,
  worst_ask REAL NOT NULL, fee_per_share REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  market_id TEXT NOT NULL REFERENCES markets(id),
  event_id TEXT NOT NULL, risk_group TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('YES','NO')),
  shares REAL NOT NULL, limit_price REAL NOT NULL,
  max_total_cost REAL NOT NULL, expected_edge REAL NOT NULL,
  time_in_force TEXT NOT NULL CHECK(time_in_force='FOK'),
  quote_json TEXT NOT NULL, quote_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('reserved','filled','rejected','expired','unknown')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  failure_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS trade_intents_active_prediction
  ON trade_intents(prediction_id) WHERE status IN ('reserved','filled','unknown');
CREATE INDEX IF NOT EXISTS trade_intents_status ON trade_intents(status,expires_at);
CREATE TABLE IF NOT EXISTS trade_intent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES trade_intents(id),
  at TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id INTEGER NOT NULL UNIQUE REFERENCES predictions(id),
  market_id TEXT NOT NULL REFERENCES markets(id), event_id TEXT NOT NULL, risk_group TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('YES','NO')), created_at TEXT NOT NULL,
  shares REAL NOT NULL, avg_price REAL NOT NULL, fee REAL NOT NULL,
  total_cost REAL NOT NULL, expected_edge REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', settlement REAL,
  intent_id INTEGER REFERENCES trade_intents(id)
);
CREATE TABLE IF NOT EXISTS paper_execution_audits (
  intent_id INTEGER PRIMARY KEY REFERENCES trade_intents(id),
  method TEXT NOT NULL, captured_at TEXT NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('fillable','rejected')),
  audit_json TEXT NOT NULL, audit_hash TEXT NOT NULL,
  execution_quote_json TEXT, execution_quote_hash TEXT
);
CREATE INDEX IF NOT EXISTS paper_orders_market ON paper_orders(market_id);
CREATE TABLE IF NOT EXISTS paper_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL REFERENCES markets(id),
  event_id TEXT NOT NULL,
  prediction_id INTEGER REFERENCES predictions(id),
  checked_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('paper_filled','skipped')),
  reason TEXT NOT NULL,
  order_id INTEGER REFERENCES paper_orders(id)
);
CREATE INDEX IF NOT EXISTS paper_decisions_event ON paper_decisions(event_id,checked_at);
CREATE TABLE IF NOT EXISTS forecast_book_checks (
    id INTEGER PRIMARY KEY,
    prediction_id INTEGER NOT NULL REFERENCES predictions(id),
    checked_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    side TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    audit_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forecast_book_checks_prediction ON forecast_book_checks(prediction_id,checked_at);
CREATE TABLE IF NOT EXISTS settlement_checks (
  market_id TEXT PRIMARY KEY REFERENCES markets(id), checked_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','verified','error')),
  reason TEXT NOT NULL, source_url TEXT NOT NULL,
  official_side TEXT CHECK(official_side IN ('YES','NO'))
);
CREATE TABLE IF NOT EXISTS risk_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
  cash_available REAL NOT NULL, liquidation_value REAL NOT NULL,
  equity REAL NOT NULL, open_fingerprint TEXT NOT NULL,
  day_start_equity REAL NOT NULL, high_water_equity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_snapshots_captured ON risk_snapshots(captured_at DESC);
CREATE TABLE IF NOT EXISTS risk_days (
  utc_day TEXT PRIMARY KEY, day_start_equity REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_halts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL,
  details TEXT NOT NULL, triggered_at TEXT NOT NULL,
  cleared_at TEXT, clear_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_halts_active_reason
  ON risk_halts(reason) WHERE cleared_at IS NULL;
CREATE TABLE IF NOT EXISTS resolutions (
  market_id TEXT PRIMARY KEY REFERENCES markets(id),
  winning_side TEXT NOT NULL CHECK(winning_side IN ('YES','NO')),
  source_url TEXT NOT NULL, resolved_at TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL REFERENCES markets(id),
  stage TEXT NOT NULL CHECK(stage IN ('analysis','review')),
  model_version TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
  error TEXT
);
CREATE INDEX IF NOT EXISTS model_runs_started ON model_runs(started_at);
CREATE TABLE IF NOT EXISTS model_usage (
  call_id INTEGER PRIMARY KEY REFERENCES model_runs(id),
  received_at TEXT NOT NULL,
  usage_status TEXT NOT NULL CHECK(usage_status IN ('measured','incomplete','invalid')),
  prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
  prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER,
  reasoning_tokens INTEGER, returned_model TEXT,
  cost_lower_usd TEXT, cost_upper_usd TEXT, receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL REFERENCES markets(id),
  rules_hash TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  as_of TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(market_id,rules_hash,evidence_hash,as_of)
);
CREATE INDEX IF NOT EXISTS analysis_drafts_key ON analysis_drafts(market_id,rules_hash,evidence_hash,id DESC);
CREATE TABLE IF NOT EXISTS analysis_market_baselines (
  market_id TEXT NOT NULL REFERENCES markets(id),
  rules_hash TEXT NOT NULL, evidence_hash TEXT NOT NULL, as_of TEXT NOT NULL,
  captured_at TEXT NOT NULL, book_timestamp TEXT NOT NULL,
  yes_best_bid REAL NOT NULL, yes_best_ask REAL NOT NULL,
  PRIMARY KEY(market_id,rules_hash,evidence_hash,as_of)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cycle_leases (
  name TEXT PRIMARY KEY, owner_token TEXT NOT NULL,
  acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycle_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_token TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL CHECK(status IN
    ('running','succeeded','partial','failed','abandoned')),
  config_hash TEXT NOT NULL, prompt_version TEXT NOT NULL,
  model_version TEXT NOT NULL, model_run_id_before INTEGER NOT NULL,
  model_calls INTEGER, watchlist_markets INTEGER,
  evidence_fetched INTEGER, new_evidence_versions INTEGER,
  analysis_attempts INTEGER, paper_checks INTEGER,
  paper_fills INTEGER, error_count INTEGER,
  errors_json TEXT
);
CREATE INDEX IF NOT EXISTS cycle_runs_started ON cycle_runs(started_at DESC);
"""


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _initialize_database(conn)
        # Schema/backfill writes must not hold the writer lock throughout a
        # caller's network requests. Business writes remain transactional.
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if "in_watchlist" not in {row["name"] for row in conn.execute("PRAGMA table_info(markets)")}:
        conn.execute("ALTER TABLE markets ADD COLUMN in_watchlist INTEGER NOT NULL DEFAULT 1")
    market_columns = {row["name"] for row in conn.execute("PRAGMA table_info(markets)")}
    for name, definition in (("category", "TEXT NOT NULL DEFAULT 'macro'"),
                             ("yes_label", "TEXT NOT NULL DEFAULT 'Yes'"),
                             ("no_label", "TEXT NOT NULL DEFAULT 'No'"),
                             ("source_ref", "TEXT")):
        if name not in market_columns:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {name} {definition}")
    if "intent_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(paper_orders)")}:
        conn.execute("ALTER TABLE paper_orders ADD COLUMN intent_id INTEGER REFERENCES trade_intents(id)")
    evidence_columns = {row["name"] for row in conn.execute("PRAGMA table_info(evidence)")}
    for name in ("source_id", "feed_url", "license_scope", "updated_at", "fetched_at",
                 "last_seen_at", "origin_story_id", "feed_published_at"):
        if name not in evidence_columns:
            conn.execute(f"ALTER TABLE evidence ADD COLUMN {name} TEXT")
    for source, source_id, feed_url in OFFICIAL_SOURCE_META:
        scope = "official_public_social" if source_id == "fed_bluesky" else "official_source"
        conn.execute("""UPDATE evidence SET source_id=?, feed_url=?,
            license_scope=?, updated_at=published_at,
            fetched_at=first_seen_at, last_seen_at=first_seen_at,
            origin_story_id=url, feed_published_at=published_at
            WHERE source=? AND source_id IS NULL""",
            (source_id, feed_url, scope, source))
        if source_id == "fed_bluesky":
            conn.execute("""UPDATE evidence SET license_scope='official_public_social'
                WHERE source_id='fed_bluesky' AND license_scope='official_source'""")
        existing = conn.execute("SELECT license_scope FROM source_registry WHERE source_id=?",
                                (source_id,)).fetchone()
        if existing is None or existing["license_scope"] != scope:
            _upsert_source(conn, source_id, source, "official", feed_url,
                           scope,
                           ("https://www.federalreserve.gov/feeds/feeds.htm"
                            if source_id == "fed_bluesky" else feed_url),
                           ("2026-09-28T00:00:00+00:00" if source_id == "fed_bluesky" else ""), 1)
    conn.execute("UPDATE source_registry SET license_checked_at='' WHERE kind='official' AND license_scope='official_source'")
    conn.execute("UPDATE evidence SET feed_published_at=published_at WHERE feed_published_at IS NULL")
    if conn.execute("SELECT 1 FROM meta WHERE key='evidence_digest_v3'").fetchone() is None:
        for row in conn.execute("""SELECT id,title,summary,published_at,updated_at,
                source_id,feed_url,license_scope FROM evidence"""):
            digest = _evidence_digest(row["title"], row["summary"], row["published_at"],
                                      row["updated_at"] or row["published_at"],
                                      row["source_id"] or "unverified", row["feed_url"] or "",
                                      row["license_scope"] or "unverified")
            conn.execute("UPDATE evidence SET content_hash=? WHERE id=?", (digest, row["id"]))
        conn.execute("INSERT INTO meta(key,value) VALUES('evidence_digest_v3',?)", (now_iso(),))
    conn.execute("""INSERT OR IGNORE INTO evidence_current(url,evidence_id,activation_seq,activated_at)
        SELECT e.url,e.id,1,COALESCE(e.last_seen_at,e.first_seen_at)
        FROM evidence e JOIN (SELECT url,MAX(id) id FROM evidence GROUP BY url) latest
        ON latest.id=e.id""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS paper_orders_intent ON paper_orders(intent_id)")
@contextmanager
def read_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing initialized database without schema or data writes."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def upsert_markets(conn: sqlite3.Connection, markets: list[dict], reset_watchlist: bool = True) -> None:
    timestamp = now_iso()
    if reset_watchlist:
        conn.execute("UPDATE markets SET in_watchlist=0 WHERE in_watchlist=1")
    for market in markets:
        conn.execute("""
        INSERT INTO markets(id,event_id,event_title,event_slug,question,description,end_at,
          yes_token,no_token,liquidity,volume_24h,min_order_size,tick_size,fee_rate,
          category,yes_label,no_label,source_ref,discovered_at,updated_at,in_watchlist)
        VALUES(:id,:event_id,:event_title,:event_slug,:question,:description,:end_at,
          :yes_token,:no_token,:liquidity,:volume_24h,:min_order_size,:tick_size,:fee_rate,
          :category,:yes_label,:no_label,:source_ref,:discovered_at,:updated_at,1)
        ON CONFLICT(id) DO UPDATE SET
          event_title=excluded.event_title,event_slug=excluded.event_slug,
          question=excluded.question,description=excluded.description,end_at=excluded.end_at,
          yes_token=excluded.yes_token,no_token=excluded.no_token,
          liquidity=excluded.liquidity,volume_24h=excluded.volume_24h,
          min_order_size=excluded.min_order_size,tick_size=excluded.tick_size,
          fee_rate=excluded.fee_rate,category=excluded.category,
          yes_label=excluded.yes_label,no_label=excluded.no_label,
          source_ref=excluded.source_ref,updated_at=excluded.updated_at,in_watchlist=1
        """, {"category": "macro", "yes_label": "Yes", "no_label": "No",
               "source_ref": None, **market, "discovered_at": timestamp,
               "updated_at": timestamp})


OFFICIAL_SOURCE_META = (
    ("Federal Reserve", "fed_monetary", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve Speeches", "fed_speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("Federal Reserve Testimony", "fed_testimony", "https://www.federalreserve.gov/feeds/testimony.xml"),
    ("Federal Reserve Bluesky", "fed_bluesky",
     "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=did:plc:d3vmaxc5ytftnf4gwdwptupg&filter=posts_no_replies&limit=30"),
    ("BLS CPI", "bls_cpi", "https://www.bls.gov/feed/cpi.rss"),
    ("BLS Employment", "bls_employment", "https://www.bls.gov/feed/empsit.rss"),
    ("BEA Releases", "bea_releases", "https://apps.bea.gov/rss/rss.xml"),
    ("SEC Press", "sec_press", "https://www.sec.gov/rss/news/press.xml"),
    ("CFTC General", "cftc_general", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml"),
    ("White House Presidential Actions", "white_house_actions",
     "https://www.whitehouse.gov/presidential-actions/feed/"),
)


def _upsert_source(conn: sqlite3.Connection, source_id: str, source: str, kind: str,
                   feed_url: str, license_scope: str, license_reference: str,
                   license_checked_at: str, enabled: int) -> None:
    timestamp = now_iso()
    conn.execute("""INSERT INTO source_registry(source_id,source,kind,feed_url,
        license_scope,license_reference,license_checked_at,enabled,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
        source=excluded.source,kind=excluded.kind,feed_url=excluded.feed_url,
        license_scope=excluded.license_scope,license_reference=excluded.license_reference,
        license_checked_at=excluded.license_checked_at,enabled=excluded.enabled,
        last_seen_at=excluded.last_seen_at""",
        (source_id, source, kind, feed_url, license_scope, license_reference,
         license_checked_at, enabled, timestamp, timestamp))


def sync_news_sources(conn: sqlite3.Connection, sources: list[dict]) -> None:
    """Removing a configured licence disables all prior items from that source."""
    conn.execute("UPDATE source_registry SET enabled=0 WHERE kind='news'")
    for source in sources:
        _upsert_source(conn, source["source_id"], source["source"], "news",
                       source["feed_url"], source["license_scope"],
                       source["license_reference"], source["license_checked_at"], 1)


def _evidence_digest(title: str, summary: str, published_at: str, updated_at: str,
                     source_id: str, feed_url: str, license_scope: str) -> str:
    return hashlib.sha256("\n".join((title, summary, published_at, updated_at,
                                     source_id, feed_url, license_scope)).encode("utf-8")).hexdigest()


def _observe_event_evidence(conn: sqlite3.Connection, evidence_id: int, item: dict, observed_at: str) -> None:
    """New receipt-time ledger only; never infer observations for legacy rows.

    Store the currently observed origin and public source descriptor as well as
    immutable content. Repeated observations and A->B->A revisions all survive.
    High-rate exchange/price snapshots have their separate raw capture protocol.
    """
    row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    source_id = row["source_id"] or "unverified"
    if source_id not in {entry[1] for entry in OFFICIAL_SOURCE_META} | {"mlb_schedule", "mlb_pregame_stats", "fed_policy_history"} and not source_id.startswith("news_"):
        return
    source = conn.execute("SELECT * FROM source_registry WHERE source_id=?", (source_id,)).fetchone()
    snapshot = dict(row)
    snapshot["origin_story_id"] = item.get("origin_story_id") or item["url"]
    snapshot["feed_published_at"] = item.get("feed_published_at") or item["published_at"]
    if source_id in ('mlb_pregame_stats', 'fed_policy_history'):
        snapshot['capture_descriptor'] = item.get('capture_descriptor')
    value = {"schema_version": 1, "protocol": "event-evidence-observation-v1",
             "observed_at": observed_at, "evidence": snapshot,
             "source_descriptor": dict(source) if source else None,
             "historical_backfill": False}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    conn.execute("""INSERT OR IGNORE INTO evidence_observations
        (evidence_id,observed_at,observation_json,observation_sha256) VALUES(?,?,?,?)""",
        (evidence_id, observed_at, raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()))


def insert_evidence(conn: sqlite3.Connection, items: list[dict], *, activate_current: bool = True) -> int:
    """Store immutable observations; research supplements may avoid replacing the live URL view."""
    if type(activate_current) is not bool:
        raise ValueError('Invalid current evidence activation policy')
    count = 0
    for item in items:
        official = next((entry for entry in OFFICIAL_SOURCE_META if entry[0] == item["source"]), None)
        source_id = item.get("source_id") or (official[1] if official else "unverified")
        feed_url = item.get("feed_url") or (official[2] if official else "")
        license_scope = item.get("license_scope") or (
            "official_public_social" if source_id == "fed_bluesky" else
            "official_source" if official else "unverified")
        updated_at = item.get("updated_at") or item["published_at"]
        origin_story_id = item.get("origin_story_id") or item["url"]
        timestamp = now_iso()
        seen_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        # A freshly captured facts cutoff can contain fractional seconds. Its
        # first observation must not be rounded to a time before those facts.
        first_seen = seen_at if source_id in ('mlb_pregame_stats', 'fed_policy_history') else timestamp
        digest = _evidence_digest(item["title"], item["summary"], item["published_at"],
                                  updated_at, source_id, feed_url, license_scope)
        cursor = conn.execute("""INSERT OR IGNORE INTO evidence
            (source,url,title,summary,published_at,first_seen_at,content_hash,
             source_id,feed_url,license_scope,updated_at,fetched_at,last_seen_at,
             origin_story_id,feed_published_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item["source"], item["url"], item["title"], item["summary"],
             item["published_at"], first_seen, digest, source_id, feed_url,
             license_scope, updated_at, seen_at, seen_at, origin_story_id,
             item.get("feed_published_at") or item["published_at"]))
        if not cursor.rowcount:
            conn.execute("UPDATE evidence SET last_seen_at=? WHERE url=? AND content_hash=?",
                         (seen_at, item["url"], digest))
        evidence_id = int(conn.execute("SELECT id FROM evidence WHERE url=? AND content_hash=?",
                                       (item["url"], digest)).fetchone()[0])
        if activate_current:
            current = conn.execute("SELECT evidence_id,activation_seq FROM evidence_current WHERE url=?",
                                   (item["url"],)).fetchone()
            if current is None:
                conn.execute("""INSERT INTO evidence_current(url,evidence_id,activation_seq,activated_at)
                    VALUES(?,?,1,?)""", (item["url"], evidence_id, seen_at))
            elif current["evidence_id"] != evidence_id:
                conn.execute("""UPDATE evidence_current SET evidence_id=?,activation_seq=?,
                    activated_at=? WHERE url=?""",
                    (evidence_id, current["activation_seq"] + 1, seen_at, item["url"]))
        _observe_event_evidence(conn, evidence_id, item, seen_at)
        count += cursor.rowcount
    return count


def withdraw_evidence(conn: sqlite3.Connection, url: str, source_url: str, reason: str) -> bool:
    from urllib.parse import urlparse
    if (urlparse(url).scheme != "https" or not urlparse(url).netloc or
            urlparse(source_url).scheme != "https" or not urlparse(source_url).netloc or
            len(reason.strip()) < 10):
        raise ValueError("Withdrawal requires HTTPS article/source URLs and a specific reason")
    if conn.execute("SELECT 1 FROM evidence WHERE url=?", (url,)).fetchone() is None:
        raise ValueError("Evidence URL is not in the archive")
    cursor = conn.execute("""INSERT OR IGNORE INTO evidence_withdrawals
        (url,source_url,reason,recorded_at) VALUES(?,?,?,?)""",
        (url, source_url, reason.strip(), now_iso()))
    return bool(cursor.rowcount)


def get_market(conn: sqlite3.Connection, market_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM markets WHERE id=? AND in_watchlist=1", (market_id,)).fetchone()
    if row is None:
        raise KeyError(f"Market {market_id} is not in the local watchlist; run scan first")
    return row


def _crypto_news(conn: sqlite3.Connection, as_of: str, lookback_hours: int,
                 limit: int = 3) -> list[sqlite3.Row]:
    """Current, authorised announcements available by the forecast cutoff."""
    candidates = conn.execute("""SELECT e.*,c.activation_seq FROM evidence e
        JOIN evidence_current c ON c.url=e.url AND c.evidence_id=e.id
        JOIN source_registry s ON s.source_id=e.source_id
        WHERE (s.source_id IN ('sec_press','cftc_general','white_house_actions')
               OR (s.kind='news' AND e.source LIKE 'News Crypto:%'))
        AND s.enabled=1 AND s.license_scope=e.license_scope
        AND (s.kind='official' OR s.feed_url=e.feed_url)
        AND julianday(e.published_at)>=julianday(?)-?/24.0
        AND julianday(e.published_at)<=julianday(e.first_seen_at)
        AND julianday(e.first_seen_at)<=julianday(?)
        AND julianday(c.activated_at)<=julianday(?)
        AND (LOWER(e.title) LIKE '%bitcoin%' OR LOWER(e.title) LIKE '%crypto%'
             OR LOWER(e.title) LIKE '%digital asset%' OR LOWER(e.title) LIKE '%blockchain%')
        AND NOT EXISTS (SELECT 1 FROM evidence_withdrawals w WHERE w.url=e.url)
        ORDER BY julianday(e.published_at) DESC,e.id DESC""",
        (as_of, lookback_hours, as_of, as_of)).fetchall()
    seen: set[str] = set()
    result: list[sqlite3.Row] = []
    for item in candidates:
        origin = item["origin_story_id"] or item["url"]
        if origin in seen:
            continue
        seen.add(origin)
        result.append(item)
        if len(result) == limit:
            break
    return result


def evidence_for_market(conn: sqlite3.Connection, market: sqlite3.Row, limit: int = 14,
                        *, as_of: str | None = None) -> list[sqlite3.Row]:
    if market["category"] in ("crypto", "crypto5m", "daily"):
        source_ids = {"crypto": ("binance_klines",),
                      "crypto5m": ("chainlink_btc_twap60", "coinbase_btc_5m"),
                      "daily": ("mlb_schedule",)}[market["category"]]
        placeholders = ",".join("?" for _ in source_ids)
        price = conn.execute(f"""SELECT e.*,c.activation_seq FROM evidence e
            JOIN evidence_current c ON c.url=e.url AND c.evidence_id=e.id
            JOIN source_registry s ON s.source_id=e.source_id
            WHERE e.source_id IN ({placeholders}) AND e.origin_story_id=? AND s.enabled=1
            AND s.license_scope=e.license_scope
            AND NOT EXISTS (SELECT 1 FROM evidence_withdrawals w WHERE w.url=e.url)
            ORDER BY e.published_at DESC,e.id DESC LIMIT ?""",
            (*source_ids, market["source_ref"], len(source_ids))).fetchall()
        if market["category"] == "daily":
            return price
        cutoff = as_of or datetime.now(timezone.utc).isoformat()
        status = []
        if market["category"] == "crypto5m":
            status = conn.execute("""SELECT e.*,c.activation_seq FROM evidence e
                JOIN evidence_current c ON c.url=e.url AND c.evidence_id=e.id
                JOIN source_registry s ON s.source_id=e.source_id
                WHERE e.source_id='coinbase_exchange_status' AND e.origin_story_id=?
                AND s.enabled=1 AND s.license_scope=e.license_scope AND s.feed_url=e.feed_url
                AND julianday(e.published_at)<=julianday(?)
                AND julianday(e.first_seen_at)<=julianday(?)
                AND julianday(e.fetched_at)<=julianday(?)
                AND julianday(c.activated_at)<=julianday(?)
                AND julianday(e.published_at)>=julianday(?)-120.0/86400
                AND NOT EXISTS (SELECT 1 FROM evidence_withdrawals w WHERE w.url=e.url)
                ORDER BY e.id DESC LIMIT 1""",
                (market["source_ref"], cutoff, cutoff, cutoff, cutoff, cutoff)).fetchall()
        # Five-minute news is a short risk context, not a calibrated price signal.
        hours = 24 if market["category"] == "crypto5m" else 7 * 24
        return price + status + _crypto_news(conn, cutoff, hours)
    q = (market["event_title"] + " " + market["question"]).lower()
    is_fed = any(x in q for x in ("fed", "fomc", "powell", "rate cut", "rate hike"))
    is_bls = any(x in q for x in ("cpi", "inflation", "unemployment", "payroll", "jobs"))
    sources: list[str] = []
    if is_fed:
        sources.extend(("Federal Reserve", "Federal Reserve Speeches", "Federal Reserve Testimony",
                        "Federal Reserve Bluesky", "BLS CPI",
                        "BLS Employment", "BEA Releases"))
    if is_bls:
        sources.extend(("BLS CPI", "BLS Employment", "BEA Releases"))
    sources = list(dict.fromkeys(sources))
    if not sources:
        return []
    placeholders = ",".join("?" for _ in sources)
    patterns = ["News Macro:%"]
    if is_fed:
        patterns.append("News Fed:%")
    news_clause = " OR ".join("e.source LIKE ?" for _ in patterns)
    candidates = conn.execute(f"""SELECT e.*,c.activation_seq FROM evidence e
        JOIN evidence_current c ON c.url=e.url AND c.evidence_id=e.id
        JOIN source_registry s ON s.source_id=e.source_id
        WHERE s.enabled=1 AND s.license_scope=e.license_scope
        AND (s.kind='official' OR s.feed_url=e.feed_url)
        AND NOT EXISTS (SELECT 1 FROM evidence_withdrawals w WHERE w.url=e.url)
        AND (e.source IN ({placeholders}) OR {news_clause})
        ORDER BY e.published_at DESC,e.id DESC LIMIT ?""",
                              (*sources, *patterns, limit * 8)).fetchall()
    seen: set[str] = set()
    unique: list[sqlite3.Row] = []
    remarks_count = 0
    original_origins = {row["origin_story_id"] or row["url"] for row in candidates
                        if row["source_id"] != "fed_bluesky"}
    social: list[sqlite3.Row] = []
    for row in candidates:
        if row["source_id"] == "fed_bluesky":
            title = row["title"].lower()
            origin = row["origin_story_id"] or row["url"]
            official_release = "/newsevents/pressreleases/monetary" in origin
            relevant_speech = ("/newsevents/speech/" in origin and
                               any(term in title for term in (
                                   "economic outlook", "monetary policy", "inflation",
                                   "interest rate", "employment", "labor market", "fomc")))
            firsthand = (origin.startswith("at://") and
                         any(term in title for term in (
                             "fomc statement", "rate decision", "target range",
                             "federal funds rate")))
            if origin not in original_origins and (official_release or relevant_speech or firsthand):
                social.append(row)
            continue
        if row["source_id"] in ("fed_speeches", "fed_testimony"):
            title = row["title"].lower()
            if remarks_count >= 3 or not any(term in title for term in (
                    "economic outlook", "monetary policy", "inflation", "interest rate",
                    "employment", "labor market", "price stability", "fomc",
                    "federal funds", "shelter")):
                continue
        origin = row["origin_story_id"] or row["url"]
        if origin in seen:
            continue
        seen.add(origin)
        unique.append(row)
        if row["source_id"] in ("fed_speeches", "fed_testimony"):
            remarks_count += 1
        if len(unique) == limit:
            break
    if social and limit >= 4:
        if len(unique) == limit:
            unique.pop()
        unique.append(social[0])
    return unique


def digest_text(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def save_prediction(conn: sqlite3.Connection, market_id: str, rules_hash: str,
                    evidence_hash: str, result: dict) -> int:
    prob = result.get("p_yes")
    abstain = bool(result.get("abstain"))
    if not abstain and (isinstance(prob, bool) or not isinstance(prob, (int, float)) or not 0 <= prob <= 1):
        raise ValueError("Codex returned invalid probability")
    conn.execute("""INSERT OR IGNORE INTO predictions(market_id,as_of,rules_hash,evidence_hash,
        probability,abstain,reasoning,assumptions_json,supports_json,counters_json,model_output_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (market_id, result.get("as_of") or now_iso(), rules_hash, evidence_hash,
         None if abstain else float(prob),
         int(abstain), str(result.get("reasoning") or ""),
         json.dumps(result.get("assumptions") or [], ensure_ascii=False),
         json.dumps(result.get("supporting_evidence_ids") or [], ensure_ascii=False),
         json.dumps(result.get("counter_evidence_ids") or [], ensure_ascii=False),
         json.dumps(result, ensure_ascii=False)))
    row = conn.execute("SELECT id FROM predictions WHERE market_id=? AND rules_hash=? AND evidence_hash=?",
                       (market_id, rules_hash, evidence_hash)).fetchone()
    return int(row["id"])


def save_market_baseline(conn: sqlite3.Connection, prediction_id: int,
                         captured_at: str, book_timestamp: str,
                         yes_best_bid: float, yes_best_ask: float) -> None:
    if not 0 < yes_best_bid <= yes_best_ask < 1:
        raise ValueError("Invalid market baseline spread")
    conn.execute("""INSERT OR IGNORE INTO market_baselines
        (prediction_id,captured_at,book_timestamp,yes_best_bid,yes_best_ask,p_yes_mid)
        VALUES(?,?,?,?,?,?)""",
        (prediction_id, captured_at, book_timestamp, yes_best_bid,
         yes_best_ask, (yes_best_bid + yes_best_ask) / 2))


def save_analysis_market_baseline(conn: sqlite3.Connection, market_id: str,
                                  rules_hash: str, evidence_hash: str,
                                  as_of: str, captured_at: str,
                                  book_timestamp: str, bid: float, ask: float) -> None:
    forecast_time = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    capture_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    if (not 0 < bid <= ask < 1 or
            not 0 <= (forecast_time - capture_time).total_seconds() <= 10):
        raise ValueError("Invalid pre-analysis market baseline")
    conn.execute("""INSERT OR IGNORE INTO analysis_market_baselines
        (market_id,rules_hash,evidence_hash,as_of,captured_at,book_timestamp,
         yes_best_bid,yes_best_ask) VALUES(?,?,?,?,?,?,?,?)""",
        (market_id, rules_hash, evidence_hash, as_of, captured_at,
         book_timestamp, bid, ask))


def analysis_market_baseline(conn: sqlite3.Connection, market_id: str,
                             rules_hash: str, evidence_hash: str, as_of: str) -> sqlite3.Row | None:
    return conn.execute("""SELECT * FROM analysis_market_baselines
        WHERE market_id=? AND rules_hash=? AND evidence_hash=? AND as_of=?""",
        (market_id, rules_hash, evidence_hash, as_of)).fetchone()


def balance(conn: sqlite3.Connection, initial_cash: float) -> dict:
    row = conn.execute("""SELECT COALESCE(SUM(CASE WHEN status IN ('open','settled') THEN total_cost ELSE 0 END),0) spent,
        COALESCE(SUM(CASE WHEN status='settled' THEN shares*settlement ELSE 0 END),0) payout,
        COALESCE(SUM(CASE WHEN status='open' THEN total_cost ELSE 0 END),0) open_cost,
        COALESCE(SUM(CASE WHEN status='settled' THEN shares*settlement-total_cost ELSE 0 END),0) realized_pnl,
        COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0) open_orders
        FROM paper_orders""").fetchone()
    cash = initial_cash - float(row["spent"]) + float(row["payout"])
    reserved = float(conn.execute("""SELECT COALESCE(SUM(max_total_cost),0)
        FROM trade_intents WHERE status IN ('reserved','unknown')""").fetchone()[0])
    return {"initial_cash": initial_cash, "open_cost": float(row["open_cost"]),
            "cash_available": cash, "reserved_cost": reserved,
            "cash_unreserved": cash - reserved,
            "realized_pnl": float(row["realized_pnl"]),
            "open_orders": int(row["open_orders"])}


def model_calls_today(conn: sqlite3.Connection, provider: str = "codex") -> int:
    day_start = datetime.now(timezone.utc).date().isoformat() + "T00:00:00+00:00"
    if provider not in ("codex", "deepseek"):
        raise ValueError("Unsupported model provider")
    clause = "LIKE" if provider == "deepseek" else "NOT LIKE"
    return int(conn.execute(f"SELECT COUNT(*) FROM model_runs WHERE started_at>=? "
                            f"AND model_version {clause} 'deepseek-%'",
                            (day_start,)).fetchone()[0])


def model_pause_until(conn: sqlite3.Connection, provider: str = "codex") -> str | None:
    if provider not in ("codex", "deepseek"):
        raise ValueError("Unsupported model provider")
    key = "model_pause_until" if provider == "codex" else "model_pause_until_deepseek"
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def save_analysis_draft(conn: sqlite3.Connection, market_id: str, rules_hash: str,
                        evidence_hash: str, payload: dict, result: dict) -> None:
    conn.execute("""INSERT OR IGNORE INTO analysis_drafts
        (market_id,rules_hash,evidence_hash,as_of,payload_json,result_json,created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (market_id, rules_hash, evidence_hash, payload["as_of"],
         json.dumps(payload, ensure_ascii=False), json.dumps(result, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat(timespec="microseconds")))


def latest_analysis_draft(conn: sqlite3.Connection, market_id: str,
                          rules_hash: str, evidence_hash: str) -> sqlite3.Row | None:
    return conn.execute("""SELECT * FROM analysis_drafts
        WHERE market_id=? AND rules_hash=? AND evidence_hash=? ORDER BY id DESC LIMIT 1""",
        (market_id, rules_hash, evidence_hash)).fetchone()


def reserve_model_call(conn: sqlite3.Connection, market_id: str, stage: str,
                       model_version: str, daily_limit: int,
                       provider: str = "codex") -> int:
    if stage not in ("analysis", "review"):
        raise ValueError("Invalid model stage")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        paused = model_pause_until(conn, provider)
        if paused and paused > now_iso():
            raise RuntimeError(f"{provider.title()} provider paused until {paused}")
        if paused:
            conn.execute("DELETE FROM meta WHERE key=?", (
                "model_pause_until" if provider == "codex" else "model_pause_until_deepseek",))
        if model_calls_today(conn, provider) >= daily_limit:
            raise RuntimeError(f"Daily {provider.title()} call limit reached")
        cursor = conn.execute("""INSERT INTO model_runs(market_id,stage,model_version,started_at,status)
            VALUES(?,?,?,?,?)""", (market_id, stage, model_version, now_iso(), "running"))
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        raise


def finish_model_call(conn: sqlite3.Connection, call_id: int, error: str | None = None,
                      pause_until: str | None = None, provider: str = "codex") -> None:
    conn.execute("""UPDATE model_runs SET finished_at=?,status=?,error=?
        WHERE id=? AND status='running'""",
        (now_iso(), "failed" if error else "succeeded", (error or "")[:500] if error else None, call_id))
    if pause_until:
        if provider not in ("codex", "deepseek"):
            raise ValueError("Unsupported model provider")
        key = "model_pause_until" if provider == "codex" else "model_pause_until_deepseek"
        conn.execute("""INSERT INTO meta(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, pause_until))
