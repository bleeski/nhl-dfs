# Register the scheduled pre-lock refresh (backlog B23): ONE Windows scheduled task that wakes every
# poll_minutes during the poll window in config/scheduled_refresh.yaml and runs the dispatcher
# (tools/scheduled_refresh.py --once). The dispatcher refreshes the newest delivered run of each slate at
# T-60 and T-20 before its first lock and shows a toast when a file changes or a goalie check is needed.
#   .\tools\register_refresh_task.ps1              register (idempotent: replaces the task with the same name)
#   .\tools\register_refresh_task.ps1 -DryRun      print what would be registered; change nothing
#   .\tools\register_refresh_task.ps1 -Unregister  remove the task
# The task runs as the current user with an interactive logon (a toast shows only in that session); it
# runs only while you are logged on. Registering is Ben's action.
param([switch]$DryRun, [switch]$Unregister)
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"   # no console window every few minutes
if (-not (Test-Path $Python)) { $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe" }
$Script = Join-Path $RepoRoot "tools\scheduled_refresh.py"
$ConfigPath = Join-Path $RepoRoot "config\scheduled_refresh.yaml"
if (-not (Test-Path $Python)) { throw "No venv python under $RepoRoot\.venv\Scripts." }

$json = & (Join-Path $RepoRoot ".venv\Scripts\python.exe") -c "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8'))))" $ConfigPath
if ($LASTEXITCODE -ne 0) { throw "Could not read $ConfigPath" }
$cfg = $json | ConvertFrom-Json
$name = [string]$cfg.task_name

if ($Unregister) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Output "no task named '$name'"; exit 0 }
    if ($DryRun) { Write-Output "would unregister $name"; exit 0 }
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Output "unregistered $name"
    exit 0
}

$windowsZone = @{ "America/Chicago" = "Central Standard Time"; "America/New_York" = "Eastern Standard Time" }[[string]$cfg.timezone]
if (-not $windowsZone) { throw "Unsupported timezone '$($cfg.timezone)' in $ConfigPath" }
$sourceZone = [System.TimeZoneInfo]::FindSystemTimeZoneById($windowsZone)
$parts = ([string]$cfg.poll_start).Split(":")
$wall = [DateTime]::SpecifyKind((Get-Date).Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1]), [DateTimeKind]::Unspecified)
$local = [System.TimeZoneInfo]::ConvertTime($wall, $sourceZone, [System.TimeZoneInfo]::Local)
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Output "Task:     $name (runs as $user, interactive, only while logged on)"
Write-Output "Action:   `"$Python`" `"$Script`" --once   (working directory $RepoRoot)"
Write-Output ("Schedule: daily from {0} {1} (local {2}) every {3} min for {4} h; refresh at T-{5} before each slate's first lock" -f `
    $cfg.poll_start, $cfg.timezone, $local.ToString("HH:mm"), $cfg.poll_minutes, $cfg.poll_hours, (($cfg.windows_min | ForEach-Object { $_ }) -join " and T-"))
Write-Output "Log:      $RepoRoot\runs\_scheduler\refresh_log.jsonl and notifications.log"

if ($DryRun) {
    Write-Output "Dry run: nothing registered."
    Write-Output "To see what the dispatcher would do right now: .venv\Scripts\python.exe tools\scheduled_refresh.py --dry-run"
    exit 0
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`" --once" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $local
$rep = New-ScheduledTaskTrigger -Once -At $local -RepetitionInterval (New-TimeSpan -Minutes ([int]$cfg.poll_minutes)) `
    -RepetitionDuration (New-TimeSpan -Hours ([int]$cfg.poll_hours))
$trigger.Repetition = $rep.Repetition
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes ([int]$cfg.task_time_limit_minutes))
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "nhl-dfs scheduled pre-lock refresh (backlog B23)" -Force | Out-Null
Write-Output "registered $name"
Write-Output "To remove it: .\tools\register_refresh_task.ps1 -Unregister"
