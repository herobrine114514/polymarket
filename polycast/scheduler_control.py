"""Narrow local controls for Polycast's one known Windows Scheduled Task."""
from __future__ import annotations

import os
import subprocess


TASK_NAME = "PolycastPaperCycle"
TASK_NAMES = {"cycle": TASK_NAME, "realtime": "PolycastRealtime"}
CREATE_NO_WINDOW = 0x08000000


def manage_task(action: str, interval_minutes: int | None = None, *, target: str = "cycle") -> None:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler is unavailable")
    if action not in ("enable", "disable", "run", "interval"):
        raise ValueError("Unsupported scheduler action")
    if target not in TASK_NAMES or (target == "realtime" and action == "interval"):
        raise ValueError("Unsupported scheduler target or interval")
    task_name = TASK_NAMES[target]
    if action == "interval" and interval_minutes not in (15, 30, 60):
        raise ValueError("Interval must be 15, 30, or 60 minutes")
    scripts = {
        "enable": f"Enable-ScheduledTask -TaskName '{task_name}' | Out-Null",
        "disable": f"Disable-ScheduledTask -TaskName '{task_name}' | Out-Null",
        "run": (f"$t=Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop;"
                f"if ([string]$t.State -ne 'Running') {{ Start-ScheduledTask -TaskName '{task_name}' }}"
                if target == "realtime" else f"Start-ScheduledTask -TaskName '{task_name}'"),
        "interval": (
            "$t=Get-ScheduledTask -TaskName 'PolycastPaperCycle' -ErrorAction Stop;"
            "$start=[datetimeoffset]::Parse($t.Triggers[0].StartBoundary);"
            "$duration=[System.Xml.XmlConvert]::ToTimeSpan($t.Triggers[0].Repetition.Duration);"
            f"$new=New-ScheduledTaskTrigger -Once -At $start.DateTime -RepetitionInterval "
            f"(New-TimeSpan -Minutes {interval_minutes}) -RepetitionDuration $duration;"
            "Set-ScheduledTask -TaskName 'PolycastPaperCycle' -Trigger $new | Out-Null"
        ),
    }
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop';" + scripts[action]],
        capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or
                            "Task Scheduler command failed")[:300])
    from .dashboard_state import invalidate_task_cache
    invalidate_task_cache()
