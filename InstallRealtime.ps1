param([switch]$DoNotStart)
$ErrorActionPreference = 'Stop'
$runtimeWorkspace = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runtimeWrapper = Join-Path $runtimeWorkspace 'polycast_realtime.pyw'
if (-not (Test-Path -LiteralPath $runtimeWrapper -PathType Leaf)) { throw 'Realtime launcher is missing.' }
$runtimePython = (Get-Command pythonw.exe -ErrorAction Stop).Source
$runtimeArguments = '"' + $runtimeWrapper + '"'
$runtimeExisting = Get-ScheduledTask -TaskName 'PolycastRealtime' -ErrorAction SilentlyContinue
if ($runtimeExisting -and ($runtimeExisting.Actions.Count -ne 1 -or
    $runtimeExisting.Actions[0].Execute -ne $runtimePython -or
    $runtimeExisting.Actions[0].Arguments -ne $runtimeArguments -or
    $runtimeExisting.Actions[0].WorkingDirectory -ne $runtimeWorkspace)) {
    throw 'PolycastRealtime already belongs to a different launcher; it was not changed.'
}
$runtimeUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$runtimeAction = New-ScheduledTaskAction -Execute $runtimePython -Argument $runtimeArguments -WorkingDirectory $runtimeWorkspace
$runtimeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $runtimeUser
$runtimeKeepAlive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$runtimePrincipal = New-ScheduledTaskPrincipal -UserId $runtimeUser -LogonType Interactive -RunLevel Limited
$runtimeSettings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'PolycastRealtime' -Action $runtimeAction -Trigger @($runtimeTrigger,$runtimeKeepAlive) -Principal $runtimePrincipal -Settings $runtimeSettings -Description 'Windowless Polycast paper research; current-user logon plus minute recovery trigger, IgnoreNew, no live orders.' -Force | Out-Null
$runtimeData = Join-Path $runtimeWorkspace '.polycast'
New-Item -ItemType Directory -Path $runtimeData -Force | Out-Null
@{task_name='PolycastRealtime';workspace=$runtimeWorkspace;pythonw=$runtimePython;wrapper=$runtimeWrapper;keep_alive_minutes=1} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeData 'realtime-task.json') -Encoding utf8
if (-not $DoNotStart) { Start-ScheduledTask -TaskName 'PolycastRealtime' }
[pscustomobject]@{task_name='PolycastRealtime';installed=$true;hidden=$true;current_user_logon=$true;started=(-not $DoNotStart)} | ConvertTo-Json
