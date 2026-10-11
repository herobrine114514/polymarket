"""One separately registered low-thinking test after the high-cap failure.

Imports the original research harness without changing production source or the
archived high-thinking experiment. Both source hashes and settings are recorded.
"""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments import deepseek_thinking_shadow as engine
from polycast.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch-seconds', type=int, default=620)
    args = parser.parse_args()
    if not 10 <= args.watch_seconds <= 1800:
        parser.error('Observation must be bounded between 10 and 1800 seconds')
    engine.EXPERIMENT = 'btc5m-v4pro-thinking-low-shadow-v1'
    engine.PARAMETERS = {**engine.PARAMETERS, 'reasoning_effort': 'low',
                         'max_tokens': 16000, 'timeout_seconds': 90, 'maximum_calls': 1}
    original_sources = engine.sources
    engine.sources = lambda: {**original_sources(), 'experiments/deepseek_thinking_shadow_low.py':
                               hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    engine.run(Config(data_dir=ROOT/'.polycast'), ROOT/'.polycast/thinking-low-shadow-20261003', args.watch_seconds)


if __name__ == '__main__':
    main()
