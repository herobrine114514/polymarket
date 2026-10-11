"""Native signed order, frozen risk and restart must refer to the same packet."""
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from experiments.execution_replay import ExecutionReplay, ReplayError
from experiments.sdk_packet_journal import recover_signed_packet, reserve_signed_packet, validate_packet

ROOT = Path(__file__).resolve().parents[1]


class SdkPacketJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = ['.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/package.json',
                    '.polycast/oss-chainlink-sdk/node_modules/ethers/package.json']
        if not shutil.which('node') or not all((ROOT / path).is_file() for path in required):
            raise unittest.SkipTest('Optional pinned Node SDK research dependencies are not installed')
        options = {'cwd': str(ROOT), 'capture_output': True, 'text': True, 'timeout': 20}
        if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(['node', 'experiments/sdk_packet_fixture.mjs', '--emit'], **options)
        if result.returncode:
            raise AssertionError('\n'.join(result.stderr.splitlines()[-8:])[-1000:])
        cls.packets = json.loads(result.stdout)['packets']

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='polycast-packet-')
        self.path = Path(self.folder.name) / 'signed-replay.sqlite3'
        self.replay = ExecutionReplay.create(self.path)
        self.packet = copy.deepcopy(self.packets[0])
        self.instant = self.packet['context']['nowMs']

    def tearDown(self): self.folder.cleanup()

    def test_ten_native_wallet_domain_packets_distinguish_contract_hash_from_deposit_signing_digest(self):
        self.assertEqual(len(self.packets), 10)
        self.assertEqual(len({p['hashes']['contract_order_hash'] for p in self.packets}), 10)
        for packet in self.packets:
            if packet['context']['intent']['signatureType'] == 3:
                self.assertNotEqual(packet['hashes']['contract_order_hash'], packet['hashes']['signing_digest'])
            self.assertFalse(packet['hashes']['server_order_identity_verified'])
            self.assertFalse(packet['can_submit'])

    def test_packet_and_reservation_commit_together_and_restart_does_not_resign_or_resubmit(self):
        state = reserve_signed_packet(self.replay, 'reserve-native', self.packet, checked_at_ms=self.instant+10)
        intent_id = self.packet['reservation']['intent_id']
        self.assertEqual(state['risk']['held_micro'], 2687360)
        self.replay.command('send-attempt-marker', 'begin_submit', {'intent_id': intent_id})
        recovered = recover_signed_packet(ExecutionReplay.open(self.path), intent_id)
        self.assertEqual(recovered['signed_packet'], self.packet)
        self.assertEqual(recovered['phase'], 'SUBMITTING')
        self.assertFalse(recovered['may_automatically_resubmit'])
        self.assertFalse(recovered['can_submit'])
        with self.assertRaises(ReplayError):
            self.replay.command('retry-new-marker', 'begin_submit', {'intent_id': intent_id})
        self.assertEqual(self.replay.snapshot()['risk']['held_micro'], 2687360)

    def test_signature_price_hash_reserve_and_scope_tampering_never_create_journal_rows(self):
        for field in ['hash', 'price', 'reserve', 'signature', 'scope']:
            packet = copy.deepcopy(self.packet)
            if field == 'hash': packet['hashes']['contract_order_hash'] = '0x'+'00'*32
            if field == 'price': packet['context']['intent']['priceMicro'] = '530000'
            if field == 'reserve': packet['reservation']['reserve_micro'] = 1
            if field == 'signature': packet['context']['prepared']['order']['signature'] = '0x'+'00'*65
            if field == 'scope': packet['scope'] = 'actual-account'
            with self.subTest(field=field), self.assertRaises(ReplayError):
                reserve_signed_packet(self.replay, field, packet, checked_at_ms=self.instant+10)
        self.assertEqual(self.replay.snapshot()['seq'], 0)
        self.assertEqual(self.replay.snapshot()['risk']['held_micro'], 0)

    def test_stale_or_future_packet_does_not_reserve_capital(self):
        for instant in [self.instant-1, self.instant+5001]:
            with self.assertRaises(ReplayError): validate_packet(self.packet, checked_at_ms=instant)
        self.assertEqual(self.replay.snapshot()['seq'], 0)

    def test_journal_content_request_chain_config_and_tail_corruption_fail_before_recovery(self):
        for case in ['event', 'request', 'chain', 'config', 'tail']:
            path = Path(self.folder.name) / (case+'.sqlite3')
            replay = ExecutionReplay.create(path)
            reserve_signed_packet(replay, 'freeze', self.packet, checked_at_ms=self.instant+10)
            replay.command('external-marker', 'begin_submit', {'intent_id': self.packet['reservation']['intent_id']})
            with closing(sqlite3.connect(path)) as db, db:
                if case == 'event': db.execute("UPDATE journal SET event_json='{}' WHERE seq=2")
                if case == 'request': db.execute("UPDATE journal SET request_json='{}' WHERE seq=2")
                if case == 'chain': db.execute("UPDATE journal SET chain_digest='changed' WHERE seq=2")
                if case == 'config': db.execute("UPDATE config SET value='{}'")
                if case == 'tail': db.execute('DELETE FROM journal WHERE seq=2')
            with self.subTest(case=case), self.assertRaises(ReplayError): ExecutionReplay.open(path)

    def test_legacy_replay_is_not_silently_rewritten_or_given_integrity_credit(self):
        path = Path(self.folder.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE config(value TEXT NOT NULL)'); db.execute("INSERT INTO config VALUES ('{}')")
        before = path.read_bytes()
        with self.assertRaisesRegex(ReplayError, 'Legacy replay integrity unverified'): ExecutionReplay.open(path)
        self.assertEqual(path.read_bytes(), before)
