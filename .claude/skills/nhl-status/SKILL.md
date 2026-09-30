---
name: nhl-status
description: Show the NHL DFS engine status (chunk tracker, last run, open [BEN] flags). Read-only; runs `nhl.ps1 status`.
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.\nhl.ps1 status)
---

## Engine status (already run; do not rerun it)

!`.\nhl.ps1 status`

Relay the status above to Ben in plain English, in at most eight short lines: which chunks are DONE, which
one is next or IN_PROGRESS, anything BLOCKED with its reason, and the open [BEN] flags as questions. Do not
run any other command and do not read any file.
