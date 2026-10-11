"""Official FOMC policy history captured now, never backdated as live input.

Only public calendar-linked statements are requested. Historical statement
content is training/reference material; its local availability is the receipt
time. The frequency baseline is an unvalidated research comparator.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polycast.evidence import ArticleText
from polycast.market import FED_OUTCOME_SLOTS

VERSION = 'official-fomc-history-v2'
BASELINE_VERSION = 'scheduled-fomc-frequency-v2'
ORIGIN = 'https://www.federalreserve.gov'
CALENDAR = ORIGIN + '/monetarypolicy/fomccalendars.htm'
RATE_TABLE = ORIGIN + '/monetarypolicy/openmarket.htm'
MAX_BYTES = 4 * 1024 ** 2
MAX_STATEMENTS = 64
MIN_BASELINE_EVENTS = 32
LOOKBACK_EVENTS = 40
SMOOTHING = 1.0
NUMBER = r'(?:\d+[- ]\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)'
RANGE = re.compile(
    r'\b(?:decided|voted) to (maintain|keep|raise|lower|reduce|increase) the target range for the federal funds rate '
    r'(?:by (' + NUMBER + r') percentage points? )?(?:at|to) (' + NUMBER + r') to (' + NUMBER + r') percent\b', re.I)
STATEMENT_PATH = re.compile(r'/newsevents/pressreleases/monetary(\d{8})a\.htm')
MONTHS = {name: n for n, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'), 1)}


def utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone-aware macro availability is required')
    return result.astimezone(timezone.utc)


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalize(value: str) -> str:
    value = re.sub(r'(?<=\d)([¼½¾])', r'-\1', value)
    for old, new in (('¼', '1/4'), ('½', '1/2'), ('¾', '3/4'), ('–', '-'), ('−', '-'),
                     ('‑', '-'), ('‐', '-'), ('\u00a0', ' ')):
        value = value.replace(old, new)
    return re.sub(r'\s+', ' ', value).strip()


def percent_bps(value: str) -> Fraction:
    parts = re.fullmatch(r'(\d+)[- ](\d+/\d+)', value)
    result = Fraction(parts[1]) + Fraction(parts[2]) if parts else Fraction(value)
    if not 0 <= result <= 30:
        raise ValueError('Unsupported policy percentage')
    return result * 100


def number(value: Fraction) -> int | float:
    return int(value) if value.denominator == 1 else float(value)


def outcome_slot(change_bps: int | float) -> str:
    if type(change_bps) not in (int, float) or not -3000 <= change_bps <= 3000:
        raise ValueError('Invalid policy change')
    if change_bps == 0:
        return 'no_change'
    direction = 'increase' if change_bps > 0 else 'decrease'
    # Contract rounds the magnitude up to the next 25 bps.
    return direction + ('_25' if abs(change_bps) <= 25 else '_50_plus')


class CalendarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.div_depth = 0
        self.heading = None
        self.year = None
        self.row = None
        self.row_depth = None
        self.field = None
        self.field_depth = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h4': self.heading = []
        if tag == 'div':
            self.div_depth += 1
            classes = (attrs.get('class') or '').split()
            if 'fomc-meeting' in classes:
                if self.row is not None: raise ValueError('Nested FOMC calendar rows')
                self.row = {'year': self.year, 'month': '', 'days': '', 'statement_urls': [], 'text': ''}
                self.row_depth = self.div_depth
            if self.row is not None:
                for field, classname in (('month', 'fomc-meeting__month'), ('days', 'fomc-meeting__date')):
                    if classname in classes: self.field, self.field_depth = field, self.div_depth
        if tag == 'a' and self.row is not None:
            address = attrs.get('href') or ''
            if STATEMENT_PATH.fullmatch(address): self.row['statement_urls'].append(ORIGIN + address)

    def handle_data(self, data):
        if self.heading is not None: self.heading.append(data)
        if self.row is not None: self.row['text'] += data
        if self.row is not None and self.field: self.row[self.field] += data

    def handle_endtag(self, tag):
        if tag == 'h4' and self.heading is not None:
            match = re.fullmatch(r'(\d{4}) FOMC Meetings', normalize(' '.join(self.heading)))
            if match: self.year = int(match[1])
            self.heading = None
        if tag == 'div':
            if self.field_depth == self.div_depth: self.field = self.field_depth = None
            if self.row_depth == self.div_depth:
                self.rows.append(self.row); self.row = self.row_depth = None
            self.div_depth -= 1


def parse_calendar(raw: bytes) -> list[dict]:
    parser = CalendarParser(); parser.feed(raw.decode('utf-8-sig'))
    records = []
    for row in parser.rows:
        days = normalize(row['days']); month = normalize(row['month'])
        # Strategy-only notation votes have no supported policy decision target.
        if '(notation vote)' in days and 'Statement on Longer-Run Goals and Monetary Policy Strategy' in normalize(row['text']):
            continue
        dm = re.fullmatch(r'(\d{1,2})(?:-(\d{1,2}))?\*?', days)
        last_month = month.split('/')[-1]
        names = [name for name in MONTHS if name.lower().startswith(last_month.lower())]
        if not dm or len(names) != 1 or type(row['year']) is not int:
            raise ValueError('Unknown FOMC meeting date')
        end = date(row['year'], MONTHS[names[0]], int(dm[2] or dm[1])).isoformat()
        urls = list(dict.fromkeys(row['statement_urls']))
        if len(urls) > 1: raise ValueError('Ambiguous FOMC statement link')
        if urls and datetime.strptime(STATEMENT_PATH.fullmatch(urls[0].removeprefix(ORIGIN))[1], '%Y%m%d').date().isoformat() != end:
            raise ValueError('FOMC calendar date and statement link disagree')
        records.append({'meeting_date': end, 'calendar_label': month+' '+days, 'statement_url': urls[0] if urls else None})
    records.sort(key=lambda r: r['meeting_date'])
    if not records or len({r['meeting_date'] for r in records}) != len(records):
        raise ValueError('Missing/duplicate FOMC calendar')
    return records


def parse_statement(raw: bytes, address: str) -> dict:
    path = STATEMENT_PATH.fullmatch(address.removeprefix(ORIGIN))
    if not path: raise ValueError('Unsupported official statement URL')
    parser = ArticleText('article'); parser.feed(raw.decode('utf-8-sig'))
    text = normalize(' '.join(parser.parts))
    if not re.search(r'Federal Reserve issues FOMC statement', text[:700], re.I):
        raise ValueError('Not an official FOMC policy statement')
    dates = re.findall(r'\b([A-Z][a-z]+ \d{1,2}, \d{4})\b', text[:250])
    release = re.search(r'For release at (\d{1,2}):(\d{2}) ([ap])\.m\. (EDT|EST)', text[:700], re.I)
    if not dates or not release: raise ValueError('Unverified official statement release time')
    day = datetime.strptime(dates[0], '%B %d, %Y')
    if day.strftime('%Y%m%d') != path[1]: raise ValueError('Statement date and URL disagree')
    hour = int(release[1]); minute = int(release[2])
    if not 1 <= hour <= 12 or not 0 <= minute < 60: raise ValueError('Invalid policy release clock')
    local = day.replace(hour=hour % 12 + (12 if release[3].lower() == 'p' else 0), minute=minute,
                        tzinfo=ZoneInfo('America/New_York'))
    if local.tzname() != release[4].upper(): raise ValueError('Policy release timezone differs from date')
    matches = list(RANGE.finditer(text))
    if len(matches) != 1: raise ValueError('Missing/ambiguous enacted target range')
    match = matches[0]; action, by, low, high = match.groups()
    lower, upper = percent_bps(low), percent_bps(high)
    if not 0 < upper-lower <= 100: raise ValueError('Invalid target interval')
    action = action.lower()
    change = Fraction(0) if action in ('maintain', 'keep') else (
        percent_bps(by) * (-1 if action in ('lower', 'reduce') else 1) if by else None)
    if change is not None and by and change == 0: raise ValueError('Zero enacted raise/cut')
    if action in ('maintain', 'keep') and by: raise ValueError('Contradictory maintain action')
    return {'meeting_date': day.date().isoformat(), 'url': address, 'released_at': local.astimezone(timezone.utc).isoformat(),
            'lower_bps': number(lower), 'upper_bps': number(upper), 'action': action,
            'explicit_change_bps': number(change) if change is not None else None,
            'policy_sentence': match[0], 'publication_time_source': 'official_statement_header'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def fetch(root: Path, name: str, address: str) -> dict:
    if address not in (CALENDAR, RATE_TABLE) and not (address.startswith(ORIGIN) and STATEMENT_PATH.fullmatch(address.removeprefix(ORIGIN))):
        raise ValueError('Only calendar-linked official policy URLs are allowed')
    receipt = {'url': address, 'sent_at': datetime.now(timezone.utc).isoformat()}
    started = time.monotonic_ns()
    try:
        request = urllib.request.Request(address, headers={'Accept': 'text/html',
            'User-Agent': 'PolycastResearch/1.0 (public official source capture)'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=12) as response:
            raw = response.read(MAX_BYTES+1)
            receipt.update(http_status=response.status, response_date=response.headers.get('Date'))
        receipt.update(received_at=datetime.now(timezone.utc).isoformat(), elapsed_ms=(time.monotonic_ns()-started)/1e6,
                       raw_filename=name+'.raw.html', raw_bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest())
        with (root/receipt['raw_filename']).open('xb') as stream: stream.write(raw)
        if len(raw) > MAX_BYTES or receipt['http_status'] != 200: raise ValueError('Non-success/oversized FOMC response')
    except Exception as exc:
        receipt.update(received_at=datetime.now(timezone.utc).isoformat(), error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        with (root/(name+'.receipt.json')).open('x', encoding='utf-8') as stream: stream.write(encoded(receipt)+'\n')
    return receipt


def read_response(root: Path, name: str, expected_url: str, as_of: datetime) -> tuple[bytes, dict]:
    path = root/(name+'.receipt.json')
    if path.is_symlink() or path.resolve().parent != root.resolve() or path.stat().st_size > 65536:
        raise ValueError('Invalid FOMC receipt path/size')
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if (receipt.get('error') or receipt.get('http_status') != 200 or receipt.get('url') != expected_url or
            receipt.get('raw_filename') != name+'.raw.html'):
        raise ValueError('Invalid official FOMC request receipt')
    path = root/receipt['raw_filename']
    if path.is_symlink() or path.resolve().parent != root.resolve() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Invalid FOMC response path/size')
    raw = path.read_bytes()
    if len(raw) != receipt['raw_bytes'] or hashlib.sha256(raw).hexdigest() != receipt['raw_sha256']:
        raise ValueError('Official FOMC response changed')
    if not utc(receipt['sent_at']) <= utc(receipt['received_at']) <= as_of:
        raise ValueError('FOMC response unavailable at cutoff')
    return raw, receipt


class RateTableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.heading = None; self.year = None
        self.in_table = False; self.row = None; self.cell = None; self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == 'h4': self.heading = []
        if tag == 'table': self.in_table = True
        if self.in_table and tag == 'tr': self.row = []
        if self.row is not None and tag in ('td', 'th'): self.cell = []

    def handle_data(self, data):
        if self.heading is not None: self.heading.append(data)
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'h4' and self.heading is not None:
            heading = normalize(' '.join(self.heading))
            if re.fullmatch(r'\d{4}', heading): self.year = int(heading)
            self.heading = None
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(normalize(' '.join(self.cell))); self.cell = None
        if tag == 'tr' and self.row is not None:
            if self.year and self.year >= 2015 and self.row and self.row[0] != 'Date':
                self.rows.append((self.year, self.row))
            self.row = None
        if tag == 'table': self.in_table = False


def parse_rate_table(raw: bytes) -> list[dict]:
    parser = RateTableParser(); parser.feed(raw.decode('utf-8-sig')); result = []
    for year, cells in parser.rows:
        if len(cells) != 4: raise ValueError('Unknown official policy table schema')
        # The official table marks a corrected 2020 date with a footnote star.
        day = datetime.strptime(re.sub(r'\s*\*$', '', cells[0]), '%B %d').replace(year=year).date().isoformat()
        increase, decrease = Fraction(cells[1]), Fraction(cells[2])
        level = re.fullmatch(r'(' + NUMBER + r')\s*-\s*(' + NUMBER + r')', cells[3])
        if not level or increase < 0 or decrease < 0 or (increase > 0) == (decrease > 0):
            raise ValueError('Invalid official policy change row')
        low, high = percent_bps(level[1]), percent_bps(level[2])
        if not 0 < high-low <= 100: raise ValueError('Invalid official table range')
        result.append({'effective_date': day, 'lower_bps': number(low), 'upper_bps': number(high),
                       'change_bps': number(increase-decrease)})
    result.sort(key=lambda r: r['effective_date'])
    if not result or len({r['effective_date'] for r in result}) != len(result):
        raise ValueError('Missing/duplicate official policy change table')
    for previous, current in zip(result, result[1:]):
        if (current['upper_bps']-previous['upper_bps'] != current['change_bps'] or
                current['lower_bps']-previous['lower_bps'] != current['change_bps']):
            raise ValueError('Official table levels and stated changes disagree')
    return result


def reconcile_history(statements: list[dict], changes: list[dict]) -> None:
    """Match effective-date rows, without turning them into release clocks."""
    used = set()
    for statement in statements:
        day = date.fromisoformat(statement['meeting_date'])
        matches = [row for row in changes if 0 <= (date.fromisoformat(row['effective_date'])-day).days <= 1
                   and row['lower_bps'] == statement['lower_bps'] and row['upper_bps'] == statement['upper_bps']]
        if statement['action'] in ('keep', 'maintain'):
            if matches: raise ValueError('Maintained policy conflicts with official change table')
            previous = [row for row in changes if row['effective_date'] <= statement['meeting_date']]
            if not previous or (previous[-1]['lower_bps'], previous[-1]['upper_bps']) != (statement['lower_bps'], statement['upper_bps']):
                raise ValueError('Maintained policy range lacks an official table anchor')
            statement['enacted_change_bps'] = 0
            statement['change_source'] = 'statement_maintain_crosschecked_official_table'
            statement['effective_date'] = None
        else:
            if len(matches) != 1: raise ValueError('Policy action lacks one matching official table change')
            row = matches[0]; delta = row['change_bps']
            if (delta < 0) != (statement['action'] in ('lower', 'reduce')):
                raise ValueError('Policy action direction conflicts with official table')
            if statement['explicit_change_bps'] is not None and statement['explicit_change_bps'] != delta:
                raise ValueError('Enacted change magnitude conflicts with official table')
            used.add(row['effective_date']); statement['enacted_change_bps'] = delta
            statement['change_source'] = 'statement_and_official_change_table'
            statement['effective_date'] = row['effective_date']
    if statements:
        first = date.fromisoformat(statements[0]['meeting_date'])
        unmatched = [row for row in changes if first <= date.fromisoformat(row['effective_date'])
                     and row['effective_date'] not in used]
        if unmatched: raise ValueError('Unlisted intermeeting changes require explicit label handling')


def build_facts(root: Path, as_of: datetime) -> dict:
    if as_of.tzinfo is None: raise ValueError('Naive FOMC cutoff')
    raw, receipt = read_response(root, 'calendar', CALENDAR, as_of)
    meetings = parse_calendar(raw); statements = []; receipts = [receipt]
    linked = [m for m in meetings if m['statement_url'] and date.fromisoformat(m['meeting_date']) <= as_of.date()]
    if len(linked) > MAX_STATEMENTS: raise ValueError('FOMC capture exceeds bounded request count')
    missing = [m['meeting_date'] for m in meetings if not m['statement_url'] and date.fromisoformat(m['meeting_date']) < as_of.date()]
    for meeting in linked:
        raw, r = read_response(root, meeting['meeting_date'], meeting['statement_url'], as_of)
        statement = parse_statement(raw, meeting['statement_url'])
        if utc(statement['released_at']) > utc(r['received_at']): raise ValueError('FOMC statement fetched before publication')
        statement['received_at'] = r['received_at']
        statements.append(statement); receipts.append(r)
    table_raw, table_receipt = read_response(root, 'rate-table', RATE_TABLE, as_of)
    changes = parse_rate_table(table_raw)
    if not statements: raise ValueError('No official policy statements')
    first = statements[0]['meeting_date']
    anchors = [row for row in changes if row['effective_date'] < first]
    if not anchors: raise ValueError('Policy change table has no initial history anchor')
    changes = anchors[-1:] + [row for row in changes if row['effective_date'] >= first]
    reconcile_history(statements, changes); receipts.append(table_receipt)
    for a, b in zip(receipts, receipts[1:]):
        if utc(a['received_at']) > utc(b['sent_at']): raise ValueError('FOMC capture request sequence changed')
    # Historical material is usable only after its real capture, not its header date.
    return {'schema_version': 1, 'version': VERSION, 'as_of': as_of.astimezone(timezone.utc).isoformat(),
            'calendar_url': CALENDAR, 'meetings': meetings, 'statements': statements,
            'source_receipts': receipts, 'missing_past_statement_dates': missing,
            'rate_table_url': RATE_TABLE, 'rate_change_rows': changes,
            'history_vintage': 'current_official_archive_received_now',
            'historical_decision_time_availability_proven': False, 'independent_signature_verified': False,
            'unlisted_intermeeting_changes_verified': True,
            'intermeeting_check_scope': 'current_archived_change_table_from_first_captured_statement_through_table_receipt'}


def research_baseline(facts: dict) -> dict:
    """Fixed smoothed research prior after full statement/table reconciliation."""
    counts = {slot: 0 for slot in FED_OUTCOME_SLOTS}; eligible = []; excluded = []
    for statement in facts['statements']:
        change = statement.get('enacted_change_bps')
        if change is None:
            excluded.append({'meeting_date': statement['meeting_date'], 'reason': 'no_reconciled_enacted_change'})
        else: eligible.append(statement)
    selected = eligible[-LOOKBACK_EVENTS:]
    for statement in selected: counts[outcome_slot(statement['enacted_change_bps'])] += 1
    probabilities = {slot: (count+SMOOTHING)/(len(selected)+len(counts)*SMOOTHING) for slot, count in counts.items()}
    enough = (len(selected) >= MIN_BASELINE_EVENTS and not facts['missing_past_statement_dates'] and not excluded
              and facts.get('unlisted_intermeeting_changes_verified') is True)
    return {'version': BASELINE_VERSION, 'status': 'research_candidate' if enough else 'unavailable_sample_or_calendar_gap',
            'lookback_events': LOOKBACK_EVENTS, 'minimum_events': MIN_BASELINE_EVENTS, 'smoothing_per_state': SMOOTHING,
            'sample_events': len(selected), 'counts': counts, 'selected_meeting_dates': [s['meeting_date'] for s in selected],
            'excluded': excluded, 'conditioning': None, 'research_state_probabilities': probabilities if enough else None,
            'current_archive_all_scheduled_statements_reconciled': not excluded and not facts['missing_past_statement_dates'],
            'regime_generalization_validated': False, 'production_eligible': False, 'profitability_validated': False}


def contract_context(packet: dict, facts: dict) -> dict:
    """Bind current facts to the next scheduled five-state Fed contract.

    The baseline estimates five states for a future meeting; its no-change state
    includes the contract fallback. It has no demonstrated calibration or edge.
    """
    at = utc(packet['as_of']); rules = normalize(packet['resolution_rules'])
    options = packet['options']
    if (packet['category'] != 'macro' or packet['kind'] != 'exclusive_multiple_choice' or
            len(options) != 5 or set(o['label'] for o in options) != set(FED_OUTCOME_SLOTS) or
            len({o['key'] for o in options}) != 5 or utc(facts['as_of']) > at or utc(packet['end_at']) <= at):
        raise ValueError('Unsupported/unavailable five-state macro context')
    lower_rules = rules.lower()
    if not all(term in lower_rules for term in ('upper bound', 'prior to', 'rounded up to the nearest 25', 'if no statement', 'no change')):
        raise ValueError('Unsupported Fed relative-upper-bound/fallback rules')
    targets = re.findall(r'meeting scheduled for ([A-Za-z]+) (?:\d{1,2}-)?(\d{1,2}), (\d{4})', rules, re.I)
    if len(targets) != 1: raise ValueError('Ambiguous official Fed contract meeting')
    month, day, year = targets[0]
    target = date(int(year), MONTHS[month.capitalize()], int(day)).isoformat()
    available = [s for s in facts['statements'] if utc(s['released_at']) <= at and utc(s['received_at']) <= at]
    if not available or any(d > available[-1]['meeting_date'] for d in facts['missing_past_statement_dates']):
        raise ValueError('No verified latest policy statement at macro cutoff')
    local_day = at.astimezone(ZoneInfo('America/New_York')).date()
    future = [m for m in facts['meetings'] if date.fromisoformat(m['meeting_date']) >= local_day
              and m['meeting_date'] > available[-1]['meeting_date']]
    if not future or future[0]['meeting_date'] != target:
        raise ValueError('Macro baseline supports the next scheduled meeting only')
    latest = available[-1]; baseline = research_baseline(facts)
    return {'version': 'fed-relative-upper-context-v1', 'meeting_date': target,
            'latest_statement_date': latest['meeting_date'], 'latest_announced_lower_bps': latest['lower_bps'],
            'latest_announced_upper_bps': latest['upper_bps'], 'latest_statement_url': latest['url'],
            'reference_semantics': 'upper_bound_immediately_before_target_meeting',
            'current_announced_upper_is_final_premeeting_reference': False,
            'unlisted_intermeeting_changes_verified': facts['unlisted_intermeeting_changes_verified'],
            'rule_rounding': 'ceil_magnitude_to_25_bps',
            'no_statement_fallback': 'no_change_after_next_scheduled_meeting_deadline',
            'state_keys': {o['label']: o['key'] for o in options},
            'historical_frequency_baseline': baseline,
            'contract_state_probabilities': {o['key']: baseline['research_state_probabilities'][o['label']]
                for o in options} if baseline['research_state_probabilities'] else None,
            'profitability_validated': False, 'live_ready': False}


def replay_directory(root: Path) -> dict:
    result = json.loads((root/'result.json').read_text(encoding='utf-8'))
    facts = build_facts(root, utc(result['facts']['as_of']))
    if facts != result['facts'] or research_baseline(facts) != result['research_baseline']:
        raise ValueError('Saved FOMC facts/baseline differ from original responses')
    return {'status': 'replayed_identical', 'responses_verified': len(facts['source_receipts']),
            'statements_verified': len(facts['statements']), 'network_calls': 0, 'model_calls': 0,
            'orders_submitted': 0, 'live_ready': False}


def finalize_capture(root: Path) -> dict:
    """Derive once from complete receipts, recording the actual parser version.

    Can finish a capture whose network requests completed but parsing failed.
    The original capture source/clock remain intact; no requests are repeated.
    """
    project = Path(__file__).resolve().parents[1]
    paths = ('experiments/macro_policy.py', 'polycast/evidence.py', 'polycast/market.py')
    blobs = {name: (project/name).read_bytes() for name in paths}
    manifest = {'protocol': 'fomc-history-derivation-source-v1', 'recorded_at': datetime.now(timezone.utc).isoformat(),
                'files': {name: {'sha256': hashlib.sha256(blob).hexdigest(), 'content_base64': base64.b64encode(blob).decode('ascii')}
                          for name, blob in blobs.items()}}
    import uuid
    derivation_file = 'derivation-source-' + uuid.uuid4().hex + '.json'
    with (root/derivation_file).open('x', encoding='utf-8') as stream: stream.write(encoded(manifest)+'\n')
    facts = build_facts(root, datetime.now(timezone.utc))
    if any((project/name).read_bytes() != blob for name, blob in blobs.items()):
        raise ValueError('FOMC derivation source changed during replay')
    result = {'facts': facts, 'research_baseline': research_baseline(facts),
              'capture_source_manifest_sha256': hashlib.sha256((root/'source-before.json').read_bytes()).hexdigest(),
              'derivation_source_filename': derivation_file,
              'derivation_source_manifest_sha256': hashlib.sha256((root/derivation_file).read_bytes()).hexdigest(),
              'model_calls': 0, 'orders_submitted': 0, 'production_database_written': False}
    with (root/'result.json').open('x', encoding='utf-8') as stream: stream.write(encoded(result)+'\n')
    return result


def collect(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    paths = ('experiments/macro_policy.py', 'polycast/evidence.py')
    project = Path(__file__).resolve().parents[1]
    blobs = {name: (project/name).read_bytes() for name in paths}
    manifest = {'protocol': 'fomc-history-capture-source-v1', 'recorded_at': datetime.now(timezone.utc).isoformat(),
                'files': {name: {'sha256': hashlib.sha256(blob).hexdigest(), 'content_base64': base64.b64encode(blob).decode('ascii')}
                          for name, blob in blobs.items()}}
    with (root/'source-before.json').open('x', encoding='utf-8') as stream:
        stream.write(encoded(manifest)+'\n'); stream.flush()
        import os
        os.fsync(stream.fileno())
    receipt = fetch(root, 'calendar', CALENDAR)
    raw, _ = read_response(root, 'calendar', CALENDAR, utc(receipt['received_at']))
    meetings = parse_calendar(raw)
    linked = [m for m in meetings if m['statement_url'] and date.fromisoformat(m['meeting_date']) <= utc(receipt['received_at']).date()]
    if len(linked) > MAX_STATEMENTS: raise ValueError('FOMC capture exceeds request bound')
    for meeting in linked: fetch(root, meeting['meeting_date'], meeting['statement_url'])
    if any((project/name).read_bytes() != blob for name, blob in blobs.items()):
        raise ValueError('FOMC source changed during capture')
    capture_rate_table(root)
    return finalize_capture(root)


def capture_rate_table(root: Path) -> dict:
    source = Path(__file__).read_bytes()
    before = {'protocol': 'fomc-rate-table-source-before-v1', 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'filename': 'experiments/macro_policy.py', 'sha256': hashlib.sha256(source).hexdigest(),
              'content_base64': base64.b64encode(source).decode('ascii')}
    with (root/'rate-table-source-before.json').open('x', encoding='utf-8') as stream:
        stream.write(encoded(before)+'\n'); stream.flush()
        import os
        os.fsync(stream.fileno())
    receipt = fetch(root, 'rate-table', RATE_TABLE)
    if Path(__file__).read_bytes() != source: raise ValueError('Rate table capture source changed during request')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); result = collect(args.output)
    print(encoded({'status': 'official_policy_history_captured', 'as_of': result['facts']['as_of'],
                   'statements': len(result['facts']['statements']), 'research_baseline': result['research_baseline'],
                   'model_calls': 0, 'orders_submitted': 0}))
