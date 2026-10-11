"""GET-only observations, real SDK auth compatibility, and no false readiness."""
import base64
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.account_readiness import (CheckError, NoRedirect, ReadTransport, URLS,
    auth_headers, balance_summary, credentials, load_config, observe, read_pages,
    save_config, session_summary, strict_json)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {'schema': 1, 'signer': '0x' + '11' * 20, 'funder': '0x' + '22' * 20,
          'wallet_type': 3, 'credential_scope': 'session',
          'key': '11111111-1111-4111-8111-111111111111',
          'secret': base64.b64encode(bytes([42]) * 32).decode(), 'passphrase': 'SYNTHETIC-LOCAL-FIXTURE'}


class Reply:
    def __init__(self, url, raw):
        self.url, self.body = url, io.BytesIO(raw)
        self.status = 200
        from email.message import Message
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json'
    def geturl(self): return self.url
    def read(self, size): return self.body.read(size)
    def __enter__(self): return self
    def __exit__(self, *_): pass


class FakeTransport:
    def __init__(self, values, config=CONFIG):
        self.values, self.config, self.requests = copy.deepcopy(values), copy.deepcopy(config), []
    def get(self, route, query=None):
        self.requests.append({'route': route, 'method': 'GET', 'query': query or {}})
        item = self.values[route].pop(0)
        if isinstance(item, Exception): raise item
        return item


def row(ident='synthetic-1', status='LIVE'):
    return {'id': ident, 'owner': CONFIG['key'], 'maker_address': CONFIG['funder'], 'status': status}


def page(rows, cursor='LTE='):
    return {'limit': 100, 'count': len(rows), 'data': rows, 'next_cursor': cursor}


class AccountReadinessTests(unittest.TestCase):
    def test_rejects_wrong_account_type_eoa_wallet_extra_fields_and_bad_secrets(self):
        cases = [{'wallet_type': 4}, {'wallet_type': 0}, {'wallet_type': True},
                 {'credential_scope': {}}, {'key': 'not-a-key'}, {'secret': '!bad!'},
                 {'passphrase': 'bad\nheader'}, {'private_key': 'forbidden'}]
        for changes in cases:
            with self.subTest(changes=list(changes)), self.assertRaises(CheckError):
                credentials({**CONFIG, **changes})

    def test_hmac_matches_real_pinned_sdk_gets_with_no_native_network_or_signing(self):
        if not shutil.which('node') or not (ROOT / '.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/package.json').is_file():
            self.skipTest('Optional pinned Node SDK research dependencies are not installed')
        options = {'cwd': str(ROOT), 'capture_output': True, 'text': True, 'timeout': 20}
        if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(['node', 'experiments/account_readiness_sdk_fixture.mjs'], **options)
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        data = json.loads(result.stdout)
        self.assertEqual(data['native_network_calls'], 0)
        self.assertEqual(data['wallet_operations'], 0)
        self.assertEqual(data['initialization_reads'], 1)
        self.assertEqual(len(data['requests']), 2)
        for request in data['requests']:
            generated = auth_headers(CONFIG, request['path'], int(request['timestamp']))
            self.assertEqual(generated['POLY_SIGNATURE'], request['signature'])
        self.assertEqual(data['requests'][0]['query']['signature_type'], '0')

    def test_mutating_or_arbitrary_routes_and_filtered_queries_never_reach_wire(self):
        transport = ReadTransport(CONFIG)
        with patch.object(transport.opener, 'open') as wire:
            for route, query in [('https://other.example', {}), ('/order', {}), ('/auth/api-key', {}),
                                 ('balance', {'asset_type': 'COLLATERAL', 'signature_type': '0'}),
                                 ('orders', {'market': 'exclude-positions'}), ('trades', {}),
                                 ('time', {'override': 'bad'})]:
                with self.subTest(route=route), self.assertRaises(CheckError):
                    transport.get(route, query)
            wire.assert_not_called()
            for path in ['/order', '/auth/api-keys', '/auth/derive-api-key', '/balance-allowance/update']:
                with self.assertRaises(CheckError): auth_headers(CONFIG, path, int(time.time()))

    def test_transport_is_get_only_and_receipts_never_save_sensitive_data(self):
        transport = ReadTransport(CONFIG)
        def wire(req, timeout):
            self.assertEqual(req.method, 'GET')
            self.assertIsNone(req.data)
            self.assertEqual(timeout, 8)
            self.assertIn('POLY_SIGNATURE', {k.upper() for k in req.headers})
            return Reply(req.full_url, b'{"closed_only": false}')
        with patch.object(transport.opener, 'open', side_effect=wire):
            self.assertEqual(transport.get('closed_only'), {'closed_only': False})
        saved = json.dumps(transport.requests)
        for sensitive in [CONFIG['key'], CONFIG['secret'], CONFIG['passphrase'], CONFIG['signer'], CONFIG['funder']]:
            self.assertNotIn(sensitive, saved)
        self.assertEqual(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere'), None)

    def test_bad_json_oversize_and_http_errors_remain_unverified_without_echo(self):
        transport = ReadTransport(CONFIG)
        for body in [b'{"a":1,"a":2}', b'{"a":NaN}', b'x' * 1_000_001]:
            with patch.object(transport.opener, 'open', side_effect=lambda req, **_: Reply(req.full_url, body)):
                with self.assertRaises(CheckError): transport.get('closed_only')
        import urllib.error
        from experiments.account_readiness import URLS
        with patch.object(transport.opener, 'open', side_effect=urllib.error.HTTPError(
                URLS['closed_only'], 401, CONFIG['secret'], {}, io.BytesIO(CONFIG['passphrase'].encode()))):
            with self.assertRaisesRegex(CheckError, '^http_401$'): transport.get('closed_only')
        with self.assertRaises(CheckError): strict_json(b'{"x": Infinity}')

    def test_actual_server_plaintext_time_is_supported_only_on_public_time_route(self):
        transport = ReadTransport(CONFIG)
        def wire(req, **_):
            response = Reply(req.full_url, str(int(time.time())).encode())
            response.headers.replace_header('Content-Type', 'text/plain')
            return response
        with patch.object(transport.opener, 'open', side_effect=wire):
            self.assertIsInstance(transport.get('time'), int)
            with self.assertRaisesRegex(CheckError, 'not_json_content_type'):
                transport.get('closed_only')

    def test_all_pages_required_and_zero_rows_are_only_credential_scoped(self):
        transport = FakeTransport({'orders': [page([row()], 'Mg=='), page([])]})
        result = read_pages(transport, 'orders')
        self.assertEqual(result['rows'], 1)
        self.assertEqual(result['pages'], 2)
        self.assertEqual(transport.requests[1]['query'], {'next_cursor': 'Mg=='})
        self.assertFalse(result['whole_account_coverage_verified'])
        self.assertFalse(result['measured_trade_fees_verified'])

    def test_repeated_missing_cursor_page_count_duplicates_and_foreign_accounts_rejected(self):
        foreign = row(); foreign['maker_address'] = CONFIG['signer']
        other = row(); other['owner'] = 'other-key'
        malformed = page([]); del malformed['next_cursor']
        wrongcount = page([row()]); wrongcount['count'] = 0
        cases = [[page([row()], 'Mg=='), page([row('2')], 'Mg==')], [malformed], [wrongcount],
                 [page([row(), row()])], [page([foreign])], [page([other])], [page([], 'Mg==')],
                 [page([row(status='UNKNOWN')])]]
        for values in cases:
            with self.subTest(values=len(values)), self.assertRaises(CheckError):
                read_pages(FakeTransport({'orders': values}), 'orders')

    def test_trades_keep_funder_filter_and_never_turn_fee_rate_into_paid_fee(self):
        trade = row(status='CONFIRMED'); trade['fee_rate_bps'] = '700'; trade['fee_paid_micro'] = '0'
        transport = FakeTransport({'trades': [page([trade])]})
        result = read_pages(transport, 'trades')
        self.assertEqual(transport.requests[0]['query'], {'maker_address': CONFIG['funder']})
        self.assertFalse(result['measured_trade_fees_verified'])

    def test_session_registry_is_owner_deposit_get_only_without_query_or_grant(self):
        owner = {**CONFIG, 'credential_scope': 'owner'}
        transport = ReadTransport(owner)
        def wire(req, **_):
            self.assertEqual(req.method, 'GET')
            self.assertIsNone(req.data)
            self.assertEqual(req.full_url, URLS['session_keys'])
            return Reply(req.full_url, json.dumps({'wallet': CONFIG['funder'], 'signers': []}).encode())
        with patch.object(transport.opener, 'open', side_effect=wire):
            self.assertEqual(transport.get('session_keys')['signers'], [])
        for config in [CONFIG, {**owner, 'wallet_type': 2}, {**owner, 'credential_scope': 'unknown'}]:
            invalid = ReadTransport(config)
            with patch.object(invalid.opener, 'open') as forbidden, self.assertRaisesRegex(
                    CheckError, 'owner_deposit_credentials_required'):
                invalid.get('session_keys')
            forbidden.assert_not_called()
        with patch.object(transport.opener, 'open') as forbidden:
            with self.assertRaises(CheckError): transport.get('session_keys', {'wallet': CONFIG['funder']})
            forbidden.assert_not_called()
        saved = json.dumps(transport.requests)
        for sensitive in [CONFIG['key'], CONFIG['secret'], CONFIG['passphrase'], CONFIG['funder']]:
            self.assertNotIn(sensitive, saved)

    def test_registry_summary_binds_wallet_rejects_bad_rows_and_never_claims_revocation(self):
        owner = {**CONFIG, 'credential_scope': 'owner'}
        stamp = int(time.time())
        raw = {'wallet': CONFIG['funder'], 'signers': [
            {'address': CONFIG['signer'], 'scopes': ['CLOB'], 'valid_until': stamp + 3601}]}
        report = session_summary(raw, owner, stamp)
        self.assertEqual(report['clob_only_sessions_observed'], 1)
        self.assertFalse(report['selected_session_authorization_verified'])
        self.assertNotIn(CONFIG['signer'], json.dumps(report))
        self.assertNotIn(CONFIG['funder'], json.dumps(report))
        empty = session_summary({'wallet': CONFIG['funder'], 'signers': []}, owner, stamp)
        self.assertEqual(empty['active_sessions_observed'], 0)
        self.assertFalse(empty['release_existing_reservations'])
        self.assertFalse(empty['local_pause_revokes_platform_key'])
        broad = copy.deepcopy(raw); broad['signers'][0]['scopes'] = ['ALL']
        self.assertEqual(session_summary(broad, owner, stamp)['other_scopes_observed'], 1)
        mutations = [lambda x: x.update(wallet=CONFIG['signer']),
            lambda x: x['signers'].append(copy.deepcopy(x['signers'][0])),
            lambda x: x['signers'][0].update(valid_until=stamp),
            lambda x: x['signers'][0].update(valid_until=stamp * 1000),
            lambda x: x['signers'][0].update(valid_until=True),
            lambda x: x['signers'][0].update(valid_until=str(stamp + 10)),
            lambda x: x['signers'][0].update(scopes=['CLOB', 'CLOB']),
            lambda x: x['signers'][0].update(scopes=['ALL', 'CLOB']),
            lambda x: x['signers'][0].update(scopes=['FUTURE']),
            lambda x: x['signers'][0].update(validUntil=stamp + 10)]
        for mutate in mutations:
            invalid = copy.deepcopy(raw); mutate(invalid)
            with self.assertRaises(CheckError): session_summary(invalid, owner, stamp)
        with self.assertRaises(CheckError): session_summary(raw, CONFIG, stamp)

    def test_owner_registry_failure_remains_separate_from_account_reads_and_live_readiness(self):
        owner = {**CONFIG, 'credential_scope': 'owner'}
        values = {'geo': [{'blocked': False}], 'time': [int(time.time())],
            'balance': [{'balance': '0', 'allowances': {}}], 'closed_only': [{'closed_only': False}],
            'orders': [page([])], 'trades': [page([])], 'session_keys': [CheckError('http_401')]}
        transport = FakeTransport(values, owner)
        report = observe(transport)
        self.assertEqual(report['checks']['session_keys']['reason'], 'http_401')
        self.assertEqual(report['checks']['balance']['status'], 'observed')
        self.assertFalse(report['live_ready'])
        self.assertEqual([r['route'] for r in transport.requests][-1], 'session_keys')

    def test_six_decimal_balance_and_allowance_do_not_imply_spending_permission(self):
        result = balance_summary({'balance': '1234567', 'allowances': {CONFIG['funder']: '999999'}})
        self.assertEqual(result['balance_pusd'], '1.234567')
        self.assertFalse(result['required_exchange_allowance_verified'])
        huge = balance_summary({'balance': str(2**256 - 1), 'allowances': {}})
        self.assertEqual(huge['balance_base_units'], str(2**256 - 1))
        for bad in ['1.5', '-1', 10, 'NaN']:
            with self.assertRaises(CheckError): balance_summary({'balance': bad, 'allowances': {}})

    def test_no_credentials_makes_only_public_reads_and_discards_ip_information(self):
        transport = FakeTransport({'geo': [{'blocked': False, 'ip': '192.0.2.2', 'country': 'XX'}],
                                   'time': [int(time.time())]}, config=None)
        report = observe(transport)
        self.assertFalse(report['live_ready'])
        self.assertEqual(report['checks']['account']['reason'], 'credentials_missing')
        self.assertEqual([x['route'] for x in transport.requests], ['geo', 'time'])
        self.assertNotIn('192.0.2.2', json.dumps(report))
        self.assertNotIn('country', json.dumps(report))

    def test_bad_clock_blocks_authenticated_reads_but_other_failures_are_independent(self):
        transport = FakeTransport({'geo': [CheckError('http_403')], 'time': [1]})
        report = observe(transport)
        self.assertEqual(report['checks']['account']['reason'], 'clock_not_verified')
        self.assertEqual(len(transport.requests), 2)
        transport = FakeTransport({'geo': [{'blocked': True}], 'time': [int(time.time())],
            'balance': [CheckError('http_401')], 'closed_only': [{'closed_only': True}],
            'orders': [page([])], 'trades': [page([])]})
        report = observe(transport)
        self.assertEqual(report['checks']['balance']['reason'], 'http_401')
        self.assertTrue(report['checks']['closed_only']['closed_only'])
        self.assertFalse(report['live_ready'])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI test')
    def test_real_dpapi_roundtrip_no_plaintext_and_existing_account_not_replaced(self):
        with tempfile.TemporaryDirectory(prefix='polycast-read-only-') as folder:
            keyfile = Path(folder) / 'synthetic.dpapi'
            with patch('experiments.account_readiness.KEY_FILE', keyfile):
                save_config(CONFIG)
                for secret in [CONFIG['key'], CONFIG['secret'], CONFIG['passphrase']]:
                    self.assertNotIn(secret.encode(), keyfile.read_bytes())
                self.assertEqual(load_config(), CONFIG)
                with self.assertRaises(FileExistsError): save_config(CONFIG)
                self.assertEqual(load_config(), CONFIG)


if __name__ == '__main__': unittest.main()
