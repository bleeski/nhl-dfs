# Register one Windows scheduled task per capture time in config/capture.yaml.
#   .\tools\register_capture_task.ps1            register (idempotent: replaces tasks with the same prefix)
#   .\tools\register_capture_task.ps1 -DryRun    print what would be registered; change nothing
#   .\tools\register_capture_task.ps1 -Unregister  remove every task with the configured prefix
# Times in the config are wall-clock in its timezone (America/Chicago) and are
# converted to this machine's local time for the trigger.
param([switch]$DryRun, [switch]$Unregister)
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Capture = Join-Path $RepoRoot "tools\capture.py"
$ConfigPath = Join-Path $RepoRoot "config\capture.yaml"
if (-not (Test-Path $Python)) { throw "No venv python at $Python. Run 'uv sync' first." }

$json = & $Python -c "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8'))))" $ConfigPath
if ($LASTEXITCODE -ne 0) { throw "Could not read $ConfigPath" }
$cfg = $json | ConvertFrom-Json
$prefix = [string]$cfg.task_name_prefix

$windowsZone = @{ "America/Chicago" = "Central Standard Time"; "America/New_York" = "Eastern Standard Time" }[[string]$cfg.timezone]
if (-not $windowsZone) { throw "Unsupported timezone '$($cfg.timezone)' in $ConfigPath" }
$sourceZone = [System.TimeZoneInfo]::FindSystemTimeZoneById($windowsZone)

$unregisterHint = "To remove every capture task: .\tools\register_capture_task.ps1 -Unregister   (or: Get-ScheduledTask -TaskName '$prefix-*' | Unregister-ScheduledTask -Confirm:`$false)"

if ($Unregister) {
    $existing = @(Get-ScheduledTask -TaskName "$prefix-*" -ErrorAction SilentlyContinue)
    foreach ($t in $existing) {
        if ($DryRun) { Write-Output "would unregister $($t.TaskName)" }
        else { Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false; Write-Output "unregistered $($t.TaskName)" }
    }
    Write-Output "$($existing.Count) task(s) matched prefix '$prefix'."
    exit 0
}

$today = (Get-Date).Date
$planned = @()
foreach ($hhmm in $cfg.times) {
    $parts = ([string]$hhmm).Split(":")
    $wall = $today.AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
    $wall = [DateTime]::SpecifyKind($wall, [DateTimeKind]::Unspecified)
    $local = [System.TimeZoneInfo]::ConvertTime($wall, $sourceZone, [System.TimeZoneInfo]::Local)
    $planned += [pscustomobject]@{ Name = "$prefix-$($parts[0])$($parts[1])"; Config = $hhmm; Local = $local.ToString("HH:mm"); At = $local }
}

Write-Output "Repo:     $RepoRoot"
Write-Output "Action:   `"$Python`" `"$Capture`" --once   (working directory $RepoRoot)"
Write-Output "Timezone: $($cfg.timezone) -> local $([System.TimeZoneInfo]::Local.Id)"
foreach ($p in $planned) { Write-Output ("  {0}  {1} {2} -> local {3}" -f $p.Name, $p.Config, $cfg.timezone, $p.Local) }

if ($DryRun) {
    Write-Output "Dry run: nothing registered. $($planned.Count) task(s) would be created or replaced."
    Write-Output $unregisterHint
    exit 0
}

$wanted = @($planned | ForEach-Object { $_.Name })
foreach ($t in @(Get-ScheduledTask -TaskName "$prefix-*" -ErrorAction SilentlyContinue)) {
    if ($wanted -notcontains $t.TaskName) { Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false; Write-Output "removed stale $($t.TaskName)" }
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes ([int]$cfg.task_time_limit_minutes))
foreach ($p in $planned) {
    $action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Capture`" --once" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $p.At
    Register-ScheduledTask -TaskName $p.Name -Action $action -Trigger $trigger -Settings $settings `
        -Description "nhl-dfs prospective capture ($($p.Config) $($cfg.timezone))" -Force | Out-Null
    Write-Output "registered $($p.Name) at $($p.Local) local"
}
Write-Output $unregisterHint
