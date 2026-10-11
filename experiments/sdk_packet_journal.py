"""Atomic native SDK synthetic signed-packet reservation; no external transport.

The pinned Node validator recomputes the signature, contract hash, serialization,
scope and amount constraints. Its result is not a live submission grant.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from experiments.account_readiness import CheckError, strict_json
from experiments.execution_replay import ExecutionReplay, ReplayError, require, text_id

ROOT = Path(__file__).resolve().parents[1]


def validate_packet(packet: dict, *, checked_at_ms: int) -> dict:
    require(type(checked_at_ms) is int and checked_at_ms > 0, 'Invalid packet check time')
    try:
        raw = json.dumps({'packet': packet, 'checked_at_ms': checked_at_ms}, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReplayError('Malformed signed packet') from exc
    require(len(raw.encode()) <= 100_000, 'Signed packet too large')
    options = {'cwd': str(ROOT), 'capture_output': True, 'text': True,
               'input': raw, 'timeout': 15}
    if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(['node', str(ROOT / 'experiments/sdk_order_packet.mjs'), '--verify'], **options)
        require(result.returncode == 0, 'Signed packet validation rejected')
        require(len(result.stdout) <= 20_000, 'Validator reply too large')
        verified = strict_json(result.stdout)
        require(verified.get('scope') == 'synthetic-packet-validation-only'
                and verified.get('can_submit') is False
                and verified.get('server_order_identity_verified') is False,
                'Unexpected packet validation scope')
        require(verified['packet_sha256'] == packet['packet_sha256'], 'Packet identity changed')
        return verified
    except (OSError, subprocess.TimeoutExpired, CheckError, KeyError, TypeError) as exc:
        raise ReplayError('Signed packet validation unavailable') from exc


def reserve_signed_packet(replay: ExecutionReplay, event_id: str, packet: dict, *, checked_at_ms: int) -> dict:
    text_id(event_id)
    verified = validate_packet(packet, checked_at_ms=checked_at_ms)
    # The full signed packet and reservation are one event in the same FULL-sync
    # SQLite transaction. No separate file/row can commit an unmatched intent.
    return replay.command(event_id, 'reserve', {**verified['reservation'],
        'signed_packet': packet, 'signed_packet_sha256': verified['packet_sha256'],
        'contract_order_hash': verified['contract_order_hash'],
        'server_order_identity_verified': False, 'can_submit': False})


def recover_signed_packet(replay: ExecutionReplay, intent_id: str) -> dict:
    state = replay.snapshot()  # Verifies digests, chain and committed head first.
    require(intent_id in state['intents'], 'Unknown signed intent')
    intent = state['intents'][intent_id]
    require('signed_packet' in intent, 'Legacy intent has no native signed packet')
    packet = intent['signed_packet']
    verified = validate_packet(packet, checked_at_ms=packet['context']['nowMs'])
    require(verified['reservation']['expected_order_id'] == intent['expected_order_id']
            and verified['packet_sha256'] == intent['signed_packet_sha256'], 'Stored signed intent mismatch')
    return {'intent_id': intent_id, 'phase': intent['phase'], 'signed_packet': packet,
            'contract_order_hash': intent['contract_order_hash'], 'can_submit': False,
            'may_automatically_resubmit': False, 'release_existing_reservations': False,
            'new_submission_requires_fresh_context': True}
