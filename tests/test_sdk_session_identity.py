"""Native SDK session envelopes and permission faults without actual authority."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class SdkSessionIdentityTests(unittest.TestCase):
    def test_native_session_wallet_formats_registry_scope_expiry_pause_and_revocation(self):
        root = Path(__file__).resolve().parents[1]
        required = ['.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/package.json',
                    '.polycast/oss-chainlink-sdk/node_modules/ethers/package.json']
        if not shutil.which('node') or not all((root / path).is_file() for path in required):
            self.skipTest('Optional pinned Node SDK research dependencies are not installed')
        options = {'cwd': str(root), 'capture_output': True, 'text': True, 'timeout': 30}
        if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(['node', 'experiments/sdk_session_order_validation.mjs'], **options)
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        report = json.loads(result.stdout)
        self.assertGreaterEqual(report['passed'], 85)
        self.assertEqual(report['synthetic_wallet_cases'], 2)
        self.assertEqual(report['native_network_calls'], 0)
        self.assertEqual(report['wallet_grants_created'], 0)
        self.assertEqual(report['real_orders_submitted'], 0)
        self.assertFalse(report['authenticated_session_authorization_verified'])
        self.assertFalse(report['live_ready'])


if __name__ == '__main__': unittest.main()
