---
name: nhl-settle
description: Settle a slate from DraftKings standings (not built yet; chunk C11 is BLOCKED until standings exports exist). Usage /nhl-settle <run-id> "<standings file>"
argument-hint: <run-id> "<standings export>"
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.\nhl.ps1 status)
---

Settlement is chunk C11, which is BLOCKED until at least one Classic and one Showdown DraftKings standings export
are saved in data/standings/inbox/. Tell Ben that in one line, and that he can save tonight's standings export
there after the contests finish. Do not run any command.
