"""Bounded, GET-only Polymarket account observations; never a trading grant.

Direct documented REST reads deliberately avoid secure-client initialization,
which may create/derive credentials or deploy a wallet. Existing L2 credentials
may themselves permit trading; this tool's transport only permits five reads.
Credentials enter through an interactive local prompt and Windows DPAPI only.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = ROOT / '.polycast/polymarket-read.dpapi'
REPORT_FILE = ROOT / '.polycast/account-readiness-latest.json'
URLS = {
    'geo': 'https://polymarket.com/api/geoblock',
    'time': 'https://clob.polymarket.com/time',
    'balance': 'https://clob.polymarket.com/balance-allowance',
    'closed_only': 'https://clob.polymarket.com/auth/ban-status/closed-only',
    'orders': 'https://clob.polymarket.com/data/orders',
    'trades': 'https://clob.polymarket.com/data/trades',
    'session_keys': 'https://clob.polymarket.com/v1/user/session-signers',
}
PRIVATE = {'balance', 'closed_only', 'orders', 'trades', 'session_keys'}
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')
MAX_BODY = 1_000_000
MAX_PAGES = 20
MAX_ITEMS = 2_000
END_CURSOR = 'LTE='


class CheckError(Exception):
    """Only fixed, non-sensitive reason codes may reach a saved report."""


def require(condition, code):
    if not condition:
        raise CheckError(code)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def address(value):
    require(isinstance(value, str) and bool(ADDRESS.fullmatch(value))
            and value.lower() != '0x' + '0' * 40, 'invalid_address')
    return value.lower()


def bounded_ascii(value, code):
    require(isinstance(value, str) and 0 < len(value) <= 256
            and all(32 < ord(c) < 127 for c in value), code)
    return value


def credentials(value):
    require(isinstance(value, dict) and set(value) == {
        'schema', 'signer', 'funder', 'wallet_type', 'credential_scope',
        'key', 'secret', 'passphrase'}, 'invalid_config_schema')
    require(value['schema'] == 1, 'invalid_config_schema')
    signer, funder = address(value['signer']), address(value['funder'])
    require(type(value['wallet_type']) is int and value['wallet_type'] in range(4), 'invalid_wallet_type')
    require(value['wallet_type'] != 0 or signer == funder, 'eoa_funder_mismatch')
    require(isinstance(value['credential_scope'], str)
            and value['credential_scope'] in {'owner', 'session', 'unknown'}, 'invalid_credential_scope')
    key = bounded_ascii(value['key'], 'invalid_api_key')
    try:
        require(str(UUID(key)) == key.lower(), 'invalid_api_key')
    except (ValueError, AttributeError):
        raise CheckError('invalid_api_key') from None
    secret = bounded_ascii(value['secret'], 'invalid_api_secret')
    try:
        decoded = base64.b64decode(secret, altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise CheckError('invalid_api_secret') from None
    require(16 <= len(decoded) <= 128, 'invalid_api_secret')
    bounded_ascii(value['passphrase'], 'invalid_passphrase')
    return {**value, 'signer': signer, 'funder': funder, 'key': key.lower()}


def identity_hash(value):
    return hashlib.sha256(json.dumps({k: value[k] for k in (
        'signer', 'funder', 'wallet_type', 'credential_scope')},
        sort_keys=True).encode()).hexdigest()


def auth_headers(value, path, timestamp):
    value = credentials(value)
    require(path in {urllib.parse.urlsplit(URLS[k]).path for k in PRIVATE}, 'route_not_allowed')
    if path == '/v1/user/session-signers':
        require(value['wallet_type'] == 3 and value['credential_scope'] == 'owner',
                'owner_deposit_credentials_required')
    require(type(timestamp) is int and timestamp > 0, 'invalid_request_time')
    # Query parameters are not part of the documented L2 signed route.
    secret = base64.b64decode(value['secret'], altchars=b'-_', validate=True)
    signed = hmac.new(secret, (str(timestamp) + 'GET' + path).encode(), hashlib.sha256).digest()
    return {'POLY_ADDRESS': value['signer'], 'POLY_API_KEY': value['key'],
            'POLY_PASSPHRASE': value['passphrase'], 'POLY_TIMESTAMP': str(timestamp),
            'POLY_SIGNATURE': base64.urlsafe_b64encode(signed).decode()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def invalid_number(_):
        raise CheckError('invalid_json_number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_number)
    except (ValueError, UnicodeError):
        raise CheckError('invalid_json') from None


class ReadTransport:
    """No arbitrary URL, method, headers, body, redirects, or credential refresh."""
    def __init__(self, config=None):
        self.config = credentials(config) if config is not None else None
        self.opener = urllib.request.build_opener(NoRedirect())
        self.requests = []

    def get(self, route, query=None):
        require(route in URLS, 'route_not_allowed')
        query = dict(query or {})
        if route in {'geo', 'time', 'closed_only', 'session_keys'}:
            require(not query, 'query_not_allowed')
        elif route == 'balance':
            require(self.config is not None and query == {
                'asset_type': 'COLLATERAL', 'signature_type': str(self.config['wallet_type'])},
                'query_not_allowed')
        else:
            allowed = {'next_cursor'} | ({'maker_address'} if route == 'trades' else set())
            require(set(query) <= allowed, 'query_not_allowed')
            if route == 'trades':
                require(self.config is not None and query.get('maker_address') == self.config['funder'],
                        'query_not_allowed')
            if 'next_cursor' in query:
                bounded_ascii(query['next_cursor'], 'invalid_cursor')
        url = URLS[route]
        headers = {'Accept': 'application/json', 'User-Agent': 'Polycast-Readiness/1'}
        if route in PRIVATE:
            require(self.config is not None, 'credentials_missing')
            headers.update(auth_headers(self.config, urllib.parse.urlsplit(url).path, int(time.time())))
        if query:
            url += '?' + urllib.parse.urlencode(query)
        start = time.perf_counter()
        receipt = {'route': route, 'method': 'GET', 'started_at': now(), 'authenticated': route in PRIVATE}
        self.requests.append(receipt)
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers, method='GET'), timeout=8) as response:
                require(response.status == 200 and response.geturl() == url, 'unexpected_response_route')
                content_types = {'application/json', 'text/plain'} if route == 'time' else {'application/json'}
                require(response.headers.get_content_type() in content_types, 'not_json_content_type')
                body = response.read(MAX_BODY + 1)
                require(len(body) <= MAX_BODY, 'response_too_large')
                value = strict_json(body)
        except CheckError as exc:
            receipt['status'] = str(exc)
            raise
        except urllib.error.HTTPError as exc:
            receipt['status'] = 'http_' + str(exc.code)
            raise CheckError(receipt['status']) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            receipt['status'] = 'transport_failed'
            raise CheckError('transport_failed') from None
        finally:
            receipt['finished_at'] = now()
            receipt['elapsed_seconds'] = round(time.perf_counter() - start, 6)
        receipt['status'] = 'received'
        # Never persist response bodies, IPs, keys, addresses, queries, or headers.
        return value


def uint(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'[0-9]{1,78}', value)), 'invalid_base_units')
    return int(value)


def balance_summary(raw):
    require(isinstance(raw, dict) and isinstance(raw.get('allowances'), dict), 'invalid_balance_response')
    cash = uint(raw.get('balance'))
    allowances = raw['allowances']
    values = []
    for spender, amount in allowances.items():
        address(spender)
        values.append(uint(amount))
    # Integer arithmetic preserves precision even for a uint256-sized balance.
    return {'balance_base_units': str(cash), 'collateral': 'pUSD', 'decimals': 6,
            'balance_pusd': f'{cash // 1_000_000}.{cash % 1_000_000:06d}',
            'positive_balance': cash > 0, 'allowance_spenders_observed': len(values),
            'some_allowance_positive': any(v > 0 for v in values),
            'required_exchange_allowance_verified': False}


def session_summary(raw, config, observed_unix):
    """Redacted owner-registry shape; never a grant or a journal release."""
    value = credentials(config)
    require(value['wallet_type'] == 3 and value['credential_scope'] == 'owner',
            'owner_deposit_credentials_required')
    require(type(observed_unix) is int and observed_unix > 0, 'invalid_observation_time')
    require(isinstance(raw, dict) and set(raw) == {'wallet', 'signers'}, 'invalid_session_response')
    require(address(raw['wallet']) == value['funder'], 'session_registry_wallet_mismatch')
    rows = raw['signers']
    require(isinstance(rows, list) and len(rows) <= MAX_ITEMS, 'invalid_session_list')
    identifiers, clob_only, broad, soon = set(), 0, 0, 0
    for entry in rows:
        require(isinstance(entry, dict) and set(entry) == {'address', 'scopes', 'valid_until'},
                'invalid_session_row')
        ident = address(entry['address'])
        require(ident not in identifiers, 'duplicate_session_signer')
        identifiers.add(ident)
        scopes = entry['scopes']
        require(isinstance(scopes, list) and 0 < len(scopes) <= 3
                and all(isinstance(s, str) and s in {'CLOB', 'COMBOSRFQ', 'ALL'} for s in scopes),
                'invalid_session_scopes')
        require(len(set(scopes)) == len(scopes)
                and ('ALL' not in scopes or len(scopes) == 1), 'invalid_session_scopes')
        expiry = entry['valid_until']
        require(type(expiry) is int and observed_unix < expiry < 100_000_000_000,
                'invalid_active_session_expiry')
        clob_only += scopes == ['CLOB']
        broad += scopes != ['CLOB']
        soon += expiry - observed_unix < 3600
    return {'declared_wallet_binding_matches': True, 'active_sessions_observed': len(rows),
            'clob_only_sessions_observed': clob_only, 'other_scopes_observed': broad,
            'sessions_expiring_within_hour': soon, 'selected_session_authorization_verified': False,
            'onchain_authorization_verified': False, 'release_existing_reservations': False,
            'local_pause_revokes_platform_key': False}


def read_pages(transport, route):
    require(route in {'orders', 'trades'}, 'route_not_allowed')
    cursor, seen, identifiers, count, statuses = None, set(), set(), 0, {}
    for page in range(MAX_PAGES):
        query = {'maker_address': transport.config['funder']} if route == 'trades' else {}
        if cursor is not None:
            query['next_cursor'] = cursor
        raw = transport.get(route, query)
        require(isinstance(raw, dict) and isinstance(raw.get('data'), list), 'invalid_page')
        items = raw['data']
        require(type(raw.get('count')) is int and raw['count'] == len(items)
                and type(raw.get('limit')) is int and 0 < raw['limit'] <= MAX_ITEMS
                and len(items) <= raw['limit'], 'invalid_page_count')
        cursor = bounded_ascii(raw.get('next_cursor'), 'missing_page_cursor')
        for item in items:
            require(isinstance(item, dict), 'invalid_account_row')
            ident = bounded_ascii(item.get('id'), 'missing_row_id')
            require(ident not in identifiers, 'duplicate_account_row')
            require(item.get('owner') == transport.config['key'], 'row_credential_mismatch')
            require(address(item.get('maker_address')) == transport.config['funder'], 'row_funder_mismatch')
            status = item.get('status')
            allowed = {'LIVE', 'MATCHED', 'DELAYED', 'UNMATCHED', 'CANCELED'} if route == 'orders' else {
                'MATCHED', 'MINED', 'CONFIRMED', 'RETRYING', 'FAILED'}
            require(status in allowed, 'unknown_account_status')
            identifiers.add(ident)
            statuses[status] = statuses.get(status, 0) + 1
        count += len(items)
        require(count <= MAX_ITEMS, 'account_item_limit')
        if cursor == END_CURSOR:
            return {'credential_scoped_pagination_complete': True, 'pages': page + 1,
                    'rows': count, 'status_counts': statuses,
                    'whole_account_coverage_verified': False,
                    'measured_trade_fees_verified': False}
        require(items and cursor not in seen, 'stalled_or_repeated_cursor')
        seen.add(cursor)
    raise CheckError('account_page_limit')


def observe(transport):
    report = {'schema': 1, 'observed_at': now(), 'scope': 'GET-only-account-observation',
              'mode': 'paper-only', 'checks': {}, 'orders_submitted': 0, 'wallet_signatures': 0,
              'credentials_created': 0, 'live_ready': False,
              'blockers': ['forecast_advantage_not_validated', 'signed_reference_unavailable',
                           'wallet_mapping_not_independently_verified', 'whole_account_coverage_unverified',
                           'positions_and_actual_fees_unverified', 'live_grant_not_requested']}
    def check(name, fn):
        try:
            report['checks'][name] = {'status': 'observed', **fn()}
        except CheckError as exc:
            report['checks'][name] = {'status': 'unverified', 'reason': str(exc)}
    def geo():
        raw = transport.get('geo')
        require(isinstance(raw, dict) and type(raw.get('blocked')) is bool, 'invalid_geo_response')
        # Do not store identifying IP/country/region. This is an egress observation only.
        return {'blocked_for_current_egress': raw['blocked'], 'account_eligibility_verified': False}
    def clock():
        start = time.time()
        server = transport.get('time')
        end = time.time()
        require(type(server) is int and server > 0, 'invalid_server_time')
        skew = abs(server - (start + end) / 2)
        return {'server_time': server, 'roundtrip_seconds': round(end - start, 6),
                'estimated_clock_skew_seconds': round(skew, 6),
                'within_five_seconds': skew <= 5 and end - start <= 5}
    check('geoblock', geo)
    check('clock', clock)
    if transport.config is None:
        report['blockers'].append('credentials_missing')
        report['checks']['account'] = {'status': 'unverified', 'reason': 'credentials_missing'}
    else:
        report['declared_identity_sha256'] = identity_hash(transport.config)
        report['declared_wallet_type'] = transport.config['wallet_type']
        report['declared_credential_scope'] = transport.config['credential_scope']
        c = report['checks']['clock']
        if c.get('within_five_seconds') is not True:
            report['checks']['account'] = {'status': 'unverified', 'reason': 'clock_not_verified'}
        else:
            check('balance', lambda: balance_summary(transport.get('balance', {
                'asset_type': 'COLLATERAL', 'signature_type': str(transport.config['wallet_type'])})))
            def closed():
                raw = transport.get('closed_only')
                require(isinstance(raw, dict) and type(raw.get('closed_only')) is bool, 'invalid_closed_only_response')
                return {'closed_only': raw['closed_only']}
            check('closed_only', closed)
            check('open_orders', lambda: read_pages(transport, 'orders'))
            check('account_trades', lambda: read_pages(transport, 'trades'))
            if transport.config['wallet_type'] == 3 and transport.config['credential_scope'] == 'owner':
                check('session_keys', lambda: session_summary(transport.get('session_keys'),
                      transport.config, int(time.time())))
            else:
                report['checks']['session_keys'] = {'status': 'unverified',
                    'reason': 'owner_deposit_credentials_required'}
    report['requests'] = transport.requests
    report['completed_at'] = now()
    return report


def protect(raw, decrypt=False):
    require(os.name == 'nt', 'windows_dpapi_required')
    from polycast.provider import _protect
    try:
        return _protect(raw, decrypt=decrypt)
    except Exception:
        raise CheckError('dpapi_failed') from None


def save_config(value):
    value = credentials(value)
    encrypted = protect(json.dumps(value).encode())
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Never silently replace an existing account binding.
    with KEY_FILE.open('xb') as f:
        f.write(encrypted)


def load_config():
    if not KEY_FILE.exists():
        return None
    try:
        require(KEY_FILE.stat().st_size < 100_000, 'config_too_large')
        return credentials(strict_json(protect(KEY_FILE.read_bytes(), decrypt=True)))
    except OSError:
        raise CheckError('config_unreadable') from None


def main():
    parser = argparse.ArgumentParser(description='仅查询账户；默认未配置时只检查公共网络与时钟，不申请钱包权限。')
    parser.add_argument('command', choices=['check', 'configure', 'status'])
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            require(sys.stdin.isatty(), 'interactive_local_terminal_required')
            require(not KEY_FILE.exists(), 'account_already_configured')
            print('只输入已有 CLOB L2 凭据；不要输入私钥、助记词、Relayer 或 Builder 密钥。')
            print('此工具仅查询，L2 凭据本身可能拥有交易权限。所有输入隐藏，DPAPI 加密保存在本机。')
            value = {'schema': 1, 'signer': getpass.getpass('Signer 地址: '),
                     'funder': getpass.getpass('账户钱包/Funder 地址: '),
                     'wallet_type': int(getpass.getpass('钱包类型 0 EOA / 1 Proxy / 2 Safe / 3 Deposit: ')),
                     'credential_scope': getpass.getpass('凭据范围 owner / session / unknown: '),
                     'key': getpass.getpass('CLOB API key: '), 'secret': getpass.getpass('CLOB secret: '),
                     'passphrase': getpass.getpass('CLOB passphrase: ')}
            save_config(value)
            print(json.dumps({'configured': True, 'encrypted_at_rest': True, 'orders_submitted': 0}))
        elif args.command == 'status':
            value = load_config()
            print(json.dumps({'configured': value is not None, 'live_ready': False,
                              'declared_identity_sha256': identity_hash(value) if value else None}))
        else:
            report = observe(ReadTransport(load_config()))
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
    except (CheckError, ValueError, FileExistsError) as exc:
        code = str(exc) if isinstance(exc, CheckError) else 'local_configuration_failed'
        print(json.dumps({'status': 'unverified', 'reason': code, 'live_ready': False}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
