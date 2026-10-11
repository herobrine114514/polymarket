"""Run bounded offline fault scenarios and save an auditable test result."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_execution_replay import ExecutionReplayTests


class Results(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.successes = []

    def addSuccess(self,test):
        self.successes.append(test.id())
        super().addSuccess(test)


def main():
    started = datetime.now(timezone.utc).isoformat()
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=Results).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ExecutionReplayTests))
    report = {"started_at":started,"completed_at":datetime.now(timezone.utc).isoformat(),
        "scope":"isolated_synthetic_buy_taker_journal", "network_calls":0,"orders_submitted":0,
        "production_database_written":False,"authenticated_adapter_tested":False,
        "live_execution_gate_passed":False,"tests_run":result.testsRun,
        "passed":result.successes,"failed":[test.id() for test,_ in result.failures+result.errors],
        "source_sha256":{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__).resolve(),ROOT/'experiments/execution_replay.py',ROOT/'tests/test_execution_replay.py']},
        "test_output":stream.getvalue(),
        "limitations":["Synthetic receipts only; no real authenticated REST/WebSocket or signed order hash tested",
            "BUY taker FOK/FAK only; fee_paid_micro is an enriched synthetic account receipt, not an SDK field",
            "Dedicated account reconciliation only; six-decimal cost units are an experimental convention",
            "FAULT has no automatic recovery; manual authenticated recovery remains unimplemented"]}
    output = ROOT/'.polycast/execution-replay-checks-20261002.json'
    with output.open('x',encoding='utf-8') as file:
        json.dump(report,file,ensure_ascii=False,indent=2)
    print(json.dumps({"tests":result.testsRun,"passed":len(result.successes),"failed":len(report['failed']),
                      "output":str(output)},ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
