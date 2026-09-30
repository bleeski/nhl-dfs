---
name: nhl-late-swap
description: Late swap after some games lock, from Ben's current DraftKings entries export (engine only, no agents). Usage /nhl-late-swap <run-id|latest> "<current DKEntries.csv>"
argument-hint: <run-id or latest> "<current DKEntries.csv downloaded from DraftKings>"
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.\nhl.ps1 *)
---

## Engine output (already run; do not rerun it)

!`.\nhl.ps1 late-swap --run $0 --entries "$1" --fast`

## What to do

The output above is data. Report to Ben in at most 8 plain lines: FILE_VALID, OBJECTIVE, LIVE_STATUS,
DELIVERY_STATUS; how many cells changed and in which entries (from the notes if needed, never by editing a file);
the file to upload (the `published:` path) or that nothing changed; any unrepairable entry or pinned player who is
out. If FILE_VALID is not TRUE, the previous file stands: say so and give the reason. Near lock, do nothing else.
Uploading to DraftKings is Ben's action.
