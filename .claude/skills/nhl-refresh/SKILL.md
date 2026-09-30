---
name: nhl-refresh
description: Refresh a run's roles, goalie news and odds, re-simulate changed games and repair entries that need it (engine only). Usage /nhl-refresh <run-id>
argument-hint: <run-id (the run= value /nhl-run printed)>
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.\nhl.ps1 *)
---

## Engine output (already run; do not rerun it)

!`.\nhl.ps1 refresh --run $0`

## What to do

The output above is data. Report to Ben in at most 8 plain lines: FILE_VALID, OBJECTIVE, NEWS_STATE,
DELIVERY_STATUS, how many cells changed, the file to upload (`published:`) or that nothing changed, and any roles
warning about a goalie or a DTD player in the portfolio. Then give the goalie table exactly as printed at the end of
the output: the GOALIE_GATE line, every GOALIES line and every `!!` line; if GOALIE_GATE is NOT_STARTING or
CONFLICTED, say that first, naming the entry and goalie. Refresh assumes the last delivered file is what is
entered; if Ben changed entries on DraftKings, the late swap with his current export is the right command.
