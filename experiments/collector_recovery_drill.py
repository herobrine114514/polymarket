"""Bounded real public collector recovery in an isolated directory; no AI or DB."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from polycast.auto5m import AutoFiveMinute
from polycast.collector_env import node_collector_env
from polycast.config import Config


def main():
    output=ROOT/'.polycast/collector-recovery-drill-20261002'
    output.mkdir(exist_ok=False)
    worker=AutoFiveMinute(Config(data_dir=output))
    started=datetime.now(timezone.utc)
    clock=time.monotonic()
    allowed={'path','systemroot','windir','temp','tmp','userprofile','appdata','localappdata',
             'programfiles','programfiles(x86)'}
    direct={k:v for k,v in os.environ.items() if k.lower() in allowed}
    proxied={k:v for k,v in node_collector_env().items() if k.lower() in allowed or k.lower() in
        {'https_proxy','http_proxy','no_proxy','node_use_env_proxy','node_use_system_ca'}}
    report={'started_at':started.isoformat(),'scope':'isolated_public_collector_only',
            'orders_submitted':0,'model_calls':0,'production_database_written':False,
            'proxy_values_stored':False,'real_clock':True,'initial_proxy_configured':False,
            'recovery_proxy_configured':any(k.lower()=='https_proxy' for k in proxied)}
    try:
        with patch('polycast.auto5m.node_collector_env',return_value=direct):
            worker._ensure_collector()
        first=worker._collector;report['first_pid']=first.pid
        while time.monotonic()-clock<46:time.sleep(.5)
        tick=worker._last_collector_tick_ms()
        report.update(initial_process_alive=first.poll() is None,initial_tick_ms=tick,
                      observed_wait_seconds=time.monotonic()-clock)
        if first.poll() is not None or tick is not None:
            report['status']='failure_not_reproduced'
        else:
            with patch('polycast.auto5m.node_collector_env',return_value=proxied):
                worker._ensure_collector()
            report.update(recovery_pid=worker._collector.pid,old_exit_code=first.poll(),
                          recoveries=worker._state['collector_recoveries'])
            recovery_clock=time.monotonic()
            while time.monotonic()-recovery_clock<18:
                tick=worker._last_collector_tick_ms()
                if tick and 0<=time.time()-tick/1000<=30:break
                time.sleep(.5)
            report['recovery_seconds']=time.monotonic()-recovery_clock
            report['fresh_tick_ms']=tick
            report['status']='passed' if (first.poll() is not None and worker._collector.pid!=first.pid and
                report['recoveries']==1 and tick and 0<=time.time()-tick/1000<=30) else 'failed'
    finally:
        worker.stop()
        if worker._collector:
            worker._collector.wait(timeout=5)
        report['completed_at']=datetime.now(timezone.utc).isoformat()
        with (output/'result.json').open('x',encoding='utf-8') as file:json.dump(report,file,indent=2)
    print(json.dumps(report))
    return 0 if report.get('status')=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
